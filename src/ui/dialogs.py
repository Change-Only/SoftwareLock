# -*- coding: utf-8 -*-
"""各类对话框：密码验证、设置密码、首次初始化、设置、通用确认。"""
from __future__ import annotations

import os
import subprocess
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import config_store
import winsys
from config_store import Store

from . import theme as T
from .widgets import CheckLine, LineEntry, Pill, RoundedButton, hline

_photo_cache: dict = {}


def app_photo(size: int = 32):
    key = ("app", size)
    if key not in _photo_cache:
        try:
            from PIL import ImageTk

            import artwork

            img = artwork.make_app_icon(size)
            _photo_cache[key] = ImageTk.PhotoImage(img) if img else None
        except Exception:
            _photo_cache[key] = None
    return _photo_cache[key]


def image_photo(path: str, name: str, size: int = 32):
    key = ("img", path, size)
    if key not in _photo_cache:
        try:
            from PIL import ImageTk

            import artwork

            img = artwork.app_avatar(path, name, size)
            _photo_cache[key] = ImageTk.PhotoImage(img) if img else None
        except Exception:
            _photo_cache[key] = None
    return _photo_cache[key]


class BaseDialog(tk.Toplevel):
    """统一风格的模态对话框基类。"""

    def __init__(self, master, title: str, width: int = 420, height: int = 260,
                 modal: bool = True, resizable: bool = False):
        super().__init__(master)
        self.withdraw()
        self.result = None
        self.title(title)
        self.configure(bg=T.CARD)
        self.resizable(resizable, resizable)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        photo = app_photo(32)
        if photo is not None:
            try:
                self.iconphoto(False, photo)
            except Exception:
                pass

        self.shell = tk.Frame(self, bg=T.CARD)
        self.shell.pack(fill="both", expand=True)
        # 底部按钮区最先占位：内容再高也不会把按钮挤出窗口外
        self._footer_host = tk.Frame(self.shell, bg=T.CARD)
        self._footer_host.pack(side="bottom", fill="x")
        self._fixed_w, self._fixed_h = width, height

        self.bind("<Escape>", lambda e: self.on_close())
        self._modal = modal
        self._parent = master if master is not None else tk._default_root
        self._initialized = False

    # -------------------------------------------------- 生命周期
    def _finish_init(self):
        if self._initialized:
            return
        self._initialized = True
        self.update_idletasks()
        self._center()
        if self._modal:
            try:
                # 父窗口隐藏（托盘模式）时不能设为 transient，否则对话框不会显示
                if self._parent is not None and self._parent.winfo_viewable():
                    self.transient(self._parent)
                self.grab_set()
            except Exception:
                pass
        try:
            self.attributes("-topmost", True)
            self.after(2200, self._release_top)
        except Exception:
            pass
        self.deiconify()
        self.lift()
        self.focus_force()
        winsys.message_beep()

    def _release_top(self):
        try:
            self.attributes("-topmost", False)
        except Exception:
            pass

    def _center(self):
        self.update_idletasks()
        w = T.px(self._fixed_w) if self._fixed_w else self.winfo_reqwidth()
        h = T.px(self._fixed_h) if self._fixed_h else self.winfo_reqheight()
        # 不同系统字体度量可能让实际内容高于设计值，以内容需要为准，绝不裁掉按钮
        req_w, req_h = self.winfo_reqwidth(), self.winfo_reqheight()
        w, h = max(w, req_w), max(h, req_h)
        self.geometry(f"{w}x{h}")
        parent = self._parent
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            visible = bool(parent.winfo_viewable())
        except Exception:
            px = py = 0
            pw = ph = 0
            visible = False
        if pw > 200 and visible:
            x = px + (pw - w) // 2
            y = py + (ph - h) // 3
        else:
            x = (self.winfo_screenwidth() - w) // 2
            y = (self.winfo_screenheight() - h) // 3
        x = max(0, min(x, self.winfo_screenwidth() - w))
        y = max(0, min(y, self.winfo_screenheight() - h))
        self.geometry(f"{w}x{h}+{x}+{y}")

    def on_close(self):
        self.result = None
        self._close()

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    def run(self):
        # 有些对话框在 __init__ 里就完成初始化，有些留到 run()，
        # 这里统一兜底，避免在隐藏窗口上 wait_window 而永久阻塞。
        self._finish_init()
        self.wait_window(self)
        return self.result

    # -------------------------------------------------- 通用构件
    def header(self, title: str, subtitle: str = "", danger: bool = False):
        box = tk.Frame(self.shell, bg=T.CARD)
        box.pack(fill="x", padx=T.px(24), pady=(T.px(22), 0))
        photo = app_photo(T.px(40))
        if photo is not None:
            lbl = tk.Label(box, image=photo, bg=T.CARD)
            lbl.image = photo
            lbl.pack(side="left", anchor="n", padx=(0, 14))
        inner = tk.Frame(box, bg=T.CARD)
        inner.pack(side="left", fill="x", expand=True)
        tk.Label(inner, text=title, bg=T.CARD,
                 fg=T.DANGER if danger else T.TEXT,
                 font=(T.fonts()["family"], 13, "bold"), anchor="w",
                 justify="left").pack(anchor="w")
        if subtitle:
            tk.Label(inner, text=subtitle, bg=T.CARD, fg=T.TEXT_SUB,
                     font=(T.fonts()["family"], 9), anchor="w", justify="left",
                     wraplength=T.px(self._fixed_w - 110) if self._fixed_w else 300,
                     ).pack(anchor="w", pady=(5, 0))
        return inner

    def footer(self, buttons, hint: str = "", hint_fg=T.MUTED):
        wrap = tk.Frame(self._footer_host, bg=T.CARD)
        wrap.pack(fill="x", padx=T.px(24), pady=(0, T.px(20)))
        hline(wrap).pack(fill="x", pady=(0, T.px(14)))
        if hint:
            self.hint_label = tk.Label(
                wrap, text=hint, bg=T.CARD, fg=hint_fg,
                font=(T.fonts()["family"], 8), anchor="w", justify="left",
                wraplength=T.px(max(120, (self._fixed_w or 420) - 48)),
            )
            self.hint_label.pack(fill="x", pady=(0, T.px(10)))
        bar = tk.Frame(wrap, bg=T.CARD)
        bar.pack(fill="x")
        inner = tk.Frame(bar, bg=T.CARD)
        inner.pack(side="right")
        for spec in buttons:
            text, command, kind, width = spec
            btn = RoundedButton(inner, text, command=command, kind=kind,
                                width=width, height=36, bg=T.CARD)
            btn.pack(side="left", padx=(T.px(8), 0))
        return bar


# ---------------------------------------------------------------------------
# 密码验证
# ---------------------------------------------------------------------------
class PasswordDialog(BaseDialog):
    """需要密码才能继续的验证框。"""

    def __init__(self, master, store: Store, title: str = "需要验证身份",
                 subtitle: str = "请输入软件锁密码", app_name: str = "",
                 confirm_text: str = "确认", timeout: int | None = None,
                 danger: bool = False,
                 hint: str = "连续输错会进入冷却；点击「取消」将拒绝本次启动并关闭该程序。"):
        super().__init__(master, f"软件锁 · {title}", width=430, height=320)
        self.store = store
        self.timeout = timeout
        self._t0 = 0.0

        body = self.header(title, subtitle, danger=danger)
        entry_wrap = tk.Frame(self.shell, bg=T.CARD)
        entry_wrap.pack(fill="x", padx=T.px(24), pady=(T.px(18), 0))
        self.entry = LineEntry(entry_wrap, width=340, show="●")
        self.entry.pack(fill="x")
        self.entry.bind_return(self._submit)

        # 「显示密码」开关：切换明/密文显示（不会改变实际输入的取值）
        self.show_var = tk.BooleanVar(value=False)
        CheckLine(entry_wrap, "显示密码", self.show_var, command=self._toggle_show,
                  bg=T.CARD).pack(anchor="w", pady=(T.px(10), 0))

        # 预留 2 行高度：出错/超时文案出现时不会把下方按钮挤出窗口
        self.msg = tk.Label(self.shell, text="", bg=T.CARD, fg=T.DANGER,
                            font=(T.fonts()["family"], 9), anchor="w", justify="left",
                            height=2, wraplength=T.px(382))
        self.msg.pack(fill="x", padx=T.px(24), pady=(8, 0))

        self.clock = tk.Label(self.shell, text="", bg=T.CARD, fg=T.MUTED,
                              font=(T.fonts()["family"], 8), anchor="w", height=1)
        self.clock.pack(fill="x", padx=T.px(24))

        self.footer(
            [("取消", self.on_close, "ghost", 84),
             (confirm_text, self._submit, "primary", 96)],
            hint=hint,
        )
        self._finish_init()
        self.entry.focus()

        if self.timeout:
            self._t0 = time.time()
            self._tick()

    # -------------------------------------------------- 逻辑
    def _toggle_show(self):
        char = "" if self.show_var.get() else "●"
        try:
            self.entry.entry.configure(show=char)
        except Exception:
            pass

    def _tick(self):
        if not self.winfo_exists():
            return
        remain = int(self.timeout - (time.time() - self._t0))
        if remain <= 0:
            self.msg.configure(text="等待超时，已拒绝本次启动。")
            self.result = False
            self.after(700, self._close)
            return
        if remain <= 30:
            self.clock.configure(text=f"等待响应：{remain} 秒后自动阻止", fg=T.DANGER)
        else:
            self.clock.configure(text=f"等待响应：{remain // 60} 分 {remain % 60} 秒后自动阻止")
        self.after(500, self._tick)

    def _submit(self):
        left = self.store.lockout_remaining()
        if left > 0:
            self.msg.configure(text=f"已进入冷却，请等待 {left} 秒后再试。")
            return
        value = self.entry.get()
        if not value:
            self.msg.configure(text="请输入密码。")
            return
        if self.store.check_password(value):
            self.store.reset_failures()
            self.result = True
            self._close()
            return
        self.entry.set("")
        cooldown = self.store.register_failure()
        if cooldown:
            self.msg.configure(text=f"密码错误。已锁定 {cooldown} 秒内不允许再试。")
        else:
            attempts = int(self.store.data.get("failed_attempts", 0) or 0)
            remain = max(0, int(self.store.settings.get("max_attempts", 5)) - attempts)
            self.msg.configure(text=f"密码错误，还可尝试 {remain} 次。")
        winsys.message_beep()


# ---------------------------------------------------------------------------
# 设置 / 修改密码
# ---------------------------------------------------------------------------
class SetPasswordDialog(BaseDialog):
    """设置或修改密码。"""

    def __init__(self, master, store: Store, change: bool = False):
        super().__init__(
            master,
            "软件锁 · 修改密码" if change else "软件锁 · 设置密码",
            width=450, height=430 if change else 380,
        )
        self.store = store
        self.change = change

        self.header(
            "修改密码" if change else "设置主密码",
            "该密码用于启动受保护应用以及退出软件锁，请务必牢记。"
            "密码以不可逆方式存储，忘记后需要手动删除配置文件才能重置。",
        )

        wrap = tk.Frame(self.shell, bg=T.CARD)
        wrap.pack(fill="x", padx=T.px(24), pady=(T.px(16), 0))

        if change:
            tk.Label(wrap, text="当前密码", bg=T.CARD, fg=T.TEXT_SUB,
                     font=(T.fonts()["family"], 9), anchor="w").pack(anchor="w")
            self.old = LineEntry(wrap, width=380, show="●")
            self.old.pack(fill="x", pady=(4, 12))

        tk.Label(wrap, text="新密码（至少 6 位）", bg=T.CARD, fg=T.TEXT_SUB,
                 font=(T.fonts()["family"], 9), anchor="w").pack(anchor="w")
        self.pwd = LineEntry(wrap, width=380, show="●")
        self.pwd.pack(fill="x", pady=(4, 12))

        tk.Label(wrap, text="确认新密码", bg=T.CARD, fg=T.TEXT_SUB,
                 font=(T.fonts()["family"], 9), anchor="w").pack(anchor="w")
        self.confirm = LineEntry(wrap, width=380, show="●")
        self.confirm.pack(fill="x", pady=(4, 0))

        self.show_var = tk.BooleanVar(value=False)
        CheckLine(wrap, "显示密码", self.show_var, command=self._toggle_show,
                  bg=T.CARD).pack(anchor="w", pady=(12, 0))

        self.msg = tk.Label(self.shell, text="", bg=T.CARD, fg=T.DANGER,
                            font=(T.fonts()["family"], 9), anchor="w", justify="left",
                            height=2, wraplength=T.px(390))
        self.msg.pack(fill="x", padx=T.px(24), pady=(8, 0))

        self.footer([("取消", self.on_close, "ghost", 84),
                     ("保存", self._submit, "primary", 96)])
        self._finish_init()
        (self.old if change else self.pwd).focus()
        for ent in (getattr(self, "old", None), self.pwd, self.confirm):
            if ent:
                ent.bind_return(self._submit)

    def _toggle_show(self):
        char = "" if self.show_var.get() else "●"
        for ent in (getattr(self, "old", None), self.pwd, self.confirm):
            if ent:
                ent.entry.configure(show=char)

    def _submit(self):
        if self.change:
            if not self.store.check_password(self.old.get()):
                self.msg.configure(text="当前密码不正确。")
                return
        new = self.pwd.get()
        if len(new) < 6:
            self.msg.configure(text="新密码至少需要 6 个字符。")
            return
        if new != self.confirm.get():
            self.msg.configure(text="两次输入的新密码不一致。")
            return
        self.store.set_password(new)
        self.result = True
        config_store.log("密码已更新")
        self._close()


# ---------------------------------------------------------------------------
# 首次初始化
# ---------------------------------------------------------------------------
class SetupDialog(BaseDialog):
    """首次运行：设置密码并选择是否开机自启。"""

    def __init__(self, master, store: Store):
        super().__init__(master, "软件锁 · 初始化", width=470, height=470)
        self.store = store
        self.header(
            "欢迎使用软件锁",
            "首次使用需要设置一个主密码。之后启动任何一个被锁定的程序，"
            "都必须先输入这个密码；退出软件锁同样需要它。",
        )

        wrap = tk.Frame(self.shell, bg=T.CARD)
        wrap.pack(fill="x", padx=T.px(24), pady=(T.px(16), 0))
        tk.Label(wrap, text="设置主密码（至少 6 位）", bg=T.CARD, fg=T.TEXT_SUB,
                 font=(T.fonts()["family"], 9), anchor="w").pack(anchor="w")
        self.pwd = LineEntry(wrap, width=400, show="●")
        self.pwd.pack(fill="x", pady=(4, 12))
        tk.Label(wrap, text="再次输入确认", bg=T.CARD, fg=T.TEXT_SUB,
                 font=(T.fonts()["family"], 9), anchor="w").pack(anchor="w")
        self.confirm = LineEntry(wrap, width=400, show="●")
        self.confirm.pack(fill="x", pady=(4, 14))

        self.auto_var = tk.BooleanVar(value=True)
        CheckLine(wrap, "开机自动启动软件锁（后台守护）", self.auto_var,
                  bg=T.CARD).pack(anchor="w")

        self.msg = tk.Label(self.shell, text="", bg=T.CARD, fg=T.DANGER,
                            font=(T.fonts()["family"], 9), anchor="w", justify="left",
                            height=2, wraplength=T.px(410))
        self.msg.pack(fill="x", padx=T.px(24), pady=(10, 0))

        self.footer([("退出程序", self._quit, "ghost", 92),
                     ("创建并开始守护", self._submit, "primary", 140)],
                    hint="密码遗失后将无法通过界面重置，请妥善保管。")
        self._finish_init()
        self.pwd.focus()
        self.pwd.bind_return(self._submit)
        self.confirm.bind_return(self._submit)

    def _quit(self):
        self.result = {"ok": False}
        self._close()

    def _submit(self):
        pwd = self.pwd.get()
        if len(pwd) < 6:
            self.msg.configure(text="密码至少需要 6 个字符。")
            return
        if pwd != self.confirm.get():
            self.msg.configure(text="两次输入的密码不一致。")
            return
        self.store.set_password(pwd)
        auto = bool(self.auto_var.get())
        self.store.update_settings(autostart=auto)
        winsys.set_autostart(auto)
        config_store.log("初始化完成，密码已设置")
        self.result = {"ok": True}
        self._close()


# ---------------------------------------------------------------------------
# 通用确认 / 选择
# ---------------------------------------------------------------------------
class ConfirmDialog(BaseDialog):
    def __init__(self, master, title: str, subtitle: str = "", width=420, height=270,
                 danger: bool = False):
        super().__init__(master, f"软件锁 · {title}", width=width, height=height)
        self._buttons: list = []
        self.header(title, subtitle, danger=danger)
        self.extra = tk.Frame(self.shell, bg=T.CARD)
        self.extra.pack(fill="both", expand=True, padx=T.px(24), pady=(T.px(14), 0))

    def set_buttons(self, specs):
        self.footer(specs)

    def show(self):
        return self.run()


class ChoiceDialog(ConfirmDialog):
    """带若干自定义按钮的确认框。choices 按「从左到右」排列，最后一个为主按钮。"""

    def __init__(self, master, title: str, subtitle: str, choices, width=470, height=300):
        super().__init__(master, title, subtitle, width=width, height=height)
        specs = []
        for text, value, kind, w in choices:
            specs.append((text, (lambda v=value: self._pick(v)), kind, w))
        self.set_buttons(specs)

    def _pick(self, value):
        self.result = value
        self._close()


class SettingsDialog(BaseDialog):
    """设置面板。"""

    def __init__(self, master, store: Store, interceptor=None, on_changed=None):
        super().__init__(master, "软件锁 · 设置", width=520, height=580)
        self.store = store
        self.interceptor = interceptor
        self.on_changed = on_changed

        self.header("设置", "调整后台守护方式与安全策略，修改后立即生效。")
        body = tk.Frame(self.shell, bg=T.CARD)
        body.pack(fill="both", expand=True, padx=T.px(24), pady=(T.px(18), 0))

        s = store.settings
        self.auto_var = tk.BooleanVar(value=bool(s.get("autostart")))
        self.guard_var = tk.BooleanVar(value=bool(s.get("self_defense")))
        self.clean_var = tk.BooleanVar(value=bool(s.get("lock_running_on_start")))
        self.admin_var = tk.BooleanVar(value=bool(s.get("require_admin", True)))
        self.relock_var = tk.BooleanVar(
            value=float(s.get("windowless_unlock_ttl", 3) or 0) <= 0.5)

        CheckLine(body, "开机自动启动", self.auto_var, command=self._on_auto,
                  desc="（登录后自动在后台守护）").pack(anchor="w", pady=(0, 12))
        CheckLine(body, "防止被强制结束", self.guard_var, command=self._on_guard,
                  desc="（在任务管理器中被结束后会自动重启）").pack(anchor="w", pady=(0, 12))
        CheckLine(body, "以管理员身份运行（推荐）", self.admin_var,
                  command=self._on_admin,
                  desc="（拦截管理员程序所必需；修改后需重启软件锁生效）"
                  ).pack(anchor="w", pady=(0, 12))
        CheckLine(body, "启动时清理已运行的受保护应用", self.clean_var,
                  command=self._on_clean,
                  desc="（避免先打开程序再开启软件锁）").pack(anchor="w", pady=(0, 12))
        CheckLine(body, "关闭后立即重新上锁", self.relock_var,
                  command=self._on_relock,
                  desc="（应用关闭后立刻恢复需要密码，不等后台注销计时）"
                  ).pack(anchor="w", pady=(0, 18))

        hline(body).pack(fill="x", pady=(0, 16))

        row = tk.Frame(body, bg=T.CARD)
        row.pack(fill="x", pady=(0, 12))
        tk.Label(row, text="后台扫描间隔", bg=T.CARD, fg=T.TEXT,
                 font=(T.fonts()["family"], 10)).pack(side="left")
        self.speed = tk.StringVar(value=self._speed_label(s.get("poll_interval_ms", 120)))
        import tkinter.ttk as ttk

        combo = ttk.Combobox(row, textvariable=self.speed, state="readonly",
                             values=["快速（60 毫秒，拦截更及时）",
                                     "标准（120 毫秒，推荐）",
                                     "省电（300 毫秒）"],
                             width=26, font=(T.fonts()["family"], 9))
        combo.pack(side="right")
        combo.bind("<<ComboboxSelected>>", self._on_speed)

        row2 = tk.Frame(body, bg=T.CARD)
        row2.pack(fill="x", pady=(0, 12))
        tk.Label(row2, text="连续输错锁定", bg=T.CARD, fg=T.TEXT,
                 font=(T.fonts()["family"], 10)).pack(side="left")
        self.cooldown = tk.StringVar(
            value=f"{s.get('max_attempts', 5)} 次 / 冷却 {s.get('cooldown_seconds', 30)} 秒"
        )
        combo2 = ttk.Combobox(
            row2, textvariable=self.cooldown, state="readonly", width=26,
            values=["3 次 / 冷却 30 秒", "5 次 / 冷却 30 秒", "5 次 / 冷却 60 秒",
                    "10 次 / 冷却 120 秒"],
            font=(T.fonts()["family"], 9),
        )
        combo2.pack(side="right")
        combo2.bind("<<ComboboxSelected>>", self._on_cooldown)

        row3 = tk.Frame(body, bg=T.CARD)
        row3.pack(fill="x", pady=(0, 12))
        tk.Label(row3, text="关窗后重新上锁", bg=T.CARD, fg=T.TEXT,
                 font=(T.fonts()["family"], 10)).pack(side="left")
        self.windowless = tk.StringVar(
            value=self._ttl_label(s.get("windowless_unlock_ttl", 3))
        )
        combo3 = ttk.Combobox(
            row3, textvariable=self.windowless, state="readonly", width=26,
            values=["立即（0 秒）", "3 秒（推荐）", "10 秒", "30 秒", "1 分钟"],
            font=(T.fonts()["family"], 9),
        )
        combo3.pack(side="right")
        combo3.bind("<<ComboboxSelected>>", self._on_windowless)

        row4 = tk.Frame(body, bg=T.CARD)
        row4.pack(fill="x", pady=(0, 12))
        tk.Label(row4, text="密码", bg=T.CARD, fg=T.TEXT,
                 font=(T.fonts()["family"], 10)).pack(side="left")
        RoundedButton(row4, "修改密码", command=self._change_pwd, kind="soft",
                      width=100, height=32, bg=T.CARD).pack(side="right")

        row5 = tk.Frame(body, bg=T.CARD)
        row5.pack(fill="x", pady=(0, 8))
        tk.Label(row5, text="数据目录", bg=T.CARD, fg=T.TEXT,
                 font=(T.fonts()["family"], 10)).pack(side="left")
        RoundedButton(row5, "打开", command=self._open_dir, kind="ghost",
                      width=70, height=32, bg=T.CARD).pack(side="right")

        row6 = tk.Frame(body, bg=T.CARD)
        row6.pack(fill="x", pady=(0, 8))
        tk.Label(row6, text="杀毒软件误报", bg=T.CARD, fg=T.TEXT,
                 font=(T.fonts()["family"], 10)).pack(side="left")
        RoundedButton(row6, "加入白名单", command=self._add_exclusion, kind="soft",
                      width=112, height=32, bg=T.CARD).pack(side="right")

        self.tip = tk.Label(
            body,
            text="提示：软件锁默认以管理员身份启动，这样才能拦截同样以管理员身份运行的"
                 "程序（如任务管理器）。若 UAC 被拒绝，将回退为普通权限运行。",
            bg=T.CARD, fg=T.MUTED, font=(T.fonts()["family"], 8),
            justify="left", wraplength=T.px(450), anchor="w",
        )
        self.tip.pack(fill="x", pady=(T.px(10), 0))

        self.footer([("关闭", self._done, "primary", 96)])

    # -------------------------------------------------- 回调
    def _speed_label(self, ms):
        ms = int(ms or 120)
        if ms <= 80:
            return "快速（60 毫秒，拦截更及时）"
        if ms >= 250:
            return "省电（300 毫秒）"
        return "标准（120 毫秒，推荐）"

    def _on_auto(self):
        value = bool(self.auto_var.get())
        ok = winsys.set_autostart(value)
        self.store.update_settings(autostart=value)
        if not ok:
            self.tip.configure(text="写入开机启动项失败，请检查系统权限。", fg=T.DANGER)
        if self.on_changed:
            self.on_changed()

    def _on_guard(self):
        self.store.update_settings(self_defense=bool(self.guard_var.get()))
        if self.on_changed:
            self.on_changed()

    def _on_admin(self):
        self.store.update_settings(require_admin=bool(self.admin_var.get()))

    def _on_clean(self):
        self.store.update_settings(lock_running_on_start=bool(self.clean_var.get()))

    def _on_relock(self):
        """勾选 -> 关窗立即上锁（0.5s）；取消 -> 回到 3 秒宽限。"""
        if bool(self.relock_var.get()):
            sec = 0.5
        else:
            sec = 3
        self.store.update_settings(windowless_unlock_ttl=sec)
        self.windowless.set(self._ttl_label(sec))

    def _on_speed(self, _e=None):
        label = self.speed.get()
        ms = 60 if label.startswith("快速") else 300 if label.startswith("省电") else 120
        self.store.update_settings(poll_interval_ms=ms)

    def _ttl_label(self, seconds) -> str:
        try:
            sec = float(seconds)
        except Exception:
            sec = 3.0
        if sec <= 0.5:
            return "立即（0 秒）"
        if sec <= 5:
            return "3 秒（推荐）"
        if sec <= 20:
            return "10 秒"
        if sec <= 45:
            return "30 秒"
        return "1 分钟"

    def _on_windowless(self, _e=None):
        label = self.windowless.get()
        if label.startswith("立即"):
            sec = 0.5
        elif label.startswith("10"):
            sec = 10
        elif label.startswith("30"):
            sec = 30
        elif label.startswith("1 "):
            sec = 60
        else:
            sec = 3
        self.store.update_settings(windowless_unlock_ttl=sec)
        self.windowless.set(self._ttl_label(sec))
        self.relock_var.set(sec <= 0.5)

    def _on_cooldown(self, _e=None):
        label = self.cooldown.get()
        try:
            attempts = int(label.split("次")[0].strip())
            cooldown = int(label.split("冷却")[1].replace("秒", "").strip())
        except Exception:
            attempts, cooldown = 5, 30
        self.store.update_settings(max_attempts=attempts, cooldown_seconds=cooldown)

    def _change_pwd(self):
        SetPasswordDialog(self, self.store, change=True).run()

    def _open_dir(self):
        config_store.ensure_dirs()
        try:
            os.startfile(str(config_store.APP_DIR))  # noqa: S606
        except Exception:
            subprocess.Popen(["explorer", str(config_store.APP_DIR)])

    def _add_exclusion(self):
        """把软件锁本体与数据目录加入 Windows Defender 排除项。"""
        self.tip.configure(text="正在加入 Windows Defender 白名单…", fg=T.MUTED)
        try:
            self.update_idletasks()
        except Exception:
            pass
        paths = [winsys.current_exe(), str(config_store.APP_DIR)]
        ok, message = winsys.add_defender_exclusions(paths)
        if ok:
            self.tip.configure(
                text="已把软件锁与数据目录加入 Windows Defender 白名单，"
                     "实时防护不会再把它当成威胁清理。",
                fg=T.SUCCESS,
            )
            return
        self.tip.configure(
            text=f"加入白名单失败：{message}。可在「Windows 安全中心 → 病毒和威胁防护 → "
                 f"排除项 → 添加排除项」中手动加入：{config_store.APP_DIR}",
            fg=T.DANGER,
        )

    def _done(self):
        self.result = True
        if self.on_changed:
            self.on_changed()
        self._close()
