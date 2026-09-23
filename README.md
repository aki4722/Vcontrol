# 録音・ラジオ操作

音声認識ライブラリは使用しません。録音時は ALSA の `arecord` を起動し、WAV ファイルを USB SSD 上の `/mnt/ssd/voice` に保存します（`RECORDINGS_DIR` で変更可能）。SSD は起動時に `/mnt/ssd` へ自動マウントされる前提で、録音開始前に `os.path.ismount("/mnt/ssd")` に加えて実際に `/mnt/ssd/voice` へ小さなファイルを書き込めるかも確認し（マウント表には残っているがUSB切断等でデバイス自体が消えた「幽霊マウント」を、空き容量取得だけでは検知できないため）、いずれかに失敗した場合は録音を開始せずエラーを記録します（マウントポイントは `SSD_MOUNT_POINT` で変更可能）。この書き込み確認はデバイス切断直後に長時間ブロックすることがあるため、タイムアウト付き（既定2秒、`SSD_PROBE_TIMEOUT_SECONDS`定数）で行います。録音終了時と1時間ごとの分割時に `rclone` で `gdrive:音声` へアップロードします。ラジオは `play_radiko.sh` を使用します。

## キーボード操作

Raspberry Piに接続した、デバイス名が `aki4722 akisan08` のキーボードを使用します。
Linux input eventをevdevで直接読み取り、名前が完全一致する `/dev/input/eventX` を
自動検出します。event番号は固定せず、抜き差し後も1秒ごとに再検出します。
他の名前のキーボードからの操作は受け付けません。Enterは不要で、
`KEY_LEFTCTRL` または `KEY_RIGHTCTRL` の押下中に、`KEY_1`～`KEY_8` の
`value=1` を受けたときだけ操作します。`value=2` のリピートは無視し、
`value=0` で押下状態を解除します。
SSH端末から送信するキーには対応しません。キー入力は他のアプリにも届きます。

| キー | 操作 |
| --- | --- |
| Ctrl+1 | 停止中は最後の局を再生／再生中は次の局 |
| Ctrl+5 | 停止中は最後の局を再生／再生中は前の局 |
| Ctrl+2 | Spotify: 停止中は前回の再生リストを再生／再生中は次の再生リスト |
| Ctrl+3 | 未使用 |
| Ctrl+4 | 録音開始／停止 |
| Ctrl+6 | Spotify: 再生中は前の再生リストへ／停止中は何もしない |
| Ctrl+7 | 未使用 |
| Ctrl+8 | ラジオ・録音・Spotify・アップロードを強制停止し待機へ |

選局は局番号順に循環します。他局への切り替えは停止完了後に再生します。
録音開始・停止はCtrl+4またはWebから操作します。
録音開始時も先にラジオを停止します。録音中のラジオ開始は受け付けません。
Webのラジオボタンは従来どおり、別の局なら切り替え、同じ局なら停止します。
強制停止では待機中のアップロードも取り消します。録音済みWAVはローカルに残り、
取り消したアップロードは自動再開しません。次の録音から通常のアップロードを再開します。

物理ボタンのGPIO入力・マトリクス走査は使用しません。
録音表示用のGPIO27 → 電流制限抵抗 → LED → GND は引き続き使用します。
GPIO27は音声録音専用で、後述のIRリモコン機能（GPIO22・GPIO10）とは用途が異なります。

## 放送局と選局Noの保存

`stations.conf` は局番号ごとに管理します。既存の10局は従来の順序のまま
1～10に移行済みです。radikoは既存の認証・再生処理を使うため `id` を指定します。
直接配信の音声URLを使う場合は、`id` の代わりに `url` を指定します。
`url` はWebページではなく、mpvで再生できるHTTP/HTTPSの音声配信URLです。

```ini
[1]
name = K-MIX
id = K-MIX

[2]
name = NHK FM
id = JOAK-FM

[3]
name = J-WAVE
id = FMJ

[4]
name = ニッポン放送
id = LFR
```

Ctrl+1は次の局、Ctrl+5は前の局です。端では先頭／末尾へ戻ります。
停止中の最初の押下は選局Noを変更せず、最後の局を再生します。
局一覧はキー押下時に `stations.conf` から読み直します。
`keybindings.conf` は使用せず、Ctrl+2 / Ctrl+3のラジオ割り当てはありません。

選局Noは再生開始時に `~/voice-control/radio_station.txt` へ番号だけを保存します
（正確な保存先は `listen.py` と同じディレクトリです）。起動時に読み込み、
ファイルがない場合・不正な内容・未登録の番号の場合はNo.1を使用します。
停止やCtrl+8の強制停止では選局Noと保存ファイルを変更しません。
その後のCtrl+1 / Ctrl+5は最後の局を再生します。

存在しない番号や不正な設定では再生を開始せず、現在の再生は維持します。
OLEDに `CONFIG ERROR` と詳細、Webにエラーを表示します。
正しい設定で再選択するかCtrl+8を押すとエラーを解除します。
OLEDには `RADIO PLAYING` と局名を表示します。長い局名は画面幅で切り詰めます。

## MP3の無限ループ再生

Web画面の「MP3 ループ再生」から `04-アクセル.mp3` を開始・停止します。
既存のキー割り当ては変わりません。実ファイルはrcloneで
`gdrive:音声/04-アクセル.mp3` に存在することを確認済みです。
保存先は録音アップロードと同じ `GDRIVE_DIR` を使用します。

開始時に `play_mp3.sh` が一時ディレクトリへ取得し、mpvの
`--loop-file=inf` で無限ループ再生します。出力先はラジオと同じ
`MPV_AUDIO_DEVICE` です。取得中も停止操作を受け付けます。
ラジオとMP3は共通の再生プロセス枠を使用し、切り替える際に前の再生を停止します。
録音開始・Ctrl+8の強制停止・終了処理でもMP3を停止します。
MP3再生中のCtrl+1 / Ctrl+5は保存済みのラジオ局へ戻ります。
選局Noの保存内容はMP3再生では変更しません。

ファイルがない場合や取得・再生に失敗した場合はログとWebの状態欄に
エラーを表示します。ダウンロード完了前の開始応答は取得開始を意味します。
停止後は一時ファイルを削除し、次回開始時に再取得します。
変更の反映には `sudo systemctl restart voice-control.service` が必要です。

## Spotify再生

Spotifyのプレイリスト・アーティストを「再生リスト」として登録し、Ctrl+2・Ctrl+6や
Web UIからシャッフル再生できます。実際の音声出力は本サービスとは別の常駐サービス
`librespot.service`（[librespot](https://github.com/librespot-org/librespot)、Spotify
Connect対応デバイス）が行い、`listen.py`はSpotify Web API経由でその再生を指示する
だけです（librespotプロセス自体の起動・停止は行いません）。

### 初回セットアップ

1. [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) でアプリを
   作成し、Client ID/Secretを取得。Redirect URIに `http://127.0.0.1:8888/callback` を登録。
2. `.env` に `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` を設定し、
   `venv/bin/python spotify_auth_setup.py` を実行（対話式。表示されたURLをブラウザで開いて
   ログイン・許可し、リダイレクト後のURLを貼り付けると `SPOTIFY_REFRESH_TOKEN` が自動的に
   `.env` へ保存されます）。このスクリプトは常駐プロセスとは無関係な使い捨てのセットアップ
   専用ツールで、`.env`の内容を表示・出力することはありません。
3. Rust(`cargo`)と `pkg-config`・`libpulse-dev` を導入し、librespotをソースから
   `cargo build --release --no-default-features --features "native-tls,pulseaudio-backend,with-libmdns"`
   でビルドします。
4. `librespot --name Vcon --backend pulseaudio --cache ~/.cache/librespot --system-cache ~/.cache/librespot --enable-device-auth`
   を一度だけ手動実行し、表示される `https://spotify.com/pair?code=XXXXXX` を任意のブラウザで
   開いてペアリングします（認証情報は`--cache`ディレクトリにキャッシュされ、以降は不要）。
5. ビルドしたバイナリを `sudo install -m 755 librespot /usr/local/bin/librespot` で配置し、
   `sudo cp librespot.service /etc/systemd/system/ && sudo systemctl daemon-reload &&
   sudo systemctl enable --now librespot.service` で常駐化します。

### 再生リストの登録

`spotify_sources.json`（`listen.py`と同じディレクトリ、JSON配列）で管理します。番号は
配列内の並び順（1始まり）です。プレイリストはSpotifyの共有リンク等からIDを調べて
手動編集で追加します（Spotify側でのプレイリストの作成・編集はそちらのアプリで行い、
本システムは再生専用です）。

```json
[
  {"type": "playlist", "name": "ドライブ", "spotify_id": "37i9dQZF1..."},
  {"type": "artist", "name": "サザンオールスターズ", "spotify_id": "3drOkl3f..."}
]
```

アーティストはWeb UIの「アーティスト検索」から選ぶと自動的にこのファイルへ追記され
（重複するspotify_idは追加しません）、選んだアーティストの曲がその場でシャッフル再生
されます。ラジオの局番号選択キー同様、Ctrl+2・Ctrl+6を押すたびにこのファイルを読み直す
ため、手動編集した内容もサービス再起動なしに反映されます（Web UIの一覧表示は
`/api/status`のポーリングで更新されます）。

再生は常にシャッフルです（プレイリスト・アーティストいずれも通常の順番再生には
対応しません）。最後に選択した再生リストは `spotify_selection.json` に保存され、
`voice-control.service`（Raspberry Pi）再起動後もCtrl+2で復元されます。

### 排他制御・エラー処理

ラジオとSpotifyは同時に再生しません。どちらかを開始すると、もう一方は自動的に
停止します（録音開始時も同様にSpotifyを停止し、録音中のSpotify開始は受け付けません）。
Ctrl+8の強制停止では録音・ラジオ・Spotifyのすべてを止めます。

Spotify Web APIの認証切れ・通信エラー・再生デバイス（librespot）未検出・プレイリスト
取得失敗などが発生してもvoice-control全体は継続動作し、ログとWeb UI・OLEDの状態欄に
エラーを表示するだけです。`.env`にSpotifyの認証情報が設定されていない場合は
Ctrl+2・Ctrl+6は何もせず、Web UIには「Spotifyが設定されていません」と表示されます。

OLEDには再生中に `SPOTIFY 現在番号/総数` と、取得できていれば曲名・アーティスト名
（未取得の間は再生リスト名）を表示します。長い場合は既存の局名表示と同じ方式で
画面幅に収まるよう切り詰めます。

## IRリモコン学習・送信

追加基板でIR受信（GPIO4）・IR送信（GPIO18、トランジスタ駆動）を使用します。
これらのGPIOはカーネルの `dtoverlay=gpio-ir,gpio_pin=4` /
`dtoverlay=gpio-ir-tx,gpio_pin=18`（設定済み）が専有するため、アプリからは
直接操作せず `/dev/lirc1`（受信）・`/dev/lirc0`（送信）を `ir-ctl`
（`v4l-utils`）経由で使用します。IR受信中はGPIO22、IR送信中はGPIO10の
ステータスLEDを点灯します（構成: GPIO → 470Ω抵抗 → LED → GND）。

Web画面の「IRリモコン学習」から「IR録音開始」を押すと受信待機になり、
その間GPIO22 LEDが点灯します。家電リモコンのボタンを押すと信号を取得し、
自動的にIR番号を採番して保存し、待機を終了します（GPIO22 LEDは消灯）。
「IR録音停止」を押した場合は信号を保存せず待機をキャンセルします。
30秒間ボタンが押されなかった場合も自動的にキャンセルされます。

登録済みのIRコードはWeb画面に番号・名前・送信ボタンの一覧で表示され、
名前は一覧から変更できます。件数に上限はありません。データは
`ir_codes.json`（`listen.py`と同じディレクトリ）にIR番号・名前・信号データ・
登録日時を保存し、再起動しても保持されます。

送信は登録済みのIR番号を指定して行う共通処理 `send_ir(番号)` を介して行います。
Web画面の「送信」ボタンとHTTP API `POST /api/ir/send/<番号>` はいずれもこの
共通処理を呼び出すだけで、送信ロジック自体は一箇所にしかありません。
送信中はGPIO10 LEDが点灯し、完了後に消灯します。

IR受信中は送信できず、送信中は受信を開始できません（自分の送信を誤って
新しいリモコン信号として受信してしまうことを防ぐため）。録音・ラジオ・MP3の
状態とIRの状態は独立しており、互いに制限しません。

HTTP APIは以下を提供します。いずれもWeb画面と同じ共通処理を呼び出すだけの
薄いラッパーです。

| メソッド・パス | 内容 |
| --- | --- |
| `POST /api/ir/learn/start` | IR受信待機を開始 |
| `POST /api/ir/learn/stop` | IR受信待機をキャンセル |
| `POST /api/ir/send/<番号>` | 指定したIR番号を送信 |
| `POST /api/ir/rename/<番号>` | 指定したIR番号の名前を変更（JSON body `{"name": "..."}`） |

## セットアップ

```sh
cd /home/akimoto/voice-control
python3 -m venv --system-site-packages venv
venv/bin/pip install -r requirements.txt
# 録音には alsa-utils の arecord、radiko 再生には curl と mpv、アップロードには rclone、
# IRリモコンには v4l-utils の ir-ctl が必要
sudo cp voice-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now voice-control.service
```

サービス定義はGPIOとI2Cへのアクセスに必要な `gpio`、`i2c`、`dialout`、キーボード用の `input`、
`/dev/lirc0`・`/dev/lirc1`（IR送受信）用の `video` グループをプロセスへ付与します。

`venv` が既にある場合は作り直す必要はありません。`systemctl status voice-control.service` で状態を確認できます。Web UI は `http://<Raspberry PiのIPアドレス>:5000/` です。同じネットワークの利用者が操作できるため、公開ネットワークへポート5000を開けないでください。

更新時も依存パッケージのインストールとサービス定義のコピー、daemon-reloadを実行し、
`sudo systemctl restart voice-control.service` で反映してください。
手動起動では実行ユーザーに `input` グループと、IRリモコンを使う場合は `video`
グループの権限が必要です
（`sudo usermod -aG input,video akimoto` 後に再ログイン）。

手動起動も仮想環境の Python を指定してください。システムの `python3` には Flask が入っていません。

```sh
/home/akimoto/voice-control/venv/bin/python /home/akimoto/voice-control/listen.py
```

## SSD1309 OLED（I2C）

128x64 SSD1309 は GND=Pin 6、VCC=Pin 1 (3.3V)、SCL=Pin 5
(GPIO3)、SDA=Pin 3 (GPIO2) に接続します。I2C の確認とテスト表示は次の
コマンドで行います。

```sh
sudo modprobe i2c-dev
sudo apt-get update
sudo apt-get install -y i2c-tools
i2cdetect -y 1
venv/bin/pip install -r requirements.txt
venv/bin/python oled_test.py --address 0x3c
```

`--address` には `i2cdetect -y 1` で表示されたアドレスを指定してください。
テスト文字は10秒間表示されます。表示を維持する場合は `--seconds 0` を追加します。
このテストは独立したプログラムで、既存の録音・ラジオ常駐サービスには影響しません。

常駐サービスではOLEDに現在時刻、CPU温度と `STANDBY`、`RECORDING`、`RADIO PLAYING` の状態を
自動表示します。CPU温度、microSD（`/`）とSSD（`/mnt/ssd`）の空き容量はそれぞれ30秒ごとに更新され、
画面下部の2段に分けて表示されます。1段目は `CPU 45c`（左）と `SD 12G`（右、microSDの空き容量）、
2段目は録音先SSDの `SSD 83.4/111GB`（空き/総容量、幅に収まらない場合は `SSD 83GB` に短縮）です。
SSDが未マウントの場合は2段目が `SSD: NOT MOUNTED`、空き容量が1GB未満の場合は `SSD LOW SPACE` となり、
この状態では新規録音を開始できません（microSD側の空き容量は表示のみで録音開始の可否には影響しません）。
録音中は経過時間、ラジオ再生中は一覧の `name` を
表示し、`RECORDING` は0.5秒間隔で点滅します。
I2Cバスとアドレスは `.env` の `OLED_PORT`、`OLED_ADDRESS` で変更できます。
局名には同梱の日本語対応Rounded Mplus 1c、その他の文字にはDejaVu Sans Mono 12px、CPU温度と空き容量には9pxを使用し、1bitで描画します。
OLEDが未接続または故障していても、録音・ラジオ機能は継続します。

Bluetoothスピーカーなど出力先を固定する場合は `mpv --audio-device=help` で名前を
確認し、`.env` に `MPV_AUDIO_DEVICE=pipewire/bluez_output...` を設定してください。

設定可能な環境変数は `WEB_HOST`、`WEB_PORT`、`RECORDINGS_DIR`、`SSD_MOUNT_POINT`、`GDRIVE_DIR`、`OLED_PORT`、`OLED_ADDRESS`、`SPOTIFY_CLIENT_ID`、`SPOTIFY_CLIENT_SECRET`、`SPOTIFY_REFRESH_TOKEN`、`SPOTIFY_DEVICE_NAME` です。放送局は `stations.conf` に番号付きINI形式で登録します。radiko プレミアムを使う場合の資格情報は既存の `.env` に設定します。Spotifyのセットアップは「Spotify再生」の章を参照してください。

## 起動時のJQ-BTプロファイル設定

サービス起動時に一度だけ `bluetoothctl info 9D:C6:55:EC:74:D5` で接続状態を確認し、
接続済みの場合だけ次のコマンドを実行します。

```sh
pactl set-card-profile bluez_card.9D_C6_55_EC_74_D5 a2dp-sink
```

標準SBCを使用します。高ビットレートの `a2dp-sink-sbc_xq` は音質は良いものの、
このPiの外付けUSB Bluetoothドングル（TP-Link UB500、Realtek RTL8761B）との組み合わせで
Spotify再生中にランダムな瞬断（無音）が発生することを確認したため、安定性を優先して
使用していません。音質より安定性を優先しない場合は上記コマンドの引数を
`a2dp-sink-sbc_xq` に変更してください（`listen.py`の`configure_bluetooth_audio()`も
合わせて変更が必要です）。

未接続時はスキップします。コマンドがない場合やプロファイル切り替え失敗時も、
ログを残して録音・ラジオ・GPIO機能の起動を継続します。
各コマンドは3秒でタイムアウトします。起動後の接続監視や再試行は行いません。
