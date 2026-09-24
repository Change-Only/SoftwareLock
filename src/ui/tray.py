# -*- coding: utf-8 -*-
"""系统托盘图标（pystray）。

托盘回调运行在托盘线程，所有 UI 动作都通过 root.after 抛回主线程执行。
"""
from __future__ import annotations

import config_store


class Tray:
    def __init__(self, root, on_open, on_lock, on_exit_soft, on_exit_hard=None,
                 title: str = "软件锁 · 后台守护中"):
        """
        on_open      : 打开主界面（需密码）
        on_lock      : 一键关闭全部受保护应用
        on_exit_soft : 退出软件锁（需密码；退出可见界面后后台守护继续保护）
        on_exit_hard : 可选；仅命令行 --quit 维护通道使用，不进托盘菜单
        """
        self.root = root
        self.on_open = on_open
        self.on_lock = on_lock
        self.on_exit_soft = on_exit_soft
        self.on_exit_hard = on_exit_hard
        self.title = title
        self.icon = None
        self._available = False

    def start(self) -> bool:
        try:
            import pystray

            import artwork

            image = artwork.make_app_icon(64)
            if image is None:
                return False
            menu = pystray.Menu(
                pystray.MenuItem("打开主界面", self._dispatch(self.on_open), default=True),
                pystray.MenuItem("一键关闭全部受保护应用", self._dispatch(self.on_lock)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出软件锁",
                                 self._dispatch(self.on_exit_soft)),
            )
            self.icon = pystray.Icon("SoftwareLock", image, self.title, menu)
            self.icon.run_detached()
            self._available = True
            config_store.log("托盘图标已启动")
            return True
        except Exception as exc:
            config_store.log(f"托盘启动失败: {exc}", "ERROR")
            self._available = False
            return False

    def _dispatch(self, func):
        def _run(icon=None, item=None):
            try:
                self.root.after(0, func)
            except Exception:
                pass

        return _run

    def set_title(self, text: str) -> None:
        self.title = text
        if self.icon is not None:
            try:
                self.icon.title = text
            except Exception:
                pass

    def notify(self, message: str, title: str = "软件锁") -> None:
        if not self._available or self.icon is None:
            return
        try:
            self.icon.notify(message, title)
        except Exception:
            pass

    # ------------------------------------------------------------ 显示控制
    def hide_icon(self) -> bool:
        """从任务栏托盘移除图标（进程与后台保护继续运行）。"""
        if self.icon is None:
            return False
        try:
            self.icon.visible = False
            return True
        except Exception:
            return False

    def show_icon(self) -> bool:
        """重新显示托盘图标（从后台界面重新打开时恢复）。"""
        if self.icon is None:
            return False
        try:
            self.icon.visible = True
            return True
        except Exception:
            return False

    def stop(self) -> None:
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None
        self._available = False
