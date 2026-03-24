const API = '/api';

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

function formatTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleString('zh-CN');
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
}

async function loadDashboard() {
  try {
    const { snapshot } = await apiFetch('/snapshots/latest');
    const el = $('#latest-snapshot');
    if (!el) return;
    if (snapshot) {
      el.innerHTML = `<div class="card-content">${escHtml(snapshot.content)}</div>
        <div class="list-meta" style="margin-top:12px">
          类型: ${snapshot.type} | 时间: ${formatTime(snapshot.created_at)}
        </div>`;
      if (snapshot.environment && snapshot.environment !== '{}') {
        try {
          const env = JSON.parse(snapshot.environment);
          if (env.summary) {
            $('#env-info').innerHTML = `<div class="card-content">${escHtml(env.summary)}</div>`;
          }
        } catch(e) {}
      }
    } else {
      el.innerHTML = '<div class="empty">暂无状态快照</div>';
    }
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
  try {
    await apiFetch(`/events/${id}`, {
      method: 'PUT',
      body: JSON.stringify({ title, description: desc, trigger_keywords: keywords, categories }),
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
  const today = new Date();
  const defaultEnd = today.toISOString().split('T')[0];
  const startDate = new Date(today.getTime() - 29 * 86400000);
  const defaultStart = startDate.toISOString().split('T')[0];
  openModal('测试状态机', `
    <div class="tabs" id="test-tabs">
      <div class="tab active" data-tab="get-state">对话开始</div>
      <div class="tab" data-tab="reflect">对话结束</div>
      <div class="tab" data-tab="recall">记忆检索</div>
      <div class="tab" data-tab="periodic-review">阶段性回顾</div>
    </div>
    <div id="test-get-state">
      <div class="form-group">
        <label>当前时间（ISO）</label>
        <input type="text" id="t-now" value="${new Date().toISOString()}">
      </div>
      <div class="form-group">
        <label>上次对话时间（ISO）</label>
        <input type="text" id="t-last" value="${new Date(Date.now() - 86400000).toISOString()}">
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

async function runGetState() {
  const res = document.getElementById('test-result');
  res.innerHTML = '<div class="loading">正在生成状态快照…（可能需要较长时间）</div>';
  try {
    const data = await apiFetch('/state/current', {
      method: 'POST',
      body: JSON.stringify({
        current_time: document.getElementById('t-now').value,
        last_interaction_time: document.getElementById('t-last').value,
      }),
    });
    res.innerHTML = `<div class="detail-content">${escHtml(data.content)}</div>`;
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
      `- 导出时间：${new Date().toLocaleString('zh-CN')}\n` +
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
    `导出时间：${new Date().toLocaleString('zh-CN')}\n` +
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
    const tags = parseJsonArray(item.tags);
    const typeLabel = getKeyRecordTypeLabel(item.type);
    const statusTag = item.status === 'archived'
      ? '<span class="tag">已归档</span>'
      : '<span class="tag" style="background:#2a4035;color:var(--success)">生效中</span>';
    const dateRange = item.start_date || item.end_date
      ? `${item.start_date || '未设开始'} ~ ${item.end_date || '未设结束'}`
      : '';
    return `
      <div class="list-item ${item.status === 'archived' ? 'archived' : ''}" onclick='openEditKeyRecordModal(${JSON.stringify(item).replace(/'/g, "&#39;")})'>
        <div>
          <span class="tag">${escHtml(typeLabel)}</span>
          ${statusTag}
          <span class="tag">${escHtml(item.source || 'manual')}</span>
          <span class="list-meta">${formatTime(item.updated_at)}</span>
        </div>
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
    const data = await apiFetch('/key-records/search', {
      method: 'POST',
      body: JSON.stringify({
        query,
        type: typeFilter || null,
        top_k: 50,
        include_archived: includeArchived,
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
    envSummary = env.summary || '';
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
    md += `- 导出时间：${new Date().toLocaleString('zh-CN')}\n`;
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
  txt += `导出时间：${new Date().toLocaleString('zh-CN')}\n`;
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
            <span class="list-meta">${formatTime(s.created_at)}</span>
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
      if (env.summary) envHtml = `<div class="form-group"><label>环境信息</label><div class="detail-content">${escHtml(env.summary)}</div></div>`;
    } catch(e) {}
  }
  openModal(`快照详情 #${snap.id}`, `
    <div class="list-meta" style="margin-bottom:12px">
      类型: ${snap.type} | 时间: ${formatTime(snap.created_at)}
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
          <span class="list-meta">${formatTime(s.created_at)}</span>
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
  'min_time_unit_hours',
  'inject_hot_events_limit',
  'llm_api_base',
  'llm_api_key',
  'llm_model',
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
];

const PERSONA_SETTINGS_KEYS = [
  'L1_character_background',
  'L1_user_background',
  'L2_character_personality',
  'L2_relationship_dynamics',
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
  prompt_event_anchor: `基于以下信息，以凯尔希的主观视角，判断是否有值得记录的事件发生。
如果有，生成事件锚点描述；如果没有值得特别记录的事，明确回复"无需记录"。

【当前状态快照】
{current_snapshot}

【环境信息】
{environment}

【角色分层设定参考】
{system_layers}

【历史记忆参考】
{memory_context}

要求：
1. 从凯尔希的主观角度判断什么事是"重要的"——对她而言重要的事
2. 用自然语言描述事件的重要性，不要用数字评分
3. 提供3-5个触发关键词（用于未来记忆检索）
4. 给出一个简短事件标题（10-20字）
5. 给出1-3个事件分类，可从以下中选择：情感交流、学术探讨、生活足迹、床榻私语、精神碰撞、工作同步
6. 如果确实没有值得特别记录的事件，回复"无需记录"
7. 内容不多于200字

输出格式（如果有事件）：
标题：[事件标题]
事件描述：[凯尔希主观视角的事件总结]
关键词：[关键词1, 关键词2, 关键词3]
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
  prompt_reflect_event: `基于以下信息，以凯尔希的主观视角，总结这次对话中值得记录的事件。

【对话后的状态快照】
{current_snapshot}

【对话摘要】
{conversation_summary}

【角色分层设定参考】
{system_layers}

【历史记忆参考】
{memory_context}

要求：
1. 从凯尔希的主观角度总结对话中的重要事件
2. 用自然语言描述事件的重要性
3. 提供3-5个触发关键词
4. 给出一个简短事件标题（10-20字）
5. 给出1-3个事件分类，可从以下中选择：情感交流、学术探讨、生活足迹、床榻私语、精神碰撞、工作同步
6. 如果对话确实平淡无奇，可以回复"无需记录"

输出格式（如果有事件）：
标题：[事件标题]
事件描述：[凯尔希主观视角的事件总结]
关键词：[关键词1, 关键词2, 关键词3]
分类：[分类1, 分类2]`,
  prompt_conversation_summary: `请将本次对话整理为“对话摘要”，供记忆系统后续使用。

【当前状态（对话前）】
{previous_snapshot}

【本次原始对话】
{conversation_text}

【历史记忆参考】
{memory_context}

【角色分层设定参考】
{system_layers}

要求：
1. 输出 120-300 字中文摘要，客观、可追溯，不写成对白。
2. 尽量保留关键信息：事实变化、情绪变化、关系变化、未完成事项。
3. 如有明确承诺/计划/约定，请单独用一句点明。
4. 不要输出标题、编号、JSON、代码块。

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
  prompt_evolution_summary: `请基于以下事件评分结果，更新动态人格层（L2）。

【当前 L2 角色人格】
{character_personality}

【当前 L2 关系模式】
{relationship_dynamics}

【近期事件（按重要性排序）】
{scored_events}

要求：
1. 只更新 L2，不能改动任何 L1 稳定背景事实
2. 输出应保持凯尔希风格，避免夸张情绪化
3. 给出简洁且可追溯的变更理由

输出格式：
角色人格更新：[更新后的完整文本]
关系模式更新：[更新后的完整文本]
变更摘要：[不超过120字]`,
  prompt_event_scoring: `以凯尔希主观视角，对以下事件逐条评分。

评分维度：
- 重要性（0-10）：对当前认知、决策和关系影响有多大
- 印象深度（0-10）：这段记忆在近期会保留多深

事件列表：
{events}

输出格式（每条事件一段）：
事件ID: <id>
重要性: <0-10数字>
印象深度: <0-10数字>
理由: <一句话>`,
  prompt_environment_generation: `请直接生成“当前环境信息”文本，供状态快照与事件锚点使用。

输入上下文：
- 时间：{time}
- 日期：{date}
- 星期：{weekday}
- 时间段：{time_period}
- 上一段环境（JSON）：{previous_env}
- 最新状态快照：{latest_snapshot}
- 连贯提示：{continuity}

要求：
1. 输出 80-180 字中文，不要使用标题、编号或代码块。
2. 内容应包含：地点/活动/外部环境氛围，并体现与上一时段的连续性。
3. 避免与上一段环境重复措辞，优先给出有变化的细节。
4. 语气客观克制，服务于后续状态推演，不要写成对白。

只输出环境正文。`,
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
    const keys = ['evolution_event_threshold', 'archive_importance_threshold'];
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
    L2_relationship_dynamics: "可选：L2关系模式"
  },
  snapshots: [
    {
      created_at: "2026-03-20T08:30:00",
      type: "accumulated",
      content: "示例快照内容",
      environment: { summary: "示例环境摘要" },
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
  const importTab = document.getElementById('settings-tab-import');
  if (persona) persona.style.display = tab === 'persona' ? 'block' : 'none';
  if (prompts) prompts.style.display = tab === 'prompts' ? 'block' : 'none';
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
    `;
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger)">${escHtml(e.message)}</div>`;
  }
}

async function previewEvolution() {
  const el = document.getElementById('evolution-preview');
  if (!el) return;
  el.innerHTML = '<div class="loading">正在生成演化预览…</div>';
  try {
    const data = await apiFetch('/evolution/preview', { method: 'POST' });
    latestEvolutionPreview = data;
    const oldCharacter = document.getElementById('setting-L2_character_personality')?.value || '';
    const oldRelationship = document.getElementById('setting-L2_relationship_dynamics')?.value || '';
    const topEvents = (data.scored_events || []).slice(0, 10);
    const eventHtml = topEvents.map(e => `
      <div class="list-item">
        <div><span class="tag">#${e.id}</span><span class="list-meta">${e.date}</span></div>
        <div class="list-preview">${escHtml(e.description)}</div>
        <div class="list-meta">重要性 ${Number(e.importance_score || 0).toFixed(1)} | 印象深度 ${Number(e.impression_depth || 0).toFixed(1)}</div>
      </div>
    `).join('');
    el.innerHTML = `
      <div class="card" style="margin-bottom:8px">
        <h2>演化摘要</h2>
        <div class="detail-content">${escHtml(data.change_summary || '无')}</div>
      </div>
      <div class="card" style="margin-bottom:8px">
        <h2>L2 角色人格 Diff 预览</h2>
        <div class="list-meta">旧版本</div>
        <div class="detail-content">${escHtml(oldCharacter)}</div>
        <div class="list-meta" style="margin-top:8px">新版本</div>
        <div class="detail-content">${escHtml(data.new_character_personality || '')}</div>
      </div>
      <div class="card" style="margin-bottom:8px">
        <h2>L2 关系模式 Diff 预览</h2>
        <div class="list-meta">旧版本</div>
        <div class="detail-content">${escHtml(oldRelationship)}</div>
        <div class="list-meta" style="margin-top:8px">新版本</div>
        <div class="detail-content">${escHtml(data.new_relationship_dynamics || '')}</div>
      </div>
      <div class="card">
        <h2>事件评分 Top10</h2>
        ${eventHtml || '<div class="empty">暂无评分事件</div>'}
      </div>
    `;
    showStatus('演化预览已生成');
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger)">${escHtml(e.message)}</div>`;
    showStatus('演化预览失败: ' + e.message, true);
  }
}

async function applyEvolution() {
  if (!latestEvolutionPreview) {
    alert('请先执行一次“预览人格更新”');
    return;
  }
  if (!confirm('确认应用本次人格演化？这会更新 L2 并归档低分事件。')) return;
  try {
    const data = await apiFetch('/evolution/apply', {
      method: 'POST',
      body: JSON.stringify({ preview: latestEvolutionPreview }),
    });
    showStatus(`演化已应用，归档事件 ${data.archived_count || 0} 条`);
    await loadSettingsPage();
  } catch (e) {
    showStatus('演化应用失败: ' + e.message, true);
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

  if (!confirm(`确认重算归档状态？将按当前 archive_importance_threshold 重新判断历史事件（${rangeText}）。`)) {
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
