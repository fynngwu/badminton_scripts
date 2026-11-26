"""Contract tests for the thin utils layer, without pulling in CV/network deps."""
import asyncio
import importlib
import re
import sys
import types

import pytest


def _load_utils(monkeypatch):
    class FakeDB(dict):
        def __getitem__(self, key):
            return [1]

    numpy = types.ModuleType('numpy')
    numpy.load = lambda *args, **kwargs: FakeDB()
    cv2 = types.ModuleType('cv2')
    pillow = types.ModuleType('pillow')
    pillow.Image = object()
    curl_cffi = types.ModuleType('curl_cffi')
    curl_cffi.requests = types.SimpleNamespace(AsyncSession=object)
    monkeypatch.setitem(sys.modules, 'numpy', numpy)
    monkeypatch.setitem(sys.modules, 'cv2', cv2)
    monkeypatch.setitem(sys.modules, 'PIL', pillow)
    monkeypatch.setitem(sys.modules, 'PIL.Image', pillow.Image)
    monkeypatch.setitem(sys.modules, 'curl_cffi', curl_cffi)
    monkeypatch.delitem(sys.modules, 'utils', raising=False)
    return importlib.import_module('utils')


def test_write_config_int_renamed_migrates_old_multi_court(monkeypatch, tmp_path):
    utils = _load_utils(monkeypatch)
    config = tmp_path / 'config.toml'
    config.write_text('[auto]\ncourt_order = [3, 4]\n', encoding='utf-8')

    utils.write_config_int_renamed('court_no', 'court_order', 4, path=config)

    text = config.read_text(encoding='utf-8')
    assert 'court_no = 4' in text
    assert 'court_order' not in text


def test_write_config_int_renamed_replaces_existing_bad_value(monkeypatch, tmp_path):
    utils = _load_utils(monkeypatch)
    config = tmp_path / 'config.toml'
    config.write_text('[auto]\ncourt_no = "8"\n', encoding='utf-8')

    utils.write_config_int_renamed('court_no', 'court_order', 4, path=config)

    text = config.read_text(encoding='utf-8')
    assert text.count('court_no') == 1
    assert 'court_no = 4' in text


def test_write_config_int_renamed_inserts_missing_field(monkeypatch, tmp_path):
    utils = _load_utils(monkeypatch)
    config = tmp_path / 'config.toml'
    config.write_text('[auto]\nslot_start = "19:00"\n', encoding='utf-8')

    utils.write_config_int_renamed('court_no', 'court_order', 8, path=config)

    assert 'court_no = 8' in config.read_text(encoding='utf-8')


def test_classify_order_reply_marks_uncertain(monkeypatch):
    utils = _load_utils(monkeypatch)
    kind, label = utils.classify_order_reply({
        'success': False, 'uncertain': True, 'msg': 'timeout'})
    assert kind == 'uncertain'
    assert '不明确' in label


def test_save_order_timeout_is_uncertain(monkeypatch):
    utils = _load_utils(monkeypatch)

    class Session:
        async def post(self, *args, **kwargs):
            raise TimeoutError('timeout')

    monkeypatch.setattr(utils, 'get_cffi_session', lambda: Session())
    result = asyncio.run(utils.save_order_once(
        vid='vid', court=utils.Court(3, 'court-3', '3号场'),
        date='2026-12-01', start='19:00', end='21:00', token='token',
        user_id='user', customer_id='customer', customer_name='name',
        customer_tel='tel', gym_id='gym', gym_name='gym-name',
    ))
    assert result['success'] is False
    assert result['uncertain'] is True
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}',
                        result['sent_at'])
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}',
                        result['returned_at'])
    assert isinstance(result['elapsed_ms'], int)
