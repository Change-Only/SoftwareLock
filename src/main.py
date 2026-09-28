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
# 看守进程轮询间隔：主进程被结束后最多这么久就会被重新拉起
WATCHDOG_POLL = 0.4
# 拉起重启实例后，等待它完成接管的时间（0.5s × 步数）
TAKEOVER_WAIT_STEPS = 24


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


def run_watchdog(parent_pid: int) -> int:
    """看守模式：主进程被任务管理器结束后把它拉起来。

    关键语义（1.4.2 修复）：
      * 看守进程由 ``spawn_detached_tree_safe`` 拉起，父进程不在软件锁的进程树里，
        「结束进程树」连坐不到它 —— 这是树杀漏洞的堵点；
      * 拉起重启实例后**等待接管成功**才算完成使命；重启实例闪退 / UAC 未批准时
        继续循环再拉（受 MAX_GUARD_RESTARTS 限制），不会一次失败就弃守；
      * 非提权环境下重启实例带 ``--no-elevate``：无人值守的重启路径上不再弹 UAC，
        静默以普通权限恢复保护（界面显示橙色警告条），好过保护直接死亡。
    """
    import time

    import psutil

    config_store.log(f"看守进程启动，监视 PID {parent_pid}")
    while True:
        time.sleep(WATCHDOG_POLL)
        try:
            if config_store.EXIT_FLAG.exists():
                config_store.log("看守进程退出（收到退出标记）")
                return 0
            if parent_pid and psutil.pid_exists(parent_pid):
                continue
            # 主进程没了：若已有实例接管（重启交接 / 用户手动启动），使命完成
            if winsys.instance_running():
                config_store.log("看守进程退出（检测到已有实例接管）")
                return 0
            count = _read_guard_fail() + 1
            if count > MAX_GUARD_RESTARTS:
                config_store.log("看守进程：短时间重启次数过多，放弃拉起", "ERROR")
                break
            _write_guard_fail(count)
            config_store.log(f"检测到主进程终止，第 {count} 次自动重启")
            restart_args = ["--minimized"]
            if winsys.is_elevated():
                # 看守继承主进程的管理员令牌：静默提权重启，无需再过 UAC
                restart_args.append("--elevated")
            else:
                restart_args.append("--no-elevate")
            winsys.spawn_detached_tree_safe(_child_command(*restart_args))
            # 等新实例接管；没接管成功就继续循环再拉
            taken_over = False
            for _ in range(TAKEOVER_WAIT_STEPS):
                time.sleep(0.5)
                if config_store.EXIT_FLAG.exists() or winsys.instance_running():
                    taken_over = True
                    break
            if taken_over:
                config_store.log("看守进程退出（重启实例已接管）")
                return 0
            config_store.log("重启实例未完成接管，继续尝试拉起", "WARNING")
            parent_pid = 0  # 原 pid 确认已死，后续只按 instance_running 判断
        except Exception as exc:
            config_store.log(f"看守进程异常: {exc}", "ERROR")
    config_store.log("看守进程退出（重启次数过多，放弃拉起）")
    return 0


def spawn_watchdog(parent_pid: int) -> int | None:
    args = _child_command("--watchdog", str(parent_pid))
    config_store.log(f"启动看守进程: {' '.join(args)}")
    # 必须经 WMI 脱树拉起：否则「结束进程树」会把看守连同主进程一起带走
    return winsys.spawn_detached_tree_safe(args)


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
        import threading
        import time as _time

        import psutil

        # 看守的拉起要经 WMI（PowerShell 一来一回可能要数秒），绝不能阻塞主线程：
        # 放后台线程里做，主循环照常运转；拉起结果放进 holder 供反向守护读取。
        guard_holder = {"pid": None}

        def _spawn_guard():
            guard_holder["pid"] = spawn_watchdog(os.getpid())

        threading.Thread(target=_spawn_guard, name="guard-spawner",
                         daemon=True).start()
        root.after(GUARD_HEALTHY_RESET_MS, lambda: _write_guard_fail(0))

        # 反向守护：看守进程被任务管理器结束掉时，主进程立刻再拉一个，
        # 两个进程互为看守，单独杀掉任何一个都会被另一个立即恢复。
        def _watch_guard():
            while not quitting["done"]:
                _time.sleep(0.6)
                try:
                    pid = guard_holder.get("pid")
                    if pid and not psutil.pid_exists(pid):
                        config_store.log("看守进程被终止，重新拉起")
                        new_pid = spawn_watchdog(os.getpid())
                        if new_pid:
                            guard_holder["pid"] = new_pid
                        else:
                            # 拉起失败（WMI/PowerShell 均不可用）：退避后再试，
                            # 避免每 0.6 秒连环重试
                            _time.sleep(3.0)
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
