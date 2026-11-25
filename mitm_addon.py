"""mitm_addon.py — mitmproxy 入口。

正常运行：
- response: 只有平台签发响应能把新凭证写进 config.toml。

首次部署：
- response: 捕获 getUserInfoByUserid 返回值，自动写入 user.id / customer_id / name / tel。

手工测试窗口刷新：
    python mitm_addon.py refresh
"""
from __future__ import annotations

import json
import re
import sys
import time

from utils import CONFIG_PATH, RESERVATION_HOST, read_config_dict, write_config_string


PROFILE_API = "/api/blade-app/qywx/getUserInfoByUserid"


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

        path = flow.request.path.split("?", 1)[0]
        stamp = time.strftime("%H:%M:%S")
        if flow.request.query.get("token") is not None:
            print(f"[{stamp}] [TOKEN] 忽略响应凭证 {token[:8]}... {path}")
            return

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


if __name__ == "__main__":
    from utils import refresh_window

    if len(sys.argv) > 1 and sys.argv[1] == "refresh":
        refresh_window()
    else:
        print("用法：")
        print("  python mitm_addon.py refresh")
