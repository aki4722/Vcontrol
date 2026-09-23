#!/usr/bin/env python3
"""キーボードと Web から録音・radiko を操作する常駐プロセス。"""

import atexit
import configparser
import json
import logging
import os
import queue
import re
import signal
import select
import shutil
import subprocess
import tempfile
import threading
import wave
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template_string, request
from gpiozero import LED
import requests

BASE_DIR = Path(__file__).resolve().parent
SSD_MOUNT_POINT = Path(os.environ.get("SSD_MOUNT_POINT", "/mnt/ssd"))
SAVE_DIR = Path(os.environ.get("RECORDINGS_DIR", str(SSD_MOUNT_POINT / "voice")))
SSD_LOW_SPACE_GIB = 1.0
STATIONS_FILE = BASE_DIR / "stations.conf"
RADIO_STATION_FILE = BASE_DIR / "radio_station.txt"
RADIO_SCRIPT = BASE_DIR / "play_radiko.sh"
MP3_SCRIPT = BASE_DIR / "play_mp3.sh"
MP3_NAME = "04-アクセル.mp3"
GDRIVE_DIR = os.environ.get("GDRIVE_DIR", "gdrive:音声")
SAMPLE_RATE = 16000
CHANNELS = 1
SPLIT_SECONDS = 3600
RECORD_LED_GPIO = 27
KEYBOARD_DEVICE_NAME = "aki4722 akisan08"
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "5000"))
ALSA_VOLUME_CONTROL = "Master"
OLED_PORT = int(os.environ.get("OLED_PORT", "1"), 0)
OLED_ADDRESS = int(os.environ.get("OLED_ADDRESS", "0x3c"), 0)
OLED_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
OLED_NAME_FONT = BASE_DIR / "assets/fonts/RoundedMplus1c-Regular.ttf"
OLED_FONT_SIZE = 12
OLED_FOOTER_FONT_SIZE = 9
SD_MOUNT_POINT = Path("/")
CPU_TEMP_FILE = Path("/sys/class/thermal/thermal_zone0/temp")
CPU_TEMP_UPDATE_SECONDS = 30
STORAGE_UPDATE_SECONDS = 30
SSD_PROBE_TIMEOUT_SECONDS = 2.0
REC_BLINK_SECONDS = 0.5
IR_RX_LED_GPIO = 22
IR_TX_LED_GPIO = 10
IR_RX_DEVICE = "/dev/lirc1"
IR_TX_DEVICE = "/dev/lirc0"
IR_CODES_FILE = BASE_DIR / "ir_codes.json"
IR_LEARN_TIMEOUT_SECONDS = 30
IR_SEND_TIMEOUT_SECONDS = 5
IR_STATE_IDLE = "idle"
IR_STATE_RECEIVING = "receiving"
IR_STATE_TRANSMITTING = "transmitting"
SPOTIFY_SOURCES_FILE = BASE_DIR / "spotify_sources.json"
SPOTIFY_SELECTION_FILE = BASE_DIR / "spotify_selection.json"
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REFRESH_TOKEN = os.environ.get("SPOTIFY_REFRESH_TOKEN", "")
SPOTIFY_DEVICE_NAME = os.environ.get("SPOTIFY_DEVICE_NAME", "Vcon")
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_REQUEST_TIMEOUT_SECONDS = 10
SPOTIFY_POLL_SECONDS = 5
SPOTIFY_TOKEN_REFRESH_MARGIN_SECONDS = 60
SPOTIFY_STOP_TIMEOUT_SECONDS = 3

log = logging.getLogger(__name__)
app = Flask(__name__)
control_lock = threading.RLock()
upload_queue = queue.Queue()
upload_lock = threading.Lock()
upload_process = None
upload_generation = 0
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
keyboard_error = None
current_station = None
record_led = None
cleanup_done = False
oled_device = None
oled_thread = None
spotify_poll_thread = None
cpu_temperature_text = "CPU --c"
cpu_temperature_updated_at = None
storage_status = {"mounted": False, "free_gib": None, "total_gib": None, "low": False}
storage_status_updated_at = None
sd_free_text = "SD --G"
sd_free_updated_at = None
ir_state = IR_STATE_IDLE
ir_process = None
ir_pgid = None
ir_learn_generation = 0
ir_learn_timer = None
ir_error = None
ir_rx_led = None
ir_tx_led = None
spotify_sources = []
spotify_current_source = None
spotify_now_playing = None
spotify_error = None
spotify_generation = 0
spotify_access_token = None
spotify_token_expiry = 0.0
spotify_device_id = None
spotify_configured_warned = False


def load_stations():
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(STATIONS_FILE.read_text(encoding="utf-8"))
    result = []
    numbers = set()
    for section in config.sections():
        number = int(section)
        if number <= 0 or number in numbers:
            raise ValueError(f"局番号が不正または重複しています: {section}")
        numbers.add(number)
        item = config[section]
        name = item.get("name", "").strip()
        station_id = item.get("id", "").strip()
        url = item.get("url", "").strip()
        if not name or bool(station_id) == bool(url):
            raise ValueError(f"局{number}: name と id または url を指定してください")
        if station_id and not re.fullmatch(r"[A-Za-z0-9-]+", station_id):
            raise ValueError(f"局{number}: radiko局IDが不正です")
        if url and (urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc):
            raise ValueError(f"局{number}: 配信URLが不正です")
        result.append({"number": number, "name": name, "id": station_id or str(number),
                       "url": url})
    return result


def load_station_number():
    try:
        number = int(RADIO_STATION_FILE.read_text(encoding="utf-8").strip())
        if any(station["number"] == number for station in stations):
            return number
    except (OSError, ValueError, UnicodeError):
        pass
    return 1


def save_station_number(number):
    temporary = RADIO_STATION_FILE.with_suffix(".tmp")
    temporary.write_text(f"{number}\n", encoding="utf-8")
    temporary.replace(RADIO_STATION_FILE)


stations = load_stations()
selected_station_number = load_station_number()


def _load_ir_codes():
    """起動時に一度だけ読み込む。存在しない・壊れている場合は空で開始する。"""
    try:
        data = json.loads(IR_CODES_FILE.read_text(encoding="utf-8"))
        codes = {int(number): entry for number, entry in data.get("codes", {}).items()}
        next_number = int(data.get("next_number", 1))
        return codes, max(next_number, max(codes, default=0) + 1)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return {}, 1


def _save_ir_codes():
    """radio_station.txtと同じ、.tmpへ書いてからPath.replace()する原子的更新。"""
    data = {
        "next_number": ir_next_number,
        "codes": {str(number): entry for number, entry in ir_codes.items()},
    }
    temporary = IR_CODES_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(IR_CODES_FILE)


ir_codes, ir_next_number = _load_ir_codes()


def load_spotify_sources():
    """spotify_sources.jsonを読む。未作成ならプレイリスト・アーティストとも未登録として空で始める。
    存在するが壊れている場合はload_stations()と同様に例外を投げて呼び出し元に判断させる。"""
    if not SPOTIFY_SOURCES_FILE.exists():
        return []
    data = json.loads(SPOTIFY_SOURCES_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("spotify_sources.jsonの形式が不正です（配列である必要があります）")
    result = []
    for index, item in enumerate(data):
        kind = item.get("type")
        name = (item.get("name") or "").strip()
        spotify_id = (item.get("spotify_id") or "").strip()
        if kind not in ("playlist", "artist") or not name or not spotify_id:
            raise ValueError(f"spotify_sources.json[{index}]: type/name/spotify_idを確認してください")
        result.append({"number": index + 1, "type": kind, "name": name, "spotify_id": spotify_id})
    return result


def _save_spotify_sources(sources):
    """radio_station.txtと同じ、.tmpへ書いてからPath.replace()する原子的更新。"""
    data = [{"type": item["type"], "name": item["name"], "spotify_id": item["spotify_id"]}
            for item in sources]
    temporary = SPOTIFY_SOURCES_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(SPOTIFY_SOURCES_FILE)


def _load_spotify_selection():
    try:
        data = json.loads(SPOTIFY_SELECTION_FILE.read_text(encoding="utf-8"))
        spotify_id = data.get("spotify_id")
        if isinstance(spotify_id, str) and spotify_id:
            return spotify_id
    except (OSError, ValueError, AttributeError, json.JSONDecodeError):
        pass
    return None


def _save_spotify_selection(source):
    temporary = SPOTIFY_SELECTION_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"spotify_id": source["spotify_id"], "type": source["type"]},
                                     ensure_ascii=False), encoding="utf-8")
    temporary.replace(SPOTIFY_SELECTION_FILE)


try:
    spotify_sources = load_spotify_sources()
except (OSError, ValueError, json.JSONDecodeError) as exc:
    spotify_sources = []
    spotify_error = f"spotify_sources.json: {exc}"
    log.error("Spotify再生リスト読み込み失敗: %s", exc)


def _oled_station_name(station):
    return station["name"]


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


def _sd_free_text():
    """microSD（システム用の/）の空き容量を30秒ごとに読み、OLED向けの短い文字列で返す。"""
    global sd_free_text, sd_free_updated_at
    now = time.monotonic()
    if sd_free_updated_at is None or now - sd_free_updated_at >= STORAGE_UPDATE_SECONDS:
        try:
            free_gib = shutil.disk_usage(SD_MOUNT_POINT).free / (1024 ** 3)
            sd_free_text = f"SD {free_gib:.0f}G"
        except OSError:
            sd_free_text = "SD --G"
        sd_free_updated_at = now
    return sd_free_text


def _probe_ssd_write(timeout=SSD_PROBE_TIMEOUT_SECONDS):
    """SAVE_DIR配下へ実際に小さなファイルを書き込めるか確認する。
    マウント表には残っているがデバイスが切断された「幽霊マウント」はstatvfs（空き容量取得）
    だけでは検知できず、古いキャッシュ値が返ることがあるため、実I/Oで確かめる。
    デバイス切断直後はI/Oが長時間ブロックすることがあるので、別スレッド+タイムアウトで実行し、
    時間内に完了しなければ失敗扱いにする（元スレッドの待ちはそこで打ち切り、探査スレッドは
    OSのタイムアウト任せでそのまま終了させる）。"""
    result = {}

    def probe():
        try:
            SAVE_DIR.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=SAVE_DIR, prefix=".ssd_probe_"):
                pass
            result["ok"] = True
        except OSError:
            result["ok"] = False

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    thread.join(timeout)
    return result.get("ok", False)


def _refresh_storage_status():
    """SSDの実マウント状態・実I/O・空き容量を即時確認する（キャッシュを使わない）。"""
    global storage_status, storage_status_updated_at
    if not os.path.ismount(SSD_MOUNT_POINT) or not _probe_ssd_write():
        storage_status = {"mounted": False, "free_gib": None, "total_gib": None, "low": False}
    else:
        try:
            usage = shutil.disk_usage(SSD_MOUNT_POINT)
            free_gib = usage.free / (1024 ** 3)
            storage_status = {
                "mounted": True,
                "free_gib": free_gib,
                "total_gib": usage.total / (1024 ** 3),
                "low": free_gib < SSD_LOW_SPACE_GIB,
            }
        except OSError:
            storage_status = {"mounted": False, "free_gib": None, "total_gib": None, "low": False}
    storage_status_updated_at = time.monotonic()
    return storage_status


def _cached_storage_status():
    """OLED表示向け。30秒ごとにのみ実ディスクを確認する。"""
    now = time.monotonic()
    if storage_status_updated_at is None or now - storage_status_updated_at >= STORAGE_UPDATE_SECONDS:
        return _refresh_storage_status()
    return storage_status


def _oled_lines():
    """現在の状態を128x64 OLED向けの表示要素にまとめる。"""
    with control_lock:
        _reconcile_recording()
        if radio_process is not None and radio_process.poll() is not None:
            _stop_radio_locked(unexpected=True)
        now = datetime.now()
        if keyboard_error:
            return (now.strftime("%Y-%m-%d %H:%M"), "CONFIG ERROR", keyboard_error, _cached_storage_status(), _cpu_temperature_text(), True, _sd_free_text())
        if record_process is not None:
            elapsed = max(0, int((now - recording_started_at).total_seconds()))
            hours, remainder = divmod(elapsed, 3600)
            minutes, seconds = divmod(remainder, 60)
            rec_visible = int(time.monotonic() / REC_BLINK_SECONDS) % 2 == 0
            return (now.strftime("%Y-%m-%d %H:%M"), "RECORDING", f"TIME {hours:02}:{minutes:02}:{seconds:02}", _cached_storage_status(), _cpu_temperature_text(), rec_visible, _sd_free_text())
        if radio_process is not None:
            label = "MP3 LOOP" if current_station.get("kind") == "mp3" else "RADIO PLAYING"
            return (now.strftime("%Y-%m-%d %H:%M"), label, _oled_station_name(current_station), _cached_storage_status(), _cpu_temperature_text(), True, _sd_free_text())
        if spotify_current_source is not None:
            index = spotify_current_source.get("number", "?")
            total = len(spotify_sources)
            label = f"SPOTIFY {index}/{total}"
            if spotify_now_playing:
                detail = f"{spotify_now_playing['track']} - {spotify_now_playing['artist']}"
            else:
                detail = spotify_current_source["name"]
            return (now.strftime("%Y-%m-%d %H:%M"), label, detail, _cached_storage_status(), _cpu_temperature_text(), True, _sd_free_text())
        return (now.strftime("%Y-%m-%d %H:%M"), "STANDBY", "VOICE CONTROL", _cached_storage_status(), _cpu_temperature_text(), True, _sd_free_text())


def oled_worker():
    """状態が変わった時と時刻が進んだ時だけOLEDを書き換える。"""
    from luma.core.render import canvas
    from PIL import ImageFont

    # SSD1309のmode="1"キャンバスへ直接描画し、中間階調を作らない。
    oled_font = ImageFont.truetype(OLED_FONT, OLED_FONT_SIZE)
    name_font = ImageFont.truetype(str(OLED_NAME_FONT), OLED_FONT_SIZE)
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
                    text = lines[2]
                    while text and draw.textbbox((0, 0), text, font=name_font)[2] > 124:
                        text = text[:-1]
                    draw.text((2, 31), text, font=name_font, fill="white")
                    # フッターは2段：1段目にCPU温度とmicroSD空き、2段目に録音先SSDの空き/総容量。
                    draw.text((2, 46), lines[4], font=footer_font, fill="white")
                    draw.text((126, 46), lines[6], font=footer_font, fill="white", anchor="ra")
                    storage = lines[3]
                    if not storage["mounted"]:
                        storage_text = "SSD: NOT MOUNTED"
                    elif storage["low"]:
                        storage_text = "SSD LOW SPACE"
                    else:
                        storage_text = f"SSD {storage['free_gib']:.1f}/{storage['total_gib']:.0f}GB"
                        if draw.textbbox((0, 0), storage_text, font=footer_font)[2] > 124:
                            storage_text = f"SSD {storage['free_gib']:.0f}GB"
                    draw.text((2, 54), storage_text, font=footer_font, fill="white")
                previous = lines
            shutdown_event.wait(0.5)
    except Exception:
        log.exception("OLED表示を停止しました")


def upload_worker():
    global upload_process
    while True:
        item = upload_queue.get()
        filepath = None
        process = None
        try:
            if item is None or shutdown_event.is_set():
                return
            filepath, generation = item
            with upload_lock:
                if shutdown_event.is_set():
                    return
                if generation != upload_generation:
                    continue
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
                if process is not None and upload_process is process and process.poll() is not None:
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
        with upload_lock:
            upload_queue.put((filepath, upload_generation))
    _refresh_storage_status()


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
    label = "MP3" if current_station and current_station.get("kind") == "mp3" else "ラジオ"
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
        radio_error = output.splitlines()[-1] if output else f"{label}再生プロセスが終了しました"
        log.error("%s異常終了: %s", label, output or radio_error)
    else:
        radio_error = None
    log.info("%s停止", label)
    return True


def stop_radio():
    with control_lock:
        return _stop_radio_locked()


def start_radio(station, toggle=True):
    global radio_process, radio_pgid, radio_error, current_station, keyboard_error
    global selected_station_number
    is_mp3 = station.get("kind") == "mp3"
    label = "MP3" if is_mp3 else "ラジオ"
    with control_lock:
        keyboard_error = None
        if shutdown_event.is_set():
            return False, "終了処理中です"
        _reconcile_recording()
        if record_process is not None:
            return False, f"録音中は{label}を再生できません"
        if spotify_current_source is not None:
            stop_spotify()
        same_station = (radio_process is not None and radio_process.poll() is None
                        and (current_station["id"], current_station.get("url"))
                        == (station["id"], station.get("url")))
        if same_station and not toggle:
            return True, f"{label}再生中です"
        if radio_process is not None:
            _stop_radio_locked()
        if same_station:
            return True, "ラジオを停止しました"
        try:
            command = [str(RADIO_SCRIPT), station["id"]]
            if is_mp3:
                command = ["bash", str(MP3_SCRIPT), f"{GDRIVE_DIR.rstrip('/')}/{MP3_NAME}"]
            elif station.get("url"):
                command = ["mpv", "--no-video", "--no-terminal", "--ytdl=no",
                           "--cache=yes", "--cache-secs=30",
                           "--audio-device=" + os.environ.get("MPV_AUDIO_DEVICE", "auto"),
                           "--", station["url"]]
            radio_process = subprocess.Popen(
                command, start_new_session=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            log.exception("%s開始失敗", label)
            return False, str(exc)
        current_station = station
        radio_pgid = radio_process.pid
        radio_error = None
        if not is_mp3:
            selected_station_number = station["number"]
            try:
                save_station_number(selected_station_number)
            except OSError as exc:
                radio_error = f"選局Noを保存できません: {exc}"
                log.exception("選局No保存失敗")
        log.info("%s開始: %s", label, station["name"])
        return True, f"{label}再生を開始しました"


def start_mp3():
    return start_radio({"kind": "mp3", "id": "local-mp3-loop",
                        "name": MP3_NAME}, toggle=False)


def start_recording():
    global record_process, writer_thread
    with control_lock:
        if shutdown_event.is_set():
            return False, "終了処理中です"
        _reconcile_recording()
        if record_process is not None:
            return False, "すでに録音中です"
        status = _refresh_storage_status()
        if not status["mounted"]:
            log.error("SSD (%s) が未マウントのため録音を開始できません", SSD_MOUNT_POINT)
            return False, "SSDが接続されていないため録音を開始できません"
        if status["low"]:
            log.error("SSDの空き容量不足のため録音を開始できません（残り %.2fGB）", status["free_gib"])
            return False, "SSDの空き容量が不足しているため録音を開始できません"
        _stop_radio_locked()
        stop_spotify()
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
    global upload_generation, radio_error, keyboard_error
    with control_lock:
        stop_recording()
        stop_radio()
        stop_spotify(wait=True)
        # 録音writerの終了後に世代を進め、取り出し済みの待機ジョブも無効化する。
        with upload_lock:
            upload_generation += 1
            while True:
                try:
                    upload_queue.get_nowait()
                except queue.Empty:
                    break
                else:
                    upload_queue.task_done()
            if upload_process is not None:
                _terminate_group(upload_process, upload_process.pid)
        radio_error = None
        keyboard_error = None
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


# ---- IR remote（学習・送信）----
# GPIO4/GPIO18はdtoverlay=gpio-ir/gpio-ir-txがカーネルドライバとして専有するため、
# アプリコードから直接触らず、必ず /dev/lirc1（受信）・/dev/lirc0（送信）を
# ir-ctl経由で使う。GPIO22・GPIO10はステータスLED（gpiozero）専用。


def start_ir_learning():
    """IR学習を開始する。ボタン押下でir-ctlが自動終了するまでバックグラウンドで待つ。"""
    global ir_process, ir_pgid, ir_state, ir_error, ir_learn_timer
    with control_lock:
        if shutdown_event.is_set():
            return False, "終了処理中です"
        if ir_state != IR_STATE_IDLE:
            return False, "IR操作が競合しています"
        try:
            process = subprocess.Popen(
                ["ir-ctl", "-d", IR_RX_DEVICE, "--receive", "--mode2", "--one-shot"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            log.exception("IR学習開始失敗")
            return False, str(exc)
        ir_process = process
        ir_pgid = process.pid
        ir_error = None
        ir_state = IR_STATE_RECEIVING
        ir_rx_led.on()
        generation = ir_learn_generation
        threading.Thread(target=_ir_learn_worker, args=(process, generation), daemon=True).start()
        ir_learn_timer = threading.Timer(IR_LEARN_TIMEOUT_SECONDS, cancel_ir_learning)
        ir_learn_timer.daemon = True
        ir_learn_timer.start()
        log.info("IR学習開始")
        return True, "リモコンのボタンを押してください"


def cancel_ir_learning():
    """学習中なら ir-ctl を止めて保存せず待機へ戻す。戻り値は実際に取消したか。"""
    global ir_process, ir_pgid, ir_state, ir_learn_generation, ir_learn_timer
    with control_lock:
        if ir_state != IR_STATE_RECEIVING:
            return False
        _terminate_group(ir_process, ir_pgid)
        ir_process = None
        ir_pgid = None
        ir_learn_generation += 1
        ir_state = IR_STATE_IDLE
        ir_rx_led.off()
        if ir_learn_timer is not None:
            ir_learn_timer.cancel()
            ir_learn_timer = None
        log.info("IR学習キャンセル")
        return True


def _ir_learn_worker(process, generation):
    """バックグラウンドスレッド。ir-ctlの終了をロック外でブロッキング待機してから確定する。"""
    global ir_process, ir_pgid, ir_state, ir_error, ir_next_number, ir_learn_timer
    try:
        stdout, stderr = process.communicate()
    except Exception:
        log.exception("IR受信の待機に失敗しました")
        stdout, stderr = b"", b""
    with control_lock:
        if generation != ir_learn_generation:
            return  # キャンセル済み、またはこの結果はもう無効
        ir_process = None
        ir_pgid = None
        if ir_learn_timer is not None:
            ir_learn_timer.cancel()
            ir_learn_timer = None
        lines = [line.strip() for line in stdout.decode("utf-8", "replace").splitlines()]
        signal_lines = [line for line in lines if re.fullmatch(r"(pulse|space) \d+", line)]
        if process.returncode != 0 or len(signal_lines) < 2:
            detail = stderr.decode("utf-8", "replace").strip().splitlines()
            ir_error = detail[-1] if detail else "IR信号を受信できませんでした"
            log.error("IR受信エラー: %s", ir_error)
        else:
            number = ir_next_number
            ir_codes[number] = {
                "name": f"リモコン{number}",
                "created_at": datetime.now().astimezone().isoformat(),
                "signal": signal_lines,
            }
            ir_next_number = number + 1
            try:
                _save_ir_codes()
                ir_error = None
                log.info("IR受信成功: IR No.%s を保存しました", number)
            except OSError as exc:
                del ir_codes[number]
                ir_next_number = number
                ir_error = f"IRデータを保存できません: {exc}"
                log.exception("IRデータ保存失敗")
        ir_state = IR_STATE_IDLE
        ir_rx_led.off()


def rename_ir_code(number, name):
    """登録済みIRコードの名前を変更する。"""
    with control_lock:
        if number not in ir_codes:
            return False, "指定したIR番号が見つかりません"
        name = name.strip()
        if not name:
            return False, "名前を入力してください"
        ir_codes[number]["name"] = name
        try:
            _save_ir_codes()
        except OSError as exc:
            return False, f"名前を保存できません: {exc}"
        return True, "名前を変更しました"


def send_ir(number):
    """*** IR送信の唯一の共通処理 ***
    Web UI・HTTP APIはこの関数を呼ぶだけにする。将来の物理ボタン・キーボード・
    タイマー等から呼び出す場合も、この関数を直接呼び出せばよい。"""
    global ir_state
    with control_lock:
        if shutdown_event.is_set():
            return False, "終了処理中です"
        if ir_state != IR_STATE_IDLE:
            return False, "IR操作が競合しています"
        if number not in ir_codes:
            return False, "指定したIR番号が見つかりません"
        signal_lines = ir_codes[number]["signal"]
        ir_state = IR_STATE_TRANSMITTING
        ir_tx_led.on()
    log.info("IR送信開始: IR No.%s", number)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="ascii") as file:
            file.write("\n".join(signal_lines) + "\n")
            temp_path = file.name
        process = subprocess.Popen(
            ["ir-ctl", "-d", IR_TX_DEVICE, f"--send={temp_path}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=IR_SEND_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _terminate_group(process, process.pid)
            stdout, stderr = process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip().splitlines()
            message = detail[-1] if detail else "IR送信に失敗しました"
            log.error("IR送信エラー: IR No.%s: %s", number, message)
            return False, message
        log.info("IR送信完了: IR No.%s", number)
        return True, "IR送信を完了しました"
    except OSError as exc:
        log.exception("IR送信失敗")
        return False, str(exc)
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        with control_lock:
            ir_state = IR_STATE_IDLE
            ir_tx_led.off()


def send_ir_sequence(numbers, gap_seconds=0.3):
    """複数のIR番号を順番に送信する薄いヘルパー（将来の複数機器一括操作向け）。
    1件失敗しても残りの送信は継続する。シーン管理・スケジューリングはここでは扱わない。"""
    results = []
    for index, number in enumerate(numbers):
        ok, message = send_ir(number)
        results.append((number, ok, message))
        if index < len(numbers) - 1:
            time.sleep(gap_seconds)
    overall_ok = all(ok for _, ok, _ in results)
    summary = "; ".join(f"No.{number}: {message}" for number, ok, message in results)
    return overall_ok, summary


# ---- Spotify（Web API再生制御）----
# 実際の音声出力は別systemdサービスのlibrespot（Spotify Connectデバイス、voice-control外）が
# 常時待受けして行う。ここではWeb API経由でそのデバイスへ再生を指示するだけで、librespot
# プロセス自体の起動・停止は一切行わない。


class SpotifyAPIError(Exception):
    """Spotify Web API呼び出しの失敗（認証・通信・4xx/5xxいずれも含む）をひとまとめにする。
    voice-control全体を落とさないよう、呼び出し側は必ず捕まえてspotify_errorへ格納すること。"""


def _spotify_configured():
    global spotify_configured_warned
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET and SPOTIFY_REFRESH_TOKEN:
        return True
    if not spotify_configured_warned:
        log.warning("Spotify未設定のため機能を無効化します（.envにSPOTIFY_CLIENT_ID/SECRET/REFRESH_TOKENが必要）")
        spotify_configured_warned = True
    return False


def _spotify_ensure_token():
    """アクセストークンが無い・期限間近ならrefresh_tokenで再取得する。control_lockの外で呼ぶこと。"""
    global spotify_access_token, spotify_token_expiry
    if spotify_access_token and time.monotonic() < spotify_token_expiry - SPOTIFY_TOKEN_REFRESH_MARGIN_SECONDS:
        return spotify_access_token
    try:
        response = requests.post(
            SPOTIFY_TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": SPOTIFY_REFRESH_TOKEN},
            auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
            timeout=SPOTIFY_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise SpotifyAPIError(f"アクセストークン更新に失敗しました: {exc}") from exc
    token = payload.get("access_token")
    if not token:
        raise SpotifyAPIError("アクセストークン更新の応答が不正です")
    spotify_access_token = token
    spotify_token_expiry = time.monotonic() + payload.get("expires_in", 3600)
    return token


def _spotify_request(method, path, params=None, json_body=None, retry_on_unauthorized=True):
    """control_lockの外で呼ぶこと（ネットワークI/O）。失敗は必ずSpotifyAPIErrorに変換する。"""
    global spotify_access_token
    token = _spotify_ensure_token()
    try:
        response = requests.request(
            method, f"{SPOTIFY_API_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params, json=json_body,
            timeout=SPOTIFY_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise SpotifyAPIError(f"Spotify通信エラー: {exc}") from exc
    if response.status_code == 401 and retry_on_unauthorized:
        spotify_access_token = None
        return _spotify_request(method, path, params=params, json_body=json_body,
                                 retry_on_unauthorized=False)
    if response.status_code >= 400:
        message = None
        try:
            message = response.json().get("error", {}).get("message")
        except ValueError:
            pass
        raise SpotifyAPIError(message or f"Spotify APIエラー: HTTP {response.status_code}")
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _spotify_find_device_id(force=False):
    global spotify_device_id
    if spotify_device_id and not force:
        return spotify_device_id
    data = _spotify_request("GET", "/me/player/devices") or {}
    for device in data.get("devices", []):
        if device.get("name") == SPOTIFY_DEVICE_NAME:
            spotify_device_id = device.get("id")
            return spotify_device_id
    raise SpotifyAPIError(f"Spotify再生デバイス「{SPOTIFY_DEVICE_NAME}」が見つかりません（librespotが起動・ペアリング済みか確認してください）")


def _spotify_context_uri(source):
    return f"spotify:{source['type']}:{source['spotify_id']}"


def _spotify_play_worker(source, generation):
    """control_lockの外でSpotify Web APIを呼ぶバックグラウンドスレッド（_ir_learn_workerと同じ形）。
    generationが古くなっていたら（キャンセル・別の選局で上書き済み）結果を捨てる。"""
    global spotify_current_source, spotify_error, spotify_now_playing
    try:
        try:
            device_id = _spotify_find_device_id()
            _spotify_request("PUT", "/me/player/play", params={"device_id": device_id},
                              json_body={"context_uri": _spotify_context_uri(source)})
        except SpotifyAPIError:
            # デバイスIDが古くなっている可能性があるので一度だけ再検出して再試行する。
            device_id = _spotify_find_device_id(force=True)
            _spotify_request("PUT", "/me/player/play", params={"device_id": device_id},
                              json_body={"context_uri": _spotify_context_uri(source)})
        _spotify_request("PUT", "/me/player/shuffle", params={"device_id": device_id, "state": "true"})
        log.info("[Spotify] Start %s: %s", source["type"], source["name"])
        log.info("[Spotify] Shuffle ON")
    except SpotifyAPIError as exc:
        with control_lock:
            if generation == spotify_generation:
                spotify_error = str(exc)
        log.error("[Spotify] 再生開始失敗: %s", exc)
        return
    with control_lock:
        if generation != spotify_generation:
            return
        spotify_current_source = source
        spotify_error = None
        spotify_now_playing = None
    try:
        _save_spotify_selection(source)
    except OSError as exc:
        log.exception("Spotify選択状態を保存できません: %s", exc)


def start_spotify(source):
    """公開エントリ（キーボード・Web UI共通）。HTTP通信はcontrol_lockの外の別スレッドで行う。"""
    global spotify_generation, spotify_error
    with control_lock:
        if shutdown_event.is_set():
            return False, "終了処理中です"
        if not _spotify_configured():
            return False, "Spotifyが設定されていません"
        _reconcile_recording()
        if record_process is not None:
            return False, "録音中はSpotifyを再生できません"
        if radio_process is not None:
            _stop_radio_locked()
            log.info("[Radio] Stop because Spotify started")
        spotify_generation += 1
        generation = spotify_generation
        spotify_error = None
        threading.Thread(target=_spotify_play_worker, args=(source, generation), daemon=True).start()
        return True, "Spotify再生を開始しました"


def _spotify_pause_best_effort():
    try:
        _spotify_request("PUT", "/me/player/pause",
                          params={"device_id": spotify_device_id} if spotify_device_id else None)
        log.info("[Spotify] Stop")
    except SpotifyAPIError as exc:
        log.warning("[Spotify] 一時停止に失敗しました（無視して継続）: %s", exc)


def stop_spotify(wait=False):
    """ローカル状態は即クリアし、実際のpause呼び出しは別スレッドでベストエフォート実行する
    （control_lockを長時間占有しないため）。wait=Trueの場合のみ（緊急停止用）短いタイムアウト
    付きで完了を待つ。"""
    global spotify_current_source, spotify_now_playing, spotify_generation
    with control_lock:
        if spotify_current_source is None:
            return False
        spotify_current_source = None
        spotify_now_playing = None
        spotify_generation += 1
    if not _spotify_configured():
        return True
    thread = threading.Thread(target=_spotify_pause_best_effort, daemon=True)
    thread.start()
    if wait:
        thread.join(SPOTIFY_STOP_TIMEOUT_SECONDS)
    return True


def spotify_search_artists(query):
    """アーティスト検索。呼び出し元（Flaskルート）でSpotifyAPIErrorを捕まえる。"""
    data = _spotify_request("GET", "/search", params={"q": query, "type": "artist", "limit": 10})
    items = (data or {}).get("artists", {}).get("items", [])
    return [{"name": item.get("name"), "spotify_id": item.get("id")} for item in items
            if item.get("name") and item.get("id")]


def add_spotify_artist(name, spotify_id):
    """検索結果から選ばれたアーティストをspotify_sources.jsonへ追記する（重複はしない）。"""
    global spotify_sources
    with control_lock:
        for item in spotify_sources:
            if item["type"] == "artist" and item["spotify_id"] == spotify_id:
                return item
        updated = spotify_sources + [{"number": len(spotify_sources) + 1, "type": "artist",
                                       "name": name, "spotify_id": spotify_id}]
        _save_spotify_sources(updated)
        spotify_sources = updated
        return updated[-1]


def spotify_poll_worker():
    """再生中のみ、現在再生中の曲情報を定期的に取得してWeb UI/OLED向けに保持する。"""
    global spotify_current_source, spotify_now_playing
    while not shutdown_event.is_set():
        shutdown_event.wait(SPOTIFY_POLL_SECONDS)
        with control_lock:
            source = spotify_current_source
        if source is None or not _spotify_configured():
            continue
        try:
            data = _spotify_request("GET", "/me/player/currently-playing")
        except SpotifyAPIError as exc:
            log.warning("[Spotify] 再生中情報の取得に失敗しました: %s", exc)
            continue
        with control_lock:
            if spotify_current_source is None:
                continue
            if not data or not data.get("is_playing") or not data.get("item"):
                # 外部（スマホのSpotifyアプリ等）から一時停止・停止された可能性が高い。
                spotify_current_source = None
                spotify_now_playing = None
                log.info("[Spotify] 外部要因により再生が停止しました")
                continue
            item = data["item"]
            artists = "・".join(artist.get("name", "") for artist in item.get("artists", []))
            images = item.get("album", {}).get("images", [])
            spotify_now_playing = {
                "track": item.get("name", ""),
                "artist": artists,
                "album_art_url": images[-1]["url"] if images else None,
            }


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
        playing = radio_process is not None
        mp3 = playing and current_station.get("kind") == "mp3"
        radio = playing and not mp3
        return {
            "recording": recording,
            "recording_file": filepath.name if filepath else None,
            "recording_seconds": max(0, int((datetime.now() - started_at).total_seconds())) if started_at else 0,
            "radio": radio,
            "mp3": mp3,
            "mp3_name": current_station["name"] if mp3 else None,
            "station": current_station["name"] if radio else None,
            "radio_error": radio_error,
            "volume": get_volume(),
            "ir_state": ir_state,
            "ir_error": ir_error,
            "ir_codes": [{"number": number, "name": ir_codes[number]["name"]}
                         for number in sorted(ir_codes)],
            "spotify_configured": _spotify_configured(),
            "spotify_playing": spotify_current_source is not None,
            "spotify_error": spotify_error,
            "spotify_source": spotify_current_source,
            "spotify_now_playing": spotify_now_playing,
            "spotify_sources": spotify_sources,
        }


HTML = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vcon</title><style>
:root{--bg:#0d0f0d;--panel:#1a1c1a;--line:#3a3f3a;--text:#c9d1c9;--dim:#7a827a;--amber:#f5a623;--green:#4caf50;--red:#e53935}
*{box-sizing:border-box}
body{font-family:'Courier New',ui-monospace,monospace;background:var(--bg);color:var(--text);margin:0;padding:16px 16px 96px}
main{max-width:650px;margin:auto}
h1{font-size:18px;letter-spacing:.12em;text-transform:uppercase;color:var(--amber);border-bottom:2px solid var(--amber);padding-bottom:10px;margin:4px 0 16px}
h2{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);margin:0 0 12px}
.status-panel{border:2px solid var(--line);background:var(--panel);padding:14px 16px;margin-bottom:16px}
.status-row{display:flex;align-items:center;gap:10px;padding:4px 0;font-size:14px}
.status-row small{color:var(--dim);margin-left:20px;display:block;font-size:12px}
.led{width:10px;height:10px;min-width:10px;border-radius:50%;background:#2a2e2a;border:1px solid var(--line)}
.led.on{background:var(--green);border-color:var(--green);box-shadow:0 0 6px var(--green)}
.led.error{background:var(--red);border-color:var(--red);box-shadow:0 0 6px var(--red)}
.led.blink{animation:blink 1s steps(1,end) infinite}
@keyframes blink{50%{opacity:.2}}
.tabs{display:flex;border-bottom:2px solid var(--line)}
.tab-btn{flex:1;background:#141614;color:var(--dim);border:2px solid var(--line);border-bottom:none;padding:10px 2px;
font:inherit;font-weight:700;font-size:12px;letter-spacing:.08em;text-transform:uppercase;cursor:pointer}
.tab-btn+.tab-btn{border-left:none}
.tab-btn.active{background:var(--panel);color:var(--amber);border-color:var(--amber)}
.tab-panel{border:2px solid var(--line);border-top:none;background:var(--panel);padding:20px}
.tab-panel[hidden]{display:none}
button{display:block;width:100%;padding:13px;margin:8px 0;border:2px solid var(--line);border-radius:2px;
background:#141614;color:var(--text);font:inherit;font-weight:700;font-size:14px;letter-spacing:.06em;
text-transform:uppercase;cursor:pointer}
button:hover{filter:brightness(1.2)}
.start{border-color:var(--green);color:var(--green)}
.start:hover{background:var(--green);color:#0d0f0d}
.stop{border-color:var(--red);color:var(--red)}
.stop:hover{background:var(--red);color:#0d0f0d}
.station{border-color:var(--amber);color:var(--amber)}
.station:hover{background:var(--amber);color:#0d0f0d}
label{display:block;font-size:12px;letter-spacing:.05em;color:var(--dim);text-transform:uppercase;margin-top:16px}
input[type=range]{width:100%;accent-color:var(--amber)}
table{width:100%;border-collapse:collapse;margin-top:8px}
td{border:1px solid var(--line);padding:6px 8px;font-size:13px}
input[type=text],table input{background:var(--bg);color:var(--text);border:1px solid var(--line);
padding:6px;font:inherit;width:100%}
.album-art{width:96px;height:96px;object-fit:cover;border:2px solid var(--line);margin-bottom:8px}
#spotifySources,#spotifySearchResults{display:flex;flex-wrap:wrap;gap:8px}
#spotifySources button,#spotifySearchResults button{width:auto;flex:1 1 auto;min-width:120px}
.emergency-bar{position:fixed;left:0;right:0;bottom:0;padding:14px 16px;
background:repeating-linear-gradient(45deg,#2a1010,#2a1010 12px,#160a0a 12px,#160a0a 24px);border-top:3px solid var(--red)}
.emergency-bar button{border:2px solid var(--red);background:var(--red);color:#0d0f0d;font-weight:900;
letter-spacing:.1em;max-width:650px;margin:0 auto}
</style></head><body><main><h1>Vcon</h1>
<section class="status-panel">
<div class="status-row"><span class="led" id="ledRecord"></span><span id="recordStatus">読み込み中</span></div>
<small id="recordDetail"></small>
<div class="status-row"><span class="led" id="ledRadio"></span><span id="radioStatus"></span></div>
<div class="status-row"><span class="led" id="ledIr"></span><span id="irStatus"></span></div>
<div class="status-row"><span class="led" id="ledSpotify"></span><span id="spotifyStatus"></span></div>
</section>
<nav class="tabs">
<button class="tab-btn" data-tab="record" onclick="showTab('record')">録音</button>
<button class="tab-btn" data-tab="radio" onclick="showTab('radio')">ラジオ</button>
<button class="tab-btn" data-tab="mp3" onclick="showTab('mp3')">MP3</button>
<button class="tab-btn" data-tab="ir" onclick="showTab('ir')">IR</button>
<button class="tab-btn" data-tab="spotify" onclick="showTab('spotify')">Spotify</button>
</nav>
<div class="tab-panel" data-tab="record" hidden>
<button class="start" onclick="postCommand('/api/record/start')">録音開始</button>
<button class="stop" onclick="postCommand('/api/record/stop')">録音停止</button>
</div>
<div class="tab-panel" data-tab="radio" hidden>{% for station in stations %}
<button class="station" onclick='startRadio({{ station.id|tojson }})'>{{ station.name }}</button>{% endfor %}
<button class="stop" onclick="postCommand('/api/radio/stop')">ラジオ停止</button>
<label for="volume">音量: <span id="volumeValue">--</span>%</label>
<input id="volume" type="range" min="0" max="100" onchange="setVolume(this.value)">
</div>
<div class="tab-panel" data-tab="mp3" hidden>
<button class="start" onclick="postCommand('/api/mp3/start')">04-アクセル.mp3 を繰り返し再生</button>
<button class="stop" onclick="postCommand('/api/mp3/stop')">MP3停止</button>
</div>
<div class="tab-panel" data-tab="ir" hidden>
<button class="start" onclick="postCommand('/api/ir/learn/start')">IR録音開始</button>
<button class="stop" onclick="postCommand('/api/ir/learn/stop')">IR録音停止</button>
<table id="irCodes"></table>
</div>
<div class="tab-panel" data-tab="spotify" hidden>
<p id="spotifyUnconfigured" hidden>Spotifyが設定されていません（.envを確認してください）</p>
<div id="spotifyControls">
<div id="spotifyNowPlaying">
<img id="spotifyAlbumArt" class="album-art" style="display:none">
<div id="spotifyNowTrack"></div>
<small id="spotifyNowArtist"></small>
</div>
<h2>再生リスト</h2>
<div id="spotifySources"></div>
<button class="stop" onclick="postCommand('/api/spotify/stop')">Spotify停止</button>
<label for="spotifyQuery">アーティスト検索</label>
<input id="spotifyQuery" type="text" placeholder="アーティスト名">
<button onclick="searchSpotifyArtist()">検索</button>
<div id="spotifySearchResults"></div>
</div>
</div>
</main>
<div class="emergency-bar"><button onclick="postCommand('/api/all/stop')">緊急停止（録音・ラジオ・MP3）</button></div>
<script>
const byId=id=>document.getElementById(id);
function showTab(name){
document.querySelectorAll('.tab-panel').forEach(el=>{el.hidden=el.dataset.tab!==name;});
document.querySelectorAll('.tab-btn').forEach(el=>{el.classList.toggle('active',el.dataset.tab===name);});
}
let lastIrCodesJson=null;
function renameIr(number,name){postCommand('/api/ir/rename/'+number,{headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});}
function renderIrCodes(codes){const json=JSON.stringify(codes);if(json===lastIrCodesJson)return;lastIrCodesJson=json;
const table=byId('irCodes');table.innerHTML='';
for(const c of codes){
const row=document.createElement('tr');
const numberCell=document.createElement('td');numberCell.textContent='No.'+c.number;
const nameCell=document.createElement('td');const input=document.createElement('input');
input.type='text';input.value=c.name;input.onchange=()=>renameIr(c.number,input.value);nameCell.appendChild(input);
const sendCell=document.createElement('td');const button=document.createElement('button');
button.className='station';button.textContent='送信';button.onclick=()=>postCommand('/api/ir/send/'+c.number);
sendCell.appendChild(button);
row.append(numberCell,nameCell,sendCell);table.appendChild(row);
}}
const IR_STATE_LABELS={idle:'IR待機中',receiving:'IR受信中…（リモコンのボタンを押してください）',transmitting:'IR送信中…'};
async function updateStatus(){try{const response=await fetch('/api/status',{cache:'no-store'});const data=await response.json();
byId('recordStatus').textContent=data.recording?'● 録音中':'録音停止';
byId('recordDetail').textContent=data.recording?`${data.recording_file} / ${data.recording_seconds}秒`:'';
byId('ledRecord').className='led'+(data.recording?' on blink':'');
byId('radioStatus').textContent=data.radio_error|| (data.mp3?`MP3ループ再生（取得中を含む）: ${data.mp3_name}`:data.radio?`ラジオ再生中: ${data.station}`:'ラジオ・MP3停止');
byId('ledRadio').className='led'+(data.radio_error?' error':(data.radio||data.mp3)?' on':'');
const slider=byId('volume');slider.disabled=data.recording||data.volume===null;
if(document.activeElement!==slider&&data.volume!==null)slider.value=data.volume;
byId('volumeValue').textContent=data.volume===null?'--':data.volume;
byId('irStatus').textContent=data.ir_error||IR_STATE_LABELS[data.ir_state]||'';
byId('ledIr').className='led'+(data.ir_error?' error':(data.ir_state&&data.ir_state!=='idle')?' on blink':'');
renderIrCodes(data.ir_codes||[]);
byId('spotifyUnconfigured').hidden=!!data.spotify_configured;
byId('spotifyControls').hidden=!data.spotify_configured;
byId('spotifyStatus').textContent=data.spotify_error||(data.spotify_playing?`Spotify再生中: ${data.spotify_source?data.spotify_source.name:''}`:'Spotify停止');
byId('ledSpotify').className='led'+(data.spotify_error?' error':data.spotify_playing?' on':'');
renderSpotifySources(data.spotify_sources||[]);
const nowPlaying=data.spotify_now_playing;
byId('spotifyNowTrack').textContent=nowPlaying?nowPlaying.track:'';
byId('spotifyNowArtist').textContent=nowPlaying?nowPlaying.artist:'';
const art=byId('spotifyAlbumArt');
if(nowPlaying&&nowPlaying.album_art_url){art.src=nowPlaying.album_art_url;art.style.display='block';}else{art.style.display='none';}
}catch(error){byId('recordStatus').textContent='状態を取得できません';}}
async function postCommand(url,options={}){try{const response=await fetch(url,{method:'POST',...options});const data=await response.json();
if(!data.ok)alert(data.message||'操作に失敗しました');await updateStatus();}catch(error){alert('サーバーと通信できません');}}
function startRadio(id){postCommand('/api/radio/start/'+encodeURIComponent(id));}
function setVolume(value){postCommand('/api/volume',{headers:{'Content-Type':'application/json'},body:JSON.stringify({volume:Number(value)})});}
function startSpotify(number){postCommand('/api/spotify/start/'+number);}
let lastSpotifySourcesJson=null;
function renderSpotifySources(sources){const json=JSON.stringify(sources);if(json===lastSpotifySourcesJson)return;lastSpotifySourcesJson=json;
const box=byId('spotifySources');box.innerHTML='';
for(const source of sources){const button=document.createElement('button');button.className='station';
button.textContent=source.number+'. '+source.name;button.onclick=()=>startSpotify(source.number);box.appendChild(button);}}
async function searchSpotifyArtist(){const query=byId('spotifyQuery').value.trim();if(!query)return;
try{const response=await fetch('/api/spotify/search?q='+encodeURIComponent(query));const data=await response.json();
if(!data.ok){alert(data.message||'検索に失敗しました');return;}
renderSpotifySearchResults(data.results||[]);
}catch(error){alert('サーバーと通信できません');}}
function renderSpotifySearchResults(results){const box=byId('spotifySearchResults');box.innerHTML='';
for(const result of results){const button=document.createElement('button');button.className='station';
button.textContent=result.name;
button.onclick=()=>postCommand('/api/spotify/add_artist',{headers:{'Content-Type':'application/json'},body:JSON.stringify({name:result.name,spotify_id:result.spotify_id})});
box.appendChild(button);}}
showTab('record');updateStatus();setInterval(updateStatus,1000);
</script></body></html>"""


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


@app.post("/api/mp3/start")
def web_mp3_start():
    ok, message = start_mp3()
    return jsonify(ok=ok, message=message), 200 if ok else 409


@app.post("/api/mp3/stop")
def web_mp3_stop():
    with control_lock:
        stopped = stop_radio() if current_station and current_station.get("kind") == "mp3" else False
        return jsonify(ok=True, stopped=stopped)


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


@app.post("/api/ir/learn/start")
def web_ir_learn_start():
    ok, message = start_ir_learning()
    return jsonify(ok=ok, message=message), 200 if ok else 409


@app.post("/api/ir/learn/stop")
def web_ir_learn_stop():
    return jsonify(ok=True, cancelled=cancel_ir_learning())


@app.post("/api/ir/send/<number>")
def web_ir_send(number):
    try:
        code_number = int(number)
    except ValueError:
        return jsonify(ok=False, message="番号は整数で指定してください"), 400
    ok, message = send_ir(code_number)
    return jsonify(ok=ok, message=message), 200 if ok else 409


@app.post("/api/ir/rename/<number>")
def web_ir_rename(number):
    try:
        code_number = int(number)
    except ValueError:
        return jsonify(ok=False, message="番号は整数で指定してください"), 400
    name = (request.get_json(silent=True) or {}).get("name", "")
    ok, message = rename_ir_code(code_number, name)
    return jsonify(ok=ok, message=message), 200 if ok else 400


@app.post("/api/spotify/start/<number>")
def web_spotify_start(number):
    try:
        index_number = int(number)
    except ValueError:
        return jsonify(ok=False, message="番号は整数で指定してください"), 400
    with control_lock:
        source = next((item for item in spotify_sources if item["number"] == index_number), None)
    if source is None:
        return jsonify(ok=False, message="指定した番号のSpotify再生リストが見つかりません"), 404
    ok, message = start_spotify(source)
    return jsonify(ok=ok, message=message), 200 if ok else 409


@app.post("/api/spotify/stop")
def web_spotify_stop():
    return jsonify(ok=True, stopped=stop_spotify())


@app.get("/api/spotify/search")
def web_spotify_search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify(ok=False, message="検索語を入力してください"), 400
    if not _spotify_configured():
        return jsonify(ok=False, message="Spotifyが設定されていません"), 409
    try:
        results = spotify_search_artists(query)
    except SpotifyAPIError as exc:
        return jsonify(ok=False, message=str(exc)), 502
    return jsonify(ok=True, results=results)


@app.post("/api/spotify/add_artist")
def web_spotify_add_artist():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    spotify_id = (body.get("spotify_id") or "").strip()
    if not name or not spotify_id:
        return jsonify(ok=False, message="アーティスト情報が不正です"), 400
    try:
        source = add_spotify_artist(name, spotify_id)
    except OSError as exc:
        return jsonify(ok=False, message=f"再生リストを保存できません: {exc}"), 500
    ok, message = start_spotify(source)
    return jsonify(ok=ok, message=message, source=source), 200 if ok else 409


def handle_keyboard_key(number):
    global stations, radio_error, keyboard_error, spotify_sources, spotify_error
    with control_lock:
        if number in (1, 5):
            keyboard_error = None
            try:
                configured_stations = sorted(load_stations(), key=lambda item: item["number"])
                station_number = selected_station_number
                playing = (radio_process is not None and radio_process.poll() is None
                           and current_station.get("kind") != "mp3")
                if playing:
                    numbers = [item["number"] for item in configured_stations]
                    if current_station["number"] in numbers:
                        index = numbers.index(current_station["number"])
                        station_number = numbers[(index + (1 if number == 1 else -1)) % len(numbers)]
                    else:
                        station_number = 1
                elif not any(item["number"] == station_number for item in configured_stations):
                    station_number = 1
                station = next((item for item in configured_stations
                                if item["number"] == station_number), None)
                if station is None:
                    keyboard_error = f"NO STATION {station_number}"
                    raise ValueError(f"Ctrl+{number}: 局番号 {station_number} が存在しません")
            except (OSError, ValueError, configparser.Error) as exc:
                keyboard_error = keyboard_error or f"CTRL{number} CONFIG"
                radio_error = str(exc)
                log.error("キー割り当てエラー: %s", exc)
                return
            stations = configured_stations
            keyboard_error = None
            radio_error = None
            start_radio(station, toggle=False)
        elif number == 4:
            keyboard_error = None
            radio_error = None
            toggle_recording()
        elif number in (2, 6):
            log.info("[Spotify] Ctrl+%s pressed", number)
            if not _spotify_configured():
                return
            try:
                configured_sources = load_spotify_sources()
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                spotify_error = str(exc)
                log.error("Spotify再生リスト読み込みエラー: %s", exc)
                return
            spotify_sources = configured_sources
            if not configured_sources:
                spotify_error = "Spotify再生リストが空です（spotify_sources.jsonを確認してください）"
                return
            playing = spotify_current_source is not None
            if number == 6 and not playing:
                return  # 停止中のCtrl+6は何もしない（仕様通り）
            if not playing:
                selected_id = _load_spotify_selection()
                source = next((item for item in configured_sources
                              if item["spotify_id"] == selected_id), configured_sources[0])
            else:
                ids = [item["spotify_id"] for item in configured_sources]
                current_id = spotify_current_source["spotify_id"]
                if current_id in ids:
                    index = ids.index(current_id)
                    source = configured_sources[(index + (1 if number == 2 else -1)) % len(ids)]
                    log.info("[Spotify] %s source: %s",
                             "Next" if number == 2 else "Previous", source["name"])
                else:
                    source = configured_sources[0]
            start_spotify(source)
        elif number == 8:
            emergency_stop()


class KeyboardShortcuts:
    """デバイスごとに押下状態を保持し、長押しのリピートを無視する。"""

    def __init__(self, codes):
        self.codes = codes
        self.pressed = set()
        self.sync_lost = False
        self.numbers = {getattr(codes, f"KEY_{number}"): number for number in range(1, 9)}

    def feed(self, event):
        if event.type != self.codes.EV_KEY:
            return
        if event.value == 0:
            self.pressed.discard(event.code)
            return
        if event.value != 1 or event.code in self.pressed:
            return
        self.pressed.add(event.code)
        if self.pressed.intersection((self.codes.KEY_LEFTCTRL, self.codes.KEY_RIGHTCTRL)):
            number = self.numbers.get(event.code)
            if number is not None:
                handle_keyboard_key(number)


def keyboard_worker():
    """evdevで直接読むため端末・Enter不要。抜き差しは1秒ごとに再検出する。"""
    from evdev import InputDevice, ecodes, list_devices

    devices = {}
    retry_at = 0
    warned = set()
    try:
        while not shutdown_event.is_set():
            if time.monotonic() >= retry_at:
                for path in list_devices():
                    if path in devices:
                        continue
                    device = None
                    try:
                        device = InputDevice(path)
                        if device.name != KEYBOARD_DEVICE_NAME:
                            device.close()
                            continue
                        keys = device.capabilities().get(ecodes.EV_KEY, [])
                        if ecodes.KEY_1 not in keys or not any(
                            key in keys for key in (ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL)
                        ):
                            device.close()
                            continue
                        shortcuts = KeyboardShortcuts(ecodes)
                        shortcuts.pressed.update(device.active_keys())
                        devices[path] = (device, shortcuts)
                        log.info("キーボード接続: %s (%s)", device.name, path)
                    except OSError:
                        if device is not None:
                            device.close()
                        if path not in warned:
                            log.exception("キーボードを開けません: %s（input権限を確認）", path)
                            warned.add(path)
                if not devices and "missing" not in warned:
                    log.warning("対象キーボード %r がありません。接続と/dev/inputの読み取り権限を確認してください", KEYBOARD_DEVICE_NAME)
                    warned.add("missing")
                retry_at = time.monotonic() + 1
            if not devices:
                shutdown_event.wait(0.5)
                continue
            ready, _, _ = select.select([item[0] for item in devices.values()], [], [], 0.1)
            for device in ready:
                shortcuts = devices[device.path][1]
                try:
                    for event in device.read():
                        if event.type == ecodes.EV_SYN and event.code == ecodes.SYN_DROPPED:
                            shortcuts.sync_lost = True
                            continue
                        if shortcuts.sync_lost:
                            if event.type == ecodes.EV_SYN and event.code == ecodes.SYN_REPORT:
                                shortcuts.pressed = set(device.active_keys())
                                shortcuts.sync_lost = False
                            continue
                        try:
                            shortcuts.feed(event)
                        except Exception:
                            log.exception("キーボード操作に失敗しました")
                except OSError:
                    devices.pop(device.path)
                    device.close()
                    log.info("キーボード切断: %s", device.path)
    finally:
        for device, _ in devices.values():
            device.close()


def cleanup():
    """Web停止とは別に、プロセス終了時も同じ録音・ラジオ停止関数を使う。"""
    global cleanup_done
    with control_lock:
        if cleanup_done:
            return
        cleanup_done = True
        shutdown_event.set()
        for stop in (stop_recording, stop_radio, cancel_ir_learning, stop_spotify):
            try:
                stop()
            except Exception:
                log.exception("終了処理中に子プロセスを停止できませんでした")
        with upload_lock:
            process = upload_process
            if process is not None:
                _terminate_group(process, process.pid)
        for led in (record_led, ir_rx_led, ir_tx_led):
            if led is not None:
                led.off()
        for led in (record_led, ir_rx_led, ir_tx_led):
            if led is not None:
                led.close()
        if oled_device is not None:
            try:
                oled_device.clear()
            except Exception:
                log.exception("OLEDを消去できませんでした")
        upload_queue.put(None)


atexit.register(cleanup)


def configure_bluetooth_audio():
    """起動時に一度だけ、接続済みのJQ-BTをSBC-XQへ切り替える。"""
    try:
        result = subprocess.run(
            ["bluetoothctl", "info", "9D:C6:55:EC:74:D5"],
            capture_output=True, text=True, check=True, timeout=3,
            env={**os.environ, "LC_ALL": "C"},
        )
        if not re.search(r"^\s*Connected:\s*yes\s*$", result.stdout, re.MULTILINE):
            log.info("JQ-BT未接続: SBC-XQ切り替えをスキップ")
            return
        subprocess.run(
            ["pactl", "set-card-profile", "bluez_card.9D_C6_55_EC_74_D5",
             "a2dp-sink-sbc_xq"],
            capture_output=True, text=True, check=True, timeout=3,
        )
        log.info("JQ-BT: SBC-XQプロファイルへ切り替えました")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        log.warning("JQ-BTの接続確認またはSBC-XQ切り替えに失敗しました（起動は継続します）: %s", exc)


def main():
    global record_led, ir_rx_led, ir_tx_led, upload_thread, oled_device, oled_thread, spotify_poll_thread
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    def request_shutdown(signum, frame):
        log.info("終了シグナル: %s", signal.Signals(signum).name)
        shutdown_event.set()
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, request_shutdown)
    # 依存不足をバックグラウンドスレッドだけの失敗にしない。
    import evdev  # noqa: F401

    scanner = None
    server = None
    try:
        configure_bluetooth_audio()
        status = _refresh_storage_status()
        if status["mounted"]:
            try:
                SAVE_DIR.mkdir(parents=True, exist_ok=True)
            except OSError:
                log.exception("録音保存先ディレクトリを作成できません: %s", SAVE_DIR)
        else:
            log.error("SSD (%s) が未マウントです。録音保存先 %s は作成しません", SSD_MOUNT_POINT, SAVE_DIR)
        record_led = LED(RECORD_LED_GPIO)
        record_led.off()
        ir_rx_led = LED(IR_RX_LED_GPIO)
        ir_tx_led = LED(IR_TX_LED_GPIO)
        ir_rx_led.off()
        ir_tx_led.off()
        scanner = threading.Thread(target=keyboard_worker, daemon=True)
        scanner.start()
        upload_thread = threading.Thread(target=upload_worker, daemon=True)
        upload_thread.start()
        spotify_poll_thread = threading.Thread(target=spotify_poll_worker, daemon=True)
        spotify_poll_thread.start()
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
        if spotify_poll_thread is not None:
            spotify_poll_thread.join(timeout=2)


if __name__ == "__main__":
    main()
