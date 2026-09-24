# -*- coding: utf-8 -*-
"""软件锁 —— 图标绘制。

* make_app_icon  : 程序自身图标（蓝色渐变 + 锁形），用于窗口、托盘与 exe
* letter_avatar  : 提取不到 exe 图标时，用应用名首字生成一个彩色头像
"""
from __future__ import annotations

import hashlib

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover
    Image = None

BRAND_TOP = (59, 130, 246)     # #3B82F6
BRAND_BOTTOM = (29, 78, 216)   # #1D4ED8
AVATAR_COLORS = [
    ((59, 130, 246), (29, 78, 216)),
    ((16, 185, 129), (5, 150, 105)),
    ((245, 158, 11), (217, 119, 6)),
    ((139, 92, 246), (109, 40, 217)),
    ((236, 72, 153), (190, 24, 93)),
    ((14, 165, 233), (2, 132, 199)),
    ((239, 68, 68), (185, 28, 28)),
    ((100, 116, 139), (51, 65, 85)),
]

_SS = 4  # 超采样倍数，让边缘更平滑


def _rounded_rect_mask(size: int, radius_ratio: float = 0.22):
    mask = Image.new("L", (size * _SS, size * _SS), 0)
    draw = ImageDraw.Draw(mask)
    r = int(size * _SS * radius_ratio)
    draw.rounded_rectangle([0, 0, size * _SS - 1, size * _SS - 1], radius=r, fill=255)
    return mask.resize((size, size), Image.LANCZOS)


def _gradient(size: int, top, bottom):
    img = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        img.putpixel(
            (0, y),
            (
                int(top[0] + (bottom[0] - top[0]) * t),
                int(top[1] + (bottom[1] - top[1]) * t),
                int(top[2] + (bottom[2] - top[2]) * t),
            ),
        )
    return img.resize((size, size), Image.BILINEAR)


def draw_lock(size: int, top=BRAND_TOP, bottom=BRAND_BOTTOM, transparent_bg: bool = False):
    """画一个圆角方块 + 白色锁形。"""
    if Image is None:
        return None
    s = size * _SS
    base = _gradient(s, top, bottom).convert("RGBA")
    canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    if not transparent_bg:
        canvas.paste(base, (0, 0), _rounded_rect_mask(size).resize((s, s), Image.NEAREST))
    else:
        canvas.paste(base, (0, 0))

    draw = ImageDraw.Draw(canvas)
    white = (255, 255, 255, 255)
    unit = s / 100.0

    # 锁梁（上半环）
    arc_box = [32 * unit, 20 * unit, 68 * unit, 56 * unit]
    draw.arc(arc_box, start=180, end=360, fill=white, width=int(9 * unit))
    # 锁体
    draw.rounded_rectangle(
        [26 * unit, 46 * unit, 74 * unit, 82 * unit],
        radius=int(9 * unit),
        fill=white,
    )
    # 锁孔
    hole_r = 5.5 * unit
    cx, cy = 50 * unit, 63 * unit
    draw.ellipse([cx - hole_r, cy - hole_r, cx + hole_r, cy + hole_r], fill=(29, 78, 216, 255))
    draw.rounded_rectangle(
        [cx - 2.1 * unit, cy, cx + 2.1 * unit, cy + 11 * unit],
        radius=int(2.1 * unit),
        fill=(29, 78, 216, 255),
    )

    return canvas.resize((size, size), Image.LANCZOS)


def make_app_icon(size: int = 256):
    """程序图标（圆角方块版）。"""
    return draw_lock(size)


def save_ico(path: str, sizes=(16, 24, 32, 48, 64, 128, 256)) -> bool:
    if Image is None:
        return False
    try:
        base = make_app_icon(256)
        base.save(path, format="ICO", sizes=[(s, s) for s in sizes])
        return True
    except Exception:
        return False


def _font(size: int):
    for name in ("msyhbd.ttc", "msyh.ttc", "segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def letter_avatar(name: str, size: int = 32):
    """按名称生成一个彩色首字头像。"""
    if Image is None:
        return None
    s = size * _SS
    key = hashlib.md5((name or "?").encode("utf-8")).hexdigest()
    top, bottom = AVATAR_COLORS[int(key[:2], 16) % len(AVATAR_COLORS)]
    img = _gradient(s, top, bottom).convert("RGBA")
    canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    canvas.paste(img, (0, 0), _rounded_rect_mask(size, 0.28).resize((s, s), Image.NEAREST))

    ch = (name or "?").strip()[:1]
    if ch and ch.isascii():
        ch = ch.upper()
    draw = ImageDraw.Draw(canvas)
    font = _font(int(s * 0.52))
    if font is not None:
        try:
            box = draw.textbbox((0, 0), ch, font=font)
            w, h = box[2] - box[0], box[3] - box[1]
            draw.text(
                ((s - w) / 2 - box[0], (s - h) / 2 - box[1]),
                ch,
                font=font,
                fill=(255, 255, 255, 255),
            )
        except Exception:
            pass
    return canvas.resize((size, size), Image.LANCZOS)


def app_avatar(path: str, name: str, size: int = 32):
    """优先提取 exe 自带图标，失败则用首字头像。"""
    import winsys

    img = None
    try:
        img = winsys.extract_exe_icon(path, size)
    except Exception:
        img = None
    if img is None:
        img = letter_avatar(name or path, size)
    return img
