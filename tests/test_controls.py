import atexit
import json
import threading
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import listen as app

atexit.unregister(app.cleanup)


class ControlsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.station_file = Path(directory.name) / 'radio_station.txt'
        patcher = patch.object(app, 'RADIO_STATION_FILE', self.station_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ir_codes_file = Path(directory.name) / 'ir_codes.json'
        patcher = patch.object(app, 'IR_CODES_FILE', self.ir_codes_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.spotify_sources_file = Path(directory.name) / 'spotify_sources.json'
        patcher = patch.object(app, 'SPOTIFY_SOURCES_FILE', self.spotify_sources_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.spotify_selection_file = Path(directory.name) / 'spotify_selection.json'
        patcher = patch.object(app, 'SPOTIFY_SELECTION_FILE', self.spotify_selection_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        app.selected_station_number = 1
        original_stations = app.stations
        self.addCleanup(setattr, app, 'stations', original_stations)
        app.shutdown_event.clear()
        app.radio_process = None
        app.radio_pgid = None
        app.current_station = None
        app.record_process = None
        app.writer_thread = None
        app.record_led = Mock()
        app.upload_process = None
        app.upload_last_failed = False
        app.radio_error = None
        app.keyboard_error = None
        while not app.upload_queue.empty():
            app.upload_queue.get_nowait()
            app.upload_queue.task_done()
        app.ir_state = app.IR_STATE_IDLE
        app.ir_process = None
        app.ir_pgid = None
        app.ir_learn_generation = 0
        app.ir_learn_timer = None
        app.ir_error = None
        app.ir_codes = {}
        app.ir_next_number = 1
        app.ir_rx_led = Mock()
        app.ir_tx_led = Mock()
        app.spotify_sources = []
        app.spotify_current_source = None
        app.spotify_now_playing = None
        app.spotify_error = None
        app.spotify_generation = 0
        app.spotify_access_token = None
        app.spotify_token_expiry = 0.0
        app.spotify_device_id = None
        app.spotify_configured_warned = False
        self.schedules_file = Path(directory.name) / 'schedules.json'
        patcher = patch.object(app, 'SCHEDULES_FILE', self.schedules_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        app.schedules = {}
        app.schedule_next_id = 1
        app.scheduler_last_checked = None
        patcher = patch.multiple(app, SPOTIFY_CLIENT_ID='client-id', SPOTIFY_CLIENT_SECRET='secret',
                                  SPOTIFY_REFRESH_TOKEN='refresh-token')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bluetooth_profile_only_when_connected(self):
        for connected in (True, False):
            with self.subTest(connected=connected), patch.object(app.subprocess, 'run') as run:
                run.return_value.stdout = 'Device 9D:C6:55:EC:74:D5\n\tConnected: ' + ('yes' if connected else 'no') + '\n'
                app.configure_bluetooth_audio()
                self.assertEqual(run.call_count, 2 if connected else 1)
                self.assertEqual(run.call_args_list[0].args[0],
                                 ['bluetoothctl', 'info', '9D:C6:55:EC:74:D5'])
                if connected:
                    self.assertEqual(run.call_args.args[0], [
                        'pactl', 'set-card-profile', 'bluez_card.9D_C6_55_EC_74_D5',
                        'a2dp-sink'])
                for call in run.call_args_list:
                    self.assertEqual(call.kwargs['timeout'], 3)
                    self.assertTrue(call.kwargs['check'])

    def test_bluetooth_failures_do_not_interrupt_startup(self):
        for error in (FileNotFoundError('missing command'),
                      app.subprocess.CalledProcessError(1, 'command'),
                      app.subprocess.TimeoutExpired('command', 3)):
            for connected in (False, True):
                results = [Mock(stdout='Connected: yes\n'), error] if connected else [error]
                with self.subTest(error=type(error), connected=connected), \
                     patch.object(app.subprocess, 'run', side_effect=results) as run, \
                     self.assertLogs(app.log, level='WARNING'):
                    app.configure_bluetooth_audio()
                self.assertEqual(run.call_count, 2 if connected else 1)
                self.assertFalse(app.shutdown_event.is_set())

    def test_radio_resume_cycle_and_emergency_stop(self):
        configured = app.stations[:4]
        for key, expected in ((1, [3, 4, 1, 2, 3]), (5, [3, 2, 1, 4, 3])):
            with self.subTest(key=key), \
                 patch.object(app, 'load_stations', return_value=configured), \
                 patch.object(app.subprocess, 'Popen', side_effect=lambda *a, **k: Mock(
                     pid=101, stdout=None, poll=Mock(return_value=None))), \
                 patch.object(app, '_terminate_group'):
                app.radio_process = None
                self.station_file.write_text('3\n')
                app.selected_station_number = app.load_station_number()
                for number in expected:
                    app.handle_keyboard_key(key)
                    self.assertEqual(app.current_station['number'], number)
                    self.assertEqual(self.station_file.read_text(), f'{number}\n')
                with patch.object(app, 'save_station_number') as save:
                    app.handle_keyboard_key(8)
                    save.assert_not_called()
                self.assertIsNone(app.radio_process)
                self.assertEqual(app.selected_station_number, 3)
                self.assertEqual(self.station_file.read_text(), '3\n')
                app.handle_keyboard_key(key)
                self.assertEqual(app.current_station['number'], 3)

    def test_web_radio_same_station_still_stops(self):
        process = Mock(pid=101, stdout=None, poll=Mock(return_value=None))
        with patch.object(app.subprocess, 'Popen', return_value=process), \
             patch.object(app, '_terminate_group'):
            app.start_radio(app.stations[2])
            app.start_radio(app.stations[2])
        self.assertIsNone(app.radio_process)
        self.assertEqual(app.selected_station_number, 3)
        self.assertEqual(self.station_file.read_text(), '3\n')

    def test_single_station_does_not_toggle_off(self):
        process = Mock(pid=101, stdout=None, poll=Mock(return_value=None))
        with patch.object(app, 'load_stations', return_value=app.stations[:1]), \
             patch.object(app.subprocess, 'Popen', return_value=process) as spawn:
            for key in (1, 1, 5):
                app.handle_keyboard_key(key)
                self.assertIs(app.radio_process, process)
            spawn.assert_called_once()

    def test_exited_radio_resumes_without_advancing(self):
        app.selected_station_number = 3
        app.current_station = app.stations[2]
        app.radio_process = Mock(pid=100, stdout=None, poll=Mock(return_value=1))
        with patch.object(app, '_terminate_group'), \
             patch.object(app.subprocess, 'Popen', return_value=Mock(pid=101)):
            app.handle_keyboard_key(1)
        self.assertEqual(app.current_station['number'], 3)

    def test_mp3_shares_player_and_preserves_station(self):
        process = Mock(pid=123, stdout=None, poll=Mock(return_value=None))
        with patch.object(app.subprocess, 'Popen', return_value=process) as spawn, \
             patch.object(app, '_terminate_group') as stop:
            app.start_radio(app.stations[2])
            self.assertTrue(app.start_mp3()[0])
            stop.assert_called_once_with(process, 123)
            self.assertEqual(spawn.call_args.args[0], [
                'bash', str(app.MP3_SCRIPT), app.GDRIVE_DIR.rstrip('/') + '/04-アクセル.mp3'])
            self.assertEqual(app.selected_station_number, 3)
            self.assertEqual(self.station_file.read_text(), '3\n')
            self.assertEqual(app._oled_lines()[1], 'MP3 LOOP')
            with patch.object(app, 'get_volume', return_value=50):
                status = app.get_status()
                self.assertTrue(status['mp3'])
                self.assertFalse(status['radio'])
            app.start_mp3()
            self.assertEqual(spawn.call_count, 2)
            app.handle_keyboard_key(5)
            self.assertEqual(app.current_station['number'], 3)
            self.assertEqual(stop.call_count, 2)

    def test_mp3_emergency_and_record_start_stop_playback(self):
        for action in ('emergency', 'record'):
            with self.subTest(action=action), \
                 patch.object(app.subprocess, 'Popen', return_value=Mock(
                     pid=123, stdout=None, poll=Mock(return_value=None))), \
                 patch.object(app, '_terminate_group') as stop, \
                 patch.object(app, '_open_file'), patch.object(app, 'SAVE_DIR'), \
                 patch.object(app.threading, 'Thread'):
                app.record_process = None
                app.start_mp3()
                if action == 'emergency':
                    app.handle_keyboard_key(8)
                else:
                    app.handle_keyboard_key(4)
                stop.assert_called_once()
                self.assertIsNone(app.radio_process)

    def test_mp3_recording_exclusion_and_web_controls(self):
        client = app.app.test_client()
        with patch.object(app.subprocess, 'Popen', return_value=Mock(
                pid=123, stdout=None, poll=Mock(return_value=None))) as spawn, \
             patch.object(app, '_terminate_group'):
            app.record_process = Mock(poll=Mock(return_value=None))
            self.assertEqual(client.post('/api/mp3/start').status_code, 409)
            spawn.assert_not_called()
            app.record_process = None
            self.assertEqual(client.post('/api/mp3/start').status_code, 200)
            self.assertTrue(client.post('/api/mp3/stop').json['stopped'])
            app.start_radio(app.stations[0])
            self.assertFalse(client.post('/api/mp3/stop').json['stopped'])
            self.assertIsNotNone(app.radio_process)

    def test_mp3_missing_file_is_reported_without_crash(self):
        process = Mock(pid=123, poll=Mock(return_value=1))
        process.stdout.read.return_value = 'MP3を取得できません: missing file'.encode()
        with patch.object(app.subprocess, 'Popen', return_value=process), \
             patch.object(app, '_terminate_group'), \
             patch.object(app, 'get_volume', return_value=50), \
             self.assertLogs(app.log, level='ERROR') as logs:
            app.start_mp3()
            status = app.get_status()
        self.assertFalse(status['mp3'])
        self.assertIn('MP3を取得できません', status['radio_error'])
        self.assertIn('MP3異常終了', logs.output[0])

    def test_record_stops_radio_before_start(self):
        order = []
        with patch.object(app, '_stop_radio_locked', side_effect=lambda: order.append('stop')), \
             patch.object(app, '_open_file'), patch.object(app, 'SAVE_DIR'), \
             patch.object(app.threading, 'Thread'), \
             patch.object(app.subprocess, 'Popen', side_effect=lambda *a, **k: order.append('record') or Mock()):
            self.assertTrue(app.start_recording()[0])
        self.assertEqual(order, ['stop', 'record'])

    def test_recording_starts_without_ssd(self):
        """SSDの有無は録音可否に影響しない。保存先はローカルのSAVE_DIR。"""
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app, 'SAVE_DIR', Path(directory) / 'recordings'), \
             patch.object(app.os.path, 'ismount', return_value=False), \
             patch.object(app, '_stop_radio_locked'), \
             patch.object(app.threading, 'Thread'), \
             patch.object(app.subprocess, 'Popen', return_value=Mock()) as spawn:
            ok, message = app.start_recording()
            self.assertTrue(ok, message)
            spawn.assert_called_once()
            self.assertEqual(app.current_filepath.parent, Path(directory) / 'recordings')
            app.wav_file.close()
            app.wav_file = None
            app.current_filepath = None

    def test_upload_success_and_failure_keep_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            filepath = Path(directory) / 'rec.wav'
            filepath.write_bytes(b'data')
            for code in (1, 0):
                with self.subTest(code=code):
                    app.upload_queue.put((filepath, app.upload_generation))
                    app.upload_queue.put(None)
                    with patch.object(app.subprocess, 'Popen', return_value=Mock(
                            wait=Mock(return_value=code), poll=Mock(return_value=code))) as spawn, \
                         self.assertLogs(app.log, level='INFO'):
                        app.upload_worker()
                    self.assertEqual(spawn.call_args[0][0],
                                     ['rclone', 'copyto', str(filepath), f'{app.GDRIVE_DIR}/rec.wav'])
                    self.assertTrue(filepath.exists())
                    self.assertEqual(app.upload_last_failed, bool(code))
                    self.assertEqual(app._upload_status_text(),
                                     'GDRIVE UPLOAD ERR' if code else 'SAVE: SD -> GDRIVE')

    def test_upload_status_text_while_uploading(self):
        app.upload_queue.put(('rec.wav', app.upload_generation))
        self.assertEqual(app._upload_status_text(), 'GDRIVE UPLOADING')
        self.assertEqual(app._oled_lines()[3], 'GDRIVE UPLOADING')

    def test_record_toggle_and_unused(self):
        with patch.object(app, 'start_recording') as start, patch.object(app, 'stop_recording') as stop:
            app.handle_keyboard_key(4)
            start.assert_called_once()
            app.record_process = Mock()
            app.record_process.poll.return_value = None
            app.handle_keyboard_key(4)
            stop.assert_called_once()
            for number in (2, 3, 6, 7):
                app.handle_keyboard_key(number)
            self.assertEqual(start.call_count, 1)
            self.assertEqual(stop.call_count, 1)

    def test_emergency_cancels_uploads_and_returns_to_standby(self):
        app.upload_process = Mock(pid=123)
        generation = app.upload_generation
        with patch.object(app, 'stop_recording', side_effect=lambda: app.upload_queue.put(('file', generation))), \
             patch.object(app, '_terminate_group') as terminate:
            app.handle_keyboard_key(8)
        terminate.assert_called_once_with(app.upload_process, 123)
        self.assertTrue(app.upload_queue.empty())
        self.assertEqual(app.upload_queue.unfinished_tasks, 0)
        self.assertEqual(app.upload_generation, generation + 1)
        self.assertFalse(app.shutdown_event.is_set())
        self.assertEqual(app._oled_lines()[1], 'STANDBY')

    def test_dequeued_old_upload_is_skipped(self):
        app.upload_queue.put(('cancelled.wav', app.upload_generation - 1))
        app.upload_queue.put(None)
        with patch.object(app.subprocess, 'Popen') as spawn:
            app.upload_worker()
        spawn.assert_not_called()
        self.assertEqual(app.upload_queue.unfinished_tasks, 0)

    def test_keyboard_ctrl_and_repeat(self):
        codes = SimpleNamespace(EV_KEY=1, KEY_LEFTCTRL=29, KEY_RIGHTCTRL=97,
                                **{f'KEY_{n}': n + 1 for n in range(1, 9)})
        keyboard = app.KeyboardShortcuts(codes)
        def key(code, value):
            keyboard.feed(SimpleNamespace(type=1, code=code, value=value))
        with patch.object(app, 'handle_keyboard_key') as handle:
            key(codes.KEY_1, 1)
            handle.assert_not_called()
            key(codes.KEY_1, 0)
            for ctrl in (codes.KEY_LEFTCTRL, codes.KEY_RIGHTCTRL):
                key(ctrl, 1)
                for n in range(1, 9):
                    code = getattr(codes, f'KEY_{n}')
                    key(code, 1)
                    key(code, 2)
                    key(code, 1)
                    key(code, 0)
                key(ctrl, 0)
            self.assertEqual([c.args[0] for c in handle.call_args_list], list(range(1, 9)) * 2)

    def test_named_device_discovery_ignores_other_keyboards(self):
        from evdev import ecodes
        for event_number in (7, 19):
            with self.subTest(event_number=event_number):
                app.shutdown_event.clear()
                target = Mock()
                target.name = 'aki4722 akisan08'
                target.path = f'/dev/input/event{event_number}'
                target.capabilities.return_value = {
                    ecodes.EV_KEY: [ecodes.KEY_1, ecodes.KEY_LEFTCTRL]
                }
                target.active_keys.return_value = []
                other = Mock()
                other.name = 'Other keyboard'
                other.path = '/dev/input/event99'
                def ready(devices, *_):
                    self.assertEqual(devices, [target])
                    app.shutdown_event.set()
                    return [target], [], []
                target.read.return_value = [
                    SimpleNamespace(type=ecodes.EV_KEY, code=code, value=value)
                    for code, value in [(ecodes.KEY_LEFTCTRL, 1), (ecodes.KEY_1, 1),
                                        (ecodes.KEY_1, 2), (ecodes.KEY_1, 0)]
                ]
                with patch('evdev.list_devices', return_value=[other.path, target.path]), \
                     patch('evdev.InputDevice', side_effect=[other, target]), \
                     patch.object(app.select, 'select', side_effect=ready), \
                     patch.object(app, 'handle_keyboard_key') as handle:
                    app.keyboard_worker()
                handle.assert_called_once_with(1)
                other.capabilities.assert_not_called()
                other.close.assert_called_once()
                target.close.assert_called_once()

    def test_ctrl_release_and_digit_before_ctrl_do_not_trigger(self):
        from evdev import ecodes
        keyboard = app.KeyboardShortcuts(ecodes)
        with patch.object(app, 'handle_keyboard_key') as handle:
            for code, value in [(ecodes.KEY_1, 1), (ecodes.KEY_LEFTCTRL, 1),
                                (ecodes.KEY_1, 2), (ecodes.KEY_LEFTCTRL, 0),
                                (ecodes.KEY_2, 1)]:
                keyboard.feed(SimpleNamespace(type=ecodes.EV_KEY, code=code, value=value))
        handle.assert_not_called()

    def test_saved_station_validation(self):
        self.assertEqual(app.load_station_number(), 1)
        for value in ('', 'invalid', '0', '-1', '999', '2.5', '2\n3', '3'):
            self.station_file.write_text(value)
            self.assertEqual(app.load_station_number(), 3 if value == '3' else 1)
        self.station_file.write_bytes(b'\xff')
        self.assertEqual(app.load_station_number(), 1)

    def test_unused_keys_have_no_radio_action(self):
        with patch.object(app, 'start_radio') as start, \
             patch.object(app, 'stop_radio') as stop, \
             patch.object(app, 'load_stations') as load, \
             patch.object(app, 'start_spotify') as spotify_start:
            for key in (3, 7):
                app.handle_keyboard_key(key)
            start.assert_not_called()
            stop.assert_not_called()
            load.assert_not_called()
            spotify_start.assert_not_called()
        self.assertFalse(self.station_file.exists())

    def test_station_config_error_preserves_playback(self):
        existing = Mock(poll=Mock(return_value=None))
        app.radio_process = existing
        app.current_station = app.stations[0]
        with patch.object(app, 'load_stations', side_effect=ValueError('invalid')), \
             patch.object(app, 'start_radio') as start, \
             self.assertLogs(app.log, level='ERROR'):
            app.handle_keyboard_key(1)
        start.assert_not_called()
        self.assertIs(app.radio_process, existing)
        self.assertEqual(app._oled_lines()[2], 'CTRL1 CONFIG')

    def test_failed_start_does_not_change_selection(self):
        app.selected_station_number = 3
        self.station_file.write_text('3\n')
        with patch.object(app.subprocess, 'Popen', side_effect=OSError('failed')), \
             self.assertLogs(app.log, level='ERROR'):
            self.assertFalse(app.start_radio(app.stations[0])[0])
        self.assertEqual(app.selected_station_number, 3)
        self.assertEqual(self.station_file.read_text(), '3\n')

    def test_station_config_url_and_name(self):
        with tempfile.TemporaryDirectory() as directory:
            station_file = Path(directory) / 'stations.conf'
            station_file.write_text('[4]\nname = 日本語の局\nurl = https://example.com/live?a=20%25\n')
            with patch.object(app, 'STATIONS_FILE', station_file):
                station = app.load_stations()[0]
            self.assertEqual(station['number'], 4)
            self.assertEqual(app._oled_station_name(station), '日本語の局')
            with patch.object(app.subprocess, 'Popen') as spawn:
                app.start_radio(station)
            self.assertEqual(spawn.call_args.args[0][-1], station['url'])

    def test_radio_oled_and_recording_exclusion(self):
        for index in range(3):
            app.radio_process = Mock()
            app.radio_process.poll.return_value = None
            app.current_station = app.stations[index]
            self.assertEqual(app._oled_lines()[1], 'RADIO PLAYING')
            self.assertEqual(app._oled_lines()[2], app.stations[index]['name'])
        app.record_process = Mock()
        app.record_process.poll.return_value = None
        with patch.object(app.subprocess, 'Popen') as spawn:
            self.assertFalse(app.start_radio(app.stations[0])[0])
        spawn.assert_not_called()

    # ---- IR remote（学習・送信）----

    def test_ir_learn_start_spawns_worker_and_sets_state(self):
        process = Mock(pid=777)
        with patch.object(app.subprocess, 'Popen', return_value=process) as spawn, \
             patch.object(app.threading, 'Thread') as thread_cls, \
             patch.object(app.threading, 'Timer') as timer_cls:
            ok, message = app.start_ir_learning()
        self.assertTrue(ok)
        self.assertEqual(app.ir_state, app.IR_STATE_RECEIVING)
        app.ir_rx_led.on.assert_called_once()
        spawn.assert_called_once_with(
            ['ir-ctl', '-d', app.IR_RX_DEVICE, '--receive', '--mode2', '--one-shot'],
            stdout=app.subprocess.PIPE, stderr=app.subprocess.PIPE, start_new_session=True)
        thread_cls.assert_called_once()
        self.assertEqual(thread_cls.call_args.kwargs['args'], (process, 0))
        timer_cls.assert_called_once_with(app.IR_LEARN_TIMEOUT_SECONDS, app.cancel_ir_learning)

    def test_ir_learn_start_rejects_when_not_idle(self):
        for state in (app.IR_STATE_RECEIVING, app.IR_STATE_TRANSMITTING):
            with self.subTest(state=state):
                app.ir_state = state
                with patch.object(app.subprocess, 'Popen') as spawn:
                    ok, message = app.start_ir_learning()
                self.assertFalse(ok)
                spawn.assert_not_called()

    def test_ir_learn_cancel_stops_process_and_resets_state(self):
        process = Mock(pid=555)
        app.ir_state = app.IR_STATE_RECEIVING
        app.ir_process = process
        app.ir_pgid = 555
        app.ir_learn_generation = 3
        timer = Mock()
        app.ir_learn_timer = timer
        with patch.object(app, '_terminate_group') as terminate:
            result = app.cancel_ir_learning()
        self.assertTrue(result)
        terminate.assert_called_once_with(process, 555)
        self.assertEqual(app.ir_state, app.IR_STATE_IDLE)
        self.assertEqual(app.ir_learn_generation, 4)
        app.ir_rx_led.off.assert_called_once()
        timer.cancel.assert_called_once()
        self.assertIsNone(app.ir_learn_timer)

    def test_ir_learn_cancel_when_idle_is_noop(self):
        with patch.object(app, '_terminate_group') as terminate:
            result = app.cancel_ir_learning()
        self.assertFalse(result)
        terminate.assert_not_called()
        app.ir_rx_led.off.assert_not_called()

    def test_ir_learn_worker_success(self):
        process = Mock(pid=555, returncode=0)
        process.communicate.return_value = (b'pulse 9024\nspace 4512\npulse 620\n', b'')
        with self.assertLogs(app.log, level='INFO'):
            app._ir_learn_worker(process, app.ir_learn_generation)
        self.assertEqual(app.ir_state, app.IR_STATE_IDLE)
        self.assertEqual(app.ir_next_number, 2)
        self.assertEqual(app.ir_codes[1]['name'], 'リモコン1')
        self.assertEqual(app.ir_codes[1]['signal'], ['pulse 9024', 'space 4512', 'pulse 620'])
        app.ir_rx_led.off.assert_called_once()
        saved = json.loads(self.ir_codes_file.read_text())
        self.assertEqual(saved['codes']['1']['name'], 'リモコン1')
        self.assertEqual(saved['next_number'], 2)

    def test_ir_learn_worker_error_without_signal(self):
        process = Mock(pid=555, returncode=1)
        process.communicate.return_value = (b'', b'ir-ctl: no data\n')
        with self.assertLogs(app.log, level='ERROR'):
            app._ir_learn_worker(process, app.ir_learn_generation)
        self.assertEqual(app.ir_state, app.IR_STATE_IDLE)
        self.assertEqual(app.ir_codes, {})
        self.assertIsNotNone(app.ir_error)
        app.ir_rx_led.off.assert_called_once()
        self.assertFalse(self.ir_codes_file.exists())

    def test_ir_learn_worker_discards_stale_generation(self):
        process = Mock(pid=555, returncode=0)
        process.communicate.return_value = (b'pulse 900\nspace 800\n', b'')
        app.ir_learn_generation = 5
        app._ir_learn_worker(process, 4)
        self.assertEqual(app.ir_codes, {})
        app.ir_rx_led.off.assert_not_called()

    def test_rename_ir_code(self):
        app.ir_codes = {1: {'name': 'リモコン1', 'created_at': 'x', 'signal': ['pulse 1', 'space 1']}}
        ok, message = app.rename_ir_code(1, 'エアコン ON')
        self.assertTrue(ok)
        self.assertEqual(app.ir_codes[1]['name'], 'エアコン ON')
        saved = json.loads(self.ir_codes_file.read_text())
        self.assertEqual(saved['codes']['1']['name'], 'エアコン ON')

    def test_rename_ir_code_missing_number(self):
        ok, message = app.rename_ir_code(99, 'x')
        self.assertFalse(ok)

    def test_rename_ir_code_rejects_blank_name(self):
        app.ir_codes = {1: {'name': 'リモコン1', 'created_at': 'x', 'signal': []}}
        ok, message = app.rename_ir_code(1, '   ')
        self.assertFalse(ok)
        self.assertEqual(app.ir_codes[1]['name'], 'リモコン1')

    def test_send_ir_success(self):
        app.ir_codes = {1: {'name': 'エアコン', 'created_at': 'x', 'signal': ['pulse 900', 'space 800']}}
        process = Mock(pid=321, returncode=0)
        process.communicate.return_value = (b'', b'')
        with patch.object(app.subprocess, 'Popen', return_value=process) as spawn:
            ok, message = app.send_ir(1)
        self.assertTrue(ok)
        args = spawn.call_args.args[0]
        self.assertEqual(args[:3], ['ir-ctl', '-d', app.IR_TX_DEVICE])
        self.assertTrue(args[3].startswith('--send='))
        temp_path = args[3].split('=', 1)[1]
        self.assertFalse(Path(temp_path).exists())
        self.assertEqual(app.ir_state, app.IR_STATE_IDLE)
        app.ir_tx_led.on.assert_called_once()
        app.ir_tx_led.off.assert_called_once()

    def test_send_ir_missing_number(self):
        with patch.object(app.subprocess, 'Popen') as spawn:
            ok, message = app.send_ir(42)
        self.assertFalse(ok)
        spawn.assert_not_called()

    def test_send_ir_blocked_while_receiving(self):
        app.ir_codes = {1: {'name': 'x', 'created_at': 'x', 'signal': ['pulse 1', 'space 1']}}
        app.ir_state = app.IR_STATE_RECEIVING
        with patch.object(app.subprocess, 'Popen') as spawn:
            ok, message = app.send_ir(1)
        self.assertFalse(ok)
        spawn.assert_not_called()
        app.ir_tx_led.on.assert_not_called()

    def test_ir_learn_start_blocked_while_transmitting(self):
        app.ir_state = app.IR_STATE_TRANSMITTING
        with patch.object(app.subprocess, 'Popen') as spawn:
            ok, message = app.start_ir_learning()
        self.assertFalse(ok)
        spawn.assert_not_called()

    def test_send_ir_failure_still_turns_off_led(self):
        app.ir_codes = {1: {'name': 'x', 'created_at': 'x', 'signal': ['pulse 1', 'space 1']}}
        process = Mock(pid=321, returncode=1)
        process.communicate.return_value = (b'', b'ir-ctl: cannot open lirc\n')
        with patch.object(app.subprocess, 'Popen', return_value=process), \
             self.assertLogs(app.log, level='ERROR'):
            ok, message = app.send_ir(1)
        self.assertFalse(ok)
        self.assertEqual(app.ir_state, app.IR_STATE_IDLE)
        app.ir_tx_led.on.assert_called_once()
        app.ir_tx_led.off.assert_called_once()

    def test_ir_codes_persist_and_reload(self):
        app.ir_codes = {1: {'name': 'リモコン1', 'created_at': 'x', 'signal': ['pulse 1', 'space 1']}}
        app.ir_next_number = 2
        app._save_ir_codes()
        codes, next_number = app._load_ir_codes()
        self.assertEqual(codes[1]['name'], 'リモコン1')
        self.assertEqual(next_number, 2)

    def test_ir_codes_load_missing_file_starts_empty(self):
        codes, next_number = app._load_ir_codes()
        self.assertEqual(codes, {})
        self.assertEqual(next_number, 1)

    def test_send_ir_sequence_continues_after_failure(self):
        results = {1: (True, 'ok'), 2: (False, 'ng'), 3: (True, 'ok')}
        with patch.object(app, 'send_ir', side_effect=lambda n: results[n]) as send, \
             patch.object(app.time, 'sleep'):
            ok, summary = app.send_ir_sequence([1, 2, 3])
        self.assertFalse(ok)
        self.assertEqual(send.call_args_list, [call(1), call(2), call(3)])
        self.assertIn('No.2: ng', summary)

    def test_ir_web_routes_delegate_to_shared_functions(self):
        client = app.app.test_client()
        with patch.object(app, 'start_ir_learning', return_value=(True, 'ok')) as start:
            response = client.post('/api/ir/learn/start')
            self.assertEqual(response.status_code, 200)
            start.assert_called_once_with()
        with patch.object(app, 'cancel_ir_learning', return_value=True) as cancel:
            response = client.post('/api/ir/learn/stop')
            self.assertTrue(response.json['cancelled'])
            cancel.assert_called_once_with()
        with patch.object(app, 'send_ir', return_value=(True, 'ok')) as send:
            response = client.post('/api/ir/send/3')
            self.assertEqual(response.status_code, 200)
            send.assert_called_once_with(3)
        with patch.object(app, 'send_ir', return_value=(False, 'ng')) as send:
            response = client.post('/api/ir/send/3')
            self.assertEqual(response.status_code, 409)
        with patch.object(app, 'rename_ir_code', return_value=(True, 'ok')) as rename:
            response = client.post('/api/ir/rename/3', json={'name': 'エアコン'})
            self.assertEqual(response.status_code, 200)
            rename.assert_called_once_with(3, 'エアコン')

    def test_ir_send_route_rejects_non_integer_number(self):
        client = app.app.test_client()
        with patch.object(app, 'send_ir') as send:
            response = client.post('/api/ir/send/abc')
        self.assertEqual(response.status_code, 400)
        send.assert_not_called()

    def test_status_includes_ir_fields(self):
        app.ir_codes = {2: {'name': 'テレビ', 'created_at': 'x', 'signal': ['pulse 1']}}
        app.ir_state = app.IR_STATE_IDLE
        with patch.object(app, 'get_volume', return_value=50):
            status = app.get_status()
        self.assertEqual(status['ir_state'], app.IR_STATE_IDLE)
        self.assertIsNone(status['ir_error'])
        self.assertEqual(status['ir_codes'], [{'number': 2, 'name': 'テレビ'}])

    def test_ir_operations_do_not_touch_recording_led(self):
        app.ir_codes = {1: {'name': 'x', 'created_at': 'x', 'signal': ['pulse 1', 'space 1']}}
        process = Mock(pid=1, returncode=0)
        process.communicate.return_value = (b'', b'')
        with patch.object(app.subprocess, 'Popen', return_value=process):
            app.send_ir(1)
        app.record_led.on.assert_not_called()
        app.record_led.off.assert_not_called()

    def test_recording_and_radio_do_not_touch_ir_leds(self):
        with patch.object(app, '_stop_radio_locked'), \
             patch.object(app, '_open_file'), patch.object(app, 'SAVE_DIR'), \
             patch.object(app.threading, 'Thread'), \
             patch.object(app.subprocess, 'Popen', return_value=Mock()):
            app.start_recording()
        app.ir_rx_led.on.assert_not_called()
        app.ir_tx_led.on.assert_not_called()

    # ---- Spotify ----

    def test_spotify_ctrl2_stopped_starts_saved_selection(self):
        sources = [
            {'number': 1, 'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'},
            {'number': 2, 'type': 'artist', 'name': 'B', 'spotify_id': 'id-b'},
        ]
        self.spotify_selection_file.write_text(json.dumps({'spotify_id': 'id-b', 'type': 'artist'}))
        with patch.object(app, 'load_spotify_sources', return_value=sources), \
             patch.object(app, 'start_spotify') as start:
            app.handle_keyboard_key(2)
        start.assert_called_once_with(sources[1])

    def test_spotify_ctrl2_stopped_defaults_to_first_without_saved_selection(self):
        sources = [{'number': 1, 'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'}]
        with patch.object(app, 'load_spotify_sources', return_value=sources), \
             patch.object(app, 'start_spotify') as start:
            app.handle_keyboard_key(2)
        start.assert_called_once_with(sources[0])

    def test_spotify_ctrl2_playing_advances_and_ctrl6_goes_back_with_wraparound(self):
        sources = [
            {'number': 1, 'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'},
            {'number': 2, 'type': 'playlist', 'name': 'B', 'spotify_id': 'id-b'},
            {'number': 3, 'type': 'artist', 'name': 'C', 'spotify_id': 'id-c'},
        ]
        app.spotify_current_source = sources[2]
        with patch.object(app, 'load_spotify_sources', return_value=sources), \
             patch.object(app, 'start_spotify') as start:
            app.handle_keyboard_key(2)
        start.assert_called_once_with(sources[0])
        app.spotify_current_source = sources[0]
        with patch.object(app, 'load_spotify_sources', return_value=sources), \
             patch.object(app, 'start_spotify') as start:
            app.handle_keyboard_key(6)
        start.assert_called_once_with(sources[2])

    def test_spotify_ctrl6_while_stopped_is_noop(self):
        sources = [{'number': 1, 'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'}]
        with patch.object(app, 'load_spotify_sources', return_value=sources), \
             patch.object(app, 'start_spotify') as start:
            app.handle_keyboard_key(6)
        start.assert_not_called()

    def test_spotify_keys_noop_when_not_configured(self):
        with patch.multiple(app, SPOTIFY_CLIENT_ID='', SPOTIFY_CLIENT_SECRET='', SPOTIFY_REFRESH_TOKEN=''), \
             patch.object(app, 'load_spotify_sources') as load, \
             patch.object(app, 'start_spotify') as start:
            app.handle_keyboard_key(2)
            app.handle_keyboard_key(6)
        load.assert_not_called()
        start.assert_not_called()

    def test_start_spotify_stops_radio_and_spawns_worker(self):
        source = {'number': 1, 'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'}
        app.radio_process = Mock(pid=1, stdout=None, poll=Mock(return_value=None))
        app.current_station = app.stations[0]
        with patch.object(app, '_terminate_group'), \
             patch.object(app.threading, 'Thread') as thread_cls:
            ok, message = app.start_spotify(source)
        self.assertTrue(ok)
        self.assertIsNone(app.radio_process)
        thread_cls.assert_called_once()
        self.assertEqual(thread_cls.call_args.kwargs['args'], (source, app.spotify_generation))

    def test_start_spotify_blocked_while_recording(self):
        app.record_process = Mock(poll=Mock(return_value=None))
        source = {'number': 1, 'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'}
        with patch.object(app.threading, 'Thread') as thread_cls:
            ok, message = app.start_spotify(source)
        self.assertFalse(ok)
        thread_cls.assert_not_called()

    def test_start_spotify_rejects_when_not_configured(self):
        source = {'number': 1, 'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'}
        with patch.multiple(app, SPOTIFY_CLIENT_ID='', SPOTIFY_CLIENT_SECRET='', SPOTIFY_REFRESH_TOKEN=''), \
             patch.object(app.threading, 'Thread') as thread_cls:
            ok, message = app.start_spotify(source)
        self.assertFalse(ok)
        thread_cls.assert_not_called()

    def test_spotify_play_worker_success(self):
        source = {'number': 1, 'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'}
        with patch.object(app, '_spotify_find_device_id', return_value='device-1'), \
             patch.object(app, '_spotify_request') as request, \
             self.assertLogs(app.log, level='INFO'):
            app._spotify_play_worker(source, app.spotify_generation)
        self.assertEqual(app.spotify_current_source, source)
        self.assertIsNone(app.spotify_error)
        self.assertEqual(json.loads(self.spotify_selection_file.read_text())['spotify_id'], 'id-a')
        self.assertEqual(request.call_count, 2)

    def test_spotify_play_worker_error_sets_spotify_error(self):
        source = {'number': 1, 'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'}
        with patch.object(app, '_spotify_find_device_id', side_effect=app.SpotifyAPIError('boom')), \
             self.assertLogs(app.log, level='ERROR'):
            app._spotify_play_worker(source, app.spotify_generation)
        self.assertIsNone(app.spotify_current_source)
        self.assertEqual(app.spotify_error, 'boom')

    def test_spotify_play_worker_discards_stale_generation(self):
        source = {'number': 1, 'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'}
        with patch.object(app, '_spotify_find_device_id', return_value='device-1'), \
             patch.object(app, '_spotify_request'):
            app._spotify_play_worker(source, app.spotify_generation - 1)
        self.assertIsNone(app.spotify_current_source)

    def test_spotify_play_worker_retries_device_lookup_once_on_failure(self):
        source = {'number': 1, 'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'}
        with patch.object(app, '_spotify_find_device_id',
                           side_effect=['device-1', 'device-2']) as find_device, \
             patch.object(app, '_spotify_request',
                           side_effect=[app.SpotifyAPIError('stale'), None, None]), \
             self.assertLogs(app.log, level='INFO'):
            app._spotify_play_worker(source, app.spotify_generation)
        self.assertEqual(find_device.call_count, 2)
        self.assertEqual(app.spotify_current_source, source)

    def test_stop_spotify_clears_state_and_fires_pause(self):
        app.spotify_current_source = {'number': 1, 'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'}
        with patch.object(app.threading, 'Thread') as thread_cls:
            stopped = app.stop_spotify()
        self.assertTrue(stopped)
        self.assertIsNone(app.spotify_current_source)
        thread_cls.assert_called_once()

    def test_stop_spotify_when_already_stopped_is_noop(self):
        with patch.object(app.threading, 'Thread') as thread_cls:
            stopped = app.stop_spotify()
        self.assertFalse(stopped)
        thread_cls.assert_not_called()

    def test_start_radio_stops_spotify(self):
        app.spotify_current_source = {'number': 1, 'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'}
        process = Mock(pid=101, stdout=None, poll=Mock(return_value=None))
        with patch.object(app.subprocess, 'Popen', return_value=process), \
             patch.object(app, '_terminate_group'), \
             patch.object(app.threading, 'Thread') as thread_cls:
            app.start_radio(app.stations[0])
        self.assertIsNone(app.spotify_current_source)
        thread_cls.assert_called_once()

    def test_start_recording_stops_spotify(self):
        app.spotify_current_source = {'number': 1, 'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'}
        with patch.object(app, '_stop_radio_locked'), \
             patch.object(app, '_open_file'), patch.object(app, 'SAVE_DIR'), \
             patch.object(app.threading, 'Thread'), \
             patch.object(app.subprocess, 'Popen', return_value=Mock()):
            app.start_recording()
        self.assertIsNone(app.spotify_current_source)

    def test_emergency_stop_stops_spotify(self):
        app.spotify_current_source = {'number': 1, 'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'}
        with patch.object(app, '_spotify_pause_best_effort'):
            app.emergency_stop()
        self.assertIsNone(app.spotify_current_source)

    def test_cleanup_stops_spotify(self):
        app.cleanup_done = False
        app.spotify_current_source = {'number': 1, 'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'}
        with patch.object(app, '_spotify_pause_best_effort'), \
             patch.object(app, 'stop_recording'), patch.object(app, 'stop_radio'), \
             patch.object(app, 'cancel_ir_learning'):
            app.cleanup()
        self.assertIsNone(app.spotify_current_source)
        app.cleanup_done = False

    def test_status_includes_spotify_fields(self):
        app.spotify_sources = [{'number': 1, 'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'}]
        app.spotify_current_source = app.spotify_sources[0]
        app.spotify_now_playing = {'track': 'T', 'artist': 'Ar', 'album_art_url': 'http://x'}
        with patch.object(app, 'get_volume', return_value=50):
            status = app.get_status()
        self.assertTrue(status['spotify_configured'])
        self.assertTrue(status['spotify_playing'])
        self.assertEqual(status['spotify_source']['name'], 'A')
        self.assertEqual(status['spotify_now_playing']['track'], 'T')
        self.assertEqual(status['spotify_sources'], app.spotify_sources)

    def test_spotify_oled_shows_source_and_now_playing(self):
        app.spotify_sources = [{'number': 1, 'type': 'playlist', 'name': 'ドライブ', 'spotify_id': 'id-a'}]
        app.spotify_current_source = app.spotify_sources[0]
        self.assertEqual(app._oled_lines()[1], 'SPOTIFY 1/1')
        self.assertEqual(app._oled_lines()[2], 'ドライブ')
        app.spotify_now_playing = {'track': '希望の轍', 'artist': 'サザンオールスターズ', 'album_art_url': None}
        self.assertEqual(app._oled_lines()[2], '希望の轍 - サザンオールスターズ')

    def test_load_spotify_sources_missing_file_returns_empty(self):
        self.assertEqual(app.load_spotify_sources(), [])

    def test_load_spotify_sources_assigns_numbers(self):
        self.spotify_sources_file.write_text(json.dumps([
            {'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'},
            {'type': 'artist', 'name': 'B', 'spotify_id': 'id-b'},
        ]))
        sources = app.load_spotify_sources()
        self.assertEqual([source['number'] for source in sources], [1, 2])

    def test_load_spotify_sources_rejects_invalid_entry(self):
        self.spotify_sources_file.write_text(json.dumps([{'type': 'song', 'name': 'A', 'spotify_id': 'id-a'}]))
        with self.assertRaises(ValueError):
            app.load_spotify_sources()

    def test_add_spotify_artist_appends_and_persists(self):
        app.spotify_sources = [{'number': 1, 'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'}]
        source = app.add_spotify_artist('新人', 'id-new')
        self.assertEqual(source['number'], 2)
        self.assertEqual(len(app.spotify_sources), 2)
        saved = json.loads(self.spotify_sources_file.read_text())
        self.assertEqual(saved[-1]['name'], '新人')

    def test_add_spotify_artist_dedupes_existing(self):
        app.spotify_sources = [{'number': 1, 'type': 'artist', 'name': 'A', 'spotify_id': 'id-a'}]
        source = app.add_spotify_artist('A again', 'id-a')
        self.assertEqual(source['name'], 'A')
        self.assertEqual(len(app.spotify_sources), 1)

    def test_spotify_ensure_token_refreshes_when_missing(self):
        response = Mock(status_code=200)
        response.json.return_value = {'access_token': 'tok', 'expires_in': 3600}
        with patch.object(app.requests, 'post', return_value=response) as post:
            token = app._spotify_ensure_token()
        self.assertEqual(token, 'tok')
        post.assert_called_once()

    def test_spotify_ensure_token_wraps_network_error(self):
        with patch.object(app.requests, 'post', side_effect=app.requests.RequestException('down')):
            with self.assertRaises(app.SpotifyAPIError):
                app._spotify_ensure_token()

    def test_spotify_request_raises_on_http_error(self):
        app.spotify_access_token = 'tok'
        app.spotify_token_expiry = app.time.monotonic() + 3600
        response = Mock(status_code=403, content=b'{"error":{"message":"forbidden"}}')
        response.json.return_value = {'error': {'message': 'forbidden'}}
        with patch.object(app.requests, 'request', return_value=response):
            with self.assertRaises(app.SpotifyAPIError):
                app._spotify_request('GET', '/me/player')

    def test_spotify_request_wraps_network_error(self):
        app.spotify_access_token = 'tok'
        app.spotify_token_expiry = app.time.monotonic() + 3600
        with patch.object(app.requests, 'request', side_effect=app.requests.RequestException('down')):
            with self.assertRaises(app.SpotifyAPIError):
                app._spotify_request('GET', '/me/player')


    # ---- スケジューラー ----

    def add_schedule(self, **fields):
        data = {'time': '07:00', 'repeat': 'daily', 'action': 'record_stop', 'enabled': True}
        data.update(fields)
        ok, message, task = app.create_schedule(data)
        self.assertTrue(ok, message)
        return task

    def test_schedule_validation_rejects_bad_input(self):
        app.ir_codes = {1: {'name': 'JQBT', 'created_at': 'x', 'signal': ['pulse 1']}}
        bad = [
            {'time': '7:00', 'repeat': 'daily', 'action': 'record_stop'},
            {'time': '24:00', 'repeat': 'daily', 'action': 'record_stop'},
            {'time': '07:00', 'repeat': 'hourly', 'action': 'record_stop'},
            {'time': '07:00', 'repeat': 'weekly', 'weekdays': [], 'action': 'record_stop'},
            {'time': '07:00', 'repeat': 'weekly', 'weekdays': [7], 'action': 'record_stop'},
            {'time': '07:00', 'repeat': 'once', 'date': '2026-13-01', 'action': 'record_stop'},
            {'time': '07:00', 'repeat': 'once', 'date': '2000-01-01', 'action': 'record_stop'},
            {'time': '07:00', 'repeat': 'daily', 'action': 'shutdown'},
            {'time': '07:00', 'repeat': 'daily', 'action': 'radio', 'target': 999},
            {'time': '07:00', 'repeat': 'daily', 'action': 'radio', 'target': 'x'},
            {'time': '07:00', 'repeat': 'daily', 'action': 'ir', 'target': 2},
            {'time': '07:00', 'repeat': 'daily', 'action': 'mp3', 'target': 'other.mp3'},
            {'time': '07:00', 'repeat': 'daily', 'action': 'record_stop', 'enabled': 'yes'},
            None,
        ]
        for data in bad:
            with self.subTest(data=data):
                ok, message, task = app.create_schedule(data)
                self.assertFalse(ok)
                self.assertIsNone(task)
        self.assertEqual(app.schedules, {})
        self.assertFalse(self.schedules_file.exists())

    def test_schedule_create_normalizes_and_persists(self):
        app.ir_codes = {1: {'name': 'JQBT', 'created_at': 'x', 'signal': ['pulse 1']}}
        radio = self.add_schedule(action='radio', target='3', repeat='weekly', weekdays=[4, 0, 4])
        ir = self.add_schedule(action='ir', target=1, time='06:59')
        mp3 = self.add_schedule(action='mp3', target=app.MP3_NAME, record='ignored')
        stop = self.add_schedule(action='playback_stop', target='ignored')
        self.assertEqual((radio['id'], radio['target'], radio['weekdays']), (1, 3, [0, 4]))
        self.assertEqual(ir['target'], 1)
        self.assertEqual(mp3['target'], app.MP3_NAME)
        self.assertNotIn('record', mp3)
        self.assertIsNone(stop['target'])
        saved = json.loads(self.schedules_file.read_text())
        self.assertEqual(saved['next_id'], 5)
        self.assertEqual([task['id'] for task in saved['tasks']], [1, 2, 3, 4])
        loaded, next_id = app._load_schedules()
        self.assertEqual(next_id, 5)
        self.assertEqual(loaded[1], app.schedules[1])

    def test_schedule_load_missing_and_broken_file(self):
        self.assertEqual(app._load_schedules(), ({}, 1))
        self.schedules_file.write_text('{broken')
        with self.assertLogs(app.log, level='ERROR'):
            self.assertEqual(app._load_schedules(), ({}, 1))
        self.assertEqual(self.schedules_file.with_suffix('.json.broken').read_text(), '{broken')

    def test_schedule_load_drops_finished_once_tasks(self):
        base = {'weekdays': [], 'action': 'record_stop', 'target': None, 'enabled': False}
        tasks = [
            dict(base, id=1, time='07:00', repeat='once', date='2026-09-26',
                 last_run='2026-09-26T07:00', last_result={'ok': True}),
            dict(base, id=2, time='07:00', repeat='once', date='2026-09-26',
                 last_run='2026-09-26T07:00', last_result={'ok': False}),
            dict(base, id=3, time='07:00', repeat='daily', date=None,
                 last_run='2026-09-26T07:00', last_result={'ok': True}),
        ]
        self.schedules_file.write_text(json.dumps({'next_id': 4, 'tasks': tasks}))
        loaded, next_id = app._load_schedules()
        self.assertEqual(sorted(loaded), [2, 3])
        self.assertEqual(next_id, 4)

    def test_schedule_update_toggle_delete(self):
        task = self.add_schedule()
        ok, message, updated = app.update_schedule(task['id'], {
            'time': '08:30', 'repeat': 'weekly', 'weekdays': [5, 6], 'action': 'record_start'})
        self.assertTrue(ok, message)
        self.assertEqual(app.schedules[1]['time'], '08:30')
        self.assertEqual(app.update_schedule(99, {})[0], False)
        self.assertTrue(app.set_schedule_enabled(1, False)[0])
        self.assertFalse(app.schedules[1]['enabled'])
        self.assertFalse(json.loads(self.schedules_file.read_text())['tasks'][0]['enabled'])
        self.assertTrue(app.delete_schedule(1)[0])
        self.assertFalse(app.delete_schedule(1)[0])
        self.assertEqual(json.loads(self.schedules_file.read_text())['tasks'], [])

    def test_schedule_save_failure_rolls_back(self):
        with patch.object(app, '_save_schedules', side_effect=OSError('disk full')):
            ok, message, task = app.create_schedule(
                {'time': '07:00', 'repeat': 'daily', 'action': 'record_stop'})
        self.assertFalse(ok)
        self.assertEqual(app.schedules, {})
        self.assertEqual(app.schedule_next_id, 1)

    def test_scheduler_first_tick_after_start_does_not_run_past_tasks(self):
        self.add_schedule(time='07:00')
        with patch.object(app, 'stop_recording') as stop:
            self.assertEqual(app.run_due_schedules(app.datetime(2026, 9, 28, 7, 0, 30)), [])
            stop.assert_not_called()
            self.assertEqual(app.run_due_schedules(app.datetime(2026, 9, 28, 7, 1, 0, 200000)), [])
            stop.assert_not_called()

    def test_scheduler_runs_daily_once_per_occurrence(self):
        self.add_schedule(time='07:00')
        app.scheduler_last_checked = app.datetime(2026, 9, 28, 6, 59, 0, 200000)
        with patch.object(app, 'stop_recording', return_value=True) as stop:
            results = app.run_due_schedules(app.datetime(2026, 9, 28, 7, 0, 0, 200000))
            self.assertEqual(results, [(1, True, '録音を停止しました')])
            # 時刻が巻き戻って同じ分をもう一度通過しても再実行しない。
            app.scheduler_last_checked = app.datetime(2026, 9, 28, 6, 59, 59)
            self.assertEqual(app.run_due_schedules(app.datetime(2026, 9, 28, 7, 0, 30)), [])
            stop.assert_called_once_with()
        saved = json.loads(self.schedules_file.read_text())['tasks'][0]
        self.assertEqual(saved['last_run'], '2026-09-28T07:00')
        self.assertTrue(saved['last_result']['ok'])
        self.assertTrue(saved['enabled'])
        # 翌日も実行される。
        app.scheduler_last_checked = app.datetime(2026, 9, 29, 6, 59, 30)
        with patch.object(app, 'stop_recording', return_value=False):
            self.assertEqual(app.run_due_schedules(app.datetime(2026, 9, 29, 7, 0, 1)),
                             [(1, True, '録音していません')])

    def test_scheduler_weekly_and_disabled(self):
        self.add_schedule(time='07:00', repeat='weekly', weekdays=[0])  # 月曜
        self.add_schedule(time='07:00', enabled=False)
        with patch.object(app, 'stop_recording', return_value=True) as stop:
            app.scheduler_last_checked = app.datetime(2026, 9, 27, 6, 59, 30)  # 日曜
            self.assertEqual(app.run_due_schedules(app.datetime(2026, 9, 27, 7, 0, 1)), [])
            app.scheduler_last_checked = app.datetime(2026, 9, 28, 6, 59, 30)  # 月曜
            self.assertEqual([r[0] for r in app.run_due_schedules(app.datetime(2026, 9, 28, 7, 0, 1))], [1])
        stop.assert_called_once_with()

    def test_scheduler_once_deleted_only_after_success(self):
        for ok in (True, False):
            with self.subTest(ok=ok):
                app.schedules = {}
                future = (app.datetime.now() + app.timedelta(days=2)).date().isoformat()
                task = self.add_schedule(time='07:00', repeat='once', date=future, action='record_start')
                minute = app.datetime.fromisoformat(f'{future}T07:00')
                app.scheduler_last_checked = minute - app.timedelta(seconds=30)
                with patch.object(app, 'start_recording', return_value=(ok, 'msg')):
                    app.run_due_schedules(minute + app.timedelta(seconds=1))
                if ok:
                    self.assertNotIn(task['id'], app.schedules)
                    self.assertEqual(json.loads(self.schedules_file.read_text())['tasks'], [])
                else:
                    self.assertTrue(app.schedules[task['id']]['enabled'])
                    self.assertFalse(app.schedules[task['id']]['last_result']['ok'])

    def test_scheduler_large_clock_jump_does_not_catch_up(self):
        self.add_schedule(time='07:00')
        self.add_schedule(time='10:59')
        app.scheduler_last_checked = app.datetime(2026, 9, 28, 3, 0)
        with patch.object(app, 'stop_recording', return_value=True) as stop, \
             self.assertLogs(app.log, level='WARNING'):
            results = app.run_due_schedules(app.datetime(2026, 9, 28, 11, 0, 5))
        self.assertEqual([r[0] for r in results], [2])  # 90秒以内の10:59だけ
        stop.assert_called_once_with()

    def test_scheduler_same_minute_runs_stop_ir_then_start_in_order(self):
        app.ir_codes = {1: {'name': 'JQBT', 'created_at': 'x', 'signal': ['pulse 1']},
                        2: {'name': 'TV', 'created_at': 'x', 'signal': ['pulse 1']}}
        self.add_schedule(action='radio', target=2)
        self.add_schedule(action='ir', target=2)
        self.add_schedule(action='playback_stop')
        self.add_schedule(action='ir', target=1)
        self.add_schedule(action='mp3', target=app.MP3_NAME)
        self.add_schedule(action='record_start')
        order = []
        with patch.object(app, 'start_radio', side_effect=lambda s, toggle: order.append(('radio', s['number'], toggle)) or (True, 'ok')), \
             patch.object(app, 'start_mp3', side_effect=lambda: order.append('mp3') or (True, 'ok')), \
             patch.object(app, 'send_ir', side_effect=lambda n: order.append(('ir', n)) or (True, 'ok')), \
             patch.object(app, 'start_recording', side_effect=lambda: order.append('record_start') or (False, '録音中')), \
             patch.object(app, 'stop_playback', side_effect=lambda: order.append('playback_stop') or True):
            app.scheduler_last_checked = app.datetime(2026, 9, 28, 6, 59, 59)
            results = app.run_due_schedules(app.datetime(2026, 9, 28, 7, 0, 0, 200000))
        self.assertEqual(order, ['playback_stop', ('ir', 2), ('ir', 1), 'record_start',
                                 ('radio', 2, False), 'mp3'])
        self.assertEqual(len(results), 6)
        self.assertFalse(app.schedules[6]['last_result']['ok'])

    def test_scheduler_uses_existing_core_functions_end_to_end(self):
        """モックはsubprocess層のみ。既存のstart_radio/start_mp3/stop_playbackを通ることを確認する。"""
        self.add_schedule(time='07:00', action='radio', target=3)
        self.add_schedule(time='07:01', action='mp3', target=app.MP3_NAME)
        self.add_schedule(time='07:02', action='playback_stop')
        process = Mock(pid=321, stdout=None, poll=Mock(return_value=None))
        with patch.object(app.subprocess, 'Popen', return_value=process) as spawn, \
             patch.object(app, '_terminate_group') as terminate:
            app.scheduler_last_checked = app.datetime(2026, 9, 28, 6, 59, 59)
            app.run_due_schedules(app.datetime(2026, 9, 28, 7, 0, 1))
            self.assertEqual(app.current_station['number'], 3)
            self.assertEqual(self.station_file.read_text(), '3\n')
            app.run_due_schedules(app.datetime(2026, 9, 28, 7, 1, 1))
            self.assertEqual(app.current_station['kind'], 'mp3')
            self.assertEqual(self.station_file.read_text(), '3\n')
            app.run_due_schedules(app.datetime(2026, 9, 28, 7, 2, 1))
        self.assertIsNone(app.radio_process)
        self.assertEqual(spawn.call_count, 2)
        self.assertEqual(terminate.call_count, 2)

    def test_scheduled_radio_blocked_while_recording_is_reported(self):
        self.add_schedule(action='radio', target=1)
        app.record_process = Mock(poll=Mock(return_value=None))
        with patch.object(app.subprocess, 'Popen') as spawn:
            app.scheduler_last_checked = app.datetime(2026, 9, 28, 6, 59, 59)
            results = app.run_due_schedules(app.datetime(2026, 9, 28, 7, 0, 1))
        spawn.assert_not_called()
        self.assertFalse(results[0][1])
        self.assertIn('録音中', app.schedules[1]['last_result']['message'])

    def test_scheduler_exception_in_action_is_recorded(self):
        self.add_schedule(action='record_start')
        with patch.object(app, 'start_recording', side_effect=RuntimeError('boom')), \
             self.assertLogs(app.log, level='ERROR'):
            app.scheduler_last_checked = app.datetime(2026, 9, 28, 6, 59, 59)
            results = app.run_due_schedules(app.datetime(2026, 9, 28, 7, 0, 1))
        self.assertEqual(results, [(1, False, 'boom')])

    def test_scheduler_skips_when_shutting_down(self):
        self.add_schedule()
        app.scheduler_last_checked = app.datetime(2026, 9, 28, 6, 59, 59)
        app.shutdown_event.set()
        with patch.object(app, 'stop_recording') as stop:
            self.assertEqual(app.run_due_schedules(app.datetime(2026, 9, 28, 7, 0, 1)), [])
        stop.assert_not_called()

    def test_scheduler_does_not_hold_lock_while_executing(self):
        self.add_schedule(action='record_start')
        observed = []
        def action():
            thread = threading.Thread(target=lambda: observed.append(app.control_lock.acquire(timeout=1)
                                                                     and app.control_lock.release() is None))
            thread.start()
            thread.join()
            return True, 'ok'
        with patch.object(app, 'start_recording', side_effect=action):
            app.scheduler_last_checked = app.datetime(2026, 9, 28, 6, 59, 59)
            app.run_due_schedules(app.datetime(2026, 9, 28, 7, 0, 1))
        self.assertEqual(observed, [True])

    def test_scheduler_worker_waits_until_next_minute(self):
        waits = []
        def fake_wait(timeout):
            waits.append(timeout)
            app.shutdown_event.set()
        with patch.object(app, 'run_due_schedules') as run, \
             patch.object(app.shutdown_event, 'wait', side_effect=fake_wait), \
             patch.object(app, 'datetime', Mock(now=Mock(return_value=app.datetime(2026, 9, 28, 7, 0, 45)))):
            app.scheduler_worker()
        run.assert_called_once_with()
        self.assertAlmostEqual(waits[0], 15.2)

    def test_stop_playback_uses_existing_stop_functions(self):
        with patch.object(app, 'stop_radio', return_value=False) as radio, \
             patch.object(app, 'stop_spotify', return_value=True) as spotify, \
             patch.object(app, 'stop_recording') as record:
            self.assertTrue(app.stop_playback())
        radio.assert_called_once_with()
        spotify.assert_called_once_with()
        record.assert_not_called()

    def test_schedule_web_api_crud(self):
        client = app.app.test_client()
        response = client.post('/api/schedules', json={
            'time': '07:15', 'repeat': 'daily', 'action': 'radio', 'target': '2', 'enabled': True})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(client.post('/api/schedules', json={'time': 'x'}).status_code, 400)
        data = client.get('/api/schedules').json
        self.assertEqual(len(data['tasks']), 1)
        self.assertEqual(data['tasks'][0]['target_label'], app.stations[1]['name'])
        self.assertEqual(data['tasks'][0]['action_label'], 'ラジオ再生')
        self.assertEqual(data['options']['mp3'], [{'value': app.MP3_NAME, 'name': app.MP3_NAME}])
        self.assertTrue(data['options']['stations'])
        response = client.post('/api/schedules/1', json={
            'time': '07:20', 'repeat': 'weekly', 'weekdays': [1], 'action': 'playback_stop'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(client.post('/api/schedules/1/enabled', json={'enabled': False}).status_code, 200)
        self.assertFalse(app.schedules[1]['enabled'])
        self.assertEqual(client.post('/api/schedules/1/enabled', json={'enabled': 'no'}).status_code, 400)
        self.assertEqual(client.post('/api/schedules/abc/delete').status_code, 400)
        self.assertEqual(client.post('/api/schedules/1/delete').status_code, 200)
        self.assertEqual(client.post('/api/schedules/1/delete').status_code, 400)
        self.assertEqual(client.get('/api/schedules').json['tasks'], [])

    def test_index_renders_schedule_tab(self):
        html = app.app.test_client().get('/').get_data(as_text=True)
        self.assertIn('data-tab="schedule"', html)
        self.assertIn('id="scheduleForm"', html)
        self.assertIn('type="datetime-local"', html)
        self.assertIn('<option value="spotify">', html)
        self.assertIn('${name}', html)

    def write_spotify_sources(self):
        self.spotify_sources_file.write_text(json.dumps([
            {'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'},
            {'type': 'artist', 'name': 'B', 'spotify_id': 'id-b'},
        ]))

    def test_schedule_spotify_validation_and_options(self):
        self.write_spotify_sources()
        for target in (None, '', 'id-missing', 2):
            with self.subTest(target=target):
                self.assertFalse(app.create_schedule({'time': '07:00', 'repeat': 'daily',
                                                      'action': 'spotify', 'target': target})[0])
        task = self.add_schedule(action='spotify', target='id-b')
        self.assertEqual(task['target'], 'id-b')
        data = app.list_schedules()
        self.assertEqual(data['options']['spotify'], [{'value': 'id-a', 'name': '1. A'},
                                                      {'value': 'id-b', 'name': '2. B'}])
        self.assertEqual(data['tasks'][0]['target_label'], 'B')
        self.assertEqual(data['tasks'][0]['action_label'], 'Spotify再生')

    def test_scheduler_spotify_uses_start_spotify_and_follows_reordering(self):
        self.write_spotify_sources()
        self.add_schedule(action='spotify', target='id-b')
        # 登録後に並び順が変わっても、spotify_idで同じ再生リストを選ぶ。
        self.spotify_sources_file.write_text(json.dumps([
            {'type': 'artist', 'name': 'B', 'spotify_id': 'id-b'},
            {'type': 'playlist', 'name': 'A', 'spotify_id': 'id-a'},
        ]))
        with patch.object(app, 'start_spotify', return_value=(True, 'ok')) as start:
            app.scheduler_last_checked = app.datetime(2026, 9, 28, 6, 59, 59)
            app.run_due_schedules(app.datetime(2026, 9, 28, 7, 0, 1))
        self.assertEqual(start.call_args.args[0]['spotify_id'], 'id-b')
        self.assertEqual(start.call_args.args[0]['number'], 1)
        # 削除されていれば失敗として記録する。
        self.spotify_sources_file.write_text('[]')
        with patch.object(app, 'start_spotify') as start:
            app.scheduler_last_checked = app.datetime(2026, 9, 29, 6, 59, 59)
            results = app.run_due_schedules(app.datetime(2026, 9, 29, 7, 0, 1))
        start.assert_not_called()
        self.assertFalse(results[0][1])

    def test_scheduled_spotify_through_existing_start_spotify(self):
        self.write_spotify_sources()
        self.add_schedule(action='spotify', target='id-a')
        app.radio_process = Mock(pid=5, stdout=None, poll=Mock(return_value=None))
        app.current_station = app.stations[0]
        with patch.object(app, '_terminate_group'), \
             patch.object(app.threading, 'Thread') as thread:
            app.scheduler_last_checked = app.datetime(2026, 9, 28, 6, 59, 59)
            results = app.run_due_schedules(app.datetime(2026, 9, 28, 7, 0, 1))
        self.assertEqual(results, [(1, True, 'Spotify再生を開始しました')])
        self.assertIsNone(app.radio_process)  # 既存仕様どおりラジオを止める
        self.assertEqual(thread.call_args.kwargs['target'], app._spotify_play_worker)


if __name__ == '__main__':
    unittest.main()
