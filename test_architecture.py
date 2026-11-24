"""Offline regression tests. External calls are replaced by a fake adapter."""
import asyncio
import importlib
import json
import sys
import types
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    adapter = types.ModuleType('utils')
    @dataclass(frozen=True)
    class Court:
        no: int
        id: str
        name: str
    adapter.Court = Court
    adapter.ROOT = tmp_path
    adapter.CONFIG_PATH = tmp_path / 'config.toml'
    adapter.CONFIG_PATH.touch()
    adapter.MITM_HOST, adapter.MITM_PORT, adapter.MITM_UPSTREAM = '127.0.0.1', 8080, ''
    adapter.read_config_dict = lambda: {'user': {'id': 'private-id'}, 'order': {'token': 'private-token'}, 'courts': {'3': 'court-3'}, 'auto': {'court_order': [3]}}
    for name in ('write_config_string', 'write_config_int', 'write_config_string_renamed', 'write_config_int_list', 'refresh_window', 'port_listening', 'kill_port_holder', 'spawn_mitmdump'):
        setattr(adapter, name, lambda *a, **k: None)
    for name in ('get_vid_once', 'get_order_once', 'save_order_once', 'close_cffi_session'):
        setattr(adapter, name, AsyncMock(side_effect=AssertionError('Unexpected external call')))
    adapter.classify_order_reply = lambda r: ('ok', 'ok')
    monkeypatch.setitem(sys.modules, 'utils', adapter)
    monkeypatch.delitem(sys.modules, 'engine', raising=False)
    monkeypatch.delitem(sys.modules, 'app', raising=False)
    return importlib.import_module('engine')


def test_config_and_snapshot(runtime):
    e = runtime.Engine()
    assert e.config.auto.court_order == (3,)
    assert e.session.token == 'private-token'
    snapshot = json.dumps(e.snapshot())
    assert 'private-token' not in snapshot and 'private-id' not in snapshot


def test_job_lifecycle():
    from state import Jobs
    async def run():
        jobs = Jobs()
        jobs.start('demo', asyncio.sleep(60))
        assert jobs.running('demo')
        with pytest.raises(RuntimeError):
            jobs.start('demo', asyncio.sleep(60))
        await jobs.stop('demo')
        assert not jobs.running('demo')
        jobs.start('other', asyncio.sleep(60))
        await jobs.stop_all()
        assert not jobs.running('other')
    asyncio.run(run())


def test_auto_plan_immutable(runtime):
    async def run():
        e = runtime.Engine()
        e._auto_loop = lambda: asyncio.sleep(60)
        assert (await e.start_auto())['ok']
        original = e.auto.plan
        e.config = replace(e.config, auto=replace(e.config.auto, slot_start='20:00'))
        assert e.auto.plan == original
        assert e.auto_state()['running']
        await e.stop_auto()
        assert not e.auto_state()['running'] and e.auto.phase == 'stopped'
    asyncio.run(run())


def test_refresh_cache(runtime):
    async def run():
        e = runtime.Engine()
        c = e.config.courts[0]
        await e._publish_refresh(c, {'ok': True, 'avail': {'19:00': '1'}, 'blocks': [{'customerTel': 'secret', 'customerCode': 'secret'}]}, '2026-11-26')
        assert len(e.snapshot()['courts']) == 1
        assert 'secret' not in json.dumps(e.snapshot()['courts'])
        assert e.snapshot()['courts'][0]['avail']['19:00'] == '1'
    asyncio.run(run())


def test_stream_reconnect(runtime):
    async def run():
        app = importlib.import_module('app')
        response = await app.stream_ep()
        stream = response.body_iterator
        first = await anext(stream)
        assert 'event: snapshot' in first
        await app.engine.bus.publish('example', {'value': 1})
        second = await anext(stream)
        assert 'event: example' in second and 'id:' in second
        await stream.aclose()
        assert not app.engine.bus._subs
        response = await app.stream_ep()
        first = await anext(response.body_iterator)
        assert 'event: snapshot' in first
        await response.body_iterator.aclose()
    asyncio.run(run())


def test_bus_overflow():
    from state import EventBus
    async def run():
        bus = EventBus()
        q = bus.subscribe()
        for i in range(q.maxsize + 1):
            await bus.publish('test', {'i': i})
        assert q.get_nowait() is None
        bus.unsubscribe(q)
    asyncio.run(run())


def test_api_routes(runtime):
    app = importlib.import_module('app').app
    routes = {(r.path, m) for r in app.routes for m in r.methods if r.path.startswith('/api/')}
    assert routes == {('/api/state','GET'), ('/api/config','PATCH'), ('/api/jobs/{name}','PUT'), ('/api/reserve','POST'), ('/api/token/fetch','POST'), ('/api/mitm/restart','POST'), ('/api/stream','GET')}
