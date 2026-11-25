"""Offline regression tests. External calls are replaced by a fake adapter."""
import asyncio
import importlib
import json
import re
import sys
import threading
import time
import types
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _write_token(path, token):
    """Simulate mitmproxy writing a captured credential into config.toml."""
    text = path.read_text(encoding="utf-8")
    path.write_text(re.sub(r'(?m)^(token\s*=\s*)"[^"]*"',
                           lambda m: m.group(1) + f'"{token}"', text),
                    encoding="utf-8")


def _flow(path, query, payload, *, status=200):
    return SimpleNamespace(
        request=SimpleNamespace(pretty_host='reservation.sustech.edu.cn',
                                path=path, query=query),
        response=SimpleNamespace(status_code=status,
                                 json=lambda: payload,
                                 get_text=lambda: json.dumps(payload)),
    )


def _count_writes(monkeypatch, module):
    writes = []
    real_write = module.write_config_string

    def counted(key, value):
        writes.append((key, value))
        real_write(key, value)

    monkeypatch.setattr(module, 'write_config_string', counted)
    return writes


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
    config_path = tmp_path / 'config.toml'
    config_path.write_text(
        '[user]\nid = "private-id"\n[order]\ntoken = "private-token"\n'
        '[courts]\n3 = "court-3"\n[auto]\ncourt_order = [3]\n',
        encoding="utf-8",
    )
    adapter.CONFIG_PATH = config_path
    adapter.MITM_HOST, adapter.MITM_PORT, adapter.MITM_UPSTREAM = '127.0.0.1', 8080, ''
    adapter.RESERVATION_HOST = 'reservation.sustech.edu.cn'
    adapter.BASE_URL = 'http://example.invalid'

    def read_config_dict():
        text = config_path.read_text(encoding="utf-8")
        token = re.search(r'(?m)^token\s*=\s*"([^"]*)"', text).group(1)
        return {'user': {'id': 'private-id'}, 'order': {'token': token},
                'courts': {'3': 'court-3'}, 'auto': {'court_order': [3]}}

    def write_config_string(key, value):
        if key == 'token':
            _write_token(config_path, value)

    adapter.read_config_dict = read_config_dict
    adapter.write_config_string = write_config_string
    for name in ('write_config_int', 'write_config_string_renamed', 'write_config_int_list',
                 'refresh_window', 'port_listening', 'kill_port_holder', 'spawn_mitmdump'):
        setattr(adapter, name, lambda *a, **k: None)
    for name in ('get_vid_once', 'get_order_once', 'save_order_once',
                 'close_cffi_session', 'limited_get'):
        setattr(adapter, name, AsyncMock(side_effect=AssertionError('Unexpected external call')))
    adapter.get_cffi_session = lambda: SimpleNamespace(post=AsyncMock())
    adapter.is_auth_fail = lambda j: '鉴权失败' in str(j.get('msg', ''))
    adapter.classify_order_reply = lambda r: ('ok', 'ok')
    monkeypatch.setitem(sys.modules, 'utils', adapter)
    monkeypatch.delitem(sys.modules, 'engine', raising=False)
    monkeypatch.delitem(sys.modules, 'app', raising=False)
    monkeypatch.delitem(sys.modules, 'mitm_addon', raising=False)
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
    assert routes == {('/api/state','GET'), ('/api/config','PATCH'), ('/api/jobs/{name}','PUT'), ('/api/reserve','POST'), ('/api/token/fetch','POST'), ('/api/mitm/restart','POST'), ('/api/orders','GET'), ('/api/orders/{order_id}','DELETE'), ('/api/stream','GET')}


# ══════════════════════════════════════════════════════
# 统一凭证入口：模拟凭证观察事件
# ══════════════════════════════════════════════════════
def test_fetch_token_accepts_only_new_value(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        e.tuning.token_refresh_wait_sec = 0.5
        e._ensure_mitm = AsyncMock(return_value=True)
        refreshes = []

        def refresh(**kwargs):
            refreshes.append(1)
            addon = importlib.import_module('mitm_addon')
            for token in ('private-token', 'private-token', 'private-token', 'new-token-value'):
                if token == 'private-token':
                    flow = _flow('/api/blade-app/qywx/getCustomerMessageNOReadCount',
                                 {'token': token}, {'success': True, 'data': {}})
                else:
                    flow = _flow('/api/blade-app/qywx/getUserIdByCode',
                                 {'code': 'oauth-code'},
                                 {'success': True, 'data': {'token': token}})
                addon.response(flow)
                time.sleep(0.01)
            return True

        monkeypatch.setattr(runtime, 'refresh_window', refresh)
        result = await e.fetch_token()

        assert result['ok'] is True
        assert result['attempts'] == 1
        assert result['token_preview'] == 'new-toke...'
        assert 'new-token-value' not in json.dumps(result)
        assert e.session.token == 'new-token-value'
        assert len(refreshes) == 1
    asyncio.run(run())


def test_fetch_token_waits_each_round_then_fails(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        wait = 0.5
        e.tuning.token_refresh_wait_sec = wait
        e._ensure_mitm = AsyncMock(return_value=True)
        refreshes = []

        def refresh(**kwargs):
            refreshes.append(1)
            _write_token(runtime.CONFIG_PATH, 'private-token')
            return True

        monkeypatch.setattr(runtime, 'refresh_window', refresh)
        start = time.monotonic()
        result = await e.fetch_token()
        elapsed = time.monotonic() - start

        assert result['ok'] is False
        assert result.get('attempts') is None
        assert len(refreshes) == e.TOKEN_ROUNDS == 3
        assert elapsed >= 3 * wait
        assert f'{wait:.1f}s' in result['msg']
    asyncio.run(run())


def test_fetch_token_concurrent_calls_refresh_once(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        e._ensure_mitm = AsyncMock(return_value=True)
        refreshing = threading.Event()
        release = threading.Event()
        refreshes = []

        def refresh(**kwargs):
            refreshes.append(1)
            refreshing.set()
            assert release.wait(2), '测试未释放窗口刷新'
            _write_token(runtime.CONFIG_PATH, 'concurrent-token')
            return True

        monkeypatch.setattr(runtime, 'refresh_window', refresh)
        first = asyncio.create_task(e.fetch_token())
        await asyncio.wait_for(asyncio.to_thread(refreshing.wait, 1), 2)
        # 第一个调用仍在等待新凭证，第二个调用从同一个旧值出发。
        second = asyncio.create_task(e.fetch_token())
        await asyncio.sleep(0.05)
        release.set()
        results = await asyncio.gather(first, second)

        assert all(r['ok'] for r in results)
        assert len(refreshes) == 1
        assert results[0]['attempts'] == 1
        assert results[1]['attempts'] == 0
        assert all(r['token_preview'] == 'concurre...' for r in results)
    asyncio.run(run())


def test_order_query_recovers_through_fetch_token(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        e._ensure_mitm = AsyncMock(return_value=True)
        seen = []

        async def limited_get(url, *, params=None, **kwargs):
            seen.append(params['token'])
            if len(seen) == 1:
                return SimpleNamespace(status_code=200, json=lambda: {
                    'success': False, 'msg': '鉴权失败'})
            return SimpleNamespace(status_code=200, json=lambda: {
                'success': True, 'data': {'records': [], 'pages': 1}})

        def refresh(**kwargs):
            _write_token(runtime.CONFIG_PATH, 'recovered-token-value')
            return True

        monkeypatch.setattr(runtime, 'limited_get', limited_get)
        monkeypatch.setattr(runtime, 'refresh_window', refresh)
        result = await e.refresh_orders()

        assert result['ok'] is True
        assert seen == ['private-token', 'recovered-token-value']
    asyncio.run(run())


def test_issued_token_writes_once(runtime, monkeypatch):
    addon = importlib.import_module('mitm_addon')
    writes = _count_writes(monkeypatch, addon)
    flow = _flow('/api/blade-app/qywx/getUserIdByCode', {'code': 'oauth-code'},
                 {'success': True, 'data': {'token': 'issued-token-value'}})

    addon.response(flow)
    addon.response(flow)

    assert writes == [('token', 'issued-token-value')]
    assert addon.read_config_dict()['order']['token'] == 'issued-token-value'


def test_request_side_token_never_writes(runtime, monkeypatch):
    addon = importlib.import_module('mitm_addon')
    writes = _count_writes(monkeypatch, addon)
    payload = {'success': True, 'data': {'token': 'new-token-value'}}

    for token in ('private-token', 'new-token-value') * 50:
        flow = _flow('/api/blade-app/qywx/getCustomerMessageNOReadCount',
                     {'token': token}, payload)
        addon.response(flow)

    assert writes == []
    assert addon.read_config_dict()['order']['token'] == 'private-token'


def test_har_sequence_writes_only_issuance(runtime, monkeypatch):
    addon = importlib.import_module('mitm_addon')
    writes = _count_writes(monkeypatch, addon)
    issued = {'success': True, 'data': {'token': 'new-token-value'}}

    auth_fail = _flow('/api/blade-app/qywx/getCustomerMessageNOReadCount',
                      {'token': 'private-token'},
                      {'success': False, 'msg': '鉴权失败2'})
    issuance = _flow('/api/blade-app/qywx/getUserIdByCode',
                     {'code': 'oauth-code'}, issued)
    business = _flow('/api/blade-app/qywx/getUserInfoByUserid',
                     {'token': 'new-token-value'}, issued)
    for flow in (auth_fail, issuance, business, business):
        addon.response(flow)

    assert writes == [('token', 'new-token-value')]
    assert addon.read_config_dict()['order']['token'] == 'new-token-value'
