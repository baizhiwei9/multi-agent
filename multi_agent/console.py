CONSOLE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Multi-Agent Hub</title>
<style>
  :root { color-scheme: light dark; --bg:#f7f7f8; --fg:#1c1c1e; --line:#8884; --card:#fff;
          --muted:#6b6b70; --blue:#0b6bcb; --green:#1a7f37; --red:#b42318; --amber:#9a6700; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#141416; --fg:#e9e9ec; --card:#1d1d21; --muted:#9a9aa2; }
  }
  * { box-sizing: border-box; }
  body { font: 14px/1.5 -apple-system, "Segoe UI", "Noto Sans SC", sans-serif;
         margin: 0; padding: 20px 24px 60px; background: var(--bg); color: var(--fg); }
  h1 { font-size: 18px; margin: 0 0 2px; }
  h2 { font-size: 15px; margin: 26px 0 10px; display: flex; align-items: center; gap: 8px;
       flex-wrap: wrap; }
  h2 .count { font-weight: 400; color: var(--muted); font-size: 12px; }
  .sub { color: var(--muted); margin-bottom: 14px; }
  .bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 6px; }
  input[type=password] { padding: 6px 8px; width: 240px; }
  button { padding: 5px 11px; cursor: pointer; border: 1px solid var(--line);
           background: var(--card); color: var(--fg); border-radius: 6px; font-size: 13px; }
  button:hover { border-color: var(--blue); color: var(--blue); }
  code { font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
  .muted { color: var(--muted); }
  .ok { color: var(--green); } .bad { color: var(--red); } .warn { color: var(--amber); }
  .grow { flex: 1; }

  /* 指标 */
  .metrics { display: flex; gap: 10px; flex-wrap: wrap; }
  .metric { background: var(--card); border: 1px solid var(--line); border-radius: 9px;
            padding: 7px 13px; min-width: 92px; }
  .metric b { display: block; font-size: 19px; line-height: 1.25; }
  .metric .k { font-size: 11.5px; color: var(--muted); }

  /* 看板：只看当前 */
  .board { display: grid; grid-template-columns: repeat(3, minmax(200px, 1fr)); gap: 10px; }
  @media (max-width: 800px) { .board { grid-template-columns: 1fr; } }
  .col { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
         padding: 8px; min-height: 84px; }
  .colhead { display: flex; justify-content: space-between; font-size: 12px;
             font-weight: 600; color: var(--muted); margin: 0 2px 8px; }
  .card { border: 1px solid var(--line); border-left: 3px solid var(--line);
          border-radius: 7px; padding: 7px 8px; margin-bottom: 7px; cursor: pointer;
          background: var(--bg); }
  .card:hover { border-color: var(--blue); }
  .card .top { display: flex; justify-content: space-between; gap: 6px; }
  .card .cap { font-size: 12.5px; font-weight: 600; word-break: break-all; }
  .card .meta { font-size: 11.5px; color: var(--muted); margin-top: 3px;
                display: flex; gap: 8px; flex-wrap: wrap; }
  .card .err { font-size: 11.5px; color: var(--red); margin-top: 3px; word-break: break-all; }
  .card.st-queued { border-left-color: #8b8b93; }
  .card.st-assigned { border-left-color: var(--blue); }
  .card.st-failed, .card.st-timeout, .card.st-canceled { border-left-color: var(--red); }
  .empty { color: var(--muted); font-size: 12px; padding: 6px 2px; }

  /* 流水线 */
  .run { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
         padding: 9px 11px; margin-bottom: 9px; }
  .runhead { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
  .runhead .name { font-weight: 600; }
  .steps { display: flex; gap: 4px; align-items: center; margin-top: 8px;
           overflow-x: auto; padding-bottom: 2px; }
  .step { flex: 0 0 auto; font-size: 11.5px; padding: 3px 8px; border-radius: 999px;
          border: 1px solid var(--line); white-space: nowrap; }
  .s-pending { color: var(--muted); }
  .s-running { color: var(--blue); border-color: var(--blue); }
  .s-succeeded { color: var(--green); border-color: #1a7f3755; background: #1a7f3712; }
  .s-failed { color: var(--red); border-color: #b4231855; background: #b4231812; }
  .s-skipped { color: var(--amber); border-color: #9a670055; }
  .arrow { color: var(--muted); font-size: 11px; }
  .stepdetail { margin-top: 9px; border-top: 1px solid var(--line); padding-top: 8px; }
  .steprow { display: grid; grid-template-columns: 90px 74px 64px 1fr; gap: 8px;
             padding: 5px 0; border-bottom: 1px dashed var(--line); font-size: 12px;
             align-items: start; }
  .steprow .res { color: var(--muted); word-break: break-all; }
  .hidden { display: none !important; }

  /* 表 */
  table { border-collapse: collapse; width: 100%; background: var(--card);
          border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
  th, td { border-bottom: 1px solid var(--line); padding: 6px 9px; text-align: left;
           vertical-align: top; font-size: 12.5px; }
  th { font-weight: 600; color: var(--muted); background: var(--bg); }
  tr:last-child td { border-bottom: none; }
  tr.click:hover td { background: #0b6bcb0d; cursor: pointer; }

  /* trace 抽屉 */
  .drawer { position: fixed; top: 0; right: 0; width: min(520px, 92vw); height: 100vh;
            background: var(--card); border-left: 1px solid var(--line); padding: 16px 18px;
            overflow-y: auto; box-shadow: -8px 0 24px #0002; z-index: 20; }
  .drawer h3 { font-size: 14px; margin: 0 0 4px; word-break: break-all; }
  .ev { display: grid; grid-template-columns: 62px 96px 1fr; gap: 8px; padding: 5px 0;
        border-bottom: 1px dashed var(--line); font-size: 12px; }
  .ev .t { color: var(--muted); }
  .ev .e { font-weight: 600; }
  .ev .d { color: var(--muted); word-break: break-all; }
  .close { position: absolute; top: 12px; right: 14px; }
</style>
</head>
<body>
<h1>Multi-Agent Hub</h1>
<div class="sub">当前任务看板 · 流水线进度 · 历史汇总（只读；token 存在浏览器 localStorage）</div>
<div class="bar">
  <input id="token" type="password" placeholder="X-Hub-Token">
  <button onclick="saveToken()">保存 token</button>
  <button onclick="refreshAll(true)">刷新</button>
  <span id="stats" class="muted"></span>
</div>

<h2>当前任务 <span class="count">失败可直接点开看哪一步、为什么</span></h2>
<div class="metrics" id="summary"></div>
<div class="board" id="board" style="margin-top:10px"></div>

<h2>流水线 <span class="count" id="runsMeta"></span></h2>
<div id="runsHint" class="muted" style="font-size:12.5px"></div>
<div id="runs"></div>

<h2>Agents <span class="count" id="agentsMeta"></span></h2>
<table><thead><tr><th>agent_id</th><th>状态</th><th>角色 / 动作</th><th>当前任务</th><th>labels</th><th>心跳</th></tr></thead>
<tbody id="agents"></tbody></table>

<h2>历史汇总 <span class="count" id="histMeta"></span></h2>
<div id="hist"></div>

<h2>任务明细
  <span class="count" id="tasksMeta"></span>
  <label class="muted" style="font-size:12px;font-weight:400">
    <input type="checkbox" id="showAll"> 含已完成历史
  </label>
  <label class="muted" style="font-size:12px;font-weight:400">
    <input type="checkbox" id="showMeta"> 含协调官查询任务
  </label>
</h2>
<table><thead><tr><th>id</th><th>capability/action</th><th>state</th><th>agent</th><th>尝试</th><th>耗时 / 错误</th></tr></thead>
<tbody id="tasks"></tbody></table>

<div id="drawer" class="drawer hidden">
  <button class="close" onclick="closeDrawer()">关闭</button>
  <h3 id="drawerTitle"></h3>
  <div class="muted" style="font-size:12px;margin-bottom:10px" id="drawerSub"></div>
  <div id="drawerBody"></div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const TERMINAL = ["succeeded", "failed", "timeout", "canceled"];
const FAILED_STATES = ["failed", "timeout", "canceled"];
const BOARD_COLS = [
  ["queued", "待派发", ["queued"]],
  ["assigned", "执行中", ["assigned"]],
  ["failed", "失败", FAILED_STATES],
];
const RUNS_EVERY = 30000;      // runs 查询本身会生成 coordinator 任务，放宽到 30s
const TASKS_EVERY = 3000;
let lastRuns = 0, runsTimer = null, tasksCache = [];

function token() { return $("token").value || localStorage.getItem("hub_token") || ""; }
function saveToken() { localStorage.setItem("hub_token", $("token").value); refreshAll(true); }
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

async function api(path) {
  const r = await fetch(path, { headers: { "X-Hub-Token": token() } });
  if (!r.ok) throw new Error(r.status + " " + (await r.text()));
  return r.json();
}
async function rpc(method, params) {
  const r = await fetch("/rpc", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Hub-Token": token() },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params: params || {} }),
  });
  const j = await r.json();
  if (j.error) throw new Error(j.error.message || JSON.stringify(j.error));
  return j.result;                       // task 记录
}
function dur(a, b) {
  if (!a) return "-";
  const s = ((b || Date.now() / 1000) - a);
  return s < 1 ? Math.round(s * 1000) + "ms" : s.toFixed(1) + "s";
}
function short(obj, n) {
  if (obj === null || obj === undefined) return "";
  const s = typeof obj === "string" ? obj : JSON.stringify(obj);
  return s.length > n ? s.slice(0, n) + "…" : s;
}
function errText(t) {
  const e = t && t.error;
  if (!e) return "";
  if (typeof e === "string") return short(e, 180);
  return short(e.message || e.error || e.detail || e.reason || e, 180);
}
function clipParams(p) {
  const keep = {};
  Object.keys(p || {}).forEach(k => { if (k !== "audit_level") keep[k] = p[k]; });
  return short(keep, 90);
}
function isMeta(t) { return t.capability === "coordinator" && ["runs", "status"].includes(t.action); }

/* ---------- 流水线 ---------- */
async function loadRuns(force) {
  if (!force && Date.now() - lastRuns < RUNS_EVERY - 500) return;
  lastRuns = Date.now();
  try {
    const task = await rpc("coordinator.runs");
    if (!task || task.state !== "succeeded" || !task.result) {
      $("runsHint").textContent = "协调官未在线或未注册 coordinator 能力，流水线视图暂不可用（任务看板仍可用）。"
        + (task && task.error ? " 原因：" + short(task.error, 120) : "");
      $("runs").innerHTML = "";
      $("runsMeta").textContent = "";
      return;
    }
    const all = task.result || [];
    const running = all.filter(r => r.state === "running").slice(0, 5);
    const recent = all.filter(r => r.state !== "running")
                      .sort((a, b) => (b.created || 0) - (a.created || 0)).slice(0, 3);
    const runs = [...running, ...recent];
    $("runsHint").textContent = "执行中的 run 全显示（最多 5 条），另取最近完成 3 条 · 上次刷新 "
      + new Date(lastRuns).toLocaleTimeString();
    $("runsMeta").textContent = (running.length ? running.length + " 条执行中" : "当前无执行中");
    $("runs").innerHTML = runs.map(r => renderRun(r)).join("")
      || '<div class="empty">还没有 run。流水线可用 <code>multi-agent pipeline --name ...</code> 启动。</div>';
  } catch (e) {
    $("runsHint").textContent = "流水线视图加载失败：" + e.message;
  }
}
const expandedRuns = new Set();
function renderRun(r) {
  const steps = Object.entries(r.steps || {});
  const pills = steps.map(([name, s], j) =>
    `<span class="step s-${esc(s.state)}" title="${esc(short(s.error || "", 160))}">`
    + `${esc(name)} · ${esc(s.state)}${s.started ? " · " + dur(s.started, s.ended) : ""}</span>`
    + (j < steps.length - 1 ? '<span class="arrow">→</span>' : "")).join("");
  const rows = steps.map(([name, s]) =>
    `<div class="steprow">
       <div><code>${esc(name)}</code></div>
       <div class="s-${esc(s.state)}">${esc(s.state)}</div>
       <div class="muted">${s.started ? dur(s.started, s.ended) : "-"}</div>
       <div class="res">${esc(short(s.error || s.result, 220))}
         ${s.task_id ? `<br><a href="#" onclick="openTrace('${esc(s.task_id)}','${esc(name)}');return false;">`
                       + `<code>${esc(s.task_id)}</code> 查看 trace</a>` : ""}</div>
     </div>`).join("");
  return `<div class="run">
    <div class="runhead">
      <span class="name">${esc(r.pipeline)}</span>
      <span class="step s-${esc(r.state)}">${esc(r.state)}</span>
      <code class="muted">${esc(r.run_id)}</code>
      <span class="muted">${dur(r.created, r.updated)}</span>
      <span class="grow"></span>
      <span class="muted">${esc(clipParams(r.params))}</span>
      <button onclick="toggleRun('${esc(r.run_id)}')">步骤明细</button>
    </div>
    <div class="steps">${pills || '<span class="muted">无步骤</span>'}</div>
    <div class="stepdetail ${expandedRuns.has(r.run_id) ? "" : "hidden"}" id="runDetail_${esc(r.run_id)}">${rows}</div>
  </div>`;
}
function toggleRun(runId) {
  const el = $("runDetail_" + runId);
  if (!el) return;
  if (expandedRuns.has(runId)) {
    expandedRuns.delete(runId);
    el.classList.add("hidden");
  } else {
    expandedRuns.add(runId);
    el.classList.remove("hidden");
  }
}

/* ---------- 当前任务 + 汇总 ---------- */
async function loadTasks() {
  const t = await api("/v1/tasks?limit=200");
  tasksCache = t.tasks || [];
  renderSummary();
  renderBoard();
  renderHistory();
  renderTasks();
}
function agentTask(agentId) {
  return tasksCache.find(t => t.agent_id === agentId && !TERMINAL.includes(t.state));
}
function renderSummary() {
  const src = tasksCache.filter(t => !isMeta(t));   // 协调官查询任务不计入成功率
  const cur = src.filter(t => !TERMINAL.includes(t.state));
  const fail = src.filter(t => FAILED_STATES.includes(t.state));
  const done = src.filter(t => t.state === "succeeded");
  const denom = done.length + fail.length;
  const rate = denom ? Math.round(done.length / denom * 100) : 0;
  const avg = done.length
    ? done.reduce((s, t) => s + ((t.updated || t.created) - t.created), 0) / done.length : 0;
  $("summary").innerHTML = `
    <div class="metric"><b class="${cur.length ? 'warn' : ''}">${cur.length}</b><span class="k">当前在跑 / 待派</span></div>
    <div class="metric"><b class="${fail.length ? 'bad' : ''}">${fail.length}</b><span class="k">失败</span></div>
    <div class="metric"><b>${done.length}</b><span class="k">已完成（近 ${src.length} 条内）</span></div>
    <div class="metric"><b>${rate}%</b><span class="k">成功率（同窗口）</span></div>
    <div class="metric"><b>${avg ? dur(0, avg) : "-"}</b><span class="k">平均耗时（成功任务）</span></div>`;
}
function renderBoard() {
  const src = tasksCache.filter(t => $("showMeta").checked || !isMeta(t));
  $("board").innerHTML = BOARD_COLS.map(([key, label, states]) => {
    const items = src.filter(t => states.includes(t.state));
    const body = items.map(t => `<div class="card st-${esc(t.state)}" onclick="openTrace('${esc(t.id)}','${esc(t.capability)}/${esc(t.action)}')">
        <div class="top"><span class="cap">${esc(t.capability)}/${esc(t.action)}</span>
          <span class="muted"><code>${esc(t.id.slice(-6))}</code></span></div>
        <div class="meta"><span>${esc(t.agent_id || "未派发")}</span>
          <span>${t.attempts > 1 ? "重试 " + t.attempts : dur(t.created, t.updated)}</span>
          <span>${esc(t.state)}</span></div>
        ${errText(t) ? `<div class="err">${esc(errText(t))}</div>` : ""}
      </div>`).join("");
    return `<div class="col"><div class="colhead"><span>${label}</span><span>${items.length}</span></div>`
      + (body || '<div class="empty">无</div>') + `</div>`;
  }).join("");
}
function renderHistory() {
  const fin = tasksCache.filter(t => TERMINAL.includes(t.state) && !isMeta(t));
  const byCap = {};
  fin.forEach(t => {
    const k = t.capability || "-";
    byCap[k] = byCap[k] || { ok: 0, bad: 0, total: 0, n: 0 };
    byCap[k].total += (t.updated || t.created) - t.created;
    byCap[k].n += 1;
    if (t.state === "succeeded") byCap[k].ok += 1; else byCap[k].bad += 1;
  });
  const rows = Object.entries(byCap).sort((a, b) => b[1].ok + b[1].bad - (a[1].ok + a[1].bad));
  $("histMeta").textContent = "近 " + tasksCache.length + " 条任务内，已完成 " + fin.length + " 条";
  if (!rows.length) { $("hist").innerHTML = '<div class="empty">窗口内还没有完成或失败的任务</div>'; return; }
  $("hist").innerHTML = `<table><thead><tr>
      <th>capability</th><th>成功</th><th>失败</th><th>成功率</th><th>平均耗时</th></tr></thead><tbody>
      ${rows.map(([cap, v]) => `<tr>
        <td><code>${esc(cap)}</code></td>
        <td class="ok">${v.ok}</td>
        <td class="${v.bad ? 'bad' : 'muted'}">${v.bad}</td>
        <td>${v.ok + v.bad ? Math.round(v.ok / (v.ok + v.bad) * 100) + "%" : "-"}</td>
        <td class="muted">${v.n ? dur(0, v.total / v.n) : "-"}</td></tr>`).join("")}
    </tbody></table>`;
}
function renderTasks() {
  const showAll = $("showAll").checked, showMeta = $("showMeta").checked;
  const list = tasksCache.filter(t =>
    (showAll || !["succeeded"].includes(t.state)) && (showMeta || !isMeta(t)));
  $("tasksMeta").textContent = list.length + " 条";
  $("tasks").innerHTML = list.map(t => `<tr class="click"
      onclick="openTrace('${esc(t.id)}','${esc(t.capability)}/${esc(t.action)}')">
      <td><code>${esc(t.id)}</code></td>
      <td><code>${esc(t.capability)}/${esc(t.action)}</code></td>
      <td class="${t.state === 'succeeded' ? 'ok' : (FAILED_STATES.includes(t.state) ? 'bad' : '')}">${esc(t.state)}</td>
      <td><code>${esc(t.agent_id || "")}</code></td>
      <td>${t.attempts || 0}</td>
      <td class="muted">${esc(t.error ? errText(t) : dur(t.created, t.updated))}</td>
    </tr>`).join("") || '<tr><td colspan="6" class="empty">没有当前任务</td></tr>';
}

/* ---------- Agents ---------- */
async function loadAgents() {
  const a = await api("/v1/agents");
  $("stats").textContent = "存活 " + a.stats.agents_alive + " · 任务 " + JSON.stringify(a.stats.tasks);
  $("agentsMeta").textContent = (a.agents || []).length + " 个已注册";
  $("agents").innerHTML = a.agents.map(x => {
    const cur = agentTask(x.agent_id);
    return `<tr>
      <td><code>${esc(x.agent_id)}</code></td>
      <td class="${x.alive ? 'ok' : 'bad'}">${x.alive ? 'alive' : 'expired'}</td>
      <td>${x.roles.map(r => `<code>${esc(r.name)}</code>: ${esc((r.actions || []).join(', '))}`).join('<br>')}</td>
      <td>${cur ? `<code>${esc(cur.capability)}/${esc(cur.action)}</code>` : '<span class="muted">空闲</span>'}</td>
      <td><code>${esc(short(x.labels, 60))}</code></td>
      <td class="muted">${esc(x.age_s)}s 前</td></tr>`;
  }).join("");
}

/* ---------- trace 抽屉（失败时直接给错误原因） ---------- */
async function openTrace(taskId, label) {
  const t = tasksCache.find(x => x.id === taskId);
  $("drawer").classList.remove("hidden");
  $("drawerTitle").innerHTML = (t && FAILED_STATES.includes(t.state) ? '<span class="bad">失败 · </span>' : "")
    + esc(label || "任务") + ' <code class="muted">' + esc(taskId) + "</code>";
  const err = t ? errText(t) : "";
  $("drawerSub").innerHTML = (err ? `<div class="bad" style="margin:2px 0 6px">错误：${esc(err)}</div>` : "")
    + `<span class="muted">加载 trace…</span>`;
  $("drawerBody").innerHTML = "";
  try {
    const d = await api("/v1/tasks/" + encodeURIComponent(taskId) + "/trace");
    const evs = d.events || [];
    $("drawerSub").innerHTML = (err ? `<div class="bad" style="margin:2px 0 6px">错误：${esc(err)}</div>` : "")
      + `<span class="muted">${evs.length} 条事件 · 提交 → 派发 → 结果</span>`;
    $("drawerBody").innerHTML = evs.map(e => `<div class="ev">
        <span class="t">${new Date(e.ts * 1000).toLocaleTimeString()}</span>
        <span class="e">${esc(e.event)}</span>
        <span class="d">${esc(short(e.detail, 220))}</span></div>`).join("")
      || '<div class="muted">没有事件记录</div>';
  } catch (e) {
    $("drawerSub").innerHTML = (err ? `<div class="bad">错误：${esc(err)}</div>` : "")
      + `<div class="muted">trace 加载失败：${esc(e.message)}</div>`;
  }
}
function closeDrawer() { $("drawer").classList.add("hidden"); }

/* ---------- 调度 ---------- */
async function refreshAll(force) {
  try {
    await Promise.all([loadAgents(), loadTasks()]);
  } catch (e) {
    $("stats").textContent = "加载失败：" + e.message;
  }
  await loadRuns(force);
}
function tick() {
  if (document.visibilityState !== "visible") return;
  refreshAll(false);
}
$("showAll").addEventListener("change", () => { renderTasks(); });
$("showMeta").addEventListener("change", () => { renderBoard(); renderTasks(); });
$("token").value = localStorage.getItem("hub_token") || "";
refreshAll(true);
runsTimer = setInterval(tick, TASKS_EVERY);
</script>
</body></html>"""