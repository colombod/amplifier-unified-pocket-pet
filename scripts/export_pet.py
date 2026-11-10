"""Export existing ChatGPT v2 pixels; no redrawing or new artwork."""
import hashlib, json, struct
from pathlib import Path
from PIL import Image
ROOT = Path(__file__).resolve().parents[1]
source = ROOT / "assets/source/tachikoma.png"
sheet = Image.open(source).convert("RGBA")
assert sheet.size == (1536, 2288), "Expected the existing v2 sheet"
states = [("idle",6),("right",8),("left",8),("wave",4),("jump",5),("error",8),("wait",6),("think",6),("review",6),("look_a",8),("look_b",8)]
out = ROOT / "assets/generated"
out.mkdir(parents=True, exist_ok=True)
data = bytearray(); manifest = []; first = 0
for row, (name, count) in enumerate(states):
    manifest.append(dict(name=name, first=first, frames=count))
    for col in range(count):
        frame = sheet.crop((col*192,row*208,(col+1)*192,(row+1)*208))
        bg = Image.new("RGBA", frame.size, (15,23,42,255))
        bg.alpha_composite(frame)
        for r,g,b,a in bg.getdata():
            data += struct.pack("<H", ((r>>3)<<11)|((g>>2)<<5)|(b>>3))
    first += count
(out/"tachikoma.rgb565").write_bytes(data)
report = dict(pet_id="pet_6abe2e4dd1f48191bbbc9389b55e641e",source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),width=192,height=208,frames=first,asset_bytes=len(data),frame_bytes=192*208*2,source_decoded_rgba_bytes=1536*2288*4,states=manifest)
(out/"manifest.json").write_text(json.dumps(report, indent=2)+"\n")
preview = Image.new("RGB", (240,284),(15,23,42))
preview.paste(sheet.crop((0,0,192,208)),(24,30),sheet.crop((0,0,192,208)))
preview.save(out/"pet-preview.png")
print(json.dumps(report,indent=2))
