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
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
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
    classify_order_reply,
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
    auto_query_batch_timeout: float = 2.0

    # ── VID ──
    vid_timeout:              float = 2.0     # 单次 generate/check 超时
    vid_max_age:              float = 7.9    # VID 有效期（覆盖 30s 提前量）
    vid_pool_size:            int   = 4       # worker 数量
    vid_pool_stagger:         float = 1.0     # worker i 延迟 i*stagger 启动
    vid_pool_interval_min:    float = 3.0     # 每次申请后随机间隔下限
    vid_pool_interval_max:    float = 5.0     # 每次申请后随机间隔上限
    vid_pool_lead_sec:        int   = 30      # 自动模式提前多少秒启动 VID 池

    # ── mitm ──
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
    log.setLevel(logging.DEBUG)
    display = _BufHandler()
    display.setLevel(logging.INFO)
    log.addHandler(display)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    log.addHandler(console)
    folder = Path(__file__).resolve().parent / "logs"
    folder.mkdir(exist_ok=True)
    archive = TimedRotatingFileHandler(folder / "debug.log", when="midnight", backupCount=0, encoding="utf-8")
    archive.suffix = "%Y-%m-%d"
    archive.setLevel(logging.DEBUG)
    archive.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    log.addHandler(archive)


def get_logs(since: int = 0):
    return [e for e in _log_buf if e["seq"] > since]


# ══════════════════════════════════════════════════════
# Engine
# ══════════════════════════════════════════════════════
class Engine:
    # worker i 用 IMPERSONATE_ROTATION[i] —— 4 个 worker 覆盖 4 个不同指纹
    IMPERSONATE_ROTATION = (
        "chrome", "safari17_0", "firefox133",
        "edge101", "safari15_5", "chrome124",
    )

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

        # 队列：4 worker × 4 批缓冲
        self._vid_queue: asyncio.Queue = asyncio.Queue(maxsize=4)
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
                "refresh_at": a.get("refresh_at", "20:00:04"),
                "order_time_gate": bool(a.get("order_time_gate", False)), "pair_order": bool(a.get("pair_order", True)),
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
                           refresh_at: str, order_time_gate: bool = False, pair_order: bool = True) -> None:
        write_config_string("slot_start", slot_start.strip())
        write_config_string("slot_end",   slot_end.strip())
        write_config_string("refresh_at", refresh_at.strip())
        text = CONFIG_PATH.read_text(encoding="utf-8")
        if "order_time_gate =" not in text:
            CONFIG_PATH.write_text(text.replace("[auto]", "[auto]\norder_time_gate = 0", 1), encoding="utf-8")
        write_config_int("order_time_gate", int(order_time_gate))
        write_config_int("pair_order", int(pair_order)) if "pair_order =" in (pair_cfg := CONFIG_PATH.read_text(encoding="utf-8")) else CONFIG_PATH.write_text(pair_cfg.replace("[auto]", f"[auto]\npair_order = {int(pair_order)}", 1), encoding="utf-8")

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

        last_today = datetime.now().date()

        while True:
            try:
                config_changed = self.reload_config()
                today = datetime.now().date()
                day_changed = today != last_today

                if day_changed:
                    log.info(
                        "本地日期跨天: %s → %s | 目标预约日更新为 %s",
                        last_today, today, self.target_date
                    )

                if config_changed or day_changed:
                    await self.bus.publish(
                        "config.updated",
                        self.public_config()
                    )
                if day_changed and not self.auto.running:
                    log.info("日期已更新，自动启动新一天的全自动模式")
                    result = await self.start_auto()

                    if not result.get("ok"):
                        log.warning(
                            "每日全自动启动失败: %s",
                            result.get("msg", "")
                        )

                last_today = today

            except Exception as e:
                log.warning("配置/日期监听异常: %s", e)

            await asyncio.sleep(poll)

    # ══════════════════════════════════════════════════
    # mitmdump 生命周期
    # ══════════════════════════════════════════════════
    def _mitm_lock_get(self) -> asyncio.Lock:
        if self._mitm_lock is None:
            self._mitm_lock = asyncio.Lock()
        return self._mitm_lock

    async def _ensure_mitm(self) -> bool:
        async with self._mitm_lock_get():
            if self._mitm_proc is not None and self._mitm_proc.poll() is not None:
                log.warning("[mitm] 之前启动的 mitmdump 已退出 (code=%s)",
                            self._mitm_proc.returncode)
                self._mitm_proc = None

            if self._mitm_proc is not None and port_listening(MITM_HOST, MITM_PORT, 0.2):
                log.info("[mitm] ✅ mitmproxy 已就绪：%s:%d", MITM_HOST, MITM_PORT)
                return True

            if self._mitm_proc is None and port_listening(MITM_HOST, MITM_PORT, 0.2):
                log.warning("[mitm] 端口 %s:%d 被占用，正在结束占用进程…",
                            MITM_HOST, MITM_PORT)
                killed = await asyncio.to_thread(
                    kill_port_holder, MITM_HOST, MITM_PORT)
                if killed:
                    log.info("[mitm] 已结束占用进程 PID: %s", killed)
                else:
                    log.warning("[mitm] 未找到占用进程（可能已自行退出）")
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if not port_listening(MITM_HOST, MITM_PORT, 0.2):
                        break
                    await asyncio.sleep(0.1)
                if port_listening(MITM_HOST, MITM_PORT, 0.2):
                    log.error("[mitm] 端口 %s:%d 仍被占用，放弃启动",
                              MITM_HOST, MITM_PORT)
                    return False

            if self._mitm_proc is None or self._mitm_proc.poll() is not None:
                log.info("[mitm] 正在拉起 mitmdump（%s:%d，无窗口）…",
                         MITM_HOST, MITM_PORT)
                proc = await asyncio.to_thread(spawn_mitmdump)
                if proc is None:
                    return False
                self._mitm_proc = proc

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
    # VID 池（4 个独立 worker 并发申请，成功后随机间隔 0-6s）
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
            "stats": list(self._vid_stats)[-8:],
        }

    async def start_vid_pool(self) -> dict:
        if self.vid_running:
            return {"ok": True, "started": False, "count": self.vid_count()}

        size = max(1, int(self.tuning.vid_pool_size))
        stagger = max(0.0, float(self.tuning.vid_pool_stagger))
        interval_min = max(0.0, float(self.tuning.vid_pool_interval_min))
        interval_max = max(interval_min, float(self.tuning.vid_pool_interval_max))

        while not self._vid_queue.empty():
            try:
                self._vid_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        self._vid_tasks = [
            asyncio.create_task(
                self._vid_worker(i + 1, i * stagger, interval_min, interval_max)
            )
            for i in range(size)
        ]
        log.info(
            "VID 池启动: %d workers / 错峰 %.2fs / 间隔 %.1f-%.1fs / 有效期 %.1fs / 队列上限 %d / 指纹=%s",
            size, stagger, interval_min, interval_max,
            self.tuning.vid_max_age, self._vid_queue.maxsize,
            ", ".join(self.IMPERSONATE_ROTATION[:size]),
        )
        return {"ok": True, "started": True, "count": self.vid_count()}

    async def stop_vid_pool(self):
        for t in self._vid_tasks:
            t.cancel()
        if self._vid_tasks:
            await asyncio.gather(*self._vid_tasks, return_exceptions=True)
        self._vid_tasks = []
        log.info("VID 池已停止")

    async def _vid_worker(self, tid: int, initial_delay: float,
                          interval_min: float, interval_max: float):
        """单个 VID worker：
           * 错峰启动（worker i 延迟 i*stagger）
           * 成功后 put 到 queue，再 sleep random(interval_min, interval_max)
           * 失败也 sleep random(interval_min, interval_max) 后重试
           * 每个 worker 固定绑定一个指纹（不同 worker 不同指纹）
        """
        impersonate = self.IMPERSONATE_ROTATION[(tid - 1) % len(self.IMPERSONATE_ROTATION)]

        if initial_delay > 0:
            await asyncio.sleep(initial_delay)

        attempt = 0
        while True:
            attempt += 1
            try:
                vid, timing = await get_vid_once(
                    timeout=self.tuning.vid_timeout,
                    impersonate=impersonate,
                )
                self._vid_stats.append({
                    "ok": True, "tid": tid, "imp": impersonate, "attempt": attempt,
                    "ts": datetime.now().strftime("%H:%M:%S"), **timing,
                })
                await self._vid_put(vid)
                if attempt > 1:
                    log.info("VID-OK [T%d/%s] 第 %d 次成功", tid, impersonate, attempt)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._vid_stats.append({
                    "ok": False, "tid": tid, "imp": impersonate, "attempt": attempt,
                    "error": str(e)[:200],
                    "ts": datetime.now().strftime("%H:%M:%S"),
                })
                log.warning("VID-RETRY [T%d/%s] 第 %d 次失败: %s",
                            tid, impersonate, attempt, str(e)[:80])

            wait = random.uniform(interval_min, interval_max)
            await asyncio.sleep(wait)

    async def _vid_put(self, vid: str):
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
        self._refresh_probe_task = asyncio.create_task(self._refresh_run(pool))
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

    async def _refresh_run(self, pool: list[Court]):
        try:
            await self._refresh_probe_then_fanout(pool)
        finally:
            await self.bus.publish("refresh.done",
                                   {"ts": datetime.now().strftime("%H:%M:%S")})

    async def _refresh_probe_then_fanout(self, pool: list[Court]):
        probe, rest = pool[0], pool[1:]
        max_tries = max(1, int(self.tuning.probe_max_tries))
        backoff   = max(0.0, float(self.tuning.probe_backoff))

        ok = False
        for attempt in range(1, max_tries + 1):
            t0 = time.monotonic()
            res = await get_order_once(probe, self.target_date,
                                       self.token, self.user_id)
            dt = (time.monotonic() - t0) * 1000

            if res.get("ok"):
                await self._publish_refresh(probe, res)
                ok = True
                break

            if res.get("auth_fail"):
                log.warning("探测 %s 鉴权失败 (%.0fms, %d/%d): %s，自动获取 Token…",
                            probe.name, dt, attempt, max_tries,
                            res.get("error", ""))
                try:
                    await self.fetch_token()
                except Exception as e:
                    log.warning("自动获取 Token 异常: %s", e)

            if backoff > 0:
                await asyncio.sleep(backoff)

        if not ok:
            log.warning("探测 %s %d 次全部失败，放弃本次刷新", probe.name, max_tries)
            await self._publish_refresh(probe, {
                "ok": False,
                "error": f"探测 {max_tries} 次全部失败",
            })
            return

        await self._refresh_courts(rest)

    async def _refresh_one(self, court: Court, timeout: float | None = None):
        try:
            query = get_order_once(court, self.target_date, self.token, self.user_id)
            res = await asyncio.wait_for(query, timeout) if timeout else await query
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("刷新 %s 失败: %s", court.name, exc)
            res = {"ok": False, "error": str(exc)}
        await self._publish_refresh(court, res)
        return court, res

    async def _refresh_courts(self, courts: list[Court], timeout: float | None = None):
        self._refresh_tasks = [asyncio.create_task(self._refresh_one(c, timeout)) for c in courts]
        try:
            return await asyncio.gather(*self._refresh_tasks)
        finally:
            self._refresh_tasks = []

    async def _publish_refresh(self, court: Court, res: dict):
        evt = {
            "court": court.no,
            "name":  court.name,
            "ok":    bool(res.get("ok")),
            "ts":    datetime.now().strftime("%H:%M:%S"),
        }
        if res.get("ok"):
            evt["avail"]  = res["avail"]
            evt["blocks"] = res["blocks"]
        else:
            evt["error"] = res.get("error", "查询失败")
        if evt["ok"]:
            free = [t for t, status in evt["avail"].items() if str(status) == "1"]
            log.info("刷新 %s：%s", court.name, "有空场（%s）" % ", ".join(sorted(free)) if free else "无空场")
        else:
            log.warning("刷新 %s：查询失败（%s）", court.name, evt["error"])
        await self.bus.publish("refresh.result", evt)

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
        try:
            vid: str | None = None

            if self.vid_running:
                vid = await self.take_vid(timeout=1.5)
                if not vid:
                    log.info("手动点击：VID 池暂无货，现解一个…")

            if not vid:
                async with self._solve_sem:
                    vid, _ = await get_vid_once(timeout=self.tuning.vid_timeout,impersonate="chrome124")

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
    AUTO_COURT_ORDER = (3, 4, 10)

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
            "refresh_at": a.get("refresh_at", "20:00:04"),
            "court_order": list(self.AUTO_COURT_ORDER),
            "query_batch_timeout": max(0.2, float(self.tuning.auto_query_batch_timeout)),
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

    async def _try_auto_order(self, court, start, end):
        try:
            vid = await self.take_vid(timeout=2.0)
            if not vid:
                async with self._solve_sem:
                    vid, _ = await get_vid_once(timeout=self.tuning.vid_timeout)
            r = await self._save_order(vid, court, self.target_date, start, end, self.token)
        except Exception as e: r = {"success": False, "msg": f"下单异常: {type(e).__name__}: {e}"}
        await self._publish_order_result(court, r)
        return bool(r.get("success")), str(r.get("msg", "") or "")

    async def _auto_loop(self):
        applied: list[dict] = []

        def has_success() -> bool:
            return any(a["ok"] for a in applied)

        def record(court: Court, ok: bool, msg: str = "") -> None:
            applied.append({"court": court.no, "name": court.name,
                            "ok": ok, "msg": msg})
            self.auto.applied = list(applied)

        async def _grab_one(court: Court, start: str, end: str) -> None:
            try:
                ok, msg = await self._try_auto_order(court, start, end)
            except Exception as e:
                ok, msg = False, f"异常: {type(e).__name__}: {e}"
            record(court, ok, msg)
            log.info("自动模式: %s 下单%s: %s",
                    court.name, "成功" if ok else "失败", msg)

        try:
            a = self.config_data.get("auto", {}) or {}
            slot_start = a.get("slot_start", "19:30")
            slot_end   = a.get("slot_end",   "21:30")
            refresh_at = a.get("refresh_at", "20:00:02")
            refresh_dt = self._parse_hms(refresh_at) + timedelta(milliseconds=50)

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
                        log.info("自动模式: ✅ 已触发 Token 刷新（异步等待写入配置）")
                        self.auto.msg = "Token 刷新已触发"
                    else:
                        log.warning("自动模式: ⚠ Token 刷新失败: %s", r.get("msg"))
                        self.auto.msg = f"Token 刷新失败: {r.get('msg')}"
                except Exception as e:
                    log.warning("自动模式: Token 刷新异常: %s", e)
                    self.auto.msg = f"Token 刷新异常: {e}"
            else:
                log.info("自动模式: 已过触发时间，跳过自动刷新 Token")

            # ── ② 提前 30s 启动 VID 池，让整点零新建连接 ──
            if not self.vid_running:
                lead_sec = max(1, int(self.tuning.vid_pool_lead_sec))
                vid_dt = refresh_dt - timedelta(seconds=lead_sec)
                if datetime.now() < vid_dt:
                    self.auto.msg = f"等待启动 VID（{vid_dt.strftime('%H:%M:%S')}）"
                    log.info("自动模式: 等待 %s 启动 VID（提前 %ds）",
                            vid_dt.strftime('%H:%M:%S'), lead_sec)
                    await self._sleep_until(vid_dt)
                log.info("自动模式: 启动 VID 池（提前 %ds）", lead_sec)
                await self.start_vid_pool()
            else:
                log.info("自动模式: VID 池已在运行，跳过启动")

            # ── ③ 等到触发时间 ──
            self.auto.phase = "waiting"
            self.auto.msg = f"等待 {refresh_at}"
            log.info("自动模式: 等待触发 %s", refresh_dt.strftime("%H:%M:%S.%f")[:-3])
            await self._sleep_until(refresh_dt)

            # 抢场前重新读取最新配置，支持中途修改
            a2 = self.config_data.get("auto", {}) or {}
            slot_start = a2.get("slot_start", slot_start)
            slot_end   = a2.get("slot_end",   slot_end)
            if (slot_start, slot_end) != (a.get("slot_start"), a.get("slot_end")):
                log.info("自动模式: 检测到时段变更 %s-%s → %s-%s",
                        a.get("slot_start"), a.get("slot_end"),
                        slot_start, slot_end)

            if a2.get("order_time_gate", False):
                await self._sleep_until(
                    datetime.now().replace(hour=20, minute=0, second=4, microsecond=600000)
                )

            # ── ④ 并发抢 5、6 号场 ──
            courts_to_grab: list[Court] = []
            for court_no in self.AUTO_COURT_ORDER:
                c = self.court_by_no.get(court_no)
                if c:
                    courts_to_grab.append(c)
                else:
                    log.warning("自动模式: 场地 %d 未配置，跳过", court_no)

            if not courts_to_grab:
                log.warning("自动模式: 没有可抢的场地")
            else:
                log.info("自动模式: 并发抢 %s（时段 %s-%s，VID 队列 %d）",
                        ", ".join(c.name for c in courts_to_grab),
                        slot_start, slot_end, self.vid_count())
                await asyncio.gather(
                    *[_grab_one(c, slot_start, slot_end) for c in courts_to_grab]
                )

            log.info("自动模式: 并发结束 | 已尝试下单 %d 次 | 成功 %s",
                    len(applied), "是" if has_success() else "否")

        except asyncio.CancelledError:
            log.info("自动模式: 被取消")
            raise
        except Exception as e:
            log.exception("自动模式异常: %s", e)
        finally:
            if self.vid_running:
                log.info("自动模式: 结束，停止 VID 池")
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
        return datetime.now().replace(
            hour=parts[0],
            minute=parts[1],
            second=parts[2],
            microsecond=0,
        )

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