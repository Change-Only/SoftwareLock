# -*- coding: utf-8 -*-
"""打包产物（dist/软件锁.exe）端到端冒烟测试。

流程：
  1. 把数据目录隔离到临时目录（SOFTLOCK_DATA_DIR），写入一份测试配置
  2. 以真实 exe 启动软件锁
  3. 确认进程存活、日志出现主窗口就绪
  4. 直接启动一个受保护程序（cmd.exe 的副本），模拟用户双击
  5. 确认它被冻结、弹出鉴权、超时后按拒绝处理（进程被结束）
  6. 收尾：结束软件锁进程；断言 exe 同级数据目录**未被动过**

隔离很重要：软件锁的数据目录现在是「exe 同级」，如果测试直接写
dist/config.json，用户切到新版时会被判定为「已有配置」而跳过 %APPDATA%
旧数据迁移 —— 用户会丢密码和已加锁列表。历史事故就出在这里。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 必须在 import config_store 之前设置：模块级路径常量在导入时就解析定了
SMOKE_DATA_DIR = Path(tempfile.mkdtemp(prefix="softlock-smoke-data-"))
os.environ["SOFTLOCK_DATA_DIR"] = str(SMOKE_DATA_DIR)

sys.path.insert(0, str(ROOT / "src"))


def _parse_args():
    import argparse

    ap = argparse.ArgumentParser(description="打包产物端到端冒烟测试")
    ap.add_argument("--exe", default=str(ROOT / "dist" / "软件锁.exe"),
                    help="被测 exe 路径（默认 dist/软件锁.exe）；可指向某份构建副本，"
                         "这样即使已有实例在运行也能验证新构建")
    ap.add_argument("--force-new", action="store_true",
                    help="给被测 exe 追加 --force-new：允许在已有软件锁实例运行时"
                         "启动这份新构建（单实例互斥体被绕过）")
    return ap.parse_args()


ARGS = _parse_args()
EXE = Path(ARGS.exe)

import config_store  # noqa: E402
import psutil  # noqa: E402
import winsys  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    if not ok:
        FAILED.append(name)


def _dir_signature(path: Path) -> str:
    """目录内所有文件的相对路径 + 内容哈希，用于断言「没被动过」。"""
    digest = hashlib.sha256()
    if not path.exists():
        return "<absent>"
    for item in sorted(path.rglob("*")):
        if item.is_file():
            digest.update(str(item.relative_to(path)).encode("utf-8", "replace"))
            try:
                digest.update(item.read_bytes())
            except Exception:
                digest.update(b"<unreadable>")
    return digest.hexdigest()


def _enumerate_exe_pids(exe_path: Path) -> list[int]:
    """枚举所有「可执行文件完整路径」等于 exe_path 的进程 pid。"""
    target = os.path.normcase(str(exe_path))
    pids: list[int] = []
    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            exe = proc.info.get("exe")
        except Exception:
            continue
        if exe and os.path.normcase(exe) == target:
            pids.append(proc.pid)
    return pids


def _apply_pid_tree(pid: int, action) -> None:
    """对某进程及其全部子进程执行 action（terminate / kill），逐个吞异常。"""
    try:
        proc = psutil.Process(pid)
    except Exception:
        return
    try:
        for child in proc.children(recursive=True):
            try:
                action(child)
            except Exception:
                pass
    except Exception:
        pass
    try:
        action(proc)
    except Exception:
        pass


def _kill_lock_processes(exe_path: Path, preexisting: set,
                         rounds: int = 10, interval: float = 0.4) -> list[int]:
    """终止该 exe 的所有进程（排除启动前已存在的 pid），返回仍残留的 pid 列表。

    软件锁是双进程互为守护（主进程 + 看守进程），而且看守进程在检测到主进程消失后
    会把它重新拉起，所以必须按「exe 完整路径」枚举出全部相关进程、连同子进程一起清，
    并在 ACTION 前先写 EXIT_FLAG 通知看守进程别再重启。
    """
    def remaining() -> list[int]:
        return [p for p in _enumerate_exe_pids(exe_path) if p not in preexisting]

    # 第一轮：优雅 terminate（含子进程），轮询等待退出
    for pid in remaining():
        _apply_pid_tree(pid, lambda p: p.terminate())
    for _ in range(rounds):
        time.sleep(interval)
        if not remaining():
            return []
    # 第二轮：对顽固不退出的进程强杀
    for pid in remaining():
        _apply_pid_tree(pid, lambda p: p.kill())
    for _ in range(rounds):
        time.sleep(interval)
        if not remaining():
            return []
    return remaining()


def main() -> int:
    if not EXE.exists():
        print(f"! 找不到 {EXE}，请先执行 python build.py")
        return 1

    # 记录 exe 同级真实数据目录的指纹：测试结束后必须一字未改
    real_data_dir = config_store.app_dir_for(EXE.parent)
    real_sig_before = _dir_signature(real_data_dir)

    # 数据目录必须已隔离到临时目录，否则说明隔离失效，直接拒绝运行
    if Path(config_store.APP_DIR).resolve() != SMOKE_DATA_DIR.resolve():
        print(f"! 数据目录隔离失败: {config_store.APP_DIR} != {SMOKE_DATA_DIR}")
        return 1
    print(f"隔离数据目录: {config_store.APP_DIR}")

    # 先做「用户实例在跑吗」的判断再动手——任何写盘动作之前就决定是否跳过，
    # 保证跳过路径不会留下任何测试残留（历史事故：跳过时把测试配置留在 dist/）。
    preexisting = set(_enumerate_exe_pids(EXE))
    if preexisting:
        print(f"  注意：启动前已存在软件锁进程 {sorted(preexisting)}，收尾时不会触碰它们")
    try:
        already_running = winsys.instance_running()
    except Exception:
        already_running = False
    if already_running and preexisting and not ARGS.force_new:
        print("\n! 检测到已有软件锁实例在运行（可能正被用户使用）。")
        print("! 为了不影响真实使用，本次端到端冒烟测试跳过（SKIP，非失败）。")
        print("! 请关闭正在运行的软件锁后重新执行本测试；")
        print("! 或把新构建复制到别处，用 --exe <副本> --force-new 单独验证。")
        shutil.rmtree(SMOKE_DATA_DIR, ignore_errors=True)
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="softlock-smoke-"))
    target = tmp / "locktest.exe"
    shutil.copy(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe", target)

    config_store.ensure_dirs()
    config = {
        "version": 1,
        "password": config_store.hash_password("smoketest123"),
        "apps": [{
            "id": "smoke0001",
            "name": "冒烟测试目标",
            "path": str(target),
            "enabled": True,
            "created_at": int(time.time()),
        }],
        "settings": dict(config_store.DEFAULT_SETTINGS, self_defense=False,
                         lock_running_on_start=False, prompt_timeout_seconds=6,
                         poll_interval_ms=120, autostart=False),
        "stats": {"blocked": 0, "allowed": 0},
    }
    config_store.CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    # 注意：st_size 是字节数，而 read_text() 返回字符数，中文字符会让两者不一致，
    # 因此这里统一按字符数记录偏移。
    try:
        log_before = len(config_store.LOG_PATH.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        log_before = 0

    app = None
    child = None
    try:
        print("\n[1] 启动打包后的 exe")
        # 加 --no-elevate 跳过运行时提权，避免弹出 UAC 阻塞自动化测试；
        # SOFTLOCK_DATA_DIR 让被测 exe 也把数据写进隔离目录
        launch = [str(EXE), "--no-elevate"]
        if ARGS.force_new:
            launch.append("--force-new")
        child_env = dict(os.environ, SOFTLOCK_DATA_DIR=str(SMOKE_DATA_DIR))
        app = subprocess.Popen(launch, cwd=str(EXE.parent),
                               env=child_env, close_fds=True)
        ok_boot = False
        for _ in range(60):
            time.sleep(0.5)
            if app.poll() is not None:
                break
            try:
                text = config_store.LOG_PATH.read_text(encoding="utf-8", errors="replace")
            except Exception:
                text = ""
            if "后台拦截引擎已启动" in text[log_before:] or "以最小化" in text[log_before:]:
                ok_boot = True
                break
        check("exe 进程存活", app.poll() is None, f"pid={app.pid} rc={app.poll()}")
        check("日志显示界面已就绪", ok_boot)

        if app.poll() is not None:
            print("! exe 启动失败，跳过后续步骤")
            return 1

        # 确认软件锁自己不会被误伤
        time.sleep(1.5)

        print("\n[2] 模拟用户直接双击受保护程序")
        child = subprocess.Popen([str(target)], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        frozen = False
        t0 = time.time()
        while time.time() - t0 < 8:
            time.sleep(0.2)
            try:
                tail = config_store.LOG_PATH.read_text(encoding="utf-8", errors="replace")[log_before:]
            except Exception:
                tail = ""
            if "已冻结" in tail:
                frozen = True
                break
        check("受保护程序被冻结", frozen, f"{(time.time() - t0) * 1000:.0f} ms")

        # 超时（6 秒）后应自动按拒绝处理并结束进程
        t0 = time.time()
        killed = False
        while time.time() - t0 < 25:
            time.sleep(0.3)
            if child.poll() is not None:
                killed = True
                break
        check("未输入密码时进程被结束", killed, f"returncode={child.poll()}")

        # 日志写入发生在结束进程之后，给一点时间
        tail = ""
        for _ in range(20):
            try:
                tail = config_store.LOG_PATH.read_text(encoding="utf-8", errors="replace")[log_before:]
            except Exception:
                tail = ""
            if "已阻止" in tail:
                break
            time.sleep(0.2)
        check("日志记录阻止动作", "已阻止" in tail)

        print("\n[3] 进程仍在守护中")
        check("软件锁未受影响", app.poll() is None)
    finally:
        print("\n[4] 收尾")
        # 1) 先写 EXIT_FLAG：否则杀掉主进程后，看守进程会立刻把它重新拉起
        try:
            config_store.EXIT_FLAG.write_text(str(os.getpid()), encoding="utf-8")
        except Exception:
            pass
        # 2) 结束被测目标与被测软件锁主进程（含子进程）
        for proc in (child, app):
            try:
                if proc and proc.poll() is None:
                    winsys.terminate_tree(proc.pid)
            except Exception:
                pass
        # 3) 按 exe 完整路径兜底清理主进程 + 看守/守护子进程（排除启动前已存在的 pid）
        residue = _kill_lock_processes(EXE, preexisting)
        check("收尾后无残留软件锁进程", not residue,
              f"残留 pid={residue}" if residue else "")
        time.sleep(0.5)
        # 4) 断言没有污染 exe 同级的真实数据目录（用户升级新版时的迁移依赖它为空）
        check("未污染 exe 同级数据目录", _dir_signature(real_data_dir) == real_sig_before,
              str(real_data_dir))
        shutil.rmtree(SMOKE_DATA_DIR, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 52)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("端到端冒烟测试全部通过 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
