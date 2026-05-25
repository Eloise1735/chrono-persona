function formatScheduleBulkEditorValue(items) {
  const normalized = (Array.isArray(items) ? items : []).map((item) => {
    let actionPayload = {};
    try {
      actionPayload = JSON.parse(item.action_payload || '{}');
    } catch (e) {
      actionPayload = {};
    }
    return {
      hour_start: Number(item.hour_start || 0),
      hour_end: Number(item.hour_end || 1),
      activity: String(item.activity || ''),
      action_type: String(item.action_type || 'internal'),
      action_payload: actionPayload,
      status: String(item.status || 'pending'),
      outcome: String(item.outcome || ''),
      source_kind: String(item.source_kind || 'generated'),
      source_ref_id: item.source_ref_id == null ? null : Number(item.source_ref_id),
      executed_at: item.executed_at || null,
    };
  });
  return JSON.stringify({ items: normalized }, null, 2);
}

function renderScheduleTimeline(items) {
  if (!Array.isArray(items) || !items.length) return '<div class="empty">计划存在，但暂无条目</div>';
  return items.map((item) => `
      <div class="history-item">
        <div class="history-title">${escHtml(String(item.hour_start).padStart(2, '0'))}:00 - ${escHtml(String(item.hour_end).padStart(2, '0'))}:00</div>
        <div class="list-meta">类型：${escHtml(item.action_type || 'internal')} | 状态：${escHtml(item.status || '')}</div>
        <div class="list-meta">${escHtml(formatPlanSource(item))}</div>
        <div class="detail-content">${escHtml(item.activity || '')}</div>
        <div class="list-meta" style="margin-top:8px;">结果：${escHtml(item.outcome || '（暂无）')}</div>
        <div class="btn-group" style="margin-top:8px;">
          <button class="btn" onclick="openPlanItemEditor(${Number(item.id)})">查看 / 编辑</button>
        </div>
      </div>
    `).join('');
}

function renderScheduleSummary(plan, baselinePlanId, items) {
  const wrap = document.getElementById('schedule-summary');
  const editor = document.getElementById('schedule-bulk-editor');
  if (!wrap) return;
  if (!plan) {
    wrap.innerHTML = '<div class="empty">今日尚无计划</div>';
    if (editor) editor.value = '';
    return;
  }
  const itemCount = Array.isArray(items) ? items.length : 0;
  wrap.innerHTML = `
    <div class="list-meta">日期：${escHtml(plan.plan_date || '')}</div>
    <div class="list-meta">状态：${escHtml(plan.status || '')}</div>
    <div class="list-meta">生成时间：${escHtml(formatTime(plan.generated_at || plan.created_at || ''))}</div>
    <div class="list-meta">计划 ID：${Number(plan.id || 0)}</div>
    <div class="list-meta">重规划触发：${escHtml(plan.replan_trigger || '—')} | 父计划：${plan.replan_parent_id == null ? '—' : Number(plan.replan_parent_id)}</div>
    <div class="list-meta">当前 baseline 计划：${baselinePlanId > 0 ? baselinePlanId : '未设置'} | 条目数：${itemCount}</div>
    <div class="btn-group" style="margin-top:12px;">
      <button class="btn btn-primary" onclick="usePlanAsBaseline(${Number(plan.id || 0)})">设为新的 baseline</button>
      <button class="btn btn-danger" onclick="deletePlan(${Number(plan.id || 0)})">删除当前计划</button>
    </div>
  `;
  if (editor) editor.value = formatScheduleBulkEditorValue(items);
}

function renderScheduleHistoryList(items) {
  if (!Array.isArray(items) || !items.length) return '<div class="empty">暂无计划历史</div>';
  return items.map((item) => {
    const planId = Number(item.id || 0);
    const checked = selectedSchedulePlanIds.has(planId);
    return `
      <details class="history-item ${checked ? 'selected' : ''}" data-schedule-plan-id="${planId}" ${scheduleHistoryExpanded ? 'open' : ''}>
        <summary style="cursor:pointer; list-style:none;">
          <div class="history-title" style="display:inline-block;">${escHtml(item.plan_date || '')}</div>
          <div class="list-meta">计划 ID：${planId} | 状态：${escHtml(item.status || '')}</div>
        </summary>
        <div style="margin-top:10px;">
          <div class="list-meta">生成：${escHtml(formatTime(item.generated_at || item.created_at || ''))} | 触发：${escHtml(item.replan_trigger || '—')}</div>
          ${scheduleHistoryManageMode ? `
            <label class="checkbox-label" style="display:flex;align-items:center;gap:8px;margin-top:8px;cursor:pointer">
              <input
                type="checkbox"
                class="schedule-history-checkbox"
                ${checked ? 'checked' : ''}
                onchange="toggleScheduleHistoryItemSelection(${planId}, this.checked)"
              >
              勾选此计划
            </label>
          ` : ''}
          <div class="btn-group" style="margin-top:8px;">
            <button class="btn" onclick="loadSpecificPlan(${planId})">查看详情</button>
            <button class="btn btn-danger" onclick="deletePlan(${planId})">删除</button>
          </div>
        </div>
      </details>
    `;
  }).join('');
}

function setScheduleHistoryExpanded(expanded) {
  scheduleHistoryExpanded = !!expanded;
  loadPlanHistory();
}

async function initSchedulePage() {
  scheduleHistoryManageMode = false;
  scheduleHistoryExpanded = false;
  selectedSchedulePlanIds.clear();
  updateScheduleHistorySelectionSummary();
  await loadSchedulePage();
  await loadPlanHistory();
}

async function loadSchedulePage() {
  try {
    const data = await apiFetch('/plans/today');
    const timeline = document.getElementById('schedule-timeline');
    const plan = data.plan;
    const baselinePlanId = Number(data.baseline_plan_id || 0);
    const items = Array.isArray(data.items) ? data.items : [];
    currentSchedulePlanId = plan ? Number(plan.id || 0) : null;
    currentScheduleItems = items;
    renderScheduleSummary(plan, baselinePlanId, items);
    if (timeline) timeline.innerHTML = renderScheduleTimeline(items);
    if (items.length && typeof openPlanItemEditor === 'function') openPlanItemEditor(Number(items[0].id));
    await loadNotificationsForSchedule();
  } catch (e) {
    showStatus(`加载日程失败：${e.message}`, true);
  }
}

async function loadPlanHistory() {
  const wrap = document.getElementById('schedule-history');
  if (!wrap) return;
  try {
    const data = await apiFetch('/plans/history?limit=20');
    latestPlanHistoryItems = Array.isArray(data.items) ? data.items : [];
    wrap.innerHTML = renderScheduleHistoryList(latestPlanHistoryItems);
    updateScheduleHistorySelectionSummary();
  } catch (e) {
    wrap.innerHTML = `<div style="color:var(--danger)">计划历史加载失败：${escHtml(e.message)}</div>`;
  }
}

async function loadSpecificPlan(planId) {
  try {
    const data = await apiFetch(`/plans/${planId}`);
    const plan = data.plan;
    const items = Array.isArray(data.items) ? data.items : [];
    currentSchedulePlanId = plan ? Number(plan.id || 0) : null;
    currentScheduleItems = items;
    renderScheduleSummary(plan, 0, items);
    const timeline = document.getElementById('schedule-timeline');
    if (timeline) timeline.innerHTML = renderScheduleTimeline(items);
    if (items.length && typeof openPlanItemEditor === 'function') openPlanItemEditor(Number(items[0].id));
  } catch (e) {
    showStatus(`加载计划详情失败：${e.message}`, true);
  }
}

async function savePlanItem(itemId) {
  try {
    const payload = JSON.parse(document.getElementById('plan-item-payload').value || '{}');
    await apiFetch(`/plans/items/${itemId}`, {
      method: 'PUT',
      body: JSON.stringify({
        hour_start: Number(document.getElementById('plan-item-hour-start').value),
        hour_end: Number(document.getElementById('plan-item-hour-end').value),
        action_type: document.getElementById('plan-item-action-type').value,
        activity: document.getElementById('plan-item-activity').value,
        action_payload: payload,
        status: document.getElementById('plan-item-status').value,
        source_kind: document.getElementById('plan-item-source-kind').value,
        source_ref_id: document.getElementById('plan-item-source-ref-id').value
          ? Number(document.getElementById('plan-item-source-ref-id').value)
          : null,
        outcome: document.getElementById('plan-item-outcome').value,
      }),
    });
    showStatus('计划项已保存');
    if (currentSchedulePlanId) await loadSpecificPlan(currentSchedulePlanId);
    else await loadSchedulePage();
  } catch (e) {
    showStatus(`保存计划项失败：${e.message}`, true);
  }
}

async function saveCurrentPlanBulkEditor() {
  if (!currentSchedulePlanId) {
    showStatus('当前没有可编辑的计划', true);
    return;
  }
  const editor = document.getElementById('schedule-bulk-editor');
  const raw = String(editor?.value || '').trim();
  if (!raw) {
    showStatus('完整日程编辑器不能为空', true);
    return;
  }
  try {
    const parsed = JSON.parse(raw);
    const items = Array.isArray(parsed) ? parsed : parsed.items;
    if (!Array.isArray(items)) {
      throw new Error('JSON 需为数组，或包含 items 数组');
    }
    const payload = {
      items: items.map((item) => ({
        hour_start: Number(item.hour_start),
        hour_end: Number(item.hour_end),
        activity: String(item.activity || ''),
        action_type: String(item.action_type || 'internal'),
        action_payload: item.action_payload && typeof item.action_payload === 'object' ? item.action_payload : {},
        status: String(item.status || 'pending'),
        outcome: String(item.outcome || ''),
        source_kind: String(item.source_kind || 'generated'),
        source_ref_id: item.source_ref_id == null || item.source_ref_id === '' ? null : Number(item.source_ref_id),
        executed_at: item.executed_at || null,
      })),
    };
    await apiFetch(`/plans/${currentSchedulePlanId}/bulk-edit`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    showStatus('完整日程已保存');
    await loadSpecificPlan(currentSchedulePlanId);
    await loadPlanHistory();
  } catch (e) {
    showStatus(`保存完整日程失败：${e.message}`, true);
  }
}

function resetCurrentPlanBulkEditor() {
  const editor = document.getElementById('schedule-bulk-editor');
  if (!editor) return;
  editor.value = formatScheduleBulkEditorValue(currentScheduleItems);
  showStatus('已重置为当前日程内容');
}
