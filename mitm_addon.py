"""mitm_addon.py — mitmproxy addon：抓 reservation 请求里的 token 写入 config.toml

    ★ win32gui 窗口操作 + hwnd 缓存，<50ms
    ★ 点击 + Ctrl+R 一气呵成，前后保存/恢复鼠标位置
    ★ 时序参数由调用方传入（app.py 从 config.toml [tuning] 读取）

依赖：
    pip install pywin32

手工调试：
    python mitm_addon.py refresh
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

try:
    from mitmproxy import http  # type: ignore
except Exception:
    http = None  # type: ignore

HOST = "reservation.sustech.edu.cn"
CONFIG = Path(__file__).parent / "config.toml"
KEYWORDS = ("reservation.sustech.edu.cn", "reservation")

# 缓存找到的窗口句柄，避免每次全量枚举
_cached_hwnd: int = 0


# ══════════════════════════════════════════════════════════
# addon：抓 token
# ══════════════════════════════════════════════════════════
def request(flow) -> None:  # type: ignore[no-untyped-def]
    """每当 reservation 域名的请求带 token 查询参数时，写入 config.toml。

    不论 token 内容是否与上次相同都重写（刷新 mtime），
    让 app.py 的 mtime 监听立刻醒来。
    """
    if http is None:
        return
    try:
        if flow.request.pretty_host != HOST:
            return
    except Exception:
        return

    token = flow.request.query.get("token")
    if not token:
        return

    try:
        text = CONFIG.read_text(encoding="utf-8")
        new, n = re.subn(
            r'(?m)^(\s*token\s*=\s*)"[^"]*"',
            rf'\1"{token}"', text, count=1,
        )
        if n:
            CONFIG.write_text(new, encoding="utf-8")
            print(f"[{time.strftime('%H:%M:%S')}] [TOKEN] captured: {token[:8]}...")
        else:
            print("[TOKEN] config.toml 中未找到 token 字段，无法写入")
    except Exception as e:
        print(f"[TOKEN] 写入失败: {e}")


# ══════════════════════════════════════════════════════════
# win32 窗口操作
# ══════════════════════════════════════════════════════════
def _find_hwnd() -> int:
    """枚举顶层窗口，找标题匹配 KEYWORDS 的 hwnd。"""
    import win32gui
    found: list[int] = []

    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd) or ""
            if title and any(k in title for k in KEYWORDS):
                found.append(hwnd)
        return True

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else 0


def _valid(hwnd: int) -> bool:
    """hwnd 是否仍然有效且标题匹配。"""
    if not hwnd:
        return False
    try:
        import win32gui
        if not win32gui.IsWindow(hwnd):
            return False
        title = win32gui.GetWindowText(hwnd) or ""
        return any(k in title for k in KEYWORDS)
    except Exception:
        return False


def _force_foreground(hwnd: int) -> None:
    """把窗口拉到前台。直接 SetForegroundWindow 被系统拒绝时，
    用 AttachThreadInput 绕过前台锁。"""
    import win32gui
    import win32process
    import win32api

    try:
        win32gui.SetForegroundWindow(hwnd)
        if win32gui.GetForegroundWindow() == hwnd:
            return
    except Exception:
        pass

    try:
        fg = win32gui.GetForegroundWindow()
        fg_tid = win32process.GetWindowThreadProcessId(fg)[0]
        my_tid = win32api.GetCurrentThreadId()
        win32process.AttachThreadInput(fg_tid, my_tid, True)
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        finally:
            win32process.AttachThreadInput(fg_tid, my_tid, False)
    except Exception:
        pass


def refresh_reservation_window(
    pre_click_delay: float = 0.05,
    post_click_delay: float = 0.05,
) -> bool:
    """找到 reservation 窗口 → 点一下内部 → 发 Ctrl+R。

    时序参数由 app.py 从 config.toml [tuning] 传入。
    """
    global _cached_hwnd

    try:
        import win32gui
        import win32con
        import win32api
    except ImportError:
        print("[refresh] 需要 pywin32：pip install pywin32")
        return False

    hwnd = _cached_hwnd if _valid(_cached_hwnd) else 0
    if not hwnd:
        hwnd = _find_hwnd()
        if hwnd:
            _cached_hwnd = hwnd

    if not hwnd:
        print("[refresh] 未找到预约窗口（企业微信是否已打开 reservation？）")
        _cached_hwnd = 0
        return False

    try:
        old_pos = win32api.GetCursorPos()
    except Exception:
        old_pos = None

    ok = False
    try:
        print(f"[refresh] 找到预约窗口: {win32gui.GetWindowText(hwnd)}")

        # 最小化 → 恢复
        try:
            if win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMINIMIZED:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        except Exception:
            pass

        _force_foreground(hwnd)

        # 把键盘焦点交给窗口，避免按键落到别处
        try:
            win32gui.SetFocus(hwnd)
        except Exception:
            pass

        if pre_click_delay > 0:
            time.sleep(pre_click_delay)

        # 点击窗口内部，让 WebView 拿到键盘
        try:
            rect = win32gui.GetWindowRect(hwnd)
            cx = (rect[0] + rect[2]) // 2
            cy = rect[1] + max(80, (rect[3] - rect[1]) // 8)
            win32api.SetCursorPos((cx, cy))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        except Exception as e:
            print(f"[refresh] click 失败（忽略）: {e}")

        if post_click_delay > 0:
            time.sleep(post_click_delay)

        # Ctrl+R
        VK_R = 0x52
        win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
        win32api.keybd_event(VK_R, 0, 0, 0)
        win32api.keybd_event(VK_R, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
        ok = True
    except Exception as e:
        print(f"[refresh] error: {e}")
        _cached_hwnd = 0
    finally:
        # 不管成败，鼠标位置都恢复，用户察觉不到
        if old_pos is not None:
            try:
                win32api.SetCursorPos(old_pos)
            except Exception:
                pass

    if ok:
        print("[refresh] 已发送 Ctrl+R")
    return ok


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "refresh":
        refresh_reservation_window()
    else:
        print("用法：")
        print("  python mitm_addon.py refresh   # 手动触发一次 Ctrl+R")