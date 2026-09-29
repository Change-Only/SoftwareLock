# -*- coding: utf-8 -*-
"""自绘控件：圆角按钮、圆角标签、描边输入框、卡片、可滚动容器。"""
from __future__ import annotations

import tkinter as tk

from . import theme as T


def round_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kwargs):
    """像素精确的圆角矩形：两个矩形 + 四个 90° 圆弧。

    不用 smooth 多边形——它在大圆角处会出现毛刺。返回创建的元素 id 列表。
    """
    r = max(0.0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    fill = kwargs.get("fill", "")
    outline = kwargs.get("outline", "")
    width = max(1, int(round(kwargs.get("width", 1) * T.SCALE)))
    ids: list = []

    if fill:
        ids.append(canvas.create_rectangle(x1, y1 + r, x2, y2 - r,
                                           width=0, fill=fill, outline=fill))
        ids.append(canvas.create_rectangle(x1 + r, y1, x2 - r, y2,
                                           width=0, fill=fill, outline=fill))
        for ox, oy, start in ((x1, y1, 90), (x2 - 2 * r, y1, 0),
                              (x2 - 2 * r, y2 - 2 * r, 270), (x1, y2 - 2 * r, 180)):
            ids.append(canvas.create_arc(ox, oy, ox + 2 * r, oy + 2 * r, start=start,
                                         extent=90, style="pieslice", width=0,
                                         fill=fill, outline=fill))

    if outline:
        ids += [
            canvas.create_line(x1 + r, y1, x2 - r, y1, fill=outline, width=width),
            canvas.create_line(x1 + r, y2, x2 - r, y2, fill=outline, width=width),
            canvas.create_line(x1, y1 + r, x1, y2 - r, fill=outline, width=width),
            canvas.create_line(x2, y1 + r, x2, y2 - r, fill=outline, width=width),
            canvas.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r, start=90, extent=90,
                              style="arc", outline=outline, width=width),
            canvas.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r, start=0, extent=90,
                              style="arc", outline=outline, width=width),
            canvas.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2, start=270, extent=90,
                              style="arc", outline=outline, width=width),
            canvas.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2, start=180, extent=90,
                              style="arc", outline=outline, width=width),
        ]
    return ids


class RoundedButton(tk.Canvas):
    """扁平圆角按钮，支持 primary / ghost / danger / soft 四种风格。"""

    KINDS = {
        "primary": (T.PRIMARY, T.PRIMARY_HOVER, "#FFFFFF", None),
        "ghost": (T.CARD, T.GHOST_HOVER, T.TEXT_SUB, T.BORDER),
        "danger": (T.DANGER, T.DANGER_HOVER, "#FFFFFF", None),
        "soft": (T.PRIMARY_SOFT, "#DCE6FC", T.PRIMARY, None),
        "success": (T.SUCCESS, "#0E8B3D", "#FFFFFF", None),
    }

    def __init__(
        self,
        master,
        text: str,
        command=None,
        kind: str = "ghost",
        width: int = 108,
        height: int = 34,
        radius: int = 8,
        font=None,
        bg=None,
        pad: int = 0,
        icon: str = "",
    ):
        self._parent_bg = bg or master.cget("bg")
        width, height, radius = T.px(width), T.px(height), T.px(radius)
        super().__init__(
            master,
            width=width,
            height=height,
            bg=self._parent_bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self._cw, self._ch, self._cr = width, height, radius
        self._command = command
        self._kind = kind
        self._state = "normal"
        self._font = font or (T.fonts()["family"], 10)
        self._label_text = text if not icon else f"{icon}  {text}"
        self._pad = pad
        self._draw()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    # ------------------------------------------------------------ 绘制
    def _colors(self):
        base, hover, fg, border = self.KINDS.get(self._kind, self.KINDS["ghost"])
        if self._state == "disabled":
            return T.BORDER, T.BORDER, T.MUTED, None
        if self._state == "hover":
            return hover, hover, fg, border
        if self._state == "press":
            return hover, hover, fg, border
        return base, hover, fg, border

    def _draw(self):
        self.delete("all")
        base, hover, fg, border = self._colors()
        outline = border or base
        round_rect(self, 0.5, 0.5, self._cw - 0.5, self._ch - 0.5, self._cr,
                   fill=base, outline=outline, width=1)
        self.create_text(
            self._cw / 2 + self._pad, self._ch / 2, text=self._label_text,
            fill=fg, font=self._font, anchor="center",
        )

    # ------------------------------------------------------------ 事件
    def _on_enter(self, _e):
        if self._state == "normal":
            self._state = "hover"
            self._draw()

    def _on_leave(self, _e):
        if self._state in ("hover", "press"):
            self._state = "normal"
            self._draw()

    def _on_press(self, _e):
        if self._state == "disabled":
            return
        self._state = "press"
        self._draw()

    def _on_release(self, _e):
        if self._state == "disabled":
            return
        self._state = "hover"
        self._draw()
        if self._command:
            self._command()

    # ------------------------------------------------------------ 接口
    def set_text(self, text: str, icon: str = ""):
        self._label_text = f"{icon}  {text}" if icon else text
        self._draw()

    def set_kind(self, kind: str):
        self._kind = kind
        self._draw()

    def set_enabled(self, enabled: bool):
        self._state = "normal" if enabled else "disabled"
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def set_parent_bg(self, color: str):
        """父容器背景变化时同步按钮底色（用于列表行悬停高亮）。"""
        if color == self._parent_bg:
            return
        self._parent_bg = color
        try:
            self.configure(bg=color)
        except Exception:
            pass
        if self._kind == "ghost":
            self._draw()


class Pill(tk.Canvas):
    """圆角状态标签。"""

    def __init__(self, master, text: str, fg=T.SUCCESS, bg=T.SUCCESS_SOFT,
                 width=76, height=24, font=None, parent_bg=None):
        width, height = T.px(width), T.px(height)
        super().__init__(
            master, width=width, height=height,
            bg=parent_bg or master.cget("bg"), highlightthickness=0, bd=0,
        )
        self._cw, self._ch = width, height
        self._fg, self._bg = fg, bg
        self._font = font or (T.fonts()["family"], 9)
        self._pill_text = text
        self._draw()

    def _draw(self):
        self.delete("all")
        round_rect(self, 0.5, 0.5, self._cw - 0.5, self._ch - 0.5, self._ch / 2,
                   fill=self._bg, outline=self._bg)
        self.create_text(self._cw / 2, self._ch / 2, text=self._pill_text,
                         fill=self._fg, font=self._font)

    def update_pill(self, text: str, fg=None, bg=None):
        self._pill_text = text
        if fg:
            self._fg = fg
        if bg:
            self._bg = bg
        self._draw()

    def set_parent_bg(self, color: str):
        try:
            self.configure(bg=color)
        except Exception:
            pass


class LineEntry(tk.Frame):
    """1px 描边的输入框，带占位提示与聚焦高亮。"""

    def __init__(self, master, width=260, show=None, font=None,
                 placeholder="", bg=T.CARD):
        super().__init__(master, bg=T.BORDER_STRONG, padx=1, pady=1)
        self._font = font or (T.fonts()["family"], 11)
        self._bg = bg
        self.inner = tk.Frame(self, bg=bg)
        self.inner.pack(fill="both", expand=True)
        self.var = tk.StringVar()
        self.entry = tk.Entry(
            self.inner, textvariable=self.var, show=show, font=self._font,
            relief="flat", bd=0, highlightthickness=0, bg=bg, fg=T.TEXT,
            insertbackground=T.PRIMARY, width=1,
        )
        # padx/pady 必须走 T.px 缩放：字体点数已随 DPI 放大，若内边距仍是
        # 固定像素，高 DPI 下输入框会被文字挤满、矮一截（与按钮 36px 不齐）。
        pad_x, pad_y = T.px(10), T.px(8)
        self.entry.pack(fill="both", expand=True, padx=pad_x, pady=pad_y)
        # 关闭几何传播：宽度/高度由本控件显式决定，否则 1 字符的 Entry 会把
        # Frame 压缩到极窄（工具条里的搜索框就会显示不全）。
        try:
            natural_h = self.entry.winfo_reqheight() + 2 * pad_y
        except Exception:
            natural_h = 0
        self.configure(width=T.px(width), height=max(natural_h, T.px(34)))
        self.pack_propagate(False)
        self.entry.bind("<FocusIn>", self._on_focus_in)
        self.entry.bind("<FocusOut>", self._on_focus_out)
        self._placeholder = placeholder
        # 占位提示用一个覆盖在输入框上的 Label 实现：空且未聚焦时显示，
        # 不影响 self.var 的真实取值（get() 永远返回用户输入的内容）。
        self._ph = tk.Label(self.inner, text=placeholder, bg=bg, fg=T.MUTED,
                            font=self._font, anchor="w", cursor="xterm")
        if placeholder:
            self._ph.bind("<Button-1>", lambda e: self.entry.focus_set())
            self.var.trace_add("write", lambda *a: self._sync_placeholder())
            self._sync_placeholder()

    # ------------------------------------------------------------ 占位提示
    def _sync_placeholder(self):
        if not self._placeholder:
            return
        try:
            focused = self.entry.focus_get() is self.entry
        except Exception:
            focused = False
        if self.var.get() or focused:
            self._ph.place_forget()
        else:
            self._ph.place(x=T.px(10), y=0, relheight=1.0)

    def _on_focus_in(self, _e):
        try:
            self.configure(bg=T.PRIMARY)
        except Exception:
            pass
        self._sync_placeholder()

    def _on_focus_out(self, _e):
        try:
            self.configure(bg=T.BORDER_STRONG)
        except Exception:
            pass
        self._sync_placeholder()

    def get(self) -> str:
        return self.var.get()

    def set(self, value: str):
        self.var.set(value)

    def focus(self):
        self.entry.focus_set()

    def bind_return(self, func):
        self.entry.bind("<Return>", lambda e: func())


class ScrollFrame(tk.Frame):
    """带细滚动条的纵向滚动容器，内容放到 .body 里。"""

    def __init__(self, master, bg=T.CARD):
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.scroll = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.body = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        for widget in (self.canvas, self.body):
            widget.bind("<Enter>", self._bind_wheel)
            widget.bind("<Leave>", self._unbind_wheel)

    def _on_scroll(self, first, last):
        self.scroll.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.scroll.pack_forget()
        else:
            self.scroll.pack(side="right", fill="y")

    def _on_body_configure(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)

    def _bind_wheel(self, _e=None):
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        self.canvas.bind_all("<Button-4>", self._on_wheel)
        self.canvas.bind_all("<Button-5>", self._on_wheel)

    def _unbind_wheel(self, _e=None):
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")

    def _on_wheel(self, event):
        try:
            if getattr(event, "num", None) == 4:
                delta = -1
            elif getattr(event, "num", None) == 5:
                delta = 1
            else:
                delta = -1 if event.delta > 0 else 1
            self.canvas.yview_scroll(delta, "units")
        except Exception:
            pass

    def clear(self):
        for child in self.body.winfo_children():
            child.destroy()
        self.canvas.yview_moveto(0)


class CheckLine(tk.Frame):
    """自绘勾选框 + 说明文字。

    旧版未勾选态只有一层极浅的灰色描边，在白色卡片上几乎看不见，看起来像一块
    莫名其妙的灰色圆斑。这里改成：白底 + 明确的灰蓝描边 + 悬停高亮，勾选态用
    等比铺排的对勾，整体比例与文字行高对齐。
    """

    UNCHECKED_BORDER = "#98A2B3"

    def __init__(self, master, text: str, var: tk.BooleanVar, bg=T.CARD,
                 desc: str = "", command=None):
        super().__init__(master, bg=bg)
        self._var = var
        self._bg = bg
        self._command = command
        self._hover = False
        self._size = T.px(16)
        self._box = tk.Canvas(self, width=self._size, height=self._size, bg=bg,
                              highlightthickness=0, bd=0, cursor="hand2")
        self._box.pack(side="left", pady=(1, 0))
        self._label = tk.Label(self, text=text, bg=bg, fg=T.TEXT,
                               font=(T.fonts()["family"], 10), anchor="w",
                               cursor="hand2", justify="left")
        self._label.pack(side="left", anchor="n", padx=(9, 0))
        if desc:
            self._desc = tk.Label(self, text=desc, bg=bg, fg=T.MUTED,
                                  font=(T.fonts()["family"], 8), anchor="w",
                                  justify="left")
            self._desc.pack(side="left", anchor="n", padx=(8, 0), pady=(2, 0))
        self._draw()
        # 变量被外部改动时（例如代码里 var.set(...)）同步重绘
        try:
            self._var.trace_add("write", lambda *_a: self._draw())
        except Exception:
            pass
        for w in (self._box, self._label):
            w.bind("<Button-1>", self._toggle)
            w.bind("<Enter>", lambda _e: self._set_hover(True))
            w.bind("<Leave>", lambda _e: self._set_hover(False))

    # ------------------------------------------------------------ 绘制
    def _set_hover(self, hover: bool):
        if self._hover == hover:
            return
        self._hover = hover
        self._draw()

    def _draw(self):
        c = self._box
        c.delete("all")
        s = self._size
        # 留 1 设计像素边距，避免圆角被画布边界裁掉
        inset = max(1, T.px(1))
        radius = T.px(4)
        if bool(self._var.get()):
            round_rect(c, inset, inset, s - inset, s - inset, radius,
                       fill=T.PRIMARY, outline=T.PRIMARY)
            # 对勾按盒子尺寸等比铺排，视觉上居中且饱满
            c.create_line(s * 0.25, s * 0.53, s * 0.42, s * 0.70, s * 0.76, s * 0.30,
                          fill="#FFFFFF", width=max(2, T.px(1.7)),
                          capstyle="round", joinstyle="round")
        else:
            # 描边不能用 round_rect 的 outline 路径 —— 那是「直线段 + 圆弧」拼的，
            # 在这么小的尺寸下圆角会断裂、甚至出现缺口。改成画两层填充圆角矩形
            #（外层描边色 + 内层白底），圆角必然平滑，且与勾选态完全同构。
            border = T.PRIMARY if self._hover else self.UNCHECKED_BORDER
            thickness = max(1, T.px(1))
            round_rect(c, inset, inset, s - inset, s - inset, radius,
                       fill=border, outline=border)
            round_rect(c, inset + thickness, inset + thickness,
                       s - inset - thickness, s - inset - thickness,
                       max(1, radius - thickness),
                       fill="#FFFFFF", outline="#FFFFFF")

    # ------------------------------------------------------------ 交互
    def _toggle(self, _e=None):
        self._var.set(not self._var.get())
        self._draw()
        if self._command:
            self._command()

    def get(self) -> bool:
        return bool(self._var.get())

    def set(self, value: bool, run_command: bool = False):
        self._var.set(bool(value))
        self._draw()
        if run_command and self._command:
            self._command()


def hline(parent, color=T.BORDER, bg=None):
    # 高度随 DPI 缩放：4K@200% 下仍是设计稿里的 1px 观感（物理 2px）
    return tk.Frame(parent, bg=color, height=max(1, T.px(1)))
