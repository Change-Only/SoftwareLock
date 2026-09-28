# -*- coding: utf-8 -*-
"""UI 自检：完整构建主窗口与所有对话框，验证退出鉴权 / 搜索过滤 / 布局不裁切。

覆盖的可证伪断言：
  * 未通过密码时点击托盘「退出软件锁」「完全退出并停止保护」 -> on_quit 不被调用、
    interceptor.stop 不被调用、窗口仍在；
  * 通过密码后「退出软件锁」 -> 窗口 withdrawn、托盘图标隐藏、on_quit 仍不被调用；
  * 通过密码后「完全退出并停止保护」 -> on_quit 被调用一次；
  * 搜索过滤按 name/path 子串命中，行数符合预期；
  * 每个对话框构建后 req <= win（不裁切）；
  * 密码框 danger 样式与「显示密码」开关。

密码校验通过可注入的 FakeBroker 替换（不改动任何安全逻辑）。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE / "src"))

# 隔离数据目录：必须在 import config_store 之前设置，否则 _writable_dir 会在
# 项目根留下 .write_probe.tmp 探针文件。
UI_DATA_DIR = Path(tempfile.mkdtemp(prefix="softlock-ui-data-"))
os.environ["SOFTLOCK_DATA_DIR"] = str(UI_DATA_DIR)

import tkinter as tk  # noqa: E402

import config_store  # noqa: E402
import winsys  # noqa: E402
from ui import theme as T  # noqa: E402

FAILED: list[str] = []
ERRORS: list[str] = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    if not ok:
        FAILED.append(name)


def guard(name: str, func):
    """执行一段界面代码，任何异常都记下来但不中断整体测试。"""
    try:
        func()
        print(f"  [PASS] {name}", flush=True)
        return True
    except Exception:
        traceback.print_exc()
        ERRORS.append(f"{name}: {traceback.format_exc().strip().splitlines()[-1]}")
        print(f"  [FAIL] {name}", flush=True)
        FAILED.append(name)
        return False


def _walk(widget):
    """深度遍历控件树。"""
    yield widget
    try:
        children = widget.winfo_children()
    except Exception:
        return
    for child in children:
        yield from _walk(child)


def _widget_texts(widget) -> list[str]:
    """收集控件树里的可见文字（Label 的 text + 自绘按钮的 _label_text）。"""
    texts: list[str] = []
    for item in _walk(widget):
        try:
            value = item.cget("text")
            if isinstance(value, str) and value:
                texts.append(value)
        except Exception:
            pass
        label_text = getattr(item, "_label_text", None)
        if isinstance(label_text, str) and label_text:
            texts.append(label_text)
    return texts


class FakeInterceptor:
    """只提供界面需要的接口，并记录 stop 是否被调用。"""

    def __init__(self, store):
        self.store = store
        self.events = []
        self.stop_called = False
        self._unlocked: set = set()
        self._running: set = set()
        self.closed: list = []
        self.focused: list = []
        self.launches: list = []

    def unlocked_paths(self):
        return set(self._unlocked)

    def is_unlocked(self, path):
        return path in self._unlocked

    def is_running(self, path):
        return path in self._unlocked or path in self._running

    def running_pids(self, path):
        return [1234] if self.is_running(path) else []

    def focus_running(self, path):
        self.focused.append(path)
        return True

    def close_one(self, app):
        self.closed.append(app.get("path"))
        self._unlocked.discard(app.get("path"))
        self._running.discard(app.get("path"))
        return 1

    def forget(self, path):
        self._unlocked.discard(path)

    def launch(self, app):
        self.launches.append(app.get("path"))
        return True, "stub"

    def lock_all_running(self):
        return 0

    def stop(self, timeout: float = 2.0):
        self.stop_called = True


class FakeBroker:
    """可注入的鉴权桩：approve 决定 verify 返回值，并记录每次调用的参数。"""

    def __init__(self, approve: bool = True):
        self.approve = approve
        self.calls: list = []

    def verify(self, title, subtitle, confirm_text="确认", danger=False, hint=""):
        self.calls.append({"title": title, "subtitle": subtitle,
                           "confirm_text": confirm_text, "danger": danger, "hint": hint})
        return self.approve


class FakeTray:
    """托盘桩：记录通知、图标隐藏/显示。"""

    def __init__(self):
        self._available = True
        self.hidden = False
        self.show_calls = 0
        self.notified: list = []
        self.title = ""

    def notify(self, message, title="软件锁"):
        self.notified.append(message)

    def hide_icon(self):
        self.hidden = True
        return True

    def show_icon(self):
        self.hidden = False
        self.show_calls += 1
        return True

    def set_title(self, text):
        self.title = text

    def stop(self):
        self._available = False


def _make_apps(store):
    """插入 3 个假应用（2 启用 1 停用），用于搜索过滤与列表渲染。"""
    store.data["apps"] = [
        {"id": "a1", "name": "测试应用一", "path": r"C:\x\one.exe", "enabled": True},
        {"id": "a2", "name": "测试应用二", "path": r"C:\x\two.exe", "enabled": True},
        {"id": "a3", "name": "另一个程序", "path": r"C:\y\three.exe", "enabled": False},
    ]
    store.save()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="softlock-ui-"))
    store = config_store.Store(tmp / "config.json")
    store.set_password("ui-test-123456")
    _make_apps(store)
    exe = Path(sys.executable)

    T.init_dpi_awareness()
    root = tk.Tk()
    T.apply_theme(root)
    print(f"DPI 缩放系数 = {T.SCALE}", flush=True)

    from ui.broker import AuthBroker
    from ui.dialogs import (ChoiceDialog, ConfirmDialog, PasswordDialog,
                            SetPasswordDialog, SettingsDialog, SetupDialog,
                            app_photo, image_photo)
    from ui.main_window import MainWindow
    from ui.widgets import CheckLine, LineEntry, Pill, RoundedButton, ScrollFrame

    # ---------------------------------------------------------------- [1] 基础控件
    print("\n[1] 基础控件", flush=True)
    guard("Pill 绘制", lambda: (Pill(root, "防护中", parent_bg=T.BG).pack(), root.update()))
    guard("RoundedButton 绘制",
          lambda: (RoundedButton(root, "按钮", command=lambda: None, kind="primary",
                                 icon="＋", bg=T.BG).pack(), root.update()))
    guard("LineEntry 构造", lambda: (LineEntry(root, width=200).pack(), root.update()))
    guard("LineEntry 占位提示",
          lambda: _check_placeholder(root, LineEntry))
    guard("CheckLine 构造",
          lambda: (CheckLine(root, "选项", tk.BooleanVar(value=True)).pack(), root.update()))

    def _check_checkline():
        var = tk.BooleanVar(value=False)
        line = CheckLine(root, "选项", var, bg=T.CARD)
        line.pack()
        root.update()
        items = list(line._box.find_all())
        # 未勾选态必须有清晰可见的描边（旧版几乎看不见，这就是「怪异」的根源）
        # 注意：只有 rectangle/arc/oval 类型支持 -outline，line 不支持。
        outlined = any(
            line._box.type(i) in ("rectangle", "arc", "oval")
            and line._box.itemcget(i, "outline") == CheckLine.UNCHECKED_BORDER
            for i in items
        )
        check("CheckLine 未勾选态有可见描边", bool(items) and outlined,
              f"items={len(items)}")
        line.set(True)
        root.update()
        check("CheckLine 勾选态重绘且 get() 同步",
              len(list(line._box.find_all())) >= 1 and line.get() is True)
        line.set(False)
        root.update()
        check("CheckLine set(False) 后 var 同步", var.get() is False)
        line.destroy()
        root.update()

    guard("CheckLine 视觉与状态", _check_checkline)
    guard("ScrollFrame 构造", lambda: (ScrollFrame(root).pack(), root.update()))
    guard("应用图标缓存", lambda: app_photo(32))
    guard("exe 图标提取", lambda: check(
        "exe 图标非空", image_photo(str(exe), "x", 34) is not None))

    # ---------------------------------------------------------------- [2] 主窗口
    print("\n[2] 主窗口", flush=True)
    interceptor = FakeInterceptor(store)
    broker = FakeBroker(approve=True)
    tray = FakeTray()
    quit_calls: list = []

    win = None

    def build_window():
        nonlocal win
        win = MainWindow(root, store, interceptor, broker, tray,
                         lambda then=None: quit_calls.append(1))
        root.update()

    if guard("MainWindow 构建", build_window):
        root.deiconify()
        root.update()

        guard("列表渲染 3 行",
              lambda: (win.refresh(), root.update(),
                       check("列表行数=3", len(win._row_widgets) == 3,
                             f"{len(win._row_widgets)}")))

        # ---------------- 搜索过滤 ----------------
        print("\n[2.1] 搜索过滤", flush=True)
        guard("按名称子串过滤", lambda: _search(win, root, "测试", 2))
        guard("部分名称过滤", lambda: _search(win, root, "应用一", 1))
        guard("按路径子串过滤", lambda: _search(win, root, "three.exe", 1))
        guard("无匹配返回空列表", lambda: _search(win, root, "不存在xyz", 0))
        guard("清空搜索恢复全部", lambda: _search(win, root, "", 3))

        # ---------------- 空态按钮 ----------------
        guard("空态按钮（无匹配）",
              lambda: (win.search.set("没有这个"), root.update(),
                       check("空态已渲染",
                             len(win.scroll.body.winfo_children()) >= 1),
                       win.search.set(""), root.update()))

        class _Evt:
            action = "blocked"
            name = "测试应用一"
            path = str(exe)

        guard("事件回调刷新", lambda: (win._apply_event(_Evt()), root.update()))
        guard("push_event_text", lambda: win.push_event_text("测试"))

        # ---------------- 一键关闭全部受保护应用 ----------------
        def _check_lock_all():
            calls: list = []

            class _I:
                def close_all_protected(self_inner):
                    calls.append(1)
                    return 2

                def unlocked_paths(self_inner):
                    return set()

                def is_unlocked(self_inner, _p):
                    return False

                def forget(self_inner, _p):
                    pass

                def launch(self_inner, _a):
                    return True, ""

            old = win.interceptor
            win.interceptor = _I()
            try:
                win.lock_all()
                root.update()
            finally:
                win.interceptor = old
            check("一键关闭全部走 close_all_protected", len(calls) == 1)

        guard("一键关闭全部应用", _check_lock_all)

        # ---------------- 行内「启动 / 关闭」按运行状态切换 ----------------
        print("\n[2.1.5] 行按钮随运行状态切换", flush=True)

        def _row_action_text(win_obj, index):
            """取第 index 行的操作按钮文字（启动/关闭/移除 中的第一个）。"""
            row = win_obj._row_widgets[index]
            from ui.widgets import RoundedButton
            btns = [w for w in _walk(row) if isinstance(w, RoundedButton)]
            return [b._label_text for b in btns]

        def _check_row_buttons():
            interceptor._unlocked.clear()
            interceptor._running.clear()
            win.refresh()
            root.update()
            texts = _row_action_text(win, 0)
            check("未运行时行按钮为「启动」", "启动" in texts, f"{texts}")
            check("未运行时出现「移除」", "移除" in texts, f"{texts}")

            # 让第一个应用进入运行态
            interceptor._unlocked.add(r"C:\x\one.exe")
            win.refresh()
            root.update()
            texts = _row_action_text(win, 0)
            check("运行时行按钮变「关闭」", "关闭" in texts, f"{texts}")
            check("运行时不再显示「启动」", "启动" not in texts, f"{texts}")
            interceptor._unlocked.clear()

        guard("行按钮启动/关闭切换", _check_row_buttons)

        def _check_close_one():
            calls: list = []
            interceptor._unlocked.add(r"C:\x\one.exe")
            win.refresh()
            root.update()
            old = interceptor.close_one
            interceptor.close_one = lambda a: (calls.append(a.get("path")), 1)[1]
            try:
                win.close_app(win.store.apps[0])
                root.update()
            finally:
                interceptor.close_one = old
            check("行「关闭」调用 close_one", calls == [r"C:\x\one.exe"], f"{calls}")
            interceptor._unlocked.clear()
            win.refresh()
            root.update()

        guard("行「关闭」走单应用关闭", _check_close_one)

        def _check_double_click_no_new_window():
            """双击：未运行 -> 调用 launch；已运行 -> 不拉起新进程。

            注意：必须用**真实存在**的 exe 路径，否则 launch_app 会先弹
            「文件不存在」的 messagebox —— 无头环境下模态框会永久阻塞整个自检。
            """
            interceptor.launches.clear()
            interceptor._unlocked.clear()
            old_apps = list(win.store.apps)
            real = {"id": "real", "name": "真实文件", "path": str(exe), "enabled": True}
            win.store.data["apps"] = [real]
            win.refresh()
            root.update()
            try:
                win.row_double_click(real)
                root.update()
                check("双击未运行应用 -> 触发 launch",
                      interceptor.launches == [str(exe)], f"{interceptor.launches}")

                interceptor.launches.clear()
                interceptor._unlocked.add(str(exe))
                win.refresh()
                root.update()
                win.row_double_click(real)
                root.update()
                check("双击已运行应用 -> 不再拉起新进程",
                      interceptor.launches == [], f"{interceptor.launches}")
            finally:
                interceptor._unlocked.clear()
                win.store.data["apps"] = old_apps
                win.refresh()
                root.update()

        guard("双击不重复启动", _check_double_click_no_new_window)

        def _check_toolbar_text():
            from ui.widgets import RoundedButton
            labels = [w._label_text for w in _walk(win.root) if isinstance(w, RoundedButton)]
            check("工具条文案已改为「关闭全部应用」",
                  "关闭全部应用" in labels, f"{labels[:8]}")
            check("不再出现旧文案「一键关闭全部应用」",
                  "一键关闭全部应用" not in labels)

        guard("工具条文案", _check_toolbar_text)

        # ---------------- 退出鉴权（未通过密码） ----------------
        print("\n[2.2] 退出需密码：未通过时什么都不发生", flush=True)

        def soft_denied():
            broker.approve = False
            interceptor.stop_called = False
            quit_calls.clear()
            win._authenticated = False
            win.exit_soft()
            root.update()
            check("未通过密码 exit_soft -> on_quit 未被调用", len(quit_calls) == 0)
            check("未通过密码 exit_soft -> interceptor.stop 未被调用",
                  not interceptor.stop_called)
            check("未通过密码 exit_soft -> 窗口仍存在", bool(win.root.winfo_exists()))
            check("未通过密码 exit_soft -> 窗口未隐藏",
                  win.root.state() != "withdrawn")

        guard("exit_soft 未通过密码", soft_denied)

        def hard_denied():
            broker.approve = False
            quit_calls.clear()
            win.exit_hard()
            root.update()
            check("未通过密码 exit_hard -> on_quit 未被调用", len(quit_calls) == 0)

        guard("exit_hard 未通过密码", hard_denied)

        # ---------------- 退出鉴权（通过密码） ----------------
        print("\n[2.3] 退出需密码：通过后行为正确", flush=True)

        def soft_allowed():
            broker.approve = True
            broker.calls.clear()
            quit_calls.clear()
            interceptor.stop_called = False
            win._authenticated = False
            win.exit_soft()
            root.update()
            check("通过密码 exit_soft -> on_quit 未被调用", len(quit_calls) == 0)
            check("通过密码 exit_soft -> interceptor.stop 未被调用",
                  not interceptor.stop_called)
            check("通过密码 exit_soft -> 窗口已隐藏",
                  win.root.state() == "withdrawn", win.root.state())
            check("通过密码 exit_soft -> 托盘图标已隐藏", tray.hidden is True)
            check("通过密码 exit_soft -> 已发退出提示", bool(tray.notified))
            check("通过密码 exit_soft -> _authenticated 复位",
                  win._authenticated is False)
            check("退出菜单使用危险色",
                  any(c["danger"] for c in broker.calls))
            check("退出菜单标题正确",
                  any(c["title"] == "退出软件锁" for c in broker.calls))

        guard("exit_soft 通过密码", soft_allowed)

        def hard_allowed():
            broker.approve = True
            broker.calls.clear()
            quit_calls.clear()
            win._authenticated = False
            win.exit_hard()
            root.update()
            check("通过密码 exit_hard -> on_quit 被调用一次", len(quit_calls) == 1)
            check("完全退出使用危险色", any(c["danger"] for c in broker.calls))
            check("完全退出标题正确",
                  any(c["title"] == "完全退出并停止保护" for c in broker.calls))

        guard("exit_hard 通过密码", hard_allowed)

        # 恢复窗口，便于后续托盘/显示逻辑
        broker.approve = True
        win._authenticated = True
        win.show()
        root.update()
        check("show 成功时恢复托盘图标", tray.show_calls >= 1 and tray.hidden is False,
              f"show_calls={tray.show_calls} hidden={tray.hidden}")

    # ---------------------------------------------------------------- [3] 对话框
    print("\n[3] 对话框（含不裁切断言）", flush=True)

    def build_and_check(name, cls, *args, **kwargs):
        def _run():
            dlg = cls(*args, **kwargs)
            # SettingsDialog / ConfirmDialog / ChoiceDialog 把 _finish_init 推迟到 run()，
            # 这里显式调用一次以完成几何计算（幂等），否则量到的是未初始化的 200x200。
            dlg._finish_init()
            root.update()
            req_w, req_h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
            w, h = dlg.winfo_width(), dlg.winfo_height()
            check(f"{name} 不裁切", req_w <= w and req_h <= h,
                  f"req={req_w}x{req_h} win={w}x{h}")
            dlg._close()
            root.update()
        return _run

    guard("初始化向导", build_and_check("SetupDialog", SetupDialog, root, store))
    guard("密码验证框", build_and_check(
        "PasswordDialog", PasswordDialog, root, store,
        title="启动「测试」需要验证", subtitle="请输入密码", timeout=120))
    guard("危险色密码框（退出软件锁）", build_and_check(
        "PasswordDialog(danger)", PasswordDialog, root, store,
        title="退出软件锁",
        subtitle="退出后软件锁会从桌面和托盘消失，但后台守护继续运行，"
                 "受保护的应用仍然需要输入密码才能打开。",
        confirm_text="退出", danger=True))
    guard("危险色密码框（完全退出）", build_and_check(
        "PasswordDialog(danger-hard)", PasswordDialog, root, store,
        title="完全退出并停止保护",
        subtitle="这将彻底结束软件锁，后台守护停止，"
                 "所有受保护应用将不再需要密码即可打开。",
        confirm_text="停止保护并退出", danger=True))
    guard("设置密码（修改）", build_and_check("SetPasswordDialog-change",
                                       SetPasswordDialog, root, store, change=True))
    guard("设置密码（新增）", build_and_check("SetPasswordDialog-new",
                                       SetPasswordDialog, root, store, change=False))
    guard("设置面板", build_and_check("SettingsDialog", SettingsDialog,
                                root, store, interceptor))
    guard("选择框", build_and_check(
        "ChoiceDialog", ChoiceDialog, root, "标题", "说明文字",
        [("取消", False, "ghost", 90), ("确定", True, "primary", 90)],
        width=500, height=300))
    guard("确认框", build_and_check("ConfirmDialog", ConfirmDialog, root, "标题", "说明"))

    def check_settings_exclusion():
        dlg = SettingsDialog(root, store, interceptor)
        dlg._finish_init()
        root.update()
        texts = _widget_texts(dlg.shell)
        check("设置面板出现「杀毒软件误报」行",
              any("杀毒软件误报" in t for t in texts), str(texts[:6]))
        check("设置面板出现「加入白名单」入口",
              any("白名单" in t for t in texts), "")
        dlg._close()
        root.update()

    guard("设置面板：杀毒软件白名单入口", check_settings_exclusion)

    def check_settings_new_options():
        """新增设置：关窗后重新上锁（下拉）与「关闭后立即重新上锁」（勾选），
        两者必须双向同步，且写回 store。"""
        dlg = SettingsDialog(root, store, interceptor)
        dlg._finish_init()
        root.update()
        texts = _widget_texts(dlg.shell)
        check("设置面板出现「关窗后重新上锁」行",
              any("关窗后重新上锁" in t for t in texts), str(texts[:10]))
        check("设置面板出现「关闭后立即重新上锁」勾选",
              any("重新上锁" in t for t in texts), "")

        dlg.windowless.set("立即（0 秒）")
        dlg._on_windowless()
        check("下拉选立即 -> store=0.5s",
              float(store.settings.get("windowless_unlock_ttl")) <= 0.5,
              str(store.settings.get("windowless_unlock_ttl")))
        check("下拉选立即 -> 勾选态自动打开", dlg.relock_var.get() is True)

        dlg.windowless.set("10 秒")
        dlg._on_windowless()
        check("下拉选 10 秒 -> store=10",
              float(store.settings.get("windowless_unlock_ttl")) == 10,
              str(store.settings.get("windowless_unlock_ttl")))
        check("下拉选 10 秒 -> 勾选态自动关闭", dlg.relock_var.get() is False)

        dlg.relock_var.set(True)
        dlg._on_relock()
        check("勾选立即上锁 -> store<=0.5s",
              float(store.settings.get("windowless_unlock_ttl")) <= 0.5)
        check("勾选立即上锁 -> 下拉同步为「立即」",
              dlg.windowless.get().startswith("立即"), dlg.windowless.get())

        dlg.relock_var.set(False)
        dlg._on_relock()
        check("取消勾选 -> 回到 3 秒",
              float(store.settings.get("windowless_unlock_ttl")) == 3,
              str(store.settings.get("windowless_unlock_ttl")))
        dlg._close()
        root.update()

    guard("设置面板：关窗后重新上锁双向同步", check_settings_new_options)

    # ---------------------------------------------------------------- [4] 密码框交互
    print("\n[4] 密码框：显示密码开关 / danger 标题", flush=True)

    def pwd_interactions():
        dlg = PasswordDialog(root, store, title="退出软件锁", subtitle="说明",
                             confirm_text="退出", danger=True)
        root.update()
        check("默认隐藏密码", dlg.entry.entry.cget("show") == "●",
              repr(dlg.entry.entry.cget("show")))
        dlg.show_var.set(True)
        dlg._toggle_show()
        root.update()
        check("打开显示密码后为明文", dlg.entry.entry.cget("show") == "",
              repr(dlg.entry.entry.cget("show")))
        dlg.show_var.set(False)
        dlg._toggle_show()
        root.update()
        check("再次关闭恢复密文", dlg.entry.entry.cget("show") == "●")
        req_w, req_h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        w, h = dlg.winfo_width(), dlg.winfo_height()
        check("密码框不裁切（切换后）", req_w <= w and req_h <= h,
              f"req={req_w}x{req_h} win={w}x{h}")
        dlg._close()
        root.update()

    guard("密码框交互", pwd_interactions)

    print("\n[5] 密码逻辑对接界面", flush=True)
    guard("冷却提示不报错", lambda: (store.register_failure(), root.update()))
    store.reset_failures()

    def check_defender_helpers():
        items = winsys.defender_exclusions()
        check("defender_exclusions 无权限时返回 None（不抛异常）",
              items is None or isinstance(items, list), repr(items)[:60])
        ok, msg = winsys.add_defender_exclusions([])
        check("add_defender_exclusions 空列表安全返回失败",
              ok is False and bool(msg), repr((ok, msg)))
        if winsys.is_elevated():
            print("  [SKIP] 当前已是管理员权限，跳过「非提权应拒绝」的断言", flush=True)
        else:
            ok2, msg2 = winsys.add_defender_exclusions([sys.executable])
            check("非管理员下拒绝写入白名单并说明原因",
                  ok2 is False and "管理员" in msg2, repr((ok2, msg2)))

    guard("Defender 白名单接口容错", check_defender_helpers)

    # ---------------------------------------------------------------- [6] 托盘
    print("\n[6] 真实托盘", flush=True)

    def tray_test():
        from ui.tray import Tray
        t = Tray(root, lambda: None, lambda: None, lambda: None, lambda: None)
        ok = t.start()
        time.sleep(1.2)
        t.set_title("软件锁 · 测试")
        t.notify("测试通知")
        hid = t.hide_icon()
        time.sleep(0.3)
        sho = t.show_icon()
        try:
            menu_texts = [getattr(item, "text", "") for item in t.icon.menu]
        except Exception as exc:
            menu_texts = []
            ERRORS.append(f"读取托盘菜单失败: {exc}")
        t.stop()
        check("托盘可启动", ok)
        check("托盘可隐藏图标", hid)
        check("托盘可恢复图标", sho)
        check("托盘菜单项为「退出软件锁」（不带冗余后缀说明）",
              "退出软件锁" in menu_texts
              and not any("后台继续保护" in str(x) for x in menu_texts),
              str(menu_texts))
    guard("托盘启动/停止/隐藏/恢复", tray_test)

    try:
        root.destroy()
    except Exception:
        pass

    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 52)
    if ERRORS:
        print("异常详情:")
        for item in ERRORS:
            print("  -", item)
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("UI 自检全部通过 OK")
    return 0


def _check_placeholder(root, entry_cls):
    ent = entry_cls(root, width=200, placeholder="搜索应用…")
    ent.pack()
    root.update()
    check("占位提示存在", ent._placeholder == "搜索应用…")
    check("空值时不污染 get()", ent.get() == "")
    ent.destroy()


def _search(win, root, text, expected):
    win.search.set(text)
    root.update()
    check(f"搜索「{text}」-> {expected} 行",
          len(win._row_widgets) == expected, f"{len(win._row_widgets)}")


if __name__ == "__main__":
    sys.exit(main())
