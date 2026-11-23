"""Per-leg placement and health table for bench sessions."""
import json, sys
from pathlib import Path
print('run/leg                          live_core frame_ms send_ms send_max uplink qpeak exit IDLE0 IDLE1 LVGL ws live')
for root in sys.argv[1:]:
    for d in sorted(Path(root).glob('*/device/summary.json')):
        s = json.load(open(d)); t = {x['name']: x for x in s['tasks']}
        st = [b for b in s['bins'] if 1 <= b['index'] and b['index'] + 1 <= s['duration_ms'] / 1000]
        fr = sum(b['frame_count'] for b in st) or 1; sn = sum(b['send_count'] for b in st) or 1
        g = lambda n: round(t[n]['cpu_percent_of_one_core']) if n in t else -1
        print(f"{d.parent.parent.parent.name + '/' + d.parent.parent.name:32s} {t['pet_live']['core']:5d} "
              f"{sum(b['frame_us_total'] for b in st)/fr/1000:8.1f} {sum(b['send_us_total'] for b in st)/sn/1000:7.1f} {s['max_send_ms']:8.0f} "
              f"{s['steady_uplink_ratio']:6.2f} {max(b['queue_peak'] for b in s['bins']):6d} {s['exit_reason']:3d} {g('IDLE0'):5d} {g('IDLE1'):5d} {g('taskLVGL'):4d} {g('websocket_task'):3d} {g('pet_live'):4d}")
