"""diagnostics.py — 诊断报告：采集 → 渲染 → gh 建 Issue。

    实际运行环境是 Windows，开发环境是 Linux，窗口操作和耗时瓶颈只能在真机上
    复现。本模块把「运行状态 + 各阶段耗时 + 可疑错误 + mitm 流量 + 近期日志」
    整理成一份 Markdown，回传给开发者定位。

    用法：
        前端   GET  /api/diagnostics/preview  预览（内容由浏览器保存）
               POST /api/diagnostics/upload   把确认过的 Markdown 原样建 Issue
        命令行 python diagnostics.py submit            打印报告到终端
               python diagnostics.py submit --upload   并创建 Issue

    只采集白名单字段：不上传 config.toml 全文、请求体与逐条请求参数。
"""
from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_FILE = ROOT / "logs" / "debug.log"

LOG_WINDOW_MIN = 10          # 只看最近 10 分钟的日志
MAX_LOG_LINES = 200          # 日志块最多 200 行
STAGE_LINES = 80             # [refresh]/[自检] 阶段行最多保留多少行
GH_TIMEOUT_SEC = 60

# 这些依赖的版本决定窗口操作行为，必须出现在报告里。
DEPENDENCIES = ("pywinauto", "comtypes", "pywin32", "mitmproxy",
                "curl_cffi", "opencv-python", "pillow", "numpy", "fastapi", "uvicorn")

# 「不符合想象的错误」：只有命中这些关键词的 WARNING/ERROR 才进入可疑错误一节。
SUSPICIOUS = (
    "[refresh]", "[自检]", "[PROFILE]", "[TOKEN]", "Token 获取失败", "鉴权失败",
    "mitmdump", "[mitm]", "[kill-port]", "Traceback", "异常", "超时", "未确认",
    "拒绝访问", "失败",
)
# 这些行不进日志块：请求明细与凭证回显。
DROP_PHRASES = ("params=", "json=", "← 下单", "cookie", "set-cookie")

log = logging.getLogger("court")


# ══════════════════════════════════════════════════
# 采集
# ══════════════════════════════════════════════════
def _run(argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess | None:
    env = {**os.environ, "GH_NO_UPDATE_NOTIFIER": "1", "GH_PROMPT_DISABLED": "1"}
    try:
        return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, env=env)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _git(*args: str) -> str | None:
    done = _run(["git", *args])
    if done is None or done.returncode != 0:
        return None
    return done.stdout.strip()


def _collect_git() -> dict:
    status = _git("status", "--porcelain") or ""
    files = sorted(line[3:].strip().split(" -> ")[-1]
                   for line in status.splitlines() if line.strip())
    return {
        "head": _git("rev-parse", "--short=8", "HEAD") or "未知",
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD") or "未知",
        "last_commit": _git("log", "-1", "--format=%s (%ad)",
                            "--date=format:%Y-%m-%d %H:%M") or "未知",
        "dirty_count": len(files),
        "dirty_files": files[:30],
    }


def _collect_env() -> dict:
    deps: list[tuple[str, str]] = []
    for name in DEPENDENCIES:
        try:
            deps.append((name, importlib.metadata.version(name)))
        except Exception:  # 未安装或元数据损坏
            continue
    return {
        "platform": f"{sys.platform} {_os_release()}".strip(),
        "python": sys.version.split()[0],
        "deps": deps,
    }


def _os_release() -> str:
    if not sys.platform.startswith("win"):
        return ""
    try:
        return f"build {sys.getwindowsversion().build}"
    except Exception:
        return "windows"


def _collect_runtime() -> dict:
    """只从 engine.snapshot() 取任务状态与计数，不取配置全文。"""
    from engine import engine

    snap = engine.snapshot()
    order = getattr(engine, "current_order", None)
    jobs = snap.get("jobs") or {}
    config = snap.get("config") or {}
    order_cfg = config.get("order") or {}
    auto, leak = jobs.get("auto") or {}, jobs.get("leak") or {}
    vid = jobs.get("vid") or {}
    orders = snap.get("orders") or {}

    def size(value) -> int:
        try:
            return len(value)
        except TypeError:
            return 0

    return {
        "running": [name for name in ("auto", "leak", "vid", "refresh")
                    if (jobs.get(name) or {}).get("running")],
        "auto_phase": str(auto.get("phase") or "-"),
        "auto_msg": str(auto.get("msg") or "-"),
        "leak_msg": str(leak.get("msg") or "-"),
        "leak_dates": " / ".join(leak.get("dates") or []) or "-",
        "vid_queue": int(vid.get("count") or 0),
        "orders_msg": str(orders.get("msg") or "正常"),
        "orders_count": size(orders.get("orders")),
        "target_date": str(order_cfg.get("target_date") or "-"),
        "token_set": bool(order_cfg.get("token_set")),
        "courts": size(snap.get("courts")),
        "unconfirmed": getattr(order, "phase", "") == "unconfirmed",
    }


def _collect_mitm() -> dict:
    """读 mitmdump 的 /__mitmproxy__/hint 拿代理实际看到的流量数，判断抓包方向。"""
    try:
        from engine import engine

        state = engine.mitm_state() or {}
        from utils import MITM_HINT, MITM_HOST, MITM_PORT
    except Exception as exc:  # 导入或探测失败本身也是诊断信息
        return {"error": type(exc).__name__}

    traffic: object = "-"
    try:
        with socket.create_connection((MITM_HOST, MITM_PORT), timeout=0.5) as sock:
            sock.sendall(b"GET /__mitmproxy__/hint HTTP/1.0\r\nHost: proxy\r\n\r\n")
            traffic = _parse_flow_count(sock.recv(65536).decode("utf-8", "replace"))
    except OSError as exc:
        traffic = f"读取失败：{type(exc).__name__}"

    return {
        "port_listening": bool(state.get("port_listening")),
        "proc_alive": bool(state.get("proc_alive")),
        "port": state.get("port"),
        "upstream": bool(state.get("upstream")),
        "flows": traffic,
        "hint": MITM_HINT,
    }


def _parse_flow_count(html: str) -> str:
    match = re.search(r"(\d+)\s+flows?", html)
    return f"{match.group(1)} 条" if match else "响应中未找到流量计数"


# ── 日志 ──────────────────────────────────────────
def _parse_line(raw: str):
    """'[2026-11-27 20:01:56] [INFO] 消息' → (datetime, level, msg)。"""
    line = raw.rstrip("\r\n")
    if len(line) < 26 or line[0] != "[" or line[20] != "]":
        return None
    close = line.find("]", 23)
    if close < 0:
        return None
    try:
        ts = datetime.strptime(line[1:20], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return ts, line[23:close], line[close + 1:].strip()


def _suspicious(level: str, msg: str) -> bool:
    """不合预期的 WARNING/ERROR；窗口刷新与自检的失败行即使不是 WARNING 也算。"""
    if any(p in msg.lower() for p in DROP_PHRASES):
        return False
    if "中止于" in msg or "结果=失败" in msg:
        return True
    if level not in ("WARNING", "ERROR", "CRITICAL"):
        return False
    return any(k in msg for k in SUSPICIOUS)


def _collect_logs(now: datetime) -> dict:
    """最近 10 分钟的日志：阶段行、可疑错误、其余白名单行分三类。"""
    cutoff = now - timedelta(minutes=LOG_WINDOW_MIN)
    stage: list[str] = []
    suspicious: list[str] = []
    rest: list[str] = []
    try:
        raw = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"stage": [], "suspicious": [], "lines": [], "total": 0, "dropped": 0}
    for raw_line in raw:
        entry = _parse_line(raw_line)
        if entry is None:
            continue
        ts, level, msg = entry
        if not (cutoff <= ts <= now + timedelta(seconds=5)):
            continue
        if any(p in msg.lower() for p in DROP_PHRASES):
            continue
        line = f"[{ts:%H:%M:%S}] [{level}] {' '.join(msg.split())[:300]}"
        if _suspicious(level, msg):
            suspicious.append(line)
        if "[refresh]" in msg or "[自检]" in msg:
            stage.append(line)
        elif level == "INFO" and any(k in msg for k in ("会话刷新", "Token", "[mitm]", "[TOKEN]")):
            rest.append(line)
    total = len(rest)
    return {"stage": stage[-STAGE_LINES:], "suspicious": suspicious[-40:],
            "lines": rest[-MAX_LOG_LINES:], "total": total,
            "dropped": max(0, total - MAX_LOG_LINES)}


def _safe(name: str, collect, fallback):
    """单个采集步骤失败不能让整份报告作废；失败原因写进报告对应字段。"""
    try:
        return collect()
    except Exception as exc:
        return {**(fallback or {}), "error": f"{name}: {type(exc).__name__}: {exc}"}


def collect_report() -> dict:
    now = datetime.now()
    return {
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "git": _safe("git", _collect_git, {}),
        "env": _safe("env", _collect_env, {}),
        "runtime": _safe("runtime", _collect_runtime, {"running": []}),
        "mitm": _safe("mitm", _collect_mitm, {}),
        "logs": _safe("logs", lambda: _collect_logs(now),
                      {"stage": [], "suspicious": [], "lines": [], "total": 0, "dropped": 0}),
    }


# ══════════════════════════════════════════════════
# Markdown
# ══════════════════════════════════════════════════
_PIPE = "\\|"


def _cell(value) -> str:
    """表格单元格：竖线与换行会破坏 Markdown 表格。"""
    return str(value).replace("|", _PIPE).replace("\n", " ")


def _table(rows: list[tuple]) -> list[str]:
    body = [f"| {_cell(k)} | {_cell(v)} |" for k, v in rows]
    return ["| 项目 | 值 |", "| --- | --- |", *body]


def build_title(now: datetime | None = None) -> str:
    return f"[诊断] {now or datetime.now():%Y-%m-%d %H:%M}"


def render_markdown(report: dict, *, visibility: str = "UNKNOWN") -> str:
    git, env, rt, mitm, logs = (report["git"], report["env"], report["runtime"],
                                report["mitm"], report["logs"])
    rt.setdefault("running", [])
    git.setdefault("dirty_files", [])
    env.setdefault("deps", [])
    lines = [
        f"# {build_title()}",
        "",
        "> 由本机一键诊断生成；不含 config.toml 全文与逐条请求参数。",
        "",
        "## 期望 / 实际",
        "",
        "**期望**：（请补充你当时在做什么、期望发生什么）",
        "",
        "**实际**：（请补充看到的现象）",
        "",
        "## 可疑错误",
        "",
    ]
    lines += [f"- `{line}`" for line in logs["suspicious"]] or ["无"]
    lines += ["", "## 各阶段耗时", ""]
    lines += [f"- `{line}`" for line in logs["stage"]] or ["无（最近 10 分钟没有 [refresh]/[自检] 行）"]
    lines += [
        "",
        "## 运行环境",
        "",
        *_table([
            ("采集时间", report["created_at"]),
            ("提交", f"{git['head']} @ {git['branch']}"),
            ("最近提交", git["last_commit"]),
            ("未提交文件", f"{git['dirty_count']} 个"),
            ("系统", env["platform"]),
            ("Python", env["python"]),
            ("依赖", "，".join(f"{n} {v}" for n, v in env["deps"]) or "未知"),
            ("采集异常", "；".join(v["error"] for v in
                                (report["git"], report["env"], report["runtime"],
                                 report["mitm"], report["logs"]) if "error" in v) or "无"),
        ]),
        "",
        "## 运行状态",
        "",
        *_table([
            ("运行中的任务", "，".join(rt.get("running", [])) or "无"),
            ("自动模式", f"{rt.get('auto_phase', '-')}（{rt.get('auto_msg', '-')}）"),
            ("检漏状态", rt.get("leak_msg", "-")),
            ("检漏目标日期", rt.get("leak_dates", "-")),
            ("VID 队列", rt.get("vid_queue", 0)),
            ("订单查询", f"{rt.get('orders_count', 0)} 条（{rt.get('orders_msg', '-')}）"),
            ("未确认订单", "有" if rt.get("unconfirmed") else "无"),
            ("目标日期", rt.get("target_date", "-")),
            ("Token 已配置", "是" if rt.get("token_set") else "否"),
            ("已缓存场地", rt.get("courts", 0)),
        ]),
        "",
        "## mitm 代理",
        "",
        *_table([
            ("端口监听", "是" if mitm.get("port_listening") else "否"),
            ("进程存活", "是" if mitm.get("proc_alive") else "否"),
            ("监听端口", mitm.get("port", "-")),
            ("upstream", "已配置" if mitm.get("upstream") else "无"),
            ("抓到的流量", mitm.get("flows", mitm.get("error", "-"))),
            ("当前规则", mitm.get("hint", "-")),
        ]),
        "",
        f"## 最近 {LOG_WINDOW_MIN} 分钟日志",
        "",
    ]
    if logs["lines"]:
        dropped = f"，更早的 {logs['dropped']} 行已省略" if logs["dropped"] else ""
        lines += [f"共 {logs['total']} 行{dropped}。", "", "```text", *logs["lines"], "```"]
    else:
        lines.append("无（最近 10 分钟没有符合条件的信息行）")
    if git["dirty_files"]:
        lines += ["", "<details><summary>未提交文件</summary>", "",
                  *[f"- {path}" for path in git["dirty_files"]], "", "</details>"]
    if visibility.upper() != "PRIVATE":
        lines += ["", "> ⚠ 目标仓库为公开仓库，报告将公开可见。"]
    return "\n".join(lines) + "\n"


# ══════════════════════════════════════════════════
# 目标仓库 + 建 Issue
# ══════════════════════════════════════════════════
def repo_from_origin() -> str | None:
    origin = (_git("config", "--get", "remote.origin.url") or "").rstrip("/")
    match = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?$", origin)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def check_repo() -> dict:
    """gh 侧确认仓库存在、可写且开启了 Issues。"""
    repo = repo_from_origin()
    if not repo:
        return {"ok": False, "reason": "未能从 git remote origin 解析出 GitHub 仓库"}
    done = _run(["gh", "repo", "view", repo, "--json",
                 "visibility,viewerPermission,hasIssuesEnabled"])
    if done is None:
        return {"ok": False, "repo": repo,
                "reason": "未找到 gh 命令，请先安装 GitHub CLI 并执行 gh auth login"}
    if done.returncode != 0:
        return {"ok": False, "repo": repo,
                "reason": f"无法读取 {repo}：{(done.stderr or '').strip()[:400]}"}
    info = json.loads(done.stdout or "{}")
    permission = str(info.get("viewerPermission") or "NONE").upper()
    if permission not in {"ADMIN", "MAINTAIN", "WRITE"}:
        return {"ok": False, "repo": repo,
                "reason": f"当前 gh 账号对 {repo} 没有写权限（{permission}）"}
    if not info.get("hasIssuesEnabled", False):
        return {"ok": False, "repo": repo, "reason": f"{repo} 未开启 Issues"}
    return {"ok": True, "repo": repo,
            "visibility": str(info.get("visibility") or "UNKNOWN").upper()}


def preview() -> dict:
    """采集并渲染；浏览器保存这份 Markdown，服务端不留任何预览状态。"""
    repo = check_repo()
    if not repo.get("ok"):
        return {"ok": False, "reason": repo.get("reason")}
    markdown = render_markdown(collect_report(), visibility=repo["visibility"])
    return {"ok": True, "markdown": markdown, "title": markdown.splitlines()[0][2:],
            "repo": repo["repo"], "visibility": repo["visibility"],
            "public": repo["visibility"] != "PRIVATE"}


def upload(markdown: str) -> dict:
    """把用户确认过的 Markdown 原样建成 Issue。"""
    if not markdown.startswith("# "):
        return {"ok": False, "reason": "报告内容不合法，请重新预览"}
    repo = check_repo()
    if not repo.get("ok"):
        return {"ok": False, "reason": repo.get("reason")}
    return _create_issue(repo["repo"], markdown.splitlines()[0][2:].strip(), markdown)


def _create_issue(repo: str, title: str, body: str) -> dict:
    import tempfile

    handle, path = tempfile.mkstemp(prefix="court-diag-", suffix=".md", text=True)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(body)
        done = _run(["gh", "issue", "create", "--repo", repo,
                     "--title", title, "--body-file", path], timeout=GH_TIMEOUT_SEC)
    finally:
        Path(path).unlink(missing_ok=True)
    if done is None:
        return {"ok": False, "reason": "调用 gh 失败（命令不可用或超时）"}
    if done.returncode != 0:
        return {"ok": False, "reason": f"gh 创建 Issue 失败：{(done.stderr or '').strip()[:400]}"}
    url = ((done.stdout or "").strip().splitlines() or [""])[-1]
    if not url.startswith("https://"):
        return {"ok": False, "reason": "gh 未返回 Issue 链接"}
    log.info("[诊断] 已创建诊断 Issue：%s", url)
    return {"ok": True, "url": url, "repo": repo}


# ══════════════════════════════════════════════════
# 命令行
# ══════════════════════════════════════════════════
def _main(argv: list[str]) -> int:
    args = argv[1:]
    if not args or args[0] != "submit" or set(args[1:]) - {"--upload"}:
        print("用法：python diagnostics.py submit [--upload]")
        return 2
    result = preview()
    if not result.get("ok"):
        print(f"❌ 预览失败：{result.get('reason')}")
        return 1
    print(result["markdown"])
    if "--upload" not in args:
        print(f"\n（未上传。加 --upload 可创建 Issue 到 {result['repo']}）")
        return 0
    issue = upload(result["markdown"])
    print(f"✅ {issue['url']}" if issue.get("ok") else f"❌ 上传失败：{issue.get('reason')}")
    return 0 if issue.get("ok") else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    sys.exit(_main(sys.argv))
