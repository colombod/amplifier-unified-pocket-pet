#!/bin/sh
# Exact parser, WG-management and snapshot bodies; peripheral seams only.
set -eu
cd "$(dirname "$0")/.."
export PET_REPO="$(cd ../.. && pwd)"
out=${1:?output directory required}
mkdir -p "$out"
python3 - "$out" <<'PY'
from pathlib import Path
import hashlib,re,sys,os
out=Path(sys.argv[1])
base=Path(os.environ["PET_REPO"])/"components/tailtiny"
internal=(base/"include/tailtiny_internal.h").read_text(encoding="utf-8")
types=[]
for name in ("ml_peer_update_t","ml_peer_t"):
    end=internal.index("} "+name+";")+len("} "+name+";")
    start=internal.rfind("typedef struct {",0,end)
    types.append(internal[start:end])
(out/"peer-types.inc").write_text("\n".join(types),encoding="utf-8")
parts=[]
for path,names in [
    (base/"include/tailtiny_internal.h",["ml_peer_diag_inc"]),
    (base/"src/ml_coord.c",["hex_to_bytes","parse_peer_map","qualification_dns","qualification_observe_server","parse_peers_from_map_response"]),
    (base/"src/ml_wg_mgr.c",["check_peer_on_tcpip","find_peer_by_key","add_peer","remove_peer","process_peer_updates"]),
    (base/"src/tailtiny.c",["tailtiny_runtime_snapshot"]),
]:
    lines=path.read_text(encoding="utf-8").splitlines(keepends=True)
    for name in names:
        start=next(i for i,line in enumerate(lines) if re.match(rf"^(?:static )?(?:inline )?[\w *]+\b{name}\(",line) and line.rstrip().endswith("{"))
        end=next(i for i in range(start+1,len(lines)) if lines[i]=="}\n")
        body="".join(lines[start:end+1])
        parts.append(f'#line {start+1} "{path}"\n'+body)
        print("production-body",name,hashlib.sha256(body.encode()).hexdigest())
(out/"peer-production.inc").write_text("\n".join(parts),encoding="utf-8")
PY
cc -Wall -Wextra -Werror -g -pthread -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I "$out" -I tests/tls_host -I "$PET_REPO/components/tailtiny/include" \
  -I "$IDF_PATH/components/json/cJSON" tests/peer_diag_test.c \
  "$IDF_PATH/components/json/cJSON/cJSON.c" -o "$out/peer_diag"
"$out/peer_diag"