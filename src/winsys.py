# -*- coding: utf-8 -*-
"""软件锁 —— Windows 底层能力封装（纯 ctypes，无第三方 GUI 依赖）。

包含：
  * 进程挂起 / 恢复 / 结束进程树
  * 枚举某进程的顶层窗口并隐藏 / 恢复
  * 从 exe 中提取图标
  * 单实例互斥体
  * 开机自启动（HKCU Run）
  * 是否需要管理员权限判断 / 提权重启
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

import psutil

IS_WIN = sys.platform == "win32"

if IS_WIN:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
else:  # pragma: no cover - 仅用于非 Windows 下的导入保护
    user32 = kernel32 = shell32 = gdi32 = None

# ---------------------------------------------------------------- 常量
SW_HIDE = 0
SW_SHOW = 5
SW_SHOWNORMAL = 1
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ALREADY_EXISTS = 183

EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000


# ---------------------------------------------------------------- 函数原型
# 明确声明参数/返回类型，避免 64 位下句柄被截断成 32 位
def _declare_prototypes() -> None:
    if not IS_WIN:
        return
    try:
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL

        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int
        user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]
        user32.GetIconInfo.restype = wintypes.BOOL
        user32.DestroyIcon.argtypes = [wintypes.HICON]
        user32.DestroyIcon.restype = wintypes.BOOL

        gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
        gdi32.GetObjectW.restype = ctypes.c_int
        gdi32.GetDIBits.argtypes = [
            wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
        ]
        gdi32.GetDIBits.restype = ctypes.c_int

        shell32.ExtractIconExW.argtypes = [
            wintypes.LPCWSTR, ctypes.c_int,
            ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.HICON), wintypes.UINT,
        ]
        shell32.ExtractIconExW.restype = wintypes.UINT
        shell32.IsUserAnAdmin.restype = wintypes.BOOL
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.OpenMutexW.restype = wintypes.HANDLE
    except Exception:
        pass


# ---------------------------------------------------------------- 结构体
class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", wintypes.LONG),
        ("bmWidth", wintypes.LONG),
        ("bmHeight", wintypes.LONG),
        ("bmWidthBytes", wintypes.LONG),
        ("bmPlanes", wintypes.WORD),
        ("bmBitsPixel", wintypes.WORD),
        ("bmBits", ctypes.c_void_p),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


# ---------------------------------------------------------------- 进程操作
def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        return psutil.pid_exists(pid)
    except Exception:
        return False


def process_exe(pid: int) -> str | None:
    """取进程的可执行文件全路径，失败返回 None。"""
    try:
        return psutil.Process(pid).exe()
    except Exception:
        return None


def process_name(pid: int) -> str | None:
    try:
        return psutil.Process(pid).name()
    except Exception:
        return None


def process_create_time(pid: int) -> float:
    try:
        return psutil.Process(pid).create_time()
    except Exception:
        return 0.0


def parent_pid(pid: int) -> int:
    """父进程 pid；取不到返回 0。

    用于区分「用户从桌面双击」（父进程是 explorer）与「受保护应用自身派生的
    子进程 / launcher 交接」（父进程是同一个应用）。父进程已退出时 Windows 仍会
    保留原始 ppid，因此交接场景判断依然有效。
    """
    try:
        return int(psutil.Process(pid).ppid() or 0)
    except Exception:
        return 0


def suspend_process(pid: int) -> bool:
    """挂起进程内的所有线程。"""
    try:
        psutil.Process(pid).suspend()
        return True
    except Exception:
        return False


def resume_process(pid: int) -> bool:
    try:
        psutil.Process(pid).resume()
        return True
    except Exception:
        return False


def terminate_tree(pid: int) -> int:
    """结束进程及其全部子进程，返回成功结束的进程数。"""
    killed = 0
    try:
        proc = psutil.Process(pid)
    except Exception:
        return 0
    try:
        children = proc.children(recursive=True)
    except Exception:
        children = []
    for child in children:
        try:
            child.kill()
            killed += 1
        except Exception:
            pass
    try:
        proc.kill()
        killed += 1
    except Exception:
        pass
    return killed


# ---------------------------------------------------------------- 窗口操作
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _windows_of_pid(pid: int) -> list[int]:
    """返回该进程所有「可见」顶层窗口句柄。"""
    result: list[int] = []

    def _cb(hwnd, _lparam):
        wnd_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wnd_pid))
        if wnd_pid.value == pid and user32.IsWindowVisible(hwnd):
            result.append(int(hwnd or 0))
        return True

    try:
        user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    except Exception:
        pass
    return result


def hide_windows(pid: int) -> list[int]:
    """隐藏进程的可见顶层窗口，返回被隐藏的句柄列表（用于之后恢复）。"""
    handles = _windows_of_pid(pid)
    for hwnd in handles:
        try:
            user32.ShowWindow(hwnd, SW_HIDE)
        except Exception:
            pass
    return handles


def pids_with_visible_windows(pids) -> set[int]:
    """一次 EnumWindows 遍历，返回其中「拥有可见顶层窗口」的 pid 子集。

    放行会话的存活判定要用：无窗口的僵尸/服务型进程不该给会话续命。
    一次遍历判定所有 pid，避免对每个 pid 都做全量窗口枚举。
    """
    want = {int(p) for p in pids if p}
    if not want:
        return set()
    found: set[int] = set()
    wnd_pid = wintypes.DWORD()

    def _cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            wnd_pid.value = 0
            try:
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wnd_pid))
            except Exception:
                return True
            if wnd_pid.value in want:
                found.add(int(wnd_pid.value))
        return True

    try:
        user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    except Exception:
        pass
    return found


def restore_windows(handles: list[int]) -> None:
    for hwnd in handles or []:
        try:
            if user32.IsWindow(hwnd):
                user32.ShowWindow(hwnd, SW_SHOWNORMAL)
        except Exception:
            pass


def find_window_by_pid(pid: int) -> int:
    handles = _windows_of_pid(pid)
    return handles[0] if handles else 0


def force_foreground(pid: int) -> None:
    """把某进程的主窗口置前（用于放行后立即显示）。"""
    try:
        hwnd = find_window_by_pid(pid)
        if not hwnd:
            return
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


# ---------------------------------------------------------------- exe 图标
def extract_exe_icon(path: str, size: int = 32):
    """从 exe 中提取图标，返回 PIL.Image（RGBA），失败返回 None。"""
    if not IS_WIN:
        return None
    try:
        from PIL import Image
    except Exception:
        return None

    large = wintypes.HICON()
    small = wintypes.HICON()
    count = 0
    try:
        count = shell32.ExtractIconExW(
            ctypes.c_wchar_p(path), 0, ctypes.byref(large), ctypes.byref(small), 1
        )
    except Exception:
        return None
    hicon = large.value or small.value
    if not count or not hicon:
        return None

    hbm_color = None
    try:
        info = ICONINFO()
        if not user32.GetIconInfo(hicon, ctypes.byref(info)):
            return None
        hbm_color = info.hbmColor or info.hbmMask
        if not hbm_color:
            return None

        bmp = BITMAP()
        if not gdi32.GetObjectW(hbm_color, ctypes.sizeof(BITMAP), ctypes.byref(bmp)):
            return None
        width, height = int(bmp.bmWidth), int(bmp.bmHeight)
        if width <= 0 or height <= 0 or width > 512 or height > 512:
            return None

        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height  # 负数 = 自上而下
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0  # BI_RGB

        buf = ctypes.create_string_buffer(width * height * 4)
        hdc = user32.GetDC(None)
        try:
            lines = gdi32.GetDIBits(
                hdc, hbm_color, 0, height, buf, ctypes.byref(header), 0
            )
        finally:
            user32.ReleaseDC(None, hdc)
        if not lines:
            return None

        img = Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1)
        img = img.convert("RGBA")
        # 部分图标没有 alpha 通道，整体透明时用 mask 补
        alpha = img.getchannel("A")
        if alpha.getextrema() == (0, 0):
            img.putalpha(alpha.point(lambda v: 255))
        if size and (width != size or height != size):
            img = img.resize((size, size), Image.LANCZOS)
        return img
    except Exception:
        return None
    finally:
        try:
            if large.value:
                user32.DestroyIcon(large)
        except Exception:
            pass
        try:
            if small.value:
                user32.DestroyIcon(small)
        except Exception:
            pass


# ---------------------------------------------------------------- 单实例
SINGLETON_NAME = "Local\\SoftwareLock.SingleInstance.v1"


def instance_running(name: str = SINGLETON_NAME) -> bool:
    """探测是否已有实例在运行（只用 OpenMutex，绝不创建）。"""
    if not IS_WIN:
        return False
    try:
        kernel32.OpenMutexW.restype = wintypes.HANDLE
        handle = kernel32.OpenMutexW(0x00100000, False, ctypes.c_wchar_p(name))  # SYNCHRONIZE
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


class SingleInstance:
    """基于命名互斥体的单实例控制。"""

    def __init__(self, name: str = SINGLETON_NAME):
        self.name = name
        self.handle = None
        self.already_running = False

    def acquire(self) -> bool:
        if not IS_WIN:
            return True
        try:
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            self.handle = kernel32.CreateMutexW(None, False, ctypes.c_wchar_p(self.name))
            self.already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
            return not self.already_running
        except Exception:
            return True

    def acquire_retry(self, retries: int = 20, delay: float = 0.2) -> bool:
        """带重试地获取互斥体：用于「提权重启」场景。

        以管理员身份重新启动时，旧进程可能还没退完，互斥体短暂仍被占用。
        这里最多重试 ``retries`` 次、每次间隔 ``delay`` 秒，期间一旦拿到就返回 True。
        """
        import time

        attempts = max(1, int(retries))
        for index in range(attempts):
            if self.acquire():
                return True
            # acquire 在已被占用时也会拿到句柄，需要及时释放，避免句柄泄漏
            self.release()
            if index < attempts - 1:
                time.sleep(max(0.0, delay))
        return False

    def release(self) -> None:
        try:
            if self.handle:
                kernel32.CloseHandle(self.handle)
                self.handle = None
        except Exception:
            pass


# ---------------------------------------------------------------- 自启动
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "SoftwareLock"
# 计划任务名：以最高权限在登录时启动，登录过程不弹 UAC
TASK_NAME = "SoftwareLock"


def _open_run_key(write: bool = False):
    import winreg

    access = winreg.KEY_SET_VALUE if write else winreg.KEY_READ
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def get_autostart() -> str | None:
    if not IS_WIN:
        return None
    try:
        import winreg

        with _open_run_key() as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
            return value
    except Exception:
        return None


def _log(message: str, level: str = "INFO") -> None:
    """延迟导入 config_store 写日志，避免与 winsys 形成导入环。"""
    try:
        import config_store

        config_store.log(message, level)
    except Exception:
        pass


def set_autostart_task(enable: bool, exe_path: str | None = None) -> bool:
    """通过 Windows 计划任务设置/取消开机自启动（最高权限，登录时不弹 UAC）。

    以 ``schtasks`` 的返回码判断成功与否，任何异常一律返回 False（绝不抛出）。
    """
    if not IS_WIN:
        return False
    import subprocess

    exe_path = exe_path or current_exe()
    try:
        if enable:
            # /TR 的值必须是 "exe路径" + 参数 的完整命令行
            tr = f'"{exe_path}" --minimized'
            cmd = ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", tr,
                   "/SC", "ONLOGON", "/RL", "HIGHEST", "/IT", "/F"]
        else:
            cmd = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        proc = subprocess.run(cmd, capture_output=True, creationflags=flags)
        return proc.returncode == 0
    except Exception:
        return False


def _set_autostart_run(enable: bool, exe_path: str) -> bool:
    """注册表 HKCU\\...\\Run 方式（源码运行时的回退方案）。"""
    import winreg

    try:
        with _open_run_key(write=True) as key:
            if enable:
                winreg.SetValueEx(
                    key, RUN_VALUE, 0, winreg.REG_SZ, f'"{exe_path}" --minimized'
                )
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


def set_autostart(enable: bool, exe_path: str | None = None) -> bool:
    """设置开机自启动。

    * 打包后的 exe：优先用计划任务（最高权限，登录不弹 UAC），成功则清理旧的注册表项；
      计划任务失败再回退到注册表 Run。
    * 源码运行：直接用注册表 Run，避免在开发机上污染计划任务。
    """
    if not IS_WIN:
        return False
    exe_path = exe_path or current_exe()
    frozen = bool(getattr(sys, "frozen", False))

    if not enable:
        task_ok = set_autostart_task(False, exe_path)
        reg_ok = _set_autostart_run(False, exe_path)
        return bool(task_ok or reg_ok)

    if frozen:
        if set_autostart_task(True, exe_path):
            _set_autostart_run(False, exe_path)  # 清理可能残留的旧注册表项
            _log("已通过计划任务设置开机自启动（最高权限）")
            return True
        _log("计划任务设置失败，回退到注册表 Run", "WARNING")

    ok = _set_autostart_run(True, exe_path)
    if not ok:
        _log("写入开机启动项失败（注册表）", "WARNING")
    return ok


# ---------------------------------------------------------------- 进程 / 权限
def current_exe() -> str:
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return os.path.abspath(sys.argv[0])


def is_elevated() -> bool:
    if not IS_WIN:
        return False
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(argv: list[str] | None = None) -> bool:
    """以管理员身份重新启动当前程序。"""
    if not IS_WIN:
        return False
    try:
        params = " ".join(f'"{a}"' for a in (argv or []))
        ret = shell32.ShellExecuteW(
            None, "runas", current_exe(), params or None, None, 1
        )
        return int(ret) > 32
    except Exception:
        return False


# ---------------------------------------------------------------- 杀软白名单
DEFENDER_EXCLUSION_PATH_KEY = r"SOFTWARE\Microsoft\Windows Defender\Exclusions\Paths"
DEFENDER_EXCLUSION_PROC_KEY = r"SOFTWARE\Microsoft\Windows Defender\Exclusions\Processes"


def _decode_console(raw: bytes) -> str:
    for encoding in ("utf-8", "gbk", "mbcs"):
        try:
            return raw.decode(encoding)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def run_powershell(script: str, timeout: int = 60) -> tuple[int, str]:
    """执行一段 PowerShell 脚本，返回 (返回码, 输出)。任何异常都不抛出。"""
    if not IS_WIN:
        return 1, "仅支持 Windows"
    import subprocess

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, timeout=timeout, creationflags=flags,
        )
    except Exception as exc:
        return 1, str(exc)
    raw = (proc.stdout or b"") + (proc.stderr or b"")
    return proc.returncode, _decode_console(raw).strip()


def spawn_detached_tree_safe(args: list[str], workdir: str | None = None) -> int | None:
    """拉起一个与当前进程**树**脱钩的进程，返回新进程 pid（失败返回 None）。

    任务管理器的「结束进程树」按父子关系连坐结束进程。软件锁是双进程互为看守，
    如果看守进程是主进程的直接子进程，一次「结束进程树」就能把两个一起带走，
    保护随之消失（这是实际发生过的漏洞）。这里经 WMI ``Win32_Process.Create``
    拉起进程，新进程的父进程是 WMI 提供程序（WmiPrvSE），不在软件锁的进程树里，
    树杀只会带走被结束的那棵子树。

    优先走 WMI（经 PowerShell 调用）；WMI 不可用时回退为普通分离式启动
    （防杀能力降一档，但看守功能不受影响）。

    注意：经 WMI 拉起的进程**不继承当前进程的环境变量**，也不继承工作目录
    （可显式传 ``workdir``）；依赖 exe 自身路径定位数据的进程（如软件锁）不受影响。
    """
    import subprocess

    cmdline = subprocess.list2cmdline(args)
    ps_arg = cmdline.replace("'", "''")
    extra = ""
    if workdir:
        extra = f";CurrentDirectory='{workdir.replace(chr(39), chr(39) * 2)}'"
    script = (
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{CommandLine='{ps_arg}'{extra}}}; "
        "Write-Output (\"{0}`t{1}\" -f $r.ReturnValue, $r.ProcessId)"
    )
    code, out = run_powershell(script, timeout=30)
    if code == 0 and out:
        try:
            first = out.splitlines()[0].strip()
            parts = first.replace(" ", "\t").split("\t")
            parts = [p for p in parts if p]
            if len(parts) >= 2 and parts[0] == "0" and parts[1].isdigit():
                return int(parts[1])
        except Exception:
            pass
    _log(f"WMI 脱树拉起失败（code={code} out={out[:120]!r}），回退普通分离式启动: "
         f"{cmdline[:160]}", "WARNING")
    # 回退：普通分离式启动（父进程仍是当前进程，可被「结束进程树」连坐）
    flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    try:
        return subprocess.Popen(args, close_fds=True, creationflags=flags,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL).pid
    except Exception as exc:
        _log(f"分离式启动也失败: {exc}", "ERROR")
        return None


def _enum_hklm_values(path: str) -> list[str] | None:
    """枚举 HKLM 下某个键的所有值名；无权限或键不存在时返回 None。"""
    if not IS_WIN:
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, path, 0,
            winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
        ) as key:
            names: list[str] = []
            index = 0
            while True:
                try:
                    name, _value, _type = winreg.EnumValue(key, index)
                except OSError:
                    break
                names.append(name)
                index += 1
            return names
    except Exception:
        return None


def defender_exclusions() -> list[str] | None:
    """读取 Defender 的路径排除项；没有管理员权限时返回 None。"""
    return _enum_hklm_values(DEFENDER_EXCLUSION_PATH_KEY)


def add_defender_exclusions(paths: list[str]) -> tuple[bool, str]:
    """把文件 / 目录加入 Windows Defender 排除项。

    这是避免「软件锁被 Defender 当成威胁自动清理」最直接的办法：排除项一旦写入，
    Defender 的实时防护就不会再检查这些路径。需要管理员权限；系统开启
    「篡改防护」时可能被静默拒绝，此时如实返回失败，由界面提示手动添加。
    """
    if not IS_WIN:
        return False, "仅支持 Windows"
    if not is_elevated():
        return False, "需要以管理员身份运行软件锁才能写入白名单"

    targets: list[str] = []
    for item in paths:
        if not item:
            continue
        try:
            full = os.path.abspath(item)
        except Exception:
            continue
        if full not in targets:
            targets.append(full)
    if not targets:
        return False, "没有可加入白名单的路径"

    script_parts: list[str] = []
    for full in targets:
        safe = full.replace("'", "''")
        script_parts.append(f"Add-MpPreference -ExclusionPath '{safe}'")
        script_parts.append(f"Add-MpPreference -ExclusionProcess '{safe}'")
    code, output = run_powershell("; ".join(script_parts))

    # 以「排除项里能不能查到」作为成功判据，比信任返回码更可靠
    after = defender_exclusions()
    if after is not None:
        written = {p.rstrip("\\/").lower() for p in after}
        missing = [p for p in targets if p.rstrip("\\/").lower() not in written]
        if not missing:
            _log("已加入 Windows Defender 排除项: " + "; ".join(targets))
            return True, "已加入 Windows Defender 白名单"
    detail = output or "写入白名单失败"
    if code == 0:
        detail = "写入白名单被系统拒绝（通常是开启了「篡改防护」）"
    _log(f"加入 Windows Defender 白名单失败: {detail}", "WARNING")
    return False, detail


def open_path(path: str, workdir: str | None = None, as_admin: bool = False) -> bool:
    """用 ShellExecute 打开路径（用于需要提权或特殊关联的场景）。"""
    if not IS_WIN:
        return False
    try:
        verb = "runas" if as_admin else "open"
        ret = shell32.ShellExecuteW(None, verb, path, None, workdir or None, 1)
        return int(ret) > 32
    except Exception:
        return False


def message_beep() -> None:
    if not IS_WIN:
        return
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONHAND)
    except Exception:
        pass


# 模块加载完成后统一声明函数原型（需要 ICONINFO / _WNDENUMPROC 已定义）
_declare_prototypes()
