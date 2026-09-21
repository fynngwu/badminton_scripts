"""core.py — 所有业务逻辑：常量 / 日志 / 验证码 / VID 池 / 场地 / 下单 / 事件总线 / 刷新 / 自动模式"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import httpx
import numpy as np
from PIL import Image

# ============================================================
# 常量
# ============================================================
BASE_URL = "https://reservation.sustech.edu.cn"
URL_GEN   = BASE_URL + "/api/blade-base/captcha/generate/d"
URL_CHK   = BASE_URL + "/api/blade-base/captcha/check/d"
URL_SAVE  = BASE_URL + "/api/blade-app/qywx/saveOrder"
URL_QUERY = BASE_URL + "/api/blade-app/qywx/getOrderTimeConfigList"

USER_ID       = "12632895"
CUSTOMER_ID   = "2097687996508684290"
CUSTOMER_NAME = "吴丰杨"
CUSTOMER_TEL  = "13651486427"
GYM_ID        = "1297443858304540673"
GYM_NAME      = "润杨羽毛球馆"

VID_TIMEOUT   = 3.5     # 验证码 generate / check 的 HTTP 超时
VID_MAX_AGE   = 8.0     # 队列里 vid 的最长有效存活时间（秒）

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
}


@dataclass(frozen=True)
class Court:
    no: int
    id: str
    name: str


COURTS = [
    Court(1,  "1298272433186332673", "1号场"),
    Court(2,  "1298272520994086913", "2号场"),
    Court(3,  "1298272615009411073", "3号场"),
    Court(4,  "1298272709167341570", "4号场"),
    Court(5,  "1298272791098875905", "5号场"),
    Court(6,  "1298273087183183874", "6号场"),
    Court(7,  "1298273175146127362", "7号场"),
    Court(8,  "1298273265650819073", "8号场"),
    Court(9,  "1298273399927267330", "9号场"),
    Court(10, "1298273500317933570", "10号场"),
]
COURT_BY_NO = {c.no: c for c in COURTS}


# ============================================================
# 日志（带内存缓冲，供 /api/logs）
# ============================================================
_log_buf: deque = deque(maxlen=500)
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


log = logging.getLogger("court")
log.setLevel(logging.INFO)
log.addHandler(_BufHandler())
log.addHandler(logging.StreamHandler())


def get_logs(since: int = 0):
    return [e for e in _log_buf if e["seq"] > since]


# ============================================================
# 共享 HTTP 客户端
# ============================================================
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(headers=HEADERS, timeout=6.0)
    return _client


# ============================================================
# 会话状态
# ============================================================
@dataclass
class Session:
    token: str = ""
    target_date: str = ""


session = Session()


# ============================================================
# 验证码 ROTATE 求解
# ============================================================
_DB = np.load(Path(__file__).parent / "fast_db.npz", allow_pickle=False)
_BG_DESCS   = _DB["bg_descs"]
_REF_FFTS   = _DB["ref_ffts"]
_ANGLE_BINS = int(_DB["angle_bins"][0])
_RADIAL_BINS = int(_DB["radial_bins"][0])
_DIR_SIGN   = int(_DB["direction_sign"][0])


class CaptchaSolver:
    W, H, TRAVEL = 242, 180, 242
    BG_W, BG_H   = 48, 36

    def __init__(self, timeout: float = VID_TIMEOUT):
        self.timeout = timeout
        self.timing: dict = {}

    async def solve(self) -> str:
        t0 = time.perf_counter()
        cid, cap = await self._generate()
        t1 = time.perf_counter()

        start, mono = datetime.now(), time.monotonic()
        move_x = await asyncio.to_thread(self._recognize, cap)
        t2 = time.perf_counter()

        track = await self._make_track(move_x, mono)
        t3 = time.perf_counter()

        stop = datetime.now()
        vid = await self._check(cid, track, start, stop)
        t4 = time.perf_counter()

        self.timing = {
            "gen_ms":   round((t1 - t0) * 1000, 1),
            "rec_ms":   round((t2 - t1) * 1000, 1),
            "trk_ms":   round((t3 - t2) * 1000, 1),
            "chk_ms":   round((t4 - t3) * 1000, 1),
            "total_ms": round((t4 - t0) * 1000, 1),
            "move_x":   move_x,
        }
        return vid

    async def _generate(self):
        r = await client().post(URL_GEN, json={"type": "ROTATE"}, timeout=self.timeout)
        r.raise_for_status()
        j = r.json()
        if not isinstance(j, dict) or "id" not in j or "captcha" not in j:
            raise RuntimeError(f"generate 字段异常: {j!r}")
        return j["id"], j["captcha"]

    async def _check(self, cid, track, start, stop):
        payload = {
            "id": cid,
            "data": {
                "bgImageWidth":  self.W,
                "bgImageHeight": self.H,
                "startTime": start.strftime("%Y-%m-%d %H:%M:%S"),
                "stopTime":  stop.strftime("%Y-%m-%d %H:%M:%S"),
                "trackList": track,
            },
        }
        r = await client().post(URL_CHK, json=payload, timeout=self.timeout)
        r.raise_for_status()
        j = r.json()
        if not j.get("success"):
            raise RuntimeError(j.get("msg", "验证码校验失败"))
        vid = j.get("data")
        if not vid:
            raise RuntimeError("验证码通过但 data 为空")
        return vid

    # ---- 图像识别（同步，放在 to_thread 里跑）----
    def _recognize(self, cap) -> int:
        bg  = self._prep_bg(self._decode(cap["backgroundImage"]))
        tpl = self._prep_tpl(self._decode(cap["templateImage"]))
        desc = self._desc(self._gray(bg))
        best = int(np.argmax(_BG_DESCS @ desc))
        sig  = self._sig(self._gray(tpl))
        corr = np.fft.irfft(_REF_FFTS[best] * np.conj(np.fft.rfft(sig)), n=_ANGLE_BINS)
        idx  = int(np.argmax(corr))
        if idx >= _ANGLE_BINS // 2:
            idx -= _ANGLE_BINS
        angle = (_DIR_SIGN * idx * 360.0 / _ANGLE_BINS) % 360.0
        return int(angle / 360.0 * self.TRAVEL + 0.5)

    @staticmethod
    def _decode(uri):
        im = Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))
        im.load()
        return im

    def _prep_bg(self, im):
        im = im.convert("RGB")
        w = round(im.width * self.H / im.height)
        im = im.resize((w, self.H), Image.Resampling.LANCZOS)
        left = max(0, (w - self.W) // 2)
        return im.crop((left, 0, left + self.W, self.H)).convert("RGBA")

    def _prep_tpl(self, im):
        im = im.convert("RGBA")
        w = max(1, round(im.width * self.H / im.height))
        im = im.resize((w, self.H), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (self.H, self.H), (255, 255, 255, 0))
        canvas.alpha_composite(im, ((self.H - im.width) // 2, 0))
        return canvas

    @staticmethod
    def _gray(im):
        arr = np.asarray(im)
        rgb = arr[:, :, :3].astype(np.float32)
        if arr.shape[2] == 4:
            a = arr[:, :, 3:4].astype(np.float32) / 255.0
            rgb = rgb * a + 255.0 * (1 - a)
        return cv2.cvtColor(rgb.astype(np.uint8), cv2.COLOR_RGB2GRAY)

    def _desc(self, gray):
        small = cv2.resize(gray, (self.BG_W, self.BG_H), interpolation=cv2.INTER_AREA)
        x = small.astype(np.float32).ravel()
        x -= x.mean()
        n = np.linalg.norm(x)
        return x / n if n > 1e-8 else x

    @staticmethod
    def _sig(gray):
        h, w = gray.shape
        polar = cv2.warpPolar(
            gray.astype(np.float32), (_RADIAL_BINS, _ANGLE_BINS),
            (w / 2.0, h / 2.0), min(h, w) * 0.48,
            cv2.WARP_POLAR_LINEAR | cv2.INTER_LINEAR,
        )
        r0, r1 = int(_RADIAL_BINS * 0.30), int(_RADIAL_BINS * 0.92)
        grad = cv2.Sobel(polar[:, r0:r1], cv2.CV_32F, 1, 0, ksize=3)
        sig = np.mean(np.abs(grad), axis=1)
        sig -= sig.mean()
        std = sig.std()
        return (sig / std).astype(np.float32) if std > 1e-6 else sig.astype(np.float32)

    async def _make_track(self, move_x, mono):
        # 放到线程里跑同步版本：Windows 上 asyncio.sleep(0.001) 实际要 15ms，
        # 只有 time.sleep 能保持与原版一致的 ~1ms 精度
        return await asyncio.to_thread(self._make_track_sync, move_x, mono)

    @staticmethod
    def _make_track_sync(move_x, mono):
        def t(): return int((time.monotonic() - mono) * 1000)
        out = [{"x": 0, "y": random.randint(-2, 2), "type": "down", "t": t()}]
        x = 0
        while x < move_x:
            x = min(x + (2 if random.random() < 0.8 else 1), move_x)
            time.sleep(0.001)
            out.append({"x": x, "y": random.randint(-2, 2), "type": "move", "t": t()})
        out.append({"x": move_x, "y": random.randint(-2, 2), "type": "up", "t": t()})
        return out


# ============================================================
# VID 池（队列元素带时间戳，取用时过滤过期 vid）
# ============================================================
class VidPool:
    def __init__(self, size: int = 3, period: float = 8., qmax: int = 3,
                 max_age: float = VID_MAX_AGE):
        self.size = size
        self.period = period
        self.max_age = max_age
        self.queue: asyncio.Queue[tuple[float, str]] = asyncio.Queue(maxsize=qmax)
        self._tasks: list[asyncio.Task] = []
        self.stats: deque = deque(maxlen=30)

    @property
    def running(self) -> bool:
        return any(not t.done() for t in self._tasks)

    def count(self) -> int:
        return self.queue.qsize()

    async def start(self):
        if self.running:
            return False
        while not self.queue.empty():
            self.queue.get_nowait()
        stagger = self.period / self.size
        self._tasks = [
            asyncio.create_task(self._worker(i + 1, i * stagger))
            for i in range(self.size)
        ]
        log.info("VID 池启动: %d workers / 周期 %.1fs / 有效期 %.1fs",
                 self.size, self.period, self.max_age)
        return True

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        log.info("VID 池已停止")

    async def take(self, timeout: float = 5.0,
                   max_age: float | None = None) -> str | None:
        """取一个未过期的 vid。过期的会被丢弃并继续尝试，直到总 timeout 耗尽。"""
        limit = self.max_age if max_age is None else max_age
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                return None
            try:
                ts, vid = await asyncio.wait_for(self.queue.get(), timeout=remain)
            except asyncio.TimeoutError:
                return None
            age = time.monotonic() - ts
            if age <= limit:
                return vid
            log.warning("VID 过期丢弃 (存活 %.1fs > %.1fs)", age, limit)

    async def _worker(self, tid: int, delay: float):
        if delay > 0:
            await asyncio.sleep(delay)
        while True:
            try:
                solver = CaptchaSolver(timeout=VID_TIMEOUT)
                vid = await solver.solve()
                self.stats.append({"ok": True, "ts": datetime.now().strftime("%H:%M:%S"),
                                   **solver.timing})
                log.info("VID-OK [T%d] %.0fms (gen %.0f/rec %.0f/trk %.0f/chk %.0f)",
                         tid, solver.timing["total_ms"], solver.timing["gen_ms"],
                         solver.timing["rec_ms"], solver.timing["trk_ms"],
                         solver.timing["chk_ms"])
                item = (time.monotonic(), vid)
                try:
                    self.queue.put_nowait(item)
                except asyncio.QueueFull:
                    try: self.queue.get_nowait()
                    except asyncio.QueueEmpty: pass
                    try: self.queue.put_nowait(item)
                    except asyncio.QueueFull: pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.stats.append({"ok": False, "error": str(e),
                                   "ts": datetime.now().strftime("%H:%M:%S")})
                log.warning("VID-ERR [T%d] %s", tid, e)
            await asyncio.sleep(self.period)


vid_pool = VidPool()


# ============================================================
# 场地查询 / 下单
# ============================================================
async def query_court(court: Court, date: str, token: str,
                      timeout: float = 4.0) -> dict[str, str] | None:
    try:
        r = await client().get(URL_QUERY, params={
            "groundId": court.id, "startDate": date, "endDate": date,
            "userid": USER_ID, "token": token,
        }, timeout=timeout)
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        log.warning("查询 %s 异常: %s", court.name, e)
        return None

    if not j.get("success"):
        log.warning("查询 %s success=false: %s", court.name, j.get("msg"))
        return None

    out: dict[str, str] = {}
    for cfg in j.get("data", {}).get("configList", []):
        if cfg.get("date") != date:
            continue
        for blk in cfg.get("timeBlockList", []):
            t = blk.get("time", "")
            if ":" in t:
                h, m = t.split(":", 1)
                if h.isdigit():
                    t = f"{h.zfill(2)}:{m}"
            out[t] = blk.get("status")
    return out


def slot_covers(avail: dict, start: str, end: str) -> bool:
    """检查 [start, end) 是否所有 30min 单元都空闲。"""
    h1, m1 = map(int, start.split(":"))
    h2, m2 = map(int, end.split(":"))
    t, e = h1 * 60 + m1, h2 * 60 + m2
    while t < e:
        if avail.get(f"{t // 60:02d}:{t % 60:02d}") != "1":
            return False
        t += 30
    return True


async def submit_order(vid: str, court: Court, date: str,
                       start: str, end: str, token: str) -> dict:
    st = f"{date} {start}:00"
    et = f"{date} {end}:00"
    now = datetime.now()
    order_dt = datetime.strptime(st, "%Y-%m-%d %H:%M:%S").replace(
        hour=now.hour, minute=now.minute, second=0, microsecond=0)
    order_time = order_dt.strftime("%Y-%m-%d %H:%M:%S")

    payload = {
        "customerEmail": "", "customerId": CUSTOMER_ID,
        "customerName": CUSTOMER_NAME, "customerTel": CUSTOMER_TEL,
        "endTime": et, "groundId": court.id, "groundName": court.name,
        "groundType": "0", "gymId": GYM_ID, "gymName": GYM_NAME,
        "id": vid, "isIllegal": "0", "messagePushType": "0",
        "orderDate": order_time, "startTime": st,
        "tmpEndTime": et, "tmpOrderDate": order_time, "tmpStartTime": st,
        "userNum": "1",
    }
    try:
        r = await client().post(URL_SAVE, params={"userid": USER_ID, "token": token},
                                json=payload, timeout=6.0)
        log.info("下单 %s: HTTP %d | %s", court.name, r.status_code, r.text[:180])
        try:
            return r.json()
        except Exception:
            return {"success": False, "msg": "non-json"}
    except Exception as e:
        log.warning("下单 %s 异常: %s", court.name, e)
        return {"success": False, "msg": str(e)}


# ============================================================
# 事件总线（带历史回放）
# ============================================================
class Bus:
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


bus = Bus()


class RefreshService:
    """探测-扇出刷新：
       1) 先挑一个场地做低频试探（失败退避 PROBE_BACKOFF 秒，最多 PROBE_MAX_TRIES 次）
       2) 任意一次成功 → 立刻并发查询其余全部场地
       3) 全部试探失败 → 放弃本次刷新，发 refresh.done(probe_failed=True)
    """

    PROBE_BACKOFF: float = 1.0
    PROBE_MAX_TRIES: int = 15

    def __init__(self):
        self._probe_task: asyncio.Task | None = None
        self._tasks: list[asyncio.Task] = []

    @property
    def running(self) -> bool:
        if self._probe_task and not self._probe_task.done():
            return True
        return any(not t.done() for t in self._tasks)

    async def start(self, date: str, token: str,
                    courts: list[Court] | None = None) -> int:
        await self.stop()
        pool = list(courts or COURTS)
        random.shuffle(pool)
        log.info("启动刷新: 探测 %s → 成功后并发剩余 %d 个场地 "
                 "(退避 %.1fs / 最多 %d 次)",
                 pool[0].name, len(pool) - 1,
                 self.PROBE_BACKOFF, self.PROBE_MAX_TRIES)
        self._probe_task = asyncio.create_task(self._run(date, token, pool))
        return len(pool)

    async def stop(self):
        tasks = [t for t in ([self._probe_task] + self._tasks) if t is not None]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._probe_task = None
        self._tasks = []

    # ---------- 内部 ----------

    async def _run(self, date, token, pool):
        await self._probe_then_fanout(date, token, pool)
        await bus.publish("refresh.done", {
            "ts": datetime.now().strftime("%H:%M:%S"),
        })

    async def _probe_then_fanout(self, date, token, pool):
        probe = pool[0]
        for attempt in range(1, self.PROBE_MAX_TRIES + 1):
            t0 = time.monotonic()
            avail = await query_court(probe, date, token)
            dt = (time.monotonic() - t0) * 1000

            if avail is not None:
                log.info("探测 %s 成功 (%.0fms, 第 %d 次)，开始并发其余 %d 个场地",
                         probe.name, dt, attempt, len(pool) - 1)
                await bus.publish("refresh.result", {
                    "court": probe.no, "name": probe.name, "ok": True,
                    "avail": avail,
                    "ts": datetime.now().strftime("%H:%M:%S"),
                })
                break

            log.info("探测 %s 失败 (%.0fms, %d/%d)",
                     probe.name, dt, attempt, self.PROBE_MAX_TRIES)

            if attempt == self.PROBE_MAX_TRIES:
                log.warning("探测 %s %d 次全部失败，放弃本次刷新",
                            probe.name, self.PROBE_MAX_TRIES)
                await bus.publish("refresh.result", {
                    "court": probe.no, "name": probe.name, "ok": False,
                    "error": f"探测 {self.PROBE_MAX_TRIES} 次全部失败",
                    "ts": datetime.now().strftime("%H:%M:%S"),
                })
                await bus.publish("refresh.done", {
                    "ts": datetime.now().strftime("%H:%M:%S"),
                    "probe_failed": True,
                })
                return

            await asyncio.sleep(self.PROBE_BACKOFF)

        # 探测成功 → 并发其余
        self._tasks = [asyncio.create_task(self._one(c, date, token))
                       for c in pool[1:]]
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _one(self, court: Court, date: str, token: str):
        t0 = time.monotonic()
        avail = await query_court(court, date, token)
        dt = (time.monotonic() - t0) * 1000
        if avail is not None:
            log.info("%s OK (%.0fms)", court.name, dt)
            await bus.publish("refresh.result", {
                "court": court.no, "name": court.name, "ok": True,
                "avail": avail,
                "ts": datetime.now().strftime("%H:%M:%S"),
            })
        else:
            log.info("%s 失败 (%.0fms)", court.name, dt)
            await bus.publish("refresh.result", {
                "court": court.no, "name": court.name, "ok": False,
                "error": "查询失败",
                "ts": datetime.now().strftime("%H:%M:%S"),
            })

refresh_service = RefreshService()


# ============================================================
# 自动模式
# ============================================================
@dataclass
class AutoState:
    running: bool = False
    phase: str = "idle"
    msg: str = ""
    candidates: list = field(default_factory=list)
    applied: list = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    plan: dict = field(default_factory=dict)


auto_state = AutoState()
_auto_task: asyncio.Task | None = None


def _parse_hms(t: str) -> datetime:
    parts = [int(x) for x in t.split(":")]
    while len(parts) < 3:
        parts.append(0)
    return datetime.now().replace(hour=parts[0], minute=parts[1],
                                  second=parts[2], microsecond=0)


async def _sleep_until(target: datetime):
    while True:
        remain = (target - datetime.now()).total_seconds()
        if remain <= 0:
            return
        await asyncio.sleep(min(remain, 0.5))


async def start_auto(*, date: str, token: str, slot_start: str, slot_end: str,
                     refresh_at: str, submit_at: str, max_orders: int, idle_exit: float):
    global _auto_task
    if auto_state.running:
        return {"ok": False, "msg": "自动模式已在运行"}

    auto_state.running = True
    auto_state.phase = "init"
    auto_state.msg = "初始化"
    auto_state.candidates = []
    auto_state.applied = []
    auto_state.started_at = datetime.now().strftime("%H:%M:%S")
    auto_state.finished_at = ""
    auto_state.plan = {
        "date": date, "slot_start": slot_start, "slot_end": slot_end,
        "refresh_at": refresh_at, "submit_at": submit_at,
        "max_orders": max_orders, "idle_exit": idle_exit,
    }
    _auto_task = asyncio.create_task(_run_auto(
        date, token, slot_start, slot_end,
        refresh_at, submit_at, max_orders, idle_exit,
    ))
    return {"ok": True}


async def stop_auto():
    global _auto_task
    if _auto_task and not _auto_task.done():
        _auto_task.cancel()
        try:
            await _auto_task
        except asyncio.CancelledError:
            pass
    auto_state.running = False
    auto_state.phase = "stopped"
    auto_state.msg = "已手动停止"
    auto_state.finished_at = datetime.now().strftime("%H:%M:%S")


async def _run_auto(date, token, slot_start, slot_end,
                    refresh_at, submit_at, max_orders, idle_exit):
    candidates: list[Court] = []
    seen: set[int] = set()
    applied: list[dict] = []
    q: asyncio.Queue | None = None

    def has_success() -> bool:
        return any(a["ok"] for a in applied)

    def harvest(evt) -> bool:
        if evt.get("_topic") != "refresh.result":
            return False
        d = evt["data"]
        if not d.get("ok") or not d.get("avail"):
            return False
        no = d["court"]
        if no in seen:
            return False
        if not slot_covers(d["avail"], slot_start, slot_end):
            return False
        seen.add(no)
        candidates.append(COURT_BY_NO[no])
        auto_state.candidates = [{"court": c.no, "name": c.name} for c in candidates]
        log.info("发现候选 %s (#%d)", COURT_BY_NO[no].name, len(candidates))
        return True

    async def apply_pending():
        """依次申请；一旦成功立即返回；失败则继续，直到尝试 max_orders 个为止。"""
        for c in list(candidates):
            if has_success():
                return                        # ← 已成功，不再申请
            if len(applied) >= max_orders:    # ← 达到尝试上限
                return
            if any(a["court"] == c.no for a in applied):
                continue

            # take 内部会丢弃过期 vid，最多等 2s
            vid = await vid_pool.take(timeout=2.0)
            if not vid:
                log.info("自动模式: 队列无有效 vid，同步兜底求解")
                try:
                    vid = await CaptchaSolver(timeout=VID_TIMEOUT).solve()
                except Exception as e:
                    log.warning("兜底求解失败: %s", e)
                    continue

            r = await submit_order(vid, c, date, slot_start, slot_end, token)
            ok = bool(r.get("success"))
            applied.append({"court": c.no, "name": c.name, "ok": ok,
                            "msg": r.get("msg", "")})
            auto_state.applied = list(applied)
            log.info("申请 %s → %s", c.name, "OK" if ok else f"FAIL ({r.get('msg','')})")

            if ok:
                log.info("自动模式: ✅ 申请成功，停止后续申请")
                return                        # ← 成功就立刻退出

    try:
        refresh_dt = _parse_hms(refresh_at)
        submit_dt  = _parse_hms(submit_at)

        # ── 自动启动 VID（刷新前 8 秒；已在运行则跳过）──────────
        if not vid_pool.running:
            vid_start_dt = refresh_dt - timedelta(seconds=8)
            if datetime.now() < vid_start_dt:
                auto_state.msg = f"等待启动 VID（{vid_start_dt.strftime('%H:%M:%S')}）"
                log.info("自动模式: 等待 %s 启动 VID", vid_start_dt.strftime('%H:%M:%S'))
                await _sleep_until(vid_start_dt)
            log.info("自动模式: 启动 VID 池")
            await vid_pool.start()
        else:
            log.info("自动模式: VID 池已在运行，跳过启动")
        # ────────────────────────────────────────────────────

        # 1) 等触发
        auto_state.phase = "waiting"
        auto_state.msg = f"等待 {refresh_at}"
        log.info("自动模式: 等待触发 %s", refresh_at)
        await _sleep_until(refresh_dt)

        # 2) 启动刷新 + 订阅
        auto_state.phase = "refreshing"
        auto_state.msg = "刷新中…"
        log.info("自动模式: 触发刷新")
        since = bus.seq
        await refresh_service.start(date, token)
        q, hist = bus.subscribe(since)

        # 3) 收集到 submit_at
        auto_state.phase = "collecting"
        auto_state.msg = f"收集空场直到 {submit_at}"
        for e in hist:
            harvest(e)
        while datetime.now() < submit_dt:
            try:
                harvest(await asyncio.wait_for(q.get(), timeout=0.1))
            except asyncio.TimeoutError:
                pass
        while not q.empty():
            harvest(q.get_nowait())

        # 4) 批量申请（成功即停）
        auto_state.phase = "applying"
        auto_state.msg = f"申请中（候选 {len(candidates)}，上限 {max_orders}）"
        log.info("自动模式: 到达申请时间，候选 %d，最多尝试 %d",
                 len(candidates), max_orders)
        await apply_pending()

        # 5) 未成功且未达上限 → 继续监听新空场
        if not has_success() and len(applied) < max_orders:
            last_new = time.monotonic()
            while not has_success() and len(applied) < max_orders:
                remain = idle_exit - (time.monotonic() - last_new)
                auto_state.msg = (f"已尝试 {len(applied)}/{max_orders}，"
                                  f"空闲 {remain:.1f}s 后退出")
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=0.5)
                    if harvest(evt):
                        last_new = time.monotonic()
                        await apply_pending()      # 内部成功即停
                except asyncio.TimeoutError:
                    if time.monotonic() - last_new >= idle_exit:
                        log.info("自动模式: %.0fs 内无新增 → 退出", idle_exit)
                        break

        log.info("自动模式: 结束 | 候选 %d | 已尝试 %d | 成功 %s",
                 len(candidates), len(applied),
                 "是" if has_success() else "否")

    except asyncio.CancelledError:
        log.info("自动模式: 被取消")
        raise
    except Exception as e:
        log.exception("自动模式异常: %s", e)
    finally:
        if q is not None:
            bus.unsubscribe(q)
        await refresh_service.stop()

        # ── 申请成功 → 停止 VID 池 ───────────────────────────
        if has_success() and vid_pool.running:
            log.info("自动模式: 申请成功，停止 VID 池")
            await vid_pool.stop()
        # ────────────────────────────────────────────────────

        auto_state.running = False
        auto_state.phase = "done"
        auto_state.msg = (f"结束：{'✅ 成功' if has_success() else '❌ 未成功'}"
                          f"（尝试 {len(applied)} 个）")
        auto_state.finished_at = datetime.now().strftime("%H:%M:%S")