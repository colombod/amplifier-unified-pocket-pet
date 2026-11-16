#!/bin/sh
# PARENT EXECUTION ONLY, inside the pinned Docker SDK. No device/network access.
# Production bodies/excerpts are verbatim. Fixture time is not a codec measurement.
set -eu
cd "$(dirname "$0")/.."
out=${1:?fresh absolute output required}
case "$out" in /*) ;; *) echo "absolute output required" >&2; exit 2;; esac
if [ -e "$out" ]; then echo "fresh output required: $out" >&2; exit 2; fi
ulimit -c 0
mkdir -p "$out"
python3 - "$out" <<'PY'
import hashlib,json,re,sys
from pathlib import Path
out=Path(sys.argv[1])
inputs=[
    Path('main/bridge_voice.c'),Path('main/audio_test.c'),
    Path('main/bridge_protocol.c'),Path('main/bridge_protocol.h'),
    Path('main/live_protocol.h'),Path('tests/bridge_runtime_test.c'),
    Path('tests/run_bridge_native.sh'),
]
def digest(data):
    return hashlib.sha256(data).hexdigest()
snapshots={str(p.resolve()):p.read_bytes() for p in inputs}
source=Path('main/bridge_voice.c')
text=snapshots[str(source.resolve())].decode('utf-8')

def unique(haystack,needle):
    assert haystack.count(needle)==1, ('nonunique/missing anchor',needle)
    return haystack.index(needle)

def brace_end(s,start):
    """Balance C braces, ignoring comments and string/character literals."""
    assert s[start]=='{'
    depth=0;i=start
    while i<len(s):
        if s.startswith('//',i):
            e=s.find('\n',i+2);i=len(s) if e<0 else e+1;continue
        if s.startswith('/*',i):
            e=s.find('*/',i+2);assert e>=0,'unterminated comment'
            i=e+2;continue
        if s[i] in "\"'":
            quote=s[i];i+=1
            while i<len(s) and s[i]!=quote:
                i+=2 if s[i]=='\\' else 1
            assert i<len(s),'unterminated literal'
        elif s[i]=='{':
            depth+=1
        elif s[i]=='}':
            depth-=1
            if depth==0:return i+1
        i+=1
    raise AssertionError('unbalanced source')

def function(name):
    matches=list(re.finditer(r'^(?:static )?(?:bool|void|const char)\s+\**'+re.escape(name)+r'\(',text,re.M))
    assert len(matches)==1,('function count',name,len(matches))
    start=matches[0].start();p=text.index('{',start)
    return start,brace_end(text,p)

def located(start,end):
    return f'#line {text.count(chr(10),0,start)+1} "main/bridge_voice.c"\n'+text[start:end]

a=unique(text,'#define BV_MIC');b=unique(text,'static bool take(')
parts=[text[a:b]]; hashes={};excerpts={}
for name in ['take','give','quiet','note_exit','stopping','exit_status','callback_failed','failure','speaker_active','capture_task','play_task','ws_event','transport_lost','pace_sent_audio','send_frame_timed','send_frame','send_action','append_audio','finish_capture','finalize_model','bridge_voice_stop','bridge_voice_finish_capture','bridge_voice_confirm','bridge_voice_snapshot','audio_exited','converse_end','converse_tail','cleanup','bridge_voice_reap_finished']:
    start,end=function(name);body=text[start:end]
    hashes[name]=digest(body.encode());parts.append(located(start,end))

# Deliberately NOT a reimplementation of next_audio/append policy. Extract the
# whole production capture-send block, including its break/failure and cursor
# argument. append_audio owns timed admission + success-only cursor advancement.
run_start,run_end=function('bridge_voice_run')
header='while(esp_timer_get_time()<c->work&&!stopping(c)&&!atomic_load(&c->failed))'
h=unique(text,header)
a=unique(text,'if((phase==BR_CAPTURING||phase==BR_CONVERSING)&&esp_timer_get_time()>=next_audio)')
p=text.index('{',a);b=brace_end(text,p)
d=unique(text,'if(esp_timer_get_time()>=c->work)failure(c,"deadline");')
de=d+len('if(esp_timer_get_time()>=c->work)failure(c,"deadline");')
assert run_start<h<a<b<d<de<run_end
assert text[b:].lstrip().startswith('publish(c);'), 'capture block moved: review extraction'
for name,start,end in [('owner_guard',h,h+len(header)),('owner_capture_send',a,b),('owner_deadline',d,de)]:
    excerpts[name]={'start':start,'end':end,'sha256':digest(text[start:end].encode())}
parts.append('''
/* Only shell/phase snapshot/tick are fixtures; guard, block and deadline below
 * are verbatim production. This is NOT the full owner or UI/event dispatcher. */
static void production_capture_owner(bridge_voice_t *c,int64_t *cursor){
    int64_t next_audio=*cursor;
'''+located(h,h+len(header))+'''{
        if(!a5_owner_tick())break; /* bounded synthetic queue-wait/producer seam */
        bridge_phase_t phase=c->model.phase;
'''+located(a,b)+'''
    }
    *cursor=next_audio;
'''+located(d,de)+'''
}
''')
(out/'bridge-runtime-production.inc').write_text('\n'.join(parts)+'\n')
(out/'production-bodies.json').write_text(json.dumps({
    'source':str(source),'sha256':digest(text.encode()),'bodies':hashes,
    'excerpts':excerpts,'scope':'actual cleanup + owner guard/capture/send/deadline; NOT full owner or vendor task',
    'fixture':'5ms queue-wait tick; 960B/20ms producer; real capture_task tested separately',
},indent=2)+'\n')
audio=snapshots[str(Path('main/audio_test.c').resolve())].decode('utf-8')
a=unique(audio,'static portMUX_TYPE amp_mux');b=unique(audio,'esp_err_t audio_hardware_quiet(')
body=audio[a:b];hashes={'amp_latch':digest(body.encode())}
(out/'bridge-amp-production.inc').write_text(body)
(out/'amp-bodies.json').write_text(json.dumps({'source':'main/audio_test.c','sha256':digest(audio.encode()),'bodies':hashes},indent=2)+'\n')
for p,data in snapshots.items():
    assert Path(p).read_bytes()==data,('input changed during extraction',p)
(out/'input-hashes.json').write_text(json.dumps({
    'inputs':{p:digest(data) for p,data in snapshots.items()},
    'generated':{str(p.resolve()):digest(p.read_bytes()) for p in [
        out/'bridge-runtime-production.inc',out/'bridge-amp-production.inc',
        out/'production-bodies.json',out/'amp-bodies.json']},
},indent=2)+'\n')
PY

# Integrity evidence is recorded on compiler failure, assertion RED and GREEN.
# Do not let a failing case skip the other independent cases or this readback.
finish() {
    rc=$?
    trap - EXIT
    if python3 - "$out" <<'PY'
import hashlib,json,sys
from pathlib import Path
out=Path(sys.argv[1]);manifest=json.loads((out/'input-hashes.json').read_text())
results=[]
for group,entries in manifest.items():
    for name,expected in entries.items():
        p=Path(name)
        actual=hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
        results.append({'group':group,'path':name,'expected':expected,'actual':actual,'match':actual==expected})
ok=all(row['match'] for row in results)
(out/'input-integrity.json').write_text(json.dumps({'all_match':ok,'files':results},indent=2)+'\n')
print('INPUT INTEGRITY:', 'MATCH' if ok else 'DRIFT')
sys.exit(0 if ok else 86)
PY
    then :; else rc=86; fi
    exit "$rc"
}
trap finish EXIT
lib=${PET_TLS_HOST_LIB:?real host mbedcrypto required}
cc -std=gnu11 -Wall -Wextra -Werror -g -fsanitize=address,undefined -fno-omit-frame-pointer \
 -UNDEBUG \
 -I "$out" -I main -I "$IDF_PATH/components/json/cJSON" -I "$IDF_PATH/components/mbedtls/mbedtls/include" \
 tests/bridge_runtime_test.c main/bridge_protocol.c "$IDF_PATH/components/json/cJSON/cJSON.c" "$lib/libmbedcrypto.a" -o "$out/runtime"
failed=0
for name in legacy realtime-0 realtime-25 realtime-75 \
    realtime-long-0 realtime-long-25 realtime-long-75 realtime-long-67 realtime-long-jitter \
    realtime-long-owner-jitter realtime-long-alternating \
    pacer-stall empty small-0 small-25 \
    finish-0 finish-25 finish-75 finish-full-75 capture-overflow capture-near-full capture-inflight-stop \
    capture-inflight-failure too-slow-200 owner-wait-stop owner-wait-failure owner-wait-deadline \
    finish-wait-stop finish-wait-failure finish-wait-deadline \
    send-stop send-failure send-deadline send-short admission-stop \
    oracle-suffix-credit prep-timing-owner prep-timing-finish \
    tick-deadline tick-parent-stop tick-failure \
    prep-deadline prep-parent-stop prep-ui-stop prep-failure \
    lock-deadline lock-parent-stop lock-failure \
    empty-target empty-target-stop empty-target-parent-stop empty-target-failure empty-target-deadline \
    finish-admission-parent-stop finish-admission-failure finish-admission-deadline \
    owner-long-stall variable-owner-drain capture-read-failure model-final-guard-failure \
    closing-work-hard-caps send-zero send-negative timed-short timed-zero timed-negative \
    timed-out-full timed-out-short timed-out-zero timed-out-negative \
    tick-failed-flag prep-failed-flag lock-failed-flag finish-wait-failed-flag \
    empty-target-failed-flag finish-admission-failed-flag \
    cleanup-reuse cleanup-unknown cleanup-audio-final cleanup-timeout cleanup-transport-timeout cleanup-hard-cap cleanup-interrupted-tail
do
    if "$out/runtime" "$name"; then rc=0; else rc=$?; failed=1; fi
    printf '%s %s\n' "$name" "$rc" >> "$out/case-results.txt"
done
exit "$failed"
