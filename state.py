"""Runtime state and task ownership, independent of the external service."""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from utils import Court

@dataclass(frozen=True)
class AutoConfig:
    slot_start: str = "19:30"
    slot_end: str = "21:30"
    save_order_at: str = "20:00:04"
    court_no: int = 0

@dataclass(frozen=True)
class Config:
    user_id: str = ""
    customer_id: str = ""
    customer_name: str = ""
    customer_tel: str = ""
    gym_id: str = ""
    gym_name: str = ""
    courts: tuple[Court, ...] = ()
    offset_days: int = 1
    auto: AutoConfig = field(default_factory=AutoConfig)

@dataclass(frozen=True)
class Session:
    token: str = ""

class Jobs:
    def __init__(self):
        self.tasks: dict[Any, asyncio.Task] = {}

    def running(self, name):
        task = self.tasks.get(name)
        return task is not None and not task.done()

    def start(self, name, coroutine):
        if self.running(name):
            coroutine.close()
            raise RuntimeError(f"Job already running: {name}")
        task = asyncio.create_task(coroutine)
        self.tasks[name] = task
        def finished(t):
            if self.tasks.get(name) is t:
                self.tasks.pop(name, None)
            if not t.cancelled() and (exc := t.exception()) is not None:
                logging.getLogger("court").error("后台任务 %s 异常: %s", name, exc,
                                                exc_info=(type(exc), exc, exc.__traceback__))
        task.add_done_callback(finished)
        return task

    async def stop(self, name):
        task = self.tasks.get(name)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def stop_all(self):
        tasks = [t for t in self.tasks.values() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

@dataclass(frozen=True)
class AutoPlan:
    date: str
    config: AutoConfig
    court: Court

@dataclass
class AutoState:
    phase: str = "idle"
    msg: str = ""
    applied: list = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    plan: AutoPlan | None = None

@dataclass
class CurrentOrder:
    scope: str
    date: str
    court: "Court"
    start: str
    end: str
    phase: str = "preparing"
    attempt_at: datetime = field(default_factory=datetime.now)
    record: dict | None = None
    sent_at: str = ""
    returned_at: str = ""
    first_empty_at: datetime | None = None
    result: str = ""


# ══════════════════════════════════════════════════════
# 事件总线
# ══════════════════════════════════════════════════════
class EventBus:
    def __init__(self):
        self._subs: set[asyncio.Queue] = set()
        self._seq = 0

    @property
    def seq(self) -> int:
        return self._seq

    async def publish(self, topic: str, data: dict):
        self._seq += 1
        evt = {"_seq": self._seq, "_topic": topic, "data": data}
        for q in list(self._subs):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                # A slow subscriber must recover from a fresh snapshot.
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(None)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q):
        self._subs.discard(q)


