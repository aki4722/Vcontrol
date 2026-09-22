#!/bin/bash

DEVICE="9D:C6:55:EC:74:D5"
SINK="bluez_output.9D:C6:55:EC:74:D5"

echo "Waiting for Bluetooth USB adapter..."

# Bluetoothコントローラが使用可能になるまで待つ
for i in {1..30}; do
    if bluetoothctl list | grep -q "Controller"; then
        break
    fi
    sleep 1
done

# JQ-BTへ接続
for i in {1..20}; do
    if bluetoothctl info "$DEVICE" | grep -q "Connected: yes"; then
        echo "JQ-BT already connected."
        break
    fi

    echo "Connecting JQ-BT..."
    bluetoothctl connect "$DEVICE"

    sleep 2
done

# PipeWireにBluetooth sinkが現れるまで待つ
for i in {1..20}; do
    if pactl list sinks short | grep -q "$SINK"; then
        echo "JQ-BT audio sink found."
        pactl set-default-sink "$SINK"
        exit 0
    fi

    sleep 1
done

echo "JQ-BT audio sink not found."
exit 1
