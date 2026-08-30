import {
  clearInteractiveRowSelection,
  handleListArrowFocus,
  refreshScrollFades,
  setInteractiveRowSelected,
  setPanelContent,
} from '../dom.js';
import { esc } from '../ui.js';

export function createListDetailView({
  rowSelector,
  buttonSelector,
  detailId,
  statusId,
  keyForRow,
  pathForRow,
  nextRequestSequence,
  isCurrentRequest,
  commit,
  reset,
  getCachedJSON,
  peekCachedJSON,
  clearQueryStatus,
  showQueryError,
  emptyMessage = 'No rows for the current filter.',
}) {
  function statusElement() {
    return document.getElementById(statusId);
  }

  function selectPending(row) {
    const previous = document.querySelector(`${rowSelector}.selected`);
    clearInteractiveRowSelection(rowSelector);
    setInteractiveRowSelected(row, true);
    return previous;
  }

  function restoreSelection(previous) {
    clearInteractiveRowSelection(rowSelector);
    if (previous?.isConnected) setInteractiveRowSelected(previous, true);
  }

  function commitSelection(row, detail) {
    clearInteractiveRowSelection(rowSelector);
    setInteractiveRowSelected(row, true);
    commit(row, detail);
  }

  function renderEmpty() {
    reset();
    statusElement().textContent = 'none';
    setPanelContent(detailId, emptyMessage, 'empty');
  }

  function renderError(error) {
    reset();
    statusElement().textContent = 'error';
    setPanelContent(detailId, esc(error?.message || error), 'error');
  }

  async function select(row, preparedDetail) {
    clearQueryStatus();
    const path = pathForRow(row);
    const requestSequence = nextRequestSequence();
    if (preparedDetail !== undefined) {
      commitSelection(row, preparedDetail);
      return;
    }
    const cached = peekCachedJSON(path);
    if (cached.hit) {
      commitSelection(row, cached.data);
      return;
    }
    const previous = selectPending(row);
    const previousStatus = statusElement().textContent;
    row.setAttribute('aria-busy', 'true');
    try {
      const detail = await getCachedJSON(path);
      if (!isCurrentRequest(requestSequence) || !row.isConnected) return;
      commitSelection(row, detail);
    } catch (error) {
      if (isCurrentRequest(requestSequence) && row.isConnected) {
        restoreSelection(previous);
        statusElement().textContent = previousStatus;
        if (!previous?.isConnected) renderError(error);
        showQueryError(error?.message || error);
        refreshScrollFades();
      }
    } finally {
      if (row.isConnected) row.removeAttribute('aria-busy');
    }
  }

  function bindRows() {
    document.querySelectorAll(rowSelector).forEach(row => {
      row.addEventListener('click', () => select(row));
    });
    document.querySelectorAll(buttonSelector).forEach(button => {
      button.addEventListener('keydown', event => handleListArrowFocus(event, buttonSelector, true));
    });
  }

  function activateRendered({ isSelected, prepared = null }) {
    const rows = [...document.querySelectorAll(rowSelector)];
    const target = rows.find(isSelected) || rows[0] || null;
    if (!target) {
      renderEmpty();
      return;
    }
    if (prepared?.error && prepared.key === keyForRow(target)) {
      renderError(prepared.error);
      return;
    }
    if (prepared && prepared.key === keyForRow(target)) {
      select(target, prepared.data);
      return;
    }
    select(target);
  }

  return { activateRendered, bindRows, select };
}
