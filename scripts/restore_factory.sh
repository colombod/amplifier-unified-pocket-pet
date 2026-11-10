#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
port=${1:?Usage: sh scripts/restore_factory.sh /dev/cu.usbmodemPORT}
# Restores this device's complete backup, including the original partition table.
.host-tools/bin/python -m esptool --chip esp32s3 --port "$port" --baud 921600 write_flash --flash_size 16MB 0x0 private-backups/factory-20261110/factory-flash.bin
