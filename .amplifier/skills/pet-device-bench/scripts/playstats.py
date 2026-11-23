"""Playback continuity per leg: bytes handed to the codec per second (48000 = continuous)."""
import json, sys
from pathlib import Path
print('leg                              out(core,prio) mic(core,prio) | played% short_s pw_max_ms pb_peak underruns | uplink exit | cpu ws live lvgl idle0 idle1')
for root in sys.argv[1:]:
    for d in sorted(Path(root).glob('*/device/summary.json')):
        s = json.load(open(d)); t = {x['name']: x for x in s['tasks']}
        st = [b for b in s['bins'] if 1 <= b['index'] and b['index'] + 1 <= min(31, s['duration_ms'] / 1000 - 1)]
        if not st or 'pet_bridge_out' not in t: continue
        tot = sum(b['play_bytes'] for b in st)
        g = lambda n: round(t[n]['cpu_percent_of_one_core']) if n in t else -1
        o, m = t['pet_bridge_out'], t.get('pet_bridge_mic', {'core': 9, 'priority': 0})
        p = s.get('playback') or {}
        print(f"{d.parent.parent.name:32s} ({o['core']:2d},{o['priority']})        ({m['core']:2d},{m['priority']})       | {100*tot/(48000*len(st)):6.1f} {sum(1 for b in st if b['play_bytes'] < 47000):7d} "
              f"{max(b['play_write_us_max'] for b in st)//1000:9d} {max(b['playback_peak'] for b in st):7d} {p.get('underruns', -1):9d} | {s['steady_uplink_ratio']:.2f} {s['exit_reason']:4d} | {g('websocket_task')} {g('pet_live')} {g('taskLVGL')} {g('IDLE0')} {g('IDLE1')}")
