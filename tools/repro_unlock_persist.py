# -*- coding: utf-8 -*-
"""复现 / 验证：受保护应用关闭后，再次启动是否仍需密码。

用户报告路径：
    软件锁前台关闭（后台继续保护）
      -> 第一次打开受保护应用：需要密码   （符合预期）
      -> 关闭该应用
      -> 再次打开：不需要密码了          （Bug！）

本脚本用真实 Store + 真实 Interceptor + 真实 exe（cmd.exe 副本）跑，
并把引擎内部状态（_pid_path / _unlocked / alive）逐帧打印出来。
"""
from __future__ import annotations

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


def check(name: str, ok: bool, detail: str = ""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    if not ok:
        FAILED.append(name)


def state(engine: "ic.Interceptor") -> str:
    with engine._lock:
        unlocked = {Path(k).name: round(v, 2) for k, v in engine._unlocked.items()}
        pid_path = dict(engine._pid_path)
        alive = set(engine._pid_path.values())
    return f"_unlocked={unlocked} _pid_path={pid_path} alive={len(alive)}"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="softlock-repro-"))
    target = tmp / "locktest.exe"
    shutil.copy(CMD_SOURCE, target)
    print(f"临时目录: {tmp}\n受保护目标: {target}", flush=True)

    store = config_store.Store(tmp / "config.json")
    store.set_password("abc123456")
    app = store.add_app(str(target))
    print(f"已登记受保护应用: {app.get('name')} enabled={app.get('enabled')}", flush=True)

    calls = {"n": 0}

    def request_auth(apps, pids, reason="launch"):
        calls["n"] += 1
        print(f"      >>> 弹出密码框 #{calls['n']} apps={[a.get('name') for a in apps]} "
              f"pids={list(pids)} reason={reason}", flush=True)
        return True

    engine = ic.Interceptor(store, request_auth)
    engine.start()
    time.sleep(1.2)
    check("引擎已启动", engine.running)

    # ---------------- 第一次启动 ----------------
    print("\n[1] 第一次打开受保护应用（应要密码）", flush=True)
    proc1 = subprocess.Popen([str(target)], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < 10 and calls["n"] == 0:
        time.sleep(0.02)
    check("第一次启动弹出了密码框", calls["n"] == 1, f"{calls['n']} 次")
    time.sleep(0.8)
    check("放行后进程存活", proc1.poll() is None)
    check("路径进入已解锁集合", engine.is_unlocked(str(target)))
    print(f"    状态: {state(engine)}", flush=True)

    # ---------------- 关闭应用 ----------------
    print("\n[2] 关闭受保护应用（模拟用户关掉它）", flush=True)
    proc1.kill()
    try:
        proc1.wait(timeout=5)
    except Exception:
        pass
    print(f"    进程已结束 rc={proc1.poll()}", flush=True)

    # 观察引擎对「进程消失」的感知
    import psutil

    for i in range(0, 8):
        time.sleep(1.0)
        print(f"    +{i + 1}s  pid={proc1.pid} 仍在psutil={proc1.pid in psutil.pids()} "
              f"状态: {state(engine)}", flush=True)

    # ---------------- 再次启动 ----------------
    print("\n[3] 关闭后再次打开受保护应用（应再次要密码）", flush=True)
    before = calls["n"]
    proc2 = subprocess.Popen([str(target)], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < 6 and calls["n"] == before:
        time.sleep(0.02)
    hit = calls["n"] > before
    check("关闭后再次打开会重新索要密码", hit,
          f"密码框调用 {before} -> {calls['n']}")
    if not hit:
        print(f"    !! 未要求密码。当前状态: {state(engine)}", flush=True)
    time.sleep(0.8)

    engine.stop()
    for p in (proc1, proc2):
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass
    try:
        winsys.terminate_tree(proc2.pid)
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 52, flush=True)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}", flush=True)
        return 1
    print("全部通过 OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
