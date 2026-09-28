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
    """每当 reservation 域名的请求带 token 查询参数时：

       - token 与 config.toml 当前值不同 → 写入，打印 [TOKEN] NEW old -> new
       - token 与 config.toml 当前值相同 → 只打印 [TOKEN] SAME，不碰文件
         （避免制造假 mtime，让 app.py 的 mtime 监听误以为配置变化）
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

        m = re.search(r'(?m)^\s*token\s*=\s*"([^"]*)"', text)
        current = m.group(1) if m else ""

        path = flow.request.path.split("?", 1)[0]
        method = flow.request.method
        ts = time.strftime("%H:%M:%S")

        # 相同 token 只记录，不再修改 config.toml
        if token == current:
            print(
                f"[{ts}] [TOKEN] SAME "
                f"{token[:8]}... {method} {path}"
            )
            return

        new, n = re.subn(
            r'(?m)^(\s*token\s*=\s*)"[^"]*"',
            rf'\1"{token}"', text, count=1,
        )

        if n:
            CONFIG.write_text(new, encoding="utf-8")
            old_preview = (current[:8] + "...") if current else "空"
            print(
                f"[{ts}] [TOKEN] NEW "
                f"{old_preview} -> {token[:8]}... "
                f"{method} {path}"
            )
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


def _ensure_foreground(hwnd: int, timeout: float = 0.6) -> bool:
    """轮询直到 hwnd 真正成为前台窗口，或超时。

    先用温和的 SetForegroundWindow；被系统拒绝时用 AttachThreadInput
    绕过前台锁；每轮都真正检查 GetForegroundWindow()，而不是盲等。
    """
    import win32gui
    import win32process
    import win32api

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            if win32gui.GetForegroundWindow() == hwnd:
                return True
        except Exception:
            pass

        # 温和路径
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        try:
            if win32gui.GetForegroundWindow() == hwnd:
                return True
        except Exception:
            pass

        # 强攻路径
        try:
            fg = win32gui.GetForegroundWindow()
            if fg and fg != hwnd:
                fg_tid = win32process.GetWindowThreadProcessId(fg)[0]
                my_tid = win32api.GetCurrentThreadId()
                if fg_tid != my_tid:
                    win32process.AttachThreadInput(fg_tid, my_tid, True)
                    try:
                        win32gui.BringWindowToTop(hwnd)
                        win32gui.SetForegroundWindow(hwnd)
                    finally:
                        win32process.AttachThreadInput(fg_tid, my_tid, False)
        except Exception:
            pass

        try:
            if win32gui.GetForegroundWindow() == hwnd:
                return True
        except Exception:
            pass

        if time.monotonic() >= deadline:
            return False
        time.sleep(0.03)


def _mouse_click(x: int, y: int) -> None:
    """在屏幕坐标 (x, y) 处按下+抬起左键。

    移动 → 停 30ms 让系统把光标位置同步给目标 → 按下 → 停 30ms → 抬起。
    一次点击里的这些微间隔能让 WebView / Chromium 稳定收到 mouse 事件。
    """
    import win32api
    import win32con

    win32api.SetCursorPos((x, y))
    time.sleep(0.03)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.03)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def _send_ctrl_r() -> None:
    """发一次 Ctrl+R。每个按键之间留 20ms，避免 WebView 丢键。"""
    import win32api
    import win32con

    VK_R = 0x52
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    time.sleep(0.02)
    win32api.keybd_event(VK_R, 0, 0, 0)
    time.sleep(0.02)
    win32api.keybd_event(VK_R, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.02)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)


def refresh_reservation_window(
    pre_click_delay: float = 0.10,
    post_click_delay: float = 0.10,
    fg_timeout: float = 0.6,
    double_click: bool = True,
) -> bool:
    """找到 reservation 窗口 → 让它真正上前台 → 点击 → Ctrl+R。

    时序参数由 app.py 从 config.toml [tuning] 传入；fg_timeout / double_click
    保留默认值即可，一般不需要暴露到 config。

    关键修复：
      * 用 _ensure_foreground() 轮询确认窗口真的上前台（而非设一次就赌）
      * Windows 首次点击常被吞掉（只用于激活窗口），因此默认点两次
      * Ctrl+R 之前再确认一次前台；掉前台就补一次激活+点击
      * 鼠标移动/按下/抬起之间、以及 Ctrl+R 各键之间留小间隔
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

        # 最小化 → 恢复，并给系统一点时间完成状态切换
        try:
            if win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMINIMIZED:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.15)
        except Exception:
            pass

        # ① 轮询等窗口真的到前台
        if not _ensure_foreground(hwnd, timeout=fg_timeout):
            print("[refresh] ⚠ 未能在前台锁定窗口，仍然尝试点击…")

        if pre_click_delay > 0:
            time.sleep(pre_click_delay)

        # ② 计算点击点：水平居中、垂直取窗口顶部往下 20%（避开标题栏/工具栏）
        cx = cy = None
        try:
            rect = win32gui.GetWindowRect(hwnd)
            w = rect[2] - rect[0]
            h = rect[3] - rect[1]
            cx = rect[0] + w // 2
            cy = rect[1] + max(100, int(h * 0.20))
        except Exception as e:
            print(f"[refresh] 计算点击坐标失败（忽略）: {e}")

        # ③ 点击：默认两次，第一次激活 WebView，第二次真正落到页面上
        if cx is not None and cy is not None:
            try:
                _mouse_click(cx, cy)
                if double_click:
                    time.sleep(0.08)
                    _mouse_click(cx, cy)
            except Exception as e:
                print(f"[refresh] click 失败（忽略）: {e}")

        if post_click_delay > 0:
            time.sleep(post_click_delay)

        # ④ Ctrl+R 之前再确认一次前台；掉前台就补一枪
        if not _ensure_foreground(hwnd, timeout=0.4):
            print("[refresh] ⚠ 发送 Ctrl+R 前窗口不在前台，仍尝试发送…")
            # 补一次：激活 + 单击
            if cx is not None and cy is not None:
                try:
                    _mouse_click(cx, cy)
                    time.sleep(0.08)
                except Exception:
                    pass

        # ⑤ 发 Ctrl+R
        _send_ctrl_r()
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