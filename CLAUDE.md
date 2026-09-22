# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-process daemon (`listen.py`) that runs on a Raspberry Pi and turns it into a
combination voice/radio recorder appliance. It is controlled by a dedicated USB keyboard
(Ctrl+1–8 shortcuts), a small Flask web UI, and shows live status on a 128x64 I2C OLED.
There is no speech recognition anywhere in this project despite the repo name — "voice
control" refers to physical key/web control of recording, not speech input.

Everything runs as one systemd service (`voice-control.service`) executing
`venv/bin/python listen.py`. There is no build step; this is a plain Python 3 script plus
a couple of bash helper scripts it shells out to.

## Commands

```sh
# install (system Python 3 lacks Flask — always use the venv)
python3 -m venv --system-site-packages venv
venv/bin/pip install -r requirements.txt

# run tests (stdlib unittest, no pytest installed)
venv/bin/python -m unittest tests/test_controls.py -v
# single test:
venv/bin/python -m unittest tests.test_controls.ControlsTest.test_radio_resume_cycle_and_emergency_stop -v

# run manually (must use venv interpreter; needs `input` group membership for keyboard access)
venv/bin/python listen.py

# deploy/reload as the systemd service
sudo cp voice-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now voice-control.service
sudo systemctl restart voice-control.service   # after any code/config change
systemctl status voice-control.service
journalctl -u voice-control.service -f

# OLED hardware smoke test (independent of the service)
sudo modprobe i2c-dev
i2cdetect -y 1
venv/bin/python oled_test.py --address 0x3c
```

Tests mock out subprocess/hardware calls (`gpiozero.LED`, `subprocess.Popen`, evdev), so
they run fine off-Pi. `tests/test_controls.py` imports `listen` as `app` and pokes at its
module-level globals directly — there's no dependency injection, so tests reset globals
manually in `setUp`.

## Architecture

Everything lives in `listen.py` as one file with module-level mutable state guarded by a
single `threading.RLock()` (`control_lock`). Three background threads plus the Flask
server (run via `werkzeug.serving.make_server`, polled in a loop instead of `app.run()`)
share this state:

- **`keyboard_worker`** — polls `evdev.list_devices()` every second for a device whose
  `.name` exactly matches `KEYBOARD_DEVICE_NAME` ("aki4722 akisan08"), reading raw input
  events directly (no terminal/Enter involved). `KeyboardShortcuts` tracks Ctrl state and
  fires `handle_keyboard_key(1-8)` only on fresh keydown (`value=1`) while Ctrl is held;
  repeats (`value=2`) are ignored. Handles hot-plug and `SYN_DROPPED` resync.
- **`upload_worker`** — single consumer of `upload_queue`, uploads finished WAV files via
  `rclone copyto` to `GDRIVE_DIR`. Uses an `upload_generation` counter so that an
  emergency stop can invalidate queued-but-not-yet-started uploads without racing the
  worker.
- **`oled_worker`** — redraws the SSD1309 (via `luma.oled`) only when the tuple of
  displayed fields changes, polling every 0.5s. Optional: if I2C init fails, the service
  logs and continues without a display.
- **Flask app** — serves a single inlined HTML/JS page (`HTML` string, no templates dir)
  and a small JSON API (`/api/record/*`, `/api/radio/*`, `/api/mp3/*`, `/api/ir/*`,
  `/api/all/stop`, `/api/status`, `/api/volume`) that all funnel through the same
  functions the keyboard uses (`start_recording`, `stop_recording`, `start_radio`,
  `stop_radio`, `start_mp3`, `emergency_stop`).

**Radio, MP3-loop, and recording are mutually exclusive** and share one "playback slot"
(`radio_process`/`radio_pgid`/`current_station`) — starting one stops whatever is
currently active. MP3 loop playback is implemented as a fake station
(`{"kind": "mp3", ...}`) pushed through `start_radio()`, which is why it shares the same
process-group teardown logic (`_terminate_group`) as radiko playback and does not
overwrite the persisted station selection.

**Station selection persistence**: `stations.conf` (INI, sections are 1-based numbers,
each with `name` + either `id` (radiko station ID) or `url` (direct HTTP(S) stream mpv
can play)) is reloaded from disk on every Ctrl+1/Ctrl+5 press — not cached — so editing
it takes effect without a restart for keyboard control (the web UI's station list is
still fixed at process start). The currently selected station number is persisted to
`radio_station.txt` via a write-to-temp-then-`Path.replace` (atomic rename) in
`save_station_number`. `keybindings.conf` is legacy/unused — the mapping is hardcoded in
`handle_keyboard_key`.

**Subprocess lifecycle**: every child (`arecord`, radiko's `play_radiko.sh`, mpv,
`play_mp3.sh`, rclone) is started with `start_new_session=True` and torn down via
`_terminate_group`, which signals the whole process group (not just the direct child) —
necessary because `play_radiko.sh`/`play_mp3.sh` are shell scripts that spawn mpv/rclone
as children of the shell, and the shell can exit before its child does.

**Recording**: `arecord` streams raw PCM to a pipe; `write_recording` (in its own thread)
writes it into hourly-rotated WAV files (`SPLIT_SECONDS = 3600`) under
`RECORDINGS_DIR`/`SAVE_DIR`, queuing each closed file for upload.

**Bluetooth**: `configure_bluetooth_audio()` runs once at startup only — checks whether
the fixed JQ-BT speaker MAC is already connected and if so switches its PipeWire profile
to `a2dp-sink-sbc_xq` for better quality. No reconnection/retry logic lives in `listen.py`
itself; `connect-jqbt.sh` is a separate, not-invoked-by-the-service script for manually
reconnecting/re-pairing the speaker and re-pointing the PipeWire default sink.

**Errors surface in two places that must stay in sync**: `radio_error` (playback/process
failures) and `keyboard_error` (bad `stations.conf` / missing station number) are both
read by `_oled_lines()` (OLED shows `CONFIG ERROR` + detail) and exposed via
`/api/status` for the web UI.

**IR remote (learn + send)**: a separate state machine (`ir_state`:
`idle`/`receiving`/`transmitting`) reuses the same `control_lock` rather than adding a
second lock, but is otherwise independent of the recording/radio slot — starting/stopping
IR learning or sending never checks or touches recording/radio state and vice versa. All
IR I/O shells out to `ir-ctl` (v4l-utils) against `/dev/lirc1` (receive) and `/dev/lirc0`
(send) — never touch GPIO4/GPIO18 directly, they're owned by the kernel's
`gpio-ir`/`gpio-ir-tx` drivers via dtoverlays already active in `config.txt`.
`start_ir_learning()` spawns `ir-ctl --receive --mode2 --one-shot` (it exits on its own
once a remote button is pressed) and a background thread (`_ir_learn_worker`) blocks on
`process.communicate()` outside the lock — same shape as `write_recording`/
`upload_worker` — then re-takes the lock to parse the captured `pulse N`/`space N` lines,
assign the next number, and persist. A generation counter (`ir_learn_generation`, same
idiom as `upload_generation`) lets `cancel_ir_learning()` discard a worker's result if it
finishes after being cancelled. `send_ir(number)` is the single shared entry point for
transmitting — Flask routes are thin callers, and any future caller (physical button,
keyboard, timer, another device) should call it directly rather than duplicating send
logic. IR codes persist in `ir_codes.json` (JSON, not INI/plain-text like the radio state
files, because each code is a variable-length list of raw pulse/space values) using the
same atomic `.tmp`-then-`Path.replace()` write pattern as `save_station_number`.

## Hardware / environment specifics

- Target device is a Raspberry Pi running Raspberry Pi OS (`platform` reports
  `7.0.0-1019-raspi`).
- GPIO27 → LED (via `gpiozero.LED`) lights while recording; blinks at 0.5s via the OLED
  loop's `REC_BLINK_SECONDS`. No other GPIO/button matrix is used.
- IR add-on board: GPIO4 = IR receive, GPIO18 = IR transmit (transistor-driven) — both
  claimed by kernel dtoverlays (`gpio-ir`/`gpio-ir-tx`, already active), so application
  code accesses them only via `/dev/lirc1`/`/dev/lirc0` through the `ir-ctl` subprocess,
  never via `gpiozero`/raw GPIO. GPIO22 → IR-receiving LED, GPIO10 → IR-sending LED (same
  `gpiozero.LED` pattern as GPIO27, distinct purpose — don't conflate them). `/dev/lirc*`
  are `root:video` (fixed OS udev rule), so both the systemd service
  (`SupplementaryGroups=`) and any manual-run user need the `video` group.
- OLED is a 128x64 SSD1309 over I2C (SDA=GPIO2/pin3, SCL=GPIO3/pin5), addressed via
  `luma.oled`; bus/address configurable through `.env` (`OLED_PORT`, `OLED_ADDRESS`).
  Station names render with the bundled `assets/fonts/RoundedMplus1c-Regular.ttf`
  (Japanese support); everything else uses system DejaVu Sans Mono.
- The control keyboard must report exactly the device name `aki4722 akisan08`; any other
  keyboard is ignored entirely, and SSH-sent keys don't reach evdev so they never trigger
  shortcuts either.
- Audio output routing is controlled by `MPV_AUDIO_DEVICE` (set in the systemd unit to a
  fixed PipeWire Bluetooth sink); `mpv --audio-device=help` lists valid values.
- External CLI dependencies (not in `requirements.txt`, must be present on the system):
  `arecord`/`amixer` (alsa-utils), `curl`, `mpv`, `rclone`, `bluetoothctl`/`pactl`
  (PipeWire/BlueZ), `ir-ctl` (v4l-utils).
- Secrets/config live in `.env` (radiko premium credentials, OLED bus/address, audio
  device) loaded both by `listen.py` (`os.environ`) and sourced directly by
  `play_radiko.sh`. Never read or print `.env` contents.
- The systemd unit runs as user `akimoto` with supplementary groups `gpio`, `i2c`,
  `dialout`, `input`, `video` — these are required for GPIO/I2C access, raw keyboard
  reads, and `/dev/lirc*` access; manual (non-systemd) runs need the invoking user in
  `input`/`video` groups too.

## Editing conventions already in the code

- Comments and log/error/UI strings are in Japanese; match that when touching existing
  strings. Code identifiers are English.
- Prefer extending the existing single-file structure and the shared `control_lock`
  pattern rather than introducing new concurrency primitives — the whole design relies on
  every state-mutating function taking `control_lock` and being safe to call from either
  the keyboard thread or a Flask request handler.
- The README (`README.md`, Japanese) is the source of truth for exact keybinding/behavior
  specs (e.g. exact edge cases around toggling, error recovery, what does/doesn't reset
  the saved station). Check it before changing control-flow behavior, and update it when
  behavior changes.
