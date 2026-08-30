import { fmt } from '../core.js';

export function createPager({ rootId, onPageChange, previousButtonId = '', nextButtonId = '' }) {
  const root = document.getElementById(rootId);
  let current = { page: 1, total: 0, perPage: 1, busy: false };

  function setBusy(busy) {
    current.busy = Boolean(busy);
    if (!root) return;
    root.setAttribute('aria-busy', current.busy ? 'true' : 'false');
    root.querySelectorAll('button').forEach(button => {
      button.disabled = current.busy || button.dataset.boundaryDisabled === 'true';
    });
  }

  function render({ page, total, perPage, busy = current.busy }) {
    if (!root) return;
    const normalizedTotal = Math.max(0, Number(total || 0));
    const normalizedPerPage = Math.max(1, Number(perPage || 1));
    const pageCount = Math.max(1, Math.ceil(normalizedTotal / normalizedPerPage));
    const normalizedPage = Math.max(1, Math.min(Number(page || 1), pageCount));
    const start = normalizedTotal ? (normalizedPage - 1) * normalizedPerPage + 1 : 0;
    const end = Math.min(normalizedTotal, normalizedPage * normalizedPerPage);
    current = {
      page: normalizedPage,
      total: normalizedTotal,
      perPage: normalizedPerPage,
      busy: Boolean(busy),
    };
    root.innerHTML = `
      <button${previousButtonId ? ` id="${previousButtonId}"` : ''} data-page-direction="prev" data-boundary-disabled="${normalizedPage <= 1}">Prev</button>
      <span class="page-status">${fmt.format(start)}-${fmt.format(end)} / ${fmt.format(normalizedTotal)}</span>
      <button${nextButtonId ? ` id="${nextButtonId}"` : ''} data-page-direction="next" data-boundary-disabled="${normalizedPage >= pageCount}">Next</button>
    `;
    root.querySelectorAll('[data-page-direction]').forEach(button => {
      button.addEventListener('click', () => {
        if (current.busy) return;
        const direction = button.dataset.pageDirection === 'next' ? 1 : -1;
        const nextPage = Math.max(1, Math.min(current.page + direction, pageCount));
        if (nextPage !== current.page) onPageChange(nextPage);
      });
    });
    setBusy(current.busy);
  }

  function clear() {
    if (!root) return;
    root.innerHTML = '';
    root.removeAttribute('aria-busy');
    current = { page: 1, total: 0, perPage: 1, busy: false };
  }

  return { clear, render, setBusy };
}
