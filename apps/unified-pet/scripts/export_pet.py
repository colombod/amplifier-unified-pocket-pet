"""Export the owner-supplied Amplifier v2 atlas without redrawing its pixels."""
import hashlib, json, struct
from pathlib import Path
from PIL import Image
ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
source = REPO / "assets/source/amplifier-assistant.webp"
# Row order/counts from the approved v2 atlas metadata. Offsets are consumed by main.c.
states = [("idle",6),("right",8),("left",8),("wave",4),("jump",5),("error",8),("wait",6),("think",6),("review",6),("look_a",8),("look_b",8)]


def export(source=source, out=ROOT / "assets/generated"):
    with Image.open(source) as sheet:
        if sheet.format != "WEBP" or sheet.mode != "RGBA" or sheet.size != (1536, 2288):
            raise ValueError("Expected a 1536x2288 RGBA WebP v2 atlas (192x208 cells)")
        data = bytearray(); manifest = []; first = 0
        background = struct.pack("<H", ((15>>3)<<11)|((23>>2)<<5)|(42>>3)) * (192*208)
        for row, (name, count) in enumerate(states):
            manifest.append(dict(name=name, first=first, frames=count))
            for col in range(count):
                frame = sheet.crop((col*192,row*208,(col+1)*192,(row+1)*208))
                if frame.getchannel("A").getbbox() is None:
                    raise ValueError(f"Blank required cell: {name}, row {row}, column {col}")
                bg = Image.new("RGBA", frame.size, (15,23,42,255))
                bg.alpha_composite(frame)
                pixels = b"".join(struct.pack("<H", ((r>>3)<<11)|((g>>2)<<5)|(b>>3))
                                  for r,g,b,a in bg.getdata())
                if pixels == background:
                    raise ValueError(f"Blank required cell: {name}, row {row}, column {col}")
                data += pixels
            first += count
        # Validate every required cell before publishing any generated output.
        out.mkdir(parents=True, exist_ok=True)
        (out/"amplifier-assistant.rgb565").write_bytes(data)
        report = dict(source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),width=192,height=208,frames=first,asset_bytes=len(data),frame_bytes=192*208*2,source_decoded_rgba_bytes=1536*2288*4,states=manifest)
        (out/"manifest.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
        preview = Image.new("RGB", (240,284),(15,23,42))
        preview.paste(sheet.crop((0,0,192,208)),(24,30),sheet.crop((0,0,192,208)))
        preview.save(out/"pet-preview.png")
        return report


if __name__ == "__main__":
    print(json.dumps(export(), indent=2))
