import { getJSON, isServiceBusyError, postJSON } from './api.js';
import { esc } from './ui.js';

const TODAY_UTC = () => new Date().toISOString().slice(0, 10);
const MUTATION_BUSY_DELAY_MS = 120;

function price(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `$${number.toLocaleString(undefined, { maximumFractionDigits: 6 })}` : '—';
}

function ratioText(rate) {
  const ratio = (rate || {}).relative_ratio || {};
  return `Input 1× · Cached ${ratio.cached_input || '—'}× · Output ${ratio.output || '—'}×`;
}

function statusText(model) {
  if (model.status === 'unavailable') return 'Unavailable';
  if (model.status === 'setup_required') return 'Setup required';
  if (model.status === 'configured') return 'Configured';
  return 'Setup required';
}

function periodKey(rate) {
  return rate?.is_default ? 'default' : String(rate?.effective_from || '');
}

function editorMarkup(editor, mutationsBlocked) {
  const rate = editor.rate || {};
  const modelField = editor.newModel
    ? `<div class="cost-rate-field"><label for="cost-rate-model-id">Model ID</label><input id="cost-rate-model-id" name="model_id" value="${esc(rate.model_id || '')}" autocomplete="off" required><p data-cost-error="model_id"></p></div>`
    : `<input type="hidden" name="model_id" value="${esc(rate.model_id || '')}">`;
  const effectiveField = rate.is_default
    ? '<input type="hidden" name="effective_from" value=""><input type="hidden" name="is_default" value="true">'
    : `<div class="cost-rate-field"><label for="cost-rate-effective-from">Effective from (UTC)</label><input id="cost-rate-effective-from" name="effective_from" type="date" value="${esc(rate.effective_from || TODAY_UTC())}" required><input type="hidden" name="is_default" value="false"><p data-cost-error="effective_from"></p></div>`;
  return `
    <form class="cost-rate-editor" data-cost-rate-editor novalidate>
      ${modelField}
      ${effectiveField}
      <div class="cost-rate-price-fields">
        <div class="cost-rate-field"><label for="cost-rate-input-price">Input</label><input id="cost-rate-input-price" name="input_price" inputmode="decimal" value="${esc(rate.input_price || '')}" required><p data-cost-error="input_price"></p></div>
        <div class="cost-rate-field"><label for="cost-rate-cached-price">Cached input</label><input id="cost-rate-cached-price" name="cached_input_price" inputmode="decimal" value="${esc(rate.cached_input_price || '')}" required><p data-cost-error="cached_input_price"></p></div>
        <div class="cost-rate-field"><label for="cost-rate-output-price">Output</label><input id="cost-rate-output-price" name="output_price" inputmode="decimal" value="${esc(rate.output_price || '')}" required><p data-cost-error="output_price"></p></div>
      </div>
      <div class="cost-rate-editor-footer">
        <span class="cost-rate-ratio-preview" data-cost-rate-ratio>Enter all three prices</span>
        <div class="cost-rate-actions">
          <button class="secondary-button compact-button" type="button" data-cost-rate-cancel>Cancel</button>
          <button class="primary-button compact-button" type="submit" ${mutationsBlocked ? 'disabled' : ''}>Save</button>
        </div>
      </div>
    </form>`;
}

function historyMarkup(model, mutationsBlocked) {
  if (!model.history.length) return '<p class="cost-rate-empty">No price history. Add the first rate for this model.</p>';
  return `<div class="cost-rate-history" role="table" aria-label="Price history for ${esc(model.model_id)}">
    <div role="rowgroup">
      <div class="cost-rate-history-head" role="row"><span role="columnheader">Period</span><span role="columnheader">Input</span><span role="columnheader">Cached</span><span role="columnheader">Output</span><span role="columnheader">Relative ratio</span><span role="columnheader" aria-label="Actions"></span></div>
    </div>
    <div role="rowgroup">
      ${model.history.map(rate => `
        <div class="cost-rate-history-row" role="row">
          <span role="cell" data-label="Period">${rate.is_default ? 'Default' : esc(rate.effective_from)}</span>
          <span role="cell" data-label="Input">${price(rate.input_price)}</span>
          <span role="cell" data-label="Cached">${price(rate.cached_input_price)}</span>
          <span role="cell" data-label="Output">${price(rate.output_price)}</span>
          <span role="cell" data-label="Relative ratio" class="cost-rate-history-ratio">${esc(ratioText(rate))}</span>
          <span role="cell" class="cost-rate-history-actions">
            ${rate.source_url ? `<a class="text-button" href="${esc(rate.source_url)}" target="_blank" rel="noopener noreferrer">Source</a>` : ''}
            <button class="text-button" type="button" data-cost-rate-edit="${esc(periodKey(rate))}" ${mutationsBlocked ? 'disabled' : ''}>Edit</button>
            ${rate.override ? `<button class="text-button" type="button" data-cost-rate-reset="${esc(periodKey(rate))}" ${mutationsBlocked ? 'disabled' : ''}>Reset</button>` : ''}
            ${rate.deletable ? `<button class="text-button danger-text" type="button" data-cost-rate-delete="${esc(periodKey(rate))}" ${mutationsBlocked ? 'disabled' : ''}>Delete</button>` : ''}
          </span>
        </div>`).join('')}
    </div>
  </div>`;
}

function turnCountText(value) {
  const count = Number(value || 0);
  return `${count.toLocaleString()} ${count === 1 ? 'turn' : 'turns'}`;
}

function modelDetailMarkup(model, mutationsBlocked, editor) {
  return `
    ${model.model_id === 'unknown'
      ? '<p class="cost-rate-empty">The transcript did not identify a model, so BOLA cannot assign a price.</p>'
      : historyMarkup(model, mutationsBlocked)}
    ${model.model_id !== 'unknown'
      ? `<div class="cost-rate-detail-actions"><button class="secondary-button compact-button" type="button" data-cost-rate-add-period ${mutationsBlocked ? 'disabled' : ''}>Add price change</button></div>`
      : ''}
    ${editor && !editor.newModel && editor.modelId === model.model_id
      ? editorMarkup(editor, mutationsBlocked)
      : ''}`;
}

export function createCostRatesController({ refreshAnalytics, dialogManager, onModelSelected = () => {} }) {
  const list = document.getElementById('cost-rate-list');
  const modelCount = document.getElementById('cost-rate-model-count');
  const status = document.getElementById('cost-rate-status');
  const recalculateButton = document.getElementById('cost-rate-recalculate');
  const resetAllButton = document.getElementById('cost-rate-reset-all');
  const addModel = document.getElementById('cost-rate-add-model');
  const modelPopover = document.getElementById('cost-rate-model-popover');
  const modelPopoverBody = document.getElementById('cost-rate-model-popover-body');
  const modelPopoverClose = document.getElementById('cost-rate-model-popover-close');
  const resetConfirmButton = document.getElementById('cost-rate-reset-confirm');
  let payload = null;
  let selectedModel = '';
  let editor = null;
  let saving = false;
  let savingVisible = false;
  let savingTimer = 0;
  let recalculating = false;
  let serviceActivity = { running: false };
  let serviceActivityRefresh = () => Promise.resolve();
  let statusMessage = '';
  let statusKind = '';
  let active = false;
  let resetConfirmResolve = null;
  const modelDialog = dialogManager.register({
    rootId: 'cost-rate-model-dialog',
    initialFocus: () => modelPopover.querySelector('[name="model_id"]') || modelPopover,
    closeSelectors: ['#cost-rate-model-popover-close'],
    canClose: () => !saving,
    fallbackFocus: () => addModel,
    onClose: () => {
      if (!editor?.newModel) return;
      editor = null;
      render();
    },
  });
  const resetDialog = dialogManager.register({
    rootId: 'cost-rate-reset-dialog',
    initialFocus: () => document.getElementById('cost-rate-reset-cancel'),
    closeSelectors: ['#cost-rate-reset-cancel'],
    fallbackFocus: () => resetAllButton,
    onClose: ({ result }) => {
      if (!resetConfirmResolve) return;
      const resolve = resetConfirmResolve;
      resetConfirmResolve = null;
      resolve(result);
    },
  });

  function mutationsBlocked() {
    return saving || recalculating || Boolean(serviceActivity.running);
  }

  function controlsBlocked() {
    return savingVisible || recalculating || Boolean(serviceActivity.running);
  }

  function customChangeCount() {
    return Number(payload?.custom_change_count ?? payload?.custom_rate_count ?? 0);
  }

  function beginSaving(message) {
    saving = true;
    savingVisible = false;
    window.clearTimeout(savingTimer);
    savingTimer = window.setTimeout(() => {
      savingTimer = 0;
      if (!saving) return;
      savingVisible = true;
      setStatus(message);
      render();
    }, MUTATION_BUSY_DELAY_MS);
  }

  function finishSaving() {
    window.clearTimeout(savingTimer);
    savingTimer = 0;
    saving = false;
    savingVisible = false;
  }

  function renderStatus() {
    const recalculationAvailable = Boolean(payload?.rebuild_required);
    status.textContent = statusMessage;
    status.dataset.kind = statusMessage ? statusKind : '';
    recalculateButton.disabled = !recalculationAvailable || controlsBlocked();
  }

  function setStatus(message, kind = '') {
    statusMessage = message || '';
    statusKind = kind;
    renderStatus();
  }

  function syncNewModelPopover() {
    const open = Boolean(editor?.newModel);
    addModel.setAttribute('aria-expanded', String(open));
    if (!open) {
      modelPopoverBody.innerHTML = '';
      if (modelDialog.isOpen()) modelDialog.close({ force: true, restoreFocus: false });
      return;
    }
    modelPopoverBody.innerHTML = editorMarkup(editor, mutationsBlocked());
    modelPopoverClose.disabled = saving;
    if (!modelDialog.isOpen()) modelDialog.open({ trigger: addModel });
  }

  function applyMutationLocks() {
    const blocked = controlsBlocked();
    addModel.disabled = blocked;
    recalculateButton.disabled = !payload?.rebuild_required || blocked;
    resetAllButton.disabled = blocked || !customChangeCount();
    [list, modelPopoverBody].forEach(root => {
      root.querySelectorAll([
        '[data-cost-rate-add-period]',
        '[data-cost-rate-edit]',
        '[data-cost-rate-reset]',
        '[data-cost-rate-delete]',
        '[data-cost-rate-editor] button[type="submit"]',
      ].join(',')).forEach(button => { button.disabled = blocked; });
    });
  }

  function focusedSummaryModelId() {
    const focused = document.activeElement;
    const summary = focused instanceof Element ? focused.closest('[data-cost-rate-select]') : null;
    if (!summary || !list.contains(summary)) return '';
    return summary.closest('[data-cost-rate-model]')?.dataset.costRateModel || '';
  }

  function restoreSummaryFocus(modelId) {
    if (!modelId) return;
    const row = [...list.querySelectorAll('[data-cost-rate-model]')]
      .find(candidate => candidate.dataset.costRateModel === modelId);
    row?.querySelector('[data-cost-rate-select]')?.focus({ preventScroll: true });
  }

  function render() {
    if (!payload) return;
    const summaryFocusModelId = focusedSummaryModelId();
    const models = payload.models || [];
    const blocked = controlsBlocked();
    list.setAttribute('aria-busy', saving ? 'true' : 'false');
    addModel.disabled = blocked;
    resetAllButton.disabled = blocked || !customChangeCount();
    const modelCountText = `${models.length.toLocaleString()} models`;
    modelCount.textContent = modelCountText;
    if (!models.length) {
      list.innerHTML = '<div class="cost-rate-empty">No models found. Add a model to configure Cost Units.</div>';
      syncNewModelPopover();
      updateRatioPreview();
      applyMutationLocks();
      renderStatus();
      return;
    }
    if (selectedModel && !models.some(model => model.model_id === selectedModel)) selectedModel = '';
    const rows = models.map(model => {
      const expanded = active && selectedModel === model.model_id;
      const current = model.current;
      const currentPrices = current
        ? `${price(current.input_price)} · ${price(current.cached_input_price)} · ${price(current.output_price)}`
        : 'No applicable price';
      return `<section class="cost-rate-model" data-cost-rate-model="${esc(model.model_id)}">
        <button class="cost-rate-summary" type="button" aria-expanded="${expanded}" aria-controls="cost-rate-detail-${esc(model.model_id)}" data-cost-rate-select>
          <span class="cost-rate-model-name">${esc(model.model_id)}</span>
          <span class="cost-rate-current">${esc(currentPrices)}</span>
          <span class="cost-rate-turns">${turnCountText(model.turns)}</span>
          <span class="cost-rate-model-status" data-status="${esc(model.status)}">${esc(statusText(model))}</span>
          <span class="cost-rate-chevron" aria-hidden="true">›</span>
        </button>
        <div id="cost-rate-detail-${esc(model.model_id)}" class="cost-rate-detail" ${expanded ? '' : 'hidden'}>
          ${expanded ? modelDetailMarkup(model, blocked, editor) : ''}
        </div>
      </section>`;
    }).join('');
    list.innerHTML = rows;
    syncNewModelPopover();
    updateRatioPreview();
    applyMutationLocks();
    renderStatus();
    restoreSummaryFocus(summaryFocusModelId);
  }

  async function load({ announce = false } = {}) {
    list.setAttribute('aria-busy', 'true');
    if (announce) setStatus('Loading cost rates');
    try {
      payload = await getJSON('/api/cost-rates');
      setStatus('');
      render();
      return true;
    } catch (error) {
      list.innerHTML = '<div class="cost-rate-empty">Cost rates could not be loaded <button class="text-button" type="button" data-cost-rate-retry>Retry</button></div>';
      modelCount.textContent = '';
      setStatus(error.message || 'Cost rates could not be loaded', 'error');
      return false;
    } finally {
      list.setAttribute('aria-busy', 'false');
    }
  }

  function modelById(modelId) {
    return (payload?.models || []).find(model => model.model_id === modelId);
  }

  function rateByPeriod(model, key) {
    return (model.history || []).find(rate => periodKey(rate) === key);
  }

  function openEditor(modelId, rate = {}, newModel = false) {
    if (!newModel) selectedModel = modelId;
    editor = { modelId, rate: { ...rate, model_id: modelId }, newModel };
    render();
    requestAnimationFrame(() => document.querySelector('[data-cost-rate-editor] input:not([type="hidden"])')?.focus());
  }

  function updateRatioPreview() {
    const form = document.querySelector('[data-cost-rate-editor]');
    if (!form) return;
    const input = Number(form.elements.input_price.value);
    const cached = Number(form.elements.cached_input_price.value);
    const output = Number(form.elements.output_price.value);
    const target = form.querySelector('[data-cost-rate-ratio]');
    target.textContent = input > 0 && cached >= 0 && output >= 0
      ? `Input 1× · Cached ${(cached / input).toLocaleString(undefined, { maximumFractionDigits: 6 })}× · Output ${(output / input).toLocaleString(undefined, { maximumFractionDigits: 6 })}×`
      : 'Enter all three prices';
  }

  function formRate(form) {
    const isDefault = form.elements.is_default.value === 'true';
    return {
      model_id: String(form.elements.model_id.value || '').trim(),
      effective_from: isDefault ? null : form.elements.effective_from.value,
      is_default: isDefault,
      input_price: String(form.elements.input_price.value || '').trim(),
      cached_input_price: String(form.elements.cached_input_price.value || '').trim(),
      output_price: String(form.elements.output_price.value || '').trim(),
    };
  }

  function clearErrors(form) {
    form.querySelectorAll('[data-cost-error]').forEach(node => { node.textContent = ''; });
  }

  async function save(action, rate) {
    if (mutationsBlocked()) return;
    const savingNewModel = Boolean(editor?.newModel);
    if (action === 'upsert' && editor) editor.rate = { ...rate };
    beginSaving(action === 'upsert' ? 'Saving cost rate' : 'Updating cost rate');
    try {
      payload = await postJSON('/api/cost-rates', { action, expected_revision: payload.revision, rate });
      editor = null;
      if (savingNewModel) {
        selectedModel = rate.model_id;
        requestAnimationFrame(() => addModel.focus());
      }
      setStatus('');
    } catch (error) {
      if (error.code === 'cost_rates_revision_conflict') {
        setStatus('Cost rates changed elsewhere. Reload and try again', 'error');
      } else if (isServiceBusyError(error)) {
        const operation = error.operation === 'cleanup'
          ? 'Cleanup'
          : (error.operation === 'cost_recalculation' ? 'Cost recalculation' : 'Analyze');
        setStatus(`${operation} is running. Try again when it finishes`, 'error');
        serviceActivityRefresh();
      } else {
        setStatus(error.message || 'Cost rate update failed', 'error');
      }
      const form = document.querySelector('[data-cost-rate-editor]');
      const field = error.field && form?.querySelector(`[data-cost-error="${CSS.escape(error.field)}"]`);
      if (field) {
        field.textContent = error.message;
        form.elements[error.field]?.focus();
      }
    } finally {
      finishSaving();
      render();
    }
  }

  function confirmResetAll() {
    return new Promise(resolve => {
      resetConfirmResolve = resolve;
      resetDialog.open({ trigger: resetAllButton });
    });
  }

  async function resetAllCustomRates() {
    if (mutationsBlocked() || !customChangeCount()) return;
    if (!await confirmResetAll()) return;
    if (mutationsBlocked()) return;
    beginSaving('Resetting model prices');
    try {
      payload = await postJSON('/api/cost-rates', {
        action: 'reset_all',
        expected_revision: payload.revision,
        confirm_reset_all: true,
      });
      editor = null;
      setStatus('');
    } catch (error) {
      if (error.code === 'cost_rates_revision_conflict') {
        setStatus('Cost rates changed elsewhere. Reload and try again', 'error');
      } else if (isServiceBusyError(error)) {
        const operation = error.operation === 'cleanup'
          ? 'Cleanup'
          : (error.operation === 'cost_recalculation' ? 'Cost recalculation' : 'Analyze');
        setStatus(`${operation} is running. Try again when it finishes`, 'error');
        serviceActivityRefresh();
      } else {
        setStatus(error.message || 'Cost rate reset failed', 'error');
      }
    } finally {
      finishSaving();
      render();
    }
  }

  async function recalculate() {
    if (mutationsBlocked()) return;
    const expectedCatalogDigest = String(payload?.catalog_digest || '');
    if (!expectedCatalogDigest) {
      setStatus('Reload cost rates before recalculating', 'error');
      return;
    }
    recalculating = true;
    setStatus('Recalculating Cost Units');
    render();
    try {
      const result = await postJSON('/api/cost-rates/recalculate', {
        expected_catalog_digest: expectedCatalogDigest,
      });
      await refreshAnalytics();
      const loaded = await load();
      if (!loaded) return;
      if (payload.rebuild_required || payload.analytics_catalog_digest !== result.catalog_digest) {
        setStatus('Cost rates changed again. Recalculate once more', 'warning');
        return;
      }
      setStatus(`Cost Units recalculated for ${Number(result.recalculated_turns || 0).toLocaleString()} turns`, 'success');
    } catch (error) {
      if (isServiceBusyError(error)) {
        const operation = error.operation === 'cleanup'
          ? 'Cleanup'
          : (error.operation === 'cost_recalculation' ? 'Cost recalculation' : 'Analyze');
        setStatus(`${operation} is running. Try again when it finishes`, 'error');
      } else {
        setStatus(error.message || 'Cost Units recalculation failed', 'error');
      }
    } finally {
      recalculating = false;
      render();
      serviceActivityRefresh();
    }
  }

  function applyServiceActivity(activity) {
    serviceActivity = activity || { running: false };
    applyMutationLocks();
    renderStatus();
  }

  function setServiceActivityRefresh(refresh) {
    serviceActivityRefresh = typeof refresh === 'function' ? refresh : serviceActivityRefresh;
  }

  function closeNewModelPopover() {
    if (!editor?.newModel || saving) return;
    modelDialog.close({ reason: 'control' });
  }

  function handleEditorInput(event) {
    if (event.target.closest('[data-cost-rate-editor]')) updateRatioPreview();
  }

  function handleEditorSubmit(event) {
    const form = event.target.closest('[data-cost-rate-editor]');
    if (!form) return;
    event.preventDefault();
    clearErrors(form);
    save('upsert', formRate(form));
  }

  list.addEventListener('input', handleEditorInput);
  list.addEventListener('submit', handleEditorSubmit);
  modelPopoverBody.addEventListener('input', handleEditorInput);
  modelPopoverBody.addEventListener('submit', handleEditorSubmit);
  modelPopoverBody.addEventListener('click', event => {
    if (event.target.closest('[data-cost-rate-cancel]')) closeNewModelPopover();
  });
  list.addEventListener('click', event => {
    const row = event.target.closest('[data-cost-rate-model]');
    const modelId = row?.dataset.costRateModel || '';
    if (event.target.closest('[data-cost-rate-retry]')) { load({ announce: true }); return; }
    if (event.target.closest('[data-cost-rate-select]')) {
      selectedModel = selectedModel === modelId ? '' : modelId;
      editor = null;
      onModelSelected(modelId);
      render();
      return;
    }
    const model = modelById(modelId);
    if (event.target.closest('[data-cost-rate-cancel]')) { editor = null; render(); return; }
    if (!model) return;
    if (event.target.closest('[data-cost-rate-add-period]')) {
      if (mutationsBlocked()) return;
      const current = model.current || model.history[0] || {};
      openEditor(modelId, { ...current, effective_from: TODAY_UTC(), is_default: false });
      return;
    }
    const edit = event.target.closest('[data-cost-rate-edit]');
    if (edit) {
      if (mutationsBlocked()) return;
      openEditor(modelId, rateByPeriod(model, edit.dataset.costRateEdit));
      return;
    }
    const reset = event.target.closest('[data-cost-rate-reset]');
    if (reset) { save('reset', rateByPeriod(model, reset.dataset.costRateReset)); return; }
    const remove = event.target.closest('[data-cost-rate-delete]');
    if (remove) { save('delete', rateByPeriod(model, remove.dataset.costRateDelete)); }
  });
  recalculateButton.addEventListener('click', () => {
    onModelSelected(selectedModel);
    recalculate();
  });
  resetAllButton.addEventListener('click', resetAllCustomRates);
  resetConfirmButton.addEventListener('click', () => resetDialog.close({ result: true }));
  addModel.addEventListener('click', () => {
    if (mutationsBlocked()) return;
    onModelSelected(selectedModel);
    if (editor?.newModel) {
      closeNewModelPopover();
      return;
    }
    openEditor('', { effective_from: TODAY_UTC(), is_default: false }, true);
  });

  function setActive(nextActive) {
    active = Boolean(nextActive);
    render();
  }

  load();
  return { applyServiceActivity, load, setActive, setServiceActivityRefresh };
}
