#!/bin/sh
# Pinned SDK's real pbuf.c + mem.c; no networking, device, keys or crypto claim.
set -eu
cd "$(dirname "$0")/.."
export PET_REPO="$(cd ../.. && pwd)"
out=${1:?output required}
mkdir -p "$out"
python3 - "$out" <<'PY'
from pathlib import Path
import hashlib,os,sys
out=Path(sys.argv[1])
path=Path(os.environ.get("WG_RX_SOURCE",os.environ["PET_REPO"]+"/components/tailtiny/components/wireguard_lwip/src/wireguardif.c"))
lines=path.read_text(encoding="utf-8").splitlines(keepends=True)
start=next(i for i,l in enumerate(lines) if l.startswith("static void wireguardif_process_data_message_locked("))
end=next(i for i in range(start+1,len(lines)) if lines[i]=="}\n")
body="".join(lines[start:end+1])
(out/"wg-rx-production.inc").write_text(f'#line {start+1} "{path}"\n'+body,encoding="utf-8")
print("production-RX",hashlib.sha256(body.encode()).hexdigest())
path=Path(os.environ["PET_REPO"])/"components/tailtiny/src/ml_wg_mgr.c"
lines=path.read_text(encoding="utf-8").splitlines(keepends=True)
start=next(i for i,l in enumerate(lines) if l.startswith("static void check_packet_on_tcpip("))
end=next(i for i in range(start+1,len(lines)) if lines[i]=="}\n")
body="".join(lines[start:end+1])
(out/"wg-policy-production.inc").write_text(lines[start-1]+f'#line {start+1} "{path}"\n'+body,encoding="utf-8")
print("production-policy-callback",hashlib.sha256(body.encode()).hexdigest())
sdk=Path(os.environ["IDF_PATH"])/"components/lwip/lwip/src"
for name in ("core/pbuf.c","core/mem.c","core/memp.c","core/def.c"):
    print("SDK-source",name,hashlib.sha256((sdk/name).read_bytes()).hexdigest())
PY
sdk="$IDF_PATH/components/lwip/lwip/src"
cc -Wall -Wextra -Werror -Wno-unused-parameter -Wno-unused-but-set-variable \
  -g -fsanitize=address,undefined -fno-omit-frame-pointer -ffunction-sections -fdata-sections \
  -I "$out" -I tests/wg_rx_host -I "$sdk/include" -I "$PET_REPO/components/tailtiny/include" \
  tests/wg_rx_test.c "$sdk/core/pbuf.c" "$sdk/core/mem.c" "$sdk/core/memp.c" "$sdk/core/def.c" \
  -Wl,--gc-sections -o "$out/wg_rx"
"$out/wg_rx"