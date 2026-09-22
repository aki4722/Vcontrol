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
                        'a2dp-sink-sbc_xq'])
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
                 patch.object(app, '_refresh_storage_status', return_value={
                     'mounted': True, 'free_gib': 100.0, 'total_gib': 200.0, 'low': False}), \
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
             patch.object(app, '_refresh_storage_status', return_value={
                 'mounted': True, 'free_gib': 100.0, 'total_gib': 200.0, 'low': False}), \
             patch.object(app.threading, 'Thread'), \
             patch.object(app.subprocess, 'Popen', side_effect=lambda *a, **k: order.append('record') or Mock()):
            self.assertTrue(app.start_recording()[0])
        self.assertEqual(order, ['stop', 'record'])

    def test_recording_blocked_when_ssd_not_mounted(self):
        with patch.object(app.os.path, 'ismount', return_value=False), \
             patch.object(app.subprocess, 'Popen') as spawn, \
             self.assertLogs(app.log, level='ERROR'):
            ok, message = app.start_recording()
        self.assertFalse(ok)
        spawn.assert_not_called()
        self.assertIsNone(app.record_process)

    def test_recording_blocked_when_ssd_space_low(self):
        usage = SimpleNamespace(free=int(0.5 * 1024 ** 3), total=100 * 1024 ** 3)
        with patch.object(app.os.path, 'ismount', return_value=True), \
             patch.object(app, '_probe_ssd_write', return_value=True), \
             patch.object(app.shutil, 'disk_usage', return_value=usage), \
             patch.object(app.subprocess, 'Popen') as spawn, \
             self.assertLogs(app.log, level='ERROR'):
            ok, message = app.start_recording()
        self.assertFalse(ok)
        spawn.assert_not_called()
        self.assertIsNone(app.record_process)

    def test_recording_blocked_when_ssd_write_probe_fails(self):
        """マウント表には残っているがデバイスが切断された「幽霊マウント」を検知する。"""
        with patch.object(app.os.path, 'ismount', return_value=True), \
             patch.object(app, '_probe_ssd_write', return_value=False), \
             patch.object(app.subprocess, 'Popen') as spawn, \
             self.assertLogs(app.log, level='ERROR'):
            ok, message = app.start_recording()
        self.assertFalse(ok)
        spawn.assert_not_called()
        self.assertIsNone(app.record_process)

    def test_refresh_storage_status_not_mounted(self):
        with patch.object(app.os.path, 'ismount', return_value=False):
            status = app._refresh_storage_status()
        self.assertFalse(status['mounted'])
        self.assertIsNone(status['free_gib'])

    def test_refresh_storage_status_write_probe_failure_reports_not_mounted(self):
        with patch.object(app.os.path, 'ismount', return_value=True), \
             patch.object(app, '_probe_ssd_write', return_value=False):
            status = app._refresh_storage_status()
        self.assertFalse(status['mounted'])
        self.assertIsNone(status['free_gib'])

    def test_refresh_storage_status_mounted(self):
        usage = SimpleNamespace(free=2 * 1024 ** 3, total=10 * 1024 ** 3)
        with patch.object(app.os.path, 'ismount', return_value=True), \
             patch.object(app, '_probe_ssd_write', return_value=True), \
             patch.object(app.shutil, 'disk_usage', return_value=usage):
            status = app._refresh_storage_status()
        self.assertTrue(status['mounted'])
        self.assertAlmostEqual(status['free_gib'], 2.0)

    def test_probe_ssd_write_creates_and_removes_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app, 'SAVE_DIR', Path(directory) / 'voice'):
                self.assertTrue(app._probe_ssd_write())
            self.assertEqual(list((Path(directory) / 'voice').iterdir()), [])

    def test_probe_ssd_write_fails_when_directory_unwritable(self):
        with patch.object(app, 'SAVE_DIR', Path('/nonexistent-root/voice')):
            self.assertFalse(app._probe_ssd_write())

    def test_probe_ssd_write_times_out_on_hanging_io(self):
        blocked = threading.Event()

        class HangingDir:
            def mkdir(self, **kwargs):
                blocked.wait(5)

        with patch.object(app, 'SAVE_DIR', HangingDir()):
            self.assertFalse(app._probe_ssd_write(timeout=0.05))
        blocked.set()

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
             patch.object(app, 'load_stations') as load:
            for key in (2, 3, 6, 7):
                app.handle_keyboard_key(key)
            start.assert_not_called()
            stop.assert_not_called()
            load.assert_not_called()
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
             patch.object(app, '_refresh_storage_status', return_value={
                 'mounted': True, 'free_gib': 100.0, 'total_gib': 200.0, 'low': False}), \
             patch.object(app.threading, 'Thread'), \
             patch.object(app.subprocess, 'Popen', return_value=Mock()):
            app.start_recording()
        app.ir_rx_led.on.assert_not_called()
        app.ir_tx_led.on.assert_not_called()


if __name__ == '__main__':
    unittest.main()
