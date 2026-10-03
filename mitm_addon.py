"""mitm_addon.py — mitmproxy 入口壳

    唯一的职责：把 reservation 请求交给 utils.capture_token_from_flow()。
    窗口枚举 / Ctrl+R / 鼠标点击等全部在 utils.refresh_window() 里。

手工调试：
    python mitm_addon.py refresh
"""
from __future__ import annotations

import sys


try:
    from utils import capture_token_from_flow
except Exception as e:
    print(f"[mitm_addon] 无法导入 utils: {e}", file=sys.stderr)

    def capture_token_from_flow(flow) -> None:  # type: ignore
        pass


def request(flow) -> None:  # type: ignore[no-untyped-def]
    capture_token_from_flow(flow)


if __name__ == "__main__":
    from utils import refresh_window
    if len(sys.argv) > 1 and sys.argv[1] == "refresh":
        refresh_window()
    else:
        print("用法：")
        print("  python mitm_addon.py refresh   # 手动触发一次 Ctrl+R")