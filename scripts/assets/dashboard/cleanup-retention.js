import { CLEANUP_RETENTION_MODES, state } from './core.js';
import { compactNumber, exactNumber } from './formatters.js';

export function normalizeCleanupRetentionMode(value) {
  const mode = String(value || '');
  return CLEANUP_RETENTION_MODES.includes(mode) ? mode : '7';
}

export function cleanupStatusClass(value) {
  return ['deletable', 'protected', 'clean', 'missing'].includes(value) ? value : 'clean';
}

export function cleanupRetentionState(row, stats) {
  if (cleanupStatusClass((row || {}).status) === 'missing') return 'missing';
  if (Number((stats || {}).deletableRows || 0) > 0) return 'deletable';
  if (Number((stats || {}).scannedRows || 0) > 0) return 'clean';
  return 'protected';
}

export function cleanupFileGroupDescription(label) {
  const descriptions = {
    'Raw Current Segments': 'active raw segments',
    'Normalized Outputs': 'normalized raw-log outputs',
    'Analytics Database': 'analytics SQLite database',
    'Archived Raw Logs': 'archived raw prefixes',
    'Pending Turn State': 'orphan turn-start state',
    'State Files': 'processing state indexes',
  };
  return descriptions[String(label || '')] || 'service-managed file group used by the dashboard';
}

export function localDateDaysAgo(days, now = new Date()) {
  const date = new Date(now.getTime());
  date.setHours(12, 0, 0, 0);
  date.setDate(date.getDate() - days);
  const year = String(date.getFullYear()).padStart(4, '0');
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

export function cleanupRetentionTimezone() {
  try {
    return String(Intl.DateTimeFormat().resolvedOptions().timeZone || '');
  } catch {
    return '';
  }
}

export function cleanupAllMode() {
  return String(state.cleanupRetentionMode || '') === 'all';
}

export function cleanupRetentionDate() {
  const mode = String(state.cleanupRetentionMode || '7');
  const input = document.getElementById('cleanup-retention-date');
  if (mode === 'all') return '';
  if (!input) return localDateDaysAgo(7);
  if (mode === 'custom') {
    if (!input.value) input.value = localDateDaysAgo(7);
    return input.value;
  }
  const days = Number(mode);
  const normalizedDays = [1, 7, 14, 30, 90].includes(days) ? days : 7;
  input.value = localDateDaysAgo(normalizedDays);
  return input.value;
}

export function setCleanupRetentionMode(value) {
  const mode = normalizeCleanupRetentionMode(value);
  state.cleanupRetentionMode = mode;
  const input = document.getElementById('cleanup-retention-date');
  const customState = document.getElementById('cleanup-retention-custom-state');
  if (input && mode === 'all') input.value = '';
  if (customState) customState.hidden = mode !== 'custom';
  document.querySelectorAll('[data-cleanup-retention-preset]').forEach(button => {
    const active = button.dataset.cleanupRetentionPreset === mode;
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

export function emptyCleanupRetention() {
  return {selected: {deletable_rows: 0, deletable_bytes: 0, affected_files: 0, cutoff_date: '', timezone: '', cutoff_unix: 0, files: []}};
}

export function allModeRetentionRows(retention) {
  return Number((retention || {}).scanned_rows || 0);
}

export function cleanupCountLabel(value, noun) {
  const count = Number(value || 0);
  return `${compactNumber(count)} ${count === 1 ? noun : `${noun}s`}`;
}

export function cleanupDisplayForRow(row) {
  const key = cleanupAllMode() ? 'delete_all_display' : 'display';
  return ((row || {})[key] || {});
}

export function cleanupRetentionFilesForMode() {
  const files = ((((state.cleanupRetention || {}).selected || {}).files) || []);
  if (!cleanupAllMode()) return files;
  return files.map(file => {
    const scannedRows = Number((file || {}).scanned_rows || 0);
    return {
      ...file,
      deletable_rows: scannedRows,
      kept_rows: 0,
      deletable_bytes: Number((file || {}).source_size || (file || {}).deletable_bytes || 0),
      affected: scannedRows > 0 || Number((file || {}).source_size || 0) > 0,
    };
  });
}

export function cleanupImpactTotals(rows = state.cleanupRows || []) {
  const totals = {affectedFiles: 0};
  for (const row of rows || []) {
    const stats = retentionStatsForRow(row);
    totals.affectedFiles += Number(stats.affectedFiles || 0);
  }
  return totals;
}

export function cleanupRawSegmentRows(rows = state.cleanupRows || []) {
  const row = (rows || []).find(item => String((item || {}).label || '') === 'Raw Current Segments');
  return row ? Number(retentionStatsForRow(row).deletableRows || 0) : 0;
}

export function cleanupSummaryViewModel() {
  const retention = (state.cleanupRetention || {}).selected || {};
  const allMode = cleanupAllMode();
  const totals = cleanupImpactTotals();
  const rawSegmentRows = cleanupRawSegmentRows();
  const cutoff = Number(retention.cutoff_unix || 0);
  const cutoffDate = String(retention.cutoff_date || '');
  return {
    selectedLabel: 'Segment Rows',
    selectedValue: cleanupCountLabel(rawSegmentRows, 'row'),
    selectedTitle: exactNumber(rawSegmentRows),
    cutoffLabel: 'Delete Through',
    cutoffValue: allMode ? 'all logs' : (cutoff > 0 && cutoffDate ? cleanupCutoffLabel() : 'cutoff unavailable'),
    affectedFilesValue: cleanupCountLabel(totals.affectedFiles, 'file'),
    affectedFilesTitle: exactNumber(totals.affectedFiles),
    action: {
      label: allMode ? 'Delete All Logs' : 'Delete Logs',
      enabled: state.cleanupRetentionAvailable,
      title: state.cleanupRetentionAvailable ? '' : 'Preview unavailable',
    },
  };
}

export function cleanupRowPaths(row) {
  return String((row || {}).path || '').split(',').map(value => value.trim()).filter(Boolean);
}

export function cleanupRetentionEffect(row) {
  return String((row || {}).retention_effect || 'not changed by retention');
}

export function retentionStatsForRow(row) {
  const display = cleanupDisplayForRow(row);
  const deletableRows = Number(display.affected_rows || 0);
  const scannedRows = Number(display.total_rows || deletableRows);
  const affectedFiles = Number(display.affected_files || 0);
  return {
    scannedRows,
    deletableRows,
    deletableBytes: Number(display.delete_size || 0),
    keptRows: Math.max(0, scannedRows - deletableRows),
    affectedFiles,
  };
}

export function cleanupSummaryForRow(row) {
  const display = cleanupDisplayForRow(row);
  const hasTargets = Array.isArray(display.targets);
  const hasItems = Array.isArray(display.items);
  return {
    operation: String(display.action || '-'),
    deleteSize: Number(display.delete_size || 0),
    scopeLabel: String(display.scope_label || '0 files'),
    scopeCount: Number(display.scope_count || 0),
    scopeUnit: String(display.scope_unit || 'none'),
    detailTitle: String(display.detail_title || 'Affected Files'),
    detailItemsKind: String(display.detail_items_kind || 'empty'),
    hasTargets: Array.isArray(display.targets),
    hasItems: Array.isArray(display.items),
    targets: hasTargets ? display.targets : [],
    items: hasItems ? display.items : [],
  };
}

export function cleanupPercent(value) {
  const normalized = Number.isFinite(Number(value)) ? Number(value) : 0;
  return `${(normalized * 100).toFixed(2)}%`;
}

export function cleanupCutoffLabel() {
  if (cleanupAllMode()) return 'all logs';
  const retention = ((state.cleanupRetention || {}).selected || {});
  const cutoffDate = String(retention.cutoff_date || '');
  const timezone = String(retention.timezone || '');
  if (!cutoffDate) return 'cutoff unavailable';
  return timezone ? `${cutoffDate} · ${timezone}` : cutoffDate;
}

export function cleanupCutoffDateLabel() {
  if (cleanupAllMode()) return 'all logs';
  const cutoffDate = String((((state.cleanupRetention || {}).selected || {}).cutoff_date) || '');
  return cutoffDate || 'cutoff unavailable';
}
