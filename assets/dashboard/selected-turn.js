import { money, state } from './core.js';
import { getJSON } from './api.js';
import { esc } from './ui.js';
import { handleListArrowFocus, setPanelContent } from './dom.js';
import {
  compactDateTime,
  compactNumber,
  compactNumberSpan,
  durationLabel,
  elapsedMs,
  exactNumber,
  sessionDetailLabel,
  turnStatusClass,
} from './formatters.js';

export function createSelectedTurnController({ params, refreshScrollFades, setPageInert }) {
  let modalDetailData = null;
  let modalToolSummaryExpanded = false;

function openTurnModal(trigger = null) {
  const modal = document.getElementById('turn-modal');
  state.modalTrigger = trigger || document.activeElement;
  modal.classList.add('open');
  modal.setAttribute('aria-hidden', 'false');
  setPageInert(true);
  document.getElementById('turn-modal-close').focus();
}

function closeTurnModal() {
  const modal = document.getElementById('turn-modal');
  modal.classList.remove('open');
  modal.setAttribute('aria-hidden', 'true');
  setPageInert(false);
  state.modalSeq += 1;
  modalDetailData = null;
  modalToolSummaryExpanded = false;
  state.modalTrigger?.focus?.();
  state.modalTrigger = null;
}

function turnPromptPreviewMarkup(turn) {
  const preview = turn.prompt_preview || '';
  if (preview) return `<div class="prompt-main">${esc(preview)}</div>`;
  return '<div class="prompt-main prompt-placeholder">(prompt text not stored)</div>';
}

function renderDetailSummary(data) {
  const { modelTotal, toolTotal } = selectedTurnTotals(data);
  document.getElementById('detail-status').textContent = `${compactNumber(modelTotal)} Model Calls / ${compactNumber(toolTotal)} Tool Calls`;
  return selectedTurnDetailMarkup(data, {
    promptExpanded: state.promptExpanded,
    promptToggle: true,
    toolExpanded: state.toolSummaryExpanded,
  });
}

function selectedTurnTotals(data) {
  const modelSummary = data.model_call_summary || {};
  const toolRows = ((data.tool_call_summary || {}).rows || []);
  const modelTotal = data.model_call_total ?? Number(modelSummary.calls || 0);
  const toolTotal = data.tool_call_total ?? toolRows.reduce((sum, row) => sum + Number(row.calls || 0), 0);
  return { modelTotal, toolTotal };
}

function selectedTurnDetailMarkup(data, options = {}) {
  const turn = data.turn || {};
  const modelSummary = data.model_call_summary || {};
  const toolRows = ((data.tool_call_summary || {}).rows || []);
  const promptExpanded = options.promptExpanded ?? false;
  const promptToggle = options.promptToggle ?? false;
  const toolExpanded = options.toolExpanded;
  const promptText = turn.prompt_preview || '(prompt text not stored)';
  const capturedAt = compactDateTime(turn.captured_at || turn.started_at || '');
  const identityMeta = [sessionDetailLabel(turn) || '-', capturedAt].filter(Boolean).join(' / ');
  const identityClass = [
    'selected-turn-identity',
    promptExpanded ? 'expanded' : '',
    promptToggle ? '' : 'static',
  ].filter(Boolean).join(' ');
  const metaAttrs = promptToggle
    ? ` data-toggle-prompt role="button" tabindex="0" aria-expanded="${promptExpanded}" title="Click to ${promptExpanded ? 'collapse' : 'expand'} prompt"`
    : '';
  const toggleIcon = promptToggle ? '<svg class="prompt-toggle-icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6l4 4 4-4"/></svg>' : '';
  return `
    <div class="selected-turn-detail">
      <div class="selected-turn-header">
        <div class="${identityClass}">
          <div class="value attribution-method-value" title="${esc(promptText)}"><span class="method-name">${esc(promptText)}</span><span class="method-desc"${metaAttrs}><span class="prompt-toggle-label">${esc(identityMeta)}</span>${toggleIcon}</span></div>
        </div>
      </div>
      <div class="selected-turn-section">
        <div class="selected-turn-section-title">Turn Context</div>
        ${turnContextSummary(turn)}
      </div>
      <div class="selected-turn-section">
        <div class="selected-turn-section-title">Turn Summary</div>
        ${turnSummaryMetrics(turn)}
      </div>
      <div class="selected-turn-section">
        <div class="selected-turn-section-title">Token Summary</div>
        ${turnTokenBreakdownMetrics(turn)}
      </div>
      <div class="selected-turn-section">
        <div class="selected-turn-section-title">Call Summary</div>
        ${selectedTurnActivityMetrics(modelSummary, toolRows)}
      </div>
      <div class="selected-turn-section"><div class="selected-turn-section-title">Tool Calls</div>${toolSummaryList(toolRows, { expanded: toolExpanded })}</div>
    </div>
  `;
}

function selectedTurnLoadingPanel() {
  const metric = '<div class="selected-turn-metric"><div class="label"><span class="loading-line loading-label"></span></div><div class="value"><span class="loading-line loading-value"></span></div></div>';
  const status = '<div class="selected-turn-context-status selected-turn-status-cell"><span class="status unknown"><span class="loading-line loading-value"></span></span></div>';
  const toolStat = '<span class="selected-turn-tool-stat"><span class="label"><span class="loading-line loading-label"></span></span><span class="value"><span class="loading-line loading-value"></span></span></span>';
  const toolRow = `<div class="selected-turn-tool-row"><div class="selected-turn-tool-name"><span class="loading-line loading-value"></span></div><div class="selected-turn-tool-stats">${toolStat}${toolStat}${toolStat}${toolStat}</div></div>`;
  return `<span class="sr-only">Loading selected turn detail.</span><div class="selected-turn-detail" aria-hidden="true">
    <div class="selected-turn-header">
      <div class="selected-turn-identity static loading-skeleton">
        <div class="value attribution-method-value"><span class="method-name"><span class="loading-line"></span></span><span class="method-desc"><span class="loading-line"></span></span></div>
      </div>
    </div>
    <div class="selected-turn-section">
      <div class="selected-turn-section-title"><span class="loading-line loading-title"></span></div>
      <div class="selected-turn-context-grid">${status}${metric}${metric}</div>
    </div>
    <div class="selected-turn-section">
      <div class="selected-turn-section-title"><span class="loading-line loading-title"></span></div>
      <div class="selected-turn-metric-grid">${metric}${metric}${metric}${metric}</div>
    </div>
    <div class="selected-turn-section">
      <div class="selected-turn-section-title"><span class="loading-line loading-title"></span></div>
      <div class="selected-turn-metric-grid">${metric}${metric}${metric}${metric}</div>
    </div>
    <div class="selected-turn-section">
      <div class="selected-turn-section-title"><span class="loading-line loading-title"></span></div>
      <div class="selected-turn-metric-grid selected-turn-call-grid">${metric}${metric}${metric}${metric}</div>
    </div>
    <div class="selected-turn-section">
      <div class="selected-turn-section-title"><span class="loading-line loading-title"></span></div>
      <div class="selected-turn-tool-list">${toolRow}${toolRow}${toolRow}</div>
    </div>
  </div>`;
}

function selectedTurnMetric(label, valueHtml, kind = '', title = '') {
  const className = kind ? `selected-turn-metric ${kind}` : 'selected-turn-metric';
  const titleAttr = title ? ` title="${esc(title)}"` : '';
  return `<div class="${className}">
    <div class="label">${esc(label)}</div>
    <div class="value"${titleAttr}>${valueHtml}</div>
  </div>`;
}

function selectedTurnMetricGrid(items, extraClass = '') {
  const className = extraClass ? `selected-turn-metric-grid ${extraClass}` : 'selected-turn-metric-grid';
  return `<div class="${className}">${items.join('')}</div>`;
}

function turnContextSummary(turn) {
  const statusText = turn.turn_status || 'unknown';
  return `<div class="selected-turn-context-grid">
    <div class="selected-turn-context-status selected-turn-status-cell" aria-label="Status">
      <span class="status ${esc(turnStatusClass(turn.turn_status))}" title="${esc(statusText)}">${esc(statusText)}</span>
    </div>
    ${selectedTurnMetric('Model', esc(turn.model || '-'), 'selected-turn-model', turn.model || '')}
    ${selectedTurnMetric('Reasoning', esc(turn.reasoning_effort || '-'))}
  </div>`;
}

function turnSummaryMetrics(turn) {
  return selectedTurnMetricGrid([
    selectedTurnMetric('Cost Units', compactNumberSpan(turn.weighted_credits || 0, 'money'), '', exactNumber(turn.weighted_credits || 0, 'money')),
    selectedTurnMetric('Total Tokens', compactNumberSpan(turn.total_tokens || 0), '', exactNumber(turn.total_tokens || 0)),
    selectedTurnMetric('Runtime', esc(durationLabel(elapsedMs(turn.started_at, turn.stopped_at))), '', `${compactDateTime(turn.started_at)} -> ${compactDateTime(turn.stopped_at)}`),
    selectedTurnMetric('Cached Ratio', esc(money.format((turn.cached_ratio || 0) * 100) + '%')),
  ]);
}

function turnTokenBreakdownMetrics(turn) {
  return selectedTurnMetricGrid([
    selectedTurnMetric('Input Tokens', compactNumberSpan(turn.input_tokens || 0), '', exactNumber(turn.input_tokens || 0)),
    selectedTurnMetric('Non-Cached Input', compactNumberSpan(turn.non_cached_input_tokens || 0), '', exactNumber(turn.non_cached_input_tokens || 0)),
    selectedTurnMetric('Cached Input', compactNumberSpan(turn.cached_input_tokens || 0), '', exactNumber(turn.cached_input_tokens || 0)),
    selectedTurnMetric('Output Tokens', compactNumberSpan(turn.output_tokens || 0), '', exactNumber(turn.output_tokens || 0)),
  ]);
}

function selectedTurnActivityMetrics(modelSummary, toolRows) {
  const toolCallCount = toolRows.reduce((sum, row) => sum + Number(row.calls || 0), 0);
  const toolFailures = toolRows.reduce((sum, row) => sum + Number(row.failed_calls || 0), 0);
  const maxToolDuration = toolRows.reduce((max, row) => Math.max(max, Number(row.max_duration_ms || 0)), 0);
  return selectedTurnMetricGrid([
    selectedTurnMetric('Model Calls', compactNumberSpan(modelSummary.calls || 0), '', exactNumber(modelSummary.calls || 0)),
    selectedTurnMetric('Tool Calls', compactNumberSpan(toolCallCount), '', exactNumber(toolCallCount)),
    selectedTurnMetric('Tool Failures', compactNumberSpan(toolFailures), '', exactNumber(toolFailures)),
    selectedTurnMetric('Max Tool Duration', esc(durationLabel(maxToolDuration))),
  ], 'selected-turn-call-grid');
}

function selectedTurnToolStat(label, valueHtml, title = '') {
  const titleAttr = title ? ` title="${esc(title)}"` : '';
  return `<span class="selected-turn-tool-stat"${titleAttr}>
    <span class="label">${esc(label)}</span>
    <span class="value">${valueHtml}</span>
  </span>`;
}

function toolSummaryList(rows, options = {}) {
  const limit = 16;
  const expanded = (options.expanded ?? state.toolSummaryExpanded) || rows.length <= limit;
  const visibleRows = expanded ? rows : rows.slice(0, limit);
  const rendered = visibleRows.map(r => `<div class="selected-turn-tool-row">
    <div class="selected-turn-tool-name" title="${esc(r.tool_name || '')}">${esc(r.tool_name || 'unknown')}</div>
    <div class="selected-turn-tool-stats">
      ${selectedTurnToolStat('Calls', compactNumberSpan(r.calls || 0), exactNumber(r.calls || 0))}
      ${selectedTurnToolStat('Tokens', compactNumberSpan(r.output_tokens || 0), exactNumber(r.output_tokens || 0))}
      ${selectedTurnToolStat('Failures', compactNumberSpan(r.failed_calls || 0), exactNumber(r.failed_calls || 0))}
      ${selectedTurnToolStat('Max Duration', esc(durationLabel(r.max_duration_ms || 0)))}
    </div>
  </div>`).join('');
  const toggle = rows.length > limit ? `<button type="button" class="selected-turn-tool-toggle" data-toggle-tools aria-expanded="${expanded}" title="${expanded ? 'Collapse tool list' : 'Show all tools'}">${expanded ? `Show top ${limit} tools` : `Show all ${compactNumber(rows.length)} tools`}</button>` : '';
  return `<div class="selected-turn-tool-list">${rendered}${toggle}</div>`;
}

function toggleSelectedTurnPrompt(selectRow, options = {}) {
  state.promptExpanded = !state.promptExpanded;
  setPanelContent('detail', renderDetailSummary(state.detailData || {}));
  bindDetailControls(selectRow);
  if (options.restoreFocus) document.querySelector('[data-toggle-prompt]')?.focus({ preventScroll: true });
  refreshScrollFades();
}

function toggleSelectedTurnTools(selectRow) {
  state.toolSummaryExpanded = !state.toolSummaryExpanded;
  setPanelContent('detail', renderDetailSummary(state.detailData || {}));
  bindDetailControls(selectRow);
  refreshScrollFades();
}

function bindDetailControls(selectRow) {
  const promptToggle = document.querySelector('[data-toggle-prompt]');
  const promptToggleRegion = document.querySelector('.selected-turn-identity');
  const toolToggle = document.querySelector('[data-toggle-tools]');
  if (promptToggle) {
    promptToggle.addEventListener('click', () => {
      toggleSelectedTurnPrompt(selectRow, { restoreFocus: true });
    });
    promptToggle.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        toggleSelectedTurnPrompt(selectRow, { restoreFocus: true });
      }
    });
  }
  if (promptToggleRegion) {
    promptToggleRegion.addEventListener('click', event => {
      if (event.target.closest('.method-name')) return;
      if (event.target.closest('[data-toggle-prompt]')) return;
      toggleSelectedTurnPrompt(selectRow, { restoreFocus: true });
    });
  }
  if (toolToggle) {
    toolToggle.addEventListener('click', () => {
      toggleSelectedTurnTools(selectRow);
    });
  }
  document.querySelectorAll('#detail [data-toggle-tools]').forEach(item => {
    item.addEventListener('keydown', event => {
      handleListArrowFocus(event, '#detail [data-toggle-tools]');
    });
  });
  updateSelectedTurnPromptOverflow();
  requestAnimationFrame(updateSelectedTurnPromptOverflow);
  refreshScrollFades();
}

function updateSelectedTurnPromptOverflow() {
  document.querySelectorAll('.selected-turn-identity').forEach(identity => {
    const prompt = identity.querySelector('.method-name');
    const hidden = Boolean(prompt && !identity.classList.contains('expanded') && prompt.scrollHeight > prompt.clientHeight + 1);
    identity.classList.toggle('has-hidden-prompt', hidden);
  });
}


function renderTurnModal(data) {
  const turn = data.turn || {};
  const { modelTotal, toolTotal } = selectedTurnTotals(data);
  document.getElementById('turn-modal-title').textContent = 'Selected Turn';
  document.getElementById('turn-modal-subtitle').textContent = `${sessionDetailLabel(turn)} / ${compactNumber(modelTotal)} model calls / ${compactNumber(toolTotal)} tool calls`;
  return selectedTurnDetailMarkup(data, {
    promptExpanded: true,
    promptToggle: false,
    toolExpanded: modalToolSummaryExpanded,
  });
}

function toggleTurnModalTools() {
  modalToolSummaryExpanded = !modalToolSummaryExpanded;
  document.getElementById('turn-modal-body').innerHTML = renderTurnModal(modalDetailData || {});
  bindTurnModalControls();
  refreshScrollFades(document.getElementById('turn-modal'));
}

function bindTurnModalControls() {
  const body = document.getElementById('turn-modal-body');
  const toolToggle = body.querySelector('[data-toggle-tools]');
  if (toolToggle) {
    toolToggle.addEventListener('click', toggleTurnModalTools);
  }
  body.querySelectorAll('[data-toggle-tools]').forEach(item => {
    item.addEventListener('keydown', event => {
      handleListArrowFocus(event, '#turn-modal-body [data-toggle-tools]');
    });
  });
}

async function openTurnModalFromToolLink(button) {
  const session = button.dataset.session || '';
  const turn = button.dataset.turn || '';
  if (!session || !turn) return;
  const seq = ++state.modalSeq;
  modalDetailData = null;
  modalToolSummaryExpanded = false;
  document.getElementById('turn-modal-title').textContent = 'Selected Turn';
  document.getElementById('turn-modal-subtitle').textContent = '';
  document.getElementById('turn-modal-body').innerHTML = selectedTurnLoadingPanel();
  openTurnModal(button);
  try {
    const q = params();
    q.set('session_id', session);
    q.set('turn_id', turn);
    const detail = await getJSON('/api/turn?' + q);
    if (seq !== state.modalSeq) return;
    modalDetailData = detail;
    document.getElementById('turn-modal-body').innerHTML = renderTurnModal(detail);
    bindTurnModalControls();
    refreshScrollFades(document.getElementById('turn-modal'));
  } catch (err) {
    if (seq === state.modalSeq) {
      document.getElementById('turn-modal-subtitle').textContent = 'error';
      document.getElementById('turn-modal-body').innerHTML = `<div class="error">${esc(err.message || err)}</div>`;
    }
  }
}

function bindToolTurnLinks(root = document) {
  root.querySelectorAll('[data-open-turn-modal]').forEach(button => {
    button.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      openTurnModalFromToolLink(button);
    });
  });
}


return {
  bindDetailControls,
  bindToolTurnLinks,
  closeTurnModal,
  openTurnModal,
  openTurnModalFromToolLink,
  renderDetailSummary,
  renderTurnModal,
  selectedTurnLoadingPanel,
  turnPromptPreviewMarkup,
  updateSelectedTurnPromptOverflow,
};
}
