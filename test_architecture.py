"""Offline regression tests. External calls are replaced by a fake adapter."""
import asyncio
import importlib
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
import types
try:
    import tomllib
except ImportError:
    import tomli as tomllib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
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
        '[courts]\n3 = "court-3"\n4 = "court-4"\n'
        '[auto]\ncourt_no = 3\nslot_start = "19:00"\nslot_end = "21:00"\n'
        'save_order_at = "20:00:04"\n',
        encoding="utf-8",
    )
    adapter.CONFIG_PATH = config_path
    adapter.MITM_HOST, adapter.MITM_PORT, adapter.MITM_UPSTREAM = '127.0.0.1', 8080, ''
    adapter.RESERVATION_HOST = 'reservation.sustech.edu.cn'
    adapter.BASE_URL = 'http://example.invalid'

    def read_config_dict():
        with config_path.open('rb') as f:
            return tomllib.load(f)

    def write_config_string(key, value):
        if key == 'token':
            _write_token(config_path, value)

    def capture_token_from_flow(flow, host='reservation.sustech.edu.cn',
                                config_path=config_path):
        if flow.request.pretty_host != host:
            return
        token = flow.request.query.get('token')
        if not token:
            return
        m = re.search(r'(?m)^\s*token\s*=\s*"([^"]*)"',
                      config_path.read_text(encoding='utf-8'))
        if m and m.group(1):
            return
        write_config_string('token', token)

    adapter.read_config_dict = read_config_dict
    adapter.write_config_string = write_config_string
    adapter.capture_token_from_flow = capture_token_from_flow
    for name in ('write_config_int', 'write_config_string_renamed',
                 'write_config_int_renamed',
                 'refresh_window', 'port_listening', 'kill_port_holder', 'spawn_mitmdump'):
        setattr(adapter, name, lambda *a, **k: None)
    for name in ('get_order_once', 'save_order_once',
                 'close_cffi_session', 'limited_get'):
        setattr(adapter, name, AsyncMock(side_effect=AssertionError('Unexpected external call')))
    adapter.get_vid_once = AsyncMock(return_value=('fake-vid', {}))
    adapter.get_cffi_session = lambda: SimpleNamespace(post=AsyncMock())
    adapter.is_auth_fail = lambda j: '鉴权失败' in str(j.get('msg', ''))
    adapter.format_stamp = lambda dt=None: (dt or datetime.now()).strftime(
        '%Y-%m-%d %H:%M:%S.%f')[:-3]

    def classify(r):
        if r.get('success'):
            return 'ok', 'ok'
        if r.get('uncertain'):
            return 'uncertain', 'uncertain'
        msg = str(r.get('msg') or '').lower()
        if any(k in msg for k in ('繁忙', 'busy', '稍后', '频繁')):
            return 'busy', 'busy'
        return 'error', 'error'

    adapter.classify_order_reply = classify
    monkeypatch.setitem(sys.modules, 'utils', adapter)
    monkeypatch.delitem(sys.modules, 'engine', raising=False)
    monkeypatch.delitem(sys.modules, 'app', raising=False)
    monkeypatch.delitem(sys.modules, 'mitm_addon', raising=False)
    return importlib.import_module('engine')


def test_config_and_snapshot(runtime):
    e = runtime.Engine()
    assert e.config.auto.court_no == 3
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


def test_frontend_court_picker_is_strict_single_select():
    node = shutil.which('node')
    if not node:
        pytest.skip('node 未安装，跳过前端行为测试')
    script = Path(__file__).with_name('test_frontend_court_picker.mjs')
    result = subprocess.run([node, str(script)], capture_output=True, text=True,
                            timeout=30)
    assert result.returncode == 0, result.stderr or result.stdout


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


def test_order_auth_failure_logs_reason_then_recovers(runtime, monkeypatch, caplog):
    async def run():
        e = runtime.Engine()
        e._ensure_mitm = AsyncMock(return_value=True)
        seen = []

        async def limited_get(url, *, params=None, **kwargs):
            seen.append(params['token'])
            if len(seen) == 1:
                return SimpleNamespace(status_code=200, json=lambda: {
                    'success': False, 'code': 400, 'msg': '鉴权失败2'})
            return SimpleNamespace(status_code=200, json=lambda: {
                'success': True, 'data': {'records': [], 'pages': 1}})

        def refresh(**kwargs):
            _write_token(runtime.CONFIG_PATH, 'recovered-token-value')
            return True

        monkeypatch.setattr(runtime, 'limited_get', limited_get)
        monkeypatch.setattr(runtime, 'refresh_window', refresh)
        with caplog.at_level(logging.INFO, logger='court'):
            result = await e.refresh_orders()

        messages = [r.getMessage() for r in caplog.records if r.name == 'court']
        assert result['ok'] is True
        assert seen == ['private-token', 'recovered-token-value']
        assert any('订单查询 getOrderPageListByParam 鉴权失败：HTTP 200 | code=400 | msg=鉴权失败2，自动获取 Token…' in m
                   for m in messages)
        assert any('会话刷新 1/3' in m for m in messages)
        assert any('恢复后重试成功' in m for m in messages)
    asyncio.run(run())


def test_request_captures_initial_token_only(runtime, monkeypatch):
    addon = importlib.import_module('mitm_addon')
    _write_token(runtime.CONFIG_PATH, '')
    first = _flow('/api/blade-app/qywx/getCustomerMessageNOReadCount',
                  {'token': 'bootstrap-token-value'},
                  {'success': True, 'data': {}})
    later = _flow('/api/blade-app/qywx/getCustomerMessageNOReadCount',
                  {'token': 'later-token-value'},
                  {'success': True, 'data': {}})

    addon.request(first)
    addon.request(later)

    assert addon.read_config_dict()['order']['token'] == 'bootstrap-token-value'


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


# ══════════════════════════════════════════════════════
# 自动模式：单场地、单次下单、等待 Save Order At
# ══════════════════════════════════════════════════════
def _auto_engine(e, runtime, save_order_at, *, court_no=3, court_nos=(3, 4)):
    courts = tuple(runtime.Court(n, f'court-{n}', f'{n}号场') for n in court_nos)
    e.config = replace(e.config, courts=courts,
                       auto=replace(e.config.auto, court_no=court_no,
                                    slot_start='19:00', slot_end='21:00',
                                    save_order_at=save_order_at))
    e.tuning.token_refresh_lead_sec = 0
    e.fetch_token = AsyncMock(return_value={'ok': True, 'token_preview': 'test...'})
    e.start_vid_pool = AsyncMock()
    return courts


def _hms(dt):
    return dt.strftime('%H:%M:%S.%f')[:-3]


async def _run_job(e, name):
    task = e.jobs.tasks[name]
    await asyncio.wait_for(task, 3)


def test_auto_config_rejects_empty_and_unknown_court(runtime):
    async def run():
        e = runtime.Engine()
        with pytest.raises(ValueError):
            await e.update_auto_config('19:00', '21:00', '20:00:04', 0)
        with pytest.raises(ValueError):
            await e.update_auto_config('19:00', '21:00', '20:00:04', 99)
        await e.update_auto_config('19:00', '21:00', '20:00:05.123', 3)
    asyncio.run(run())


def test_auto_config_old_multi_court_requires_reselection(runtime, monkeypatch):
    monkeypatch.setattr(runtime, 'read_config_dict', lambda: {
        'user': {}, 'gym': {}, 'order': {'token': 'private-token'},
        'courts': {'3': 'court-3', '4': 'court-4'},
        'auto': {'court_order': [3, 4]},
    })

    async def run():
        e = runtime.Engine()
        assert e.config.auto.court_no == 0
        assert (await e.start_auto())['ok'] is False
    asyncio.run(run())


def test_save_order_at_keeps_milliseconds(runtime):
    e = runtime.Engine()
    parsed = e._parse_hms('20:00:05.123')
    assert (parsed.hour, parsed.minute, parsed.second) == (20, 0, 5)
    assert parsed.microsecond == 123000


def test_auto_waits_then_orders_only_target_court_once(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        target = datetime.now() + timedelta(seconds=0.35)
        _auto_engine(e, runtime, _hms(target), court_no=4)
        calls = []

        async def save_order(**kwargs):
            calls.append((kwargs, datetime.now()))
            return {'success': True, 'msg': 'ok'}

        query = AsyncMock(side_effect=AssertionError('自动模式不应查询场地可用性'))
        monkeypatch.setattr(runtime, 'save_order_once', save_order)
        monkeypatch.setattr(runtime, 'get_order_once', query)
        e.refresh_orders = AsyncMock(side_effect=AssertionError('自动模式不应查询订单'))

        assert (await e.start_auto())['ok']
        await _run_job(e, 'auto')

        assert len(calls) == 1
        kwargs, sent_at = calls[0]
        assert kwargs['court'].no == 4
        assert kwargs['start'] == '19:00' and kwargs['end'] == '21:00'
        assert sent_at >= target - timedelta(seconds=0.05)
        assert query.await_count == 0
        assert e.auto.phase == 'done'
        entry = e.auto.applied[0]
        assert (entry['court'], entry['name'], entry['ok'], entry['result']) == (
            4, '4号场', True, 'ok')
        for key in ('scheduled_at', 'sent_at', 'returned_at', 'confirmed_at'):
            assert re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}',
                                entry[key])
        assert entry['scheduled_at'] <= entry['sent_at'] <= entry['returned_at']

        await asyncio.sleep(0.05)
        assert len(calls) == 1
    asyncio.run(run())


def test_auto_ends_after_explicit_failure_with_one_request(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        _auto_engine(e, runtime, '20:00:04', court_no=4)
        e._parse_hms = lambda value: datetime.now() - timedelta(seconds=1)
        calls = []

        async def save_order(**kwargs):
            calls.append(kwargs)
            return {'success': False, 'msg': '系统繁忙'}

        monkeypatch.setattr(runtime, 'save_order_once', save_order)
        query = AsyncMock(side_effect=AssertionError('自动模式不应查询场地可用性'))
        monkeypatch.setattr(runtime, 'get_order_once', query)
        e.refresh_orders = AsyncMock(side_effect=AssertionError('明确失败无需核对订单'))

        assert (await e.start_auto())['ok']
        await _run_job(e, 'auto')

        assert len(calls) == 1
        assert calls[0]['court'].no == 4
        assert query.await_count == 0
        assert e.auto.phase == 'done'
        assert e.auto.applied and e.auto.applied[0]['ok'] is False

        await asyncio.sleep(0.05)
        assert len(calls) == 1
    asyncio.run(run())


# ══════════════════════════════════════════════════════
# 检漏模式：发现即预约，成功停止，失败继续
# ══════════════════════════════════════════════════════
def _prepare_leak(e, monkeypatch):
    date = (datetime.now() + timedelta(days=1)).date().isoformat()
    court = e.config.courts[0]
    e._leak_dates = lambda now=None: [date]
    e._leak_slot = lambda d, avail: ('19:00', '21:00')
    e._leak_paused = lambda now=None: False
    e._leak_query_budget = lambda now: 4.0
    e.refresh_orders = AsyncMock(return_value={'ok': True, 'msg': '', 'orders': []})
    e._refresh_one = AsyncMock(return_value=(court, {'ok': True, 'avail': {}}))
    e.tuning.leak_interval = 0.02
    return date, court


def test_leak_reserves_automatically_then_stops(runtime, monkeypatch, caplog):
    async def run():
        e = runtime.Engine()
        date, court = _prepare_leak(e, monkeypatch)
        query_done = []

        async def refresh_one(c, d, **kwargs):
            await asyncio.sleep(0.03)
            query_done.append(datetime.now())
            return court, {'ok': True, 'avail': {}}

        e._refresh_one = refresh_one
        calls = []

        async def save_order(**kwargs):
            calls.append(kwargs)
            return {'success': True, 'msg': 'ok'}

        monkeypatch.setattr(runtime, 'save_order_once', save_order)

        with caplog.at_level(logging.INFO, logger='court'):
            assert (await e.start_leak())['ok']
            await _run_job(e, 'leak')

        assert len(calls) == 1
        assert calls[0]['court'].no == court.no
        assert calls[0]['date'] == date
        assert calls[0]['start'] == '19:00' and calls[0]['end'] == '21:00'
        assert not e.jobs.running('leak')
        record = e.leak_found
        assert record and record['booked'] is True
        assert record['court'] == court.no
        assert record['date'] == date
        assert (record['start'], record['end']) == ('19:00', '21:00')
        assert record['result'] == 'ok'
        for key in ('found_at', 'sent_at', 'returned_at', 'confirmed_at'):
            assert re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}',
                                record[key])
        found_at = datetime.strptime(record['found_at'], '%Y-%m-%d %H:%M:%S.%f')
        sent_at = datetime.strptime(record['sent_at'], '%Y-%m-%d %H:%M:%S.%f')
        returned_at = datetime.strptime(record['returned_at'], '%Y-%m-%d %H:%M:%S.%f')
        confirmed_at = datetime.strptime(record['confirmed_at'], '%Y-%m-%d %H:%M:%S.%f')
        assert query_done[0] - timedelta(milliseconds=1) <= found_at
        assert found_at <= sent_at <= returned_at <= confirmed_at
        assert '预约成功' in e._leak_msg

        messages = [r.getMessage() for r in caplog.records if r.name == 'court']
        assert any(m.startswith('检漏发现：') for m in messages)
        assert any('检漏下单：' in m for m in messages)
        assert any(re.search(r'检漏预约成功：.*耗时\d+ms', m)
                   for m in messages)

        saved = dict(record)
        await e.stop_leak()
        await e.apply_config_change()
        assert e.leak_found['found_at'] == saved['found_at']
        assert e.snapshot()['leak_found']['found_at'] == saved['found_at']

        await asyncio.sleep(0.05)
        assert len(calls) == 1
    asyncio.run(run())


def test_leak_failure_keeps_last_discovery(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        _prepare_leak(e, monkeypatch)
        e.tuning.leak_interval = 1.0
        calls = []

        async def save_order(**kwargs):
            calls.append(kwargs)
            return {'success': False, 'msg': '系统繁忙'}

        monkeypatch.setattr(runtime, 'save_order_once', save_order)
        assert (await e.start_leak())['ok']
        for _ in range(200):
            if e.leak_found and e.leak_found.get('result') != 'pending':
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError('检漏未记录预约失败结果')

        record = dict(e.leak_found)
        assert record['result'] == 'busy'
        assert record['booked'] is False
        assert record['found_at']
        assert record['sent_at'] and record['returned_at']
        assert record['confirmed_at'] == record['returned_at']

        await e.stop_leak()
        await e.apply_config_change()
        assert e.leak_found['found_at'] == record['found_at']
        assert e.snapshot()['leak_found']['found_at'] == record['found_at']
        assert len(calls) == 1
    asyncio.run(run())


def test_leak_explicit_failure_continues_until_success(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        _prepare_leak(e, monkeypatch)
        calls = []
        results = iter([{'success': False, 'msg': '系统繁忙'},
                        {'success': True, 'msg': 'ok'}])

        async def save_order(**kwargs):
            calls.append(kwargs)
            return next(results)

        monkeypatch.setattr(runtime, 'save_order_once', save_order)

        assert (await e.start_leak())['ok']
        await _run_job(e, 'leak')

        assert len(calls) == 2
        assert e.leak_found and e.leak_found['booked'] is True
        assert '预约成功' in e._leak_msg
    asyncio.run(run())


def test_leak_uncertain_result_confirms_order_before_stopping(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        _prepare_leak(e, monkeypatch)
        calls = []

        async def save_order(**kwargs):
            calls.append(kwargs)
            return {'success': False, 'msg': 'timeout', 'uncertain': True}

        monkeypatch.setattr(runtime, 'save_order_once', save_order)
        e._confirm_order = AsyncMock(return_value='created')

        assert (await e.start_leak())['ok']
        await _run_job(e, 'leak')

        assert len(calls) == 1
        assert e._confirm_order.await_count == 1
        assert e.leak_found and e.leak_found['booked'] is True
        assert '预约成功' in e._leak_msg
    asyncio.run(run())


def test_leak_uncertain_without_order_waits_before_retrying(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        _prepare_leak(e, monkeypatch)
        calls = []
        results = iter([{'success': False, 'msg': 'timeout', 'uncertain': True},
                        {'success': True, 'msg': 'ok'}])

        async def save_order(**kwargs):
            calls.append(kwargs)
            return next(results)

        monkeypatch.setattr(runtime, 'save_order_once', save_order)
        e._confirm_order = AsyncMock(return_value='none')

        assert (await e.start_leak())['ok']
        await _run_job(e, 'leak')

        assert len(calls) == 2
        assert e._confirm_order.await_count == 1
        assert e.leak_found and e.leak_found['booked'] is True
    asyncio.run(run())


def test_auto_config_multi_court_payload_rejected(runtime):
    app = importlib.import_module('app')
    with pytest.raises(Exception):
        app.ConfigIn(court_no=[3, 4])


def test_confirm_order_distinguishes_query_failure(runtime):
    async def run():
        e = runtime.Engine()
        court = e.config.courts[0]
        date = '2026-12-01'
        e.refresh_orders = AsyncMock(return_value={'ok': False, 'msg': '网络异常'})
        assert await e._confirm_order(date, court, '19:00', '21:00') == 'unknown'

        e.refresh_orders = AsyncMock(return_value={'ok': True, 'msg': '',
                                                   'orders': []})
        e._orders = [{'date': date, 'start': '19:00', 'end': '21:00',
                      'court': '3号场地', 'court_id': ''}]
        assert await e._confirm_order(date, court, '19:00', '21:00') == 'created'

        e._orders = []
        assert await e._confirm_order(date, court, '19:00', '21:00') == 'none'
    asyncio.run(run())


def test_order_matches_accepts_ground_id(runtime):
    e = runtime.Engine()
    court = e.config.courts[0]
    base = {'date': '2026-12-01', 'start': '19:00', 'end': '21:00'}
    assert e._order_matches({**base, 'court': '其他名称',
                             'court_id': court.id}, '2026-12-01', court,
                            '19:00', '21:00')
    assert e._order_matches({**base, 'court': '3号场地',
                             'court_id': ''}, '2026-12-01', court,
                            '19:00', '21:00')


def test_reserve_rejected_for_active_task_but_not_history(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()

        async def blocked():
            await asyncio.Event().wait()

        e.jobs.start(('2026-12-01', 3, '19:00', '21:00'), blocked())
        result = await e.reserve(3, '18:00', '19:00', '2026-12-01')
        assert result['accepted'] is False
        assert '提交' in result['msg']
        await e.jobs.stop_all()

        async def save_order(**kwargs):
            return {'success': True, 'msg': 'ok'}

        monkeypatch.setattr(runtime, 'save_order_once', save_order)
        e.leak_found = {'result': 'pending'}
        result = await e.reserve(3, '19:00', '21:00', '2026-12-01')
        assert result['accepted'] is True
        await asyncio.sleep(0.02)
    asyncio.run(run())


def test_stop_leak_cancels_pending_and_allows_manual(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        date, _ = _prepare_leak(e, monkeypatch)
        e.tuning.leak_interval = 1.0
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def blocking_save(**kwargs):
            calls.append(kwargs)
            started.set()
            await release.wait()
            return {'success': True, 'msg': 'ok'}

        monkeypatch.setattr(runtime, 'save_order_once', blocking_save)
        e.refresh_orders = AsyncMock(return_value={'ok': True, 'msg': '',
                                                   'orders': []})
        assert (await e.start_leak())['ok']
        await asyncio.wait_for(started.wait(), 2)
        assert e.leak_found['result'] == 'pending'

        await e.stop_leak()
        assert e.leak_found['result'] in ('cancelled', 'uncertain')
        assert e._leak_order_intent is False
        assert not e.jobs.running('leak')

        async def quick_save(**kwargs):
            return {'success': True, 'msg': 'ok'}

        monkeypatch.setattr(runtime, 'save_order_once', quick_save)
        result = await e.reserve(3, '18:00', '19:00', date)
        assert result['accepted'] is True
        await asyncio.sleep(0.02)
        release.set()
    asyncio.run(run())


def test_leak_uncertain_query_failure_never_resubmits(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        date, _ = _prepare_leak(e, monkeypatch)
        calls = []

        async def save_order(**kwargs):
            calls.append(kwargs)
            return {'success': False, 'msg': 'timeout', 'uncertain': True}

        monkeypatch.setattr(runtime, 'save_order_once', save_order)
        refreshes = 0

        async def refresh_orders():
            nonlocal refreshes
            refreshes += 1
            if refreshes == 1:
                return {'ok': True, 'msg': '', 'orders': []}
            return {'ok': False, 'msg': '网络异常', 'orders': []}

        e.refresh_orders = refresh_orders
        assert (await e.start_leak())['ok']
        for _ in range(200):
            if e.leak_found and e.leak_found.get('result') == 'uncertain':
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError('检漏未保留未确认状态')

        await asyncio.sleep(0.1)
        assert len(calls) == 1
        assert e._uncertain_order is not None
        result = await e.reserve(3, '18:00', '19:00', date)
        assert result['accepted'] is False
        assert '未确认' in result['msg']
        await e.stop_leak()
    asyncio.run(run())


def test_manual_order_blocks_auto_submission(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        _auto_engine(e, runtime, '20:00:04', court_no=3)
        e._parse_hms = lambda value: datetime.now() - timedelta(seconds=1)
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def save_order(**kwargs):
            calls.append(kwargs)
            started.set()
            await release.wait()
            return {'success': True, 'msg': 'ok'}

        monkeypatch.setattr(runtime, 'save_order_once', save_order)
        manual = await e.reserve(3, '19:00', '21:00', e.target_date)
        assert manual['accepted'] is True
        await asyncio.wait_for(started.wait(), 2)

        assert (await e.start_auto())['ok']
        await _run_job(e, 'auto')
        assert len(calls) == 1
        entry = e.auto.applied[0]
        assert entry['ok'] is False
        assert entry['sent_at'] == ''
        assert entry['returned_at'] == ''
        assert entry['confirmed_at'] == ''

        release.set()
        await asyncio.sleep(0.02)
    asyncio.run(run())


def test_leak_order_blocks_auto_submission(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        _prepare_leak(e, monkeypatch)
        _auto_engine(e, runtime, '20:00:04', court_no=3)
        e._parse_hms = lambda value: datetime.now() - timedelta(seconds=1)
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def save_order(**kwargs):
            calls.append(kwargs)
            started.set()
            await release.wait()
            return {'success': True, 'msg': 'ok'}

        monkeypatch.setattr(runtime, 'save_order_once', save_order)
        assert (await e.start_leak())['ok']
        await asyncio.wait_for(started.wait(), 2)
        assert e.leak_found['result'] == 'pending'

        assert (await e.start_auto())['ok']
        await _run_job(e, 'auto')
        assert len(calls) == 1
        assert e.auto.applied and e.auto.applied[0]['ok'] is False

        await e.stop_leak()
        release.set()
        await asyncio.sleep(0.02)
    asyncio.run(run())


def test_uncertain_confirmation_preserves_http_times(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        _prepare_leak(e, monkeypatch)
        sent_at = '2026-11-26 20:16:32.130'
        returned_at = '2026-11-26 20:16:32.456'

        async def save_order(**kwargs):
            return {'success': False, 'msg': 'timeout', 'uncertain': True,
                    'sent_at': sent_at, 'returned_at': returned_at,
                    'elapsed_ms': 326}

        monkeypatch.setattr(runtime, 'save_order_once', save_order)
        e._confirm_order = AsyncMock(return_value='created')
        assert (await e.start_leak())['ok']
        await _run_job(e, 'leak')

        record = e.leak_found
        assert record['result'] == 'ok'
        assert record['sent_at'] == sent_at
        assert record['returned_at'] == returned_at
        assert record['confirmed_at']
        assert record['confirmed_at'] not in (sent_at, returned_at)
        assert e._uncertain_order is None
    asyncio.run(run())


def test_manual_uncertain_blocks_repeat_until_order_check(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        date = '2026-12-01'

        async def save_order(**kwargs):
            return {'success': False, 'msg': 'timeout', 'uncertain': True,
                    'sent_at': '2026-12-01 20:00:00.100',
                    'returned_at': '2026-12-01 20:00:00.400'}

        monkeypatch.setattr(runtime, 'save_order_once', save_order)
        first = await e.reserve(3, '19:00', '21:00', date)
        assert first['accepted'] is True
        await asyncio.sleep(0.02)
        assert e._uncertain_order is not None

        e.refresh_orders = AsyncMock(return_value={'ok': False, 'msg': '网络异常',
                                                   'orders': []})
        blocked = await e.reserve(3, '18:00', '19:00', date)
        assert blocked['accepted'] is False
        assert '未确认' in blocked['msg']

        e.refresh_orders = AsyncMock(return_value={'ok': True, 'msg': '',
                                                   'orders': []})
        allowed = await e.reserve(3, '18:00', '19:00', date)
        assert allowed['accepted'] is True
        await asyncio.sleep(0.02)
    asyncio.run(run())


def test_leak_resolves_uncertain_order_on_later_query(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        date, court = _prepare_leak(e, monkeypatch)
        record = {
            'found_at': runtime.format_stamp(), 'court': court.no,
            'name': court.name,
            'date': date, 'start': '19:00', 'end': '21:00',
            'sent_at': '2026-11-26 20:16:32.130',
            'returned_at': '2026-11-26 20:16:32.456',
            'confirmed_at': '', 'result': 'uncertain', 'elapsed_ms': 0,
            'booked': False,
        }
        e.leak_found = record
        e._uncertain_order = {
            'scope': 'leak', 'date': date, 'court': court,
            'start': '19:00', 'end': '21:00',
            'attempt_at': datetime.now() - timedelta(milliseconds=300),
            'record': record,
        }

        async def refresh_orders():
            e._orders = [{'date': date, 'start': '19:00', 'end': '21:00',
                          'court': court.name, 'court_id': court.id,
                          'gym_id': ''}]
            e._orders_error = ''
            return {'ok': True, 'msg': '', 'orders': list(e._orders)}

        e.refresh_orders = refresh_orders
        start_calls = []

        async def save_order(**kwargs):
            start_calls.append(kwargs)
            return {'success': True, 'msg': 'ok'}

        monkeypatch.setattr(runtime, 'save_order_once', save_order)
        assert (await e.start_leak())['ok']
        await _run_job(e, 'leak')

        assert start_calls == []
        assert record['result'] == 'ok'
        assert record['booked'] is True
        assert record['sent_at'] == '2026-11-26 20:16:32.130'
        assert record['returned_at'] == '2026-11-26 20:16:32.456'
        assert record['confirmed_at']
        assert record['confirmed_at'] != record['returned_at']
        assert e._uncertain_order is None
        assert '预约成功' in e._leak_msg
    asyncio.run(run())


def test_manual_rejected_while_leak_order_inflight(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        date, _ = _prepare_leak(e, monkeypatch)
        started = asyncio.Event()
        release = asyncio.Event()

        async def save_order(**kwargs):
            started.set()
            await release.wait()
            return {'success': True, 'msg': 'ok'}

        monkeypatch.setattr(runtime, 'save_order_once', save_order)
        assert (await e.start_leak())['ok']
        await asyncio.wait_for(started.wait(), 2)

        result = await e.reserve(3, '19:00', '21:00', date)
        assert result['accepted'] is False
        assert '提交' in result['msg']

        await e.stop_leak()
        release.set()
    asyncio.run(run())


def test_leak_recheck_handles_foreign_uncertain_order(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        date, court = _prepare_leak(e, monkeypatch)
        e._leak_slot = lambda d, avail: None
        e._refresh_one = AsyncMock(return_value=(court, {'ok': True, 'avail': {}}))
        e._uncertain_order = {
            'scope': 'manual', 'date': date, 'court': court,
            'start': '19:00', 'end': '21:00',
            'attempt_at': datetime.now(), 'record': None,
        }

        async def refresh_orders():
            e._orders = [{'date': date, 'start': '19:00', 'end': '21:00',
                          'court': court.name, 'court_id': court.id,
                          'gym_id': ''}]
            e._orders_error = ''
            return {'ok': True, 'msg': '', 'orders': list(e._orders)}

        e.refresh_orders = refresh_orders
        assert (await e.start_leak())['ok']
        for _ in range(200):
            if e._uncertain_order is None:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError('检漏未清除其他模式的未确认订单')

        assert e.jobs.running('leak')
        assert e.leak_found is None
        await e.stop_leak()
    asyncio.run(run())


def test_manual_resolution_uses_captured_uncertain_target(runtime, monkeypatch):
    async def run():
        e = runtime.Engine()
        date = '2026-12-01'
        court = e.config.courts[0]
        e._uncertain_order = {
            'scope': 'auto', 'date': date, 'court': court,
            'start': '19:00', 'end': '21:00',
            'attempt_at': datetime.now(), 'record': None,
        }

        async def refresh_orders():
            e._orders = [{'date': date, 'start': '19:00', 'end': '21:00',
                          'court': court.name, 'court_id': court.id,
                          'gym_id': ''}]
            e._orders_error = ''
            e._uncertain_order = None
            return {'ok': True, 'msg': '', 'orders': list(e._orders)}

        e.refresh_orders = refresh_orders
        result = await e.reserve(3, '19:00', '21:00', date)
        assert result['accepted'] is False
        assert '已确认' in result['msg']
    asyncio.run(run())
