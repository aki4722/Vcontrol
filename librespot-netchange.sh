#!/bin/sh
# networkd-dispatcher用フック。有線(eth0)の抜き差しでlibrespotを再起動する。
# librespotはSpotifyへの常時接続を張りっぱなしにし、経路(送信元IP)が消えても
# 自力で再接続しないため、Spotify ConnectデバイスがSpotifyから見えなくなる。
# 配置先: /etc/networkd-dispatcher/no-carrier.d/ と routable.d/（READMEを参照）
[ "$IFACE" = eth0 ] && systemctl restart librespot.service
exit 0
