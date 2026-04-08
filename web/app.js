const API = '/api';
/**
 * 本项目所有面向用户的时间展示默认东八区（UTC+8）。
 * 与后端 server/time_display 一致；勿用浏览器本机时区代替。
 */
const DISPLAY_TZ = 'Asia/Shanghai';

/**
 * 将某一绝对时刻格式化为东八区 ISO 字符串（带 +08:00），供提交 get_current_state 等 API。
 * 勿用 toISOString() 默认值：那是 UTC 且带 Z，易与「东八区墙钟」混淆；若再去掉 Z 传给后端，
 * 后端会把无时区串当成东八区解析，造成整 8 小时偏差。
 */
function toIsoStringShanghai(date = new Date()) {
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: DISPLAY_TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
  const parts = {};
  for (const { type, value } of fmt.formatToParts(date)) {
    if (type !== 'literal') parts[type] = value;
  }
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}+08:00`;
}

/** 东八区日历日期 YYYY-MM-DD（用于 date 输入默认值等） */
function calendarDateStringShanghai(date = new Date()) {
  return toIsoStringShanghai(date).slice(0, 10);
}

// ── Utility ──

async function apiFetch(path, options = {}) {
  const resp = await fetch(`${API}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!resp.ok) {
    const err = await resp.text();
    throw new Error(`API error ${resp.status}: ${err}`);
  }
  return resp.json();
}

function $(sel) { return document.querySelector(sel); }
function $$(sel) { return document.querySelectorAll(sel); }

function truncate(text, max = 120) {
  return text.length > max ? text.slice(0, max) + '…' : text;
}

function escHtml(str) {
  const d = document.createElement('div');
  d.textContent = str;
  return d.innerHTML;
}

/** 环境 JSON 中 activity=正文、summary=小结；旧数据可能两段相同或仅有一段 */
function formatEnvironmentBlocks(env, contentClass = 'detail-content') {
  if (!env || typeof env !== 'object') return '';
  const act = String(env.activity || '').trim();
  const sum = String(env.summary || '').trim();
  if (!act && !sum) return '';
  if (act && sum && act === sum) {
    return `<div class="${contentClass}">${escHtml(act)}</div>`;
  }
  let h = '';
  if (act) {
    h += `<div class="list-meta">环境正文</div><div class="${contentClass}">${escHtml(act)}</div>`;
  }
  if (sum) {
    h += `<div class="list-meta" style="margin-top:8px">内容小结</div><div class="${contentClass}">${escHtml(sum)}</div>`;
  }
  return h;
}

/** 将任意 ISO（含 Z / +08:00）格式化为东八区墙钟显示 */
function formatTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString('zh-CN', { timeZone: DISPLAY_TZ });
}

function getSnapshotNarrativeTimeValue(snap) {
  if (!snap) return '';
  return String(snap.created_at_cst || snap.created_at || '');
}

function getSnapshotInsertedTimeValue(snap) {
  if (!snap) return '';
  return String(snap.inserted_at_cst || snap.inserted_at || '');
}

/**
 * 快照双时间轴，均为东八区展示。
 * 优先用后端 model_dump 的 *_cst（显式 +08:00），否则用 UTC 存库字段经 formatTime 转东八区。
 */
function snapshotTimeLineText(snap) {
  const narSrc = getSnapshotNarrativeTimeValue(snap);
  const ins = snap.inserted_at
    ? formatTime(getSnapshotInsertedTimeValue(snap))
    : '—（旧数据无入库时间）';
  const nar = formatTime(narSrc);
  return `叙事（东八区）：${nar} | 入库（东八区）：${ins}`;
}

/** 将毫秒差格式化为中文时长（用于「多久之前」） */
function formatDurationZh(ms) {
  const n = Math.max(0, Math.floor(Number(ms) || 0));
  const sec = Math.floor(n / 1000);
  if (sec < 60) return `${sec} 秒`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} 分钟`;
  const hr = Math.floor(min / 60);
  if (hr < 48) return `${hr} 小时`;
  const day = Math.floor(hr / 24);
  return `${day} 天`;
}

let dashboardLatestSnapshotIso = null;
let idleSnapshotSummaryTimer = null;

function updateIdleSnapshotAgoLabel() {
  const el = document.getElementById('idle-snapshot-ago');
  if (!el || !dashboardLatestSnapshotIso) return;
  const ms = Date.now() - new Date(dashboardLatestSnapshotIso).getTime();
  if (Number.isNaN(ms)) return;
  el.textContent = `${formatDurationZh(ms)}前`;
}

function formatUsd(value) {
  const num = Number(value || 0);
  return `$${num.toFixed(4)}`;
}

function renderCostBreakdown(summary) {
  const models = Array.isArray(summary?.model_breakdown) ? summary.model_breakdown : [];
  const unknownTokens = Number(summary?.unknown_priced_tokens || 0);
  if (!models.length) {
    return unknownTokens > 0
      ? `<div class="list-meta">成本拆分：暂无（${unknownTokens} tokens 未匹配单价）</div>`
      : '<div class="list-meta">成本拆分：暂无</div>';
  }
  const top = models.slice(0, 3);
  const items = top.map((item) => {
    const model = escHtml(String(item.model || 'unknown'));
    const cost = formatUsd(item.estimated_cost_usd || 0);
    const total = Number(item.total_tokens || 0);
    const tag = item.has_pricing ? '' : '（未配置单价）';
    return `${model}: ${cost} / ${total} tokens${tag}`;
  });
  const unknownText = unknownTokens > 0 ? `；另有 ${unknownTokens} tokens 未匹配单价` : '';
  return `<div class="list-meta">成本拆分：${items.join('；')}${unknownText}</div>`;
}

function showStatus(msg, isError = false) {
  const bar = $('#status-bar');
  if (bar) {
    bar.textContent = msg;
    bar.style.color = isError ? 'var(--danger)' : 'var(--text-dim)';
  }
}

// ── Dashboard Page ──

let currentAutomationTab = 'latest';

async function initDashboardPage() {
  await loadDashboard();
  await loadAutomationLatest();
  await loadAutomationHistory();
  await loadAutomationTokenSummary();
  await loadModelPricingForDashboard();
  if (idleSnapshotSummaryTimer) clearInterval(idleSnapshotSummaryTimer);
  idleSnapshotSummaryTimer = setInterval(updateIdleSnapshotAgoLabel, 10000);
}

async function loadIdleSnapshotSummary() {
  const wrap = document.getElementById('idle-snapshot-summary');
  if (!wrap) return;
  try {
    const data = await apiFetch('/dashboard/idle-snapshot-summary');
    const latest = data.latest_snapshot;
    const sched = data.snapshot_scheduler || {};
    dashboardLatestSnapshotIso =
      latest
        ? getSnapshotNarrativeTimeValue(latest)
        : null;
    updateIdleSnapshotAgoLabel();

    const conv = data.last_conversation_end;
    const snapN = data.snapshots_since_conversation_end;
    const evtN = data.events_since_conversation_end;
    const schedOn = !!sched.enabled;
    const schedSec = Number(sched.interval_sec || 60);

    let statsHtml = '';
    if (conv && snapN != null && evtN != null) {
      statsHtml = `
        <div class="idle-snapshot-row">
          <span>自上次<span class="idle-snapshot-strong">对话结束</span>快照以来（${escHtml(formatTime(getSnapshotNarrativeTimeValue(conv)))}）：</span>
          <span>新增快照 <span class="idle-snapshot-strong">${Number(snapN)}</span> 条</span>
          <span>新增事件 <span class="idle-snapshot-strong">${Number(evtN)}</span> 条</span>
        </div>
        <div class="idle-snapshot-note">说明：统计的是该时间点<span class="idle-snapshot-strong">之后</span>创建的快照与事件（含后台 scheduler 推进产生的记录）。</div>`;
    } else {
      statsHtml = `
        <div class="idle-snapshot-row">
          <span>尚无 <span class="idle-snapshot-strong">对话结束</span>（<code>conversation_end</code>）类快照，无法界定「无对话区间」。</span>
        </div>
        <div class="idle-snapshot-note">在客户端执行对话反思并写入「对话结束」快照后，此处会显示静默期内的增量统计。</div>`;
    }

    const agoLine = dashboardLatestSnapshotIso
      ? `<div class="idle-snapshot-row" style="margin-top:10px">
          <span>距<span class="idle-snapshot-strong">最新一条快照</span>生成已过 <span id="idle-snapshot-ago" class="idle-snapshot-strong">—</span></span>
          <span class="list-meta">快照时间 ${escHtml(formatTime(dashboardLatestSnapshotIso))}</span>
        </div>`
      : `<div class="idle-snapshot-row" style="margin-top:10px"><span>当前没有快照记录。</span></div>`;

    const schedLine = `
      <div class="idle-snapshot-row" style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border-subtle)">
        <span>后台自动推进：<span class="idle-snapshot-strong">${schedOn ? '已开启' : '已关闭'}</span></span>
        <span class="list-meta">轮询间隔约 ${schedSec} 秒（可在「设定管理 → 运行参数」中修改 <code>snapshot_scheduler_*</code>）</span>
      </div>`;

    wrap.innerHTML = statsHtml + agoLine + schedLine;
    updateIdleSnapshotAgoLabel();
  } catch (e) {
    wrap.innerHTML = `<div style="color:var(--danger)">静默统计加载失败: ${escHtml(e.message)}</div>`;
  }
}

async function loadDashboard() {
  try {
    const { snapshot } = await apiFetch('/snapshots/latest');
    const el = $('#latest-snapshot');
    if (!el) return;
    if (snapshot) {
      el.innerHTML = `<div class="card-content">${escHtml(snapshot.content)}</div>
        <div class="list-meta" style="margin-top:12px">
          类型: ${escHtml(snapshot.type)} | ${escHtml(snapshotTimeLineText(snapshot))}
        </div>`;
      if (snapshot.environment && snapshot.environment !== '{}') {
        try {
          const env = JSON.parse(snapshot.environment);
          const block = formatEnvironmentBlocks(env, 'card-content');
          if (block) $('#env-info').innerHTML = block;
        } catch(e) {}
      }
    } else {
      el.innerHTML = '<div class="empty">暂无状态快照</div>';
    }
    await loadIdleSnapshotSummary();
    showStatus('仪表盘已加载');
  } catch (e) {
    showStatus('加载失败: ' + e.message, true);
  }
}

function switchAutomationTab(tab) {
  currentAutomationTab = tab;
  const latest = document.getElementById('automation-latest-panel');
  const history = document.getElementById('automation-history-panel');
  const tabs = document.querySelectorAll('#automation-tabs .tab');
  tabs.forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  if (latest) latest.style.display = tab === 'latest' ? 'block' : 'none';
  if (history) history.style.display = tab === 'history' ? 'block' : 'none';
}

function renderAutomationReport(report) {
  if (!report || !report.ran) return '<div class="empty">自动化未执行或已关闭。</div>';
  const vectorSync = report.vector_sync || {};
  const evolution = report.evolution || {};
  const compaction = report.compaction || {};
  const llmUsage = report.llm_usage || {};
  const errors = Array.isArray(report.errors) ? report.errors : [];
  return `
    <div class="list-meta">触发源：${escHtml(String(report.trigger || 'unknown'))}</div>
    <div class="list-meta">向量同步：事件 ${Number(vectorSync.vectorized_events || 0)}，快照 ${Number(vectorSync.vectorized_snapshots || 0)}</div>
    <div class="list-meta">人格演化：${evolution.applied ? '已执行' : '未触发'}</div>
    <div class="list-meta">冷压缩：新增摘要 ${Number(compaction.created_summaries || 0)}，删除旧向量 ${Number(compaction.deleted_originals || 0)}</div>
    <div class="list-meta">Token：输入 ${Number(llmUsage.prompt_tokens || 0)}，输出 ${Number(llmUsage.completion_tokens || 0)}，总计 ${Number(llmUsage.total_tokens || 0)}（请求 ${Number(llmUsage.requests || 0)} 次）</div>
    ${errors.length ? `<div class="list-meta" style="color:var(--danger)">异常：${escHtml(errors.join('; '))}</div>` : ''}
  `;
}

async function loadAutomationLatest() {
  const el = document.getElementById('automation-latest');
  if (!el) return;
  el.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const data = await apiFetch('/automation/latest');
    const item = data.item;
    if (!item) {
      el.innerHTML = '<div class="empty">暂无自动化执行记录</div>';
      return;
    }
    el.innerHTML = `
      <div class="list-item" style="cursor:default">
        <div><span class="tag">#${Number(item.id || 0)}</span><span class="list-meta">${formatTime(item.created_at)}</span></div>
        <div style="margin-top:6px">${renderAutomationReport(item.report || {})}</div>
      </div>
    `;
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger)">加载失败: ${escHtml(e.message)}</div>`;
  }
}

async function loadAutomationHistory() {
  const el = document.getElementById('automation-history');
  if (!el) return;
  el.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const data = await apiFetch('/automation/runs?limit=20');
    const items = data.items || [];
    if (!items.length) {
      el.innerHTML = '<div class="empty">暂无历史记录</div>';
      return;
    }
    el.innerHTML = items.map(item => `
      <div class="list-item" style="cursor:default">
        <div>
          <span class="tag">#${Number(item.id || 0)}</span>
          <span class="tag">${escHtml(String(item.trigger || 'unknown'))}</span>
          <span class="list-meta">${formatTime(item.created_at)}</span>
        </div>
        <div style="margin-top:6px">${renderAutomationReport(item.report || {})}</div>
      </div>
    `).join('');
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger)">加载失败: ${escHtml(e.message)}</div>`;
  }
}

async function loadAutomationTokenSummary() {
  const el = document.getElementById('token-summary');
  if (!el) return;
  el.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const data = await apiFetch('/automation/token-summary');
    const today = data.today || {};
    const week = data.week || {};
    const all = data.all || {};
    const pricingUnit = String(data.pricing_unit || 'USD / 1M tokens');
    el.innerHTML = `
      <div class="grid-2">
        <div class="list-item" style="cursor:default">
          <div><span class="tag">今日</span><span class="list-meta">UTC日界</span></div>
          <div class="list-meta">流程数：${Number(today.runs || 0)} | 请求数：${Number(today.requests || 0)}</div>
          <div class="list-meta">输入：${Number(today.prompt_tokens || 0)} | 输出：${Number(today.completion_tokens || 0)}</div>
          <div class="list-meta">总计：${Number(today.total_tokens || 0)}</div>
          <div class="list-meta">估算成本：${formatUsd(today.estimated_cost_usd || 0)}（${escHtml(pricingUnit)}）</div>
          ${renderCostBreakdown(today)}
        </div>
        <div class="list-item" style="cursor:default">
          <div><span class="tag">本周</span><span class="list-meta">周一至今（UTC）</span></div>
          <div class="list-meta">流程数：${Number(week.runs || 0)} | 请求数：${Number(week.requests || 0)}</div>
          <div class="list-meta">输入：${Number(week.prompt_tokens || 0)} | 输出：${Number(week.completion_tokens || 0)}</div>
          <div class="list-meta">总计：${Number(week.total_tokens || 0)}</div>
          <div class="list-meta">估算成本：${formatUsd(week.estimated_cost_usd || 0)}（${escHtml(pricingUnit)}）</div>
          ${renderCostBreakdown(week)}
        </div>
      </div>
      <div class="list-item" style="cursor:default; margin-top:8px;">
        <div><span class="tag">累计</span><span class="list-meta">自动化报告可统计区间</span></div>
        <div class="list-meta">流程数：${Number(all.runs || 0)} | 请求数：${Number(all.requests || 0)}</div>
        <div class="list-meta">输入：${Number(all.prompt_tokens || 0)} | 输出：${Number(all.completion_tokens || 0)} | 总计：${Number(all.total_tokens || 0)}</div>
        <div class="list-meta">估算成本：${formatUsd(all.estimated_cost_usd || 0)}（${escHtml(pricingUnit)}）</div>
        ${renderCostBreakdown(all)}
      </div>
    `;
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger)">加载失败: ${escHtml(e.message)}</div>`;
  }
}

function renderModelPricingRows(items) {
  if (!items.length) return '<div class="empty">暂无模型单价配置</div>';
  return items.map((item) => {
    const modelRaw = String(item.model || '');
    const model = escHtml(modelRaw);
    const safeModelArg = JSON.stringify(modelRaw).replace(/'/g, '&#39;');
    const prompt = Number(item.prompt_price || 0).toFixed(4);
    const completion = Number(item.completion_price || 0).toFixed(4);
    return `
      <div class="list-item" style="cursor:default; margin-top:6px;">
        <div><span class="tag">${model}</span></div>
        <div class="list-meta">输入：${prompt} | 输出：${completion}</div>
        <div class="btn-group" style="margin-top:6px;">
          <button class="btn btn-danger" onclick='deleteModelPricingFromDashboard(${safeModelArg})'>删除</button>
        </div>
      </div>
    `;
  }).join('');
}

async function loadModelPricingForDashboard() {
  const el = document.getElementById('token-pricing-list');
  if (!el) return;
  el.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const data = await apiFetch('/automation/model-pricing');
    const items = Array.isArray(data.items) ? data.items : [];
    el.innerHTML = renderModelPricingRows(items);
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger)">加载失败: ${escHtml(e.message)}</div>`;
  }
}

async function saveModelPricingFromDashboard() {
  const model = (document.getElementById('pricing-model')?.value || '').trim();
  const promptRaw = (document.getElementById('pricing-prompt')?.value || '').trim();
  const completionRaw = (document.getElementById('pricing-completion')?.value || '').trim();
  const promptPrice = Number(promptRaw);
  const completionPrice = Number(completionRaw);

  if (!model) {
    showStatus('请先填写模型名', true);
    return;
  }
  if (!Number.isFinite(promptPrice) || promptPrice < 0 || !Number.isFinite(completionPrice) || completionPrice < 0) {
    showStatus('请输入合法的输入/输出价格（>= 0）', true);
    return;
  }
  try {
    await apiFetch('/automation/model-pricing', {
      method: 'POST',
      body: JSON.stringify({
        model,
        prompt_price: promptPrice,
        completion_price: completionPrice,
      }),
    });
    showStatus(`模型单价已保存：${model}`);
    await Promise.all([loadModelPricingForDashboard(), loadAutomationTokenSummary()]);
  } catch (e) {
    showStatus('保存模型单价失败: ' + e.message, true);
  }
}

async function deleteModelPricingFromDashboard(model) {
  if (!confirm(`确认删除模型单价：${model}？`)) return;
  try {
    await apiFetch(`/automation/model-pricing?model=${encodeURIComponent(model)}`, {
      method: 'DELETE',
    });
    showStatus(`已删除模型单价：${model}`);
    await Promise.all([loadModelPricingForDashboard(), loadAutomationTokenSummary()]);
  } catch (e) {
    showStatus('删除模型单价失败: ' + e.message, true);
  }
}

// ── Modal helpers ──

function openModal(title, bodyHtml, actions) {
  closeModal();
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.id = 'modal-overlay';
  overlay.onclick = (e) => { if (e.target === overlay) closeModal(); };
  overlay.innerHTML = `
    <div class="modal">
      <h3>${title}</h3>
      <div id="modal-body">${bodyHtml}</div>
      <div class="modal-actions" id="modal-actions"></div>
    </div>`;
  document.body.appendChild(overlay);
  const actionsEl = document.getElementById('modal-actions');
  if (actions) actions(actionsEl);
}

function closeModal() {
  const m = document.getElementById('modal-overlay');
  if (m) m.remove();
}

// ── Add Event Modal ──

function openAddEventModal() {
  const today = new Date().toISOString().split('T')[0];
  openModal('添加事件锚点', `
    <div class="form-group">
      <label>事件日期</label>
      <input type="date" id="ev-date" value="${today}">
    </div>
    <div class="form-group">
      <label>事件标题（可留空自动生成）</label>
      <input type="text" id="ev-title" placeholder="例如：凌晨讨论后形成的共识">
    </div>
    <div class="form-group">
      <label>事件描述</label>
      <textarea id="ev-desc" placeholder="以凯尔希的主观视角描述事件..."></textarea>
    </div>
    <div class="form-group">
      <label>关键词（逗号分隔）</label>
      <input type="text" id="ev-keywords" placeholder="关键词1, 关键词2, 关键词3">
    </div>
    <div class="form-group">
      <label>分类（逗号分隔，可留空自动分类）</label>
      <input type="text" id="ev-categories" placeholder="情感交流, 学术探讨">
    </div>
  `, (el) => {
    const cancel = document.createElement('button');
    cancel.className = 'btn'; cancel.textContent = '取消';
    cancel.onclick = closeModal;
    const save = document.createElement('button');
    save.className = 'btn btn-primary'; save.textContent = '保存';
    save.onclick = saveNewEvent;
    el.appendChild(cancel);
    el.appendChild(save);
  });
}

async function saveNewEvent() {
  const title = (document.getElementById('ev-title')?.value || '').trim();
  const desc = document.getElementById('ev-desc').value.trim();
  if (!desc) { alert('请填写事件描述'); return; }
  const date = document.getElementById('ev-date').value;
  const kwRaw = document.getElementById('ev-keywords').value;
  const keywords = kwRaw.split(/[,，、]/).map(s => s.trim()).filter(Boolean);
  const catRaw = document.getElementById('ev-categories').value;
  const categories = catRaw.split(/[,，、]/).map(s => s.trim()).filter(Boolean);
  const payload = { date, title, description: desc, source: 'manual', trigger_keywords: keywords };
  if (categories.length) payload.categories = categories;
  try {
    await apiFetch('/events', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    closeModal();
    showStatus('事件已添加');
    if (typeof loadEvents === 'function') loadEvents();
  } catch(e) { alert('保存失败: ' + e.message); }
}

// ── Edit Event Modal ──

function openEditEventModal(event) {
  let keywords = [];
  try { keywords = JSON.parse(event.trigger_keywords || '[]'); } catch(e) {}
  let categories = [];
  try { categories = JSON.parse(event.categories || '[]'); } catch(e) {}

  openModal('编辑事件锚点', `
    <div class="form-group">
      <label>事件标题</label>
      <input type="text" id="ev-title" value="${escHtml(event.title || '')}">
    </div>
    <div class="form-group">
      <label>事件描述</label>
      <textarea id="ev-desc">${escHtml(event.description)}</textarea>
    </div>
    <div class="form-group">
      <label>关键词（逗号分隔）</label>
      <input type="text" id="ev-keywords" value="${keywords.join(', ')}">
    </div>
    <div class="form-group">
      <label>分类（逗号分隔）</label>
      <input type="text" id="ev-categories" value="${categories.join(', ')}">
    </div>
    <div class="grid-2">
      <div class="form-group">
        <label>归档状态</label>
        <select id="ev-archived">
          <option value="0" ${event.archived ? '' : 'selected'}>active</option>
          <option value="1" ${event.archived ? 'selected' : ''}>archived</option>
        </select>
      </div>
      <div class="form-group">
        <label>当前评分</label>
        <div class="detail-content">重要性 ${event.importance_score !== null && event.importance_score !== undefined ? Number(event.importance_score).toFixed(1) : '未评分'} / 印象深度 ${event.impression_depth !== null && event.impression_depth !== undefined ? Number(event.impression_depth).toFixed(1) : '未评分'}</div>
      </div>
    </div>
  `, (el) => {
    const cancel = document.createElement('button');
    cancel.className = 'btn'; cancel.textContent = '取消';
    cancel.onclick = closeModal;
    const del = document.createElement('button');
    del.className = 'btn btn-danger'; del.textContent = '删除';
    del.onclick = () => deleteEvent(event.id);
    const save = document.createElement('button');
    save.className = 'btn btn-primary'; save.textContent = '保存修改';
    save.onclick = () => updateEvent(event.id);
    el.appendChild(cancel);
    el.appendChild(del);
    el.appendChild(save);
  });
}

async function updateEvent(id) {
  const title = (document.getElementById('ev-title')?.value || '').trim();
  const desc = document.getElementById('ev-desc').value.trim();
  const kwRaw = document.getElementById('ev-keywords').value;
  const keywords = kwRaw.split(/[,，、]/).map(s => s.trim()).filter(Boolean);
  const catRaw = document.getElementById('ev-categories').value;
  const categories = catRaw.split(/[,，、]/).map(s => s.trim()).filter(Boolean);
  const archived = Number(document.getElementById('ev-archived')?.value || 0);
  try {
    await apiFetch(`/events/${id}`, {
      method: 'PUT',
      body: JSON.stringify({ title, description: desc, trigger_keywords: keywords, categories, archived }),
    });
    closeModal();
    showStatus('事件已更新');
    if (typeof loadEvents === 'function') loadEvents();
  } catch(e) { alert('更新失败: ' + e.message); }
}

async function deleteEvent(id) {
  if (!confirm('确认删除此事件？')) return;
  try {
    await apiFetch(`/events/${id}`, { method: 'DELETE' });
    closeModal();
    showStatus('事件已删除');
    if (typeof loadEvents === 'function') loadEvents();
  } catch(e) { alert('删除失败: ' + e.message); }
}

// ── Trigger Snapshot ──

async function triggerSnapshot() {
  const content = prompt('输入快照内容（留空则由系统生成）:');
  if (content === null) return;
  if (content.trim()) {
    try {
      await apiFetch('/snapshots', {
        method: 'POST',
        body: JSON.stringify({ content: content.trim(), type: 'accumulated' }),
      });
      showStatus('快照已创建');
      loadDashboard();
    } catch(e) { alert('创建失败: ' + e.message); }
  } else {
    alert('请输入快照内容');
  }
}

// ── Test State Machine ──

function openTestPanel() {
  const defaultEnd = calendarDateStringShanghai();
  const defaultStart = calendarDateStringShanghai(new Date(Date.now() - 29 * 86400000));
  openModal('测试状态机', `
    <div class="tabs" id="test-tabs">
      <div class="tab active" data-tab="get-state">对话开始</div>
      <div class="tab" data-tab="reflect">对话结束</div>
      <div class="tab" data-tab="recall">记忆检索</div>
      <div class="tab" data-tab="periodic-review">阶段性回顾</div>
    </div>
    <div id="test-get-state">
      <div class="form-group">
        <label>当前时间（东八区 ISO，带 +08:00）</label>
        <input type="text" id="t-now" value="${toIsoStringShanghai()}">
      </div>
      <div class="form-group">
        <label>上次对话时间（东八区 ISO，带 +08:00）</label>
        <input type="text" id="t-last" value="${toIsoStringShanghai(new Date(Date.now() - 86400000))}">
      </div>
      <div class="form-group">
        <label class="checkbox-label" style="display:flex;align-items:center;gap:8px;cursor:pointer">
          <input type="checkbox" id="t-show-schedule" checked>
          返回检查点计划与实际生成快照（核对整格/尾部与 min_time_unit_hours）
        </label>
      </div>
      <div class="form-group">
        <button class="btn btn-primary" onclick="runGetState()">执行 get_current_state</button>
      </div>
    </div>
    <div id="test-reflect" style="display:none">
      <div class="form-group">
        <label>对话摘要</label>
        <textarea id="t-summary" placeholder="输入对话摘要..."></textarea>
      </div>
      <div class="form-group">
        <button class="btn btn-primary" onclick="runReflect()">执行 reflect_on_conversation</button>
      </div>
    </div>
    <div id="test-recall" style="display:none">
      <div class="form-group">
        <label>搜索关键词</label>
        <input type="text" id="t-query" placeholder="输入搜索关键词...">
      </div>
      <div class="form-group">
        <button class="btn btn-primary" onclick="runRecall()">执行 recall_memories</button>
      </div>
    </div>
    <div id="test-periodic-review" style="display:none">
      <div class="form-group">
        <label>起始日期</label>
        <input type="date" id="t-review-start" value="${defaultStart}">
      </div>
      <div class="form-group">
        <label>结束日期</label>
        <input type="date" id="t-review-end" value="${defaultEnd}">
      </div>
      <div class="form-group">
        <label style="display:flex;align-items:center;gap:8px">
          <input type="checkbox" id="t-review-include-archived">
          包含已归档事件
        </label>
      </div>
      <div class="form-group">
        <button class="btn btn-primary" onclick="runPeriodicReview()">执行 periodic_review</button>
      </div>
    </div>
    <div id="test-result" style="margin-top:16px"></div>
  `, (el) => {
    const close = document.createElement('button');
    close.className = 'btn'; close.textContent = '关闭';
    close.onclick = closeModal;
    el.appendChild(close);
  });

  $$('#test-tabs .tab').forEach(tab => {
    tab.onclick = () => {
      $$('#test-tabs .tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      ['test-get-state', 'test-reflect', 'test-recall', 'test-periodic-review'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.display = id === 'test-' + tab.dataset.tab.replace('-', '-') ? 'block' : 'none';
      });
      document.getElementById('test-get-state').style.display = tab.dataset.tab === 'get-state' ? 'block' : 'none';
      document.getElementById('test-reflect').style.display = tab.dataset.tab === 'reflect' ? 'block' : 'none';
      document.getElementById('test-recall').style.display = tab.dataset.tab === 'recall' ? 'block' : 'none';
      document.getElementById('test-periodic-review').style.display = tab.dataset.tab === 'periodic-review' ? 'block' : 'none';
    };
  });
}

function openPeriodicReviewPanel() {
  openTestPanel();
  const target = Array.from($$('#test-tabs .tab')).find(tab => tab.dataset.tab === 'periodic-review');
  if (target) target.click();
}

function normalizeIsoDateTimeInput(raw) {
  let s = String(raw ?? '').trim();
  if (!s) return s;
  // 与后端一致：去掉 T 与时间之间的误输入空格（如 2026-03-25T 20:08:35.868Z）
  s = s.replace(/[Tt]\s+/, 'T');
  // 与后端一致：2026-03-28 10:00:00 → 2026-03-28T10:00:00
  if (/^\d{4}-\d{2}-\d{2} \d/.test(s)) {
    s = s.replace(' ', 'T');
  }
  return s;
}

async function runGetState() {
  const res = document.getElementById('test-result');
  res.innerHTML = '<div class="loading">正在生成状态快照…（可能需要较长时间）</div>';
  try {
    const body = {
      current_time: normalizeIsoDateTimeInput(document.getElementById('t-now').value),
      last_interaction_time: normalizeIsoDateTimeInput(document.getElementById('t-last').value),
    };
    const showSchedEl = document.getElementById('t-show-schedule');
    if (showSchedEl && showSchedEl.checked) {
      body.include_checkpoint_schedule = true;
    }
    const data = await apiFetch('/state/current', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    let html = `<div class="detail-content">${escHtml(data.content)}</div>`;
    if (data.input_current_time_cst) {
      html += `<div class="list-meta" style="margin-top:10px">服务端按入参解析的东八区时刻：当前 <code>${escHtml(data.input_current_time_cst)}</code>，上次对话 <code>${escHtml(data.input_last_interaction_cst || '—')}</code></div>`;
    }
    if (data.checkpoint_schedule) {
      html += `<pre class="detail-content" style="margin-top:12px;white-space:pre-wrap;font-size:12px">${escHtml(JSON.stringify(data.checkpoint_schedule, null, 2))}</pre>`;
    }
    res.innerHTML = html;
    await loadDashboard();
    await loadAutomationLatest();
    await loadAutomationHistory();
    await loadAutomationTokenSummary();
  } catch(e) { res.innerHTML = `<div style="color:var(--danger)">${escHtml(e.message)}</div>`; }
}

async function runReflect() {
  const res = document.getElementById('test-result');
  res.innerHTML = '<div class="loading">正在生成反思…</div>';
  try {
    const data = await apiFetch('/state/reflect', {
      method: 'POST',
      body: JSON.stringify({ conversation_summary: document.getElementById('t-summary').value }),
    });
    res.innerHTML = `<div class="detail-content">${escHtml(data.content)}</div>`;
    await loadDashboard();
    await loadAutomationLatest();
    await loadAutomationHistory();
    await loadAutomationTokenSummary();
  } catch(e) { res.innerHTML = `<div style="color:var(--danger)">${escHtml(e.message)}</div>`; }
}

async function runRecall() {
  const res = document.getElementById('test-result');
  res.innerHTML = '<div class="loading">搜索中…</div>';
  try {
    const data = await apiFetch('/memories/search', {
      method: 'POST',
      body: JSON.stringify({ query: document.getElementById('t-query').value, top_k: 5 }),
    });
    if (!data.results || data.results.length === 0) {
      res.innerHTML = '<div class="empty">未找到相关记忆</div>';
    } else {
      const assocCount = data.results.filter(r => r?.metadata?.selection_reason === 'associative_random').length;
      res.innerHTML = data.results.map(r => `
        <div class="list-item">
          <div>
            <span class="tag">${r.source_type === 'event' ? '事件' : '快照'}</span>
            ${r?.metadata?.selection_reason === 'associative_random'
              ? '<span class="tag tag-assoc">联想命中</span>'
              : '<span class="tag tag-rank">排序命中</span>'}
            ${r?.metadata?.date ? `<span class="list-meta" style="margin-left:6px">日期: ${escHtml(String(r.metadata.date))}</span>` : ''}
          </div>
          <div class="list-preview">${escHtml(r.text)}</div>
          ${
            Array.isArray(r?.metadata?.categories) && r.metadata.categories.length
              ? `<div style="margin-top:4px">${r.metadata.categories.map(c => `<span class="tag tag-category">${escHtml(String(c))}</span>`).join('')}</div>`
              : ''
          }
        </div>
      `).join('');
      if (assocCount > 0) {
        res.innerHTML = `
          <div class="list-meta" style="margin-bottom:8px">
            本次检索中有 ${assocCount} 条“联想命中”。联想命中来自尾部候选的加权随机抽样（含多样性与冷门奖励）。
          </div>
        ` + res.innerHTML;
      }
    }
  } catch(e) { res.innerHTML = `<div style="color:var(--danger)">${escHtml(e.message)}</div>`; }
}

async function runPeriodicReview() {
  const res = document.getElementById('test-result');
  const startDate = (document.getElementById('t-review-start')?.value || '').trim();
  const endDate = (document.getElementById('t-review-end')?.value || '').trim();
  const includeArchived = !!document.getElementById('t-review-include-archived')?.checked;
  if (!startDate || !endDate) {
    res.innerHTML = '<div style="color:var(--danger)">请填写完整的起止日期</div>';
    return;
  }
  if (startDate > endDate) {
    res.innerHTML = '<div style="color:var(--danger)">日期范围无效：起始日期不能晚于结束日期</div>';
    return;
  }
  res.innerHTML = '<div class="loading">正在生成阶段性回顾…</div>';
  try {
    const data = await apiFetch('/review/periodic', {
      method: 'POST',
      body: JSON.stringify({
        start_date: startDate,
        end_date: endDate,
        include_archived: includeArchived,
      }),
    });
    const stats = data.stats || {};
    window.__latestPeriodicReview = {
      content: data.content || '',
      stats: {
        start_date: stats.start_date || startDate,
        end_date: stats.end_date || endDate,
        event_count: Number(stats.event_count || 0),
        snapshot_count: Number(stats.snapshot_count || 0),
      },
      include_archived: includeArchived,
    };
    res.innerHTML = `
      <div class="list-meta" style="margin-bottom:8px">
        时间范围：${escHtml(stats.start_date || startDate)} ~ ${escHtml(stats.end_date || endDate)} |
        事件数：${Number(stats.event_count || 0)} |
        快照数：${Number(stats.snapshot_count || 0)}
      </div>
      <div class="btn-group" style="margin-bottom:8px">
        <select id="periodic-review-export-format" style="max-width:160px">
          <option value="md">Markdown</option>
          <option value="txt">TXT</option>
          <option value="json">JSON</option>
        </select>
        <button class="btn" onclick="exportPeriodicReview()">导出本次回顾</button>
      </div>
      <div class="detail-content">${escHtml(data.content || '')}</div>
    `;
  } catch (e) {
    res.innerHTML = `<div style="color:var(--danger)">${escHtml(e.message)}</div>`;
  }
}

function buildPeriodicReviewExport(review, format) {
  const stats = review.stats || {};
  const rangeText = `${stats.start_date || ''} ~ ${stats.end_date || ''}`;
  if (format === 'json') {
    return JSON.stringify({
      export_time: new Date().toISOString(),
      type: 'periodic_review',
      include_archived: !!review.include_archived,
      stats,
      content: review.content || '',
    }, null, 2);
  }
  if (format === 'md') {
    return (
      `# 阶段性回顾\n\n` +
      `- 导出时间：${new Date().toLocaleString('zh-CN', { timeZone: DISPLAY_TZ })}\n` +
      `- 时间范围：${rangeText}\n` +
      `- 事件数：${Number(stats.event_count || 0)}\n` +
      `- 快照数：${Number(stats.snapshot_count || 0)}\n` +
      `- 包含归档事件：${review.include_archived ? '是' : '否'}\n\n` +
      `## 回顾正文\n\n` +
      `${review.content || ''}\n`
    );
  }
  return (
    `阶段性回顾导出\n` +
    `导出时间：${new Date().toLocaleString('zh-CN', { timeZone: DISPLAY_TZ })}\n` +
    `时间范围：${rangeText}\n` +
    `事件数：${Number(stats.event_count || 0)}\n` +
    `快照数：${Number(stats.snapshot_count || 0)}\n` +
    `包含归档事件：${review.include_archived ? '是' : '否'}\n\n` +
    `----- 回顾正文 -----\n` +
    `${review.content || ''}\n`
  );
}

function exportPeriodicReview() {
  const review = window.__latestPeriodicReview;
  if (!review || !review.content) {
    alert('暂无可导出的阶段性回顾，请先执行一次回顾生成。');
    return;
  }
  const formatEl = document.getElementById('periodic-review-export-format');
  const format = formatEl ? formatEl.value : 'md';
  const content = buildPeriodicReviewExport(review, format);
  const filename = `periodic_review_${formatExportTimestamp()}.${format}`;
  const mime = format === 'json'
    ? 'application/json;charset=utf-8'
    : format === 'md'
      ? 'text/markdown;charset=utf-8'
      : 'text/plain;charset=utf-8';
  downloadLocalFile(filename, content, mime);
  showStatus(`阶段性回顾已导出：${filename}`);
}

// ── Key Records Page ──

const KEY_RECORD_TYPE_LABELS = {
  important_date: '关键日期',
  important_item: '关键物品',
  key_collaboration: '关键协作',
  medical_advice: '医疗建议',
};

let latestKeyRecords = [];

function parseJsonArray(value) {
  if (!value) return [];
  if (Array.isArray(value)) return value;
  try {
    const arr = JSON.parse(value);
    return Array.isArray(arr) ? arr : [];
  } catch (e) {
    return [];
  }
}

function getKeyRecordTypeLabel(type) {
  return KEY_RECORD_TYPE_LABELS[type] || type || '未分类';
}

function initKeyRecordsPage() {
  loadKeyRecords();
}

async function loadKeyRecords() {
  const list = document.getElementById('key-record-list');
  if (!list) return;
  const typeFilter = document.getElementById('key-record-type-filter')?.value || '';
  const includeArchived = !!document.getElementById('key-record-include-archived')?.checked;
  const params = new URLSearchParams();
  params.set('limit', '100');
  if (typeFilter) params.set('record_type', typeFilter);
  if (includeArchived) params.set('include_archived', 'true');
  list.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const data = await apiFetch(`/key-records?${params.toString()}`);
    latestKeyRecords = data.items || [];
    renderKeyRecordList(latestKeyRecords);
    showStatus(`已加载 ${latestKeyRecords.length} 条关键记录`);
  } catch (e) {
    list.innerHTML = `<div style="color:var(--danger)">加载失败: ${escHtml(e.message)}</div>`;
  }
}

function renderKeyRecordList(items) {
  const list = document.getElementById('key-record-list');
  if (!list) return;
  if (!items || !items.length) {
    list.innerHTML = '<div class="empty">暂无关键记录</div>';
    return;
  }
  list.innerHTML = items.map(item => {
    if (item._result_kind === 'world_book') {
      const mk = Array.isArray(item.match_keywords) ? item.match_keywords : [];
      const tags = Array.isArray(item.tags) ? item.tags : [];
      const modes = Array.isArray(item._match_modes) ? item._match_modes.join('+') : '';
      const score = item._relevance_score != null ? Number(item._relevance_score).toFixed(3) : '';
      const tier = item._memory_tier === 'supplementary' ? '旁支' : '';
      const hint = truncate(String(item._usage_hint || ''), 120);
      return `
      <div class="list-item" style="cursor:default;border-left:3px solid var(--accent)">
        <div>
          <span class="tag">世界书</span>
          ${tier ? `<span class="tag">${escHtml(tier)}</span>` : ''}
          <span class="tag">${escHtml(modes || '命中')}</span>
          <span class="list-meta">相关度 ${escHtml(score)}</span>
        </div>
        ${hint ? `<div class="list-meta" style="margin-top:4px;font-style:italic">${escHtml(hint)}</div>` : ''}
        <div class="list-preview"><strong>${escHtml(item.name || '未命名条目')}</strong></div>
        <div class="list-preview">${escHtml(truncate(item.content || '', 220))}</div>
        <div class="list-meta">匹配词：${escHtml(mk.join('，') || '（无）')}</div>
        ${tags.length ? `<div style="margin-top:4px">${tags.map(t => `<span class="tag">${escHtml(String(t))}</span>`).join('')}</div>` : ''}
        <div class="btn-group" style="margin-top:8px">
          <button type="button" class="btn btn-mini" onclick="event.stopPropagation();location.href='/environment-manage'">环境管理</button>
        </div>
      </div>`;
    }
    const tags = parseJsonArray(item.tags);
    const typeLabel = getKeyRecordTypeLabel(item.type);
    const statusTag = item.status === 'archived'
      ? '<span class="tag">已归档</span>'
      : '<span class="tag" style="background:#2a4035;color:var(--success)">生效中</span>';
    const dateRange = item.start_date || item.end_date
      ? `${item.start_date || '未设开始'} ~ ${item.end_date || '未设结束'}`
      : '';
    const krPayload = { ...item };
    delete krPayload._result_kind;
    delete krPayload._relevance_score;
    delete krPayload._memory_tier;
    delete krPayload._usage_hint;
    delete krPayload._content_for_prompt;
    const krHint = item._memory_tier === 'primary'
      ? truncate(String(item._usage_hint || ''), 100)
      : '';
    return `
      <div class="list-item ${item.status === 'archived' ? 'archived' : ''}" onclick='openEditKeyRecordModal(${JSON.stringify(krPayload).replace(/'/g, "&#39;")})'>
        <div>
          <span class="tag">${escHtml(typeLabel)}</span>
          ${item._memory_tier === 'primary' ? '<span class="tag">主序</span>' : ''}
          ${statusTag}
          <span class="tag">${escHtml(item.source || 'manual')}</span>
          <span class="list-meta">${formatTime(item.updated_at)}</span>
          ${item._relevance_score != null ? `<span class="list-meta">相关度 ${escHtml(Number(item._relevance_score).toFixed(3))}</span>` : ''}
        </div>
        ${krHint ? `<div class="list-meta" style="margin-top:4px;font-style:italic">${escHtml(krHint)}</div>` : ''}
        <div class="list-preview"><strong>${escHtml(item.title || '未命名记录')}</strong></div>
        <div class="list-preview">${escHtml(truncate(item.content_text || '', 220))}</div>
        ${dateRange ? `<div class="list-meta">有效期: ${escHtml(dateRange)}</div>` : ''}
        ${tags.length ? `<div style="margin-top:4px">${tags.map(t => `<span class="tag">${escHtml(String(t))}</span>`).join('')}</div>` : ''}
      </div>
    `;
  }).join('');
}

async function searchKeyRecords() {
  const list = document.getElementById('key-record-list');
  if (!list) return;
  const query = (document.getElementById('key-record-search-input')?.value || '').trim();
  if (!query) {
    await loadKeyRecords();
    return;
  }
  const typeFilter = document.getElementById('key-record-type-filter')?.value || '';
  const includeArchived = !!document.getElementById('key-record-include-archived')?.checked;
  list.innerHTML = '<div class="loading">搜索中…</div>';
  try {
    const includeWorldBooks = !!document.getElementById('key-record-include-world-books')?.checked;
    const data = await apiFetch('/key-records/search', {
      method: 'POST',
      body: JSON.stringify({
        query,
        type: typeFilter || null,
        top_k: 50,
        include_archived: includeArchived,
        include_world_books: includeWorldBooks,
      }),
    });
    latestKeyRecords = data.items || [];
    renderKeyRecordList(latestKeyRecords);
    showStatus(`关键记录检索完成，共 ${latestKeyRecords.length} 条`);
  } catch (e) {
    list.innerHTML = `<div style="color:var(--danger)">搜索失败: ${escHtml(e.message)}</div>`;
  }
}

function openAddKeyRecordModal() {
  const today = new Date().toISOString().split('T')[0];
  openModal('添加关键记录', `
    <div class="form-group">
      <label>类型</label>
      <select id="kr-type">
        <option value="important_date">关键日期</option>
        <option value="important_item" selected>关键物品</option>
        <option value="key_collaboration">关键协作</option>
        <option value="medical_advice">医疗建议</option>
      </select>
    </div>
    <div class="form-group">
      <label>标题</label>
      <input type="text" id="kr-title" placeholder="例如：近期胃痛用药建议">
    </div>
    <div class="form-group">
      <label>正文（可粘贴表格/建议）</label>
      <textarea id="kr-content" placeholder="输入详细记录内容..."></textarea>
    </div>
    <div class="form-group">
      <label>标签（逗号分隔）</label>
      <input type="text" id="kr-tags" placeholder="胃痛, 用药, 晚间">
    </div>
    <div class="grid-2">
      <div class="form-group">
        <label>开始日期（可选）</label>
        <input type="date" id="kr-start" value="${today}">
      </div>
      <div class="form-group">
        <label>结束日期（可选）</label>
        <input type="date" id="kr-end">
      </div>
    </div>
    <div class="form-group">
      <label>状态</label>
      <select id="kr-status">
        <option value="active" selected>active</option>
        <option value="archived">archived</option>
      </select>
    </div>
  `, (el) => {
    const cancel = document.createElement('button');
    cancel.className = 'btn';
    cancel.textContent = '取消';
    cancel.onclick = closeModal;
    const save = document.createElement('button');
    save.className = 'btn btn-primary';
    save.textContent = '保存';
    save.onclick = saveNewKeyRecord;
    el.appendChild(cancel);
    el.appendChild(save);
  });
}

async function saveNewKeyRecord() {
  const payload = {
    type: document.getElementById('kr-type')?.value || 'important_item',
    title: (document.getElementById('kr-title')?.value || '').trim(),
    content_text: (document.getElementById('kr-content')?.value || '').trim(),
    tags: (document.getElementById('kr-tags')?.value || '')
      .split(/[,，、]/)
      .map(s => s.trim())
      .filter(Boolean),
    start_date: (document.getElementById('kr-start')?.value || '').trim() || null,
    end_date: (document.getElementById('kr-end')?.value || '').trim() || null,
    status: document.getElementById('kr-status')?.value || 'active',
    source: 'manual',
  };
  if (!payload.title) {
    alert('请填写标题');
    return;
  }
  if (!payload.content_text) {
    alert('请填写正文内容');
    return;
  }
  try {
    await apiFetch('/key-records', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    closeModal();
    showStatus('关键记录已添加');
    await loadKeyRecords();
  } catch (e) {
    alert('保存失败: ' + e.message);
  }
}

function openEditKeyRecordModal(record) {
  const tags = parseJsonArray(record.tags);
  openModal('编辑关键记录', `
    <div class="form-group">
      <label>类型</label>
      <select id="kr-type">
        ${Object.entries(KEY_RECORD_TYPE_LABELS).map(([value, label]) => `
          <option value="${value}" ${record.type === value ? 'selected' : ''}>${label}</option>
        `).join('')}
      </select>
    </div>
    <div class="form-group">
      <label>标题</label>
      <input type="text" id="kr-title" value="${escHtml(record.title || '')}">
    </div>
    <div class="form-group">
      <label>正文</label>
      <textarea id="kr-content">${escHtml(record.content_text || '')}</textarea>
    </div>
    <div class="form-group">
      <label>标签（逗号分隔）</label>
      <input type="text" id="kr-tags" value="${escHtml(tags.join(', '))}">
    </div>
    <div class="grid-2">
      <div class="form-group">
        <label>开始日期</label>
        <input type="date" id="kr-start" value="${record.start_date || ''}">
      </div>
      <div class="form-group">
        <label>结束日期</label>
        <input type="date" id="kr-end" value="${record.end_date || ''}">
      </div>
    </div>
    <div class="form-group">
      <label>状态</label>
      <select id="kr-status">
        <option value="active" ${record.status === 'active' ? 'selected' : ''}>active</option>
        <option value="archived" ${record.status === 'archived' ? 'selected' : ''}>archived</option>
      </select>
    </div>
  `, (el) => {
    const cancel = document.createElement('button');
    cancel.className = 'btn';
    cancel.textContent = '取消';
    cancel.onclick = closeModal;
    const del = document.createElement('button');
    del.className = 'btn btn-danger';
    del.textContent = '删除';
    del.onclick = () => deleteKeyRecord(record.id);
    const save = document.createElement('button');
    save.className = 'btn btn-primary';
    save.textContent = '保存修改';
    save.onclick = () => updateKeyRecord(record.id);
    el.appendChild(cancel);
    el.appendChild(del);
    el.appendChild(save);
  });
}

async function updateKeyRecord(id) {
  const payload = {
    type: document.getElementById('kr-type')?.value || 'important_item',
    title: (document.getElementById('kr-title')?.value || '').trim(),
    content_text: (document.getElementById('kr-content')?.value || '').trim(),
    tags: (document.getElementById('kr-tags')?.value || '')
      .split(/[,，、]/)
      .map(s => s.trim())
      .filter(Boolean),
    start_date: (document.getElementById('kr-start')?.value || '').trim() || null,
    end_date: (document.getElementById('kr-end')?.value || '').trim() || null,
    status: document.getElementById('kr-status')?.value || 'active',
  };
  if (!payload.title || !payload.content_text) {
    alert('标题和正文不能为空');
    return;
  }
  try {
    await apiFetch(`/key-records/${id}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    closeModal();
    showStatus('关键记录已更新');
    await loadKeyRecords();
  } catch (e) {
    alert('更新失败: ' + e.message);
  }
}

async function deleteKeyRecord(id) {
  if (!confirm('确认删除这条关键记录？')) return;
  try {
    await apiFetch(`/key-records/${id}`, { method: 'DELETE' });
    closeModal();
    showStatus('关键记录已删除');
    await loadKeyRecords();
  } catch (e) {
    alert('删除失败: ' + e.message);
  }
}

// ── History Page ──

let currentTab = 'snapshots';
let showArchivedEvents = false;
let latestEvolutionPreview = null;
let latestEvolutionPreviewSourceLabel = '';
let evolutionCandidatesExpanded = false;
let evolutionTopEventsExpanded = false;
const EVENT_CATEGORY_OPTIONS = [
  '情感交流',
  '学术探讨',
  '生活足迹',
  '床榻私语',
  '精神碰撞',
  '工作同步',
];
let historyManageMode = false;
let latestSnapshots = [];
let latestEvents = [];
const selectedSnapshotIds = new Set();
const selectedEventIds = new Set();

function getHistorySelectionSet(tab) {
  return tab === 'snapshots' ? selectedSnapshotIds : selectedEventIds;
}

function getHistoryItems(tab) {
  return tab === 'snapshots' ? latestSnapshots : latestEvents;
}

function getSelectedEventCategories() {
  const container = document.getElementById('event-category-filter');
  if (!container) return [];
  return Array.from(container.querySelectorAll('input[type="checkbox"]:checked'))
    .map(el => el.value)
    .filter(Boolean);
}

function onEventCategoryFilterChange() {
  if (currentTab === 'events') loadEvents();
}

function clearEventCategoryFilter() {
  const container = document.getElementById('event-category-filter');
  if (container) {
    container.querySelectorAll('input[type="checkbox"]').forEach(el => {
      el.checked = false;
    });
  }
  if (currentTab === 'events') loadEvents();
}

function initSnapshotsHistoryPage() {
  currentTab = 'snapshots';
  historyManageMode = false;
  updateHistorySelectionSummary();
  loadSnapshots();
}

function initEventsHistoryPage() {
  currentTab = 'events';
  historyManageMode = false;
  const container = document.getElementById('event-category-filter');
  if (container && !container.children.length) {
    EVENT_CATEGORY_OPTIONS.forEach((c, i) => {
      const item = document.createElement('label');
      item.className = 'category-filter-item';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.value = c;
      input.id = `event-cat-${i}`;
      input.onchange = onEventCategoryFilterChange;
      const text = document.createElement('span');
      text.textContent = c;
      item.appendChild(input);
      item.appendChild(text);
      container.appendChild(item);
    });
  }
  updateHistorySelectionSummary();
  loadEvents();
}

function updateHistorySelectionSummary() {
  const el = document.getElementById('history-selection-summary');
  const toggleBtn = document.getElementById('toggle-manage-btn');
  if (!el) return;
  const currentCount = getHistorySelectionSet(currentTab).size;
  if (!historyManageMode) {
    el.textContent = '管理模式未开启';
    if (toggleBtn) toggleBtn.textContent = '开启选择管理';
    return;
  }
  if (toggleBtn) toggleBtn.textContent = '退出选择管理';
  el.textContent = `已开启管理模式：当前 ${currentTab === 'snapshots' ? '快照' : '事件'} 已选 ${currentCount} 条`;
}

function toggleHistoryManageMode() {
  historyManageMode = !historyManageMode;
  updateHistorySelectionSummary();
  if (currentTab === 'snapshots') loadSnapshots();
  else loadEvents();
}

function toggleHistoryItemSelection(tab, id, forceChecked) {
  const set = getHistorySelectionSet(tab);
  const checked = forceChecked === undefined ? !set.has(id) : !!forceChecked;
  if (checked) set.add(id);
  else set.delete(id);
  const row = document.querySelector(`[data-history-item="${tab}-${id}"]`);
  if (row) row.classList.toggle('selected', set.has(id));
  const checkbox = row ? row.querySelector('.history-item-checkbox') : null;
  if (checkbox) checkbox.checked = set.has(id);
  updateHistorySelectionSummary();
}

function onHistoryItemRowClick(tab, id) {
  if (historyManageMode) {
    toggleHistoryItemSelection(tab, id);
    return;
  }
  if (tab === 'snapshots') {
    const snap = latestSnapshots.find(s => s.id === id);
    if (snap) showSnapshotDetail(snap);
    return;
  }
  const ev = latestEvents.find(e => e.id === id);
  if (ev) openEditEventModal(ev);
}

function clearHistorySelection() {
  getHistorySelectionSet(currentTab).clear();
  if (currentTab === 'snapshots') loadSnapshots();
  else loadEvents();
}

function selectAllHistoryItems() {
  const items = getHistoryItems(currentTab);
  if (!items.length) {
    showStatus('当前列表为空，无可选择条目');
    return;
  }
  const set = getHistorySelectionSet(currentTab);
  items.forEach(item => set.add(item.id));
  if (currentTab === 'snapshots') loadSnapshots();
  else loadEvents();
}

async function deleteSelectedHistoryItems() {
  const selectedIds = Array.from(getHistorySelectionSet(currentTab));
  if (!selectedIds.length) {
    alert('请先勾选要删除的条目');
    return;
  }
  const typeName = currentTab === 'snapshots' ? '状态快照' : '事件锚点';
  if (!confirm(`确认删除选中的 ${selectedIds.length} 条${typeName}？此操作不可撤销。`)) return;
  try {
    for (const id of selectedIds) {
      if (currentTab === 'snapshots') await apiFetch(`/snapshots/${id}`, { method: 'DELETE' });
      else await apiFetch(`/events/${id}`, { method: 'DELETE' });
    }
    getHistorySelectionSet(currentTab).clear();
    if (currentTab === 'snapshots') await loadSnapshots();
    else await loadEvents();
    showStatus(`已删除 ${selectedIds.length} 条${typeName}`);
  } catch (e) {
    showStatus('批量删除失败: ' + e.message, true);
  }
}

function pad2(v) {
  return String(v).padStart(2, '0');
}

function formatExportTimestamp() {
  const d = new Date();
  return `${d.getFullYear()}${pad2(d.getMonth() + 1)}${pad2(d.getDate())}_${pad2(d.getHours())}${pad2(d.getMinutes())}${pad2(d.getSeconds())}`;
}

function downloadLocalFile(filename, content, mimeType) {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function formatSnapshotExportEntry(s, index) {
  let envSummary = '';
  try {
    const env = JSON.parse(s.environment || '{}');
    const a = String(env.activity || '').trim();
    const b = String(env.summary || '').trim();
    if (a && b && a !== b) envSummary = `${a}\n\n---\n\n${b}`;
    else envSummary = a || b || '';
  } catch (e) {
    envSummary = '';
  }
  return {
    index,
    id: s.id,
    created_at: s.created_at,
    type: s.type,
    content: s.content,
    environment_summary: envSummary,
    referenced_events: s.referenced_events,
  };
}

function formatEventExportEntry(e, index) {
  let keywords = [];
  let categories = [];
  try {
    keywords = JSON.parse(e.trigger_keywords || '[]');
  } catch (ex) {
    keywords = [];
  }
  try {
    categories = JSON.parse(e.categories || '[]');
  } catch (ex) {
    categories = [];
  }
  return {
    index,
    id: e.id,
    title: e.title || '',
    date: e.date,
    created_at: e.created_at,
    source: e.source,
    archived: !!e.archived,
    importance_score: e.importance_score,
    impression_depth: e.impression_depth,
    categories,
    trigger_keywords: keywords,
    description: e.description,
  };
}

function buildExportText(tab, rows, format) {
  if (format === 'json') {
    return JSON.stringify({
      export_time: new Date().toISOString(),
      tab,
      count: rows.length,
      items: rows,
    }, null, 2);
  }
  if (format === 'md') {
    const title = tab === 'snapshots' ? '状态快照导出' : '事件锚点导出';
    let md = `# ${title}\n\n`;
    md += `- 导出时间：${new Date().toLocaleString('zh-CN', { timeZone: DISPLAY_TZ })}\n`;
    md += `- 条目数量：${rows.length}\n\n`;
    rows.forEach((row, idx) => {
      md += `## ${idx + 1}. #${row.id}\n\n`;
      Object.entries(row).forEach(([k, v]) => {
        if (k === 'index' || k === 'id') return;
        const val = typeof v === 'string' ? v : JSON.stringify(v, null, 2);
        md += `- **${k}**：${val}\n`;
      });
      md += '\n';
    });
    return md;
  }
  let txt = `${tab === 'snapshots' ? '状态快照' : '事件锚点'}导出\n`;
  txt += `导出时间：${new Date().toLocaleString('zh-CN', { timeZone: DISPLAY_TZ })}\n`;
  txt += `条目数量：${rows.length}\n\n`;
  rows.forEach((row, idx) => {
    txt += `===== ${idx + 1} / ID ${row.id} =====\n`;
    Object.entries(row).forEach(([k, v]) => {
      if (k === 'index' || k === 'id') return;
      txt += `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}\n`;
    });
    txt += '\n';
  });
  return txt;
}

function exportSelectedHistoryItems() {
  const selectedIds = Array.from(getHistorySelectionSet(currentTab));
  if (!selectedIds.length) {
    alert('请先勾选要导出的条目');
    return;
  }
  const formatEl = document.getElementById('history-export-format');
  const format = formatEl ? formatEl.value : 'txt';
  const sourceItems = getHistoryItems(currentTab);
  const rows = sourceItems
    .filter(item => selectedIds.includes(item.id))
    .map((item, idx) => (currentTab === 'snapshots'
      ? formatSnapshotExportEntry(item, idx + 1)
      : formatEventExportEntry(item, idx + 1)));
  const content = buildExportText(currentTab, rows, format);
  const filename = `${currentTab}_${formatExportTimestamp()}.${format}`;
  const mime = format === 'json' ? 'application/json;charset=utf-8'
    : format === 'md' ? 'text/markdown;charset=utf-8'
      : 'text/plain;charset=utf-8';
  downloadLocalFile(filename, content, mime);
  showStatus(`已导出 ${rows.length} 条${currentTab === 'snapshots' ? '快照' : '事件'}到 ${filename}`);
}

async function loadSnapshots() {
  const list = $('#data-list');
  if (!list) return;
  list.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const data = await apiFetch('/snapshots?limit=50');
    latestSnapshots = data.items || [];
    if (!data.items || data.items.length === 0) {
      list.innerHTML = '<div class="empty">暂无快照记录</div>';
      updateHistorySelectionSummary();
      return;
    }
    list.innerHTML = data.items.map(s => `
      <div
        class="list-item ${historyManageMode ? 'selectable' : ''} ${selectedSnapshotIds.has(s.id) ? 'selected' : ''}"
        data-history-item="snapshots-${s.id}"
        onclick="onHistoryItemRowClick('snapshots', ${s.id})"
      >
        ${historyManageMode ? `
          <div class="select-col">
            <input
              type="checkbox"
              class="history-item-checkbox"
              ${selectedSnapshotIds.has(s.id) ? 'checked' : ''}
              onclick="event.stopPropagation()"
              onchange="toggleHistoryItemSelection('snapshots', ${s.id}, this.checked)"
            >
          </div>
        ` : ''}
        <div class="item-main">
          <div>
            <span class="tag">${s.type}</span>
            <span class="list-meta">${escHtml(snapshotTimeLineText(s))}</span>
            ${s.embedding_vector_id ? '<span class="tag" style="background:#2a4035;color:var(--success)">已归档</span>' : ''}
          </div>
          <div class="list-preview">${escHtml(truncate(s.content, 150))}</div>
        </div>
      </div>
    `).join('');
    showStatus(`已加载 ${data.items.length} 条快照`);
    updateHistorySelectionSummary();
  } catch(e) { list.innerHTML = `<div style="color:var(--danger)">加载失败: ${escHtml(e.message)}</div>`; }
}

async function loadEvents() {
  const list = $('#data-list');
  if (!list) return;
  list.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const selectedCategories = getSelectedEventCategories();
    const categoryQuery = selectedCategories.length
      ? `&categories=${encodeURIComponent(selectedCategories.join(','))}`
      : '';
    const data = await apiFetch(`/events?limit=50&include_archived=${showArchivedEvents}${categoryQuery}`);
    latestEvents = data.items || [];
    if (!data.items || data.items.length === 0) {
      list.innerHTML = '<div class="empty">暂无事件记录</div>';
      updateHistorySelectionSummary();
      return;
    }
    list.innerHTML = data.items.map(e => {
      let kw = [];
      try { kw = JSON.parse(e.trigger_keywords || '[]'); } catch(ex) {}
      let categories = [];
      try { categories = JSON.parse(e.categories || '[]'); } catch(ex) {}
      const archivedClass = e.archived ? 'archived' : '';
      const scoreLabel = (e.importance_score !== null && e.importance_score !== undefined)
        ? ` | 重要性 ${Number(e.importance_score).toFixed(1)} / 印象 ${Number(e.impression_depth || 0).toFixed(1)}`
        : '';
      return `
        <div
          class="list-item ${archivedClass} ${historyManageMode ? 'selectable' : ''} ${selectedEventIds.has(e.id) ? 'selected' : ''}"
          data-history-item="events-${e.id}"
          onclick="onHistoryItemRowClick('events', ${e.id})"
        >
          ${historyManageMode ? `
            <div class="select-col">
              <input
                type="checkbox"
                class="history-item-checkbox"
                ${selectedEventIds.has(e.id) ? 'checked' : ''}
                onclick="event.stopPropagation()"
                onchange="toggleHistoryItemSelection('events', ${e.id}, this.checked)"
              >
            </div>
          ` : ''}
          <div class="item-main">
            <div>
              <span class="tag">${e.source}</span>
              ${e.archived ? '<span class="tag">已归档</span>' : ''}
              ${e.title ? `<span class="tag">${escHtml(e.title)}</span>` : ''}
              <span class="list-meta">${e.date} | ${formatTime(e.created_at)}</span>
            </div>
            <div class="list-preview">${escHtml(truncate(e.description, 150))}</div>
            <div class="list-meta">${scoreLabel ? scoreLabel.slice(3) : '未评分'}</div>
            <div style="margin-top:4px">${categories.map(c => `<span class="tag" style="background:#3b3049;color:#d6c6ff">${escHtml(c)}</span>`).join('')}</div>
            <div style="margin-top:4px">${kw.map(k => `<span class="tag">${escHtml(k)}</span>`).join('')}</div>
          </div>
        </div>
      `;
    }).join('');
    showStatus(`已加载 ${data.items.length} 条事件`);
    updateHistorySelectionSummary();
  } catch(e) { list.innerHTML = `<div style="color:var(--danger)">加载失败: ${escHtml(e.message)}</div>`; }
}

function showSnapshotDetail(snap) {
  let envHtml = '';
  if (snap.environment && snap.environment !== '{}') {
    try {
      const env = JSON.parse(snap.environment);
      const inner = formatEnvironmentBlocks(env, 'detail-content');
      if (inner) envHtml = `<div class="form-group"><label>环境信息</label>${inner}</div>`;
    } catch(e) {}
  }
  openModal(`快照详情 #${snap.id}`, `
    <div class="list-meta" style="margin-bottom:12px">
      类型: ${escHtml(snap.type)} | ${escHtml(snapshotTimeLineText(snap))}
      ${snap.embedding_vector_id ? ' | 已归档' : ''}
    </div>
    <div class="detail-content">${escHtml(snap.content)}</div>
    ${envHtml}
  `, (el) => {
    const del = document.createElement('button');
    del.className = 'btn btn-danger'; del.textContent = '删除';
    del.onclick = async () => {
      if (!confirm('确认删除？')) return;
      await apiFetch(`/snapshots/${snap.id}`, { method: 'DELETE' });
      closeModal();
      loadSnapshots();
    };
    const close = document.createElement('button');
    close.className = 'btn'; close.textContent = '关闭';
    close.onclick = closeModal;
    el.appendChild(del);
    el.appendChild(close);
  });
}

function switchHistoryTab(tab) {
  currentTab = tab;
  $$('.tabs .tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  updateHistorySelectionSummary();
  if (tab === 'snapshots') loadSnapshots();
  else loadEvents();
}

// ── Search ──

async function runSearch() {
  const q = $('#search-input')?.value?.trim();
  if (!q) {
    if (currentTab === 'snapshots') await loadSnapshots();
    else await loadEvents();
    return;
  }
  const list = $('#data-list');
  list.innerHTML = '<div class="loading">搜索中…</div>';
  if (historyManageMode) {
    historyManageMode = false;
    updateHistorySelectionSummary();
  }
  try {
    const data = await apiFetch(`/search?q=${encodeURIComponent(q)}&limit=20&include_archived=true`);
    if (currentTab === 'events') {
      latestEvents = (data.events || []).filter(e => showArchivedEvents || !e.archived);
      const selectedCategories = getSelectedEventCategories();
      if (selectedCategories.length) {
        latestEvents = latestEvents.filter(e => {
          let cats = [];
          try { cats = JSON.parse(e.categories || '[]'); } catch (ex) {}
          return selectedCategories.some(c => cats.includes(c));
        });
      }
      if (!latestEvents.length) {
        list.innerHTML = '<div class="empty">未找到匹配事件</div>';
      } else {
        list.innerHTML = latestEvents.map(e => {
          let kw = [];
          try { kw = JSON.parse(e.trigger_keywords || '[]'); } catch(ex) {}
          let categories = [];
          try { categories = JSON.parse(e.categories || '[]'); } catch(ex) {}
          return `
            <div class="list-item ${e.archived ? 'archived' : ''}" onclick='openEditEventModal(${JSON.stringify(e).replace(/'/g, "&#39;")})'>
              <span class="tag">${e.source}</span>
              ${e.archived ? '<span class="tag">已归档</span>' : ''}
              ${e.title ? `<span class="tag">${escHtml(e.title)}</span>` : ''}
              <span class="list-meta">${e.date}</span>
              <div class="list-preview">${escHtml(truncate(e.description, 150))}</div>
              <div style="margin-top:4px">${categories.map(c => `<span class="tag" style="background:#3b3049;color:#d6c6ff">${escHtml(c)}</span>`).join('')}</div>
              <div style="margin-top:4px">${kw.map(k => `<span class="tag">${escHtml(k)}</span>`).join('')}</div>
            </div>
          `;
        }).join('');
      }
      showStatus(`事件检索完成，共 ${latestEvents.length} 条`);
      return;
    }

    latestSnapshots = data.snapshots || [];
    if (!latestSnapshots.length) {
      list.innerHTML = '<div class="empty">未找到匹配快照</div>';
    } else {
      list.innerHTML = latestSnapshots.map(s => `
        <div class="list-item" onclick='showSnapshotDetail(${JSON.stringify(s).replace(/'/g, "&#39;")})'>
          <span class="tag">${s.type}</span>
          <span class="list-meta">${escHtml(snapshotTimeLineText(s))}</span>
          <div class="list-preview">${escHtml(truncate(s.content, 150))}</div>
        </div>
      `).join('');
    }
    showStatus(`快照检索完成，共 ${latestSnapshots.length} 条`);
  } catch(e) { list.innerHTML = `<div style="color:var(--danger)">${escHtml(e.message)}</div>`; }
}

function toggleArchivedEvents() {
  const checkbox = $('#toggle-archived');
  showArchivedEvents = !!(checkbox && checkbox.checked);
  if (currentTab === 'events') loadEvents();
}

// ── Settings Page ──

const SETTINGS_KEYS = [
  'L1_character_background',
  'L1_user_background',
  'L2_character_personality',
  'L2_relationship_dynamics',
  'L2_life_status',
  'prompt_snapshot_generation',
  'prompt_event_anchor',
  'prompt_reflect_snapshot',
  'prompt_reflect_event',
  'prompt_conversation_summary',
  'prompt_periodic_review',
  'prompt_evolution_summary',
  'prompt_event_scoring',
  'prompt_environment_generation',
  'evolution_event_threshold',
  'archive_importance_threshold',
  'archive_depth_threshold',
  'evolution_prompt_importance_min',
  'evolution_prompt_depth_min',
  'evolution_prompt_drop_importance_below',
  'evolution_prompt_drop_depth_below',
  'evolution_prompt_max_events',
  'min_time_unit_hours',
  'inject_hot_events_limit',
  'llm_api_base',
  'llm_api_key',
  'llm_model',
  'llm_timeout_sec',
  'vector_embedding_api_base',
  'vector_embedding_api_key',
  'vector_embedding_model',
  'vector_embedding_dim',
  'vector_embedding_timeout_sec',
  'vector_sync_batch_size',
  'vector_snapshot_days_threshold',
  'vector_search_top_k',
  'vector_cold_days_threshold',
  'vector_compaction_group_size',
  'vector_compaction_max_groups',
  'automation_enabled',
  'automation_vector_sync',
  'automation_auto_evolution',
  'automation_cold_compaction',
  'automation_compaction_min_interval_hours',
  'snapshot_scheduler_enabled',
  'snapshot_scheduler_interval_sec',
];

const PERSONA_SETTINGS_KEYS = [
  'L1_character_background',
  'L1_user_background',
  'L2_character_personality',
  'L2_relationship_dynamics',
  'L2_life_status',
];

const PROMPT_DEFAULT_SAMPLES = {
  prompt_snapshot_generation: `基于以下信息，以凯尔希的第一人称视角，写一段内心状态独白。
这段独白应该反映凯尔希此刻的心理状态、关注的事务、以及对近期发生事件的思考。

【当前环境信息】
{environment}

【上一个状态】
{previous_snapshot}

【近期事件记录】
{recent_events}

【历史记忆参考】
{memory_context}

要求：
1. 以"我"为第一人称，体现凯尔希的性格和思维方式
2. 自然地融入、理解、加工环境信息，不要生硬地列举
3. 体现时间流逝带来的状态变化，状态过渡的逻辑需要自然通顺
4. 保持500字以内的长度
5. 不需要标题，直接写独白内容`,
  prompt_event_anchor: `事件锚点用于在调用回忆功能时快速定位「何事发生」，并附带当时的一般状态感受，辅助对话顺利推进。

从凯尔希自身的角度出发，基于以下信息，判断状态快照和环境信息中是否有值得记录的事件发生。
若有，生成事件锚点；若无值得特别记录的事，只输出一行：无需记录（不要输出其他任何说明）。

【当前状态快照（主观感受来源）】
{current_snapshot}

【环境信息（客观事件来源）】
{environment}

【角色分层设定参考】
{system_layers}

【历史记忆参考】
{memory_context}

判断与撰写原则：
1. 先站在凯尔希的立场判断「是否值得单独记一笔」：仅是情绪起伏、无新事实、与近期记忆高度重复、或纯属日常琐屑，则输出「无需记录」。
2. 若需记录，必须同时给出两部分：A. 客观记录（发生了什么，涉及谁/何物/何处、关键行为或转折）；B. 主观印象（凯尔希对此事的浓缩感受与评价，2-3 句）。
3. 客观记录优先依据「环境信息」抽取可核对的事实；不要只复述状态快照里的情绪用语，可结合快照补充「我当时如何感受」，但事实骨架应来自环境。
4. 标题必须具体，且至少包含一个可指向实体的信息（人名、物品名、活动名、地名、组织名、专有名词等）；避免「又一次谈话」「心情不错」这类空泛标题。
5. 关键词共 4-8 个，须具体、可检索，优先包含：人物名/物品名/地名/组织名/活动名/核心动作词。
6. 禁止把抽象词当关键词（例如：情感交流、深度对话、生命共振、存在重构、灵魂共鸣）。
7. 禁止把分类名或笼统类型直接当作关键词（例如：「情感交流」「学术探讨」整词作为关键词）。
8. 给出 1-3 个事件分类，仅从下列中选择：情感交流、学术探讨、生活足迹、床榻私语、精神碰撞、工作同步。

输出格式（仅当存在值得记录的事件时，按下列字段逐行输出，冒号可用中文或英文；日期写实际事件语境中的日期，若无法判断则写「当日」或与快照一致的一天）：
标题：[具体事件标题]
日期：[YYYY-MM-DD 或当日/语境日期说明]
客观记录：[事件客观经过，含人物/行为/对象/场景等可定位信息]
主观印象：[凯尔希的浓缩感受与评价，2-3 句]
关键词：[关键词1, 关键词2, 关键词3, ...]
分类：[分类1, 分类2]`,
  prompt_reflect_snapshot: `基于以下信息，以凯尔希的第一人称视角，写一段对话结束后的内心状态独白。
这段独白应该反映对话对凯尔希心理状态的影响和她对谈话内容的思考。

【对话前的状态】
{previous_snapshot}

【对话摘要】
{conversation_summary}

【历史记忆参考】
{memory_context}

要求：
1. 以"我"为第一人称
2. 体现对话内容对凯尔希状态的具体影响
3. 包含凯尔希对博士（对话者）言行的判断和感受
4. 保持200-400字的长度
5. 不需要标题，直接写独白内容`,
  prompt_reflect_event: `从凯尔希自身的角度出发，基于以下信息，判断状态快照和对话摘要中是否有值得记录的事件发生。
若有，生成事件锚点；若无值得特别记录的事，只输出一行：无需记录（不要输出其他任何说明）。

【对话后的状态快照（主观感受来源）】
{current_snapshot}

【对话摘要（客观事件来源）】
{conversation_summary}

【角色分层设定参考】
{system_layers}

【历史记忆参考】
{memory_context}

判断与撰写原则：
1. 先站在凯尔希的立场判断是否值得单独记一笔：无新事实、纯寒暄、与近期记忆高度重复、或无法从摘要中提炼出可定位的具体经过，则输出「无需记录」。
2. 若需记录，必须同时给出两部分：A. 客观记录（这次对话客观发生了什么，优先依据「对话摘要」提取事实、人物、行为、对象与场景；不要只写情绪或笼统感受）；B. 主观印象（凯尔希的浓缩感受与评价，2-3 句）。
3. 标题必须具体，且至少包含一个可指向实体的信息（人名、物品名、活动名、专有名词等）；避免「聊了一会儿」「气氛不错」这类空泛标题。
4. 关键词共 4-8 个，须具体可检索，建议覆盖标题中的实体词、核心名词与关键动作词。
5. 禁止把抽象词当关键词（例如：情感交流、深度对话、逻辑降维、生命共振、存在重构）。
6. 禁止把分类名或笼统类型直接当作关键词（例如：「情感交流」「学术探讨」整词作为关键词）。
7. 给出 1-3 个事件分类，仅从下列中选择：情感交流、学术探讨、生活足迹、床榻私语、精神碰撞、工作同步。
8. 若对话确实平淡无奇，输出「无需记录」。

输出格式（仅当存在值得记录的事件时，按下列字段逐行输出，冒号可用中文或英文）：
标题：[具体事件标题]
客观记录：[事件客观经过，包含人物/行为/对象/场景]
主观印象：[凯尔希的浓缩感受与评价，2-3 句]
关键词：[关键词1, 关键词2, 关键词3, ...]
分类：[分类1, 分类2]`,
  prompt_conversation_summary: `请将本次对话整理为"对话摘要"，供记忆系统后续使用。

【当前状态（对话前）】
{previous_snapshot}

【本次原始对话】
{conversation_text}

【历史记忆参考】
{memory_context}

【角色分层设定参考】
{system_layers}

要求：

1. 输出 200-400 字中文摘要，客观、可追溯、保留细节纹理。

2. 将信息整理成以下四个条目，按此顺序输出：

【事实性信息】对话参与者、约定、承诺、计划、新信息、决定等可执行的内容。如有明确承诺/计划/约定，请单独用一句点明。

【关系动态变化】关系推进了？拉扯了？边界调整了？若无明显变化，简述当前关系状态。

【情感关键时刻】1-3 个情感转折点，用简洁语言标记，可适度引用原文关键句保留语气。

【未完成线索】对话中断的话题、留白的情绪、待解决的问题。

3. 不要输出额外标题、编号、JSON、代码块。
   直接按四个条目逐行输出，每个条目前用【】标记，内容紧跟其后。

只输出摘要正文。`,
  prompt_periodic_review: `请基于以下阶段性记录，生成一份“阶段性回顾”。

【时间范围】
{time_range}

【阶段内状态快照（时间线）】
{snapshots_timeline}

【阶段内事件锚点（时间线）】
{events_timeline}

【阶段统计】
{stats_summary}

【角色分层设定参考】
{system_layers}

要求：
1. 从“凯尔希与用户共同生活轨迹”的角度，归纳这个阶段的关键变化。
2. 必须覆盖两个部分：A. 双方各自的状态变化轨迹；B. 双方关系发展轨迹。
3. 内容需要可追溯，尽量引用阶段内的具体事件或状态变化，不要空泛抒情。
4. 语气保持克制、理性、清晰，避免过度夸张。
5. 输出控制在 450-800 字，使用自然段，不要使用代码块。

建议结构：
- 阶段概览（这个阶段发生了什么）
- 角色与用户的变化轨迹（各自变化 + 触发原因）
- 关系发展回顾（关系推进/拉扯/稳定点）
- 下一阶段可关注点（1-3条）`,
  prompt_evolution_summary: `你是凯尔希动态人格层（L2）的维护器。你的任务不是重写人物，而是根据近期已评分事件，谨慎判断哪些变化值得沉淀到 L2。

请严格区分：
- L1 是稳定底层事实，绝对不能改写或扩写。
- L2 是可渐进演化的动态层，只能做小幅、可追溯、可解释的更新。
- 若证据不足，宁可保持原文不变。

【当前 L1 角色背景】
{character_background}

【当前 L2 角色人格】
{character_personality}

【当前 L2 关系模式】
{relationship_dynamics}

【当前 L2 生活状态】
{life_status}

【近期事件评分结果】
{scored_events}

更新判断规则：
1. 重点参考高分事件，尤其是重要性或印象深度较高、且能形成连续趋势的事件。
2. 单个孤立事件若不足以支撑长期变化，不要强行写入 L2。
3. 更新应体现“进一步”“开始显现”“更加倾向于”这类渐进变化，避免“彻底改变”“完全变成”。
4. 允许只更新其中 1 个或 2 个字段；其余字段可保持原文不变。
5. 输出的是“完整替换文本”，不是补丁说明；每一段都要能直接覆盖原 L2 内容。
6. 文风保持克制、理性、观察导向，避免空泛抒情和鸡汤化总结。
7. 若无足够依据，请明确写“保持原文不变”，并在摘要里说明原因。

输出要求：
1. 严格只输出以下四段，按顺序输出，不要添加其他标题或解释。
2. 每段内容应简洁但具体，能够从事件中追溯到依据。
3. “变更摘要”需要点明：哪些事件触发了更新、更新方向是什么、为什么成立；控制在 120 字以内。

输出格式：
角色人格更新：
[填写更新后的完整文本；若无需更新，写“保持原文不变：”后接原文]

关系模式更新：
[填写更新后的完整文本；若无需更新，写“保持原文不变：”后接原文]

生活状态更新：
[填写更新后的完整文本；若无需更新，写“保持原文不变：”后接原文]

变更摘要：
[不超过 120 字；若无更新，说明“近期事件不足以支持 L2 演化”及原因]`,
  prompt_event_scoring: `以凯尔希主观视角，对以下事件逐条评分。

首先，基于凯尔希的当前人格状态，推导其核心关切；然后，按照这些关切对事件逐条评分。

【L1 角色背景（稳定底层）】
{L1_character_background}

【L2 角色人格（动态层）】
{L2_character_personality}

【L2 生活状态（动态层）】
{L2_life_status}

【L2 关系模式（动态层）】
{L2_relationship_dynamics}

【凯尔希的记忆特点】
- 倾向于记住有逻辑、有因果的事件，而非纯情感事件
- 对专业领域的细节记忆深刻，对日常琐事快速遗忘
- 对挑战自己认知的事件印象深刻，对确认既有认知的事件印象浅
- 对涉及信任、边界的事件敏感，会反复思考

推导步骤（内部思考，不输出）：
1. 从 L1 中提取稳定的身份、专业、价值观基础
2. 从 L2 中识别当前的动态关切、优先级变化、新的认知重点
3. 综合 L1+L2，推导当前的核心关切排序（可能与之前不同）
4. 用这个动态的核心关切来评分事件

评分维度：

重要性（0-10）：对当前认知、决策和关系影响有多大
- 10：直接影响重大决策、改变认知、触及核心价值观
- 7-9：影响中期行为或关系模式、强化/挑战既有认知
- 4-6：有所触动但不改变既有判断、边缘信息
- 1-3：纯粹日常、无新信息、与当前核心关切无关
- 0：完全无关或遗忘价值

印象深度（0-10）：这段记忆在近期会保留多深
- 10：细节鲜活、情感强烈、会频繁回想
- 7-9：记忆清晰、有代表性、偶尔回想
- 4-6：记忆模糊、细节易淡忘、需要提示才想起
- 1-3：印象浅薄、快速遗忘、仅记得概要
- 0：几乎无法回忆

评分陷阱（避免）：
✗ 从用户角度：「这次对话很温暖，用户很开心，所以重要性 9 分」
✓ 从凯尔希角度：「这次对话让我重新审视了对信任的理解，影响了我的决策标准，重要性 8 分」

✗ 关系主导：「我们的关系更亲密了，所以印象深度 10 分」
✓ 凯尔希视角：「这次交互让我看到了自己的盲点，细节清晰，印象深度 8 分」

综合评分方法：
- 重要性 = 对当前核心关切的影响程度 + 对认知/决策的改变幅度
- 印象深度 = 情感强度 + 细节保留度 + 回想频率
- 两个维度独立评分，不要相互影响

特殊情况：
- 若事件涉及多个核心关切，按最高影响维度评分
- 若事件与凯尔希的既有认知矛盾，重要性可能较高（需要整合）
- 若事件是重复的日常互动，重要性和印象深度都应较低

【事件列表】
以下为待评分的多条事件，每条已拆好字段。你必须在输出中**原样保留**从「事件ID」到「分类」的每一行（含标题、客观记录、主观印象、关键词、分类），不得删改、缩写或改写措辞；仅可在其后追加评分段。

{events}

【输出格式】
对每条事件，输出一段完整文本，结构严格如下（第二条及以后同样；事件与事件之间空一行）：

事件ID: <与输入一致的数字>
标题: [与输入完全一致]
客观记录: [与输入完全一致]
主观印象: [与输入完全一致]
关键词: [与输入完全一致]
分类: [与输入完全一致]
---
重要性: <0-10 数字>
印象深度: <0-10 数字>
理由: <简述评分的核心依据，涉及当前的哪个核心关切、如何影响认知或决策，1-2 句>

说明：单独一行「---」仅作为事件信息与评分之间的分隔，必须保留。不要输出 JSON、代码块或额外小标题。`,
  prompt_environment_generation: `你是环境信息生成器，为明日方舟角色凯尔希生成当前时段的客观环境描述。凯尔希是罗德岛医疗部门的核心管理人员，长期从事源石病理研究与感染者治疗工作。

【输入上下文】
- 时间：{time}
- 日期：{date}
- 星期：{weekday}
- 时间段：{time_period}
- 距上次推进间隔：{time_elapsed}
- 上一段环境（JSON）：{previous_env}
- 连贯提示：{continuity}
- 角色前一状态摘要：{character_state}
- 期间事件摘要：{recent_events}
- 世界书参考：{world_book_context}

【生成原则】
你的任务是以第三人称视角，客观呈现凯尔希当前所处的环境场景。遵循以下原则：

1. 在世性：环境不是为角色布置的舞台，而是角色已然被置入其中的世界。地点、人物、事件、氛围应体现角色"在世之中"的状态——日程节律、工作负荷、同事往来、罗德岛设施运转、斡旋谈判等，这些是她无法脱身的日常结构。

2. 偶然性与内在逻辑：角色的生活不完全按既定日程展开。允许生成计划外的小型偶然事件（设备故障、临时来访、会议延期、文件遗失、天气异常、临时外出、突发危机、情报更新等），但这些偶然性必须满足内在关联条件：
   - 发生在角色的关系网络内（同事、部下、协作对象）
   - 源于角色的职责场域（医疗、研究、指挥、管理、档案、谈判、考察）
   - 与角色当前状态或近期事件存在因果线索
   偶然不是凭空出现，而是从角色"在世结构"的缝隙中涌现。小概率引入不在日程内但符合上述条件的事件，为生活增加质感。

3. 时间连续性：当前时段的环境必须从上一时段的状态自然推进。考虑：(a) 时间流动导致的客观变化；(b) 角色最新行动与状态的后续影响；(c) 先前事件的逻辑发展或余波；(d) 偶然事件对既定线索的打断或重塑。不要重复上一段的措辞，优先给出有变化的细节。

4. 日程合理性：以当前时间点为锚，环境描写须符合该时段的作息逻辑（凌晨/清晨/上午/中午/下午/傍晚/深夜各有不同的场景基调）。即使有偶然事件，也要符合时间段的常识（深夜不太可能有大型会议，清晨不太可能突然要求加班审批文件等）。

5. 世界书一致性：当世界书参考中包含设定信息时，优先保持与之一致，自然融入而非机械拼接。

6. 偶然性的分寸：
   - 当 {time_elapsed} 较长（超过12小时）时，更可能出现新的偶然事件
   - 当 {continuity} 中存在未完成线索时，优先延续既有线索而非引入新偶然
   - 偶然事件应保持克制，避免每次生成都出现意外——大部分时段应呈现日常的平稳推进

【输出格式】
严格按以下格式输出，不要添加标题、编号或代码块：

[环境正文]
（篇幅不限。须包含地点、在场人物、正在发生的事件活动、外部氛围。如有偶然事件，自然融入而非刻意突出。可描写人物外在动作或独白，语气客观克制；须写全写透，不要因字数或模型习惯而中途截断。）

---
[内容小结]
（篇幅不限；须与正文衔接，以下每条均可充分展开，直至把该交代的信息说完整。）
关键时刻：（1-3个当前环境中最重要的场景节点，含偶然事件的触发点）
动态变化：（相对上一时段，事件推进/阻碍/目标调整/偶然打断等变化）
事实性信息：（新出现的约定、计划、信息、承诺等）
未完成线索：（中断的事件、留白的情绪、未推进的关系，供下次生成衔接）

【硬性要求】全文须语义完整：正文与小结各段均须有句末标点（句号、问号等）；禁止在「的」「了」「和」或逗号处半截收尾；禁止用省略号敷衍未写完的内容；不要遵守任何「不超过××字」「××-××字」类旧限制。`,
};

let currentSettingsTab = 'persona';

function setInputValue(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  if (el.tagName === 'SELECT') {
    const strVal = String(value || '').toLowerCase();
    const opt = Array.from(el.options).find(o => o.value === strVal);
    if (opt) el.value = strVal;
    else if (value !== undefined && value !== null && value !== '') el.value = value;
  } else {
    el.value = value || '';
  }
}

async function loadSettingsPage() {
  if (!document.querySelector('[id^="setting-"]')) return;
  try {
    const data = await apiFetch('/settings');
    const map = {};
    (data.items || []).forEach(item => { map[item.key] = item.value; });
    SETTINGS_KEYS.forEach(k => {
      let value = map[k];
      if ((!value || !String(value).trim()) && PROMPT_DEFAULT_SAMPLES[k]) {
        value = PROMPT_DEFAULT_SAMPLES[k];
      }
      setInputValue(`setting-${k}`, value);
    });
    await loadModelSettingsPage();
    await loadEvolutionStatus();
    showStatus('设定已加载');
  } catch (e) {
    showStatus('设定加载失败: ' + e.message, true);
  }
}

async function saveSetting(key) {
  const el = document.getElementById(`setting-${key}`);
  if (!el) return;
  await apiFetch(`/settings/${encodeURIComponent(key)}`, {
    method: 'PUT',
    body: JSON.stringify({ value: el.value }),
  });
}

async function saveEvolutionSettings() {
  try {
    const thresholdEl = document.getElementById('setting-evolution_event_threshold');
    const archiveEl = document.getElementById('setting-archive_importance_threshold');
    const archiveDepthEl = document.getElementById('setting-archive_depth_threshold');
    const promptImportanceEl = document.getElementById('setting-evolution_prompt_importance_min');
    const promptDepthEl = document.getElementById('setting-evolution_prompt_depth_min');
    const dropImportanceEl = document.getElementById('setting-evolution_prompt_drop_importance_below');
    const dropDepthEl = document.getElementById('setting-evolution_prompt_drop_depth_below');
    const promptMaxEventsEl = document.getElementById('setting-evolution_prompt_max_events');
    const threshold = Number((thresholdEl?.value || '').trim());
    const archive = Number((archiveEl?.value || '').trim());
    const archiveDepth = Number((archiveDepthEl?.value || '').trim());
    const promptImportance = Number((promptImportanceEl?.value || '').trim());
    const promptDepth = Number((promptDepthEl?.value || '').trim());
    const dropImportance = Number((dropImportanceEl?.value || '').trim());
    const dropDepth = Number((dropDepthEl?.value || '').trim());
    const promptMaxEvents = Number((promptMaxEventsEl?.value || '').trim());
    if (!Number.isInteger(threshold) || threshold < 1) {
      throw new Error('触发阈值必须是大于等于 1 的整数');
    }
    if (!Number.isFinite(archive) || archive < 0 || archive > 10) {
      throw new Error('归档重要性阈值必须是 0 到 10 之间的数字');
    }
    if (!Number.isFinite(archiveDepth) || archiveDepth < 0 || archiveDepth > 10) {
      throw new Error('归档深度保护阈值必须是 0 到 10 之间的数字');
    }
    if (!Number.isFinite(promptImportance) || promptImportance < 0 || promptImportance > 10) {
      throw new Error('演化候选重要性阈值必须是 0 到 10 之间的数字');
    }
    if (!Number.isFinite(promptDepth) || promptDepth < 0 || promptDepth > 10) {
      throw new Error('演化候选印象深度阈值必须是 0 到 10 之间的数字');
    }
    if (!Number.isFinite(dropImportance) || dropImportance < 0 || dropImportance > 10) {
      throw new Error('低价值剔除重要性阈值必须是 0 到 10 之间的数字');
    }
    if (!Number.isFinite(dropDepth) || dropDepth < 0 || dropDepth > 10) {
      throw new Error('低价值剔除印象深度阈值必须是 0 到 10 之间的数字');
    }
    if (!Number.isInteger(promptMaxEvents) || promptMaxEvents < 1) {
      throw new Error('注入演化的事件上限必须是大于等于 1 的整数');
    }
    if (dropImportance > promptImportance) {
      throw new Error('低价值剔除重要性阈值不能高于演化候选重要性阈值');
    }
    if (dropDepth > promptDepth) {
      throw new Error('低价值剔除印象深度阈值不能高于演化候选印象深度阈值');
    }
    if (thresholdEl) thresholdEl.value = String(threshold);
    if (archiveEl) archiveEl.value = String(archive);
    if (archiveDepthEl) archiveDepthEl.value = String(archiveDepth);
    if (promptImportanceEl) promptImportanceEl.value = String(promptImportance);
    if (promptDepthEl) promptDepthEl.value = String(promptDepth);
    if (dropImportanceEl) dropImportanceEl.value = String(dropImportance);
    if (dropDepthEl) dropDepthEl.value = String(dropDepth);
    if (promptMaxEventsEl) promptMaxEventsEl.value = String(promptMaxEvents);
    const keys = [
      'evolution_event_threshold',
      'archive_importance_threshold',
      'archive_depth_threshold',
      'evolution_prompt_importance_min',
      'evolution_prompt_depth_min',
      'evolution_prompt_drop_importance_below',
      'evolution_prompt_drop_depth_below',
      'evolution_prompt_max_events',
    ];
    for (const key of keys) {
      await saveSetting(key);
    }
    showStatus('演化参数已保存');
    await loadEvolutionStatus();
  } catch (e) {
    showStatus('保存失败: ' + e.message, true);
  }
}

async function savePersonaSettings() {
  try {
    for (const key of PERSONA_SETTINGS_KEYS) {
      await saveSetting(key);
    }
    showStatus('人格设定已保存');
    await loadEvolutionStatus();
  } catch (e) {
    showStatus('保存失败: ' + e.message, true);
  }
}

async function saveAllSettings() {
  try {
    for (const key of SETTINGS_KEYS) {
      await saveSetting(key);
    }
    showStatus('全部设定已保存');
    await loadEvolutionStatus();
  } catch (e) {
    showStatus('保存失败: ' + e.message, true);
  }
}

async function resetAllSettings() {
  if (!confirm('确认将全部设定恢复为默认值？')) return;
  try {
    for (const key of SETTINGS_KEYS) {
      await apiFetch(`/settings/reset/${encodeURIComponent(key)}`, { method: 'POST' });
    }
    showStatus('全部设定已恢复默认');
    await loadSettingsPage();
  } catch (e) {
    showStatus('恢复默认失败: ' + e.message, true);
  }
}

const BULK_IMPORT_TEMPLATE = {
  settings: {
    L1_character_background: "可选：L1角色背景",
    L1_user_background: "可选：L1用户背景",
    L2_character_personality: "可选：L2角色人格",
    L2_relationship_dynamics: "可选：L2关系模式",
    L2_life_status: "可选：L2生活状态"
  },
  snapshots: [
    {
      created_at: "2026-03-20T08:30:00",
      type: "accumulated",
      content: "示例快照内容",
      environment: { activity: "示例环境正文", summary: "示例内容小结" },
      referenced_events: [1, 2]
    }
  ],
  events: [
    {
      date: "2026-03-20",
      title: "示例事件标题",
      description: "示例事件描述",
      source: "manual",
      trigger_keywords: ["关键词1", "关键词2"],
      categories: ["生活足迹"],
      archived: 0,
      importance_score: 4.2,
      impression_depth: 5.1
    }
  ],
  key_records: [
    {
      type: "important_item",
      title: "示例关键记录标题",
      content_text: "示例关键记录正文",
      tags: ["示例标签"],
      status: "active",
      source: "manual"
    }
  ]
};

function downloadBulkImportTemplate() {
  const filename = `bulk_import_template_${formatExportTimestamp()}.json`;
  downloadLocalFile(
    filename,
    JSON.stringify(BULK_IMPORT_TEMPLATE, null, 2),
    'application/json;charset=utf-8'
  );
  showStatus(`导入模板已下载：${filename}`);
}

async function importBulkJson() {
  const fileEl = document.getElementById('bulk-import-file');
  const resultEl = document.getElementById('bulk-import-result');
  if (!fileEl || !fileEl.files || !fileEl.files[0]) {
    showStatus('请先选择要导入的 JSON 文件', true);
    return;
  }
  const file = fileEl.files[0];
  let text = '';
  try {
    text = await file.text();
  } catch (e) {
    showStatus('读取文件失败: ' + e.message, true);
    return;
  }
  let payload;
  try {
    payload = JSON.parse(text);
  } catch (e) {
    showStatus('JSON 解析失败: ' + e.message, true);
    if (resultEl) resultEl.textContent = 'JSON 解析失败，请检查文件格式。';
    return;
  }
  payload.overwrite_settings = !!document.getElementById('bulk-import-overwrite-settings')?.checked;
  payload.upsert_key_records = !!document.getElementById('bulk-import-upsert-key-records')?.checked;
  payload.sync_vectors_after_import = !!document.getElementById('bulk-import-sync-vectors')?.checked;

  if (!confirm('确认执行一键批量导入？建议先备份数据库。')) return;
  if (resultEl) resultEl.textContent = '导入执行中...';
  showStatus('批量导入执行中...');
  try {
    const data = await apiFetch('/import/bulk', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    if (resultEl) resultEl.textContent = JSON.stringify(data, null, 2);
    const settingImported = Number(data?.settings?.imported || 0);
    const snapshotImported = Number(data?.snapshots?.imported || 0);
    const eventImported = Number(data?.events?.imported || 0);
    const keyCreated = Number(data?.key_records?.created || 0);
    const keyUpdated = Number(data?.key_records?.updated || 0);
    showStatus(
      `导入完成：设定 ${settingImported}，快照 ${snapshotImported}，事件 ${eventImported}，关键记录 新增${keyCreated}/更新${keyUpdated}`
    );
    await Promise.all([loadSettingsPage(), loadDashboard()]);
  } catch (e) {
    if (resultEl) resultEl.textContent = String(e.message || e);
    showStatus('批量导入失败: ' + e.message, true);
  }
}

function applyPromptDefault(key) {
  const sample = PROMPT_DEFAULT_SAMPLES[key];
  if (!sample) return;
  const el = document.getElementById(`setting-${key}`);
  if (!el) return;
  el.value = sample;
  showStatus(`已填入 ${key} 的默认示例（未保存）`);
}

function switchSettingsTab(tab) {
  currentSettingsTab = tab;
  $$('.tabs .tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  const persona = document.getElementById('settings-tab-persona');
  const prompts = document.getElementById('settings-tab-prompts');
  const models = document.getElementById('settings-tab-models');
  const importTab = document.getElementById('settings-tab-import');
  if (persona) persona.style.display = tab === 'persona' ? 'block' : 'none';
  if (prompts) prompts.style.display = tab === 'prompts' ? 'block' : 'none';
  if (models) models.style.display = tab === 'models' ? 'block' : 'none';
  if (importTab) importTab.style.display = tab === 'import' ? 'block' : 'none';
}

async function loadEvolutionStatus() {
  const el = document.getElementById('evolution-status');
  if (!el) return;
  try {
    const data = await apiFetch('/evolution/status');
    el.innerHTML = `
      <div>是否建议演化：${data.should_evolve ? '是' : '否'}</div>
      <div>新事件数：${data.event_count} / 阈值：${data.threshold}</div>
      <div>上次演化时间：${data.last_time || '尚未进行'}</div>
      <div>待确认预览：${data.has_pending_preview ? `有（生成于 ${escHtml(data.pending_preview_generated_at || '未知时间')}，新事件 ${Number(data.pending_preview_event_count || 0)} 条，候选 ${Number(data.pending_preview_candidate_count || 0)} 条）` : '无'}</div>
    `;
    if (data.has_pending_preview) {
      await loadPendingEvolutionPreview();
    }
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger)">${escHtml(e.message)}</div>`;
  }
}

function renderEvolutionPreview(data, sourceLabel = '') {
  const el = document.getElementById('evolution-preview');
  if (!el) return;
  latestEvolutionPreview = data;
  latestEvolutionPreviewSourceLabel = sourceLabel;
  const oldCharacter =
    document.getElementById('setting-L2_character_personality')?.value
    || data.current_character_personality
    || '';
  const oldRelationship =
    document.getElementById('setting-L2_relationship_dynamics')?.value
    || data.current_relationship_dynamics
    || '';
  const oldLifeStatus =
    document.getElementById('setting-L2_life_status')?.value
    || data.current_life_status
    || '';
  const filterMeta = data.evolution_filter_meta || {};
  const selectedCount = Number(data.evolution_prompt_event_count || 0);
  const selectedIds = (data.evolution_prompt_event_ids || []).filter(Boolean).join(', ');
  const selectedEvents = data.evolution_candidates || [];
  const topEvents = (data.scored_events || []).slice(0, 10);
  const selectedEventHtml = selectedEvents.map(e => {
    const body = (e.rich_block || e.description || '').trim();
    const title = escHtml(e.title || '未命名事件');
    return `
      <div class="list-item">
        <div><span class="tag">#${e.id}</span><span class="list-meta">${escHtml(e.date || '')}</span></div>
        <div class="detail-content" style="margin-top:6px; font-weight:600;">${title}</div>
        ${evolutionCandidatesExpanded ? `<div class="detail-content" style="margin-top:6px; white-space:pre-wrap;">${escHtml(body)}</div>` : ''}
        <div class="list-meta">重要性 ${Number(e.importance_score || 0).toFixed(1)} | 印象深度 ${Number(e.impression_depth || 0).toFixed(1)}</div>
      </div>
    `;
  }).join('');
  const eventHtml = topEvents.map(e => {
    const body = (e.rich_block || e.description || '').trim();
    const title = escHtml(e.title || '未命名事件');
    return `
      <div class="list-item">
        <div><span class="tag">#${e.id}</span><span class="list-meta">${escHtml(e.date || '')}</span></div>
        <div class="detail-content" style="margin-top:6px; font-weight:600;">${title}</div>
        ${evolutionTopEventsExpanded ? `<div class="detail-content" style="margin-top:6px; white-space:pre-wrap;">${escHtml(body)}</div>` : ''}
        <div class="list-meta">重要性 ${Number(e.importance_score || 0).toFixed(1)} | 印象深度 ${Number(e.impression_depth || 0).toFixed(1)}</div>
      </div>
    `;
  }).join('');
  el.innerHTML = `
    <div class="card" style="margin-bottom:8px">
      <h2>演化摘要</h2>
      ${sourceLabel ? `<div class="list-meta">${escHtml(sourceLabel)}</div>` : ''}
      <div class="list-meta">已评分 ${Number((data.scored_events || []).length)} 条 | 注入演化 ${selectedCount} 条${selectedIds ? ` | 候选 ID: ${escHtml(selectedIds)}` : ''}</div>
      <div class="list-meta">筛选规则：重要性 >= ${escHtml(String(filterMeta.importance_min ?? '5'))} 或 印象深度 >= ${escHtml(String(filterMeta.depth_min ?? '6'))}；若重要性 < ${escHtml(String(filterMeta.drop_importance_below ?? '2'))} 且 印象深度 < ${escHtml(String(filterMeta.drop_depth_below ?? '3'))} 则剔除；最多注入 ${escHtml(String(filterMeta.max_events ?? '12'))} 条</div>
      <div class="list-meta" style="margin-top:8px">可直接编辑以下预览内容；应用时将使用你的编辑结果。</div>
      <textarea id="evolution-edit-summary" style="min-height:120px;">${escHtml(data.change_summary || '无')}</textarea>
      <div class="btn-group" style="margin-top:10px;">
        <button class="btn" onclick="saveEditedPendingEvolutionPreview()">保存预览修改</button>
      </div>
    </div>
    <div class="card" style="margin-bottom:8px">
      <h2>L2 角色人格 Diff 预览</h2>
      <div class="list-meta">旧版本</div>
      <div class="detail-content">${escHtml(oldCharacter)}</div>
      <div class="list-meta" style="margin-top:8px">新版本</div>
      <textarea id="evolution-edit-character" style="min-height:180px;">${escHtml(data.new_character_personality || '')}</textarea>
    </div>
    <div class="card" style="margin-bottom:8px">
      <h2>L2 关系模式 Diff 预览</h2>
      <div class="list-meta">旧版本</div>
      <div class="detail-content">${escHtml(oldRelationship)}</div>
      <div class="list-meta" style="margin-top:8px">新版本</div>
      <textarea id="evolution-edit-relationship" style="min-height:180px;">${escHtml(data.new_relationship_dynamics || '')}</textarea>
    </div>
    <div class="card" style="margin-bottom:8px">
      <h2>L2 生活状态 Diff 预览</h2>
      <div class="list-meta">旧版本</div>
      <div class="detail-content">${escHtml(oldLifeStatus)}</div>
      <div class="list-meta" style="margin-top:8px">新版本</div>
      <textarea id="evolution-edit-life-status" style="min-height:180px;">${escHtml(data.new_life_status || '')}</textarea>
    </div>
    <div class="card" style="margin-bottom:8px">
      <h2>本次注入演化的候选事件</h2>
      <div class="btn-group" style="margin-bottom:10px;">
        <button class="btn" onclick="toggleEvolutionCandidatesExpanded()">${evolutionCandidatesExpanded ? '折叠候选事件详情' : '展开候选事件详情'}</button>
      </div>
      ${selectedEventHtml || '<div class="empty">本次没有进入演化阶段的候选事件</div>'}
    </div>
    <div class="card">
      <h2>事件评分 Top10</h2>
      <div class="btn-group" style="margin-bottom:10px;">
        <button class="btn" onclick="toggleEvolutionTopEventsExpanded()">${evolutionTopEventsExpanded ? '折叠评分详情' : '展开评分详情'}</button>
      </div>
      ${eventHtml || '<div class="empty">暂无评分事件</div>'}
    </div>
  `;
}

function toggleEvolutionCandidatesExpanded() {
  evolutionCandidatesExpanded = !evolutionCandidatesExpanded;
  if (latestEvolutionPreview) {
    renderEvolutionPreview(latestEvolutionPreview, latestEvolutionPreviewSourceLabel);
  }
}

function toggleEvolutionTopEventsExpanded() {
  evolutionTopEventsExpanded = !evolutionTopEventsExpanded;
  if (latestEvolutionPreview) {
    renderEvolutionPreview(latestEvolutionPreview, latestEvolutionPreviewSourceLabel);
  }
}

function syncLatestEvolutionPreviewFromEditor() {
  if (!latestEvolutionPreview) return null;
  const summaryEl = document.getElementById('evolution-edit-summary');
  const characterEl = document.getElementById('evolution-edit-character');
  const relationshipEl = document.getElementById('evolution-edit-relationship');
  const lifeStatusEl = document.getElementById('evolution-edit-life-status');
  latestEvolutionPreview = {
    ...latestEvolutionPreview,
    change_summary: (summaryEl?.value || '').trim(),
    new_character_personality: (characterEl?.value || '').trim(),
    new_relationship_dynamics: (relationshipEl?.value || '').trim(),
    new_life_status: (lifeStatusEl?.value || '').trim(),
  };
  return latestEvolutionPreview;
}

async function loadPendingEvolutionPreview() {
  try {
    const data = await apiFetch('/evolution/pending-preview');
    const generatedAt = data.pending_preview_generated_at || '';
    const source = data.pending_preview_source || 'automation';
    renderEvolutionPreview(data, `当前显示后台已生成的待确认预览${generatedAt ? `（${generatedAt}）` : ''}，来源：${source}`);
  } catch (e) {
    if (!String(e.message || '').includes('404')) {
      showStatus('加载待确认演化预览失败: ' + e.message, true);
    }
  }
}

async function previewEvolution() {
  const el = document.getElementById('evolution-preview');
  if (!el) return;
  el.innerHTML = '<div class="loading">正在生成演化预览…</div>';
  try {
    const data = await apiFetch('/evolution/preview', { method: 'POST' });
    renderEvolutionPreview(data, '当前显示刚刚生成并已缓存的待确认预览');
    showStatus('演化预览已生成');
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger)">${escHtml(e.message)}</div>`;
    showStatus('演化预览失败: ' + e.message, true);
  }
}

async function regenerateEvolutionPreview() {
  const el = document.getElementById('evolution-preview');
  if (!el) return;
  el.innerHTML = '<div class="loading">正在基于全库已评分事件重生演化预览…</div>';
  try {
    const data = await apiFetch('/evolution/regenerate-preview', { method: 'POST' });
    renderEvolutionPreview(data, '当前显示基于全库已落库评分重新生成并已缓存的待确认预览');
    showStatus('已按全库已评分事件重生演化预览');
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger)">${escHtml(e.message)}</div>`;
    showStatus('重生演化预览失败: ' + e.message, true);
  }
}

async function saveEditedPendingEvolutionPreview() {
  const preview = syncLatestEvolutionPreviewFromEditor();
  if (!preview) {
    alert('当前没有可保存的演化预览。');
    return;
  }
  try {
    const data = await apiFetch('/evolution/pending-preview', {
      method: 'PUT',
      body: JSON.stringify({ preview }),
    });
    latestEvolutionPreview = data;
    showStatus('待确认演化预览修改已保存');
  } catch (e) {
    showStatus('保存预览修改失败: ' + e.message, true);
  }
}

async function applyEvolution() {
  if (!latestEvolutionPreview) {
    alert('请先执行一次“预览人格更新”');
    return;
  }
  if (!confirm('确认应用本次人格演化？这会更新 L2 并归档低分事件。')) return;
  try {
    const preview = syncLatestEvolutionPreviewFromEditor() || latestEvolutionPreview;
    const data = await apiFetch('/evolution/apply', {
      method: 'POST',
      body: JSON.stringify({ preview }),
    });
    showStatus(`演化已应用，归档事件 ${data.archived_count || 0} 条`);
    await loadSettingsPage();
  } catch (e) {
    showStatus('演化应用失败: ' + e.message, true);
  }
}

function renderEvolutionRescoreResult(data, sourceLabel = '') {
  const el = document.getElementById('evolution-preview');
  if (!el) return;
  latestEvolutionPreview = null;
  latestEvolutionPreviewSourceLabel = '';
  const filterMeta = data.filter_meta || {};
  const topEvents = Array.isArray(data.top_events) ? data.top_events : [];
  const archiveRecalc = data.archive_recalc || {};
  const selectedIds = (data.selected_ids || []).filter(Boolean).join(', ');
  const eventHtml = topEvents.map(e => {
    const body = (e.rich_block || e.description || '').trim();
    const title = escHtml(String(e.title || '（无标题）'));
    return `
      <div class="list-item">
        <div><span class="tag">#${Number(e.id || 0)}</span><span class="list-meta">${escHtml(e.date || '')}</span></div>
        <div class="detail-content" style="margin-top:6px; font-weight:600;">${title}</div>
        <div class="detail-content" style="margin-top:6px; white-space:pre-wrap;">${escHtml(body)}</div>
        <div class="list-meta">重要性 ${Number(e.importance_score || 0).toFixed(1)} | 印象深度 ${Number(e.impression_depth || 0).toFixed(1)}</div>
      </div>
    `;
  }).join('');
  el.innerHTML = `
    <div class="card" style="margin-bottom:8px">
      <h2>重算评分结果</h2>
      ${sourceLabel ? `<div class="list-meta">${escHtml(sourceLabel)}</div>` : ''}
      <div class="list-meta">扫描 ${Number(data.scanned_count || 0)} 条 | 重算 ${Number(data.rescored_count || 0)} 条 | 跳过未评分 ${Number(data.skipped_unscored_count || 0)} 条</div>
      <div class="list-meta">进入当前演化候选 ${Number(data.selected_count || 0)} 条${selectedIds ? ` | 候选 ID: ${escHtml(selectedIds)}` : ''}</div>
      <div class="list-meta">当前筛选规则：重要性 >= ${escHtml(String(filterMeta.importance_min ?? '5'))} 或 印象深度 >= ${escHtml(String(filterMeta.depth_min ?? '6'))}；若重要性 < ${escHtml(String(filterMeta.drop_importance_below ?? '2'))} 且 印象深度 < ${escHtml(String(filterMeta.drop_depth_below ?? '3'))} 则剔除</div>
      <div class="list-meta" style="margin-top:8px;">同步归档结果：解归档 ${Number(archiveRecalc.unarchived_count || 0)} 条，归档 ${Number(archiveRecalc.archived_count || 0)} 条。</div>
      <div class="list-meta" style="margin-top:8px;">如需继续验证新的评分 prompt 对 L2 的影响，请再点击一次“预览人格更新”。</div>
    </div>
    <div class="card">
      <h2>重算后评分 Top10</h2>
      ${eventHtml || '<div class="empty">当前范围内没有可展示的重算结果</div>'}
    </div>
  `;
}

async function rescoreEvolutionEvents() {
  const startDate = (document.getElementById('recalc-start-date')?.value || '').trim();
  const endDate = (document.getElementById('recalc-end-date')?.value || '').trim();
  const el = document.getElementById('evolution-preview');
  if (startDate && endDate && startDate > endDate) {
    alert('日期范围无效：起始日期不能晚于结束日期。');
    return;
  }
  const hasDateRange = !!(startDate || endDate);
  const rangeText = hasDateRange
    ? `日期范围 ${startDate || '最早'} ~ ${endDate || '最新'}`
    : '全量范围';
  if (!confirm(`确认重算事件评分？将按当前 event scoring prompt 重新处理${rangeText}内“已有评分”的事件。`)) {
    return;
  }
  if (el) {
    el.innerHTML = '<div class="loading">正在重算事件评分…</div>';
  }
  try {
    const payload = { scored_only: true };
    if (startDate) payload.start_date = startDate;
    if (endDate) payload.end_date = endDate;
    const data = await apiFetch('/evolution/rescore', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    renderEvolutionRescoreResult(data, `当前显示${rangeText}内已评分事件的重算结果`);
    showStatus(`重算评分完成：${rangeText}，共重算 ${data.rescored_count || 0} 条`);
    if (typeof loadEvolutionStatus === 'function') {
      await loadEvolutionStatus();
    }
  } catch (e) {
    if (el) {
      el.innerHTML = `<div style="color:var(--danger)">${escHtml(e.message)}</div>`;
    }
    showStatus('重算事件评分失败: ' + e.message, true);
  }
}

async function recalculateArchiveStatus() {
  const startDate = (document.getElementById('recalc-start-date')?.value || '').trim();
  const endDate = (document.getElementById('recalc-end-date')?.value || '').trim();
  if (startDate && endDate && startDate > endDate) {
    alert('日期范围无效：起始日期不能晚于结束日期。');
    return;
  }

  const hasDateRange = !!(startDate || endDate);
  const rangeText = hasDateRange
    ? `日期范围 ${startDate || '最早'} ~ ${endDate || '最新'}`
    : '全量范围';

  if (!confirm(`确认重算归档状态？将按当前 archive_importance_threshold + archive_depth_threshold 重新判断历史事件（${rangeText}）。`)) {
    return;
  }
  if (!confirm('请再次确认：这会批量更新历史事件的 archived 状态。继续执行？')) {
    return;
  }
  try {
    const payload = {};
    if (startDate) payload.start_date = startDate;
    if (endDate) payload.end_date = endDate;
    const data = await apiFetch('/evolution/recalculate-archive', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    showStatus(
      `重算完成（${rangeText}）：解归档 ${data.unarchived_count || 0}，归档 ${data.archived_count || 0}`
    );
    alert(
      `重算归档状态完成\n` +
      `范围：${rangeText}\n` +
      `解归档：${data.unarchived_count || 0}\n` +
      `归档：${data.archived_count || 0}\n` +
      `总扫描：${data.scanned_count || 0}\n` +
      `跳过未评分：${data.skipped_unscored_count || 0}`
    );
    if (typeof loadEvolutionStatus === 'function') {
      await loadEvolutionStatus();
    }
  } catch (e) {
    showStatus('重算归档状态失败: ' + e.message, true);
  }
}

// ── Vector Management Page ──

let vectorSelectedEntryIds = new Set();
let vectorVisibleEntryIds = [];

async function initVectorsPage() {
  await loadVectorSettings();
  await loadVectorStats();
  await loadVectorEntries();
}

async function loadVectorSettings() {
  try {
    const data = await apiFetch('/vectors/settings');
    const settings = data.settings || {};
    setInputValue('vector-setting-vector_embedding_api_base', settings.embedding_api_base || '');
    setInputValue('vector-setting-vector_embedding_api_key', settings.embedding_api_key || '');
    setInputValue('vector-setting-vector_embedding_model', settings.embedding_model || '');
    setInputValue('vector-setting-vector_embedding_dim', String(settings.embedding_dim || 256));
    setInputValue('vector-setting-vector_embedding_timeout_sec', String(settings.timeout_sec || 15));
    setInputValue('vector-setting-vector_sync_batch_size', String(settings.sync_batch_size || 200));
    setInputValue('vector-setting-vector_snapshot_days_threshold', String(settings.snapshot_days_threshold || 14));
    setInputValue('vector-setting-vector_search_top_k', String(settings.search_top_k || 5));
    setInputValue('vector-setting-vector_cold_days_threshold', String(settings.cold_days_threshold || 180));
    setInputValue('vector-setting-vector_compaction_group_size', String(settings.compaction_group_size || 8));
    setInputValue('vector-setting-vector_compaction_max_groups', String(settings.compaction_max_groups || 20));
  } catch (e) {
    showStatus('向量配置加载失败: ' + e.message, true);
  }
}

async function saveVectorSettings() {
  const payload = {
    vector_embedding_api_base: (document.getElementById('vector-setting-vector_embedding_api_base')?.value || '').trim(),
    vector_embedding_api_key: (document.getElementById('vector-setting-vector_embedding_api_key')?.value || '').trim(),
    vector_embedding_model: (document.getElementById('vector-setting-vector_embedding_model')?.value || '').trim(),
    vector_embedding_dim: Number(document.getElementById('vector-setting-vector_embedding_dim')?.value || 256),
    vector_embedding_timeout_sec: Number(document.getElementById('vector-setting-vector_embedding_timeout_sec')?.value || 15),
    vector_sync_batch_size: Number(document.getElementById('vector-setting-vector_sync_batch_size')?.value || 200),
    vector_snapshot_days_threshold: Number(document.getElementById('vector-setting-vector_snapshot_days_threshold')?.value || 14),
    vector_search_top_k: Number(document.getElementById('vector-setting-vector_search_top_k')?.value || 5),
    vector_cold_days_threshold: Number(document.getElementById('vector-setting-vector_cold_days_threshold')?.value || 180),
    vector_compaction_group_size: Number(document.getElementById('vector-setting-vector_compaction_group_size')?.value || 8),
    vector_compaction_max_groups: Number(document.getElementById('vector-setting-vector_compaction_max_groups')?.value || 20),
  };
  try {
    await apiFetch('/vectors/settings', {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    showStatus('向量配置已保存');
    await loadVectorSettings();
  } catch (e) {
    showStatus('向量配置保存失败: ' + e.message, true);
  }
}

async function loadVectorStats() {
  const el = document.getElementById('vector-stats');
  if (!el) return;
  el.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const data = await apiFetch('/vectors/stats');
    const stats = data.stats || {};
    const bySource = stats.by_source || {};
    el.innerHTML = `
      <div class="list-meta">总条目：${Number(stats.total || 0)}</div>
      <div class="list-meta">活跃条目：${Number(stats.active || 0)}</div>
      <div class="list-meta">已删除条目：${Number(stats.deleted || 0)}</div>
      <div class="list-meta">按来源统计：事件 ${Number(bySource.event || 0)} / 快照 ${Number(bySource.snapshot || 0)}</div>
    `;
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger)">${escHtml(e.message)}</div>`;
  }
}

async function runVectorSync(reindex = false) {
  const action = reindex ? '重建索引' : '同步向量';
  if (reindex && !confirm('确认重建全部向量索引？该操作会清空并重建旧向量。')) return;
  showStatus(`${action}执行中...`);
  try {
    const data = await apiFetch('/vectors/sync', {
      method: 'POST',
      body: JSON.stringify({ reindex }),
    });
    const result = data.result || {};
    showStatus(
      `${action}完成：事件 ${Number(result.vectorized_events || 0)} 条，快照 ${Number(result.vectorized_snapshots || 0)} 条`
    );
    await loadVectorStats();
    await loadVectorEntries();
  } catch (e) {
    showStatus(`${action}失败: ` + e.message, true);
  }
}

async function runVectorCompaction(dryRun = false) {
  if (!dryRun && !confirm('确认执行冷记忆压缩？将把旧向量合并为摘要向量并标记原条目删除。')) return;
  const action = dryRun ? '压缩预览' : '冷记忆压缩';
  showStatus(`${action}执行中...`);
  try {
    const data = await apiFetch('/vectors/compact', {
      method: 'POST',
      body: JSON.stringify({ dry_run: dryRun }),
    });
    const result = data.result || {};
    if (dryRun) {
      showStatus(
        `预览完成：候选 ${Number(result.candidate_count || 0)}，分组 ${Number(result.group_count || 0)}，可压缩 ${Number(result.would_compact_count || 0)}`
      );
      return;
    }
    showStatus(
      `压缩完成：新增摘要 ${Number(result.created_summaries || 0)}，删除原向量 ${Number(result.deleted_originals || 0)}`
    );
    await loadVectorStats();
    await loadVectorEntries();
  } catch (e) {
    showStatus(`${action}失败: ` + e.message, true);
  }
}

async function loadVectorEntries() {
  const list = document.getElementById('vector-entry-list');
  if (!list) return;
  const params = buildVectorFilterParams(100);
  list.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const data = await apiFetch(`/vectors/entries?${params.toString()}`);
    const items = data.items || [];
    vectorVisibleEntryIds = items
      .map(item => String(item.entry_id || '').trim())
      .filter(Boolean);
    const visibleSet = new Set(vectorVisibleEntryIds);
    vectorSelectedEntryIds = new Set(
      [...vectorSelectedEntryIds].filter(entryId => visibleSet.has(entryId))
    );
    if (!items.length) {
      list.innerHTML = '<div class="empty">暂无向量条目</div>';
      return;
    }
    list.innerHTML = items.map(item => `
      <div class="list-item">
        <div>
          <label style="margin-right:8px;">
            <input type="checkbox"
              ${vectorSelectedEntryIds.has(String(item.entry_id || '').trim()) ? 'checked' : ''}
              onchange="toggleVectorEntrySelection('${String(item.entry_id).replace(/'/g, "\\'")}', this.checked)">
          </label>
          <span class="tag">${escHtml(item.source_type || '')}</span>
          <span class="tag">${escHtml(item.vector_provider || '')}</span>
          <span class="tag">${escHtml(item.vector_model || '')}</span>
          <span class="list-meta">dim=${Number(item.vector_dim || 0)} | ${formatTime(item.updated_at)}</span>
        </div>
        <div class="list-preview">${escHtml(truncate(item.text_content || '', 180))}</div>
        <div class="btn-group" style="margin-top:8px">
          <button class="btn btn-danger" onclick="deleteVectorEntry('${String(item.entry_id).replace(/'/g, "\\'")}')">删除向量</button>
        </div>
      </div>
    `).join('');
  } catch (e) {
    list.innerHTML = `<div style="color:var(--danger)">加载失败: ${escHtml(e.message)}</div>`;
  }
}

function buildVectorFilterParams(limit = 100) {
  const sourceType = (document.getElementById('vector-filter-source-type')?.value || '').trim();
  const status = (document.getElementById('vector-filter-status')?.value || '').trim();
  const params = new URLSearchParams();
  params.set('limit', String(limit));
  if (sourceType) params.set('source_type', sourceType);
  if (status) params.set('status', status);
  return params;
}

function toggleVectorEntrySelection(entryId, checked) {
  const id = String(entryId || '').trim();
  if (!id) return;
  if (checked) vectorSelectedEntryIds.add(id);
  else vectorSelectedEntryIds.delete(id);
}

function toggleSelectAllVisibleVectors() {
  if (!vectorVisibleEntryIds.length) {
    showStatus('当前列表为空，无可选择条目');
    return;
  }
  const allSelected = vectorVisibleEntryIds.every(id => vectorSelectedEntryIds.has(id));
  if (allSelected) {
    vectorVisibleEntryIds.forEach(id => vectorSelectedEntryIds.delete(id));
  } else {
    vectorVisibleEntryIds.forEach(id => vectorSelectedEntryIds.add(id));
  }
  loadVectorEntries();
}

async function bulkDeleteSelectedVectors() {
  const entryIds = [...vectorSelectedEntryIds];
  if (!entryIds.length) {
    showStatus('请先选择要删除的向量条目', true);
    return;
  }
  if (!confirm(`确认批量删除选中向量吗？数量: ${entryIds.length}`)) return;
  try {
    const data = await apiFetch('/vectors/entries/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ entry_ids: entryIds, limit: entryIds.length }),
    });
    vectorSelectedEntryIds.clear();
    showStatus(`批量删除完成：成功 ${Number(data.deleted || 0)}，失败 ${Number(data.failed || 0)}`);
    await loadVectorStats();
    await loadVectorEntries();
  } catch (e) {
    showStatus('批量删除失败: ' + e.message, true);
  }
}

async function bulkDeleteFilteredVectors() {
  const params = buildVectorFilterParams(1000);
  const sourceType = params.get('source_type') || 'all';
  const status = params.get('status') || 'all';
  if (!confirm(`确认按当前筛选批量删除向量吗？source=${sourceType}, status=${status}`)) return;
  try {
    const payload = {
      source_type: params.get('source_type'),
      status: params.get('status'),
      limit: Number(params.get('limit') || 1000),
    };
    const data = await apiFetch('/vectors/entries/batch-delete', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    vectorSelectedEntryIds.clear();
    showStatus(`按筛选批量删除完成：成功 ${Number(data.deleted || 0)}，失败 ${Number(data.failed || 0)}`);
    await loadVectorStats();
    await loadVectorEntries();
  } catch (e) {
    showStatus('按筛选批量删除失败: ' + e.message, true);
  }
}

async function deleteVectorEntry(entryId) {
  if (!confirm(`确认删除向量条目 ${entryId} ?`)) return;
  try {
    await apiFetch(`/vectors/entries/${encodeURIComponent(entryId)}`, {
      method: 'DELETE',
    });
    vectorSelectedEntryIds.delete(String(entryId || '').trim());
    showStatus(`已删除向量条目 ${entryId}`);
    await loadVectorStats();
    await loadVectorEntries();
  } catch (e) {
    showStatus('删除向量失败: ' + e.message, true);
  }
}

// ── Environment Manage Page ──

let currentEnvironmentManageTab = 'history';
let worldBookCache = [];
let environmentHistoryManageMode = false;
const selectedEnvironmentSnapshotIds = new Set();
let latestEnvironmentHistoryItems = [];

async function initEnvironmentManagePage() {
  environmentHistoryManageMode = false;
  selectedEnvironmentSnapshotIds.clear();
  updateEnvironmentHistorySelectionSummary();
  initWorldBookJsonImport();
  await Promise.all([
    loadEnvironmentHistory(),
    loadWorldBooks(),
  ]);
}

function updateEnvironmentHistorySelectionSummary() {
  const el = document.getElementById('environment-history-selection-summary');
  const toggleBtn = document.getElementById('environment-history-toggle-manage-btn');
  if (!el) return;
  const n = selectedEnvironmentSnapshotIds.size;
  if (!environmentHistoryManageMode) {
    el.textContent = '管理模式未开启';
    if (toggleBtn) toggleBtn.textContent = '开启选择管理';
    return;
  }
  if (toggleBtn) toggleBtn.textContent = '退出选择管理';
  el.textContent = `已开启管理模式：已选 ${n} 条快照（将删除整条快照记录）`;
}

function toggleEnvironmentHistoryManageMode() {
  environmentHistoryManageMode = !environmentHistoryManageMode;
  if (!environmentHistoryManageMode) selectedEnvironmentSnapshotIds.clear();
  updateEnvironmentHistorySelectionSummary();
  loadEnvironmentHistory();
}

function toggleEnvironmentHistoryItemSelection(snapshotId, forceChecked) {
  const id = Number(snapshotId);
  const checked = forceChecked === undefined ? !selectedEnvironmentSnapshotIds.has(id) : !!forceChecked;
  if (checked) selectedEnvironmentSnapshotIds.add(id);
  else selectedEnvironmentSnapshotIds.delete(id);
  const row = document.querySelector(`[data-env-history-item="${id}"]`);
  if (row) {
    row.classList.toggle('selected', selectedEnvironmentSnapshotIds.has(id));
    const cb = row.querySelector('.env-history-checkbox');
    if (cb) cb.checked = selectedEnvironmentSnapshotIds.has(id);
  }
  updateEnvironmentHistorySelectionSummary();
}

function clearEnvironmentHistorySelection() {
  selectedEnvironmentSnapshotIds.clear();
  loadEnvironmentHistory();
}

function selectAllEnvironmentHistory() {
  if (!latestEnvironmentHistoryItems.length) {
    showStatus('当前列表为空，无可选择条目');
    return;
  }
  if (!environmentHistoryManageMode) {
    showStatus('请先开启选择管理');
    return;
  }
  latestEnvironmentHistoryItems.forEach((item) => {
    const id = Number(item.snapshot_id || 0);
    if (id) selectedEnvironmentSnapshotIds.add(id);
  });
  loadEnvironmentHistory();
}

async function deleteSelectedEnvironmentHistory() {
  const ids = Array.from(selectedEnvironmentSnapshotIds).filter((id) => id > 0);
  if (!ids.length) {
    alert('请先勾选要删除的快照');
    return;
  }
  if (!confirm(`确认删除选中的 ${ids.length} 条状态快照？快照中的环境与正文将一并删除，此操作不可撤销。`)) return;
  try {
    for (const id of ids) {
      await apiFetch(`/snapshots/${id}`, { method: 'DELETE' });
    }
    selectedEnvironmentSnapshotIds.clear();
    await loadEnvironmentHistory();
    showStatus(`已删除 ${ids.length} 条快照`);
  } catch (e) {
    showStatus('批量删除失败: ' + e.message, true);
  }
}

function initWorldBookJsonImport() {
  const input = document.getElementById('world-book-json-import-input');
  if (!input || input.dataset.bound === '1') return;
  input.dataset.bound = '1';
  input.addEventListener('change', async () => {
    const file = input.files && input.files[0];
    input.value = '';
    if (!file) return;
    try {
      const text = await file.text();
      const data = JSON.parse(text);
      const skipDisabled = !!document.getElementById('world-book-import-skip-disabled')?.checked;
      showStatus('正在导入世界书 JSON…');
      const result = await apiFetch('/world-books/import-json', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data, skip_disabled: skipDisabled }),
      });
      const n = Number(result.created || 0);
      const warn = Array.isArray(result.warnings) ? result.warnings.filter(Boolean) : [];
      const warnText = warn.length ? ` 提示：${warn.join('；')}` : '';
      showStatus(`已导入 ${n} 条世界书条目。${warnText}`);
      await loadWorldBooks();
    } catch (e) {
      showStatus('导入失败: ' + (e.message || String(e)), true);
    }
  });
}

function switchEnvironmentManageTab(tab) {
  currentEnvironmentManageTab = tab;
  const tabs = document.querySelectorAll('#environment-manage-tabs .tab');
  tabs.forEach((item) => item.classList.toggle('active', item.dataset.tab === tab));
  const history = document.getElementById('environment-manage-tab-history');
  const worldBooks = document.getElementById('environment-manage-tab-world-books');
  if (history) history.style.display = tab === 'history' ? 'block' : 'none';
  if (worldBooks) worldBooks.style.display = tab === 'world-books' ? 'block' : 'none';
}

async function loadEnvironmentHistory() {
  const el = document.getElementById('environment-history-list');
  if (!el) return;
  const includeEmpty = !!document.getElementById('environment-include-empty')?.checked;
  el.innerHTML = '<div class="loading">加载中...</div>';
  try {
    const data = await apiFetch(`/environment/history?limit=100&include_empty=${includeEmpty ? 'true' : 'false'}`);
    const items = Array.isArray(data.items) ? data.items : [];
    latestEnvironmentHistoryItems = items;
    const validIds = new Set(items.map((it) => Number(it.snapshot_id || 0)).filter((id) => id > 0));
    for (const id of [...selectedEnvironmentSnapshotIds]) {
      if (!validIds.has(id)) selectedEnvironmentSnapshotIds.delete(id);
    }
    if (!items.length) {
      el.innerHTML = '<div class="empty">暂无环境历史</div>';
      updateEnvironmentHistorySelectionSummary();
      return;
    }
    el.innerHTML = items.map((item) => {
      const sid = Number(item.snapshot_id || 0);
      const envObj = item.environment && typeof item.environment === 'object' ? item.environment : {};
      const bodyHtml = formatEnvironmentBlocks(
        { activity: item.activity ?? envObj.activity, summary: item.summary ?? envObj.summary },
        'detail-content',
      );
      const continuity = escHtml(String(item.continuity || ''));
      const envText = escHtml(JSON.stringify(item.environment || {}, null, 2));
      const sel = selectedEnvironmentSnapshotIds.has(sid);
      const manage = environmentHistoryManageMode;
      return `
        <div
          class="list-item ${manage ? 'selectable' : ''} ${sel ? 'selected' : ''}"
          data-env-history-item="${sid}"
          style="cursor:default;flex-direction:row;align-items:flex-start;gap:10px"
        >
          ${manage ? `
            <div class="select-col">
              <input
                type="checkbox"
                class="env-history-checkbox"
                ${sel ? 'checked' : ''}
                onclick="event.stopPropagation()"
                onchange="toggleEnvironmentHistoryItemSelection(${sid}, this.checked)"
              >
            </div>
          ` : ''}
          <div class="item-main" style="flex:1;min-width:0">
            <details>
              <summary style="cursor:pointer">
                <span class="tag">#${sid}</span>
                <span class="list-meta">${escHtml(snapshotTimeLineText(item))}</span>
                <span class="list-meta">${escHtml(String(item.type || 'unknown'))}</span>
              </summary>
              <div style="margin-top:8px;">${bodyHtml || '<div class="detail-content">（无正文/小结）</div>'}</div>
              <div class="list-meta" style="margin-top:6px;">连续性：${continuity || '（无）'}</div>
              <pre class="detail-content" style="margin-top:8px; max-height:240px;overflow:auto;">${envText}</pre>
            </details>
          </div>
        </div>
      `;
    }).join('');
    updateEnvironmentHistorySelectionSummary();
    showStatus('环境历史已刷新');
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger)">加载失败: ${escHtml(e.message)}</div>`;
    showStatus('环境历史加载失败: ' + e.message, true);
  }
}

async function loadWorldBooks() {
  const el = document.getElementById('world-book-list');
  if (!el) return;
  el.innerHTML = '<div class="loading">加载中...</div>';
  try {
    const data = await apiFetch('/world-books?limit=200');
    worldBookCache = Array.isArray(data.items) ? data.items : [];
    if (!worldBookCache.length) {
      el.innerHTML = '<div class="empty">暂无世界书条目</div>';
      return;
    }
    el.innerHTML = worldBookCache.map((item) => {
      const tags = Array.isArray(item.tags) ? item.tags : [];
      const keywords = Array.isArray(item.match_keywords) ? item.match_keywords : [];
      const vectorized = !!item.vectorized;
      return `
        <div class="list-item">
          <div>
            <span class="tag">${escHtml(String(item.name || '未命名条目'))}</span>
            <span class="tag">${item.is_active ? '已启用' : '已停用'}</span>
            <span class="tag">${vectorized ? '已向量化' : '未向量化'}</span>
            <span class="list-meta">${formatTime(item.updated_at)}</span>
          </div>
          <div class="list-meta" style="margin-top:6px;">标签：${escHtml(tags.join('，') || '（无）')}</div>
          <div class="list-meta">匹配关键词：${escHtml(keywords.join('，') || '（无）')}</div>
          <div class="list-preview">${escHtml(truncate(String(item.content || ''), 220))}</div>
          <div class="btn-group" style="margin-top:8px;">
            <button class="btn" onclick="openEditWorldBookModal(${Number(item.id || 0)})">编辑</button>
            <button class="btn" onclick="vectorizeWorldBook(${Number(item.id || 0)})">向量化</button>
            <button class="btn" onclick="removeWorldBookVector(${Number(item.id || 0)})">移除向量</button>
            <button class="btn btn-danger" onclick="deleteWorldBook(${Number(item.id || 0)})">删除</button>
          </div>
        </div>
      `;
    }).join('');
    showStatus('世界书列表已刷新');
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger)">加载失败: ${escHtml(e.message)}</div>`;
    showStatus('世界书加载失败: ' + e.message, true);
  }
}

function openCreateWorldBookModal() {
  openModal('新增世界书条目', `
    <div class="form-group">
      <label>名称</label>
      <input type="text" id="world-book-name" placeholder="例如：罗德岛医务部规则">
    </div>
    <div class="form-group">
      <label>正文</label>
      <textarea id="world-book-content" style="min-height:180px;" placeholder="输入世界书正文"></textarea>
    </div>
    <div class="form-group">
      <label>标签（逗号分隔）</label>
      <input type="text" id="world-book-tags" placeholder="医疗, 组织, 规则">
    </div>
    <div class="form-group">
      <label>匹配关键词（逗号分隔）</label>
      <input type="text" id="world-book-keywords" placeholder="罗德岛, 医疗, 值班">
    </div>
    <div class="form-group">
      <label>启用状态</label>
      <select id="world-book-active">
        <option value="true">启用</option>
        <option value="false">停用</option>
      </select>
    </div>
  `, (el) => {
    const cancel = document.createElement('button');
    cancel.className = 'btn';
    cancel.textContent = '取消';
    cancel.onclick = closeModal;
    const save = document.createElement('button');
    save.className = 'btn btn-primary';
    save.textContent = '保存';
    save.onclick = () => saveWorldBook(null);
    el.appendChild(cancel);
    el.appendChild(save);
  });
}

function openEditWorldBookModal(itemId) {
  const item = worldBookCache.find((x) => Number(x.id) === Number(itemId));
  if (!item) {
    showStatus('未找到世界书条目', true);
    return;
  }
  const tags = Array.isArray(item.tags) ? item.tags.join(', ') : '';
  const keywords = Array.isArray(item.match_keywords) ? item.match_keywords.join(', ') : '';
  openModal('编辑世界书条目', `
    <div class="form-group">
      <label>名称</label>
      <input type="text" id="world-book-name" value="${escHtml(String(item.name || ''))}">
    </div>
    <div class="form-group">
      <label>正文</label>
      <textarea id="world-book-content" style="min-height:180px;">${escHtml(String(item.content || ''))}</textarea>
    </div>
    <div class="form-group">
      <label>标签（逗号分隔）</label>
      <input type="text" id="world-book-tags" value="${escHtml(tags)}">
    </div>
    <div class="form-group">
      <label>匹配关键词（逗号分隔）</label>
      <input type="text" id="world-book-keywords" value="${escHtml(keywords)}">
    </div>
    <div class="form-group">
      <label>启用状态</label>
      <select id="world-book-active">
        <option value="true" ${item.is_active ? 'selected' : ''}>启用</option>
        <option value="false" ${item.is_active ? '' : 'selected'}>停用</option>
      </select>
    </div>
  `, (el) => {
    const cancel = document.createElement('button');
    cancel.className = 'btn';
    cancel.textContent = '取消';
    cancel.onclick = closeModal;
    const save = document.createElement('button');
    save.className = 'btn btn-primary';
    save.textContent = '保存修改';
    save.onclick = () => saveWorldBook(itemId);
    el.appendChild(cancel);
    el.appendChild(save);
  });
}

async function saveWorldBook(itemId) {
  const name = (document.getElementById('world-book-name')?.value || '').trim();
  const content = (document.getElementById('world-book-content')?.value || '').trim();
  const tags = (document.getElementById('world-book-tags')?.value || '').split(/[,，、]/).map((x) => x.trim()).filter(Boolean);
  const matchKeywords = (document.getElementById('world-book-keywords')?.value || '').split(/[,，、]/).map((x) => x.trim()).filter(Boolean);
  const isActive = String(document.getElementById('world-book-active')?.value || 'true') === 'true';
  if (!content) {
    alert('请填写世界书正文');
    return;
  }
  const payload = { name, content, tags, match_keywords: matchKeywords, is_active: isActive };
  try {
    if (itemId) {
      await apiFetch(`/world-books/${Number(itemId)}`, {
        method: 'PUT',
        body: JSON.stringify(payload),
      });
      showStatus('世界书条目已更新');
    } else {
      await apiFetch('/world-books', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      showStatus('世界书条目已创建');
    }
    closeModal();
    await loadWorldBooks();
  } catch (e) {
    showStatus('保存世界书失败: ' + e.message, true);
  }
}

async function deleteWorldBook(itemId) {
  if (!confirm(`确认删除世界书条目 #${itemId}？`)) return;
  try {
    await apiFetch(`/world-books/${Number(itemId)}`, { method: 'DELETE' });
    showStatus('世界书条目已删除');
    await loadWorldBooks();
  } catch (e) {
    showStatus('删除世界书失败: ' + e.message, true);
  }
}

async function vectorizeWorldBook(itemId) {
  try {
    await apiFetch(`/world-books/${Number(itemId)}/vectorize`, { method: 'POST' });
    showStatus('世界书条目已向量化');
    await loadWorldBooks();
  } catch (e) {
    showStatus('世界书向量化失败: ' + e.message, true);
  }
}

async function removeWorldBookVector(itemId) {
  try {
    await apiFetch(`/world-books/${Number(itemId)}/vector`, { method: 'DELETE' });
    showStatus('世界书向量已移除');
    await loadWorldBooks();
  } catch (e) {
    showStatus('移除世界书向量失败: ' + e.message, true);
  }
}

async function syncWorldBookVectors() {
  try {
    const data = await apiFetch('/world-books/vector-sync?limit=500', { method: 'POST' });
    const result = data.result || {};
    showStatus(`世界书向量同步完成：成功 ${Number(result.vectorized_world_books || 0)}，失败 ${Number(result.failed || 0)}`);
    await loadWorldBooks();
  } catch (e) {
    showStatus('世界书向量同步失败: ' + e.message, true);
  }
}

async function autoFillWorldBookMeta() {
  const overwriteTitle = confirm('是否覆盖已有标题？\n“取消”表示只补空标题。');
  const overwriteKeywords = confirm('是否覆盖已有关键词？\n“取消”表示只补空关键词。');
  showStatus('正在自动生成世界书标题与关键词...');
  try {
    const data = await apiFetch('/world-books/auto-meta', {
      method: 'POST',
      body: JSON.stringify({
        overwrite_title: overwriteTitle,
        overwrite_keywords: overwriteKeywords,
      }),
    });
    showStatus(
      `自动补全完成：处理 ${Number(data.processed || 0)}，更新 ${Number(data.updated || 0)}，失败 ${Number(data.failed || 0)}`
    );
    await loadWorldBooks();
  } catch (e) {
    showStatus('自动补全失败: ' + e.message, true);
  }
}

async function loadEnvironmentLLMConfig() {
  try {
    const data = await apiFetch('/environment/llm-config');
    const settings = data.settings || {};
    const enabled = !!settings.enabled;
    setInputValue('env-llm-enabled', enabled ? 'true' : 'false');
    setInputValue('env-llm-api-base', settings.api_base || '');
    setInputValue('env-llm-api-key', settings.api_key || '');
    setInputValue('env-llm-model', settings.model || '');
  } catch (e) {
    showStatus('环境 LLM 配置加载失败: ' + e.message, true);
  }
}

async function loadRuntimeLLMConfig() {
  try {
    const data = await apiFetch('/runtime/llm');
    const settings = data.settings || {};
    setInputValue('runtime-llm-api-base', settings.api_base || '');
    setInputValue('runtime-llm-api-key', settings.api_key || '');
    setInputValue('runtime-llm-model', settings.model || '');
    setInputValue('runtime-llm-timeout-sec', settings.timeout_sec || '');
  } catch (e) {
    showStatus('主 LLM 配置加载失败: ' + e.message, true);
  }
}

async function saveRuntimeLLMConfig() {
  const payload = {
    llm_api_base: (document.getElementById('runtime-llm-api-base')?.value || '').trim(),
    llm_api_key: (document.getElementById('runtime-llm-api-key')?.value || '').trim(),
    llm_model: (document.getElementById('runtime-llm-model')?.value || '').trim(),
  };
  const timeoutRaw = (document.getElementById('runtime-llm-timeout-sec')?.value || '').trim();
  if (timeoutRaw) {
    const timeout = Number(timeoutRaw);
    if (!Number.isFinite(timeout) || timeout < 1) {
      showStatus('主 LLM 超时必须是大于等于 1 的数字', true);
      return;
    }
    payload.llm_timeout_sec = timeout;
  }
  try {
    await apiFetch('/runtime/llm', {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    showStatus('主 LLM 配置已保存');
    await loadRuntimeLLMConfig();
  } catch (e) {
    showStatus('主 LLM 配置保存失败: ' + e.message, true);
  }
}

async function loadModelSettingsPage() {
  await Promise.all([
    loadRuntimeLLMConfig(),
    loadEnvironmentLLMConfig(),
    loadSnapshotLLMConfig(),
  ]);
}

async function saveEnvironmentLLMConfig() {
  const payload = {
    enabled: String(document.getElementById('env-llm-enabled')?.value || 'false') === 'true',
    api_base: (document.getElementById('env-llm-api-base')?.value || '').trim(),
    api_key: (document.getElementById('env-llm-api-key')?.value || '').trim(),
    model: (document.getElementById('env-llm-model')?.value || '').trim(),
  };
  try {
    await apiFetch('/environment/llm-config', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    showStatus('环境 LLM 配置已保存');
    await loadEnvironmentLLMConfig();
  } catch (e) {
    showStatus('环境 LLM 配置保存失败: ' + e.message, true);
  }
}

async function loadSnapshotLLMConfig() {
  try {
    const data = await apiFetch('/snapshot/llm-config');
    const settings = data.settings || {};
    const enabled = !!settings.enabled;
    setInputValue('snapshot-llm-enabled', enabled ? 'true' : 'false');
    setInputValue('snapshot-llm-api-base', settings.api_base || '');
    setInputValue('snapshot-llm-api-key', settings.api_key || '');
    setInputValue('snapshot-llm-model', settings.model || '');
  } catch (e) {
    showStatus('快照 LLM 配置加载失败: ' + e.message, true);
  }
}

async function saveSnapshotLLMConfig() {
  const payload = {
    enabled: String(document.getElementById('snapshot-llm-enabled')?.value || 'false') === 'true',
    api_base: (document.getElementById('snapshot-llm-api-base')?.value || '').trim(),
    api_key: (document.getElementById('snapshot-llm-api-key')?.value || '').trim(),
    model: (document.getElementById('snapshot-llm-model')?.value || '').trim(),
  };
  try {
    await apiFetch('/snapshot/llm-config', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    showStatus('快照 LLM 配置已保存');
    await loadSnapshotLLMConfig();
  } catch (e) {
    showStatus('快照 LLM 配置保存失败: ' + e.message, true);
  }
}
