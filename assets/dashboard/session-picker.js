import { getJSON } from './api.js';
import { esc } from './ui.js';
import { state } from './core.js';
import {
  compactNumber,
  compactSessionId,
  sessionDetailLabel,
  sessionPathLabel,
} from './formatters.js';

export function createSessionPickerController({ saveSettings, resetAllPages, safeLoad, timeRangeDaysValue }) {
let sessionOptionsTimer = null;
let sessionOptionsSeq = 0;
function sessionFilterValue() {
  return document.getElementById('session').value;
}

function sessionOptionDaysValue() {
  return typeof timeRangeDaysValue === 'function' ? timeRangeDaysValue() : '7';
}

function sessionFilterRows() {
  const current = sessionFilterValue();
  const rows = [{session_id: '', thread_name: 'All Sessions', cwd: '', turns: 0, credits: 0}, ...state.sessionOptions];
  if (current && !state.sessionOptions.some(row => String(row.session_id || '') === current)) {
    rows.splice(1, 0, {session_id: current, thread_name: '', cwd: '', turns: null, credits: 0, out_of_scope: true});
  }
  return rows;
}

function sessionSearchText(row) {
  return [
    row.session_id || '',
    compactSessionId(row.session_id || ''),
    row.thread_name || '',
    row.cwd || '',
    sessionPathLabel(row),
    sessionDetailLabel(row),
  ].join(' ').toLowerCase();
}

function sessionOptionPrimary(row) {
  if (!row || !row.session_id) return 'All Sessions';
  return String(row.thread_name || '').trim() || sessionPathLabel(row) || 'Session';
}

function sessionOptionId(row) {
  if (!row || !row.session_id) return '';
  return compactSessionId(row.session_id || '');
}

function sessionOptionSecondary(row) {
  if (!row || !row.session_id) return 'all recorded sessions';
  if (row.out_of_scope) return 'not in current range';
  const cwd = String(row.cwd || '').replace(/\\/g, '/');
  const path = cwd ? `~/${sessionPathLabel(row)}` : 'path unavailable';
  const turns = `${compactNumber(row.turns || 0)} turns`;
  return `${path} · ${turns}`;
}

function updateSessionPickerLabel() {
  const value = sessionFilterValue();
  const row = state.sessionOptions.find(item => String(item.session_id || '') === value);
  document.getElementById('session-picker-label').textContent = value ? sessionDetailLabel(row || {session_id: value}) : 'All Sessions';
}

function renderSessionPickerOptions() {
  if (state.sessionOptionsError) {
    document.getElementById('session-options').innerHTML = '<div class="session-option-empty">Unable to load sessions</div>';
    document.getElementById('session-options').setAttribute('aria-activedescendant', '');
    return;
  }
  const options = sessionFilterRows();
  const value = sessionFilterValue();
  state.sessionActiveIndex = Math.min(Math.max(0, state.sessionActiveIndex), Math.max(0, options.length - 1));
  const activeId = `session-option-${state.sessionActiveIndex}`;
  document.getElementById('session-options').innerHTML = options.length
    ? options.map((row, index) => {
      const sessionId = String(row.session_id || '');
      const selected = sessionId === value;
      const optionId = sessionOptionId(row);
      return `<button id="session-option-${index}" class="session-option" type="button" role="option" aria-selected="${selected ? 'true' : 'false'}" data-session-id="${esc(sessionId)}" data-active="${index === state.sessionActiveIndex ? 'true' : 'false'}">
        <span class="session-option-main"><span class="session-option-name">${esc(sessionOptionPrimary(row))}</span>${optionId ? `<span class="session-option-id">${esc(optionId)}</span>` : ''}</span>
        <span class="session-option-sub">${esc(sessionOptionSecondary(row))}</span>
      </button>`;
    }).join('')
    : '<div class="session-option-empty">No matching sessions</div>';
  document.getElementById('session-options').setAttribute('aria-activedescendant', options.length ? activeId : '');
}

function setSessionFilterValue(sessionId, { reload = false } = {}) {
  document.getElementById('session').value = String(sessionId || '');
  updateSessionPickerLabel();
  renderSessionPickerOptions();
  if (reload) {
    saveSettings();
    resetAllPages();
    safeLoad();
  }
}

function openSessionPicker() {
  const popover = document.getElementById('session-picker-popover');
  const button = document.getElementById('session-picker-button');
  state.sessionFilterOpen = true;
  popover.hidden = false;
  button.setAttribute('aria-expanded', 'true');
  document.getElementById('session-search').value = '';
  state.sessionActiveIndex = Math.max(0, sessionFilterRows().findIndex(row => String(row.session_id || '') === sessionFilterValue()));
  renderSessionPickerOptions();
  loadSessionOptions();
  requestAnimationFrame(() => document.getElementById('session-search').focus());
}

function closeSessionPicker() {
  state.sessionFilterOpen = false;
  document.getElementById('session-picker-popover').hidden = true;
  document.getElementById('session-picker-button').setAttribute('aria-expanded', 'false');
}

function chooseSessionFilter(sessionId) {
  setSessionFilterValue(sessionId, { reload: true });
  closeSessionPicker();
  document.getElementById('session-picker-button').focus();
}

function moveSessionPickerActive(delta) {
  const rows = sessionFilterRows();
  if (!rows.length) return;
  state.sessionActiveIndex = (state.sessionActiveIndex + delta + rows.length) % rows.length;
  renderSessionPickerOptions();
  document.getElementById(`session-option-${state.sessionActiveIndex}`)?.scrollIntoView({ block: 'nearest' });
}

function chooseActiveSessionFilter() {
  const row = sessionFilterRows()[state.sessionActiveIndex];
  if (row) chooseSessionFilter(row.session_id || '');
}

function handleSessionPickerKeydown(event) {
  if (event.key === 'ArrowDown') {
    event.preventDefault();
    if (!state.sessionFilterOpen) {
      openSessionPicker();
    } else {
      moveSessionPickerActive(1);
    }
  } else if (event.key === 'ArrowUp') {
    event.preventDefault();
    if (state.sessionFilterOpen) moveSessionPickerActive(-1);
  } else if (event.key === 'Enter') {
    if (state.sessionFilterOpen) {
      event.preventDefault();
      chooseActiveSessionFilter();
    }
  } else if (event.key === 'Escape') {
    if (state.sessionFilterOpen) {
      event.preventDefault();
      closeSessionPicker();
      document.getElementById('session-picker-button').focus();
    }
  }
}

async function loadSessionOptions() {
  const search = String(document.getElementById('session-search')?.value || '').trim();
  const current = state.pendingSession || sessionFilterValue();
  const seq = ++sessionOptionsSeq;
  try {
    const q = new URLSearchParams();
    q.set('limit', '50');
    q.set('days', sessionOptionDaysValue());
    if (search) q.set('q', search);
    const data = await getJSON('/api/session-options?' + q);
    const currentSearch = String(document.getElementById('session-search')?.value || '').trim();
    if (seq !== sessionOptionsSeq || search !== currentSearch) return;
    const rows = data.rows || [];
    state.sessionOptions = rows;
    state.sessionOptionsError = false;
    if (current && rows.some(row => String(row.session_id || '') === current)) {
      setSessionFilterValue(current);
      state.pendingSession = '';
    } else if (current) {
      setSessionFilterValue(current);
    } else {
      setSessionFilterValue('');
      state.pendingSession = '';
    }
  } catch {
    if (seq !== sessionOptionsSeq) return;
    state.sessionOptions = [];
    state.sessionOptionsError = true;
    renderSessionPickerOptions();
  }
}

function scheduleSessionOptionsLoad() {
  clearTimeout(sessionOptionsTimer);
  sessionOptionsTimer = setTimeout(() => {
    sessionOptionsTimer = null;
    loadSessionOptions();
  }, 180);
}

function restoreSessionFilter(settings) {
  state.pendingSession = typeof settings.session_id === 'string' ? settings.session_id : '';
}

function bindSessionPickerControls() {
  document.getElementById('session-picker-button').addEventListener('click', () => {
    state.sessionFilterOpen ? closeSessionPicker() : openSessionPicker();
  });
  document.getElementById('session-picker-button').addEventListener('keydown', handleSessionPickerKeydown);
  document.getElementById('session-search').addEventListener('input', () => {
    state.sessionActiveIndex = 0;
    scheduleSessionOptionsLoad();
  });
  document.getElementById('session-search').addEventListener('keydown', handleSessionPickerKeydown);
  document.getElementById('session-options').addEventListener('click', event => {
    const option = event.target.closest('[data-session-id]');
    if (option) chooseSessionFilter(option.dataset.sessionId || '');
  });
  document.addEventListener('pointerdown', event => {
    if (!event.target.closest('#session-picker')) closeSessionPicker();
  });
}

return {
  bindSessionPickerControls,
  closeSessionPicker,
  loadSessionOptions,
  restoreSessionFilter,
  scheduleSessionOptionsLoad,
  sessionFilterValue,
};
}
