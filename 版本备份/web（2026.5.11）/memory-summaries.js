const memorySummaryLevels = ['day', 'week', 'month', 'year'];
const latestMemorySummaryPreview = {
  day: null,
  week: null,
  month: null,
  year: null,
};

function renderDaySummaryColumn(level, status, active) {
  const relationshipText = JSON.stringify(active?.relationship_lines || [], null, 2);
  const lifeText = JSON.stringify(active?.life_lines || [], null, 2);
  const carryText = JSON.stringify(active?.carry_forward_points || [], null, 2);
  const sourceEventsText = JSON.stringify(active?.source_events_resolved || [], null, 2);
  const sourceLifeText = JSON.stringify(active?.source_life_nodes_resolved || [], null, 2);
  return `
    <div class="card">
      <h2>日摘要</h2>
      <div class="list-meta">已应用：${escHtml(status?.latest_applied_window_end || '暂无')} | 待确认预览：${status?.has_pending_preview ? '有' : '无'}</div>
      <div class="btn-group" style="margin-top:10px;">
        <button class="btn" onclick="previewMemorySummary('${level}')">生成预览</button>
        <button class="btn" onclick="loadPendingMemorySummary('${level}')">读取待确认预览</button>
        <button class="btn btn-primary" onclick="applyMemorySummary('${level}')">应用当前预览</button>
      </div>
      <div class="form-group" style="margin-top:12px;">
        <label>关系线</label>
        <textarea id="memory-day-relationship-${level}" style="min-height:120px;">${escHtml(relationshipText)}</textarea>
      </div>
      <div class="form-group">
        <label>生活线</label>
        <textarea id="memory-day-life-${level}" style="min-height:120px;">${escHtml(lifeText)}</textarea>
      </div>
      <div class="form-group">
        <label>计划与现实</label>
        <textarea id="memory-day-plan-${level}" style="min-height:100px;">${escHtml(active?.plan_vs_reality || '')}</textarea>
      </div>
      <div class="form-group">
        <label>延续点</label>
        <textarea id="memory-day-carry-${level}" style="min-height:120px;">${escHtml(carryText)}</textarea>
      </div>
      <details class="card-section">
        <summary>来源关系事件</summary>
        <textarea readonly style="min-height:120px;">${escHtml(sourceEventsText)}</textarea>
      </details>
      <details class="card-section">
        <summary>来源生活节点</summary>
        <textarea readonly style="min-height:120px;">${escHtml(sourceLifeText)}</textarea>
      </details>
    </div>
  `;
}

function renderHierarchicalSummaryColumn(level, title, status, active) {
  const bridgePackText = JSON.stringify(active?.bridge_pack || [], null, 2);
  const digestText = JSON.stringify(active?.event_line_digest || [], null, 2);
  const summaryPackText = JSON.stringify(active?.summary_pack || [], null, 2);
  const sourceEventsText = JSON.stringify(active?.source_events_resolved || [], null, 2);
  const sourceSummariesText = JSON.stringify(active?.source_summary_runs_resolved || [], null, 2);
  const sourceLifeText = JSON.stringify(active?.source_life_nodes_resolved || [], null, 2);
  return `
    <div class="card">
      <h2>${title}</h2>
      <div class="list-meta">已应用：${escHtml(status?.latest_applied_window_end || '暂无')} | 待确认预览：${status?.has_pending_preview ? '有' : '无'}</div>
      <div class="btn-group" style="margin-top:10px;">
        <button class="btn" onclick="previewMemorySummary('${level}')">生成预览</button>
        <button class="btn" onclick="loadPendingMemorySummary('${level}')">读取待确认预览</button>
        <button class="btn btn-primary" onclick="applyMemorySummary('${level}')">应用当前预览</button>
      </div>
      <div class="form-group" style="margin-top:12px;">
        <label>摘要包</label>
        <textarea id="memory-summary-pack-${level}" style="min-height:180px;">${escHtml(summaryPackText)}</textarea>
      </div>
      <div class="form-group">
        <label>桥层包</label>
        <textarea id="memory-bridge-pack-${level}" style="min-height:180px;">${escHtml(bridgePackText)}</textarea>
      </div>
      <div class="form-group">
        <label>线索摘要</label>
        <textarea id="memory-event-digest-${level}" style="min-height:160px;">${escHtml(digestText)}</textarea>
      </div>
      <details class="card-section">
        <summary>来源事件</summary>
        <textarea readonly style="min-height:120px;">${escHtml(sourceEventsText)}</textarea>
      </details>
      <details class="card-section">
        <summary>来源下层摘要</summary>
        <textarea readonly style="min-height:120px;">${escHtml(sourceSummariesText)}</textarea>
      </details>
      <details class="card-section">
        <summary>来源生活节点</summary>
        <textarea readonly style="min-height:120px;">${escHtml(sourceLifeText)}</textarea>
      </details>
    </div>
  `;
}

function renderMemorySummaryColumn(level, status, runs) {
  const titleMap = { week: '周摘要', month: '月摘要', year: '年摘要' };
  const latestApplied = (runs || []).find(item => item.status === 'applied') || null;
  const latestPreview = (runs || []).find(item => item.status === 'preview') || null;
  const active = latestMemorySummaryPreview[level] || latestPreview || latestApplied;
  if (level === 'day') {
    return renderDaySummaryColumn(level, status, active);
  }
  return renderHierarchicalSummaryColumn(level, titleMap[level], status, active);
}

async function initMemorySummariesPage() {
  const container = document.getElementById('memory-summary-columns');
  if (!container) return;
  const [dayStatus, weekStatus, monthStatus, yearStatus, dayRuns, weekRuns, monthRuns, yearRuns] = await Promise.all([
    apiFetch('/memory-summaries/day/status'),
    apiFetch('/memory-summaries/week/status'),
    apiFetch('/memory-summaries/month/status'),
    apiFetch('/memory-summaries/year/status'),
    apiFetch('/memory-summaries/day/runs?limit=8'),
    apiFetch('/memory-summaries/week/runs?limit=8'),
    apiFetch('/memory-summaries/month/runs?limit=8'),
    apiFetch('/memory-summaries/year/runs?limit=8'),
  ]);
  container.innerHTML = [
    renderMemorySummaryColumn('day', dayStatus, dayRuns),
    renderMemorySummaryColumn('week', weekStatus, weekRuns),
    renderMemorySummaryColumn('month', monthStatus, monthRuns),
    renderMemorySummaryColumn('year', yearStatus, yearRuns),
  ].join('');
}

async function previewMemorySummary(level) {
  try {
    const data = await apiFetch('/memory-summaries/preview', {
      method: 'POST',
      body: JSON.stringify({ summary_level: level, store_pending: true }),
    });
    latestMemorySummaryPreview[level] = data;
    showStatus(`${level} 摘要预览已生成`);
    await initMemorySummariesPage();
  } catch (e) {
    showStatus(`${level} 摘要预览失败: ${e.message}`, true);
  }
}

async function loadPendingMemorySummary(level) {
  try {
    const data = await apiFetch(`/memory-summaries/${level}/pending-preview`);
    latestMemorySummaryPreview[level] = data;
    showStatus(`${level} 待确认预览已读取`);
    await initMemorySummariesPage();
  } catch (e) {
    showStatus(`${level} 读取预览失败: ${e.message}`, true);
  }
}

function syncMemorySummaryPreviewFromEditor(level) {
  const base = latestMemorySummaryPreview[level];
  if (!base) return null;
  if (level === 'day') {
    let relationshipLines = base.relationship_lines || [];
    let lifeLines = base.life_lines || [];
    let carryForwardPoints = base.carry_forward_points || [];
    let planVsReality = base.plan_vs_reality || '';
    try { relationshipLines = JSON.parse(document.getElementById(`memory-day-relationship-${level}`)?.value || '[]'); } catch (_) {}
    try { lifeLines = JSON.parse(document.getElementById(`memory-day-life-${level}`)?.value || '[]'); } catch (_) {}
    try { carryForwardPoints = JSON.parse(document.getElementById(`memory-day-carry-${level}`)?.value || '[]'); } catch (_) {}
    planVsReality = document.getElementById(`memory-day-plan-${level}`)?.value || '';
    latestMemorySummaryPreview[level] = {
      ...base,
      relationship_lines: Array.isArray(relationshipLines) ? relationshipLines : [],
      life_lines: Array.isArray(lifeLines) ? lifeLines : [],
      carry_forward_points: Array.isArray(carryForwardPoints) ? carryForwardPoints : [],
      plan_vs_reality: planVsReality,
    };
    return latestMemorySummaryPreview[level];
  }
  let summaryPack = base.summary_pack || [];
  let bridgePack = base.bridge_pack || [];
  let digest = base.event_line_digest || [];
  try { summaryPack = JSON.parse(document.getElementById(`memory-summary-pack-${level}`)?.value || '[]'); } catch (_) {}
  try { bridgePack = JSON.parse(document.getElementById(`memory-bridge-pack-${level}`)?.value || '[]'); } catch (_) {}
  try { digest = JSON.parse(document.getElementById(`memory-event-digest-${level}`)?.value || '[]'); } catch (_) {}
  latestMemorySummaryPreview[level] = {
    ...base,
    summary_pack: Array.isArray(summaryPack) ? summaryPack : [],
    bridge_pack: Array.isArray(bridgePack) ? bridgePack : [],
    event_line_digest: Array.isArray(digest) ? digest : [],
  };
  return latestMemorySummaryPreview[level];
}

async function applyMemorySummary(level) {
  const preview = syncMemorySummaryPreviewFromEditor(level) || latestMemorySummaryPreview[level];
  if (!preview) {
    alert('请先生成或读取一个摘要预览。');
    return;
  }
  try {
    await apiFetch(`/memory-summaries/${level}/apply`, {
      method: 'POST',
      body: JSON.stringify({ summary_level: level, preview }),
    });
    showStatus(`${level} 摘要已应用`);
    await initMemorySummariesPage();
  } catch (e) {
    showStatus(`${level} 摘要应用失败: ${e.message}`, true);
  }
}
