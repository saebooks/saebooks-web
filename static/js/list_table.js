/**
 * list_table.js — kebab popovers + cross-page bulk-select for SAE Books list pages.
 *
 * Design decisions (locked 2026-05-23 with Richard):
 *
 *  1. Selection persistence: XERO-STYLE preserve across pagination/filter.
 *     - Selected ids live in sessionStorage under key `bulk:<entity>:ids`.
 *     - Pagination, filter, sort changes do not clear the set.
 *     - The user clears via the "Clear" button in the bulk bar.
 *     - On mount, every row checkbox checks itself if its id is in the
 *       persisted set, so the selection visibly survives navigation.
 *
 *  2. Destructive confirmations: modal always; typed-confirm when count > 10.
 *     - Plain confirm: "Are you sure you want to <Action> N rows?" + buttons.
 *     - Typed confirm: same modal + an input that must equal the action
 *       label uppercase (e.g. "DELETE", "VOID") before the Confirm button
 *       enables.
 *
 * Globals (intentional; one app, one namespace):
 *   window.__listTable.toggleKebab(button)
 *   window.__listTable.toggleSelection(checkbox)
 *   window.__listTable.toggleSelectAll(checkbox, scope)
 *   window.__listTable.clearSelection(entity)
 *   window.__listTable.runBulkAction(button)
 *   window.__listTable.closeConfirm()
 *
 * No external dependencies. Vanilla JS, IE11 not supported (template
 * literals + arrow functions + Map are used freely).
 */
(function () {
  'use strict';

  const STORAGE_PREFIX = 'bulk:';

  // ── Selection set (per entity) ───────────────────────────────────────
  function setForEntity(entity) {
    const key = STORAGE_PREFIX + entity + ':ids';
    let raw;
    try { raw = window.sessionStorage.getItem(key); } catch (e) { raw = null; }
    return {
      key,
      get: () => new Set(raw ? raw.split(',').filter(Boolean) : []),
      save: (set) => {
        try {
          if (set.size === 0) window.sessionStorage.removeItem(key);
          else window.sessionStorage.setItem(key, Array.from(set).join(','));
        } catch (e) { /* sessionStorage quota or disabled */ }
      },
    };
  }

  function entityFromBar() {
    const bar = document.querySelector('[data-bulk-bar]');
    return bar ? bar.getAttribute('data-entity') : null;
  }

  function refreshBar() {
    const bar = document.querySelector('[data-bulk-bar]');
    if (!bar) return;
    const entity = bar.getAttribute('data-entity');
    const store = setForEntity(entity);
    const set = store.get();
    bar.classList.toggle('hidden', set.size === 0);
    const countEl = bar.querySelector('[data-bulk-count]');
    if (countEl) countEl.textContent = String(set.size);
  }

  function setRowChecked(checkbox, checked) {
    checkbox.checked = checked;
    const tr = checkbox.closest('tr');
    if (tr) tr.classList.toggle('bg-blue-50', checked);
  }

  function syncHeaderSelectAll() {
    const header = document.querySelector('[data-select-all]');
    if (!header) return;
    const rowBoxes = Array.from(document.querySelectorAll('[data-row-select]'));
    if (rowBoxes.length === 0) {
      header.checked = false;
      header.indeterminate = false;
      return;
    }
    const checkedCount = rowBoxes.filter((b) => b.checked).length;
    header.checked = checkedCount === rowBoxes.length;
    header.indeterminate = checkedCount > 0 && checkedCount < rowBoxes.length;
  }

  // ── Kebab popover ────────────────────────────────────────────────────
  let openKebab = null;

  function closeOpenKebab() {
    if (!openKebab) return;
    const trigger = openKebab.querySelector('.kebab-trigger');
    const popover = openKebab.querySelector('.kebab-popover');
    if (trigger) trigger.setAttribute('aria-expanded', 'false');
    if (popover) popover.classList.add('hidden');
    openKebab = null;
  }

  function toggleKebab(button) {
    const wrap = button.closest('.kebab');
    if (!wrap) return;
    const popover = wrap.querySelector('.kebab-popover');
    if (!popover) return;
    if (openKebab === wrap) {
      closeOpenKebab();
      return;
    }
    closeOpenKebab();
    popover.classList.remove('hidden');
    button.setAttribute('aria-expanded', 'true');
    openKebab = wrap;
  }

  // ── Row selection ────────────────────────────────────────────────────
  function toggleSelection(checkbox) {
    const id = checkbox.getAttribute('data-id');
    if (!id) return;
    const entity = entityFromBar();
    if (!entity) return;
    const store = setForEntity(entity);
    const set = store.get();
    if (checkbox.checked) set.add(id);
    else set.delete(id);
    store.save(set);
    setRowChecked(checkbox, checkbox.checked);
    syncHeaderSelectAll();
    refreshBar();
  }

  function toggleSelectAll(headerBox) {
    const entity = entityFromBar();
    if (!entity) return;
    const store = setForEntity(entity);
    const set = store.get();
    const rowBoxes = Array.from(document.querySelectorAll('[data-row-select]'));
    const target = headerBox.checked;
    rowBoxes.forEach((b) => {
      const id = b.getAttribute('data-id');
      if (!id) return;
      if (target) set.add(id);
      else set.delete(id);
      setRowChecked(b, target);
    });
    store.save(set);
    refreshBar();
  }

  function clearSelection(entity) {
    const store = setForEntity(entity);
    store.save(new Set());
    document.querySelectorAll('[data-row-select]').forEach((b) => setRowChecked(b, false));
    syncHeaderSelectAll();
    refreshBar();
  }

  // ── Bulk actions ─────────────────────────────────────────────────────
  function runBulkAction(button) {
    const action = button.getAttribute('data-bulk-action');
    const label = button.getAttribute('data-action-label') || action;
    const destructive = button.getAttribute('data-destructive') === 'true';
    const entity = entityFromBar();
    if (!entity) return;
    const set = setForEntity(entity).get();
    if (set.size === 0) {
      window.alert('No rows selected.');
      return;
    }
    const submit = () => submitBulk(action);
    if (destructive) {
      openConfirm({ action, label, count: set.size, onConfirm: submit });
    } else {
      // Non-destructive: simple confirm.
      openConfirm({ action, label, count: set.size, onConfirm: submit, plain: true });
    }
  }

  function submitBulk(action) {
    const bar = document.querySelector('[data-bulk-bar]');
    if (!bar) return;
    const entity = bar.getAttribute('data-entity');
    const form = bar.querySelector('[data-bulk-form]');
    if (!form) return;
    const set = setForEntity(entity).get();
    // Reset prior ids[] hidden inputs.
    form.querySelectorAll('input[name="ids[]"]').forEach((el) => el.remove());
    set.forEach((id) => {
      const inp = document.createElement('input');
      inp.type = 'hidden';
      inp.name = 'ids[]';
      inp.value = id;
      form.appendChild(inp);
    });
    const actionInput = form.querySelector('[data-bulk-action-input]');
    if (actionInput) actionInput.value = action;
    // Clear selection on submit — the server response will redirect with a flash.
    setForEntity(entity).save(new Set());
    form.submit();
  }

  // ── Confirmation modal ───────────────────────────────────────────────
  let confirmState = null;

  function openConfirm({ action, label, count, onConfirm, plain }) {
    const modal = document.getElementById('bulk-confirm-modal');
    if (!modal) { if (window.confirm(label + ' ' + count + ' rows?')) onConfirm(); return; }
    const title = modal.querySelector('[data-confirm-title]');
    const body = modal.querySelector('[data-confirm-body]');
    const typedWrap = modal.querySelector('[data-confirm-typed]');
    const typedWord = modal.querySelector('[data-typed-word]');
    const typedInput = modal.querySelector('[data-confirm-typed-input]');
    const goBtn = modal.querySelector('[data-confirm-go]');
    const requireTyped = !plain && count > 10;

    title.textContent = label + ' ' + count + ' row' + (count === 1 ? '' : 's') + '?';
    body.textContent = plain
      ? 'This will ' + label.toLowerCase() + ' ' + count + ' row' + (count === 1 ? '' : 's') + '.'
      : 'This will ' + label.toLowerCase() + ' ' + count + ' row' + (count === 1 ? '' : 's') +
        '. This cannot be undone in bulk.';

    if (requireTyped) {
      const word = label.toUpperCase().split(' ')[0]; // "Mark Paid" → "MARK"; "Delete" → "DELETE"
      typedWord.textContent = word;
      typedWrap.classList.remove('hidden');
      typedInput.value = '';
      goBtn.disabled = true;
      typedInput.oninput = function () {
        goBtn.disabled = typedInput.value.toUpperCase().trim() !== word;
      };
    } else {
      typedWrap.classList.add('hidden');
      goBtn.disabled = false;
    }

    // Tint Confirm button red for destructive, blue for plain.
    if (plain) goBtn.classList.replace('bg-red-600', 'bg-[#194291]');
    else goBtn.classList.replace('bg-[#194291]', 'bg-red-600');

    confirmState = { onConfirm };
    goBtn.onclick = function () {
      if (goBtn.disabled) return;
      closeConfirm();
      confirmState && confirmState.onConfirm();
    };
    modal.classList.remove('hidden');
    if (!requireTyped) goBtn.focus();
    else typedInput.focus();
  }

  function closeConfirm() {
    const modal = document.getElementById('bulk-confirm-modal');
    if (!modal) return;
    modal.classList.add('hidden');
    confirmState = null;
  }

  // ── Global listeners ─────────────────────────────────────────────────
  document.addEventListener('click', function (e) {
    if (openKebab && !openKebab.contains(e.target)) closeOpenKebab();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      closeOpenKebab();
      closeConfirm();
    }
  });

  // ── Init on DOMContentLoaded ─────────────────────────────────────────
  function init() {
    // Restore visible-selected state from persisted set.
    const entity = entityFromBar();
    if (entity) {
      const set = setForEntity(entity).get();
      document.querySelectorAll('[data-row-select]').forEach((b) => {
        const id = b.getAttribute('data-id');
        if (id && set.has(id)) setRowChecked(b, true);
      });
      syncHeaderSelectAll();
      refreshBar();
    }
    // Auto-submit destructive POST confirmations from kebabs (data-confirm).
    document.querySelectorAll('form[data-confirm]').forEach((form) => {
      form.addEventListener('submit', function (e) {
        const msg = form.getAttribute('data-confirm');
        if (!window.confirm(msg)) e.preventDefault();
      });
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // ── Expose globals ───────────────────────────────────────────────────
  window.__listTable = {
    toggleKebab,
    toggleSelection,
    toggleSelectAll,
    clearSelection,
    runBulkAction,
    closeConfirm,
  };
})();
