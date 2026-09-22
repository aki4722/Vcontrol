#!/bin/bash
# rclone取得とmpvを同じプロセスグループで管理する。
set -u
REMOTE=${1:?Google Driveのファイルパスを指定してください}
WORK_DIR=$(mktemp -d) || exit 1
CHILD_PID=""
cleanup() {
    if [ -n "$CHILD_PID" ]; then
        kill "$CHILD_PID" 2>/dev/null || true
        wait "$CHILD_PID" 2>/dev/null || true
    fi
    rm -rf -- "$WORK_DIR"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

rclone copyto "$REMOTE" "$WORK_DIR/audio.mp3" \
    --contimeout 10s --timeout 30s --retries 3 --retries-sleep 5s \
    --low-level-retries 10 --tpslimit 1 --tpslimit-burst 1 \
    >"$WORK_DIR/download.log" 2>&1 &
CHILD_PID=$!
if ! wait "$CHILD_PID"; then
    CHILD_PID=""
    tail -n 10 "$WORK_DIR/download.log" >&2
    DETAIL=$(tail -n 1 "$WORK_DIR/download.log")
    echo "MP3を取得できません: $REMOTE ($DETAIL)" >&2
    exit 1
fi
CHILD_PID=""
if [ ! -s "$WORK_DIR/audio.mp3" ]; then
    echo "MP3が存在しないか空です: $REMOTE" >&2
    exit 1
fi

mpv --no-video --audio-display=no --no-terminal --ytdl=no \
    --audio-device="${MPV_AUDIO_DEVICE:-auto}" --loop-file=inf \
    --log-file="$WORK_DIR/mpv.log" -- "$WORK_DIR/audio.mp3" \
    > /dev/null 2>&1 &
CHILD_PID=$!
wait "$CHILD_PID"
RESULT=$?
CHILD_PID=""
if [ "$RESULT" -ne 0 ]; then
    tail -n 10 "$WORK_DIR/mpv.log" >&2
    echo "MP3再生に失敗しました: $REMOTE" >&2
fi
exit "$RESULT"
