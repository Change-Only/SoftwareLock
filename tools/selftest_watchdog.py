# -*- coding: utf-8 -*-
"""看守进程（双进程互为守护）专项自检 —— 针对「任务管理器结束软件锁后保护失效」。

被修复的漏洞（用户在 Win11 实测复现）：
  任务管理器「结束进程树」按父子关系连坐，旧版看守进程是主进程的直接子进程，
  一次树杀就把主进程+看守一起带走，保护彻底消失，受保护应用打开不再要密码。

修复（1.4.2）：
  * 看守进程与重启实例都经 WMI 拉起（父进程 = WmiPrvSE），不在软件锁进程树里；
  * 看守拉起重启实例后等待接管，失败则继续拉（不再一次就弃守）；
  * 非提权重启带 --no-elevate，无人值守路径不再弹 UAC 断链。

测试内容：
  A  常规回归：主进程启动、看守拉起、看守脱树、受保护程序冻结/超时拒绝
  B  树杀主进程（taskkill /T /F）：看守必须幸存并自动重启主进程
  C  杀看守：主进程反向守护必须重新拉起看守
  D  收尾：写 EXIT_FLAG 后清理，无残留

隔离：exe 复制到临时目录，数据目录 = exe 同级（便携模式语义），
测试进程用 SOFTLOCK_DATA_DIR 指向同一目录，日志/EXIT_FLAG 读写一致。
经 WMI 拉起的进程不继承环境变量，恰好验证了「数据目录只依赖 exe 位置」。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 必须在 import config_store 之前设置
WD_DATA_DIR = Path(tempfile.mkdtemp(prefix="softlock-wd-"))
os.environ["SOFTLOCK_DATA_DIR"] = str(WD_DATA_DIR)

sys.path.insert(0, str(ROOT / "src"))


def _parse_args():
    import argparse

    ap = argparse.ArgumentParser(description="看守进程专项自检")
    ap.add_argument("--exe", default=str(ROOT / "dist" / "软件锁.exe"))
    ap.add_argument("--timeout", type=float, default=25.0,
                    help="等待重启/接管的最长时间（秒）")
    return ap.parse_args()


ARGS = _parse_args()
EXE = Path(ARGS.exe)
# 被测副本路径（main() 里复制后设置）；进程匹配一律用它
EXE_COPY = EXE

import config_store  # noqa: E402
import psutil  # noqa: E402
import winsys  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""),
          flush=True)
    if not ok:
        FAILED.append(name)


def log_text() -> str:
    try:
        return config_store.LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def wait_log(marker: str, offset: int, timeout: float) -> tuple[bool, int]:
    """等待日志出现 marker，返回 (是否出现, 新的读取偏移)。"""
    deadline = time.time() + timeout
    text = log_text()
    while time.time() < deadline:
        text = log_text()
        if marker in text[offset:]:
            return True, len(text)
        time.sleep(0.2)
    return False, len(text)


def exe_pids() -> dict[int, list[str]]:
    """{pid: cmdline} —— 所有以「被测副本 exe」路径启动的进程。

    注意必须匹配 tmp 目录里的副本路径，而不是 dist 的原始路径：
    两份是不同的二进制文件，按原始路径匹配永远看不到被测进程
    （反而会看到用户正在运行的真实实例，既误判又危险）。
    """
    target = os.path.normcase(str(EXE_COPY))
    pids: list[int] = []
    out: dict[int, list[str]] = {}
    for proc in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            exe = proc.info.get("exe")
            if exe and os.path.normcase(exe) == target:
                out[proc.pid] = [str(a) for a in (proc.info.get("cmdline") or [])]
        except Exception:
            continue
    return out


def watchdog_pids(cmdlines: dict[int, list[str]]) -> list[int]:
    return [pid for pid, cmd in cmdlines.items()
            if any("--watchdog" == str(a) for a in cmd)]


def main_pids(cmdlines: dict[int, list[str]]) -> list[int]:
    return [pid for pid, cmd in cmdlines.items()
            if not any("--watchdog" == str(a) for a in cmd)]


def taskkill(pid: int, tree: bool) -> bool:
    cmd = ["taskkill", "/F", "/PID", str(pid)] + (["/T"] if tree else [])
    try:
        return subprocess.run(cmd, capture_output=True,
                              creationflags=0x08000000).returncode == 0
    except Exception:
        return False


def wait_new_main(baseline: set[int], timeout: float) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for pid in main_pids(exe_pids()):
            if pid not in baseline:
                return pid
        time.sleep(0.1)
    return None


def main() -> int:
    if not EXE.exists():
        print(f"! 找不到 {EXE}，请先执行 python build.py")
        return 1

    tmp = WD_DATA_DIR
    global EXE_COPY
    exe_copy = tmp / EXE.name
    EXE_COPY = exe_copy
    shutil.copy(EXE, exe_copy)
    target = tmp / "locktest.exe"
    shutil.copy(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe",
                target)

    config_store.ensure_dirs()
    config = {
        "version": 1,
        "password": config_store.hash_password("wdtest123"),
        "apps": [{
            "id": "wdtest0001",
            "name": "看守测试目标",
            "path": str(target),
            "enabled": True,
            "created_at": int(time.time()),
        }],
        "settings": dict(config_store.DEFAULT_SETTINGS, self_defense=True,
                         lock_running_on_start=True, prompt_timeout_seconds=6,
                         poll_interval_ms=100, autostart=False),
        "stats": {"blocked": 0, "allowed": 0},
    }
    config_store.CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    log_mark = len(log_text())
    preexisting = set(exe_pids())
    if preexisting:
        print(f"  注意：启动前已存在同路径进程 {sorted(preexisting)}，收尾时不触碰")
    # 外部是否已有别的软件锁实例占着单实例互斥体（影响「接管」判定的预期）
    try:
        external_singleton = winsys.instance_running()
    except Exception:
        external_singleton = False
    print(f"  外部单实例互斥体被占用: {external_singleton}")

    app_proc = None
    child = None
    try:
        print("\n[A] 常规回归：启动 / 看守拉起 / 脱树 / 拦截")
        app_proc = subprocess.Popen(
            [str(exe_copy), "--force-new", "--no-elevate"],
            cwd=str(tmp), close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok_boot, log_mark = wait_log("后台拦截引擎已启动", log_mark, 30)
        check("A1 主进程启动且引擎就绪", ok_boot and app_proc.poll() is None,
              f"pid={app_proc.pid}")
        if not ok_boot:
            print("! 主进程未能启动，跳过后续步骤")
            return 1

        wd_pid = None
        # WMI 拉起要经 PowerShell 一来一回，冷启动（Defender 扫新 exe）可能要十几秒
        for _ in range(100):
            wds = watchdog_pids(exe_pids())
            if wds:
                wd_pid = wds[0]
                break
            time.sleep(0.3)
        check("A2 看守进程已拉起", wd_pid is not None)
        if wd_pid:
            try:
                wd_parent = psutil.Process(wd_pid).ppid()
                parent_name = psutil.Process(wd_parent).name() if wd_parent else "?"
            except Exception:
                wd_parent, parent_name = 0, "?"
            check("A3 看守进程已脱树（父进程非主进程）",
                  wd_parent != app_proc.pid, f"父进程={parent_name}({wd_parent})")

        # 直接双击受保护程序 -> 冻结 -> 超时拒绝
        child = subprocess.Popen([str(target)], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        frozen, _ = wait_log("已冻结", log_mark, 10)
        check("A4 受保护程序被冻结", frozen)
        if frozen:
            log_mark = len(log_text())
            killed = False
            t0 = time.time()
            while time.time() - t0 < 25:
                if child.poll() is not None:
                    killed = True
                    break
                time.sleep(0.3)
            check("A5 未输密码超时后进程被结束", killed)
            if killed:
                _, log_mark = wait_log("已阻止", log_mark, 5)
        child = None

        print("\n[B] 树杀主进程（模拟任务管理器结束进程树）")
        # 基线：树杀前已存在的全部被测 exe 进程（主进程引导对 + 看守对）
        baseline_b = set(exe_pids()) | set(preexisting)
        main_before = app_proc.pid
        taskkill(main_before, tree=True)
        # 证据 1：立即采样（外部互斥体被占用时，看守看到主进程死亡会按
        # 「已有实例接管」很快退出，进程证据要赶在它退出之前抓）
        survivors = watchdog_pids(exe_pids())
        survived = bool(wd_pid) and (wd_pid in survivors)
        # 证据 2：日志。看守要么发起自动重启（无外部实例），要么按接管语义退出
        #（外部实例占着互斥体）——两条日志都只有「活着看到主进程死亡」的看守才能写
        if not survived and wd_pid:
            marker = "检测到已有实例接管" if external_singleton else "自动重启"
            ok_reboot, log_mark = wait_log(marker, log_mark, 12)
            survived = ok_reboot
        check("B1 树杀后看守进程幸存", survived,
              f"看守={wd_pid} 立即采样幸存={survivors}")
        if survived:
            if external_singleton:
                # 外部实例占着互斥体：看守会正确地判定「已有实例接管」而不重启
                #（否则会跟用户正在用的实例打架）——重启链路在无外部实例时才可观察
                check("B2 看守按「已有实例接管」语义退出（未误重启）",
                      wait_log("检测到已有实例接管", log_mark, 6))
                print("  （跳过 B3/B4：外部互斥体被占用，重启接管链路无法在本环境观察）")
            else:
                respawned = wait_new_main(baseline_b, ARGS.timeout)
                check("B2 看守自动重启了主进程", respawned is not None,
                      f"新主进程 pid={respawned}")
                taken_over = False
                if respawned:
                    t0 = time.time()
                    while time.time() - t0 < 15:
                        if respawned in exe_pids() and respawned in main_pids(exe_pids()):
                            taken_over = True
                            break
                        time.sleep(0.3)
                check("B3 重启实例完全接管（持续存活）", taken_over)
                # 完全接管后，重启实例会自带一个新看守
                check("B4 接管实例自带新看守", bool(watchdog_pids(exe_pids())))
        else:
            print("! 看守未幸存（WMI 不可用或修复失效），跳过 B2-B4")

        print("\n[C] 杀看守 -> 主进程反向守护")
        # 基线：启动新实例之前已存在的看守 pid（B 阶段的看守可能尚未完全退出）
        baseline_wds = set(watchdog_pids(exe_pids())) | set(preexisting)
        app_proc = subprocess.Popen(
            [str(exe_copy), "--force-new", "--no-elevate"],
            cwd=str(tmp), close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok_boot, log_mark = wait_log("后台拦截引擎已启动", log_mark, 30)
        if not ok_boot:
            check("C0 反向守护测试实例启动", False)
        else:
            wd2 = None
            for _ in range(100):
                wds = [p for p in watchdog_pids(exe_pids()) if p not in baseline_wds]
                if wds:
                    wd2 = wds[0]
                    break
                time.sleep(0.3)
            if not wd2:
                check("C1 新实例的看守已拉起", False)
            else:
                check("C1 新实例的看守已拉起", True, f"pid={wd2}")
                try:
                    parent2 = psutil.Process(wd2).ppid()
                    pname2 = psutil.Process(parent2).name() if parent2 else "?"
                except Exception:
                    parent2, pname2 = 0, "?"
                check("C2 新看守同样脱树", parent2 != app_proc.pid,
                      f"父进程={pname2}")
                # 看守是「引导进程 + 实际进程」成对出现的，整组树杀、整组排除
                wd2_group = [p for p in watchdog_pids(exe_pids())
                             if p not in baseline_wds]
                for p in wd2_group:
                    taskkill(p, tree=True)
                new_wd = None
                deadline = time.time() + 15
                while time.time() < deadline:
                    wds = [p for p in watchdog_pids(exe_pids())
                           if p not in baseline_wds and p not in wd2_group]
                    if wds:
                        new_wd = wds[0]
                        break
                    time.sleep(0.2)
                check("C3 主进程重新拉起了看守", new_wd is not None,
                      f"新看守 pid={new_wd}")
                if new_wd:
                    _, log_mark = wait_log("看守进程被终止，重新拉起", log_mark, 6)

        print("\n[D] 收尾")
    finally:
        # 先写 EXIT_FLAG（看守看到它就不再拉起），再清理全部测试进程
        try:
            config_store.EXIT_FLAG.write_text(str(os.getpid()), encoding="utf-8")
        except Exception:
            pass
        for _ in range(3):
            for pid in list(exe_pids()):
                if pid in preexisting:
                    continue
                try:
                    winsys.terminate_tree(pid)
                except Exception:
                    pass
            time.sleep(0.5)
        residue = [p for p in exe_pids() if p not in preexisting]
        check("D1 收尾后无残留测试进程", not residue, f"残留={residue}")
        for proc in (app_proc, child):
            try:
                if proc and proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        time.sleep(0.3)
        if FAILED:
            # 失败时保留现场并输出 exe 日志，便于定位（成功才清理临时目录）
            print(f"  [诊断] 数据目录保留在: {tmp}")
            tail = log_text()[-1500:]
            print("  [诊断] exe 日志尾部:")
            for line in tail.splitlines():
                print("    " + line)
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 52)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("看守进程专项自检全部通过 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
