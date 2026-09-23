#!/usr/bin/env python3
"""Spotify Web API（再生制御用）のrefresh_tokenを一度だけ取得し、.envへ書き込む。

事前に developer.spotify.com/dashboard でアプリを作成し、
  - Client ID / Client Secret を .env の SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET に設定
  - Redirect URI に REDIRECT_URI（下記）を登録
してから実行する。listen.py本体からは呼ばれない、使い捨てのセットアップ専用スクリプト。
"""

import os
import sys
import urllib.parse
import urllib.request
import urllib.error
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = (
    "user-read-playback-state user-modify-playback-state "
    "user-read-currently-playing playlist-read-private playlist-read-collaborative"
)
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"


def read_env():
    values = {}
    if not ENV_FILE.exists():
        return values
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        key = key.strip()
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def set_env_value(key, value):
    """指定キーの行を置換（無ければ追記）し、.tmp -> Path.replace()で原子的に書き込む。
    パーミッションは0600に固定する（内容は表示・出力しない）。"""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    quoted = "'" + value.replace("'", "'\\''") + "'"
    new_line = f"{key}={quoted}"
    replaced = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            lines[index] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)
    temporary = ENV_FILE.with_suffix(".env.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(ENV_FILE)


def main():
    env = read_env()
    client_id = env.get("SPOTIFY_CLIENT_ID") or os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = env.get("SPOTIFY_CLIENT_SECRET") or os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("先に .env へ SPOTIFY_CLIENT_ID と SPOTIFY_CLIENT_SECRET を設定してから再実行してください。")
        print("（developer.spotify.com/dashboard でアプリを作成し、Redirect URIに以下を登録）")
        print(f"  {REDIRECT_URI}")
        sys.exit(1)

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "show_dialog": "true",
    }
    authorize_url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
    print("以下のURLを任意のブラウザで開き、ログイン・許可してください。")
    print("リダイレクト先（127.0.0.1:8888）には接続できませんが、ブラウザのアドレスバーに")
    print("表示される最終的なURL全体をコピーして、この画面に貼り付けてください。\n")
    print(authorize_url)
    print()

    code = None
    while code is None:
        pasted = input("リダイレクト後のURLを貼り付け: ").strip()
        query = urllib.parse.urlsplit(pasted).query
        params = urllib.parse.parse_qs(query)
        if "error" in params:
            print(f"認可が拒否されました: {params['error'][0]}。もう一度試してください。")
            continue
        if "code" not in params:
            print("URLにcodeが見つかりません。もう一度貼り付けてください。")
            continue
        code = params["code"][0]

    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode("ascii")
    request = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print(f"トークン交換に失敗しました（HTTP {exc.code}）。Client Secretやredirect_uriを確認してください。")
        print(detail)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"トークン交換に失敗しました（通信エラー）: {exc.reason}")
        sys.exit(1)

    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        print("レスポンスにrefresh_tokenが含まれていません。scopeやアプリ設定を確認してください。")
        sys.exit(1)

    set_env_value("SPOTIFY_REFRESH_TOKEN", refresh_token)
    print("SPOTIFY_REFRESH_TOKEN を .env に保存しました（値は表示しません）。")
    print("voice-control.service を再起動すると反映されます。")


if __name__ == "__main__":
    main()
