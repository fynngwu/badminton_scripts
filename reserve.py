"""reserve.py — 验证码解题 + vid 常驻 worker + 场地查询 + saveOrder + 后端日志"""

import base64
import collections
import io
import queue
import random
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
import numpy as np
from PIL import Image
import cv2


# ============================================================
# 1. 常量
# ============================================================

BASE_URL = "https://reservation.sustech.edu.cn"

GENERATE_URL   = BASE_URL + "/api/blade-base/captcha/generate/d"
CHECK_URL      = BASE_URL + "/api/blade-base/captcha/check/d"
SAVE_ORDER_URL = BASE_URL + "/api/blade-app/qywx/saveOrder"
GET_ORDER_URL  = BASE_URL + "/api/blade-app/qywx/getOrderTimeConfigList"

# ---------- 固定账号信息（后端常量）----------
USER_ID       = "12632895"
CUSTOMER_ID   = "2097687996508684290"
CUSTOMER_NAME = "吴丰杨"
CUSTOMER_TEL  = "13651486427"

GYM_ID   = "1297443858304540673"
GYM_NAME = "润杨羽毛球馆"

CANDIDATE_GROUNDS = [
    ("1298272433186332673", "1号场"),
    ("1298272520994086913", "2号场"),
    ("1298272615009411073", "3号场"),
    ("1298272709167341570", "4号场"),
    ("1298272791098875905", "5号场"),
    ("1298273087183183874", "6号场"),
    ("1298273175146127362", "7号场"),
    ("1298273265650819073", "8号场"),
    ("1298273399927267330", "9号场"),
    ("1298273500317933570", "10号场"),
]

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
}


# ============================================================
# 2. 后端日志缓冲区
# ============================================================

LOG_BUFFER = collections.deque(maxlen=500)
_LOG_SEQ   = 0
_LOG_LOCK  = threading.Lock()


def _log(level: str, msg: str):
    global _LOG_SEQ
    with _LOG_LOCK:
        _LOG_SEQ += 1
        seq = _LOG_SEQ
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        LOG_BUFFER.append({"seq": seq, "ts": ts, "level": level, "msg": msg})
    print(f"[{level}] {msg}")


def get_logs(since: int = 0):
    with _LOG_LOCK:
        return [l for l in LOG_BUFFER if l["seq"] > since]


VID_STATS   = collections.deque(maxlen=30)
_STATS_LOCK = threading.Lock()


def _record_vid_stat(stat: dict):
    with _STATS_LOCK:
        VID_STATS.append(stat)


def get_vid_stats():
    with _STATS_LOCK:
        return list(VID_STATS)


# ============================================================
# 3. 线程本地 Session
# ============================================================

_tls = threading.local()


def _session() -> requests.Session:
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _tls.s = s
    return s


# ============================================================
# 4. ROTATE 验证码自动求解
# ============================================================

_DB_PATH = Path(__file__).resolve().parent / "fast_db.npz"
_DB = np.load(_DB_PATH, allow_pickle=False)

_BG_DESCS       = _DB["bg_descs"]
_REF_FFTS       = _DB["ref_ffts"]
_ANGLE_BINS     = int(_DB["angle_bins"][0])
_RADIAL_BINS    = int(_DB["radial_bins"][0])
_DIRECTION_SIGN = int(_DB["direction_sign"][0])


class CaptchaSolver:
    WIDTH  = 242
    HEIGHT = 180
    SLIDER_TRAVEL = 242
    BG_W = 48
    BG_H = 36
    STEP_SLEEP = 0.001

    def __init__(self, session, timeout: float = 4.0):
        self.session = session
        self.timeout = timeout
        self.last_timing = {}

    def solve(self):
        t0 = time.perf_counter()
        captcha_id, captcha = self._generate()
        t1 = time.perf_counter()

        start_time = datetime.now()
        start_monotonic = time.monotonic()

        move_x = self._get_px(captcha)
        t2 = time.perf_counter()

        track_list = self._generate_track(move_x, start_monotonic)
        t3 = time.perf_counter()

        stop_time = datetime.now()

        vid = self._check(captcha_id, track_list, start_time, stop_time)
        t4 = time.perf_counter()

        self.last_timing = {
            "captcha_id":   captcha_id,
            "move_x":       move_x,
            "generate_ms":  (t1 - t0) * 1000,
            "recognize_ms": (t2 - t1) * 1000,
            "track_ms":     (t3 - t2) * 1000,
            "check_ms":     (t4 - t3) * 1000,
            "total_ms":     (t4 - t0) * 1000,
        }
        return vid

    def _generate(self):
        r = self.session.post(
            GENERATE_URL,
            json={"type": "ROTATE"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        result = r.json()

        if not isinstance(result, dict) or "id" not in result or "captcha" not in result:
            raise RuntimeError(f"generate字段异常: {result!r}")

        captcha = result["captcha"]
        if not isinstance(captcha, dict) or "backgroundImage" not in captcha or "templateImage" not in captcha:
            raise RuntimeError(f"captcha字段异常: {captcha!r}")

        return result["id"], captcha

    def _check(self, captcha_id, track_list, start_time, stop_time):
        payload = {
            "id": captcha_id,
            "data": {
                "bgImageWidth":  self.WIDTH,
                "bgImageHeight": self.HEIGHT,
                "startTime": start_time.strftime("%Y-%m-%d %H:%M:%S"),
                "stopTime":  stop_time.strftime("%Y-%m-%d %H:%M:%S"),
                "trackList": track_list,
            },
        }
        r = self.session.post(CHECK_URL, json=payload, timeout=self.timeout)
        r.raise_for_status()
        result = r.json()
        if not result.get("success"):
            raise RuntimeError(result.get("msg", "CAPTCHA validation failed"))
        vid = result.get("data")
        if not vid:
            raise RuntimeError("CAPTCHA passed but response.data is empty")
        return vid

    @staticmethod
    def _decode_data_uri(uri):
        b64 = uri.split(",", 1)[1]
        im = Image.open(io.BytesIO(base64.b64decode(b64)))
        im.load()
        return im

    def _prepare_background(self, bg):
        bg = bg.convert("RGB")
        scale = self.HEIGHT / bg.height
        new_w = round(bg.width * scale)
        bg = bg.resize((new_w, self.HEIGHT), Image.Resampling.LANCZOS)
        left = max(0, (new_w - self.WIDTH) // 2)
        return bg.crop((left, 0, left + self.WIDTH, self.HEIGHT)).convert("RGBA")

    def _prepare_template(self, tpl):
        tpl = tpl.convert("RGBA")
        scale = self.HEIGHT / tpl.height
        new_w = max(1, round(tpl.width * scale))
        tpl = tpl.resize((new_w, self.HEIGHT), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (self.HEIGHT, self.HEIGHT), (255, 255, 255, 0))
        canvas.alpha_composite(tpl, ((self.HEIGHT - tpl.width) // 2, 0))
        return canvas

    @staticmethod
    def _to_gray(im):
        arr = np.asarray(im)
        rgb = arr[:, :, :3].astype(np.float32)
        if arr.shape[2] == 4:
            alpha = arr[:, :, 3:4].astype(np.float32) / 255.0
            rgb = rgb * alpha + 255.0 * (1.0 - alpha)
        return cv2.cvtColor(rgb.astype(np.uint8), cv2.COLOR_RGB2GRAY)

    def _get_bg_desc(self, gray):
        small = cv2.resize(gray, (self.BG_W, self.BG_H), interpolation=cv2.INTER_AREA)
        x = small.astype(np.float32).ravel()
        x -= x.mean()
        n = np.linalg.norm(x)
        if n > 1e-8:
            x /= n
        return x

    @staticmethod
    def _get_angle_signature(gray):
        h, w = gray.shape
        polar = cv2.warpPolar(
            gray.astype(np.float32),
            (_RADIAL_BINS, _ANGLE_BINS),
            (w / 2.0, h / 2.0),
            min(h, w) * 0.48,
            cv2.WARP_POLAR_LINEAR | cv2.INTER_LINEAR,
        )
        r0 = int(_RADIAL_BINS * 0.30)
        r1 = int(_RADIAL_BINS * 0.92)
        grad = cv2.Sobel(polar[:, r0:r1], cv2.CV_32F, 1, 0, ksize=3)
        sig = np.mean(np.abs(grad), axis=1)
        sig -= sig.mean()
        std = sig.std()
        if std > 1e-6:
            sig /= std
        return sig.astype(np.float32)

    def _get_px(self, captcha) -> int:
        bg  = self._prepare_background(self._decode_data_uri(captcha["backgroundImage"]))
        tpl = self._prepare_template(self._decode_data_uri(captcha["templateImage"]))

        bg_gray = self._to_gray(bg)
        desc = self._get_bg_desc(bg_gray)
        best_i = int(np.argmax(_BG_DESCS @ desc))

        tpl_gray = self._to_gray(tpl)
        sig = self._get_angle_signature(tpl_gray)
        test_fft = np.fft.rfft(sig)

        corr = np.fft.irfft(_REF_FFTS[best_i] * np.conj(test_fft), n=_ANGLE_BINS)
        idx = int(np.argmax(corr))
        if idx >= _ANGLE_BINS // 2:
            idx -= _ANGLE_BINS

        angle = (_DIRECTION_SIGN * idx * 360.0 / _ANGLE_BINS) % 360.0
        move_x = angle / 360.0 * self.SLIDER_TRAVEL
        return int(move_x + 0.5)

    @staticmethod
    def _elapsed_ms(start_monotonic):
        return int((time.monotonic() - start_monotonic) * 1000)

    def _generate_track(self, move_x, start_monotonic):
        track = [{
            "x": 0,
            "y": random.randint(-2, 2),
            "type": "down",
            "t": self._elapsed_ms(start_monotonic),
        }]
        x = 0
        while x < move_x:
            step = 2 if random.random() < 0.8 else 1
            x = min(x + step, move_x)
            time.sleep(self.STEP_SLEEP)
            track.append({
                "x": x,
                "y": random.randint(-2, 2),
                "type": "move",
                "t": self._elapsed_ms(start_monotonic),
            })
        track.append({
            "x": move_x,
            "y": random.randint(-2, 2),
            "type": "up",
            "t": self._elapsed_ms(start_monotonic),
        })
        return track


# ============================================================
# 5. VID 常驻 worker（3 个长期线程，各自独立周期）
# ============================================================

VID_THREAD_COUNT = 3
VID_PERIOD_SEC   = 8.2    # 每个 worker 从一次 solve 发起到下一次发起的周期
VID_REQ_TIMEOUT  = 4.0    # 单个 HTTP 请求硬超时
VID_QUEUE_MAX    = 3

VID_QUEUE = queue.Queue(maxsize=VID_QUEUE_MAX)

_VID_STOP_EVENT = None
_VID_THREADS    = []
_VID_LOCK       = threading.Lock()


def _clear_vid_queue() -> int:
    n = 0
    while True:
        try:
            VID_QUEUE.get_nowait()
            n += 1
        except queue.Empty:
            break
    return n


def _sleep_stop(seconds: float, stop_event: threading.Event, step: float = 0.05):
    deadline = time.monotonic() + seconds
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            return
        if stop_event.is_set():
            return
        time.sleep(min(step, remain))


def _try_solve_once(tid: int):
    t0 = time.perf_counter()
    try:
        s = requests.Session()
        s.headers.update(HEADERS)
        solver = CaptchaSolver(s, timeout=VID_REQ_TIMEOUT)
        vid = solver.solve()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        tm = solver.last_timing

        _record_vid_stat({
            "ts":           datetime.now().strftime("%H:%M:%S"),
            "ok":           True,
            "total_ms":     round(elapsed_ms, 1),
            "generate_ms":  round(tm.get("generate_ms", 0), 1),
            "recognize_ms": round(tm.get("recognize_ms", 0), 1),
            "track_ms":     round(tm.get("track_ms", 0), 1),
            "check_ms":     round(tm.get("check_ms", 0), 1),
            "move_x":       tm.get("move_x"),
        })

        _log("VID-OK",
             f"[T{tid}] solve 成功 {elapsed_ms:.0f}ms | "
             f"gen={tm.get('generate_ms', 0):.0f} "
             f"rec={tm.get('recognize_ms', 0):.0f} "
             f"trk={tm.get('track_ms', 0):.0f} "
             f"chk={tm.get('check_ms', 0):.0f} | "
             f"move_x={tm.get('move_x')}")
        return vid

    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        _record_vid_stat({
            "ts":       datetime.now().strftime("%H:%M:%S"),
            "ok":       False,
            "total_ms": round(elapsed_ms, 1),
            "error":    str(e),
        })
        _log("VID-ERR", f"[T{tid}] solve 失败 {elapsed_ms:.0f}ms: {e}")
        return None


def _vid_worker(tid: int, initial_delay: float, stop_event: threading.Event):
    _log("VID", f"[T{tid}] worker 启动（初始延迟 {initial_delay:.2f}s）")
    if initial_delay > 0:
        _sleep_stop(initial_delay, stop_event)

    while not stop_event.is_set():
        t_start = time.monotonic()

        vid = _try_solve_once(tid)

        if stop_event.is_set():
            break

        if vid is not None:
            try:
                VID_QUEUE.put_nowait(vid)
                _log("VID", f"[T{tid}] 入队 depth={VID_QUEUE.qsize()}")
            except queue.Full:
                try:
                    VID_QUEUE.get_nowait()
                except queue.Empty:
                    pass
                try:
                    VID_QUEUE.put_nowait(vid)
                    _log("VID", f"[T{tid}] 队列满→丢旧→入队 depth={VID_QUEUE.qsize()}")
                except queue.Full:
                    _log("VID", f"[T{tid}] 队列仍满，丢弃")

        elapsed = time.monotonic() - t_start
        wait = VID_PERIOD_SEC - elapsed
        if wait > 0:
            _sleep_stop(wait, stop_event)

    _log("VID", f"[T{tid}] worker 退出")


def start_vid_workers() -> bool:
    global _VID_STOP_EVENT, _VID_THREADS
    with _VID_LOCK:
        if any(t.is_alive() for t in _VID_THREADS):
            return False

        stop_event = threading.Event()
        _VID_STOP_EVENT = stop_event
        _clear_vid_queue()
        _VID_THREADS = []

        stagger = VID_PERIOD_SEC / VID_THREAD_COUNT
        for i in range(VID_THREAD_COUNT):
            t = threading.Thread(
                target=_vid_worker,
                args=(i + 1, i * stagger, stop_event),
                daemon=True,
                name=f"vid-{i + 1}",
            )
            t.start()
            _VID_THREADS.append(t)

        _log("VID",
             f"{VID_THREAD_COUNT} 个常驻 worker 已启动 | "
             f"周期 {VID_PERIOD_SEC}s | 单请求超时 {VID_REQ_TIMEOUT}s | "
             f"队列 {VID_QUEUE_MAX} | 启动错开 {stagger:.2f}s")
        return True


def stop_vid_workers():
    ev = _VID_STOP_EVENT
    if ev is not None:
        ev.set()

    for t in _VID_THREADS:
        t.join(timeout=VID_REQ_TIMEOUT + 2.0)

    cleared = _clear_vid_queue()
    if cleared:
        _log("VID", f"停止时清空 {cleared} 个残留 vid")
    _log("VID", "所有 VID worker 已停止")


def get_vid_count() -> int:
    return VID_QUEUE.qsize()


def take_vid(timeout: float = 5.0):
    try:
        return VID_QUEUE.get(timeout=timeout)
    except queue.Empty:
        return None


# ============================================================
# 6. 场地可用性查询
# ============================================================

def _normalize_time_key(t: str) -> str:
    """统一成带前导零的 HH:MM：'8:00' -> '08:00'。"""
    if not isinstance(t, str) or ":" not in t:
        return t
    h, m = t.split(":", 1)
    if h.isdigit():
        return h.zfill(2) + ":" + m
    return t


def get_court_availability(ground_id: str, date_str: str, token: str):
    """
    返回 { "08:00": "1", "08:30": "2", ... }
    status == "1" → 空闲；其他 → 占用/不可约。
    出错返回 None。
    """
    try:
        r = _session().get(
            GET_ORDER_URL,
            params={
                "groundId":  ground_id,
                "startDate": date_str,
                "endDate":   date_str,
                "userid":    USER_ID,
                "token":     token,
            },
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        _log("ORDER-ERR", f"{ground_id} 查询异常: {e}")
        return None

    if not data.get("success"):
        _log("ORDER-ERR", f"{ground_id} 返回 success=false: {data.get('msg')}")
        return None

    result = {}
    for cfg in data.get("data", {}).get("configList", []):
        if cfg.get("date") != date_str:
            continue
        for blk in cfg.get("timeBlockList", []):
            t = _normalize_time_key(blk.get("time", ""))
            result[t] = blk.get("status")
    return result


# ============================================================
# 7. saveOrder
# ============================================================

def _build_order_date(start_time: str) -> str:
    reserve_dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
    now = datetime.now()
    order_dt = reserve_dt.replace(
        hour=now.hour, minute=now.minute, second=0, microsecond=0,
    )
    return order_dt.strftime("%Y-%m-%d %H:%M:%S")


def post_order_once(vid, ground_id, ground_name,
                    start_time, end_time, token):
    order_time = _build_order_date(start_time)
    payload = {
        "customerEmail":   "",
        "customerId":      CUSTOMER_ID,
        "customerName":    CUSTOMER_NAME,
        "customerTel":     CUSTOMER_TEL,
        "endTime":         end_time,
        "groundId":        ground_id,
        "groundName":      ground_name,
        "groundType":      "0",
        "gymId":           GYM_ID,
        "gymName":         GYM_NAME,
        "id":              vid,
        "isIllegal":       "0",
        "messagePushType": "0",
        "orderDate":       order_time,
        "startTime":       start_time,
        "tmpEndTime":      end_time,
        "tmpOrderDate":    order_time,
        "tmpStartTime":    start_time,
        "userNum":         "1",
    }

    try:
        s = requests.Session()
        s.headers.update(HEADERS)
        r = s.post(
            SAVE_ORDER_URL,
            params={"userid": USER_ID, "token": token},
            json=payload,
            timeout=6,
        )
        snippet = r.text[:200].replace("\n", " ")
        _log("RESERVE", f"HTTP {r.status_code} | {ground_name} | {snippet}")
        try:
            return r.json()
        except Exception:
            return {"success": False, "msg": "non-json response"}
    except Exception as e:
        _log("RESERVE", f"请求异常: {e}")
        return {"success": False, "msg": f"request error: {e}"}