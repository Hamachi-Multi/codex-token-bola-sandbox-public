import {
  DEFAULT_TURN_PAGE_SIZE,
  ROLLUP_SORT_DEFAULTS,
  ROLLUP_SORT_KEYS,
  SETTINGS_KEY,
  TURN_SORT_KEYS,
  TURN_SORT_LABELS,
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
import {
  focusActiveViewRow,
  detailGridLoadingPanel,
  metric,
  refreshScrollFades,
  restoreReplacedControlFocus,
  sessionDetailLoadingPanel,
  clearQueryStatus,
  setGlobalError,
  setPanelContent,
  showQueryError,
  table,
  tableLoadingPanel,
} from './dom.js';
import {
  compactDateTime,
  compactNumber,
  compactNumberSpan,
  exactNumber,
  formatBytes,
  normalizeSessionLabelMode,
  sessionLabel,
  sessionLabelMarkup,
  turnStatusClass,
} from './formatters.js';
import { createSelectedTurnController } from './selected-turn.js';
import { createAnalyzeController } from './analyze.js';
import { createCostRatesController } from './cost-rates.js';
import { createSettingsView } from './settings-view.js';
import { createSessionPickerController } from './session-picker.js';
import { createServiceActivityController } from './service-activity.js';
import { createToolbarController, selectHasValue } from './toolbar.js';
import { createOverviewRenderers } from './overview-render.js';
import { createPager } from './components/pager.js';
import { createDialogManager } from './components/dialog.js';
import { createCleanupSummary } from './components/cleanup-summary.js';
import { createListDetailView } from './components/list-detail-view.js';

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
let initialDataLoadStarted = false;
let settingsAnalyticsRefreshPending = false;
const pageNav = document.querySelector('.page-nav');
const pageNavFrame = document.querySelector('.page-nav-frame');

function updatePageNavOverflow() {
  if (!pageNav || !pageNavFrame) return;
  const maxScrollLeft = Math.max(0, pageNav.scrollWidth - pageNav.clientWidth);
  pageNavFrame.dataset.canScrollLeft = String(pageNav.scrollLeft > 1);
  pageNavFrame.dataset.canScrollRight = String(pageNav.scrollLeft < maxScrollLeft - 1);
}

function revealActivePageNav() {
  const activeButton = pageNav?.querySelector('.nav-btn.active');
  if (!activeButton) return;
  activeButton.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  requestAnimationFrame(updatePageNavOverflow);
}

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
  return value === 'dark' || value === 'light' ? value : 'system';
}

function storedThemeMode(settings = {}) {
  return normalizeThemeMode(settings.themeMode);
}

function systemThemeMode() {
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function resolveInitialThemeMode(settings = {}) {
  return storedThemeMode(settings);
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

function bindSystemThemePreference() {
  unbindSystemThemePreference();
  if (state.themeMode !== 'system' || typeof window.matchMedia !== 'function') return;
  systemThemeMedia = window.matchMedia('(prefers-color-scheme: dark)');
  const sync = () => {
    if (state.themeMode !== 'system') {
      unbindSystemThemePreference();
      return;
    }
    applyThemeMode('system', {suppressTransitions: true});
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
  root.dataset.theme = normalized === 'system' ? systemThemeMode() : normalized;
  if (suppressTransitions) {
    themeCommitTimer = window.setTimeout(releaseThemeCommit, THEME_TRANSITION_MS);
  } else {
    releaseThemeCommit();
  }
}

function applyThemeMode(mode, {transition = false, suppressTransitions = false} = {}) {
  const normalized = normalizeThemeMode(mode);
  const resolved = normalized === 'system' ? systemThemeMode() : normalized;
  const canViewTransition = transition
    && !suppressTransitions
    && document.documentElement.dataset.theme !== resolved
    && typeof document.startViewTransition === 'function'
    && !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  document.documentElement.style.setProperty('--theme-transition-duration', `${THEME_TRANSITION_MS}ms`);
  if (canViewTransition) {
    const viewTransition = document.startViewTransition(() => commitThemeMode(normalized, {suppressTransitions: true}));
    return viewTransition.updateCallbackDone.catch(() => {});
  }
  commitThemeMode(normalized, {suppressTransitions: suppressTransitions || (transition && document.documentElement.dataset.theme !== resolved)});
  return Promise.resolve();
}

function applyThemeModeAndSave(mode) {
  const normalized = normalizeThemeMode(mode);
  unbindSystemThemePreference();
  applyThemeMode(normalized, {transition: true}).then(() => {
    bindSystemThemePreference();
    saveSettings();
  });
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
    sessionLabelMode: state.sessionLabelMode,
  };
  payload.themeMode = state.themeMode;
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(payload));
}

function restoreSettings() {
  const settings = readSettings();
  state.sessionLabelMode = normalizeSessionLabelMode(settings.sessionLabelMode);
  document.getElementById('session-label-mode').value = state.sessionLabelMode;
  applyThemeMode(resolveInitialThemeMode(settings), {suppressTransitions: true});
  bindSystemThemePreference();
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
  document.body.dataset.activeView = view;
  document.querySelectorAll('.view').forEach(section => {
    section.classList.toggle('active', section.dataset.view === view);
  });
  document.querySelectorAll('.nav-btn').forEach(button => {
    button.classList.toggle('active', button.dataset.viewTarget === view);
    button.setAttribute('aria-current', button.dataset.viewTarget === view ? 'page' : 'false');
  });
  requestAnimationFrame(revealActivePageNav);
  if (updateHash && location.hash.slice(1) !== view) {
    history.replaceState(null, '', '#' + view);
  }
  saveSettings();
  state.pendingViewFocus = Boolean(focusContent);
  state.pendingViewFocusOwner = focusContent && document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null;
  if (view === 'cleanup') loadCleanup();
  if (view !== 'cleanup' && view !== 'settings' && settingsAnalyticsRefreshPending) {
    settingsAnalyticsRefreshPending = false;
    initialDataLoadStarted = true;
    safeLoadWithSessionOptions();
  } else if (view !== 'cleanup' && view !== 'settings' && state.requestSeq > 0) {
    loadVisibleRollupData(state.requestSeq);
  } else if (view !== 'cleanup' && view !== 'settings' && state.requestSeq === 0) {
    ensureInitialDataLoad();
  }
  if (view !== 'cleanup' && focusContent) focusActiveViewRow();
  refreshScrollFades();
  requestAnimationFrame(updateSelectedTurnPromptOverflow);
}

function params() {
  const sessionId = sessionFilterValue();
  const q = new URLSearchParams({ days: timeRangeDaysValue() });
  q.set('session_label_mode', state.sessionLabelMode);
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
    title = `${compactNumber(pendingAnalysisRows)} rows pending`;
  } else if (status === 'needs_analyze' && pendingRecoveryFiles > 0) {
    title = `${compactNumber(pendingRecoveryFiles)} files pending recovery`;
  } else if (status === 'degraded' || data.data_health === 'degraded') {
    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
    const firstWarning = warnings.length ? String(warnings[0].code || '') : '';
    title = firstWarning || 'Data warning';
  } else {
    if (status !== 'current') return '';
    title = 'global current';
  }
  return `<span class="metric-freshness-dot" data-freshness-state="${esc(status)}" data-tooltip="${esc(title)}" aria-label="${esc(title)}" tabindex="0"></span>`;
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
  if (busy && !peekCachedJSON(path).hit) setListPagerBusy('projects', true);
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
    if (listSeq === state.sessionListSeq) setListPagerBusy('projects', false);
  }
}

async function loadToolsData(seq = state.requestSeq, page = state.listPages.tools || 1, busy = false) {
  const listSeq = ++state.toolListSeq;
  const path = toolsPath(page);
  if (busy && !peekCachedJSON(path).hit) setListPagerBusy('tools', true);
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
    if (listSeq === state.toolListSeq) setListPagerBusy('tools', false);
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
  state.selected = { session: row.dataset.session, turn: row.dataset.turn };
  state.promptExpanded = false;
  state.toolSummaryExpanded = false;
  state.detailData = detail;
  setPanelContent('detail', renderDetailSummary(detail));
  bindDetailControls(() => selectTurnRow(row));
  refreshScrollFades();
}

const turnDetailView = createListDetailView({
  rowSelector: '#turn-list tr[data-turn]',
  buttonSelector: '#turn-list tr[data-turn] .row-select-button',
  detailId: 'detail',
  statusId: 'detail-status',
  keyForRow: row => `${row.dataset.session || ''}\u0000${row.dataset.turn || ''}`,
  pathForRow: row => turnDetailPath(row.dataset.session || '', row.dataset.turn || ''),
  nextRequestSequence: () => ++state.detailSeq,
  isCurrentRequest: sequence => sequence === state.detailSeq,
  commit: commitTurnRow,
  reset: () => {
    state.selected = null;
    state.detailData = null;
  },
  getCachedJSON,
  peekCachedJSON,
  clearQueryStatus,
  showQueryError,
});

function selectTurnRow(row, preparedDetail) {
  return turnDetailView.select(row, preparedDetail);
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
      const costAvailable = tokenAvailable && r.credits !== null && r.credits !== undefined;
      const costReason = costAvailable ? '' : (tokenAvailable ? 'Cost rate is not configured for this model and date' : resolutionReason);
      const creditValue = costAvailable ? compactNumberSpan(r.credits, 'money') : `<span class="token-unavailable-value" title="${esc(costReason)}">—</span>`;
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
  turnDetailView.bindRows();
  turnDetailView.activateRendered({
    isSelected: row => state.selected
      && row.dataset.session === state.selected.session
      && row.dataset.turn === state.selected.turn,
    prepared,
  });
  focusActiveViewRow();
  turnPager.render({
    total: turns.total || 0,
    page: turns.page || 1,
    perPage: turns.per_page || state.turnPageSize,
  });
  refreshScrollFades();
}

function prefetchNextTurnPage(turns) {
  prefetchNextPage(turns, turnsPath, row => turnDetailPath(row.session_id || '', row.turn_id || ''));
}

async function loadTurnPage(page) {
  const requestSeq = state.requestSeq;
  const listSeq = ++state.turnListSeq;
  const path = turnsPath(page);
  if (!peekCachedJSON(path).hit) turnPager.setBusy(true);
  try {
    const turns = await cachedValue(path);
    const first = targetTurnRow(turns);
    if (first && !peekCachedJSON(turnDetailPath(first.session_id || '', first.turn_id || '')).hit) {
      turnPager.setBusy(true);
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
    if (listSeq === state.turnListSeq) turnPager.setBusy(false);
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
      metric('Cost Units', summary.cost_complete === false ? '—' : compactNumber(summary.weighted_credits || 0, 'money'), '', summary.cost_complete === false ? `${exactNumber(summary.unpriced_turns || 0)} turns need a cost rate` : exactNumber(summary.weighted_credits || 0, 'money')),
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

function ensureInitialDataLoad() {
  if (initialDataLoadStarted || state.requestSeq > 0) return;
  initialDataLoadStarted = true;
  loadSessionOptions().then(() => safeLoad());
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

const dialogManager = createDialogManager();
const cleanupSummary = createCleanupSummary();
let costRatesController = null;
const settingsViewController = createSettingsView({
  onSelectionChange: key => costRatesController?.setActive(key === 'cost-rates'),
});

const cleanupController = createCleanupController({
  load,
  loadSessionOptions,
  prepareAnalyticsReload,
  setAnalyticsUnavailable,
  dialogManager,
  cleanupSummary,
});
const {
  deleteCleanupFiles,
  invalidateCleanupPreview,
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
  applyServiceActivity,
  rebuildAndRefresh,
  setAnalyzeButtonState,
} = analyzeController;

async function refreshAnalyticsAfterCostRecalculation() {
  invalidateAnalyticsQueries();
  resetAllPages();
  settingsAnalyticsRefreshPending = true;
  await loadSessionOptions();
}

costRatesController = createCostRatesController({
  refreshAnalytics: refreshAnalyticsAfterCostRecalculation,
  dialogManager,
  onModelSelected: () => settingsViewController.select('cost-rates'),
});
const serviceActivityController = createServiceActivityController();
serviceActivityController.subscribe(applyServiceActivity);
serviceActivityController.subscribe(costRatesController.applyServiceActivity);
costRatesController.setServiceActivityRefresh(serviceActivityController.refresh);

const selectedTurnController = createSelectedTurnController({
  params,
  refreshScrollFades,
  dialogManager,
});
const {
  bindDetailControls,
  bindToolTurnLinks,
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
  setListPagerBusy,
} = overviewRenderers;

const turnPager = createPager({
  rootId: 'turn-pager',
  previousButtonId: 'prev-page',
  nextButtonId: 'next-page',
  onPageChange: page => safeLoadTurnPage(page),
});

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
    invalidateCleanupPreview('Preview loading');
    loadCleanup({preserveRows: true});
  });
});
document.getElementById('cleanup-retention-date').addEventListener('change', () => {
  setCleanupRetentionMode('custom');
  saveSettings();
  invalidateCleanupPreview('Preview loading');
  loadCleanup({preserveRows: true});
});
bindToolbarControls();
document.getElementById('session-label-mode').addEventListener('change', event => {
  state.sessionLabelMode = normalizeSessionLabelMode(event.target.value);
  event.target.value = state.sessionLabelMode;
  saveSettings();
  resetAllPages();
  if (state.view === 'settings') settingsAnalyticsRefreshPending = true;
  else safeLoadWithSessionOptions();
});
document.getElementById('turn-page-size').addEventListener('change', event => {
  state.turnPageSize = Number(event.target.value || DEFAULT_TURN_PAGE_SIZE);
  saveSettings();
  resetAllPages();
  if (state.view === 'settings') settingsAnalyticsRefreshPending = true;
  else safeLoad();
});
document.querySelectorAll('[data-theme-mode]').forEach(button => {
  button.addEventListener('click', () => {
    applyThemeModeAndSave(button.dataset.themeMode);
  });
});
bindSessionPickerControls();
document.getElementById('cleanup-detail-modal').addEventListener('click', event => {
  if (event.target.closest('[data-cleanup-modal-delete]')) {
    deleteCleanupFiles();
    return;
  }
});
document.getElementById('cleanup-confirm-delete').addEventListener('click', () => resolveCleanupConfirmModal(true));
document.querySelectorAll('.nav-btn').forEach(button => {
  button.addEventListener('click', () => setView(button.dataset.viewTarget));
});
pageNav?.addEventListener('scroll', updatePageNavOverflow, { passive: true });
window.addEventListener('hashchange', () => setView(location.hash.slice(1), false));
window.addEventListener('resize', () => {
  revealActivePageNav();
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
}
