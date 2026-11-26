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
from datetime import date as Date

from engine import engine
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
    return FileResponse(ROOT / "index.html", headers={"Cache-Control": "no-store"})


# ══════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════
@app.get("/api/state")
async def get_state():
    return engine.snapshot()


class ConfigIn(BaseModel):
    offset_days: int | None = None
    token: str | None = None
    slot_start: str | None = None
    slot_end: str | None = None
    save_order_at: str | None = None
    court_no: int | None = None


@app.patch("/api/config")
async def set_config(d: ConfigIn):
    try:
        if d.offset_days is not None:
            engine.update_offset_days(d.offset_days)
        if d.token:
            engine.update_token(d.token)
        if any(v is not None for v in (d.slot_start, d.slot_end, d.save_order_at, d.court_no)):
            a = engine.config.auto
            await engine.update_auto_config(d.slot_start or a.slot_start,
                d.slot_end or a.slot_end, d.save_order_at or a.save_order_at, d.court_no)
        await engine.apply_config_change()
        return {"ok": True, "target_date": engine.target_date}
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}


class JobIn(BaseModel):
    running: bool
    date: Date | None = None


@app.put("/api/jobs/{name}")
async def control_job(name: str, d: JobIn):
    jobs = {"auto": (engine.start_auto, engine.stop_auto),
            "leak": (engine.start_leak, engine.stop_leak),
            "vid": (engine.start_vid_pool, engine.stop_vid_pool),
            "refresh": (engine.start_refresh, engine.stop_refresh)}
    if name not in jobs:
        from fastapi import HTTPException
        raise HTTPException(404, "Unknown job")
    start, stop = jobs[name]
    if not d.running:
        await stop()
        return {"ok": True}
    if name == "refresh":
        return await start(d.date.isoformat() if d.date else None)
    return await start()


@app.post("/api/token/fetch")
async def token_fetch():
    return await engine.fetch_token()


@app.post("/api/mitm/restart")
async def mitm_restart_ep():
    return {"ok": await engine.restart_mitm()}


class ReserveIn(BaseModel):
    court_no: int
    start: str
    end: str
    date: Date | None = None


@app.post("/api/reserve")
async def reserve_ep(d: ReserveIn):
    return await engine.reserve(d.court_no, d.start, d.end, d.date.isoformat() if d.date else None)


@app.get("/api/orders")
async def orders_ep():
    return await engine.refresh_orders()


@app.delete("/api/orders/{order_id}")
async def cancel_order_ep(order_id: str):
    return await engine.cancel_order(order_id)


@app.get("/api/stream")
async def stream_ep():
    def frame(topic, data, seq=None):
        prefix = f"id: {seq}\n" if seq is not None else ""
        return prefix + f"event: {topic}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    async def gen():
        q = engine.bus.subscribe()
        try:
            last = engine.bus.seq
            yield frame("snapshot", engine.snapshot(), last)
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if event is None:
                    last = engine.bus.seq
                    yield frame("snapshot", engine.snapshot(), last)
                elif event["_seq"] > last:
                    last = event["_seq"]
                    yield frame(event["_topic"], event, last)
        finally:
            engine.bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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