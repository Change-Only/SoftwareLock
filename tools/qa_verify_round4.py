# -*- coding: utf-8 -*-
"""QA 第四轮独立回归验证 —— 需求 2「退出后受保护应用仍不能直接打开」。

设计原则（按 QA 任务要求）：
  * 使用 **真实** 的 Store（指向临时配置路径）、**真实** 的 Interceptor、
    **真实** 的 MainWindow、**真实** 的 tk.Tk()。
  * 唯一允许的桩是「用户输入密码」这一步（可编程返回 True/False），
    通过注入 FakeBroker 实现；Store / Interceptor / MainWindow 的实现均未替换。
  * Interceptor.request_auth 用阻塞式桩，以便在「冻结态」观察进程是否真被挂起
    （这是拦截引擎对外部输入的等待，不属于被测实现）。

断言链见 qa-round4 报告 B1..B5。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE / "src"))

import tkinter as tk  # noqa: E402

import psutil  # noqa: E402

import config_store  # noqa: E402
from config_store import Store  # noqa: E402
from interceptor import Interceptor  # noqa: E402
from ui import theme as T  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    RESULTS.append((name, bool(ok), detail))
    return bool(ok)


# --------------------------------------------------------------------------
# 唯一允许的桩：代替「用户输入密码」
# --------------------------------------------------------------------------
class FakeBroker:
    """可编程返回的密码校验桩（不改动任何安全逻辑）。"""

    def __init__(self, approve: bool = True):
        self.approve = approve
        self.calls: list[dict] = []

    def verify(self, title, subtitle, confirm_text="确认", danger=False, hint=""):
        self.calls.append({"title": title, "danger": danger, "confirm_text": confirm_text})
        return self.approve


# 真实 Tray 的子类：只增加调用记录，行为仍走真实实现
from ui.tray import Tray  # noqa: E402


class RecordingTray(Tray):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.hide_calls = 0
        self.show_calls = 0
        self.notified: list[str] = []

    def hide_icon(self):
        self.hide_calls += 1
        return super().hide_icon()

    def show_icon(self):
        self.show_calls += 1
        return super().show_icon()

    def notify(self, message, title="软件锁"):
        self.notified.append(message)
        return super().notify(message, title)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="softlock-qa4-"))
    exit_flag = config_store.EXIT_FLAG  # 真实的 %APPDATA%\SoftwareLock\allow_exit.flag
    flag_orig = exit_flag.exists()
    try:
        if flag_orig:
            exit_flag.unlink()
    except Exception:
        pass

    # 真实经典桌面程序：用 cmd.exe 的副本作为受保护目标（notepad.exe 在
    # Win11 上是会跳转到 WindowsApps 的 UWP 应用别名，路径不匹配，见 README「UWP 不适用」）
    target = str(tmp / "qa_target.exe")
    shutil.copy(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe", target)
    spawned: list[subprocess.Popen] = []

    store = Store(tmp / "config.json")
    store.set_password("qa-round4-123456")
    store.update_settings(lock_running_on_start=False, self_defense=False,
                          poll_interval_ms=80, prompt_timeout_seconds=300)
    app = store.add_app(target, "qa_target")
    check("前置：受保护目标已加入列表（真实 add_app）", bool(app), target)

    release = threading.Event()
    auth_calls: list[tuple[str, list[int]]] = []

    def request_auth(apps, pids, reason="launch"):
        auth_calls.append((reason, list(pids)))
        release.wait(20)  # 阻塞，令被拦截进程停留在「冻结」状态以便观测
        return False      # 最终拒绝，进程应被结束

    interceptor = Interceptor(store, request_auth=request_auth)  # 真实引擎

    T.init_dpi_awareness()
    root = tk.Tk()
    T.apply_theme(root)
    root.withdraw()

    broker = FakeBroker(True)
    tray = RecordingTray(root, on_open=lambda: None, on_lock=lambda: None,
                         on_exit_soft=lambda: None, on_exit_hard=lambda: None)
    quit_calls: list[int] = []

    def on_quit(then=None):
        """忠实复刻 main.py 中注入给 MainWindow 的 quit_app 行为。"""
        quit_calls.append(1)
        try:
            exit_flag.write_text(str(os.getpid()), encoding="utf-8")
        except Exception:
            pass
        try:
            interceptor.stop()
        except Exception:
            pass
        try:
            tray.stop()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass
        if then:
            try:
                then()
            except Exception:
                pass

    from ui.main_window import MainWindow  # noqa: E402

    win = MainWindow(root, store, interceptor, broker, tray, on_quit)  # 真实主窗口
    interceptor.start()  # 真实后台拦截线程
    time.sleep(0.9)
    check("前置：真实拦截引擎已启动", interceptor.running)

    # ============================================================ B1
    print("\n[B1] exit_soft + 密码桩返回 False -> 什么都不发生", flush=True)
    broker.approve = False
    quit_calls.clear()
    win._authenticated = False
    win.exit_soft()
    root.update()
    check("B1 on_quit 调用次数 == 0", len(quit_calls) == 0, f"{len(quit_calls)}")
    check("B1 EXIT_FLAG 不存在", not exit_flag.exists(), str(exit_flag))
    check("B1 interceptor.running 仍为 True", interceptor.running)

    # ============================================================ B2
    print("\n[B2] exit_soft + 密码桩返回 True -> 只退界面", flush=True)
    broker.approve = True
    broker.calls.clear()
    quit_calls.clear()
    win._authenticated = False
    win.exit_soft()
    root.update()
    check("B2 on_quit 调用次数 == 0", len(quit_calls) == 0, f"{len(quit_calls)}")
    check("B2 EXIT_FLAG 仍不存在", not exit_flag.exists(), str(exit_flag))
    check("B2 interceptor.running 仍为 True", interceptor.running)
    check("B2 主窗口 state() == withdrawn", win.root.state() == "withdrawn", win.root.state())
    check("B2 tray.hide_icon() 已被调用", tray.hide_calls >= 1, f"hide_calls={tray.hide_calls}")
    check("B2 退出提示已发出", bool(tray.notified))
    check("B2 退出对话框使用 danger 红色标题",
          any(c["danger"] for c in broker.calls),
          str([(c["title"], c["danger"]) for c in broker.calls]))

    # ============================================================ B-extra: show() 恢复
    print("\n[B-extra] _soft_exit 之后 show() 能重新显示并恢复托盘图标", flush=True)
    broker.approve = True
    show_before = tray.show_calls
    win._authenticated = False
    win.show()
    root.update()
    check("B-extra show() 后窗口重新可见", win.root.state() != "withdrawn", win.root.state())
    if tray is None:
        print("  [SKIP] tray 为 None，跳过 tray.show_icon 断言", flush=True)
    else:
        check("B-extra show() 调用了 tray.show_icon()",
              tray.show_calls > show_before, f"{show_before} -> {tray.show_calls}")

    # ============================================================ B3
    print("\n[B3] 退出界面后，用户直接双击受保护程序 -> 仍被拦截冻结", flush=True)
    proc = subprocess.Popen([target], stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    spawned.append(proc)
    frozen = False
    last_status = None
    t0 = time.time()
    while time.time() - t0 < 10:
        time.sleep(0.1)
        try:
            last_status = psutil.Process(proc.pid).status()
        except Exception:
            last_status = None
        if last_status == psutil.STATUS_STOPPED:
            frozen = True
            break
    hit_internal = proc.pid in interceptor._pid_path
    check("B3 该 pid 被拦截引擎命中（内部记录 _pid_path）", hit_internal,
          f"pid={proc.pid}")
    check("B3 该 pid 被真实挂起（psutil.status == stopped）", frozen,
          f"status={last_status}")
    check("B3 后台守护仍在运行", interceptor.running)
    check("B3 未通过密码时不会写 EXIT_FLAG", not exit_flag.exists())
    # 收尾：释放 request_auth，结束被冻结的 notepad
    release.set()
    time.sleep(0.6)
    try:
        psutil.Process(proc.pid).kill()
    except Exception:
        pass

    # ============================================================ B4
    print("\n[B4] exit_hard + 密码桩返回 False -> 什么都不发生", flush=True)
    try:
        win._authenticated = False
    except Exception:
        pass
    broker.approve = False
    broker.calls.clear()
    quit_calls.clear()
    win._authenticated = False
    win.exit_hard()
    root.update() if win.root.winfo_exists() else None
    check("B4 on_quit 调用次数 == 0", len(quit_calls) == 0, f"{len(quit_calls)}")
    check("B4 EXIT_FLAG 不存在", not exit_flag.exists())

    # ============================================================ B5
    print("\n[B5] exit_hard + 密码桩返回 True -> 真正退出并写 EXIT_FLAG", flush=True)
    broker.approve = True
    broker.calls.clear()
    quit_calls.clear()
    win._authenticated = False
    win.exit_hard()
    check("B5 on_quit 调用次数 == 1", len(quit_calls) == 1, f"{len(quit_calls)}")
    check("B5 EXIT_FLAG 已写入", exit_flag.exists())
    check("B5 interceptor 已停止", not interceptor.running)
    check("B5 完全退出对话框使用 danger 红色标题",
          any(c["danger"] and c["title"] == "完全退出并停止保护" for c in broker.calls),
          str([(c["title"], c["danger"]) for c in broker.calls]))

    # ============================================================ 收尾
    print("\n[cleanup] 还原环境", flush=True)
    try:
        interceptor.stop()
    except Exception:
        pass
    for p in spawned:
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass
    # 恢复 EXIT_FLAG 到测试前状态
    try:
        if flag_orig:
            exit_flag.write_text("0", encoding="utf-8")
        else:
            exit_flag.unlink()
    except Exception:
        pass
    try:
        root.destroy()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 56)
    print(f"QA 集成验证：{passed}/{total} 通过", flush=True)
    fails = [n for n, ok, _ in RESULTS if not ok]
    if fails:
        print(f"失败项: {fails}", flush=True)
        return 1
    print("全部通过 OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
