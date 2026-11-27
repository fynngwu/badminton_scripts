"""utils.py — 稳定原子能力层

    所有函数都是"一次输入 → 一次输出"，不维护任何跨调用状态。
    Engine 决定何时调用、调几次、调用后状态怎么变。

依赖：
    pip install pywinauto curl_cffi opencv-python-headless pillow numpy
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from curl_cffi import requests as curl_requests

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


def write_config_string_renamed(key: str, old_key: str, value: str,
                                path: Path = CONFIG_PATH) -> None:
    """写字符串字段；若新字段不存在，把旧字段整行改名为新字段再写。"""
    text = path.read_text(encoding="utf-8")
    pattern = rf'(?m)^(\s*{re.escape(key)}\s*=\s*)"[^"]*"'
    new_text, n = re.subn(pattern, rf'\1"{value}"', text, count=1)
    if n:
        path.write_text(new_text, encoding="utf-8")
        return
    pattern_old = rf'(?m)^(\s*){re.escape(old_key)}(\s*=\s*)"[^"]*"'
    new_text, n = re.subn(pattern_old, rf'\1{key}\2"{value}"', text, count=1)
    if n:
        path.write_text(new_text, encoding="utf-8")
        return
    raise RuntimeError(f"config.toml 未找到字段 {key!r} 或 {old_key!r}")


def write_config_int_renamed(key: str, old_key: str, value: int,
                             path: Path = CONFIG_PATH) -> None:
    """写整数字段；旧的数组/标量字段整行改名，新字段不存在则插到 [auto] 段首。"""
    text = path.read_text(encoding="utf-8")
    pattern = rf'(?m)^(\s*{re.escape(key)}\s*=\s*)[^\n]*'
    new_text, n = re.subn(pattern, rf'\g<1>{int(value)}', text, count=1)
    if n:
        path.write_text(new_text, encoding="utf-8")
        return
    new_text, n = re.subn(
        rf'(?m)^(\s*){re.escape(old_key)}(\s*=\s*)[^\n]*',
        rf'\g<1>{key}\g<2>{int(value)}', text, count=1)
    if n:
        path.write_text(new_text, encoding="utf-8")
        return
    m = re.search(r'(?m)^\[auto\][^\n]*$', text)
    if not m:
        raise RuntimeError(f"config.toml 未找到 [auto] 段，无法写入 {key}")
    new_text = text[:m.end()] + f"\n{key} = {int(value)}" + text[m.end():]
    path.write_text(new_text, encoding="utf-8")


# ══════════════════════════════════════════════════════
# HTTP 客户端（curl_cffi AsyncSession 全局单例）
#
#   * 默认 HTTP/2（curl_cffi 文档明确）
#   * impersonate="chrome"：与 VID 池 worker-1 同源指纹
#   * 全局单例 → getOrder / saveOrder 复用同一条 TCP+TLS+H2 连接
# ══════════════════════════════════════════════════════
_cffi_session: curl_requests.AsyncSession | None = None


def get_cffi_session() -> curl_requests.AsyncSession:
    global _cffi_session
    if _cffi_session is None:
        _cffi_session = curl_requests.AsyncSession(
            impersonate="chrome",
            timeout=6.0,
            max_clients=20,
        )
    return _cffi_session


async def close_cffi_session() -> None:
    global _cffi_session
    if _cffi_session is not None:
        try:
            await _cffi_session.close()
        except Exception:
            pass
        _cffi_session = None


_get_starts: deque = deque()
_get_lock = asyncio.Lock()


async def limited_get(url, *, court_name="", quiet: bool = False, **kwargs):
    """同进程所有 GET 共用限流：任意 8.7 秒内最多 14 次起发。"""
    async with _get_lock:
        while True:
            now = time.monotonic()
            while _get_starts and now - _get_starts[0] >= 8.7:
                _get_starts.popleft()
            if len(_get_starts) < 14:
                _get_starts.append(now)
                break
            delay = max(0.001, _get_starts[0] + 8.7 - now)
            log.warning("[GET限流] 14次/8.7s 已满，等待 %.0fms", delay * 1000)
            await asyncio.sleep(delay)
    name = url.rsplit("/", 1)[-1]
    tag = f"{name} {court_name}" if court_name else name
    sent = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    start = time.monotonic()
    if not quiet:
        log.debug("[GET %s] → 发送 @ %s", tag, sent)
    try:
        r = await get_cffi_session().get(url, **kwargs)
        if not quiet:
            info = ""
            try:
                j = r.json()
                data = j.get("data") or {}
                if name == "getGroundDetailByParam":
                    info = f"maxDay={data.get('canOrderMaxDay')}"
                elif name != "getOrderTimeConfigList":
                    info = f"success={j.get('success')}"
                if j.get("success") is False:
                    info += f" code={j.get('code')} msg={str(j.get('msg') or '')[:60]}"
            except (ValueError, TypeError, AttributeError, KeyError):
                info = "non-json/字段异常"
            log.debug("[GET %s] ← 返回 @ %s (+%.0fms) HTTP %d %s",
                     tag, datetime.now().strftime("%H:%M:%S.%f")[:-3],
                     (time.monotonic() - start) * 1000, r.status_code, info)
        return r
    except Exception as e:
        log.warning("[GET %s] ✗ 异常 @ %s (+%.0fms): %s",
                    name, datetime.now().strftime("%H:%M:%S.%f")[:-3],
                    (time.monotonic() - start) * 1000, e)
        raise


# ══════════════════════════════════════════════════════
# 纯函数小工具
# ══════════════════════════════════════════════════════
def format_stamp(dt: datetime | None = None) -> str:
    """本地时间字符串，精确到毫秒。"""
    return (dt or datetime.now()).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


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
    """saveOrder 返回分类：ok / busy / uncertain / error"""
    if r.get("success"):
        return "ok", "✅ 预约成功"
    if r.get("uncertain"):
        return "uncertain", "⚠ 结果不明确，正在核对订单"
    msg = str(r.get("msg", "") or "")
    for kw in ("繁忙", "稍后", "系统忙", "请求过于", "频繁",
               "frequent", "too many", "try again", "busy"):
        if kw in msg.lower() or kw in msg:
            return "busy", "⚠ 系统繁忙"
    if not msg or msg == "non-json":
        return "error", "❌ 响应异常"
    return "error", "❌ 下单失败"


# ══════════════════════════════════════════════════════
# 验证码 ROTATE 求解（curl_cffi，默认 HTTP/2）
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

    def __init__(self, timeout: float = 2.0, impersonate: str = "chrome"):
        self.timeout = timeout
        self.impersonate = impersonate
        self.timing: dict = {}

    async def solve(self) -> str:
        t0 = time.perf_counter()
        session = curl_requests.AsyncSession(
            impersonate=self.impersonate,
            timeout=self.timeout,
        )
        try:
            cid, cap = await self._generate(session)
            t1 = time.perf_counter()

            start, mono = datetime.now(), time.monotonic()
            move_x = await asyncio.to_thread(self._recognize, cap)
            t2 = time.perf_counter()

            track = await asyncio.to_thread(self._make_track_sync, move_x, mono)
            t3 = time.perf_counter()

            stop = datetime.now()
            vid = await self._check(session, cid, track, start, stop)
            t4 = time.perf_counter()

            self.timing = {
                "imp":      self.impersonate,
                "gen_ms":   round((t1 - t0) * 1000, 1),
                "rec_ms":   round((t2 - t1) * 1000, 1),
                "trk_ms":   round((t3 - t2) * 1000, 1),
                "chk_ms":   round((t4 - t3) * 1000, 1),
                "total_ms": round((t4 - t0) * 1000, 1),
                "move_x":   move_x,
            }
            return vid
        finally:
            await session.close()

    async def _generate(self, session):
        r = await session.post(URL_GEN, json={"type": "ROTATE"},
                               timeout=self.timeout)
        r.raise_for_status()
        j = r.json()
        if not isinstance(j, dict):
            raise RuntimeError(f"验证码生成响应异常: {str(j)[:180]}")
        if not j.get("success", True):
            raise RuntimeError(f"验证码生成失败: {j.get('msg', j)}")
        if not isinstance(j.get("captcha"), dict):
            raise RuntimeError(
                f"验证码数据异常: {j.get('msg', '')} "
                f"captcha={str(j.get('captcha'))[:100]}")
        if not j.get("id"):
            raise RuntimeError(f"验证码缺少 id: {str(j)[:180]}")
        return j["id"], j["captcha"]

    async def _check(self, session, cid, track, start, stop):
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
        r = await session.post(URL_CHK, json=payload, timeout=self.timeout)
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


async def get_vid_once(timeout: float = 2.0,
                       impersonate: str = "chrome") -> tuple[str, dict]:
    """单次：generate → recognize → track → check，返回 (vid, timing)。"""
    solver = _CaptchaSolver(timeout, impersonate)
    vid = await solver.solve()
    return vid, solver.timing


# ══════════════════════════════════════════════════════
# 预约 API（走 cffi 全局 session，HTTP/2 + 连接复用）
# ══════════════════════════════════════════════════════
async def get_order_once(court: Court, date: str, token: str, user_id: str,
                         timeout: float = 4.0, *, quiet: bool = False) -> dict:
    """单次查询某个场地的时段配置；quiet 仅关闭常规 DEBUG 日志。"""
    try:
        r = await limited_get(URL_QUERY, court_name=court.name, quiet=quiet, params={
            "groundId": court.id, "startDate": date, "endDate": date,
            "userid": user_id, "token": token,
        }, timeout=timeout)
    except Exception as e:
        return {"ok": False, "auth_fail": False, "error": f"网络异常: {e}"}

    if r.status_code != 200:
        return {"ok": False, "auth_fail": False, "error": f"HTTP {r.status_code}"}

    try:
        j = r.json()
    except Exception:
        return {"ok": False, "auth_fail": False, "error": "non-json"}

    if not j.get("success"):
        msg = str(j.get("msg", "") or "")
        auth = is_auth_fail(j)
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

    sent_at = format_stamp()
    t0 = time.monotonic()
    log.info("→ 下单 %s 发送 @ %s (vid=%s…)", court.name, sent_at[11:], vid[:8])

    try:
        r = await get_cffi_session().post(
            URL_SAVE, params={"userid": user_id, "token": token},
            json=payload, timeout=6.0,
        )
        dt_ms = int((time.monotonic() - t0) * 1000)
        returned_at = format_stamp()
        log.info("← 下单 %s 返回 @ %s (+%dms): HTTP %d | %s",
                 court.name, returned_at[11:], dt_ms, r.status_code, r.text[:180])
        try:
            result = r.json()
        except Exception:
            result = {"success": False, "msg": "non-json", "uncertain": True}
    except Exception as e:
        dt_ms = int((time.monotonic() - t0) * 1000)
        returned_at = format_stamp()
        log.warning("✗ 下单 %s 异常 (+%dms): %s", court.name, dt_ms, e)
        result = {"success": False, "msg": str(e), "uncertain": True}

    if not isinstance(result, dict):
        result = {"success": False, "msg": "响应格式异常", "uncertain": True}
    result.setdefault("sent_at", sent_at)
    result.setdefault("returned_at", returned_at)
    result.setdefault("elapsed_ms", dt_ms)
    return result


# ══════════════════════════════════════════════════════
# Windows 窗口操作（refresh reservation 窗口）
#
# 唯一的跨调用缓存：预约窗口 HWND。每次使用前校验句柄与标题，
# 失效后重新枚举；其余状态仍只存在于单次调用内。
# ══════════════════════════════════════════════════════
_REFRESH_TITLE_PREFIXES = (RESERVATION_HOST, f"https://{RESERVATION_HOST}")
_refresh_hwnd: int | None = None

_NAV_TITLE = "场地预约"
_SEARCH_TITLE = "请输入搜索的场馆"


def refresh_window(fg_timeout: float = 0.6) -> bool:
    """进入场馆列表并刷新，随后最小化；True 表示已发送 Ctrl+R。

    各阶段耗时以 INFO 日志输出（毫秒），用于真实 Windows 环境验收。
    """
    global _refresh_hwnd
    com_initialized = False
    hwnd = 0
    stage = "初始化"
    started = time.monotonic()
    stage_started = started

    def mark(name: str) -> None:
        nonlocal stage_started
        now = time.monotonic()
        log.info("[refresh] %s耗时 %.1fms（累计 %.1fms）",
                 name, (now - stage_started) * 1000, (now - started) * 1000)
        stage_started = now

    try:
        import comtypes
        import win32api
        import win32con
        import win32gui
        from pywinauto import keyboard
        from pywinauto.uia_defines import NoPatternInterfaceError, get_elem_interface
        from pywinauto.uia_element_info import UIAElementInfo

        comtypes.CoInitializeEx()
        com_initialized = True

        def titled(window: int) -> bool:
            try:
                return win32gui.GetWindowText(window).startswith(_REFRESH_TITLE_PREFIXES)
            except Exception:
                return False

        stage = "窗口查找"
        if _refresh_hwnd and win32gui.IsWindow(_refresh_hwnd) and titled(_refresh_hwnd):
            hwnd = _refresh_hwnd
            log.info("[refresh] 复用缓存预约窗口 HWND=%d", hwnd)
        else:
            found: list[int] = []

            def collect(window: int, _param) -> bool:
                if titled(window):
                    found.append(window)
                return True

            win32gui.EnumWindows(collect, None)
            if len(found) != 1:
                log.warning("[refresh] Win32 匹配预约窗口数量=%d，需要唯一窗口", len(found))
                _refresh_hwnd = None
                return False
            hwnd = _refresh_hwnd = found[0]
            log.info("[refresh] 枚举到预约窗口 HWND=%d", hwnd)
        mark("窗口查找")

        stage = "恢复窗口"
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            deadline = time.monotonic() + 3.0
            while win32gui.IsIconic(hwnd):
                if time.monotonic() >= deadline:
                    log.warning("[refresh] 窗口恢复超时")
                    return False
                time.sleep(0.01)
            log.info("[refresh] 最小化窗口已恢复")
        mark("窗口恢复")

        def ensure_foreground(action: str) -> bool:
            if win32gui.GetForegroundWindow() == hwnd:
                return True
            log.info("[refresh] %s前窗口失焦: 目标 HWND=%d, 前台 HWND=%d，尝试置前",
                     action, hwnd, win32gui.GetForegroundWindow())
            deadline = time.monotonic() + max(0.0, float(fg_timeout))
            while True:
                try:
                    win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                    win32gui.SetForegroundWindow(hwnd)
                except Exception as exc:
                    log.debug("[refresh] 置前尝试失败: %r", exc)
                if win32gui.GetForegroundWindow() == hwnd:
                    return True
                if time.monotonic() >= deadline:
                    log.warning("[refresh] %s前置前失败: 目标 HWND=%d, 前台 HWND=%d",
                                action, hwnd, win32gui.GetForegroundWindow())
                    return False
                time.sleep(0.01)

        stage = "置前窗口"
        if not ensure_foreground("点击"):
            return False
        mark("窗口聚焦")

        stage = "定位控件"
        window_el = UIAElementInfo(hwnd)

        def descendants(title: str, control_type: str):
            # 条件由 UIA 服务端执行，只返回命中的节点，不做 Python 层全量遍历。
            return window_el.descendants(title=title, control_type=control_type)

        def navigation():
            targets = []
            for el in descendants(_NAV_TITLE, "Text"):
                try:
                    if not el.enabled or not el.visible:
                        continue
                    get_elem_interface(el.element, "Invoke")
                except NoPatternInterfaceError:
                    continue
                targets.append(el)
            return targets

        def on_gym_page() -> bool:
            return bool(descendants(_SEARCH_TITLE, "Edit")
                        and descendants(_NAV_TITLE, "Text"))

        deadline = time.monotonic() + 5.0
        while True:
            targets = navigation()
            if len(targets) == 1:
                target = targets[0]
                break
            if time.monotonic() >= deadline:
                log.warning("[refresh] 导航控件数量=%d，无法唯一定位", len(targets))
                return False
            time.sleep(0.02)

        rect = target.rectangle
        if rect.right <= rect.left or rect.bottom <= rect.top:
            log.warning("[refresh] 导航控件坐标无效: (%d,%d)-(%d,%d)",
                        rect.left, rect.top, rect.right, rect.bottom)
            return False
        click_x = (rect.left + rect.right) // 2
        click_y = (rect.top + rect.bottom) // 2
        mark("控件定位")

        # 即使已在场馆列表，也点击导航，让网页获得键盘焦点。
        stage = "点击场地预约"
        if win32gui.GetForegroundWindow() != hwnd:
            log.warning("[refresh] 点击前预约窗口失焦，取消点击")
            return False
        win32api.SetCursorPos((click_x, click_y))
        if win32api.GetCursorPos() != (click_x, click_y):
            win32api.SetCursorPos((click_x, click_y))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        log.info("[refresh] 已点击「场地预约」导航控件 (%d,%d)", click_x, click_y)
        mark("点击")

        stage = "确认场馆列表"
        deadline = time.monotonic() + 5.0
        while not on_gym_page():
            if time.monotonic() >= deadline:
                log.warning("[refresh] 未返回场馆列表，取消刷新")
                return False
            time.sleep(0.02)
        mark("页面确认")

        stage = "发送 Ctrl+R"
        # 只有发生失焦、需要重新激活时，才重新读取网页控件。
        if win32gui.GetForegroundWindow() != hwnd:
            if not ensure_foreground("刷新"):
                return False
            if not on_gym_page():
                log.warning("[refresh] 刷新前场馆列表已变化")
                return False
        if win32gui.GetForegroundWindow() != hwnd:
            log.warning("[refresh] 发送 Ctrl+R 前再次失焦，取消发送")
            return False
        keyboard.send_keys("^r", pause=0.02, turn_off_numlock=False, vk_packet=False)
        log.info("[refresh] 已发送 Ctrl+R")
        mark("发送快捷键")
        return True
    except Exception as exc:
        log.exception("[refresh] %s失败: %s: %r", stage, type(exc).__name__, exc)
        return False
    finally:
        if hwnd:
            try:
                if not win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                    log.info("[refresh] 预约窗口已最小化")
            except Exception as exc:
                log.warning("[refresh] 预约窗口最小化失败: %r", exc)
        log.info("[refresh] 窗口操作总耗时 %.1fms（最后阶段=%s）",
                 (time.monotonic() - started) * 1000, stage)
        if com_initialized:
            comtypes.CoUninitialize()


def capture_token_from_flow(
    flow,
    host: str = RESERVATION_HOST,
    config_path: Path = CONFIG_PATH,
) -> None:
    """初始化专用：配置还没有凭证时，取业务请求携带的当前凭证。"""
    try:
        if flow.request.pretty_host != host:
            return
        token = flow.request.query.get("token")
        if not token:
            return

        text = config_path.read_text(encoding="utf-8")
        m = re.search(r'(?m)^\s*token\s*=\s*"([^"]*)"', text)
        if m and m.group(1):
            return

        new, n = re.subn(
            r'(?m)^(\s*token\s*=\s*)"[^"]*"',
            rf'\1"{token}"',
            text, count=1,
        )
        if n:
            config_path.write_text(new, encoding="utf-8")
            print(f"[{time.strftime('%H:%M:%S')}] [TOKEN] 初始凭证 {token[:8]}...")
    except Exception as e:
        print(f"[TOKEN] 写入失败: {e}")


def kill_port_holder(host: str, port: int) -> list[int]:
    """强杀所有监听 host:port 的进程，返回被杀的 PID 列表。"""
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


_KEEP_RE = re.compile(
    r"\[TOKEN\]"
    r"|listening at|Proxy server"
    r"|error|Error|ERROR"
    r"|warn|Warn|WARNING|WARN"
    r"|Traceback|Exception"
)

_DROP_RE = re.compile(
    r"TLS handshake failed"
    r"|Client TLS"
    r"|unknown ca"
    r"|does not trust the proxy"
)


def _decode_mitm(raw: bytes) -> str:
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
            if _DROP_RE.search(txt):
                continue
            if _KEEP_RE.search(txt):
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