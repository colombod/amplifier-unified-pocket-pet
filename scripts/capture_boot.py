"""Reset and observe only the selected board; retain raw output privately."""
import argparse,json,time
from pathlib import Path
import serial
from esptool.reset import HardReset
p=argparse.ArgumentParser();p.add_argument('port');p.add_argument('output');p.add_argument('--seconds',type=int,default=25);p.add_argument('--no-reset',action='store_true');a=p.parse_args()
s=serial.Serial(port=None,baudrate=115200,timeout=.25);s.dtr=False;s.rts=False;s.port=a.port;s.open();s.reset_input_buffer()
if not a.no_reset: HardReset(s,uses_usb=True)()
output=bytearray();until=time.monotonic()+a.seconds
while time.monotonic()<until:
 output.extend(s.read(8192))
s.close();path=Path(a.output);path.write_bytes(output);path.chmod(0o600)
text=output.decode('utf-8','replace')
print(json.dumps(dict(bytes=len(output),ui_ready='Tachikoma UI ready' in text,panic='Guru Meditation' in text or 'abort() was called' in text,boot_count=text.count('ESP-ROM:'),psram_detected='Found 8MB PSRAM device' in text)))
