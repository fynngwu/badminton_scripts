"""app.py — 羽毛球预约后端（配置 + 业务 + 路由 三合一）

启动：
    python app.py
    # 或
    uvicorn app:app --host 0.0.0.0 --port 8000

架构：
    企业微信 → Clash（单域名规则） → mitmdump:8080 → reservation 服务器
                                   ↑
                             只收 reservation.sustech.edu.cn

    mitmdump 在服务启动时【常驻】拉起（无窗口），程序退出时自动回收。
    “自动获取 Token”按钮只做两件事：刷新预约窗口 + 等 config.toml 里 token 变化。

环境变量：
    MITM_UPSTREAM   可选。例如 "http://127.0.0.1:7897"。设置后 mitmdump 走
                    --mode upstream:<URL>，由这个上游再出去（注意：上游不能再把
                    reservation 路由回 8080，否则会形成环路）。
                    不设置 = mitmdump 直连服务器。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

import cv2
import httpx
import numpy as np
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image
from pydantic import BaseModel

try:
    from mitm_addon import refresh_reservation_window
except Exception:
    def refresh_reservation_window() -> bool:  # type: ignore
        log.warning("mitm_addon 未就绪，无法刷新窗口")
        return False


# ══════════════════════════════════════════════════════
# 路径 / 常量
# ══════════════════════════════════════════════════════
ROOT        = Path(__file__).parent
CONFIG_PATH = ROOT / "config.toml"
INDEX_PATH  = ROOT / "index.html"

BASE_URL  = "https://reservation.sustech.edu.cn"
URL_GEN   = BASE_URL + "/api/blade-base/captcha/generate/d"
URL_CHK   = BASE_URL + "/api/blade-base/captcha/check/d"
URL_SAVE  = BASE_URL + "/api/blade-app/qywx/saveOrder"
URL_QUERY = BASE_URL + "/api/blade-app/qywx/getOrderTimeConfigList"

VID_TIMEOUT = 3.5
VID_MAX_AGE = 8.0

# ── mitmdump 常驻参数 ──
MITM_HOST = "127.0.0.1"
MITM_PORT = 8080
MITM_STARTUP_TIMEOUT = 12.0   # 等 mitmdump 监听就绪的上限（首次会生成证书，稍慢）
MITM_SHUTDOWN_GRACE  = 3.0    # terminate 后等它自己退出的宽限
MITM_UPSTREAM        = os.environ.get("MITM_UPSTREAM", "").strip()

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
# 配置：config.toml 加载 / 监听 / 原地写入
# ══════════════════════════════════════════════════════
class ConfigStore:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self.data: dict[str, Any] = {}
        self._mtime: float = 0.0
        self.load(force=True)

    def load(self, force: bool = False) -> bool:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return False
        if not force and mtime == self._mtime:
            return False
        with self.path.open("rb") as f:
            self.data = tomllib.load(f)
        self._mtime = mtime
        return True

    @property
    def mtime(self) -> float:
        return self._mtime

    @property
    def user(self)  -> dict: return self.data.get("user", {})
    @property
    def gym(self)   -> dict: return self.data.get("gym", {})
    @property
    def order(self) -> dict: return self.data.get("order", {})

    @property
    def courts_map(self) -> dict[int, str]:
        return {int(k): str(v) for k, v in self.data.get("courts", {}).items()}

    def set_string(self, key: str, value: str) -> None:
        text = self.path.read_text(encoding="utf-8")
        pattern = rf'(?m)^(\s*{re.escape(key)}\s*=\s*)"[^"]*"'
        new_text, n = re.subn(pattern, rf'\1"{value}"', text, count=1)
        if n == 0:
            raise RuntimeError(f"config.toml 未找到字符串字段 {key!r}")
        self.path.write_text(new_text, encoding="utf-8")

    def set_int(self, key: str, value: int) -> None:
        text = self.path.read_text(encoding="utf-8")
        pattern = rf'(?m)^(\s*{re.escape(key)}\s*=\s*)-?\d+'
        new_text, n = re.subn(pattern, rf'\g<1>{int(value)}', text, count=1)
        if n == 0:
            raise RuntimeError(f"config.toml 未找到整数字段 {key!r}")
        self.path.write_text(new_text, encoding="utf-8")

    def public_view(self) -> dict:
        tok = str(self.order.get("token", "") or "")
        return {
            "user": self.user,
            "gym":  self.gym,
            "order": {
                "offset_days": int(self.order.get("offset_days", 1) or 1),
                "token_set": bool(tok),
                "token_preview": (tok[:8] + "...") if tok else "",
                "target_date": session.target_date,
            },
            "courts": {str(k): v for k, v in self.courts_map.items()},
        }


config_store = ConfigStore()


USER_ID       = ""
CUSTOMER_ID   = ""
CUSTOMER_NAME = ""
CUSTOMER_TEL  = ""
GYM_ID        = ""
GYM_NAME      = ""
COURTS: list[Court] = []
COURT_BY_NO: dict[int, Court] = {}


@dataclass
class Session:
    token: str = ""
    offset_days: int = 1

    @property
    def target_date(self) -> str:
        d = datetime.now() + timedelta(days=self.offset_days)
        return d.strftime("%Y-%m-%d")


session = Session()


def _reload_config():
    global USER_ID, CUSTOMER_ID, CUSTOMER_NAME, CUSTOMER_TEL
    global GYM_ID, GYM_NAME, COURTS, COURT_BY_NO

    u, g, o = config_store.user, config_store.gym, config_store.order
    USER_ID       = str(u.get("id", "") or "")
    CUSTOMER_ID   = str(u.get("customer_id", "") or "")
    CUSTOMER_NAME = str(u.get("name", "") or "")
    CUSTOMER_TEL  = str(u.get("tel", "") or "")
    GYM_ID        = str(g.get("id", "") or "")
    GYM_NAME      = str(g.get("name", "") or "")

    COURTS = [Court(no, cid, f"{no}号场")
              for no, cid in sorted(config_store.courts_map.items())]
    COURT_BY_NO = {c.no: c for c in COURTS}

    session.token = str(o.get("token", "") or "")
    try:
        session.offset_days = int(o.get("offset_days", 1))
    except Exception:
        session.offset_days = 1
    session.offset_days = max(0, min(2, session.offset_days))


_reload_config()


# ══════════════════════════════════════════════════════
# 日志
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


log = logging.getLogger("court")
log.setLevel(logging.INFO)
log.addHandler(_BufHandler())
log.addHandler(logging.StreamHandler())


def get_logs(since: int = 0):
    return [e for e in _log_buf if e["seq"] > since]


# ══════════════════════════════════════════════════════
# 业务 HTTP 客户端（直连，忽略系统代理）
# ══════════════════════════════════════════════════════
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            headers=HEADERS,
            timeout=6.0,
            trust_env=False,   # ★ 忽略 HTTP(S)_PROXY / NO_PROXY
        )
    return _client


def _port_listening(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ══════════════════════════════════════════════════════
# mitmdump 常驻管理
# ══════════════════════════════════════════════════════
_mitm_proc: subprocess.Popen | None = None
_mitm_owned: bool = False          # 是否由本进程启动（决定退出时要不要 kill）
_mitm_lock: asyncio.Lock | None = None


def _mitm_lock_get() -> asyncio.Lock:
    global _mitm_lock
    if _mitm_lock is None:
        _mitm_lock = asyncio.Lock()
    return _mitm_lock


# mitmdump 会刷很多请求流水，只把这些行转发到前端
_KEEP_RE = re.compile(
    r"\[TOKEN\]"
    r"|listening at|Proxy server"
    r"|error|Error|ERROR"
    r"|warn|Warn|WARNING|WARN"
    r"|certificate|Cert"
    r"|Traceback|Exception"
)


def _mitm_reader(pipe) -> None:
    """后台线程：逐行读 mitmdump 输出，只转发关键行到日志面板。"""
    try:
        for raw in iter(pipe.readline, b""):
            if not raw:
                break
            try:
                txt = raw.decode("utf-8", errors="replace").rstrip()
            except Exception:
                txt = repr(raw)
            if txt and _KEEP_RE.search(txt):
                log.info("[mitm] %s", txt)
    except Exception as e:
        log.warning("[mitm] reader 异常: %s", e)
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _build_mitm_cmd() -> list[str] | None:
    """构造 mitmdump 命令。

    ★ 优先用与当前 Python 解释器【同环境】的 mitmdump，
      避免 PATH 里捞到全局 Python 装的另一只 mitmdump（证书/依赖可能不同）。
    """
    script = str(ROOT / "mitm_addon.py")
    args: list[str] = [
        "--listen-host", MITM_HOST,
        "--listen-port", str(MITM_PORT),
        "-s", script,
    ]
    if MITM_UPSTREAM:
        args += ["--mode", f"upstream:{MITM_UPSTREAM}"]

    exe_name = "mitmdump.exe" if sys.platform == "win32" else "mitmdump"

    # 1) venv / 当前解释器同目录
    local = Path(sys.executable).with_name(exe_name)
    if local.exists():
        return [str(local)] + args

    # 2) PATH
    exe = shutil.which("mitmdump")
    if exe:
        return [exe] + args

    # 3) python -m 回退
    try:
        import mitmproxy  # noqa: F401
    except Exception:
        return None
    return [sys.executable, "-m", "mitmproxy.tools.main", "dump"] + args


def _spawn_mitmdump() -> subprocess.Popen | None:
    cmd = _build_mitm_cmd()
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
            cmd,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            bufsize=0,
        )
    except FileNotFoundError as e:
        log.error("[mitm] 可执行文件不存在: %s", e)
        return None
    except Exception as e:
        log.error("[mitm] 启动失败: %s", e)
        return None

    threading.Thread(
        target=_mitm_reader, args=(proc.stdout,), daemon=True,
    ).start()
    return proc


async def _ensure_mitmdump(verbose_if_reuse: bool = False) -> bool:
    """确保 8080 有 mitmdump 在跑。返回 True = 已就绪。

    逻辑：
      - 端口已被监听 且 我们没启动过 → 复用外部 mitm（不动它）
      - 我们自己启动过但进程死了 → 重启
      - 端口空闲 → 启动
    """
    global _mitm_proc, _mitm_owned

    async with _mitm_lock_get():
        # 之前启的 mitmdump 挂了？
        if _mitm_proc is not None and _mitm_proc.poll() is not None:
            log.warning("[mitm] 之前的 mitmdump 已退出 (code=%s)",
                        _mitm_proc.returncode)
            _mitm_proc = None
            _mitm_owned = False

        # 端口有人在监听 → 复用
        if _port_listening(MITM_HOST, MITM_PORT):
            if _mitm_proc is None and verbose_if_reuse:
                log.info("[mitm] 检测到 %s:%d 已被监听，直接复用（外部启动）",
                         MITM_HOST, MITM_PORT)
            return True

        # 需要一个新进程
        if _mitm_proc is None or _mitm_proc.poll() is not None:
            log.info("[mitm] 正在拉起 mitmdump（%s:%d，无窗口）…",
                     MITM_HOST, MITM_PORT)
            proc = await asyncio.to_thread(_spawn_mitmdump)
            if proc is None:
                return False
            _mitm_proc = proc
            _mitm_owned = True

        # 等端口就绪
        deadline = time.monotonic() + MITM_STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if _mitm_proc.poll() is not None:
                log.error("[mitm] mitmdump 提前退出 (code=%s)，"
                          "可能端口被占用或证书异常", _mitm_proc.returncode)
                _mitm_proc = None
                _mitm_owned = False
                return False
            if _port_listening(MITM_HOST, MITM_PORT):
                log.info("[mitm] ✅ mitmproxy 已就绪：%s:%d",
                         MITM_HOST, MITM_PORT)
                return True
            await asyncio.sleep(0.2)

        log.error("[mitm] %.0fs 内未监听 %d，放弃",
                  MITM_STARTUP_TIMEOUT, MITM_PORT)
        return False


def _kill_mitmdump() -> None:
    """只在“本进程启动过”的情况下关闭 mitmdump。"""
    global _mitm_proc, _mitm_owned
    if not _mitm_owned or _mitm_proc is None:
        return
    proc = _mitm_proc
    _mitm_proc = None
    _mitm_owned = False

    if proc.poll() is not None:
        return

    log.info("[mitm] 关闭 mitmdump（本进程启动）")
    try:
        proc.terminate()
        try:
            proc.wait(timeout=MITM_SHUTDOWN_GRACE)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
    except Exception as e:
        log.warning("[mitm] 关闭进程异常: %s", e)


async def _boot_mitm() -> None:
    try:
        ok = await _ensure_mitmdump(verbose_if_reuse=True)
        if not ok:
            log.warning("⚠ mitmdump 启动失败；"
                        "点击“自动获取 Token”时会重试，或检查 8080 端口占用")
    except Exception as e:
        log.warning("mitmdump 预启动异常: %s", e)


# ══════════════════════════════════════════════════════
# 验证码 ROTATE 求解
# ══════════════════════════════════════════════════════
_DB = np.load(ROOT / "fast_db.npz", allow_pickle=False)
_BG_DESCS    = _DB["bg_descs"]
_REF_FFTS    = _DB["ref_ffts"]
_ANGLE_BINS  = int(_DB["angle_bins"][0])
_RADIAL_BINS = int(_DB["radial_bins"][0])
_DIR_SIGN    = int(_DB["direction_sign"][0])


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


# ══════════════════════════════════════════════════════
# VID 池
# ══════════════════════════════════════════════════════
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
        self._tasks = [asyncio.create_task(self._worker(i + 1, i * stagger))
                       for i in range(self.size)]
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


# ══════════════════════════════════════════════════════
# 场地查询 / 下单
# ══════════════════════════════════════════════════════
def _norm_hm(t: str) -> str:
    if not t or ":" not in t:
        return t
    h, m = t.split(":", 1)
    return f"{h.zfill(2)}:{m}" if h.isdigit() else t


async def query_court(court: Court, date: str, token: str,
                      timeout: float = 4.0) -> dict | None:
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

    avail: dict[str, str] = {}
    blocks: list[dict] = []
    for cfg in j.get("data", {}).get("configList", []):
        if cfg.get("date") != date:
            continue
        for blk in cfg.get("timeBlockList", []):
            t = _norm_hm(blk.get("time", ""))
            e = _norm_hm(blk.get("endTime", ""))
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
    return {"avail": avail, "blocks": blocks}


def slot_covers(avail: dict, start: str, end: str) -> bool:
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


# ══════════════════════════════════════════════════════
# 事件总线
# ══════════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════════
# 配置监听
# ══════════════════════════════════════════════════════
async def watch_config(poll: float = 1.0):
    log.info("配置监听已启动: %s", config_store.path)
    while True:
        try:
            if config_store.load():
                _reload_config()
                log.info("检测到 config.toml 变化: date=%s token=%s courts=%d",
                         session.target_date,
                         (session.token[:8] + "...") if session.token else "空",
                         len(COURTS))
                await bus.publish("config.updated", config_store.public_view())
        except Exception as e:
            log.warning("配置监听异常: %s", e)
        await asyncio.sleep(poll)


# ══════════════════════════════════════════════════════
# 刷新服务
# ══════════════════════════════════════════════════════
class RefreshService:
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

    async def _run(self, date, token, pool):
        await self._probe_then_fanout(date, token, pool)
        await bus.publish("refresh.done", {
            "ts": datetime.now().strftime("%H:%M:%S"),
        })

    async def _probe_then_fanout(self, date, token, pool):
        probe = pool[0]
        for attempt in range(1, self.PROBE_MAX_TRIES + 1):
            t0 = time.monotonic()
            res = await query_court(probe, date, token)
            dt = (time.monotonic() - t0) * 1000

            if res is not None:
                log.info("探测 %s 成功 (%.0fms, 第 %d 次)，开始并发其余 %d 个场地",
                         probe.name, dt, attempt, len(pool) - 1)
                await bus.publish("refresh.result", {
                    "court": probe.no, "name": probe.name, "ok": True,
                    "avail":  res["avail"], "blocks": res["blocks"],
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

        self._tasks = [asyncio.create_task(self._one(c, date, token))
                       for c in pool[1:]]
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _one(self, court: Court, date: str, token: str):
        t0 = time.monotonic()
        res = await query_court(court, date, token)
        dt = (time.monotonic() - t0) * 1000
        if res is not None:
            log.info("%s OK (%.0fms)", court.name, dt)
            await bus.publish("refresh.result", {
                "court": court.no, "name": court.name, "ok": True,
                "avail":  res["avail"], "blocks": res["blocks"],
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


# ══════════════════════════════════════════════════════
# 自动模式
# ══════════════════════════════════════════════════════
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
        for c in list(candidates):
            if has_success():
                return
            if len(applied) >= max_orders:
                return
            if any(a["court"] == c.no for a in applied):
                continue

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
                return

    try:
        refresh_dt = _parse_hms(refresh_at)
        submit_dt  = _parse_hms(submit_at)

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

        auto_state.phase = "waiting"
        auto_state.msg = f"等待 {refresh_at}"
        log.info("自动模式: 等待触发 %s", refresh_at)
        await _sleep_until(refresh_dt)

        auto_state.phase = "refreshing"
        auto_state.msg = "刷新中…"
        log.info("自动模式: 触发刷新")
        since = bus.seq
        await refresh_service.start(date, token)
        q, hist = bus.subscribe(since)

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

        auto_state.phase = "applying"
        auto_state.msg = f"申请中（候选 {len(candidates)}，上限 {max_orders}）"
        log.info("自动模式: 到达申请时间，候选 %d，最多尝试 %d",
                 len(candidates), max_orders)
        await apply_pending()

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
                        await apply_pending()
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

        if has_success() and vid_pool.running:
            log.info("自动模式: 申请成功，停止 VID 池")
            await vid_pool.stop()

        auto_state.running = False
        auto_state.phase = "done"
        auto_state.msg = (f"结束：{'✅ 成功' if has_success() else '❌ 未成功'}"
                          f"（尝试 {len(applied)} 个）")
        auto_state.finished_at = datetime.now().strftime("%H:%M:%S")


# ══════════════════════════════════════════════════════
# FastAPI
# ══════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app):
    log.info("服务已启动")
    # ★ 启动时后台常驻拉起 mitmdump（不阻塞 web 服务）
    boot_task = asyncio.create_task(_boot_mitm())
    watch_task = asyncio.create_task(watch_config())
    try:
        yield
    finally:
        for t in (watch_task, boot_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await vid_pool.stop()
        await refresh_service.stop()
        await stop_auto()
        _kill_mitmdump()


app = FastAPI(title="Court Reserver", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(INDEX_PATH)


@app.get("/api/config")
def get_config():
    return config_store.public_view()


class ConfigIn(BaseModel):
    offset_days: int | None = None
    token: str | None = None


@app.post("/api/config")
async def set_config(d: ConfigIn):
    try:
        if d.offset_days is not None:
            config_store.set_int("offset_days", max(0, min(2, int(d.offset_days))))
        if d.token:
            config_store.set_string("token", d.token.strip())
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    if config_store.load():
        _reload_config()
        log.info("配置已更新（前端保存）: offset=%d date=%s",
                 session.offset_days, session.target_date)
        await bus.publish("config.updated", config_store.public_view())
    return {"ok": True, "target_date": session.target_date}


# ──────────────────────────────────────────────────────
# 一键抓 token：确保 mitm 就绪 → 刷新窗口 → 等 token 变化
# ──────────────────────────────────────────────────────
class TokenFetchIn(BaseModel):
    timeout: float = 20.0


_token_fetch_busy = False


@app.post("/api/token/fetch")
async def token_fetch(d: TokenFetchIn):
    global _token_fetch_busy
    if _token_fetch_busy:
        return {"ok": False, "msg": "已有一次抓取正在进行，请稍候"}
    _token_fetch_busy = True
    try:
        return await _token_fetch_impl(d)
    finally:
        _token_fetch_busy = False


async def _token_fetch_impl(d: TokenFetchIn):
    old_token = session.token
    log.info("🔑 开始获取 Token")

    # 1) 确保 mitmdump 常驻在跑（如果之前崩了会自动重启）
    ok = await _ensure_mitmdump(verbose_if_reuse=True)
    if not ok:
        log.error("❌ mitmdump 未就绪，无法抓取 token")
        return {"ok": False,
                "msg": "mitmdump 未就绪；请检查 pip install mitmproxy、"
                       "8080 端口是否被占用，或看日志 [mitm] 行"}

    # 2) 记录 config.toml 当前 mtime
    try:
        old_mtime = config_store.path.stat().st_mtime
    except FileNotFoundError:
        old_mtime = 0.0

    # 3) 触发预约窗口 Ctrl+R
    log.info("🪟 正在触发预约窗口刷新（Ctrl+R）…")
    ok = await asyncio.to_thread(refresh_reservation_window)
    if not ok:
        log.warning("❌ 未找到预约窗口（企业微信是否已打开 reservation？）")
        return {"ok": False,
                "msg": "未找到预约窗口（企业微信是否已打开 reservation？）"}
    log.info("✓ 已发送 Ctrl+R，等待 mitmproxy 捕获新 token…")

    # 4) 轮询 config.toml 变更
    deadline = time.monotonic() + max(3.0, float(d.timeout))
    while time.monotonic() < deadline:
        await asyncio.sleep(0.3)
        try:
            mtime = config_store.path.stat().st_mtime
        except FileNotFoundError:
            continue
        if mtime == old_mtime:
            continue

        config_store.load(force=True)
        _reload_config()
        old_mtime = mtime

        if not session.token:
            log.warning("config.toml 已更新但 token 仍为空，继续等待…")
            continue

        await bus.publish("config.updated", config_store.public_view())
        preview = session.token[:8] + "..."
        if session.token != old_token:
            log.info("✅ Token 获取成功：%s（已更新）", preview)
        else:
            log.info("✅ Token 获取成功：%s（与之前相同）", preview)
        return {"ok": True, "token_preview": preview}

    log.warning("❌ 超时：%.0fs 内未捕获到新 token", d.timeout)
    return {"ok": False,
            "msg": f"{d.timeout:.0f}s 内未捕获到新 token；"
                   f"请确认企业微信已打开 reservation 页面、mitm 证书已信任、"
                   f"Clash 已把 reservation 送到 {MITM_HOST}:{MITM_PORT}"}


@app.get("/api/mitm/state")
def mitm_state_ep():
    proc = _mitm_proc
    return {
        "host": MITM_HOST,
        "port": MITM_PORT,
        "port_listening": _port_listening(MITM_HOST, MITM_PORT, 0.2),
        "proc_alive": proc is not None and proc.poll() is None,
        "owned_by_us": _mitm_owned,
        "upstream": MITM_UPSTREAM or None,
    }


@app.post("/api/mitm/restart")
async def mitm_restart_ep():
    """手动重启 mitmdump（仅会 kill 自己启的那个；外部监听的不会被碰）。"""
    _kill_mitmdump()
    await asyncio.sleep(0.3)
    ok = await _ensure_mitmdump(verbose_if_reuse=True)
    return {"ok": ok}


@app.get("/api/logs")
def logs_ep(since: int = 0):
    return {"logs": get_logs(since)}


@app.post("/api/vid/start")
async def vid_start():
    started = await vid_pool.start()
    return {"ok": True, "started": started, "count": vid_pool.count()}


@app.post("/api/vid/stop")
async def vid_stop():
    await vid_pool.stop()
    return {"ok": True}


@app.get("/api/vid/state")
def vid_state():
    return {"running": vid_pool.running, "count": vid_pool.count(),
            "stats": list(vid_pool.stats)[-3:]}


@app.post("/api/refresh")
async def refresh_ep():
    if not session.token:
        return {"ok": False, "msg": "未配置 token"}
    n = await refresh_service.start(session.target_date, session.token)
    return {"ok": True, "total": n}


@app.get("/api/refresh/seq")
def refresh_seq():
    return {"seq": bus.seq}


class ReserveIn(BaseModel):
    court_no: int
    start: str
    end: str


@app.post("/api/reserve")
async def reserve_ep(d: ReserveIn):
    if not session.token:
        return {"ok": False, "msg": "未配置 token"}
    court = COURT_BY_NO.get(d.court_no)
    if not court:
        return {"ok": False, "msg": f"非法场地号 {d.court_no}"}
    vid = await vid_pool.take(timeout=5.0)
    if not vid:
        return {"ok": False, "msg": "VID 队列为空"}
    result = await submit_order(
        vid, court, session.target_date, d.start, d.end, session.token,
    )
    return {"ok": bool(result.get("success")), "msg": result.get("msg", "")}


class AutoIn(BaseModel):
    slot_start: str = "20:00"
    slot_end:   str = "22:00"
    refresh_at: str = "20:00:01"
    submit_at:  str = "20:00:04"
    max_orders: int = 3
    idle_exit:  float = 10.0


@app.post("/api/auto/start")
async def auto_start_ep(d: AutoIn):
    if not session.token:
        return {"ok": False, "msg": "未配置 token"}
    return await start_auto(
        date=session.target_date, token=session.token,
        slot_start=d.slot_start, slot_end=d.slot_end,
        refresh_at=d.refresh_at, submit_at=d.submit_at,
        max_orders=int(d.max_orders), idle_exit=float(d.idle_exit),
    )


@app.post("/api/auto/stop")
async def auto_stop_ep():
    await stop_auto()
    return {"ok": True}


@app.get("/api/auto/state")
def auto_state_ep():
    s = auto_state
    return {
        "running": s.running, "phase": s.phase, "msg": s.msg,
        "candidates": s.candidates, "applied": s.applied,
        "started_at": s.started_at, "finished_at": s.finished_at,
        "plan": s.plan,
    }


@app.get("/api/stream")
async def stream_ep(since: int = 0):
    q, hist = bus.subscribe(since)

    async def gen():
        last = since
        try:
            for e in hist:
                yield f"event: {e['_topic']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
                last = e["_seq"]
            while True:
                try:
                    e = await asyncio.wait_for(q.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if e["_seq"] <= last:
                    continue
                last = e["_seq"]
                yield f"event: {e['_topic']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")