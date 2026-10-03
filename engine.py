"""engine.py — 运行时大脑

    维护所有跨调用状态：config / token / VID pool / refresh / auto / mitm 生命周期。
    依赖 utils 提供的稳定原子能力。
"""
from __future__ import annotations

import asyncio
import logging
import random
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from utils import (
    Court,
    CONFIG_PATH,
    MITM_HOST, MITM_PORT, MITM_UPSTREAM,
    read_config_dict, write_config_string, write_config_int,
    get_vid_once, get_order_once, save_order_once,
    refresh_window,
    slot_covers, classify_order_reply,
    port_listening, kill_port_holder, spawn_mitmdump,
)


log = logging.getLogger("court")


# ══════════════════════════════════════════════════════
# 可调参数
# ══════════════════════════════════════════════════════
@dataclass
class Tuning:
    refresh_pre_click_delay:  float = 0.05
    refresh_post_click_delay: float = 0.05
    probe_backoff:            float = 1.0
    probe_max_tries:          int   = 6
    auth_refresh_timeout:     float = 4.0
    token_refresh_lead_sec:   int   = 120
    vid_timeout:              float = 3.5
    vid_max_age:              float = 8.0
    vid_pool_size:            int   = 3
    vid_pool_period:          float = 8.0
    mitm_startup_timeout:     float = 12.0
    mitm_shutdown_grace:      float = 3.0


# ══════════════════════════════════════════════════════
# 自动模式状态
# ══════════════════════════════════════════════════════
@dataclass
class AutoState:
    running: bool = False
    phase: str = "idle"
    msg: str = ""
    applied: list = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    plan: dict = field(default_factory=dict)


# ══════════════════════════════════════════════════════
# 事件总线
# ══════════════════════════════════════════════════════
class EventBus:
    def __init__(self, history: int = 500):
        self._subs: set[asyncio.Queue] = set()
        self._hist: deque = deque(maxlen=history)
        self._seq = 0

    @property
    def seq(self) -> int:
        return self._seq

    async def publish(self, topic: str, data: dict):
        self._seq += 1
        evt = {"_seq": self._seq, "_topic": topic, "data": data}
        self._hist.append(evt)
        for q in list(self._subs):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                pass

    def subscribe(self, since: int = 0) -> tuple[asyncio.Queue, list]:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subs.add(q)
        return q, [e for e in self._hist if e["_seq"] > since]

    def unsubscribe(self, q):
        self._subs.discard(q)


# ══════════════════════════════════════════════════════
# 日志缓冲
# ══════════════════════════════════════════════════════
_log_buf: deque = deque(maxlen=800)
_log_seq = 0


class _BufHandler(logging.Handler):
    def emit(self, record):
        global _log_seq
        _log_seq += 1
        _log_buf.append({
            "seq": _log_seq,
            "ts": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "level": record.levelname,
            "msg": record.getMessage(),
        })


def setup_logging():
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    log.addHandler(_BufHandler())
    log.addHandler(logging.StreamHandler())


def get_logs(since: int = 0):
    return [e for e in _log_buf if e["seq"] > since]


# ══════════════════════════════════════════════════════
# Engine
# ══════════════════════════════════════════════════════
class Engine:
    def __init__(self):
        # ── 配置快照 ──
        self.config_data: dict[str, Any] = {}
        self.config_mtime: float = 0.0
        self.tuning = Tuning()

        # ── 派生字段 ──
        self.user_id = ""
        self.customer_id = ""
        self.customer_name = ""
        self.customer_tel = ""
        self.gym_id = ""
        self.gym_name = ""
        self.courts: list[Court] = []
        self.court_by_no: dict[int, Court] = {}

        # ── session ──
        self.token = ""
        self.offset_days = 1

        # ── runtime ──
        self.bus = EventBus()
        self.auto = AutoState()

        self._vid_queue: asyncio.Queue = asyncio.Queue(maxsize=3)
        self._vid_tasks: list[asyncio.Task] = []
        self._vid_stats: deque = deque(maxlen=30)

        self._refresh_probe_task: asyncio.Task | None = None
        self._refresh_tasks: list[asyncio.Task] = []

        self._auto_task: asyncio.Task | None = None

        self._reserve_inflight: set = set()
        self._solve_sem = asyncio.Semaphore(3)

        self._mitm_proc: subprocess.Popen | None = None
        self._mitm_lock: asyncio.Lock | None = None

        self._token_fetch_busy = False

        self._watch_task: asyncio.Task | None = None
        self._boot_task: asyncio.Task | None = None

        # 首帧加载
        self.reload_config(force=True)

    # ══════════════════════════════════════════════════
    # 配置
    # ══════════════════════════════════════════════════
    @property
    def target_date(self) -> str:
        d = datetime.now() + timedelta(days=self.offset_days)
        return d.strftime("%Y-%m-%d")

    def reload_config(self, force: bool = False) -> bool:
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except FileNotFoundError:
            return False
        if not force and mtime == self.config_mtime:
            return False

        try:
            data = read_config_dict()
        except Exception as e:
            log.warning("config.toml 读取失败: %s", e)
            return False

        self.config_data = data
        self.config_mtime = mtime

        u = data.get("user", {}) or {}
        g = data.get("gym", {}) or {}
        o = data.get("order", {}) or {}

        self.user_id       = str(u.get("id", "") or "")
        self.customer_id   = str(u.get("customer_id", "") or "")
        self.customer_name = str(u.get("name", "") or "")
        self.customer_tel  = str(u.get("tel", "") or "")
        self.gym_id        = str(g.get("id", "") or "")
        self.gym_name      = str(g.get("name", "") or "")

        courts_map = {int(k): str(v) for k, v in (data.get("courts", {}) or {}).items()}
        self.courts = [Court(no, cid, f"{no}号场")
                       for no, cid in sorted(courts_map.items())]
        self.court_by_no = {c.no: c for c in self.courts}

        self.token = str(o.get("token", "") or "")
        try:
            self.offset_days = int(o.get("offset_days", 1))
        except Exception:
            self.offset_days = 1
        self.offset_days = max(0, min(2, self.offset_days))

        self._load_tuning()
        return True

    def _load_tuning(self):
        self.tuning = Tuning()
        raw = self.config_data.get("tuning", {}) or {}
        for k, v in raw.items():
            if not hasattr(self.tuning, k):
                log.warning("config.toml [tuning] 未知字段: %s", k)
                continue
            cur = getattr(self.tuning, k)
            try:
                setattr(self.tuning, k, type(cur)(v))
            except (TypeError, ValueError) as e:
                log.warning("config.toml [tuning].%s 值无效: %r (%s)", k, v, e)

    def public_config(self) -> dict:
        tok = self.token or ""
        a = self.config_data.get("auto", {}) or {}
        return {
            "user": self.config_data.get("user", {}),
            "gym":  self.config_data.get("gym", {}),
            "order": {
                "offset_days": self.offset_days,
                "token_set": bool(tok),
                "token_preview": (tok[:8] + "...") if tok else "",
                "target_date": self.target_date,
            },
            "courts": {str(c.no): c.id for c in self.courts},
            "auto": {
                "slot_start": a.get("slot_start", "19:30"),
                "slot_end":   a.get("slot_end", "21:30"),
                "refresh_at": a.get("refresh_at", "20:00:01"),
            },
        }

    def update_offset_days(self, offset: int) -> None:
        offset = max(0, min(2, int(offset)))
        write_config_int("offset_days", offset)

    def update_token(self, token: str) -> None:
        token = token.strip()
        if token:
            write_config_string("token", token)

    def update_auto_config(self, slot_start: str, slot_end: str,
                           refresh_at: str) -> None:
        write_config_string("slot_start", slot_start.strip())
        write_config_string("slot_end",   slot_end.strip())
        write_config_string("refresh_at", refresh_at.strip())

    async def apply_config_change(self):
        self.reload_config(force=True)
        await self.bus.publish("config.updated", self.public_config())

    # ══════════════════════════════════════════════════
    # 生命周期
    # ══════════════════════════════════════════════════
    async def start(self):
        log.info("服务已启动")
        self._boot_task = asyncio.create_task(self._boot_mitm())
        self._watch_task = asyncio.create_task(self._watch_config())

    async def stop(self):
        for t in (self._watch_task, self._boot_task):
            if t is None:
                continue
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await self.stop_vid_pool()
        await self.stop_refresh()
        await self.stop_auto()
        self._kill_mitm()

    async def _watch_config(self, poll: float = 1.0):
        log.info("配置监听已启动: %s", CONFIG_PATH)
        while True:
            try:
                if self.reload_config():
                    log.info("检测到 config.toml 变化: date=%s token=%s courts=%d",
                             self.target_date,
                             (self.token[:8] + "...") if self.token else "空",
                             len(self.courts))
                    await self.bus.publish("config.updated", self.public_config())
            except Exception as e:
                log.warning("配置监听异常: %s", e)
            await asyncio.sleep(poll)

    # ══════════════════════════════════════════════════
    # mitmdump 生命周期（严格单实例：本进程启动，本进程关闭）
    # ══════════════════════════════════════════════════
    def _mitm_lock_get(self) -> asyncio.Lock:
        if self._mitm_lock is None:
            self._mitm_lock = asyncio.Lock()
        return self._mitm_lock

    async def _ensure_mitm(self) -> bool:
        """确保 mitmdump 就绪。

        规则：
            1) 自启进程存活且端口在监听 → 直接 True。
            2) 端口被其他进程占用 → 强杀占用者，再启动新的。
            3) 未启动 → 启动并等待端口监听。
        """
        async with self._mitm_lock_get():
            # 清理已退出的句柄
            if self._mitm_proc is not None and self._mitm_proc.poll() is not None:
                log.warning("[mitm] 之前启动的 mitmdump 已退出 (code=%s)",
                            self._mitm_proc.returncode)
                self._mitm_proc = None

            # 自启进程仍在监听 → 就绪
            if self._mitm_proc is not None and port_listening(MITM_HOST, MITM_PORT, 0.2):
                log.info("[mitm] ✅ mitmproxy 已就绪：%s:%d", MITM_HOST, MITM_PORT)
                return True

            # 端口被外人占用 → 强杀
            if self._mitm_proc is None and port_listening(MITM_HOST, MITM_PORT, 0.2):
                log.warning("[mitm] 端口 %s:%d 被占用，正在结束占用进程…",
                            MITM_HOST, MITM_PORT)
                killed = await asyncio.to_thread(
                    kill_port_holder, MITM_HOST, MITM_PORT)
                if killed:
                    log.info("[mitm] 已结束占用进程 PID: %s", killed)
                else:
                    log.warning("[mitm] 未找到占用进程（可能已自行退出）")
                # 等端口真正释放
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if not port_listening(MITM_HOST, MITM_PORT, 0.2):
                        break
                    await asyncio.sleep(0.1)
                if port_listening(MITM_HOST, MITM_PORT, 0.2):
                    log.error("[mitm] 端口 %s:%d 仍被占用，放弃启动",
                              MITM_HOST, MITM_PORT)
                    return False

            # 未启动或句柄已死 → 拉起
            if self._mitm_proc is None or self._mitm_proc.poll() is not None:
                log.info("[mitm] 正在拉起 mitmdump（%s:%d，无窗口）…",
                         MITM_HOST, MITM_PORT)
                proc = await asyncio.to_thread(spawn_mitmdump)
                if proc is None:
                    return False
                self._mitm_proc = proc

            # 等待端口监听
            deadline = time.monotonic() + self.tuning.mitm_startup_timeout
            while time.monotonic() < deadline:
                if self._mitm_proc.poll() is not None:
                    log.error("[mitm] mitmdump 提前退出 (code=%s)",
                              self._mitm_proc.returncode)
                    self._mitm_proc = None
                    return False
                if port_listening(MITM_HOST, MITM_PORT):
                    log.info("[mitm] ✅ mitmproxy 已就绪：%s:%d", MITM_HOST, MITM_PORT)
                    return True
                await asyncio.sleep(0.2)

            log.error("[mitm] %.0fs 内未监听 %d，放弃",
                      self.tuning.mitm_startup_timeout, MITM_PORT)
            return False

    def _kill_mitm(self):
        proc = self._mitm_proc
        self._mitm_proc = None
        if proc is None or proc.poll() is not None:
            return

        log.info("[mitm] 关闭 mitmdump")
        try:
            proc.terminate()
            try:
                proc.wait(timeout=self.tuning.mitm_shutdown_grace)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
        except Exception as e:
            log.warning("[mitm] 关闭进程异常: %s", e)

    async def _boot_mitm(self):
        try:
            ok = await self._ensure_mitm()
            if not ok:
                log.warning("⚠ mitmdump 未能就绪；点击「自动获取 Token」时会重试")
        except Exception as e:
            log.warning("mitmdump 预启动异常: %s", e)

    def mitm_state(self) -> dict:
        proc = self._mitm_proc
        return {
            "host": MITM_HOST,
            "port": MITM_PORT,
            "port_listening": port_listening(MITM_HOST, MITM_PORT, 0.2),
            "proc_alive": proc is not None and proc.poll() is None,
            "upstream": MITM_UPSTREAM or None,
        }

    async def restart_mitm(self) -> bool:
        self._kill_mitm()
        await asyncio.sleep(0.3)
        return await self._ensure_mitm()

    # ══════════════════════════════════════════════════
    # Token 抓取
    # ══════════════════════════════════════════════════
    async def fetch_token(self) -> dict:
        if self._token_fetch_busy:
            return {"ok": False, "msg": "刷新正在进行，请稍候"}

        self._token_fetch_busy = True
        try:
            ok = await self._ensure_mitm()
            if not ok:
                log.error("❌ mitmdump 未就绪")
                return {
                    "ok": False,
                    "msg": "mitmdump 未就绪，请查看日志",
                }

            log.info("🪟 触发预约窗口刷新（Ctrl+R）…")
            ok = await asyncio.to_thread(
                refresh_window,
                pre_click_delay=self.tuning.refresh_pre_click_delay,
                post_click_delay=self.tuning.refresh_post_click_delay,
            )
            if not ok:
                msg = "未找到预约窗口"
                log.warning("❌ %s", msg)
                return {"ok": False, "msg": msg}

            log.info("✓ 已发送 Ctrl+R，等待 mitmproxy 自动捕获 Token")
            return {
                "ok": True,
                "msg": "已刷新窗口，Token 将由 mitmproxy 自动同步",
            }
        finally:
            self._token_fetch_busy = False

    # ══════════════════════════════════════════════════
    # VID 池（只服务自动模式）
    # ══════════════════════════════════════════════════
    @property
    def vid_running(self) -> bool:
        return any(not t.done() for t in self._vid_tasks)

    def vid_count(self) -> int:
        return self._vid_queue.qsize()

    def vid_state(self) -> dict:
        return {
            "running": self.vid_running,
            "count": self.vid_count(),
            "stats": list(self._vid_stats)[-3:],
        }

    async def start_vid_pool(self) -> dict:
        if self.vid_running:
            return {"ok": True, "started": False, "count": self.vid_count()}

        size = max(1, int(self.tuning.vid_pool_size))
        period = max(1.0, float(self.tuning.vid_pool_period))

        while not self._vid_queue.empty():
            try:
                self._vid_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        stagger = period / size
        self._vid_tasks = [
            asyncio.create_task(self._vid_worker(i + 1, i * stagger, period))
            for i in range(size)
        ]
        log.info("VID 池启动: %d workers / 周期 %.1fs / 有效期 %.1fs / 队列上限 %d",
                 size, period, self.tuning.vid_max_age, self._vid_queue.maxsize)
        return {"ok": True, "started": True, "count": self.vid_count()}

    async def stop_vid_pool(self):
        for t in self._vid_tasks:
            t.cancel()
        if self._vid_tasks:
            await asyncio.gather(*self._vid_tasks, return_exceptions=True)
        self._vid_tasks = []
        log.info("VID 池已停止")

    async def take_vid(self, timeout: float = 5.0,
                       max_age: float | None = None) -> str | None:
        limit = self.tuning.vid_max_age if max_age is None else max_age
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                return None
            try:
                ts, vid = await asyncio.wait_for(self._vid_queue.get(),
                                                 timeout=remain)
            except asyncio.TimeoutError:
                return None
            age = time.monotonic() - ts
            if age <= limit:
                return vid
            log.warning("VID 过期丢弃 (存活 %.1fs > %.1fs)", age, limit)

    async def _vid_worker(self, tid: int, delay: float, period: float):
        if delay > 0:
            await asyncio.sleep(delay)
        while True:
            try:
                vid, timing = await get_vid_once(timeout=self.tuning.vid_timeout)
                self._vid_stats.append({
                    "ok": True,
                    "ts": datetime.now().strftime("%H:%M:%S"),
                    **timing,
                })
                item = (time.monotonic(), vid)
                try:
                    self._vid_queue.put_nowait(item)
                except asyncio.QueueFull:
                    try:
                        self._vid_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        self._vid_queue.put_nowait(item)
                    except asyncio.QueueFull:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._vid_stats.append({
                    "ok": False,
                    "error": str(e),
                    "ts": datetime.now().strftime("%H:%M:%S"),
                })
                log.warning("VID-ERR [T%d] %s", tid, e)
            await asyncio.sleep(period)

    # ══════════════════════════════════════════════════
    # Refresh（刷新场地）
    # ══════════════════════════════════════════════════
    @property
    def refresh_running(self) -> bool:
        if self._refresh_probe_task and not self._refresh_probe_task.done():
            return True
        return any(not t.done() for t in self._refresh_tasks)

    async def start_refresh(self) -> dict:
        if not self.token:
            return {"ok": False, "msg": "未配置 token"}
        await self.stop_refresh()
        pool = list(self.courts)
        random.shuffle(pool)
        if not pool:
            return {"ok": False, "msg": "config.toml 未配置任何场地"}
        log.info("启动刷新: 探测 %s → 成功后并发剩余 %d 个场地 (退避 %.1fs / 最多 %d 次)",
                 pool[0].name, len(pool) - 1,
                 self.tuning.probe_backoff, self.tuning.probe_max_tries)
        self._refresh_probe_task = asyncio.create_task(
            self._refresh_run(self.target_date, self.token, pool)
        )
        return {"ok": True, "total": len(pool)}

    async def stop_refresh(self):
        tasks = [t for t in ([self._refresh_probe_task] + self._refresh_tasks)
                 if t is not None]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._refresh_probe_task = None
        self._refresh_tasks = []

    async def _refresh_run(self, date: str, token: str, pool: list[Court]):
        try:
            await self._refresh_probe_then_fanout(date, token, pool)
        finally:
            await self.bus.publish("refresh.done",
                                   {"ts": datetime.now().strftime("%H:%M:%S")})

    async def _refresh_probe_then_fanout(self, date, token, pool):
        probe = pool[0]
        attempt = 0
        token_refreshed = False
        probe_ok = False
        last_error = ""

        max_tries = max(1, int(self.tuning.probe_max_tries))
        backoff = max(0.0, float(self.tuning.probe_backoff))

        while attempt < max_tries:
            attempt += 1

            if self.token and self.token != token:
                log.info("🔄 探测切换到最新 Token：%s → %s",
                         (token[:8] + "...") if token else "空",
                         self.token[:8] + "...")
                token = self.token

            t0 = time.monotonic()
            res = await get_order_once(probe, date, token, self.user_id)
            dt = (time.monotonic() - t0) * 1000

            if res.get("ok"):
                log.info("探测 %s 成功 (%.0fms, 第 %d 次)，开始并发其余 %d 个场地",
                         probe.name, dt, attempt, len(pool) - 1)
                await self.bus.publish("refresh.result", {
                    "court": probe.no, "name": probe.name, "ok": True,
                    "avail":  res["avail"], "blocks": res["blocks"],
                    "ts": datetime.now().strftime("%H:%M:%S"),
                })
                probe_ok = True
                break

            last_error = res.get("error", "查询失败")

            if res.get("auth_fail"):
                log.warning("探测 %s 鉴权失败 (%.0fms, %d/%d): %s",
                            probe.name, dt, attempt, max_tries, last_error)
                if not token_refreshed:
                    log.info("→ 检测到鉴权失败，自动获取 Token…")
                    try:
                        r = await self.fetch_token(
                            timeout=self.tuning.auth_refresh_timeout)
                    except Exception as e:
                        log.warning("自动获取 Token 异常: %s", e)
                        r = {"ok": False, "msg": str(e)}
                    if r.get("ok"):
                        token_refreshed = True
                        token = self.token
                        log.info("✅ Token 已更新 (%s)，继续探测",
                                 r.get("token_preview", ""))
                        attempt -= 1
                        continue
                    else:
                        log.warning("自动获取 Token 失败: %s，按普通失败继续重试",
                                    r.get("msg"))
                else:
                    log.info("本轮已自动刷新过 Token，不再重复刷新")
            else:
                log.info("探测 %s 失败 (%.0fms, %d/%d)",
                         probe.name, dt, attempt, max_tries)

            if backoff > 0:
                await asyncio.sleep(backoff)

        if not probe_ok:
            log.warning("探测 %s %d 次全部失败，放弃本次刷新", probe.name, max_tries)
            await self.bus.publish("refresh.result", {
                "court": probe.no, "name": probe.name, "ok": False,
                "error": f"探测 {max_tries} 次全部失败: {last_error}",
                "ts": datetime.now().strftime("%H:%M:%S"),
            })
            await self.bus.publish("refresh.done", {
                "ts": datetime.now().strftime("%H:%M:%S"),
                "probe_failed": True,
            })
            return

        self._refresh_tasks = [
            asyncio.create_task(self._refresh_one(c, date, token))
            for c in pool[1:]
        ]
        await asyncio.gather(*self._refresh_tasks, return_exceptions=True)

    async def _refresh_one(self, court: Court, date: str, token: str):
        t0 = time.monotonic()
        res = await get_order_once(court, date, token, self.user_id)
        dt = (time.monotonic() - t0) * 1000

        if res.get("ok"):
            log.info("%s OK (%.0fms)", court.name, dt)
            await self.bus.publish("refresh.result", {
                "court": court.no, "name": court.name, "ok": True,
                "avail":  res["avail"], "blocks": res["blocks"],
                "ts": datetime.now().strftime("%H:%M:%S"),
            })
        else:
            err = res.get("error", "查询失败")
            tag = "鉴权失败" if res.get("auth_fail") else "失败"
            log.info("%s %s (%.0fms): %s", court.name, tag, dt, err)
            await self.bus.publish("refresh.result", {
                "court": court.no, "name": court.name, "ok": False,
                "error": err,
                "ts": datetime.now().strftime("%H:%M:%S"),
            })

    # ══════════════════════════════════════════════════
    # 手动下单（不拉起 VID 池）
    # ══════════════════════════════════════════════════
    async def reserve(self, court_no: int, start: str, end: str) -> dict:
        if not self.token:
            return {"accepted": False, "kind": "err",
                    "label": "❌ 未配置 Token",
                    "msg": "请先保存或获取 Token"}

        court = self.court_by_no.get(court_no)
        if not court:
            return {"accepted": False, "kind": "err",
                    "label": "❌ 非法场地",
                    "msg": f"非法场地号 {court_no}"}

        key = (court.no, start, end)
        if key in self._reserve_inflight:
            return {"accepted": False, "kind": "warn",
                    "label": "⏳ 该时段已在提交",
                    "msg": "同一时段已有请求在飞"}

        self._reserve_inflight.add(key)
        asyncio.create_task(self._reserve_worker(key, court, start, end))

        return {"accepted": True, "court": court.no, "name": court.name,
                "start": start, "end": end}

    async def _reserve_worker(self, key, court: Court, start: str, end: str):
        """手动下单：只解一个 VID，用完即弃。

        * 池在跑 → 顺手从池里取一个（快）。
        * 池没跑 → 现解一个，绝不拉起 pool（避免污染全局状态）。
        """
        try:
            vid: str | None = None

            if self.vid_running:
                vid = await self.take_vid(timeout=1.5)
                if not vid:
                    log.info("手动点击：VID 池暂无货，现解一个…")

            if not vid:
                async with self._solve_sem:
                    vid, _ = await get_vid_once(timeout=self.tuning.vid_timeout)

            r = await self._save_order(vid, court, self.target_date,
                                       start, end, self.token)
            await self._publish_order_result(court, r)
        except Exception as e:
            log.exception("reserve worker 异常: %s", e)
            await self.bus.publish("order.result", {
                "court": court.no, "name": court.name,
                "kind": "error", "label": "❌ 下单异常",
                "msg": str(e),
                "ts": datetime.now().strftime("%H:%M:%S"),
            })
        finally:
            self._reserve_inflight.discard(key)

    async def _save_order(self, vid, court: Court, date: str,
                          start: str, end: str, token: str) -> dict:
        return await save_order_once(
            vid=vid, court=court, date=date, start=start, end=end, token=token,
            user_id=self.user_id,
            customer_id=self.customer_id,
            customer_name=self.customer_name,
            customer_tel=self.customer_tel,
            gym_id=self.gym_id,
            gym_name=self.gym_name,
        )

    async def _publish_order_result(self, court: Court, result: dict):
        kind, label = classify_order_reply(result)
        try:
            await self.bus.publish("order.result", {
                "court": court.no,
                "name":  court.name,
                "kind":  kind,
                "label": label,
                "msg":   str(result.get("msg", "") or ""),
                "code":  result.get("code"),
                "ts":    datetime.now().strftime("%H:%M:%S"),
            })
        except Exception as e:
            log.warning("order.result 事件发布失败: %s", e)

    # ══════════════════════════════════════════════════
    # 全自动流程
    # ══════════════════════════════════════════════════
    # 自动模式排除的场地号
    AUTO_EXCLUDE_COURTS: tuple[int, ...] = (5, 6)

    def auto_state(self) -> dict:
        s = self.auto
        return {
            "running": s.running, "phase": s.phase, "msg": s.msg,
            "applied": s.applied,
            "started_at": s.started_at, "finished_at": s.finished_at,
            "plan": s.plan,
        }

    async def start_auto(self) -> dict:
        if not self.token:
            return {"ok": False, "msg": "未配置 token"}
        if self.auto.running:
            return {"ok": False, "msg": "自动模式已在运行"}

        a = self.config_data.get("auto", {}) or {}

        self.auto.running = True
        self.auto.phase = "init"
        self.auto.msg = "初始化"
        self.auto.applied = []
        self.auto.started_at = datetime.now().strftime("%H:%M:%S")
        self.auto.finished_at = ""
        self.auto.plan = {
            "date": self.target_date,
            "slot_start": a.get("slot_start", "19:30"),
            "slot_end":   a.get("slot_end",   "21:30"),
            "refresh_at": a.get("refresh_at", "20:00:01"),
            "excluded_courts": list(self.AUTO_EXCLUDE_COURTS),
        }
        self._auto_task = asyncio.create_task(self._auto_loop())
        return {"ok": True}

    async def stop_auto(self):
        if self._auto_task and not self._auto_task.done():
            self._auto_task.cancel()
            try:
                await self._auto_task
            except asyncio.CancelledError:
                pass
        self.auto.running = False
        self.auto.phase = "stopped"
        self.auto.msg = "已手动停止"
        self.auto.finished_at = datetime.now().strftime("%H:%M:%S")

    async def _auto_loop(self):
        """自动模式主循环。

        * 全程使用 self.token / self.target_date，与 config.toml 保持一致。
        * 随机遍历场地并排除 AUTO_EXCLUDE_COURTS。
        * 「系统异常」视为场地被占，继续遍历下一个。
        """
        applied: list[dict] = []

        def has_success() -> bool:
            return any(a["ok"] for a in applied)

        def record(court: Court, ok: bool, msg: str = "") -> None:
            applied.append({"court": court.no, "name": court.name,
                            "ok": ok, "msg": msg})
            self.auto.applied = list(applied)

        try:
            a = self.config_data.get("auto", {}) or {}
            slot_start = a.get("slot_start", "19:30")
            slot_end   = a.get("slot_end",   "21:30")
            refresh_at = a.get("refresh_at", "20:00:01")
            refresh_dt = self._parse_hms(refresh_at)

            # ── ① Token 自动刷新 ──
            if datetime.now() < refresh_dt:
                lead = max(0, int(self.tuning.token_refresh_lead_sec))
                tr_dt = refresh_dt - timedelta(seconds=lead)
                if datetime.now() < tr_dt:
                    self.auto.phase = "waiting_token"
                    self.auto.msg = (f"等待自动刷新 Token"
                                     f"（{tr_dt.strftime('%H:%M:%S')}）")
                    log.info("自动模式: Token 将在 %s 自动刷新",
                             tr_dt.strftime('%H:%M:%S'))
                    await self._sleep_until(tr_dt)

                self.auto.phase = "fetching_token"
                self.auto.msg = "自动获取 Token 中…"
                log.info("自动模式: 触发前 %ds，开始自动获取 Token", lead)
                try:
                    r = await self.fetch_token()
                    if r.get("ok"):
                        log.info("自动模式: ✅ Token 刷新成功 (%s)",
                                 r.get("token_preview"))
                        self.auto.msg = f"Token OK ({r.get('token_preview')})"
                    else:
                        log.warning("自动模式: ⚠ Token 刷新失败: %s", r.get("msg"))
                        self.auto.msg = f"Token 刷新失败: {r.get('msg')}"
                except Exception as e:
                    log.warning("自动模式: Token 刷新异常: %s", e)
                    self.auto.msg = f"Token 刷新异常: {e}"
            else:
                log.info("自动模式: 已过触发时间，跳过自动刷新 Token")

            # ── ② 启动 VID 池（自动模式专属） ──
            if not self.vid_running:
                vid_dt = refresh_dt - timedelta(seconds=8)
                if datetime.now() < vid_dt:
                    self.auto.msg = f"等待启动 VID（{vid_dt.strftime('%H:%M:%S')}）"
                    log.info("自动模式: 等待 %s 启动 VID",
                             vid_dt.strftime('%H:%M:%S'))
                    await self._sleep_until(vid_dt)
                log.info("自动模式: 启动 VID 池")
                await self.start_vid_pool()
            else:
                log.info("自动模式: VID 池已在运行，跳过启动")

            # ── ③ 等到触发时间 ──
            self.auto.phase = "waiting"
            self.auto.msg = f"等待 {refresh_at}"
            log.info("自动模式: 等待触发 %s", refresh_at)
            await self._sleep_until(refresh_dt)

            # ── ④ 随机遍历场地（排除 5、6 号） ──
            pool = [c for c in self.courts
                    if c.no not in self.AUTO_EXCLUDE_COURTS]
            random.shuffle(pool)
            total = len(pool)
            log.info("自动模式: 随机遍历 %d 个场地 (时段 %s-%s)，已排除 %s",
                     total, slot_start, slot_end, list(self.AUTO_EXCLUDE_COURTS))

            for idx, court in enumerate(pool, 1):
                if has_success():
                    break

                self.auto.phase = "scanning"
                self.auto.msg = f"扫描 {court.name}（{idx}/{total}）"

                res = await get_order_once(court, self.target_date,
                                           self.token, self.user_id)

                # 鉴权失败 → 刷新 token 后重试一次
                if not res.get("ok") and res.get("auth_fail"):
                    log.warning("自动模式: %s 鉴权失败，自动刷新 Token 后重试",
                                court.name)
                    r = await self.fetch_token(
                        timeout=self.tuning.auth_refresh_timeout)
                    if r.get("ok"):
                        res = await get_order_once(court, self.target_date,
                                                   self.token, self.user_id)
                    else:
                        log.warning("自动模式: Token 刷新失败: %s，继续下一个",
                                    r.get("msg"))
                        await self.bus.publish("refresh.result", {
                            "court": court.no, "name": court.name, "ok": False,
                            "error": f"鉴权失败且 Token 刷新失败: {r.get('msg')}",
                            "ts": datetime.now().strftime("%H:%M:%S"),
                        })
                        continue

                if not res.get("ok"):
                    err = str(res.get("error", "") or "")
                    log.info("自动模式: %s 查询失败: %s → 下一个", court.name, err)
                    await self.bus.publish("refresh.result", {
                        "court": court.no, "name": court.name, "ok": False,
                        "error": err,
                        "ts": datetime.now().strftime("%H:%M:%S"),
                    })
                    continue

                await self.bus.publish("refresh.result", {
                    "court": court.no, "name": court.name, "ok": True,
                    "avail":  res["avail"], "blocks": res["blocks"],
                    "ts": datetime.now().strftime("%H:%M:%S"),
                })

                if not slot_covers(res["avail"], slot_start, slot_end):
                    log.info("自动模式: %s 该时段无空场 → 下一个", court.name)
                    continue

                log.info("自动模式: %s 有空场，立即抢！", court.name)
                self.auto.msg = f"抢 {court.name}…"

                vid = await self.take_vid(timeout=2.0)
                if not vid:
                    log.info("自动模式: VID 池暂无货，同步兜底求解")
                    async with self._solve_sem:
                        vid, _ = await get_vid_once(timeout=self.tuning.vid_timeout)

                r = await self._save_order(vid, court, self.target_date,
                                           slot_start, slot_end, self.token)
                await self._publish_order_result(court, r)

                ok = bool(r.get("success"))
                msg = str(r.get("msg", "") or "")
                record(court, ok, msg)

                if ok:
                    log.info("自动模式: ✅ %s 抢场成功，结束遍历", court.name)
                    break

                # 「系统异常」= 场地被占，继续下一个；系统繁忙等 4s
                if "系统异常" in msg:
                    log.info("自动模式: %s 系统异常（场地被占）→ 下一个", court.name)
                    continue

                kind, _ = classify_order_reply(r)
                if kind == "busy":
                    log.warning("自动模式: %s 系统繁忙，等 4s 后继续下一个",
                                court.name)
                    await asyncio.sleep(4.0)
                    continue

                log.info("自动模式: %s 抢场失败: %s → 下一个", court.name, msg)

            log.info("自动模式: 遍历结束 | 已尝试 %d | 成功 %s",
                     len(applied), "是" if has_success() else "否")

        except asyncio.CancelledError:
            log.info("自动模式: 被取消")
            raise
        except Exception as e:
            log.exception("自动模式异常: %s", e)
        finally:
            if has_success() and self.vid_running:
                log.info("自动模式: 抢场成功，停止 VID 池")
                await self.stop_vid_pool()

            self.auto.running = False
            self.auto.phase = "done"
            self.auto.msg = (f"结束：{'✅ 成功' if has_success() else '❌ 未成功'}"
                             f"（尝试 {len(applied)} 个）")
            self.auto.finished_at = datetime.now().strftime("%H:%M:%S")

    # ══════════════════════════════════════════════════
    # 时间小工具
    # ══════════════════════════════════════════════════
    @staticmethod
    def _parse_hms(t: str) -> datetime:
        parts = [int(x) for x in t.split(":")]
        while len(parts) < 3:
            parts.append(0)
        return datetime.now().replace(hour=parts[0], minute=parts[1],
                                      second=parts[2], microsecond=0)

    @staticmethod
    async def _sleep_until(target: datetime):
        while True:
            remain = (target - datetime.now()).total_seconds()
            if remain <= 0:
                return
            await asyncio.sleep(min(remain, 0.5))


# ══════════════════════════════════════════════════════
# 模块级单例
# ══════════════════════════════════════════════════════
engine = Engine()
setup_logging()