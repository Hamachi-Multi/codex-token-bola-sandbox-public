import { fmt, state } from './core.js';
import { esc } from './ui.js';

export function handleListArrowFocus(event, selector, activate = false) {
  const delta = event.key === 'ArrowDown' ? 1 : event.key === 'ArrowUp' ? -1 : 0;
  if (!delta) return false;
  const items = [...document.querySelectorAll(selector)]
    .filter(item => item.tabIndex >= 0 && !item.disabled && item.offsetParent !== null);
  const index = items.indexOf(event.currentTarget);
  if (index < 0 || !items.length) return false;
  event.preventDefault();
  const nextIndex = Math.max(0, Math.min(items.length - 1, index + delta));
  const next = items[nextIndex];
  next.focus({ preventScroll: true });
  next.scrollIntoView({ block: 'nearest' });
  if (activate) next.click();
  return true;
}

export function focusTargetForView(view = state.view) {
  const selectors = {
    overview: ['#projects tr.selected .row-select-button', '#projects tr[data-session-id] .row-select-button'],
    turns: ['#turn-list tr.selected .row-select-button', '#turn-list tr[data-turn] .row-select-button'],
    tools: ['#tool-output tr.selected .row-select-button', '#tool-output tr[data-tool] .row-select-button'],
    subagents: ['#subagent-rollups tr.selected .row-select-button', '#subagent-rollups tr[data-confidence] .row-select-button'],
    cleanup: ['#cleanup-files tr.selected .row-select-button', '#cleanup-files tr[data-cleanup-file] .row-select-button'],
    settings: ['#settings-list .settings-list-item.selected'],
  }[view] || [];
  for (const selector of selectors) {
    const target = document.querySelector(selector);
    if (target && target.tabIndex >= 0 && target.offsetParent !== null) return target;
  }
  return null;
}

export function setInteractiveRowSelected(row, selected) {
  if (!row) return;
  row.classList.toggle('selected', selected);
  row.querySelector('.row-select-button')?.setAttribute('aria-pressed', selected ? 'true' : 'false');
}

export function clearInteractiveRowSelection(selector) {
  document.querySelectorAll(selector).forEach(row => setInteractiveRowSelected(row, false));
}

export function focusActiveViewRow({ force = false, replacedControl = null } = {}) {
  const active = document.activeElement;
  const documentOwnsFocus = [document.body, document.documentElement].includes(active);
  const replacingFocusedControl = replacedControl instanceof HTMLElement
    && !replacedControl.isConnected
    && documentOwnsFocus;
  const pendingOwner = state.pendingViewFocusOwner;
  const pendingFocusStillOwned = !pendingOwner
    || active === pendingOwner
    || documentOwnsFocus;
  if (state.pendingViewFocus && !pendingFocusStillOwned && !replacingFocusedControl) {
    state.pendingViewFocus = false;
    state.pendingViewFocusOwner = null;
  }
  if (!force && !state.pendingViewFocus && !replacingFocusedControl) return false;
  const target = focusTargetForView();
  if (!target) return false;
  target.focus({ preventScroll: true });
  target.scrollIntoView({ block: 'nearest' });
  state.pendingViewFocus = false;
  state.pendingViewFocusOwner = null;
  return true;
}

export function restoreReplacedControlFocus(trigger, selector) {
  if (!(trigger instanceof HTMLElement) || trigger.isConnected) return false;
  if (![document.body, document.documentElement].includes(document.activeElement)) return false;
  const replacement = document.querySelector(selector);
  if (!(replacement instanceof HTMLElement) || replacement.offsetParent === null) return false;
  replacement.focus({ preventScroll: true });
  return true;
}

export function metric(label, value, kind = '', title = '', labelAddon = '') {
  const cls = kind ? `metric ${kind}` : 'metric';
  const titleAttr = title ? ` title="${esc(title)}"` : '';
  return `<div class="${cls}"><div class="label">${esc(label)}${labelAddon}</div><div class="value"${titleAttr}>${esc(value)}</div></div>`;
}

function defaultTurnSortDir(key) {
  return ['session', 'prompt', 'status'].includes(key) ? 'asc' : 'desc';
}

export function tableHeader(header, sortState = null) {
  const sortable = Boolean(header.sort);
  const sortKey = sortState ? sortState.key : state.turnSortKey;
  const sortDir = sortState ? sortState.dir : state.turnSortDir;
  const sortAttribute = sortState ? sortState.attribute : 'data-turn-sort';
  const defaultDir = sortState && typeof sortState.defaultDir === 'function' ? sortState.defaultDir : defaultTurnSortDir;
  const active = sortable && sortKey === header.sort;
  const classes = [header.cls || '', sortable ? 'sortable-header' : '', active ? 'sorted' : ''].filter(Boolean).join(' ');
  const ariaSort = sortable ? ` aria-sort="${active ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}"` : '';
  const ariaHidden = header.ariaHidden ? ' aria-hidden="true"' : '';
  if (!sortable) return `<th class="${classes}"${ariaSort}${ariaHidden}>${esc(header.label)}</th>`;
  const dir = active ? sortDir : defaultDir(header.sort);
  return `<th class="${classes}"${ariaSort}><button type="button" class="sort-button" ${sortAttribute}="${esc(header.sort)}" title="Sort by ${esc(header.label)}" aria-label="Sort by ${esc(header.label)}"><span class="sort-label">${esc(header.label)}</span><span class="sort-indicator ${esc(dir)}" aria-hidden="true"></span></button></th>`;
}

export function table(headers, rows, sortState = null) {
  if (!rows.length) return '<div class="empty">No rows for the current filter.</div>';
  return `<div class="table-scroll scrollbar-hidden"><div class="table-header-shadow" aria-hidden="true"></div><table><thead><tr>${headers.map(header => tableHeader(header, sortState)).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
}

export function loadingPanel(label = 'Loading panel data.') {
  return `<span class="sr-only">${esc(label)}</span><div class="loading-lines" aria-hidden="true"><div class="loading-line"></div><div class="loading-line"></div><div class="loading-line"></div></div>`;
}

function detailMetricSkeleton(kind = '') {
  const cls = kind ? `detail-cell ${kind}` : 'detail-cell';
  return `<div class="${cls}"><div class="label"><span class="loading-line loading-label"></span></div><div class="value"><span class="loading-line loading-value"></span></div></div>`;
}

function sectionTitleSkeleton(cls) {
  return `<div class="${cls}"><span class="loading-line loading-title"></span></div>`;
}

function tableSkeleton(rowCount = 4, columnCount = 4) {
  const head = Array.from({length: columnCount}).map((_, index) => `<th${index ? ' class="num"' : ''}><div class="loading-line"></div></th>`).join('');
  const cells = Array.from({length: columnCount}).map((_, index) => `<td${index ? ' class="num"' : ''}><div class="loading-line"></div></td>`).join('');
  const rows = Array.from({length: rowCount}).map(() => `<tr>${cells}</tr>`).join('');
  return `<div class="table-scroll table-skeleton scrollbar-hidden" aria-hidden="true"><div class="table-header-shadow" aria-hidden="true"></div><table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>`;
}

export function sessionDetailLoadingPanel(label = 'Loading session detail.') {
  return `<span class="sr-only">${esc(label)}</span>
    <div class="session-detail-summary" aria-hidden="true">
      ${detailMetricSkeleton()}${detailMetricSkeleton()}${detailMetricSkeleton()}${detailMetricSkeleton()}
    </div>
    <div class="session-detail-section" aria-hidden="true">
      ${sectionTitleSkeleton('session-detail-section-title')}
      ${tableSkeleton(6, 4)}
    </div>
    <div class="session-detail-section" aria-hidden="true">
      ${sectionTitleSkeleton('session-detail-section-title')}
      ${tableSkeleton(6, 3)}
    </div>
    <div class="session-detail-section" aria-hidden="true">
      ${sectionTitleSkeleton('session-detail-section-title')}
      ${tableSkeleton(6, 4)}
    </div>
    <div class="session-detail-section" aria-hidden="true">
      ${sectionTitleSkeleton('session-detail-section-title')}
      ${tableSkeleton(6, 4)}
    </div>`;
}

export function detailGridLoadingPanel(label = 'Loading detail data.', distributionColumns = 5, detailColumns = 3, distributionRows = 10, detailRows = 10) {
  return `<span class="sr-only">${esc(label)}</span><div class="tool-detail-summary" aria-hidden="true">
    <div class="detail-grid tool-detail-grid">
      <div class="detail-cell tool-name-cell"><div class="value attribution-method-value"><span class="method-name"><span class="loading-line loading-value"></span></span><span class="method-desc"><span class="loading-line loading-label"></span></span></div></div>
      ${detailMetricSkeleton()}${detailMetricSkeleton()}${detailMetricSkeleton()}
    </div>
    ${sectionTitleSkeleton('tool-detail-section-title')}
    ${tableSkeleton(distributionRows, distributionColumns)}
    ${sectionTitleSkeleton('tool-detail-section-title')}
    ${tableSkeleton(detailRows, detailColumns)}
  </div>`;
}

export function tableLoadingPanel(label = 'Loading table data.', rowCount = 6, columnCount = 4) {
  return `<span class="sr-only">${esc(label)}</span>${tableSkeleton(rowCount, columnCount)}`;
}

export function setPanelContent(id, html, mode = '') {
  const el = document.getElementById(id);
  if (!el) return false;
  el.classList.remove('empty', 'loading', 'error');
  if (mode) el.classList.add(mode);
  el.innerHTML = html;
  return true;
}

function setTextIfPresent(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

export function setGlobalError(message) {
  const text = esc(message || 'Dashboard data could not be loaded.');
  ['projects', 'session-detail', 'turn-list', 'tool-output', 'tool-detail', 'subagent-rollups', 'subagent-mix', 'cleanup-files'].forEach(id => {
    setPanelContent(id, text, 'error');
  });
  setTextIfPresent('session-detail-status', 'error');
  setTextIfPresent('tool-detail-status', 'error');
  setTextIfPresent('subagent-detail-status', 'error');
  setTextIfPresent('detail-status', 'error');
  showQueryError(message || 'Dashboard data could not be loaded.', 0);
}

let queryStatusTimer = null;

export function clearQueryStatus() {
  if (queryStatusTimer !== null) {
    clearTimeout(queryStatusTimer);
    queryStatusTimer = null;
  }
  const status = document.getElementById('query-status');
  if (!status) return;
  status.textContent = '';
  status.hidden = true;
}

export function showQueryError(message, timeoutMs = 6000) {
  clearQueryStatus();
  const status = document.getElementById('query-status');
  if (!status) return;
  status.textContent = String(message || 'Dashboard data could not be loaded.');
  status.hidden = false;
  if (timeoutMs > 0) queryStatusTimer = setTimeout(clearQueryStatus, timeoutMs);
}

const scrollFadeSelector = [
  '#projects',
  '#session-detail',
  '#turn-list',
  '#tool-output',
  '#tool-detail',
  '#subagent-rollups',
  '#subagent-mix',
  '#detail',
  '.turn-prompt.expanded',
].join(',');

export function updateScrollFade(el) {
  if (!el) return;
  const canScroll = el.scrollHeight - el.clientHeight > 1;
  const canScrollUp = canScroll && el.scrollTop > 1;
  const canScrollDown = canScroll && el.scrollHeight - el.scrollTop - el.clientHeight > 1;
  el.classList.toggle('scroll-fade-target', canScroll);
  el.classList.toggle('can-scroll-up', canScrollUp);
  el.classList.toggle('can-scroll-down', canScrollDown);
}

export function refreshScrollFades(root = document) {
  requestAnimationFrame(() => {
    root.querySelectorAll(scrollFadeSelector).forEach(el => {
      if (el.dataset.scrollFadeBound !== '1') {
        el.dataset.scrollFadeBound = '1';
        el.addEventListener('scroll', () => updateScrollFade(el), { passive: true });
      }
      updateScrollFade(el);
    });
  });
}

export function detailMetric(label, value, kind = '', title = '') {
  const cls = kind ? `detail-cell ${kind}` : 'detail-cell';
  const titleAttr = title ? ` title="${esc(title)}"` : '';
  return `<div class="${cls}"><div class="label">${esc(label)}</div><div class="value"${titleAttr}>${esc(value)}</div></div>`;
}
