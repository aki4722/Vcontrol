# 録音・ラジオ操作

音声認識ライブラリは使用しません。録音時は ALSA の `arecord` を起動し、WAV ファイルを `/home/akimoto/recordings` に保存します。録音終了時と1時間ごとの分割時に `rclone` で `gdrive:音声` へアップロードします。ラジオは `play_radiko.sh` を使用します。

## 配線（BCM番号）

- マトリクス行（上から）: GPIO5、GPIO6、GPIO13
- マトリクス列（左から）: GPIO16、GPIO20、GPIO21
- GPIO27 → 電流制限抵抗 → LED → GND: 録音中に点灯

マトリクスの各スイッチは対応する行と列を接続します。キー配置は次の通りです。放送局は `stations.conf` の先頭から6局に対応します。

| 位置 | 左 | 中 | 右 |
| --- | --- | --- | --- |
| 上段 | 録音開始／停止 | 録音停止 | 全停止 |
| 中段 | 1局目 | 2局目 | 3局目 |
| 下段 | 4局目 | 5局目 | 6局目 |

## セットアップ

```sh
cd /home/akimoto/voice-control
python3 -m venv --system-site-packages venv
venv/bin/pip install -r requirements.txt
# 録音には alsa-utils の arecord、radiko 再生には curl と mpv、アップロードには rclone が必要
sudo cp voice-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now voice-control.service
```

サービス定義はGPIOとI2Cへのアクセスに必要な `gpio`、`i2c`、`dialout`
グループをプロセスへ付与します。

`venv` が既にある場合は作り直す必要はありません。`systemctl status voice-control.service` で状態を確認できます。Web UI は `http://<Raspberry PiのIPアドレス>:5000/` です。同じネットワークの利用者が操作できるため、公開ネットワークへポート5000を開けないでください。

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

常駐サービスではOLEDに現在時刻と `READY`、`REC`、`RADIO` の状態を
自動表示します。録音中は経過時間、ラジオ再生中は局IDも
表示します。I2Cバスとアドレスは `.env` の `OLED_PORT`、`OLED_ADDRESS` で変更できます。
OLEDが未接続または故障していても、録音・ラジオ機能は継続します。

Bluetoothスピーカーなど出力先を固定する場合は `mpv --audio-device=help` で名前を
確認し、`.env` に `MPV_AUDIO_DEVICE=pipewire/bluez_output...` を設定してください。

設定可能な環境変数は `WEB_HOST`、`WEB_PORT`、`RECORDINGS_DIR`、`GDRIVE_DIR`、`OLED_PORT`、`OLED_ADDRESS` です。放送局は `stations.conf` に `表示名|局ID` で登録します。radiko プレミアムを使う場合の資格情報は既存の `.env` に設定します。
