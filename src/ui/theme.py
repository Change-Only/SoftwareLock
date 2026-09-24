# -*- coding: utf-8 -*-
"""界面主题：颜色、字体与 ttk 样式（浅色）。"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

BG = "#F3F5F9"
CARD = "#FFFFFF"
CARD_ALT = "#FAFBFD"
BORDER = "#E2E7F0"
BORDER_STRONG = "#CBD5E1"
TEXT = "#1B2436"
TEXT_SUB = "#5A6779"
MUTED = "#8A94A6"

PRIMARY = "#2563EB"
PRIMARY_HOVER = "#1D4ED8"
PRIMARY_SOFT = "#EAF0FE"

DANGER = "#DC2626"
DANGER_HOVER = "#B91C1C"
DANGER_SOFT = "#FEECEC"

SUCCESS = "#15A34A"
SUCCESS_SOFT = "#E7F7EC"

WARN = "#D97706"
WARN_SOFT = "#FDF3E3"

GHOST_HOVER = "#EDF1F7"

FONT_FAMILY = "Microsoft YaHei UI"

# 由 DPI 决定的界面缩放因子，1.0 = 96 DPI
SCALE = 1.0
_DPI = 96


def px(value: float) -> int:
    """把设计稿像素换算成当前 DPI 下的像素。"""
    return int(round(value * SCALE))


def init_dpi_awareness() -> None:
    """必须在创建 Tk 之前调用，避免系统对窗口做位图拉伸。"""
    global _DPI
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        return
    try:
        import ctypes

        hdc = ctypes.windll.user32.GetDC(0)
        dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))  # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        if dpi > 0:
            _DPI = dpi
    except Exception:
        pass


def fonts() -> dict:
    """按可用性挑选中文字体，返回各档字号。"""
    available = set(tkfont.families())
    family = FONT_FAMILY
    for candidate in (FONT_FAMILY, "Microsoft YaHei", "微软雅黑", "Segoe UI"):
        if candidate in available:
            family = candidate
            break
    return {
        "family": family,
        "title": (family, 16, "bold"),
        "h1": (family, 12, "bold"),
        "h2": (family, 10, "bold"),
        "body": (family, 10),
        "small": (family, 9),
        "tiny": (family, 8),
        "mono": ("Consolas", 9),
        "big": (family, 22, "bold"),
        "entry": (family, 11),
    }


def apply_theme(root: tk.Tk) -> dict:
    global SCALE
    try:
        root.tk.call("tk", "scaling", _DPI / 72.0)
    except Exception:
        pass
    SCALE = max(1.0, min(2.5, _DPI / 96.0))
    f = fonts()
    root.configure(bg=BG)
    try:
        root.option_add("*Font", f["body"])
    except Exception:
        pass

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure(".", background=BG, foreground=TEXT, font=f["body"])
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("TLabel", background=BG, foreground=TEXT, font=f["body"])
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=f["small"])

    style.configure(
        "Vertical.TScrollbar",
        gripcount=0,
        background=BORDER_STRONG,
        darkcolor=CARD,
        lightcolor=CARD,
        troughcolor=BG,
        bordercolor=BG,
        arrowcolor=MUTED,
        arrowsize=12,
        relief="flat",
    )
    style.map("Vertical.TScrollbar", background=[("active", MUTED)])

    style.configure(
        "TCheckbutton",
        background=CARD,
        foreground=TEXT,
        font=f["body"],
        focuscolor=BG,
    )
    style.map("TCheckbutton", background=[("active", CARD)])

    style.configure("TSeparator", background=BORDER)
    return f
