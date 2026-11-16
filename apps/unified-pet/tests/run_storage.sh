#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
repo=$(CDPATH= cd ../.. && pwd)
out=${1:?output required}
mkdir -p "$out"
python3 - "$out" <<'PY'
import hashlib
import re
import sys
from pathlib import Path
source=Path("main/network.c")
lines=source.read_text(encoding="utf-8").splitlines(keepends=True)
parts=[]
for name in ("mode_ready","refresh_bridge_mode","network_copy_api_key","network_copy_tailscale_auth_key","network_copy_unified_target",
             "network_init","join_network"):
    start=next(i for i,line in enumerate(lines) if re.match(rf"^(?:esp_err_t|void|static void|static bool) {name}\(",line))
    end=next(i for i in range(start+1,len(lines)) if lines[i]=="}\n")
    body="".join(lines[start:end+1])
    parts.append(f'#line {start+1} "{source}"\n'+body)
    print("production-storage-accessor",name,hashlib.sha256(body.encode()).hexdigest())
(Path(sys.argv[1])/"storage-production.inc").write_text("\n".join(parts),encoding="utf-8")
PY
cc -Wall -Wextra -Werror -Wno-misleading-indentation -g -fsanitize=address,undefined \
  -I "$out" -I main -I "$repo/tests/touch_host" -I tests/tls_host \
  -I "$IDF_PATH/components/json/cJSON" tests/storage_compat_test.c \
  "$IDF_PATH/components/json/cJSON/cJSON.c" -o "$out/storage_compat"
"$out/storage_compat"