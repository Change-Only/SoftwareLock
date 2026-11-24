# -*- coding: utf-8 -*-
"""软件锁主窗口。"""
from __future__ import annotations

import os
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox

import config_store
import winsys
from config_store import Store

from . import theme as T
from .dialogs import SettingsDialog, app_photo, image_photo
from .widgets import Pill, RoundedButton, ScrollFrame, hline

# 轻提示 Toast 的存活时长（毫秒）
TOAST_MS = 2400

# 事件驱动的列表刷新合并窗口（毫秒）：避免放行/关闭时高并发事件把刷新打爆
REFRESH_COALESCE_MS = 350


def _recolor(widget, color: str):
    """递归刷新控件背景色（列表行悬停高亮用）。"""
    for child in widget.winfo_children():
        if hasattr(child, "set_parent_bg"):
            child.set_parent_bg(color)
        else:
            try:
                child.configure(bg=color)
            except Exception:
                pass
        _recolor(child, color)


class MainWindow:
    def __init__(self, root: tk.Tk, store: Store, interceptor, broker, tray, on_quit):
        self.root = root
        self.store = store
        self.interceptor = interceptor
        self.broker = broker
        self.tray = tray
        self.on_quit = on_quit
        self._row_widgets: list = []
        self._last_event_text = "暂无活动"
        self._authenticated = False
        self._toast = None
        self._refresh_job = None
        # 拖拽排序状态：拖拽期间禁止重建列表（重建会销毁被拖动的行，隐式抓取失效）
        self._drag_row = None
        self._drag_app = None
        self._drag_moved = False
        self._refresh_after_drag = False

        self._build()

    # ================================================================= 构建
    def _build(self):
        root = self.root
        root.title("软件锁")
        # 首次显示时居中到屏幕；窗口不超过屏幕可用区域（保险丝：
        # 极小分辨率 + 高缩放的自适应系数触底后仍可能偏大）
        win_w, win_h = T.px(780), T.px(600)
        scr_w, scr_h = root.winfo_screenwidth(), root.winfo_screenheight()
        win_w = min(win_w, max(T.px(560), int(scr_w * 0.94)))
        win_h = min(win_h, max(T.px(460), int(scr_h * 0.9)))
        root.minsize(min(T.px(720), win_w), min(T.px(520), win_h))
        pos_x = max(0, (scr_w - win_w) // 2)
        pos_y = max(0, (scr_h - win_h) // 3)
        root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        photo = app_photo(T.px(32))
        if photo is not None:
            try:
                root.iconphoto(True, photo)
            except Exception:
                pass
        root.configure(bg=T.BG)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_header()
        self._build_admin_bar()
        self._build_toolbar()
        self._build_list()
        self._build_footer()
        self._bind_shortcuts()
        self.refresh()

    # -------------------------------------------------------------- 顶部
    def _build_header(self):
        head = tk.Frame(self.root, bg=T.CARD)
        head.pack(fill="x")
        inner = tk.Frame(head, bg=T.CARD)
        inner.pack(fill="x", padx=T.px(24), pady=T.px(18))

        photo = app_photo(T.px(44))
        if photo is not None:
            lbl = tk.Label(inner, image=photo, bg=T.CARD)
            lbl.image = photo
            lbl.pack(side="left", padx=(0, 14))
        text = tk.Frame(inner, bg=T.CARD)
        text.pack(side="left")
        tk.Label(text, text="软件锁", bg=T.CARD, fg=T.TEXT,
                 font=(T.fonts()["family"], 15, "bold")).pack(anchor="w")
        self.sub_label = tk.Label(text, text="后台守护运行中", bg=T.CARD, fg=T.MUTED,
                                  font=(T.fonts()["family"], 9))
        self.sub_label.pack(anchor="w", pady=(3, 0))

        # 已提权时在状态胶囊旁显示绿色「管理员」徽标（最右侧）
        if winsys.is_elevated():
            Pill(inner, "管理员", fg=T.SUCCESS, bg=T.SUCCESS_SOFT,
                 width=66, height=26, parent_bg=T.CARD).pack(side="right")
        self.status_pill = Pill(inner, "防护中", fg=T.SUCCESS, bg=T.SUCCESS_SOFT,
                                width=86, height=26, parent_bg=T.CARD)
        self.status_pill.pack(side="right", padx=(0, 8))
        hline(self.root, T.BORDER).pack(fill="x")

    # -------------------------------------------------------------- 管理员提示条
    def _build_admin_bar(self):
        self._admin_bar = None
        if winsys.is_elevated():
            return  # 已提权：不显示提示条
        bar = tk.Frame(self.root, bg=T.WARN_SOFT)
        bar.pack(fill="x")
        inner = tk.Frame(bar, bg=T.WARN_SOFT)
        inner.pack(fill="x", padx=T.px(24), pady=T.px(8))
        tk.Label(inner, text="⚠ 当前以普通权限运行，无法拦截以管理员身份启动的程序",
                 bg=T.WARN_SOFT, fg=T.WARN,
                 font=(T.fonts()["family"], 9)).pack(side="left")
        RoundedButton(inner, "以管理员身份重启", command=self.relaunch_admin,
                      kind="soft", width=152, height=30,
                      bg=T.WARN_SOFT).pack(side="right")
        self._admin_bar = bar

    # -------------------------------------------------------------- 工具条
    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg=T.BG)
        bar.pack(fill="x", padx=T.px(24), pady=(T.px(16), T.px(12)))

        RoundedButton(bar, "添加应用", icon="＋", command=self.add_app, kind="primary",
                      width=126, height=36, bg=T.BG).pack(side="left")
        RoundedButton(bar, "关闭全部应用", command=self.lock_all, kind="ghost",
                      width=140, height=36, bg=T.BG).pack(side="left", padx=(10, 0))
        RoundedButton(bar, "刷新状态", command=self.check_status, kind="ghost",
                      width=100, height=36, bg=T.BG).pack(side="left", padx=(10, 0))
        RoundedButton(bar, "设置", command=self.open_settings, kind="ghost",
                      width=84, height=36, bg=T.BG).pack(side="left", padx=(10, 0))

        RoundedButton(bar, "隐藏到托盘", command=self.hide, kind="ghost",
                      width=112, height=36, bg=T.BG).pack(side="right")

    # -------------------------------------------------------------- 列表
    def _build_list(self):
        wrap = tk.Frame(self.root, bg=T.BORDER, padx=1, pady=1)
        wrap.pack(fill="both", expand=True, padx=T.px(24))
        self.list_card = tk.Frame(wrap, bg=T.CARD)
        self.list_card.pack(fill="both", expand=True)
        self.scroll = ScrollFrame(self.list_card, bg=T.CARD)
        self.scroll.pack(fill="both", expand=True)

    # -------------------------------------------------------------- 底部
    def _build_footer(self):
        foot = tk.Frame(self.root, bg=T.BG)
        foot.pack(fill="x", padx=T.px(24), pady=(T.px(12), T.px(16)))
        self.stats_label = tk.Label(foot, text="", bg=T.BG, fg=T.TEXT_SUB,
                                    font=(T.fonts()["family"], 9), anchor="w")
        self.stats_label.pack(anchor="w")
        self.event_label = tk.Label(foot, text="", bg=T.BG, fg=T.MUTED,
                                    font=(T.fonts()["family"], 8), anchor="w")
        self.event_label.pack(anchor="w", pady=(3, 0))

    # -------------------------------------------------------------- 快捷键
    def _bind_shortcuts(self):
        self.root.bind("<Control-a>", self._on_add_shortcut)
        self.root.bind("<Control-A>", self._on_add_shortcut)
        self.root.bind("<F5>", lambda e: (self.check_status(), "break")[1])
        self.root.bind("<Escape>", self._on_escape)

    def _on_add_shortcut(self, _e=None):
        self.add_app()
        return "break"

    def _on_escape(self, _e=None):
        self.hide()
        return "break"

    # ================================================================= 刷新
    def refresh_later(self, delay: int = REFRESH_COALESCE_MS):
        """把多次刷新请求合并成一次（放行/关闭会连续触发多条事件）。"""
        if self._refresh_job is not None:
            return
        try:
            self._refresh_job = self.root.after(delay, self._refresh_now)
        except Exception:
            self._refresh_job = None

    def _refresh_now(self):
        self._refresh_job = None
        self.refresh()

    def _running_paths_now(self) -> set:
        """当前「正在运行」的受保护应用路径集合（已归一化）。

        账本命中零开销；账本没命中的才做一次**批量**进程枚举兜底，所以即使
        放行账本已被引擎注销，界面也能立刻反映真实进程状态。只统计启用状态
        的应用，与行按钮（启动 / 关闭）的语义保持一致。
        """
        if not self.interceptor:
            return set()
        keys: dict[str, str] = {}
        for app in self.store.apps:
            if not app.get("enabled", True):
                continue
            key = config_store.norm_path(app.get("path", ""))
            if key:
                keys[key] = app.get("path", "")
        if not keys:
            return set()
        try:
            unlocked = self.interceptor.unlocked_paths()
        except Exception:
            unlocked = set()
        running = {k for k in keys if k in unlocked}
        pending = [keys[k] for k in keys if k not in unlocked]
        if pending:
            try:
                found = self.interceptor.running_map(pending)
            except AttributeError:      # 旧版注入桩
                found = {}
                for p in pending:
                    try:
                        if self.interceptor.is_running(p):
                            found[config_store.norm_path(p)] = [1]
                    except Exception:
                        continue
            running |= {k for k, pids in found.items() if pids}
        return running

    def refresh(self, running_paths=None):
        if not self.root.winfo_exists():
            return
        if self._drag_row is not None:
            # 拖拽过程中重建列表会销毁正在拖动的行，隐式抓取随之中断；
            # 记下来，等松手后再补一次刷新。
            self._refresh_after_drag = True
            return
        self._refresh_after_drag = False
        self.scroll.clear()
        self._row_widgets = []
        if running_paths is None:
            running_paths = self._running_paths_now()
        apps = list(self.store.apps)
        if not apps:
            self._build_empty_state()
        else:
            total = len(apps)
            for index, app in enumerate(apps):
                key = config_store.norm_path(app.get("path", ""))
                self._row_widgets.append(
                    self._build_row(app, index, total, running=key in running_paths))

        stats = self.store.stats()
        protected = len(self.store.enabled_apps())
        running = len(running_paths)
        self.stats_label.configure(
            text=f"已保护 {protected} 个应用 · 已拦截启动 {stats.get('blocked', 0)} 次 "
                 f"· 已放行 {stats.get('allowed', 0)} 次 · 运行中 {running}"
        )
        self.event_label.configure(text=f"最近活动：{self._last_event_text}")
        hint = "  ·  拖动行首圆点可排序" if len(apps) > 1 else ""
        self.sub_label.configure(
            text=f"后台守护运行中 · 扫描间隔 "
                 f"{self.store.settings.get('poll_interval_ms', 120)} 毫秒{hint}"
        )
        if running:
            self.status_pill.update_pill(f"已解锁 {running}", fg=T.WARN, bg=T.WARN_SOFT)
        else:
            self.status_pill.update_pill("防护中", fg=T.SUCCESS, bg=T.SUCCESS_SOFT)
        if self.tray:
            self.tray.set_title(
                f"软件锁 · 已保护 {protected} 个应用 · 运行中 {running}"
            )

    # ------------------------------------------------------- 手动状态检查
    def check_status(self):
        """重新检查全部已添加应用的运行状态，并刷新列表（工具条「刷新状态」/ F5）。

        比平时的自动刷新多做两件事：
        1. 不做「账本优先」短路，直接按 exe 路径全量枚举一遍，拿到权威结果；
        2. 顺手清理「进程已经全部退出、放行账本却还留着」的残留会话——
           否则这些应用会一直被界面显示成运行中。
        """
        apps = list(self.store.apps)
        paths = [a.get("path", "") for a in apps if a.get("path")]
        running: set = set()
        if self.interceptor and paths:
            try:
                found = self.interceptor.running_map(paths)
            except AttributeError:      # 旧版注入桩
                found = {}
                for p in paths:
                    try:
                        if self.interceptor.is_running(p):
                            found[config_store.norm_path(p)] = [1]
                    except Exception:
                        continue
            running = {k for k, pids in found.items() if pids}

            try:
                leftover = self.interceptor.unlocked_paths()
            except Exception:
                leftover = set()
            stale = 0
            for key in leftover:
                if key in running:
                    continue
                try:
                    if self.interceptor.startup_pending(key):
                        continue        # 刚点过「启动」，进程可能还在创建中
                except AttributeError:
                    pass
                try:
                    self.interceptor.forget(key)
                    stale += 1
                except Exception:
                    continue
            if stale:
                config_store.log(f"状态检查：清理 {stale} 条已退出的放行残留")

        self.refresh(running_paths=running)

        enabled_keys = set()
        for app in apps:
            if app.get("enabled", True):
                key = config_store.norm_path(app.get("path", ""))
                if key:
                    enabled_keys.add(key)
        running_now = len(running & enabled_keys)
        total = len(apps)
        if not total:
            self.toast("还没有添加任何应用", kind="info")
        elif running_now:
            self.toast(f"状态已更新 · {running_now} 个应用正在运行", kind="info")
        else:
            self.toast(f"状态已更新 · {total} 个应用均未运行", kind="info")
        self.push_event_text(f"已检查应用状态（运行中 {running_now} / 共 {total}）")
        return running_now

    def _build_empty_state(self):
        box = tk.Frame(self.scroll.body, bg=T.CARD)
        box.pack(fill="both", expand=True, pady=T.px(60))
        tk.Label(box, text="还没有受保护的应用", bg=T.CARD, fg=T.TEXT,
                 font=(T.fonts()["family"], 12, "bold")).pack()
        tk.Label(
            box,
            text="添加后，从软件锁里启动无需密码；在其他任何地方打开都必须先输入密码。",
            bg=T.CARD, fg=T.MUTED, font=(T.fonts()["family"], 9), justify="center",
        ).pack(pady=(10, 0))
        RoundedButton(box, "添加第一个应用", icon="＋", command=self.add_app,
                      kind="primary", width=168, height=40,
                      bg=T.CARD).pack(pady=(18, 0))

    def _build_row(self, app: dict, index: int, total: int, running: bool = False):
        row = tk.Frame(self.scroll.body, bg=T.CARD)
        row.pack(fill="x")
        inner = tk.Frame(row, bg=T.CARD)
        inner.pack(fill="x", padx=T.px(18), pady=T.px(11))

        # 行首拖拽手柄（六个小圆点）：只在手柄上起拖，避免和双击启动、
        # 行内按钮抢事件。
        grip = self._make_grip(inner)
        grip.pack(side="left", padx=(0, T.px(8)))

        def _on_grip_press(_e=None, a=app, r=row):
            self._drag_start(_e, a, r)

        grip.bind("<Button-1>", _on_grip_press)
        grip.bind("<B1-Motion>", self._drag_motion)
        grip.bind("<ButtonRelease-1>", self._drag_end)

        photo = image_photo(app.get("path", ""), app.get("name", ""), T.px(34))
        if photo is not None:
            icon = tk.Label(inner, image=photo, bg=T.CARD)
            icon.image = photo
            icon.pack(side="left", padx=(0, 12))

        text = tk.Frame(inner, bg=T.CARD)
        text.pack(side="left", fill="x", expand=True)
        name_row = tk.Frame(text, bg=T.CARD)
        name_row.pack(fill="x", anchor="w")
        tk.Label(name_row, text=app.get("name") or "未命名", bg=T.CARD, fg=T.TEXT,
                 font=(T.fonts()["family"], 10, "bold")).pack(side="left")
        unlocked = bool(running)
        if unlocked:
            Pill(name_row, "运行中", fg=T.WARN, bg=T.WARN_SOFT, width=58, height=20,
                 parent_bg=T.CARD).pack(side="left", padx=(8, 0))
        path_text = app.get("path", "")
        path_lbl = tk.Label(text, text=self._ellipsis(path_text, 62), bg=T.CARD, fg=T.MUTED,
                            font=(T.fonts()["family"], 8))
        path_lbl.pack(anchor="w", pady=(3, 0))

        actions = tk.Frame(inner, bg=T.CARD)
        actions.pack(side="right")

        enabled = bool(app.get("enabled", True))
        toggle = Pill(actions, "已启用" if enabled else "已停用",
                      fg=T.SUCCESS if enabled else T.MUTED,
                      bg=T.SUCCESS_SOFT if enabled else "#EFF2F6",
                      width=66, height=26, parent_bg=T.CARD)
        toggle.pack(side="left", padx=(0, 10))
        toggle.configure(cursor="hand2")
        def _on_toggle_click(_e=None, a=app):
            """点击「已启用 / 已停用」标签切换该应用的保护开关。

            允许无 event 调用（tk 在少数合成/销毁路径下会 0 参回调），
            否则会抛 TypeError 让这次切换静默失败。
            """
            self.toggle_app(a)

        toggle.bind("<Button-1>", _on_toggle_click)

        # 运行中 -> 「关闭」（结束进程、恢复保护）；未运行 -> 「启动」（免密放行）
        if unlocked:
            RoundedButton(actions, "关闭", command=lambda a=app: self.close_app(a),
                          kind="danger", width=76, height=32,
                          bg=T.CARD).pack(side="left")
        else:
            RoundedButton(actions, "启动", command=lambda a=app: self.launch_app(a),
                          kind="soft", width=76, height=32, bg=T.CARD).pack(side="left")
        RoundedButton(actions, "移除", command=lambda a=app: self.remove_app(a),
                      kind="ghost", width=76, height=32, bg=T.CARD).pack(side="left", padx=(8, 0))

        # 分隔线始终创建、只有非末行才 pack：拖拽换位时靠它保证分隔线永远只
        # 出现在「行与行之间」，不会跟着某行跑到末尾，也不会让中间断开。
        sep = hline(row, T.BORDER)
        if index < total - 1:
            sep.pack(fill="x", padx=T.px(18))
        row._sep = sep
        row._app_id = app.get("id")

        # 悬停高亮
        widgets = [row, inner, grip, text, name_row, path_lbl, actions]

        def on_enter(_e, ws=widgets):
            if self._drag_row is not None:
                return          # 拖拽中不抢被拖动行的高亮
            for w in ws:
                try:
                    w.configure(bg=T.CARD_ALT)
                except Exception:
                    pass
            _recolor(row, T.CARD_ALT)

        def on_leave(_e, ws=widgets):
            if self._drag_row is not None:
                return
            for w in ws:
                try:
                    w.configure(bg=T.CARD)
                except Exception:
                    pass
            _recolor(row, T.CARD)

        for w in widgets:
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)
            # 双击：未运行 -> 启动；已在运行 -> 不重复拉起（把窗口带回前台）
            w.bind("<Double-Button-1>", lambda _e=None, a=app: self.row_double_click(a))
            w.bind("<Button-3>", lambda e, a=app: self._show_row_menu(e, a))
        return row

    # ------------------------------------------------------- 拖拽排序
    def _make_grip(self, parent):
        """行首拖拽手柄：两列三行小圆点（不依赖字体里是否有 ⠿ 这类字符）。"""
        w, h = T.px(9), T.px(18)
        c = tk.Canvas(parent, width=w, height=h, bg=T.CARD, highlightthickness=0,
                      bd=0, cursor="fleur", takefocus=0)
        r = max(1, int(round(T.px(1.2))))
        for cx in (0.28, 0.72):
            for cy in (0.2, 0.5, 0.8):
                c.create_oval(w * cx - r, h * cy - r, w * cx + r, h * cy + r,
                              fill=T.MUTED, outline=T.MUTED)
        return c

    def _drag_start(self, _event, app, row):
        if self._drag_row is not None or len(self._row_widgets) < 2:
            return
        self._drag_row = row
        self._drag_app = app
        self._drag_moved = False
        try:
            _recolor(row, T.PRIMARY_SOFT)   # 被拖动行淡蓝底，明确「正在搬这一行」
        except Exception:
            pass

    def _drag_motion(self, event=None):
        """拖动中直接换位：只调整 pack 顺序，不重建控件。

        B1-Motion 由隐式抓取送给按下时的那个手柄，所以行被移动后事件仍然
        连续进来；一旦重建列表（refresh）抓取就会丢失，故 refresh 在拖拽期间
        只能延后。
        """
        row = self._drag_row
        if row is None or event is None:
            return
        try:
            if not row.winfo_exists():
                return
            y = event.y_root - self.scroll.body.winfo_rooty()
        except Exception:
            return
        rows = self._row_widgets
        if row not in rows:
            return

        target = len(rows) - 1
        for i, r in enumerate(rows):
            try:
                if y < r.winfo_y() + r.winfo_height() // 2:
                    target = i
                    break
            except Exception:
                continue
        cur = rows.index(row)
        if target == cur:
            return
        try:
            if target > cur:
                row.pack_configure(after=rows[target])
            else:
                row.pack_configure(before=rows[target])
        except Exception:
            return
        rows.remove(row)
        rows.insert(target, row)
        self._drag_moved = True
        self._sync_separators()
        try:
            self.scroll.canvas.configure(scrollregion=self.scroll.canvas.bbox("all"))
        except Exception:
            pass

    def _sync_separators(self):
        """按当前视觉顺序重排分隔线：最后一行不带分隔线，其余都带。"""
        rows = self._row_widgets
        total = len(rows)
        for i, r in enumerate(rows):
            sep = getattr(r, "_sep", None)
            if sep is None:
                continue
            try:
                if i < total - 1:
                    sep.pack(fill="x", padx=T.px(18))
                else:
                    sep.pack_forget()
            except Exception:
                pass

    def _drag_end(self, _event=None):
        row, app = self._drag_row, self._drag_app
        self._drag_row = None
        self._drag_app = None
        if row is None:
            return
        moved, self._drag_moved = self._drag_moved, False
        try:
            if row.winfo_exists():
                _recolor(row, T.CARD)
        except Exception:
            pass
        if moved:
            ids = [getattr(r, "_app_id", "") for r in self._row_widgets]
            try:
                ok = self.store.reorder_apps(ids)
            except Exception:
                ok = False
            if not ok:
                self.toast("顺序未能保存，已恢复原顺序", kind="warn")
                self.refresh()
                return
            config_store.log(f"已调整应用顺序: {(app or {}).get('name', '')}")
            self.push_event_text("已调整应用顺序")
            self.toast("顺序已保存", kind="success")
        if self._refresh_after_drag:
            self._refresh_after_drag = False
            self.refresh()

    def _show_row_menu(self, event, app: dict):
        menu = tk.Menu(self.root, tearoff=0, bg=T.CARD, fg=T.TEXT,
                       activebackground=T.PRIMARY_SOFT, activeforeground=T.PRIMARY,
                       bd=0, relief="flat")
        running = self._is_running(app)
        if running:
            menu.add_command(label="关闭", command=lambda a=app: self.close_app(a))
        else:
            menu.add_command(label="启动", command=lambda a=app: self.launch_app(a))
        menu.add_command(label="打开所在文件夹",
                         command=lambda a=app: self._open_folder(a))
        menu.add_separator()
        menu.add_command(label="移除", command=lambda a=app: self.remove_app(a))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    @staticmethod
    def _open_folder(app: dict):
        path = app.get("path", "")
        try:
            subprocess.Popen(["explorer", "/select,", path])
        except Exception:
            pass

    @staticmethod
    def _ellipsis(text: str, limit: int) -> str:
        text = text or ""
        return text if len(text) <= limit else "…" + text[-(limit - 1):]

    # ================================================================= 轻提示
    def toast(self, text: str, kind: str = "info"):
        """在窗口底部浮出圆角轻提示，2.4 秒后自动消失。"""
        palette = {
            "info": (T.TEXT, T.CARD_ALT, T.BORDER_STRONG),
            "success": (T.SUCCESS, T.SUCCESS_SOFT, T.SUCCESS),
            "warn": (T.WARN, T.WARN_SOFT, T.WARN),
            "error": (T.DANGER, T.DANGER_SOFT, T.DANGER),
        }
        fg, bg, border = palette.get(kind, palette["info"])
        try:
            if self._toast is not None:
                try:
                    self._toast.destroy()
                except Exception:
                    pass
                self._toast = None
            holder = tk.Frame(self.root, bg=border, padx=1, pady=1)
            tk.Label(holder, text=text, bg=bg, fg=fg,
                     font=(T.fonts()["family"], 9),
                     padx=T.px(16), pady=T.px(9)).pack()
            holder.place(relx=0.5, rely=1.0, anchor="s", y=-T.px(66))
            self._toast = holder
            holder.after(TOAST_MS, lambda h=holder: self._hide_toast(h))
        except Exception:
            pass

    def _hide_toast(self, holder):
        try:
            holder.place_forget()
            holder.destroy()
        except Exception:
            pass
        if self._toast is holder:
            self._toast = None

    # ================================================================= 动作
    def add_app(self):
        path = filedialog.askopenfilename(
            parent=self.root,
            title="选择要加锁的程序",
            filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")],
        )
        if not path:
            return
        app = self.store.add_app(path)
        if not app:
            messagebox.showerror("添加失败", "无法添加该文件。", parent=self.root)
            return
        config_store.log(f"已添加受保护应用: {app.get('name')} ({path})")
        self.refresh()
        if self.interceptor:
            self.interceptor.forget(path)  # 新增后首次启动必须验证
        self._ask_kill_running(app)

    def _ask_kill_running(self, app: dict):
        """刚添加的程序如果已经在运行，立即结束，保证加锁生效。"""
        import psutil

        target = config_store.norm_path(app.get("path", ""))
        pids = []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if (proc.info.get("name") or "").lower() != os.path.basename(target):
                    continue
            except Exception:
                continue
            exe = winsys.process_exe(proc.pid)
            if exe and config_store.norm_path(exe) == target:
                pids.append(proc.pid)
        if not pids:
            return
        total = sum(winsys.terminate_tree(pid) for pid in pids)
        config_store.log(f"添加后立即锁定 {app.get('name')}，结束 {total} 个进程")
        self.store.bump_stat("blocked", 1)
        self.push_event_text(f"已锁定 {app.get('name')}")
        self.refresh()

    def remove_app(self, app: dict):
        name = app.get("name") or "该应用"
        self.store.remove_app(app.get("id"))
        if self.interceptor:
            self.interceptor.forget(app.get("path", ""))
        config_store.log(f"已移除受保护应用: {name}")
        self.refresh()

    def toggle_app(self, app: dict):
        enabled = bool(app.get("enabled", True))
        name = app.get("name") or "该应用"
        if enabled:
            self.store.set_enabled(app.get("id"), False)
            if self.interceptor:
                self.interceptor.forget(app.get("path", ""))
            config_store.log(f"已停用保护: {name}")
        else:
            self.store.set_enabled(app.get("id"), True)
            config_store.log(f"已启用保护: {name}")
        self.refresh()

    def launch_app(self, app: dict):
        """界面内启动：免密码，直接放行。已在运行时不重复拉起。"""
        name = app.get("name") or "该程序"
        path = app.get("path", "")
        if not os.path.isfile(path):
            messagebox.showerror("无法启动", "文件不存在或已被移动，请重新添加。",
                                 parent=self.root)
            return
        if self._is_running(app):
            # 双击已运行的行不应该再开一个窗口，只把它带回前台
            self.push_event_text(f"{name} 已在运行")
            self.toast(f"{name} 已在运行", kind="info")
            self._focus_running(path)
            self.refresh()
            return
        ok, message = self.interceptor.launch(app)
        self.push_event_text(("已启动 " if ok else "启动失败 ") + name)
        if ok:
            self.toast(f"已启动 {name}", kind="success")
        else:
            messagebox.showerror("启动失败", message, parent=self.root)
        self.root.after(1200, self.refresh)

    def close_app(self, app: dict):
        """关闭单个受保护应用：结束它的全部进程并恢复保护。"""
        name = app.get("name") or "该程序"
        try:
            total = self.interceptor.close_one(app)
        except AttributeError:  # 兼容旧版注入桩
            self.interceptor.forget(app.get("path", ""))
            total = 0
        if total:
            self.toast(f"已关闭 {name}（结束 {total} 个进程）", kind="success")
            self.push_event_text(f"已关闭 {name}")
        else:
            self.toast(f"{name} 已不在运行", kind="info")
        self.refresh_later(0)

    def _focus_running(self, path: str):
        """让该应用的已有窗口回到前台（方法不存在时静默跳过）。"""
        if not self.interceptor:
            return
        try:
            self.interceptor.focus_running(path)
        except AttributeError:
            pass

    def _is_running(self, app: dict) -> bool:
        """应用当前是否在运行：放行账本优先，账本没有时回落到进程枚举。"""
        if not self.interceptor:
            return False
        path = app.get("path", "")
        try:
            return bool(self.interceptor.is_running(path))
        except AttributeError:  # 旧版注入桩只提供 is_unlocked
            try:
                return bool(self.interceptor.unlocked_paths()
                            and self.interceptor.is_unlocked(path))
            except Exception:
                return False

    def row_double_click(self, app: dict):
        """双击整行：未运行则启动；已在运行时只把窗口带回前台，不新开一个。"""
        self.launch_app(app)

    def lock_all(self):
        """一键关闭全部受保护应用（按 exe 路径全量枚举）。"""
        try:
            total = self.interceptor.close_all_protected()
        except AttributeError:  # 兼容旧版注入桩
            total = self.interceptor.lock_all_running()
        if total:
            self.toast(f"已关闭全部受保护应用（结束 {total} 个进程）", kind="success")
            if self.tray:
                self.tray.notify(f"已一键关闭全部受保护应用（结束 {total} 个进程）")
        else:
            self.toast("当前没有正在运行的受保护应用", kind="info")
        self.refresh()

    def open_settings(self):
        SettingsDialog(self.root, self.store, self.interceptor,
                       on_changed=self.refresh).run()
        self.refresh()

    def relaunch_admin(self):
        # 等价于一次退出：需要密码；通过后以管理员身份重启
        if not self._authenticate_with(
                "以管理员身份重启",
                "重启软件锁需要输入主密码。重启期间保护会短暂中断。",
                confirm="重启", danger=True,
                hint="将以管理员权限重新启动软件锁，以便拦截管理员程序。"):
            config_store.log("提权重启被取消（密码未通过）")
            return
        self.on_quit(then=lambda: winsys.relaunch_as_admin(["--force-new", "--elevated"]))

    # ================================================================= 显示控制
    def show(self):
        """打开主界面：每次都需要输入密码，验证不通过则保持隐藏。"""
        if not self._authenticate():
            config_store.log("主界面打开被取消（密码未通过）")
            return
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.focus_force()
            self.refresh()
            if self.tray:
                self.tray.show_icon()  # 恢复可能被隐藏的托盘图标
        except Exception:
            pass

    def _authenticate(self) -> bool:
        if self._authenticated:
            return True
        ok = self._authenticate_with(
            "打开软件锁",
            "请输入主密码以打开软件锁。",
            confirm="解锁",
            hint="连续输错会进入冷却；点击「取消」窗口将保持隐藏，后台保护继续。",
        )
        self._authenticated = bool(ok)
        return self._authenticated

    def _authenticate_with(self, title: str, subtitle: str, confirm: str = "确认",
                           danger: bool = False, hint: str = "") -> bool:
        """统一的密码校验入口（内部走 broker，测试时可注入假的 broker）。"""
        if self.broker is None:
            return False
        try:
            return bool(self.broker.verify(title, subtitle, confirm_text=confirm,
                                           danger=danger, hint=hint))
        except Exception as exc:
            config_store.log(f"鉴权异常: {exc}", "ERROR")
            return False

    def tray_available(self) -> bool:
        return bool(self.tray and getattr(self.tray, "_available", False))

    def hide(self):
        # 托盘不可用时绝不能隐藏窗口，否则用户再也找不回来
        if not self.tray_available():
            messagebox.showwarning(
                "无法隐藏到托盘",
                "系统托盘图标未能启动，窗口不能隐藏，否则将无法重新打开。\n"
                "请直接最小化窗口。",
                parent=self.root,
            )
            try:
                self.root.iconify()
            except Exception:
                pass
            return
        try:
            self.root.withdraw()
            self._authenticated = False  # 下次打开需要重新输入密码
            config_store.log("主窗口已隐藏到托盘")
        except Exception:
            pass

    def on_close(self):
        # 关闭窗口 = 隐藏到托盘，后台继续守护；不做任何弹窗、不需要密码
        if self.tray_available():
            self.hide()
        else:
            try:
                self.root.iconify()
            except Exception:
                pass

    # ================================================================= 退出
    def exit_soft(self):
        """托盘菜单「退出软件锁」：需密码，只退出可见界面，后台守护继续保护。"""
        if not self._authenticate_with(
                "退出软件锁",
                "退出后软件锁会从桌面和托盘消失，但后台守护继续运行，"
                "受保护的应用仍然需要输入密码才能打开。",
                confirm="退出", danger=True,
                hint="软件锁仍在后台保护你的应用；再次双击「软件锁.exe」可重新打开界面。"):
            config_store.log("退出界面被取消（密码未通过）")
            return
        self._soft_exit()

    def _soft_exit(self):
        """只退出可见界面：隐藏窗口与托盘图标，后台守护照常运行。"""
        try:
            if self.tray:
                # 通知必须在隐藏图标之前发出，否则用户看不到
                self.tray.notify("软件锁已退出界面，后台守护继续运行")
            self.root.withdraw()
            if self.tray:
                self.tray.hide_icon()
            self._authenticated = False
            config_store.log("已退出界面，后台守护继续保护")
        except Exception as exc:
            config_store.log(f"退出界面失败: {exc}", "ERROR")

    def exit_hard(self):
        """托盘菜单「完全退出并停止保护」：需密码，真正结束软件锁。"""
        if not self._authenticate_with(
                "完全退出并停止保护",
                "这将彻底结束软件锁，后台守护停止，"
                "所有受保护应用将不再需要密码即可打开。",
                confirm="停止保护并退出", danger=True,
                hint="完全退出后，任何受保护应用都可以直接打开，且不会再有密码提示。"):
            config_store.log("完全退出被取消（密码未通过）")
            return
        self.on_quit()

    # ================================================================= 事件
    def push_event_text(self, text: str):
        import time

        self._last_event_text = f"{time.strftime('%H:%M:%S')} {text}"
        try:
            if self.root.winfo_exists():
                self.event_label.configure(text=f"最近活动：{self._last_event_text}")
        except Exception:
            pass

    def notify_event(self, event):
        """由拦截引擎回调（可能在后台线程），转回主线程刷新。"""
        try:
            self.root.after(0, lambda: self._apply_event(event))
        except Exception:
            pass

    def _apply_event(self, event):
        action = getattr(event, "action", "")
        name = getattr(event, "name", "") or "应用"
        if action == "blocked":
            self.push_event_text(f"已阻止 {name}（验证未通过）")
            if self.tray:
                self.tray.notify(f"已阻止「{name}」启动：未通过密码验证")
        elif action == "allowed":
            self.push_event_text(f"已放行 {name}")
        self.refresh_later()
