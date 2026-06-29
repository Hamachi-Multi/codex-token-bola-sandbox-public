import { fmt, money } from './core.js';
import { esc } from './ui.js';

export function statusLabel(status) {
  const raw = String(status || 'unknown');
  const cls = ['completed', 'aborted', 'incomplete'].includes(raw) ? raw : 'unknown';
  return `<span class="status ${cls}">${esc(raw)}</span>`;
}

export function turnStatusClass(status) {
  const raw = String(status || 'unknown');
  return ['completed', 'aborted', 'incomplete'].includes(raw) ? raw : 'unknown';
}

export function shortSession(value) {
  const text = String(value || '');
  if (text.length <= 18) return text;
  return `${text.slice(0, 8)}...${text.slice(-6)}`;
}

export function compactSessionId(value) {
  const text = String(value || '').replaceAll('-', '');
  return text.slice(-4);
}

export function sessionPathLabel(row) {
  const text = String((row || {}).cwd || '').replaceAll('\\', '/').replace(/\/+$/g, '');
  const part = text.split('/').filter(Boolean).pop() || '';
  return part ? `${part}/` : '';
}

export function sessionLabel(row) {
  const name = String((row || {}).thread_name || '').trim();
  const compact = compactSessionId((row || {}).session_id || '');
  if (name) return compact ? `${name} · ${compact}` : name;
  const path = sessionPathLabel(row);
  if (compact && path) return `${path} · ${compact}`;
  return compact || path;
}

export function sessionDetailLabel(row) {
  const name = String((row || {}).thread_name || '').trim();
  const id = String((row || {}).session_id || '').trim();
  const compact = compactSessionId(id);
  if (compact && name) return `${name} · ${compact}`;
  const path = sessionPathLabel(row);
  if (compact && path) return `${path} · ${compact}`;
  return compact || path || name || '';
}

export function sessionLabelParts(row) {
  const name = String((row || {}).thread_name || '').trim();
  const path = sessionPathLabel(row);
  const compact = compactSessionId((row || {}).session_id || '');
  const primary = name || path;
  return { primary, compact };
}

export function sessionLabelMarkup(row) {
  const parts = sessionLabelParts(row);
  if (parts.primary && parts.compact) {
    return `<span class="session-label"><span class="session-label-name">${esc(parts.primary)}</span><span class="session-label-separator">·</span><span class="session-label-id">${esc(parts.compact)}</span></span>`;
  }
  return esc(parts.primary || parts.compact || '(unknown)');
}

export function sessionDetailMetric(row) {
  const name = String((row || {}).thread_name || '').trim() || sessionPathLabel(row) || 'unnamed';
  const id = compactSessionId((row || {}).session_id || '') || '-';
  return `<div class="detail-cell session"><div class="label">Session</div><div class="value" title="${esc(sessionDetailLabel(row))}"><span class="session-detail-name">${esc(name)}</span><span class="session-label-separator">·</span><span class="session-detail-id">${esc(id)}</span></div></div>`;
}

export function confidenceLabel(value) {
  const labels = {
    spawn_call_turn_context: 'spawn call',
    child_task_time_overlap: 'time overlap',
    spawn_edge_nearest_parent_turn: 'nearest parent',
    parent_pruned_by_retention: 'parent pruned',
    orphan: 'orphan',
  };
  return labels[value] || value || 'unknown';
}

export function confidenceDescription(value) {
  const descriptions = {
    spawn_call_turn_context: 'direct spawn_agent call in the parent turn transcript linked this child session',
    child_task_time_overlap: 'child session started inside the parent turn working-time window',
    spawn_edge_nearest_parent_turn: 'fallback match to the nearest earlier parent turn in the same parent session',
    parent_pruned_by_retention: 'parent turn was removed by retention while the child session remains visible',
    orphan: 'no parent attribution was found for this child session',
  };
  return descriptions[value] || 'no attribution rule description is available for this value';
}

export function confidenceDisplay(value) {
  return `${confidenceLabel(value)} - ${confidenceDescription(value)}`;
}

export function toolDescription(value) {
  const descriptions = {
    exec_command: 'shell command execution output captured from terminal runs',
    write_stdin: 'input sent to an existing interactive terminal session',
    wait_agent: 'wait time and result payload from a delegated subagent',
    spawn_agent: 'subagent creation call used to delegate parallel work',
    close_agent: 'subagent shutdown call after delegated work is no longer needed',
    send_input: 'follow-up input sent to an existing delegated subagent',
    resume_agent: 'request to resume a previously closed delegated subagent',
    update_plan: 'task plan update emitted during multi-step work',
    view_image: 'local image inspection result rendered for the assistant',
    get_goal: 'active goal lookup used to inspect long-running task state',
    update_goal: 'active goal completion update',
    _create_pull_request: 'GitHub pull request creation through the connector',
  };
  return descriptions[value] || 'tool output rows grouped by this tool name';
}

export function toolDisplay(value) {
  const name = value || 'unknown';
  return `${name} - ${toolDescription(name)}`;
}

export function pct(value, total) {
  return total > 0 ? money.format((value / total) * 100) + '%' : '0%';
}

export function exactNumber(value, kind = 'number') {
  const number = Number(value || 0);
  if (!Number.isFinite(number)) return kind === 'money' ? money.format(0) : fmt.format(0);
  return kind === 'money' ? money.format(number) : fmt.format(number);
}

export function compactNumber(value, kind = 'number') {
  const number = Number(value || 0);
  if (!Number.isFinite(number)) return exactNumber(0, kind);
  const abs = Math.abs(number);
  if (abs < 1000) return exactNumber(number, kind);
  const units = [
    [1_000_000_000_000, 'T'],
    [1_000_000_000, 'B'],
    [1_000_000, 'M'],
    [1_000, 'K'],
  ];
  const [divisor, suffix] = units.find(([threshold]) => abs >= threshold) || [1, ''];
  const scaled = number / divisor;
  const scaledAbs = Math.abs(scaled);
  const digits = scaledAbs < 10 ? 2 : 1;
  const text = scaled.toFixed(digits).replace(/\.0+$/, '').replace(/(\.\d*[1-9])0+$/, '$1');
  return `${text}${suffix}`;
}

export function compactNumberSpan(value, kind = 'number') {
  const exact = exactNumber(value, kind);
  const compact = compactNumber(value, kind);
  return `<span class="compact-number" title="${esc(exact)}">${esc(compact)}</span>`;
}

export function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes >= 1_000_000_000) return `${money.format(bytes / 1_000_000_000)} GB`;
  if (bytes >= 1_000_000) return `${money.format(bytes / 1_000_000)} MB`;
  if (bytes >= 1_000) return `${money.format(bytes / 1_000)} KB`;
  return `${fmt.format(bytes)} B`;
}

export function durationLabel(value) {
  const ms = Number(value || 0);
  if (!ms) return '-';
  if (ms < 1000) return `${fmt.format(ms)} ms`;
  if (ms >= 60000) return `${money.format(ms / 60000)} min`;
  return `${money.format(ms / 1000)} s`;
}

export function elapsedMs(start, stop) {
  const started = Date.parse(start || '');
  const stopped = Date.parse(stop || '');
  if (!Number.isFinite(started) || !Number.isFinite(stopped) || stopped < started) return 0;
  return stopped - started;
}

export function compactTime(value) {
  const text = String(value || '');
  if (!text) return '-';
  const match = text.match(/T?(\d{2}:\d{2}:\d{2})/);
  return match ? match[1] : text;
}

export function compactDate(value) {
  const text = String(value || '');
  if (!text) return '-';
  const match = text.match(/(\d{4}-\d{2}-\d{2})/);
  return match ? match[1] : text;
}

export function compactDateTime(value) {
  const text = String(value || '');
  if (!text) return '-';
  const match = text.match(/(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})/);
  return match ? `${match[1]} ${match[2]}` : text;
}

export function toolOutputTokens(row) {
  const reported = Number(row.output_reported_tokens || 0);
  if (reported > 0) return reported;
  return Math.ceil(Number(row.output_chars || 0) / 4);
}
