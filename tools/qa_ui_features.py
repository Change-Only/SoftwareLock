# -*- coding: utf-8 -*-
"""QA 第四轮补充：需求 4「界面与体验」关键接线独立核验（非 selftest_ui 覆盖部分）。"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE / "src"))

import tkinter as tk  # noqa: E402

import winsys  # noqa: E402
from config_store import Store  # noqa: E402
from ui import theme as T  # noqa: E402
from ui.widgets import RoundedButton  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    RESULTS.append((name, bool(ok), detail))


class FakeInterceptor:
    def unlocked_paths(self):
        return set()

    def is_unlocked(self, p):
        return False

    def forget(self, p):
        pass

    def launch(self, a):
        return True, "s"

    def lock_all_running(self):
        return 0

    def stop(self, t=2.0):
        pass


class FakeBroker:
    def verify(self, *a, **k):
        return True


class FakeTray:
    _available = True

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


def walk(widget):
    yield widget
    for ch in widget.winfo_children():
        yield from walk(ch)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="softlock-qa4ui-"))
    store = Store(tmp / "config.json")
    store.set_password("ui4-123456")

    T.init_dpi_awareness()
    root = tk.Tk()
    T.apply_theme(root)
    T.SCALE = T.SCALE  # noqa
    root.withdraw()

    from ui.dialogs import PasswordDialog
    from ui.main_window import MainWindow

    # 空态：0 个应用
    win = MainWindow(root, store, FakeInterceptor(), FakeBroker(), FakeTray(),
                     lambda then=None: None)
    root.update()

    # 1. 未提权时橙色提示条存在
    check("未提权时出现管理员提示条 _admin_bar",
          getattr(win, "_admin_bar", None) is not None)
    # 2. 空态引导按钮
    btns = [w for w in walk(win.scroll.body) if isinstance(w, RoundedButton)]
    check("空态存在引导按钮（添加第一个应用）",
          any("添加第一个应用" in getattr(b, "_label_text", "") for b in btns),
          str([getattr(b, "_label_text", "") for b in btns]))
    # 3. 底部「运行中 T」统计
    check("底部统计含「运行中」", "运行中" in win.stats_label.cget("text"),
          repr(win.stats_label.cget("text")))
    # 4. 快捷键绑定
    for key in ("<Control-a>", "<F5>", "<Escape>"):
        check(f"快捷键 {key} 已绑定", bool(root.bind(key)))
    # 5. toast
    win.toast("测试提示", "success")
    root.update()
    check("toast() 生成浮层", win._toast is not None)
    win._hide_toast(win._toast)
    root.update()
    check("toast 可隐藏", win._toast is None)

    # 6. 行双击 / 右键绑定（插入 1 个应用）
    store.data["apps"] = [{"id": "1", "name": "示例", "path": r"C:\x\a.exe",
                           "enabled": True}]
    store.save()
    win.refresh()
    root.update()
    row = win._row_widgets[0]
    check("行绑定双击启动 (<Double-Button-1>)", bool(row.bind("<Double-Button-1>")))
    check("行绑定右键菜单 (<Button-3>)", bool(row.bind("<Button-3>")))

    # 7. 窗口居中：geometry 含预期居中坐标
    geo = root.geometry()
    m = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geo)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    if m:
        w, h, x, y = map(int, m.groups())
        exp_x = max(0, (sw - w) // 2)
        check("窗口水平居中", x == exp_x, f"geo={geo} exp_x={exp_x}")
    else:
        check("窗口 geometry 可解析", False, geo)

    # 8. 密码框 danger 标题用红色
    dlg = PasswordDialog(root, store, title="退出软件锁", subtitle="x", danger=True)
    root.update()
    found = False
    for w in walk(dlg):
        if isinstance(w, tk.Label):
            try:
                if w.cget("text") == "退出软件锁" and str(w.cget("fg")) == T.DANGER:
                    found = True
            except Exception:
                pass
    check("密码框 danger 标题为红色 (T.DANGER)", found, T.DANGER)
    # 9. 显示密码开关存在
    check("密码框含「显示密码」开关", hasattr(dlg, "show_var"))
    dlg._close()
    root.update()

    try:
        root.destroy()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 56)
    print(f"需求4 界面接线：{passed}/{total} 通过", flush=True)
    fails = [n for n, ok, _ in RESULTS if not ok]
    if fails:
        print(f"失败项: {fails}", flush=True)
        return 1
    print("全部通过 OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
