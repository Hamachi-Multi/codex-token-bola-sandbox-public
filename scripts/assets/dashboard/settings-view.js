const SETTING_LABELS = {
  general: 'General',
  'cost-rates': 'Cost Units',
};

export function createSettingsView({ onSelectionChange = () => {} } = {}) {
  const list = document.getElementById('settings-list');
  const detail = document.querySelector('.settings-detail');
  const status = document.getElementById('settings-detail-status');
  let selected = 'general';

  function select(key, { focusDetail = false } = {}) {
    if (!SETTING_LABELS[key]) return;
    selected = key;
    list.dataset.selectedSetting = key;
    list.querySelectorAll('[data-settings-select]').forEach(button => {
      const active = button.dataset.settingsSelect === key;
      button.classList.toggle('selected', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    document.querySelectorAll('[data-settings-detail]').forEach(section => {
      section.hidden = section.dataset.settingsDetail !== key;
    });
    status.textContent = SETTING_LABELS[key];
    detail.scrollTop = 0;
    onSelectionChange(key);
    if (focusDetail) {
      const candidates = document.querySelectorAll(`[data-settings-detail="${CSS.escape(key)}"] :is(button, select, input, a)`);
      [...candidates].find(element => !element.disabled && element.offsetParent !== null)?.focus({ preventScroll: true });
    }
  }

  function selectableItems() {
    return [...list.querySelectorAll('.settings-list-item')];
  }

  list.addEventListener('click', event => {
    const button = event.target.closest('[data-settings-select]');
    if (button) select(button.dataset.settingsSelect);
  });
  list.addEventListener('keydown', event => {
    const current = event.target.closest('.settings-list-item');
    if (!current || !['ArrowDown', 'ArrowUp', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    if (event.key === 'ArrowRight') {
      select(current.dataset.settingsSelect || 'cost-rates', { focusDetail: true });
      return;
    }
    const items = selectableItems();
    const index = items.indexOf(current);
    const next = items[index + (event.key === 'ArrowDown' ? 1 : -1)];
    next?.focus({ preventScroll: true });
  });

  select(selected);
  return { select };
}
