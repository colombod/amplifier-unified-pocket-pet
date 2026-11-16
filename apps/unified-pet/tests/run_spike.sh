#!/bin/sh
# Execute the verbatim spike run/sample/cleanup control flow with host seams.
set -eu
cd "$(dirname "$0")/.."
PET_REPO="$(cd ../.. && pwd)"
out=${1:?output directory required}
mkdir -p "$out"
python3 - "$out/spike-production.inc" <<'PY'
import hashlib
from pathlib import Path
import re
import sys

path = Path("main/tailnet_spike.c")
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
parts = []
for name in ("now", "tailnet_spike_audio_acquire", "tailnet_spike_audio_release",
             "stage", "deadline", "sample", "pause_wait", "entered_http", "run"):
    start = next(i for i, line in enumerate(lines)
                 if re.match(rf"^(?:static )?[\w *]+\b{name}\(", line))
    end = start if lines[start].rstrip().endswith("}") else next(
        i for i in range(start + 1, len(lines)) if lines[i] == "}\n")
    body = "".join(lines[start:end + 1])
    parts.append(f'#line {start + 1} "{path}"\n' + body)
    print("production-body", name, hashlib.sha256(body.encode()).hexdigest())
Path(sys.argv[1]).write_text("\n".join(parts), encoding="utf-8")
PY
cc -Wall -Wextra -Werror -g -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I "$out" -I main -I tests/tls_host -I "$PET_REPO/components/pet_tls/include" -I "$PET_REPO/components/tailtiny/include" \
  tests/tailnet_spike_test.c -o "$out/tailnet_spike"
"$out/tailnet_spike"