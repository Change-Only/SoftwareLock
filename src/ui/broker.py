# -*- coding: utf-8 -*-
"""跨线程鉴权调度器。

后台拦截线程不能直接操作 Tk 控件，这里把它变成一个「排队 + 阻塞等待」的桥：
后台线程调用 request()/verify()，任务被压入队列，由主线程用 after 轮询取出并弹出
模态密码框，结果通过 threading.Event 回传给后台线程。
"""
from __future__ import annotations

import threading

import config_store

from . import theme as T
from .dialogs import PasswordDialog


class AuthBroker:
    def __init__(self, root, store):
        self.root = root
        self.store = store
        self._queue: list = []
        self._lock = threading.Lock()
        self._busy = False
        self._after_id = None
        self._main_thread = threading.get_ident()
        self._schedule()

    # ------------------------------------------------------------ 对外接口
    def request(self, apps, pids, reason: str = "launch") -> bool:
        """后台线程调用：请求放行某个/某些被拦截的应用。"""
        if isinstance(apps, dict):
            apps = [apps]
        return self._enqueue("auth", {"apps": list(apps), "pids": list(pids), "reason": reason})

    def verify(self, title: str, subtitle: str, confirm_text: str = "确认",
               danger: bool = False, hint: str = "") -> bool:
        """任意线程调用：为一个敏感操作索要密码。"""
        return self._enqueue("verify", {"title": title, "subtitle": subtitle,
                                        "confirm_text": confirm_text, "danger": danger,
                                        "hint": hint})

    # ------------------------------------------------------------ 内部
    def _enqueue(self, kind: str, payload: dict) -> bool:
        # 主线程自己调用时必须直接执行，否则 event.wait 会阻塞掉
        # 唯一能弹出对话框的主线程，形成死锁。
        if threading.get_ident() == self._main_thread:
            try:
                if kind == "auth":
                    return bool(self._handle_auth(payload))
                return bool(self._handle_verify(payload))
            except Exception as exc:
                config_store.log(f"鉴权窗口异常: {exc}", "ERROR")
                return False

        event = threading.Event()
        box = {"approved": False}
        with self._lock:
            self._queue.append((kind, payload, event, box))
        try:
            timeout = float(self.store.settings.get("prompt_timeout_seconds", 300) or 300) + 30
        except Exception:
            timeout = 330
        event.wait(timeout)
        return bool(box.get("approved"))

    def _schedule(self):
        if self._after_id is None:
            try:
                self._after_id = self.root.after(60, self._pump)
            except Exception:
                self._after_id = None

    def _pump(self):
        self._after_id = None
        self._schedule()  # 先排下一次，保证模态框打开期间队列仍在流动
        if self._busy:
            return
        self._busy = True
        try:
            while True:
                with self._lock:
                    if not self._queue:
                        break
                    item = self._queue.pop(0)
                self._handle(item)
        finally:
            self._busy = False

    def _handle(self, item):
        kind, payload, event, box = item
        try:
            if kind == "auth":
                box["approved"] = self._handle_auth(payload)
            else:
                box["approved"] = self._handle_verify(payload)
        except Exception as exc:  # 任何异常都按拒绝处理，绝不放行
            config_store.log(f"鉴权窗口异常: {exc}", "ERROR")
            box["approved"] = False
        finally:
            event.set()

    def _handle_auth(self, payload: dict) -> bool:
        apps = payload.get("apps") or []
        pids = payload.get("pids") or []
        if not apps:
            return False
        app = apps[0]
        name = app.get("name") or "该程序"
        count = f"（{len(pids)} 个进程）" if len(pids) > 1 else ""
        dialog = PasswordDialog(
            self._owner(),
            self.store,
            title=f"启动「{name}」需要验证",
            subtitle=f"「{name}」已被软件锁保护{count}，请输入主密码后继续。",
            confirm_text="解锁并启动",
            timeout=int(self.store.settings.get("prompt_timeout_seconds", 300) or 300),
        )
        return bool(dialog.run())

    def _handle_verify(self, payload: dict) -> bool:
        dialog = PasswordDialog(
            self._owner(),
            self.store,
            title=payload.get("title", "需要验证"),
            subtitle=payload.get("subtitle", "请输入主密码"),
            confirm_text=payload.get("confirm_text", "确认"),
            danger=bool(payload.get("danger", False)),
            hint=payload.get("hint") or "连续输错会进入冷却。",
        )
        return bool(dialog.run())

    def _owner(self):
        return getattr(self, "owner_window", None) or self.root
