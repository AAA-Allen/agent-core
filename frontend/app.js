const STORAGE_KEY = 'agent-workbench-history-v1';
const AGENT_NAME = 'workbench';
const TERMINAL_STATUSES = ['COMPLETED', 'FAILED', 'BLOCKED', 'CANCELED'];

const state = { tasks: [], activeId: null, running: false, view: 'tasks', live: false };
const $ = (id) => document.getElementById(id);
const nowTime = () => new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(new Date());
const clockTime = (iso) => {
  if (!iso) return '--:--';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '--:--';
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(date);
};
const sleep = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

function icon(name) { return `<i data-lucide="${name}"></i>`; }
function refreshIcons() { if (window.lucide) window.lucide.createIcons(); }
function escapeHtml(value) { return String(value).replace(/[&<>'"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

// ---------------------------------------------------------------- storage
function loadHistory() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const tasks = JSON.parse(raw);
    if (Array.isArray(tasks)) state.tasks = tasks.filter((task) => task && task.id && task.title);
  } catch { state.tasks = []; }
}
function persistHistory() {
  try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state.tasks.slice(0, 30))); } catch { /* storage unavailable */ }
}

// ---------------------------------------------------------------- api
async function apiFetch(path, options = {}, timeoutMs = 4000) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, { ...options, signal: controller.signal });
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      let code = `HTTP_${response.status}`;
      try {
        const body = await response.json();
        if (body && body.error) { message = body.error.message || message; code = body.error.code || code; }
      } catch { /* non-json error body */ }
      const error = new Error(message);
      error.code = code;
      throw error;
    }
    return await response.json();
  } catch (error) {
    if (error.name === 'AbortError') { error.code = 'TIMEOUT'; error.message = '请求超时'; }
    throw error;
  } finally { window.clearTimeout(timer); }
}

async function populateAgentSelect() {
  const select = $('agentSelect');
  if (!select) return;
  try {
    const payload = await apiFetch('/api/agents', {}, 3000);
    select.innerHTML = payload.agents.map((agent) => `<option value="${escapeHtml(agent.name)}">${escapeHtml(agent.name)}</option>`).join('');
    if (!payload.agents.some((agent) => agent.name === AGENT_NAME)) select.value = payload.agents[0].name;
  } catch { select.innerHTML = `<option value="${AGENT_NAME}">${AGENT_NAME}</option>`; }
}

async function detectMode() {
  try {
    await apiFetch('/api/health', {}, 1500);
    state.live = true;
  } catch { state.live = false; }
  updateModeUi();
  if (state.live) populateAgentSelect();
}

function updateModeUi() {
  document.body.classList.toggle('live', state.live);
  $('brandStatusText').textContent = state.live ? '在线运行' : '离线演示';
  $('demoBadgeText').textContent = state.live ? 'Runtime 已连接' : '本地演示模式';
  $('inputMode').textContent = state.live ? '实时执行' : '演示模式';
  const note = document.querySelector('.settings-note code');
  if (note) note.textContent = 'python -m agent_core.server';
}

function markOffline() {
  if (!state.live) return;
  state.live = false;
  updateModeUi();
}

// ---------------------------------------------------------------- rendering
function statusLabel(task) {
  if (task.status === 'running') return '执行中';
  if (task.status === 'failed') return '执行失败';
  return '已完成';
}
function pillForTask(task) {
  if (task.status === 'running') return ['执行中', 'running'];
  if (task.status === 'failed') return ['失败', 'failed'];
  return ['已完成', 'completed'];
}

function renderHistory() {
  const list = $('historyList');
  $('taskCount').textContent = `${state.tasks.length} 个`;
  $('emptyHistory').classList.toggle('hidden', state.tasks.length > 0);
  list.innerHTML = state.tasks.map((task) => `<button class="history-item ${task.id === state.activeId ? 'active' : ''}" data-task-id="${escapeHtml(task.id)}"><span class="history-icon">${icon(task.status === 'failed' ? 'triangle-alert' : 'wand-sparkles')}</span><span class="history-content"><span class="history-name">${escapeHtml(task.title)}</span><span class="history-meta"><span class="mini-status ${task.status}"></span>${statusLabel(task)} · ${escapeHtml(task.time)}</span></span></button>`).join('');
  list.querySelectorAll('[data-task-id]').forEach((item) => item.addEventListener('click', () => selectTask(item.dataset.taskId)));
  refreshIcons();
}

function timelineHtml(steps) {
  return `<div class="timeline-list">${steps.map((step, index) => `<div class="timeline-step ${step.state}"><span class="step-marker">${icon(step.marker)}</span>${index < steps.length - 1 ? '<span class="step-rail"></span>' : ''}<div><div class="step-name">${escapeHtml(step.title)}</div><div class="step-desc">${escapeHtml(step.desc)}</div></div><span class="step-time">${escapeHtml(step.time || '--:--')}</span></div>`).join('')}</div>`;
}

function demoTimeline(task, phase = task.status) {
  const steps = [
    { title: '任务已接收', desc: '已理解你的目标，准备拆解执行。', marker: 'inbox', state: 'done', time: task.receivedAt },
    { title: '任务处理中', desc: 'Agent 正在整理信息并执行步骤。', marker: phase === 'running' ? 'loader-circle' : 'loader-circle', state: phase === 'running' ? 'current' : 'done', time: phase === 'running' ? '现在' : (task.completedAt || '--:--') },
    { title: phase === 'failed' ? '任务未完成' : '任务执行完成', desc: phase === 'failed' ? '执行过程中遇到问题，请查看错误提示。' : '所有步骤已完成，结果已经准备好。', marker: phase === 'failed' ? 'triangle-alert' : 'check', state: phase === 'failed' ? 'failed' : 'done', time: task.completedAt || '--:--' },
  ];
  if (phase === 'running') steps[2].state = '';
  $('timeline').innerHTML = timelineHtml(steps);
  refreshIcons();
}

const NODE_OUTCOMES = { 'node.completed': ['成功', 'done', 'check'], 'node.blocked': ['已阻断', 'failed', 'shield-alert'], 'node.failed': ['失败', 'failed', 'triangle-alert'] };

function runTimeline(run) {
  const steps = [{ title: '任务已接收', desc: `运行 ${run.run_id}`, marker: 'inbox', state: 'done', time: clockTime(run.trace[0] && run.trace[0].started_at) }];
  const terminalByExecution = new Map();
  run.trace.forEach((event) => {
    if (NODE_OUTCOMES[event.event_type]) terminalByExecution.set(event.execution_id, event);
  });
  const seen = new Set();
  run.trace.forEach((event) => {
    if (event.event_type !== 'node.started' || seen.has(event.node_id)) return;
    seen.add(event.node_id);
    const outcome = terminalByExecution.get(event.execution_id);
    const [label, nodeState, marker] = outcome ? NODE_OUTCOMES[outcome.event_type] : ['执行中', 'current', 'loader-circle'];
    steps.push({ title: event.node_id, desc: label, marker, state: nodeState, time: clockTime(event.started_at) });
  });
  const final = run.trace[run.trace.length - 1];
  const failed = run.status !== 'COMPLETED';
  steps.push({
    title: failed ? '任务未完成' : '任务执行完成',
    desc: failed ? `原因码 ${run.reason_code || run.status}` : '所有步骤已完成，结果已就绪。',
    marker: failed ? 'triangle-alert' : 'check',
    state: failed ? 'failed' : 'done',
    time: clockTime(final && final.started_at),
  });
  return steps;
}

const LIVE_PHASES = { 'run.created': ['任务已接收', 'inbox'], 'run.running': ['Runtime 执行中', 'loader-circle'], 'run.resumed': ['恢复执行', 'rotate-cw'], 'run.recovered': ['边界恢复', 'rotate-cw'] };

function liveTimeline(task) {
  const events = task.lifecycle || [];
  const steps = [];
  events.forEach((event) => {
    const phase = LIVE_PHASES[event.event_type];
    if (phase) steps.push({ title: phase[0], desc: `事件 ${event.event_type}`, marker: event.event_type === 'run.running' ? 'loader-circle' : phase[1], state: event.event_type === 'run.running' ? 'current' : 'done', time: clockTime(event.occurred_at) });
  });
  if (!steps.length) steps.push({ title: '已提交', desc: '等待 Runtime 接收…', marker: 'loader-circle', state: 'current', time: task.receivedAt });
  return steps;
}

function renderTaskView(task) {
  const [pillText, pillClass] = pillForTask(task);
  $('statePill').className = `state-pill ${pillClass}`;
  $('statePill').textContent = pillText;
  updateCancelButton(task);
  updateResumeButton(task);
  if (task.status === 'running' && task.mode === 'live') {
    $('timeline').innerHTML = timelineHtml(liveTimeline(task));
  } else if (task.mode === 'live' && task.run) {
    $('timeline').innerHTML = timelineHtml(runTimeline(task.run));
  } else {
    demoTimeline(task);
  }
  updateResult(task);
  refreshIcons();
}

function selectTask(id) {
  const task = state.tasks.find((item) => item.id === id); if (!task) return;
  state.activeId = id; $('runId').textContent = task.id; $('executionTitle').textContent = task.title;
  renderTaskView(task); renderHistory();
}

function updateResult(task) {
  const panel = $('resultPanel');
  if (task.status === 'running') { panel.classList.add('hidden'); updateCancelButton(task); return; }
  panel.classList.remove('hidden'); panel.classList.toggle('failed', task.status === 'failed');
  $('resultTitle').textContent = task.status === 'failed' ? '任务执行失败' : '任务已完成';
  $('resultSummary').textContent = task.summary;
  panel.querySelector('.result-icon').innerHTML = icon(task.status === 'failed' ? 'triangle-alert' : 'check');
  updateResumeButton(task);
  refreshIcons();
}

function updateCancelButton(task) {
  let button = $('cancelButton');
  const show = task && task.mode === 'live' && task.status === 'running' && state.live;
  if (!show) { if (button) button.remove(); return; }
  if (!button) {
    button = document.createElement('button');
    button.id = 'cancelButton';
    button.className = 'cancel-button';
    button.innerHTML = `${icon('circle-slash')}<span>取消运行</span>`;
    button.addEventListener('click', () => requestCancel());
    document.querySelector('.card-topline').appendChild(button);
  }
  refreshIcons();
}

function updateResumeButton(task) {
  let button = $('resumeButton');
  const show = task && task.mode === 'live' && task.status !== 'running' && !!task.run && task.run.resumable && state.live;
  if (!show) { if (button) button.remove(); return; }
  if (!button) {
    button = document.createElement('button');
    button.id = 'resumeButton';
    button.className = 'resume-button';
    button.innerHTML = `${icon('rotate-cw')}<span>恢复运行</span>`;
    button.addEventListener('click', () => resumeTask(task));
    document.querySelector('.card-topline').appendChild(button);
  }
  refreshIcons();
}

async function resumeTask(task) {
  if (!task.run || !task.run.resumable || state.running) return;
  let patch = {};
  const raw = window.prompt('恢复运行:输入要合并进状态的对象(JSON),留空直接恢复。例如 {"priority": "high"}', '{}');
  if (raw === null) return;
  if (raw.trim()) {
    try { patch = JSON.parse(raw); } catch { window.alert('JSON 解析失败,请输入合法的 JSON 对象。'); return; }
    if (!patch || typeof patch !== 'object' || Array.isArray(patch)) { window.alert('需要 JSON 对象,例如 {"priority": "high"}'); return; }
  }
  state.running = true;
  task.status = 'running';
  task.lifecycle = [];
  selectTask(task.id);
  try {
    const payload = await apiFetch(`/api/runs/${task.id}/resume`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ state_updates: patch }),
    }, 30000);
    completeLiveTask(task, payload.run);
  } catch (error) {
    state.running = false;
    if (error.code === 'UNKNOWN_RUN') { failTask(task, '服务端没有这条运行的记录。'); return; }
    task.status = 'failed';
    task.completedAt = nowTime();
    task.summary = `恢复失败:${error.message}`;
    persistHistory();
    selectTask(task.id);
  }
}

async function requestCancel() {
  const task = state.tasks.find((item) => item.id === state.activeId);
  if (!task || task.status !== 'running' || task.mode !== 'live') return;
  try {
    await apiFetch(`/api/runs/${task.id}/cancel`, { method: 'POST' }, 4000);
    const pill = $('statePill');
    pill.textContent = '取消请求已发送';
  } catch { /* run may have finished already */ }
}

// ---------------------------------------------------------------- live run
function failTask(task, message) {
  task.status = 'failed'; task.completedAt = nowTime(); task.summary = message; state.running = false;
  persistHistory();
  if (state.activeId === task.id) { selectTask(task.id); } else { renderHistory(); }
}

function completeLiveTask(task, run) {
  task.status = run.status === 'COMPLETED' ? 'completed' : 'failed';
  task.completedAt = nowTime();
  task.run = run;
  task.summary = run.status === 'COMPLETED'
    ? (run.state && run.state.summary) || '运行已完成。'
    : `${run.status} · 原因码 ${run.reason_code || '未提供'}`;
  state.running = false;
  persistHistory();
  if (state.activeId === task.id) { selectTask(task.id); } else { renderHistory(); }
}

function watchLiveTask(task) {
  return new Promise((resolve) => {
    let settled = false;
    let watchdog = null;
    const finish = (status) => {
      if (settled) return;
      settled = true;
      window.clearInterval(watchdog);
      try { source.close(); } catch { /* already closed */ }
      resolve(status);
    };
    const source = new EventSource(`/api/runs/${task.id}/events/stream`);
    source.addEventListener('lifecycle', (event) => {
      try {
        const item = JSON.parse(event.data);
        if (!task.lifecycle.some((entry) => entry.event_id === item.event_id)) {
          task.lifecycle.push(item);
          if (state.activeId === task.id) renderTaskView(task);
        }
      } catch { /* malformed frame */ }
    });
    source.addEventListener('done', (event) => {
      let status = 'UNKNOWN';
      try { status = JSON.parse(event.data).status; } catch { /* malformed frame */ }
      finish(status);
    });
    source.onerror = () => { /* EventSource 自带重连;断连由看门狗兜底 */ };
    watchdog = window.setInterval(async () => {
      try {
        const status = await apiFetch(`/api/runs/${task.id}/status`, {}, 3000);
        if (TERMINAL_STATUSES.includes(status.status)) finish(status.status);
      } catch (error) {
        if (error.code === 'TIMEOUT' || error.name === 'TypeError') { markOffline(); finish('CONNECTION_LOST'); }
      }
    }, 2000);
  });
}

async function pollLiveTask(task) {
  const status = await watchLiveTask(task);
  if (task.status !== 'running') return;
  if (status === 'CONNECTION_LOST') { failTask(task, '与 Runtime 服务的连接中断。'); return; }
  try {
    const payload = await apiFetch(`/api/runs/${task.id}`, {}, 8000);
    completeLiveTask(task, payload.run);
  } catch (error) {
    if (error.code === 'UNKNOWN_RUN') failTask(task, '服务端没有这条运行的记录，可能已被重启清除。');
    else failTask(task, `获取运行结果失败：${error.message}`);
  }
}

async function runLiveTask(task) {
  try {
    const start = await apiFetch('/api/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent: task.agent || AGENT_NAME, input: { task: task.title }, wait: false }),
    }, 8000);
    if (state.activeId === task.id) state.activeId = start.run_id;
    task.id = start.run_id;
    task.lifecycle = [];
    persistHistory();
    if (state.activeId === task.id) { $('runId').textContent = task.id; renderTaskView(task); }
    pollLiveTask(task);
  } catch (error) {
    if (error.code === 'TIMEOUT' || error.name === 'TypeError') markOffline();
    failTask(task, `无法提交任务：${error.message}`);
  }
}

// ---------------------------------------------------------------- demo run
function runDemoTask(task) {
  window.setTimeout(() => { if (state.activeId === task.id) selectTask(task.id); }, 80);
  window.setTimeout(() => {
    const failed = /失败|错误|报错|fail/i.test(task.title);
    task.status = failed ? 'failed' : 'completed';
    task.completedAt = nowTime();
    task.summary = failed ? '演示流程模拟了一个可恢复的错误，请重试或调整任务描述。' : `Agent 已完成对“${task.title}”的演示处理，输出已整理为可查看的结果。`;
    state.running = false;
    persistHistory();
    selectTask(task.id);
  }, 1600);
}

// ---------------------------------------------------------------- actions
function submitTask(raw) {
  const text = raw.trim(); if (!text || state.running) return;
  const agentName = ($('agentSelect') && $('agentSelect').value) || AGENT_NAME;
  const task = { id: `pending-${Date.now().toString(36)}`, title: text, status: 'running', time: nowTime(), receivedAt: nowTime(), summary: '', mode: state.live ? 'live' : 'demo', agent: agentName, run: null };
  state.tasks.unshift(task); state.activeId = task.id; state.running = true;
  $('topInput').value = ''; $('bottomInput').value = '';
  selectTask(task.id); persistHistory();
  if (task.mode === 'live') runLiveTask(task); else runDemoTask(task);
}

const serverRunCache = new Map();

function runInspectorBody(run) {
  const nodes = run.trace.filter((event) => event.event_type === 'node.started').map((event) => event.node_id).join(' → ') || '（无节点执行）';
  const traceRows = run.trace.map((event) => `<tr><td>${escapeHtml(event.sequence)}</td><td>${escapeHtml(event.event_type)}</td><td>${escapeHtml(event.node_id || '—')}</td><td>${escapeHtml(event.status)}</td><td>${escapeHtml(event.duration_ms != null ? `${event.duration_ms} ms` : '—')}</td></tr>`).join('');
  const lifecycleRows = (run.lifecycle_events || []).map((event) => `<tr><td>${escapeHtml(event.sequence)}</td><td>${escapeHtml(event.event_type)}</td><td>${escapeHtml(event.from_status || '—')} → ${escapeHtml(event.to_status)}</td><td>${escapeHtml(event.reason_code || '—')}</td></tr>`).join('');
  return `<div class="detail-grid">
    <div class="detail-item"><span>运行 ID</span><strong>${escapeHtml(run.run_id)}</strong></div>
    <div class="detail-item"><span>状态</span><strong>${escapeHtml(run.status)}</strong></div>
    <div class="detail-item"><span>原因码</span><strong>${escapeHtml(run.reason_code || '—')}</strong></div>
    <div class="detail-item"><span>停止于</span><strong>${escapeHtml(run.stopped_at || '—')}</strong></div>
    <div class="detail-item"><span>模拟标记</span><strong>${run.simulated ? 'simulated' : '真实执行'}</strong></div>
    <div class="detail-item"><span>Trace 事件</span><strong>${run.trace.length} 条</strong></div>
  </div>
  <div class="detail-output"><strong>执行路径</strong><br />${escapeHtml(nodes)}</div>
  ${traceRows ? `<table class="detail-table"><thead><tr><th>#</th><th>事件</th><th>节点</th><th>状态</th><th>耗时</th></tr></thead><tbody>${traceRows}</tbody></table>` : ''}
  ${lifecycleRows ? `<table class="detail-table"><thead><tr><th>#</th><th>生命周期</th><th>状态迁移</th><th>原因码</th></tr></thead><tbody>${lifecycleRows}</tbody></table>` : ''}
  <pre class="detail-json">${escapeHtml(JSON.stringify(run.state, null, 2))}</pre>`;
}

function openDetail() {
  const task = state.tasks.find((item) => item.id === state.activeId); if (!task) return;
  let body;
  if (task.mode === 'live' && task.run) {
    body = runInspectorBody(task.run);
  } else {
    body = `<div class="detail-grid">
      <div class="detail-item"><span>运行 ID</span><strong>${escapeHtml(task.id)}</strong></div>
      <div class="detail-item"><span>状态</span><strong>${task.status === 'failed' ? 'FAILED' : 'COMPLETED'}</strong></div>
      <div class="detail-item"><span>接收时间</span><strong>${escapeHtml(task.receivedAt)}</strong></div>
      <div class="detail-item"><span>执行模式</span><strong>${task.mode === 'live' ? 'RUNTIME_API' : 'LOCAL_DEMO'}</strong></div>
    </div>
    <div class="detail-output"><strong>结果摘要</strong><br />${escapeHtml(task.summary || '—')}</div>`;
  }
  $('modalTitle').textContent = task.title;
  $('modalBody').innerHTML = body;
  $('detailModal').showModal(); refreshIcons();
}

function openServerRunDetail(runId) {
  const run = serverRunCache.get(runId); if (!run) return;
  $('modalTitle').textContent = `服务端运行 ${runId}`;
  $('modalBody').innerHTML = runInspectorBody(run);
  $('detailModal').showModal(); refreshIcons();
}

async function renderLogsView() {
  $('executionTitle').textContent = '运行日志';
  const rows = [];
  if (state.live) {
    try {
      const payload = await apiFetch('/api/runs', {}, 3000);
      const recent = payload.runs.slice(-12).reverse();
      const details = await Promise.all(recent.map(async (runId) => {
        try {
          const { run } = await apiFetch(`/api/runs/${runId}`, {}, 3000);
          serverRunCache.set(runId, run);
          return run;
        } catch { return null; }
      }));
      const serverRows = details.filter(Boolean).map((run) => `<button class="history-item server-run" data-run-id="${escapeHtml(run.run_id)}"><span class="history-icon">${icon(run.status === 'COMPLETED' ? 'check' : 'triangle-alert')}</span><span class="history-content"><span class="history-name">${escapeHtml((run.state && run.state.summary) || run.run_id)}</span><span class="history-meta"><span class="mini-status ${run.status === 'COMPLETED' ? 'completed' : 'failed'}"></span>${escapeHtml(run.status)} · ${escapeHtml(clockTime(run.stopped_at))}</span></span></button>`);
      rows.push(`<div class="history-heading server-heading"><h2>服务端运行记录</h2><span>${payload.runs.length} 条 · 活跃 ${payload.active.length}</span></div>`);
      if (serverRows.length) rows.push(serverRows.join(''));
      else rows.push('<div class="timeline-empty">服务端暂无持久化运行</div>');
    } catch { rows.push('<div class="timeline-empty">无法读取服务端运行记录</div>'); }
  }
  rows.push('<div class="history-heading server-heading"><h2>本页任务</h2></div>');
  rows.push(state.tasks.map((task) => `<div class="timeline-step ${task.status === 'failed' ? 'failed' : 'done'}"><span class="step-marker">${icon(task.status === 'failed' ? 'triangle-alert' : 'check')}</span><span class="step-rail"></span><div><div class="step-name">${escapeHtml(task.title)}</div><div class="step-desc">${escapeHtml(`${task.mode === 'live' ? 'Runtime API' : '本地演示'} · ${statusLabel(task)}`)}</div></div><span class="step-time">${escapeHtml(task.time)}</span></div>`).join('') || '<div class="timeline-empty">暂无本页任务</div>');
  $('timeline').innerHTML = `<div class="timeline-list">${rows.join('')}</div>`;
  $('timeline').querySelectorAll('[data-run-id]').forEach((item) => item.addEventListener('click', () => openServerRunDetail(item.dataset.runId)));
  $('resultPanel').classList.add('hidden');
  updateCancelButton(null);
  updateResumeButton(null);
  refreshIcons();
}

async function renderAgentTopology() {
  const container = $('agentTopology');
  if (!container) return;
  if (!state.live) { container.innerHTML = '<div class="settings-note"><span>离线模式下无法读取 Agent 拓扑。</span></div>'; return; }
  try {
    const payload = await apiFetch('/api/agents', {}, 3000);
    const count = $('topologyCount');
    if (count) count.textContent = `${payload.agents.length} 个已注册`;
    container.innerHTML = payload.agents.map((agent) => {
      const graph = agent.graph || { nodes: [], edges: [], entry: '?' };
      const flow = `${escapeHtml(graph.entry)} → ${escapeHtml(graph.nodes.filter((n) => n !== graph.entry).join(' → ') || '—')}`;
      const edges = graph.edges.map((edge) => `${escapeHtml(edge.source)}→${escapeHtml(edge.target)}${edge.when ? ` [${escapeHtml(edge.when)}]` : ''}`).join('，');
      return `<div class="topology-card"><strong>${escapeHtml(agent.name)}</strong><span>${escapeHtml(agent.description || agent.instructions || '')}</span><code>${flow}</code><span class="topology-edges">${edges || '无边'}</span></div>`;
    }).join('');
  } catch {
    container.innerHTML = '<div class="settings-note"><span>读取 Agent 拓扑失败。</span></div>';
  }
}

function resetWorkspace() {
  state.tasks.length = 0; state.activeId = null; state.running = false;
  persistHistory(); renderHistory();
  $('runId').textContent = '等待新任务'; $('executionTitle').textContent = '准备好开始';
  $('statePill').className = 'state-pill neutral'; $('statePill').textContent = '待机';
  $('timeline').innerHTML = `<div class="timeline-empty">${icon('route')}<span>提交任务后，这里会显示执行过程</span></div>`;
  $('resultPanel').classList.add('hidden');
  updateCancelButton(null);
  updateResumeButton(null);
  refreshIcons();
}

function switchView(view) {
  state.view = view;
  document.querySelectorAll('.nav-item').forEach((item) => item.classList.toggle('active', item.dataset.view === view));
  if (view === 'logs') { renderLogsView(); }
  else if (view === 'settings') { $('settingsModal').showModal(); renderAgentTopology(); switchView('tasks'); }
  else if (state.activeId) selectTask(state.activeId);
}

document.addEventListener('DOMContentLoaded', () => {
  refreshIcons();
  loadHistory();
  renderHistory();
  detectMode();
  state.tasks.filter((task) => task.status === 'running' && task.mode === 'live').forEach((task) => pollLiveTask(task));

  $('topComposer').addEventListener('submit', (event) => { event.preventDefault(); submitTask($('topInput').value); });
  $('bottomComposer').addEventListener('submit', (event) => { event.preventDefault(); submitTask($('bottomInput').value); });
  $('topInput').addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); submitTask(event.target.value); } });
  $('bottomInput').addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); submitTask(event.target.value); } });
  $('detailButton').addEventListener('click', openDetail);
  $('settingsButton').addEventListener('click', () => $('settingsModal').showModal());
  $('clearHistory').addEventListener('click', resetWorkspace);
  document.querySelectorAll('.nav-item').forEach((item) => item.addEventListener('click', () => switchView(item.dataset.view)));
  document.querySelectorAll('[data-close]').forEach((button) => button.addEventListener('click', () => $(button.dataset.close).close()));
  document.addEventListener('keydown', (event) => { if ((event.metaKey || event.ctrlKey) && ['1','2','3'].includes(event.key)) { event.preventDefault(); switchView(['tasks','logs','settings'][Number(event.key) - 1]); } });
});
