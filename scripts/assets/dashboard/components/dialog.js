function setPageInert(inert) {
  document.querySelector('header')?.toggleAttribute('inert', inert);
  document.querySelector('main')?.toggleAttribute('inert', inert);
}

function focusableElements(root) {
  return [...root.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
    .filter(element => !element.disabled && element.offsetParent !== null);
}

function trapFocus(event, root) {
  const focusable = focusableElements(root);
  if (!focusable.length) {
    event.preventDefault();
    (root.querySelector('[role="status"]') || root.querySelector('[role="dialog"]'))?.focus?.();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function canReceiveFocus(element) {
  return element instanceof HTMLElement
    && element.isConnected
    && element.offsetParent !== null
    && !element.disabled;
}

export function createDialogManager() {
  const dialogs = new Map();
  const stack = [];

  function activeDialog() {
    return stack[stack.length - 1] || null;
  }

  function syncActiveDialog() {
    const active = activeDialog();
    setPageInert(Boolean(active));
    dialogs.forEach(dialog => {
      const isActive = dialog === active;
      dialog.root.toggleAttribute('inert', Boolean(active) && !isActive);
      dialog.root.setAttribute('aria-hidden', isActive ? 'false' : 'true');
    });
  }

  function register({
    rootId,
    initialFocus,
    closeSelectors = [],
    canClose = () => true,
    fallbackFocus = () => null,
    onClose = () => {},
  }) {
    const root = document.getElementById(rootId);
    if (!root) throw new Error(`Dialog root not found: ${rootId}`);
    const dialog = {
      root,
      trigger: null,
      isOpen: () => stack.includes(dialog),
      open({ trigger = document.activeElement } = {}) {
        const existing = stack.indexOf(dialog);
        if (existing >= 0) stack.splice(existing, 1);
        dialog.trigger = trigger instanceof HTMLElement ? trigger : null;
        stack.push(dialog);
        root.classList.add('open');
        syncActiveDialog();
        const target = typeof initialFocus === 'function' ? initialFocus() : initialFocus;
        target?.focus?.();
      },
      close({ reason = 'programmatic', result = false, force = false, restoreFocus = true } = {}) {
        const index = stack.indexOf(dialog);
        if (index < 0 || (!force && !canClose(reason))) return false;
        const wasActive = index === stack.length - 1;
        stack.splice(index, 1);
        root.classList.remove('open');
        root.setAttribute('aria-hidden', 'true');
        const trigger = dialog.trigger;
        dialog.trigger = null;
        syncActiveDialog();
        onClose({ reason, result });
        if (restoreFocus && wasActive) {
          const target = canReceiveFocus(trigger) ? trigger : fallbackFocus();
          if (canReceiveFocus(target)) target.focus();
          else activeDialog()?.root.querySelector('[role="dialog"]')?.focus?.();
        }
        return true;
      },
      requestClose(reason) {
        return dialog.close({ reason });
      },
    };
    dialogs.set(rootId, dialog);
    root.addEventListener('click', event => {
      if (event.target === root) dialog.requestClose('backdrop');
      if (closeSelectors.some(selector => event.target.closest(selector))) dialog.requestClose('control');
    });
    syncActiveDialog();
    return dialog;
  }

  window.addEventListener('keydown', event => {
    const active = activeDialog();
    if (!active) return;
    if (event.key === 'Escape') {
      if (active.requestClose('escape')) event.preventDefault();
    } else if (event.key === 'Tab') {
      trapFocus(event, active.root);
    }
  });

  return { register };
}
