#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成应用图标（渐变蓝 + VL 文字），输出 build/icon.png 与多尺寸 build/icon.ico"""
import os
import math
from PIL import Image, ImageDraw, ImageFont

SIZE = 512
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "build")
os.makedirs(OUT_DIR, exist_ok=True)

# 渐变：与 TitleBar logo 一致 (#2563eb -> #1e40af)
C1 = (0x25, 0x63, 0xEB)
C2 = (0x1E, 0x40, 0xAF)

img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
px = img.load()
for y in range(SIZE):
    t = y / (SIZE - 1)
    r = int(C1[0] + (C2[0] - C1[0]) * t)
    g = int(C1[1] + (C2[1] - C1[1]) * t)
    b = int(C1[2] + (C2[2] - C1[2]) * t)
    for x in range(SIZE):
        px[x, y] = (r, g, b, 255)

# 圆角遮罩
radius = int(SIZE * 0.22)
mask = Image.new("L", (SIZE, SIZE), 0)
d = ImageDraw.Draw(mask)
d.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=radius, fill=255)
img.putalpha(mask)

# 顶部高光（微妙的玻璃感）
hl = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
dh = ImageDraw.Draw(hl)
dh.ellipse([-SIZE * 0.35, -SIZE * 0.5, SIZE * 1.35, SIZE * 0.55], fill=(255, 255, 255, 26))
img = Image.alpha_composite(img, hl)

# 文字 "VL"
font_size = int(SIZE * 0.46)
font_paths = [
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\msyhbd.ttc",
]
font = None
for fp in font_paths:
    if os.path.exists(fp):
        try:
            font = ImageFont.truetype(fp, font_size)
            break
        except Exception:
            continue
if font is None:
    font = ImageFont.load_default()

txt = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
dt = ImageDraw.Draw(txt)
bbox = dt.textbbox((0, 0), "VL", font=font)
tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
dt.text(((SIZE - tw) / 2 - bbox[0], (SIZE - th) / 2 - bbox[1]), "VL", font=font, fill=(255, 255, 255, 255))
img = Image.alpha_composite(img, txt)

# PNG + 多尺寸 ICO
png_path = os.path.join(OUT_DIR, "icon.png")
ico_path = os.path.join(OUT_DIR, "icon.ico")
img.save(png_path)
img.save(ico_path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon written:", png_path, ico_path)
