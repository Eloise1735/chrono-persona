const API = '/api';

// 鈹€鈹€ Utility 鈹€鈹€

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
  return text.length > max ? text.slice(0, max) + '...' : text;
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
      ? `<div class="list-meta">Cost: unavailable (${unknownTokens} tokens unpriced)</div>`
      : '<div class="list-meta">Cost: unavailable</div>';
  }
  const top = models.slice(0, 3);
  const items = top.map((item) => {
    const model = escHtml(String(item.model || 'unknown'));
    const cost = formatUsd(item.estimated_cost_usd || 0);
    const total = Number(item.total_tokens || 0);
    const tag = item.has_pricing ? '' : ' (unpriced)';
    return `${model}: ${cost} / ${total} tokens${tag}`;
  });
  const unknownText = unknownTokens > 0 ? `; plus ${unknownTokens} unpriced tokens` : '';
  return `<div class="list-meta">Cost: ${items.join('; ')}${unknownText}</div>`;
}

function showStatus(msg, isError = false) {
  const bar = $('#status-bar');
  if (bar) {
    bar.textContent = msg;
    bar.style.color = isError ? 'var(--danger)' : 'var(--text-dim)';
  }
}

// 鈹€鈹€ Dashboard Page 鈹€鈹€

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
          绫诲瀷: ${snapshot.type} | 鏃堕棿: ${formatTime(snapshot.created_at)}
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
      el.innerHTML = '<div class="empty">鏆傛棤鐘舵€佸揩鐓?/div>';
    }
    showStatus('浠〃鐩樺凡鍔犺浇');
  } catch (e) {
    showStatus('鍔犺浇澶辫触: ' + e.message, true);
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
  if (!report || !report.ran) return '<div class="empty">鑷姩鍖栨湭鎵ц鎴栧凡鍏抽棴銆?/div>';
  const vectorSync = report.vector_sync || {};
  const evolution = report.evolution || {};
  const compaction = report.compaction || {};
  const llmUsage = report.llm_usage || {};
  const errors = Array.isArray(report.errors) ? report.errors : [];
  return `
    <div class="list-meta">瑙﹀彂婧愶細${escHtml(String(report.trigger || 'unknown'))}</div>
    <div class="list-meta">鍚戦噺鍚屾锛氫簨浠?${Number(vectorSync.vectorized_events || 0)}锛屽揩鐓?${Number(vectorSync.vectorized_snapshots || 0)}</div>
    <div class="list-meta">浜烘牸婕斿寲锛?{evolution.applied ? '宸叉墽琛? : '鏈Е鍙?}</div>
    <div class="list-meta">鍐峰帇缂╋細鏂板鎽樿 ${Number(compaction.created_summaries || 0)}锛屽垹闄ゆ棫鍚戦噺 ${Number(compaction.deleted_originals || 0)}</div>
    <div class="list-meta">Token锛氳緭鍏?${Number(llmUsage.prompt_tokens || 0)}锛岃緭鍑?${Number(llmUsage.completion_tokens || 0)}锛屾€昏 ${Number(llmUsage.total_tokens || 0)}锛堣姹?${Number(llmUsage.requests || 0)} 娆★級</div>
    ${errors.length ? `<div class="list-meta" style="color:var(--danger)">寮傚父锛?{escHtml(errors.join('; '))}</div>` : ''}
  `;
}

async function loadAutomationLatest() {
  const el = document.getElementById('automation-latest');
  if (!el) return;
  el.innerHTML = '<div class="loading">鍔犺浇涓€?/div>';
  try {
    const data = await apiFetch('/automation/latest');
    const item = data.item;
    if (!item) {
      el.innerHTML = '<div class="empty">鏆傛棤鑷姩鍖栨墽琛岃褰?/div>';
      return;
    }
    el.innerHTML = `
      <div class="list-item" style="cursor:default">
        <div><span class="tag">#${Number(item.id || 0)}</span><span class="list-meta">${formatTime(item.created_at)}</span></div>
        <div style="margin-top:6px">${renderAutomationReport(item.report || {})}</div>
      </div>
    `;
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger)">鍔犺浇澶辫触: ${escHtml(e.message)}</div>`;
  }
}

async function loadAutomationHistory() {
  const el = document.getElementById('automation-history');
  if (!el) return;
  el.innerHTML = '<div class="loading">鍔犺浇涓€?/div>';
  try {
    const data = await apiFetch('/automation/runs?limit=20');
    const items = data.items || [];
    if (!items.length) {
      el.innerHTML = '<div class="empty">鏆傛棤鍘嗗彶璁板綍</div>';
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
    el.innerHTML = `<div style="color:var(--danger)">鍔犺浇澶辫触: ${escHtml(e.message)}</div>`;
  }
}

async function loadAutomationTokenSummary() {
  const el = document.getElementById('token-summary');
  if (!el) return;
  el.innerHTML = '<div class="loading">鍔犺浇涓€?/div>';
  try {
    const data = await apiFetch('/automation/token-summary');
    const today = data.today || {};
    const week = data.week || {};
    const all = data.all || {};
    const pricingUnit = String(data.pricing_unit || 'USD / 1M tokens');
    el.innerHTML = `
      <div class="grid-2">
        <div class="list-item" style="cursor:default">
          <div><span class="tag">浠婃棩</span><span class="list-meta">UTC鏃ョ晫</span></div>
          <div class="list-meta">娴佺▼鏁帮細${Number(today.runs || 0)} | 璇锋眰鏁帮細${Number(today.requests || 0)}</div>
          <div class="list-meta">杈撳叆锛?{Number(today.prompt_tokens || 0)} | 杈撳嚭锛?{Number(today.completion_tokens || 0)}</div>
          <div class="list-meta">鎬昏锛?{Number(today.total_tokens || 0)}</div>
          <div class="list-meta">浼扮畻鎴愭湰锛?{formatUsd(today.estimated_cost_usd || 0)}锛?{escHtml(pricingUnit)}锛?/div>
          ${renderCostBreakdown(today)}
        </div>
        <div class="list-item" style="cursor:default">
          <div><span class="tag">鏈懆</span><span class="list-meta">鍛ㄤ竴鑷充粖锛圲TC锛?/span></div>
          <div class="list-meta">娴佺▼鏁帮細${Number(week.runs || 0)} | 璇锋眰鏁帮細${Number(week.requests || 0)}</div>
          <div class="list-meta">杈撳叆锛?{Number(week.prompt_tokens || 0)} | 杈撳嚭锛?{Number(week.completion_tokens || 0)}</div>
          <div class="list-meta">鎬昏锛?{Number(week.total_tokens || 0)}</div>
          <div class="list-meta">浼扮畻鎴愭湰锛?{formatUsd(week.estimated_cost_usd || 0)}锛?{escHtml(pricingUnit)}锛?/div>
          ${renderCostBreakdown(week)}
        </div>
      </div>
      <div class="list-item" style="cursor:default; margin-top:8px;">
        <div><span class="tag">绱</span><span class="list-meta">鑷姩鍖栨姤鍛婂彲缁熻鍖洪棿</span></div>
        <div class="list-meta">娴佺▼鏁帮細${Number(all.runs || 0)} | 璇锋眰鏁帮細${Number(all.requests || 0)}</div>
        <div class="list-meta">杈撳叆锛?{Number(all.prompt_tokens || 0)} | 杈撳嚭锛?{Number(all.completion_tokens || 0)} | 鎬昏锛?{Number(all.total_tokens || 0)}</div>
        <div class="list-meta">浼扮畻鎴愭湰锛?{formatUsd(all.estimated_cost_usd || 0)}锛?{escHtml(pricingUnit)}锛?/div>
        ${renderCostBreakdown(all)}
      </div>
    `;
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger)">鍔犺浇澶辫触: ${escHtml(e.message)}</div>`;
  }
}

function renderModelPricingRows(items) {
  if (!items.length) return '<div class="empty">鏆傛棤妯″瀷鍗曚环閰嶇疆</div>';
  return items.map((item) => {
    const modelRaw = String(item.model || '');
    const model = escHtml(modelRaw);
    const safeModelArg = JSON.stringify(modelRaw).replace(/'/g, '&#39;');
    const prompt = Number(item.prompt_price || 0).toFixed(4);
    const completion = Number(item.completion_price || 0).toFixed(4);
    return `
      <div class="list-item" style="cursor:default; margin-top:6px;">
        <div><span class="tag">${model}</span></div>
        <div class="list-meta">杈撳叆锛?{prompt} | 杈撳嚭锛?{completion}</div>
        <div class="btn-group" style="margin-top:6px;">
          <button class="btn btn-danger" onclick='deleteModelPricingFromDashboard(${safeModelArg})'>鍒犻櫎</button>
        </div>
      </div>
    `;
  }).join('');
}

async function loadModelPricingForDashboard() {
  const el = document.getElementById('token-pricing-list');
  if (!el) return;
  el.innerHTML = '<div class="loading">鍔犺浇涓€?/div>';
  try {
    const data = await apiFetch('/automation/model-pricing');
    const items = Array.isArray(data.items) ? data.items : [];
    el.innerHTML = renderModelPricingRows(items);
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger)">鍔犺浇澶辫触: ${escHtml(e.message)}</div>`;
  }
}

async function saveModelPricingFromDashboard() {
  const model = (document.getElementById('pricing-model')?.value || '').trim();
  const promptRaw = (document.getElementById('pricing-prompt')?.value || '').trim();
  const completionRaw = (document.getElementById('pricing-completion')?.value || '').trim();
  const promptPrice = Number(promptRaw);
  const completionPrice = Number(completionRaw);

  if (!model) {
    showStatus('Please enter a model name.', true);
    return;
  }
  if (!Number.isFinite(promptPrice) || promptPrice < 0 || !Number.isFinite(completionPrice) || completionPrice < 0) {
    showStatus('Please enter valid non-negative prices.', true);
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
    showStatus(`妯″瀷鍗曚环宸蹭繚瀛橈細${model}`);
    await Promise.all([loadModelPricingForDashboard(), loadAutomationTokenSummary()]);
  } catch (e) {
    showStatus('淇濆瓨妯″瀷鍗曚环澶辫触: ' + e.message, true);
  }
}

async function deleteModelPricingFromDashboard(model) {
  if (!confirm(`Delete pricing for model "${model}"?`)) return;
  try {
    await apiFetch(`/automation/model-pricing?model=${encodeURIComponent(model)}`, {
      method: 'DELETE',
    });
    showStatus(`宸插垹闄ゆā鍨嬪崟浠凤細${model}`);
    await Promise.all([loadModelPricingForDashboard(), loadAutomationTokenSummary()]);
  } catch (e) {
    showStatus('鍒犻櫎妯″瀷鍗曚环澶辫触: ' + e.message, true);
  }
}

// 鈹€鈹€ Modal helpers 鈹€鈹€

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

// 鈹€鈹€ Add Event Modal 鈹€鈹€

function openAddEventModal() {
  const today = new Date().toISOString().split('T')[0];
  openModal('娣诲姞浜嬩欢閿氱偣', `
    <div class="form-group">
      <label>浜嬩欢鏃ユ湡</label>
      <input type="date" id="ev-date" value="${today}">
    </div>
    <div class="form-group">
      <label>浜嬩欢鏍囬锛堝彲鐣欑┖鑷姩鐢熸垚锛?/label>
      <input type="text" id="ev-title" placeholder="渚嬪锛氬噷鏅ㄨ璁哄悗褰㈡垚鐨勫叡璇?>
    </div>
    <div class="form-group">
      <label>浜嬩欢鎻忚堪</label>
      <textarea id="ev-desc" placeholder="浠ュ嚡灏斿笇鐨勪富瑙傝瑙掓弿杩颁簨浠?.."></textarea>
    </div>
    <div class="form-group">
      <label>鍏抽敭璇嶏紙閫楀彿鍒嗛殧锛?/label>
      <input type="text" id="ev-keywords" placeholder="鍏抽敭璇?, 鍏抽敭璇?, 鍏抽敭璇?">
    </div>
    <div class="form-group">
      <label>鍒嗙被锛堥€楀彿鍒嗛殧锛屽彲鐣欑┖鑷姩鍒嗙被锛?/label>
      <input type="text" id="ev-categories" placeholder="鎯呮劅浜ゆ祦, 瀛︽湳鎺㈣">
    </div>
  `, (el) => {
    const cancel = document.createElement('button');
    cancel.className = 'btn'; cancel.textContent = '鍙栨秷';
    cancel.onclick = closeModal;
    const save = document.createElement('button');
    save.className = 'btn btn-primary'; save.textContent = '淇濆瓨';
    save.onclick = saveNewEvent;
    el.appendChild(cancel);
    el.appendChild(save);
  });
}

async function saveNewEvent() {
  const title = (document.getElementById('ev-title')?.value || '').trim();
  const desc = document.getElementById('ev-desc').value.trim();
  if (!desc) { alert('Please fill in event description.'); return; }
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
    showStatus('Done');
    if (typeof loadEvents === 'function') loadEvents();
  } catch(e) { alert('淇濆瓨澶辫触: ' + e.message); }
}

// 鈹€鈹€ Edit Event Modal 鈹€鈹€

function openEditEventModal(event) {
  let keywords = [];
  try { keywords = JSON.parse(event.trigger_keywords || '[]'); } catch(e) {}
  let categories = [];
  try { categories = JSON.parse(event.categories || '[]'); } catch(e) {}

  openModal('缂栬緫浜嬩欢閿氱偣', `
    <div class="form-group">
      <label>浜嬩欢鏍囬</label>
      <input type="text" id="ev-title" value="${escHtml(event.title || '')}">
    </div>
    <div class="form-group">
      <label>浜嬩欢鎻忚堪</label>
      <textarea id="ev-desc">${escHtml(event.description)}</textarea>
    </div>
    <div class="form-group">
      <label>鍏抽敭璇嶏紙閫楀彿鍒嗛殧锛?/label>
      <input type="text" id="ev-keywords" value="${keywords.join(', ')}">
    </div>
    <div class="form-group">
      <label>鍒嗙被锛堥€楀彿鍒嗛殧锛?/label>
      <input type="text" id="ev-categories" value="${categories.join(', ')}">
    </div>
  `, (el) => {
    const cancel = document.createElement('button');
    cancel.className = 'btn'; cancel.textContent = '鍙栨秷';
    cancel.onclick = closeModal;
    const del = document.createElement('button');
    del.className = 'btn btn-danger'; del.textContent = '鍒犻櫎';
    del.onclick = () => deleteEvent(event.id);
    const save = document.createElement('button');
    save.className = 'btn btn-primary'; save.textContent = '淇濆瓨淇敼';
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
    showStatus('Done');
    if (typeof loadEvents === 'function') loadEvents();
  } catch(e) { alert('鏇存柊澶辫触: ' + e.message); }
}

async function deleteEvent(id) {
  if (!confirm('纭鍒犻櫎姝や簨浠讹紵')) return;
  try {
    await apiFetch(`/events/${id}`, { method: 'DELETE' });
    closeModal();
    showStatus('Done');
    if (typeof loadEvents === 'function') loadEvents();
  } catch(e) { alert('鍒犻櫎澶辫触: ' + e.message); }
}

// 鈹€鈹€ Trigger Snapshot 鈹€鈹€

async function triggerSnapshot() {
  const content = prompt('杈撳叆蹇収鍐呭锛堢暀绌哄垯鐢辩郴缁熺敓鎴愶級:');
  if (content === null) return;
  if (content.trim()) {
    try {
      await apiFetch('/snapshots', {
        method: 'POST',
        body: JSON.stringify({ content: content.trim(), type: 'accumulated' }),
      });
      showStatus('Done');
      loadDashboard();
    } catch(e) { alert('鍒涘缓澶辫触: ' + e.message); }
  } else {
    alert('Action required.');
  }
}

// 鈹€鈹€ Test State Machine 鈹€鈹€

function openTestPanel() {
  const today = new Date();
  const defaultEnd = today.toISOString().split('T')[0];
  const startDate = new Date(today.getTime() - 29 * 86400000);
  const defaultStart = startDate.toISOString().split('T')[0];
  openModal('娴嬭瘯鐘舵€佹満', `
    <div class="tabs" id="test-tabs">
      <div class="tab active" data-tab="get-state">瀵硅瘽寮€濮?/div>
      <div class="tab" data-tab="reflect">瀵硅瘽缁撴潫</div>
      <div class="tab" data-tab="recall">璁板繂妫€绱?/div>
      <div class="tab" data-tab="periodic-review">闃舵鎬у洖椤?/div>
    </div>
    <div id="test-get-state">
      <div class="form-group">
        <label>褰撳墠鏃堕棿锛圛SO锛?/label>
        <input type="text" id="t-now" value="${new Date().toISOString()}">
      </div>
      <div class="form-group">
        <label>涓婃瀵硅瘽鏃堕棿锛圛SO锛?/label>
        <input type="text" id="t-last" value="${new Date(Date.now() - 86400000).toISOString()}">
      </div>
      <div class="form-group">
        <button class="btn btn-primary" onclick="runGetState()">鎵ц get_current_state</button>
      </div>
    </div>
    <div id="test-reflect" style="display:none">
      <div class="form-group">
        <label>瀵硅瘽鎽樿</label>
        <textarea id="t-summary" placeholder="杈撳叆瀵硅瘽鎽樿..."></textarea>
      </div>
      <div class="form-group">
        <button class="btn btn-primary" onclick="runReflect()">鎵ц reflect_on_conversation</button>
      </div>
    </div>
    <div id="test-recall" style="display:none">
      <div class="form-group">
        <label>鎼滅储鍏抽敭璇?/label>
        <input type="text" id="t-query" placeholder="杈撳叆鎼滅储鍏抽敭璇?..">
      </div>
      <div class="form-group">
        <button class="btn btn-primary" onclick="runRecall()">鎵ц recall_memories</button>
      </div>
    </div>
    <div id="test-periodic-review" style="display:none">
      <div class="form-group">
        <label>璧峰鏃ユ湡</label>
        <input type="date" id="t-review-start" value="${defaultStart}">
      </div>
      <div class="form-group">
        <label>缁撴潫鏃ユ湡</label>
        <input type="date" id="t-review-end" value="${defaultEnd}">
      </div>
      <div class="form-group">
        <label style="display:flex;align-items:center;gap:8px">
          <input type="checkbox" id="t-review-include-archived">
          鍖呭惈宸插綊妗ｄ簨浠?        </label>
      </div>
      <div class="form-group">
        <button class="btn btn-primary" onclick="runPeriodicReview()">鎵ц periodic_review</button>
      </div>
    </div>
    <div id="test-result" style="margin-top:16px"></div>
  `, (el) => {
    const close = document.createElement('button');
    close.className = 'btn'; close.textContent = '鍏抽棴';
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
  res.innerHTML = '<div class="loading">姝ｅ湪鐢熸垚鐘舵€佸揩鐓р€︼紙鍙兘闇€瑕佽緝闀挎椂闂达級</div>';
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
  res.innerHTML = '<div class="loading">姝ｅ湪鐢熸垚鍙嶆€濃€?/div>';
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
  res.innerHTML = '<div class="loading">鎼滅储涓€?/div>';
  try {
    const data = await apiFetch('/memories/search', {
      method: 'POST',
      body: JSON.stringify({ query: document.getElementById('t-query').value, top_k: 5 }),
    });
    if (!data.results || data.results.length === 0) {
      res.innerHTML = '<div class="empty">鏈壘鍒扮浉鍏宠蹇?/div>';
    } else {
      const assocCount = data.results.filter(r => r?.metadata?.selection_reason === 'associative_random').length;
      res.innerHTML = data.results.map(r => `
        <div class="list-item">
          <div>
            <span class="tag">${r.source_type === 'event' ? '浜嬩欢' : '蹇収'}</span>
            ${r?.metadata?.selection_reason === 'associative_random'
              ? '<span class="tag tag-assoc">鑱旀兂鍛戒腑</span>'
              : '<span class="tag tag-rank">鎺掑簭鍛戒腑</span>'}
            ${r?.metadata?.date ? `<span class="list-meta" style="margin-left:6px">鏃ユ湡: ${escHtml(String(r.metadata.date))}</span>` : ''}
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
            鏈妫€绱腑鏈?${assocCount} 鏉♀€滆仈鎯冲懡涓€濄€傝仈鎯冲懡涓潵鑷熬閮ㄥ€欓€夌殑鍔犳潈闅忔満鎶芥牱锛堝惈澶氭牱鎬т笌鍐烽棬濂栧姳锛夈€?          </div>
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
    res.innerHTML = '<div style="color:var(--danger)">璇峰～鍐欏畬鏁寸殑璧锋鏃ユ湡</div>';
    return;
  }
  if (startDate > endDate) {
    res.innerHTML = '<div style="color:var(--danger)">鏃ユ湡鑼冨洿鏃犳晥锛氳捣濮嬫棩鏈熶笉鑳芥櫄浜庣粨鏉熸棩鏈?/div>';
    return;
  }
  res.innerHTML = '<div class="loading">姝ｅ湪鐢熸垚闃舵鎬у洖椤锯€?/div>';
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
        鏃堕棿鑼冨洿锛?{escHtml(stats.start_date || startDate)} ~ ${escHtml(stats.end_date || endDate)} |
        浜嬩欢鏁帮細${Number(stats.event_count || 0)} |
        蹇収鏁帮細${Number(stats.snapshot_count || 0)}
      </div>
      <div class="btn-group" style="margin-bottom:8px">
        <select id="periodic-review-export-format" style="max-width:160px">
          <option value="md">Markdown</option>
          <option value="txt">TXT</option>
          <option value="json">JSON</option>
        </select>
        <button class="btn" onclick="exportPeriodicReview()">瀵煎嚭鏈鍥為【</button>
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
      `# 闃舵鎬у洖椤綷n\n` +
      `- 瀵煎嚭鏃堕棿锛?{new Date().toLocaleString('zh-CN')}\n` +
      `- 鏃堕棿鑼冨洿锛?{rangeText}\n` +
      `- 浜嬩欢鏁帮細${Number(stats.event_count || 0)}\n` +
      `- 蹇収鏁帮細${Number(stats.snapshot_count || 0)}\n` +
      `- 鍖呭惈褰掓。浜嬩欢锛?{review.include_archived ? '鏄? : '鍚?}\n\n` +
      `## 鍥為【姝ｆ枃\n\n` +
      `${review.content || ''}\n`
    );
  }
  return (
    `闃舵鎬у洖椤惧鍑篭n` +
    `瀵煎嚭鏃堕棿锛?{new Date().toLocaleString('zh-CN')}\n` +
    `鏃堕棿鑼冨洿锛?{rangeText}\n` +
    `浜嬩欢鏁帮細${Number(stats.event_count || 0)}\n` +
    `蹇収鏁帮細${Number(stats.snapshot_count || 0)}\n` +
    `鍖呭惈褰掓。浜嬩欢锛?{review.include_archived ? '鏄? : '鍚?}\n\n` +
    `----- 鍥為【姝ｆ枃 -----\n` +
    `${review.content || ''}\n`
  );
}

function exportPeriodicReview() {
  const review = window.__latestPeriodicReview;
  if (!review || !review.content) {
    alert('Action required.');
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
  showStatus(`闃舵鎬у洖椤惧凡瀵煎嚭锛?{filename}`);
}

// 鈹€鈹€ Key Records Page 鈹€鈹€

const KEY_RECORD_TYPE_LABELS = {
  important_date: '鍏抽敭鏃ユ湡',
  important_item: '鍏抽敭鐗╁搧',
  key_collaboration: '鍏抽敭鍗忎綔',
  medical_advice: '鍖荤枟寤鸿',
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
  return KEY_RECORD_TYPE_LABELS[type] || type || ''
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
  list.innerHTML = '<div class="loading">鍔犺浇涓€?/div>';
  try {
    const data = await apiFetch(`/key-records?${params.toString()}`);
    latestKeyRecords = data.items || [];
    renderKeyRecordList(latestKeyRecords);
    showStatus(`Loaded ${latestKeyRecords.length} key records`);
  } catch (e) {
    list.innerHTML = `<div style="color:var(--danger)">鍔犺浇澶辫触: ${escHtml(e.message)}</div>`;
  }
}

function renderKeyRecordList(items) {
  const list = document.getElementById('key-record-list');
  if (!list) return;
  if (!items || !items.length) {
    list.innerHTML = '<div class="empty">鏆傛棤鍏抽敭璁板綍</div>';
    return;
  }
  list.innerHTML = items.map(item => {
    const tags = parseJsonArray(item.tags);
    const typeLabel = getKeyRecordTypeLabel(item.type);
    const statusTag = item.status === 'archived'
      ? '<span class="tag">宸插綊妗?/span>'
      : '<span class="tag" style="background:#2a4035;color:var(--success)">鐢熸晥涓?/span>';
    const dateRange = item.start_date || item.end_date
      ? `${item.start_date || 'N/A'} ~ ${item.end_date || 'N/A'}`
      : '';
    return `
      <div class="list-item ${item.status === 'archived' ? 'archived' : ''}" onclick='openEditKeyRecordModal(${JSON.stringify(item).replace(/'/g, "&#39;")})'>
        <div>
          <span class="tag">${escHtml(typeLabel)}</span>
          ${statusTag}
          <span class="tag">${escHtml(item.source || 'manual')}</span>
          <span class="list-meta">${formatTime(item.updated_at)}</span>
        </div>
        <div class="list-preview"><strong>${escHtml(item.title || '')}</strong></div>
        <div class="list-preview">${escHtml(truncate(item.content_text || '', 220))}</div>
        ${dateRange ? `<div class="list-meta">鏈夋晥鏈? ${escHtml(dateRange)}</div>` : ''}
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
  list.innerHTML = '<div class="loading">鎼滅储涓€?/div>';
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
    showStatus(`Key records search completed: ${latestKeyRecords.length}`);
  } catch (e) {
    list.innerHTML = `<div style="color:var(--danger)">鎼滅储澶辫触: ${escHtml(e.message)}</div>`;
  }
}

function openAddKeyRecordModal() {
  const today = new Date().toISOString().split('T')[0];
  openModal('娣诲姞鍏抽敭璁板綍', `
    <div class="form-group">
      <label>绫诲瀷</label>
      <select id="kr-type">
        <option value="important_date">鍏抽敭鏃ユ湡</option>
        <option value="important_item" selected>鍏抽敭鐗╁搧</option>
        <option value="key_collaboration">鍏抽敭鍗忎綔</option>
        <option value="medical_advice">鍖荤枟寤鸿</option>
      </select>
    </div>
    <div class="form-group">
      <label>鏍囬</label>
      <input type="text" id="kr-title" placeholder="渚嬪锛氳繎鏈熻儍鐥涚敤鑽缓璁?>
    </div>
    <div class="form-group">
      <label>姝ｆ枃锛堝彲绮樿创琛ㄦ牸/寤鸿锛?/label>
      <textarea id="kr-content" placeholder="杈撳叆璇︾粏璁板綍鍐呭..."></textarea>
    </div>
    <div class="form-group">
      <label>鏍囩锛堥€楀彿鍒嗛殧锛?/label>
      <input type="text" id="kr-tags" placeholder="鑳冪棝, 鐢ㄨ嵂, 鏅氶棿">
    </div>
    <div class="grid-2">
      <div class="form-group">
        <label>寮€濮嬫棩鏈燂紙鍙€夛級</label>
        <input type="date" id="kr-start" value="${today}">
      </div>
      <div class="form-group">
        <label>缁撴潫鏃ユ湡锛堝彲閫夛級</label>
        <input type="date" id="kr-end">
      </div>
    </div>
    <div class="form-group">
      <label>鐘舵€?/label>
      <select id="kr-status">
        <option value="active" selected>active</option>
        <option value="archived">archived</option>
      </select>
    </div>
  `, (el) => {
    const cancel = document.createElement('button');
    cancel.className = 'btn';
    cancel.textContent = '鍙栨秷';
    cancel.onclick = closeModal;
    const save = document.createElement('button');
    save.className = 'btn btn-primary';
    save.textContent = '淇濆瓨';
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
    alert('Action required.');
    return;
  }
  if (!payload.content_text) {
    alert('Action required.');
    return;
  }
  try {
    await apiFetch('/key-records', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    closeModal();
    showStatus('Done');
    await loadKeyRecords();
  } catch (e) {
    alert('淇濆瓨澶辫触: ' + e.message);
  }
}

function openEditKeyRecordModal(record) {
  const tags = parseJsonArray(record.tags);
  openModal('缂栬緫鍏抽敭璁板綍', `
    <div class="form-group">
      <label>绫诲瀷</label>
      <select id="kr-type">
        ${Object.entries(KEY_RECORD_TYPE_LABELS).map(([value, label]) => `
          <option value="${value}" ${record.type === value ? 'selected' : ''}>${label}</option>
        `).join('')}
      </select>
    </div>
    <div class="form-group">
      <label>鏍囬</label>
      <input type="text" id="kr-title" value="${escHtml(record.title || '')}">
    </div>
    <div class="form-group">
      <label>姝ｆ枃</label>
      <textarea id="kr-content">${escHtml(record.content_text || '')}</textarea>
    </div>
    <div class="form-group">
      <label>鏍囩锛堥€楀彿鍒嗛殧锛?/label>
      <input type="text" id="kr-tags" value="${escHtml(tags.join(', '))}">
    </div>
    <div class="grid-2">
      <div class="form-group">
        <label>寮€濮嬫棩鏈?/label>
        <input type="date" id="kr-start" value="${record.start_date || ''}">
      </div>
      <div class="form-group">
        <label>缁撴潫鏃ユ湡</label>
        <input type="date" id="kr-end" value="${record.end_date || ''}">
      </div>
    </div>
    <div class="form-group">
      <label>鐘舵€?/label>
      <select id="kr-status">
        <option value="active" ${record.status === 'active' ? 'selected' : ''}>active</option>
        <option value="archived" ${record.status === 'archived' ? 'selected' : ''}>archived</option>
      </select>
    </div>
  `, (el) => {
    const cancel = document.createElement('button');
    cancel.className = 'btn';
    cancel.textContent = '鍙栨秷';
    cancel.onclick = closeModal;
    const del = document.createElement('button');
    del.className = 'btn btn-danger';
    del.textContent = '鍒犻櫎';
    del.onclick = () => deleteKeyRecord(record.id);
    const save = document.createElement('button');
    save.className = 'btn btn-primary';
    save.textContent = '淇濆瓨淇敼';
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
    alert('Action required.');
    return;
  }
  try {
    await apiFetch(`/key-records/${id}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    closeModal();
    showStatus('Done');
    await loadKeyRecords();
  } catch (e) {
    alert('鏇存柊澶辫触: ' + e.message);
  }
}

async function deleteKeyRecord(id) {
  if (!confirm('Delete this key record?')) return;
  try {
    await apiFetch(`/key-records/${id}`, { method: 'DELETE' });
    closeModal();
    showStatus('Done');
    await loadKeyRecords();
  } catch (e) {
    alert('鍒犻櫎澶辫触: ' + e.message);
  }
}

// 鈹€鈹€ History Page 鈹€鈹€

let currentTab = 'snapshots';
let showArchivedEvents = false;
let latestEvolutionPreview = null;
const EVENT_CATEGORY_OPTIONS = [
  '鎯呮劅浜ゆ祦',
  '瀛︽湳鎺㈣',
  '鐢熸椿瓒宠抗',
  '搴婃绉佽',
  '绮剧纰版挒',
  '宸ヤ綔鍚屾',
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
    el.textContent = '';
    if (toggleBtn) toggleBtn.textContent = '寮€鍚€夋嫨绠＄悊';
    return;
  }
  if (toggleBtn) toggleBtn.textContent = '閫€鍑洪€夋嫨绠＄悊';
  el.textContent = `管理模式已开启：当前${currentTab === 'snapshots' ? '快照' : '事件'} 已选 ${currentCount} 条`;
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
    showStatus('褰撳墠鍒楄〃涓虹┖锛屾棤鍙€夋嫨鏉＄洰');
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
    alert('Action required.');
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
    showStatus(`宸插垹闄?${selectedIds.length} 鏉?{typeName}`);
  } catch (e) {
    showStatus('鎵归噺鍒犻櫎澶辫触: ' + e.message, true);
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
    alert('Action required.');
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
  showStatus(`宸插鍑?${rows.length} 鏉?{currentTab === 'snapshots' ? '蹇収' : '浜嬩欢'}鍒?${filename}`);
}

async function loadSnapshots() {
  const list = $('#data-list');
  if (!list) return;
  list.innerHTML = '<div class="loading">鍔犺浇涓€?/div>';
  try {
    const data = await apiFetch('/snapshots?limit=50');
    latestSnapshots = data.items || [];
    if (!data.items || data.items.length === 0) {
      list.innerHTML = '<div class="empty">鏆傛棤蹇収璁板綍</div>';
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
            ${s.embedding_vector_id ? '<span class="tag" style="background:#2a4035;color:var(--success)">宸插綊妗?/span>' : ''}
          </div>
          <div class="list-preview">${escHtml(truncate(s.content, 150))}</div>
        </div>
      </div>
    `).join('');
    showStatus(`Loaded ${data.items.length} snapshots`);
    updateHistorySelectionSummary();
  } catch(e) { list.innerHTML = `<div style="color:var(--danger)">鍔犺浇澶辫触: ${escHtml(e.message)}</div>`; }
}

async function loadEvents() {
  const list = $('#data-list');
  if (!list) return;
  list.innerHTML = '<div class="loading">鍔犺浇涓€?/div>';
  try {
    const selectedCategories = getSelectedEventCategories();
    const categoryQuery = selectedCategories.length
      ? `&categories=${encodeURIComponent(selectedCategories.join(','))}`
      : '';
    const data = await apiFetch(`/events?limit=50&include_archived=${showArchivedEvents}${categoryQuery}`);
    latestEvents = data.items || [];
    if (!data.items || data.items.length === 0) {
      list.innerHTML = '<div class="empty">鏆傛棤浜嬩欢璁板綍</div>';
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
        ? ` | 閲嶈鎬?${Number(e.importance_score).toFixed(1)} / 鍗拌薄 ${Number(e.impression_depth || 0).toFixed(1)}`
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
              ${e.archived ? '<span class="tag">宸插綊妗?/span>' : ''}
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
    showStatus(`Loaded ${data.items.length} events`);
    updateHistorySelectionSummary();
  } catch(e) { list.innerHTML = `<div style="color:var(--danger)">鍔犺浇澶辫触: ${escHtml(e.message)}</div>`; }
}

function showSnapshotDetail(snap) {
  let envHtml = '';
  if (snap.environment && snap.environment !== '{}') {
    try {
      const env = JSON.parse(snap.environment);
      if (env.summary) envHtml = `<div class="form-group"><label>鐜淇℃伅</label><div class="detail-content">${escHtml(env.summary)}</div></div>`;
    } catch(e) {}
  }
  openModal(`蹇収璇︽儏 #${snap.id}`, `
    <div class="list-meta" style="margin-bottom:12px">
      绫诲瀷: ${snap.type} | 鏃堕棿: ${formatTime(snap.created_at)}
      ${snap.embedding_vector_id ? ' | 已向量化' : ''}
    </div>
    <div class="detail-content">${escHtml(snap.content)}</div>
    ${envHtml}
  `, (el) => {
    const del = document.createElement('button');
    del.className = 'btn btn-danger'; del.textContent = '鍒犻櫎';
    del.onclick = async () => {
      if (!confirm('Delete this snapshot?')) return;
      await apiFetch(`/snapshots/${snap.id}`, { method: 'DELETE' });
      closeModal();
      loadSnapshots();
    };
    const close = document.createElement('button');
    close.className = 'btn'; close.textContent = '鍏抽棴';
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

// 鈹€鈹€ Search 鈹€鈹€

async function runSearch() {
  const q = $('#search-input')?.value?.trim();
  if (!q) {
    if (currentTab === 'snapshots') await loadSnapshots();
    else await loadEvents();
    return;
  }
  const list = $('#data-list');
  list.innerHTML = '<div class="loading">鎼滅储涓€?/div>';
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
        list.innerHTML = '<div class="empty">鏈壘鍒板尮閰嶄簨浠?/div>';
      } else {
        list.innerHTML = latestEvents.map(e => {
          let kw = [];
          try { kw = JSON.parse(e.trigger_keywords || '[]'); } catch(ex) {}
          let categories = [];
          try { categories = JSON.parse(e.categories || '[]'); } catch(ex) {}
          return `
            <div class="list-item ${e.archived ? 'archived' : ''}" onclick='openEditEventModal(${JSON.stringify(e).replace(/'/g, "&#39;")})'>
              <span class="tag">${e.source}</span>
              ${e.archived ? '<span class="tag">宸插綊妗?/span>' : ''}
              ${e.title ? `<span class="tag">${escHtml(e.title)}</span>` : ''}
              <span class="list-meta">${e.date}</span>
              <div class="list-preview">${escHtml(truncate(e.description, 150))}</div>
              <div style="margin-top:4px">${categories.map(c => `<span class="tag" style="background:#3b3049;color:#d6c6ff">${escHtml(c)}</span>`).join('')}</div>
              <div style="margin-top:4px">${kw.map(k => `<span class="tag">${escHtml(k)}</span>`).join('')}</div>
            </div>
          `;
        }).join('');
      }
      showStatus(`Event search completed: ${latestEvents.length}`);
      return;
    }

    latestSnapshots = data.snapshots || [];
    if (!latestSnapshots.length) {
      list.innerHTML = '<div class="empty">鏈壘鍒板尮閰嶅揩鐓?/div>';
    } else {
      list.innerHTML = latestSnapshots.map(s => `
        <div class="list-item" onclick='showSnapshotDetail(${JSON.stringify(s).replace(/'/g, "&#39;")})'>
          <span class="tag">${s.type}</span>
          <span class="list-meta">${formatTime(s.created_at)}</span>
          <div class="list-preview">${escHtml(truncate(s.content, 150))}</div>
        </div>
      `).join('');
    }
    showStatus(`Snapshot search completed: ${latestSnapshots.length}`);
  } catch(e) { list.innerHTML = `<div style="color:var(--danger)">${escHtml(e.message)}</div>`; }
}

function toggleArchivedEvents() {
  const checkbox = $('#toggle-archived');
  showArchivedEvents = !!(checkbox && checkbox.checked);
  if (currentTab === 'events') loadEvents();
}

// 鈹€鈹€ Settings Page 鈹€鈹€

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
  prompt_snapshot_generation: 'Generate snapshot text from environment, previous snapshot, recent events and memory context.',
  prompt_event_anchor: 'Determine whether an event anchor should be recorded, then output title/description/keywords/categories.',
  prompt_reflect_snapshot: 'Reflect on conversation impact and produce a post-conversation snapshot in first person.',
  prompt_reflect_event: 'Summarize important conversation events and provide title/keywords/categories.',
  prompt_periodic_review: 'Generate a periodic review from events and snapshots.',
  prompt_event_scoring: 'Score events by importance and impression depth.',
  prompt_environment_generation: 'Generate concise environment context text from recent state and continuity hints.',
};

let currentSettingsTab = 'persona';
let DEFAULT_SETTINGS_MAP = {};

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
    DEFAULT_SETTINGS_MAP = data.defaults || {};
    const map = {};
    (data.items || []).forEach(item => { map[item.key] = item.value; });
    SETTINGS_KEYS.forEach(k => {
      let value = map[k];
      if (!value || !String(value).trim()) {
        value = DEFAULT_SETTINGS_MAP[k] || PROMPT_DEFAULT_SAMPLES[k] || '';
      }
      setInputValue(`setting-${k}`, value);
    });
    await loadEvolutionStatus();
    showStatus('Done');
  } catch (e) {
    showStatus('璁惧畾鍔犺浇澶辫触: ' + e.message, true);
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
    showStatus('Done');
    await loadEvolutionStatus();
  } catch (e) {
    showStatus('淇濆瓨澶辫触: ' + e.message, true);
  }
}

async function savePersonaSettings() {
  try {
    for (const key of PERSONA_SETTINGS_KEYS) {
      await saveSetting(key);
    }
    showStatus('Done');
    await loadEvolutionStatus();
  } catch (e) {
    showStatus('淇濆瓨澶辫触: ' + e.message, true);
  }
}

async function saveAllSettings() {
  try {
    for (const key of SETTINGS_KEYS) {
      await saveSetting(key);
    }
    showStatus('Done');
    await loadEvolutionStatus();
  } catch (e) {
    showStatus('淇濆瓨澶辫触: ' + e.message, true);
  }
}

async function resetAllSettings() {
  if (!confirm('纭灏嗗叏閮ㄨ瀹氭仮澶嶄负榛樿鍊硷紵')) return;
  try {
    for (const key of SETTINGS_KEYS) {
      await apiFetch(`/settings/reset/${encodeURIComponent(key)}`, { method: 'POST' });
    }
    showStatus('Done');
    await loadSettingsPage();
  } catch (e) {
    showStatus('鎭㈠榛樿澶辫触: ' + e.message, true);
  }
}

const BULK_IMPORT_TEMPLATE = {
  settings: {
    L1_character_background: "鍙€夛細L1瑙掕壊鑳屾櫙",
    L1_user_background: "鍙€夛細L1鐢ㄦ埛鑳屾櫙",
    L2_character_personality: "鍙€夛細L2瑙掕壊浜烘牸",
    L2_relationship_dynamics: "鍙€夛細L2鍏崇郴妯″紡"
  },
  snapshots: [
    {
      created_at: "2026-03-20T08:30:00",
      type: "accumulated",
      content: "绀轰緥蹇収鍐呭",
      environment: { summary: "绀轰緥鐜鎽樿" },
      referenced_events: [1, 2]
    }
  ],
  events: [
    {
      date: "2026-03-20",
      title: "绀轰緥浜嬩欢鏍囬",
      description: "绀轰緥浜嬩欢鎻忚堪",
      source: "manual",
      trigger_keywords: ["鍏抽敭璇?", "鍏抽敭璇?"],
      categories: ["鐢熸椿瓒宠抗"],
      archived: 0,
      importance_score: 4.2,
      impression_depth: 5.1
    }
  ],
  key_records: [
    {
      type: "important_item",
      title: "绀轰緥鍏抽敭璁板綍鏍囬",
      content_text: "绀轰緥鍏抽敭璁板綍姝ｆ枃",
      tags: ["绀轰緥鏍囩"],
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
  showStatus(`瀵煎叆妯℃澘宸蹭笅杞斤細${filename}`);
}

async function importBulkJson() {
  const fileEl = document.getElementById('bulk-import-file');
  const resultEl = document.getElementById('bulk-import-result');
  if (!fileEl || !fileEl.files || !fileEl.files[0]) {
    showStatus('璇峰厛閫夋嫨瑕佸鍏ョ殑 JSON 鏂囦欢', true);
    return;
  }
  const file = fileEl.files[0];
  let text = '';
  try {
    text = await file.text();
  } catch (e) {
    showStatus('璇诲彇鏂囦欢澶辫触: ' + e.message, true);
    return;
  }
  let payload;
  try {
    payload = JSON.parse(text);
  } catch (e) {
    showStatus('JSON 瑙ｆ瀽澶辫触: ' + e.message, true);
    if (resultEl) resultEl.textContent = ''
    return;
  }
  payload.overwrite_settings = !!document.getElementById('bulk-import-overwrite-settings')?.checked;
  payload.upsert_key_records = !!document.getElementById('bulk-import-upsert-key-records')?.checked;
  payload.sync_vectors_after_import = !!document.getElementById('bulk-import-sync-vectors')?.checked;

  if (!confirm('Confirm this action?')) return;
  if (resultEl) resultEl.textContent = '瀵煎叆鎵ц涓?..';
  showStatus('鎵归噺瀵煎叆鎵ц涓?..');
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
      `瀵煎叆瀹屾垚锛氳瀹?${settingImported}锛屽揩鐓?${snapshotImported}锛屼簨浠?${eventImported}锛屽叧閿褰?鏂板${keyCreated}/鏇存柊${keyUpdated}`
    );
    await Promise.all([loadSettingsPage(), loadDashboard()]);
  } catch (e) {
    if (resultEl) resultEl.textContent = String(e.message || e);
    showStatus('鎵归噺瀵煎叆澶辫触: ' + e.message, true);
  }
}

function applyPromptDefault(key) {
  const sample = DEFAULT_SETTINGS_MAP[key] || PROMPT_DEFAULT_SAMPLES[key];
  if (!sample) return;
  const el = document.getElementById(`setting-${key}`);
  if (!el) return;
  el.value = sample;
  showStatus(`宸插～鍏?${key} 鐨勯粯璁ょず渚嬶紙鏈繚瀛橈級`);
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
      <div>鏄惁寤鸿婕斿寲锛?{data.should_evolve ? '鏄? : '鍚?}</div>
      <div>鏂颁簨浠舵暟锛?{data.event_count} / 闃堝€硷細${data.threshold}</div>
      <div>涓婃婕斿寲鏃堕棿锛?{data.last_time || '灏氭湭杩涜'}</div>
    `;
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger)">${escHtml(e.message)}</div>`;
  }
}

async function previewEvolution() {
  const el = document.getElementById('evolution-preview');
  if (!el) return;
  el.innerHTML = '<div class="loading">姝ｅ湪鐢熸垚婕斿寲棰勮鈥?/div>';
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
        <div class="list-meta">閲嶈鎬?${Number(e.importance_score || 0).toFixed(1)} | 鍗拌薄娣卞害 ${Number(e.impression_depth || 0).toFixed(1)}</div>
      </div>
    `).join('');
    el.innerHTML = `
      <div class="card" style="margin-bottom:8px">
        <h2>婕斿寲鎽樿</h2>
        <div class="detail-content">${escHtml(data.change_summary || '')}</div>
      </div>
      <div class="card" style="margin-bottom:8px">
        <h2>L2 瑙掕壊浜烘牸 Diff 棰勮</h2>
        <div class="list-meta">鏃х増鏈?/div>
        <div class="detail-content">${escHtml(oldCharacter)}</div>
        <div class="list-meta" style="margin-top:8px">鏂扮増鏈?/div>
        <div class="detail-content">${escHtml(data.new_character_personality || '')}</div>
      </div>
      <div class="card" style="margin-bottom:8px">
        <h2>L2 鍏崇郴妯″紡 Diff 棰勮</h2>
        <div class="list-meta">鏃х増鏈?/div>
        <div class="detail-content">${escHtml(oldRelationship)}</div>
        <div class="list-meta" style="margin-top:8px">鏂扮増鏈?/div>
        <div class="detail-content">${escHtml(data.new_relationship_dynamics || '')}</div>
      </div>
      <div class="card">
        <h2>浜嬩欢璇勫垎 Top10</h2>
        ${eventHtml || '<div class="empty">鏆傛棤璇勫垎浜嬩欢</div>'}
      </div>
    `;
    showStatus('Done');
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger)">${escHtml(e.message)}</div>`;
    showStatus('婕斿寲棰勮澶辫触: ' + e.message, true);
  }
}

async function applyEvolution() {
  if (!latestEvolutionPreview) {
    alert('Action required.');
    return;
  }
  if (!confirm('Confirm this action?')) return;
  try {
    const data = await apiFetch('/evolution/apply', {
      method: 'POST',
      body: JSON.stringify({ preview: latestEvolutionPreview }),
    });
    showStatus(`Evolution applied, archived events: ${data.archived_count || 0}`);
    await loadSettingsPage();
  } catch (e) {
    showStatus('婕斿寲搴旂敤澶辫触: ' + e.message, true);
  }
}

async function recalculateArchiveStatus() {
  const startDate = (document.getElementById('recalc-start-date')?.value || '').trim();
  const endDate = (document.getElementById('recalc-end-date')?.value || '').trim();
  if (startDate && endDate && startDate > endDate) {
    alert('Action required.');
    return;
  }

  const hasDateRange = !!(startDate || endDate);
  const rangeText = hasDateRange
    ? `Date range ${startDate || 'min'} ~ ${endDate || 'max'}`
    : 'Full range';

  if (!confirm(`Recalculate archive status with current threshold? (${rangeText})`)) {
    return;
  }
  if (!confirm('璇峰啀娆＄‘璁わ細杩欎細鎵归噺鏇存柊鍘嗗彶浜嬩欢鐨?archived 鐘舵€併€傜户缁墽琛岋紵')) {
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
      `閲嶇畻瀹屾垚锛?{rangeText}锛夛細瑙ｅ綊妗?${data.unarchived_count || 0}锛屽綊妗?${data.archived_count || 0}`
    );
    alert(
      `閲嶇畻褰掓。鐘舵€佸畬鎴怽n` +
      `鑼冨洿锛?{rangeText}\n` +
      `瑙ｅ綊妗ｏ細${data.unarchived_count || 0}\n` +
      `褰掓。锛?{data.archived_count || 0}\n` +
      `鎬绘壂鎻忥細${data.scanned_count || 0}\n` +
      `璺宠繃鏈瘎鍒嗭細${data.skipped_unscored_count || 0}`
    );
    if (typeof loadEvolutionStatus === 'function') {
      await loadEvolutionStatus();
    }
  } catch (e) {
    showStatus('閲嶇畻褰掓。鐘舵€佸け璐? ' + e.message, true);
  }
}

// 鈹€鈹€ Vector Management Page 鈹€鈹€

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
    showStatus('鍚戦噺閰嶇疆鍔犺浇澶辫触: ' + e.message, true);
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
    showStatus('Done');
    await loadVectorSettings();
  } catch (e) {
    showStatus('鍚戦噺閰嶇疆淇濆瓨澶辫触: ' + e.message, true);
  }
}

async function loadVectorStats() {
  const el = document.getElementById('vector-stats');
  if (!el) return;
  el.innerHTML = '<div class="loading">鍔犺浇涓€?/div>';
  try {
    const data = await apiFetch('/vectors/stats');
    const stats = data.stats || {};
    const bySource = stats.by_source || {};
    el.innerHTML = `
      <div class="list-meta">鎬绘潯鐩細${Number(stats.total || 0)}</div>
      <div class="list-meta">娲昏穬鏉＄洰锛?{Number(stats.active || 0)}</div>
      <div class="list-meta">宸插垹闄ゆ潯鐩細${Number(stats.deleted || 0)}</div>
      <div class="list-meta">鎸夋潵婧愮粺璁★細浜嬩欢 ${Number(bySource.event || 0)} / 蹇収 ${Number(bySource.snapshot || 0)}</div>
    `;
  } catch (e) {
    el.innerHTML = `<div style="color:var(--danger)">${escHtml(e.message)}</div>`;
  }
}

async function runVectorSync(reindex = false) {
  const action = reindex ? '閲嶅缓绱㈠紩' : '鍚屾鍚戦噺';
  if (reindex && !confirm('Rebuild all vectors? This will recreate existing vector entries.')) return;
  showStatus(`${action}鎵ц涓?..`);
  try {
    const data = await apiFetch('/vectors/sync', {
      method: 'POST',
      body: JSON.stringify({ reindex }),
    });
    const result = data.result || {};
    showStatus(
      `${action} completed: events ${Number(result.vectorized_events || 0)}, snapshots ${Number(result.vectorized_snapshots || 0)}`
    );
    await loadVectorStats();
    await loadVectorEntries();
  } catch (e) {
    showStatus(`${action}澶辫触: ` + e.message, true);
  }
}

async function runVectorCompaction(dryRun = false) {
  if (!dryRun && !confirm('Run cold-memory compaction now?')) return;
  const action = dryRun ? '鍘嬬缉棰勮' : ''
  showStatus(`${action}鎵ц涓?..`);
  try {
    const data = await apiFetch('/vectors/compact', {
      method: 'POST',
      body: JSON.stringify({ dry_run: dryRun }),
    });
    const result = data.result || {};
    if (dryRun) {
      showStatus(
        `棰勮瀹屾垚锛氬€欓€?${Number(result.candidate_count || 0)}锛屽垎缁?${Number(result.group_count || 0)}锛屽彲鍘嬬缉 ${Number(result.would_compact_count || 0)}`
      );
      return;
    }
    showStatus(
      `鍘嬬缉瀹屾垚锛氭柊澧炴憳瑕?${Number(result.created_summaries || 0)}锛屽垹闄ゅ師鍚戦噺 ${Number(result.deleted_originals || 0)}`
    );
    await loadVectorStats();
    await loadVectorEntries();
  } catch (e) {
    showStatus(`${action}澶辫触: ` + e.message, true);
  }
}

async function loadVectorEntries() {
  const list = document.getElementById('vector-entry-list');
  if (!list) return;
  const params = buildVectorFilterParams(100);
  list.innerHTML = '<div class="loading">加载中...</div>';
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
  if (!vectorVisibleEntryIds.length) return;
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
  if (!confirm(`纭鍒犻櫎鍚戦噺鏉＄洰 ${entryId} ?`)) return;
  try {
    await apiFetch(`/vectors/entries/${encodeURIComponent(entryId)}`, {
      method: 'DELETE',
    });
    vectorSelectedEntryIds.delete(String(entryId || '').trim());
    showStatus(`宸插垹闄ゅ悜閲忔潯鐩?${entryId}`);
    await loadVectorStats();
    await loadVectorEntries();
  } catch (e) {
    showStatus('鍒犻櫎鍚戦噺澶辫触: ' + e.message, true);
  }
}






