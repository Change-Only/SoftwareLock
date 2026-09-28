# -*- coding: utf-8 -*-
"""打包脚本：把 src/ 打成 Windows 可执行程序。

产出两套，互为备份：

* ``dist/软件锁.exe``            —— 单文件版，便于拷贝携带
* ``dist/软件锁便携版/``          —— 目录版（PyInstaller ``--onedir``）

为什么做两个版本：单文件版每次启动都会把自身解压到 ``%TEMP%\\_MEIxxxx`` 再执行，
这种「自解压 + 临时目录运行」的行为是杀毒软件启发式误报的高发特征。目录版不这么干，
被杀软拦截的概率明显更低；如果单文件版被杀软清理，换目录版即可。

另外全程显式 ``--noupx``：UPX 压缩壳同样是误报高发特征。

用法：
    python build.py               # 单文件版 + 便携版
    python build.py --no-clean    # 复用上次的 build 缓存，速度更快
    python build.py --no-portable # 只出单文件版
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
ASSETS = ROOT / "assets"
ICON = ASSETS / "softlock.ico"
NAME_EN = "SoftwareLock"
NAME_CN = "软件锁.exe"
PORTABLE_NAME_EN = "SoftwareLockPortable"
PORTABLE_DIR_CN = "软件锁便携版"

EXCLUDES = [
    "numpy", "pandas", "matplotlib", "scipy", "PyQt5", "PyQt6", "PySide2",
    "PySide6", "IPython", "notebook", "sqlite3", "distutils", "setuptools",
    "pytest", "unittest", "pydoc_data", "lib2to3", "test",
]

HIDDEN = ["pystray._win32", "PIL._tkinter_finder", "psutil"]

# tkinter 依赖的本地运行库。PyInstaller 对 Anaconda 布局常常找不到它们，
# 漏打会导致 exe 启动时报「找不到 tcl86t.dll」而直接卡住。
TK_BINARIES = ["tcl86t.dll", "tk86t.dll", "tcl8t.dll", "tk8t.dll",
               "ffi.dll", "zlib.dll", "zlib-ng2.dll"]
TK_DATADIRS = [("tcl8.6", "_tcl_data"), ("tk8.6", "_tk_data"),
               ("tcl8", "_tcl_data"), ("tk8", "_tk_data")]


def _search_dirs() -> list[Path]:
    base = Path(sys.base_prefix)
    return [
        base / "Library" / "bin",   # Anaconda / conda-forge
        base / "DLLs",              # python.org
        base / "bin",
        base,
    ]


def _search_lib_dirs() -> list[Path]:
    base = Path(sys.base_prefix)
    return [
        base / "Library" / "lib",   # Anaconda
        base / "tcl",               # python.org
        base / "lib",
        base,
    ]


def collect_tk() -> tuple[list[tuple[Path, str]], list[tuple[Path, str]]]:
    """返回 (binaries, datas)，都是 (源路径, 打包内目标目录)。"""
    binaries: list[tuple[Path, str]] = []
    for name in TK_BINARIES:
        for folder in _search_dirs():
            candidate = folder / name
            if candidate.is_file():
                binaries.append((candidate, "."))
                break
    datas: list[tuple[Path, str]] = []
    lib_dirs = _search_lib_dirs()
    for src_name, dest in TK_DATADIRS:
        for folder in lib_dirs:
            candidate = folder / src_name
            if candidate.is_dir():
                datas.append((candidate, dest))
                break
    return binaries, datas


def make_icon() -> bool:
    sys.path.insert(0, str(SRC))
    try:
        import artwork
    except Exception as exc:
        print(f"! 无法导入 artwork: {exc}")
        return False
    ASSETS.mkdir(parents=True, exist_ok=True)
    ok = artwork.save_ico(str(ICON))
    print(("√ 图标已生成: " if ok else "! 图标生成失败: ") + str(ICON))
    return ok


def make_version_file() -> Path:
    path = BUILD / "version_info.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(\n"
        "    filevers=(1, 4, 2, 0), prodvers=(1, 4, 2, 0),\n"
        "    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,\n"
        "    date=(0, 0)\n"
        "  ),\n"
        "  kids=[\n"
        "    StringFileInfo([\n"
        "      StringTable('080404b0', [\n"
        "        StringStruct('CompanyName', 'SoftwareLock'),\n"
        "        StringStruct('FileDescription', '软件锁 - 应用启动密码保护'),\n"
        "        StringStruct('FileVersion', '1.4.2.0'),\n"
        "        StringStruct('InternalName', 'SoftwareLock'),\n"
        "        StringStruct('LegalCopyright', 'SoftwareLock'),\n"
        "        StringStruct('OriginalFilename', 'SoftwareLock.exe'),\n"
        "        StringStruct('ProductName', '软件锁'),\n"
        "        StringStruct('ProductVersion', '1.4.2.0')\n"
        "      ])\n"
        "    ]),\n"
        "    VarFileInfo([VarStruct('Translation', [2052, 1200])])\n"
        "  ]\n"
        ")\n",
        encoding="utf-8",
    )
    return path


def check_deps() -> bool:
    missing = []
    for module in ("PyInstaller", "PIL", "psutil", "pystray"):
        try:
            __import__(module)
        except Exception:
            missing.append(module)
    if missing:
        print("! 缺少依赖: " + ", ".join(missing))
        print("  请先执行: pip install -r requirements.txt")
        return False
    return True


def _pyi_cmd(name_en: str, dist_dir: Path, work_dir: Path, onefile: bool,
             icon_ok: bool, version_file: Path, clean: bool,
             binaries: list, datas: list) -> list[str]:
    """组装一条 PyInstaller 命令。

    统一带 ``--noupx``：UPX 压缩壳是杀毒软件误报的高发特征，显式关闭。
    """
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile" if onefile else "--onedir",
        "--windowed",
        "--noupx",
        "--name", name_en,
        "--paths", str(SRC),
        "--distpath", str(dist_dir),
        "--workpath", str(work_dir),
        "--specpath", str(work_dir),
        "--version-file", str(version_file),
        "--collect-submodules", "pystray",
    ]
    if clean:
        cmd.append("--clean")
    if icon_ok:
        cmd += ["--icon", str(ICON)]
    for hidden in HIDDEN:
        cmd += ["--hidden-import", hidden]
    for exclude in EXCLUDES:
        cmd += ["--exclude-module", exclude]
    for src, dest in binaries:
        cmd += ["--add-binary", f"{src}{os.pathsep}{dest}"]
    for src, dest in datas:
        cmd += ["--add-data", f"{src}{os.pathsep}{dest}"]
    cmd.append(str(SRC / "main.py"))
    return cmd


def _backup_path(target: Path, label: str = "") -> None:
    """把已有产物改名成带时间戳的备份（绝不删除任何文件）。"""
    if not target.exists():
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = target.with_name(f"{target.name}.{stamp}.bak")
    try:
        os.replace(target, backup)
        print(f"  {label}旧产物已备份: {backup.name}")
    except Exception as exc:
        print(f"! {label}旧产物备份失败（将尝试直接覆盖）: {exc}")


def _build_onefile(icon_ok: bool, version_file: Path, binaries: list,
                   datas: list, clean: bool) -> int:
    cmd = _pyi_cmd(NAME_EN, DIST, BUILD, True, icon_ok, version_file, clean,
                   binaries, datas)
    print(f"  打包 Tcl/Tk 运行库: {len(binaries)} 个 dll, {len(datas)} 个脚本目录")
    print("\n$ " + " ".join(cmd) + "\n")
    if subprocess.run(cmd, cwd=str(ROOT)).returncode != 0:
        print("! 单文件版打包失败")
        return 1

    produced = DIST / f"{NAME_EN}.exe"
    final = DIST / NAME_CN
    if produced.exists():
        try:
            _backup_path(final)
            produced.replace(final)
        except Exception as exc:
            print(f"! 重命名失败（保留原名）: {exc}")
            final = produced
    if not final.exists():
        print("! 未找到单文件版产物")
        return 1
    print(f"√ 单文件版: {final}  ({final.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


def _build_portable(icon_ok: bool, version_file: Path, binaries: list,
                    datas: list, clean: bool) -> int:
    """目录版（--onedir）：不把自己解压到临时目录再运行，误报概率更低。"""
    staging = DIST / "_portable_staging"
    cmd = _pyi_cmd(PORTABLE_NAME_EN, staging, BUILD / "portable", False,
                   icon_ok, version_file, clean, binaries, datas)
    print("\n$ " + " ".join(cmd) + "\n")
    if subprocess.run(cmd, cwd=str(ROOT)).returncode != 0:
        print("! 便携版打包失败")
        return 1

    produced_dir = staging / PORTABLE_NAME_EN
    if not produced_dir.is_dir():
        print("! 未找到便携版产物")
        return 1
    final_dir = DIST / PORTABLE_DIR_CN
    if final_dir.exists():
        _backup_path(final_dir, "便携版")
    try:
        produced_dir.replace(final_dir)
    except Exception as exc:
        print(f"! 便携版目录改名失败（保留原名）: {exc}")
        final_dir = produced_dir

    exe_in_dir = final_dir / f"{PORTABLE_NAME_EN}.exe"
    target = final_dir / NAME_CN
    try:
        if exe_in_dir.exists() and exe_in_dir != target:
            os.replace(exe_in_dir, target)
    except Exception as exc:
        print(f"! 便携版 exe 改名失败: {exc}")
        target = exe_in_dir
    total = sum(f.stat().st_size for f in final_dir.rglob("*") if f.is_file())
    print(f"√ 便携版: {target}  (整目录 {total / 1024 / 1024:.1f} MB)")
    return 0


def build(clean: bool = True, portable: bool = True) -> int:
    if not check_deps():
        return 1
    icon_ok = make_icon()
    version_file = make_version_file()
    binaries, datas = collect_tk()
    for src, _dest in binaries:
        print(f"    + {src}")
    for src, _dest in datas:
        print(f"    + {src}")

    if _build_onefile(icon_ok, version_file, binaries, datas, clean) != 0:
        return 1

    if portable:
        if _build_portable(icon_ok, version_file, binaries, datas, clean) != 0:
            print("  （便携版打包失败，单文件版仍可用）")

    print("\n√ 打包完成")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-clean", action="store_true", help="复用 build 缓存")
    parser.add_argument("--no-portable", action="store_true",
                        help="只打单文件版，跳过便携目录版")
    args = parser.parse_args()
    return build(clean=not args.no_clean, portable=not args.no_portable)


if __name__ == "__main__":
    sys.exit(main())
