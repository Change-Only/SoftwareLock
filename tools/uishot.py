# -*- coding: utf-8 -*-
"""渲染界面并截图，用于人工确认视觉效果。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE / "src"))

import tkinter as tk  # noqa: E402

import config_store  # noqa: E402
from ui import theme as T  # noqa: E402

OUT = Path(os.environ.get("TEMP", ".")) / "softlock-ui-shot"
OUT.mkdir(parents=True, exist_ok=True)


def grab(widget, name: str, init: bool = True):
    from PIL import ImageGrab

    if init and hasattr(widget, "_finish_init"):
        widget._finish_init()
    widget.update_idletasks()
    widget.update()
    time.sleep(0.8)
    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    w, h = widget.winfo_width(), widget.winfo_height()
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
    path = OUT / f"{name}.png"
    img.save(path)
    print(f"saved {path}  {img.size}")
    return path


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="softlock-shot-"))
    store = config_store.Store(tmp / "config.json")
    store.set_password("shot-123456")

    sysroot = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    for name, title in [("cmd.exe", "命令提示符"), ("notepad.exe", "记事本"),
                        ("charmap.exe", "字符映射表")]:
        path = sysroot / name
        if path.exists():
            store.add_app(str(path), title)
    store.apps[2]["enabled"] = False
    store.stats()["blocked"] = 12
    store.stats()["allowed"] = 5
    store.save()

    T.init_dpi_awareness()
    root = tk.Tk()
    T.apply_theme(root)

    from ui.broker import AuthBroker
    from ui.dialogs import PasswordDialog, SettingsDialog, SetupDialog
    from ui.main_window import MainWindow

    class FakeInterceptor:
        def __init__(self):
            self._running = set()

        # 引擎里的路径账本一律是归一化的，桩也照做，避免大小写带来假阴性
        @staticmethod
        def _key(path):
            return config_store.norm_path(path)

        def unlocked_paths(self):
            return set(self._running)

        def is_unlocked(self, path):
            return self._key(path) in self._running

        def is_running(self, path):
            return self._key(path) in self._running

        def running_pids(self, path):
            return [1] if self.is_running(path) else []

        def running_map(self, paths):
            return {self._key(p): ([1] if self.is_running(p) else [])
                    for p in paths}

        def startup_pending(self, path):
            return False

        def focus_running(self, path):
            return True

        def close_one(self, app):
            self._running.discard(self._key(app.get("path")))
            return 1

        def forget(self, path):
            self._running.discard(self._key(path))

        def launch(self, app):
            return True, "stub"

        def lock_all_running(self):
            return 0

    interceptor = FakeInterceptor()
    broker = AuthBroker(root, store)
    win = MainWindow(root, store, interceptor, broker, None, lambda then=None: None)
    win.push_event_text("已阻止 命令提示符（验证未通过）")
    root.update()
    root.deiconify()
    root.lift()
    root.update()

    grab(root, "01-main")

    # 运行中：该行出现「运行中」标签，按钮切成「关闭」
    if store.apps:
        interceptor._running.add(config_store.norm_path(store.apps[0]["path"]))
        win.refresh()
        root.update()
        grab(root, "10-main-running")
        interceptor._running.clear()
        win.refresh()
        root.update()

    # 「刷新状态」后的提示（toast：状态已更新 · N 个应用正在运行）
    if store.apps:
        interceptor._running.add(config_store.norm_path(store.apps[0]["path"]))
        win.check_status()
        root.update()
        grab(root, "11-status-checked", init=False)
        interceptor._running.clear()
        win.refresh()
        root.update()

    # 空态（含「添加第一个应用」主按钮）
    saved_apps = list(store.apps)
    store.apps.clear()
    win.refresh()
    root.update()
    grab(root, "09-empty")
    store.apps[:] = saved_apps
    win.refresh()
    root.update()

    dlg = PasswordDialog(root, store, title="启动「命令提示符」需要验证",
                         subtitle="「命令提示符」已被软件锁保护，请输入主密码后继续。",
                         confirm_text="解锁并启动", timeout=300)
    grab(dlg, "02-password")
    dlg._close()

    # 打开软件锁时弹出的密码框（broker.verify 用的参数）
    dlg = PasswordDialog(root, store, title="打开软件锁",
                         subtitle="请输入主密码以打开软件锁。",
                         confirm_text="解锁")
    grab(dlg, "06-open-lock")
    dlg._close()

    # 托盘菜单「退出软件锁（后台继续保护）」的危险色密码框
    dlg = PasswordDialog(root, store, title="退出软件锁",
                         subtitle="退出后软件锁会从桌面和托盘消失，但后台守护继续运行，"
                                  "受保护的应用仍然需要输入密码才能打开。",
                         confirm_text="退出", danger=True,
                         hint="软件锁仍在后台保护你的应用；再次双击「软件锁.exe」可重新打开界面。")
    grab(dlg, "07-exit-soft")
    dlg._close()

    # 维护通道 `软件锁.exe --quit` 使用的密码框
    dlg = PasswordDialog(root, store, title="完全退出并停止保护",
                         subtitle="这将彻底结束软件锁，后台守护停止，"
                                  "所有受保护应用将不再需要密码即可打开。",
                         confirm_text="停止保护并退出", danger=True,
                         hint="该入口仅用于升级 / 卸载，不在界面菜单中提供。")
    grab(dlg, "08-quit-cli")
    dlg._close()

    dlg = SettingsDialog(root, store, None)
    grab(dlg, "04-settings")
    dlg._close()

    dlg = SetupDialog(root, store)
    grab(dlg, "05-setup")
    dlg._close()

    root.destroy()
    shutil.rmtree(tmp, ignore_errors=True)
    print("done")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
