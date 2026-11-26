// Frontend regression: the auto-mode court picker must be strict single-select.
// Run: node test_frontend_court_picker.mjs
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const html = fs.readFileSync(path.join(here, "index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/);
if (!script) throw new Error("index.html 中没有内联脚本");

class ClassList {
  constructor() { this.items = new Set(); }
  add(c) { this.items.add(c); }
  remove(c) { this.items.delete(c); }
  contains(c) { return this.items.has(c); }
  toggle(c, force) {
    const on = force === undefined ? !this.items.has(c) : force;
    if (on) this.items.add(c); else this.items.delete(c);
    return on;
  }
}

class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.listeners = {};
    this.classList = new ClassList();
    this.dataset = {};
    this.style = {};
    this.value = "";
    this.textContent = "";
    this._className = "";
    this.disabled = false;
    this._innerHTML = "";
  }
  set className(value) {
    this._className = value;
    this.classList.items = new Set(String(value).split(/\s+/).filter(Boolean));
  }
  get className() { return this._className; }
  set innerHTML(value) {
    this._innerHTML = value;
    if (value === "") this.children = [];
  }
  get innerHTML() { return this._innerHTML; }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; }
  insertBefore(child) { this.children.unshift(child); return child; }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  setAttribute(key, value) { this[key] = String(value); }
  remove() {}
  closest() { return null; }
  querySelectorAll() { return []; }
  async click() {
    if (this.listeners.click) await this.listeners.click({ target: this });
  }
}

const elements = new Map();
const document = {
  createElement: tag => new Element(tag),
  getElementById: id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  },
  addEventListener() {},
};
const requests = [];
const context = {
  console,
  requests,
  setTimeout,
  clearTimeout,
  document,
  window: { addEventListener() {} },
  EventSource: class { addEventListener() {} },
  fetch: async (url, options = {}) => {
    if (url === "/api/config") requests.push(JSON.parse(options.body));
    return { ok: true, json: async () => ({ ok: true }) };
  },
};
vm.createContext(context);

const assertions = `
(async () => {
  allCourtNos = [3, 4, 5];
  targetCourt = 0;
  document.getElementById("auto-ss").value = "19:00";
  document.getElementById("auto-se").value = "21:00";
  document.getElementById("auto-rt").value = "20:00:05.123";

  renderCourtPicker();
  const box = document.getElementById("court-picker");
  if (box.children.length !== 3) throw new Error("场地按钮数量不正确");

  await box.children[0].click();
  if (targetCourt !== 3) throw new Error("第一次选择未生效");
  let picked = box.children.filter(el => el.classList.contains("on"));
  if (picked.length !== 1 || picked[0].textContent !== "3号场")
    throw new Error("第一次选择不是唯一选中项");

  await box.children[2].click();
  if (targetCourt !== 5) throw new Error("新场地未替换旧选择");
  picked = box.children.filter(el => el.classList.contains("on"));
  if (picked.length !== 1 || picked[0].textContent !== "5号场")
    throw new Error("选择新场地后仍存在多个选中项");

  if (requests.length !== 2) throw new Error("每次选择应写回一次配置");
  if (requests[0].court_no !== 3 || requests[1].court_no !== 5)
    throw new Error("写回的场地不是单个数字");
  if (Array.isArray(requests[0].court_no))
    throw new Error("前端仍提交多场地数组");

  targetCourt = 0;
  const before = requests.length;
  const saved = await saveAutoPlan({ requireCourt: true });
  if (saved !== false || requests.length !== before)
    throw new Error("空场地不应提交配置");

  const record = {
    found_at: "2026-11-26 20:16:32.128", court: 8, name: "8号场",
    date: "2026-11-26", start: "18:30", end: "20:30",
    sent_at: "2026-11-26 20:16:32.130",
    returned_at: "2026-11-26 20:16:32.456",
    confirmed_at: "2026-11-26 20:16:32.457",
    result: "ok", elapsed_ms: 327, booked: true,
  };
  renderLeak(record);
  const leakText = document.getElementById("leak-found").children
    .map(el => el.textContent).join(" | ");
  for (const part of [record.found_at, "8号场", record.date,
                      "18:30-20:30", record.sent_at, record.returned_at,
                      record.confirmed_at, "327ms"]) {
    if (!leakText.includes(part)) throw new Error("发现记录缺少字段：" + part);
  }
  renderLeak(record);
  if (!document.getElementById("leak-found").children[0].textContent
      .includes(record.found_at)) {
    throw new Error("状态刷新后发现时间被清空");
  }
})()
`;

Promise.resolve(vm.runInContext(script[1] + assertions, context)).catch(err => {
  console.error(err);
  process.exitCode = 1;
});
