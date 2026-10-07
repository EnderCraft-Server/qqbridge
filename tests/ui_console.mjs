/* 控制台前端回归测试：在 Node 里拿一个假 DOM 跑 ui.html 的脚本。
 *
 * 起因：控制台「改监控群聊没有用」—— refresh() 每 4 秒拉一次 /api/state，
 * 原来只判断 activeElement 是不是输入框，用户正在编辑的内容会被服务端值冲掉，
 * 接着点保存保存的还是旧值。这里把那条路径钉死。
 *
 * 跑法：node tests/ui_console.mjs [ui.html 路径]
 */

import fs from "node:fs";
const html = fs.readFileSync((process.argv[2] || new URL("../qqbridge/ui.html", import.meta.url).pathname.replace(/^\//, "")), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

const calls = [];
const STATE = {
  config: { allow_send: true, allow_manage: true, owners: ["98704929"], admins: ["98704929"],
            watch_groups: ["1060667115", "1083430859", "1108270682"], auto_reply: true },
  scheduler: { settings: { enabled: true, interval_seconds: 300, min_gap_seconds: 30,
               max_turns_per_hour: 60, quiet_hours: [] },
               bus: { stored: 1, last_id: 9, pending_high: 0, pending_mid: 0, pending_low: 0, dropped: 0 } },
  control: { mode: "auto" }, keywords: ["a", "b"],
  llm: { api_base: "https://api.deepseek.com", model: "m", max_tokens: 2048, calls: 0 },
  agent_loop: {}, qzone: {}, auth: { username: "admin" },
  bot: { self_id: "461282697", ws_connected: true },
};
const GROUPS = {
  groups: [
    { group_id: "1060667115", name: "EnderCraft 玩家交流群", member_count: 120, watched: true },
    { group_id: "1083430859", name: "群二", member_count: 30, watched: true },
    { group_id: "1108270682", name: "群三", member_count: 44, watched: true },
    { group_id: "889900112", name: "新群（还没被监控）", member_count: 7, watched: false },
  ],
  watched: ["1060667115", "1083430859", "1108270682"], watch_all: false, total: 4,
};

function el(id) {
  return { id, value: "", checked: false, textContent: "", innerHTML: "", disabled: false,
    dataset: {}, style: {}, _handlers: {}, type: "text",
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener(t, f) { (this._handlers[t] ||= []).push(f); },
    closest: () => null, setAttribute() {}, removeAttribute() {} };
}
const els = new Map();
const docHandlers = {};
const document = {
  getElementById(id) { if (!els.has(id)) els.set(id, el(id)); return els.get(id); },
  querySelectorAll: () => [],
  addEventListener(t, f) { (docHandlers[t] ||= []).push(f); },
  cookie: "", activeElement: { tagName: "BODY" },
};
const fire = (type, target) => (docHandlers[type] || []).forEach((f) => f({ target }));

const fetch = async (path, opts = {}) => {
  calls.push({ path, body: opts.body });
  let data = {};
  if (path.startsWith("/api/state")) data = STATE;
  else if (path.startsWith("/api/auth/state")) data = { logged_in: true, agreed: true, username: "admin" };
  else if (path.startsWith("/api/groups")) data = GROUPS;
  else if (path.startsWith("/api/prompt")) data = { prompt: "P", chars: 1 };
  else if (path.startsWith("/api/events")) data = { events: [] };
  else if (path.startsWith("/api/agent_log")) data = { lines: [] };
  else if (path.startsWith("/api/audit")) data = { records: [] };
  else if (path.startsWith("/api/watch")) {
    const body = JSON.parse(opts.body);
    GROUPS.watched = body.watch_groups; GROUPS.watch_all = !body.watch_groups.length;
    GROUPS.groups.forEach((g) => { g.watched = body.watch_groups.includes(g.group_id); });
    STATE.config.watch_groups = body.watch_groups;
    data = { watch_groups: body.watch_groups };
  }
  return { ok: true, status: 200, text: async () => JSON.stringify(data), json: async () => data };
};

const ctx = {
  document, fetch, localStorage: { getItem: () => "", setItem() {} },
  location: { search: "", hash: "", reload() {} }, history: { replaceState() {} },
  setInterval: () => 0, setTimeout: () => 0, clearTimeout() {}, console, addEventListener() {},
  URLSearchParams, JSON, Date, Math, Object, Number, String, Array, Error, Promise,
  encodeURIComponent, decodeURIComponent,
};
ctx.window = ctx;

const tail = "\n; return { go, refresh, loadGroups, cache: () => groupCache, render: () => renderGroups() };";
const fn = new Function(...Object.keys(ctx), script + tail);
const RT = fn(...Object.values(ctx));
const flush = () => new Promise((r) => setTimeout(r, 60));
const checks = [];
const ok = (name, cond, extra = "") => { checks.push((cond ? "PASS  " : "FAIL  ") + name + (extra ? "  << " + extra : "")); return cond; };

await flush(); await flush();

// 1) 进入「巡检与唤醒」页 -> 群列表渲染
await RT.go("watch");
await flush();
const listHtml = els.get("watch-list").innerHTML;
ok("群列表渲染出全部 4 个群", ["1060667115", "1083430859", "1108270682", "889900112"].every((g) => listHtml.includes(g)));
ok("新群默认没勾选", /data-gid="889900112">/.test(listHtml) && !/data-gid="889900112" checked/.test(listHtml));
ok("已监控的群默认勾选", /data-gid="1060667115" checked/.test(listHtml));

// 2) 勾上新群 -> 保存
const before = calls.length;
RT.cache().find((g) => g.group_id === "889900112").sel = true;
els.get("save-watch").onclick();
await flush(); await flush();
const posted = calls.slice(before).filter((c) => c.path === "/api/watch");
ok("保存发出了一次 /api/watch", posted.length === 1, "实际 " + posted.length + " 次");
const body = posted[0] ? JSON.parse(posted[0].body) : { watch_groups: [] };
ok("POST 里含新加的群 889900112", body.watch_groups.includes("889900112"), JSON.stringify(body));

// 3) 防覆盖回归：改了 owners 之后，refresh() 不能把它冲掉
els.get("owners").value = "98704929,1029399006";
fire("input", els.get("owners"));
await RT.refresh(); await flush();
ok("refresh 不会冲掉用户刚改的 owners", els.get("owners").value === "98704929,1029399006",
   "实际变成 " + JSON.stringify(els.get("owners").value));

// 4) 没碰过的字段照常回填
els.get("owners").value = "98704929,1029399006";
els.get("api_base").value = "旧的";
await RT.refresh(); await flush();
ok("没碰过的 api_base 会被回填", els.get("api_base").value === "https://api.deepseek.com",
   "实际 " + JSON.stringify(els.get("api_base").value));

console.log(checks.join("\n"));
console.log(checks.some((c) => c.startsWith("FAIL")) ? "\n== 有失败 ==" : "\n== ALL PASS ==");
process.exit(checks.some((c) => c.startsWith("FAIL")) ? 1 : 0);