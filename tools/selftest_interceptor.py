# -*- coding: utf-8 -*-
"""软件锁自动化自检（不依赖界面）。

A. 拦截引擎生命周期 —— 用真实可执行文件（cmd.exe 的副本）验证
   1. 密码哈希 / 校验 / 不落明文
   2. 直接启动受保护程序 -> 被检测、冻结、弹出鉴权请求
   3. 拒绝 -> 进程被结束
   4. 放行 -> 进程存活、path 进入放行集合
   5. 放行会话仍在运行时再启动 -> 不再重复索要密码（多开不打扰）
   6. **关闭该应用后立即重开 -> 必须再次输入密码**（放行随进程退出立即失效）
   7. 立即锁定 -> 结束进程并清空放行状态

B. 窗口原语 —— 用真实带窗口的进程验证 hide_windows / restore_windows
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE / "src"))

import config_store  # noqa: E402
import interceptor as ic  # noqa: E402
import winsys  # noqa: E402

CMD_SOURCE = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
FAILED: list[str] = []
_OUT = None


def _copy_cmd(tmp: Path) -> Path:
    target = tmp / "locktest.exe"
    if not target.exists():
        shutil.copy(CMD_SOURCE, target)
    return target


def _emit(line: str) -> None:
    print(line, flush=True)
    if _OUT is not None:
        try:
            with open(_OUT, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass


def check(name: str, ok: bool, detail: str = ""):
    _emit(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILED.append(name)


def spawn(target: Path, retries: int = 5):
    """启动目标程序，容忍刚被结束后短暂的 ERROR_ACCESS_DENIED。"""
    last = None
    for attempt in range(retries):
        try:
            return subprocess.Popen(
                [str(target)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except PermissionError as exc:
            last = exc
            time.sleep(0.6 * (attempt + 1))
    raise last


def kill_all(procs):
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass


# ---------------------------------------------------------------------------
def test_interceptor(tmp: Path) -> None:
    target = _copy_cmd(tmp)


    store = config_store.Store(tmp / "config.json")
    _emit("\n[A1] 密码哈希与校验")
    store.set_password("abc123456")
    check("正确密码通过", store.check_password("abc123456"))
    check("错误密码拒绝", not store.check_password("abc1234567"))
    check("空密码拒绝", not store.check_password(""))
    check("配置文件不含明文密码", "abc123456" not in (tmp / "config.json").read_text(encoding="utf-8"))
    check("首次失败未触发冷却", store.register_failure() == 0)

    app = store.add_app(str(target))
    check("添加受保护应用", bool(app), app.get("name") if app else "")
    check("重复添加不产生副本", len(store.apps) == 1)

    state = {"mode": "deny", "launches": 0}
    procs: list = []

    def request_auth(apps, pids, reason="launch"):
        names = [a.get("name") for a in apps]
        _emit(f"       鉴权请求 reason={reason} apps={names} pids={list(pids)}")
        if reason == "preexisting":
            return False
        state["launches"] += 1
        return state["mode"] == "allow"

    engine = ic.Interceptor(store, request_auth)
    engine.start()
    time.sleep(1.2)
    check("拦截引擎已启动", engine.running)

    # -------------------------------------------------------- A2 拒绝
    _emit("\n[A2] 模拟双击受保护程序（首次，拒绝）")
    proc = spawn(target)
    procs.append(proc)
    t0 = time.time()
    while time.time() - t0 < 10 and state["launches"] == 0:
        time.sleep(0.02)
    latency = (time.time() - t0) * 1000
    check("检测到启动并请求鉴权", state["launches"] == 1, f"{latency:.0f} ms")
    check("检测延迟 < 500 毫秒", latency < 500, f"{latency:.0f} ms")
    time.sleep(1.0)
    check("拒绝后进程被结束", proc.poll() is not None, f"returncode={proc.poll()}")

    # -------------------------------------------------------- A3 放行
    _emit("\n[A3] 再次启动并放行")
    state["mode"] = "allow"
    proc2 = spawn(target)
    procs.append(proc2)
    t0 = time.time()
    while time.time() - t0 < 10 and state["launches"] == 1:
        time.sleep(0.02)
    check("第二次弹出鉴权请求", state["launches"] == 2)
    time.sleep(1.2)
    check("放行后进程存活", proc2.poll() is None)
    check("路径进入已解锁集合", engine.is_unlocked(str(target)))

    # -------------------------------------------------------- A4 免密（多开）
    _emit("\n[A4] 已解锁期间再次启动")
    before = state["launches"]
    proc3 = spawn(target)
    procs.append(proc3)
    time.sleep(1.5)
    check("不再重复索要密码", state["launches"] == before,
          f"launches={state['launches']}，期望 {before}")
    check("新进程同样被放行", proc3.poll() is None)

    # -------------------------------------------------------- A5 关闭后立即重开
    # 用户报的 Bug：应用关掉后再打开不需要密码（旧实现有 3 秒免密空窗）
    _emit("\n[A5] 关闭受保护应用后立即重开（必须重新要密码）")
    for proc in (proc2, proc3):
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
    time.sleep(0.6)
    t0 = time.time()
    while time.time() - t0 < 2.0 and engine.is_unlocked(str(target)):
        time.sleep(0.02)
    revoke_ms = (time.time() - t0) * 1000
    check("进程退出后放行立即注销（≤1000 毫秒）",
          not engine.is_unlocked(str(target)) and revoke_ms <= 1000, f"{revoke_ms:.0f} ms")

    before = state["launches"]
    state["mode"] = "allow"
    proc4 = spawn(target)
    procs.append(proc4)
    t0 = time.time()
    while time.time() - t0 < 8 and state["launches"] == before:
        time.sleep(0.02)
    check("关闭后重新打开会再次索要密码", state["launches"] == before + 1,
          f"鉴权请求 {before} -> {state['launches']}")
    time.sleep(1.0)
    check("重新验证通过后进程存活", proc4.poll() is None)

    # -------------------------------------------------------- A6 立即锁定
    _emit("\n[A6] 立即锁定全部")
    time.sleep(0.5)
    killed = engine.lock_all_running()
    time.sleep(1.0)
    check("立即锁定生效", killed >= 1, f"结束 {killed} 个进程")
    check("解锁状态已清空", not engine.is_unlocked(str(target)))

    engine.stop()
    check("引擎可正常停止", not engine.running)
    stats = store.stats()
    check("统计已累加", stats.get("blocked", 0) >= 1 and stats.get("allowed", 0) >= 1, str(stats))
    kill_all(procs)


# ---------------------------------------------------------------------------
def test_window_primitives(tmp: Path) -> None:
    """窗口原语：在本进程内建一个真实窗口，验证枚举 / 隐藏 / 恢复 / 挂起 / 恢复。"""
    _emit("\n[B] 窗口枚举 / 隐藏 / 恢复")
    try:
        import tkinter as tk
    except Exception as exc:
        check("tkinter 可用", False, str(exc))
        return

    # B1：能看到别的进程的窗口（这是拦截其他软件窗口的前提）
    others = set()
    try:
        def _cb(hwnd, _lparam):
            pid = wintypes.DWORD()
            winsys.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value and pid.value != os.getpid() and winsys.user32.IsWindowVisible(hwnd):
                others.add(pid.value)
            return True

        winsys.user32.EnumWindows(winsys._WNDENUMPROC(_cb), 0)
    except Exception:
        pass
    check("可以枚举到其他进程的窗口", len(others) > 0, f"{len(others)} 个进程")

    # B2：自建窗口 -> 隐藏 -> 恢复
    root = tk.Tk()
    root.title("softlock-window-test")
    root.geometry("320x200+120+120")
    root.update()
    root.update_idletasks()
    try:
        mine = winsys._windows_of_pid(os.getpid())
        check("能找到本进程的可见窗口", bool(mine), f"{len(mine)} 个句柄")
        if not mine:
            return
        hidden = winsys.hide_windows(os.getpid())
        root.update()
        time.sleep(0.25)
        check("hide_windows 后窗口不可见", len(winsys._windows_of_pid(os.getpid())) == 0,
              f"隐藏了 {len(hidden)} 个")
        winsys.restore_windows(hidden)
        root.update()
        time.sleep(0.35)
        check("restore_windows 后窗口恢复可见",
              len(winsys._windows_of_pid(os.getpid())) > 0)
    finally:
        try:
            root.destroy()
        except Exception:
            pass

    # B3：挂起 / 恢复一个真实进程
    sub = spawn(_copy_cmd(tmp))
    try:
        time.sleep(0.6)
        check("子进程已启动", sub.poll() is None)
        check("suspend_process 生效", winsys.suspend_process(sub.pid))
        time.sleep(0.3)
        check("挂起后进程仍存在", sub.poll() is None)
        check("resume_process 生效", winsys.resume_process(sub.pid))
        time.sleep(0.4)
        check("恢复后进程仍存活", sub.poll() is None)
    finally:
        try:
            winsys.terminate_tree(sub.pid)
        except Exception:
            pass


def test_close_all(tmp: Path) -> None:
    """A5：一键关闭全部受保护应用（按 exe 路径全量枚举，不依赖扫描缓存）。"""
    _emit("\n[A5] 一键关闭全部受保护应用")

    store = config_store.Store(tmp / "config_close_all.json")
    store.set_password("abc123456")
    target = _copy_cmd(tmp)
    app = store.add_app(str(target))
    check("close_all：已登记受保护应用", bool(app))

    engine = ic.Interceptor(store, request_auth=lambda apps, pids, reason="launch": False)

    # 目标进程正在运行，但引擎从未扫描到它（模拟解锁状态下启动 / 错过扫描窗口）
    sub = spawn(target)
    try:
        time.sleep(0.6)
        check("close_all：目标进程已启动", sub.poll() is None)
        check("close_all：引擎未登记该进程", sub.pid not in engine._pid_path)

        killed = engine.close_all_protected()
        time.sleep(0.4)
        check("close_all：返回被结束的进程数 > 0", killed >= 1, f"killed={killed}")
        check("close_all：目标进程已被结束", sub.poll() is not None)
        check("close_all：解锁状态已清空", not engine.unlocked_paths())
    finally:
        try:
            winsys.terminate_tree(sub.pid)
        except Exception:
            pass

    # 没有正在运行的受保护进程时应返回 0
    check("close_all：无进程时返回 0", engine.close_all_protected() == 0)
    # 未启用 / 空列表时不做任何事
    store.set_enabled(app["id"], False)
    check("close_all：空目标列表返回 0", engine.close_all_protected() == 0)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="softlock-test-"))
    _emit(f"临时目录: {tmp}")
    try:
        test_interceptor(tmp)
        test_close_all(tmp)
        test_window_primitives(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    _emit("\n" + "=" * 52)
    if FAILED:
        _emit(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    _emit("全部通过 OK")
    return 0


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        if arg.startswith("--out="):
            _OUT = arg.split("=", 1)[1]
            try:
                Path(_OUT).unlink()
            except Exception:
                pass
    sys.exit(main())
