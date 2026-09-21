"""main.py — FastAPI 路由层（薄壳）"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import core
from core import (COURT_BY_NO, auto_state, bus, get_logs, log, refresh_service,
                  session, vid_pool)


def default_date() -> str:
    return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


@asynccontextmanager
async def lifespan(app):
    log.info("服务已启动")
    yield
    await vid_pool.stop()
    await refresh_service.stop()
    await core.stop_auto()


app = FastAPI(title="Court Reserver", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


# ---------- config / logs ----------
class ConfigIn(BaseModel):
    token: str
    target_date: str = ""


@app.post("/api/config")
def set_config(d: ConfigIn):
    session.token = d.token.strip()
    session.target_date = d.target_date.strip() or default_date()
    log.info("配置更新: date=%s", session.target_date)
    return {"ok": True, "target_date": session.target_date,
            "token_set": bool(session.token)}


@app.get("/api/logs")
def logs_ep(since: int = 0):
    return {"logs": get_logs(since)}


# ---------- vid ----------
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


# ---------- refresh ----------
@app.post("/api/refresh")
async def refresh_ep():
    if not session.token:
        return {"ok": False, "msg": "未配置 token"}
    n = await refresh_service.start(session.target_date or default_date(), session.token)
    return {"ok": True, "total": n}


@app.get("/api/refresh/seq")
def refresh_seq():
    return {"seq": bus.seq}


# ---------- reserve ----------
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
    result = await core.submit_order(
        vid, court, session.target_date or default_date(),
        d.start, d.end, session.token,
    )
    return {"ok": bool(result.get("success")), "msg": result.get("msg", "")}


# ---------- auto ----------
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
    return await core.start_auto(
        date=session.target_date or default_date(), token=session.token,
        slot_start=d.slot_start, slot_end=d.slot_end,
        refresh_at=d.refresh_at, submit_at=d.submit_at,
        max_orders=int(d.max_orders), idle_exit=float(d.idle_exit),
    )


@app.post("/api/auto/stop")
async def auto_stop_ep():
    await core.stop_auto()
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


# ---------- SSE ----------
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