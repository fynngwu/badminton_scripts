"""app.py — FastAPI 壳：只处理 HTTP / SSE。

    所有真正的业务逻辑都在 engine.py 里。本文件不应该知道
    CAPTCHA、HTTP payload、Windows 窗口、VID 过期策略等任何细节。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from engine import engine, get_logs
from utils import ROOT


# ══════════════════════════════════════════════════════
# lifespan
# ══════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app):
    logging.getLogger("court").info("[生命周期] FastAPI 启动 PID=%s, PPID=%s", os.getpid(), os.getppid())
    await engine.start()
    try:
        yield
    finally:
        logging.getLogger("court").warning("[生命周期] FastAPI lifespan 收到关闭流程，正在清理任务")
        await engine.stop()


app = FastAPI(title="Court Reserver", lifespan=lifespan)


# ══════════════════════════════════════════════════════
# 静态页面
# ══════════════════════════════════════════════════════
@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")


# ══════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════
@app.get("/api/config")
def get_config():
    return engine.public_config()


class ConfigIn(BaseModel):
    offset_days: int | None = None
    token: str | None = None


@app.post("/api/config")
async def set_config(d: ConfigIn):
    try:
        if d.offset_days is not None:
            engine.update_offset_days(d.offset_days)
        if d.token:
            engine.update_token(d.token)
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    await engine.apply_config_change()
    return {"ok": True, "target_date": engine.target_date}


class AutoConfigIn(BaseModel):
    slot_start: str
    slot_end: str
    refresh_at: str
    order_time_gate: bool = False


@app.post("/api/auto/config")
async def set_auto_config(d: AutoConfigIn):
    try:
        engine.update_auto_config(d.slot_start, d.slot_end, d.refresh_at, d.order_time_gate)
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    await engine.apply_config_change()
    return {"ok": True}


# ══════════════════════════════════════════════════════
# Token / mitm
# ══════════════════════════════════════════════════════
class TokenFetchIn(BaseModel):
    timeout: float | None = None


@app.post("/api/token/fetch")
async def token_fetch(d: TokenFetchIn):
    return await engine.fetch_token()


@app.get("/api/mitm/state")
def mitm_state_ep():
    return engine.mitm_state()


@app.post("/api/mitm/restart")
async def mitm_restart_ep():
    ok = await engine.restart_mitm()
    return {"ok": ok}


# ══════════════════════════════════════════════════════
# 日志
# ══════════════════════════════════════════════════════
@app.get("/api/logs")
def logs_ep(since: int = 0):
    return {"logs": get_logs(since)}


# ══════════════════════════════════════════════════════
# VID 池
# ══════════════════════════════════════════════════════
@app.post("/api/vid/start")
async def vid_start():
    return await engine.start_vid_pool()


@app.post("/api/vid/stop")
async def vid_stop():
    await engine.stop_vid_pool()
    return {"ok": True}


@app.get("/api/vid/state")
def vid_state():
    return engine.vid_state()


# ══════════════════════════════════════════════════════
# 刷新
# ══════════════════════════════════════════════════════
@app.post("/api/refresh")
async def refresh_ep():
    return await engine.start_refresh()


@app.get("/api/refresh/seq")
def refresh_seq():
    return {"seq": engine.bus.seq}


# ══════════════════════════════════════════════════════
# 手动预约
# ══════════════════════════════════════════════════════
class ReserveIn(BaseModel):
    court_no: int
    start: str
    end: str


@app.post("/api/reserve")
async def reserve_ep(d: ReserveIn):
    return await engine.reserve(d.court_no, d.start, d.end)


# ══════════════════════════════════════════════════════
# 全自动
# ══════════════════════════════════════════════════════
@app.post("/api/auto/start")
async def auto_start_ep():
    return await engine.start_auto()


@app.post("/api/auto/stop")
async def auto_stop_ep():
    await engine.stop_auto()
    return {"ok": True}


@app.get("/api/auto/state")
def auto_state_ep():
    return engine.auto_state()


# ══════════════════════════════════════════════════════
# SSE
# ══════════════════════════════════════════════════════
@app.get("/api/stream")
async def stream_ep(since: int = 0):
    q, hist = engine.bus.subscribe(since)

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
                yield (f"event: {e['_topic']}\n"
                       f"data: {json.dumps(e, ensure_ascii=False)}\n\n")
        except asyncio.CancelledError:
            pass
        finally:
            engine.bus.unsubscribe(q)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    class SignalLoggingServer(uvicorn.Server):
        """使用 python app.py 启动时，记录导致 Uvicorn 退出的 OS 信号。"""
        def handle_exit(self, sig, frame):
            try:
                name = signal.Signals(sig).name
            except ValueError:
                name = str(sig)
            logging.getLogger("court").warning(
                "[生命周期] Uvicorn 收到退出信号 %s (%s), PID=%s",
                name, sig, os.getpid(),
            )
            return super().handle_exit(sig, frame)

    SignalLoggingServer(uvicorn.Config(
        app, host="0.0.0.0", port=8000, log_level="info",
    )).run()
