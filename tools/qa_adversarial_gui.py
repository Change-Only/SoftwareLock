# -*- coding: utf-8 -*-
"""QA 第四轮对抗用例（需要界面）：C4 密码冷却 / C5 搜索过滤边界 /
C6 对话框不回归 / C8 托盘隐藏恢复。

运行：python tools/qa_adversarial_gui.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE / "src"))

import tkinter as tk  # noqa: E402

import config_store  # noqa: E402
from config_store import Store  # noqa: E402
from ui import theme as T  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    RESULTS.append((name, bool(ok), detail))
    return bool(ok)


class FakeInterceptor:
    def unlocked_paths(self):
        return set()

    def is_unlocked(self, path):
        return False

    def forget(self, path):
        pass

    def launch(self, app):
        return True, "stub"

    def lock_all_running(self):
        return 0

    def stop(self, timeout=2.0):
        pass


class FakeBroker:
    def __init__(self, approve=True):
        self.approve = approve

    def verify(self, *a, **k):
        return self.approve


class FakeTray:
    def __init__(self):
        self._available = True

    def notify(self, *a, **k):
        pass

    def hide_icon(self):
        return True

    def show_icon(self):
        return True

    def set_title(self, t):
        pass

    def stop(self):
        pass


def test_c4(root, tmp):
    print("\n[C4] 密码冷却：连续输错后冷却期内正确密码也应被拒绝", flush=True)
    store = Store(tmp / "c4.json")
    store.set_password("secret123")
    maxa = int(store.settings.get("max_attempts", 5))
    for _ in range(maxa):
        store.register_failure()
    check("连续错误 max_attempts 次后 lockout_remaining() > 0",
          store.lockout_remaining() > 0, f"{store.lockout_remaining()}s")

    from ui.dialogs import PasswordDialog
    dlg = PasswordDialog(root, store, title="退出软件锁", subtitle="x",
                         confirm_text="退出", danger=True)
    root.update()
    dlg.entry.set("secret123")  # 正确密码
    dlg._submit()               # _submit 应先查冷却，直接拒绝
    root.update()
    check("冷却期内 _submit 未接受正确密码（result 非 True）",
          dlg.result is not True, f"result={dlg.result!r}")
    check("提示文案为冷却提示",
          "冷却" in (dlg.msg.cget("text") or ""), repr(dlg.msg.cget("text")))
    check("冷却期内不会关闭对话框", bool(dlg.winfo_exists()))
    dlg._close()
    root.update()
    store.reset_failures()
    check("冷却可被 reset 清除", store.lockout_remaining() == 0)


def _rows(win):
    return len(win._row_widgets)


def _separators(win):
    """统计带 1px 分隔线的行数（hline 是 height=1 的 Frame）。"""
    n = 0
    for row in win.scroll.body.winfo_children():
        if not isinstance(row, tk.Frame):
            continue
        for ch in row.winfo_children():
            try:
                if isinstance(ch, tk.Frame) and int(ch.cget("height")) == 1:
                    n += 1
                    break
            except Exception:
                pass
    return n


def test_c5(root, tmp):
    print("\n[C5] 搜索过滤边界（正则特殊字符 / 中文 / 大小写 / 路径 / 分隔线）", flush=True)
    store = Store(tmp / "c5.json")
    store.set_password("x")
    store.data["apps"] = [
        {"id": "1", "name": "MyApp 微信", "path": r"C:\a\wechat.exe", "enabled": True},
        {"id": "2", "name": "NotePad", "path": r"C:\b\notepad.exe", "enabled": True},
        {"id": "3", "name": "工具(测试)", "path": r"C:\c\tool[1].exe", "enabled": True},
        {"id": "4", "name": "星号*程序", "path": r"C:\d\star.exe", "enabled": True},
    ]
    store.save()

    from ui.main_window import MainWindow
    win = MainWindow(root, store, FakeInterceptor(), FakeBroker(), FakeTray(),
                     lambda then=None: None)
    root.update()

    def search(text):
        win.search.set(text)
        root.update()
        return _rows(win)

    for kw in ["(", "[", "*", ")", "\\", "+", "?", ".*", "^$", "|"]:
        try:
            n = search(kw)
            check(f"搜索正则特殊字符 {kw!r} 不抛异常", True, f"rows={n}")
        except Exception as exc:
            check(f"搜索正则特殊字符 {kw!r} 不抛异常", False, repr(exc))

    check("中文关键字「微信」命中 1", search("微信") == 1, f"{_rows(win)}")
    check("中文关键字「工具」命中 1", search("工具") == 1, f"{_rows(win)}")
    check("中文括号特殊字符「(测试)」命中 1", search("(测试)") == 1, f"{_rows(win)}")
    check("大小写不敏感「myapp」命中 1", search("myapp") == 1, f"{_rows(win)}")
    check("大小写不敏感「notepad」命中 1", search("notepad") == 1, f"{_rows(win)}")
    check("路径片段「wechat.exe」命中 1", search("wechat.exe") == 1, f"{_rows(win)}")
    check("路径片段「C:\\c」命中 1", search(r"C:\c") == 1, f"{_rows(win)}")
    check("无匹配 -> 0 行", search("zzz不存在") == 0, f"{_rows(win)}")

    # 分隔线：过滤态下应为 (行数 - 1) 条，且不随「移除」错位
    search("")
    check("全部 4 行时分隔线 = 3", _separators(win) == 3, f"{_separators(win)}")
    search("exe")
    n_exe = _rows(win)
    check(f"过滤到 {n_exe} 行时分隔线 = {max(0, n_exe - 1)}",
          _separators(win) == max(0, n_exe - 1), f"rows={n_exe} seps={_separators(win)}")
    search("notepad")
    check("过滤到 1 行时分隔线 = 0", _separators(win) == 0,
          f"rows={_rows(win)} seps={_separators(win)}")

    # 过滤态下「移除」中间一行：应按 id 删除，不产生错位
    search("")
    root.update()
    before_ids = [a["id"] for a in store.apps]
    target_app = next(a for a in store.apps if a["id"] == "2")
    win.remove_app(target_app)
    root.update()
    after_ids = [a["id"] for a in store.apps]
    check("过滤态移除按 id 精确删除（无索引错位）",
          after_ids == [i for i in before_ids if i != "2"], str(after_ids))
    check("移除后行数 = 3", _rows(win) == 3, f"{_rows(win)}")
    check("移除后分隔线 = 2", _separators(win) == 2, f"{_separators(win)}")


def test_c6(root, store):
    print("\n[C6] 对话框构建不裁切回归（9 个）", flush=True)
    from ui.dialogs import (ChoiceDialog, ConfirmDialog, PasswordDialog,
                            SetPasswordDialog, SettingsDialog, SetupDialog)

    def build_check(name, cls, *a, **k):
        dlg = cls(*a, **k)
        dlg._finish_init()
        root.update()
        rw, rh = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        w, h = dlg.winfo_width(), dlg.winfo_height()
        check(f"{name} 不裁切", rw <= w and rh <= h, f"req={rw}x{rh} win={w}x{h}")
        dlg._close()
        root.update()

    build_check("SetupDialog", SetupDialog, root, store)
    build_check("PasswordDialog(normal)", PasswordDialog, root, store, title="打开软件锁", subtitle="请输入主密码")
    build_check("PasswordDialog(danger)", PasswordDialog, root, store, title="退出软件锁",
                subtitle="退出后从托盘消失但后台继续守护", confirm_text="退出", danger=True)
    build_check("PasswordDialog(danger-hard)", PasswordDialog, root, store, title="完全退出并停止保护",
                subtitle="彻底结束软件锁", confirm_text="停止保护并退出", danger=True)
    build_check("SetPasswordDialog(change=True)", SetPasswordDialog, root, store, change=True)
    build_check("SetPasswordDialog(change=False)", SetPasswordDialog, root, store, change=False)
    build_check("SettingsDialog", SettingsDialog, root, store, None)
    build_check("ChoiceDialog", ChoiceDialog, root, "标题", "说明",
                [("取消", False, "ghost", 90), ("确定", True, "primary", 90)])
    build_check("ConfirmDialog", ConfirmDialog, root, "标题", "说明")


def test_c8(root):
    print("\n[C8] 托盘隐藏/恢复 + 图标不可用时不得抛异常", flush=True)
    from ui.tray import Tray
    t = Tray(root, lambda: None, lambda: None, lambda: None, lambda: None)
    raised = None
    try:
        r1 = t.hide_icon()
        r2 = t.show_icon()
    except Exception as exc:
        raised = exc
        r1 = r2 = None
    check("未启动（icon=None）时 hide_icon/show_icon 不抛异常", raised is None, repr(raised))
    check("未启动时二者返回 False", r1 is False and r2 is False, f"{r1},{r2}")

    ok = t.start()
    time.sleep(1.2)
    if ok:
        check("真实托盘启动成功", True)
        check("hide_icon() 返回 True", t.hide_icon() is True)
        time.sleep(0.3)
        check("show_icon() 恢复返回 True", t.show_icon() is True)
    else:
        check("真实托盘启动成功（环境限制则不可测）", False, "托盘启动失败")
    t.stop()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="softlock-qa4c-"))
    store = Store(tmp / "main.json")
    store.set_password("qa-c-123456")

    T.init_dpi_awareness()
    root = tk.Tk()
    T.apply_theme(root)
    root.withdraw()

    try:
        test_c4(root, tmp)
        test_c5(root, tmp)
        test_c6(root, store)
        test_c8(root)
    finally:
        try:
            root.destroy()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 56)
    print(f"对抗用例（界面）：{passed}/{total} 通过", flush=True)
    fails = [n for n, ok, _ in RESULTS if not ok]
    if fails:
        print(f"失败项: {fails}", flush=True)
        return 1
    print("全部通过 OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
