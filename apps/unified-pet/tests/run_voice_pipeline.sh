#!/bin/sh
# Exact production bodies; only RTOS, hardware, clock and transport are seams.
set -eu
cd "$(dirname "$0")/.."
out=${1:?output directory required}
mkdir -p "$out"
python3 - "$out" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re
import sys
out=Path(sys.argv[1]).expanduser()
source=Path(os.environ.get("VOICE_SOURCE", "main/live_voice.c"))
text=source.read_text(encoding="utf-8")
start=text.index("#define CHUNK")
end=text.index("static void set_state")
parts=[text[start:end]]
names=["set_state","live_voice_snapshot","fail","send_text","speaker_active",
       "capture_audio","play_audio","synchronize_clock","run_session","worker",
       "live_voice_reap_finished","live_voice_start","live_voice_stop","live_voice_init"]
# Instrumentation helpers, when present, are also verbatim production, not stubs.
for name in ("session_stop_locked", "session_stop", "bridge_session_stop", "pipeline_fail", "pipeline_log"):
    if re.search(rf"^static .*?\b{name}\(",text,re.M):
        names.insert(names.index("fail"),name)
hashes={}
for name in names:
    match=re.search(rf"^(?:static )?[\w *]+\b{name}\(",text,re.M)
    assert match, name
    pos=text.index("{", match.start())
    # Braces in strings balance in these bodies; no preprocessor body replacement.
    depth=1
    end=pos+1
    while depth:
        depth += (text[end]=="{")-(text[end]=="}")
        end+=1
    body=text[match.start():end]+"\n"
    hashes[name]=hashlib.sha256(body.encode()).hexdigest()
    parts.append(f'#line {text.count(chr(10),0,match.start())+1} "{source}"\n'+body)
definitions=parts[0]
hashes["connection-and-globals"]=hashlib.sha256(definitions.encode()).hexdigest()
prefix='#define PET_VOICE_METRICS 1\n' if "voice_pipeline_t" in definitions else ""
(out/"voice-production.inc").write_text(prefix+"\n".join(parts),encoding="utf-8")
(out/"production-body-hashes.json").write_text(json.dumps({
    "source":str(source),"sha256":hashlib.sha256(text.encode()).hexdigest(),
    "bodies":hashes},indent=2)+"\n",encoding="utf-8")
print("production bodies:",len(hashes)-1,"source sha256:",hashlib.sha256(text.encode()).hexdigest())
PY
lib=${PET_TLS_HOST_LIB:?real host mbedTLS library directory required}
cc -Wall -Wextra -Werror -g -pthread -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I "$out" -I main -I tests -I tests/tls_host \
  -I "$IDF_PATH/components/mbedtls/mbedtls/include" \
  tests/voice_pipeline_test.c "$lib/libmbedcrypto.a" -lm -o "$out/voice_pipeline"
"$out/voice_pipeline" "${VOICE_CASE:-all}"