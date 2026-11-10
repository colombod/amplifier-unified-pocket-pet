"""Owned-board setup observation. Start BEFORE asking the user to pair.
Opening this USB serial port can reset the chip; keep it open for the whole test.
Only pet_net's credential-free status messages are printed. Raw logs stay private.
"""
import argparse,json,time
from pathlib import Path
import serial
p=argparse.ArgumentParser();p.add_argument('port');p.add_argument('output');p.add_argument('--seconds',type=int,default=240);a=p.parse_args()
s=serial.Serial(port=None,baudrate=115200,timeout=.25);s.dtr=False;s.rts=False;s.port=a.port;s.open()
path=Path(a.output);path.parent.mkdir(exist_ok=True,parents=True)
raw=path.open('wb');path.chmod(0o600);until=time.monotonic()+a.seconds;pending=b''
print('Observation started; USB opening may reset the chip. Keep this port open during pairing.',flush=True)
while time.monotonic()<until:
 chunk=s.read(4096)
 if not chunk:continue
 raw.write(chunk);raw.flush();pending+=chunk
 while b'\n' in pending:
  line,pending=pending.split(b'\n',1);text=line.decode('utf-8','replace').strip()
  if 'pet_net:' in text or 'pet_live:' in text or 'audio_test:' in text or 'Tachikoma UI ready' in text or 'App version:' in text or 'Guru Meditation' in text or 'ESP_ERROR_CHECK failed' in text:
   print(text,flush=True)
raw.close();s.close();print('Observation ended.',flush=True)
