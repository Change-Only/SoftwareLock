# -*- coding: utf-8 -*-
"""界面主题：颜色、字体与 ttk 样式（浅色）。"""
from __future__ import annotations

import os
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
# 主显示器的真实像素尺寸（供自适应布局使用）
MONITOR_W = 0
MONITOR_H = 0

# ---- 自适应缩放 ----------------------------------------------------------
# 设计基准：SCALE=1.0 的界面按 1920×1080 的「逻辑桌面」设计。
# 逻辑桌面 = 物理分辨率 ÷ 系统缩放（4K@200% 与 1080p@100% 都是 1920×1080，
# 所以这两台机器上的观感比例天然一致）。
# 实际缩放 = 系统缩放 × min(1, 逻辑宽/1920, 逻辑高/1080)：
#   · 1080p@125% 笔记本（逻辑 1536×864）→ ×0.8 → 有效缩放回到 1.0，
#     窗口占比与 4K@200% 机器完全相同，不会再显得「字大占满屏」；
#   · 4K@200%（逻辑 1920×1080）→ ×1.0，与现状完全一致；
#   · 只缩不放：逻辑桌面比基准还大时不放大字体（尊重系统缩放偏好）。
DESIGN_W = 1920
DESIGN_H = 1080
SCALE_FLOOR = 0.85     # 小屏下限，避免 1366×768 之类字体小到不可读
SCALE_CEIL = 2.5


def compute_scale(dpi: int, mon_w: int, mon_h: int) -> float:
    """按 DPI 与显示器真实分辨率计算界面缩放因子。"""
    dpi_scale = max(0.5, float(dpi) / 96.0)
    factor = 1.0
    if mon_w > 0 and mon_h > 0:
        logical_w = mon_w / dpi_scale
        logical_h = mon_h / dpi_scale
        factor = min(1.0, logical_w / DESIGN_W, logical_h / DESIGN_H)
    return max(SCALE_FLOOR, min(SCALE_CEIL, dpi_scale * factor))


def px(value: float) -> int:
    """把设计稿像素换算成当前 DPI 下的像素。"""
    return int(round(value * SCALE))


def _tuning() -> tuple:
    """测试用覆盖：SOFTLOCK_UI_DPI / SOFTLOCK_UI_SCREEN=1920x1080。

    在一台高 DPI 机器上复现另一台机器的渲染结果（视觉回归对比用）。
    """
    dpi = 0
    raw = (os.environ.get("SOFTLOCK_UI_DPI") or "").strip()
    if raw:
        try:
            dpi = max(48, min(480, int(float(raw))))
        except ValueError:
            dpi = 0
    sw = sh = 0
    raw = (os.environ.get("SOFTLOCK_UI_SCREEN") or "").strip().lower()
    if "x" in raw:
        try:
            left, right = raw.split("x", 1)
            sw, sh = int(left), int(right)
        except ValueError:
            sw = sh = 0
    return dpi, sw, sh


def init_dpi_awareness() -> None:
    """必须在创建 Tk 之前调用，避免系统对窗口做位图拉伸。"""
    global _DPI, MONITOR_W, MONITOR_H
    force_dpi, force_w, force_h = _tuning()
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
        # DPI 感知之后 GetSystemMetrics 才返回真实像素（否则是被系统缩放过的值）
        MONITOR_W = int(ctypes.windll.user32.GetSystemMetrics(0))
        MONITOR_H = int(ctypes.windll.user32.GetSystemMetrics(1))
        if not force_dpi:
            hdc = ctypes.windll.user32.GetDC(0)
            dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))  # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(0, hdc)
            if dpi > 0:
                _DPI = dpi
    except Exception:
        pass
    if force_dpi:
        _DPI = force_dpi
    if force_w and force_h:
        MONITOR_W, MONITOR_H = force_w, force_h


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
    SCALE = compute_scale(_DPI, MONITOR_W, MONITOR_H)
    # 字体点数也要跟随同样的自适应系数（tk scaling = 每磅像素数）。
    # 例：1080p@125% 时 SCALE 从 1.25 收回 1.0，字体同步从 16.7px 回到 13.3px，
    # 否则会出现「布局缩了、字还是大的」错位。
    dpi_scale = max(0.5, _DPI / 96.0)
    font_factor = SCALE / dpi_scale
    try:
        root.tk.call("tk", "scaling", (_DPI / 72.0) * font_factor)
    except Exception:
        pass
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
