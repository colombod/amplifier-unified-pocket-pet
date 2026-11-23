#!/bin/sh
# The connected network's name: the pure wording rules, and the production
# station refresh extracted from main/network.c over a synthetic station.
set -eu
cd "$(dirname "$0")/.."
out=${1:?output required}
mkdir -p "$out"
python3 - "$out" <<'PY'
import hashlib
import re
import sys
from pathlib import Path
source=Path("main/network.c")
lines=source.read_text(encoding="utf-8").splitlines(keepends=True)
start=next(i for i,line in enumerate(lines) if re.match(r"^static void refresh_station\(",line))
end=next(i for i in range(start+1,len(lines)) if lines[i]=="}\n")
body="".join(lines[start:end+1])
print("production-station-refresh",hashlib.sha256(body.encode()).hexdigest())
(Path(sys.argv[1])/"station-refresh.inc").write_text(f'#line {start+1} "{source}"\n'+body,encoding="utf-8")
PY
cc -Wall -Wextra -Werror -g -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I "$out" -I main tests/wifi_name_test.c -o "$out/wifi_name"
"$out/wifi_name"
