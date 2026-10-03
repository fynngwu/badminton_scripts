"""utils.py — 稳定原子能力层

    所有函数都是"一次输入 → 一次输出"，不维护任何跨调用状态。
    Engine 决定何时调用、调几次、调用后状态怎么变。

依赖：
    pip install pywin32 httpx opencv-python pillow numpy
"""
from __future__ import annotations

import base64
import io
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import httpx
import numpy as np
from PIL import Image

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore


log = logging.getLogger("court")


# ══════════════════════════════════════════════════════
# 路径 / 常量
# ══════════════════════════════════════════════════════
ROOT        = Path(__file__).parent
CONFIG_PATH = ROOT / "config.toml"

BASE_URL  = "https://reservation.sustech.edu.cn"
URL_GEN   = BASE_URL + "/api/blade-base/captcha/generate/d"
URL_CHK   = BASE_URL + "/api/blade-base/captcha/check/d"
URL_SAVE  = BASE_URL + "/api/blade-app/qywx/saveOrder"
URL_QUERY = BASE_URL + "/api/blade-app/qywx/getOrderTimeConfigList"

MITM_HOST     = "127.0.0.1"
MITM_PORT     = 8080
MITM_UPSTREAM = os.environ.get("MITM_UPSTREAM", "").strip()

RESERVATION_HOST = "reservation.sustech.edu.cn"
WINDOW_KEYWORDS  = ("reservation.sustech.edu.cn", "reservation")

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


# ══════════════════════════════════════════════════════
# 配置文件读写（原子操作）
# ══════════════════════════════════════════════════════
def read_config_dict(path: Path = CONFIG_PATH) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def write_config_string(key: str, value: str, path: Path = CONFIG_PATH) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = rf'(?m)^(\s*{re.escape(key)}\s*=\s*)"[^"]*"'
    new_text, n = re.subn(pattern, rf'\1"{value}"', text, count=1)
    if n == 0:
        raise RuntimeError(f"config.toml 未找到字符串字段 {key!r}")
    path.write_text(new_text, encoding="utf-8")


def write_config_int(key: str, value: int, path: Path = CONFIG_PATH) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = rf'(?m)^(\s*{re.escape(key)}\s*=\s*)-?\d+'
    new_text, n = re.subn(pattern, rf'\g<1>{int(value)}', text, count=1)
    if n == 0:
        raise RuntimeError(f"config.toml 未找到整数字段 {key!r}")
    path.write_text(new_text, encoding="utf-8")


# ══════════════════════════════════════════════════════
# HTTP 客户端（进程内单例）
# ══════════════════════════════════════════════════════
_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(headers=HEADERS, timeout=6.0, trust_env=False)
    return _client


# ══════════════════════════════════════════════════════
# 纯函数小工具
# ══════════════════════════════════════════════════════
def norm_hm(t: str) -> str:
    if not t or ":" not in t:
        return t
    h, m = t.split(":", 1)
    return f"{h.zfill(2)}:{m}" if h.isdigit() else t


def is_auth_fail(j: dict) -> bool:
    msg = str(j.get("msg", "") or "")
    if "鉴权失败" in msg:
        return True
    low = msg.lower()
    if "token" in low and ("失效" in msg or "无效" in msg or "过期" in msg):
        return True
    if j.get("code") in (401, 403):
        return True
    return False


def classify_order_reply(r: dict) -> tuple[str, str]:
    """saveOrder 返回分类：ok / busy / error"""
    if r.get("success"):
        return "ok", "✅ 预约成功"
    msg = str(r.get("msg", "") or "")
    for kw in ("繁忙", "稍后", "系统忙", "请求过于", "频繁",
               "frequent", "too many", "try again", "busy"):
        if kw in msg.lower() or kw in msg:
            return "busy", "⚠ 系统繁忙"
    if not msg or msg == "non-json":
        return "error", "❌ 响应异常"
    return "error", "❌ 下单失败"


def slot_covers(avail: dict, start: str, end: str) -> bool:
    h1, m1 = map(int, start.split(":"))
    h2, m2 = map(int, end.split(":"))
    t, e = h1 * 60 + m1, h2 * 60 + m2
    while t < e:
        if avail.get(f"{t // 60:02d}:{t % 60:02d}") != "1":
            return False
        t += 30
    return True


# ══════════════════════════════════════════════════════
# 验证码 ROTATE 求解
# ══════════════════════════════════════════════════════
_DB = np.load(ROOT / "fast_db.npz", allow_pickle=False)
_BG_DESCS    = _DB["bg_descs"]
_REF_FFTS    = _DB["ref_ffts"]
_ANGLE_BINS  = int(_DB["angle_bins"][0])
_RADIAL_BINS = int(_DB["radial_bins"][0])
_DIR_SIGN    = int(_DB["direction_sign"][0])


class _CaptchaSolver:
    W, H, TRAVEL = 242, 180, 242
    BG_W, BG_H   = 48, 36

    def __init__(self, timeout: float = 3.5):
        self.timeout = timeout
        self.timing: dict = {}

    async def solve(self) -> str:
        t0 = time.perf_counter()
        cid, cap = await self._generate()
        t1 = time.perf_counter()

        start, mono = datetime.now(), time.monotonic()
        move_x = await __import__("asyncio").to_thread(self._recognize, cap)
        t2 = time.perf_counter()

        track = await __import__("asyncio").to_thread(self._make_track_sync, move_x, mono)
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
        r = await get_client().post(URL_GEN, json={"type": "ROTATE"},
                                    timeout=self.timeout)
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
        r = await get_client().post(URL_CHK, json=payload, timeout=self.timeout)
        r.raise_for_status()
        j = r.json()
        if not j.get("success"):
            raise RuntimeError(j.get("msg", "验证码校验失败"))
        vid = j.get("data")
        if not vid:
            raise RuntimeError("验证码通过但 data 为空")
        return vid

    def _recognize(self, cap) -> int:
        bg   = self._prep_bg(self._decode(cap["backgroundImage"]))
        tpl  = self._prep_tpl(self._decode(cap["templateImage"]))
        desc = self._desc(self._gray(bg))
        best = int(np.argmax(_BG_DESCS @ desc))
        sig  = self._sig(self._gray(tpl))
        corr = np.fft.irfft(_REF_FFTS[best] * np.conj(np.fft.rfft(sig)),
                            n=_ANGLE_BINS)
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
        small = cv2.resize(gray, (self.BG_W, self.BG_H),
                           interpolation=cv2.INTER_AREA)
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

    @staticmethod
    def _make_track_sync(move_x, mono):
        import random as _random
        def t(): return int((time.monotonic() - mono) * 1000)
        out = [{"x": 0, "y": _random.randint(-2, 2), "type": "down", "t": t()}]
        x = 0
        while x < move_x:
            x = min(x + (2 if _random.random() < 0.8 else 1), move_x)
            time.sleep(0.001)
            out.append({"x": x, "y": _random.randint(-2, 2), "type": "move", "t": t()})
        out.append({"x": move_x, "y": _random.randint(-2, 2), "type": "up", "t": t()})
        return out


async def get_vid_once(timeout: float = 3.5) -> tuple[str, dict]:
    """单次：generate → recognize → track → check，返回 (vid, timing)。"""
    solver = _CaptchaSolver(timeout)
    vid = await solver.solve()
    return vid, solver.timing


# ══════════════════════════════════════════════════════
# 预约 API
# ══════════════════════════════════════════════════════
async def get_order_once(court: Court, date: str, token: str, user_id: str,
                         timeout: float = 4.0) -> dict:
    """单次查询某个场地的时段配置。

    返回：
        {"ok": bool, "auth_fail": bool, "avail": dict, "blocks": list, "error": str}
    """
    try:
        r = await get_client().get(URL_QUERY, params={
            "groundId": court.id, "startDate": date, "endDate": date,
            "userid": user_id, "token": token,
        }, timeout=timeout)
    except Exception as e:
        log.warning("查询 %s 异常: %s", court.name, e)
        return {"ok": False, "auth_fail": False, "error": f"网络异常: {e}"}

    if r.status_code != 200:
        log.warning("查询 %s HTTP %d: %s", court.name, r.status_code, r.text[:120])
        return {"ok": False, "auth_fail": False, "error": f"HTTP {r.status_code}"}

    try:
        j = r.json()
    except Exception:
        return {"ok": False, "auth_fail": False, "error": "non-json"}

    if not j.get("success"):
        msg = str(j.get("msg", "") or "")
        auth = is_auth_fail(j)
        log.warning("查询 %s %s: %s",
                    court.name, "鉴权失败" if auth else "success=false", msg)
        return {"ok": False, "auth_fail": auth,
                "error": msg or f"success=false (code={j.get('code')})"}

    avail: dict[str, str] = {}
    blocks: list[dict] = []
    for cfg in j.get("data", {}).get("configList", []):
        if cfg.get("date") != date:
            continue
        for blk in cfg.get("timeBlockList", []):
            t = norm_hm(blk.get("time", ""))
            e = norm_hm(blk.get("endTime", ""))
            avail[t] = blk.get("status")
            blocks.append({
                "time": t, "endTime": e,
                "status": blk.get("status"),
                "customerName": blk.get("customerName"),
                "customerCode": blk.get("customerCode"),
                "customerTel":  blk.get("customerTel"),
                "type":         blk.get("type"),
            })
    blocks.sort(key=lambda b: b["time"])
    return {"ok": True, "auth_fail": False, "avail": avail, "blocks": blocks}


async def save_order_once(
    *,
    vid: str, court: Court, date: str, start: str, end: str, token: str,
    user_id: str, customer_id: str, customer_name: str, customer_tel: str,
    gym_id: str, gym_name: str,
) -> dict:
    """单次 POST saveOrder。只返回原始响应 dict，不发事件、不落盘。"""
    st = f"{date} {start}:00"
    et = f"{date} {end}:00"
    now = datetime.now()
    order_dt = datetime.strptime(st, "%Y-%m-%d %H:%M:%S").replace(
        hour=now.hour, minute=now.minute, second=0, microsecond=0)
    order_time = order_dt.strftime("%Y-%m-%d %H:%M:%S")

    payload = {
        "customerEmail": "", "customerId": customer_id,
        "customerName": customer_name, "customerTel": customer_tel,
        "endTime": et, "groundId": court.id, "groundName": court.name,
        "groundType": "0", "gymId": gym_id, "gymName": gym_name,
        "id": vid, "isIllegal": "0", "messagePushType": "0",
        "orderDate": order_time, "startTime": st,
        "tmpEndTime": et, "tmpOrderDate": order_time, "tmpStartTime": st,
        "userNum": "1",
    }

    send_hms = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    t0 = time.monotonic()
    log.info("→ 下单 %s 发送 @ %s (vid=%s…)", court.name, send_hms, vid[:8])

    try:
        r = await get_client().post(
            URL_SAVE, params={"userid": user_id, "token": token},
            json=payload, timeout=6.0,
        )
        dt_ms = (time.monotonic() - t0) * 1000
        recv_hms = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        log.info("← 下单 %s 返回 @ %s (+%.0fms): HTTP %d | %s",
                 court.name, recv_hms, dt_ms, r.status_code, r.text[:180])
        try:
            result = r.json()
        except Exception:
            result = {"success": False, "msg": "non-json"}
    except Exception as e:
        dt_ms = (time.monotonic() - t0) * 1000
        log.warning("✗ 下单 %s 异常 (+%.0fms): %s", court.name, dt_ms, e)
        result = {"success": False, "msg": str(e)}

    return result


# ══════════════════════════════════════════════════════
# Windows 窗口操作（refresh reservation 窗口）
# ══════════════════════════════════════════════════════
_cached_hwnd: int = 0


def _find_hwnd() -> int:
    import win32gui
    found: list[int] = []

    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd) or ""
            if title and any(k in title for k in WINDOW_KEYWORDS):
                found.append(hwnd)
        return True

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else 0


def _valid_hwnd(hwnd: int) -> bool:
    if not hwnd:
        return False
    try:
        import win32gui
        if not win32gui.IsWindow(hwnd):
            return False
        title = win32gui.GetWindowText(hwnd) or ""
        return any(k in title for k in WINDOW_KEYWORDS)
    except Exception:
        return False


def _ensure_foreground(hwnd: int, timeout: float = 0.6) -> bool:
    """轮询直到 hwnd 真正成为前台窗口，或超时。"""
    import win32gui
    import win32process
    import win32api

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            if win32gui.GetForegroundWindow() == hwnd:
                return True
        except Exception:
            pass

        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        try:
            if win32gui.GetForegroundWindow() == hwnd:
                return True
        except Exception:
            pass

        try:
            fg = win32gui.GetForegroundWindow()
            if fg and fg != hwnd:
                fg_tid = win32process.GetWindowThreadProcessId(fg)[0]
                my_tid = win32api.GetCurrentThreadId()
                if fg_tid != my_tid:
                    win32process.AttachThreadInput(fg_tid, my_tid, True)
                    try:
                        win32gui.BringWindowToTop(hwnd)
                        win32gui.SetForegroundWindow(hwnd)
                    finally:
                        win32process.AttachThreadInput(fg_tid, my_tid, False)
        except Exception:
            pass

        try:
            if win32gui.GetForegroundWindow() == hwnd:
                return True
        except Exception:
            pass

        if time.monotonic() >= deadline:
            return False
        time.sleep(0.03)


def _mouse_click(x: int, y: int) -> None:
    import win32api
    import win32con

    win32api.SetCursorPos((x, y))
    time.sleep(0.03)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.03)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def _send_ctrl_r() -> None:
    import win32api
    import win32con

    VK_R = 0x52
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    time.sleep(0.02)
    win32api.keybd_event(VK_R, 0, 0, 0)
    time.sleep(0.02)
    win32api.keybd_event(VK_R, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.02)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)


def refresh_window(
    pre_click_delay: float = 0.10,
    post_click_delay: float = 0.10,
    fg_timeout: float = 0.6,
    double_click: bool = True,
) -> bool:
    """找到 reservation 窗口 → 上前台 → 点击 → Ctrl+R。"""
    global _cached_hwnd

    try:
        import win32gui
        import win32con
        import win32api
    except ImportError:
        print("[refresh] 需要 pywin32：pip install pywin32")
        return False

    hwnd = _cached_hwnd if _valid_hwnd(_cached_hwnd) else 0
    if not hwnd:
        hwnd = _find_hwnd()
        if hwnd:
            _cached_hwnd = hwnd

    if not hwnd:
        print("[refresh] 未找到预约窗口（企业微信是否已打开 reservation？）")
        _cached_hwnd = 0
        return False

    try:
        old_pos = win32api.GetCursorPos()
    except Exception:
        old_pos = None

    ok = False
    try:
        print(f"[refresh] 找到预约窗口: {win32gui.GetWindowText(hwnd)}")

        try:
            if win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMINIMIZED:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.15)
        except Exception:
            pass

        if not _ensure_foreground(hwnd, timeout=fg_timeout):
            print("[refresh] ⚠ 未能在前台锁定窗口，仍然尝试点击…")

        if pre_click_delay > 0:
            time.sleep(pre_click_delay)

        cx = cy = None
        try:
            rect = win32gui.GetWindowRect(hwnd)
            w = rect[2] - rect[0]
            h = rect[3] - rect[1]
            cx = rect[0] + w // 2
            cy = rect[1] + max(100, int(h * 0.20))
        except Exception as e:
            print(f"[refresh] 计算点击坐标失败（忽略）: {e}")

        if cx is not None and cy is not None:
            try:
                _mouse_click(cx, cy)
                if double_click:
                    time.sleep(0.08)
                    _mouse_click(cx, cy)
            except Exception as e:
                print(f"[refresh] click 失败（忽略）: {e}")

        if post_click_delay > 0:
            time.sleep(post_click_delay)

        if not _ensure_foreground(hwnd, timeout=0.4):
            print("[refresh] ⚠ 发送 Ctrl+R 前窗口不在前台，仍尝试发送…")
            if cx is not None and cy is not None:
                try:
                    _mouse_click(cx, cy)
                    time.sleep(0.08)
                except Exception:
                    pass

        _send_ctrl_r()
        ok = True
    except Exception as e:
        print(f"[refresh] error: {e}")
        _cached_hwnd = 0
    finally:
        if old_pos is not None:
            try:
                win32api.SetCursorPos(old_pos)
            except Exception:
                pass

    if ok:
        print("[refresh] 已发送 Ctrl+R")
    return ok


# ══════════════════════════════════════════════════════
# mitmproxy 抓 token（供 mitm_addon.py 调用）
# ══════════════════════════════════════════════════════
_last_token_seen = ""
_last_token_seen_at = 0.0
def capture_token_from_flow(
    flow,
    host: str = RESERVATION_HOST,
    config_path: Path = CONFIG_PATH,
) -> None:
    global _last_token_seen, _last_token_seen_at

    try:
        if flow.request.pretty_host != host:
            return
    except Exception:
        return

    token = flow.request.query.get("token")
    if not token:
        return

    now = time.monotonic()

    # 同一个 token 在 1 秒内出现多次，只处理第一次
    if token == _last_token_seen and now - _last_token_seen_at < 1.0:
        return

    _last_token_seen = token
    _last_token_seen_at = now

    try:
        text = config_path.read_text(encoding="utf-8")

        m = re.search(
            r'(?m)^\s*token\s*=\s*"([^"]*)"',
            text,
        )
        current = m.group(1) if m else ""

        path = flow.request.path.split("?", 1)[0]
        ts = time.strftime("%H:%M:%S")

        if token == current:
            print(
                f"[{ts}] [TOKEN] 捕获成功 SAME "
                f"{token[:8]}... {path}"
            )
            return

        new, n = re.subn(
            r'(?m)^(\s*token\s*=\s*)"[^"]*"',
            rf'\1"{token}"',
            text,
            count=1,
        )

        if n:
            config_path.write_text(new, encoding="utf-8")
            print(
                f"[{ts}] [TOKEN] 更新成功 "
                f"{current[:8]}... -> {token[:8]}..."
            )
        else:
            print("[TOKEN] config.toml 未找到 token 字段")

    except Exception as e:
        print(f"[TOKEN] 写入失败: {e}")

def kill_port_holder(host: str, port: int) -> list[int]:
    """强杀所有监听 host:port 的进程，返回被杀的 PID 列表。

    Windows: netstat -ano 找 PID → taskkill /F
    Linux/Mac: lsof -ti 找 PID → SIGKILL
    """
    pids: set[int] = set()

    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception as e:
            log.warning("[kill-port] netstat 失败: %s", e)
            return []
        needle = f":{port}"
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5 or parts[3] != "LISTENING":
                continue
            if not parts[1].endswith(needle):
                continue
            try:
                pids.add(int(parts[4]))
            except ValueError:
                pass
    else:
        try:
            out = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for tok in out.split():
                try:
                    pids.add(int(tok))
                except ValueError:
                    pass
        except Exception as e:
            log.warning("[kill-port] lsof 失败: %s", e)
            return []

    killed: list[int] = []
    for pid in pids:
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True, timeout=5,
                )
            else:
                os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except Exception as e:
            log.warning("[kill-port] 结束 PID %d 失败: %s", pid, e)
    return killed

# ══════════════════════════════════════════════════════
# mitmdump 底层封装
# ══════════════════════════════════════════════════════
def port_listening(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def build_mitm_cmd() -> list[str] | None:
    script = str(ROOT / "mitm_addon.py")
    args: list[str] = [
        "--listen-host", MITM_HOST,
        "--listen-port", str(MITM_PORT),
        "-s", script,
    ]
    if MITM_UPSTREAM:
        args += ["--mode", f"upstream:{MITM_UPSTREAM}"]

    exe_name = "mitmdump.exe" if sys.platform == "win32" else "mitmdump"

    local = Path(sys.executable).with_name(exe_name)
    if local.exists():
        return [str(local)] + args

    exe = shutil.which("mitmdump")
    if exe:
        return [exe] + args

    try:
        import mitmproxy  # noqa: F401
    except Exception:
        return None
    return [sys.executable, "-m", "mitmproxy.tools.main", "dump"] + args


# 白名单：只有命中这些关键字的行才进 log
_KEEP_RE = re.compile(
    r"\[TOKEN\]"
    r"|listening at|Proxy server"
    r"|error|Error|ERROR"
    r"|warn|Warn|WARNING|WARN"
    r"|Traceback|Exception"
)

# 黑名单：即便命中白名单，也要丢弃的行（噪音）
_DROP_RE = re.compile(
    r"TLS handshake failed"
    r"|Client TLS"
    r"|unknown ca"
    r"|does not trust the proxy"
)


def _decode_mitm(raw: bytes) -> str:
    """mitmdump 子进程在 Windows 上默认用 cp936(GBK) 写 stdout，
    这里按 UTF-8 → GBK → 兜底 的顺序解码，避免中文变问号。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _mitm_reader(pipe) -> None:
    try:
        for raw in iter(pipe.readline, b""):
            if not raw:
                break
            txt = _decode_mitm(raw).rstrip()
            if not txt:
                continue
            if _DROP_RE.search(txt):        # 先杀噪音
                continue
            if _KEEP_RE.search(txt):        # 再走白名单
                log.info("[mitm] %s", txt)
    except Exception as e:
        log.warning("[mitm] reader 异常: %s", e)
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def spawn_mitmdump() -> subprocess.Popen | None:
    cmd = build_mitm_cmd()
    if cmd is None:
        log.error("[mitm] 未找到 mitmdump —— 请先 `pip install mitmproxy`")
        return None

    log.info("[mitm] 启动命令: %s", cmd[0])
    log.info("[mitm] 参数: %s", " ".join(cmd[1:]))

    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=creationflags, bufsize=0,
        )
    except FileNotFoundError as e:
        log.error("[mitm] 可执行文件不存在: %s", e)
        return None
    except Exception as e:
        log.error("[mitm] 启动失败: %s", e)
        return None

    threading.Thread(target=_mitm_reader, args=(proc.stdout,), daemon=True).start()
    return proc