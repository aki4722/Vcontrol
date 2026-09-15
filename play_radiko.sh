#!/bin/bash

STATION=${1:-}
if [[ ! "$STATION" =~ ^[A-Za-z0-9-]+$ ]]; then
    echo "station ID: NG" >&2
    exit 1
fi

MPV_AUDIO_DEVICE=${MPV_AUDIO_DEVICE:-auto}

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [ -f "$SCRIPT_DIR/.env" ]; then
    # shellcheck disable=SC1090
    source "$SCRIPT_DIR/.env"
fi

WORK_DIR=$(mktemp -d) || exit 1
RADIKO_SESSION=""
PLAYLIST_REFRESH_PID=""
cleanup() {
    if [ -n "$PLAYLIST_REFRESH_PID" ]; then
        kill "$PLAYLIST_REFRESH_PID" 2>/dev/null || true
        wait "$PLAYLIST_REFRESH_PID" 2>/dev/null || true
    fi
    if [ -n "$RADIKO_SESSION" ]; then
        curl -fs --max-time 10 -o /dev/null \
            --data-urlencode "radiko_session=$RADIKO_SESSION" \
            https://radiko.jp/v4/api/member/logout >/dev/null 2>&1 || true
    fi
    rm -rf -- "$WORK_DIR"
}
trap cleanup EXIT

PREMIUM=0
AREAFREE=0
if [ -n "${RADIKO_MAIL:-}" ] || [ -n "${RADIKO_PASSWORD:-}" ]; then
    if [ -z "${RADIKO_MAIL:-}" ] || [ -z "${RADIKO_PASSWORD:-}" ]; then
        echo "premium login: NG"
        echo "areafree: 0"
        exit 1
    fi

    if ! curl -fs --max-time 15 -o "$WORK_DIR/login.json" \
        --data-urlencode "mail=$RADIKO_MAIL" \
        --data-urlencode "pass=$RADIKO_PASSWORD" \
        https://radiko.jp/v4/api/member/login >/dev/null 2>&1; then
        echo "premium login: NG"
        echo "areafree: 0"
        exit 1
    fi

    LOGIN_RESULT=$(python3 - "$WORK_DIR/login.json" <<'PY'
import json
import sys
try:
    with open(sys.argv[1], encoding="utf-8") as file:
        data = json.load(file)
    session = data.get("radiko_session")
    paid = str(data.get("paid_member")) == "1"
    areafree = str(data.get("areafree")) == "1"
    if isinstance(session, str) and session and paid and areafree:
        print(session)
except (OSError, ValueError, TypeError):
    pass
PY
)
    if [ -z "$LOGIN_RESULT" ]; then
        echo "premium login: NG"
        echo "areafree: 0"
        exit 1
    fi
    RADIKO_SESSION=$LOGIN_RESULT
    PREMIUM=1
    AREAFREE=1
    echo "premium login: OK"
else
    echo "premium login: NG"
fi
echo "areafree: $AREAFREE"

AUTHKEY="bcd151073c03b352e1ef2fd66c32209da9ca0afa"
if ! curl -fs --max-time 15 -D "$WORK_DIR/auth1.headers" -o /dev/null \
    -H "X-Radiko-App: pc_html5" \
    -H "X-Radiko-App-Version: 0.0.1" \
    -H "X-Radiko-User: dummy_user" \
    -H "X-Radiko-Device: pc" \
    https://radiko.jp/v2/api/auth1 >/dev/null 2>&1; then
    echo "auth1: NG"
    exit 1
fi

TOKEN=$(awk 'tolower($1)=="x-radiko-authtoken:" {gsub("\r", "", $2); print $2}' "$WORK_DIR/auth1.headers")
OFFSET=$(awk 'tolower($1)=="x-radiko-keyoffset:" {gsub("\r", "", $2); print $2}' "$WORK_DIR/auth1.headers")
LENGTH=$(awk 'tolower($1)=="x-radiko-keylength:" {gsub("\r", "", $2); print $2}' "$WORK_DIR/auth1.headers")
if [ -z "$TOKEN" ] || [[ ! "$OFFSET" =~ ^[0-9]+$ ]] || [[ ! "$LENGTH" =~ ^[0-9]+$ ]]; then
    echo "auth1: NG"
    exit 1
fi
echo "auth1: OK"

PARTIALKEY=$(printf '%s' "$AUTHKEY" | dd bs=1 skip="$OFFSET" count="$LENGTH" 2>/dev/null | base64 -w 0)
AUTH2_ARGS=()
if [ "$PREMIUM" -eq 1 ]; then
    AUTH2_ARGS=(--get --data-urlencode "radiko_session=$RADIKO_SESSION")
fi
if ! curl -fs --max-time 15 -o "$WORK_DIR/auth2.txt" \
    -H "X-Radiko-AuthToken: $TOKEN" \
    -H "X-Radiko-PartialKey: $PARTIALKEY" \
    -H "X-Radiko-User: dummy_user" \
    -H "X-Radiko-Device: pc" \
    "${AUTH2_ARGS[@]}" \
    https://radiko.jp/v2/api/auth2 >/dev/null 2>&1; then
    echo "auth2: NG"
    exit 1
fi
AREA=$(cut -d, -f1 "$WORK_DIR/auth2.txt")
if [[ ! "$AREA" =~ ^JP[0-9]+$ ]]; then
    echo "auth2: NG"
    exit 1
fi
echo "auth2: OK"
echo "station ID: $STATION"

LSID=$(tr -d '-' < /proc/sys/kernel/random/uuid)
if [ "$PREMIUM" -eq 1 ]; then
    if ! curl -fs --max-time 15 -o "$WORK_DIR/stream.xml" \
        "https://radiko.jp/v3/station/stream/pc_html5/${STATION}.xml" >/dev/null 2>&1; then
        echo "stream URL取得: NG"
        exit 1
    fi
    STREAM_BASE=$(python3 - "$WORK_DIR/stream.xml" <<'PY'
import sys
import xml.etree.ElementTree as ET
try:
    root = ET.parse(sys.argv[1]).getroot()
    for item in root.findall("url"):
        if item.get("areafree") == "1" and item.get("timefree") == "0":
            url = item.findtext("playlist_create_url", "")
            if url.startswith("https://"):
                print(url)
                break
except (OSError, ET.ParseError):
    pass
PY
)
    if [ -z "$STREAM_BASE" ]; then
        echo "stream URL取得: NG"
        exit 1
    fi
    URL="${STREAM_BASE}?station_id=${STATION}&l=15&lsid=${LSID}&type=c"
else
    URL="https://alliance-stream-radiko.smartstream.ne.jp/so/playlist.m3u8?station_id=${STATION}&l=15&lsid=${LSID}&type=b"
fi

# 認証トークン付きで最初のプレイリストを実際に取得して確認する。
if ! curl -fs --max-time 15 -o "$WORK_DIR/playlist.m3u8" \
    -H "X-Radiko-AuthToken: $TOKEN" "$URL" >/dev/null 2>&1 \
    || ! grep -q '^#EXTM3U' "$WORK_DIR/playlist.m3u8"; then
    echo "stream URL取得: NG"
    exit 1
fi

if [ "$PREMIUM" -eq 1 ]; then
    MEDIA_URL=$(grep '^https://' "$WORK_DIR/playlist.m3u8" | head -n 1 | tr -d '\r')
    if [ -z "$MEDIA_URL" ] \
        || ! curl -fs --max-time 15 -o "$WORK_DIR/media.m3u8" \
            -H "X-Radiko-AuthToken: $TOKEN" "$MEDIA_URL" >/dev/null 2>&1 \
        || ! grep -q '^#EXTM3U' "$WORK_DIR/media.m3u8"; then
        echo "stream URL取得: NG"
        exit 1
    fi
fi
echo "stream URL取得: OK"

PLAY_URL=$URL
if [ "$PREMIUM" -eq 1 ]; then
    refresh_playlist() {
        while :; do
            if curl -fs --max-time 10 -o "$WORK_DIR/media.next" \
                -H "X-Radiko-AuthToken: $TOKEN" "$MEDIA_URL" >/dev/null 2>&1 \
                && grep -q '^#EXTM3U' "$WORK_DIR/media.next"; then
                mv -f "$WORK_DIR/media.next" "$WORK_DIR/media.m3u8"
            fi
            sleep 1
        done
    }
    refresh_playlist &
    PLAYLIST_REFRESH_PID=$!
    PLAY_URL=$WORK_DIR/media.m3u8
fi

# HLSの更新と音声断片の取得はmpv/FFmpegに任せる。シェルで断片を連結すると
# 境界ごとに入力が枯れやすいため、十分な先読みキャッシュを持たせる。
mpv --no-video --audio-display=no --ytdl=no \
    --audio-device="$MPV_AUDIO_DEVICE" \
    --no-terminal --msg-level=all=warn \
    --log-file="$WORK_DIR/mpv.log" \
    --cache=yes --cache-secs=30 \
    --cache-pause-initial=yes --cache-pause-wait=5 \
    --demuxer-readahead-secs=20 \
    --demuxer-lavf-o='protocol_whitelist=[file,http,https,tcp,tls,crypto]' \
    --http-header-fields="X-Radiko-AuthToken: $TOKEN" "$PLAY_URL" \
    </dev/null
MPV_EXIT=$?
if [ "$MPV_EXIT" -ne 0 ]; then
    tail -n 30 "$WORK_DIR/mpv.log" >&2
fi
exit "$MPV_EXIT"
