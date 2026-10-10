# -*- coding: utf-8 -*-
"""校验 PE 文件是否带有效 Authenticode 签名（ctypes 调 WinVerifyTrust，与 UAC 同源）。

用法：
    python tools/verify_signature.py <exe 或 pe 文件> [更多文件...]

输出每个文件的证书目录大小与 WinVerifyTrust 返回码：
    0x00000000  有效签名
    0x800B0100  无签名（NO_SIGNATURE）
    其它        签名无效 / 证书链不受信任等

退出码：全部有效 = 0，任一无效 = 1（可 scripts 里直接 if 判断）。
"""
from __future__ import annotations

import ctypes
import struct
import sys
from ctypes import wintypes


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]


class WINTRUST_FILE_INFO(ctypes.Structure):
    _fields_ = [("cbStruct", wintypes.DWORD), ("pcwszFilePath", wintypes.LPCWSTR),
                ("hFile", wintypes.HANDLE), ("pgKnownSubject", ctypes.POINTER(GUID))]


class WINTRUST_DATA(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD), ("pPolicyCallbackData", ctypes.c_void_p),
        ("pSIPClientData", ctypes.c_void_p), ("dwUIChoice", wintypes.DWORD),
        ("fdwRevocationChecks", wintypes.DWORD), ("dwUnionChoice", wintypes.DWORD),
        ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)), ("dwStateAction", wintypes.DWORD),
        ("hWVTStateData", wintypes.HANDLE), ("pwszURLReference", wintypes.LPCWSTR),
        ("dwProvFlags", wintypes.DWORD), ("dwUIContext", wintypes.DWORD),
    ]


# WINTRUST_ACTION_GENERIC_VERIFY_V2
ACTION = GUID(0x00AAC56B, 0xCD44, 0x11D0,
              (ctypes.c_byte * 8)(*[0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE]))
WTD_UI_NONE = 2
WTD_CHOICE_FILE = 1
WTD_REVOCATION_CHECK_NONE = 0x00000010

STATUS_TEXT = {
    0x00000000: "有效签名",
    0x800B0100: "无签名",
}


def cert_table_size(path: str) -> int:
    """读 PE 安全目录大小：0 = 完全没有签名块；-1 = 不是 PE。"""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:2] != b"MZ":
        return -1
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return -1
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    dd_off = opt + (96 if magic == 0x10B else 112)
    size = struct.unpack_from("<I", data, dd_off + 4 * 8 + 4)[0]  # 第 4 项 = 安全目录
    return size


def win_verify(path: str) -> int:
    fi = WINTRUST_FILE_INFO()
    fi.cbStruct = ctypes.sizeof(WINTRUST_FILE_INFO)
    fi.pcwszFilePath = path
    fi.hFile = None
    fi.pgKnownSubject = None
    wd = WINTRUST_DATA()
    wd.cbStruct = ctypes.sizeof(WINTRUST_DATA)
    wd.dwUIChoice = WTD_UI_NONE            # 绝不能少：缺了会弹模态 UI 卡死脚本
    wd.fdwRevocationChecks = 0
    wd.dwUnionChoice = WTD_CHOICE_FILE
    wd.pFile = ctypes.pointer(fi)
    wd.dwProvFlags = WTD_REVOCATION_CHECK_NONE
    return ctypes.windll.wintrust.WinVerifyTrust(None, ctypes.byref(ACTION),
                                                 ctypes.byref(wd)) & 0xFFFFFFFF


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    all_ok = True
    for path in argv[1:]:
        code = win_verify(path)
        ok = code == 0
        all_ok &= ok
        text = STATUS_TEXT.get(code, "无效")
        print(f"{path}\n  cert_table={cert_table_size(path)}  "
              f"WinVerifyTrust=0x{code:08X}  [{text}]" + ("" if ok else "  <-- 不通过"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
