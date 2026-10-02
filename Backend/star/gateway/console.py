"""Developer **ops console** served at ``/`` by the gateway.

IMPORTANT — this is *not* the Star frontend and does not replace it. The visible
Star remains the PySide6 HUD in ``Frontend/`` (blueprint CORE RULE). This page is
a diagnostics bench for the gateway/brain/tool/security layers: it lets a human
watch the mission event stream, approve high-risk actions, inspect the tool
registry, memory, audit log and background workspaces, and hit the emergency stop.

Everything is inline (no CDN, no build step) so it also works inside a sandboxed
preview iframe with no network access.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["render_console", "CONSOLE_TITLE"]

CONSOLE_TITLE = "STAR 2.0 · Gateway Ops Console"

_CSS = """
:root{
  --navy:#070d1c; --navy2:#0b1428; --glass:#0e1a33cc; --line:#1d3a63;
  --cyan:#31e1f7; --cyan-dim:#1a8ba3; --gold:#ffc857; --ok:#3ddc97;
  --alert:#ff5470; --purple:#a06bff; --text:#dce9ff; --dim:#7f97bd;
  --mono:ui-monospace,"Cascadia Mono",Consolas,"Courier New",monospace;
  --sans:"Segoe UI",system-ui,-apple-system,sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:radial-gradient(1200px 700px at 20% -10%,#12233f 0%,var(--navy) 55%,#04070f 100%);color:var(--text);font-family:var(--sans)}
body{display:flex;flex-direction:column;height:100vh;overflow:hidden}
header{display:flex;align-items:center;gap:14px;padding:10px 16px;border-bottom:1px solid var(--line);background:linear-gradient(90deg,#0b1428 0%,#101f3d 100%)}
header h1{font-size:14px;letter-spacing:.16em;margin:0;text-transform:uppercase;color:var(--cyan);font-weight:600}
header .sub{font-size:11px;color:var(--dim);letter-spacing:.08em}
.spacer{flex:1}
.badge{font-family:var(--mono);font-size:10.5px;padding:3px 8px;border:1px solid var(--line);border-radius:3px;color:var(--dim);background:#08111f;letter-spacing:.06em}
.badge.on{color:var(--ok);border-color:#1d5b41}
.badge.warn{color:var(--gold);border-color:#5b4a1d}
.badge.bad{color:var(--alert);border-color:#5b1d2b}
.dot{width:8px;height:8px;border-radius:50%;background:var(--alert);box-shadow:0 0 8px currentColor;display:inline-block;margin-right:6px}
.dot.live{background:var(--ok)}
button{font-family:var(--sans);font-size:12px;letter-spacing:.06em;color:var(--cyan);background:#0a1730;border:1px solid var(--cyan-dim);border-radius:4px;padding:6px 12px;cursor:pointer;transition:.15s}
button:hover{background:#102545;box-shadow:0 0 12px #31e1f733}
button.danger{color:var(--alert);border-color:#7a2233}
button.danger:hover{background:#2a0c14;box-shadow:0 0 14px #ff547044}
button.gold{color:var(--gold);border-color:#6b5320}
button:disabled{opacity:.4;cursor:not-allowed}
main{flex:1;display:grid;grid-template-columns:minmax(340px,42%) 1fr;gap:12px;padding:12px;min-height:0}
.col{display:flex;flex-direction:column;gap:12px;min-height:0}
.panel{background:var(--glass);border:1px solid var(--line);border-radius:6px;display:flex;flex-direction:column;min-height:0;overflow:hidden;backdrop-filter:blur(6px)}
.panel>h2{margin:0;padding:8px 12px;font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px}
.panel>h2 .spacer{flex:1}
.scroll{overflow:auto;padding:10px 12px;flex:1;min-height:0}
.scroll::-webkit-scrollbar{width:8px;height:8px}
.scroll::-webkit-scrollbar-thumb{background:#1d3a63;border-radius:4px}
#chat{flex:1}
.msg{margin-bottom:10px;padding:8px 10px;border-radius:5px;border:1px solid transparent;font-size:13px;line-height:1.45;white-space:pre-wrap;word-break:break-word}
.msg.user{background:#0d1c36;border-color:#1d3a63}
.msg.star{background:#08202a;border-color:#155e6b}
.msg.sys{background:#150f22;border-color:#3d2b5e;color:#cbb6ff;font-family:var(--mono);font-size:11.5px}
.msg .who{display:block;font-family:var(--mono);font-size:10px;letter-spacing:.12em;color:var(--dim);margin-bottom:4px;text-transform:uppercase}
.msg .meta{display:block;margin-top:6px;font-family:var(--mono);font-size:10.5px;color:var(--dim)}
.composer{display:flex;gap:8px;padding:10px;border-top:1px solid var(--line);background:#08111f}
.composer input[type=text]{flex:1;background:#050b16;border:1px solid var(--line);color:var(--text);padding:9px 10px;border-radius:4px;font-size:13px;font-family:var(--sans)}
.composer input[type=text]:focus{outline:none;border-color:var(--cyan);box-shadow:0 0 10px #31e1f733}
select{background:#050b16;border:1px solid var(--line);color:var(--text);border-radius:4px;padding:6px;font-size:12px}
.evt{font-family:var(--mono);font-size:11.5px;padding:4px 6px;border-left:2px solid var(--line);margin-bottom:4px;background:#08111f99;display:flex;gap:8px;align-items:baseline}
.evt .t{color:var(--dim);flex:0 0 62px}
.evt .k{flex:0 0 168px;color:var(--cyan)}
.evt .p{color:var(--text);opacity:.85;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.evt.task{border-color:var(--gold)} .evt.task .k{color:var(--gold)}
.evt.tool{border-color:var(--purple)} .evt.tool .k{color:var(--purple)}
.evt.security{border-color:var(--alert)} .evt.security .k{color:var(--alert)}
.evt.memory,.evt.learning{border-color:var(--ok)} .evt.memory .k,.evt.learning .k{color:var(--ok)}
.evt.voice{border-color:#5ad1ff} .evt.voice .k{color:#5ad1ff}
.chips{display:flex;gap:6px;flex-wrap:wrap;padding:8px 12px;border-bottom:1px solid var(--line)}
.chip{font-family:var(--mono);font-size:10px;padding:3px 8px;border:1px solid var(--line);border-radius:99px;cursor:pointer;color:var(--dim)}
.chip.active{color:var(--navy);background:var(--cyan);border-color:var(--cyan)}
.tabs{display:flex;gap:4px;padding:8px 10px 0;flex-wrap:wrap}
.tab{font-size:11px;letter-spacing:.08em;text-transform:uppercase;padding:6px 10px;border:1px solid var(--line);border-bottom:none;border-radius:4px 4px 0 0;cursor:pointer;color:var(--dim);background:#08111f}
.tab.active{color:var(--cyan);background:#0d1c36;border-color:var(--cyan-dim)}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;font-family:var(--mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);padding:6px 8px;border-bottom:1px solid var(--line);position:sticky;top:0;background:#0b1428}
td{padding:6px 8px;border-bottom:1px solid #142744;vertical-align:top;color:var(--text)}
td.mono,.mono{font-family:var(--mono);font-size:11px}
.pill{font-family:var(--mono);font-size:10px;padding:2px 6px;border-radius:99px;border:1px solid var(--line);color:var(--dim)}
.pill.low{color:var(--ok);border-color:#1d5b41}.pill.medium{color:var(--gold);border-color:#5b4a1d}
.pill.high{color:#ff9f45;border-color:#6b3f1d}.pill.critical{color:var(--alert);border-color:#7a2233}
.pill.done,.pill.ok,.pill.succeeded{color:var(--ok);border-color:#1d5b41}
.pill.failed,.pill.denied{color:var(--alert);border-color:#7a2233}
.pill.running,.pill.pending{color:var(--cyan);border-color:var(--cyan-dim)}
.confirm{border:1px solid var(--gold);background:#1a1408;padding:10px;border-radius:5px;margin-bottom:8px}
.confirm h3{margin:0 0 6px;font-size:12.5px;color:var(--gold);letter-spacing:.06em}
.confirm p{margin:4px 0;font-size:12px;color:var(--text)}
.confirm .row{display:flex;gap:8px;margin-top:8px}
.empty{color:var(--dim);font-size:12px;font-style:italic;padding:8px 0}
footer{padding:6px 14px;border-top:1px solid var(--line);font-family:var(--mono);font-size:10.5px;color:var(--dim);display:flex;gap:14px;align-items:center;background:#0b1428ee}
kbd{font-family:var(--mono);background:#0a1730;border:1px solid var(--line);border-radius:3px;padding:1px 5px;font-size:10px}
@media(max-width:900px){main{grid-template-columns:1fr;overflow:auto}body{overflow:auto}}
"""

_JS = """
const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => Array.from(r.querySelectorAll(s));
const INFO = window.__STAR_INFO__ || {};
const state = { ws:null, seq:0, filter:new Set(), tab:'tasks', poll:null, reconnects:0, listening:false };

const PHASES = ['gateway','voice','brain','plan','task','tool','workspace','memory','security','system'];
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const hhmmss = iso => { try { const d = new Date(iso); return d.toTimeString().slice(0,8); } catch(e){ return '--:--:--'; } };
const short = (o, n=140) => { const s = typeof o === 'string' ? o : JSON.stringify(o); return s.length > n ? s.slice(0,n)+'…' : s; };

function log(who, text, meta, cls='sys'){
  const box = $('#chat'); const el = document.createElement('div');
  el.className = 'msg ' + cls;
  el.innerHTML = `<span class="who">${esc(who)}</span>${esc(text)}${meta?`<span class="meta">${esc(meta)}</span>`:''}`;
  box.appendChild(el); box.scrollTop = box.scrollHeight;
  while (box.children.length > 200) box.removeChild(box.firstChild);
}

function addEvent(ev){
  state.seq = Math.max(state.seq, ev.seq || 0);
  if (state.filter.size && !state.filter.has(ev.phase) && !state.filter.has(ev.kind)) return;
  const box = $('#events'); const el = document.createElement('div');
  el.className = 'evt ' + (ev.phase || '');
  el.innerHTML = `<span class="t">${hhmmss(ev.ts)}</span><span class="k">${esc(ev.kind)}</span><span class="p" title="${esc(JSON.stringify(ev.payload))}">${esc(short(ev.payload))}</span>`;
  box.appendChild(el);
  while (box.children.length > 400) box.removeChild(box.firstChild);
  if ($('#autoscroll').checked) box.scrollTop = box.scrollHeight;
  if (ev.kind === 'confirmation.requested') renderConfirmation(ev.payload);
  if (ev.kind === 'task.completed' || ev.kind === 'task.failed') refreshTab();
}

async function api(path, opts={}){
  const r = await fetch(path, Object.assign({headers:{'Content-Type':'application/json'}}, opts));
  const text = await r.text();
  try { return { status:r.status, data: JSON.parse(text) }; } catch(e){ return { status:r.status, data:text }; }
}

function connect(){
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}/ws?session_id=console`;
  let ws;
  try { ws = new WebSocket(url); } catch(e){ startPolling(); return; }
  state.ws = ws;
  ws.onopen = () => { $('#conn').classList.add('live'); $('#connTxt').textContent='WS LIVE'; stopPolling(); state.reconnects=0; };
  ws.onmessage = m => {
    let msg; try { msg = JSON.parse(m.data); } catch(e){ return; }
    if (msg.type === 'hello'){
      $('#connTxt').textContent = 'WS LIVE';
      log('gateway', `connected · protocol ${msg.protocol} · session ${msg.session_id}`,
          `dry_run=${msg.info?.settings?.security?.dry_run} safety=${msg.info?.settings?.security?.safety_level}`);
      (msg.replay||[]).forEach(addEvent);
    } else if (msg.type === 'event') addEvent(msg.event);
    else if (msg.type === 'result') handleResult(msg);
    else if (msg.type === 'error') log('error', msg.error || 'unknown error', '', 'sys');
    else if (msg.type === 'ack') { /* ignore */ }
  };
  ws.onclose = () => { $('#conn').classList.remove('live'); $('#connTxt').textContent='WS DOWN';
    if (!state.poll) startPolling();
    if (state.reconnects++ < 40) setTimeout(connect, 1200 + Math.min(state.reconnects*400, 6000)); };
  ws.onerror = () => { try{ ws.close(); }catch(e){} };
}

function send(obj){
  if (state.ws && state.ws.readyState === 1){ state.ws.send(JSON.stringify(obj)); return true; }
  return false;
}

async function sendChat(){
  const input = $('#input'); const text = input.value.trim();
  if (!text) return;
  input.value = '';
  log('you', text, '', 'user');
  if (!send({type:'chat', text, language: $('#lang').value === 'auto' ? undefined : $('#lang').value, request_id:'c'+Date.now()})){
    const r = await api('/api/v1/chat', {method:'POST', body: JSON.stringify({text, source:'console'})});
    if (r.status === 200) handleResult(r.data); else log('error', `HTTP ${r.status}: ${short(r.data)}`);
  }
}

function handleResult(msg){
  const d = msg;
  const reply = d.response || d.summary || d.message || '';
  if (reply) log('star', reply, metaLine(d), 'star');
  if (d.plan && d.plan.tasks && d.plan.tasks.length){
    const lines = d.plan.tasks.map(t => `  ▸ [${t.agent}] ${t.goal} → ${t.state}`).join('\\n');
    log('plan', `${d.plan.intent || 'intent'} · ${d.plan.tasks.length} task(s)\\n${lines}`, d.plan.rationale || '', 'sys');
  }
  if (d.audio_url) log('tts', `audio ready: ${d.audio_url}`, '', 'sys');
  if (d.ok === false && !reply) log('error', short(d.error || d), '', 'sys');
  refreshTab();
}

function metaLine(d){
  const bits = [];
  if (d.language) bits.push('lang=' + d.language);
  if (d.source) bits.push('src=' + d.source);
  if (d.latency_ms != null) bits.push(d.latency_ms.toFixed(0) + 'ms');
  if (d.task_count != null) bits.push('tasks=' + d.task_count);
  if (d.dry_run != null) bits.push('dry_run=' + d.dry_run);
  return bits.join(' · ');
}

function renderConfirmation(p){
  const box = $('#confirms'); box.innerHTML = '';
  const el = document.createElement('div'); el.className = 'confirm';
  el.innerHTML = `<h3>⚠ ${esc(p.risk || 'HIGH')} RISK — confirmation required</h3>
    <p><b>${esc(p.tool || p.title || 'action')}</b></p>
    <p class="mono">${esc(short(p.arguments || p.detail || {}, 220))}</p>
    <p class="mono">id ${esc(p.confirmation_id || '')} · task ${esc(p.task_id || '')} · expires in ${esc(p.ttl_s || '?')}s</p>
    <div class="row"><button class="gold" data-a="1">Approve</button><button class="danger" data-a="0">Deny</button></div>`;
  el.querySelectorAll('button').forEach(b => b.onclick = async () => {
    const approve = b.dataset.a === '1';
    const ok = send({type:'confirm', confirmation_id: p.confirmation_id, approve});
    if (!ok) await api('/api/v1/confirmations', {method:'POST', body: JSON.stringify({confirmation_id:p.confirmation_id, approve})});
    log(approve ? 'approved' : 'denied', `${p.tool || p.confirmation_id}`, '', 'sys');
    el.remove(); refreshTab();
  });
  box.appendChild(el);
}

function startPolling(){
  if (state.poll) return;
  state.poll = setInterval(async () => {
    const r = await api(`/api/v1/events?limit=80&since=${state.seq}`);
    if (r.status === 200 && Array.isArray(r.data.events)) r.data.events.forEach(addEvent);
  }, 1500);
}
function stopPolling(){ if (state.poll){ clearInterval(state.poll); state.poll = null; } }

// ── tabs ────────────────────────────────────────────────────────────────────
const TABS = {
  tasks:   {url:'/api/v1/tasks',      render: renderTasks},
  tools:   {url:'/api/v1/tools',      render: renderTools},
  agents:  {url:'/api/v1/agents',     render: renderAgents},
  browser: {url:'/api/v1/browser',    render: renderBrowser},
  computer:{url:'/api/v1/computer',   render: renderComputer},
  memory:  {url:'/api/v1/memory',     render: renderMemory},
  learning:{url:'/api/v1/learning',   render: renderLearning},
  audit:   {url:'/api/v1/audit?limit=120', render: renderAudit},
  workspace:{url:'/api/v1/workspaces',render: renderWorkspaces},
  confirm: {url:'/api/v1/confirmations', render: renderConfirmList},
  info:    {url:'/api/v1/info',       render: renderInfo},
  health:  {url:'/api/v1/health',     render: renderHealth},
};

async function refreshTab(){
  const spec = TABS[state.tab]; if (!spec) return;
  const r = await api(spec.url);
  const body = $('#tabbody');
  if (r.status !== 200){ body.innerHTML = `<div class="empty">HTTP ${r.status}</div>`; return; }
  body.innerHTML = spec.render(r.data);
  bindTabActions(body);
}

function bindTabActions(body){
  body.querySelectorAll('[data-cancel]').forEach(b => b.onclick = async () => {
    await api(`/api/v1/tasks/${b.dataset.cancel}/cancel`, {method:'POST'}); refreshTab();
  });
  body.querySelectorAll('[data-stop]').forEach(b => b.onclick = emergencyStop);
  body.querySelectorAll('[data-browser-run]').forEach(b => b.onclick = async () => {
    const input = body.querySelector('#browsergoal');
    const goal = (input && input.value || '').trim();
    if (!goal){ log('safety', 'browser goal is empty', '', 'sys'); return; }
    log('tool', `browser goal → ${goal}`, '', 'send');
    const r = await api('/api/v1/browser', {method:'POST', body: JSON.stringify({goal})});
    log('tool', `browser run: ${r.data && r.data.state || 'http ' + r.status}`, '', 'recv');
    refreshTab();
  });
  body.querySelectorAll('[data-ws-create]').forEach(b => b.onclick = async () => {
    const kind = (body.querySelector('#wskind')||{}).value || 'generic';
    const label = ((body.querySelector('#wslabel')||{}).value || '').trim();
    log('task', `workspace → open ${kind} session${label?` “${label}”`:''}`, '', 'send');
    const r = await api('/api/v1/workspaces', {method:'POST', body: JSON.stringify({kind, label})});
    const sid = r.data && r.data.data && r.data.data.session_id || '';
    log('task', `workspace: ${r.data && r.data.decision || 'http ' + r.status} ${sid}`, '', 'recv');
    refreshTab();
  });
  body.querySelectorAll('[data-ws-ckpt]').forEach(b => b.onclick = async () => {
    const r = await api(`/api/v1/workspaces/${b.dataset.wsCkpt}/checkpoint`, {method:'POST', body: JSON.stringify({label:'console'})});
    const ck = r.data && r.data.data && r.data.data.checkpoint || {};
    log('task', `checkpoint ${ck.checkpoint_id||''} — ${ck.files??0} file(s), ${ck.bytes??0} B${ck.truncated?' (truncated)':''}`, '', 'recv');
    refreshTab();
  });
  body.querySelectorAll('[data-ws-close]').forEach(b => b.onclick = async () => {
    await api(`/api/v1/workspaces/${b.dataset.wsClose}/close`, {method:'POST', body: JSON.stringify({})});
    log('task', `workspace ${b.dataset.wsClose} closed (files kept)`, '', 'recv');
    refreshTab();
  });
  body.querySelectorAll('[data-ws-destroy]').forEach(b => b.onclick = async () => {
    if (!window.confirm("Delete this session's files? They are inside the session jail, but this cannot be undone.")) return;
    const r = await api(`/api/v1/workspaces/${b.dataset.wsDestroy}/close`, {method:'POST', body: JSON.stringify({remove_files:true})});
    const d = r.data || {};
    if (d.confirmation_id) log('safety', `deleting files needs your approval: ${d.confirmation_id} (Approvals tab)`, '', 'sys');
    else log('safety', `workspace ${b.dataset.wsDestroy}: ${d.decision||'http ' + r.status}`, '', 'recv');
    refreshTab();
  });
  body.querySelectorAll('[data-computer-run]').forEach(b => b.onclick = async () => {
    const input = body.querySelector('#computergoal');
    const goal = (input && input.value || '').trim();
    const box = body.querySelector('#computerdry');
    const dry = !box || box.checked;                 // dry-run stays the default
    if (!goal){ log('safety', 'computer goal is empty', '', 'sys'); return; }
    if (!dry && !window.confirm('Run this for real? The mouse and keyboard WILL move.')) return;
    log('tool', `computer goal → ${goal}${dry?' (dry-run)':' (REAL)'}`, '', dry?'send':'safety');
    const r = await api('/api/v1/computer', {method:'POST', body: JSON.stringify({goal, dry_run: dry})});
    const d = r.data || {};
    log('safety', `computer run: ${d.state || 'http ' + r.status} — ${d.summary || d.error || ''}`, '', 'recv');
    if (d.confirmation_id) log('safety', `needs your approval: ${d.confirmation_id} (Approvals tab)`, '', 'sys');
    refreshTab();
  });
  body.querySelectorAll('[data-learn-fb]').forEach(b => b.onclick = async () => {
    const signal = (body.querySelector('#fbsig')||{}).value || 'positive';
    const key = ((body.querySelector('#fbkey')||{}).value || '').trim();
    const value = ((body.querySelector('#fbval')||{}).value || '').trim();
    const note = ((body.querySelector('#fbnote')||{}).value || '').trim();
    if (!key && !value && !note){ log('learning', 'feedback is empty — say something or state a key/value', '', 'sys'); return; }
    log('learning', `feedback → ${signal}${key?` ${key}=${value}`:''}${note?` “${note}”`:''}`, '', 'send');
    const r = await api('/api/v1/learning', {method:'POST', body: JSON.stringify({signal, key, value, note})});
    const d = r.data || {};
    const cyc = d.cycle || {};
    log('learning', cyc.promoted ? `learned ${cyc.promoted} and stored it` : `noted (${d.ok?'ok':'http '+r.status})`, '', 'recv');
    refreshTab();
  });
  body.querySelectorAll('[data-learn-cycle]').forEach(b => b.onclick = async () => {
    log('learning', 'running one validation + promotion sweep', '', 'send');
    const r = await api('/api/v1/learning/cycle', {method:'POST', body: JSON.stringify({})});
    const d = r.data || {};
    log('learning', `cycle: considered ${d.considered??0} · validated ${d.validated??0} · rejected ${d.rejected??0} · promoted ${d.promoted??0}`, '', 'recv');
    refreshTab();
  });
}

const pill = v => `<span class="pill ${esc(String(v||'').toLowerCase())}">${esc(v ?? '—')}</span>`;
const table = (cols, rows) => rows.length
  ? `<table><thead><tr>${cols.map(c=>`<th>${esc(c[1])}</th>`).join('')}</tr></thead><tbody>${
      rows.map(r=>`<tr>${cols.map(c=>`<td class="${c[2]||''}">${c[3]?c[3](r):esc(r[c[0]] ?? '—')}</td>`).join('')}</tr>`).join('')
    }</tbody></table>`
  : '<div class="empty">nothing yet</div>';

function renderTasks(d){
  const tasks = (d.tasks||[]).slice().reverse();
  const plans = (d.plans||[]).slice().reverse();
  const t = table([['task_id','task',,v=>`<span class="mono">${esc(String(v.task_id||'').slice(0,14))}</span>`],
                   ['goal','goal'],['agent','agent'],['state','state',,v=>pill(v.state)],
                   ['risk','risk',,v=>pill(v.risk)],
                   ['calls','calls',,v=>esc((v.steps||[]).length)],
                   ['summary','summary'],
                   ['task_id','', 'mono', v => v.state==='running'||v.state==='pending' ? `<button data-cancel="${esc(v.task_id)}">cancel</button>` : '']], tasks);
  const p = table([['plan_id','plan',,v=>`<span class="mono">${esc(String(v.plan_id||'').slice(0,14))}</span>`],
                   ['intent','intent'],['requires_confirmation','confirm',,v=>pill(v.requires_confirmation?'high':'low')],
                   ['tasks','tasks',,v=>esc((v.tasks||[]).length)],['rationale','rationale']], plans);
  return `<h3 class="mono" style="color:var(--dim);font-size:11px">PLANS</h3>${p}<h3 class="mono" style="color:var(--dim);font-size:11px;margin-top:14px">TASKS</h3>${t}`;
}
function renderTools(d){
  return table([['name','tool','mono'],['category','category'],['risk','risk',,v=>pill(v.risk)],
                ['agent','agent'],['requires_confirmation','confirm',,v=>pill(v.requires_confirmation?'yes':'no')],
                ['description','description'],['source','source','mono']], d.tools||[]);
}
function renderAgents(d){
  return table([['name','agent','mono'],
                ['description','responsibility',,v=>esc(v.responsibility||v.description||'—')],
                ['tools','tools',,v=>esc((v.tools||[]).join(', '))],
                ['runs','runs',,v=>esc(v.runs ?? 0)],
                ['status','status',,v=>pill(v.status||(v.executor?'ready':'no executor'))]], d.agents||[]);
}
function renderBrowser(d){
  if (!d.ok) return `<div class="empty">${esc(d.error||'browser agent not wired')}</div>`;
  const g = (d.agent||{}).guardrails||{};
  const st = d.settings||{};
  const stats = (d.agent||{}).stats||{};
  const head = `<table><tbody>
    <tr><td class="mono">enabled</td><td>${pill(d.enabled?'yes':'no')}</td>
        <td class="mono">schemes</td><td class="mono">${esc((st.allowed_schemes||[]).join(', '))}</td></tr>
    <tr><td class="mono">private hosts</td><td>${pill(st.allow_private_hosts?'allowed':'refused')}</td>
        <td class="mono">auto-open</td><td>${pill(st.auto_open?'on':'off')}</td></tr>
    <tr><td class="mono">max steps</td><td class="mono">${esc(st.max_steps)}</td>
        <td class="mono">timeout / cap</td><td class="mono">${esc(st.timeout_s)}s · ${esc(st.max_bytes)} B</td></tr>
    <tr><td class="mono">url checks</td><td class="mono">${esc(g.checks??0)} (${esc(g.refused??0)} refused)</td>
        <td class="mono">agent runs</td><td class="mono">${esc(d.runs??0)} · steps ${esc(stats.steps??0)} · recovered ${esc(stats.recovered??0)}</td></tr>
  </tbody></table>
  <div class="row" style="margin:10px 0">
    <input id="browsergoal" placeholder="browser goal — e.g. read https://example.com" style="flex:1">
    <button class="gold" data-browser-run="1">Run goal</button>
  </div>`;
  const last = d.last_run;
  if (!last) return head + '<div class="empty">no browser run yet</div>';
  const steps = table([['index','#'],['action','action','mono'],['tool','tool','mono'],
                       ['decision','decision',,v=>pill(v.decision)],
                       ['verified','verified',,v=>pill(v.verified?'yes':(v.recovered?'recovered':'no'))],
                       ['verification_note','note',,v=>esc(v.verification_note||v.error||'—')]], last.steps||[]);
  return head + `<h3 class="mono" style="color:var(--dim);font-size:11px">LAST RUN · ${pill(last.state)} · ${esc(last.summary||'')}</h3>${steps}`;
}
function renderComputer(d){
  if (!d.ok) return `<div class="empty">${esc(d.error||'computer agent not wired')}</div>`;
  const motor = d.motor||{}, guard = d.guardrails||{}, st = d.settings||{}, stats = (d.agent||{}).stats||{};
  const head = `<table><tbody>
    <tr><td class="mono">enabled</td><td>${pill(d.enabled?'yes':'no')}</td>
        <td class="mono">motor</td><td>${pill(motor.mode||'?')} <span class="mono">${esc(motor.backend||'')}</span></td></tr>
    <tr><td class="mono">dry-run</td><td>${pill(d.dry_run?'ON — nothing moves':'OFF — actions are real')}</td>
        <td class="mono">screen</td><td class="mono">${esc(st.screen||'')} · max ${esc(st.max_steps)} steps</td></tr>
    <tr><td class="mono">motor note</td><td colspan="3" class="mono">${esc(motor.note||'—')}</td></tr>
    <tr><td class="mono">actions</td><td class="mono">${esc(motor.actions??0)} (${esc(motor.succeeded??0)} ok · ${esc(motor.failed??0)} failed)</td>
        <td class="mono">guardrails</td><td class="mono">${esc(guard.checks??0)} checks · ${esc(guard.refused??0)} refused · ${esc(guard.governor_refused??0)} governor</td></tr>
    <tr><td class="mono">governor invariants</td><td colspan="3" class="mono">${esc((guard.governor_invariants||[]).join(', '))}</td></tr>
    <tr><td class="mono">blocked hotkeys</td><td class="mono">${esc((st.blocked_hotkeys||[]).join(', ')||'—')}</td>
        <td class="mono">forbidden regions</td><td class="mono">${esc((st.forbidden_regions||[]).join(' · ')||'none')}</td></tr>
    <tr><td class="mono">agent runs</td><td class="mono">${esc(d.runs??0)} · steps ${esc(stats.steps??0)} · recovered ${esc(stats.recovered??0)}</td>
        <td class="mono">guardrail log</td><td class="mono">${esc((guard.refusals||[]).slice(0,3).map(x=>x.rule).join(', ')||'clean')}</td></tr>
  </tbody></table>
  <div class="row" style="margin:10px 0">
    <input id="computergoal" placeholder="computer goal — e.g. click at 480,320 / take a screenshot / type 'hello'" style="flex:1">
    <label class="mono" style="white-space:nowrap"><input type="checkbox" id="computerdry" checked> dry-run</label>
    <button class="gold" data-computer-run="1">Run goal</button>
  </div>`;
  const last = d.last_run;
  if (!last) return head + '<div class="empty">no computer run yet</div>';
  const steps = table([['index','#'],['action','action','mono'],['tool','tool','mono'],
                       ['decision','decision',,v=>pill(v.decision)],
                       ['verified','verified',,v=>pill(v.verified?'yes':(v.recovered?'recovered':'no'))],
                       ['verification_note','note',,v=>esc(v.verification_note||v.error||'—')]], last.steps||[]);
  const extra = last.confirmation_id
    ? `<div class="empty" style="color:var(--gold)">waiting for approval: ${esc(last.confirmation_id)} — use the Approvals tab</div>` : '';
  return head + extra + `<h3 class="mono" style="color:var(--dim);font-size:11px">LAST RUN · ${pill(last.state)} · ${esc(last.summary||'')}</h3>${steps}`;
}
const memHead = t => `<h3 class="mono" style="color:var(--dim);font-size:11px;margin-top:14px">${esc(t)}</h3>`;
function renderMemory(d){
  const layers = d.layers||{};
  const rows = Object.entries(layers).map(([k,v])=>`<tr><td class="mono">${esc(k)}</td><td>${esc(v.count ?? 0)}</td>
    <td class="mono">${(v.weight ?? 0).toFixed(2)}</td><td>${v.available===false?pill('unavailable'):pill('ok')}</td>
    <td>${esc(v.store||'')}</td><td>${esc(v.detail||'')}</td></tr>`).join('');
  const rec = (d.retrieved||[]).map(r=>`<tr><td class="mono">${esc(r.layer||'')}</td><td>${esc(r.text||'')}</td><td class="mono">${(r.score??0).toFixed(3)}</td><td>${esc(r.kind||'')}</td></tr>`).join('');
  const prefs = (d.preferences||[]).map(r=>`<tr><td class="mono">${esc(r.key)}</td><td>${esc(r.value)}</td><td class="mono">${hhmmss(r.timestamp)}</td></tr>`).join('');
  const eps = (d.episodes||[]).map(e=>`<tr><td class="mono">${hhmmss(e.started_at)}</td><td>${esc(e.goal||'')}</td>
    <td class="mono">${esc(e.steps ?? 0)}</td><td>${pill(e.succeeded?'succeeded':'failed')}</td></tr>`).join('');
  const pats = (d.patterns||[]).map(p=>`<tr><td class="mono">${esc(p.from)}</td><td class="mono">→ ${esc(p.to)}</td><td class="mono">${esc(p.count)}</td></tr>`).join('');
  const work = (d.working||[]).map(t=>`<li>${esc(t)}</li>`).join('');
  const tr = d.trace||{};
  const trace = d.query ? `<div class="empty mono" style="text-align:left">candidates ${esc(tr.candidates ?? 0)} · returned ${esc((d.retrieved||[]).length)} · duplicates dropped ${esc(tr.duplicates ?? 0)}${(tr.timeouts||[]).length?` · timed out: ${esc((tr.timeouts||[]).join(', '))}`:''}${(tr.errors||[]).length?` · errors: ${esc((tr.errors||[]).join(' | '))}`:''}</div>` : '';
  const off = d.enabled===false ? `<div class="empty" style="color:var(--gold)">layered memory is disabled (STAR_MEMORY_ENABLED=false) — retrieval and writes are refused, nothing stored is deleted</div>` : '';
  return off + `<table><thead><tr><th>layer</th><th>items</th><th>weight</th><th>state</th><th>store</th><th>detail</th></tr></thead><tbody>${rows||'<tr><td colspan=6 class="empty">empty</td></tr>'}</tbody></table>
  ${memHead('PREFERENCES')}
  <table><thead><tr><th>key</th><th>value</th><th>updated</th></tr></thead><tbody>${prefs||'<tr><td colspan=3 class="empty">none</td></tr>'}</tbody></table>
  ${memHead(`RETRIEVAL${d.query?` for “${d.query}”`:''}`)}
  <table><thead><tr><th>layer</th><th>text</th><th>score</th><th>kind</th></tr></thead><tbody>${rec||'<tr><td colspan=4 class="empty">no matches</td></tr>'}</tbody></table>${trace}
  ${memHead('EPISODIC · recent interactions')}
  <table><thead><tr><th>when</th><th>goal</th><th>steps</th><th>outcome</th></tr></thead><tbody>${eps||'<tr><td colspan=4 class="empty">nothing recorded yet</td></tr>'}</tbody></table>
  ${memHead(`WORKING · session ${d.session_id||''}`)}
  ${work?`<ul class="mono" style="margin:0;padding-left:18px;font-size:11px;line-height:1.6">${work}</ul>`:'<div class="empty">empty</div>'}
  ${memHead('PROCEDURAL PATTERNS · tool → tool (read-only in Phase 8)')}
  <table><thead><tr><th>from</th><th>next</th><th>seen</th></tr></thead><tbody>${pats||'<tr><td colspan=3 class="empty">no patterns recorded yet</td></tr>'}</tbody></table>`;
}
function renderLearning(d){
  if (d.pending) return `<div class="empty">${esc(d.detail||'learning pending')}</div>`;
  const g = d.gates||{}, t = d.targets||{}, cs = d.candidates||{}, fb = d.feedback||{}, st = d.stats||{};
  const off = d.enabled===false
    ? `<div class="empty" style="color:var(--gold)">the safe learning loop is disabled (STAR_LEARNING_ENABLED=false) — nothing is observed, validated or promoted, and no learning files are touched</div>`
    : '';
  const head = `<table><tbody>
    <tr><td class="mono">loop</td><td>${pill(d.enabled?'on':'off')} ${pill(d.auto_promote?'auto-promote':'manual')}</td>
        <td class="mono">anti-self-modification</td><td>${pill(d.no_self_modification?'enforced':'?')}</td></tr>
    <tr><td class="mono">gates</td><td colspan="3" class="mono">frequency ≥ ${esc(g.min_frequency??'?')} · success ≥ ${esc(g.min_success_rate??'?')} · risk ≤ ${esc(g.max_risk||'?')} · no executable code · no secret material${g.require_approval_for_preferences?' · preferences need explicit approval':''}</td></tr>
    <tr><td class="mono">promotes into</td><td colspan="3" class="mono">pattern store ${pill(t.pattern_store?'attached':'none')} · memory manager ${pill(t.memory_manager?'attached':'none')} <span style="color:var(--dim)">(the existing stores — never a second one)</span></td></tr>
    <tr><td class="mono">candidates</td><td class="mono">${esc(cs.count??0)} total · observing ${esc((cs.by_state||{}).observing??0)} · validated ${esc((cs.by_state||{}).validated??0)} · promoted ${esc((cs.by_state||{}).promoted??0)} · rejected ${esc((cs.by_state||{}).rejected??0)}</td>
        <td class="mono">counters</td><td class="mono">observed ${esc(st.observed??0)} · feedback ${esc(st.feedback??0)} · promoted ${esc(st.promoted??0)} · cycles ${esc(st.cycles??0)}</td></tr>
  </tbody></table>
  <div class="row" style="margin:10px 0">
    <select id="fbsig" class="mono"><option value="positive">positive</option><option value="negative">negative</option><option value="correction">correction</option></select>
    <input id="fbkey" placeholder="key (optional)" class="mono" style="width:120px">
    <input id="fbval" placeholder="value (optional)" class="mono" style="width:120px">
    <input id="fbnote" placeholder="feedback — e.g. always reply in bangla" style="flex:1">
    <button class="gold" data-learn-fb="1">Send feedback</button>
    <button data-learn-cycle="1">Run cycle</button>
  </div>`;
  const cands = table([['signature','candidate','mono'],['kind','kind',,v=>pill(v.kind)],
                       ['observations','seen','mono'],['success_rate','success','mono',v=>`${Math.round((v.success_rate??0)*100)}%`],
                       ['approvals','✓','mono'],['rejections','✗','mono'],['risk','risk',,v=>pill(v.risk)],
                       ['state','state',,v=>pill(v.state)],
                       ['reasons','why',,v=>`<span class="mono">${esc((v.reasons||[]).join('; ')||'—')}</span>`]], cs.candidates||[]);
  const fed = table([['ts','when','mono',v=>hhmmss(v.ts)],['signal','signal',,v=>pill(v.signal)],
                     ['subject','subject','mono'],['note','note',,v=>esc(v.note||'—')]], fb.recent||[]);
  const lp = d.last_promotion
    ? `<div class="empty mono" style="text-align:left">last promotion: ${esc(d.last_promotion.signature||'')} → ${esc(d.last_promotion.promoted_to||'')} (${d.last_promotion.ok?'ok':'refused'})</div>`
    : '';
  return off + head
    + memHead('CANDIDATE LEDGER · what Star is considering (inert until every gate passes)') + cands + lp
    + memHead('FEEDBACK · the human half of the loop') + fed;
}
function renderAudit(d){
  return table([['ts','time','mono',v=>hhmmss(v.ts)],['risk','risk',,v=>pill(v.risk)],['decision','decision',,v=>pill(v.decision)],
                ['tool','tool','mono'],['session_id','session','mono',v=>esc(String(v.session_id||'').slice(0,12))],
                ['detail','detail',,v=>`<span class="mono">${esc(short(v.detail||{},120))}</span>`]], d.entries||[]);
}
function renderWorkspaces(d){
  const iso = d.isolation||{}, q = d.quotas||{};
  const head = d.pending ? `<div class="empty">${esc(d.detail||'workspace pending')}</div>` : `<table><tbody>
    <tr><td class="mono">enabled</td><td>${pill(d.enabled?'yes':'no')}</td>
        <td class="mono">isolation</td><td>${pill(iso.backend||'?')} <span class="mono">${esc(iso.available===false?'unavailable':'available')}</span></td></tr>
    <tr><td class="mono">root</td><td colspan="3" class="mono">${esc(d.root||'—')} ${d.root_exists?'':'(missing)'}</td></tr>
    <tr><td class="mono">isolation note</td><td colspan="3" class="mono">${esc(iso.note||d.isolation_note||'—')}</td></tr>
    <tr><td class="mono">sessions</td><td class="mono">${esc(d.sessions_alive??0)} alive / max ${esc(d.sessions_max??0)} · tracked ${esc(d.sessions_tracked??0)} · ttl ${esc(d.session_ttl_s??0)}s</td>
        <td class="mono">quotas</td><td class="mono">${esc(q.max_files??0)} files · ${esc(q.max_bytes??0)} B · ${esc(q.max_write_bytes??0)} B/write</td></tr>
    <tr><td class="mono">counters</td><td class="mono">created ${esc(d.created??0)} · reused ${esc(d.reused??0)} · closed ${esc(d.closed??0)} · destroyed ${esc(d.destroyed??0)} · expired ${esc(d.expired??0)}</td>
        <td class="mono">safety</td><td class="mono">checkpoints ${esc(d.checkpoints??0)} · restores ${esc(d.restores??0)} · refused ${esc(d.refused??0)} · <b>jail escapes ${esc(d.escapes??0)}</b></td></tr>
  </tbody></table>
  <div class="row" style="margin:10px 0">
    <select id="wskind" class="mono"><option>generic</option><option>browser</option><option>computer</option><option>files</option><option>coding</option><option>system</option></select>
    <input id="wslabel" placeholder="label — e.g. invoice scrape" style="flex:1">
    <button class="gold" data-ws-create="1">Open session</button>
  </div>`;
  const rows = (d.workspaces||[]).map(w => `<tr>
    <td class="mono">${esc(String(w.session_id||'').slice(0,16))}</td>
    <td>${esc(w.kind||'')}</td>
    <td class="mono"><span title="${esc(w.path||'')}">${esc(String(w.path||'').slice(-40))}</span></td>
    <td>${pill(w.state)}</td>
    <td class="mono">${hhmmss(w.created_at)}</td>
    <td class="mono">${esc(w.agent||'—')}</td>
    <td class="mono">${esc(w.files??0)} · ${esc(w.bytes??0)} B</td>
    <td class="mono">${esc(w.checkpoints??0)}</td>
    <td><button data-ws-ckpt="${esc(w.session_id)}">ckpt</button>
        <button data-ws-close="${esc(w.session_id)}">close</button>
        <button class="gold" data-ws-destroy="${esc(w.session_id)}" title="delete the files — needs approval">delete</button></td>
  </tr>`).join('');
  const list = rows
    ? `<table><thead><tr><th>session</th><th>kind</th><th>path</th><th>state</th><th>created</th><th>agent</th><th>files</th><th>ckpt</th><th></th></tr></thead><tbody>${rows}</tbody></table>`
    : '<div class="empty">no background sessions yet — the browser and computer agents open one when they run</div>';
  return head + `<h3 class="mono" style="color:var(--dim);font-size:11px">SESSIONS · the invisible background desktop (jailed under the workspace root)</h3>` + list;
}
function renderConfirmList(d){
  const rows = (d.confirmations||[]);
  if (!rows.length) return '<div class="empty">no pending confirmations — policy is quiet</div>';
  return rows.map(c=>`<div class="confirm"><h3>${esc(c.risk||'').toUpperCase()} · ${esc(c.tool||'')}</h3>
    <p class="mono">${esc(short(c.arguments||{},200))}</p>
    <p class="mono">id ${esc(c.confirmation_id)} · expires ${esc(c.expires_at||'')}</p>
    <div class="row"><button class="gold" data-cid="${esc(c.confirmation_id)}" data-ap="1">Approve</button>
    <button class="danger" data-cid="${esc(c.confirmation_id)}" data-ap="0">Deny</button></div></div>`).join('');
}
function renderInfo(d){ return `<pre class="mono" style="font-size:11px;white-space:pre-wrap">${esc(JSON.stringify(d,null,2))}</pre>`; }
function renderHealth(d){
  const rows = Object.entries(d.checks||d).map(([k,v])=>{
    const status = (v && v.status) ? v.status : v;
    return `<tr><td class="mono">${esc(k)}</td><td>${pill(status)}</td><td class="mono">${esc(short((v&&v.detail)||'',100))}</td></tr>`;
  }).join('');
  return `<table><thead><tr><th>subsystem</th><th>status</th><th>detail</th></tr></thead><tbody>${rows}</tbody></table>
          <div style="margin-top:10px"><button data-stop="1">EMERGENCY STOP</button></div>`;
}

async function emergencyStop(){
  log('safety', 'EMERGENCY STOP requested', '', 'sys');
  if (!send({type:'stop', reason:'console'})) await api('/api/v1/stop', {method:'POST', body: JSON.stringify({reason:'console'})});
}

// ── voice (browser STT, optional) ───────────────────────────────────────────
function toggleMic(){
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR){ log('voice', 'browser speech recognition unavailable here — type instead, or use the desktop HUD mic', '', 'sys'); return; }
  if (state.listening){ state.rec && state.rec.stop(); return; }
  const rec = new SR();
  rec.lang = $('#miclang').value; rec.continuous = false; rec.interimResults = false;
  rec.onresult = e => { const t = e.results[0][0].transcript; $('#input').value = t; sendChat(); };
  rec.onerror = e => log('voice', 'STT error: ' + e.error, '', 'sys');
  rec.onend = () => { state.listening = false; $('#mic').textContent = '🎙 MIC'; };
  state.rec = rec; state.listening = true; $('#mic').textContent = '■ STOP';
  rec.start();
}

// ── boot ────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  PHASES.forEach(p => {
    const chip = document.createElement('span'); chip.className = 'chip'; chip.textContent = p;
    chip.onclick = () => { chip.classList.toggle('active');
      chip.classList.contains('active') ? state.filter.add(p) : state.filter.delete(p); };
    $('#chips').appendChild(chip);
  });
  $$('.tab').forEach(t => t.onclick = () => { $$('.tab').forEach(x=>x.classList.remove('active'));
    t.classList.add('active'); state.tab = t.dataset.tab; refreshTab(); });
  $('#send').onclick = sendChat;
  $('#input').addEventListener('keydown', e => { if (e.key === 'Enter') sendChat(); });
  $('#stop').onclick = emergencyStop;
  $('#mic').onclick = toggleMic;
  $('#clear').onclick = () => { $('#events').innerHTML=''; };
  $('#remember').onclick = async () => {
    const text = $('#input').value.trim(); if (!text) return;
    $('#input').value='';
    await api('/api/v1/memory', {method:'POST', body: JSON.stringify({text, kind:'fact'})});
    log('memory', 'stored: ' + text, '', 'sys'); refreshTab();
  };
  log('console', 'STAR 2.0 ops console ready. The visible Star stays the desktop HUD — this page only observes and approves.',
      `build ${INFO.version||'?'} · dry_run ${INFO.settings?.security?.dry_run}`, 'sys');
  connect(); refreshTab(); setInterval(refreshTab, 6000);
});
"""

_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>__CSS__</style>
</head>
<body>
<header>
  <h1>★ STAR 2.0</h1>
  <span class="sub">GATEWAY OPS CONSOLE · diagnostics &amp; approvals · the visible Star remains the desktop HUD</span>
  <span class="spacer"></span>
  <span class="badge" id="conn"><span class="dot"></span><span id="connTxt">CONNECTING</span></span>
  <span class="badge warn" id="dryrun">DRY-RUN __DRY__</span>
  <span class="badge" id="safety">SAFETY __SAFETY__</span>
  <button class="danger" id="stop">■ EMERGENCY STOP</button>
</header>

<main>
  <div class="col">
    <section class="panel" style="flex:1">
      <h2>Conversation <span class="spacer"></span>
        <select id="lang" title="response language">
          <option value="auto">auto</option><option value="bn">বাংলা</option><option value="en">English</option>
        </select>
        <button id="remember" class="gold" title="store the typed text as a memory">＋MEM</button>
      </h2>
      <div class="scroll" id="chat"></div>
      <div class="composer">
        <select id="miclang" title="mic language"><option value="bn-IN">bn-IN</option><option value="en-IN">en-IN</option></select>
        <button id="mic" title="browser speech-to-text (optional)">🎙 MIC</button>
        <input id="input" type="text" placeholder="বলো বা টাইপ করো — 'volume 40 koro', 'search lo-fi on youtube', 'what is on my screen'…" autocomplete="off">
        <button id="send">SEND</button>
      </div>
    </section>
    <section class="panel" style="max-height:26%">
      <h2>Pending confirmations</h2>
      <div class="scroll" id="confirms"><div class="empty">none</div></div>
    </section>
  </div>

  <div class="col">
    <section class="panel" style="flex:1">
      <h2>Mission events <span class="spacer"></span>
        <label class="badge" style="cursor:pointer"><input type="checkbox" id="autoscroll" checked> autoscroll</label>
        <button id="clear">clear</button>
      </h2>
      <div class="chips" id="chips"></div>
      <div class="scroll" id="events"></div>
    </section>
    <section class="panel" style="flex:1">
      <div class="tabs">
        <span class="tab active" data-tab="tasks">Tasks</span>
        <span class="tab" data-tab="tools">Tools</span>
        <span class="tab" data-tab="agents">Agents</span>
        <span class="tab" data-tab="browser">Browser</span>
        <span class="tab" data-tab="computer">Computer</span>
        <span class="tab" data-tab="memory">Memory</span>
        <span class="tab" data-tab="learning">Learning</span>
        <span class="tab" data-tab="audit">Audit</span>
        <span class="tab" data-tab="workspace">Workspace</span>
        <span class="tab" data-tab="confirm">Approvals</span>
        <span class="tab" data-tab="health">Health</span>
        <span class="tab" data-tab="info">Info</span>
      </div>
      <div class="scroll" id="tabbody"></div>
    </section>
  </div>
</main>

<footer>
  <span id="build">__BUILD__</span>
  <span>·</span><span>REST <kbd>/api/v1/*</kbd></span>
  <span>·</span><span>WS <kbd>/ws</kbd></span>
  <span>·</span><span>press <kbd>Enter</kbd> to send</span>
  <span class="spacer"></span>
  <span id="clock"></span>
</footer>

<script>window.__STAR_INFO__ = __BOOTSTRAP__;</script>
<script>__JS__
setInterval(()=>{ const c=document.getElementById('clock'); if(c) c.textContent = new Date().toISOString().slice(11,19) + ' UTC'; }, 1000);
</script>
</body>
</html>
"""


def render_console(info: dict[str, Any] | None = None) -> str:
    """Render the console page with a secret-free bootstrap payload."""
    info = info or {}
    settings = info.get("settings", {}) if isinstance(info, dict) else {}
    security = settings.get("security", {}) if isinstance(settings, dict) else {}
    bootstrap = {
        "version": info.get("version", "?"),
        "protocol": info.get("protocol", "star.mission.v1"),
        "settings": {"security": {"dry_run": security.get("dry_run"), "safety_level": security.get("safety_level")}},
        "capabilities": info.get("capabilities", []),
    }
    return (
        _HTML.replace("__TITLE__", CONSOLE_TITLE)
        .replace("__CSS__", _CSS)
        .replace("__JS__", _JS)
        .replace("__DRY__", str(security.get("dry_run", True)).upper())
        .replace("__SAFETY__", str(security.get("safety_level", "normal")).upper())
        .replace("__BUILD__", f"build {bootstrap['version']} · protocol {bootstrap['protocol']}")
        .replace("__BOOTSTRAP__", json.dumps(bootstrap, ensure_ascii=False))
    )
