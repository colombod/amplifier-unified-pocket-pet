#!/bin/sh
# Run from any directory. Lean 4.19.0; no Lake packages or network required.
set -eu
cd "$(dirname "$0")/../.."
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
lean -DwarningAsError=true tests/proofs/Wake.lean
lean -DwarningAsError=true --run tests/proofs/Lan.lean > "$work/lean-table"
PYTHONDONTWRITEBYTECODE=1 python3 - "$work/firmware-policy.inc" <<'PYCODE'
from pathlib import Path
import sys
sys.path.insert(0, "tests")
from input_diag_test import function

power = Path("main/device_power.c").read_text()
bridge = Path("main/bridge_voice.c").read_text()
voice = Path("main/live_voice.c").read_text()
definitions = {"BV_MIC", "BV_CHUNK", "BV_STALL_MS", "BV_APPEND", "BV_PREBUFFER",
               "BV_CATCHUP", "BV_AUDIO_CORE", "BV_AUDIO_PRIORITY",
               "VOICE_OWNER_CORE", "VOICE_OWNER_PRIORITY"}
lines = [line for line in (bridge + "\n" + voice).splitlines()
         if line.startswith("#define ") and line.split()[1] in definitions]
assert len(lines) == len(definitions)
lines.append(next(line for line in power.splitlines() if line.startswith("enum { KEY_POLL_MS=")))
lines.append(function(power, "device_power_read_source"))
ui = Path("main/main.c").read_text()
lines.append(function(ui, "update_label"))
refresh = function(ui, "refresh")
frame = refresh[refresh.index("    unsigned frame;"):refresh.index("    diag_visibility")]
lines.append("static void frame_tick(link_fixture_t s,live_status_t voice) {\n" + frame + "}")
Path(sys.argv[1]).write_text("\n".join(lines))
PYCODE
cc -I "$work" -std=c11 -Wall -Wextra -Werror -I main tests/proofs/source_table.c -o "$work/table"
"$work/table" > "$work/c-table"
cmp "$work/lean-table" "$work/c-table"
echo 'Lean checked; all 65,536 PMIC register pairs agree with the C decoder.'
