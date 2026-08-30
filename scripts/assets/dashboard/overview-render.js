import { money, state } from './core.js';
import { esc } from './ui.js';
import {
  detailMetric,
  focusActiveViewRow,
  refreshScrollFades,
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
import { createPager } from './components/pager.js';
import { createListDetailView } from './components/list-detail-view.js';

export function createOverviewRenderers({
  params,
  requestListPage,
  getCachedJSON,
  peekCachedJSON,
  clearQueryStatus,
  showQueryError,
  bindToolTurnLinks,
  listTableSortState,
  setListSort,
}) {
const LIST_PAGER_IDS = {
  projects: 'projects-pager',
  tools: 'tool-output-pager',
};
const listPagerPayloads = { projects: null, tools: null };
const listPagers = Object.fromEntries(Object.entries(LIST_PAGER_IDS).map(([key, rootId]) => [
  key,
  createPager({
    rootId,
    onPageChange: page => {
      const payload = listPagerPayloads[key];
      if (listIsServerPaged(payload)) {
        requestListPage(key, page);
      } else {
        state.listPages[key] = page;
        renderListPage(key);
      }
    },
  }),
]));

function clearListPagers() {
  Object.values(listPagers).forEach(pager => pager.clear());
}

function setListPagerBusy(key, busy) {
  listPagers[key]?.setBusy(busy);
}

function listPayloadRows(payload) {
  if (Array.isArray(payload)) return payload;
  return (payload || {}).rows || [];
}

function costCompact(value) {
  return value === null || value === undefined ? '<span class="token-unavailable-value">—</span>' : compactNumberSpan(value, 'money');
}

function costText(value) {
  return value === null || value === undefined ? '—' : compactNumber(value, 'money');
}

function costExact(value) {
  return value === null || value === undefined ? 'Cost rate is not configured for every turn' : exactNumber(value, 'money');
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
      setListSort(key, button.dataset.listSort, button);
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
  listPagerPayloads[key] = payload;
  listPagers[key]?.render({ page, total: totalRows, perPage });
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
      <td class="num">${costCompact(r.credits)}</td>
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
      <td class="num">${costCompact(r.child_credits)}</td>
      <td class="num">${compactNumberSpan(r.child_raw || 0)}</td>
    </tr>
  `);
  const turnRows = (data.turns || []).map(r => `
    <tr class="session-detail-turn">
      <td class="truncate-cell" title="${esc(r.prompt_preview || '')}">${esc(r.prompt_preview || '(prompt text not stored)')}</td>
      <td>${statusLabel(r.turn_status)}</td>
      <td class="num">${costCompact(r.credits)}</td>
      <td class="num">${compactNumberSpan(r.raw || 0)}</td>
    </tr>
  `);
  return `
    <div class="session-detail-summary">
      ${detailMetric('Cost Units', costText(summary.credits), '', costExact(summary.credits))}
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

function sessionDetailPath(sessionId) {
  const q = params();
  q.set('selected_session_id', sessionId);
  return '/api/session-detail?' + q;
}

function commitSessionRow(row, detail) {
  state.selectedSession = row.dataset.sessionId || '';
  const label = row.dataset.sessionLabel || compactSessionId(state.selectedSession) || '(unknown)';
  setPanelContent('session-detail', renderSessionDetail(detail));
  document.getElementById('session-detail-status').textContent = label;
  refreshScrollFades();
}

const sessionDetailView = createListDetailView({
  rowSelector: '#projects tr[data-session-id]',
  buttonSelector: '#projects tr[data-session-id] .row-select-button',
  detailId: 'session-detail',
  statusId: 'session-detail-status',
  keyForRow: row => row.dataset.sessionId || '',
  pathForRow: row => sessionDetailPath(row.dataset.sessionId || ''),
  nextRequestSequence: () => ++state.sessionSeq,
  isCurrentRequest: sequence => sequence === state.sessionSeq,
  commit: commitSessionRow,
  reset: () => { state.selectedSession = ''; },
  getCachedJSON,
  peekCachedJSON,
  clearQueryStatus,
  showQueryError,
});

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

function toolDetailPath(toolName) {
  const q = params();
  q.set('tool_name', toolName);
  return '/api/tool?' + q;
}

function commitToolRow(row, detail) {
  const toolName = row.dataset.tool || '';
  state.selectedTool = toolName;
  document.getElementById('tool-detail-status').textContent = `${compactNumber((detail.summary || {}).calls || 0)} calls`;
  setPanelContent('tool-detail', renderToolDetail(detail));
  bindToolTurnLinks(document.getElementById('tool-detail'));
  refreshScrollFades();
}

const toolDetailView = createListDetailView({
  rowSelector: '#tool-output tr[data-tool]',
  buttonSelector: '#tool-output tr[data-tool] .row-select-button',
  detailId: 'tool-detail',
  statusId: 'tool-detail-status',
  keyForRow: row => row.dataset.tool || '',
  pathForRow: row => toolDetailPath(row.dataset.tool || ''),
  nextRequestSequence: () => ++state.toolSeq,
  isCurrentRequest: sequence => sequence === state.toolSeq,
  commit: commitToolRow,
  reset: () => { state.selectedTool = ''; },
  getCachedJSON,
  peekCachedJSON,
  clearQueryStatus,
  showQueryError,
});

function renderSubagentDetail(data) {
  const summary = data.summary || {};
  const sessions = data.sessions || [];
  const rows = data.rows || [];
  const sessionRows = sessions.map(r => {
    const rows = Number(r.rows || 0);
    const childCredits = r.child_credits === null || r.child_credits === undefined ? null : Number(r.child_credits);
    const avgCost = childCredits === null ? null : (rows ? childCredits / rows : 0);
    const share = childCredits === null || summary.child_credits === null || summary.child_credits === undefined ? '—' : pct(childCredits, Number(summary.child_credits));
    return `<tr><td class="truncate-cell session-label-cell" title="${esc(sessionDetailLabel(r))}">${sessionLabelMarkup(r)}</td><td class="num">${compactNumberSpan(rows)}</td><td class="num">${costCompact(avgCost)}</td><td class="num">${costCompact(childCredits)}</td><td class="num">${compactNumberSpan(r.child_raw || 0)}</td><td class="num">${share}</td></tr>`;
  });
  const childRows = rows.map(r => {
    const childLabel = r.child_agent_nickname || r.child_agent_role || shortSession(r.child_session_id || '');
    return `<tr>
      <td class="truncate-cell session-label-cell" title="${esc(sessionDetailLabel(r))}">${sessionLabelMarkup(r)}</td>
      <td class="truncate-cell" title="${esc(childLabel + ' / ' + (r.prompt_preview || ''))}">${esc(r.prompt_preview || childLabel || '(prompt text not stored)')}</td>
      <td class="num">${costCompact(r.child_credits)}</td>
      <td class="num">${compactNumberSpan(r.child_raw || 0)}</td>
    </tr>`;
  });
  return `
    <div class="tool-detail-summary">
      <div class="detail-grid tool-detail-grid">
        <div class="detail-cell tool-name-cell"><div class="value attribution-method-value" title="${esc(confidenceDisplay(summary.confidence))}"><span class="method-name">${esc(confidenceLabel(summary.confidence))}</span><span class="method-desc">${esc(confidenceDescription(summary.confidence))}</span></div></div>
        ${detailMetric('Rows', compactNumber(summary.rows || 0), '', exactNumber(summary.rows || 0))}
        ${detailMetric('Child Cost Units', costText(summary.child_credits), '', costExact(summary.child_credits))}
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

function subagentDetailPath(confidence) {
  const q = params();
  q.set('confidence', confidence);
  return '/api/subagent?' + q;
}

function commitSubagentRow(row, detail) {
  const confidence = row.dataset.confidence || '';
  state.selectedSubagentConfidence = confidence;
  document.getElementById('subagent-detail-status').textContent = `${compactNumber((detail.summary || {}).rows || 0)} rows`;
  setPanelContent('subagent-mix', renderSubagentDetail(detail));
  refreshScrollFades();
}

const subagentDetailView = createListDetailView({
  rowSelector: '#subagent-rollups tr[data-confidence]',
  buttonSelector: '#subagent-rollups tr[data-confidence] .row-select-button',
  detailId: 'subagent-mix',
  statusId: 'subagent-detail-status',
  keyForRow: row => row.dataset.confidence || '',
  pathForRow: row => subagentDetailPath(row.dataset.confidence || ''),
  nextRequestSequence: () => ++state.subagentSeq,
  isCurrentRequest: sequence => sequence === state.subagentSeq,
  commit: commitSubagentRow,
  reset: () => { state.selectedSubagentConfidence = ''; },
  getCachedJSON,
  peekCachedJSON,
  clearQueryStatus,
  showQueryError,
});

function renderSessionList(payload, prepared = null) {
  const rows = listPayloadRows(payload);
  const replacedControl = document.activeElement?.closest?.('#projects .row-select-button') || null;
  state.listRows.sessions = payload || [];
  setPanelContent('projects', table(
    [{label:'Session', sort:'session'}, {label:'Cost Units', sort:'credits', cls:'num'}, {label:'Total Tokens', sort:'raw', cls:'num'}, {label:'Turns', sort:'turns', cls:'num'}],
    paginateListRows('projects', payload).map(r => {
      const label = overviewSessionLabel(r);
      return `<tr data-session-id="${esc(r.session_id || '')}" data-session-label="${esc(label)}"><td class="truncate-cell session-label-cell" title="${esc(label)}"><button type="button" class="row-select-button" aria-pressed="false" aria-label="Select session ${esc(label)}">${sessionLabelMarkup(r)}</button></td><td class="num">${costCompact(r.credits)}</td><td class="num">${compactNumberSpan(r.raw)}</td><td class="num">${compactNumberSpan(r.turns)}</td></tr>`;
    }),
    listTableSortState('projects')
  ));
  renderListPager('projects', payload);
  bindListSortButtons('projects', 'projects');
  sessionDetailView.bindRows();
  sessionDetailView.activateRendered({
    isSelected: row => row.dataset.sessionId === state.selectedSession,
    prepared,
  });
  focusActiveViewRow({replacedControl});
}

function renderToolList(payload, prepared = null) {
  const rows = listPayloadRows(payload);
  const replacedControl = document.activeElement?.closest?.('#tool-output .row-select-button') || null;
  state.listRows.tools = payload || [];
  const toolOutputTotal = Number((payload || {}).output_tokens_total ?? rows.reduce((sum, r) => sum + Number(r.output_tokens || 0), 0));
  setPanelContent('tool-output', table(
    [{label:'Tool', sort:'tool_name'}, {label:'Calls', sort:'calls', cls:'num'}, {label:'Tokens', sort:'output_tokens', cls:'num'}, {label:'Share', sort:'share', cls:'num'}],
    paginateListRows('tools', payload).map(r => `<tr data-tool="${esc(r.tool_name || '')}"><td title="${esc(r.tool_name || '')}"><button type="button" class="row-select-button" aria-pressed="false" aria-label="Select tool ${esc(r.tool_name || 'unknown')}"><span>${esc(r.tool_name)}</span><span class="mobile-row-meta">${compactNumber(r.output_tokens || 0)} tokens · ${compactNumber(r.calls)} calls</span></button></td><td class="num">${compactNumberSpan(r.calls)}</td><td class="num">${compactNumberSpan(r.output_tokens || 0)}</td><td class="num">${pct(Number(r.output_tokens || 0), toolOutputTotal)}</td></tr>`),
    listTableSortState('tools')
  ));
  renderListPager('tools', payload);
  bindListSortButtons('tool-output', 'tools');
  toolDetailView.bindRows();
  toolDetailView.activateRendered({
    isSelected: row => row.dataset.tool === state.selectedTool,
    prepared,
  });
  focusActiveViewRow({replacedControl});
}

function renderSubagentList(rows, prepared = null) {
  const replacedControl = document.activeElement?.closest?.('#subagent-rollups .row-select-button') || null;
  state.listRows.subagents = rows || [];
  setPanelContent('subagent-rollups', table(
    [{label:'Attribution Method', sort:'confidence'}, {label:'Rows', sort:'rows', cls:'num'}, {label:'Cost Units', sort:'child_credits', cls:'num'}, {label:'Tokens', sort:'child_raw', cls:'num'}],
    (rows || []).map(r => `<tr data-confidence="${esc(r.confidence || '')}"><td title="${esc(r.confidence)}"><button type="button" class="row-select-button" aria-pressed="false" aria-label="Select attribution ${esc(confidenceLabel(r.confidence))}"><span>${esc(confidenceLabel(r.confidence))}</span><span class="mobile-row-meta">${costText(r.child_credits)} cost · ${compactNumber(r.child_raw)} tokens</span></button></td><td class="num">${compactNumberSpan(r.rows)}</td><td class="num">${costCompact(r.child_credits)}</td><td class="num">${compactNumberSpan(r.child_raw)}</td></tr>`),
    listTableSortState('subagents')
  ));
  bindListSortButtons('subagent-rollups', 'subagents');
  subagentDetailView.bindRows();
  subagentDetailView.activateRendered({
    isSelected: row => row.dataset.confidence === state.selectedSubagentConfidence,
    prepared,
  });
  focusActiveViewRow({replacedControl});
}


return {
  clearListPagers,
  renderSessionList,
  renderToolList,
  renderSubagentList,
  setListPagerBusy,
};
}
