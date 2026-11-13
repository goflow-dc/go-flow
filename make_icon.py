"""Draws the Go Flow icon (green rounded square with a wave) to assets/icon.ico and icon.png.

Run: python make_icon.py   (needs Pillow). Only needed again if the logo changes.
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "assets"
S = 1024  # draw big, then shrink for smooth edges


def draw():
    # diagonal gradient, green to teal
    grad = Image.new("RGB", (S, S))
    px = grad.load()
    a, b = (0x22, 0xC5, 0x5E), (0x14, 0xB8, 0xA6)
    for y in range(S):
        for x in range(S):
            t = (x + y) / (2 * S)
            px[x, y] = tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.29), fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)

    # one smooth wave across the middle
    d = ImageDraw.Draw(img)
    r = S * 0.105 / 2
    for i in range(3001):  # stamp a round brush along the curve: smooth stroke with round ends
        t = i / 3000
        x = S * (0.17 + 0.66 * t)
        y = S * (0.52 - 0.17 * math.sin(t * 2 * math.pi))
        d.ellipse([x - r, y - r, x + r, y + r], fill=(0x0F, 0x14, 0x19, 255))
    return img


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    icon = draw()
    icon.resize((512, 512), Image.LANCZOS).save(OUT / "icon.png")
    icon.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", OUT / "icon.ico", "and icon.png")
