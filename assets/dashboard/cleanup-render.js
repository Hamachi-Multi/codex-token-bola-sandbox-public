import { CLEANUP_AFFECTED_FILE_PAGE_SIZE, fmt, state } from './core.js';
import { esc } from './ui.js';
import { tableHeader } from './dom.js';
import { compactNumber, exactNumber, formatBytes } from './formatters.js';
import {
  allModeRetentionRows,
  cleanupAllMode,
  cleanupCutoffDateLabel,
  cleanupCutoffLabel,
  cleanupDisplayForRow,
  cleanupFileGroupDescription,
  cleanupRetentionEffect,
  cleanupRetentionFilesForMode,
  cleanupRetentionState,
  cleanupRowPaths,
  cleanupSummaryForRow,
  cleanupImpactTotals,
  retentionStatsForRow,
} from './cleanup-retention.js';

export function createCleanupRenderController({ updateOpenCleanupDetailModal }) {
function cleanupConfirmMetric(label, value, title = '') {
  const titleAttr = title ? ` title="${esc(title)}"` : '';
  return `<div><dt>${esc(label)}</dt><dd${titleAttr}>${esc(value)}</dd></div>`;
}

function retentionDeleteConfirmBody() {
  const retention = (state.cleanupRetention || {}).selected || {};
  const rows = Number(retention.deletable_rows || 0);
  const files = cleanupImpactTotals().affectedFiles;
  const cutoff = cleanupCutoffLabel();
  return `<dl class="cleanup-confirm-summary">
    ${cleanupConfirmMetric('Rows', compactNumber(rows), exactNumber(rows))}
    ${cleanupConfirmMetric('Cutoff', cutoff)}
    ${cleanupConfirmMetric('Files', compactNumber(files), exactNumber(files))}
  </dl>
  <p class="cleanup-confirm-warning">This deletes every eligible raw log row across service-managed files, then rebuilds analysis data.</p>`;
}

function allDataDeleteConfirmBody() {
  const retention = (state.cleanupRetention || {}).selected || {};
  const rows = allModeRetentionRows(retention);
  const files = cleanupImpactTotals().affectedFiles;
  return `<dl class="cleanup-confirm-summary">
    ${cleanupConfirmMetric('Rows', compactNumber(rows), exactNumber(rows))}
    ${cleanupConfirmMetric('Cutoff', 'all logs')}
    ${cleanupConfirmMetric('Files', compactNumber(files), exactNumber(files))}
  </dl>
  <p class="cleanup-confirm-warning">This deletes every raw log row across service-managed files, then clears generated outputs managed by this service.</p>`;
}

function cleanupRowsSignature(rows) {
  return (rows || []).map(row => [
    row.group_id || '',
    row.retention_effect || '',
  ].join(String.fromCharCode(31))).join(String.fromCharCode(30));
}

const CLEANUP_TABLE_HEADERS = [
  {label:'File Group'},
  {label:'Total Size', cls:'num'},
  {label:'Affected Size', cls:'num'},
  {label:'Affected Files', cls:'cleanup-affected-files-header'},
];
function renderCleanupTableFrame(body, rowCount, options = {}) {
  const busyAttr = options.ariaBusy ? ' aria-busy="true"' : '';
  const labelAttr = options.ariaLabel ? ` aria-label="${esc(options.ariaLabel)}"` : '';
  return `<div class="table-scroll"><div class="table-header-shadow" aria-hidden="true"></div><table${busyAttr}${labelAttr}>
    <colgroup>
      <col class="cleanup-file-col">
      <col class="cleanup-size-col">
      <col class="cleanup-affected-size-col">
      <col class="cleanup-affected-files-col">
    </colgroup>
    <thead><tr>${CLEANUP_TABLE_HEADERS.map(tableHeader).join('')}</tr></thead>
    <tbody style="--cleanup-row-count:${rowCount}">${body}</tbody></table></div>`;
}

function cleanupSourceFileName(path) {
  const text = String(path || '');
  return text.split('/').filter(Boolean).pop() || text || '-';
}

function cleanupSourceDirectory(path) {
  const text = String(path || '');
  const lastSlash = text.lastIndexOf('/');
  if (lastSlash <= 0) return text || '(unknown directory)';
  return text.slice(0, lastSlash);
}

function cleanupFileGroupDisplay(label) {
  const labels = {
    'Raw Current Segments': 'Current Raw Segments',
    'Normalized Outputs': 'Normalized Outputs',
    'Analytics Database': 'Analytics Database',
    'Archived Raw Logs': 'Archived Raw Logs',
    'Pending Turn State': 'Pending Turn State',
    'State Files': 'Service State Files',
  };
  return labels[String(label || '')] || String(label || '').toLowerCase();
}

function cleanupSourceKind(path, file = null) {
  const name = cleanupSourceFileName(path);
  if (name.startsWith('prompt-usage.raw.jsonl')) return 'prompt usage';
  if (name.includes('normalized')) return 'normalized';
  if (name.endsWith('.sqlite')) return 'database';
  if (name.endsWith('.gz')) return 'gzip archive';
  return 'service file';
}

function cleanupSourceRowsForPath(path) {
  const files = (((state.cleanupRetention || {}).selected || {}).files || []);
  const text = String(path || '');
  return files.find(file => String((file || {}).path || '') === text) || null;
}

function cleanupFilesFromPaths(paths, row, options = {}) {
  return paths.map(path => cleanupSourceRowsForPath(path) || {
    path,
    source_size: path === String((row || {}).path || '') ? Number((row || {}).bytes || 0) : 0,
    source_mtime_ns: 0,
    scanned_rows: 0,
    deletable_rows: 0,
    deletable_bytes: 0,
    affected: Boolean(options.affected),
    is_derived_output: Boolean(options.isDerivedOutput),
  });
}

function cleanupDetailFilesForRow(row) {
  const summary = cleanupSummaryForRow(row);
  if (summary.hasItems) return summary.items;
  if (summary.detailItemsKind === 'derived_outputs') {
    const paths = summary.hasTargets ? summary.targets : cleanupRowPaths(row);
    return cleanupFilesFromPaths(paths, row, {affected: true, isDerivedOutput: true});
  }
  if (summary.hasTargets) return cleanupFilesFromPaths(summary.targets, row);
  const paths = cleanupRowPaths(row);
  const files = cleanupRetentionFilesForMode();
  const matched = files.filter(file => {
    const filePath = String((file || {}).path || '');
    return paths.some(path => filePath === path || filePath.startsWith(path + '/'));
  });
  if (matched.length) return matched;
  return cleanupFilesFromPaths(paths, row);
}

function cleanupDefinitionItem(label, value, cls = '') {
  const classAttr = cls ? ` class="${esc(cls)}"` : '';
  const iconLabel = cleanupAffectedFilesSummaryLabel(label);
  return `<div${classAttr}><dt title="${esc(iconLabel)}" aria-label="${esc(iconLabel)}"><span class="cleanup-detail-summary-icon">${cleanupAffectedFilesSummaryIcon(label)}</span></dt><dd>${esc(value)}</dd></div>`;
}

function cleanupDetailCutoffMeta() {
  return `<div class="cleanup-detail-meta"><span>Delete before</span><span class="cleanup-detail-meta-value">${esc(cleanupCutoffDateLabel())}</span></div>`;
}

function cleanupDetailSummaryItems(row, stats) {
  return cleanupAffectedFilesSummaryItems(row, stats);
}

function renderCleanupDetailSummary(row, stats) {
  const groupName = row.label || '';
  const groupDisplay = cleanupFileGroupDisplay(groupName);
  const description = `${cleanupFileGroupDescription(groupName)} · ${cleanupRetentionEffect(row)}`;
  return `<section class="cleanup-detail-hero">
    <div class="cleanup-detail-identity">
      <div class="cleanup-detail-heading">
        <p class="cleanup-detail-description"><span class="cleanup-detail-description-name">${esc(groupDisplay)}</span> <span class="cleanup-detail-description-copy">${esc(description)}</span></p>
      </div>
      ${cleanupDetailCutoffMeta()}
    </div>
    <dl class="cleanup-detail-summary">
      ${cleanupDetailSummaryItems(row, stats).map(([label, value]) => cleanupDefinitionItem(label, value)).join('')}
    </dl>
  </section>`;
}

function cleanupHasAffectedFileImpact(row, stats = retentionStatsForRow(row)) {
  const summary = cleanupSummaryForRow(row);
  if (summary.operation === '-') return false;
  return Number((stats || {}).affectedFiles || 0) > 0 || Number(summary.deleteSize || 0) > 0;
}

function cleanupAffectedFileAction(row, file) {
  const summary = cleanupSummaryForRow(row);
  if (summary.operation === '-') return '-';
  if (summary.detailItemsKind === 'derived_outputs') return 'Rebuild';
  if (summary.detailItemsKind === 'file_targets' && summary.operation === 'Delete') return 'Delete';
  if (cleanupAllMode()) return 'Delete';
  const scannedRows = Number((file || {}).scanned_rows || 0);
  const deleteRows = Number((file || {}).deletable_rows || 0);
  if (deleteRows > 0 && scannedRows > deleteRows) return 'Rewrite';
  if (deleteRows > 0) return 'Delete';
  return '-';
}

function cleanupActionFileCounts(row, stats = retentionStatsForRow(row)) {
  const display = cleanupDisplayForRow(row);
  const actionCounts = (display || {}).action_file_counts || {};
  return {
    Delete: Math.max(0, Number(actionCounts.Delete || 0)),
    Rewrite: Math.max(0, Number(actionCounts.Rewrite || 0)),
    Rebuild: Math.max(0, Number(actionCounts.Rebuild || 0)),
  };
}

function cleanupActionFileColumnText(count) {
  const normalized = Math.max(0, Number(count || 0));
  return cleanupFileColumnNumberText(normalized);
}

function cleanupFileColumnNumberText(count) {
  const normalized = Math.max(0, Number(count || 0));
  return compactNumber(normalized);
}

function cleanupAffectedFilesTotalCount(row, stats = retentionStatsForRow(row)) {
  const counts = cleanupActionFileCounts(row, stats);
  return counts.Delete + counts.Rebuild + counts.Rewrite;
}

function cleanupAffectedFilesSummaryItems(row, stats = retentionStatsForRow(row)) {
  const counts = cleanupActionFileCounts(row, stats);
  return [
    ['Total', cleanupFileColumnNumberText(cleanupAffectedFilesTotalCount(row, stats))],
    ['Delete', cleanupActionFileColumnText(counts.Delete)],
    ['Rebuild', cleanupActionFileColumnText(counts.Rebuild)],
    ['Rewrite', cleanupActionFileColumnText(counts.Rewrite)],
  ];
}

function cleanupAffectedFilesSummaryLabel(label) {
  const labels = {
    Total: 'Total Files',
    Delete: 'Delete Files',
    Rebuild: 'Rebuild Files',
    Rewrite: 'Rewrite Files',
  };
  return labels[label] || String(label || '');
}

function cleanupAffectedFilesSummaryTitle(row, stats = retentionStatsForRow(row)) {
  return cleanupAffectedFilesSummaryItems(row, stats)
    .map(([label, value]) => `${cleanupAffectedFilesSummaryLabel(label)} ${value}`)
    .join(', ');
}

function cleanupActionIcon(label) {
  const icons = {
    Delete: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 4.5h9"></path><path d="M6.5 4.5v-2h3v2"></path><path d="M5 6.5l.5 6h5l.5-6"></path><path d="M7 7.5v3.5"></path><path d="M9 7.5v3.5"></path></svg>',
    Rebuild: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M12.5 6.5a4.5 4.5 0 1 0-1.1 4.6"></path><path d="M12.5 3.5v3h-3"></path></svg>',
    Rewrite: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 12.5l2.8-.6 6-6a1.4 1.4 0 0 0-2-2l-6 6-.8 2.6z"></path><path d="M9.5 4.7l1.8 1.8"></path></svg>',
  };
  return icons[label] || '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 8h8"></path></svg>';
}

function cleanupAffectedFilesSummaryIcon(label) {
  if (label !== 'Total') return cleanupActionIcon(label);
  return '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 4.5h9v7h-9z"></path><path d="M5.5 2.5h7v7"></path></svg>';
}

function renderCleanupAffectedFilesSummaryCell(row, stats = retentionStatsForRow(row)) {
  const items = cleanupAffectedFilesSummaryItems(row, stats);
  const title = cleanupAffectedFilesSummaryTitle(row, stats);
  return `<span class="cleanup-affected-files-summary" title="${esc(title)}" aria-label="${esc(title)}">${items.map(([label, value]) => {
    const iconLabel = cleanupAffectedFilesSummaryLabel(label);
    return `<span class="cleanup-affected-files-part" title="${esc(iconLabel)}" aria-label="${esc(iconLabel)}" data-kind="${esc(label.toLowerCase())}"><span class="cleanup-affected-files-icon">${cleanupAffectedFilesSummaryIcon(label)}</span><span class="cleanup-affected-files-value">${esc(value)}</span></span>`;
  }).join('')}</span>`;
}

function renderCleanupAffectedFilesSummaryLoadingCell() {
  const parts = ['total', 'delete', 'rebuild', 'rewrite'];
  return `<span class="cleanup-affected-files-summary cleanup-affected-files-loading">${parts.map(kind => `<span class="cleanup-affected-files-part" data-kind="${esc(kind)}"><span class="cleanup-affected-files-icon"><span class="cleanup-loading-cell tiny"></span></span><span class="cleanup-affected-files-value"><span class="cleanup-loading-cell short"></span></span></span>`).join('')}</span>`;
}

function renderCleanupAffectedFileLedgerRows(row, files) {
  if (!files.length) return '<div class="cleanup-affected-file-empty">No affected files.</div>';
  return files.map(file => {
    const path = String((file || {}).path || '');
    const kind = cleanupSourceKind(path, file);
    const fileName = cleanupSourceFileName(path);
    const directory = cleanupSourceDirectory(path);
    const action = cleanupAffectedFileAction(row, file);
    return `<div class="cleanup-affected-file-row">
      <div class="cleanup-affected-file-row-main">
        <div class="cleanup-affected-file-cell">
          <span class="cleanup-affected-file-kind" title="${esc(kind)}">${esc(kind)}</span>
          <span class="cleanup-affected-file-name" title="${esc(fileName)}">${esc(fileName)}</span>
        </div>
        <span class="cleanup-affected-file-action" data-action="${esc(action.toLowerCase())}" title="${esc(action)}" aria-label="${esc(action)}"><span class="cleanup-affected-file-action-icon">${cleanupActionIcon(action)}</span></span>
      </div>
      <div class="cleanup-affected-file-directory"><span class="cleanup-affected-file-directory-label">Dir:</span><span class="cleanup-affected-file-path" title="${esc(directory)}">${esc(directory)}</span></div>
    </div>`;
  }).join('');
}

function cleanupAffectedFilePageState(files) {
  const total = files.length;
  const pages = Math.max(1, Math.ceil(total / CLEANUP_AFFECTED_FILE_PAGE_SIZE));
  const page = Math.max(1, Math.min(Number(state.cleanupDetailPage || 1), pages));
  state.cleanupDetailPage = page;
  const start = (page - 1) * CLEANUP_AFFECTED_FILE_PAGE_SIZE;
  const end = Math.min(total, start + CLEANUP_AFFECTED_FILE_PAGE_SIZE);
  return {
    total,
    pages,
    page,
    start,
    end,
    pageFiles: files.slice(start, end),
  };
}

function renderCleanupAffectedFilePager(pageState) {
  const from = pageState.total ? pageState.start + 1 : 0;
  const prevPage = Math.max(1, pageState.page - 1);
  const nextPage = Math.min(pageState.pages, pageState.page + 1);
  return `<div class="pager cleanup-affected-file-pager">
    <button type="button" data-cleanup-affected-file-page="${prevPage}" ${pageState.page <= 1 ? 'disabled' : ''}>Prev</button>
    <span class="page-status">${esc(fmt.format(from))}-${esc(fmt.format(pageState.end))} / ${esc(fmt.format(pageState.total))}</span>
    <button type="button" data-cleanup-affected-file-page="${nextPage}" ${pageState.page >= pageState.pages ? 'disabled' : ''}>Next</button>
  </div>`;
}

function setCleanupAffectedFilePage(page) {
  state.cleanupDetailPage = Math.max(1, Number(page || 1));
  updateOpenCleanupDetailModal();
}

function bindCleanupAffectedFilePager() {
  document.querySelectorAll('[data-cleanup-affected-file-page]').forEach(button => {
    button.addEventListener('click', () => setCleanupAffectedFilePage(button.dataset.cleanupAffectedFilePage));
  });
}

function renderCleanupAffectedFileLedger(row, files, pageState = cleanupAffectedFilePageState(files)) {
  return `<section class="cleanup-affected-file-ledger">
    ${renderCleanupAffectedFileLedgerHeader(row, files)}
    <div class="cleanup-affected-file-list">${renderCleanupAffectedFileLedgerRows(row, pageState.pageFiles)}</div>
  </section>`;
}

function cleanupAffectedFileLoadingState(row) {
  const files = cleanupDetailLedgerFilesForRow(row);
  if (files.length) {
    return {
      files,
      pageState: cleanupAffectedFilePageState(files),
      affectedFileCount: files.length,
    };
  }
  const stats = retentionStatsForRow(row);
  const affectedFileCount = Math.max(0, Number(stats.affectedFiles || 0));
  const pages = Math.max(1, Math.ceil(affectedFileCount / CLEANUP_AFFECTED_FILE_PAGE_SIZE));
  const page = Math.max(1, Math.min(Number(state.cleanupDetailPage || 1), pages));
  state.cleanupDetailPage = page;
  const start = (page - 1) * CLEANUP_AFFECTED_FILE_PAGE_SIZE;
  const end = Math.min(affectedFileCount, start + CLEANUP_AFFECTED_FILE_PAGE_SIZE);
  return {
    files,
    pageState: {
      total: affectedFileCount,
      pages,
      page,
      start,
      end,
      pageFiles: Array.from({length: Math.max(0, end - start)}),
    },
    affectedFileCount,
  };
}

function renderCleanupAffectedFileLedgerLoading(row, files, pageState = cleanupAffectedFilePageState(files), headerSummary = null) {
  const loadingRows = pageState.pageFiles.length
    ? pageState.pageFiles.map(() => `<div class="cleanup-affected-file-row cleanup-affected-file-loading-row" aria-hidden="true">
      <div class="cleanup-affected-file-row-main">
        <div class="cleanup-affected-file-cell">
          <span class="cleanup-affected-file-kind"><span class="cleanup-loading-cell short"></span></span>
          <span class="cleanup-affected-file-name"><span class="cleanup-loading-cell wide"></span></span>
        </div>
        <span class="cleanup-affected-file-action"><span class="cleanup-loading-cell short"></span></span>
      </div>
      <div class="cleanup-affected-file-directory"><span class="cleanup-affected-file-directory-label">Dir:</span><span class="cleanup-affected-file-path"><span class="cleanup-loading-cell wide"></span></span></div>
    </div>`).join('')
    : renderCleanupAffectedFileLedgerRows(row, []);
  return `<section class="cleanup-affected-file-ledger">
    ${renderCleanupAffectedFileLedgerHeader(row, files, headerSummary)}
    <div class="cleanup-affected-file-list" aria-hidden="true">${loadingRows}</div>
  </section>`;
}

function renderCleanupAffectedFileLedgerHeader(row, affectedFiles, headerSummary = null) {
  const title = cleanupSummaryForRow(row).detailTitle || 'Affected Files';
  return `<div class="cleanup-affected-file-header">
    <div class="cleanup-affected-file-title">${esc(title)}</div>
  </div>`;
}

function cleanupDetailLedgerFilesForRow(row) {
  const files = cleanupDetailFilesForRow(row);
  if (cleanupSummaryForRow(row).detailItemsKind === 'derived_outputs') return files;
  const stats = retentionStatsForRow(row);
  if (!cleanupHasAffectedFileImpact(row, stats)) return [];
  if (cleanupAllMode()) return files;
  const matched = files.filter(file => Number((file || {}).deletable_rows || 0) > 0);
  if (matched.length) return matched;
  return cleanupSummaryForRow(row).targets.map(path => ({
    path,
    source_size: 0,
    source_mtime_ns: 0,
    scanned_rows: 0,
    deletable_rows: 0,
    deletable_bytes: 0,
    affected: true,
  }));
}

function renderCleanupTable(rows) {
  if (!rows.length) return '<div class="empty">No rows for the current filter.</div>';
	  const body = rows.map((row, index) => {
	      const summary = cleanupSummaryForRow(row);
	      const stats = retentionStatsForRow(row);
	      return `<tr tabindex="0" aria-selected="false" aria-haspopup="dialog" aria-label="${esc(cleanupFileGroupDisplay(row.label || ''))}. Press Enter or Space to open file detail." data-cleanup-index="${index}" data-cleanup-file="${esc(row.group_id || '')}">
	        <td class="truncate-cell cleanup-file-cell" title="${esc(cleanupRetentionEffect(row) + ' · ' + (row.path || ''))}">${esc(cleanupFileGroupDisplay(row.label || ''))}</td>
	        <td class="num cleanup-size-cell">${formatBytes(row.bytes || 0)}</td>
	        <td class="num cleanup-affected-size-cell">${formatBytes(summary.deleteSize)}</td>
        <td class="cleanup-affected-files-cell" title="${esc(cleanupAffectedFilesSummaryTitle(row, stats))}">${renderCleanupAffectedFilesSummaryCell(row, stats)}</td>
      </tr>`;
    }).join('');
  return renderCleanupTableFrame(body, rows.length);
}

function renderCleanupTableLoading(message = 'Loading cleanup data.') {
  const rowCount = 8;
  const body = Array.from({length: rowCount}).map((_, rowIndex) => `<tr class="cleanup-loading-row" aria-hidden="true">
        <td class="truncate-cell cleanup-file-cell"><span class="cleanup-loading-cell wide"></span></td>
        <td class="num cleanup-size-cell"><span class="cleanup-loading-cell medium"></span></td>
        <td class="num cleanup-affected-size-cell"><span class="cleanup-loading-cell short"></span></td>
        <td class="cleanup-affected-files-cell">${renderCleanupAffectedFilesSummaryLoadingCell()}</td>
      </tr>`).join('');
  return `<span class="sr-only">${esc(message)}</span>${renderCleanupTableFrame(body, rowCount, {ariaBusy: true, ariaLabel: message})}`;
}

function updateCleanupFileRows() {
  document.querySelectorAll('#cleanup-files tr[data-cleanup-file]').forEach(rowEl => {
    const index = Number(rowEl.dataset.cleanupIndex || -1);
    const row = (state.cleanupRows || [])[index];
    if (!row) return;
    const summary = cleanupSummaryForRow(row);
    const stats = retentionStatsForRow(row);
    const sizeCell = rowEl.querySelector('.cleanup-size-cell');
    const affectedSizeCell = rowEl.querySelector('.cleanup-affected-size-cell');
    const affectedFilesCell = rowEl.querySelector('.cleanup-affected-files-cell');
    if (sizeCell) sizeCell.textContent = formatBytes(row.bytes || 0);
    if (affectedSizeCell) affectedSizeCell.textContent = formatBytes(summary.deleteSize);
    if (affectedFilesCell) {
      affectedFilesCell.innerHTML = renderCleanupAffectedFilesSummaryCell(row, stats);
      affectedFilesCell.title = cleanupAffectedFilesSummaryTitle(row, stats);
    }
  });
}

function renderCleanupFileDetail(row, detailRow = row) {
  if (!row) return '<div class="empty">Select a managed file group.</div>';
  const stats = retentionStatsForRow(detailRow);
  const retentionState = cleanupRetentionState(detailRow, stats);
  const affectedFiles = cleanupDetailLedgerFilesForRow(detailRow);
  const pageState = cleanupAffectedFilePageState(affectedFiles);
  return `<div class="cleanup-detail ${esc(retentionState)}">
      ${renderCleanupDetailSummary(detailRow, stats)}
      ${renderCleanupAffectedFileLedger(detailRow, affectedFiles, pageState)}
      <div class="cleanup-detail-footer">
        ${renderCleanupAffectedFilePager(pageState)}
        <div class="cleanup-detail-footer-actions"></div>
      </div>
    </div>`;
}

function renderCleanupFileDetailLoading(row) {
  const stats = retentionStatsForRow(row);
  const retentionState = cleanupRetentionState(row, stats);
  const loading = cleanupAffectedFileLoadingState(row);
  return `<div class="cleanup-detail loading ${esc(retentionState)}">
    <span class="sr-only">Loading affected files.</span>
    ${renderCleanupDetailSummary(row, stats)}
    ${renderCleanupAffectedFileLedgerLoading(row, loading.files, loading.pageState, {affectedFileCount: loading.affectedFileCount})}
    <div class="cleanup-detail-footer cleanup-detail-loading-footer" aria-hidden="true"></div>
  </div>`;
}

function renderCleanupFileDetailError(row, message) {
  return `<div class="cleanup-detail error">
    <section class="cleanup-detail-hero">
      <div class="cleanup-detail-identity">
        <div class="cleanup-detail-heading">
          <p class="cleanup-detail-description">${esc(message || 'Cleanup detail failed.')}</p>
        </div>
      </div>
    </section>
    <section class="cleanup-affected-file-ledger">
      <div class="cleanup-affected-file-header"><div class="cleanup-affected-file-title">Affected Files</div></div>
      <div class="cleanup-affected-file-list"><div class="cleanup-affected-file-empty">${esc(message || 'Cleanup detail failed.')}</div></div>
    </section>
  </div>`;
}


return {
  allDataDeleteConfirmBody,
  bindCleanupAffectedFilePager,
  cleanupRowsSignature,
  renderCleanupFileDetail,
  renderCleanupFileDetailError,
  renderCleanupFileDetailLoading,
  renderCleanupTable,
  renderCleanupTableLoading,
  retentionDeleteConfirmBody,
  updateCleanupFileRows,
};
}
