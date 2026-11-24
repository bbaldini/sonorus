"""Genera sonorus.ico: bacchetta dorata con onde sonore su fondo viola scuro."""
from pathlib import Path

from PIL import Image, ImageDraw

S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([16, 16, S - 16, S - 16], radius=110, fill=(28, 22, 42, 255))
gold = (232, 185, 86, 255)
# bacchetta
d.line([(110, 400), (290, 220)], fill=gold, width=34)
d.ellipse([92, 382, 128, 418], fill=gold)
d.ellipse([270, 200, 310, 240], fill=(255, 226, 150, 255))
# onde
cx, cy = 300, 210
for i, (r, a) in enumerate([(70, 255), (120, 200), (170, 150)]):
    col = (95, 211, 216, a)
    d.arc([cx - r, cy - r, cx + r, cy + r], start=-65, end=25, fill=col, width=26)
out = Path(__file__).resolve().parent.parent / "sonorus.ico"
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(out)
