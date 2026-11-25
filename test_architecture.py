"""Offline regression tests. External calls are replaced by a fake adapter."""
import asyncio
import json
import threading
from collections import deque
from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app
import engine
import utils


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text("""[user]
id = "private-id"
[order]
token = "private-token"
[courts]
3 = "court-3"
[auto]
court_order = [3]
""", encoding="utf-8")
    monkeypatch.setattr(engine, 'CONFIG_PATH', path)
    for name in ('read_config_dict', 'write_config_string', 'write_config_int',
                 'write_config_string_renamed', 'write_config_int_list'):
        monkeypatch.setattr(engine, name, partial(getattr(utils, name), path=path))

    def unexpected(*args, **kwargs):
        raise AssertionError('Unexpected external call')

    for name in ('refresh_window', 'port_listening', 'kill_port_holder', 'spawn_mitmdump'):
        monkeypatch.setattr(engine, name, unexpected)
    for name in ('get_vid_once', 'close_cffi_session'):
        monkeypatch.setattr(engine, name, AsyncMock(side_effect=unexpected))
    monkeypatch.setattr(utils, 'get_cffi_session', unexpected)
    monkeypatch.setattr(app, 'engine', engine.Engine())
    return engine


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


def test_api_routes():
    routes = {(r.path, m) for r in app.app.routes for m in r.methods if r.path.startswith('/api/')}
    assert routes == {
        ('/api/state', 'GET'), ('/api/config', 'PATCH'), ('/api/jobs/{name}', 'PUT'),
        ('/api/reserve', 'POST'), ('/api/window/refresh', 'POST'), ('/api/mitm/restart', 'POST'),
        ('/api/stream', 'GET'), ('/api/orders', 'GET'), ('/api/orders/{order_id}', 'DELETE'),
    }


def test_token_save_result(runtime, monkeypatch):
    async def run():
        same = await app.set_config(app.ConfigIn(token='private-token'))
        assert (same['ok'], same['status']) == (False, 'SAME')
        token = 'new-"token"\\value'
        changed = await app.set_config(app.ConfigIn(token=token))
        assert (changed['ok'], changed['status']) == (True, 'NEW')
        assert app.engine.session.token == token
        assert set(changed) == {'ok', 'status', 'msg', 'target_date'}
        assert runtime.read_config_dict()['order']['token'] == token
        assert (await app.set_config(app.ConfigIn(token='another-token')))['status'] == 'NEW'

        def failed(*args):
            raise OSError('write failed')

        monkeypatch.setattr(runtime, 'write_config_string', failed)
        failure = await app.set_config(app.ConfigIn(token='unsaved-token'))
        assert (failure['ok'], failure['status']) == (False, 'FAILED')
        assert app.engine.session.token == 'another-token'
    asyncio.run(run())


@pytest.mark.parametrize('time_out', [False, True])
def test_window_refresh_shared(runtime, monkeypatch, time_out):
    started, release = threading.Event(), threading.Event()
    calls = []

    def refresh(**kwargs):
        calls.append(kwargs)
        started.set()
        return release.wait(2)

    monkeypatch.setattr(runtime, 'refresh_window', refresh)

    async def run():
        if time_out:
            app.engine.tuning.window_refresh_timeout = 0.02
        try:
            first = asyncio.create_task(app.refresh_window_ep())
            assert await asyncio.to_thread(started.wait, 2)
            if time_out:
                result = await first
                assert not result['ok'] and '超时' in result['msg']
                app.engine.tuning.window_refresh_timeout = 1
            others = [asyncio.create_task(app.refresh_window_ep()) for _ in range(4)]
            await asyncio.sleep(0)
            release.set()
            assert all(r['ok'] for r in await asyncio.gather(*others))
            if not time_out:
                assert (await first)['ok']
            assert len(calls) == 1
        finally:
            release.set()
    asyncio.run(run())


def reply(status, body=None):
    def decode():
        if body is None:
            raise ValueError('non-JSON')
        return body
    return SimpleNamespace(status_code=status, json=decode)


@pytest.mark.parametrize(('status', 'body', 'message'), [
    (401, None, '鉴权失败'),
    (200, {'success': False, 'code': '401'}, '鉴权失败'),
    (403, {'msg': '鉴权失败'}, '拒绝访问'),
    (429, {'msg': '鉴权失败'}, '限流'),
])
def test_queries_stop_on_access_error(runtime, monkeypatch, status, body, message):
    async def run():
        get = AsyncMock(return_value=reply(status, body))
        monkeypatch.setattr(utils, 'get_cffi_session', lambda: SimpleNamespace(get=get))
        e = app.engine
        court, = e.config.courts
        queue = e.bus.subscribe()
        await e._refresh_probe_then_fanout([court], e.target_date)
        event = queue.get_nowait()
        assert message in event["data"]['error']
        get.assert_awaited_once()
        get.reset_mock()
        orders = await app.orders_ep()
        assert not orders['ok'] and message in orders['msg']
        get.assert_awaited_once()
    # The real local request limiter keeps timestamps. Reset it per event loop.
    monkeypatch.setattr(utils, '_get_starts', deque())
    asyncio.run(run())


@pytest.mark.parametrize(('outcome', 'kind'), [
    (TimeoutError(), 'timeout'),
    (utils.curl_requests.exceptions.ConnectionError('disconnected'), 'unknown'),
    (reply(500, {'success': False}), 'unknown'),
    (reply(200, {'success': False, 'code': 500}), 'unknown'),
    (reply(400), 'rejected'),
    (reply(200), 'unknown'),
    (reply(307), 'unknown'),
    (reply(200, {'success': False, 'msg': '不可预约'}), 'rejected'),
    (reply(401), 'auth'),
    (reply(403), 'restricted'),
    (reply(429), 'rate_limited'),
])
def test_reservation_post_only_sent_once(runtime, monkeypatch, outcome, kind):
    async def run():
        post = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
        monkeypatch.setattr(utils, 'get_cffi_session', lambda: SimpleNamespace(post=post))
        e = app.engine
        court, = e.config.courts
        result = await e._save_order('fake-vid', court, '2026-11-26', '19:00', '20:00', e.session.token)
        assert not result['success'] and result['kind'] == kind
        if kind in ('timeout', 'unknown'):
            assert '请先查询订单' in result['msg']
        post.assert_awaited_once()
        assert post.await_args.kwargs['allow_redirects'] is False
    asyncio.run(run())


def test_cancel_order_timeout_is_not_replayed(runtime, monkeypatch):
    async def run():
        order = {'id': 'order-id', 'status': 2, 'isDeleted': 0, 'orderDate': '2026-11-26',
                 'startTime': '2026-11-26 19:00:00', 'endTime': '2026-11-26 20:00:00',
                 'gymId': 'gym-id'}
        get = AsyncMock(return_value=reply(200, {'success': True, 'data': {'records': [order], 'pages': 1}}))
        post = AsyncMock(side_effect=[reply(200, {'success': True, 'data': order}), TimeoutError()])
        monkeypatch.setattr(utils, 'get_cffi_session', lambda: SimpleNamespace(get=get, post=post))
        result = await app.cancel_order_ep('order-id')
        assert not result['ok'] and '执行结果未知' in result['msg']
        assert [call.args[0].split('/')[-1] for call in post.await_args_list] == [
            'getOrderDetailByParam', 'operatOrder',
        ]
    monkeypatch.setattr(utils, '_get_starts', deque())
    asyncio.run(run())


def test_successful_order_preserves_server_data(runtime, monkeypatch):
    async def run():
        body = {'success': True, 'code': 200, 'data': {'id': 'order-id'}}
        post = AsyncMock(return_value=reply(200, body))
        monkeypatch.setattr(utils, 'get_cffi_session', lambda: SimpleNamespace(post=post))
        e = app.engine
        court, = e.config.courts
        assert await e._save_order('fake-vid', court, '2026-11-26', '19:00', '20:00', e.session.token) == body
        post.assert_awaited_once()
    asyncio.run(run())
