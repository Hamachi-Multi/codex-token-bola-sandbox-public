import { getJSON } from './api.js';
import { compactNumber } from './formatters.js';

export function createCleanupProgressController({ setCleanupConfirmProgress }) {
let cleanupRequest = null;
let cleanupProgressTimer = null;
let cleanupProgressPollInFlight = false;
const CLEANUP_STATUS_PHASES = {
  'cleanup-prepare': 'Preparing cleanup',
  'cleanup-delete': 'Deleting logs',
  'cleanup-rebuild': 'Rebuilding analysis',
  'cleanup-refresh': 'Refreshing dashboard data',
};

function cleanupProgressPhaseLabel(progress = {}) {
  const phase = String((progress || {}).phase || '');
  return CLEANUP_STATUS_PHASES[phase] || 'Cleaning up logs';
}

function cleanupProgressDetail(progress = {}) {
  const checkpoint = String((progress || {}).checkpoint || '').replaceAll('-', ' ').replaceAll(':', ' ');
  const processed = Number((progress || {}).processed);
  const total = Number((progress || {}).total);
  const parts = [];
  if (checkpoint) parts.push(checkpoint);
  if (Number.isFinite(processed) && Number.isFinite(total) && total > 0) {
    parts.push(`${compactNumber(processed)} / ${compactNumber(total)}`);
  }
  return parts.join(' · ') || 'Cleanup is running.';
}

function applyCleanupProgress(progress = {}, request = cleanupRequest) {
  const phaseIndex = Math.max(0, Number((progress || {}).phase_index || 0));
  const phaseCount = Math.max(1, Number((progress || {}).phase_count || 4));
  const percent = Number((progress || {}).overall_progress);
  setCleanupConfirmProgress({
    message: cleanupProgressPhaseLabel(progress),
    detail: cleanupProgressDetail(progress),
    step: phaseIndex + 1,
    total: phaseCount,
    progressPercent: Number.isFinite(percent) ? percent : null,
    tone: String((progress || {}).status || '') === 'failed' ? 'error' : 'busy',
    done: false,
  });
}

function clearCleanupProgressTimer() {
  if (cleanupProgressTimer) {
    clearInterval(cleanupProgressTimer);
    cleanupProgressTimer = null;
  }
}

async function pollCleanupProgress(request) {
  if (cleanupProgressPollInFlight || cleanupRequest !== request) return;
  cleanupProgressPollInFlight = true;
  try {
    const progress = await getJSON('/api/log-cleanup/progress');
    const updatedAt = Number((progress || {}).updated_at_unix);
    if (Number.isFinite(updatedAt) && Number.isFinite(request.startedUnix) && updatedAt + 0.1 < request.startedUnix) return;
    if (cleanupRequest === request && (progress || {}).status !== 'idle') {
      applyCleanupProgress(progress, request);
    }
  } catch {
    if (cleanupRequest === request) {
      setCleanupConfirmProgress({
        message: 'Cleaning up logs',
        detail: 'Waiting for cleanup progress.',
        step: 1,
        total: 4,
      });
    }
  } finally {
    cleanupProgressPollInFlight = false;
  }
}

function startCleanupProgress(request) {
  clearCleanupProgressTimer();
  cleanupRequest = request;
  request.startedUnix = Date.now() / 1000;
  const render = () => pollCleanupProgress(request);
  setTimeout(render, 250);
  cleanupProgressTimer = setInterval(render, 1000);
}

function finishCleanupProgress(request) {
  if (cleanupRequest === request) cleanupRequest = null;
  clearCleanupProgressTimer();
}


return {
  finishCleanupProgress,
  startCleanupProgress,
};
}
