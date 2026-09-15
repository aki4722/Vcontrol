#!/usr/bin/env python3
"""GPIO と Web から録音・radiko を操作する常駐プロセス。"""

import atexit
import logging
import os
import queue
import re
import signal
import shutil
import subprocess
import threading
import wave
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request
from gpiozero import DigitalInputDevice, DigitalOutputDevice, LED

BASE_DIR = Path(__file__).resolve().parent
SAVE_DIR = Path(os.environ.get("RECORDINGS_DIR", "/home/akimoto/recordings"))
STATIONS_FILE = BASE_DIR / "stations.conf"
RADIO_SCRIPT = BASE_DIR / "play_radiko.sh"
GDRIVE_DIR = os.environ.get("GDRIVE_DIR", "gdrive:音声")
SAMPLE_RATE = 16000
CHANNELS = 1
SPLIT_SECONDS = 3600
RECORD_LED_GPIO = 27
MATRIX_ROWS = (5, 6, 13)
MATRIX_COLUMNS = (16, 20, 21)
MATRIX_ACTIONS = ("record_toggle", "record_stop", "all_stop", 0, 1, 2, 3, 4, 5)
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "5000"))
ALSA_VOLUME_CONTROL = "Master"
OLED_PORT = int(os.environ.get("OLED_PORT", "1"), 0)
OLED_ADDRESS = int(os.environ.get("OLED_ADDRESS", "0x3c"), 0)
OLED_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
OLED_FONT_SIZE = 12
OLED_FOOTER_FONT_SIZE = 10
CPU_TEMP_FILE = Path("/sys/class/thermal/thermal_zone0/temp")
CPU_TEMP_UPDATE_SECONDS = 30
STORAGE_UPDATE_SECONDS = 30
REC_BLINK_SECONDS = 0.5

log = logging.getLogger(__name__)
app = Flask(__name__)
control_lock = threading.RLock()
upload_queue = queue.Queue()
upload_lock = threading.Lock()
upload_process = None
upload_thread = None
shutdown_event = threading.Event()
record_process = None
writer_thread = None
wav_file = None
current_filepath = None
recording_started_at = None
radio_process = None
radio_pgid = None
radio_error = None
current_station = None
record_led = None
matrix_rows = []
matrix_columns = []
cleanup_done = False
oled_device = None
oled_thread = None
cpu_temperature_text = "CPU --c"
cpu_temperature_updated_at = None
storage_free_text = "FREE --G"
storage_free_updated_at = None


def load_stations():
    result = []
    with STATIONS_FILE.open(encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, station_id = line.partition("|")
            if not separator or not re.fullmatch(r"[A-Za-z0-9-]+", station_id):
                raise ValueError(f"放送局設定が不正です: {line}")
            result.append({"name": name.strip(), "id": station_id})
    return result


stations = load_stations()


def _oled_station_name(station):
    """OLEDは英数字だけに統一し、放送局はradiko局IDで表示する。"""
    return station["id"]


def _cpu_temperature_text():
    """CPU温度を30秒ごとに読み、OLED向けの短いASCII文字列で返す。"""
    global cpu_temperature_text, cpu_temperature_updated_at
    now = time.monotonic()
    if cpu_temperature_updated_at is None or now - cpu_temperature_updated_at >= CPU_TEMP_UPDATE_SECONDS:
        try:
            temperature = int(CPU_TEMP_FILE.read_text(encoding="ascii").strip()) / 1000
            cpu_temperature_text = f"CPU {temperature:.0f}c"
        except (OSError, ValueError):
            cpu_temperature_text = "CPU --c"
        cpu_temperature_updated_at = now
    return cpu_temperature_text


def _storage_free_text():
    """録音保存先の空き容量を30秒ごとに取得する。"""
    global storage_free_text, storage_free_updated_at
    now = time.monotonic()
    if storage_free_updated_at is None or now - storage_free_updated_at >= STORAGE_UPDATE_SECONDS:
        try:
            free_gib = shutil.disk_usage(SAVE_DIR).free / (1024 ** 3)
            storage_free_text = f"FREE {free_gib:.0f}G"
        except OSError:
            storage_free_text = "FREE --G"
        storage_free_updated_at = now
    return storage_free_text


def _oled_lines():
    """現在の状態を128x64 OLED向けの表示要素にまとめる。"""
    with control_lock:
        _reconcile_recording()
        if radio_process is not None and radio_process.poll() is not None:
            _stop_radio_locked(unexpected=True)
        now = datetime.now()
        if record_process is not None:
            elapsed = max(0, int((now - recording_started_at).total_seconds()))
            hours, remainder = divmod(elapsed, 3600)
            minutes, seconds = divmod(remainder, 60)
            rec_visible = int(time.monotonic() / REC_BLINK_SECONDS) % 2 == 0
            return (now.strftime("%Y-%m-%d %H:%M"), "REC", f"TIME {hours:02}:{minutes:02}:{seconds:02}", _storage_free_text(), _cpu_temperature_text(), rec_visible)
        if radio_process is not None:
            return (now.strftime("%Y-%m-%d %H:%M"), "RADIO", _oled_station_name(current_station), _storage_free_text(), _cpu_temperature_text(), True)
        return (now.strftime("%Y-%m-%d %H:%M"), "READY", "VOICE CONTROL", _storage_free_text(), _cpu_temperature_text(), True)


def oled_worker():
    """状態が変わった時と時刻が進んだ時だけOLEDを書き換える。"""
    from luma.core.render import canvas
    from PIL import ImageFont

    # SSD1309のmode="1"キャンバスへ直接描画し、中間階調を作らない。
    oled_font = ImageFont.truetype(OLED_FONT, OLED_FONT_SIZE)
    footer_font = ImageFont.truetype(OLED_FONT, OLED_FOOTER_FONT_SIZE)
    previous = None
    try:
        while not shutdown_event.is_set():
            lines = _oled_lines()
            if lines != previous:
                with canvas(oled_device) as draw:
                    draw.text((2, -2), lines[0], font=oled_font, fill="white")
                    draw.line((2, 13, 125, 13), fill="white")
                    if lines[5]:
                        draw.text((2, 14), lines[1], font=oled_font, fill="white")
                    draw.text((2, 31), lines[2][:18], font=oled_font, fill="white")
                    draw.text((2, 52), lines[4], font=footer_font, fill="white")
                    if lines[3]:
                        draw.text((126, 52), lines[3], font=footer_font, fill="white", anchor="ra")
                previous = lines
            shutdown_event.wait(0.5)
    except Exception:
        log.exception("OLED表示を停止しました")


def upload_worker():
    global upload_process
    while True:
        filepath = upload_queue.get()
        try:
            if filepath is None or shutdown_event.is_set():
                return
            with upload_lock:
                if shutdown_event.is_set():
                    return
                process = subprocess.Popen(
                    ["rclone", "copyto", str(filepath), f"{GDRIVE_DIR}/{filepath.name}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                upload_process = process
            code = process.wait()
            if code:
                log.error("アップロード失敗 %s: 終了コード %s", filepath, code)
            else:
                log.info("アップロード完了: %s", filepath)
        except OSError:
            log.exception("アップロード実行エラー: %s", filepath)
        finally:
            with upload_lock:
                if upload_process is not None and upload_process.poll() is not None:
                    upload_process = None
            upload_queue.task_done()


def _open_file():
    global wav_file, current_filepath, recording_started_at
    now = datetime.now()
    path = SAVE_DIR / f"{now:%Y%m%d_%H%M%S_%f}.wav"
    file = wave.open(str(path), "wb")
    file.setnchannels(CHANNELS)
    file.setsampwidth(2)
    file.setframerate(SAMPLE_RATE)
    wav_file = file
    current_filepath = path
    recording_started_at = now
    log.info("録音ファイル: %s", path)


def _close_file(upload=True):
    global wav_file, current_filepath, recording_started_at
    if wav_file is None:
        return
    filepath = current_filepath
    try:
        wav_file.close()
    finally:
        wav_file = None
        current_filepath = None
        recording_started_at = None
    if upload and filepath:
        upload_queue.put(filepath)


def write_recording(process):
    try:
        while chunk := process.stdout.read(8192):
            if (datetime.now() - recording_started_at).total_seconds() >= SPLIT_SECONDS:
                _close_file()
                _open_file()
            wav_file.writeframes(chunk)
    except Exception:
        log.exception("録音ファイルの書き込みに失敗しました")
        shutdown_event.set()
    finally:
        process.stdout.close()
        _close_file()


def _reconcile_recording():
    global record_process, writer_thread
    if record_process is not None and record_process.poll() is not None:
        writer_thread.join()
        record_process = None
        writer_thread = None
        record_led.off()


def _group_exists(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def _terminate_group(process, pgid, graceful_signal=signal.SIGTERM):
    """シェルが先に終了していても、同じグループの再生子プロセスを止める。"""
    if process is None or pgid is None:
        return
    try:
        os.killpg(pgid, graceful_signal)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 3
    while _group_exists(pgid) and time.monotonic() < deadline:
        if process.poll() is None:
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
        else:
            time.sleep(0.1)
    if _group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            log.error("子プロセスを回収できませんでした: %s", process.pid)


def _stop_radio_locked(unexpected=False):
    global radio_process, radio_pgid, radio_error, current_station
    process = radio_process
    pgid = radio_pgid
    radio_process = None
    radio_pgid = None
    current_station = None
    if process is None:
        return False
    _terminate_group(process, pgid)
    if process.stdout is not None:
        output = process.stdout.read().decode("utf-8", errors="replace").strip()
        process.stdout.close()
    else:
        output = ""
    if unexpected:
        radio_error = output.splitlines()[-1] if output else "ラジオ再生プロセスが終了しました"
        log.error("ラジオ異常終了: %s", radio_error)
    else:
        radio_error = None
    log.info("ラジオ停止")
    return True


def stop_radio():
    with control_lock:
        return _stop_radio_locked()


def start_radio(station):
    global radio_process, radio_pgid, radio_error, current_station
    with control_lock:
        if shutdown_event.is_set():
            return False, "終了処理中です"
        _reconcile_recording()
        if record_process is not None:
            return False, "録音中はラジオを再生できません"
        if radio_process is not None and radio_process.poll() is None:
            return False, "ラジオはすでに再生中です"
        if radio_process is not None:
            _stop_radio_locked()
        try:
            radio_process = subprocess.Popen(
                [str(RADIO_SCRIPT), station["id"]], start_new_session=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            log.exception("ラジオ開始失敗")
            return False, str(exc)
        current_station = station
        radio_pgid = radio_process.pid
        radio_error = None
        log.info("ラジオ開始: %s", station["name"])
        return True, "ラジオ再生を開始しました"


def start_recording():
    global record_process, writer_thread
    with control_lock:
        if shutdown_event.is_set():
            return False, "終了処理中です"
        _reconcile_recording()
        if record_process is not None:
            return False, "すでに録音中です"
        _stop_radio_locked()
        try:
            SAVE_DIR.mkdir(parents=True, exist_ok=True)
            _open_file()
            process = subprocess.Popen(
                ["arecord", "-q", "-t", "raw", "-f", "S16_LE",
                 "-r", str(SAMPLE_RATE), "-c", str(CHANNELS)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            record_process = process
            writer_thread = threading.Thread(target=write_recording, args=(process,), daemon=True)
            writer_thread.start()
            record_led.on()
            log.info("録音開始")
            return True, "録音を開始しました"
        except Exception as exc:
            log.exception("録音開始失敗")
            if "process" in locals():
                _terminate_group(process, process.pid)
            record_process = None
            writer_thread = None
            _close_file(upload=False)
            record_led.off()
            return False, f"録音を開始できません: {exc}"


def stop_recording():
    global record_process, writer_thread
    with control_lock:
        _reconcile_recording()
        if record_process is None:
            return False
        process = record_process
        try:
            _terminate_group(process, process.pid, signal.SIGINT)
        finally:
            writer_thread.join()
            record_process = None
            writer_thread = None
            record_led.off()
        log.info("録音停止")
        return True


def emergency_stop():
    with control_lock:
        stop_recording()
        stop_radio()
        if record_led is not None:
            record_led.off()
        log.info("強制停止")


def toggle_recording():
    with control_lock:
        _reconcile_recording()
        if record_process is None:
            start_recording()
        else:
            stop_recording()


def get_volume():
    try:
        result = subprocess.run(
            ["amixer", "get", ALSA_VOLUME_CONTROL], capture_output=True,
            text=True, check=True, timeout=3,
        )
        match = re.search(r"Playback[^\n]*\[(\d+)%\]", result.stdout)
        return int(match.group(1)) if match else None
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def get_status():
    with control_lock:
        _reconcile_recording()
        recording = record_process is not None
        filepath = current_filepath
        started_at = recording_started_at
        if radio_process is not None and radio_process.poll() is not None:
            _stop_radio_locked(unexpected=True)
        radio = radio_process is not None
        return {
            "recording": recording,
            "recording_file": filepath.name if filepath else None,
            "recording_seconds": max(0, int((datetime.now() - started_at).total_seconds())) if started_at else 0,
            "radio": radio,
            "station": current_station["name"] if radio else None,
            "radio_error": radio_error,
            "volume": get_volume(),
        }


HTML = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>録音・ラジオ操作</title><style>
body{font-family:system-ui,sans-serif;background:#111827;color:#f9fafb;margin:0;padding:20px}
main{max-width:650px;margin:auto}.card{background:#1f2937;padding:20px;margin:16px 0;border-radius:14px}
button{display:block;width:100%;padding:15px;margin:8px 0;border:0;border-radius:10px;color:white;font-size:18px;cursor:pointer}
.start{background:#15803d}.stop{background:#b91c1c}.station{background:#1d4ed8}.emergency{background:#b45309}
small{color:#d1d5db}input[type=range]{width:100%}
</style></head><body><main><h1>録音・ラジオ操作</h1>
<section class="card"><h2>状態</h2><p id="recordStatus">読み込み中</p><small id="recordDetail"></small><p id="radioStatus"></p></section>
<section class="card"><h2>録音</h2><button class="start" onclick="postCommand('/api/record/start')">録音開始</button>
<button class="stop" onclick="postCommand('/api/record/stop')">録音停止</button></section>
<section class="card"><h2>ラジオ</h2>{% for station in stations %}
<button class="station" onclick='startRadio({{ station.id|tojson }})'>{{ station.name }}</button>{% endfor %}
<button class="stop" onclick="postCommand('/api/radio/stop')">ラジオ停止</button>
<label for="volume">音量: <span id="volumeValue">--</span>%</label><input id="volume" type="range" min="0" max="100" onchange="setVolume(this.value)"></section>
<section class="card"><button class="emergency" onclick="postCommand('/api/all/stop')">録音・ラジオをすべて停止</button></section>
<script>
const byId=id=>document.getElementById(id);
async function updateStatus(){try{const response=await fetch('/api/status',{cache:'no-store'});const data=await response.json();
byId('recordStatus').textContent=data.recording?'● 録音中':'録音停止';
byId('recordDetail').textContent=data.recording?`${data.recording_file} / ${data.recording_seconds}秒`:'';
byId('radioStatus').textContent=data.radio?`ラジオ再生中: ${data.station}`:(data.radio_error||'ラジオ停止');
const slider=byId('volume');slider.disabled=data.recording||data.volume===null;
if(document.activeElement!==slider&&data.volume!==null)slider.value=data.volume;
byId('volumeValue').textContent=data.volume===null?'--':data.volume;
}catch(error){byId('recordStatus').textContent='状態を取得できません';}}
async function postCommand(url,options={}){try{const response=await fetch(url,{method:'POST',...options});const data=await response.json();
if(!data.ok)alert(data.message||'操作に失敗しました');await updateStatus();}catch(error){alert('サーバーと通信できません');}}
function startRadio(id){postCommand('/api/radio/start/'+encodeURIComponent(id));}
function setVolume(value){postCommand('/api/volume',{headers:{'Content-Type':'application/json'},body:JSON.stringify({volume:Number(value)})});}
updateStatus();setInterval(updateStatus,1000);
</script></main></body></html>"""


@app.get("/")
def index():
    return render_template_string(HTML, stations=stations)


@app.get("/api/status")
def status():
    return jsonify(get_status())


@app.post("/api/record/start")
def web_record_start():
    ok, message = start_recording()
    return jsonify(ok=ok, message=message), 200 if ok else 409


@app.post("/api/record/stop")
def web_record_stop():
    return jsonify(ok=True, stopped=stop_recording())


@app.post("/api/radio/start/<station_id>")
def web_radio_start(station_id):
    station = next((item for item in stations if item["id"] == station_id), None)
    if station is None:
        return jsonify(ok=False, message="放送局が見つかりません"), 404
    ok, message = start_radio(station)
    return jsonify(ok=ok, message=message), 200 if ok else 409


@app.post("/api/radio/stop")
def web_radio_stop():
    return jsonify(ok=True, stopped=stop_radio())


@app.post("/api/all/stop")
def web_all_stop():
    emergency_stop()
    return jsonify(ok=True)


@app.post("/api/volume")
def web_volume():
    volume = (request.get_json(silent=True) or {}).get("volume")
    if type(volume) is not int or not 0 <= volume <= 100:
        return jsonify(ok=False, message="音量は0〜100の整数で指定してください"), 400
    with control_lock:
        _reconcile_recording()
        if record_process is not None:
            return jsonify(ok=False, message="録音中は音量を変更できません"), 409
        try:
            subprocess.run(["amixer", "set", ALSA_VOLUME_CONTROL, f"{volume}%"],
                           capture_output=True, check=True, timeout=3)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return jsonify(ok=False, message="音量を変更できません"), 500
    return jsonify(ok=True, volume=get_volume())


def handle_matrix_key(index):
    action = MATRIX_ACTIONS[index]
    if action == "record_toggle":
        toggle_recording()
    elif action == "record_stop":
        stop_recording()
    elif action == "all_stop":
        emergency_stop()
    elif action < len(stations):
        start_radio(stations[action])


def scan_matrix():
    # 行を一つずつ Low にして列を読み、押下が安定した時だけ操作する。
    previous = [False] * 9
    changed_at = [time.monotonic()] * 9
    stable = [False] * 9
    while not shutdown_event.is_set():
        for row_index, row in enumerate(matrix_rows):
            row.off()
            time.sleep(0.001)
            for column_index, column in enumerate(matrix_columns):
                index = row_index * 3 + column_index
                pressed = bool(column.value)
                now = time.monotonic()
                if pressed != previous[index]:
                    previous[index] = pressed
                    changed_at[index] = now
                elif pressed != stable[index] and now - changed_at[index] >= 0.05:
                    stable[index] = pressed
                    if pressed:
                        try:
                            handle_matrix_key(index)
                        except Exception:
                            log.exception("ボタン操作に失敗しました: %s", index)
            row.on()
        shutdown_event.wait(0.01)


def cleanup():
    """Web停止とは別に、プロセス終了時も同じ録音・ラジオ停止関数を使う。"""
    global cleanup_done
    with control_lock:
        if cleanup_done:
            return
        cleanup_done = True
        shutdown_event.set()
        for stop in (stop_recording, stop_radio):
            try:
                stop()
            except Exception:
                log.exception("終了処理中に子プロセスを停止できませんでした")
        with upload_lock:
            process = upload_process
            if process is not None:
                _terminate_group(process, process.pid)
        if record_led is not None:
            record_led.off()
        for device in matrix_rows + matrix_columns:
            device.close()
        if record_led is not None:
            record_led.close()
        if oled_device is not None:
            try:
                oled_device.clear()
            except Exception:
                log.exception("OLEDを消去できませんでした")
        upload_queue.put(None)


atexit.register(cleanup)


def main():
    global record_led, matrix_rows, matrix_columns, upload_thread, oled_device, oled_thread
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    def request_shutdown(signum, frame):
        log.info("終了シグナル: %s", signal.Signals(signum).name)
        shutdown_event.set()
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, request_shutdown)
    scanner = None
    server = None
    try:
        record_led = LED(RECORD_LED_GPIO)
        record_led.off()
        matrix_rows = [DigitalOutputDevice(pin, initial_value=True) for pin in MATRIX_ROWS]
        matrix_columns = [DigitalInputDevice(pin, pull_up=True) for pin in MATRIX_COLUMNS]
        scanner = threading.Thread(target=scan_matrix, daemon=True)
        scanner.start()
        upload_thread = threading.Thread(target=upload_worker, daemon=True)
        upload_thread.start()
        try:
            from luma.core.interface.serial import i2c
            from luma.oled.device import ssd1309

            serial = i2c(port=OLED_PORT, address=OLED_ADDRESS)
            oled_device = ssd1309(serial, width=128, height=64)
            oled_thread = threading.Thread(target=oled_worker, daemon=True)
            oled_thread.start()
            log.info("OLED: I2C-%s 0x%02X", OLED_PORT, OLED_ADDRESS)
        except Exception:
            log.exception("OLEDを初期化できません（本体の操作は継続します）")
        from werkzeug.serving import make_server
        server = make_server(WEB_HOST, WEB_PORT, app, threaded=True)
        server.timeout = 0.5
        log.info("Web UI: http://%s:%s", WEB_HOST, WEB_PORT)
        while not shutdown_event.is_set():
            server.handle_request()
    finally:
        shutdown_event.set()
        if scanner is not None:
            scanner.join()
        cleanup()
        if server is not None:
            server.server_close()
        if upload_thread is not None:
            upload_thread.join(timeout=5)
        if oled_thread is not None:
            oled_thread.join(timeout=2)


if __name__ == "__main__":
    main()
