import { getJSON } from './api.js';

const IDLE_STATUS = Object.freeze({
  running: false,
  operation: null,
  status: 'idle',
  progress_available: false,
  phase: '',
  checkpoint: '',
  overall_progress: null,
  operation_id: null,
});

function normalizedStatus(payload) {
  if (!(payload || {}).running) return IDLE_STATUS;
  const progress = payload.overall_progress === null || payload.overall_progress === undefined
    ? Number.NaN
    : Number(payload.overall_progress);
  return {
    running: true,
    operation: ['analysis', 'cleanup', 'cost_recalculation'].includes(payload.operation) ? payload.operation : 'analysis',
    status: 'running',
    progress_available: Boolean(payload.progress_available) && Number.isFinite(progress),
    phase: String(payload.phase || ''),
    checkpoint: String(payload.checkpoint || ''),
    overall_progress: Number.isFinite(progress) ? Math.max(0, Math.min(100, progress)) : null,
    operation_id: String(payload.operation_id || '') || null,
  };
}

function activityLabel(status) {
  const action = status.operation === 'cleanup'
    ? 'Cleanup'
    : (status.operation === 'cost_recalculation' ? 'Recalculate' : 'Analyze');
  if (!status.progress_available) return action;
  return `${action} · ${Math.round(status.overall_progress)}%`;
}

export function createServiceActivityController() {
  const element = document.getElementById('service-activity');
  const label = element?.querySelector('[data-service-activity-label]');
  const listeners = new Set();
  let current = IDLE_STATUS;
  let requestInFlight = false;
  let lastSignature = '';

  function render(status) {
    if (!element || !label) return;
    element.hidden = !status.running;
    label.textContent = status.running ? activityLabel(status) : '';
    const detail = [status.phase, status.checkpoint].filter(Boolean).join(' · ');
    element.title = detail || label.textContent;
  }

  function publish(next) {
    const signature = JSON.stringify(next);
    if (signature === lastSignature) return;
    lastSignature = signature;
    current = next;
    render(next);
    listeners.forEach(listener => listener(next));
  }

  async function refresh() {
    if (requestInFlight || document.visibilityState === 'hidden') return current;
    requestInFlight = true;
    try {
      publish(normalizedStatus(await getJSON('/api/service-status')));
    } catch {
      // A transient status failure must not hide a previously visible operation.
    } finally {
      requestInFlight = false;
    }
    return current;
  }

  function subscribe(listener) {
    listeners.add(listener);
    listener(current);
    return () => listeners.delete(listener);
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') refresh();
  });
  window.addEventListener('focus', refresh);
  window.setInterval(refresh, 1000);
  refresh();

  return { refresh, subscribe };
}
