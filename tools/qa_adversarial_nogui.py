# -*- coding: utf-8 -*-
"""QA 第四轮对抗用例（不需要界面）：C1 提权循环 / C2 单实例 / C3 自启动降级。

运行：python tools/qa_adversarial_nogui.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config_store  # noqa: E402
import winsys  # noqa: E402

PY = sys.executable
RESULTS: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    RESULTS.append((name, bool(ok), detail))
    return bool(ok)


HOLDER_SRC = (
    "import sys; sys.path.insert(0, r'{src}'); import winsys; "
    "s=winsys.SingleInstance(); print('CHILD', s.acquire(), flush=True); "
    "import time; time.sleep(60)"
).format(src=str(ROOT / "src"))


def start_mutex_holder():
    p = subprocess.Popen([PY, "-c", HOLDER_SRC], cwd=str(ROOT),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = p.stdout.readline().strip()
    return p, line


# ============================================================ C1
def test_c1():
    print("\n[C1] 提权循环：互斥体被他人持有时 acquire_retry 的超时与返回", flush=True)
    holder, line = start_mutex_holder()
    try:
        check("子进程成功持有互斥体", line == "CHILD True", line)
        time.sleep(0.4)
        s = winsys.SingleInstance()
        got = s.acquire()
        s.release()
        check("互斥体被占用时 acquire() 返回 False", got is False, f"{got}")
        check("instance_running() 检测为已在运行", winsys.instance_running() is True)

        t0 = time.time()
        s2 = winsys.SingleInstance()
        ok = s2.acquire_retry(retries=20, delay=0.2)
        elapsed = time.time() - t0
        s2.release()
        check("acquire_retry 最终返回 False", ok is False, f"{ok}")
        check("acquire_retry 耗时约 4 秒（20 次 × 0.2s）", 3.0 <= elapsed <= 5.5, f"{elapsed:.2f}s")

        # 短重试次数校验
        t0 = time.time()
        s3 = winsys.SingleInstance()
        ok3 = s3.acquire_retry(retries=3, delay=0.1)
        el3 = time.time() - t0
        s3.release()
        check("acquire_retry(retries=3) 也返回 False 且更快", ok3 is False and el3 < 1.5,
              f"{ok3} {el3:.2f}s")
    finally:
        try:
            holder.terminate()
            holder.wait(timeout=5)
        except Exception:
            pass
    time.sleep(0.6)
    check("持有者退出后互斥体被释放", winsys.instance_running() is False)


# ============================================================ C2
def test_c2():
    print("\n[C2] 单实例未被破坏：第二实例只写 SHOW_FLAG 并退出", flush=True)
    if config_store.SHOW_FLAG.exists():
        try:
            config_store.SHOW_FLAG.unlink()
        except Exception:
            pass
    holder, line = start_mutex_holder()
    try:
        check("子进程成功持有互斥体", line == "CHILD True", line)
        time.sleep(0.4)
        t0 = time.time()
        p = subprocess.run([PY, str(ROOT / "src" / "main.py"), "--no-elevate", "--no-guard"],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=25)
        el = time.time() - t0
        check("第二实例退出码为 0", p.returncode == 0, f"rc={p.returncode} stderr={p.stderr[-200:]!r}")
        check("第二实例在 5 秒内退出（未真正启动）", el < 5.0, f"{el:.2f}s")
        check("第二实例写入了 SHOW_FLAG", config_store.SHOW_FLAG.exists())
        check("第二实例未写 EXIT_FLAG", not config_store.EXIT_FLAG.exists())
        # 确认没有产生第二个长期存活的 main.py 进程
        import psutil
        others = []
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cl = proc.info.get("cmdline") or []
                if any("main.py" in str(x) for x in cl) and proc.pid != os.getpid():
                    others.append(proc.pid)
            except Exception:
                pass
        check("没有残留第二个主进程", not others, str(others))
    finally:
        try:
            holder.terminate()
            holder.wait(timeout=5)
        except Exception:
            pass
        try:
            if config_store.SHOW_FLAG.exists():
                config_store.SHOW_FLAG.unlink()
        except Exception:
            pass


# ============================================================ C3
def _restore_run_value(value):
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, winsys.RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        if value is None:
            try:
                winreg.DeleteValue(k, winsys.RUN_VALUE)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(k, winsys.RUN_VALUE, 0, winreg.REG_SZ, value)


def test_c3():
    print("\n[C3] set_autostart_task 降级路径 + 注册表回退（未提权）", flush=True)
    orig = winsys.get_autostart()
    print(f"        测试前 HKCU Run = {orig!r}", flush=True)
    try:
        raised = None
        ret = None
        try:
            ret = winsys.set_autostart_task(True)
        except Exception as exc:
            raised = exc
        check("set_autostart_task(True) 不抛异常", raised is None, repr(raised))
        check("未提权/schtasks 不可用时 set_autostart_task 返回 False", ret is False, f"{ret}")

        exe = str(ROOT / "dist" / "软件锁.exe")
        ok = winsys.set_autostart(True, exe)
        check("set_autostart(True) 回退到注册表并返回 True", ok is True, f"{ok}")
        val = winsys.get_autostart()
        check("HKCU Run 项已写入源码运行版本", bool(val) and "软件锁.exe" in val, repr(val))

        ok2 = winsys.set_autostart(False, exe)
        check("set_autostart(False) 返回 True", ok2 is True, f"{ok2}")
        check("HKCU Run 项已删除", winsys.get_autostart() is None, repr(winsys.get_autostart()))
    finally:
        _restore_run_value(orig)
        print(f"        已还原 HKCU Run = {winsys.get_autostart()!r}", flush=True)
        check("自启动状态已还原到测试前", winsys.get_autostart() == orig)


def main():
    test_c1()
    test_c2()
    test_c3()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 56)
    print(f"对抗用例（非界面）：{passed}/{total} 通过", flush=True)
    fails = [n for n, ok, _ in RESULTS if not ok]
    if fails:
        print(f"失败项: {fails}", flush=True)
        return 1
    print("全部通过 OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
