(function () {
  function safeParseArray(text, fallback) {
    try {
      const parsed = JSON.parse(text || '[]');
      return Array.isArray(parsed) ? parsed : fallback;
    } catch (_) {
      return fallback;
    }
  }

  window.renderEvolutionPreview = function renderEvolutionPreviewEnhanced(data, sourceLabel = '') {
    const el = document.getElementById('evolution-preview');
    if (!el) return;
    latestEvolutionPreview = data;
    latestEvolutionPreviewSourceLabel = sourceLabel;
    const oldCharacter = data.current_character_personality || '';
    const oldRelationship = data.current_relationship_dynamics || '';
    const oldLifeStatus = data.current_life_status || '';
    const filterMeta = data.evolution_filter_meta || {};
    const selectedCount = Number(data.evolution_prompt_event_count || 0);
    const selectedIds = (data.evolution_prompt_event_ids || []).filter(Boolean).join(', ');
    const selectedEvents = data.evolution_candidates || [];
    const topEvents = (data.scored_events || []).slice(0, 10);
    const bridgePack = Array.isArray(data.bridge_summary_pack) ? data.bridge_summary_pack : [];
    const eventLineDigest = Array.isArray(data.event_line_digest) ? data.event_line_digest : [];
    const appliedSummaryContext = data.applied_summary_context || {};
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
        <div class="list-meta" style="margin-top:8px">可直接编辑以下整包内容；应用时将使用你的编辑结果。</div>
        <textarea id="evolution-edit-summary" style="min-height:120px;">${escHtml(data.change_summary || '')}</textarea>
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
        <h2>已应用周/月摘要参考</h2>
        <textarea id="evolution-applied-summary-context" style="min-height:180px;" readonly>${escHtml(JSON.stringify(appliedSummaryContext, null, 2))}</textarea>
      </div>
      <div class="card" style="margin-bottom:8px">
        <h2>桥层摘要包</h2>
        <textarea id="evolution-edit-bridge-pack" style="min-height:220px;">${escHtml(JSON.stringify(bridgePack, null, 2))}</textarea>
      </div>
      <div class="card" style="margin-bottom:8px">
        <h2>事件线概括</h2>
        <textarea id="evolution-edit-event-line-digest" style="min-height:180px;">${escHtml(JSON.stringify(eventLineDigest, null, 2))}</textarea>
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
  };

  window.syncLatestEvolutionPreviewFromEditor = function syncLatestEvolutionPreviewFromEditorEnhanced() {
    if (!latestEvolutionPreview) return null;
    const summaryEl = document.getElementById('evolution-edit-summary');
    const characterEl = document.getElementById('evolution-edit-character');
    const relationshipEl = document.getElementById('evolution-edit-relationship');
    const lifeStatusEl = document.getElementById('evolution-edit-life-status');
    const bridgePackEl = document.getElementById('evolution-edit-bridge-pack');
    const eventLineDigestEl = document.getElementById('evolution-edit-event-line-digest');
    latestEvolutionPreview = {
      ...latestEvolutionPreview,
      change_summary: (summaryEl?.value || '').trim(),
      new_character_personality: (characterEl?.value || '').trim(),
      new_relationship_dynamics: (relationshipEl?.value || '').trim(),
      new_life_status: (lifeStatusEl?.value || '').trim(),
      bridge_summary_pack: safeParseArray(bridgePackEl?.value || '[]', latestEvolutionPreview.bridge_summary_pack || []),
      event_line_digest: safeParseArray(eventLineDigestEl?.value || '[]', latestEvolutionPreview.event_line_digest || []),
    };
    return latestEvolutionPreview;
  };
})();
