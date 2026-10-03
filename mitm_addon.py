"""mitm_addon.py — mitmproxy 入口。

正常运行：
- request: 捕获并更新 token。

首次部署：
- response: 捕获 getUserInfoByUserid 返回值，
  自动写入 user.id / customer_id / name / tel / token。

手工测试窗口刷新：
    python mitm_addon.py refresh
"""
from __future__ import annotations

import json
import re
import sys
import time

from utils import CONFIG_PATH, capture_token_from_flow


PROFILE_API = "/api/blade-app/qywx/getUserInfoByUserid"
PROFILE_HOST = "reservation.sustech.edu.cn"


def request(flow) -> None:  # type: ignore[no-untyped-def]
    capture_token_from_flow(flow)


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


def response(flow) -> None:  # type: ignore[no-untyped-def]
    """首次部署时，从当前登录用户资料接口自动填写 config.toml。"""
    try:
        if flow.request.pretty_host != PROFILE_HOST:
            return

        path = flow.request.path.split("?", 1)[0]
        if path != PROFILE_API:
            return

        if not flow.response or flow.response.status_code != 200:
            return

        try:
            payload = flow.response.json()
        except Exception:
            payload = json.loads(flow.response.get_text())

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

        token = data.get("token")
        if token:
            text = _replace_in_section(text, "order", "token", str(token))

        tmp = CONFIG_PATH.with_suffix(".toml.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(CONFIG_PATH)

        preview = f"{str(token)[:8]}..." if token else "none"
        print(
            f"[{time.strftime('%H:%M:%S')}] [PROFILE] "
            f"自动配置成功 id={data['code']} token={preview}"
        )

    except Exception as e:
        print(f"[PROFILE] 自动配置失败: {e}", file=sys.stderr)


if __name__ == "__main__":
    from utils import refresh_window

    if len(sys.argv) > 1 and sys.argv[1] == "refresh":
        refresh_window()
    else:
        print("用法：")
        print("  python mitm_addon.py refresh")
