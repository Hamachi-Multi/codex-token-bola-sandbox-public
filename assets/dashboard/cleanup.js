import { state } from './core.js';
import { getJSON, isServiceBusyError, postJSON } from './api.js';
import { esc } from './ui.js';
import {
  focusActiveViewRow,
  handleListArrowFocus,
  refreshScrollFades,
  setPageInert,
  setPanelContent,
  trapModalFocus,
} from './dom.js';
import { compactNumber, formatBytes } from './formatters.js';
import { createCleanupProgressController } from './cleanup-progress.js';
import { createCleanupRenderController } from './cleanup-render.js';
import {
  cleanupAllMode,
  cleanupRetentionDate,
  disableCleanupAction,
  emptyCleanupRetention,
  setCleanupActionLoading,
  setCleanupRetentionMode,
  updateCleanupActionState,
} from './cleanup-retention.js';

export { normalizeCleanupRetentionMode } from './cleanup-retention.js';

export function createCleanupController({ load, loadSessionOptions, resetAllPages }) {
let cleanupConfirmResolve = null;
let cleanupStatusClearTimer = null;
let cleanupDetailSeq = 0;

function cleanupRowId(row) {
  return String((row || {}).group_id || '');
}

function setCleanupStatus(message = '', tone = '', autoClearMs = 0) {
  const status = document.getElementById('cleanup-action-status');
  if (!status) return;
  if (cleanupStatusClearTimer) {
    clearTimeout(cleanupStatusClearTimer);
    cleanupStatusClearTimer = null;
  }
  status.textContent = message;
  status.title = message;
  if (tone) {
    status.dataset.tone = tone;
  } else {
    delete status.dataset.tone;
  }
  if (message && autoClearMs > 0) {
    cleanupStatusClearTimer = setTimeout(() => {
      if (status.textContent === message) {
        status.textContent = '';
        status.title = '';
        delete status.dataset.tone;
      }
      cleanupStatusClearTimer = null;
    }, autoClearMs);
  }
}

function clearCleanupStatus() {
  setCleanupStatus('', '');
}

function openCleanupDetailModal(rowEl) {
  const rows = state.cleanupRows || [];
  const selected = rows.find(row => cleanupRowId(row) === state.cleanupSelectedFile) || rows[Number(rowEl?.dataset.cleanupIndex || -1)] || null;
  if (!selected) return;
  state.cleanupSelectedFile = cleanupRowId(selected);
  updateCleanupFileSelection();
  const modal = document.getElementById('cleanup-detail-modal');
  state.cleanupModalTrigger = rowEl || document.activeElement;
  state.cleanupDetailPage = 1;
  state.cleanupDetailRow = null;
  state.cleanupDetailKey = '';
  document.getElementById('cleanup-detail-modal-title').textContent = 'File Detail';
  document.getElementById('cleanup-detail-modal-body').innerHTML = renderCleanupFileDetailLoading(selected);
  modal.classList.add('open');
  modal.setAttribute('aria-hidden', 'false');
  setPageInert(true);
  document.getElementById('cleanup-detail-modal-close').focus();
  refreshScrollFades(modal);
  loadCleanupDetail(selected);
}

function updateOpenCleanupDetailModal() {
  const modal = document.getElementById('cleanup-detail-modal');
  if (!modal.classList.contains('open')) return;
  const rows = state.cleanupRows || [];
  const selected = rows.find(row => cleanupRowId(row) === state.cleanupSelectedFile) || null;
  if (!selected) {
    closeCleanupDetailModal();
    return;
  }
  document.getElementById('cleanup-detail-modal-title').textContent = 'File Detail';
  const key = cleanupDetailKey(selected);
  if (state.cleanupDetailKey === key && state.cleanupDetailRow) {
    document.getElementById('cleanup-detail-modal-body').innerHTML = renderCleanupFileDetail(selected, state.cleanupDetailRow);
    bindCleanupAffectedFilePager();
  } else {
    document.getElementById('cleanup-detail-modal-body').innerHTML = renderCleanupFileDetailLoading(selected);
    loadCleanupDetail(selected);
  }
  refreshScrollFades(modal);
}

function closeCleanupDetailModal() {
  const modal = document.getElementById('cleanup-detail-modal');
  const confirmOpen = document.getElementById('cleanup-confirm-modal')?.classList.contains('open');
  modal.classList.remove('open');
  modal.setAttribute('aria-hidden', 'true');
  if (!confirmOpen) {
    setPageInert(false);
    state.cleanupModalTrigger?.focus?.();
  }
  state.cleanupModalTrigger = null;
  cleanupDetailSeq++;
}

function closeCleanupConfirmModal(result = false) {
  const modal = document.getElementById('cleanup-confirm-modal');
  if (modal.dataset.busy === 'true') return;
  const detailOpen = document.getElementById('cleanup-detail-modal')?.classList.contains('open');
  modal.classList.remove('open');
  modal.setAttribute('aria-hidden', 'true');
  delete modal.dataset.busy;
  setPageInert(Boolean(detailOpen));
  if (detailOpen) {
    const focusTarget = document.querySelector('#cleanup-detail-modal-close');
    focusTarget?.focus?.();
  }
  if (cleanupConfirmResolve) {
    const resolve = cleanupConfirmResolve;
    cleanupConfirmResolve = null;
    resolve(result);
  }
}

function resolveCleanupConfirmModal(result = false) {
  if (!cleanupConfirmResolve) return;
  const resolve = cleanupConfirmResolve;
  cleanupConfirmResolve = null;
  resolve(result);
}

function confirmCleanupAction({title, subtitle, body, confirmLabel}) {
  const modal = document.getElementById('cleanup-confirm-modal');
  document.getElementById('cleanup-confirm-title').textContent = title;
  document.getElementById('cleanup-confirm-subtitle').textContent = subtitle;
  document.getElementById('cleanup-confirm-body').innerHTML = body;
  delete modal.dataset.busy;
  const closeButton = document.getElementById('cleanup-confirm-close');
  const cancelButton = document.getElementById('cleanup-confirm-cancel');
  const deleteButton = document.getElementById('cleanup-confirm-delete');
  if (closeButton) closeButton.disabled = false;
  cancelButton.textContent = 'Cancel';
  cancelButton.disabled = false;
  deleteButton.textContent = confirmLabel;
  deleteButton.hidden = false;
  deleteButton.disabled = false;
  modal.classList.add('open');
  modal.setAttribute('aria-hidden', 'false');
  setPageInert(true);
  cancelButton.focus();
  return new Promise(resolve => {
    cleanupConfirmResolve = resolve;
  });
}

function setCleanupConfirmProgress({message, detail = '', step = 1, total = 3, tone = 'busy', done = false, progressPercent = null}) {
  const modal = document.getElementById('cleanup-confirm-modal');
  modal.dataset.busy = done ? 'false' : 'true';
  const closeButton = document.getElementById('cleanup-confirm-close');
  const cancelButton = document.getElementById('cleanup-confirm-cancel');
  const deleteButton = document.getElementById('cleanup-confirm-delete');
  const normalizedStep = Math.max(1, Math.min(Number(step || 1), Number(total || 1)));
  const normalizedTotal = Math.max(1, Number(total || 1));
  const explicitProgress = progressPercent !== null && progressPercent !== undefined && Number.isFinite(Number(progressPercent));
  const pct = explicitProgress ? Math.max(0, Math.min(100, Math.round(Number(progressPercent)))) : Math.round((normalizedStep / normalizedTotal) * 100);
  document.getElementById('cleanup-confirm-body').innerHTML = `
    <div class="cleanup-confirm-progress" data-tone="${esc(tone)}" style="--cleanup-progress: ${pct}%">
      <div class="cleanup-confirm-progress-head">
        <div class="cleanup-confirm-progress-title">${esc(message)}</div>
        <div class="cleanup-confirm-progress-step">${done ? 'Done' : (explicitProgress ? `${pct}%` : `${normalizedStep}/${normalizedTotal}`)}</div>
      </div>
      <div class="cleanup-confirm-progress-track" aria-hidden="true"><div class="cleanup-confirm-progress-fill"></div></div>
      <p class="cleanup-confirm-progress-detail">${esc(detail)}</p>
    </div>`;
  if (closeButton) closeButton.disabled = !done;
  cancelButton.textContent = done ? 'Close' : 'Working';
  cancelButton.disabled = !done;
  deleteButton.hidden = true;
  if (done) {
    modal.dataset.busy = 'false';
    cancelButton.focus();
  }
}

const cleanupProgressController = createCleanupProgressController({ setCleanupConfirmProgress });
const {
  finishCleanupProgress,
  startCleanupProgress,
} = cleanupProgressController;

let cleanupRenderController = null;
function cleanupRender() {
  if (!cleanupRenderController) {
    cleanupRenderController = createCleanupRenderController({ updateOpenCleanupDetailModal });
  }
  return cleanupRenderController;
}

function bindCleanupAffectedFilePager() {
  cleanupRender().bindCleanupAffectedFilePager();
}

function renderCleanupFileDetail(row, detailRow = row) {
  return cleanupRender().renderCleanupFileDetail(row, detailRow);
}

function renderCleanupFileDetailLoading(row) {
  return cleanupRender().renderCleanupFileDetailLoading(row);
}

function renderCleanupFileDetailError(row, message) {
  return cleanupRender().renderCleanupFileDetailError(row, message);
}

function renderCleanupTable(rows) {
  return cleanupRender().renderCleanupTable(rows);
}

function renderCleanupTableLoading(message) {
  return cleanupRender().renderCleanupTableLoading(message);
}

function updateCleanupFileRows() {
  cleanupRender().updateCleanupFileRows();
}

function retentionDeleteConfirmBody() {
  return cleanupRender().retentionDeleteConfirmBody();
}

function allDataDeleteConfirmBody() {
  return cleanupRender().allDataDeleteConfirmBody();
}

function cleanupRowsSignature(rows) {
  return cleanupRender().cleanupRowsSignature(rows);
}


function bindCleanupFileRows() {
  document.querySelectorAll('#cleanup-files tr[data-cleanup-file]').forEach(row => {
    row.addEventListener('click', () => selectCleanupFileRow(row));
    row.addEventListener('dblclick', event => {
      event.preventDefault();
      selectCleanupFileRow(row);
      openCleanupDetailModal(row);
    });
    row.addEventListener('keydown', event => {
      if (handleListArrowFocus(event, '#cleanup-files tr[data-cleanup-file]', true)) return;
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        selectCleanupFileRow(row);
        openCleanupDetailModal(row);
      }
    });
  });
}

function updateCleanupFileSelection() {
  const rows = state.cleanupRows || [];
  const selected = rows.find(row => cleanupRowId(row) === state.cleanupSelectedFile) || rows[0] || null;
  if (selected) state.cleanupSelectedFile = cleanupRowId(selected);
  document.querySelectorAll('#cleanup-files tr[data-cleanup-file]').forEach(row => {
    const active = row.dataset.cleanupFile === state.cleanupSelectedFile;
    row.classList.toggle('selected', active);
    row.setAttribute('aria-selected', active ? 'true' : 'false');
  });
}

function selectCleanupFileRow(row) {
  const nextFile = row.dataset.cleanupFile || '';
  if (nextFile !== state.cleanupSelectedFile) state.cleanupDetailPage = 1;
  state.cleanupSelectedFile = nextFile;
  updateCleanupFileSelection();
}

function cleanupDetailKey(row) {
  return [state.cleanupRetentionMode || '', cleanupRetentionDate(), cleanupRowId(row)].join(String.fromCharCode(31));
}

function cleanupHasRetentionWork(retention = ((state.cleanupRetention || {}).selected || {})) {
  const selectedRows = Number((retention || {}).deletable_rows || 0);
  const pendingTurnStateFiles = Number((retention || {}).pending_turn_state_deletable_files || 0);
  return selectedRows > 0 || pendingTurnStateFiles > 0;
}

function mergeCleanupDisplay(baseDisplay = {}, detailDisplay = {}) {
  const merged = {...(baseDisplay || {})};
  ['targets', 'targets_truncated', 'items'].forEach(key => {
    if (Object.prototype.hasOwnProperty.call(detailDisplay || {}, key)) {
      merged[key] = detailDisplay[key];
    }
  });
  return merged;
}

function mergeCleanupDetailRow(row, detailRow) {
  const source = detailRow || {};
  return {
    ...(row || {}),
    ...source,
    display: mergeCleanupDisplay((row || {}).display, source.display),
    delete_all_display: mergeCleanupDisplay((row || {}).delete_all_display, source.delete_all_display),
  };
}

async function loadCleanupDetail(row) {
  const modal = document.getElementById('cleanup-detail-modal');
  const key = cleanupDetailKey(row);
  const seq = ++cleanupDetailSeq;
  try {
    const q = new URLSearchParams();
    q.set('group_id', cleanupRowId(row));
    const cutoffDate = cleanupRetentionDate();
    if (cutoffDate) q.set('cutoff_date', cutoffDate);
    const previewSignature = String((((state.cleanupRetention || {}).selected || {}).preview_signature) || '');
    q.set('preview_signature', previewSignature);
    const detail = await getJSON('/api/log-cleanup/detail?' + q);
    if (seq !== cleanupDetailSeq || !modal.classList.contains('open') || key !== cleanupDetailKey(row)) return;
    state.cleanupDetailKey = key;
    state.cleanupDetailRow = mergeCleanupDetailRow(row, detail.row || {});
    document.getElementById('cleanup-detail-modal-body').innerHTML = renderCleanupFileDetail(row, state.cleanupDetailRow || row);
    bindCleanupAffectedFilePager();
    refreshScrollFades(modal);
  } catch (err) {
    if (seq !== cleanupDetailSeq || !modal.classList.contains('open')) return;
    if (String((err || {}).code || '') === 'cleanup_preview_stale') {
      document.getElementById('cleanup-detail-modal-body').innerHTML = renderCleanupFileDetailError(row, 'Cleanup preview changed. Refreshing cleanup data.');
      await loadCleanup({keepStatus: true});
      refreshScrollFades(modal);
      return;
    }
    document.getElementById('cleanup-detail-modal-body').innerHTML = renderCleanupFileDetailError(row, String(err.message || err));
    refreshScrollFades(modal);
  }
}

function renderCleanup(data) {
  updateCleanup(data);
}

function cleanupPreviewKey() {
  return [String(state.cleanupRetentionMode || ''), cleanupRetentionDate()].join(String.fromCharCode(31));
}

function commitCleanupView({summary, rows, previousSignature, nextSignature, tableExists, options}) {
  state.cleanupRetentionAvailable = true;
  updateCleanupActionState(summary);
  if (options.preserveRows && tableExists && previousSignature === nextSignature) {
    updateCleanupFileRows();
    updateCleanupFileSelection();
    updateOpenCleanupDetailModal();
    focusActiveViewRow();
    refreshScrollFades();
    return;
  }
  setPanelContent('cleanup-files', renderCleanupTable(rows));
  bindCleanupFileRows();
  updateCleanupFileSelection();
  updateOpenCleanupDetailModal();
  focusActiveViewRow();
  refreshScrollFades();
}

function updateCleanup(data, options = {}) {
  const summary = data.summary || {};
  const rows = data.rows || [];
  const retention = data.retention || {};
  const previousSignature = cleanupRowsSignature(state.cleanupRows || []);
  const nextSignature = cleanupRowsSignature(rows);
  const tableExists = Boolean(document.querySelector('#cleanup-files tr[data-cleanup-file]'));
  state.cleanupDetailRow = null;
  state.cleanupDetailKey = '';
  state.cleanupSummary = summary;
  state.cleanupRows = rows;
  state.cleanupRetention = retention;
  commitCleanupView({summary, rows, previousSignature, nextSignature, tableExists, options});
}

async function loadCleanup(options = {}) {
  const seq = ++state.cleanupSeq;
  const tableExists = Boolean(document.querySelector('#cleanup-files tr[data-cleanup-file]'));
  const expectedPreviewKey = cleanupPreviewKey();
  if (!options.keepStatus) clearCleanupStatus();
  if (!options.preserveRows || !tableExists) {
    setCleanupActionLoading();
    setPanelContent('cleanup-files', renderCleanupTableLoading(), 'loading');
  }
  try {
    const q = new URLSearchParams();
    const cutoffDate = cleanupRetentionDate();
    if (cutoffDate) q.set('cutoff_date', cutoffDate);
    const data = await getJSON('/api/log-cleanup?' + q);
    if (seq !== state.cleanupSeq) return;
    if (expectedPreviewKey !== cleanupPreviewKey()) return;
    updateCleanup(data, options);
    if (!options.keepStatus) clearCleanupStatus();
  } catch (err) {
    if (seq !== state.cleanupSeq) return;
    if (isServiceBusyError(err)) {
      setCleanupStatus('analysis or cleanup is running · try again when it finishes', 'busy');
      if (!tableExists) {
        setPanelContent('cleanup-files', 'Cleanup is busy. Try again when the current job finishes.', 'loading');
      }
      refreshScrollFades();
      return;
    }
    state.cleanupRetention = emptyCleanupRetention();
    state.cleanupRetentionAvailable = false;
    setPanelContent('cleanup-files', esc(err.message || err), 'error');
    setCleanupStatus('cleanup data failed', 'error');
    document.querySelectorAll('[data-cleanup-modal-delete]').forEach(modalButton => { modalButton.disabled = true; });
    disableCleanupAction('Preview unavailable');
    refreshScrollFades();
  }
}

async function refreshDashboardAfterCleanup(request, detail) {
  finishCleanupProgress(request);
  setCleanupConfirmProgress({
    message: 'Refreshing dashboard data',
    detail,
    step: 4,
    total: 4,
    progressPercent: 96,
  });
  try {
    await loadSessionOptions();
    resetAllPages();
    await load();
    return true;
  } catch (err) {
    setCleanupConfirmProgress({
      message: 'Delete completed, refresh failed',
      detail: String(err.message || err),
      step: 4,
      total: 4,
      tone: 'error',
      done: true,
    });
    updateCleanupActionState(state.cleanupSummary || {});
    refreshScrollFades();
    return false;
  }
}

async function deleteCleanupFiles() {
  if (cleanupAllMode()) {
    await deleteAllLogs();
    return;
  }
  const confirmed = await confirmCleanupAction({
    title: 'Delete Logs',
    subtitle: 'all eligible rows across managed files',
    body: retentionDeleteConfirmBody(),
    confirmLabel: 'Delete Logs',
  });
  if (!confirmed) return;
  const button = document.getElementById('cleanup-delete');
  const modalDeleteButtons = Array.from(document.querySelectorAll('[data-cleanup-modal-delete]'));
  closeCleanupDetailModal();
  if (!cleanupHasRetentionWork()) {
    clearCleanupStatus();
    setCleanupConfirmProgress({
      message: 'No logs to delete',
      detail: 'The current cleanup preview has 0 eligible rows, so no delete or rebuild was started.',
      step: 3,
      total: 3,
      tone: 'success',
      done: true,
    });
    updateCleanupActionState(state.cleanupSummary || {});
    return;
  }
  button.disabled = true;
  modalDeleteButtons.forEach(modalButton => { modalButton.disabled = true; });
  const cutoffDate = cleanupRetentionDate();
  const previewSignature = String(((state.cleanupRetention || {}).selected || {}).preview_signature || '');
  let retentionDeleteSucceeded = false;
  clearCleanupStatus();
  const request = {};
  startCleanupProgress(request);
  setCleanupConfirmProgress({
    message: 'Deleting logs and rebuilding analysis',
    detail: 'The service is applying the selected cutoff and rebuilding derived dashboard data.',
    step: 1,
    total: 4,
  });
  try {
    const result = await postJSON('/api/log-cleanup/retention', {cutoff_date: cutoffDate, preview_signature: previewSignature});
    const deletedRows = Number((result.retention || {}).deleted_rows || 0);
    renderCleanup(result.cleanup || {});
    retentionDeleteSucceeded = true;
    const refreshed = await refreshDashboardAfterCleanup(
      request,
      'Cleanup finished. The dashboard is loading the rebuilt analysis state.',
    );
    if (!refreshed) return;
    setCleanupConfirmProgress({
      message: `Deleted ${compactNumber(deletedRows)} rows`,
      detail: 'Analysis data was rebuilt from the remaining logs.',
      step: 3,
      total: 3,
      tone: 'success',
      done: true,
    });
  } catch (err) {
    if (isServiceBusyError(err)) {
      finishCleanupProgress(request);
      setCleanupConfirmProgress({
        message: 'Delete was not started',
        detail: 'Analysis or another cleanup is already running. Try again when it finishes.',
        step: 1,
        total: 1,
        tone: 'error',
        done: true,
      });
      updateCleanupActionState(state.cleanupSummary || {});
      return;
    }
    if ((err || {}).error === 'cleanup_preview_stale') {
      finishCleanupProgress(request);
      setCleanupConfirmProgress({
        message: 'Cleanup preview changed',
        detail: 'Refresh the cleanup preview and try again.',
        step: 1,
        total: 1,
        tone: 'error',
        done: true,
      });
      await loadCleanup({keepStatus: true});
      updateCleanupActionState(state.cleanupSummary || {});
      return;
    }
    if ((err || {}).partial_mutation) {
      finishCleanupProgress(request);
      const deletedRows = Number((err || {}).deleted_rows || 0);
      setCleanupConfirmProgress({
        message: 'Delete changed data but rebuild failed',
        detail: `${compactNumber(deletedRows)} rows were affected. Refresh after resolving the rebuild error.`,
        step: 1,
        total: 1,
        tone: 'error',
        done: true,
      });
      await loadCleanup({keepStatus: true});
      updateCleanupActionState(state.cleanupSummary || {});
      return;
    }
    finishCleanupProgress(request);
    setCleanupConfirmProgress({
      message: 'Delete failed',
      detail: String(err.message || err),
      step: 1,
      total: 1,
      tone: 'error',
      done: true,
    });
    state.cleanupRetention = emptyCleanupRetention();
    updateCleanupActionState(state.cleanupSummary || {});
    setPanelContent('cleanup-files', esc(err.message || err), 'error');
    refreshScrollFades();
  } finally {
    finishCleanupProgress(request);
    updateCleanupActionState(state.cleanupSummary || {});
    modalDeleteButtons.forEach(modalButton => { modalButton.disabled = !retentionDeleteSucceeded; });
  }
}

async function deleteAllLogs() {
  const button = document.getElementById('cleanup-delete');
  const modalDeleteButtons = Array.from(document.querySelectorAll('[data-cleanup-modal-delete]'));
  const confirmed = await confirmCleanupAction({
    title: 'Delete All Logs',
    subtitle: 'all logs and analysis data',
    body: allDataDeleteConfirmBody(),
    confirmLabel: 'Delete All Logs',
  });
  if (!confirmed) return;
  closeCleanupDetailModal();
  button.disabled = true;
  modalDeleteButtons.forEach(modalButton => { modalButton.disabled = true; });
  clearCleanupStatus();
  const request = {};
  startCleanupProgress(request);
  setCleanupConfirmProgress({
    message: 'Deleting logs',
    detail: 'The service is deleting all managed logs and clearing generated analysis outputs.',
    step: 1,
    total: 4,
  });
  try {
    const result = await postJSON('/api/log-cleanup/all', {confirm_all_logs: true});
    renderCleanup(result.cleanup || {});
    const refreshed = await refreshDashboardAfterCleanup(
      request,
      'Cleanup finished. The dashboard is loading the cleared analysis state.',
    );
    if (!refreshed) return;
    setCleanupConfirmProgress({
      message: `Deleted ${formatBytes(Number(result.deleted_bytes || 0))}`,
      detail: 'Generated analysis data was cleared.',
      step: 3,
      total: 3,
      tone: 'success',
      done: true,
    });
  } catch (err) {
    finishCleanupProgress(request);
    if (isServiceBusyError(err)) {
      setCleanupConfirmProgress({
        message: 'Delete was not started',
        detail: 'Analysis or another cleanup is already running. Try again when it finishes.',
        step: 1,
        total: 1,
        tone: 'error',
        done: true,
      });
    } else if ((err || {}).partial_mutation) {
      const deletedBytes = Number((err || {}).deleted_bytes || 0);
      if ((err || {}).cleanup) renderCleanup((err || {}).cleanup || {});
      setCleanupConfirmProgress({
        message: 'Delete changed data but cleanup failed',
        detail: `${formatBytes(deletedBytes)} were removed before the failure. Refresh cleanup data after resolving the error.`,
        step: 1,
        total: 1,
        tone: 'error',
        done: true,
      });
    } else {
      setCleanupConfirmProgress({
        message: 'Delete failed',
        detail: String(err.message || err),
        step: 1,
        total: 1,
        tone: 'error',
        done: true,
      });
      setPanelContent('cleanup-files', esc(err.message || err), 'error');
      refreshScrollFades();
    }
  } finally {
    finishCleanupProgress(request);
    updateCleanupActionState(state.cleanupSummary || {});
  }
}

return {
  clearCleanupStatus,
  closeCleanupConfirmModal,
  closeCleanupDetailModal,
  deleteCleanupFiles,
  loadCleanup,
  resolveCleanupConfirmModal,
  renderCleanup,
  setCleanupRetentionMode,
};
}
