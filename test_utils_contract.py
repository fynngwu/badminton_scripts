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


# ══════════════════════════════════════════════════════
# refresh_window：Windows 窗口操作（伪造 Win32/UIA 边界）
# ══════════════════════════════════════════════════════
class _FakeWindows:
    """记录 refresh_window 对外部世界做了什么，不模拟真实窗口行为。"""

    def __init__(self, *, windows, foreground, nav_count=1, gym_search=True):
        self.windows = list(windows)
        self.foreground = foreground
        self.iconic = False
        self.nav_count = nav_count
        self.gym_search = gym_search
        self.lose_focus_on_click = False
        self.enumerate_count = 0
        self.sent_keys = []
        self.clicks = []
        self.sleeps = []
        self.lookups = []
        self.minimized = []
        self.cursor = (-1, -1)


def _install_windows_fakes(monkeypatch, *, windows=(1234,), foreground=1234,
                           nav_count=1, gym_search=True):
    state = _FakeWindows(windows=windows, foreground=foreground,
                         nav_count=nav_count, gym_search=gym_search)

    class NoPatternInterfaceError(Exception):
        pass

    class FakeElement:
        def __init__(self, name, control_type):
            self.name = name
            self.control_type = control_type
            self.enabled = True
            self.visible = True
            self.element = object()

        @property
        def rectangle(self):
            return types.SimpleNamespace(left=10, top=20, right=110, bottom=60)

    class FakeUIAElementInfo:
        def __init__(self, handle=None, *_args, **_kwargs):
            self.handle = handle

        def descendants(self, **kwargs):
            state.lookups.append(kwargs)
            title = kwargs.get('title')
            control_type = kwargs.get('control_type')
            if title == '场地预约' and control_type == 'Text':
                return [FakeElement(title, control_type)
                        for _ in range(state.nav_count)]
            if title == '请输入搜索的场馆' and control_type == 'Edit':
                return [FakeElement(title, control_type)] if state.gym_search else []
            return []

    win32con = types.ModuleType('win32con')
    win32con.SW_RESTORE = 9
    win32con.SW_SHOW = 5
    win32con.SW_MINIMIZE = 6
    win32con.MOUSEEVENTF_LEFTDOWN = 2
    win32con.MOUSEEVENTF_LEFTUP = 4

    def titled(hwnd):
        return hwnd in state.windows

    def show_window(hwnd, command):
        if command == win32con.SW_MINIMIZE:
            state.iconic = True
            state.minimized.append(hwnd)
        elif command == win32con.SW_RESTORE:
            state.iconic = False

    def enum_windows(callback, param):
        state.enumerate_count += 1
        for hwnd in state.windows:
            callback(hwnd, param)
        return True

    win32gui = types.ModuleType('win32gui')
    win32gui.IsWindow = titled
    win32gui.GetWindowText = lambda hwnd: (
        'https://reservation.sustech.edu.cn/venue' if titled(hwnd) else 'other')
    win32gui.EnumWindows = enum_windows
    win32gui.IsIconic = lambda hwnd: state.iconic
    win32gui.GetForegroundWindow = lambda: state.foreground
    win32gui.ShowWindow = show_window
    win32gui.SetForegroundWindow = lambda hwnd: None

    win32api = types.ModuleType('win32api')

    def set_cursor_pos(pos):
        state.cursor = pos
        state.clicks.append(('move', pos))

    win32api.SetCursorPos = set_cursor_pos
    win32api.GetCursorPos = lambda: state.cursor

    def mouse_event(*args):
        state.clicks.append(('mouse', args))
        if state.lose_focus_on_click:
            state.foreground = 999

    win32api.mouse_event = mouse_event

    comtypes = types.ModuleType('comtypes')
    comtypes.CoInitializeEx = lambda: None
    comtypes.CoUninitialize = lambda: None

    uia_defines = types.ModuleType('pywinauto.uia_defines')
    uia_defines.NoPatternInterfaceError = NoPatternInterfaceError
    uia_defines.get_elem_interface = lambda element, name: object()
    uia_element_info = types.ModuleType('pywinauto.uia_element_info')
    uia_element_info.UIAElementInfo = FakeUIAElementInfo
    keyboard = types.ModuleType('pywinauto.keyboard')
    keyboard.send_keys = lambda *args, **kwargs: state.sent_keys.append((args, kwargs))
    pywinauto = types.ModuleType('pywinauto')
    pywinauto.keyboard = keyboard
    pywinauto.uia_defines = uia_defines
    pywinauto.uia_element_info = uia_element_info

    for name, module in (
        ('win32con', win32con), ('win32gui', win32gui), ('win32api', win32api),
        ('comtypes', comtypes), ('pywinauto', pywinauto),
        ('pywinauto.keyboard', keyboard), ('pywinauto.uia_defines', uia_defines),
        ('pywinauto.uia_element_info', uia_element_info),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr('time.sleep', lambda seconds: state.sleeps.append(seconds))
    return state


def test_refresh_window_reuses_cached_hwnd(monkeypatch):
    utils = _load_utils(monkeypatch)
    state = _install_windows_fakes(monkeypatch)

    assert utils.refresh_window() is True
    assert state.enumerate_count == 1
    assert state.sent_keys and state.sent_keys[0][0] == ('^r',)
    assert state.minimized == [1234]
    assert state.sleeps == []
    assert state.lookups and all(lookup.get('title') for lookup in state.lookups)

    # 第二次直接复用已缓存的 HWND，不再全桌面枚举。
    assert utils.refresh_window() is True
    assert state.enumerate_count == 1


def test_refresh_window_reenumerates_when_cached_hwnd_is_gone(monkeypatch):
    utils = _load_utils(monkeypatch)
    state = _install_windows_fakes(monkeypatch)

    assert utils.refresh_window() is True
    state.windows = [4321]
    state.foreground = 4321

    assert utils.refresh_window() is True
    assert state.enumerate_count == 2
    assert state.sent_keys[-1][0] == ('^r',)


def test_refresh_window_refuses_ambiguous_windows(monkeypatch):
    utils = _load_utils(monkeypatch)
    state = _install_windows_fakes(monkeypatch, windows=(1234, 5678), foreground=1234)

    assert utils.refresh_window() is False
    assert state.clicks == []
    assert state.sent_keys == []


def test_refresh_window_requires_foreground_before_click(monkeypatch):
    utils = _load_utils(monkeypatch)
    state = _install_windows_fakes(monkeypatch, foreground=999)

    assert utils.refresh_window(fg_timeout=0.0) is False
    assert state.clicks == []
    assert state.sent_keys == []
    # 失败也要把窗口恢复成最小化。
    assert state.minimized == [1234]


def test_refresh_window_rechecks_foreground_before_hotkey(monkeypatch):
    utils = _load_utils(monkeypatch)
    state = _install_windows_fakes(monkeypatch)
    state.lose_focus_on_click = True

    assert utils.refresh_window(fg_timeout=0.0) is False
    assert state.clicks, '导航点击应已发生'
    assert state.sent_keys == []
    assert state.minimized == [1234]
