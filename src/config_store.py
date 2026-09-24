# -*- coding: utf-8 -*-
"""软件锁 —— 配置存储、密码校验与运行日志。

数据目录默认与「软件锁.exe」同级（便携模式）；若该目录不可写（例如放在
Program Files 下），自动回退到 %APPDATA%\\SoftwareLock。
密码以 PBKDF2-HMAC-SHA256(20 万次迭代 + 随机盐) 形式存储，不保存明文。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path

APP_NAME = "SoftwareLock"
APP_DISPLAY_NAME = "软件锁"
APP_VERSION = "1.4.1"

# 旧版数据目录（升级迁移用）
LEGACY_APP_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME

PBKDF2_ITERATIONS = 200_000
MAX_LOG_BYTES = 512 * 1024


def _writable_dir(path: Path) -> bool:
    """真实写探针（Windows 上 os.access 对目录的判定不可靠）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe.tmp"
        probe.write_text("ok", encoding="utf-8")
        try:
            probe.unlink()
        except Exception:
            pass
        return True
    except Exception:
        return False


def _resolve_app_dir() -> Path:
    """数据目录 = 软件锁所在目录；不可写时回退 %APPDATA%。

    环境变量 SOFTLOCK_DATA_DIR 可显式指定数据目录，供自动化测试把数据隔离到
    临时目录——否则测试进程会把测试配置写进 exe 同级目录，污染用户真实数据。
    """
    override = (os.environ.get("SOFTLOCK_DATA_DIR") or "").strip()
    if override:
        try:
            path = Path(override).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            return path
        except Exception:
            pass
    try:
        if getattr(sys, "frozen", False):
            return app_dir_for(Path(sys.executable).resolve().parent)
        return app_dir_for(Path(__file__).resolve().parent.parent)
    except Exception:
        return LEGACY_APP_DIR


def app_dir_for(base_dir: Path) -> Path:
    """给定 exe/脚本所在目录，返回实际使用的数据目录（不可写则回退旧目录）。

    打包产物与自动化测试都要用同一个规则解析，两边才能对得上。
    """
    try:
        if _writable_dir(base_dir):
            return base_dir
    except Exception:
        pass
    return LEGACY_APP_DIR


APP_DIR = _resolve_app_dir()
CONFIG_PATH = APP_DIR / "config.json"
LOG_DIR = APP_DIR / "logs"
LOG_PATH = LOG_DIR / "app.log"
SHOW_FLAG = APP_DIR / "show.flag"          # 第二实例请求唤起主窗口
EXIT_FLAG = APP_DIR / "allow_exit.flag"    # 正常退出标记，看守进程据此不再重启
GUARD_FAIL = APP_DIR / "guard_fail.count"  # 看守进程重启计数

DEFAULT_SETTINGS = {
    "poll_interval_ms": 120,          # 后台扫描间隔
    "autostart": False,               # 开机自启动
    "self_defense": True,             # 看守进程防退出
    "lock_running_on_start": True,    # 启动时清理已在运行的受保护应用
    "max_attempts": 5,                # 连续输错密码几次后进入冷却
    "cooldown_seconds": 30,           # 冷却时长
    "prompt_timeout_seconds": 300,    # 密码窗口最长等待，超时按拒绝处理
    "require_admin": True,            # 启动时尝试以管理员身份运行（拦截管理员程序所必需）
}

_lock = threading.RLock()


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def ensure_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def norm_path(path: str) -> str:
    """路径归一化，用于比较（大小写不敏感 + 绝对路径）。"""
    try:
        return os.path.normcase(os.path.abspath(os.path.expandvars(path)))
    except Exception:
        return (path or "").strip().lower()


def set_app_user_model_id(app_id: str) -> None:
    """让任务栏图标归属独立的应用标识，而不是 python.exe。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def show_flag_requested() -> bool:
    if SHOW_FLAG.exists():
        try:
            SHOW_FLAG.unlink()
        except Exception:
            pass
        return True
    return False


# --------------------------------------------------------------------------
# 密码
# --------------------------------------------------------------------------
def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> dict:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {
        "algo": "pbkdf2_sha256",
        "iterations": iterations,
        "salt": salt.hex(),
        "hash": dk.hex(),
    }


def verify_password(password: str, record: dict | None) -> bool:
    if not record or not password:
        return False
    try:
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(record["salt"]),
            int(record["iterations"]),
        )
    except Exception:
        return False
    return hmac.compare_digest(dk.hex(), str(record.get("hash", "")))


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
def log(message: str, level: str = "INFO") -> None:
    ensure_dirs()
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {message}"
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_LOG_BYTES:
            rotated = LOG_PATH.with_suffix(".old.log")
            try:
                if rotated.exists():
                    rotated.unlink()
                LOG_PATH.replace(rotated)
            except Exception:
                pass
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    if sys.stdout and not getattr(sys, "frozen", False):
        try:
            print(line, flush=True)
        except Exception:
            pass


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
class Store:
    """配置读写 + 密码校验 + 冷却控制，线程安全。"""

    def __init__(self, path: Path = CONFIG_PATH):
        self.path = Path(path)
        self.data: dict = {}
        self.load()

    # ---------------- 读写 ----------------
    def _blank(self) -> dict:
        return {
            "version": 1,
            "password": None,
            "apps": [],
            "settings": dict(DEFAULT_SETTINGS),
            "stats": {"blocked": 0, "allowed": 0},
        }

    def load(self) -> None:
        with _lock:
            data = self._blank()
            try:
                if self.path.exists():
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        data.update(raw)
            except Exception as exc:  # 配置损坏时回退到默认值
                log(f"读取配置失败，使用默认配置: {exc}", "ERROR")
            settings = dict(DEFAULT_SETTINGS)
            settings.update(data.get("settings") or {})
            data["settings"] = settings
            if not isinstance(data.get("apps"), list):
                data["apps"] = []
            if not isinstance(data.get("stats"), dict):
                data["stats"] = {"blocked": 0, "allowed": 0}
            self.data = data

    def save(self) -> None:
        with _lock:
            ensure_dirs()
            tmp = self.path.with_suffix(".tmp")
            try:
                tmp.write_text(
                    json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                os.replace(tmp, self.path)
            except Exception as exc:
                log(f"保存配置失败: {exc}", "ERROR")

    # ---------------- 密码 ----------------
    @property
    def has_password(self) -> bool:
        return bool(self.data.get("password"))

    def set_password(self, password: str) -> None:
        with _lock:
            self.data["password"] = hash_password(password)
            self.data["lockout_until"] = 0
            self.data["failed_attempts"] = 0
            self.save()

    def check_password(self, password: str) -> bool:
        return verify_password(password, self.data.get("password"))

    def lockout_remaining(self) -> int:
        try:
            remain = float(self.data.get("lockout_until", 0) or 0) - time.time()
        except Exception:
            remain = 0
        return max(0, int(remain + 0.999))

    def register_failure(self) -> int:
        """记录一次失败，返回剩余冷却秒数（0 表示未进入冷却）。"""
        with _lock:
            attempts = int(self.data.get("failed_attempts", 0) or 0) + 1
            self.data["failed_attempts"] = attempts
            max_attempts = int(self.settings.get("max_attempts", 5))
            if attempts >= max_attempts:
                cooldown = int(self.settings.get("cooldown_seconds", 30))
                self.data["lockout_until"] = time.time() + cooldown
                self.data["failed_attempts"] = 0
                self.save()
                return cooldown
            self.save()
            return 0

    def reset_failures(self) -> None:
        with _lock:
            self.data["failed_attempts"] = 0
            self.data["lockout_until"] = 0
            self.save()

    # ---------------- 设置 ----------------
    @property
    def settings(self) -> dict:
        return self.data.setdefault("settings", dict(DEFAULT_SETTINGS))

    def update_settings(self, **kwargs) -> None:
        with _lock:
            self.settings.update(kwargs)
            self.save()

    # ---------------- 应用列表 ----------------
    @property
    def apps(self) -> list:
        return self.data.setdefault("apps", [])

    def find_by_path(self, path: str) -> dict | None:
        key = norm_path(path)
        for app in self.apps:
            if norm_path(app.get("path", "")) == key:
                return app
        return None

    def add_app(self, path: str, name: str = "", enabled: bool = True) -> dict | None:
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            return None
        with _lock:
            exist = self.find_by_path(path)
            if exist:
                exist["enabled"] = True
                self.save()
                return exist
            app = {
                "id": uuid.uuid4().hex[:12],
                "name": name or os.path.splitext(os.path.basename(path))[0],
                "path": path,
                "enabled": bool(enabled),
                "created_at": int(time.time()),
            }
            self.apps.append(app)
            self.save()
            return app

    def remove_app(self, app_id: str) -> bool:
        with _lock:
            before = len(self.apps)
            self.data["apps"] = [a for a in self.apps if a.get("id") != app_id]
            changed = len(self.data["apps"]) != before
            if changed:
                self.save()
            return changed

    def set_enabled(self, app_id: str, enabled: bool) -> None:
        with _lock:
            for app in self.apps:
                if app.get("id") == app_id:
                    app["enabled"] = bool(enabled)
                    self.save()
                    return

    def enabled_apps(self) -> list:
        return [a for a in self.apps if a.get("enabled", True)]

    def bump_stat(self, key: str, delta: int = 1) -> None:
        with _lock:
            stats = self.data.setdefault("stats", {})
            stats[key] = int(stats.get(key, 0) or 0) + delta
            self.save()

    def stats(self) -> dict:
        return self.data.setdefault("stats", {"blocked": 0, "allowed": 0})


# --------------------------------------------------------------------------
# 旧版数据目录迁移（%APPDATA%\SoftwareLock -> exe 同级目录）
# --------------------------------------------------------------------------
def migrate_legacy_config() -> bool:
    """把旧版数据目录里的配置迁移到当前数据目录，保留密码与已保护列表。

    只在打包运行（sys.frozen）且「新位置没有配置、旧位置有配置」时执行一次；
    源码运行或显式指定了 SOFTLOCK_DATA_DIR 的测试进程不做迁移，避免把用户配置
    复制进项目目录 / 临时目录，更避免误动用户真实的旧配置。
    """
    try:
        if not getattr(sys, "frozen", False):
            return False
        if os.environ.get("SOFTLOCK_DATA_DIR"):
            # 数据目录被显式指定（自动化测试）-> 绝不触碰用户真实的旧配置
            return False
        if APP_DIR == LEGACY_APP_DIR:
            return False
        if CONFIG_PATH.exists():
            return False
        legacy = LEGACY_APP_DIR / "config.json"
        if not legacy.is_file():
            return False
        ensure_dirs()
        backup = CONFIG_PATH.with_suffix(".json.migrated")
        shutil.copy2(legacy, backup)          # 先备份，迁移失败可回滚
        shutil.copy2(legacy, CONFIG_PATH)
        # 故意**保留**旧目录里的 config.json（只额外留一份 .migrated 副本）：
        # 万一用户要回退到旧版本，旧版本仍然能正常读到自己的配置，不会被清空。
        try:
            shutil.copy2(legacy, LEGACY_APP_DIR / "config.json.migrated")
        except Exception:
            pass
        log(f"已迁移旧版配置: {legacy} -> {CONFIG_PATH}")
        return True
    except Exception as exc:
        log(f"迁移旧版配置失败（将使用默认配置）: {exc}", "ERROR")
        return False


migrate_legacy_config()
