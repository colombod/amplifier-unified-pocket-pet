#!/bin/sh
# Inside the pinned Docker SDK only. No device, network, provider or Unified.
# Runs the WHOLE production bridge_voice.c body (owner loop, both audio tasks,
# cleanup and reuse) verbatim, in conversation mode, against a scripted host.
# RTOS, clock, codec and transport are synthetic: not physical or TLS proof.
set -eu
cd "$(dirname "$0")/.."
out=${1:?fresh absolute output required}
case "$out" in /*) ;; *) echo "absolute output required" >&2; exit 2;; esac
if [ -e "$out" ]; then echo "fresh output required: $out" >&2; exit 2; fi
ulimit -c 0
mkdir -p "$out"
python3 - "$out" <<'PY'
import hashlib,json,sys
from pathlib import Path
out=Path(sys.argv[1])
inputs=[Path('main/bridge_voice.c'),Path('main/bridge_protocol.c'),Path('main/bridge_protocol.h'),
        Path('main/live_protocol.h'),Path('main/live_voice.h'),Path('main/assistant_config.h'),
        Path('tests/bridge_converse_test.c'),Path('tests/run_bridge_converse.sh')]
snapshots={str(p):p.read_bytes() for p in inputs}
text=snapshots['main/bridge_voice.c'].decode('utf-8')
anchor='#define BV_MIC'
assert text.count(anchor)==1,'production anchor moved'
start=text.index(anchor)
# Everything after the include block, unmodified: no body is replaced or skipped.
assert '#include' not in text[start:],'unexpected include inside production body'
body=text[start:]
(out/'bridge-converse-production.inc').write_text(
    f'#line {text.count(chr(10),0,start)+1} "main/bridge_voice.c"\n'+body)
(out/'inputs.json').write_text(json.dumps({
    'inputs':{p:hashlib.sha256(d).hexdigest() for p,d in snapshots.items()},
    'production_body_sha256':hashlib.sha256(body.encode()).hexdigest(),
    'scope':'complete bridge_voice.c after its includes; synthetic RTOS/clock/codec/transport',
},indent=2)+'\n')
PY
finish() {
    rc=$?
    trap - EXIT
    if python3 - "$out" <<'PY'
import hashlib,json,sys
from pathlib import Path
manifest=json.loads((Path(sys.argv[1])/'inputs.json').read_text())['inputs']
ok=all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in manifest.items())
print('INPUT INTEGRITY:','MATCH' if ok else 'DRIFT')
sys.exit(0 if ok else 86)
PY
    then :; else rc=86; fi
    exit "$rc"
}
trap finish EXIT
lib=${PET_TLS_HOST_LIB:?real host mbedcrypto required}
cc -std=gnu11 -Wall -Wextra -Werror -g -pthread -fsanitize=address,undefined -fno-omit-frame-pointer \
 -UNDEBUG \
 -I "$out" -I main -I "$IDF_PATH/components/json/cJSON" -I "$IDF_PATH/components/mbedtls/mbedtls/include" \
 tests/bridge_converse_test.c main/bridge_protocol.c "$IDF_PATH/components/json/cJSON/cJSON.c" "$lib/libmbedcrypto.a" -o "$out/converse"
failed=0
for name in normal off-connecting off-connected off-started off-opening off-listening off-asking \
    off-answering off-parent off-no-reply host-quiet host-time-limit host-close host-drop host-burst-error host-error \
    error-busy error-mid malformed turn-frame unknown-type no-connect no-begin deadline \
    audio-open mic-read speaker-write send-short setup-token setup-start stall flood burst-ok \
    gappy lag late-600 late-600-heavy recover stall-long retained sequence
do
    if "$out/converse" "$name"; then rc=0; else rc=$?; failed=1; fi
    printf '%s %s\n' "$name" "$rc" >> "$out/case-results.txt"
done
exit "$failed"
