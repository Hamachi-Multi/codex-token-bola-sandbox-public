import { fmt, money, state } from './core.js';
import { getJSON } from './api.js';
import { esc } from './ui.js';
import {
  detailMetric,
  detailGridLoadingPanel,
  focusActiveViewRow,
  handleListArrowFocus,
  refreshScrollFades,
  sessionDetailLoadingPanel,
  setPanelContent,
  table,
} from './dom.js';
import {
  compactNumber,
  compactNumberSpan,
  compactSessionId,
  confidenceDescription,
  confidenceDisplay,
  confidenceLabel,
  exactNumber,
  pct,
  sessionDetailLabel,
  sessionLabelMarkup,
  shortSession,
  statusLabel,
  toolDescription,
  toolDisplay,
  toolOutputTokens,
} from './formatters.js';

export function createOverviewRenderers({ params, safeLoad, bindToolTurnLinks, listTableSortState, setListSort }) {
const LIST_PAGER_IDS = {
  projects: 'projects-pager',
  tools: 'tool-output-pager',
};

function clearListPagers() {
  Object.values(LIST_PAGER_IDS).forEach(id => {
    const pager = document.getElementById(id);
    if (pager) pager.innerHTML = '';
  });
}

function listPayloadRows(payload) {
  if (Array.isArray(payload)) return payload;
  return (payload || {}).rows || [];
}

function listIsServerPaged(payload) {
  return payload && !Array.isArray(payload) && Number.isFinite(Number(payload.total));
}

function listTotalRows(payload) {
  if (Array.isArray(payload)) return payload.length;
  return Number((payload || {}).total ?? ((payload || {}).rows || []).length);
}

function listPerPage(payload) {
  if (listIsServerPaged(payload)) return Math.max(1, Number(payload.per_page || state.turnPageSize));
  return state.turnPageSize;
}

function clampedListPage(key, total, perPage = state.turnPageSize) {
  const pageCount = Math.max(1, Math.ceil(total / perPage));
  const page = Math.max(1, Math.min(Number(state.listPages[key] || 1), pageCount));
  state.listPages[key] = page;
  return page;
}

function paginateListRows(key, payload) {
  const allRows = listPayloadRows(payload);
  if (listIsServerPaged(payload)) {
    state.listPages[key] = Math.max(1, Number(payload.page || state.listPages[key] || 1));
    return allRows;
  }
  const page = clampedListPage(key, allRows.length);
  const start = (page - 1) * state.turnPageSize;
  return allRows.slice(start, start + state.turnPageSize);
}

function renderListPage(key) {
  if (key === 'projects') renderSessionList(state.listRows.sessions);
  if (key === 'tools') renderToolList(state.listRows.tools);
  refreshScrollFades();
}

function bindListSortButtons(rootId, key) {
  document.querySelectorAll(`#${rootId} [data-list-sort]`).forEach(button => {
    button.addEventListener('click', event => {
      event.preventDefault();
      setListSort(key, button.dataset.listSort);
    });
  });
}

function renderListPager(key, total) {
  const payload = total;
  const totalRows = listTotalRows(payload);
  const perPage = listPerPage(payload);
  const serverPaged = listIsServerPaged(payload);
  const page = serverPaged
    ? Math.max(1, Math.min(Number((payload || {}).page || state.listPages[key] || 1), Math.max(1, Math.ceil(totalRows / perPage))))
    : clampedListPage(key, totalRows, perPage);
  state.listPages[key] = page;
  const pageCount = Math.max(1, Math.ceil(totalRows / perPage));
  const start = totalRows ? (page - 1) * perPage + 1 : 0;
  const end = Math.min(totalRows, page * perPage);
  const pager = document.getElementById(LIST_PAGER_IDS[key]);
  if (!pager) return;
  pager.innerHTML = `
    <button data-list-page="prev" ${page <= 1 ? 'disabled' : ''}>Prev</button>
    <span class="page-status">${fmt.format(start)}-${fmt.format(end)} / ${fmt.format(totalRows)}</span>
    <button data-list-page="next" ${page >= pageCount ? 'disabled' : ''}>Next</button>
  `;
  pager.querySelectorAll('[data-list-page]').forEach(button => {
    button.addEventListener('click', () => {
      const direction = button.dataset.listPage === 'next' ? 1 : -1;
      const nextPage = Math.max(1, Math.min(Number(state.listPages[key] || 1) + direction, pageCount));
      if (nextPage === state.listPages[key]) return;
      state.listPages[key] = nextPage;
      if (serverPaged) {
        safeLoad();
      } else {
        renderListPage(key);
      }
    });
  });
}

function overviewSessionLabel(row) {
  return sessionDetailLabel(row) || '(unknown)';
}

function renderSessionDetail(data) {
  const summary = data.summary || {};
  const workflowRows = (data.workflows || []).map(r => `
    <tr>
      <td class="truncate-cell" title="${esc(r.workflow || '')}">${esc(r.workflow || '(unlabeled)')}</td>
      <td class="truncate-cell" title="${esc(r.category || '')}">${esc(r.category || '(unlabeled)')}</td>
      <td class="num">${compactNumberSpan(r.credits || 0, 'money')}</td>
      <td class="num">${compactNumberSpan(r.turns || 0)}</td>
    </tr>
  `);
  const toolRows = (data.tools || []).map(r => `
    <tr>
      <td class="truncate-cell" title="${esc(r.tool_name || '')}">${esc(r.tool_name || '(unknown)')}</td>
      <td class="num">${compactNumberSpan(r.calls || 0)}</td>
      <td class="num">${compactNumberSpan(r.output_tokens || 0)}</td>
    </tr>
  `);
  const subagentRows = (data.subagents || []).map(r => `
    <tr>
      <td class="truncate-cell" title="${esc(r.confidence || '')}">${esc(confidenceLabel(r.confidence))}</td>
      <td class="num">${compactNumberSpan(r.rows || 0)}</td>
      <td class="num">${compactNumberSpan(r.child_credits || 0, 'money')}</td>
      <td class="num">${compactNumberSpan(r.child_raw || 0)}</td>
    </tr>
  `);
  const turnRows = (data.turns || []).map(r => `
    <tr class="session-detail-turn">
      <td class="truncate-cell" title="${esc(r.prompt_preview || '')}">${esc(r.prompt_preview || '(prompt text not stored)')}</td>
      <td>${statusLabel(r.turn_status)}</td>
      <td class="num">${compactNumberSpan(r.credits || 0, 'money')}</td>
      <td class="num">${compactNumberSpan(r.raw || 0)}</td>
    </tr>
  `);
  return `
    <div class="session-detail-summary">
      ${detailMetric('Cost Units', compactNumber(summary.credits || 0, 'money'), '', exactNumber(summary.credits || 0, 'money'))}
      ${detailMetric('Total Tokens', compactNumber(summary.raw || 0), '', exactNumber(summary.raw || 0))}
      ${detailMetric('Cached Ratio', money.format((summary.cached_ratio || 0) * 100) + '%')}
      ${detailMetric('Turns', compactNumber(summary.turns || 0), '', exactNumber(summary.turns || 0))}
    </div>
    <div class="session-detail-section">
      <div class="session-detail-section-title">Workflow Distribution</div>
      ${table([{label:'Workflow'}, {label:'Category'}, {label:'Cost Units', cls:'num'}, {label:'Turns', cls:'num'}], workflowRows)}
    </div>
    <div class="session-detail-section">
      <div class="session-detail-section-title">Tool Output</div>
      ${table([{label:'Tool'}, {label:'Calls', cls:'num'}, {label:'Tokens', cls:'num'}], toolRows)}
    </div>
    <div class="session-detail-section">
      <div class="session-detail-section-title">Subagent Usage</div>
      ${table([{label:'Attribution'}, {label:'Rows', cls:'num'}, {label:'Cost Units', cls:'num'}, {label:'Tokens', cls:'num'}], subagentRows)}
    </div>
    <div class="session-detail-section">
      <div class="session-detail-section-title">Expensive Turns</div>
      ${table([{label:'Prompt'}, {label:'Status'}, {label:'Cost Units', cls:'num'}, {label:'Total Tokens', cls:'num'}], turnRows)}
    </div>
  `;
}

async function selectSessionRow(row) {
  document.querySelectorAll('#projects tr.selected').forEach(active => active.classList.remove('selected'));
  document.querySelectorAll('#projects tr[aria-selected="true"]').forEach(active => active.setAttribute('aria-selected', 'false'));
  row.classList.add('selected');
  row.setAttribute('aria-selected', 'true');
  state.selectedSession = row.dataset.sessionId || '';
  const seq = ++state.sessionSeq;
  const label = row.dataset.sessionLabel || compactSessionId(state.selectedSession) || '(unknown)';
  document.getElementById('session-detail-status').textContent = label;
  setPanelContent('session-detail', sessionDetailLoadingPanel('Loading session detail.'), 'loading');
  refreshScrollFades();
  try {
    const q = params();
    q.set('selected_session_id', state.selectedSession);
    const detail = await getJSON('/api/session-detail?' + q);
    if (seq !== state.sessionSeq || state.selectedSession !== (row.dataset.sessionId || '')) return;
    setPanelContent('session-detail', renderSessionDetail(detail));
    document.getElementById('session-detail-status').textContent = label;
    refreshScrollFades();
  } catch (err) {
    if (seq === state.sessionSeq && state.selectedSession === (row.dataset.sessionId || '')) {
      document.getElementById('session-detail-status').textContent = 'error';
      setPanelContent('session-detail', esc(err.message || err), 'error');
      refreshScrollFades();
    }
  }
}

function selectFirstVisibleSessionRow(row) {
  if (row) {
    row.click();
    return;
  }
  state.selectedSession = '';
  document.getElementById('session-detail-status').textContent = 'none';
  setPanelContent('session-detail', 'No rows for the current filter.', 'empty');
}

function renderToolDetail(data) {
  const summary = data.summary || {};
  const toolName = summary.tool_name || 'unknown';
  const sessionRows = (data.sessions || []).map(r => {
    const calls = Number(r.calls || 0);
    const outputTokens = Number(r.output_tokens || 0);
    const avgTokens = calls ? Math.round(outputTokens / calls) : 0;
      return `<tr><td class="truncate-cell session-label-cell" title="${esc(sessionDetailLabel(r))}">${sessionLabelMarkup(r)}</td><td class="num">${compactNumberSpan(calls)}</td><td class="num">${compactNumberSpan(avgTokens)}</td><td class="num">${compactNumberSpan(outputTokens)}</td><td class="num">${pct(outputTokens, Number(summary.output_tokens || 0))}</td></tr>`;
  });
  const callRows = (data.calls || []).map(r => {
    return `<tr><td class="truncate-cell session-label-cell" title="${esc(sessionDetailLabel(r))}">${sessionLabelMarkup(r)}</td><td class="truncate-cell" title="${esc(r.prompt_preview || '')}"><button class="prompt-jump" data-open-turn-modal="1" data-session="${esc(r.session_id || '')}" data-turn="${esc(r.turn_id || '')}" title="${esc('Open turn: ' + (r.prompt_preview || ''))}">${esc(r.prompt_preview || '(prompt text not stored)')}</button></td><td class="num">${compactNumberSpan(r.output_tokens ?? toolOutputTokens(r))}</td></tr>`;
  });
  return `
    <div class="tool-detail-summary">
      <div class="detail-grid tool-detail-grid">
        <div class="detail-cell tool-name-cell"><div class="value attribution-method-value" title="${esc(toolDisplay(toolName))}"><span class="method-name">${esc(toolName)}</span><span class="method-desc">${esc(toolDescription(toolName))}</span></div></div>
        ${detailMetric('Calls', compactNumber(summary.calls || 0), '', exactNumber(summary.calls || 0))}
        ${detailMetric('Output Tokens', compactNumber(summary.output_tokens || 0), '', exactNumber(summary.output_tokens || 0))}
        ${detailMetric('Avg Tokens', compactNumber(Math.round(summary.avg_output_tokens || 0)), '', exactNumber(Math.round(summary.avg_output_tokens || 0)))}
      </div>
      <div class="tool-detail-section-title">Session Distribution</div>
      <div class="tool-session-distribution">
        ${table([{label:'Session'}, {label:'Calls', cls:'num'}, {label:'Avg Tokens', cls:'num'}, {label:'Tokens', cls:'num'}, {label:'Share', cls:'num'}], sessionRows)}
      </div>
      <div class="tool-detail-section-title">Largest Tool Outputs</div>
      ${table([{label:'Session'}, {label:'Prompt'}, {label:'Tokens', cls:'num'}], callRows)}
    </div>
  `;
}

async function selectToolRow(row) {
  document.querySelectorAll('#tool-output tr.selected').forEach(active => active.classList.remove('selected'));
  document.querySelectorAll('#tool-output tr[aria-selected="true"]').forEach(active => active.setAttribute('aria-selected', 'false'));
  row.classList.add('selected');
  row.setAttribute('aria-selected', 'true');
  const toolName = row.dataset.tool || '';
  state.selectedTool = toolName;
  const seq = ++state.toolSeq;
  document.getElementById('tool-detail-status').textContent = '';
  setPanelContent('tool-detail', detailGridLoadingPanel('Loading tool detail.'), 'loading');
  refreshScrollFades();
  try {
    const q = params();
    q.set('tool_name', toolName);
    const detail = await getJSON('/api/tool?' + q);
    if (seq !== state.toolSeq || state.selectedTool !== toolName) return;
    document.getElementById('tool-detail-status').textContent = `${compactNumber((detail.summary || {}).calls || 0)} calls`;
    setPanelContent('tool-detail', renderToolDetail(detail));
    bindToolTurnLinks(document.getElementById('tool-detail'));
    refreshScrollFades();
  } catch (err) {
    if (seq === state.toolSeq && state.selectedTool === toolName) {
      document.getElementById('tool-detail-status').textContent = 'error';
      setPanelContent('tool-detail', esc(err.message || err), 'error');
      refreshScrollFades();
    }
  }
}

function selectFirstVisibleToolRow(row) {
  if (row) {
    row.click();
    return;
  }
  state.selectedTool = '';
  document.getElementById('tool-detail-status').textContent = 'none';
  setPanelContent('tool-detail', 'No rows for the current filter.', 'empty');
}

function renderSubagentDetail(data) {
  const summary = data.summary || {};
  const sessions = data.sessions || [];
  const rows = data.rows || [];
  const sessionRows = sessions.map(r => {
    const rows = Number(r.rows || 0);
    const childCredits = Number(r.child_credits || 0);
    const avgCost = rows ? childCredits / rows : 0;
    return `<tr><td class="truncate-cell session-label-cell" title="${esc(sessionDetailLabel(r))}">${sessionLabelMarkup(r)}</td><td class="num">${compactNumberSpan(rows)}</td><td class="num">${compactNumberSpan(avgCost, 'money')}</td><td class="num">${compactNumberSpan(childCredits, 'money')}</td><td class="num">${compactNumberSpan(r.child_raw || 0)}</td><td class="num">${pct(childCredits, Number(summary.child_credits || 0))}</td></tr>`;
  });
  const childRows = rows.map(r => {
    const childLabel = r.child_agent_nickname || r.child_agent_role || shortSession(r.child_session_id || '');
    return `<tr>
      <td class="truncate-cell session-label-cell" title="${esc(sessionDetailLabel(r))}">${sessionLabelMarkup(r)}</td>
      <td class="truncate-cell" title="${esc(childLabel + ' / ' + (r.prompt_preview || ''))}">${esc(r.prompt_preview || childLabel || '(prompt text not stored)')}</td>
      <td class="num">${compactNumberSpan(r.child_credits || 0, 'money')}</td>
      <td class="num">${compactNumberSpan(r.child_raw || 0)}</td>
    </tr>`;
  });
  return `
    <div class="tool-detail-summary">
      <div class="detail-grid tool-detail-grid">
        <div class="detail-cell tool-name-cell"><div class="value attribution-method-value" title="${esc(confidenceDisplay(summary.confidence))}"><span class="method-name">${esc(confidenceLabel(summary.confidence))}</span><span class="method-desc">${esc(confidenceDescription(summary.confidence))}</span></div></div>
        ${detailMetric('Rows', compactNumber(summary.rows || 0), '', exactNumber(summary.rows || 0))}
        ${detailMetric('Child Cost Units', compactNumber(summary.child_credits || 0, 'money'), '', exactNumber(summary.child_credits || 0, 'money'))}
        ${detailMetric('Child Tokens', compactNumber(summary.child_raw || 0), '', exactNumber(summary.child_raw || 0))}
      </div>
      <div class="tool-detail-section-title">Session Distribution</div>
      <div class="subagent-session-distribution">
        ${table([{label:'Session'}, {label:'Rows', cls:'num'}, {label:'Avg Cost', cls:'num'}, {label:'Cost Units', cls:'num'}, {label:'Tokens', cls:'num'}, {label:'Share', cls:'num'}], sessionRows)}
      </div>
      <div class="tool-detail-section-title">Largest Parent Prompts</div>
      ${table([{label:'Session'}, {label:'Parent Prompt'}, {label:'Cost Units', cls:'num'}, {label:'Tokens', cls:'num'}], childRows)}
    </div>
  `;
}

async function selectSubagentRow(row) {
  document.querySelectorAll('#subagent-rollups tr.selected').forEach(active => active.classList.remove('selected'));
  document.querySelectorAll('#subagent-rollups tr[aria-selected="true"]').forEach(active => active.setAttribute('aria-selected', 'false'));
  row.classList.add('selected');
  row.setAttribute('aria-selected', 'true');
  const confidence = row.dataset.confidence || '';
  state.selectedSubagentConfidence = confidence;
  const seq = ++state.subagentSeq;
  document.getElementById('subagent-detail-status').textContent = '';
  setPanelContent('subagent-mix', detailGridLoadingPanel('Loading attribution detail.', 6, 4, 4, 4), 'loading');
  refreshScrollFades();
  try {
    const q = params();
    q.set('confidence', confidence);
    const detail = await getJSON('/api/subagent?' + q);
    if (seq !== state.subagentSeq || state.selectedSubagentConfidence !== confidence) return;
    document.getElementById('subagent-detail-status').textContent = `${compactNumber((detail.summary || {}).rows || 0)} rows`;
    setPanelContent('subagent-mix', renderSubagentDetail(detail));
    refreshScrollFades();
  } catch (err) {
    if (seq === state.subagentSeq && state.selectedSubagentConfidence === confidence) {
      document.getElementById('subagent-detail-status').textContent = 'error';
      setPanelContent('subagent-mix', esc(err.message || err), 'error');
      refreshScrollFades();
    }
  }
}

function selectFirstVisibleSubagentRow(row) {
  if (row) {
    row.click();
    return;
  }
  state.selectedSubagentConfidence = '';
  document.getElementById('subagent-detail-status').textContent = 'none';
  setPanelContent('subagent-mix', 'No rows for the current filter.', 'empty');
}

function renderSessionList(payload) {
  const rows = listPayloadRows(payload);
  state.listRows.sessions = payload || [];
  setPanelContent('projects', table(
    [{label:'Session', sort:'session'}, {label:'Cost Units', sort:'credits', cls:'num'}, {label:'Total Tokens', sort:'raw', cls:'num'}, {label:'Turns', sort:'turns', cls:'num'}],
    paginateListRows('projects', payload).map(r => {
      const label = overviewSessionLabel(r);
      return `<tr tabindex="0" role="button" aria-selected="false" data-session-id="${esc(r.session_id || '')}" data-session-label="${esc(label)}"><td class="truncate-cell session-label-cell" title="${esc(label)}">${sessionLabelMarkup(r)}</td><td class="num">${compactNumberSpan(r.credits, 'money')}</td><td class="num">${compactNumberSpan(r.raw)}</td><td class="num">${compactNumberSpan(r.turns)}</td></tr>`;
    }),
    listTableSortState('projects')
  ));
  renderListPager('projects', payload);
  bindListSortButtons('projects', 'projects');
  document.querySelectorAll('#projects tr[data-session-id]').forEach(row => {
    row.addEventListener('click', () => selectSessionRow(row));
    row.addEventListener('keydown', event => {
      if (handleListArrowFocus(event, '#projects tr[data-session-id]', true)) return;
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        selectSessionRow(row);
      }
    });
  });
  const visibleSession = [...document.querySelectorAll('#projects tr[data-session-id]')]
    .find(row => row.dataset.sessionId === state.selectedSession);
  const firstSession = document.querySelector('#projects tr[data-session-id]');
  if (visibleSession) {
    visibleSession.click();
  } else {
    selectFirstVisibleSessionRow(firstSession);
  }
  focusActiveViewRow();
}

function renderToolList(payload) {
  const rows = listPayloadRows(payload);
  state.listRows.tools = payload || [];
  const toolOutputTotal = Number((payload || {}).output_tokens_total ?? rows.reduce((sum, r) => sum + Number(r.output_tokens || 0), 0));
  setPanelContent('tool-output', table(
    [{label:'Tool', sort:'tool_name'}, {label:'Calls', sort:'calls', cls:'num'}, {label:'Tokens', sort:'output_tokens', cls:'num'}, {label:'Share', sort:'share', cls:'num'}],
    paginateListRows('tools', payload).map(r => `<tr tabindex="0" role="button" aria-selected="false" data-tool="${esc(r.tool_name || '')}"><td title="${esc(r.tool_name || '')}"><div>${esc(r.tool_name)}</div><div class="mobile-row-meta">${compactNumber(r.output_tokens || 0)} tokens · ${compactNumber(r.calls)} calls</div></td><td class="num">${compactNumberSpan(r.calls)}</td><td class="num">${compactNumberSpan(r.output_tokens || 0)}</td><td class="num">${pct(Number(r.output_tokens || 0), toolOutputTotal)}</td></tr>`),
    listTableSortState('tools')
  ));
  renderListPager('tools', payload);
  bindListSortButtons('tool-output', 'tools');
  document.querySelectorAll('#tool-output tr[data-tool]').forEach(row => {
    row.addEventListener('click', () => selectToolRow(row));
    row.addEventListener('keydown', event => {
      if (handleListArrowFocus(event, '#tool-output tr[data-tool]', true)) return;
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        selectToolRow(row);
      }
    });
  });
  const visibleTool = [...document.querySelectorAll('#tool-output tr[data-tool]')]
    .find(row => row.dataset.tool === state.selectedTool);
  const firstTool = document.querySelector('#tool-output tr[data-tool]');
  if (visibleTool) {
    visibleTool.click();
  } else {
    selectFirstVisibleToolRow(firstTool);
  }
  focusActiveViewRow();
}

function renderSubagentList(rows) {
  state.listRows.subagents = rows || [];
  setPanelContent('subagent-rollups', table(
    [{label:'Attribution Method', sort:'confidence'}, {label:'Rows', sort:'rows', cls:'num'}, {label:'Cost Units', sort:'child_credits', cls:'num'}, {label:'Tokens', sort:'child_raw', cls:'num'}],
    (rows || []).map(r => `<tr tabindex="0" role="button" aria-selected="false" data-confidence="${esc(r.confidence || '')}"><td title="${esc(r.confidence)}"><div>${esc(confidenceLabel(r.confidence))}</div><div class="mobile-row-meta">${compactNumber(r.child_credits, 'money')} cost · ${compactNumber(r.child_raw)} tokens</div></td><td class="num">${compactNumberSpan(r.rows)}</td><td class="num">${compactNumberSpan(r.child_credits, 'money')}</td><td class="num">${compactNumberSpan(r.child_raw)}</td></tr>`),
    listTableSortState('subagents')
  ));
  bindListSortButtons('subagent-rollups', 'subagents');
  document.querySelectorAll('#subagent-rollups tr[data-confidence]').forEach(row => {
    row.addEventListener('click', () => selectSubagentRow(row));
    row.addEventListener('keydown', event => {
      if (handleListArrowFocus(event, '#subagent-rollups tr[data-confidence]', true)) return;
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        selectSubagentRow(row);
      }
    });
  });
  const visibleSubagent = [...document.querySelectorAll('#subagent-rollups tr[data-confidence]')]
    .find(row => row.dataset.confidence === state.selectedSubagentConfidence);
  const firstSubagent = document.querySelector('#subagent-rollups tr[data-confidence]');
  if (visibleSubagent) {
    visibleSubagent.click();
  } else {
    selectFirstVisibleSubagentRow(firstSubagent);
  }
  focusActiveViewRow();
}


return {
  clearListPagers,
  renderSessionList,
  renderToolList,
  renderSubagentList,
};
}
