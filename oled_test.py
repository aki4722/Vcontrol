#!/usr/bin/env python3
"""I2C 接続した 128x64 SSD1309 OLED にテスト表示する。"""

import argparse
import time

from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import ssd1309


def i2c_address(value: str) -> int:
    address = int(value, 0)
    if not 0x03 <= address <= 0x77:
        raise argparse.ArgumentTypeError("I2C address must be between 0x03 and 0x77")
    return address


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=1, help="I2C bus number (default: 1)")
    parser.add_argument(
        "--address",
        type=i2c_address,
        default=0x3C,
        help="OLED I2C address (default: 0x3C)",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=10,
        help="display duration; 0 keeps it displayed (default: 10)",
    )
    args = parser.parse_args()

    serial = i2c(port=args.port, address=args.address)
    device = ssd1309(serial, width=128, height=64)
    with canvas(device) as draw:
        draw.text((8, 12), "SSD1309 OLED", fill="white")
        draw.text((8, 32), f"I2C: 0x{args.address:02X}", fill="white")
        draw.text((8, 48), "Hello, Raspberry Pi!", fill="white")

    if args.seconds > 0:
        time.sleep(args.seconds)
        device.clear()


if __name__ == "__main__":
    main()
