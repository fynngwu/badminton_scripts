"""mitm_addon.py — mitmproxy addon：抓 reservation 请求里的 token 写入 config.toml

推荐部署方式（Clash/Mihomo 单域名转发 + mitmdump 常驻）：

    1) Clash 里把 reservation.sustech.edu.cn 的流量转发到 127.0.0.1:8080
       （只这一个域名，其它域名走原规则）

           企业微信 → Clash ──(其它)──→ 原规则
                        │
                        └──(reservation)──→ mitmdump:8080 → 服务器

    2) 由 app.py 在服务启动时【常驻】拉起 mitmdump（无窗口）：
           mitmdump --listen-host 127.0.0.1 --listen-port 8080 -s mitm_addon.py
       app.py 退出时会自动回收这个 mitmdump。

    3) 前端点“🔑 自动获取 Token”只是：
           刷新预约窗口 → 等 config.toml 里 token 变化 → 返回

    ★ 本 addon 每次捕获到 token 都会重写 config.toml（即使内容与上次相同），
      以刷新 mtime，让 app.py 立刻收到“已抓到 token”的信号。

手工调试（可选）：
    mitmdump --listen-host 127.0.0.1 --listen-port 8080 -s mitm_addon.py
    python mitm_addon.py refresh     # 单独触发企业微信窗口 Ctrl+R

前置条件：
    ★ mitmproxy 的 CA 证书必须被企业微信（WebView）信任。
      否则握手会直接失败，addon 根本收不到 HTTP 请求。

      安装（当前用户）：
          certutil -addstore -user Root "%USERPROFILE%\\.mitmproxy\\mitmproxy-ca-cert.cer"
      或：
          certutil -addstore Root "%USERPROFILE%\\.mitmproxy\\mitmproxy-ca-cert.cer"

      装完彻底退出企业微信（托盘右键退出）再重开，让 WebView 重新加载证书存储。
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

try:
    from mitmproxy import http  # type: ignore
except Exception:               # pragma: no cover
    http = None                 # type: ignore

HOST = "reservation.sustech.edu.cn"
CONFIG = Path(__file__).parent / "config.toml"


# ══════════════════════════════════════════════════════════
# addon：抓 token
# ══════════════════════════════════════════════════════════
def request(flow) -> None:  # type: ignore[no-untyped-def]
    """每当有 reservation 域名的请求带 token 查询参数时，把它写进 config.toml。

    语义：'这次 Ctrl+R 是否抓到了一条有效的 token 请求'。
         不是 'token 内容是否与上次不同'。

    所以这里【不】与上次比较，只要拿到 token 就写；
    即使内容相同，write_text 也会刷新 mtime，
    让 app.py 的 mtime 监听立刻醒来（避免相同 token 造成的假超时）。
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
            rf'\1"{token}"',
            text, count=1,
        )
        if n:
            CONFIG.write_text(new, encoding="utf-8")
            print(
                f"[{time.strftime('%H:%M:%S')}] "
                f"[TOKEN] captured: {token[:8]}..."
            )
        else:
            print("[TOKEN] config.toml 中未找到 token 字段，无法写入")
    except Exception as e:
        print(f"[TOKEN] 写入失败: {e}")


# ══════════════════════════════════════════════════════════
# CLI / 外部调用：企业微信窗口刷新
# ══════════════════════════════════════════════════════════
def refresh_reservation_window() -> bool:
    """找到 reservation 窗口 → 点一下内部 → 发 Ctrl+R。"""
    try:
        from pywinauto import Desktop
    except Exception as e:
        print(f"[refresh] pywinauto 不可用: {e}")
        return False

    keywords = ("reservation.sustech.edu.cn", "reservation")

    try:
        windows = Desktop(backend="win32").windows()
    except Exception as e:
        print(f"[refresh] 枚举窗口失败: {e}")
        return False

    for w in windows:
        try:
            title = (w.window_text() or "").strip()
            if not title:
                continue
            if not any(k in title for k in keywords):
                continue

            print(f"[refresh] 找到预约窗口: {title}")
            try:
                w.restore()
            except Exception:
                pass
            try:
                w.set_focus()
            except Exception:
                pass

            time.sleep(0.5)

            try:
                rect = w.rectangle()
                cx = rect.width() // 2
                cy = max(80, rect.height() // 8)   # 避开标题栏，保证 webview 拿到焦点
                w.click_input(coords=(cx, cy))
            except Exception as e:
                print(f"[refresh] click_input 失败（忽略）: {e}")

            time.sleep(0.5)
            w.type_keys("^r")
            print("[refresh] 已发送 Ctrl+R")
            return True

        except Exception as e:
            print(f"[refresh] error: {e}")

    print("[refresh] 未找到预约页面")
    return False


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "refresh":
        refresh_reservation_window()
    else:
        print("用法：")
        print("  1) 常驻抓 token（由 app.py 自动拉起，一般不用手跑）：")
        print("     mitmdump --listen-host 127.0.0.1 --listen-port 8080 -s mitm_addon.py")
        print("  2) 单独触发企业微信窗口 Ctrl+R：")
        print("     python mitm_addon.py refresh")