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

    def __init__(self, *, windows, foreground, nav_invokable=(True,),
                 gym_search=True, iconic=False, focus_works=True):
        self.windows = list(windows)
        self.foreground = foreground
        self.iconic = iconic
        self.focus_works = focus_works
        self.nav_invokable = list(nav_invokable)
        self.gym_search = gym_search
        self.lose_focus_on_click = False
        self.enumerate_count = 0
        self.focus_calls = 0
        self.sent_keys = []
        self.clicks = []
        self.sleeps = []
        self.lookups = []
        self.minimized = []
        self.restored = []
        self.events = []
        self.cursor = (-1, -1)
        self.clicked = False
        self.fail_ready_once = False
        self.raise_on_click = False
        self.clock = 1000.0


def _install_windows_fakes(monkeypatch, *, windows=(1234,), foreground=1234,
                           nav_invokable=(True,), gym_search=True, iconic=False,
                           focus_works=True):
    state = _FakeWindows(windows=windows, foreground=foreground,
                         nav_invokable=nav_invokable, gym_search=gym_search,
                         iconic=iconic, focus_works=focus_works)

    class FakeElement:
        def __init__(self, name, control_type, *, invokable=True, index=0):
            self.name = name
            self.control_type = control_type
            self.enabled = True
            self.visible = True
            self.invokable = invokable
            self.left = 10 + 100 * index
            self.element = self

        @property
        def rectangle(self):
            return types.SimpleNamespace(left=self.left, top=20,
                                         right=self.left + 100, bottom=60)

    class FakeWindowElement:
        def FindFirst(self, scope, condition):
            state.lookups.append(('first', dict(condition), state.clicked))
            if state.fail_ready_once:
                state.fail_ready_once = False
                raise FakeCOMError('UIA 树正在重建')
            if (condition.get('title') == '请输入搜索的场馆'
                    and condition.get('control_type') == 'Edit'
                    and state.gym_search):
                return object()
            return None

    class FakeUIAElementInfo:
        def __init__(self, handle=None, *_args, **_kwargs):
            self.handle = handle
            self.element = FakeWindowElement()

        def descendants(self, **kwargs):
            state.lookups.append(('all', kwargs, state.clicked))
            title = kwargs.get('title')
            control_type = kwargs.get('control_type')
            if title == '场地预约' and control_type == 'Text':
                return [FakeElement(title, control_type, invokable=flag, index=i)
                        for i, flag in enumerate(state.nav_invokable)]
            return []

    class FakeIUIA:
        tree_scope = {'descendants': 'descendants'}

        def build_condition(self, **kwargs):
            return dict(kwargs)

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
            state.events.append(('minimize', hwnd))
        elif command == win32con.SW_RESTORE:
            state.iconic = False
            state.restored.append(hwnd)
            state.events.append(('restore', hwnd))

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
    # Windows 会拒绝后台进程直接置前，只有 set_focus 的输入事件能解除锁定。
    win32gui.SetForegroundWindow = lambda hwnd: None

    win32api = types.ModuleType('win32api')

    def set_cursor_pos(pos):
        state.cursor = pos
        state.clicks.append(('move', pos))

    win32api.SetCursorPos = set_cursor_pos
    win32api.GetCursorPos = lambda: state.cursor

    def mouse_event(*args):
        state.clicks.append(('mouse', args))
        if state.raise_on_click:
            raise RuntimeError('点击失败')
        state.clicked = True
        state.events.append(('click', None))
        if state.lose_focus_on_click:
            state.foreground = 999

    win32api.mouse_event = mouse_event

    class FakeHwndWrapper:
        def __init__(self, hwnd):
            self.handle = hwnd

        def set_focus(self):
            state.focus_calls += 1
            state.events.append(('focus', self.handle))
            if state.focus_works:
                state.foreground = self.handle
            return self

    class FakeCOMError(Exception):
        pass

    comtypes = types.ModuleType('comtypes')
    comtypes.COMError = FakeCOMError
    comtypes.CoInitializeEx = lambda: None
    comtypes.CoUninitialize = lambda: None

    class NoPatternInterfaceError(Exception):
        pass

    def get_elem_interface(element, name):
        if not element.invokable:
            raise NoPatternInterfaceError()
        return object()

    uia_defines = types.ModuleType('pywinauto.uia_defines')
    uia_defines.IUIA = FakeIUIA
    uia_defines.NoPatternInterfaceError = NoPatternInterfaceError
    uia_defines.get_elem_interface = get_elem_interface
    uia_element_info = types.ModuleType('pywinauto.uia_element_info')
    uia_element_info.UIAElementInfo = FakeUIAElementInfo
    keyboard = types.ModuleType('pywinauto.keyboard')

    def send_keys(*args, **kwargs):
        state.sent_keys.append((args, kwargs))
        state.events.append(('keys', None))

    keyboard.send_keys = send_keys
    hwndwrapper = types.ModuleType('pywinauto.controls.hwndwrapper')
    hwndwrapper.HwndWrapper = FakeHwndWrapper
    controls = types.ModuleType('pywinauto.controls')
    controls.hwndwrapper = hwndwrapper
    pywinauto = types.ModuleType('pywinauto')
    pywinauto.keyboard = keyboard
    pywinauto.controls = controls
    pywinauto.uia_defines = uia_defines
    pywinauto.uia_element_info = uia_element_info

    for name, module in (
        ('win32con', win32con), ('win32gui', win32gui), ('win32api', win32api),
        ('comtypes', comtypes), ('pywinauto', pywinauto),
        ('pywinauto.keyboard', keyboard), ('pywinauto.controls', controls),
        ('pywinauto.controls.hwndwrapper', hwndwrapper),
        ('pywinauto.uia_defines', uia_defines),
        ('pywinauto.uia_element_info', uia_element_info),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    def fake_sleep(seconds):
        state.sleeps.append(seconds)
        state.clock += seconds

    monkeypatch.setattr('time.sleep', fake_sleep)
    monkeypatch.setattr('time.monotonic', lambda: state.clock)
    return state


def test_refresh_window_reuses_cached_hwnd(monkeypatch):
    utils = _load_utils(monkeypatch)
    state = _install_windows_fakes(monkeypatch)

    assert utils.refresh_window() == 'sent'
    assert state.enumerate_count == 1
    assert state.sent_keys and state.sent_keys[0][0] == ('^r',)
    assert state.minimized == [1234]
    assert state.sleeps == []
    assert state.lookups and all(lookup[1].get('title') for lookup in state.lookups)

    # 第二次直接复用已缓存的 HWND，不再全桌面枚举。
    assert utils.refresh_window() == 'sent'
    assert state.enumerate_count == 1


def test_refresh_window_reenumerates_when_cached_hwnd_is_gone(monkeypatch):
    utils = _load_utils(monkeypatch)
    state = _install_windows_fakes(monkeypatch)

    assert utils.refresh_window() == 'sent'
    state.windows = [4321]
    state.foreground = 4321

    assert utils.refresh_window() == 'sent'
    assert state.enumerate_count == 2
    assert state.sent_keys[-1][0] == ('^r',)


def test_refresh_window_refuses_ambiguous_windows(monkeypatch):
    utils = _load_utils(monkeypatch)
    state = _install_windows_fakes(monkeypatch, windows=(1234, 5678), foreground=1234)

    assert utils.refresh_window() == 'idle'
    assert state.clicks == []
    assert state.sent_keys == []


def test_refresh_window_restores_background_minimized_window_focus(monkeypatch):
    utils = _load_utils(monkeypatch)
    state = _install_windows_fakes(monkeypatch, foreground=999, iconic=True)

    assert utils.refresh_window() == 'sent'
    assert state.restored == [1234]
    assert state.focus_calls == 1
    assert state.sent_keys
    kinds = [event[0] for event in state.events]
    assert kinds.index('focus') < kinds.index('click') < kinds.index('keys')
    assert state.minimized == [1234]


def test_refresh_window_does_not_send_hotkey_when_focus_never_arrives(monkeypatch):
    utils = _load_utils(monkeypatch)
    state = _install_windows_fakes(monkeypatch, foreground=999, focus_works=False)

    assert utils.refresh_window(fg_timeout=0.0) == 'idle'
    assert state.focus_calls >= 1
    assert state.clicks == []
    assert state.sent_keys == []
    assert state.minimized == [1234]


def test_refresh_window_stops_when_foreground_lost_after_click(monkeypatch):
    utils = _load_utils(monkeypatch)
    state = _install_windows_fakes(monkeypatch, foreground=1234,
                                   focus_works=False)
    state.lose_focus_on_click = True

    assert utils.refresh_window(fg_timeout=0.0) == 'clicked'
    assert state.clicks, '导航点击应已发生'
    assert state.sent_keys == []
    assert state.minimized == [1234]


def test_refresh_window_confirms_page_with_search_box_only(monkeypatch):
    utils = _load_utils(monkeypatch)
    state = _install_windows_fakes(monkeypatch)

    assert utils.refresh_window() == 'sent'
    nav_queries_after_click = [
        lookup for lookup in state.lookups
        if lookup[0] == 'all' and lookup[2]
    ]
    assert nav_queries_after_click == []
    ready_queries = [lookup[1] for lookup in state.lookups if lookup[0] == 'first']
    assert ready_queries
    assert all(q.get('title') == '请输入搜索的场馆' for q in ready_queries)


def test_refresh_window_retries_transient_uia_error(monkeypatch):
    utils = _load_utils(monkeypatch)
    state = _install_windows_fakes(monkeypatch)
    state.fail_ready_once = True

    assert utils.refresh_window() == 'sent'
    assert state.sent_keys
    ready_queries = [lookup for lookup in state.lookups if lookup[0] == 'first']
    assert len(ready_queries) == 2


def test_refresh_window_treats_mid_click_error_as_clicked(monkeypatch):
    utils = _load_utils(monkeypatch)
    state = _install_windows_fakes(monkeypatch)
    state.raise_on_click = True

    assert utils.refresh_window(fg_timeout=0.0) == 'clicked'
    assert state.sent_keys == []
    assert state.minimized == [1234]


def test_refresh_window_selects_only_invokable_navigation(monkeypatch):
    utils = _load_utils(monkeypatch)
    state = _install_windows_fakes(monkeypatch, nav_invokable=(False, True))

    assert utils.refresh_window() == 'sent'
    assert ('move', (160, 40)) in state.clicks
    assert state.sent_keys
    assert state.minimized == [1234]


def test_refresh_window_rejects_navigation_when_both_invokable(monkeypatch):
    utils = _load_utils(monkeypatch)
    state = _install_windows_fakes(monkeypatch, nav_invokable=(True, True))

    assert utils.refresh_window() == 'idle'
    assert state.clicks == []
    assert state.sent_keys == []
    assert state.minimized == [1234]


def test_refresh_window_fails_when_navigation_not_invokable(monkeypatch):
    utils = _load_utils(monkeypatch)
    state = _install_windows_fakes(monkeypatch, nav_invokable=(False,))

    assert utils.refresh_window() == 'idle'
    assert state.clicks == []
    assert state.sent_keys == []
    assert state.minimized == [1234]
