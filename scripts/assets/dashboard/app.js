import {
  DEFAULT_TURN_PAGE_SIZE,
  ROLLUP_SORT_DEFAULTS,
  ROLLUP_SORT_KEYS,
  SETTINGS_KEY,
  TURN_SORT_KEYS,
  TURN_SORT_LABELS,
  fmt,
  state,
  views,
} from './core.js';
import { getJSON } from './api.js';
import {
  clearAnalyticsQueryCache,
  getCachedJSON,
  peekCachedJSON,
  prefetchJSON,
  primeCachedJSON,
  setAnalyticsCacheGeneration,
} from './query-cache.js';
import { esc } from './ui.js';
import { createCleanupController, normalizeCleanupRetentionMode } from './cleanup.js';
import { markCleanupPreviewUnavailable } from './cleanup-retention.js';
import {
  clearInteractiveRowSelection,
  focusActiveViewRow,
  handleListArrowFocus,
  detailGridLoadingPanel,
  metric,
  refreshScrollFades,
  restoreReplacedControlFocus,
  sessionDetailLoadingPanel,
  clearQueryStatus,
  setGlobalError,
  setActiveModal,
  setInteractiveRowSelected,
  setPanelContent,
  showQueryError,
  table,
  tableLoadingPanel,
  trapModalFocus,
} from './dom.js';
import {
  compactDateTime,
  compactNumber,
  compactNumberSpan,
  exactNumber,
  formatBytes,
  sessionLabel,
  sessionLabelMarkup,
  turnStatusClass,
} from './formatters.js';
import { createSelectedTurnController } from './selected-turn.js';
import { createAnalyzeController } from './analyze.js';
import { createSessionPickerController } from './session-picker.js';
import { createToolbarController, selectHasValue } from './toolbar.js';
import { createOverviewRenderers } from './overview-render.js';

export function initDashboard() {

function storageAvailable() {
  try {
    const key = SETTINGS_KEY + ':probe';
    localStorage.setItem(key, '1');
    localStorage.removeItem(key);
    return true;
  } catch {
    return false;
  }
}

const canStoreSettings = storageAvailable();
const THEME_TRANSITION_MS = 160;
let themeCommitTimer = 0;
let systemThemeMedia = null;
let systemThemeSync = null;

function normalizeTurnSortKey(value) {
  if (value === 'time' || value === 'clock') return 'date';
  if (value === 'project') return 'session';
  return TURN_SORT_KEYS.has(value) ? value : 'date';
}

function normalizeTurnSortDir(value) {
  return value === 'asc' ? 'asc' : 'desc';
}

function normalizeListSortKind(value) {
  return Object.prototype.hasOwnProperty.call(ROLLUP_SORT_DEFAULTS, value) ? value : 'projects';
}

function normalizeListSortKey(kind, value) {
  const normalizedKind = normalizeListSortKind(kind);
  return ROLLUP_SORT_KEYS[normalizedKind].has(value) ? value : ROLLUP_SORT_DEFAULTS[normalizedKind].key;
}

function normalizeListSortDir(value) {
  return value === 'asc' ? 'asc' : 'desc';
}

function normalizeThemeMode(value) {
  return value === 'dark' ? 'dark' : 'light';
}

function storedThemeMode(settings = {}) {
  return settings.themeMode === 'dark' || settings.themeMode === 'light' ? settings.themeMode : '';
}

function systemThemeMode() {
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function resolveInitialThemeMode(settings = {}) {
  return storedThemeMode(settings) || systemThemeMode();
}

function unbindSystemThemePreference() {
  if (!systemThemeMedia || !systemThemeSync) return;
  if (typeof systemThemeMedia.removeEventListener === 'function') {
    systemThemeMedia.removeEventListener('change', systemThemeSync);
  } else if (typeof systemThemeMedia.removeListener === 'function') {
    systemThemeMedia.removeListener(systemThemeSync);
  }
  systemThemeMedia = null;
  systemThemeSync = null;
}

function bindSystemThemePreference(settings = readSettings()) {
  unbindSystemThemePreference();
  if (storedThemeMode(settings) || typeof window.matchMedia !== 'function') return;
  systemThemeMedia = window.matchMedia('(prefers-color-scheme: dark)');
  const sync = () => {
    if (storedThemeMode(readSettings())) {
      unbindSystemThemePreference();
      return;
    }
    applyThemeMode(systemThemeMode(), {suppressTransitions: true});
  };
  systemThemeSync = sync;
  if (typeof systemThemeMedia.addEventListener === 'function') {
    systemThemeMedia.addEventListener('change', sync);
  } else if (typeof systemThemeMedia.addListener === 'function') {
    systemThemeMedia.addListener(sync);
  }
}

function releaseThemeCommit() {
  const root = document.documentElement;
  root.classList.remove('theme-commit');
  themeCommitTimer = 0;
}

function commitThemeMode(normalized, {suppressTransitions = false} = {}) {
  const root = document.documentElement;
  if (themeCommitTimer) window.clearTimeout(themeCommitTimer);
  if (suppressTransitions) root.classList.add('theme-commit');
  state.themeMode = normalized;
  document.querySelectorAll('[data-theme-mode]').forEach(button => {
    const active = button.dataset.themeMode === normalized;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  root.dataset.theme = normalized;
  if (suppressTransitions) {
    themeCommitTimer = window.setTimeout(releaseThemeCommit, THEME_TRANSITION_MS);
  } else {
    releaseThemeCommit();
  }
}

function applyThemeMode(mode, {transition = false, suppressTransitions = false} = {}) {
  const normalized = normalizeThemeMode(mode);
  const canViewTransition = transition
    && !suppressTransitions
    && state.themeMode !== normalized
    && typeof document.startViewTransition === 'function'
    && !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  document.documentElement.style.setProperty('--theme-transition-duration', `${THEME_TRANSITION_MS}ms`);
  if (canViewTransition) {
    const viewTransition = document.startViewTransition(() => commitThemeMode(normalized, {suppressTransitions: true}));
    return viewTransition.updateCallbackDone.catch(() => {});
  }
  commitThemeMode(normalized, {suppressTransitions: suppressTransitions || (transition && state.themeMode !== normalized)});
  return Promise.resolve();
}

function applyThemeModeAndSave(mode) {
  state.themeModeExplicit = true;
  unbindSystemThemePreference();
  applyThemeMode(mode, {transition: true}).then(saveSettings);
}

function defaultTurnSortDir(key) {
  return ['session', 'prompt', 'status'].includes(key) ? 'asc' : 'desc';
}

function defaultListSortDir(kind, key) {
  const normalizedKind = normalizeListSortKind(kind);
  if (normalizedKind === 'projects') return key === 'session' ? 'asc' : 'desc';
  if (normalizedKind === 'tools') return key === 'tool_name' ? 'asc' : 'desc';
  if (normalizedKind === 'subagents') return key === 'confidence' ? 'asc' : 'desc';
  return 'desc';
}

function listTableSortState(kind) {
  const normalizedKind = normalizeListSortKind(kind);
  const sort = state.listSorts[normalizedKind] || ROLLUP_SORT_DEFAULTS[normalizedKind];
  return {
    key: sort.key,
    dir: sort.dir,
    attribute: 'data-list-sort',
    defaultDir: key => defaultListSortDir(normalizedKind, key),
  };
}

function turnSortSummary() {
  const label = TURN_SORT_LABELS[state.turnSortKey] || 'Date';
  return `Sorted by ${label} ${state.turnSortDir === 'asc' ? 'asc' : 'desc'}`;
}

function readSettings() {
  if (!canStoreSettings) return {};
  try {
    return JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}') || {};
  } catch {
    return {};
  }
}

function persistedDaysSetting() {
  const value = document.getElementById('days').value;
  if (value === 'custom' && state.appliedDaysMode !== 'custom') {
    return state.appliedDaysMode;
  }
  return value;
}

function saveSettings() {
  if (!canStoreSettings) return;
  const payload = {
    view: state.view,
    days: persistedDaysSetting(),
    customDays: document.getElementById('custom-days').value,
    session_id: sessionFilterValue(),
    turnPageSize: String(state.turnPageSize),
    turnSortKey: state.turnSortKey,
    turnSortDir: state.turnSortDir,
    listSorts: state.listSorts,
    cleanupRetentionMode: state.cleanupRetentionMode,
    cleanupRetentionDate: document.getElementById('cleanup-retention-date').value,
  };
  if (state.themeModeExplicit) payload.themeMode = state.themeMode;
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(payload));
}

function restoreSettings() {
  const settings = readSettings();
  state.themeModeExplicit = Boolean(storedThemeMode(settings));
  applyThemeMode(resolveInitialThemeMode(settings), {suppressTransitions: true});
  bindSystemThemePreference(settings);
  restoreToolbarSettings(settings);
  restoreSessionFilter(settings);
  if (selectHasValue('turn-page-size', settings.turnPageSize)) {
    document.getElementById('turn-page-size').value = String(settings.turnPageSize);
    state.turnPageSize = Number(settings.turnPageSize || DEFAULT_TURN_PAGE_SIZE);
  }
  state.turnSortKey = normalizeTurnSortKey(settings.turnSortKey);
  state.turnSortDir = normalizeTurnSortDir(settings.turnSortDir);
  Object.keys(ROLLUP_SORT_DEFAULTS).forEach(kind => {
    const saved = (settings.listSorts || {})[kind] || {};
    const key = normalizeListSortKey(kind, saved.key);
    state.listSorts[kind] = {
      key,
      dir: saved.dir === 'asc' || saved.dir === 'desc' ? normalizeListSortDir(saved.dir) : defaultListSortDir(kind, key),
    };
  });
  const cleanupMode = normalizeCleanupRetentionMode(settings.cleanupRetentionMode);
  if (typeof settings.cleanupRetentionDate === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(settings.cleanupRetentionDate)) {
    document.getElementById('cleanup-retention-date').value = settings.cleanupRetentionDate;
  }
  setCleanupRetentionMode(cleanupMode);
  return views.has(settings.view) ? settings.view : 'overview';
}

function setView(name, updateHash = true, { focusContent = true } = {}) {
  const view = views.has(name) ? name : 'overview';
  state.view = view;
  document.querySelectorAll('.view').forEach(section => {
    section.classList.toggle('active', section.dataset.view === view);
  });
  document.querySelectorAll('.nav-btn').forEach(button => {
    button.classList.toggle('active', button.dataset.viewTarget === view);
    button.setAttribute('aria-current', button.dataset.viewTarget === view ? 'page' : 'false');
  });
  if (updateHash && location.hash.slice(1) !== view) {
    history.replaceState(null, '', '#' + view);
  }
  saveSettings();
  state.pendingViewFocus = Boolean(focusContent);
  state.pendingViewFocusOwner = focusContent && document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null;
  if (view === 'cleanup') loadCleanup();
  if (view !== 'cleanup' && state.requestSeq > 0) loadVisibleRollupData(state.requestSeq);
  if (view !== 'cleanup' && focusContent) focusActiveViewRow();
  refreshScrollFades();
  requestAnimationFrame(updateSelectedTurnPromptOverflow);
}

function params() {
  const sessionId = sessionFilterValue();
  const q = new URLSearchParams({ days: timeRangeDaysValue() });
  if (sessionId) q.set('session_id', sessionId);
  return q;
}

function turnsParams() {
  const q = params();
  q.set('page', String(state.turnPage));
  q.set('per_page', String(state.turnPageSize));
  q.set('sort', state.turnSortKey);
  q.set('sort_dir', state.turnSortDir);
  q.set('sessions_page', String(state.listPages.projects || 1));
  q.set('tools_page', String(state.listPages.tools || 1));
  q.set('session_sort', state.listSorts.projects.key);
  q.set('session_sort_dir', state.listSorts.projects.dir);
  q.set('tool_sort', state.listSorts.tools.key);
  q.set('tool_sort_dir', state.listSorts.tools.dir);
  return q;
}

function turnsPath(page = state.turnPage) {
  const q = params();
  q.set('page', String(page));
  q.set('per_page', String(state.turnPageSize));
  q.set('sort', state.turnSortKey);
  q.set('sort_dir', state.turnSortDir);
  return '/api/turns?' + q;
}

function sessionsPath(page = state.listPages.projects || 1) {
  const q = params();
  q.set('per_page', String(state.turnPageSize));
  q.set('sessions_page', String(page));
  q.set('session_sort', state.listSorts.projects.key);
  q.set('session_sort_dir', state.listSorts.projects.dir);
  return '/api/sessions?' + q;
}

function toolsPath(page = state.listPages.tools || 1) {
  const q = params();
  q.set('per_page', String(state.turnPageSize));
  q.set('tools_page', String(page));
  q.set('tool_sort', state.listSorts.tools.key);
  q.set('tool_sort_dir', state.listSorts.tools.dir);
  return '/api/tools?' + q;
}

function subagentsPath() {
  const q = params();
  q.set('subagent_sort', state.listSorts.subagents.key);
  q.set('subagent_sort_dir', state.listSorts.subagents.dir);
  return '/api/subagents?' + q;
}

function turnDetailPath(sessionId, turnId) {
  const q = params();
  q.set('session_id', sessionId);
  q.set('turn_id', turnId);
  return '/api/turn?' + q;
}

function sessionDetailPath(sessionId) {
  const q = params();
  q.set('selected_session_id', sessionId);
  return '/api/session-detail?' + q;
}

function toolDetailPath(toolName) {
  const q = params();
  q.set('tool_name', toolName);
  return '/api/tool?' + q;
}

function subagentDetailPath(confidence) {
  const q = params();
  q.set('confidence', confidence);
  return '/api/subagent?' + q;
}

function resetTurnPage() {
  state.turnPage = 1;
}

function resetListPages() {
  state.listPages = { projects: 1, tools: 1 };
}

function resetAllPages() {
  resetTurnPage();
  resetListPages();
}

function setTurnSort(key, trigger = null) {
  const previous = {
    key: state.turnSortKey,
    dir: state.turnSortDir,
    page: state.turnPage,
  };
  const nextKey = normalizeTurnSortKey(key);
  if (state.turnSortKey === nextKey) {
    state.turnSortDir = state.turnSortDir === 'asc' ? 'desc' : 'asc';
  } else {
    state.turnSortKey = nextKey;
    state.turnSortDir = defaultTurnSortDir(nextKey);
  }
  resetTurnPage();
  saveSettings();
  safeLoadTurnPage(
    1,
    () => {
      state.turnSortKey = previous.key;
      state.turnSortDir = previous.dir;
      state.turnPage = previous.page;
      saveSettings();
    },
    () => restoreReplacedControlFocus(trigger, `#turn-list [data-turn-sort="${CSS.escape(nextKey)}"]`),
  );
}

function setListSort(kind, key, trigger = null) {
  const normalizedKind = normalizeListSortKind(kind);
  const previous = {
    sort: {...(state.listSorts[normalizedKind] || ROLLUP_SORT_DEFAULTS[normalizedKind])},
    page: state.listPages[normalizedKind],
  };
  const nextKey = normalizeListSortKey(normalizedKind, key);
  const current = state.listSorts[normalizedKind] || ROLLUP_SORT_DEFAULTS[normalizedKind];
  if (current.key === nextKey) {
    state.listSorts[normalizedKind] = { key: nextKey, dir: current.dir === 'asc' ? 'desc' : 'asc' };
  } else {
    state.listSorts[normalizedKind] = { key: nextKey, dir: defaultListSortDir(normalizedKind, nextKey) };
  }
  if (normalizedKind in state.listPages) state.listPages[normalizedKind] = 1;
  saveSettings();
  const rootId = {projects: 'projects', tools: 'tool-output', subagents: 'subagent-rollups'}[normalizedKind];
  safeLoadListPage(
    normalizedKind,
    1,
    () => {
      state.listSorts[normalizedKind] = previous.sort;
      if (normalizedKind in state.listPages) state.listPages[normalizedKind] = previous.page || 1;
      saveSettings();
    },
    () => restoreReplacedControlFocus(trigger, `#${rootId} [data-list-sort="${CSS.escape(nextKey)}"]`),
  );
}

function setLoading() {
  setPanelContent('projects', tableLoadingPanel('Loading session rows.', 14, 4), 'loading');
  setPanelContent('session-detail', sessionDetailLoadingPanel('Loading session detail.'), 'loading');
  setPanelContent('turn-list', tableLoadingPanel('Loading turn rows.', 16, 5), 'loading');
  setPanelContent('tool-output', tableLoadingPanel('Loading tool rows.', 16, 4), 'loading');
  setPanelContent('tool-detail', detailGridLoadingPanel('Loading tool detail.'), 'loading');
  setPanelContent('subagent-rollups', tableLoadingPanel('Loading attribution rows.', 5, 4), 'loading');
  setPanelContent('subagent-mix', detailGridLoadingPanel('Loading attribution detail.', 6, 4, 4, 4), 'loading');
  state.selectedSession = null;
  state.sessionSeq += 1;
  document.getElementById('session-detail-status').textContent = 'select a session';
  state.selectedTool = null;
  state.toolSeq += 1;
  document.getElementById('tool-detail-status').textContent = 'select a row';
  state.selectedSubagentConfidence = null;
  state.subagentSeq += 1;
  document.getElementById('subagent-detail-status').textContent = 'select a row';
  document.getElementById('turn-pager').innerHTML = '';
  state.selected = null;
  state.detailData = null;
  state.promptExpanded = false;
  state.toolSummaryExpanded = false;
  state.detailSeq += 1;
  document.getElementById('turn-count').textContent = '';
  document.getElementById('detail-status').textContent = 'none';
  setPanelContent('detail', 'Select a row to inspect details.', 'empty');
  setPanelContent('subagent-mix', 'Select a row to inspect details.', 'empty');
  clearListPagers();
  document.getElementById('summary').innerHTML = [
    metric('Analyzed Turns', '...'),
    metric('Cost Units', '...'),
    metric('Total Tokens', '...'),
    metric('Cached Input', '...'),
    metric('Non-Cached Input', '...'),
    metric('Model Calls', '...'),
    metric('Tool Calls', '...'),
  ].join('');
  refreshScrollFades();
}

function freshnessIndicator(freshness) {
  const data = freshness || {};
  const status = String(data.status || 'unknown');
  const pendingRows = Number(data.pending_raw_rows || 0);
  const pendingAnalysisRows = Number(data.pending_analysis_rows ?? pendingRows);
  const pendingRecoveryFiles = Number(data.pending_recovery_files || 0);
  let title = '';
  if (status === 'needs_analyze' && pendingAnalysisRows > 0) {
    title = `global pending · ${compactNumber(pendingAnalysisRows)} rows`;
    if (data.data_health === 'degraded') title = `${title} · freshness degraded`;
  } else if (status === 'needs_analyze' && pendingRecoveryFiles > 0) {
    title = `global recovery pending · ${compactNumber(pendingRecoveryFiles)} files`;
    if (data.data_health === 'degraded') title = `${title} · freshness degraded`;
  } else if (status === 'degraded' || data.data_health === 'degraded') {
    title = 'global freshness degraded';
    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
    const firstWarning = warnings.length ? String(warnings[0].code || '') : '';
    if (firstWarning) title = `${title} · ${firstWarning}`;
  } else {
    if (status !== 'current') return '';
    title = 'global current';
  }
  return `<span class="metric-freshness-dot" data-freshness-state="${esc(status)}" data-tooltip="${esc(title)}" aria-label="${esc(title)}" tabindex="0"></span>`;
}

function setPagerBusy(id, busy) {
  const pager = document.getElementById(id);
  if (!pager) return;
  pager.setAttribute('aria-busy', busy ? 'true' : 'false');
  pager.querySelectorAll('button').forEach(button => {
    if (busy) {
      button.dataset.wasDisabled = button.disabled ? 'true' : 'false';
      button.disabled = true;
    } else if (button.dataset.wasDisabled === 'false') {
      button.disabled = false;
    }
  });
}

function renderPager(total, page, perPage) {
  const pageCount = Math.max(1, Math.ceil(total / perPage));
  const start = total ? (page - 1) * perPage + 1 : 0;
  const end = Math.min(total, page * perPage);
  document.getElementById('turn-pager').innerHTML = `
    <button id="prev-page" ${page <= 1 ? 'disabled' : ''}>Prev</button>
    <span class="page-status">${fmt.format(start)}-${fmt.format(end)} / ${fmt.format(total)}</span>
    <button id="next-page" ${page >= pageCount ? 'disabled' : ''}>Next</button>
  `;
  document.getElementById('prev-page')?.addEventListener('click', () => {
    if (state.turnPage > 1) {
      safeLoadTurnPage(state.turnPage - 1);
    }
  });
  document.getElementById('next-page')?.addEventListener('click', () => {
    if (state.turnPage < pageCount) {
      safeLoadTurnPage(state.turnPage + 1);
    }
  });
}

function pageRows(payload) {
  return Array.isArray(payload) ? payload : ((payload || {}).rows || []);
}

function targetRow(rows, key, selected) {
  return rows.find(row => String(row[key] || '') === String(selected || '')) || rows[0] || null;
}

async function cachedValue(path) {
  const cached = peekCachedJSON(path);
  return cached.hit ? cached.data : getCachedJSON(path);
}

async function prepareDetail(key, path) {
  try {
    return { key, data: await cachedValue(path) };
  } catch (error) {
    return { key, error };
  }
}

async function prepareSessionDetail(payload) {
  const row = targetRow(pageRows(payload), 'session_id', state.selectedSession);
  if (!row) return null;
  const key = row.session_id || '';
  return prepareDetail(key, sessionDetailPath(key));
}

async function prepareToolDetail(payload) {
  const row = targetRow(pageRows(payload), 'tool_name', state.selectedTool);
  if (!row) return null;
  const key = row.tool_name || '';
  return prepareDetail(key, toolDetailPath(key));
}

async function prepareSubagentDetail(payload) {
  const row = targetRow(pageRows(payload), 'confidence', state.selectedSubagentConfidence);
  if (!row) return null;
  const key = row.confidence || '';
  return prepareDetail(key, subagentDetailPath(key));
}

function prefetchNextPage(payload, pathForPage, detailPathForRow) {
  const total = Number((payload || {}).total || 0);
  const perPage = Math.max(1, Number((payload || {}).per_page || state.turnPageSize));
  const page = Math.max(1, Number((payload || {}).page || 1));
  if (page >= Math.max(1, Math.ceil(total / perPage))) return;
  prefetchJSON(pathForPage(page + 1)).then(nextPayload => {
    const first = pageRows(nextPayload)[0];
    if (first) prefetchJSON(detailPathForRow(first));
  });
}

async function loadOverviewData(seq = state.requestSeq, page = state.listPages.projects || 1, busy = false) {
  const listSeq = ++state.sessionListSeq;
  const path = sessionsPath(page);
  if (busy && !peekCachedJSON(path).hit) setPagerBusy('projects-pager', true);
  try {
    const sessions = await cachedValue(path);
    const prepared = await prepareSessionDetail(sessions);
    if (seq !== state.requestSeq || listSeq !== state.sessionListSeq) return false;
    renderSessionList(sessions, prepared);
    if (prepared?.error) showQueryError(prepared.error.message || prepared.error);
    prefetchNextPage(sessions, sessionsPath, row => sessionDetailPath(row.session_id || ''));
    refreshScrollFades();
    return true;
  } catch (error) {
    if (seq !== state.requestSeq || listSeq !== state.sessionListSeq) return false;
    throw error;
  } finally {
    if (listSeq === state.sessionListSeq) setPagerBusy('projects-pager', false);
  }
}

async function loadToolsData(seq = state.requestSeq, page = state.listPages.tools || 1, busy = false) {
  const listSeq = ++state.toolListSeq;
  const path = toolsPath(page);
  if (busy && !peekCachedJSON(path).hit) setPagerBusy('tool-output-pager', true);
  try {
    const tools = await cachedValue(path);
    const prepared = await prepareToolDetail(tools);
    if (seq !== state.requestSeq || listSeq !== state.toolListSeq) return false;
    renderToolList(tools, prepared);
    if (prepared?.error) showQueryError(prepared.error.message || prepared.error);
    prefetchNextPage(tools, toolsPath, row => toolDetailPath(row.tool_name || ''));
    refreshScrollFades();
    return true;
  } catch (error) {
    if (seq !== state.requestSeq || listSeq !== state.toolListSeq) return false;
    throw error;
  } finally {
    if (listSeq === state.toolListSeq) setPagerBusy('tool-output-pager', false);
  }
}

async function loadSubagentData(seq = state.requestSeq) {
  const listSeq = ++state.subagentListSeq;
  try {
    const subagents = await cachedValue(subagentsPath());
    const prepared = await prepareSubagentDetail(subagents);
    if (seq !== state.requestSeq || listSeq !== state.subagentListSeq) return false;
    renderSubagentList((subagents || {}).rows || [], prepared);
    if (prepared?.error) showQueryError(prepared.error.message || prepared.error);
    refreshScrollFades();
    return true;
  } catch (error) {
    if (seq !== state.requestSeq || listSeq !== state.subagentListSeq) return false;
    throw error;
  }
}

function safeLoadListPage(kind, page, onError = null, onCommit = null) {
  clearQueryStatus();
  const action = kind === 'projects'
    ? loadOverviewData(state.requestSeq, page, true)
    : kind === 'tools'
      ? loadToolsData(state.requestSeq, page, true)
      : loadSubagentData(state.requestSeq);
  action
    .then(committed => {
      if (committed && typeof onCommit === 'function') onCommit();
    })
    .catch(err => {
      if (typeof onError === 'function') onError();
      showQueryError(err.message || err);
      refreshScrollFades();
    });
}

function requestListPage(kind, page) {
  safeLoadListPage(kind, page);
}

function loadVisibleRollupData(seq = state.requestSeq) {
  if (state.view === 'overview') {
    loadOverviewData(seq).catch(err => {
      if (seq === state.requestSeq) {
        document.getElementById('session-detail-status').textContent = 'error';
        setPanelContent('projects', esc(err.message || err), 'error');
        setPanelContent('session-detail', 'Unable to load session detail.', 'error');
        refreshScrollFades();
      }
    });
  } else if (state.view === 'tools') {
    loadToolsData(seq).catch(err => {
      if (seq === state.requestSeq) {
        document.getElementById('tool-detail-status').textContent = 'error';
        setPanelContent('tool-output', esc(err.message || err), 'error');
        setPanelContent('tool-detail', 'Unable to load tool detail.', 'error');
        refreshScrollFades();
      }
    });
  } else if (state.view === 'subagents') {
    loadSubagentData(seq).catch(err => {
      if (seq === state.requestSeq) {
        document.getElementById('subagent-detail-status').textContent = 'error';
        setPanelContent('subagent-rollups', esc(err.message || err), 'error');
        setPanelContent('subagent-mix', 'Unable to load attribution detail.', 'error');
        refreshScrollFades();
      }
    });
  }
}


async function prepareTurnDetail(turns) {
  const row = targetTurnRow(turns);
  if (!row) return null;
  const path = turnDetailPath(row.session_id || '', row.turn_id || '');
  const key = `${row.session_id || ''}\u0000${row.turn_id || ''}`;
  return prepareDetail(key, path);
}

function targetTurnRow(turns) {
  const rows = pageRows(turns);
  return rows.find(item => state.selected
    && String(item.session_id || '') === String(state.selected.session || '')
    && String(item.turn_id || '') === String(state.selected.turn || '')) || rows[0] || null;
}

function commitTurnRow(row, detail) {
  clearInteractiveRowSelection('#turn-list tr[data-turn]');
  setInteractiveRowSelected(row, true);
  state.selected = { session: row.dataset.session, turn: row.dataset.turn };
  state.promptExpanded = false;
  state.toolSummaryExpanded = false;
  state.detailData = detail;
  setPanelContent('detail', renderDetailSummary(detail));
  bindDetailControls(() => selectTurnRow(row));
  refreshScrollFades();
}

async function selectTurnRow(row, preparedDetail) {
  clearQueryStatus();
  const path = turnDetailPath(row.dataset.session || '', row.dataset.turn || '');
  const detailSeq = ++state.detailSeq;
  if (preparedDetail !== undefined) {
    commitTurnRow(row, preparedDetail);
    return;
  }
  const cached = peekCachedJSON(path);
  if (cached.hit) {
    commitTurnRow(row, cached.data);
    return;
  }
  const previous = document.querySelector('#turn-list tr.selected');
  const previousStatus = document.getElementById('detail-status').textContent;
  clearInteractiveRowSelection('#turn-list tr[data-turn]');
  setInteractiveRowSelected(row, true);
  row.setAttribute('aria-busy', 'true');
  try {
    const detail = await getCachedJSON(path);
    if (detailSeq !== state.detailSeq || !row.isConnected) return;
    commitTurnRow(row, detail);
  } catch (err) {
    if (detailSeq === state.detailSeq && row.isConnected) {
      setInteractiveRowSelected(row, false);
      if (previous && previous.isConnected) {
        setInteractiveRowSelected(previous, true);
      }
      document.getElementById('detail-status').textContent = previousStatus;
      if (!previous || !previous.isConnected) {
        state.selected = null;
        state.detailData = null;
        document.getElementById('detail-status').textContent = 'error';
        setPanelContent('detail', esc(err.message || err), 'error');
      }
      showQueryError(err.message || err);
      refreshScrollFades();
    }
  } finally {
    if (row.isConnected) row.removeAttribute('aria-busy');
  }
}

function renderTurnPage(turns, prepared = null) {
  state.turnPage = Math.max(1, Number(turns.page || state.turnPage || 1));
  document.getElementById('turn-count').textContent = turns.focused
    ? (turns.total ? 'Linked turn' : 'Linked turn not in scope')
    : `${turnSortSummary()}: ${compactNumber(turns.total || turns.rows.length)} turns · list uses date/session filters`;
  setPanelContent('turn-list', table(
    [{label:'Date', sort:'date'}, {label:'Session', sort:'session'}, {label:'Prompt', sort:'prompt'}, {label:'Cost Units', sort:'credits', cls:'num'}, {label:'Total Tokens', sort:'raw', cls:'num'}],
    turns.rows.map(r => {
      const label = sessionLabel(r);
      const status = r.turn_status || 'unknown';
      const tokenAvailable = Number(r.token_data_available ?? 1) !== 0;
      const resolutionReason = r.token_resolution_reason || 'Token data unavailable';
      const resolutionMeta = tokenAvailable ? '' : `<span class="status token-unavailable" title="${esc(resolutionReason)}">Token unavailable</span>`;
      const rawValue = tokenAvailable ? compactNumberSpan(r.raw) : `<span class="token-unavailable-value" title="${esc(resolutionReason)}">—</span>`;
      const creditValue = tokenAvailable ? compactNumberSpan(r.credits, 'money') : `<span class="token-unavailable-value" title="${esc(resolutionReason)}">—</span>`;
      const promptLabel = r.prompt_preview || 'No prompt preview';
      const promptAt = r.started_at || r.captured_at || '';
      const turnLabel = ['Turn', status, compactDateTime(promptAt), label, promptLabel].filter(Boolean).join(' · ');
      return `<tr data-session="${esc(r.session_id)}" data-turn="${esc(r.turn_id)}" data-status="${esc(turnStatusClass(status))}" title="${esc('Status: ' + status)}"><td class="datetime-cell" title="${esc(promptAt)}">${esc(compactDateTime(promptAt))}</td><td class="session-cell session-label-cell" title="${esc(label)}">${sessionLabelMarkup(r)}</td><td class="prompt" title="${esc(r.prompt_preview || '')}"><button type="button" class="row-select-button" aria-pressed="false" aria-label="Select ${esc(turnLabel)}">${turnPromptPreviewMarkup(r)}<span class="row-meta"><span title="${esc(promptAt)}">${esc(compactDateTime(promptAt))}</span><span title="${esc(label)}">${esc(label)}</span><span>${esc(status)}</span>${resolutionMeta}${tokenAvailable ? `<span title="${esc(exactNumber(r.raw))}">${esc(compactNumber(r.raw))} raw</span>` : ''}</span></button></td><td class="num">${creditValue}</td><td class="num">${rawValue}</td></tr>`;
    })
  ));
  document.querySelectorAll('#turn-list [data-turn-sort]').forEach(button => {
    button.addEventListener('click', event => {
      event.preventDefault();
      setTurnSort(button.dataset.turnSort, button);
    });
  });
  document.querySelectorAll('#turn-list tr[data-turn]').forEach(row => {
    row.addEventListener('click', () => selectTurnRow(row));
  });
  document.querySelectorAll('#turn-list tr[data-turn] .row-select-button').forEach(button => {
    button.addEventListener('keydown', event => handleListArrowFocus(event, '#turn-list tr[data-turn] .row-select-button', true));
  });
  const visibleTurn = [...document.querySelectorAll('#turn-list tr[data-turn]')]
    .find(row => state.selected && row.dataset.session === state.selected.session && row.dataset.turn === state.selected.turn);
  const target = visibleTurn || document.querySelector('#turn-list tr[data-turn]');
  const targetKey = target ? `${target.dataset.session}\u0000${target.dataset.turn}` : '';
  if (target && prepared?.error && prepared.key === targetKey) {
    state.selected = null;
    state.detailData = null;
    document.getElementById('detail-status').textContent = 'error';
    setPanelContent('detail', esc(prepared.error.message || prepared.error), 'error');
  } else if (target && prepared && prepared.key === targetKey) {
    selectTurnRow(target, prepared.data);
  } else if (target) {
    selectTurnRow(target);
  } else {
    state.selected = null;
    state.detailData = null;
    document.getElementById('detail-status').textContent = 'none';
    setPanelContent('detail', 'No rows for the current filter.', 'empty');
  }
  focusActiveViewRow();
  renderPager(turns.total || 0, turns.page || 1, turns.per_page || state.turnPageSize);
  refreshScrollFades();
}

function prefetchNextTurnPage(turns) {
  prefetchNextPage(turns, turnsPath, row => turnDetailPath(row.session_id || '', row.turn_id || ''));
}

async function loadTurnPage(page) {
  const requestSeq = state.requestSeq;
  const listSeq = ++state.turnListSeq;
  const path = turnsPath(page);
  if (!peekCachedJSON(path).hit) setPagerBusy('turn-pager', true);
  try {
    const turns = await cachedValue(path);
    const first = targetTurnRow(turns);
    if (first && !peekCachedJSON(turnDetailPath(first.session_id || '', first.turn_id || '')).hit) {
      setPagerBusy('turn-pager', true);
    }
    const prepared = await prepareTurnDetail(turns);
    if (requestSeq !== state.requestSeq || listSeq !== state.turnListSeq) return false;
    renderTurnPage(turns, prepared);
    if (prepared?.error) showQueryError(prepared.error.message || prepared.error);
    prefetchNextTurnPage(turns);
    return true;
  } catch (error) {
    if (requestSeq !== state.requestSeq || listSeq !== state.turnListSeq) return false;
    throw error;
  } finally {
    if (listSeq === state.turnListSeq) setPagerBusy('turn-pager', false);
  }
}

function safeLoadTurnPage(page, onError = null, onCommit = null) {
  clearQueryStatus();
  loadTurnPage(page)
    .then(committed => {
      if (committed && typeof onCommit === 'function') onCommit();
    })
    .catch(err => {
      if (typeof onError === 'function') onError();
      showQueryError(err.message || err);
      refreshScrollFades();
    });
}

async function load() {
  const coldStart = state.requestSeq === 0;
  const seq = ++state.requestSeq;
  const turnListSeq = ++state.turnListSeq;
  state.sessionListSeq += 1;
  state.toolListSeq += 1;
  state.subagentListSeq += 1;
  try {
    if (coldStart) setLoading();
    const tq = turnsParams();
    tq.set('lite', '1');
    const dashboardPath = '/api/dashboard?' + tq;
    const turnCachePath = turnsPath(state.turnPage);
    const dashboard = await getJSON(dashboardPath);
    if (seq !== state.requestSeq) return;
    setAnalyticsCacheGeneration((dashboard.freshness || {}).analytics_db_mtime_unix ?? 'missing');
    const { summary, turns } = dashboard;
    document.getElementById('summary').innerHTML = [
      metric('Analyzed Turns', compactNumber(summary.turns || 0), '', `${exactNumber(summary.turns || 0)} eligible · ${exactNumber(summary.unavailable_turns || 0)} unavailable`, freshnessIndicator(dashboard.freshness)),
      metric('Cost Units', compactNumber(summary.weighted_credits || 0, 'money'), '', exactNumber(summary.weighted_credits || 0, 'money')),
      metric('Total Tokens', compactNumber(summary.total_tokens || 0), '', exactNumber(summary.total_tokens || 0)),
      metric('Cached Input', compactNumber(summary.cached_input_tokens || 0), '', exactNumber(summary.cached_input_tokens || 0)),
      metric('Non-Cached Input', compactNumber(summary.non_cached_input_tokens || 0), '', exactNumber(summary.non_cached_input_tokens || 0)),
      metric('Model Calls', compactNumber(summary.model_calls || 0), '', exactNumber(summary.model_calls || 0)),
      metric('Tool Calls', compactNumber(summary.tool_calls || 0), '', exactNumber(summary.tool_calls || 0)),
    ].join('');
    loadVisibleRollupData(seq);
    if (turnListSeq === state.turnListSeq) {
      primeCachedJSON(turnCachePath, turns);
      const prepared = await prepareTurnDetail(turns);
      if (seq !== state.requestSeq || turnListSeq !== state.turnListSeq) return;
      renderTurnPage(turns, prepared);
      if (prepared?.error) showQueryError(prepared.error.message || prepared.error);
      prefetchNextTurnPage(turns);
    }
    refreshScrollFades();
  } catch (error) {
    if (seq !== state.requestSeq) return;
    throw error;
  }
}

function safeLoad() {
  const coldStart = state.requestSeq === 0;
  clearQueryStatus();
  load().catch(err => {
    if (coldStart) setGlobalError(err.message || err);
    else showQueryError(err.message || err);
    refreshScrollFades();
  });
}

function invalidateAnalyticsQueries() {
  clearAnalyticsQueryCache();
  state.requestSeq += 1;
  state.turnListSeq += 1;
  state.sessionListSeq += 1;
  state.toolListSeq += 1;
  state.subagentListSeq += 1;
  state.detailSeq += 1;
  state.sessionSeq += 1;
  state.toolSeq += 1;
  state.subagentSeq += 1;
  state.modalSeq += 1;
}

function prepareAnalyticsReload() {
  invalidateAnalyticsQueries();
  resetAllPages();
  setLoading();
}

function setAnalyticsUnavailable(message = 'Analysis data is unavailable. Run Analyze to rebuild it.') {
  const unavailable = esc(message);
  ['projects', 'session-detail', 'turn-list', 'tool-output', 'tool-detail', 'subagent-rollups', 'subagent-mix', 'detail'].forEach(id => {
    setPanelContent(id, unavailable, 'error');
  });
  clearListPagers();
  document.getElementById('turn-pager').innerHTML = '';
  document.getElementById('summary').innerHTML = [
    metric('Analyzed Turns', 'Unavailable'),
    metric('Cost Units', 'N/A'),
    metric('Total Tokens', 'N/A'),
    metric('Cached Input', 'N/A'),
    metric('Non-Cached Input', 'N/A'),
    metric('Model Calls', 'N/A'),
    metric('Tool Calls', 'N/A'),
  ].join('');
  refreshScrollFades();
}

function safeLoadWithSessionOptions() {
  safeLoad();
  loadSessionOptions();
}

const toolbarController = createToolbarController({
  saveSettings,
  resetAllPages,
  safeLoad: safeLoadWithSessionOptions,
});
const {
  bindToolbarControls,
  closeToolbarCustomPopover,
  restoreToolbarSettings,
  timeRangeDaysValue,
} = toolbarController;

const sessionPickerController = createSessionPickerController({
  saveSettings,
  resetAllPages,
  safeLoad,
  timeRangeDaysValue,
});
const {
  bindSessionPickerControls,
  closeSessionPicker,
  loadSessionOptions,
  restoreSessionFilter,
  sessionFilterValue,
} = sessionPickerController;

const cleanupController = createCleanupController({
  load,
  loadSessionOptions,
  prepareAnalyticsReload,
  setAnalyticsUnavailable,
});
const {
  clearCleanupStatus,
  closeCleanupConfirmModal,
  closeCleanupDetailModal,
  deleteCleanupFiles,
  loadCleanup,
  resolveCleanupConfirmModal,
  setCleanupRetentionMode,
} = cleanupController;

const analyzeController = createAnalyzeController({
  load,
  loadCleanup,
  loadSessionOptions,
  prepareAnalyticsReload,
  showQueryError,
  setGlobalError,
  refreshScrollFades,
});
const {
  rebuildAndRefresh,
  setAnalyzeButtonState,
} = analyzeController;

const selectedTurnController = createSelectedTurnController({
  params,
  refreshScrollFades,
  setActiveModal,
});
const {
  bindDetailControls,
  bindToolTurnLinks,
  closeTurnModal,
  openTurnModalFromToolLink,
  renderDetailSummary,
  turnPromptPreviewMarkup,
  updateSelectedTurnPromptOverflow,
} = selectedTurnController;

const overviewRenderers = createOverviewRenderers({
  params,
  requestListPage,
  getCachedJSON,
  peekCachedJSON,
  clearQueryStatus,
  showQueryError,
  bindToolTurnLinks,
  listTableSortState,
  setListSort,
});
const {
  clearListPagers,
  renderSessionList,
  renderToolList,
  renderSubagentList,
} = overviewRenderers;

document.getElementById('refresh').addEventListener('click', () => {
  saveSettings();
  resetAllPages();
  invalidateAnalyticsQueries();
  safeLoad();
});
document.getElementById('rebuild').addEventListener('click', () => { saveSettings(); rebuildAndRefresh(); });
document.getElementById('cleanup-refresh').addEventListener('click', () => { loadCleanup(); });
document.getElementById('cleanup-delete').addEventListener('click', () => { deleteCleanupFiles(); });
document.querySelectorAll('[data-cleanup-retention-preset]').forEach(button => {
  button.addEventListener('click', () => {
    setCleanupRetentionMode(button.dataset.cleanupRetentionPreset);
    saveSettings();
    markCleanupPreviewUnavailable('Preview loading');
    loadCleanup({preserveRows: true});
  });
});
document.getElementById('cleanup-retention-date').addEventListener('change', () => {
  setCleanupRetentionMode('custom');
  saveSettings();
  markCleanupPreviewUnavailable('Preview loading');
  loadCleanup({preserveRows: true});
});
bindToolbarControls();
document.getElementById('turn-page-size').addEventListener('change', event => {
  state.turnPageSize = Number(event.target.value || DEFAULT_TURN_PAGE_SIZE);
  saveSettings();
  resetAllPages();
  safeLoad();
});
document.querySelectorAll('[data-theme-mode]').forEach(button => {
  button.addEventListener('click', () => {
    applyThemeModeAndSave(button.dataset.themeMode);
  });
});
bindSessionPickerControls();
document.getElementById('turn-modal-close').addEventListener('click', closeTurnModal);
document.getElementById('turn-modal').addEventListener('click', event => {
  if (event.target.id === 'turn-modal') closeTurnModal();
});
document.getElementById('cleanup-detail-modal-close').addEventListener('click', closeCleanupDetailModal);
document.getElementById('cleanup-detail-modal').addEventListener('click', event => {
  if (event.target.closest('[data-cleanup-modal-delete]')) {
    deleteCleanupFiles();
    return;
  }
  if (event.target.id === 'cleanup-detail-modal') closeCleanupDetailModal();
});
document.getElementById('cleanup-confirm-close')?.addEventListener('click', () => closeCleanupConfirmModal(false));
document.getElementById('cleanup-confirm-cancel').addEventListener('click', () => closeCleanupConfirmModal(false));
document.getElementById('cleanup-confirm-delete').addEventListener('click', () => resolveCleanupConfirmModal(true));
document.getElementById('cleanup-confirm-modal').addEventListener('click', event => {
  if (event.target.id === 'cleanup-confirm-modal') closeCleanupConfirmModal(false);
});
window.addEventListener('keydown', event => {
  if (document.getElementById('cleanup-confirm-modal').classList.contains('open')) {
    if (event.key === 'Escape') {
      closeCleanupConfirmModal(false);
    } else if (event.key === 'Tab') {
      trapModalFocus(event, 'cleanup-confirm-modal');
    }
  } else if (document.getElementById('turn-modal').classList.contains('open')) {
    if (event.key === 'Escape') {
      closeTurnModal();
    } else if (event.key === 'Tab') {
      trapModalFocus(event);
    }
  } else if (document.getElementById('cleanup-detail-modal').classList.contains('open')) {
    if (event.key === 'Escape') {
      closeCleanupDetailModal();
    } else if (event.key === 'Tab') {
      trapModalFocus(event, 'cleanup-detail-modal');
    }
  }
});
document.querySelectorAll('.nav-btn').forEach(button => {
  button.addEventListener('click', () => setView(button.dataset.viewTarget));
});
window.addEventListener('hashchange', () => setView(location.hash.slice(1), false));
window.addEventListener('resize', () => {
  refreshScrollFades();
  updateSelectedTurnPromptOverflow();
  requestAnimationFrame(updateSelectedTurnPromptOverflow);
});
Object.assign(window, { compactNumber, formatBytes, setAnalyzeButtonState });
if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
const restoredView = restoreSettings();
const hashView = location.hash.slice(1);
setView(views.has(hashView) ? hashView : restoredView, false, {focusContent: false});
requestAnimationFrame(() => window.scrollTo(0, 0));
loadSessionOptions().then(() => safeLoad());
}
