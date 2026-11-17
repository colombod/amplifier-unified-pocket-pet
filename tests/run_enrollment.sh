#!/bin/sh
# Compile verbatim production constructor/registration bodies, not a model of
# auth. Only RTOS, hardware, NVS and the Noise transport are host substitutes.
set -eu
cd "$(dirname "$0")/.."
out=${1:?output directory required}
mkdir -p "$out"
python3 - "$out/enrollment-production.inc" <<'PY'
import hashlib
import pathlib
import sys

base = pathlib.Path("components/tailtiny")
parts = []
for name, functions in [
    ("tailtiny.c", ["load_or_generate_keys", "cjson_psram_malloc",
                   "tailtiny_init", "tailtiny_init_checked",
                   "tailtiny_stop", "tailtiny_destroy", "tailtiny_runtime_snapshot"]),
    ("ml_coord.c", ["bytes_to_hex", "do_register"]),
]:
    path = base / "src" / name
    text = path.read_text()
    for function in functions:
        lines = text.splitlines(keepends=True)
        start = next(i for i, line in enumerate(lines)
                     if function + "(" in line and line.rstrip().endswith("{")
                     and not line.startswith((" ", "\t")))
        end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}\n")
        body = "".join(lines[start:end + 1])
        parts.append(f'#line {start + 1} "{path}"\n' + body)
        print("production-body", function, hashlib.sha256(body.encode()).hexdigest())
pathlib.Path(sys.argv[1]).write_text("\n".join(parts))
PY
cc -Wall -Wextra -Werror -g -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I "$out" -I tests/enrollment_host -I tests/tls_host \
  -I components/tailtiny/include -I components/tailtiny/src \
  -I "$IDF_PATH/components/json/cJSON" tests/tailnet_enrollment_test.c \
  "$IDF_PATH/components/json/cJSON/cJSON.c" components/tailtiny/src/x25519.c \
  components/tailtiny/src/ml_h2.c -o "$out/tailnet_enrollment"
status=0
for scenario in copy_retry power_loss mark_set mark_commit mark_reopen mark_readback marked_no_wire \
  ambiguous registered corrupt partial read open write commit reopen readback legacy \
  phase_unknown keypair authorized alloc_fault_auth alloc_fault_reg; do
  "$out/tailnet_enrollment" "$scenario" || status=1
done
exit "$status"