import { getJSON, postJSON } from './api.js';
import { money, state } from './core.js';
import { compactNumber, formatBytes } from './formatters.js';

export function createAnalyzeController({ load, loadCleanup, loadSessionOptions, resetAllPages, setGlobalError, refreshScrollFades }) {
let analyzeProgressTimer = null;
let analyzeStatusClearTimer = null;
let analyzeRequest = null;
let analyzeProgressPollInFlight = false;
function clearAnalyzeTimers() {
  if (analyzeProgressTimer) {
    clearInterval(analyzeProgressTimer);
    analyzeProgressTimer = null;
  }
  if (analyzeStatusClearTimer) {
    clearTimeout(analyzeStatusClearTimer);
    analyzeStatusClearTimer = null;
  }
}

const ANALYZE_STATUS_PHASES = ['normalizing raw logs', 'building analytics database', 'refreshing dashboard data'];

function setAnalyzeButtonState(stateName, detail = '', phaseIndex = -1, progressPercent = null) {
  const button = document.getElementById('rebuild');
  if (!button) return;
  const label = button.querySelector('.analyze-button-label');
  const stage = button.querySelector('.analyze-button-stage');
  const labelText = detail || 'Analyze';
  const phaseCount = ANALYZE_STATUS_PHASES.length;
  const fallbackProgress = ['running', 'cancelling'].includes(stateName) && phaseIndex >= 0
    ? Math.round(((phaseIndex + 1) / phaseCount) * 100)
    : (stateName === 'success' ? 100 : 0);
  const hasExplicitProgress = progressPercent !== null && progressPercent !== undefined && Number.isFinite(Number(progressPercent));
  const progress = hasExplicitProgress
    ? Math.max(0, Math.min(100, Number(progressPercent)))
    : fallbackProgress;
  button.dataset.analyzeState = stateName;
  button.dataset.analyzePhase = phaseIndex >= 0 ? String(phaseIndex + 1) : '';
  button.style.setProperty('--analyze-progress', `${progress}%`);
  const visibleLabel = ['running', 'cancelling'].includes(stateName) ? 'Cancel' : 'Analyze';
  if (label) label.textContent = visibleLabel;
  if (stage) stage.textContent = phaseIndex >= 0 ? `${Math.round(progress)}%` : '';
  const actionLabel = ['running', 'cancelling'].includes(stateName)
    ? `Cancel analysis, ${Math.round(progress)}%, ${labelText}`
    : labelText;
  button.setAttribute('aria-label', actionLabel);
  button.title = actionLabel;
}

function progressPhaseIndex(progress) {
  const value = Number((progress || {}).phase_index);
  if (Number.isFinite(value)) return Math.max(0, Math.min(ANALYZE_STATUS_PHASES.length - 1, value));
  const phase = String((progress || {}).phase || '');
  if (phase === 'build') return 1;
  if (phase === 'refresh') return 2;
  return 0;
}

function currentAnalyzePhaseLabel() {
  const button = document.getElementById('rebuild');
  const phaseIndex = Math.max(0, Number((button || {}).dataset?.analyzePhase || 1) - 1);
  return ANALYZE_STATUS_PHASES[phaseIndex] || ANALYZE_STATUS_PHASES[0];
}

function applyAnalyzeProgress(progress, started, request = analyzeRequest) {
  const phaseIndex = progressPhaseIndex(progress);
  const elapsed = Math.max(0, (performance.now() - started) / 1000);
  const phase = ANALYZE_STATUS_PHASES[phaseIndex] || ANALYZE_STATUS_PHASES[0];
  const checkpoint = String((progress || {}).checkpoint || '').replaceAll('-', ' ');
  const percent = Number((progress || {}).overall_progress);
  const detail = `${phase}${checkpoint ? ` · ${checkpoint}` : ''} · ${money.format(elapsed)}s`;
  const stateName = request && request.cancelRequested ? 'cancelling' : 'running';
  setAnalyzeButtonState(stateName, detail, phaseIndex, Number.isFinite(percent) ? percent : null);
}

async function pollAnalyzeProgress(request, started) {
  if (analyzeProgressPollInFlight || analyzeRequest !== request) return;
  analyzeProgressPollInFlight = true;
  try {
    const progress = await getJSON('/api/rebuild/progress');
    const updatedAt = Number((progress || {}).updated_at_unix);
    if (Number.isFinite(updatedAt) && Number.isFinite(request.startedUnix) && updatedAt + 0.1 < request.startedUnix) return;
    if (analyzeRequest === request && (progress || {}).status !== 'idle') {
      applyAnalyzeProgress(progress, started, request);
    }
  } catch {
    const elapsed = Math.max(0, (performance.now() - started) / 1000);
    const phaseIndex = Math.min(ANALYZE_STATUS_PHASES.length - 1, Math.floor(elapsed / 1.8));
    const phase = ANALYZE_STATUS_PHASES[phaseIndex];
    setAnalyzeButtonState('running', `${phase} · ${money.format(elapsed)}s`, phaseIndex);
  } finally {
    analyzeProgressPollInFlight = false;
  }
}

function startAnalyzeProgress(request) {
  clearAnalyzeTimers();
  const started = performance.now();
  request.startedUnix = Date.now() / 1000;
  const render = () => {
    pollAnalyzeProgress(request, started);
  };
  setAnalyzeButtonState('running', `${ANALYZE_STATUS_PHASES[0]} · 0s`, 0, 0);
  setTimeout(render, 250);
  analyzeProgressTimer = setInterval(render, 1000);
  return started;
}

function finishAnalyzeStatus(message, clearAfterMs = 2000) {
  clearAnalyzeTimers();
  if (String(message || '').includes('cancelled')) {
    setAnalyzeButtonState('cancelled', message);
  } else if (String(message || '').includes('failed')) {
    setAnalyzeButtonState('error', message);
  } else {
    setAnalyzeButtonState('success', message);
  }
  if (clearAfterMs > 0) {
    analyzeStatusClearTimer = setTimeout(() => {
      setAnalyzeButtonState('idle', 'Analyze');
      analyzeStatusClearTimer = null;
    }, clearAfterMs);
  }
}

function isAnalyzeRunning() {
  return analyzeRequest !== null;
}

async function cancelAnalyze() {
  const request = analyzeRequest;
  if (!request) return;
  const button = document.getElementById('rebuild');
  const phaseIndex = Math.max(0, Number((button || {}).dataset?.analyzePhase || 1) - 1);
  request.cancelRequested = true;
  setAnalyzeButtonState('cancelling', 'cancel requested', phaseIndex);
  try {
    await postJSON('/api/rebuild/cancel', {reason: 'user'});
  } catch (err) {
    setGlobalError(err.message || err);
  }
}

async function rebuildAndRefresh() {
  if (isAnalyzeRunning()) {
    cancelAnalyze();
    return;
  }
  const button = document.getElementById('rebuild');
  const request = { cancelRequested: false };
  analyzeRequest = request;
  const started = startAnalyzeProgress(request);
  try {
    const result = await postJSON('/api/rebuild');
    if ((result || {}).cancelled) {
      finishAnalyzeStatus('analysis cancelled', 2500);
      return;
    }
    const elapsed = Math.max(0, (performance.now() - started) / 1000);
    setAnalyzeButtonState('running', `refreshing dashboard data · ${money.format(elapsed)}s`, ANALYZE_STATUS_PHASES.length - 1, 92);
    await loadSessionOptions();
    resetAllPages();
    await load();
    if (state.view === 'cleanup') await loadCleanup({keepStatus: true});
    const compact = result.post_analysis_compact || {};
    const promptBytes = Number(((compact.prompt_usage || {}).archived_bytes) || 0);
    const archivedBytes = promptBytes;
    const compactNote = compact.error ? ' · archive failed' : (archivedBytes > 0 ? ` · archived ${formatBytes(archivedBytes)}` : '');
    finishAnalyzeStatus(`analyzed ${compactNumber(result.new_turn_rows ?? result.turn_rows ?? 0)} new turns${compactNote}`);
  } catch (err) {
    const failedPhase = currentAnalyzePhaseLabel();
    finishAnalyzeStatus(`analysis failed · ${failedPhase}`, 9000);
    setGlobalError(err.message || err);
    refreshScrollFades();
  } finally {
    if (analyzeRequest === request) analyzeRequest = null;
    button.disabled = false;
  }
}


return {
  rebuildAndRefresh,
  setAnalyzeButtonState,
};
}
