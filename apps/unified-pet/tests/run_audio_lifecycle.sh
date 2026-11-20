#!/bin/sh
# Verbatim module lifecycle bodies, real host threads and bounded RTOS seams.
set -eu
cd "$(dirname "$0")/.."
out=${1:?output directory required}
mkdir -p "$out"
python3 - "$out" <<'PY'
import hashlib
from pathlib import Path
import re
import os
import sys

out = Path(sys.argv[1])
def extract(path, names):
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    result = []
    for name in names:
        start = next((i for i, line in enumerate(lines)
                      if re.match(rf"^(?:static )?[\w *]+\b{name}\(", line)), None)
        if start is None:
            # Old code must compile to expose boot allocation RED, not fail
            # because the new public reaper contract does not exist yet.
            assert name.endswith("_reap_finished")
            result.append(f"void {name}(void) {{}}\n")
            continue
        depth = 0
        for end in range(start, len(lines)):
            depth += lines[end].count("{") - lines[end].count("}")
            if depth == 0 and "}" in lines[end]:
                break
        if name=="live_voice_start":
            # Both preprocessor branches open the same failure if; keep them
            # verbatim through the column-zero function end, not the next body.
            end=next(i for i in range(start+1,len(lines)) if lines[i].startswith("}"))
            assert lines[end+1].startswith("void live_voice_stop(")
        body = "".join(lines[start:end + 1])
        result.append(f'#line {start + 1} "{path}"\n' + body)
        print("production-body", path.name, name, hashlib.sha256(body.encode()).hexdigest())
    return text, "\n".join(result)

for module, names in [
    ("audio_test", ["update", "audio_test_snapshot", "audio_test_set_volume",
                    "worker", "audio_test_request", "audio_tests_init", "audio_test_reap_finished"]),
    ("live_voice", ["set_state", "live_voice_snapshot", "session_stop_locked",
                    "live_voice_bridge_audio_begin", "live_voice_bridge_audio_end", "worker", "live_voice_start",
                    "live_voice_stop", "live_voice_init", "live_voice_reap_finished"]),
]:
    path = Path("main") / f"{module}.c"
    text, bodies = extract(path, names)
    if module=="live_voice" and os.environ.get("AUDIO_LIFECYCLE_MUTANT")=="1":
        needle=" xSemaphoreTake(status_lock,portMAX_DELAY);xSemaphoreGive(status_lock);\n run_session();"
        assert bodies.count(needle)==1
        bodies=bodies.replace(needle," run_session();")
        print("TEST-ONLY MUTANT: removed voice initial publication mutex")
    globals_ = []
    for line in text.splitlines(keepends=True):
        if "(" not in line and re.match(r"^static (?:QueueHandle_t|SemaphoreHandle_t|TaskHandle_t|audio_test_status_t|live_status_t|nvs_handle_t|pet_store_t|atomic_bool|bool|const char \*)", line) and not line.startswith("static bool amp_inhibited"):
            globals_.append(line)
        # Bind actual private cap state/value, never a fixture duplicate or a
        # disabled deadline. Start/reap below remain verbatim production.
        if module=="live_voice" and re.match(
                r"^(?:#define VOICE_(?:SESSION_LIMIT_MS|OWNER_CORE|OWNER_PRIORITY)\b|static int64_t session_deadline_us;)",line):
            globals_.append(line)
    # Test-only empty lifecycle slots allow compiling the old init for RED.
    if not re.search(r"^static TaskHandle_t worker_task;", text, re.M):
        globals_ += ["static TaskHandle_t worker_task;\n", "static bool worker_finished;\n"]
    (out / f"{module}-lifecycle.inc").write_text("".join(globals_) + bodies, encoding="utf-8")
_, admission = extract(Path("main/tailnet_spike.c"),
                       ["tailnet_spike_audio_acquire", "tailnet_spike_audio_release", "tailnet_spike_start"])
(out / "audio-admission.inc").write_text(admission, encoding="utf-8")
PY
cc -Wall -Wextra -Werror -g -pthread -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I "$out" -I main -I tests/tls_host -I ../../tests/touch_host tests/audio_lifecycle_test.c -o "$out/audio_lifecycle"
"$out/audio_lifecycle"