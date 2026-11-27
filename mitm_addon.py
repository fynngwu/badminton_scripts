"""mitm_addon.py — mitmproxy 入口。

- response: 捕获平台签发的 Token 与 getUserInfoByUserid 的用户资料，自动写入 config.toml。
- running:  启动时把实际生效的规则打到日志，判断「抓不到 Token」是否方向配错。

实机验证窗口刷新：
    python mitm_addon.py refresh
结果写入 logs/debug.log，可直接用于诊断报告。
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time

from utils import CONFIG_PATH, RESERVATION_HOST, read_config_dict, write_config_string

log = logging.getLogger("court")


PROFILE_API = "/api/blade-app/qywx/getUserInfoByUserid"


def running() -> None:  # type: ignore[no-untyped-def]
    from mitmproxy import ctx

    mode = ctx.options.mode
    log.info("[mitm] 规则: mode=%s | 目标主机 %s", mode, RESERVATION_HOST)


def _payload(flow):  # type: ignore[no-untyped-def]
    try:
        return flow.response.json()
    except Exception:
        try:
            return json.loads(flow.response.get_text())
        except Exception:
            return None


def _issued_token(flow) -> str | None:  # type: ignore[no-untyped-def]
    if not flow.response or flow.response.status_code != 200:
        return None
    payload = _payload(flow)
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None
    data = payload.get("data")
    token = data.get("token") if isinstance(data, dict) else None
    return token if isinstance(token, str) and token else None


def _capture_issued_token(flow) -> None:  # type: ignore[no-untyped-def]
    try:
        if flow.request.pretty_host != RESERVATION_HOST:
            return
        token = _issued_token(flow)
        if not token:
            return

        if flow.request.query.get("token") is not None:
            return
        path = flow.request.path.split("?", 1)[0]
        stamp = time.strftime("%H:%M:%S")

        current = str(read_config_dict().get("order", {}).get("token", "") or "")
        if current == token:
            return
        write_config_string("token", token)
        print(f"[{stamp}] [TOKEN] 签发成功 {token[:8]}... {path}")
    except Exception as e:
        print(f"[TOKEN] 写入失败: {e}", file=sys.stderr)


def _replace_in_section(text: str, section: str, key: str, value: str) -> str:
    """只替换 TOML 指定 section 内的字符串字段。"""
    m = re.search(
        rf"(?ms)^\[{re.escape(section)}\]\s*$.*?(?=^\[|\Z)",
        text,
    )
    if not m:
        raise RuntimeError(f"config.toml 缺少 [{section}]")

    block = m.group(0)
    new_block, n = re.subn(
        rf'(?m)^(\s*{re.escape(key)}\s*=\s*)"[^"]*"',
        lambda x: f'{x.group(1)}"{value}"',
        block,
        count=1,
    )
    if not n:
        raise RuntimeError(f"config.toml 缺少 [{section}].{key}")

    return text[:m.start()] + new_block + text[m.end():]


def _capture_profile(flow) -> None:  # type: ignore[no-untyped-def]
    try:
        if flow.request.pretty_host != RESERVATION_HOST:
            return
        if flow.request.path.split("?", 1)[0] != PROFILE_API:
            return
        if not flow.response or flow.response.status_code != 200:
            return

        payload = _payload(flow)
        if not isinstance(payload, dict) or not payload.get("success"):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            return

        values = {
            ("user", "id"): data.get("code"),
            ("user", "customer_id"): data.get("id"),
            ("user", "name"): data.get("name"),
            ("user", "tel"): data.get("tel"),
        }
        missing = [f"{s}.{k}" for (s, k), v in values.items() if v in (None, "")]
        if missing:
            raise RuntimeError("用户资料缺少字段: " + ", ".join(missing))

        text = CONFIG_PATH.read_text(encoding="utf-8")
        for (section, key), value in values.items():
            text = _replace_in_section(text, section, key, str(value))

        tmp = CONFIG_PATH.with_suffix(".toml.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(CONFIG_PATH)

        token = data.get("token")
        preview = f"{str(token)[:8]}..." if token else "none"
        print(
            f"[{time.strftime('%H:%M:%S')}] [PROFILE] "
            f"自动配置成功 id={data['code']} token={preview}"
        )
    except Exception as e:
        print(f"[PROFILE] 自动配置失败: {e}", file=sys.stderr)


def response(flow) -> None:  # type: ignore[no-untyped-def]
    _capture_issued_token(flow)
    _capture_profile(flow)


def _self_check() -> int:
    """实机自检：窗口刷新 + 3 秒内是否真的换发了新 Token。

    「已发出 Ctrl+R」不代表页面真的刷新了，只有拿到不同的新 Token 才算成功。
    """
    from utils import ROOT, refresh_window

    (ROOT / "logs").mkdir(exist_ok=True)
    handler = logging.FileHandler(ROOT / "logs" / "debug.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)

    def current_token() -> str:
        try:
            return str(read_config_dict().get("order", {}).get("token", "") or "")
        except Exception:
            return ""

    old = current_token()
    log.info("[自检] 开始窗口刷新（当前 Token=%s）", (old[:8] + "...") if old else "无")
    started = time.monotonic()
    sent = refresh_window()
    window_ms = (time.monotonic() - started) * 1000
    if not sent:
        log.warning("[自检] 结果=失败（窗口操作未走到发送 Ctrl+R，耗时 %.0fms）", window_ms)
        print("❌ 窗口刷新失败：未走到发送 Ctrl+R，失败原因见 logs/debug.log")
        return 1

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        token = current_token()
        if token and token != old:
            capture_ms = (time.monotonic() - started - window_ms / 1000) * 1000
            log.info("[自检] 结果=成功（窗口操作 %.0fms，刷新后捕获 %.0fms，合计 %.0fms）",
                     window_ms, capture_ms, window_ms + capture_ms)
            print(f"✅ 窗口已刷新并换发新 Token（窗口操作 {window_ms:.0f}ms，"
                  f"捕获 {capture_ms:.0f}ms）")
            return 0
        time.sleep(0.1)

    log.warning("[自检] 结果=失败（已发送 Ctrl+R，但 3s 内未换发新 Token；"
                "窗口操作 %.0fms）", window_ms)
    print("❌ 已发送 Ctrl+R，但 3 秒内没有新 Token：刷新可能没有生效，见 logs/debug.log")
    return 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "refresh":
        sys.exit(_self_check())
    print("用法：")
    print("  python mitm_addon.py refresh")
