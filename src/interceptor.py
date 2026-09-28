# -*- coding: utf-8 -*-
"""软件锁 —— 后台拦截引擎。

工作原理
--------
守护线程以很短的间隔做「增量」进程扫描（只解析新出现的 PID，开销极小）。
一旦发现受保护应用的可执行文件被创建，立刻：

  1. 挂起该进程（冻结在启动瞬间，避免窗口画出内容）
  2. 隐藏它的可见窗口
  3. 请求 UI 弹出密码验证窗口
  4. 验证通过 -> 恢复窗口并继续运行，path 进入「已解锁」集合
     验证失败 -> 结束整棵进程树

「已放行」集合的意义：同一个可执行文件一旦通过密码放行，在其**放行进程仍然存活**期间，
它自身派生的子进程 / 多开实例不再重复索要密码（例如微信、QQ 的多开，Chrome/Electron
的渲染进程）；**一旦这些放行进程全部退出，放行状态立即注销**（最长只多等一个扫描周期，
约 120 毫秒），下次启动必须重新输入密码。

只有「进程全退出」还不够堵漏洞：有些应用（本地服务 + 浏览器界面型、关窗后留后台进程的）
进程长期活着却**一个可见窗口都没有**——用户早把应用关了，进程却给会话无限续命，之后
每次打开都免密。因此放行会话还要求**至少有一个放行进程持有可见窗口**：完全无窗口持续
``WINDOWLESS_TTL`` 秒且没有「新生儿」进程（见 ``STARTUP_GRACE``）即注销。

放行状态按「进程 + 可见窗口」记账而不是按「时间」记账，是为了堵住这个漏洞：
旧实现用「路径最后一次被观察到存活的时刻 + 3 秒」判断过期，于是用户关掉受保护应用后
3 秒内再打开会被当成「应用还开着」，不再索要密码。
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque

import psutil

import config_store
import winsys

# 界面「启动」发起后，等待目标进程出现的握手窗口；超时未出现即失效，需重新验证
UI_ADOPT_WINDOW = 10.0
# 放行进程退出后仍记住其 pid 的时长，用于识别「应用自身交接」
# （launcher 进程退出、真正进程随后以同一个 exe 启动）
HANDOFF_MEMORY = 60.0
# 放行会话内「完全没有任何可见窗口」持续超过该时长 -> 会话注销。
# 没有窗口说明用户视角里应用已经关了（界面型应用窗口就是应用本身；
# 本地服务 + 浏览器 UI 型应用进程本来就常驻无窗口，更不能给会话续命）。
# 实际取值可被设置项 windowless_unlock_ttl 覆盖，这里只作兜底默认值。
WINDOWLESS_TTL = 3.0
# 会话里存在「新生儿」进程（存活不足该时长）时不做无窗口注销：
# 应用刚放行时是被挂起状态，主窗口要等恢复后初始化完才出现（慢盘/杀软扫描时
# 可能要十几秒），期间派生的子进程也必须免密并账，否则会二次弹密码。
STARTUP_GRACE = 15.0
# 可见窗口检查的间隔（EnumWindows 有开销，不必每帧 120ms 都做一次）
WINDOW_CHECK_INTERVAL = 1.0
# 单次扫描最多解析多少个新进程的可执行路径（防止极端情况卡顿）
MAX_RESOLVE_PER_SCAN = 40


class LockEvent:
    __slots__ = ("ts", "path", "name", "pid", "action", "detail")

    def __init__(self, path: str, name: str, pid: int, action: str, detail: str = ""):
        self.ts = time.time()
        self.path = path
        self.name = name
        self.pid = pid
        self.action = action  # blocked / allowed / error
        self.detail = detail


class Interceptor:
    """后台守护：监控并拦截受保护应用的启动。"""

    def __init__(self, store: config_store.Store, request_auth, on_event=None):
        """
        store        : 配置对象
        request_auth : callable(apps: list[dict], pids: list[int], reason: str) -> bool
        on_event     : callable(LockEvent)，用于刷新界面
        """
        self.store = store
        self.request_auth = request_auth
        self.on_event = on_event

        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._known_pids: dict[int, float] = {}   # pid -> create_time
        self._pid_path: dict[int, str] = {}       # pid -> 命中的受保护路径
        # path -> {已放行进程 pid: create_time}；进程全部退出即整条注销
        self._unlocked: dict[str, dict[int, float]] = {}
        # path -> {刚注销的放行进程 pid: 注销时刻}，用于识别应用自身交接
        self._resigned: dict[str, dict[int, float]] = {}
        # path -> 界面「启动」发起的握手窗口截止时刻
        self._ui_intent: dict[str, float] = {}
        # path -> 最近一次「会话内存在可见窗口」的时刻（无窗口注销计时基准）
        self._last_window_seen: dict[str, float] = {}
        # 上次做可见窗口批量检查的时刻（EnumWindows 有开销，按间隔做）
        self._last_window_check: float = 0.0
        self._pending: dict[str, dict] = {}       # path -> {event, approved, pids, app}
        self._hidden: dict[int, list[int]] = {}   # pid -> 被我们隐藏的窗口句柄

        self.events: deque[LockEvent] = deque(maxlen=200)
        self.last_error: str = ""
        self._paused = False

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="interceptor", daemon=True)
        self._thread.start()
        config_store.log("后台拦截引擎已启动")

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def pause(self, paused: bool = True) -> None:
        """临时暂停扫描（例如用户正在批量编辑列表）。"""
        self._paused = paused

    # ------------------------------------------------------------------ 状态查询
    def unlocked_paths(self) -> set[str]:
        with self._lock:
            return set(self._unlocked)

    def is_unlocked(self, path: str) -> bool:
        with self._lock:
            return config_store.norm_path(path) in self._unlocked

    def is_running(self, path: str) -> bool:
        """该受保护应用当前是否有进程在跑（用于界面「启动 / 关闭」按钮状态）。

        优先看放行账本（几乎零开销），账本里没有时才做一次全量进程比对。
        """
        key = config_store.norm_path(path)
        if not key:
            return False
        with self._lock:
            if key in self._unlocked:
                return True
        return bool(self.running_pids(path))

    def forget(self, path: str) -> None:
        """把某个应用从「已放行」状态移除，下次启动重新要求密码。"""
        key = config_store.norm_path(path)
        with self._lock:
            self._unlocked.pop(key, None)
            self._resigned.pop(key, None)
            self._ui_intent.pop(key, None)
            self._last_window_seen.pop(key, None)

    def forget_all(self) -> None:
        with self._lock:
            self._unlocked.clear()
            self._resigned.clear()
            self._ui_intent.clear()
            self._last_window_seen.clear()

    # ------------------------------------------------------------------ 放行记账
    def _adopt(self, path: str, pids) -> None:
        """把进程纳入某路径的放行集合（调用方需已持有锁）。"""
        bucket = self._unlocked.setdefault(path, {})
        for pid in pids:
            bucket[pid] = self._known_pids.get(pid, 0.0)
        # 无窗口注销计时：首次纳管时从现在起算（只播种不刷新——
        # 会话期间派生的新进程不能给「无窗口」状态续命）
        self._last_window_seen.setdefault(path, time.time())

    def _revoke(self, path: str, now: float, reason: str = "进程已全部退出") -> None:
        """注销某路径的放行状态；被注销的 pid 留档用于识别应用自身交接。"""
        bucket = self._unlocked.pop(path, None)
        if bucket:
            self._resigned[path] = {pid: now for pid in bucket}
            self._last_window_seen.pop(path, None)
            config_store.log(f"放行会话结束（{reason}），恢复保护: {path}")

    def _prune_unlocked(self, now: float, live_pids: set[int], targets: dict) -> None:
        """逐帧校验放行会话，两类情况立即注销（调用方需已持有锁）：

        1. 放行进程全部退出；
        2. 会话虽然还有活进程，但**长时间没有任何可见窗口**——用户视角里应用
           已经关了（本地服务型 / 关窗留后台的应用），不能再给会话续命。
        """
        survivors: dict[str, list[int]] = {}
        for path in list(self._unlocked):
            if path not in targets:               # 应用已被移除 / 停用
                self._unlocked.pop(path, None)
                self._resigned.pop(path, None)
                self._last_window_seen.pop(path, None)
                continue
            alive = {
                pid: ct for pid, ct in self._unlocked[path].items()
                # 既要在进程列表里，create_time 也要对得上（防 pid 被复用后误放行）
                if pid in live_pids and self._known_pids.get(pid, -1.0) == ct
            }
            if alive:
                self._unlocked[path] = alive
                survivors[path] = list(alive)
            else:
                self._revoke(path, now)

        # ---- 无窗口注销：会话内至少要有一个进程持有可见窗口 ----
        if survivors:
            ttl = self._windowless_ttl()
            if now - self._last_window_check >= WINDOW_CHECK_INTERVAL:
                self._last_window_check = now
                try:
                    with_win = winsys.pids_with_visible_windows(
                        pid for pids in survivors.values() for pid in pids)
                except Exception as exc:
                    config_store.log(f"可见窗口检查失败: {exc}", "ERROR")
                    with_win = set()
                for path, pids in survivors.items():
                    if any(pid in with_win for pid in pids):
                        self._last_window_seen[path] = now
            for path, pids in survivors.items():
                if now - self._last_window_seen.get(path, 0.0) <= ttl:
                    continue
                # 存在「新生儿」进程：应用可能刚放行还没画出主窗口，暂不注销
                if any(self._is_young(pid, now) for pid in pids):
                    continue
                self._revoke(path, now, "会话内已无任何可见窗口（应用疑似已关闭）")

        # 清理过期的交接留档
        for path in list(self._resigned):
            kept = {pid: t for pid, t in self._resigned[path].items()
                    if now - t <= HANDOFF_MEMORY}
            if kept:
                self._resigned[path] = kept
            else:
                del self._resigned[path]
        for path in list(self._ui_intent):
            if now > self._ui_intent[path] or path not in targets:
                del self._ui_intent[path]

    def _windowless_ttl(self) -> float:
        """当前生效的「无可见窗口多久后注销放行」时长（秒）。"""
        try:
            value = float(self.store.settings.get("windowless_unlock_ttl",
                                                  WINDOWLESS_TTL))
        except Exception:
            return WINDOWLESS_TTL
        return max(0.5, value)

    def _is_young(self, pid: int, now: float) -> bool:
        """进程是否「新生儿」（刚放行不久，主窗口可能还没画出来）。"""
        ct = self._known_pids.get(pid, 0.0) or 0.0
        if ct <= 0.0:
            return True     # create_time 未知时按新生儿处理，宁可少注销不可误弹密码
        return now - ct < STARTUP_GRACE

    def _is_trusted_origin(self, path: str, pids: list[int], now: float) -> bool:
        """判断这次启动是否免密。只有两个明确来源可以免密：

        1. 软件锁界面刚点过「启动」（握手窗口内）——见 ``launch()``；
        2. 父进程是「刚刚退出、此前被密码放行」的同名进程，即应用自身交接
           （launcher 退出、真正进程随后以同一个 exe 启动）。

        用户在资源管理器/桌面双击时父进程是 explorer，两条都不满足 -> 必须输密码。
        """
        with self._lock:
            if now <= self._ui_intent.get(path, 0.0):
                return True
            resigned = set(self._resigned.get(path, {}))
        if not resigned:
            return False
        for pid in pids:
            if winsys.parent_pid(pid) in resigned:
                return True
        return False

    # ------------------------------------------------------------------ 主循环
    def _loop(self) -> None:
        self._prime()
        while not self._stop.is_set():
            interval = 0.12
            try:
                interval = max(0.05, int(self.store.settings.get("poll_interval_ms", 120)) / 1000.0)
            except Exception:
                pass
            try:
                if not self._paused:
                    self.scan_once()
            except Exception as exc:  # 守护线程绝不能因异常退出
                self.last_error = str(exc)
                config_store.log(f"扫描异常: {exc}", "ERROR")
            self._stop.wait(interval)

    def _prime(self) -> None:
        """首次全量登记，避免把启动前就存在的几百个进程当成新进程。"""
        with self._lock:
            try:
                for proc in psutil.process_iter(["pid", "create_time"]):
                    try:
                        self._known_pids[proc.pid] = proc.info.get("create_time") or 0.0
                    except Exception:
                        continue
            except Exception as exc:
                config_store.log(f"初始化进程快照失败: {exc}", "ERROR")
        # 启动时已在运行的受保护进程，交给 UI 决定是否立即锁定
        if self.store.settings.get("lock_running_on_start", True):
            self._handle_preexisting()

    def _handle_preexisting(self) -> None:
        """启动时发现受保护应用已在运行：直接结束，保证加锁立即生效。"""
        targets = self._target_map()
        if not targets:
            return
        running: dict[str, list[int]] = {}
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = (proc.info.get("name") or "").lower()
            except Exception:
                continue
            if name in targets:
                path = winsys.process_exe(proc.pid)
                if path and config_store.norm_path(path) in targets:
                    running.setdefault(config_store.norm_path(path), []).append(proc.pid)
        if not running:
            return
        total = 0
        for pids in running.values():
            for pid in pids:
                total += winsys.terminate_tree(pid)
        if total:
            self.store.bump_stat("blocked", 1)
            self._emit(LockEvent("", "启动清理", 0, "blocked", f"关闭了 {total} 个进程"))
            config_store.log(f"启动清理：结束了 {total} 个已在运行的受保护进程")

    # ------------------------------------------------------------------ 扫描
    def _target_map(self) -> dict[str, dict]:
        """{归一化路径: app}，同时以文件名作索引辅助快速过滤。"""
        result: dict[str, dict] = {}
        for app in self.store.enabled_apps():
            path = app.get("path") or ""
            if path:
                result[config_store.norm_path(path)] = app
        return result

    def scan_once(self) -> None:
        now = time.time()
        try:
            pids = set(psutil.pids())
        except Exception:
            return

        hits: dict[str, list[int]] = {}

        with self._lock:
            # 清理已退出进程
            for dead in [p for p in self._known_pids if p not in pids]:
                self._known_pids.pop(dead, None)
                self._pid_path.pop(dead, None)
                self._hidden.pop(dead, None)

            new_pids = [p for p in pids if p not in self._known_pids]
            if new_pids:
                targets = self._target_map()
                if targets:
                    name_index: dict[str, str] = {}
                    for tpath in targets:
                        name_index[os.path.basename(tpath)] = tpath
                    for pid in new_pids[:MAX_RESOLVE_PER_SCAN]:
                        self._known_pids[pid] = winsys.process_create_time(pid) or 0.0
                        try:
                            pname = (psutil.Process(pid).name() or "").lower()
                        except Exception:
                            pname = ""
                        if not pname:
                            continue
                        if pname not in name_index:
                            continue
                        exe = winsys.process_exe(pid)
                        if not exe:
                            continue
                        key = config_store.norm_path(exe)
                        if key in targets:
                            self._pid_path[pid] = key
                            hits.setdefault(key, []).append(pid)
                    for pid in new_pids[MAX_RESOLVE_PER_SCAN:]:
                        self._known_pids[pid] = 0.0
                else:
                    for pid in new_pids:
                        self._known_pids[pid] = 0.0

            # 放行会话的存活判定：放行进程一旦全部退出，立即注销（不再等 3 秒）
            targets = self._target_map()
            self._prune_unlocked(now, pids, targets)
            # 应用被移除/停用后，清理掉它的命中记录
            for pid, path in list(self._pid_path.items()):
                if path not in targets:
                    self._pid_path.pop(pid, None)

        if not hits:
            return

        targets = self._target_map()
        for path, pid_list in hits.items():
            app = targets.get(path)
            if not app:
                continue
            with self._lock:
                unlocked = path in self._unlocked
                already = path in self._pending
                if unlocked:
                    # 放行会话运行期间的多开 / 派生进程：并入同一会话
                    self._adopt(path, pid_list)
            if unlocked:
                config_store.log(f"放行（放行会话仍在运行）: {app.get('name')} pid={pid_list}")
                continue
            # 免密只认「软件锁界面发起」与「放行会话自身的派生进程」；
            # 用户从桌面/资源管理器再次双击 -> 父进程是 explorer -> 必须输密码
            if not already and self._is_trusted_origin(path, pid_list, now):
                with self._lock:
                    self._adopt(path, pid_list)
                    self._ui_intent.pop(path, None)
                config_store.log(
                    f"放行（界面启动 / 应用自身派生）: {app.get('name')} pid={pid_list}")
                continue
            # 挂起 + 隐藏（在等待密码期间冻结应用）
            self._freeze(pid_list, app)
            if already:
                with self._lock:
                    group = self._pending.get(path)
                    if group:
                        group["pids"].update(pid_list)
                continue
            self._start_guard(path, app, pid_list)

    # ------------------------------------------------------------------ 拦截处理
    def _freeze(self, pids: list[int], app: dict) -> None:
        """先挂起（防止继续绘制窗口），再枚举并隐藏其可见窗口。"""
        for pid in pids:
            if not winsys.process_alive(pid):
                continue
            winsys.suspend_process(pid)
            try:
                handles = winsys.hide_windows(pid)
            except Exception:
                handles = []
            with self._lock:
                self._hidden.setdefault(pid, []).extend(handles)
        config_store.log(f"已冻结 {app.get('name')} ({app.get('path')}) pids={pids}")

    def _start_guard(self, path: str, app: dict, pids: list[int]) -> None:
        event = threading.Event()
        group = {
            "event": event,
            "approved": False,
            "pids": set(pids),
            "app": app,
            "reason": "launch",
        }
        with self._lock:
            self._pending[path] = group
        worker = threading.Thread(
            target=self._guard_worker, args=(path, group), name=f"guard-{app.get('id')}", daemon=True
        )
        worker.start()

    def _guard_worker(self, path: str, group: dict) -> None:
        app = group["app"]
        approved = False
        timeout = float(self.store.settings.get("prompt_timeout_seconds", 300) or 300)
        try:
            approved = bool(
                self.request_auth([app], sorted(group["pids"]), reason=group["reason"])
            )
        except Exception as exc:
            config_store.log(f"密码验证异常: {exc}", "ERROR")
            approved = False
            # 超时或异常 -> 按拒绝处理
        finally:
            with self._lock:
                current = self._pending.pop(path, None)
                group = current or group
            pids = sorted(group["pids"])
            if approved:
                self._approve(path, app, pids)
            else:
                self._deny(path, app, pids)
            group["event"].set()

    def _approve(self, path: str, app: dict, pids: list[int]) -> None:
        with self._lock:
            # 按 pid 记放行账：这些进程全部退出后，放行状态立即失效
            self._unlocked[path] = {}
            self._adopt(path, pids)
            self._resigned.pop(path, None)
            self._ui_intent.pop(path, None)
            # 无窗口注销的计时基准：从放行这一刻起算（进程刚恢复，窗口还没画出来）
            self._last_window_seen[path] = time.time()
            handles = {pid: self._hidden.pop(pid, []) for pid in pids}
        for pid in pids:
            winsys.restore_windows(handles.get(pid) or [])
            winsys.resume_process(pid)
            winsys.force_foreground(pid)
        self.store.bump_stat("allowed", 1)
        self._emit(
            LockEvent(app.get("path", ""), app.get("name", ""), pids[0] if pids else 0,
                      "allowed", f"{len(pids)} 个进程")
        )
        config_store.log(f"密码验证通过，放行 {app.get('name')} pids={pids}")

    def _deny(self, path: str, app: dict, pids: list[int]) -> None:
        with self._lock:
            for pid in pids:
                self._hidden.pop(pid, None)
        total = 0
        for pid in pids:
            total += winsys.terminate_tree(pid)
        self.store.bump_stat("blocked", 1)
        self._emit(
            LockEvent(app.get("path", ""), app.get("name", ""), pids[0] if pids else 0,
                      "blocked", f"结束 {total} 个进程")
        )
        config_store.log(f"密码验证未通过，已阻止 {app.get('name')}（结束 {total} 个进程）")

    # ------------------------------------------------------------------ 主动启动
    def launch(self, app: dict, remember: bool = True):
        """在验证通过后由界面调用，启动受保护应用。

        返回 (ok, message)。启动前先登记「界面发起」握手窗口，目标进程出现时
        由扫描线程免密纳管（用户从别处启动同一程序不走这条路径）。
        """
        import subprocess

        path = app.get("path") or ""
        if not os.path.isfile(path):
            return False, "文件不存在或已被移动"
        key = config_store.norm_path(path)
        with self._lock:
            if key not in self._unlocked:
                self._ui_intent[key] = time.time() + UI_ADOPT_WINDOW
        if remember and self.is_running(path):
            # 已在运行：不再拉起新副本（会造成「双击两次开出两个窗口」），
            # 直接把已有窗口带到前台交给用户。
            self.focus_running(path)
            config_store.log(f"界面启动 {app.get('name')}：已在运行，改为置前")
            return True, "已在运行"
        workdir = os.path.dirname(path) or None
        try:
            proc = subprocess.Popen([path], cwd=workdir, close_fds=True)
            config_store.log(f"界面启动 {app.get('name')} pid={proc.pid}")
            return True, f"已启动（PID {proc.pid}）"
        except Exception as exc:
            # 需要提权 / 特殊关联的软件走 ShellExecute
            if winsys.open_path(path, workdir):
                config_store.log(f"通过 ShellExecute 启动 {app.get('name')}")
                return True, "已启动"
            with self._lock:
                self._ui_intent.pop(key, None)
            config_store.log(f"启动失败 {path}: {exc}", "ERROR")
            return False, f"启动失败：{exc}"

    def lock_all_running(self) -> int:
        """结束所有正在运行的受保护应用，返回被结束的进程数。"""
        return self.close_all_protected()

    def focus_running(self, path: str) -> bool:
        """把该应用已有窗口带回前台（不拉起新进程）。"""
        for pid in self.running_pids(path):
            try:
                winsys.force_foreground(pid)
                return True
            except Exception:
                continue
        return False

    def running_pids(self, path: str) -> list[int]:
        """枚举某受保护应用当前正在运行的进程 pid（按 exe 路径全量比对）。

        与 ``close_all_protected`` 同源：直接遍历全部进程按可执行文件路径命中，
        不依赖扫描缓存，因此解锁状态下（甚至刚启动还没被扫描到）的实例也能找到。
        """
        target = config_store.norm_path(path)
        if not target:
            return []
        base = os.path.basename(target)
        if not base:
            return []
        result: list[int] = []
        me = os.getpid()
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                pid = proc.info.get("pid") or 0
                if not pid or pid == me:
                    continue
                if (proc.info.get("name") or "").lower() != base:
                    continue
                exe = winsys.process_exe(pid)
                if not exe or config_store.norm_path(exe) != target:
                    continue
                result.append(pid)
            except Exception:
                continue
        return result

    def close_one(self, app: dict) -> int:
        """关闭单个受保护应用，返回被结束的进程数。

        先把该路径从各类放行账本里清掉（否则实例还没死透时扫描线程可能把它
        重新纳管），再结束整棵进程树。同时把「放行后残留的窗口句柄记录」清空，
        避免这些 pid 复用时被误当成我们隐藏过的窗口。
        """
        path = app.get("path") or ""
        key = config_store.norm_path(path)
        pids = self.running_pids(path)
        with self._lock:
            bucket = self._unlocked.pop(key, None)
            self._resigned.pop(key, None)
            self._ui_intent.pop(key, None)
            self._last_window_seen.pop(key, None)
            self._pending.pop(key, None)
            for pid in set(pids) | set(bucket or {}):
                self._hidden.pop(pid, None)
                self._pid_path.pop(pid, None)
        total = 0
        for pid in pids:
            total += winsys.terminate_tree(pid)
        total += sum(winsys.terminate_tree(pid) for pid in (bucket or {}))
        if total:
            self.store.bump_stat("blocked", 1)
            self._emit(LockEvent(path, app.get("name") or "", pids[0] if pids else 0,
                                 "blocked", f"结束 {total} 个进程"))
        config_store.log(
            f"关闭受保护应用 {app.get('name')}：结束 {total} 个进程 "
            f"(pids={pids} 残留放行 pids={sorted(bucket or {})})")
        return total

    def close_all_protected(self) -> int:
        """一键关闭全部受保护应用（按 exe 路径全量枚举，不依赖扫描缓存）。

        之前的 lock_all_running 只处理「扫描引擎已经登记过」的进程，
        错过扫描窗口或在解锁状态下启动的实例可能漏掉；这里改为直接遍历
        全部进程，凡是可执行路径命中受保护列表的一律结束。
        """
        targets = self._target_map()
        if not targets:
            return 0
        names = {os.path.basename(p).lower() for p in targets}
        total = 0
        me = os.getpid()
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                pid = proc.info.get("pid") or 0
                if not pid or pid == me:
                    continue
                if (proc.info.get("name") or "").lower() not in names:
                    continue
                exe = winsys.process_exe(pid)
                if not exe or config_store.norm_path(exe) not in targets:
                    continue
                total += winsys.terminate_tree(pid)
            except Exception:
                continue
        with self._lock:
            self._unlocked.clear()
            self._resigned.clear()
            self._ui_intent.clear()
            self._last_window_seen.clear()
            self._pid_path.clear()
            self._hidden.clear()
        if total:
            self.store.bump_stat("blocked", 1)
            self._emit(LockEvent("", "一键关闭全部", 0, "blocked", f"结束 {total} 个进程"))
        config_store.log(f"一键关闭全部受保护应用：结束 {total} 个进程")
        return total

    # ------------------------------------------------------------------ 事件
    def _emit(self, event: LockEvent) -> None:
        self.events.appendleft(event)
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass
