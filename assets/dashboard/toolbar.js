import { DEFAULT_TURN_PAGE_SIZE, state } from './core.js';

export function selectHasValue(id, value) {
  return [...document.getElementById(id).options].some(option => option.value === String(value));
}

export function boundedNumber(value, fallback, min, max) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(min, Math.min(max, Math.round(parsed)));
}

export function createToolbarController({ saveSettings, resetAllPages, safeLoad }) {
function restoreNumericInput(id, value, fallback, min, max) {
  document.getElementById(id).value = String(boundedNumber(value, fallback, min, max));
}

function updateToolbarCustomOptionLabels() {
  const days = boundedNumber(document.getElementById('custom-days').value, 7, 1, 3650);
  const daysOption = document.getElementById('custom-days-current');
  const daysReselectOption = document.getElementById('custom-days-reselect');
  const daysCustom = state.appliedDaysMode === 'custom';
  const daysLabel = daysCustom ? `~ ${days} ${days === 1 ? 'Day' : 'Days'}` : 'Custom';
  daysOption.textContent = daysLabel;
  daysReselectOption.textContent = daysLabel;
}

function armToolbarCustomReselect(selectId, appliedValue, reselectValue) {
  const select = document.getElementById(selectId);
  if (select.value !== appliedValue) return;
  select.value = reselectValue;
  select.dataset.customReselectArmed = 'true';
}

function restoreToolbarCustomReselect(selectId, appliedValue, reselectValue) {
  const select = document.getElementById(selectId);
  if (select.value === reselectValue) {
    select.value = appliedValue;
  }
  delete select.dataset.customReselectArmed;
}

function handleToolbarCustomReselectKeydown(event, selectId, kind, appliedValue) {
  const select = document.getElementById(selectId);
  if (select.value !== appliedValue) return;
  if (!['Enter', ' ', 'F2'].includes(event.key)) return;
  event.preventDefault();
  openToolbarCustomPopover(kind);
}

function focusToolbarCustomField(inputId) {
  requestAnimationFrame(() => {
    const input = document.getElementById(inputId);
    input.focus();
    input.select();
  });
}

function closeToolbarCustomPopover(kind, restoreSelect = true) {
  document.getElementById('custom-days-popover').hidden = true;
  if (restoreSelect && document.getElementById('days').value === 'custom') {
    document.getElementById('days').value = state.appliedDaysMode || '7';
  }
  updateToolbarCustomOptionLabels();
}

function openToolbarCustomPopover(kind) {
  closeToolbarCustomPopover(kind, true);
  if (kind === 'days') {
    document.getElementById('days').value = 'custom';
    restoreNumericInput('custom-days-input', document.getElementById('custom-days').value, 7, 1, 3650);
    document.getElementById('custom-days-popover').hidden = false;
    focusToolbarCustomField('custom-days-input');
  }
  updateToolbarCustomOptionLabels();
}

function clearToolbarCustomDays() {
  document.getElementById('custom-days').value = '7';
  state.appliedDaysMode = '7';
  document.getElementById('days').value = '7';
  document.getElementById('custom-days-popover').hidden = true;
  updateToolbarCustomOptionLabels();
  saveSettings();
  resetAllPages();
  safeLoad();
}

function commitToolbarCustomDays() {
  if (document.getElementById('custom-days-input').value.trim() === '') {
    clearToolbarCustomDays();
    return;
  }
  const nextDays = boundedNumber(document.getElementById('custom-days-input').value, 7, 1, 3650);
  document.getElementById('custom-days').value = String(nextDays);
  state.appliedDaysMode = 'custom';
  document.getElementById('days').value = 'custom';
  document.getElementById('custom-days-popover').hidden = true;
  updateToolbarCustomOptionLabels();
  saveSettings();
  resetAllPages();
  safeLoad();
}

function handleToolbarCustomInputKeydown(event, commit, kind) {
  if (event.key === 'Enter') {
    event.preventDefault();
    commit();
  } else if (event.key === 'Escape') {
    event.preventDefault();
    closeToolbarCustomPopover(kind, true);
  }
}

function applyToolbarDaysChange() {
  const value = document.getElementById('days').value;
  if (value === 'custom-reselect') return;
  if (value === 'custom') {
    openToolbarCustomPopover('days');
    return;
  }
  closeToolbarCustomPopover('', true);
  state.appliedDaysMode = value;
  updateToolbarCustomOptionLabels();
  saveSettings();
  resetAllPages();
  safeLoad();
}

function timeRangeDaysValue() {
  const mode = document.getElementById('days').value;
  if (mode === 'custom') return String(boundedNumber(document.getElementById('custom-days').value, 7, 1, 3650));
  return mode;
}

function restoreToolbarSettings(settings) {
  if (selectHasValue('days', settings.days)) {
    document.getElementById('days').value = String(settings.days);
  }
  restoreNumericInput('custom-days', settings.customDays, 7, 1, 3650);
  state.appliedDaysMode = document.getElementById('days').value;
  updateToolbarCustomOptionLabels();
}

function bindToolbarControls() {
  document.getElementById('days').addEventListener('change', applyToolbarDaysChange);
  document.getElementById('days').addEventListener('pointerdown', () => armToolbarCustomReselect('days', 'custom', 'custom-reselect'));
  document.getElementById('days').addEventListener('keydown', event => handleToolbarCustomReselectKeydown(event, 'days', 'days', 'custom'));
  document.getElementById('days').addEventListener('blur', () => restoreToolbarCustomReselect('days', 'custom', 'custom-reselect'));
  document.getElementById('custom-days-apply').addEventListener('click', commitToolbarCustomDays);
  document.getElementById('custom-days-input').addEventListener('keydown', event => handleToolbarCustomInputKeydown(event, commitToolbarCustomDays, 'days'));
  document.addEventListener('pointerdown', event => {
    if (event.target.closest('.custom-filter-control')) return;
    closeToolbarCustomPopover('days', true);
  });
}

return {
  bindToolbarControls,
  closeToolbarCustomPopover,
  restoreToolbarSettings,
  timeRangeDaysValue,
};
}
