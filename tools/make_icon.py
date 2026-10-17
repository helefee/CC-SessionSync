# -*- coding: utf-8 -*-
"""生成程序图标（icon.ico / icon.png / tray-active.ico / tray-badge.ico）。python tools/make_icon.py；要 Pillow。
图案：暖橙圆角方块 + 两段白色同步箭头，中间一个对话气泡（会话在两个账号 / 两个工具之间同步）。"""
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "cc_sessionsync"
S = 1024


def base() -> Image.Image:
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # 竖直渐变的圆角方块
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    top, bot = (222, 125, 88), (176, 74, 44)
    for y in range(S):
        t = y / (S - 1)
        gd.line([(0, y), (S, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([40, 40, S - 40, S - 40], radius=230, fill=255)
    im.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(im)
    cx, cy, r, w = S / 2, S / 2, 300, 74
    white = (255, 255, 255, 255)
    for start in (200, 20):                       # 两段弧，各 130°，末端带箭头
        a0, a1 = start, start + 130
        d.arc([cx - r, cy - r, cx + r, cy + r], a0, a1, fill=white, width=w)
        ang = math.radians(a1)
        tip = (cx + r * math.cos(ang), cy + r * math.sin(ang))
        tang = ang + math.pi / 2                  # 顺时针方向的切线
        L, H = 120, 95
        p1 = (tip[0] + L * math.cos(tang), tip[1] + L * math.sin(tang))
        nrm = ang
        p2 = (tip[0] + H * math.cos(nrm), tip[1] + H * math.sin(nrm))
        p3 = (tip[0] - H * math.cos(nrm), tip[1] - H * math.sin(nrm))
        d.polygon([p1, p2, p3], fill=white)
    # 中间的对话气泡
    bw, bh = 250, 180
    x0, y0 = cx - bw / 2, cy - bh / 2 - 15
    d.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], radius=60, fill=white)
    d.polygon([(x0 + 55, y0 + bh - 10), (x0 + 40, y0 + bh + 60), (x0 + 120, y0 + bh - 10)], fill=white)
    dot = (190, 85, 52, 255)
    for i in range(3):
        x = x0 + 70 + i * 55
        d.ellipse([x - 19, y0 + bh / 2 - 19, x + 19, y0 + bh / 2 + 19], fill=dot)
    return im


def with_dot(im: Image.Image, color) -> Image.Image:
    im = im.copy()
    d = ImageDraw.Draw(im)
    r = 210
    d.ellipse([S - 2 * r - 10, S - 2 * r - 10, S - 10, S - 10], fill=(255, 255, 255, 255))
    d.ellipse([S - 2 * r + 25, S - 2 * r + 25, S - 45, S - 45], fill=color)
    return im


def main():
    im = base()
    sizes = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]
    im.resize((256, 256), Image.LANCZOS).save(OUT / "icon.png")
    im.save(OUT / "icon.ico", sizes=sizes)
    with_dot(im, (232, 145, 40, 255)).save(OUT / "tray-active.ico", sizes=sizes[:7])
    with_dot(im, (214, 48, 49, 255)).save(OUT / "tray-badge.ico", sizes=sizes[:7])
    if len(sys.argv) > 1:
        im.resize((512, 512), Image.LANCZOS).save(sys.argv[1])
    print("ok")


if __name__ == "__main__":
    main()
