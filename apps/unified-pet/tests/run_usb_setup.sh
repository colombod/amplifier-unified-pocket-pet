#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
out=${1:?output required}
mkdir -p "$out"
python3 - "$out/usb-production.inc" <<'PY'
from pathlib import Path
import sys
parts = []
for source, signature in [
    ("main/setup_tailscale.h", "static inline cJSON *setup_parse_json("),
    ("main/network.c", "void network_usb_setup("),
]:
    text = Path(source).read_text()
    start = text.index(signature)
    parts.append(text[start:text.index("\n}", start) + 2])
Path(sys.argv[1]).write_text("\n".join(parts))
PY
cc -Wall -Wextra -Werror -fsanitize=address,undefined -I "$out" \
  -I "$IDF_PATH/components/json/cJSON" tests/usb_setup_test.c \
  "$IDF_PATH/components/json/cJSON/cJSON.c" -o "$out/usb_setup"
"$out/usb_setup"
