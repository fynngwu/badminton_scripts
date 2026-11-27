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
import threading
import json
from collections import deque
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

from state import AutoState, EventBus, Config, AutoConfig, AutoPlan, Session, Jobs, CurrentOrder

from utils import (
    Court,
    CONFIG_PATH,
    MITM_HOST, MITM_PORT, MITM_UPSTREAM,
    read_config_dict, write_config_string, write_config_int,
    write_config_string_renamed, write_config_int_renamed,
    get_vid_once, get_order_once, save_order_once,
    refresh_window,
    classify_order_reply, format_stamp,
    port_listening, kill_port_holder, spawn_mitmdump,
    close_cffi_session, limited_get, get_cffi_session, is_auth_fail, BASE_URL,
)


log = logging.getLogger("court")


# ══════════════════════════════════════════════════════
# 可调参数
# ══════════════════════════════════════════════════════
@dataclass
class Tuning:
    probe_backoff:            float = 1.0
    probe_max_tries:          int   = 6
    token_refresh_lead_sec:   int   = 120
    token_refresh_wait_sec:   float = 5.0    # 每轮会话刷新后等待新 Token 的时间

    # ── 检漏 ──
    leak_interval:            float = 10.0
    leak_pause_start:         str = "19:55"
    leak_pause_end:           str = "20:05"
    leak_summary_interval:    float = 600.0

    # ── VID ──
    vid_timeout:              float = 2.0
    vid_max_age:              float = 7.9
    vid_pool_size:            int   = 7
    vid_pool_stagger:         float = 1.3
    vid_pool_interval_min:    float = 9.0
    vid_pool_interval_max:    float = 11.0
    vid_pool_lead_sec:        int   = 30

    # ── mitm ──
    mitm_startup_timeout:     float = 12.0
    mitm_shutdown_grace:      float = 3.0


# ══════════════════════════════════════════════════════
# 自动模式状态
# ══════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════
# 日志缓冲
# ══════════════════════════════════════════════════════
_log_buf: deque = deque(maxlen=800)
_log_seq = 0
_log_lock = threading.Lock()


class _BufHandler(logging.Handler):
    def emit(self, record):
        global _log_seq
        with _log_lock:
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
    archive = TimedRotatingFileHandler(folder / "debug.log", when="midnight", backupCount=7, encoding="utf-8")
    archive.suffix = "%Y-%m-%d"
    archive.setLevel(logging.DEBUG)
    archive.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    log.addHandler(archive)


def get_logs(since: int = 0):
    with _log_lock:
        return [e for e in _log_buf if e["seq"] > since]


# ══════════════════════════════════════════════════════
# Engine
# ══════════════════════════════════════════════════════
class Engine:
    IMPERSONATE_ROTATION = (
    "chrome", "safari17_0", "firefox133",
    "edge101", "safari15_5", "chrome124",
    "chrome152",  # 新增：明确的现代 Chrome 指纹
)
    TOKEN_ROUNDS = 3
    # 一次空结果不足以证明未创建；同一空结果持续至少该秒数后才确认 none。
    ORDER_CONFIRM_GRACE = 10.0

    def __init__(self):
        self.config = Config()
        self.session = Session()
        self.config_mtime = 0
        self.tuning = Tuning()
        self.jobs = Jobs()
        self.schedules = {}
        self.leak_found = None
        # ── runtime ──
        self.bus = EventBus()
        self.auto = AutoState()
        self._leak_scanning = False
        self._leak_msg = "未启动"
        self._orders: list[dict] = []
        self._orders_error = "尚未查询"
        self._orders_lock = asyncio.Lock()
        self.current_order: CurrentOrder | None = None
        self._leak_cursor = 0
        self._legacy_court_warned = False

        self._vid_queue: asyncio.Queue = asyncio.Queue(maxsize=6)
        self._vid_stats: deque = deque(maxlen=30)


        self._solve_sem = asyncio.Semaphore(3)

        self._mitm_proc: subprocess.Popen | None = None
        self._mitm_lock: asyncio.Lock | None = None
        self._token_fetch_lock = asyncio.Lock()

        # 首帧加载
        self.reload_config(force=True)

    # ══════════════════════════════════════════════════
    # 配置
    # ══════════════════════════════════════════════════
    @property
    def target_date(self) -> str:
        d = datetime.now() + timedelta(days=self.config.offset_days)
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

        try:
            u, g, o, a = (data.get(k, {}) or {} for k in ("user", "gym", "order", "auto"))
            courts = tuple(Court(int(k), str(v), f"{int(k)}号场")
                           for k, v in sorted(data.get("courts", {}).items(), key=lambda x: int(x[0])))
            court_no = a.get("court_no")
            if isinstance(court_no, bool) or not isinstance(court_no, int) or court_no <= 0:
                court_no = 0
            if not court_no and a.get("court_order") and not self._legacy_court_warned:
                self._legacy_court_warned = True
                log.warning("检测到旧版多场地配置，请重新选择一个目标场地")
            auto = AutoConfig(str(a.get("slot_start", "19:30")),
                              str(a.get("slot_end", "21:30")),
                              str(a.get("save_order_at") or a.get("refresh_at", "20:00:04")),
                              court_no)
            config = Config(*(str(v or "") for v in (u.get("id"), u.get("customer_id"),
                            u.get("name"), u.get("tel"), g.get("id"), g.get("name"))),
                            courts=courts, offset_days=max(0, min(2, int(o.get("offset_days", 1)))), auto=auto)
            session = Session(str(o.get("token", "") or ""))
        except (ValueError, TypeError, AttributeError) as e:
            log.warning("配置无效: %s", e)
            return False
        self.config, self.session, self.config_mtime = config, session, mtime
        return True

    def public_config(self) -> dict:
        return {
            "order": {"offset_days": self.config.offset_days,
                      "token_set": bool(self.session.token),
                      "target_date": self.target_date},
            "courts": {str(c.no): c.id for c in self.config.courts},
            "auto": asdict(self.config.auto),
        }

    def job_states(self):
        return {"auto": self.auto_state(), "leak": self.leak_state(),
                "vid": self.vid_state(), "refresh": {"running": self.refresh_running}}

    def snapshot(self):
        return {"config": self.public_config(), "jobs": self.job_states(),
                "courts": list(self.schedules.values()), "logs": get_logs(),
                "leak_found": self.leak_found, "orders": self.orders_state()}

    async def _publish_state_changes(self):
        """One shared sampler, independent of the number of connected browsers."""
        previous = None
        last_log = 0
        while True:
            jobs = self.job_states()
            current = json.dumps(jobs, sort_keys=True, ensure_ascii=False)
            if current != previous:
                previous = current
                await self.bus.publish("jobs", jobs)
            for entry in get_logs(last_log):
                last_log = entry["seq"]
                await self.bus.publish("log", entry)
            await asyncio.sleep(1)

    @property
    def reserve_inflight(self):
        return self.current_order is not None

    def update_offset_days(self, offset: int) -> None:
        offset = max(0, min(2, int(offset)))
        write_config_int("offset_days", offset)

    def update_token(self, token: str) -> None:
        token = token.strip()
        if token:
            write_config_string("token", token)

    async def update_auto_config(self, slot_start: str, slot_end: str,
                                 save_order_at: str,
                                 court_no: int | None = None) -> None:
        if court_no is not None and court_no not in {c.no for c in self.config.courts}:
            raise ValueError(f"非法场地号 {court_no}，请选择一个已配置的场地")
        write_config_string("slot_start", slot_start.strip())
        write_config_string("slot_end",   slot_end.strip())
        write_config_string_renamed("save_order_at", "refresh_at", save_order_at.strip())
        if court_no is not None:
            write_config_int_renamed("court_no", "court_order", court_no)

    async def apply_config_change(self):
        self.reload_config(force=True)
        await self.bus.publish("config.updated", self.public_config())

    # ══════════════════════════════════════════════════
    # 生命周期
    # ══════════════════════════════════════════════════
    async def start(self):
        log.info("服务已启动")
        self.jobs.start("boot", self._boot_mitm())
        self.jobs.start("watch", self._watch_config())
        self.jobs.start("state", self._publish_state_changes())

    async def stop(self):
        await self.jobs.stop("watch")
        await self.stop_auto()
        await self.jobs.stop_all()
        await self.stop_vid_pool()
        await close_cffi_session()
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
                if day_changed and not self.jobs.running("auto"):
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
        """统一凭证入口：最多三轮会话刷新，拿到与原值不同的新凭证即成功。"""
        self.reload_config(force=True)
        old = self.session.token

        async with self._token_fetch_lock:
            # 并发调用期间若已有其它调用更新了凭证，直接复用，不重复刷新窗口。
            token = self.session.token
            if token and token != old:
                return {"ok": True, "msg": "Token 已更新",
                        "token_preview": token[:8] + "...", "attempts": 0}

            if not await self._ensure_mitm():
                return {"ok": False, "msg": "mitmdump 未就绪，请查看日志"}

            wait = max(0.5, float(self.tuning.token_refresh_wait_sec))
            refreshes = 0
            for round_no in range(1, self.TOKEN_ROUNDS + 1):
                log.info("会话刷新 %d/%d：触发窗口刷新，最多等待 %.1fs 捕获新凭证",
                         round_no, self.TOKEN_ROUNDS, wait)
                refresh_started = time.monotonic()
                try:
                    sent = await asyncio.to_thread(refresh_window)
                except Exception as exc:
                    # 窗口线程异常时无法判断页面是否已被操作，按可能产生凭证处理。
                    sent = True
                    log.warning("预约窗口刷新异常: %s", exc)
                refresh_ms = (time.monotonic() - refresh_started) * 1000
                refreshes += 1
                if not sent:
                    log.warning("预约窗口未确认发送 Ctrl+R，仍检查本轮是否捕获新凭证")

                capture_started = time.monotonic()
                deadline = time.monotonic() + wait
                while True:
                    # 同一轮内的多次观察（A、A、A、B）只有不同于原值的 B 算成功。
                    self.reload_config(force=True)
                    token = self.session.token
                    if token and token != old:
                        capture_ms = (time.monotonic() - capture_started) * 1000
                        await self.bus.publish("config.updated", self.public_config())
                        log.info("✅ Token 已更新（第 %d 轮）: %s...（窗口操作 %.0fms，"
                                 "刷新后捕获 %.0fms，合计 %.0fms）",
                                 round_no, token[:8], refresh_ms,
                                 capture_ms, refresh_ms + capture_ms)
                        return {"ok": True, "msg": f"Token 已更新（第 {round_no} 轮）",
                                "token_preview": token[:8] + "...", "attempts": refreshes}
                    if time.monotonic() >= deadline:
                        break
                    await asyncio.sleep(0.1)
                log.warning("第 %d/%d 轮未获得新 Token（等待 %.1fs）",
                            round_no, self.TOKEN_ROUNDS, wait)

            state = await asyncio.to_thread(self.mitm_state)
            log.warning("Token 获取失败：mitm 端口监听=%s，进程存活=%s；"
                        "请结合 [mitm]/[TOKEN] 日志确认刷新流量是否经过代理",
                        state["port_listening"], state["proc_alive"])

        return {"ok": False,
                "msg": (f"{self.TOKEN_ROUNDS} 轮会话刷新均未获得新 Token"
                        f"（每轮最多等待 {wait:.1f}s）")}

    # ══════════════════════════════════════════════════
    # VID 池
    # ══════════════════════════════════════════════════
    @property
    def vid_running(self) -> bool:
        return self.jobs.running("vid")

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

        self.jobs.start("vid", self._run_vid_pool(size, stagger, interval_min, interval_max))
        log.info(
            "VID 池启动: %d workers / 错峰 %.2fs / 间隔 %.1f-%.1fs / 有效期 %.1fs / 队列上限 %d / 指纹=%s",
            size, stagger, interval_min, interval_max,
            self.tuning.vid_max_age, self._vid_queue.maxsize,
            ", ".join(self.IMPERSONATE_ROTATION[:size]),
        )
        return {"ok": True, "started": True, "count": self.vid_count()}

    async def _run_vid_pool(self, size, stagger, interval_min, interval_max):
        tasks = [asyncio.create_task(self._vid_worker(i + 1, i * stagger, interval_min, interval_max))
                 for i in range(size)]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_vid_pool(self):
        await self.jobs.stop("vid")
        log.info("VID 池已停止")

    async def _vid_worker(self, tid: int, initial_delay: float,
                          interval_min: float, interval_max: float):
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
        return self.jobs.running("refresh")

    async def start_refresh(self, date: str | None = None) -> dict:
        date = date or self.target_date
        if self._leak_scanning:
            return {"ok": False, "msg": "检漏正在查询，请稍后刷新"}
        if not self.session.token:
            return {"ok": False, "msg": "未配置 token"}
        if self.refresh_running:
            return {"ok": True}
        pool = list(self.config.courts)
        random.shuffle(pool)
        if not pool:
            return {"ok": False, "msg": "config.toml 未配置任何场地"}
        log.info("启动刷新: 探测 %s → 成功后并发剩余 %d 个场地 (退避 %.1fs / 最多 %d 次)",
                 pool[0].name, len(pool) - 1,
                 self.tuning.probe_backoff, self.tuning.probe_max_tries)
        self.schedules = {k: v for k, v in self.schedules.items() if k[0] != date}
        await self.bus.publish("refresh.reset", {"date": date})
        self.jobs.start("refresh", self._refresh_run(pool, date))
        return {"ok": True, "total": len(pool)}

    async def stop_refresh(self):
        await self.jobs.stop("refresh")

    async def _refresh_run(self, pool: list[Court], date: str):
        try:
            await self._refresh_probe_then_fanout(pool, date)
        finally:
            await self.bus.publish("refresh.done",
                                   {"date": date, "ts": datetime.now().strftime("%H:%M:%S")})

    async def _refresh_probe_then_fanout(self, pool: list[Court], date: str):
        probe, rest = pool[0], pool[1:]
        max_tries = max(1, int(self.tuning.probe_max_tries))
        backoff   = max(0.0, float(self.tuning.probe_backoff))

        ok = False
        recovered = False
        for attempt in range(1, max_tries + 1):
            t0 = time.monotonic()
            res = await get_order_once(probe, date,
                                       self.session.token, self.config.user_id)
            dt = (time.monotonic() - t0) * 1000

            if res.get("ok"):
                await self._publish_refresh(probe, res, date)
                ok = True
                break

            if res.get("auth_fail") and not recovered:
                # 鉴权失败只恢复一次，避免连续刷新窗口触碰平台访问限制。
                recovered = True
                log.warning("探测 %s 鉴权失败 (%.0fms, %d/%d): %s，自动获取 Token…",
                            probe.name, dt, attempt, max_tries,
                            res.get("error", ""))
                result = await self.fetch_token()
                log.info("探测恢复凭证: %s (%s)", result["msg"],
                         result.get("token_preview", "none"))

            if backoff > 0:
                await asyncio.sleep(backoff)

        if not ok:
            log.warning("探测 %s %d 次全部失败，放弃本次刷新", probe.name, max_tries)
            await self._publish_refresh(probe, {
                "ok": False,
                "error": f"探测 {max_tries} 次全部失败",
            }, date)
            return

        await self._refresh_courts(rest, date)

    async def _refresh_one(self, court: Court, date: str, timeout: float | None = None, *, publish: bool = True, quiet: bool = False):
        try:
            query = get_order_once(court, date, self.session.token, self.config.user_id, quiet=quiet)
            res = await asyncio.wait_for(query, timeout) if timeout else await query
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not quiet:
                log.warning("刷新 %s 失败: %s", court.name, exc)
            res = {"ok": False, "error": str(exc)}
        if publish:
            await self._publish_refresh(court, res, date)
        return court, res

    async def _refresh_courts(self, courts: list[Court], date: str, timeout: float | None = None):
        return await asyncio.gather(*(self._refresh_one(c, date, timeout) for c in courts))

    async def _publish_refresh(self, court: Court, res: dict, date: str):
        evt = {
            "date": date,
            "court": court.no,
            "name":  court.name,
            "ok":    bool(res.get("ok")),
            "ts":    datetime.now().strftime("%H:%M:%S"),
        }
        if res.get("ok"):
            evt["avail"]  = res["avail"]
            evt["blocks"] = [{k: v for k, v in block.items() if k not in ("customerCode", "customerTel")} for block in res["blocks"]]
            self.schedules[(date, court.no)] = evt
            if len(self.schedules) > 100:
                self.schedules.pop(next(iter(self.schedules)))
        else:
            evt["error"] = res.get("error", "查询失败")
        if evt["ok"]:
            free = [t for t, status in evt["avail"].items() if str(status) == "1"]
            log.info("刷新 %s：%s", court.name, "有空场（%s）" % ", ".join(sorted(free)) if free else "无空场")
        else:
            log.warning("刷新 %s：查询失败（%s）", court.name, evt["error"])
        await self.bus.publish("refresh.result", evt)

    # ══════════════════════════════════════════════════
    # 手动下单
    # ══════════════════════════════════════════════════
    async def reserve(self, court_no: int, start: str, end: str, date: str | None = None) -> dict:
        date = date or self.target_date
        if not self.session.token:
            return {"accepted": False, "kind": "err",
                    "label": "❌ 未配置 Token",
                    "msg": "请先保存或获取 Token"}

        court = next((c for c in self.config.courts if c.no == court_no), None)
        if not court:
            return {"accepted": False, "kind": "err",
                    "label": "❌ 非法场地",
                    "msg": f"非法场地号 {court_no}"}

        key = (date, court.no, start, end)
        if self.jobs.running(key):
            return {"accepted": False, "kind": "warn",
                    "label": "⏳ 该时段已在提交",
                    "msg": "同一时段已有请求在飞"}
        order = self.current_order
        if order is not None and order.phase == "unconfirmed":
            status = await self._refresh_and_status(order)
            if status == "created":
                return {"accepted": False, "kind": "warn",
                        "label": "⚠ 上一笔订单已确认",
                        "msg": "上一笔结果不明确的订单已确认成功，请刷新订单"}
            if status == "unknown":
                return {"accepted": False, "kind": "warn",
                        "label": "⚠ 订单结果未确认",
                        "msg": "上一笔订单结果未确认，请先刷新订单核对"}
        if self.current_order is not None:
            return {"accepted": False, "kind": "warn",
                    "label": "⏳ 已有下单任务",
                    "msg": "已有下单任务正在提交，请稍后"}

        order = self._begin_order("manual", court, date, start, end)
        if order is None:
            return {"accepted": False, "kind": "warn",
                    "label": "⏳ 已有下单任务",
                    "msg": "已有下单任务正在提交，请稍后"}
        try:
            self.jobs.start(key, self._reserve_worker(order))
        except Exception:
            self._release_order(order)
            raise

        return {"accepted": True, "court": court.no, "name": court.name,
                "start": start, "end": end, "date": date}

    def _begin_order(self, scope: str, court: Court, date: str,
                     start: str, end: str,
                     record: dict | None = None) -> CurrentOrder | None:
        if self.current_order is not None:
            return None
        order = CurrentOrder(scope=scope, date=date, court=court,
                             start=start, end=end, record=record)
        self.current_order = order
        return order

    def _release_order(self, order: CurrentOrder) -> None:
        if self.current_order is order:
            self.current_order = None

    async def _try_order(self, order: CurrentOrder, *,
                         announce: bool = False) -> tuple[str, str, dict]:
        started = datetime.now()
        try:
            if announce and order.record is not None:
                await self.bus.publish("leak.found", order.record)
            vid: str | None = None
            if self.vid_running:
                vid = await self.take_vid(timeout=1.5)
                if not vid:
                    log.info("VID 池暂无货，现解一个…")
            if not vid:
                async with self._solve_sem:
                    vid, _ = await get_vid_once(timeout=self.tuning.vid_timeout,
                                                impersonate="chrome124")
            order.phase = "sending"
            order.sent_at = format_stamp()
            r = await self._save_order(vid, order.court, order.date,
                                       order.start, order.end, self.session.token)
            order.returned_at = str(r.get("returned_at") or format_stamp())
            if r.get("sent_at"):
                order.sent_at = str(r["sent_at"])
        except asyncio.CancelledError:
            if order.phase == "preparing":
                self._release_order(order)
                if order.record is not None:
                    order.record.update(result="cancelled", msg="下单任务已取消",
                                        confirmed_at="")
            else:
                order.phase = "unconfirmed"
                order.returned_at = order.returned_at or format_stamp()
                if order.record is not None:
                    order.record.update(result="uncertain",
                                        sent_at=order.sent_at,
                                        returned_at=order.returned_at,
                                        confirmed_at="",
                                        msg="下单请求已取消，结果未确认")
            raise
        except Exception as e:
            if order.phase == "sending":
                log.warning("下单结果异常 %s: %s", order.court.name, e)
                order.phase = "unconfirmed"
                order.returned_at = order.returned_at or format_stamp()
                kind, msg = "uncertain", f"下单请求结果未确认: {e}"
            else:
                log.warning("下单准备失败 %s: %s", order.court.name, e)
                self._release_order(order)
                kind, msg = "error", f"下单准备失败: {e}"
            outcome = {
                "sent_at": order.sent_at if kind == "uncertain" else "",
                "returned_at": order.returned_at if kind == "uncertain" else "",
                "elapsed_ms": int((datetime.now() - started).total_seconds() * 1000),
                "code": None,
                "msg": msg,
            }
            return kind, msg, outcome
        kind, label = classify_order_reply(r)
        if kind == "uncertain":
            order.phase = "unconfirmed"
        else:
            order.phase = "result"
            self._release_order(order)
        outcome = {
            "sent_at": order.sent_at,
            "returned_at": order.returned_at,
            "elapsed_ms": int(r.get("elapsed_ms") or
                              (datetime.now() - started).total_seconds() * 1000),
            "code": r.get("code"),
            "msg": r.get("msg"),
        }
        return kind, str(r.get("msg") or label), outcome

    def _record_outcome(self, order: CurrentOrder, kind: str,
                        msg: str, outcome: dict) -> None:
        if order.record is None:
            return
        sent_at = str(outcome.get("sent_at") or "")
        returned_at = str(outcome.get("returned_at") or "")
        order.record.update(
            sent_at=sent_at,
            returned_at=returned_at,
            result=kind,
            msg=msg,
            confirmed_at=returned_at if kind != "uncertain" and returned_at else "",
            elapsed_ms=int(outcome.get("elapsed_ms") or 0) if kind != "uncertain" else 0,
        )

    async def _reserve_worker(self, order: CurrentOrder):
        try:
            kind, msg, result = await self._try_order(order)
            await self._publish_order_result(order.court, order.date,
                                             kind, msg, result)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("reserve worker 异常: %s", e)
            await self.bus.publish("order.result", {
                "court": order.court.no, "name": order.court.name, "date": order.date,
                "kind": "error", "label": "❌ 下单异常",
                "msg": str(e),
                "ts": datetime.now().strftime("%H:%M:%S"),
            })

    @staticmethod
    def _order_matches(order: dict, date: str, court: Court,
                       start: str, end: str) -> bool:
        name = str(order.get("court") or "")
        same_court = (str(order.get("court_id") or "") == court.id
                      or name == court.name
                      or name.startswith(f"{court.no}号"))
        return (order.get("date") == date and order.get("start") == start
                and order.get("end") == end and same_court)

    async def _save_order(self, vid, court: Court, date: str,
                          start: str, end: str, token: str) -> dict:
        return await save_order_once(
            vid=vid, court=court, date=date, start=start, end=end, token=token,
            user_id=self.config.user_id,
            customer_id=self.config.customer_id,
            customer_name=self.config.customer_name,
            customer_tel=self.config.customer_tel,
            gym_id=self.config.gym_id,
            gym_name=self.config.gym_name,
        )

    async def _publish_order_result(self, court: Court, date: str, kind: str,
                                    msg: str, result: dict | None = None):
        labels = {"ok": "✅ 预约成功", "busy": "⚠ 系统繁忙",
                  "uncertain": "⚠ 结果不明确，正在核对订单",
                  "error": "❌ 下单失败"}
        try:
            await self.bus.publish("order.result", {
                "date": date,
                "court": court.no,
                "name":  court.name,
                "kind":  kind,
                "label": labels.get(kind, "❌ 下单失败"),
                "msg":   msg,
                "code":  (result or {}).get("code"),
                "ts":    datetime.now().strftime("%H:%M:%S"),
            })
        except Exception as e:
            log.warning("order.result 事件发布失败: %s", e)

    # ══════════════════════════════════════════════════
    # 全自动流程
    # ══════════════════════════════════════════════════
    def auto_state(self) -> dict:
        s = self.auto
        return {
            "running": self.jobs.running("auto"), "phase": s.phase, "msg": s.msg,
            "applied": s.applied,
            "started_at": s.started_at, "finished_at": s.finished_at,
            "plan": ({"date": s.plan.date, "court": s.plan.court.no,
                      **asdict(s.plan.config)} if s.plan else {}),
        }

    async def start_auto(self) -> dict:
        if not self.session.token:
            return {"ok": False, "msg": "未配置 token"}
        if self.jobs.running("auto"):
            return {"ok": False, "msg": "自动模式已在运行"}

        a = self.config.auto
        court = next((c for c in self.config.courts if c.no == a.court_no), None)
        if court is None:
            return {"ok": False, "msg": "请选择一个唯一目标场地（court_no）"}

        self.auto.phase = "init"
        self.auto.msg = "初始化"
        self.auto.applied = []
        self.auto.started_at = datetime.now().strftime("%H:%M:%S")
        self.auto.finished_at = ""
        self.auto.plan = AutoPlan(self.target_date, a, court)
        self.jobs.start("auto", self._auto_loop())
        return {"ok": True}

    async def stop_auto(self):
        if not self.jobs.running("auto"):
            return
        await self.jobs.stop("auto")
        self.auto.phase = "stopped"
        self.auto.msg = "已手动停止"
        self.auto.finished_at = datetime.now().strftime("%H:%M:%S")

    async def _auto_loop(self):
        applied: list[dict] = []
        plan = self.auto.plan
        assert plan is not None
        court = plan.court
        date = plan.date

        def record(entry: dict) -> None:
            applied.append(entry)
            self.auto.applied = list(applied)

        try:
            slot_start = plan.config.slot_start
            slot_end = plan.config.slot_end
            save_dt = self._parse_hms(plan.config.save_order_at)

            # ── ① Token 自动刷新 ──
            if datetime.now() < save_dt:
                lead = max(0, int(self.tuning.token_refresh_lead_sec))
                tr_dt = save_dt - timedelta(seconds=lead)
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
                r = await self.fetch_token()
                if r.get("ok"):
                    log.info("自动模式: ✅ Token 已捕获 (%s)", r.get("token_preview", ""))
                    self.auto.msg = f"Token 已捕获（{r.get('token_preview', '')}）"
                else:
                    log.warning("自动模式: ⚠ Token 刷新失败: %s", r.get("msg"))
                    self.auto.msg = f"Token 刷新失败: {r.get('msg')}"
            else:
                log.info("自动模式: 已过触发时间，跳过自动刷新 Token")

            # ── ② 提前 30s 启动 VID 池，让整点零新建连接 ──
            if not self.vid_running:
                lead_sec = max(1, int(self.tuning.vid_pool_lead_sec))
                vid_dt = save_dt - timedelta(seconds=lead_sec)
                if datetime.now() < vid_dt:
                    self.auto.msg = f"等待启动 VID（{vid_dt.strftime('%H:%M:%S')}）"
                    log.info("自动模式: 等待 %s 启动 VID（提前 %ds）",
                            vid_dt.strftime('%H:%M:%S'), lead_sec)
                    await self._sleep_until(vid_dt)
                log.info("自动模式: 启动 VID 池（提前 %ds）", lead_sec)
                await self.start_vid_pool()
            else:
                log.info("自动模式: VID 池已在运行，跳过启动")

            # ── ③ 等待 Save Order At，向唯一目标场地下单一次 ──
            self.auto.phase = "waiting"
            self.auto.msg = f"等待下单（{save_dt.strftime('%H:%M:%S.%f')[:-3]}）"
            log.info("自动模式: 等待下单 %s（目标 %s %s-%s）",
                     save_dt.strftime("%H:%M:%S.%f")[:-3],
                     court.name, slot_start, slot_end)
            await self._sleep_until(save_dt)

            self.auto.phase = "ordering"
            self.auto.msg = f"{court.name} 正在下单"
            log.info("自动模式: 已到触发时刻，向 %s 提交唯一一次预约", court.name)
            entry = {
                "court": court.no, "name": court.name,
                "msg": "", "result": "pending",
                "scheduled_at": format_stamp(save_dt),
                "sent_at": "", "returned_at": "", "confirmed_at": "",
                "elapsed_ms": 0,
            }
            order = self._begin_order("auto", court, date,
                                      slot_start, slot_end, entry)
            record(entry)
            if order is None:
                kind, msg = "error", "已有下单任务在提交"
                entry.update(result=kind, msg=msg)
                await self._publish_order_result(court, date, kind, msg)
            else:
                kind, msg, outcome = await self._try_order(order)
                self._record_outcome(order, kind, msg, outcome)
                if kind == "uncertain":
                    status = await self._refresh_and_status(order)
                    if status == "created":
                        kind, msg = "ok", "订单已确认"
                    elif status == "none":
                        kind, msg = "error", "确认未产生订单"
                    else:
                        msg = f"结果不明确且订单核对失败：{msg}"
                entry.update(result=kind, msg=msg)
                await self._publish_order_result(court, date, kind, msg, outcome)
            log.info("自动模式: %s 下单%s: %s",
                     court.name, "成功" if kind == "ok" else "失败", msg)

        except asyncio.CancelledError:
            log.info("自动模式: 被取消")
            raise
        except Exception as e:
            log.exception("自动模式异常: %s", e)
        finally:
            if self.vid_running:
                log.info("自动模式: 结束，停止 VID 池")
                await self.stop_vid_pool()

            self.auto.phase = "done"
            self.auto.msg = (f"结束：{'✅ 成功' if applied and applied[0]['result'] == 'ok' else '❌ 未成功'}"
                            f"（目标 {court.name}）")
            self.auto.finished_at = datetime.now().strftime("%H:%M:%S")

    # ══════════════════════════════════════════════════
    # 当前有效订单：服务器是唯一事实来源
    # ══════════════════════════════════════════════════
    def orders_state(self):
        return {"ok": not self._orders_error, "msg": self._orders_error,
                "orders": list(self._orders)}

    def _booked_dates(self):
        if self._orders_error:
            return set()
        return {o["date"] for o in self._orders
                if o["gym_id"] == str(self.config.gym_id)}

    async def _order_api(self, endpoint, *, method="GET", params=None,
                         payload=None, recover=True):
        url = BASE_URL + "/api/blade-app/qywx/" + endpoint
        recovered = False
        for attempt in range(2 if recover else 1):
            token = self.session.token
            if not token:
                if recover and attempt == 0:
                    log.warning("订单查询 %s 无有效 Token，自动获取 Token…", endpoint)
                    if (await self.fetch_token()).get("ok"):
                        recovered = True
                        continue
                raise RuntimeError("未取得有效 Token")
            query = {**(params or {}), "userid": self.config.user_id, "token": token}
            if method == "GET":
                response = await limited_get(url, params=query, quiet=True)
            else:
                kwargs = {"params": query}
                if payload is not None:
                    kwargs["json"] = payload
                response = await get_cffi_session().post(url, **kwargs)
            try:
                result = response.json()
            except ValueError:
                result = {}
            if not isinstance(result, dict):
                raise RuntimeError("订单接口响应格式异常")
            if response.status_code in (401, 403) or is_auth_fail(result):
                if recover and attempt == 0:
                    log.warning("订单查询 %s 鉴权失败：HTTP %d | code=%s | msg=%s，自动获取 Token…",
                                endpoint, response.status_code,
                                result.get("code"), result.get("msg", ""))
                    if (await self.fetch_token()).get("ok"):
                        recovered = True
                        continue
                raise RuntimeError("订单接口鉴权失败")
            if response.status_code != 200 or result.get("success") is not True:
                raise RuntimeError(f"订单接口失败（HTTP {response.status_code}）：{result.get('msg', '未知错误')}")
            if recovered:
                log.info("订单查询 %s 恢复后重试成功", endpoint)
            return result.get("data")
        raise RuntimeError("Token 恢复后仍无法查询订单")

    @staticmethod
    def _active_order(row):
        return (str(row.get("status")) == "2"
                and str(row.get("isDeleted")) == "0" and not row.get("cancelTime"))

    async def _read_orders(self):
        rows = []
        page = 1
        while True:
            data = await self._order_api("getOrderPageListByParam", params={
                "current": page, "size": 20, "status": 2,
                "date": int(time.time() * 1000)})
            if not isinstance(data, dict) or not isinstance(data.get("records"), list):
                raise RuntimeError("订单列表格式异常")
            for row in data["records"]:
                if self._active_order(row):
                    rows.append({"id": str(row["id"]), "date": row["orderDate"][:10],
                                 "start": row["startTime"][11:16], "end": row["endTime"][11:16],
                                 "gym_id": str(row["gymId"]), "gym": row.get("gymName") or "",
                                 "court": row.get("groundName") or "",
                                 "court_id": str(row.get("groundId") or "")})
            if page >= int(data["pages"]):
                break
            page += 1
        self._orders = sorted(rows, key=lambda o: (o["date"], o["start"]))
        self._orders_error = ""

    def _settle_from_orders(self) -> CurrentOrder | None:
        order = self.current_order
        if order is None or order.phase != "unconfirmed":
            return None
        if any(self._order_matches(o, order.date, order.court, order.start, order.end)
               for o in self._orders):
            status = "created"
        else:
            now = datetime.now()
            if order.first_empty_at is None:
                order.first_empty_at = now
            if (now - order.first_empty_at).total_seconds() < self.ORDER_CONFIRM_GRACE:
                return None
            status = "none"
        order.result = status
        self.current_order = None
        self._apply_order_result(order, status)
        return order

    def _apply_order_result(self, order: CurrentOrder, status: str) -> None:
        created = status == "created"
        log.info("%s 订单核对完成：%s", order.court.name,
                 "已创建" if created else "未创建")
        if order.record is not None:
            order.record.update(
                result="ok" if created else "error",
                msg="订单已确认" if created else "确认未产生订单",
                confirmed_at=format_stamp(),
                elapsed_ms=int((datetime.now() - order.attempt_at).total_seconds() * 1000),
            )
        if order.scope == "auto":
            self.auto.msg = (f"结束：{'✅ 成功' if created else '❌ 未成功'}"
                             f"（目标 {order.court.name}）")
        if order.scope == "leak" and created:
            self._leak_msg = (f"预约成功：{order.date} {order.court.name} "
                              f"{order.start}-{order.end}，检漏已停止")

    async def _refresh_and_status(self, order: CurrentOrder) -> str:
        await self.refresh_orders()
        if self.current_order is order:
            return "unknown"
        return order.result or "unknown"

    async def refresh_orders(self):
        settled = None
        async with self._orders_lock:
            try:
                await self._read_orders()
            except Exception as exc:
                self._orders_error = str(exc)
                log.warning("订单查询失败: %s", self._orders_error)
            else:
                settled = self._settle_from_orders()
            result = self.orders_state()
        await self.bus.publish("orders.updated", result)
        if settled is not None and settled.scope == "leak" and settled.record is not None:
            await self.bus.publish("leak.found", settled.record)
        return result

    async def cancel_order(self, order_id):
        async with self._orders_lock:
            try:
                await self._read_orders()
                order = next((o for o in self._orders if o["id"] == order_id), None)
                if not order:
                    raise RuntimeError("该订单已不存在或不可取消")
                detail = await self._order_api("getOrderDetailByParam", method="POST",
                                               params={"orderId": order_id})
                if (not isinstance(detail, dict) or str(detail.get("id")) != order_id
                        or not self._active_order(detail)):
                    raise RuntimeError("订单状态已变化，请刷新")
                payload = {**detail, "status": 6,
                           "appointmentTime": f"{order['date'].replace('-', '/')} {order['start']}-{order['end']}"}
                # 取消是写操作：不自动重发，避免响应丢失时重复提交。
                await self._order_api("operatOrder", method="POST", payload=payload, recover=False)
            except Exception as exc:
                return {"ok": False, "msg": f"取消未确认：{exc}；请刷新订单核对"}
            try:
                await self._read_orders()
            except Exception as exc:
                self._orders_error = str(exc)
            await self.bus.publish("orders.updated", self.orders_state())
            log.info("取消预约成功")
            return {"ok": True, "msg": "取消预约成功", "orders_state": self.orders_state()}

    # ══════════════════════════════════════════════════
    # 常驻检漏：独立日期、单轮最多一个日期、发现空位后提示预约
    # ══════════════════════════════════════════════════
    @staticmethod
    def _leak_dates(now: datetime | None = None) -> list[str]:
        now = now or datetime.now()
        days = 2 if now.hour >= 20 else 1
        return [d.isoformat() for i in range(days + 1)
                if (d := now.date() + timedelta(days=i)).weekday() in (1, 3)]

    @staticmethod
    def _leak_slot(date: str, avail: dict, now: datetime | None = None):
        now = now or datetime.now()
        for minute in range(18 * 60, 20 * 60 + 1, 30):
            start = f"{minute // 60:02d}:{minute % 60:02d}"
            dt = datetime.fromisoformat(f"{date}T{start}")
            if dt <= now:
                continue
            slots = [(dt + timedelta(minutes=30 * i)).strftime("%H:%M")
                     for i in range(4)]
            if all(str(avail.get(t)) == "1" for t in slots):
                return start, (dt + timedelta(hours=2)).strftime("%H:%M")
        return None

    def _leak_paused(self, now: datetime | None = None) -> bool:
        hm = (now or datetime.now()).strftime("%H:%M")
        return self.tuning.leak_pause_start <= hm < self.tuning.leak_pause_end

    def _leak_query_budget(self, now: datetime) -> float:
        """避免等待限流的检漏请求跨过暂停起点。"""
        if self._leak_paused(now):
            return 0.0
        pause_start = datetime.fromisoformat(
            f"{now.date()}T{self.tuning.leak_pause_start}")
        if now < pause_start:
            return min(4.0, max(0.0, (pause_start - now).total_seconds() - 0.1))
        return 4.0

    def leak_state(self) -> dict:
        return {"running": self.jobs.running("leak"),
                "msg": self._leak_msg, "dates": self._leak_dates(),
                "done_dates": sorted(self._booked_dates()),
                "paused": self._leak_paused(),
                "pause_start": self.tuning.leak_pause_start,
                "pause_end": self.tuning.leak_pause_end}

    async def start_leak(self) -> dict:
        if self.leak_state()["running"]:
            return {"ok": True}
        if not self.config.courts:
            return {"ok": False, "msg": "请先配置场地"}
        self._leak_msg = "等待检查"
        log.info("检漏模式已启动")
        self.jobs.start("leak", self._leak_loop())
        return {"ok": True}

    async def stop_leak(self):
        await self.jobs.stop("leak")
        self._leak_msg = "已停止"
        if (self.leak_found
                and self.leak_found.get("result") == "pending"):
            self.leak_found.update(
                result="cancelled", confirmed_at=format_stamp(),
                msg="检漏停止，下单任务取消")
        if self.leak_found:
            await self.bus.publish("leak.found", self.leak_found)

    async def _leak_success(self, record: dict, court: Court, elapsed_ms: int):
        record.update(result="ok", elapsed_ms=elapsed_ms)
        self.leak_found = record
        self._leak_msg = (f"预约成功：{record['date']} {court.name} "
                          f"{record['start']}-{record['end']}，检漏已停止")
        log.info("检漏预约成功：%s，耗时%dms", court.name, elapsed_ms)
        await self.bus.publish("leak.found", record)

    async def _leak_loop(self):
        queries = 0
        last_summary = time.monotonic()
        paused = False
        try:
            while True:
                if self.leak_found and self.leak_found.get("result") == "ok":
                    return
                now = datetime.now()
                if self._leak_paused(now):
                    self._leak_msg = (f"每日暂停 {self.tuning.leak_pause_start}–"
                                      f"{self.tuning.leak_pause_end}")
                    if not paused:
                        log.info("检漏: %s", self._leak_msg)
                    paused = True
                    await asyncio.sleep(self.tuning.leak_interval)
                    continue
                if paused:
                    log.info("检漏: 每日暂停结束，恢复检查")
                    paused = False
                if time.monotonic() - last_summary >= self.tuning.leak_summary_interval:
                    log.info("检漏运行正常，累计查询 %d 次", queries)
                    last_summary = time.monotonic()

                orders = None
                current = self.current_order
                if current is not None and current.phase == "unconfirmed":
                    self._leak_msg = "正在核对上一笔未确认订单"
                    status = await self._refresh_and_status(current)
                    if status == "unknown":
                        self._leak_msg = "上一笔订单结果未确认，等待订单查询恢复"
                        await asyncio.sleep(30)
                        continue
                    orders = self.orders_state()
                    if current.scope == "leak":
                        if status == "created":
                            return
                        self._leak_msg = "上一笔订单已确认未产生，恢复检查"
                        log.info("检漏复核：确认未产生订单，恢复检查")
                    else:
                        self._leak_msg = "其他模式未确认订单已核对，恢复检查"
                        log.info("检漏复核：其他模式订单核对完成（%s）", status)

                dates = [d for d in self._leak_dates(now)
                         if datetime.fromisoformat(d + "T20:00") > now]
                auto_busy = self.jobs.running("auto") and self.auto.phase != "waiting_token"
                if not dates or auto_busy or self.refresh_running or self.reserve_inflight:
                    self._leak_msg = "等待目标日期或其他任务结束"
                    await asyncio.sleep(self.tuning.leak_interval)
                    continue
                if self._leak_query_budget(datetime.now()) < 4.0:
                    await asyncio.sleep(self.tuning.leak_interval)
                    continue
                if orders is None:
                    self._leak_msg = "正在核对有效订单"
                    orders = await self.refresh_orders()
                if self._leak_paused():
                    continue
                if not orders.get("ok"):
                    self._leak_msg = f"订单查询失败，等待恢复：{orders.get('msg')}"
                    await asyncio.sleep(30)
                    continue
                dates = [d for d in dates if d not in self._booked_dates()]
                if not dates:
                    self._leak_msg = "目标日期已有预约，等待订单变化"
                    await asyncio.sleep(self.tuning.leak_interval)
                    continue
                date = dates[self._leak_cursor % len(dates)]
                self._leak_cursor += 1
                self._leak_msg = f"正在检查 {date}"
                self._leak_scanning = True
                delay = self.tuning.leak_interval
                try:
                    for court in list(self.config.courts)[:10]:
                        if ((self.jobs.running("auto") and self.auto.phase != "waiting_token")
                                or self.refresh_running or self.reserve_inflight):
                            break
                        now = datetime.now()
                        budget = self._leak_query_budget(now)
                        if budget < 4.0:
                            # 临近暂停起点时不再开始新查询。
                            break
                        _, res = await self._refresh_one(
                            court, date, timeout=budget, publish=False, quiet=True)
                        queries += 1
                        if self._leak_paused():
                            break
                        if not res.get("ok"):
                            self._leak_msg = f"查询失败，等待重试：{res.get('error', '未知错误')}"
                            log.warning("检漏 %s %s", date, self._leak_msg)
                            if res.get("auth_fail") or "鉴权失败" in str(res.get("error", "")):
                                self._leak_msg = "鉴权失败，正在自动获取 Token"
                                result = await self.fetch_token()
                                self._leak_msg = ("已取得 Token，下一轮重新鉴权" if result.get("ok")
                                                  else f"Token 恢复失败：{result['msg']}")
                            delay = 30
                            break
                        slot = self._leak_slot(date, res.get("avail", {}))
                        if slot:
                            if self.reserve_inflight:
                                self._leak_msg = (f"发现 {date} {court.name} "
                                                  f"{slot[0]}-{slot[1]}，但已有下单任务，"
                                                  f"本轮不重复提交")
                                log.info("检漏: %s", self._leak_msg)
                                break
                            start, end = slot
                            record = {
                                "found_at": format_stamp(),
                                "court": court.no, "name": court.name,
                                "date": date, "start": start, "end": end,
                                "sent_at": "", "returned_at": "",
                                "confirmed_at": "", "result": "pending",
                                "elapsed_ms": 0,
                            }
                            self._leak_msg = (f"发现 {date} {court.name} {start}-{end}，"
                                              f"正在自动预约")
                            log.info("检漏发现：%s %s %s-%s",
                                     date, court.name, start, end)
                            order = self._begin_order(
                                "leak", court, date, start, end, record)
                            if order is None:
                                record.update(result="cancelled",
                                              msg="已有下单任务，本次不提交")
                                self.leak_found = record
                                await self.bus.publish("leak.found", record)
                                break
                            self.leak_found = record
                            log.info("检漏下单：%s", court.name)
                            kind, msg, outcome = await self._try_order(
                                order, announce=True)
                            self._record_outcome(order, kind, msg, outcome)
                            if kind == "uncertain":
                                status = await self._refresh_and_status(order)
                                if status == "created":
                                    kind, msg = "ok", "订单已确认"
                                elif status == "none":
                                    kind, msg = "error", "确认未产生订单"
                                else:
                                    msg = f"结果不明确且订单核对失败：{msg}"
                                record.update(result=kind, msg=msg)
                            await self._publish_order_result(
                                court, date, kind, msg, outcome)
                            if kind == "ok":
                                await self._leak_success(
                                    record, court, record.get("elapsed_ms", 0))
                                return
                            await self.bus.publish("leak.found", record)
                            if kind == "uncertain":
                                self._leak_msg = ("预约结果不明确，订单核对失败，"
                                                  "等待下一轮复核")
                                delay = 30
                            else:
                                self._leak_msg = f"预约未成功，继续检漏：{msg}"
                            log.warning("检漏预约失败：%s，耗时%dms：%s",
                                        court.name, record.get("elapsed_ms", 0), msg)
                            break
                    else:
                        self._leak_msg = f"{date} 本轮检查结束"
                finally:
                    self._leak_scanning = False
                    if (self.leak_found
                            and self.leak_found.get("result") == "pending"):
                        self.leak_found.update(
                            result="cancelled", confirmed_at=format_stamp(),
                            msg="检漏停止，下单任务取消")
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._leak_msg = f"检漏异常：{exc}"
            log.exception("检漏异常")

    # ══════════════════════════════════════════════════
    # 时间小工具
    # ══════════════════════════════════════════════════
    @staticmethod
    def _parse_hms(t: str) -> datetime:
        parsed = datetime.strptime(t, "%H:%M:%S.%f" if "." in t else "%H:%M:%S")
        return datetime.now().replace(
            hour=parsed.hour, minute=parsed.minute,
            second=parsed.second, microsecond=parsed.microsecond,
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