# -*- coding: utf-8 -*-
"""软件锁 —— 程序入口。

职责：
  * 解析启动参数（--minimized / --watchdog / --force-new）
  * 单实例控制、DPI 与主题初始化
  * 首次运行初始化密码
  * 组装 配置 / 拦截引擎 / 鉴权调度 / 主窗口 / 托盘 / 看守进程
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config_store  # noqa: E402
import winsys  # noqa: E402
from config_store import Store  # noqa: E402

MAX_GUARD_RESTARTS = 30
GUARD_HEALTHY_RESET_MS = 20_000


# ---------------------------------------------------------------------------
# 看守进程
# ---------------------------------------------------------------------------
def _child_command(*extra: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *extra]
    return [sys.executable, os.path.abspath(sys.argv[0]), *extra]


def _read_guard_fail() -> int:
    try:
        return int((config_store.GUARD_FAIL.read_text(encoding="utf-8") or "0").strip())
    except Exception:
        return 0


def _write_guard_fail(value: int) -> None:
    try:
        config_store.ensure_dirs()
        config_store.GUARD_FAIL.write_text(str(value), encoding="utf-8")
    except Exception:
        pass


def _spawn_detached(args: list[str]) -> int | None:
    try:
        flags = 0
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        proc = subprocess.Popen(args, close_fds=True, creationflags=flags,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        return proc.pid
    except Exception as exc:
        config_store.log(f"拉起子进程失败: {exc}", "ERROR")
        return None


def run_watchdog(parent_pid: int) -> int:
    """看守模式：主进程非正常退出时把它拉起来。"""
    import time

    import psutil

    config_store.log(f"看守进程启动，监视 PID {parent_pid}")
    while True:
        time.sleep(0.8)
        try:
            if config_store.EXIT_FLAG.exists():
                break
            if psutil.pid_exists(parent_pid):
                continue
            # 主进程没了：若已有新实例在跑，说明是正常交接，无需干预
            if winsys.instance_running():
                break
            count = _read_guard_fail() + 1
            if count > MAX_GUARD_RESTARTS:
                config_store.log("看守进程：短时间重启次数过多，放弃拉起", "ERROR")
                break
            _write_guard_fail(count)
            config_store.log(f"检测到主进程终止，第 {count} 次自动重启")
            restart_args = ["--minimized"]
            if winsys.is_elevated():
                # 已在管理员权限下运行，带上 --elevated 避免重启时再弹 UAC
                restart_args.append("--elevated")
            _spawn_detached(_child_command(*restart_args))
            break
        except Exception as exc:
            config_store.log(f"看守进程异常: {exc}", "ERROR")
    config_store.log("看守进程退出")
    return 0


def spawn_watchdog(parent_pid: int) -> int | None:
    args = _child_command("--watchdog", str(parent_pid))
    config_store.log(f"启动看守进程: {' '.join(args)}")
    return _spawn_detached(args)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(add_help=True, description="软件锁")
    parser.add_argument("--watchdog", type=int, default=0, metavar="PID")
    parser.add_argument("--minimized", action="store_true")
    parser.add_argument("--force-new", action="store_true")
    parser.add_argument("--no-guard", action="store_true")
    parser.add_argument("--no-elevate", action="store_true")   # 跳过提权（调试 / 自动化测试）
    parser.add_argument("--elevated", action="store_true")     # 内部标记：本进程已由提权启动
    parser.add_argument("--quit", "--stop", dest="quit", action="store_true",
                        help="维护通道：完全停止软件锁（需输入主密码），用于升级或卸载")
    args, _unknown = parser.parse_known_args()

    config_store.ensure_dirs()

    if args.watchdog:
        return run_watchdog(args.watchdog)

    # ---------------- 读取配置（提权判断需在创建 Tk 之前） ----------------
    store = Store()

    # ---------------- 管理员权限启动 ----------------
    if (not args.elevated and not args.no_elevate
            and store.settings.get("require_admin", True)
            and not winsys.is_elevated()):
        if winsys.relaunch_as_admin(["--force-new", "--elevated"]):
            config_store.log("已请求以管理员身份重新启动，本进程退出")
            return 0
        config_store.log("提权请求被拒绝，将以普通权限继续运行", "WARNING")

    # ---------------- 单实例 ----------------
    singleton = winsys.SingleInstance()
    if args.elevated:
        # 提权后的新实例：旧进程可能还没退完，互斥体短暂仍被占用 —— 带重试等待。
        # 注意：这里不走 --force-new，避免绕过单实例造成多开。
        if not singleton.acquire_retry(retries=20, delay=0.2):
            config_store.log("提权实例已存在，请求唤起主窗口")
            try:
                config_store.SHOW_FLAG.write_text(str(os.getpid()), encoding="utf-8")
            except Exception:
                pass
            return 0
    elif not args.force_new and not singleton.acquire():
        config_store.log("检测到已有实例，请求唤起主窗口")
        try:
            config_store.SHOW_FLAG.write_text(str(os.getpid()), encoding="utf-8")
        except Exception:
            pass
        return 0

    config_store.set_app_user_model_id("SoftwareLock.Desktop")
    try:
        config_store.EXIT_FLAG.unlink()
    except Exception:
        pass

    # ---------------- 界面骨架 ----------------
    import tkinter as tk

    from ui import theme as T

    T.init_dpi_awareness()
    root = tk.Tk()
    T.apply_theme(root)
    root.withdraw()

    # ---------------- 首次初始化 ----------------
    if not store.has_password:
        from ui.dialogs import SetupDialog

        setup = SetupDialog(None, store)
        result = setup.run()
        if not result or not result.get("ok"):
            config_store.log("用户取消了初始化")
            singleton.release()
            try:
                root.destroy()
            except Exception:
                pass
            return 0

    # ---------------- 组装组件 ----------------
    from interceptor import Interceptor
    from ui.broker import AuthBroker
    from ui.main_window import MainWindow
    from ui.tray import Tray

    broker = AuthBroker(root, store)
    holder: dict = {}

    interceptor = Interceptor(
        store,
        request_auth=lambda apps, pids, reason="launch": broker.request(apps, pids, reason),
        on_event=lambda event: holder["win"].notify_event(event) if holder.get("win") else None,
    )
    broker.owner_window = root

    quitting = {"done": False}

    def quit_app(then=None):
        if quitting["done"]:
            return
        quitting["done"] = True
        try:
            config_store.EXIT_FLAG.write_text(str(os.getpid()), encoding="utf-8")
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
        singleton.release()
        if then:
            try:
                then()
            except Exception as exc:
                config_store.log(f"退出后回调失败: {exc}", "ERROR")

    window = MainWindow(root, store, interceptor, broker, None, quit_app)
    holder["win"] = window

    tray = Tray(root,
                on_open=window.show,
                on_lock=window.lock_all,
                on_exit_soft=window.exit_soft)
    tray_ok = tray.start()
    window.tray = tray

    if not tray_ok:
        config_store.log("托盘不可用，已改回带窗口启动", "ERROR")
        if args.minimized:
            # 托盘不可用时不能以隐藏状态启动，否则用户无法再打开界面
            args.minimized = False
        store.update_settings(autostart=False)
        winsys.set_autostart(False)

    # ---------------- 启动守护 ----------------
    interceptor.start()

    # ---------------- 维护通道：命令行完全停止（需密码） ----------------
    if args.quit:
        # 界面上刻意不提供「完全退出」入口（退出 = 后台继续保护），
        # 这里保留一条带密码校验的命令行出口，供升级 / 卸载时停止软件锁。
        window.exit_hard()
        if not quitting["done"]:
            config_store.log("命令行停止被取消（密码未通过），软件锁继续后台保护")
            args.minimized = True

    if not args.no_guard and store.settings.get("self_defense", True):
        guard_pid = spawn_watchdog(os.getpid())
        root.after(GUARD_HEALTHY_RESET_MS, lambda: _write_guard_fail(0))

        # 反向守护：看守进程被任务管理器结束掉时，主进程立刻再拉一个，
        # 两个进程互为看守，单独杀掉任何一个都会被另一个立即恢复。
        if guard_pid:
            import threading
            import time as _time

            import psutil

            def _watch_guard():
                nonlocal guard_pid
                while not quitting["done"]:
                    _time.sleep(1.0)
                    try:
                        if guard_pid and not psutil.pid_exists(guard_pid):
                            config_store.log("看守进程被终止，重新拉起")
                            guard_pid = spawn_watchdog(os.getpid()) or None
                    except Exception as exc:
                        config_store.log(f"反向守护异常: {exc}", "ERROR")

            threading.Thread(target=_watch_guard, name="guard-watcher",
                             daemon=True).start()

    def _watch_show_flag():
        try:
            if config_store.show_flag_requested():
                window.show()
        except Exception:
            pass
        try:
            root.after(700, _watch_show_flag)
        except Exception:
            pass

    root.after(700, _watch_show_flag)

    if not args.minimized:
        window.show()
        config_store.log("主窗口已显示")
    else:
        config_store.log("以最小化（托盘）方式启动")

    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        if not quitting["done"]:
            quit_app()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        config_store.log("未捕获异常:\n" + traceback.format_exc(), "ERROR")
        sys.exit(1)
