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
import hashlib,json,os,re,sys
from pathlib import Path
out=Path(sys.argv[1])
inputs=[Path('main/bridge_voice.c'),Path('main/bridge_protocol.c'),Path('main/bridge_protocol.h'),
        Path('main/live_protocol.h'),Path('main/live_voice.h'),Path('main/live_voice.c'),Path('main/assistant_config.h'),
        Path('tests/bridge_converse_test.c'),Path('tests/run_bridge_converse.sh')]
snapshots={str(p):p.read_bytes() for p in inputs}
text=snapshots['main/bridge_voice.c'].decode('utf-8')
anchor='#define BV_MIC'
assert text.count(anchor)==1,'production anchor moved'
start=text.index(anchor)
# Everything after the include block, unmodified: no body is replaced or skipped.
assert '#include' not in text[start:],'unexpected include inside production body'
body=text[start:]
mutant=os.environ.get('PET_CERT_TEST_MUTANT','')
if mutant:
    if mutant!='owner':raise SystemExit('Unsupported certificate test mutation')
    needle='else if(c->transport_terminal==BR_TRANSPORT_CERT_VERIFY)s="Host certificate verification failed.";'
    assert body.count(needle)==1
    body=body.replace(needle,'else if(false&&c->transport_terminal==BR_TRANSPORT_CERT_VERIFY)s="Host certificate verification failed.";',1)
(out/'bridge-converse-production.inc').write_text(
    f'#line {text.count(chr(10),0,start)+1} "main/bridge_voice.c"\n'+body)
generated={}
if os.environ.get('BRIDGE_JOIN_REAL_PARENT')=='1':
    parent=snapshots['main/live_voice.c'].decode('utf-8')
    globals_=('static SemaphoreHandle_t status_lock;','static atomic_bool stop_requested;',
              'static int64_t session_deadline_us;')
    assert all(parent.count(line)==1 for line in globals_)
    globals_path=out/'joined-parent-globals.inc'
    globals_path.write_text('\n'.join(globals_)+'\n')
    bodies=[]
    for name in ('session_stop_locked','bridge_session_stop','live_voice_bridge_audio_begin',
                 'live_voice_bridge_audio_end','live_voice_stop'):
        matches=list(re.finditer(r'^(?:static )?(?:bool|void) '+name+r'\(',parent,re.M))
        assert len(matches)==1,name
        first=matches[0].start();p=parent.index('{',first);depth=1;end=p+1
        while depth:
            depth+=(parent[end]=='{')-(parent[end]=='}');end+=1
        bodies.append(f'#line {parent.count(chr(10),0,first)+1} "main/live_voice.c"\n'+parent[first:end]+'\n')
    parent_path=out/'joined-parent-production.inc'
    parent_path.write_text('\n'.join(bodies))
    generated={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (globals_path,parent_path)}
(out/'inputs.json').write_text(json.dumps({
    'inputs':{p:hashlib.sha256(d).hexdigest() for p,d in snapshots.items()},
    'generated':generated,
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
manifest=json.loads((Path(sys.argv[1])/'inputs.json').read_text())
ok=all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h
       for group in ('inputs','generated') for p,h in manifest[group].items())
print('INPUT INTEGRITY:','MATCH' if ok else 'DRIFT')
sys.exit(0 if ok else 86)
PY
    then :; else rc=86; fi
    exit "$rc"
}
trap finish EXIT
lib=${PET_TLS_HOST_LIB:?real host mbedcrypto required}
if [ "${BRIDGE_JOIN_REAL_PARENT:-0}" = 1 ]; then
    cc -std=gnu11 -Wall -Wextra -Werror -g -pthread -fsanitize=address,undefined -fno-omit-frame-pointer \
     -fno-pie -no-pie -UNDEBUG -DBRIDGE_JOIN_REAL_PARENT=1 \
     -I "$out" -I main -I "$IDF_PATH/components/json/cJSON" -I "$IDF_PATH/components/mbedtls/mbedtls/include" \
     tests/bridge_converse_test.c main/bridge_protocol.c "$IDF_PATH/components/json/cJSON/cJSON.c" "$lib/libmbedcrypto.a" -o "$out/converse"
    failed=0
    for name in joined-gate-owner-wins joined-gate-stop-wins; do
        if "$out/converse" "$name"; then rc=0; else rc=$?; failed=1; fi
        printf '%s %s\n' "$name" "$rc" >> "$out/case-results.txt"
    done
    exit "$failed"
fi
cc -std=gnu11 -Wall -Wextra -Werror -g -pthread -fsanitize=address,undefined -fno-omit-frame-pointer \
 -UNDEBUG \
 -I "$out" -I main -I "$IDF_PATH/components/json/cJSON" -I "$IDF_PATH/components/mbedtls/mbedtls/include" \
 tests/bridge_converse_test.c main/bridge_protocol.c "$IDF_PATH/components/json/cJSON/cJSON.c" "$lib/libmbedcrypto.a" -o "$out/converse"
if [ "${PET_CERT_STITCH_ONLY:-0}" = 1 ]; then exit 0; fi
if [ "${PET_CERT_TEST_MUTANT:-}" = owner ]; then "$out/converse" cert-first; exit "$?"; fi
failed=0
for name in stop-before-active stop-registration stop-reconnect-registration stop-audio-arm stop-parent-audio-arm stop-start-send-admitted stop-capture-guard stop-guard-timeout stop-audio-work-deadline stop-codec-work-deadline open-success-stop open-success-cutoff open-success-control normal off-connecting off-connected off-started off-opening off-listening off-asking \
    off-answering off-parent off-no-reply host-quiet host-time-limit host-burst-error \
    notice-queue notice-linger notice-begin notice-user notice-fatal terminal-wire cert-first cert-reconnect cert-muted cert-unknown cert-no-evidence cert-silent cert-silent-reconnect malformed-tail reviewed-races sdk-event-loss no-error \
    malformed turn-frame unknown-type deadline audio-open mic-read speaker-write setup-token setup-start \
    burst-ok gappy lag late-600 late-600-heavy recover phrase-control phrase-stall phrase-stall-radio send-wait send-wait-off send-wait-off-lost retained \
    re-listening re-asking re-answering re-close re-error re-flood re-send re-stall re-stall-long re-send-wait-long \
    re-first re-first-503 re-first-timeout re-error-busy re-no-begin re-busy \
    re-give-up re-give-up-first re-give-up-timeout re-off-wait re-off-connect re-off-begin re-off-instant \
    terminal-error-delayed terminal-error-overwrite terminal-error-tail-queued terminal-error-tail-pre terminal-error-tail-post \
    terminal-error-muted terminal-local-close terminal-local-normal uncertain-visible-loss uncertain-expired-loss uncertain-tail-queued \
    uncertain-tail-post uncertain-dispose uncertain-muted uncertain-tail-muted ending-controls \
    cleanup-unconfirmed-local cleanup-unconfirmed-short cleanup-unconfirmed-failed cleanup-unconfirmed-cancel cleanup-unconfirmed-cancel-full \
    cleanup-unconfirmed-pre cleanup-unconfirmed-prior cleanup-unconfirmed-later cleanup-unconfirmed-uncertain \
    cleanup-unconfirmed-uncertain-tail cleanup-unconfirmed-muted cleanup-unconfirmed-startup cleanup-unconfirmed-disconnect \
    cleanup-unconfirmed-protocol cleanup-unconfirmed-policy cleanup-unconfirmed-ingress cleanup-unconfirmed-retained \
    cleanup-unconfirmed-peer cleanup-unconfirmed-notice cleanup-unconfirmed-same-tick cleanup-unconfirmed-no-stamp \
    cleanup-unconfirmed-fragment-pre cleanup-unconfirmed-fragment-post \
    cleanup-unconfirmed-pending cleanup-unconfirmed-pending-tail \
    cleanup-race-unconfirmed cleanup-race-independent-unconfirmed cleanup-race-independent-normal \
    cleanup-race-json-closed cleanup-race-confirmed cleanup-race-independent-confirmed \
    cleanup-race-bare-loss cleanup-race-muted cleanup-race-uncertain cleanup-race-pending \
    cleanup-race-unconfirmed-only cleanup-race-retained-json cleanup-race-retained-normal cleanup-race-retained-unconfirmed \
    pending-eof pending-callback pending-accepted pending-1011 pending-close pending-notice pending-listening pending-unknown \
    pending-muted pending-terminal pending-resolved pending-resolved-listening pending-reasking pending-uncertain-resolved \
    pending-tail-queued pending-tail-post pending-tail-resolved pending-dispose pending-dispose-resolved \
    no-http no-retained retry-boundaries re-budget re-budget-short re-budget-long-cleanup re-budget-short-cleanup re-window-origin terminal-code-matrix re-budget-callback-delay re-budget-close-delay re-ceiling re-no-stale sequence
do
    if "$out/converse" "$name"; then rc=0; else rc=$?; failed=1; fi
    printf '%s %s\n' "$name" "$rc" >> "$out/case-results.txt"
done
exit "$failed"
