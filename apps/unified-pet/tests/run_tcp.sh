#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
export PET_REPO="$(cd ../.. && pwd)"
out=${1:?output required}
mkdir -p "$out"
python3 - "$out" <<'PY'
from pathlib import Path
import hashlib,sys,os
path=Path(os.environ["PET_REPO"])/"components/tailtiny/src/ml_wg_mgr.c"
text=path.read_text(encoding="utf-8")
start=text.index("        uint32_t requested=__atomic_load_n(&ml->app_connect_ip,")
end=text.index("        /* Track DERP connection",start)
body=text[start:end]
assert body.count("ml_wg_mgr_trigger_handshake(")==1
out=Path(sys.argv[1])
(out/"tcp-manager.inc").write_text(
    "static void manager_poll(tailtiny_t *ml,bool *requested_once,bool *ready_once) {\n"
    "bool app_handshake_requested=*requested_once,app_ready_observed=*ready_once;\n"+
    f'#line {text[:start].count(chr(10))+1} "{path}"\n'+body+
    "*requested_once=app_handshake_requested;*ready_once=app_ready_observed;\n}\n",encoding="utf-8")
print("production-block",path,hashlib.sha256(body.encode()).hexdigest())
print("production-source ml_tcp.c",hashlib.sha256((path.parent/"ml_tcp.c").read_bytes()).hexdigest())
PY
cc -Wall -Wextra -Werror -g -fsanitize=address,undefined -fno-omit-frame-pointer \
  -D TCP_DIAG_TEST -D socket=test_socket -D bind=test_bind -D connect=test_connect \
  -D fcntl=test_fcntl -D close=test_close -D select=test_select -D getsockopt=test_getsockopt \
  -D calloc=test_calloc -I tests/tls_host -I "$PET_REPO/components/tailtiny/include" \
  -c "$PET_REPO/components/tailtiny/src/ml_tcp.c" -o "$out/ml_tcp.o"
cc -Wall -Wextra -Werror -g -fsanitize=address,undefined -fno-omit-frame-pointer \
  -D TCP_DIAG_TEST -I "$out" -I tests/tls_host -I "$PET_REPO/components/tailtiny/include" \
  tests/tailnet_tcp_test.c "$out/ml_tcp.o" -o "$out/tailnet_tcp"
"$out/tailnet_tcp"