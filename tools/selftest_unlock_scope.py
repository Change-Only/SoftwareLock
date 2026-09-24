# -*- coding: utf-8 -*-
"""放行范围自检：受保护应用「关闭后再次打开必须重新输密码」。

背景（用户报的 Bug）：
    软件锁退出界面、后台继续保护 -> 第一次打开受保护应用要密码
    -> 关闭该应用 -> 再打开**不需要**密码了。

根因：旧实现用「路径最后一次被观察到存活的时刻 + 3 秒」判断放行是否过期，
受保护进程退出后仍有约 3 秒的免密空窗，关掉应用后马上重开会被当成「应用还开着」。

本脚本用真实 Store + 真实 Interceptor + 真实进程验证修复后的语义：
    S1 关闭后立即（约 0.2 秒内）重开  -> 必须重新输密码，且放行注销延迟 ≤ 1 秒
    S2 关闭后 1.5 秒重开              -> 必须重新输密码
    S3 应用仍在运行时再启动同一程序    -> 同一放行会话内，免密（多开不重复打扰）
    S4 界面「启动」发起                -> 免密
    S5 应用自身交接（launcher 退出、真正进程随后启动）-> 免密
    S6 pid 被复用（create_time 变化）  -> 放行立即失效，不得误放行
    S7 未通过密码                      -> 进程被结束，不得进入放行集合
    S8 进程活着但无任何可见窗口        -> 会话注销（服务型 / 关窗留后台的应用不给会话续命）
    S9 会话内有可见窗口                -> 不受无窗口规则影响（正常应用不误伤）
"""
from __future__ import annotations

import faulthandler
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE / "src"))

import config_store  # noqa: E402
import interceptor as ic  # noqa: E402
import winsys  # noqa: E402

CMD_SOURCE = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
FAILED: list[str] = []
PROCS: list[subprocess.Popen] = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    if not ok:
        FAILED.append(name)


def make_target(tmp: Path, name: str = "locktest.exe") -> Path:
    target = tmp / name
    if not target.exists():
        shutil.copy(CMD_SOURCE, target)
    return target


def spawn_direct(target: Path) -> subprocess.Popen:
    """本进程直接拉起目标（等同于「软件锁界面启动」的父进程特征）。"""
    proc = subprocess.Popen([str(target)], stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PROCS.append(proc)
    return proc


def _pids_for_exe(target: Path) -> set[int]:
    """枚举可执行文件路径正好是 target 的进程 pid。"""
    import psutil

    want = os.path.normcase(str(target))
    found: set[int] = set()
    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            exe = proc.info.get("exe")
        except Exception:
            continue
        if exe and os.path.normcase(exe) == want:
            found.add(proc.pid)
    return found


def spawn_external(target: Path, timeout: float = 10.0, new_console: bool = False):
    """模拟「资源管理器双击」：由中间进程拉起目标，使目标父进程是普通 shell 进程。

    new_console=True 时给目标独立控制台（CREATE_NEW_CONSOLE），目标会拥有
    一个真实可见的控制台窗口——用于验证「有窗口的会话不被无窗口规则注销」。
    pid 用「按 exe 路径枚举新增进程」得到，不依赖中间进程回传。
    """
    before = _pids_for_exe(target)
    # 注意：必须把 Popen 赋给变量并保持存活。否则 Popen 被回收 -> 它所持有的
    # stdin 管道关闭 -> 子进程（cmd.exe 副本）读到 EOF 立刻退出，目标变成「秒退」，
    # 「运行时多开免密」这类场景根本无从验证。
    flags = "creationflags=0x10," if new_console else ""
    code = (
        "import subprocess,sys,time\n"
        f"p=subprocess.Popen([sys.argv[1]], {flags} stdin=subprocess.PIPE,"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "time.sleep(120)\n"
    )
    bridge = subprocess.Popen(
        [sys.executable, "-c", code, str(target)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    PROCS.append(bridge)

    pid = 0
    t0 = time.time()
    while time.time() - t0 < timeout:
        new = _pids_for_exe(target) - before
        if new:
            pid = max(new)
            break
        time.sleep(0.05)
    if not pid:
        print(f"      ! 外部启动失败（等待 {timeout:g}s 未发现新进程）", flush=True)
    return pid, bridge


def kill_pid(pid: int):
    if pid:
        try:
            winsys.terminate_tree(pid)
        except Exception:
            pass


def wait_prompt(calls: dict, before: int, timeout: float = 8.0) -> float:
    t0 = time.time()
    while time.time() - t0 < timeout and calls["n"] == before:
        time.sleep(0.02)
    return (time.time() - t0) * 1000


def make_engine(tmp: Path, name: str, approve: bool = True):
    """每场景一套全新的 Store + Interceptor，避免互相污染。"""
    store = config_store.Store(tmp / f"config_{name}.json")
    store.set_password("abc123456")
    calls = {"n": 0, "pids": []}

    def request_auth(apps, pids, reason="launch"):
        calls["n"] += 1
        calls["pids"] = list(pids)     # 引擎认定的目标进程；比外部探测更可靠
        return approve

    engine = ic.Interceptor(store, request_auth)
    return store, engine, calls


def wait_alive(pid: int, timeout: float = 5.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            import psutil
            if not psutil.pid_exists(pid):
                return False
        except Exception:
            pass
        time.sleep(0.05)
    return True


def wait_dead(pid: int, timeout: float = 5.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            import psutil
            if pid and not psutil.pid_exists(pid):
                return True
        except Exception:
            pass
        time.sleep(0.05)
    return False


def wait_unlocked(engine, target: Path, pid: int = 0, timeout: float = 5.0) -> bool:
    """等引擎把 target 真正写入放行集合。

    `request_auth` 回调在 `_approve()` 之前返回，所以「密码框已弹出」不代表
    放行已经落账；多开/断言场景必须等状态稳定，否则会拿到假阴性。
    """
    key = str(target)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if engine.is_unlocked(key):
            return True
        time.sleep(0.02)
    return False


def scenario_close_reopen(tmp: Path, gap: float, label: str) -> None:
    print(f"\n[{label}] 关闭应用后 {gap:g} 秒重新打开（应重新要密码）", flush=True)
    store, engine, calls = make_engine(tmp, f"gap{int(gap * 1000)}")
    target = make_target(tmp, f"locktest_{int(gap * 1000)}.exe")
    store.add_app(str(target))
    engine.start()
    time.sleep(1.0)

    pid1, _ = spawn_external(target)
    ms = wait_prompt(calls, 0)
    check(f"{label}：首次打开弹出密码框", calls["n"] == 1, f"{ms:.0f} ms")
    check(f"{label}：首次放行后进程存活", pid1 > 0 and wait_alive(pid1, 0.2), f"pid={pid1}")
    check(f"{label}：路径进入放行集合", engine.is_unlocked(str(target)))

    # 关闭应用
    kill_pid(pid1)
    check(f"{label}：应用进程已退出", wait_dead(pid1), f"pid={pid1}")

    # 放行注销延迟
    t0 = time.time()
    while time.time() - t0 < 3.0 and engine.is_unlocked(str(target)):
        time.sleep(0.02)
    revoke_ms = (time.time() - t0) * 1000
    check(f"{label}：进程退出后放行立即注销（≤1000ms）",
          not engine.is_unlocked(str(target)) and revoke_ms <= 1000,
          f"{revoke_ms:.0f} ms")

    time.sleep(gap)
    before = calls["n"]
    pid2, _ = spawn_external(target)
    wait_prompt(calls, before, timeout=6.0)
    check(f"{label}：重新打开会再次索要密码", calls["n"] == before + 1,
          f"密码框 {before} -> {calls['n']}")
    kill_pid(pid2)
    engine.stop()


def scenario_running_multiopen(tmp: Path) -> None:
    print("\n[S3] 应用仍在运行时再启动同一程序（同一放行会话，应免密）", flush=True)
    store, engine, calls = make_engine(tmp, "multi")
    target = make_target(tmp, "locktest_multi.exe")
    store.add_app(str(target))
    engine.start()
    time.sleep(1.0)

    pid1, _ = spawn_external(target)
    wait_prompt(calls, 0)
    check("S3：首次打开弹出密码框", calls["n"] == 1)
    # 等第一个实例真正进入放行会话（否则第二个实例会被当成「会话外首次打开」而再次要密码）
    check("S3：首个实例进入放行会话", wait_unlocked(engine, target))
    # 必须是「真的活着」——目标若秒退，多开场景就没有意义了
    check("S3：第一个实例存活", pid1 > 0 and wait_alive(pid1, 0.6), f"pid={pid1}")

    before = calls["n"]
    pid2, _ = spawn_external(target)
    # 第二个实例应被并进同一放行会话：既不弹密码框，也不被冻结/结束
    alive2 = bool(pid2) and wait_alive(pid2, 1.0)
    check("S3：会话仍在运行时不再重复索要密码", calls["n"] == before,
          f"密码框 {before} -> {calls['n']}")
    check("S3：新实例同样被放行且存活", alive2, f"pid={pid2}")
    kill_pid(pid1)
    kill_pid(pid2)
    engine.stop()


def scenario_ui_launch(tmp: Path) -> None:
    print("\n[S4] 软件锁界面「启动」发起（应免密）", flush=True)
    store, engine, calls = make_engine(tmp, "uilaunch")
    target = make_target(tmp, "locktest_ui.exe")
    app = store.add_app(str(target))
    engine.start()
    time.sleep(1.0)

    ok, message = engine.launch(app)
    check("S4：界面启动返回成功", ok, message)
    time.sleep(1.5)
    check("S4：界面启动不索要密码", calls["n"] == 0, f"密码框调用 {calls['n']} 次")
    check("S4：目标进入放行集合", engine.is_unlocked(str(target)))
    for proc in PROCS:
        try:
            if proc.poll() is None:
                winsys.terminate_tree(proc.pid)
        except Exception:
            pass
    engine.stop()


def scenario_handoff(tmp: Path) -> None:
    print("\n[S5] 免密来源判定（谓词级：外部双击 vs 应用自身交接 vs 界面启动）", flush=True)
    store, engine, calls = make_engine(tmp, "handoff")
    target = make_target(tmp, "locktest_handoff.exe")
    store.add_app(str(target))
    key = config_store.norm_path(str(target))
    # 故意不启动扫描线程，避免后台线程干扰判定
    now = time.time()

    pid, bridge = spawn_external(target)
    ppid = winsys.parent_pid(pid)
    check("S5：目标父进程不是软件锁进程本身（确实是外部启动）",
          ppid not in (0, os.getpid()), f"ppid={ppid}")

    check("S5：外部双击（父进程是 shell）-> 必须输密码",
          not engine._is_trusted_origin(key, [pid], now))

    with engine._lock:
        engine._resigned[key] = {ppid: now}
    check("S5：父进程是刚退出的放行进程 -> 免密（应用自身交接）",
          engine._is_trusted_origin(key, [pid], now))

    with engine._lock:
        engine._resigned.pop(key, None)
        engine._ui_intent[key] = now + 5.0
    check("S5：界面「启动」握手窗口内 -> 免密",
          engine._is_trusted_origin(key, [pid], now))

    with engine._lock:
        engine._ui_intent[key] = now - 1.0        # 窗口已过期
    check("S5：界面握手窗口过期后 -> 必须输密码",
          not engine._is_trusted_origin(key, [pid], now))

    kill_pid(pid)


def scenario_pid_reuse(tmp: Path) -> None:
    print("\n[S6] pid 被复用（create_time 变化）后不得继续放行", flush=True)
    store, engine, calls = make_engine(tmp, "pidreuse")
    target = make_target(tmp, "locktest_reuse.exe")
    store.add_app(str(target))
    engine.start()
    time.sleep(1.0)

    pid1, _ = spawn_external(target)
    wait_prompt(calls, 0)
    # 先等放行真正落账，再断言（request_auth 在 _approve 之前返回，需给引擎一点时间）
    wait_unlocked(engine, target, pid1)
    check("S6：已进入放行集合", engine.is_unlocked(str(target)))

    # 模拟「同名 pid 被另一个进程占用」：create_time 不再匹配
    with engine._lock:
        engine._known_pids[pid1] = (engine._known_pids.get(pid1, 0.0) or 0.0) + 999.0
    t0 = time.time()
    while time.time() - t0 < 2.0 and engine.is_unlocked(str(target)):
        time.sleep(0.02)
    check("S6：身份不符时放行被注销", not engine.is_unlocked(str(target)),
          f"{(time.time() - t0) * 1000:.0f} ms")
    kill_pid(pid1)
    engine.stop()


def scenario_windowless_zombie(tmp: Path) -> None:
    """S8：本次用户报的漏洞——进程活着但没有任何窗口，不得给放行会话续命。"""
    print("\n[S8] 无窗口僵尸进程不给放行会话续命（服务型 / 关窗留后台的应用）", flush=True)
    ic.STARTUP_GRACE = 2.0
    ic.WINDOWLESS_TTL = 0.5
    ic.WINDOW_CHECK_INTERVAL = 0.2
    try:
        store, engine, calls = make_engine(tmp, "zombie")
        target = make_target(tmp, "locktest_zombie.exe")
        store.add_app(str(target))
        engine.start()
        time.sleep(1.0)

        pid1, _ = spawn_external(target)      # 继承控制台 -> 目标自身无窗口
        wait_prompt(calls, 0)
        check("S8：首次打开弹出密码框", calls["n"] == 1)
        check("S8：进入放行会话", wait_unlocked(engine, target))
        check("S8：目标进程确实没有可见窗口", not winsys._windows_of_pid(pid1),
              f"pid={pid1}")

        # 进程一直活着，但超过 STARTUP_GRACE + WINDOWLESS_TTL 仍无窗口 -> 应注销
        time.sleep(4.0)
        check("S8：僵尸进程仍然存活", wait_alive(pid1, 0.2), f"pid={pid1}")
        check("S8：无窗口僵尸不再维持放行", not engine.is_unlocked(str(target)))

        # 注销后重开 -> 必须重新要密码（这才是用户期望的行为）
        before = calls["n"]
        pid2, _ = spawn_external(target)
        wait_prompt(calls, before, timeout=6.0)
        check("S8：堵住续命后重开必须重新要密码", calls["n"] == before + 1,
              f"密码框 {before} -> {calls['n']}")
        kill_pid(pid1)
        kill_pid(pid2)
        engine.stop()
    finally:
        ic.STARTUP_GRACE = 15.0
        ic.WINDOWLESS_TTL = 3.0
        ic.WINDOW_CHECK_INTERVAL = 1.0


def scenario_windowed_session(tmp: Path) -> None:
    """S9：正常应用（有自己的可见窗口）不被无窗口规则误伤。"""
    print("\n[S9] 会话内有可见窗口时不被无窗口规则注销（正常应用不受影响）", flush=True)
    ic.STARTUP_GRACE = 2.0
    ic.WINDOWLESS_TTL = 0.5
    ic.WINDOW_CHECK_INTERVAL = 0.2
    try:
        store, engine, calls = make_engine(tmp, "windowed")
        target = make_target(tmp, "locktest_windowed.exe")
        store.add_app(str(target))
        engine.start()
        time.sleep(1.0)

        pid1, _ = spawn_external(target, new_console=True)   # 独立控制台 -> 有可见窗口
        wait_prompt(calls, 0)
        check("S9：首次打开弹出密码框", calls["n"] == 1)
        check("S9：进入放行会话", wait_unlocked(engine, target))
        # _approve 里 is_unlocked 在锁内置位、窗口恢复在锁外，这里轮询等窗口真正恢复
        t0 = time.time()
        while time.time() - t0 < 3.0 and not winsys._windows_of_pid(pid1):
            time.sleep(0.05)
        check("S9：目标持有可见窗口", bool(winsys._windows_of_pid(pid1)), f"pid={pid1}")

        time.sleep(4.0)     # 远超 STARTUP_GRACE + WINDOWLESS_TTL
        check("S9：进程仍然存活", wait_alive(pid1, 0.2), f"pid={pid1}")
        check("S9：有窗口的正常会话保持放行", engine.is_unlocked(str(target)))
        kill_pid(pid1)
        engine.stop()
    finally:
        ic.STARTUP_GRACE = 15.0
        ic.WINDOWLESS_TTL = 3.0
        ic.WINDOW_CHECK_INTERVAL = 1.0


def scenario_deny(tmp: Path) -> None:
    print("\n[S7] 未通过密码：进程被结束且不得进入放行集合", flush=True)
    store, engine, calls = make_engine(tmp, "deny", approve=False)
    target = make_target(tmp, "locktest_deny.exe")
    store.add_app(str(target))
    engine.start()
    time.sleep(1.0)

    pid, _ = spawn_external(target)
    wait_prompt(calls, 0)
    check("S7：弹出密码框", calls["n"] == 1)
    # 目标 pid 以引擎回调告知的为准（进程被冻结后外部枚举可能已错过）
    target_pid = pid or (calls["pids"][0] if calls["pids"] else 0)
    check("S7：拿到被拦截的目标进程 pid", bool(target_pid), f"pid={target_pid}")
    check("S7：被拒绝后进程结束", wait_dead(target_pid, 6.0), f"pid={target_pid}")
    check("S7：未进入放行集合", not engine.is_unlocked(str(target)))
    engine.stop()


def main() -> int:
    # 整体超时保护：万一某处卡住，打印调用栈并退出，而不是无限期挂着
    faulthandler.dump_traceback_later(180, exit=True)

    tmp = Path(tempfile.mkdtemp(prefix="softlock-unlock-"))
    print(f"临时目录: {tmp}", flush=True)
    try:
        scenario_close_reopen(tmp, 0.2, "S1")
        scenario_close_reopen(tmp, 1.5, "S2")
        scenario_running_multiopen(tmp)
        scenario_ui_launch(tmp)
        scenario_handoff(tmp)
        scenario_pid_reuse(tmp)
        scenario_windowless_zombie(tmp)
        scenario_windowed_session(tmp)
        scenario_deny(tmp)
    finally:
        faulthandler.cancel_dump_traceback_later()
        for proc in PROCS:
            try:
                if proc.poll() is None:
                    winsys.terminate_tree(proc.pid)
            except Exception:
                pass
        # 兜底：结束一切「exe 落在本次临时目录里」的进程（含界面启动拉起的那些）
        try:
            import psutil
            root = str(tmp).lower()
            for proc in psutil.process_iter(["pid", "exe"]):
                try:
                    exe = (proc.info.get("exe") or "").lower()
                    if exe.startswith(root):
                        winsys.terminate_tree(proc.pid)
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(0.5)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 52, flush=True)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}", flush=True)
        return 1
    print("全部通过 OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
