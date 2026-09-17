"""reserve_v4.py — vid 一次性缓冲 + 场地轮询 + 系统异常自动剔除"""

import base64
import io
import json
import queue
import random
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import requests
from PIL import Image


# ============================================================
# 1. 配置
# ============================================================

BASE_URL = "https://reservation.sustech.edu.cn"

GENERATE_URL   = BASE_URL + "/api/blade-base/captcha/generate/d"
CHECK_URL      = BASE_URL + "/api/blade-base/captcha/check/d"
SAVE_ORDER_URL = BASE_URL + "/api/blade-app/qywx/saveOrder"


# ------------------------------------------------------------
# 登录信息
# ------------------------------------------------------------
USER_ID = "12632895"
TOKEN   = "82e5a4f1-3545-4b98-8ed9-9d1e5fc94f04"

CUSTOMER_ID   = "2097687996508684290"
CUSTOMER_NAME = "吴丰杨"
CUSTOMER_TEL  = "13651486427"

GYM_ID   = "1297443858304540673"
GYM_NAME = "润杨羽毛球馆"


# ------------------------------------------------------------
# 候选场地池（1–10 号场）
# ------------------------------------------------------------
CANDIDATE_GROUNDS = [
    ("1298272433186332673", "1号场"),
    ("1298272520994086913", "2号场"),
    ("1298272615009411073", "3号场"),
    ("1298272709167341570", "4号场"),
    ("1298272791098875905", "5号场"),
    # ("1298273087183183874", "6号场"),
    ("1298273175146127362", "7号场"),
    ("1298273265650819073", "8号场"),
    ("1298273399927267330", "9号场"),
    ("1298273500317933570", "10号场"),
]


# ------------------------------------------------------------
# 预约信息
# ------------------------------------------------------------
START_TIME = "2026-09-19 20:00:00"
END_TIME   = "2026-09-19 22:00:00"

DRY_RUN = False


# ------------------------------------------------------------
# 启动时间节点
# ------------------------------------------------------------
PREFETCH_START_HMS = (20, 0, 1)   # 预取线程启动时刻 (时, 分, 秒)
GATE_OPEN_HMS      = (20, 0, 4)   # 开闸（开始抢）时刻 (时, 分, 秒)


# ------------------------------------------------------------
# 抢单参数
# ------------------------------------------------------------
MAX_ATTEMPTS = 9   # 总尝试次数上限

# 命中这些关键词 → 视为该场地不可用，永久剔除
DROP_GROUND_KEYWORDS = ("系统异常")


# ------------------------------------------------------------
# 预取参数
# ------------------------------------------------------------
PREFETCH_BATCH_SIZE    = 3      # 每批并发起 3 个线程
PREFETCH_BATCH_TIMEOUT = 8.1    # 单批节拍：每 8s 开一批新线程
PREFETCH_RETRY_DELAY   = 1.0    # 单次 solve 失败后，等多久再重试
BUFFER_MAX             = 16     # vid 队列上限


# ------------------------------------------------------------
# 抢单节流
# ------------------------------------------------------------
INTER_GROUND_MIN_S    = 1.0   # 每场地之间抖动：400~600 ms
INTER_GROUND_JITTER_S = 0.5

INTER_ROUND_MIN_S    = 0.20    # 整轮之间停顿
INTER_ROUND_JITTER_S = 0.15


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

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# 2. 计时 / 等待辅助
# ============================================================

class Stopwatch:
    def __init__(self, verbose=True):
        self.verbose = verbose
        self._t = time.perf_counter()
        self._start = self._t

    def lap(self, label):
        now = time.perf_counter()
        dt_ms = (now - self._t) * 1000
        if self.verbose:
            print(f"    ⏱ {label:<38s} {dt_ms:8.1f} ms")
        self._t = now
        return dt_ms

    def total(self):
        return (time.perf_counter() - self._start) * 1000


def _wait_until(target: datetime, tolerance=0.02):
    while True:
        remain = (target - datetime.now()).total_seconds()
        if remain <= 0:
            return
        if remain > tolerance:
            time.sleep(0.005)


# ============================================================
# 3. 小工具
# ============================================================

def build_order_date(start_time: str) -> str:
    reserve_dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
    now = datetime.now()
    order_dt = reserve_dt.replace(
        hour=now.hour, minute=now.minute, second=0, microsecond=0,
    )
    return order_dt.strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# 4. ROTATE 验证码自动求解
# ============================================================

_DB_PATH = Path(__file__).resolve().parent / "fast_db.npz"

_db_t0 = time.perf_counter()
_DB = np.load(_DB_PATH, allow_pickle=False)
_DB_LOAD_MS = (time.perf_counter() - _db_t0) * 1000

_BG_DESCS = _DB["bg_descs"]
_REF_FFTS = _DB["ref_ffts"]

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

    def __init__(self, session):
        self.session = session

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

        try:
            vid = self._check(
                captcha_id,
                track_list,
                start_time,
                stop_time,
            )
        except Exception as e:
            print(
                f"    captcha_id={captcha_id} | "
                f"generate={(t1-t0)*1000:.0f}ms | "
                f"solve={(t2-t1)*1000:.0f}ms | "
                f"track={(t3-t2)*1000:.0f}ms | "
                f"before_check={(t3-t0)*1000:.0f}ms | "
                f"check_error={e}"
            )
            raise

        return vid

    def _generate(self):
        r = self.session.post(
            GENERATE_URL,
            json={"type": "ROTATE"},
            timeout=(2, 4),
        )
        r.raise_for_status()

        result = r.json()

        if not isinstance(result, dict):
            raise RuntimeError(
                f"generate result不是dict: {type(result).__name__} {result!r}"
            )

        if "id" not in result or "captcha" not in result:
            raise RuntimeError(
                f"generate字段异常: {result!r}"
            )

        captcha = result["captcha"]

        if not isinstance(captcha, dict):
            raise RuntimeError(
                f"captcha类型异常: "
                f"{type(captcha).__name__}, value={captcha!r}"
            )

        if "backgroundImage" not in captcha or "templateImage" not in captcha:
            raise RuntimeError(
                f"captcha字段异常: {captcha.keys()}"
            )

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
        r = self.session.post(CHECK_URL, json=payload, timeout=(2, 4))
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
        bg = self._prepare_background(self._decode_data_uri(captcha["backgroundImage"]))
        tpl = self._prepare_template(self._decode_data_uri(captcha["templateImage"]))

        bg_gray = self._to_gray(bg)
        desc = self._get_bg_desc(bg_gray)
        scores = _BG_DESCS @ desc
        best_i = int(np.argmax(scores))

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
# 5. vid 缓冲（queue.Queue + 每批 3 个独立线程，固定 8s 节拍）
# ============================================================

class VidBuffer:
    """
    固定节拍：每 PREFETCH_BATCH_TIMEOUT 秒发起一批 3 个独立 daemon 线程。
    每个线程在批次 deadline 之前反复重试 solve，成功即 put_nowait 入队。
    线程跑完不会提前结束批次——批次一定等满 8s 才开下一批。

    超时的线程无法强杀，交给 requests 自己的 timeout 收尾。
    """

    def __init__(self,
                 batch_size=PREFETCH_BATCH_SIZE,
                 batch_timeout=PREFETCH_BATCH_TIMEOUT,
                 retry_delay=PREFETCH_RETRY_DELAY,
                 buffer_max=BUFFER_MAX):
        self.batch_size = batch_size
        self.batch_timeout = batch_timeout
        self.retry_delay = retry_delay

        self._buf = queue.Queue(maxsize=buffer_max)
        self._stop = threading.Event()
        self._thread = None

    # ---------- 生命周期 ----------

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._worker, name="vid-prefetcher", daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.batch_timeout + 2.0)

    # ---------- 单个线程：在 deadline 之前反复重试 ----------

    def _solve_and_put(self, deadline):
        t0 = time.monotonic()
        attempt = 0

        while not self._stop.is_set() and time.monotonic() < deadline:
            attempt += 1
            try:
                fresh_session = requests.Session()
                fresh_session.headers.update(HEADERS)
                solver = CaptchaSolver(fresh_session)
                vid = solver.solve()
            except Exception as exc:
                msg = str(exc)
                remain = deadline - time.monotonic()
                if remain <= self.retry_delay:
                    print(f"    [prefetch] solve 失败"
                          f"(第 {attempt} 次，节拍将到): {msg}")
                    return
                print(f"    [prefetch] solve 失败(第 {attempt} 次): "
                      f"{msg} → {self.retry_delay}s 后重试")
                if self._stop.wait(self.retry_delay):
                    return
                continue

            # 成功：入队后本线程结束
            try:
                self._buf.put_nowait(vid)
                depth = self._buf.qsize()
                print(f"    [prefetch] +1 vid "
                      f"(solve {time.monotonic() - t0:.2f}s, "
                      f"depth={depth}, 第 {attempt} 次尝试)")
            except queue.Full:
                print(f"    [prefetch] 队列已满，丢弃 vid")
            return

        if not self._stop.is_set():
            print(f"    [prefetch] 到节拍仍未成功（尝试 {attempt} 次），"
                  f"等待下一批")

    # ---------- 主 worker：固定 8s 一批 ----------

    def _worker(self):
        while not self._stop.is_set():
            t0 = time.monotonic()
            deadline = t0 + self.batch_timeout

            threads = []
            for _ in range(self.batch_size):
                t = threading.Thread(
                    target=self._solve_and_put,
                    args=(deadline,),
                    daemon=True,
                )
                t.start()
                threads.append(t)

            # 固定节拍：跑完也不提前开下一批，等到 deadline
            while time.monotonic() < deadline:
                if self._stop.is_set():
                    return
                time.sleep(0.02)

            elapsed = time.monotonic() - t0
            alive = sum(1 for t in threads if t.is_alive())
            if alive:
                print(f"    [prefetch] 节拍到 {elapsed:.2f}s，"
                      f"{alive}/{self.batch_size} 线程仍未结束 → 开新一批")
            else:
                print(f"    [prefetch] 节拍到 {elapsed:.2f}s "
                      f"(depth={self._buf.qsize()})")

    # ---------- 前台接口 ----------

    def get(self, timeout=5.0):
        try:
            return self._buf.get(timeout=timeout)
        except queue.Empty:
            print("    [consume ] 等待 vid 超时")
            return None


# ============================================================
# 6. saveOrder
# ============================================================

def _build_payload(vid, ground_id, ground_name, start_time, end_time):
    order_time = build_order_date(start_time)
    return {
        "customerEmail": "",
        "customerId":   CUSTOMER_ID,
        "customerName": CUSTOMER_NAME,
        "customerTel":  CUSTOMER_TEL,
        "endTime":      end_time,
        "groundId":     ground_id,
        "groundName":   ground_name,
        "groundType":   "0",
        "gymId":        GYM_ID,
        "gymName":      GYM_NAME,
        "id":           vid,
        "isIllegal":    "0",
        "messagePushType": "0",
        "orderDate":    order_time,
        "startTime":    start_time,
        "tmpEndTime":   end_time,
        "tmpOrderDate": order_time,
        "tmpStartTime": start_time,
        "userNum":      "1",
    }


def post_order_once(vid, ground_id, ground_name, start_time, end_time):
    if DRY_RUN:
        return {"success": False, "msg": "DRY_RUN"}

    payload = _build_payload(vid, ground_id, ground_name, start_time, end_time)
    params = {"userid": USER_ID, "token": TOKEN}

    try:
        r = session.post(SAVE_ORDER_URL, params=params, json=payload, timeout=4)
    except Exception as exc:
        print(f"      [{datetime.now():%H:%M:%S}] request error: {exc}")
        return {"success": False, "msg": f"request error: {exc}"}

    snippet = r.text[:160].replace("\n", " ")
    print(f"      [{datetime.now():%H:%M:%S}] "
          f"HTTP {r.status_code} | {snippet}")
    try:
        return r.json()
    except Exception:
        return {"success": False, "msg": "non-json response"}


# ============================================================
# 7. main
# ============================================================

def main():
    print("=" * 55)
    print(" SUSTech 羽毛球场预约（vid 一次性缓冲 + 场地轮询）")
    print("=" * 55)

    vid_buffer = VidBuffer(
        batch_size=PREFETCH_BATCH_SIZE,
        batch_timeout=PREFETCH_BATCH_TIMEOUT,
        retry_delay=PREFETCH_RETRY_DELAY,
        buffer_max=BUFFER_MAX,
    )

    t0 = time.perf_counter()

    # ---------- 启动预取线程 ----------
    target = datetime.now().replace(
        hour=PREFETCH_START_HMS[0],
        minute=PREFETCH_START_HMS[1],
        second=PREFETCH_START_HMS[2],
        microsecond=0,
    )
    print(f"\n⏳ 等待到 {target:%H:%M:%S} 启动预取线程 ...")
    _wait_until(target)
    vid_buffer.start()
    print(f"✅ 预取线程已启动 now={datetime.now():%H:%M:%S.%f}")

    # ---------- 开闸 ----------
    target = datetime.now().replace(
        hour=GATE_OPEN_HMS[0],
        minute=GATE_OPEN_HMS[1],
        second=GATE_OPEN_HMS[2],
        microsecond=0,
    )
    print(f"⏳ 等待到 {target:%H:%M:%S} 开闸 ...")
    _wait_until(target)
    print(f"🚀 开闸 now={datetime.now():%H:%M:%S.%f}\n")

    # 活跃池：只减不增，命中“系统异常/系统繁忙”就从中剔除
    active_grounds = list(CANDIDATE_GROUNDS)

    attempt = 0
    round_no = 0

    try:
        while active_grounds and attempt < MAX_ATTEMPTS:
            round_no += 1
            pool = active_grounds[:]
            random.shuffle(pool)
            tag = "随机"

            print(f"\n─── 第 {round_no} 轮（{tag}，{len(pool)} 个候选）───")

            for ground_id, ground_name in pool:
                if (ground_id, ground_name) not in active_grounds:
                    continue

                attempt += 1
                if attempt > MAX_ATTEMPTS:
                    break

                vid = vid_buffer.get(timeout=5.0)
                if vid is None:
                    print("    ⚠️ 拿不到 vid，本轮跳过")
                    break

                result = post_order_once(
                    vid, ground_id, ground_name, START_TIME, END_TIME,
                )
                msg = (result.get("msg") or "")

                if result.get("success"):
                    elapsed_ms = (time.perf_counter() - t0) * 1000
                    print(f"\n🎉 抢到 {ground_name} | "
                          f"尝试 {attempt} 次 | 总耗时 {elapsed_ms:.0f} ms")
                    return

                print(f"[{attempt:3d}] {ground_name:<6s}  ❌ {msg}")

                # 命中不可用关键词 → 视为该时段已被占，永久剔除该场地
                if any(k in msg for k in DROP_GROUND_KEYWORDS):
                    if (ground_id, ground_name) in active_grounds:
                        active_grounds.remove((ground_id, ground_name))
                        print(f"    🗑 {ground_name} {msg} → 从候选池移除，"
                              f"剩 {len(active_grounds)} 个")

                time.sleep(INTER_GROUND_MIN_S
                           + random.uniform(0.0, INTER_GROUND_JITTER_S))

            if active_grounds and attempt < MAX_ATTEMPTS:
                print(f"    ⏸ 第 {round_no} 轮结束，稍作停顿")
                time.sleep(INTER_ROUND_MIN_S
                           + random.uniform(0.0, INTER_ROUND_JITTER_S))

        if attempt >= MAX_ATTEMPTS:
            print(f"\n⚠️ 已达尝试上限 {MAX_ATTEMPTS} 次，停止")
        elif not active_grounds:
            print("\n⚠️ 候选池已空（所有场地都报系统异常/系统繁忙），停止")

    finally:
        vid_buffer.stop()


if __name__ == "__main__":
    main()