"""app.py — FastAPI 薄壳
   场地查询：3 并发 + 批间隔 3s + 可终止 + 日期可选 + 永久/动态双面板
"""

from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import reserve


app = FastAPI(title="Court Reserver")

CONFIG = {
    "token":       "",
    "target_date": "",
}


def _default_target_date() -> str:
    return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


def _pad_hm(t: str) -> str:
    """'8:00' -> '08:00'；'16:00' 保持。给 saveOrder 的 start/end 用。"""
    if ":" not in t:
        return t
    h, m = t.split(":", 1)
    return h.zfill(2) + ":" + m


# ============================================================
# 内联前端页面
# ============================================================

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>羽毛球场预约</title>
<style>
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                 "PingFang SC", "Microsoft YaHei", sans-serif;
    max-width: 1080px; margin: 24px auto; padding: 0 20px;
    color: #222; background: #fafafa;
  }
  h1 { font-size: 22px; margin: 0 0 18px; }

  .card {
    background: #fff; padding: 12px 16px;
    border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    margin-bottom: 12px;
  }

  .config-row {
    display: flex; gap: 10px; flex-wrap: wrap; align-items: center;
  }
  .config-row label { font-size: 13px; color: #666; }
  .config-row input {
    padding: 7px 10px; border: 1px solid #ddd;
    border-radius: 5px; font-size: 13px; flex: 1; min-width: 260px;
  }
  .config-row input:focus { outline: none; border-color: #1677ff; }
  .config-row select {
    padding: 7px 10px; border: 1px solid #ddd;
    border-radius: 5px; font-size: 13px;
    background: #fff; cursor: pointer;
  }
  .config-row select:focus { outline: none; border-color: #1677ff; }

  button {
    padding: 7px 16px; border: none; border-radius: 5px;
    cursor: pointer; font-size: 13px; transition: 0.15s;
  }
  button.primary { background: #1677ff; color: #fff; }
  button.primary:hover:enabled { background: #0e5fcc; }
  button:disabled { background: #ccc; cursor: not-allowed; }
  button.ghost { background: #f0f0f0; color: #333; }
  button.ghost:hover:enabled { background: #e0e0e0; }
  button.danger { background: #ff4d4f; color: #fff; }
  button.danger:hover:enabled { background: #d9363e; }

  .status-bar {
    display: flex; align-items: center; gap: 16px;
    font-size: 14px; flex-wrap: wrap;
  }
  .status-bar b { color: #1677ff; font-size: 16px; }
  .status-bar .stat-mini {
    font-family: "SFMono-Regular", Consolas, monospace;
    font-size: 12px; color: #555;
  }
  .status-bar .spacer { flex: 1; }

  .section { margin-top: 22px; }
  .section h2 {
    font-size: 15px; color: #555; margin: 0 0 10px;
    padding-left: 8px; border-left: 3px solid #1677ff;
  }
  .section.dynamic-section h2 {
    border-left-color: #52c41a;
  }
  .grid { display: flex; flex-wrap: wrap; gap: 8px; }
  .court {
    padding: 12px 20px; border-radius: 6px; font-size: 13px;
    user-select: none; min-width: 68px; text-align: center;
    transition: 0.15s;
  }
  .court.unknown  { background: #f5f5f5; color: #bbb; cursor: default; }
  .court.checking { background: #faad14; color: #fff; cursor: wait; }
  .court.available { background: #1677ff; color: #fff; cursor: pointer; }
  .court.available:hover { background: #0e5fcc; transform: translateY(-1px); }
  .court.occupied { background: #e8e8e8; color: #aaa; cursor: not-allowed; }

  .empty-hint {
    color: #999; font-size: 13px; padding: 20px 0; text-align: center;
  }

  .avail-grid {
    display: flex; flex-wrap: wrap; gap: 8px;
  }
  .avail-btn {
    padding: 10px 18px; border-radius: 6px; font-size: 13px;
    background: #52c41a; color: #fff; border: none; cursor: pointer;
    transition: 0.15s; font-weight: 500;
  }
  .avail-btn:hover { background: #389e0d; transform: translateY(-1px); }
  .avail-btn:active { transform: translateY(0); }

  .legend {
    display: flex; gap: 16px; flex-wrap: wrap;
    font-size: 12px; color: #666; margin-top: 4px;
  }
  .legend span.sw {
    display: inline-block; width: 12px; height: 12px;
    border-radius: 3px; margin-right: 4px;
    vertical-align: middle;
  }

  .panel-title {
    font-size: 13px; color: #999; margin: 26px 0 4px;
    padding-bottom: 6px; border-bottom: 1px dashed #ddd;
  }

  .log {
    background: #1a1a1a; color: #d0d0d0;
    padding: 10px 14px; border-radius: 6px;
    font-family: "SFMono-Regular", Consolas, monospace;
    font-size: 12px; max-height: 320px; overflow-y: auto;
    margin-top: 18px; white-space: pre-wrap; line-height: 1.55;
  }
  .log .l-VID_OK    { color: #7ee787; }
  .log .l-VID_ERR   { color: #ff7b72; }
  .log .l-VID       { color: #79c0ff; }
  .log .l-ORDER_ERR { color: #ffa657; }
  .log .l-ORDER_OK  { color: #56d364; }
  .log .l-ORDER     { color: #8b949e; }
  .log .l-RESERVE   { color: #d2a8ff; }
  .log .l-UI        { color: #a5d6ff; }
</style>
</head>
<body>

<h1>🏸 羽毛球场预约</h1>

<div class="card config-row">
  <label>Token</label>
  <input id="token" placeholder="粘贴今天的 token">
  <label>日期</label>
  <select id="date-sel">
    <option value="0">今天</option>
    <option value="1" selected>明天</option>
    <option value="2">后天</option>
  </select>
  <button class="primary" onclick="saveConfig()">保存</button>
</div>

<div class="card status-bar">
  <span>VID 缓存：<b id="vid-count">0</b></span>
  <span>目标日期：<b id="cur-date">-</b></span>
  <button class="primary" id="btn-start-vid" onclick="toggleVid()">启动 VID Worker</button>
  <button class="primary" id="btn-refresh" onclick="refreshOrders()">刷新场地</button>
  <span class="spacer"></span>
  <span class="stat-mini" id="last-solve">最近 solve：-</span>
</div>

<div class="card">
  <div class="legend">
    <span><span class="sw" style="background:#f5f5f5;border:1px solid #ddd"></span>未查</span>
    <span><span class="sw" style="background:#faad14"></span>查询中</span>
    <span><span class="sw" style="background:#1677ff"></span>整段空（可点）</span>
    <span><span class="sw" style="background:#e8e8e8"></span>占用/失败</span>
    <span><span class="sw" style="background:#52c41a"></span>下方动态候选</span>
  </div>
</div>

<!-- 永久面板：16-18 / 18-20 / 20-22 -->
<div class="panel-title">标准时段（永久显示）</div>
<div id="buckets">
  <div class="empty-hint">
    点上方「刷新场地」逐个查询（每批 3 并发，批间隔 3s，可随时终止）
  </div>
</div>

<!-- 动态面板：其他时段有空就加 -->
<div class="panel-title">其他时段（查到有空才显示，30min 为最小单位，窗口内可合并）</div>
<div id="dynamic">
  <div class="empty-hint">暂无</div>
</div>

<div class="log" id="log"></div>

<script>
// ===== 永久面板：3 个标准 2 小时窗口 =====
const PERMANENT_BUCKETS = ["16:00-18:00", "18:00-20:00", "20:00-22:00"];
const PERMANENT_SLOTS = {
  "16:00-18:00": ["16:00", "16:30", "17:00", "17:30"],
  "18:00-20:00": ["18:00", "18:30", "19:00", "19:30"],
  "20:00-22:00": ["20:00", "20:30", "21:00", "21:30"],
};

// ===== 动态面板：其他时段 =====
// 合并窗口：只在同一组的 slots 内，连续空闲的 30min 块会被合并
// 例：9:30-10:00 在组 "8:00-10:00" 里；10:00-10:30 在组 "10:00-12:00" 里
//     两者属于不同组，绝不会被合并
const DYNAMIC_GROUPS = [
  { title: "08:00-10:00",  slots: ["08:00", "08:30", "09:00", "09:30"],  end: "10:00" },
  { title: "10:00-12:00",  slots: ["10:00", "10:30", "11:00", "11:30"],  end: "12:00" },
  { title: "12:00-14:00",  slots: ["12:00", "12:30", "13:00", "13:30"],  end: "14:00" },
  { title: "14:00-16:00",  slots: ["14:00", "14:30", "15:00", "15:30"],  end: "16:00" },
];

const TOTAL_COURTS = 10;

// 批间隔配置
const BATCH_SIZE      = 5;
const BATCH_WAIT_MS   = 3000;

let courtState = {};    // courtState[c][bucket] = "unknown"|"checking"|"available"|"occupied"
let availMap   = {};    // availMap[c] = { "8:00": "0", "8:30": "2", ... } 原始数据
let pending    = null;
let lastLogSeq = 0;
let refreshing = false;
let lastRefreshTs = 0;
let abortRefresh = false;
let vidRunning = false;
const REFRESH_COOLDOWN_MS = 500;


function offsetToDateStr(offset) {
  const d = new Date();
  d.setDate(d.getDate() + parseInt(offset, 10));
  const yyyy = d.getFullYear();
  const mm   = String(d.getMonth() + 1).padStart(2, "0");
  const dd   = String(d.getDate()).padStart(2, "0");
  return yyyy + "-" + mm + "-" + dd;
}

function initAllState() {
  courtState = {};
  availMap = {};
  for (let c = 1; c <= TOTAL_COURTS; c++) {
    courtState[c] = {};
    for (const b of PERMANENT_BUCKETS) courtState[c][b] = "unknown";
  }
  renderPermanent();
  renderDynamic();
}

function appendLog(html) {
  const el = document.getElementById("log");
  const div = document.createElement("div");
  div.innerHTML = html;
  el.insertBefore(div, el.firstChild);
  while (el.childNodes.length > 400) el.removeChild(el.lastChild);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}

function logUI(msg) {
  const t = new Date().toLocaleTimeString();
  appendLog('<span class="l-UI">[' + t + '] [UI] ' + escapeHtml(msg) + '</span>');
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function shuffle(arr) {
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
}


// ===== 时间 key 容错查找：同时兼容 "8:00" 与 "08:00" =====
function getAvail(avail, time) {
  if (!avail) return undefined;
  if (time in avail) return avail[time];
  // 兜底：兼容 "8:00" ↔ "08:00"
  const parts = time.split(":");
  if (parts.length !== 2) return undefined;
  const padded = parts[0].padStart(2, "0") + ":" + parts[1];
  if (padded in avail) return avail[padded];
  const unpadded = String(parseInt(parts[0], 10)) + ":" + parts[1];
  if (unpadded in avail) return avail[unpadded];
  return undefined;
}

function isFree(avail, time) {
  // 后端语义：status === "1" 才是空闲
  return getAvail(avail, time) === "1";
}


// ===== 从 avail 里找连续空闲段 =====
function findFreeRuns(group, avail) {
  const runs = [];
  let startIdx = -1;
  for (let i = 0; i < group.slots.length; i++) {
    const s = group.slots[i];
    const free = isFree(avail, s);
    if (free && startIdx === -1) {
      startIdx = i;
    }
    if (!free && startIdx !== -1) {
      runs.push({
        start: group.slots[startIdx],
        end:   group.slots[i],
      });
      startIdx = -1;
    }
  }
  if (startIdx !== -1) {
    runs.push({
      start: group.slots[startIdx],
      end:   group.end,
    });
  }
  return runs;
}


// ===== 事件绑定（只绑一次，用委托） =====

window.addEventListener("DOMContentLoaded", function () {
  initAllState();
  refreshVid();
  pollLogs();
  setInterval(refreshVid, 1000);
  setInterval(pollLogs, 1000);
  setInterval(refreshStats, 2000);

  document.getElementById("buckets").addEventListener("click", function (ev) {
    const el = ev.target.closest(".court.available");
    if (!el) return;
    doReserveRange(
      parseInt(el.dataset.court, 10),
      el.dataset.start,
      el.dataset.end
    );
  });

  document.getElementById("dynamic").addEventListener("click", function (ev) {
    const btn = ev.target.closest(".avail-btn");
    if (!btn) return;
    doReserveRange(
      parseInt(btn.dataset.court, 10),
      btn.dataset.start,
      btn.dataset.end
    );
  });
});


// ===== 配置 / VID / stats =====

async function saveConfig() {
  const token = document.getElementById("token").value.trim();
  if (!token) { alert("请填写 token"); return; }
  const offset = document.getElementById("date-sel").value;
  const target_date = offsetToDateStr(offset);

  try {
    const r = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: token, target_date: target_date }),
    });
    const d = await r.json();
    logUI("配置已保存: target_date=" + d.config.target_date);
    document.getElementById("cur-date").textContent = d.config.target_date;
    initAllState();
  } catch (e) {
    logUI("保存配置失败: " + e);
  }
}

async function refreshVid() {
  try {
    const r = await fetch("/api/vid_count");
    const d = await r.json();
    document.getElementById("vid-count").textContent = d.count;
  } catch (_) {}
}

async function refreshStats() {
  try {
    const r = await fetch("/api/vid_stats");
    const d = await r.json();
    const arr = d.stats || [];
    const el = document.getElementById("last-solve");
    if (arr.length === 0) { el.textContent = "最近 solve：-"; return; }
    const last = arr[arr.length - 1];
    if (last.ok) {
      el.textContent =
        "最近 solve：" + last.total_ms + "ms  " +
        "(gen " + last.generate_ms + " / rec " + last.recognize_ms +
        " / trk " + last.track_ms + " / chk " + last.check_ms + ")";
    } else {
      el.textContent = "最近 solve：失败 (" + (last.error || "") + ")";
    }
  } catch (_) {}
}


// ===== VID Worker toggle =====

async function toggleVid() {
  const btn = document.getElementById("btn-start-vid");
  try {
    if (!vidRunning) {
      const r = await fetch("/api/start_vid", { method: "POST" });
      const d = await r.json();
      vidRunning = true;
      btn.textContent = "停止 VID Worker";
      btn.classList.remove("primary");
      btn.classList.add("danger");
      logUI("VID Worker 已启动（started=" + d.started +
            " 队列=" + d.count + "）");
    } else {
      await fetch("/api/stop_vid", { method: "POST" });
      vidRunning = false;
      btn.textContent = "启动 VID Worker";
      btn.classList.remove("danger");
      btn.classList.add("primary");
      logUI("VID Worker 已停止");
    }
  } catch (e) {
    logUI("VID toggle 失败: " + e);
  }
}


// ===== 刷新主流程：5 并发 + 批间隔 3s + 单按钮 toggle =====

async function refreshOrders() {
  // 已经是运行状态 → 当作「停止」处理
  if (refreshing) {
    abortRefresh = true;
    logUI("⛔ 收到停止请求，等待当前批次结束...");
    return;
  }

  const now = Date.now();
  if (now - lastRefreshTs < REFRESH_COOLDOWN_MS) {
    logUI("⏸ 冷却中，请稍后再点");
    return;
  }

  refreshing = true;
  abortRefresh = false;

  const btn = document.getElementById("btn-refresh");
  btn.classList.remove("primary");
  btn.classList.add("danger");
  btn.textContent = "停止刷新";

  initAllState();

  const order = shuffle([1,2,3,4,5,6,7,8,9,10]);
  let okCnt = 0, failCnt = 0, doneCnt = 0;
  const t0 = Date.now();
  const totalBatches = Math.ceil(order.length / BATCH_SIZE);

  logUI("→ 开始批量查询（随机顺序：" + order.join(",") +
        "，每批 " + BATCH_SIZE + " 并发，批间隔 " + (BATCH_WAIT_MS/1000) + "s）");

  for (let bi = 0; bi < totalBatches; bi++) {
    if (abortRefresh) break;

    const batch = order.slice(bi * BATCH_SIZE, (bi + 1) * BATCH_SIZE);

    // 标记该批为查询中
    for (const c of batch) {
      for (const b of PERMANENT_BUCKETS) courtState[c][b] = "checking";
    }
    renderPermanent();
    btn.textContent = "停止刷新 (" + (bi + 1) + "/" + totalBatches + ")";

    // 并发查询该批
    const results = await Promise.all(batch.map(async (c) => {
      try {
        const r = await fetch("/api/order_one?court_no=" + c);
        return { court: c, data: await r.json() };
      } catch (e) {
        return { court: c, error: String(e) };
      }
    }));

    if (abortRefresh) {
      // 回滚仍在 checking 状态
      for (const c of batch) {
        for (const b of PERMANENT_BUCKETS) {
          if (courtState[c][b] === "checking") courtState[c][b] = "unknown";
        }
      }
      renderPermanent();
      break;
    }

    // 处理该批结果
    for (const item of results) {
      const c = item.court;

      if (item.error) {
        for (const b of PERMANENT_BUCKETS) courtState[c][b] = "occupied";
        delete availMap[c];
        failCnt++;
        logUI("❌ " + c + "号场 异常：" + item.error);
      } else {
        const d = item.data;
        if (!d.ok) {
          for (const b of PERMANENT_BUCKETS) courtState[c][b] = "occupied";
          delete availMap[c];
          failCnt++;
          logUI("❌ " + c + "号场：" + (d.error || d.msg || "查询失败"));
        } else {
          availMap[c] = d.avail;
          for (const b of PERMANENT_BUCKETS) {
            const slots = PERMANENT_SLOTS[b];
            const allFree = slots.every(s => isFree(d.avail, s));
            courtState[c][b] = allFree ? "available" : "occupied";
          }
          okCnt++;
          logUI("✓ " + c + "号场 已查");
        }
      }
      doneCnt++;
    }

    renderPermanent();
    renderDynamic();

    // 不是最后一批 → 等 3s（可中断）
    const isLastBatch = (bi === totalBatches - 1);
    if (!isLastBatch && !abortRefresh) {
      const deadline = Date.now() + BATCH_WAIT_MS;
      while (Date.now() < deadline) {
        if (abortRefresh) break;
        await sleep(100);
      }
    }
  }

  const dt = ((Date.now() - t0) / 1000).toFixed(1);
  if (abortRefresh) {
    logUI("⏹ 已终止 —— 成功 " + okCnt + " 失败 " + failCnt +
          " 共查 " + doneCnt + "/" + order.length + " 用时 " + dt + "s");
  } else {
    logUI("✅ 查询完成 成功 " + okCnt + "/" + order.length +
          " 失败 " + failCnt + " 用时 " + dt + "s");
    if (failCnt === order.length) {
      logUI("⚠️ 全部失败！token 可能失效或被风控，请停止操作等待恢复");
      alert("⚠️ 全部查询失败！\n\n可能 token 失效或被风控。\n不要立刻重试，等几分钟再说。");
    }
  }

  refreshing = false;
  abortRefresh = false;
  btn.classList.remove("danger");
  btn.classList.add("primary");
  btn.textContent = "刷新场地";
  lastRefreshTs = Date.now();
}


// ===== 日志拉取 =====

async function pollLogs() {
  try {
    const r = await fetch("/api/logs?since=" + lastLogSeq);
    const d = await r.json();
    for (const e of (d.logs || [])) {
      lastLogSeq = e.seq;
      const cls = "l-" + e.level.replace(/[^A-Za-z0-9]/g, "_");
      appendLog(
        '<span class="' + cls + '">[' + e.ts + '] [' + escapeHtml(e.level) +
        '] ' + escapeHtml(e.msg) + '</span>'
      );
    }
  } catch (_) {}
}


// ===== 渲染：永久面板 =====

function renderPermanent() {
  const container = document.getElementById("buckets");
  container.innerHTML = "";

  for (const b of PERMANENT_BUCKETS) {
    const div = document.createElement("div");
    div.className = "section";

    const h2 = document.createElement("h2");
    h2.textContent = b;
    div.appendChild(h2);

    const grid = document.createElement("div");
    grid.className = "grid";

    const parts = b.split("-");
    const startHM = parts[0];
    const endHM   = parts[1];

    for (let c = 1; c <= TOTAL_COURTS; c++) {
      const state = courtState[c] ? courtState[c][b] : "unknown";
      const el = document.createElement("div");

      if (state === "available") {
        el.className = "court available";
        el.dataset.court = c;
        el.dataset.start = startHM;
        el.dataset.end   = endHM;
      } else if (state === "checking") {
        el.className = "court checking";
      } else if (state === "occupied") {
        el.className = "court occupied";
      } else {
        el.className = "court unknown";
      }

      el.textContent = c + "号场";
      grid.appendChild(el);
    }

    div.appendChild(grid);
    container.appendChild(div);
  }
}


// ===== 渲染：动态面板 =====

function renderDynamic() {
  const wrap = document.getElementById("dynamic");
  let html = "";

  let totalItems = 0;

  for (const g of DYNAMIC_GROUPS) {
    const items = [];
    for (let c = 1; c <= TOTAL_COURTS; c++) {
      const avail = availMap[c];
      if (!avail) continue;
      const runs = findFreeRuns(g, avail);
      for (const r of runs) {
        items.push({ court: c, start: r.start, end: r.end });
      }
    }
    if (items.length === 0) continue;

    items.sort((a, b) => {
      const ta = timeToMin(a.start);
      const tb = timeToMin(b.start);
      if (ta !== tb) return ta - tb;
      return a.court - b.court;
    });

    totalItems += items.length;

    html += '<div class="section dynamic-section">';
    html += '<h2>' + escapeHtml(g.title) + '</h2>';
    html += '<div class="avail-grid">';
    for (const it of items) {
      html += '<button class="avail-btn" ' +
              'data-court="' + it.court + '" ' +
              'data-start="' + escapeHtml(it.start) + '" ' +
              'data-end="' + escapeHtml(it.end) + '">' +
              it.court + '号场 · ' + escapeHtml(it.start) + '-' + escapeHtml(it.end) +
              '</button>';
    }
    html += '</div></div>';
  }

  if (totalItems === 0) {
    wrap.innerHTML = '<div class="empty-hint">暂无 —— 刷新场地后有空的其他时段会显示在这里</div>';
  } else {
    wrap.innerHTML = html;
  }
}

function timeToMin(t) {
  const parts = t.split(":");
  return parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10);
}


// ===== 预约 =====

async function doReserveRange(court, start, end) {
  if (pending) { logUI("已有预约进行中，请稍后"); return; }
  pending = { court: court, start: start, end: end };
  renderPermanent();

  logUI("→ 预约 " + court + "号场 " + start + "-" + end + " ...");
  try {
    const r = await fetch("/api/reserve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ court_no: court, start: start, end: end }),
    });
    const d = await r.json();
    logUI("← " + JSON.stringify(d));
      } catch (e) {
    logUI("预约异常: " + e);
    alert("请求异常: " + e);
  } finally {
    pending = null;
    renderPermanent();
  }
}
</script>

</body>
</html>
"""


# ============================================================
# 请求模型
# ============================================================

class ConfigModel(BaseModel):
    token: str
    target_date: str = ""


class ReserveModel(BaseModel):
    court_no: int
    start:    str
    end:      str


# ============================================================
# 路由
# ============================================================

@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


@app.get("/api/ping")
def ping():
    return {"ok": True}


@app.post("/api/config")
def set_config(data: ConfigModel):
    CONFIG["token"] = data.token.strip()
    CONFIG["target_date"] = data.target_date.strip() or _default_target_date()
    reserve._log("UI", f"配置已更新: target_date={CONFIG['target_date']}")
    return {"ok": True, "config": CONFIG}


@app.get("/api/logs")
def logs(since: int = 0):
    return {"logs": reserve.get_logs(since)}


@app.get("/api/vid_stats")
def vid_stats():
    return {"stats": reserve.get_vid_stats()}


@app.get("/api/order_one")
def order_one(court_no: int):
    if not CONFIG["token"]:
        return {"ok": False, "court": court_no, "msg": "未配置 token"}

    idx = court_no - 1
    if idx < 0 or idx >= len(reserve.CANDIDATE_GROUNDS):
        return {"ok": False, "court": court_no, "msg": f"非法场地号 {court_no}"}

    gid, gname = reserve.CANDIDATE_GROUNDS[idx]
    date  = CONFIG["target_date"] or _default_target_date()
    token = CONFIG["token"]

    avail = reserve.get_court_availability(gid, date, token)

    if avail is None:
        return {"ok": False, "court": court_no, "name": gname,
                "error": "查询失败（token 可能已过期或被风控）"}

    return {"ok": True, "court": court_no, "name": gname,
            "date": date, "avail": avail}


@app.post("/api/start_vid")
def start_vid():
    started = reserve.start_vid_workers()
    return {"ok": True, "started": started, "count": reserve.get_vid_count()}


@app.post("/api/stop_vid")
def stop_vid():
    reserve.stop_vid_workers()
    return {"ok": True, "count": reserve.get_vid_count()}


@app.get("/api/vid_count")
def vid_count():
    return {"count": reserve.get_vid_count()}


@app.post("/api/reserve")
def reserve_endpoint(data: ReserveModel):
    if not CONFIG["token"]:
        return {"ok": False, "msg": "未配置 token"}

    idx = data.court_no - 1
    if idx < 0 or idx >= len(reserve.CANDIDATE_GROUNDS):
        return {"ok": False, "msg": f"非法场地号 {data.court_no}"}
    ground_id, ground_name = reserve.CANDIDATE_GROUNDS[idx]

    vid = reserve.take_vid(timeout=5.0)
    if vid is None:
        return {"ok": False, "msg": "VID 缓冲区为空，请先启动 VID Worker 或稍等"}

    date = CONFIG["target_date"] or _default_target_date()
    start_time = f"{date} {_pad_hm(data.start)}:00"
    end_time   = f"{date} {_pad_hm(data.end)}:00"

    result = reserve.post_order_once(
        vid=vid,
        ground_id=ground_id,
        ground_name=ground_name,
        start_time=start_time,
        end_time=end_time,
        token=CONFIG["token"],
    )
    return {
        "ok":  bool(result.get("success")),
        "msg": result.get("msg", ""),
        "raw": result,
    }