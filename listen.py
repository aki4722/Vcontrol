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
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template_string, request
from gpiozero import LED
import requests

BASE_DIR = Path(__file__).resolve().parent
# 録音はまずPi本体（microSD）のローカルへ書き、完成したファイルをrcloneでGDRIVE_DIRへアップロードする。
SAVE_DIR = Path(os.environ.get("RECORDINGS_DIR", "/home/akimoto/recordings"))
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
SCHEDULES_FILE = BASE_DIR / "schedules.json"
# 同じ分に複数タスクがある場合はこの順で実行する（停止→IR→開始）。
SCHEDULE_ACTIONS = ("playback_stop", "record_stop", "ir", "record_start", "radio", "mp3", "spotify")
SCHEDULE_ACTION_LABELS = {
    "radio": "ラジオ再生", "mp3": "MP3再生", "spotify": "Spotify再生", "ir": "IR送信",
    "record_start": "録音開始", "record_stop": "録音停止", "playback_stop": "再生停止",
}
SCHEDULE_REPEATS = ("daily", "weekly", "once")
SCHEDULE_CATCHUP_SECONDS = 90
SCHEDULE_IR_GAP_SECONDS = 0.3

log = logging.getLogger(__name__)
app = Flask(__name__)
control_lock = threading.RLock()
upload_queue = queue.Queue()
upload_lock = threading.Lock()
upload_process = None
upload_generation = 0
upload_thread = None
upload_last_failed = False
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
scheduler_thread = None
scheduler_last_checked = None


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


def _normalize_schedule(data, check_targets=True):
    """Web入力・保存ファイルのスケジュール1件を検証して正規化する。不正ならValueError。
    check_targets=Falseは起動時の読み込み用（局やIRが後から消えていても実行時エラーとして扱う）。"""
    if not isinstance(data, dict):
        raise ValueError("スケジュールの形式が不正です")
    time_text = data.get("time")
    if not isinstance(time_text, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_text):
        raise ValueError("時刻はHH:MM形式で指定してください")
    repeat = data.get("repeat")
    if repeat not in SCHEDULE_REPEATS:
        raise ValueError("繰り返しの指定が不正です")
    weekdays = []
    run_date = None
    if repeat == "weekly":
        weekdays = data.get("weekdays")
        if (not isinstance(weekdays, list) or not weekdays
                or any(type(day) is not int or not 0 <= day <= 6 for day in weekdays)):
            raise ValueError("曜日を1つ以上選択してください")
        weekdays = sorted(set(weekdays))
    elif repeat == "once":
        try:
            run_date = date.fromisoformat(data.get("date") or "").isoformat()
        except (TypeError, ValueError):
            raise ValueError("実行日をYYYY-MM-DD形式で指定してください") from None
    action = data.get("action")
    if action not in SCHEDULE_ACTIONS:
        raise ValueError("実行する機能の指定が不正です")
    target = data.get("target")
    if action in ("radio", "ir"):
        try:
            target = int(target)
        except (TypeError, ValueError):
            raise ValueError("対象を選択してください") from None
        if check_targets:
            if action == "radio" and not any(item["number"] == target for item in _schedule_stations()):
                raise ValueError("指定した放送局が見つかりません")
            if action == "ir" and target not in ir_codes:
                raise ValueError("指定したIR番号が見つかりません")
    elif action == "mp3":
        if not isinstance(target, str) or not target:
            raise ValueError("対象を選択してください")
        if check_targets and target != MP3_NAME:
            raise ValueError("指定した音楽が見つかりません")
    elif action == "spotify":
        # 番号は並び順で変わるため、spotify_idで保存する。
        if not isinstance(target, str) or not target:
            raise ValueError("対象を選択してください")
        if check_targets and not any(item["spotify_id"] == target for item in _schedule_spotify_sources()):
            raise ValueError("指定したSpotify再生リストが見つかりません")
    else:
        target = None
    enabled = data.get("enabled", True)
    if type(enabled) is not bool:
        raise ValueError("有効/無効の指定が不正です")
    return {"time": time_text, "repeat": repeat, "weekdays": weekdays, "date": run_date,
            "action": action, "target": target, "enabled": enabled}


def _schedule_once_finished(task):
    """1回のみのタスクが予定日時に正常実行済みか（以前の版で無効化だけされて残ったものの掃除用）。"""
    return (task["repeat"] == "once" and bool((task.get("last_result") or {}).get("ok"))
            and task.get("last_run") == f"{task['date']}T{task['time']}")


def _load_schedules():
    """起動時に一度だけ読み込む。存在しない場合は空で開始する。
    壊れている場合は次回保存で上書きしてしまわないよう .broken へ退避してから空で開始する。"""
    if not SCHEDULES_FILE.exists():
        return {}, 1
    try:
        data = json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
        result = {}
        for item in data.get("tasks", []):
            task = _normalize_schedule(item, check_targets=False)
            task["id"] = int(item["id"])
            task["last_run"] = item.get("last_run")
            task["last_result"] = item.get("last_result")
            if _schedule_once_finished(task):
                log.info("[Scheduler] 完了済みの1回のみのタスク No.%s を削除しました", task["id"])
                continue
            result[task["id"]] = task
        next_id = int(data.get("next_id", 1))
        return result, max(next_id, max(result, default=0) + 1)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        log.error("schedules.jsonを読み込めません（空で開始します）: %s", exc)
        try:
            shutil.copy2(SCHEDULES_FILE, SCHEDULES_FILE.with_suffix(".json.broken"))
        except OSError:
            log.exception("壊れたschedules.jsonを退避できません")
        return {}, 1


def _save_schedules():
    """radio_station.txtと同じ、.tmpへ書いてからPath.replace()する原子的更新。control_lock内で呼ぶ。"""
    data = {"next_id": schedule_next_id,
            "tasks": [schedules[number] for number in sorted(schedules)]}
    temporary = SCHEDULES_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(SCHEDULES_FILE)


schedules, schedule_next_id = _load_schedules()


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


def _upload_status_text():
    """OLEDフッター2段目向け。録音の最終保存先（GDRIVE_DIR）へのアップロード状況を返す。"""
    if upload_queue.unfinished_tasks:
        return "GDRIVE UPLOADING"
    if upload_last_failed:
        return "GDRIVE UPLOAD ERR"
    return "SAVE: SD -> GDRIVE"


def _oled_lines():
    """現在の状態を128x64 OLED向けの表示要素にまとめる。"""
    with control_lock:
        _reconcile_recording()
        if radio_process is not None and radio_process.poll() is not None:
            _stop_radio_locked(unexpected=True)
        now = datetime.now()
        if keyboard_error:
            return (now.strftime("%Y-%m-%d %H:%M"), "CONFIG ERROR", keyboard_error, _upload_status_text(), _cpu_temperature_text(), True, _sd_free_text())
        if record_process is not None:
            elapsed = max(0, int((now - recording_started_at).total_seconds()))
            hours, remainder = divmod(elapsed, 3600)
            minutes, seconds = divmod(remainder, 60)
            rec_visible = int(time.monotonic() / REC_BLINK_SECONDS) % 2 == 0
            return (now.strftime("%Y-%m-%d %H:%M"), "RECORDING", f"TIME {hours:02}:{minutes:02}:{seconds:02}", _upload_status_text(), _cpu_temperature_text(), rec_visible, _sd_free_text())
        if radio_process is not None:
            label = "MP3 LOOP" if current_station.get("kind") == "mp3" else "RADIO PLAYING"
            return (now.strftime("%Y-%m-%d %H:%M"), label, _oled_station_name(current_station), _upload_status_text(), _cpu_temperature_text(), True, _sd_free_text())
        if spotify_current_source is not None:
            index = spotify_current_source.get("number", "?")
            total = len(spotify_sources)
            label = f"SPOTIFY {index}/{total}"
            if spotify_now_playing:
                detail = f"{spotify_now_playing['track']} - {spotify_now_playing['artist']}"
            else:
                detail = spotify_current_source["name"]
            return (now.strftime("%Y-%m-%d %H:%M"), label, detail, _upload_status_text(), _cpu_temperature_text(), True, _sd_free_text())
        return (now.strftime("%Y-%m-%d %H:%M"), "STANDBY", "VOICE CONTROL", _upload_status_text(), _cpu_temperature_text(), True, _sd_free_text())


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
                    # フッターは2段：1段目にCPU温度とmicroSD空き、2段目にGoogle Driveへのアップロード状況。
                    draw.text((2, 46), lines[4], font=footer_font, fill="white")
                    draw.text((126, 46), lines[6], font=footer_font, fill="white", anchor="ra")
                    draw.text((2, 54), lines[3], font=footer_font, fill="white")
                previous = lines
            shutdown_event.wait(0.5)
    except Exception:
        log.exception("OLED表示を停止しました")


def upload_worker():
    """アップロード成否にかかわらずローカルの録音ファイルは削除しない（失敗時は手動で再アップロードできる）。"""
    global upload_process, upload_last_failed
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
                upload_last_failed = True
                log.error("アップロード失敗 %s: 終了コード %s（ローカルに残します）", filepath, code)
            else:
                upload_last_failed = False
                log.info("アップロード完了: %s", filepath)
        except OSError:
            upload_last_failed = True
            log.exception("アップロード実行エラー: %s（ローカルに残します）", filepath)
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


def stop_playback():
    """録音・アップロードには触れず、再生枠（ラジオ/MP3）とSpotifyだけを既存の停止処理で止める。"""
    with control_lock:
        stopped_radio = stop_radio()
        stopped_spotify = stop_spotify()
    return stopped_radio or stopped_spotify


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


# ---- スケジューラー（時刻指定実行）----
# 専用の再生・録音・IR処理は持たず、キーボード・Web UIと同じ既存関数を同一プロセス内で呼ぶ。
# スケジュールの状態もcontrol_lockで保護し、実行（IR送信等のブロッキング処理）はロック外で行う。


def _schedule_stations():
    """キーボードと同様にstations.confを読み直す。壊れている場合は起動時の一覧を使う。"""
    try:
        return load_stations()
    except (OSError, ValueError, configparser.Error):
        return stations


def _schedule_spotify_sources():
    """Ctrl+2と同様にspotify_sources.jsonを読み直す。壊れている場合は現在の一覧を使う。"""
    try:
        return load_spotify_sources()
    except (OSError, ValueError):
        return spotify_sources


def _schedule_matches(task, minute):
    """minute（秒以下を0にしたローカル時刻）にtaskの予定があるか。有効/無効は見ない。"""
    if task["time"] != minute.strftime("%H:%M"):
        return False
    if task["repeat"] == "weekly":
        return minute.weekday() in task["weekdays"]
    if task["repeat"] == "once":
        return task["date"] == minute.date().isoformat()
    return True


def _schedule_target_label(task, station_list=None):
    action = task["action"]
    target = task["target"]
    if action == "radio":
        station = next((item for item in (station_list or _schedule_stations())
                        if item["number"] == target), None)
        return station["name"] if station else f"不明な局 No.{target}"
    if action == "ir":
        return ir_codes[target]["name"] if target in ir_codes else f"不明なIR No.{target}"
    if action == "mp3":
        return target
    if action == "spotify":
        source = next((item for item in _schedule_spotify_sources() if item["spotify_id"] == target), None)
        return source["name"] if source else "不明なSpotify再生リスト"
    return ""


def _execute_schedule_action(task):
    """スケジュール1件を既存のコア処理へ振り分ける。戻り値は (ok, message)。"""
    action = task["action"]
    target = task["target"]
    if action == "radio":
        try:
            station = next((item for item in load_stations() if item["number"] == target), None)
        except (OSError, ValueError, configparser.Error) as exc:
            return False, f"stations.confを読み込めません: {exc}"
        if station is None:
            return False, f"局番号 {target} が存在しません"
        return start_radio(station, toggle=False)
    if action == "mp3":
        if target != MP3_NAME:
            return False, f"音楽が見つかりません: {target}"
        return start_mp3()
    if action == "spotify":
        try:
            source = next((item for item in load_spotify_sources() if item["spotify_id"] == target), None)
        except (OSError, ValueError) as exc:
            return False, f"spotify_sources.jsonを読み込めません: {exc}"
        if source is None:
            return False, "Spotify再生リストが見つかりません"
        return start_spotify(source)
    if action == "ir":
        return send_ir(target)
    if action == "record_start":
        return start_recording()
    if action == "record_stop":
        return True, "録音を停止しました" if stop_recording() else "録音していません"
    if action == "playback_stop":
        return True, "再生を停止しました" if stop_playback() else "再生していません"
    return False, f"不明な機能です: {action}"


def run_due_schedules(now=None):
    """前回確認時刻からnowまでに予定時刻を迎えたタスクを実行する。
    - 起動直後の初回呼び出しは基準時刻を記録するだけ（再起動前の予定は実行しない）。
    - 時刻が大きく飛んだ場合（NTP補正等）もSCHEDULE_CATCHUP_SECONDS以上は遡らない。
    - 実行前にlast_runを保存し、同じ予定時刻を二度実行しない（時刻の巻き戻りや異常終了でも）。"""
    global scheduler_last_checked
    now = now or datetime.now()
    with control_lock:
        previous = scheduler_last_checked
        scheduler_last_checked = now
        if previous is None or shutdown_event.is_set():
            return []
        oldest = now - timedelta(seconds=SCHEDULE_CATCHUP_SECONDS)
        if previous < oldest:
            log.warning("[Scheduler] 時刻が %s から %s へ飛んだため、%s秒より前の予定は実行しません",
                        previous, now, SCHEDULE_CATCHUP_SECONDS)
        start = max(previous, oldest)
        due = []
        minute = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
        while minute <= now:
            key = minute.strftime("%Y-%m-%dT%H:%M")
            for number in sorted(schedules):
                task = schedules[number]
                if task["enabled"] and task.get("last_run") != key and _schedule_matches(task, minute):
                    task["last_run"] = key
                    due.append((minute, dict(task)))
            minute += timedelta(minutes=1)
        if not due:
            return []
        due.sort(key=lambda item: (item[0], SCHEDULE_ACTIONS.index(item[1]["action"]), item[1]["id"]))
        try:
            _save_schedules()
        except OSError:
            log.exception("[Scheduler] 実行記録を保存できません")
    results = []
    previous_action = None
    for minute, task in due:
        if task["action"] == "ir" and previous_action == "ir":
            shutdown_event.wait(SCHEDULE_IR_GAP_SECONDS)
        previous_action = task["action"]
        log.info("[Scheduler] 実行: No.%s %s %s %s", task["id"], task["time"],
                 SCHEDULE_ACTION_LABELS[task["action"]], task["target"] or "")
        try:
            ok, message = _execute_schedule_action(task)
        except Exception as exc:
            log.exception("[Scheduler] 実行中にエラーが発生しました: No.%s", task["id"])
            ok, message = False, str(exc)
        if ok:
            log.info("[Scheduler] 完了: No.%s %s", task["id"], message)
        else:
            log.error("[Scheduler] 失敗: No.%s %s", task["id"], message)
        results.append((task["id"], ok, message))
        with control_lock:
            current = schedules.get(task["id"])
            if current is None:
                continue  # 実行中に削除された
            current["last_result"] = {"ok": ok, "message": message,
                                      "at": datetime.now().isoformat(timespec="seconds")}
            # 実行中に日時を編集された場合は、編集後の予定を削除しない。
            if ok and current["repeat"] == "once" and _schedule_matches(current, minute):
                del schedules[task["id"]]
                log.info("[Scheduler] 1回のみのタスク No.%s を完了したため削除しました", task["id"])
            try:
                _save_schedules()
            except OSError:
                log.exception("[Scheduler] 実行結果を保存できません")
    return results


def scheduler_worker():
    """毎分0秒直後に一度だけ起きて期限到来タスクを確認する（busy loopにしない）。"""
    while not shutdown_event.is_set():
        try:
            run_due_schedules()
        except Exception:
            log.exception("[Scheduler] スケジュール確認に失敗しました")
        now = datetime.now()
        delay = 60 - now.second - now.microsecond / 1_000_000 + 0.2
        shutdown_event.wait(max(0.2, delay))


def list_schedules():
    with control_lock:
        station_list = _schedule_stations()
        tasks = []
        for number in sorted(schedules, key=lambda n: (schedules[n]["time"], n)):
            task = dict(schedules[number])
            task["action_label"] = SCHEDULE_ACTION_LABELS[task["action"]]
            task["target_label"] = _schedule_target_label(task, station_list)
            tasks.append(task)
        options = {
            "stations": [{"value": item["number"], "name": item["name"]} for item in station_list],
            "mp3": [{"value": MP3_NAME, "name": MP3_NAME}],
            "spotify": [{"value": item["spotify_id"], "name": f"{item['number']}. {item['name']}"}
                        for item in _schedule_spotify_sources()],
            "ir": [{"value": number, "name": ir_codes[number]["name"]} for number in sorted(ir_codes)],
        }
    return {"tasks": tasks, "options": options, "now": datetime.now().strftime("%Y-%m-%d %H:%M")}


def _check_once_in_future(task):
    if task["enabled"] and task["repeat"] == "once":
        scheduled = datetime.fromisoformat(f"{task['date']}T{task['time']}")
        if scheduled <= datetime.now():
            raise ValueError("過去の日時は指定できません")


def create_schedule(data):
    global schedule_next_id
    with control_lock:
        try:
            task = _normalize_schedule(data)
            _check_once_in_future(task)
        except ValueError as exc:
            return False, str(exc), None
        number = schedule_next_id
        task.update(id=number, last_run=None, last_result=None)
        schedules[number] = task
        schedule_next_id = number + 1
        try:
            _save_schedules()
        except OSError as exc:
            del schedules[number]
            schedule_next_id = number
            return False, f"スケジュールを保存できません: {exc}", None
        log.info("[Scheduler] 登録: No.%s", number)
        return True, "スケジュールを登録しました", task


def update_schedule(number, data):
    with control_lock:
        previous = schedules.get(number)
        if previous is None:
            return False, "指定したスケジュールが見つかりません", None
        try:
            task = _normalize_schedule(data)
            _check_once_in_future(task)
        except ValueError as exc:
            return False, str(exc), None
        task.update(id=number, last_run=previous.get("last_run"),
                    last_result=previous.get("last_result"))
        schedules[number] = task
        try:
            _save_schedules()
        except OSError as exc:
            schedules[number] = previous
            return False, f"スケジュールを保存できません: {exc}", None
        log.info("[Scheduler] 更新: No.%s", number)
        return True, "スケジュールを更新しました", task


def set_schedule_enabled(number, enabled):
    with control_lock:
        previous = schedules.get(number)
        if previous is None:
            return False, "指定したスケジュールが見つかりません"
        task = dict(previous, enabled=enabled)
        try:
            _check_once_in_future(task)
        except ValueError as exc:
            return False, f"{exc}（編集で日時を変更してください）"
        schedules[number] = task
        try:
            _save_schedules()
        except OSError as exc:
            schedules[number] = previous
            return False, f"スケジュールを保存できません: {exc}"
        log.info("[Scheduler] %s: No.%s", "有効化" if enabled else "無効化", number)
        return True, "有効にしました" if enabled else "無効にしました"


def delete_schedule(number):
    with control_lock:
        previous = schedules.pop(number, None)
        if previous is None:
            return False, "指定したスケジュールが見つかりません"
        try:
            _save_schedules()
        except OSError as exc:
            schedules[number] = previous
            return False, f"スケジュールを保存できません: {exc}"
        log.info("[Scheduler] 削除: No.%s", number)
        return True, "スケジュールを削除しました"


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
select,input[type=time],input[type=date],input[type=datetime-local]{background:var(--bg);color:var(--text);border:1px solid var(--line);
border-radius:0;padding:8px;font:inherit;font-size:16px;width:100%}
.schedule-card{border:1px solid var(--line);padding:10px 12px;margin:8px 0}
.schedule-card.disabled .schedule-body{opacity:.45}
.schedule-head{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.schedule-time{font-size:20px;font-weight:700;color:var(--amber)}
.schedule-meta{font-size:12px;color:var(--dim)}
.schedule-meta.error{color:var(--red)}
.btn-row{display:flex;gap:8px}
.btn-row button{flex:1;margin:8px 0 0;padding:10px 4px;font-size:13px}
.weekdays{display:flex;gap:4px}
.weekdays label{flex:1;display:flex;flex-direction:column;align-items:center;margin:0;padding:6px 0;
border:1px solid var(--line);color:var(--text);font-size:14px}
.weekdays input{margin:6px 0 0;width:20px;height:20px}
label.check-row{display:flex;align-items:center;gap:8px;color:var(--text);font-size:14px}
label.check-row input{width:20px;height:20px;margin:0}
#scheduleCancel[hidden]{display:none}
input[type=time],input[type=date],input[type=datetime-local]{color-scheme:dark;min-height:44px;cursor:pointer}
input::-webkit-calendar-picker-indicator{filter:invert(1);opacity:.8;cursor:pointer}
@media (max-width:420px){.tab-btn{letter-spacing:0;font-size:11px;min-width:0;overflow:hidden}}
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
<button class="tab-btn" data-tab="schedule" onclick="showTab('schedule')">予定</button>
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
<div class="tab-panel" data-tab="schedule" hidden>
<small class="schedule-meta" id="schedulePiTime"></small>
<h2 style="margin-top:12px">登録済みスケジュール</h2>
<div id="scheduleList"></div>
<h2 id="scheduleFormTitle" style="margin-top:24px">新規登録</h2>
<form id="scheduleForm" onsubmit="saveSchedule(event)" novalidate>
<label for="scheduleRepeat">繰り返し</label>
<select id="scheduleRepeat" onchange="updateScheduleForm()">
<option value="once" selected>1回のみ（日時指定）</option><option value="daily">毎日</option><option value="weekly">曜日指定</option>
</select>
<div id="scheduleDateBox"><label for="scheduleDateTime">実行日時</label><input id="scheduleDateTime" type="datetime-local" step="60" placeholder="YYYY-MM-DD HH:MM"></div>
<div id="scheduleTimeBox" hidden><label for="scheduleTime">実行時刻</label><input id="scheduleTime" type="time" placeholder="HH:MM"></div>
<div id="scheduleWeekdaysBox" hidden><label>曜日</label><div class="weekdays" id="scheduleWeekdays"></div></div>
<label for="scheduleAction">実行する機能</label>
<select id="scheduleAction" onchange="updateScheduleForm()">
<option value="radio">ラジオ再生</option><option value="mp3">MP3再生</option><option value="spotify">Spotify再生</option><option value="ir">IR送信</option>
<option value="record_start">録音開始</option><option value="record_stop">録音停止</option><option value="playback_stop">再生停止（ラジオ・MP3・Spotify）</option>
</select>
<div id="scheduleTargetBox"><label for="scheduleTarget">対象</label><select id="scheduleTarget"></select></div>
<label class="check-row"><input id="scheduleEnabled" type="checkbox" checked>有効</label>
<button class="start" type="submit" id="scheduleSubmit">登録</button>
<button type="button" id="scheduleCancel" onclick="resetScheduleForm()" hidden>編集をやめる</button>
</form>
</div>
</main>
<div class="emergency-bar"><button onclick="postCommand('/api/all/stop')">緊急停止（録音・ラジオ・MP3）</button></div>
<script>
const byId=id=>document.getElementById(id);
function showTab(name){
document.querySelectorAll('.tab-panel').forEach(el=>{el.hidden=el.dataset.tab!==name;});
document.querySelectorAll('.tab-btn').forEach(el=>{el.classList.toggle('active',el.dataset.tab===name);});
if(name==='schedule')loadSchedules();
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
const WEEKDAY_NAMES=['月','火','水','木','金','土','日'];
let scheduleData={tasks:[],options:{stations:[],mp3:[],spotify:[],ir:[]},now:''};
let editingScheduleId=null;let lastSchedulesJson=null;
byId('scheduleWeekdays').innerHTML=WEEKDAY_NAMES.map((name,index)=>`<label>${name}<input type="checkbox" value="${index}"></label>`).join('');
// 入力欄のどこをタップしてもブラウザ標準の日時ピッカーを開く（アイコンだけでなく）。
for(const id of ['scheduleDateTime','scheduleTime']){const input=byId(id);
input.addEventListener('click',()=>{if(input.showPicker){try{input.showPicker();}catch(error){}}});}
function scheduleTargetOptions(action){const o=scheduleData.options;
return action==='radio'?o.stations:action==='mp3'?o.mp3:action==='spotify'?o.spotify:action==='ir'?o.ir:null;}
function updateScheduleForm(selectedTarget){const repeat=byId('scheduleRepeat').value;
byId('scheduleWeekdaysBox').hidden=repeat!=='weekly';byId('scheduleDateBox').hidden=repeat!=='once';byId('scheduleTimeBox').hidden=repeat==='once';
const dateTime=byId('scheduleDateTime'),time=byId('scheduleTime');
if(repeat==='once'&&!dateTime.value&&time.value&&scheduleData.now)dateTime.value=scheduleData.now.slice(0,10)+'T'+time.value;
if(repeat!=='once'&&!time.value&&dateTime.value)time.value=dateTime.value.slice(11,16);
const action=byId('scheduleAction').value;const options=scheduleTargetOptions(action);
byId('scheduleTargetBox').hidden=!options;if(!options)return;
const select=byId('scheduleTarget');const key=action+JSON.stringify(options);
const current=selectedTarget!==undefined?String(selectedTarget):select.value;
if(select.dataset.key!==key){select.dataset.key=key;select.innerHTML='';
for(const item of options){const option=document.createElement('option');option.value=item.value;option.textContent=item.name;select.appendChild(option);}
if(!options.length){const option=document.createElement('option');option.value='';option.textContent='登録がありません';select.appendChild(option);}}
if([...select.options].some(option=>option.value===current))select.value=current;}
function scheduleWhen(task){return task.repeat==='daily'?'毎日':task.repeat==='weekly'?task.weekdays.map(day=>WEEKDAY_NAMES[day]).join('・'):task.date+'（1回のみ）';}
function renderSchedules(){const json=JSON.stringify(scheduleData.tasks);if(json===lastSchedulesJson)return;lastSchedulesJson=json;
const list=byId('scheduleList');list.innerHTML='';
if(!scheduleData.tasks.length){const empty=document.createElement('small');empty.className='schedule-meta';empty.textContent='登録はありません';list.appendChild(empty);return;}
for(const task of scheduleData.tasks){const card=document.createElement('div');card.className='schedule-card'+(task.enabled?'':' disabled');
const body=document.createElement('div');body.className='schedule-body';
const head=document.createElement('div');head.className='schedule-head';
const time=document.createElement('span');time.className='schedule-time';time.textContent=task.time;
const state=document.createElement('span');state.className='schedule-meta';state.textContent=(task.enabled?'有効':'無効')+' / No.'+task.id;
head.append(time,state);
const when=document.createElement('div');when.textContent=scheduleWhen(task);
const what=document.createElement('div');what.textContent=task.action_label+(task.target_label?': '+task.target_label:'');
body.append(head,when,what);
if(task.last_result){const result=document.createElement('div');result.className='schedule-meta'+(task.last_result.ok?'':' error');
result.textContent=(task.last_result.ok?'前回実行 ':'前回失敗 ')+task.last_result.at.replace('T',' ')+' '+task.last_result.message;body.appendChild(result);}
const buttons=document.createElement('div');buttons.className='btn-row';
const toggle=document.createElement('button');toggle.className=task.enabled?'stop':'start';toggle.textContent=task.enabled?'無効にする':'有効にする';
toggle.onclick=()=>scheduleRequest('/api/schedules/'+task.id+'/enabled',{enabled:!task.enabled});
const edit=document.createElement('button');edit.className='station';edit.textContent='編集';edit.onclick=()=>editSchedule(task);
const remove=document.createElement('button');remove.className='stop';remove.textContent='削除';
remove.onclick=()=>{if(confirm(`${task.time} ${task.action_label} を削除しますか？`))scheduleRequest('/api/schedules/'+task.id+'/delete');};
buttons.append(toggle,edit,remove);card.append(body,buttons);list.appendChild(card);}}
async function loadSchedules(){try{const response=await fetch('/api/schedules',{cache:'no-store'});const data=await response.json();
if(!data.ok)return;scheduleData=data;byId('schedulePiTime').textContent='Raspberry Piの現在時刻: '+data.now;
byId('scheduleDateTime').min=data.now.replace(' ','T');
renderSchedules();updateScheduleForm();}catch(error){byId('schedulePiTime').textContent='スケジュールを取得できません';}}
async function scheduleRequest(url,body){try{const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
const data=await response.json();if(!data.ok)alert(data.message||'操作に失敗しました');await loadSchedules();return data.ok;
}catch(error){alert('サーバーと通信できません');return false;}}
function resetScheduleForm(){editingScheduleId=null;byId('scheduleForm').reset();
byId('scheduleFormTitle').textContent='新規登録';byId('scheduleSubmit').textContent='登録';byId('scheduleCancel').hidden=true;updateScheduleForm();}
function editSchedule(task){editingScheduleId=task.id;byId('scheduleTime').value=task.time;byId('scheduleRepeat').value=task.repeat;
byId('scheduleWeekdays').querySelectorAll('input').forEach(input=>{input.checked=task.weekdays.includes(Number(input.value));});
byId('scheduleDateTime').value=task.date?task.date+'T'+task.time:'';byId('scheduleAction').value=task.action;byId('scheduleEnabled').checked=task.enabled;
byId('scheduleFormTitle').textContent='No.'+task.id+' を編集';byId('scheduleSubmit').textContent='更新';byId('scheduleCancel').hidden=false;
updateScheduleForm(task.target===null?undefined:task.target);byId('scheduleForm').scrollIntoView({behavior:'smooth'});}
async function saveSchedule(event){event.preventDefault();const repeat=byId('scheduleRepeat').value;
const dateTime=byId('scheduleDateTime').value.trim().replace(' ','T'),time=repeat==='once'?dateTime.slice(11,16):byId('scheduleTime').value.trim();
if(repeat==='once'&&!/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}/.test(dateTime)){alert('実行日時を選択してください（例: 2026-10-01 07:00）');return;}
if(!time){alert('実行時刻を入力してください');return;}
const body={time,repeat,
weekdays:[...byId('scheduleWeekdays').querySelectorAll('input:checked')].map(input=>Number(input.value)),
date:repeat==='once'?dateTime.slice(0,10):null,action:byId('scheduleAction').value,
target:byId('scheduleTargetBox').hidden?null:byId('scheduleTarget').value,enabled:byId('scheduleEnabled').checked};
const url=editingScheduleId===null?'/api/schedules':'/api/schedules/'+editingScheduleId;
if(await scheduleRequest(url,body))resetScheduleForm();}
setInterval(()=>{if(!document.querySelector('.tab-panel[data-tab="schedule"]').hidden)loadSchedules();},10000);
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


def _schedule_number(value):
    try:
        return int(value)
    except ValueError:
        return None


@app.get("/api/schedules")
def web_schedules():
    return jsonify(ok=True, **list_schedules())


@app.post("/api/schedules")
def web_schedule_create():
    ok, message, task = create_schedule(request.get_json(silent=True))
    return jsonify(ok=ok, message=message, task=task), 200 if ok else 400


@app.post("/api/schedules/<number>")
def web_schedule_update(number):
    task_number = _schedule_number(number)
    if task_number is None:
        return jsonify(ok=False, message="番号は整数で指定してください"), 400
    ok, message, task = update_schedule(task_number, request.get_json(silent=True))
    return jsonify(ok=ok, message=message, task=task), 200 if ok else 400


@app.post("/api/schedules/<number>/enabled")
def web_schedule_enabled(number):
    task_number = _schedule_number(number)
    enabled = (request.get_json(silent=True) or {}).get("enabled")
    if task_number is None or type(enabled) is not bool:
        return jsonify(ok=False, message="番号または有効/無効の指定が不正です"), 400
    ok, message = set_schedule_enabled(task_number, enabled)
    return jsonify(ok=ok, message=message), 200 if ok else 400


@app.post("/api/schedules/<number>/delete")
def web_schedule_delete(number):
    task_number = _schedule_number(number)
    if task_number is None:
        return jsonify(ok=False, message="番号は整数で指定してください"), 400
    ok, message = delete_schedule(task_number)
    return jsonify(ok=ok, message=message), 200 if ok else 400


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
    """起動時に一度だけ、接続済みのJQ-BTを標準SBCへ切り替える。
    外付けUSB Bluetoothドングル（TP-Link UB500、Realtek RTL8761B）+ SBC-XQ（高ビットレート）
    の組み合わせでは、Spotify再生中にランダムな瞬断（無音）が発生することを確認済みのため、
    安定性を優先してSBC-XQは使用しない。"""
    try:
        result = subprocess.run(
            ["bluetoothctl", "info", "9D:C6:55:EC:74:D5"],
            capture_output=True, text=True, check=True, timeout=3,
            env={**os.environ, "LC_ALL": "C"},
        )
        if not re.search(r"^\s*Connected:\s*yes\s*$", result.stdout, re.MULTILINE):
            log.info("JQ-BT未接続: SBCプロファイル切り替えをスキップ")
            return
        subprocess.run(
            ["pactl", "set-card-profile", "bluez_card.9D_C6_55_EC_74_D5",
             "a2dp-sink"],
            capture_output=True, text=True, check=True, timeout=3,
        )
        log.info("JQ-BT: SBCプロファイルへ切り替えました")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        log.warning("JQ-BTの接続確認またはSBCプロファイル切り替えに失敗しました（起動は継続します）: %s", exc)


def main():
    global record_led, ir_rx_led, ir_tx_led, upload_thread, oled_device, oled_thread, spotify_poll_thread
    global scheduler_thread
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
        try:
            SAVE_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.exception("録音保存先ディレクトリを作成できません: %s", SAVE_DIR)
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
        scheduler_thread = threading.Thread(target=scheduler_worker, daemon=True)
        scheduler_thread.start()
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
        if scheduler_thread is not None:
            scheduler_thread.join(timeout=2)


if __name__ == "__main__":
    main()
