function getEventScoreFilterState() {
  const maxImportanceRaw = (document.getElementById('event-max-importance-filter')?.value || '').trim();
  const maxImpressionRaw = (document.getElementById('event-max-impression-filter')?.value || '').trim();
  const scoredOnly = !!document.getElementById('event-scored-only-filter')?.checked;
  const maxImportance = maxImportanceRaw === '' ? null : Number(maxImportanceRaw);
  const maxImpression = maxImpressionRaw === '' ? null : Number(maxImpressionRaw);
  return {
    scoredOnly,
    maxImportance: Number.isFinite(maxImportance) ? maxImportance : null,
    maxImpression: Number.isFinite(maxImpression) ? maxImpression : null,
  };
}

function applyEventScoreFilter() {
  eventsPageOffset = 0;
  loadEvents();
}

function clearEventScoreFilter() {
  const maxImportanceInput = document.getElementById('event-max-importance-filter');
  const maxImpressionInput = document.getElementById('event-max-impression-filter');
  const scoredOnlyInput = document.getElementById('event-scored-only-filter');
  if (maxImportanceInput) maxImportanceInput.value = '';
  if (maxImpressionInput) maxImpressionInput.value = '';
  if (scoredOnlyInput) scoredOnlyInput.checked = false;
  eventsPageOffset = 0;
  loadEvents();
}

function updateEventsPaginationSummary() {
  const el = document.getElementById('events-pagination-summary');
  if (!el) return;
  if (!eventsPageTotal) {
    el.textContent = '当前没有可显示的事件。';
    return;
  }
  const start = eventsPageOffset + 1;
  const end = Math.min(eventsPageOffset + latestEvents.length, eventsPageTotal);
  const page = Math.floor(eventsPageOffset / eventsPageLimit) + 1;
  const totalPages = Math.max(1, Math.ceil(eventsPageTotal / eventsPageLimit));
  el.textContent = `第 ${page}/${totalPages} 页，显示 ${start}-${end} / 共 ${eventsPageTotal} 条`;
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
  el.textContent = `已开启管理模式：当前${currentTab === 'snapshots' ? '快照' : '事件'}已选 ${currentCount} 条`;
}

function initEventsHistoryPage() {
  currentTab = 'events';
  historyManageMode = false;
  eventsPageOffset = 0;
  const container = document.getElementById('event-category-filter');
  if (container && !container.children.length) {
    EVENT_CATEGORY_OPTIONS.forEach((category, index) => {
      const item = document.createElement('label');
      item.className = 'category-filter-item';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.value = category;
      input.id = `event-cat-${index}`;
      input.onchange = onEventCategoryFilterChange;
      const text = document.createElement('span');
      text.textContent = category;
      item.appendChild(input);
      item.appendChild(text);
      container.appendChild(item);
    });
  }
  updateHistorySelectionSummary();
  loadEvents();
}

async function loadEvents() {
  const list = $('#data-list');
  if (!list) return;
  list.innerHTML = '<div class="loading">加载中...</div>';
  try {
    const selectedCategories = getSelectedEventCategories();
    const params = new URLSearchParams();
    params.set('limit', String(eventsPageLimit));
    params.set('offset', String(eventsPageOffset));
    params.set('include_archived', String(showArchivedEvents));
    if (selectedCategories.length) params.set('categories', selectedCategories.join(','));

    const sourceFilter = (document.getElementById('event-source-filter')?.value || '').trim();
    if (sourceFilter) params.set('sources', sourceFilter);

    const scoreFilter = getEventScoreFilterState();
    if (scoreFilter.scoredOnly) params.set('scored_only', 'true');
    if (scoreFilter.maxImportance !== null) {
      params.set('max_importance_score', String(scoreFilter.maxImportance));
    }
    if (scoreFilter.maxImpression !== null) {
      params.set('max_impression_depth', String(scoreFilter.maxImpression));
    }

    const data = await apiFetch(`/events?${params.toString()}`);
    latestEvents = Array.isArray(data.items) ? data.items : [];
    eventsPageTotal = Number(data.total || 0);

    if (!latestEvents.length && eventsPageOffset > 0) {
      eventsPageOffset = Math.max(0, eventsPageOffset - eventsPageLimit);
      return await loadEvents();
    }
    if (!latestEvents.length) {
      list.innerHTML = '<div class="empty">暂无符合条件的事件记录</div>';
      updateEventsPaginationSummary();
      updateHistorySelectionSummary();
      return;
    }

    list.innerHTML = renderEventHistoryList(latestEvents);
    updateEventsPaginationSummary();
    updateHistorySelectionSummary();

    const scoreParts = [];
    if (scoreFilter.maxImportance !== null) scoreParts.push(`重要性<=${scoreFilter.maxImportance}`);
    if (scoreFilter.maxImpression !== null) scoreParts.push(`印象<=${scoreFilter.maxImpression}`);
    if (scoreFilter.scoredOnly) scoreParts.push('仅已评分');
    const suffix = scoreParts.length ? `；评分筛选：${scoreParts.join(' / ')}` : '';
    showStatus(`已加载 ${latestEvents.length} 条事件（共 ${eventsPageTotal} 条）${suffix}`);
  } catch (e) {
    list.innerHTML = `<div style="color:var(--danger)">加载失败: ${escHtml(e.message)}</div>`;
    const el = document.getElementById('events-pagination-summary');
    if (el) el.textContent = '分页信息加载失败';
  }
}

async function deleteEventsByCurrentScoreFilter() {
  const sourceFilter = (document.getElementById('event-source-filter')?.value || '').trim();
  const selectedCategories = getSelectedEventCategories();
  const scoreFilter = getEventScoreFilterState();
  if (scoreFilter.maxImportance === null && scoreFilter.maxImpression === null) {
    showStatus('请先设置至少一个评分上限，再执行批量删除。', true);
    return;
  }

  const confirmText = [
    '确认删除当前评分筛选结果吗？此操作不可撤销。',
    scoreFilter.maxImportance !== null ? `重要性 <= ${scoreFilter.maxImportance}` : null,
    scoreFilter.maxImpression !== null ? `印象深度 <= ${scoreFilter.maxImpression}` : null,
    sourceFilter ? `来源 = ${sourceFilter}` : null,
    selectedCategories.length ? `分类 = ${selectedCategories.join(', ')}` : null,
    scoreFilter.scoredOnly ? '仅删除已评分事件' : null,
  ].filter(Boolean).join('\n');
  if (!confirm(confirmText)) return;

  try {
    const payload = {
      include_archived: !!showArchivedEvents,
      categories: selectedCategories,
      sources: sourceFilter ? [sourceFilter] : [],
      scored_only: scoreFilter.scoredOnly,
      max_importance_score: scoreFilter.maxImportance,
      max_impression_depth: scoreFilter.maxImpression,
    };
    const data = await apiFetch('/events/delete-by-score', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    selectedEventIds.clear();
    eventsPageOffset = 0;
    await loadEvents();
    showStatus(`已删除 ${Number(data.deleted || 0)} 条低分事件`);
  } catch (e) {
    showStatus(`按评分批量删除失败: ${e.message}`, true);
  }
}

async function runSearch() {
  const q = $('#search-input')?.value?.trim();
  if (!q) {
    if (currentTab === 'snapshots') await loadSnapshots();
    else await loadEvents();
    return;
  }

  const list = $('#data-list');
  list.innerHTML = '<div class="loading">搜索中...</div>';
  if (historyManageMode) {
    historyManageMode = false;
    updateHistorySelectionSummary();
  }

  try {
    const data = await apiFetch(`/search?q=${encodeURIComponent(q)}&limit=200&include_archived=true`);
    if (currentTab === 'events') {
      latestEvents = (data.events || []).filter((event) => showArchivedEvents || !event.archived);

      const selectedCategories = getSelectedEventCategories();
      if (selectedCategories.length) {
        latestEvents = latestEvents.filter((event) => {
          let categories = [];
          try {
            categories = JSON.parse(event.categories || '[]');
          } catch (ex) {}
          return selectedCategories.some((category) => categories.includes(category));
        });
      }

      const sourceFilter = (document.getElementById('event-source-filter')?.value || '').trim();
      if (sourceFilter) {
        latestEvents = latestEvents.filter((event) => String(event.source || '') === sourceFilter);
      }

      const scoreFilter = getEventScoreFilterState();
      if (scoreFilter.scoredOnly) {
        latestEvents = latestEvents.filter((event) =>
          event.importance_score !== null &&
          event.importance_score !== undefined &&
          event.impression_depth !== null &&
          event.impression_depth !== undefined
        );
      }
      if (scoreFilter.maxImportance !== null) {
        latestEvents = latestEvents.filter((event) => Number(event.importance_score ?? 999999) <= scoreFilter.maxImportance);
      }
      if (scoreFilter.maxImpression !== null) {
        latestEvents = latestEvents.filter((event) => Number(event.impression_depth ?? 999999) <= scoreFilter.maxImpression);
      }

      eventsPageTotal = latestEvents.length;
      eventsPageOffset = 0;
      if (!latestEvents.length) {
        list.innerHTML = '<div class="empty">未找到匹配事件</div>';
      } else {
        list.innerHTML = renderEventHistoryList(latestEvents);
      }
      updateEventsPaginationSummary();
      updateHistorySelectionSummary();
      showStatus(`事件搜索完成，共 ${latestEvents.length} 条`);
      return;
    }

    latestSnapshots = data.snapshots || [];
    if (!latestSnapshots.length) {
      list.innerHTML = '<div class="empty">未找到匹配快照</div>';
    } else {
      list.innerHTML = latestSnapshots.map((snapshot) => `
        <div class="list-item" onclick='showSnapshotDetail(${JSON.stringify(snapshot).replace(/'/g, '&#39;')})'>
          <span class="tag">${snapshot.type}</span>
          <span class="list-meta">${snapshotTimeLineText(snapshot)}</span>
          <div class="list-preview">${escHtml(truncate(snapshot.content, 150))}</div>
        </div>
      `).join('');
    }
    showStatus(`快照搜索完成，共 ${latestSnapshots.length} 条`);
  } catch (e) {
    list.innerHTML = `<div style="color:var(--danger)">搜索失败: ${escHtml(e.message)}</div>`;
  }
}
