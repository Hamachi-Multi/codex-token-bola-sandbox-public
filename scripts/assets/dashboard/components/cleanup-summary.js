export function createCleanupSummary() {
  const selectedLabel = document.getElementById('cleanup-selected-label');
  const selectedValue = document.getElementById('cleanup-selected-bytes');
  const cutoffLabel = document.getElementById('cleanup-selected-cutoff-label');
  const cutoffValue = document.getElementById('cleanup-selected-count');
  const affectedFiles = document.getElementById('cleanup-retention-files');
  const actionButton = document.getElementById('cleanup-delete');

  function setAction({ label = 'Delete Logs', enabled = false, title = '' } = {}) {
    if (!actionButton) return;
    actionButton.textContent = label;
    actionButton.disabled = !enabled;
    actionButton.title = title;
  }

  function renderReady(view) {
    if (selectedLabel) selectedLabel.textContent = view.selectedLabel;
    if (selectedValue) {
      selectedValue.textContent = view.selectedValue;
      selectedValue.title = view.selectedTitle;
    }
    if (cutoffLabel) cutoffLabel.textContent = view.cutoffLabel;
    if (cutoffValue) cutoffValue.textContent = view.cutoffValue;
    if (affectedFiles) {
      affectedFiles.textContent = view.affectedFilesValue;
      affectedFiles.title = view.affectedFilesTitle;
    }
    setAction(view.action);
  }

  function renderLoading() {
    if (selectedLabel) selectedLabel.textContent = 'Segment Rows';
    if (cutoffLabel) cutoffLabel.textContent = 'Delete Through';
    if (selectedValue) {
      selectedValue.innerHTML = '<span class="sr-only">Loading cleanup rows.</span><span class="cleanup-summary-loading-cell value" aria-hidden="true"></span>';
      selectedValue.removeAttribute('title');
    }
    if (cutoffValue) {
      cutoffValue.innerHTML = '<span class="sr-only">Loading cleanup cutoff.</span><span class="cleanup-summary-loading-cell hint" aria-hidden="true"></span>';
      cutoffValue.removeAttribute('title');
    }
    if (affectedFiles) {
      affectedFiles.innerHTML = '<span class="sr-only">Loading affected files.</span><span class="cleanup-summary-loading-cell hint short" aria-hidden="true"></span>';
      affectedFiles.removeAttribute('title');
    }
    setAction({ enabled: false, title: 'Loading cleanup preview' });
  }

  function renderUnavailable(message = 'Preview unavailable') {
    setAction({ enabled: false, title: message });
  }

  return { renderLoading, renderReady, renderUnavailable };
}
