CONSOLE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Multi-Agent Hub</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.5 -apple-system, "Segoe UI", "Noto Sans SC", sans-serif; margin: 24px; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .sub { opacity: .65; margin-bottom: 16px; }
  .bar { display: flex; gap: 8px; align-items: center; margin-bottom: 16px; flex-wrap: wrap; }
  input { padding: 6px 8px; width: 260px; }
  button { padding: 6px 12px; cursor: pointer; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 20px; }
  th, td { border-bottom: 1px solid #8884; padding: 6px 8px; text-align: left; vertical-align: top; }
  th { font-weight: 600; opacity: .8; }
  code { font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
  .ok { color: #1a7f37; } .bad { color: #b42318; } .muted { opacity: .6; }
</style>
</head>
<body>
<h1>Multi-Agent Hub</h1>
<div class="sub">Agent 注册表与任务流水（本页只读；token 存在浏览器 localStorage）</div>
<div class="bar">
  <input id="token" type="password" placeholder="X-Hub-Token">
  <button onclick="saveToken()">保存 token</button>
  <button onclick="refresh()">刷新</button>
  <span id="stats" class="muted"></span>
</div>
<h2 style="font-size:15px">Agents</h2>
<table><thead><tr><th>agent_id</th><th>状态</th><th>角色 / 动作</th><th>labels</th><th>last_seen</th></tr></thead>
<tbody id="agents"></tbody></table>
<h2 style="font-size:15px">Tasks（最近 50）</h2>
<table><thead><tr><th>id</th><th>capability/action</th><th>state</th><th>agent</th><th>耗时/错误</th></tr></thead>
<tbody id="tasks"></tbody></table>
<script>
const $ = (id) => document.getElementById(id);
function token() { return $("token").value || localStorage.getItem("hub_token") || ""; }
function saveToken() { localStorage.setItem("hub_token", $("token").value); refresh(); }
async function api(path) {
  const r = await fetch(path, { headers: { "X-Hub-Token": token() } });
  if (!r.ok) throw new Error(r.status + " " + (await r.text()));
  return r.json();
}
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
async function refresh() {
  try {
    const a = await api("/v1/agents");
    $("stats").textContent = `存活 ${a.stats.agents_alive} · 任务 ${JSON.stringify(a.stats.tasks)}`;
    $("agents").innerHTML = a.agents.map(x => `<tr>
      <td><code>${esc(x.agent_id)}</code></td>
      <td class="${x.alive ? 'ok' : 'bad'}">${x.alive ? 'alive' : 'expired'}</td>
      <td>${x.roles.map(r => `<code>${esc(r.name)}</code>: ${esc((r.actions||[]).join(', '))}`).join('<br>')}</td>
      <td><code>${esc(JSON.stringify(x.labels))}</code></td>
      <td class="muted">${esc(x.age_s)}s 前</td></tr>`).join("");
    const t = await api("/v1/tasks?limit=50");
    $("tasks").innerHTML = t.tasks.map(x => `<tr>
      <td><code>${esc(x.id)}</code></td>
      <td><code>${esc(x.capability)}/${esc(x.action)}</code></td>
      <td class="${x.state === 'succeeded' ? 'ok' : (['failed','timeout','canceled'].includes(x.state) ? 'bad' : '')}">${esc(x.state)}</td>
      <td><code>${esc(x.agent_id || '')}</code></td>
      <td class="muted">${esc(x.error ? JSON.stringify(x.error) : ((x.updated - x.created).toFixed(1) + 's'))}</td></tr>`).join("");
  } catch (e) { $("stats").textContent = "加载失败：" + e.message; }
}
$("token").value = localStorage.getItem("hub_token") || "";
refresh(); setInterval(refresh, 3000);
</script>
</body></html>"""