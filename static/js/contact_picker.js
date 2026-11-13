/**
 * contact_picker.js — type-ahead contact filter for SAE Books list screens.
 *
 * Markup (rendered by templates/_components/list_tools.html :: contact_picker):
 *   <div data-contact-picker data-role="supplier" data-autosubmit>
 *     <input type="hidden" name="contact_id" data-cp-value>
 *     <input type="search" data-cp-text role="combobox" ...>
 *     <ul data-cp-list role="listbox" hidden></ul>
 *   </div>
 *
 * On a form (no data-autosubmit) the picker just fills the hidden input.
 * Typing asks /contacts/_lookup?q=…&role=… (a JSON list of {id, name, type})
 * and shows the matches. Picking one stores its id in the hidden input and,
 * with data-autosubmit, submits the filter form. Clearing the box clears the
 * filter. Keyboard: Up / Down to move, Enter to pick, Escape to close.
 *
 * Options reuse the command palette's .cmdk-item look, which already follows
 * the light and dark themes through CSS variables.
 */
(function () {
  if (window.__contactPicker) return;
  window.__contactPicker = true;

  var DEBOUNCE_MS = 200;

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function typeWords(t) {
    return {
      CUSTOMER: 'Customer', SUPPLIER: 'Supplier', CONTRACTOR: 'Contractor',
      SUB_CONTRACTOR: 'Sub-contractor', BOTH: 'Customer and supplier',
      BENEFICIARY: 'Beneficiary'
    }[t] || '';
  }

  function mount(root) {
    if (root.__cpMounted) return;
    root.__cpMounted = true;
    var hidden = root.querySelector('[data-cp-value]');
    var text = root.querySelector('[data-cp-text]');
    var list = root.querySelector('[data-cp-list]');
    var autosubmit = root.hasAttribute('data-autosubmit');
    var timer = null;
    var items = [];
    var active = -1;
    var lastQuery = null;
    var pickedName = text.value;

    function close() {
      list.hidden = true;
      text.setAttribute('aria-expanded', 'false');
      text.removeAttribute('aria-activedescendant');
      active = -1;
    }

    function submitForm() {
      if (!autosubmit) return;
      var form = root.closest('form');
      if (form) {
        var offset = form.querySelector('input[name="offset"]');
        if (offset) offset.value = '0';
        if (form.requestSubmit) form.requestSubmit(); else form.submit();
      }
    }

    function pick(i) {
      var c = items[i];
      if (!c) return;
      hidden.value = c.id;
      text.value = c.name;
      pickedName = c.name;
      close();
      submitForm();
    }

    function highlight(i) {
      var opts = list.querySelectorAll('[role="option"]');
      opts.forEach(function (el, idx) {
        var on = idx === i;
        el.classList.toggle('cmdk-selected', on);
        el.setAttribute('aria-selected', on ? 'true' : 'false');
        if (on) {
          text.setAttribute('aria-activedescendant', el.id);
          el.scrollIntoView({ block: 'nearest' });
        }
      });
      active = i;
    }

    function render(q) {
      if (!items.length) {
        list.innerHTML = '<li class="cmdk-sub" style="padding:9px 14px;">' +
          escapeHtml(q ? 'No contacts match "' + q + '".' : 'Start typing a name.') + '</li>';
      } else {
        list.innerHTML = items.map(function (c, idx) {
          return '<li id="' + text.id + '-opt-' + idx + '" role="option" aria-selected="false"' +
            ' class="cmdk-item" data-idx="' + idx + '" style="padding:7px 14px;">' +
            '<div><div>' + escapeHtml(c.name) + '</div>' +
            '<div class="cmdk-sub">' + escapeHtml(typeWords(c.type)) + '</div></div></li>';
        }).join('');
        list.querySelectorAll('[role="option"]').forEach(function (el) {
          el.addEventListener('mousedown', function (ev) {
            ev.preventDefault(); // keep focus; pick before blur closes the list
            pick(parseInt(el.getAttribute('data-idx'), 10));
          });
        });
      }
      list.hidden = false;
      text.setAttribute('aria-expanded', 'true');
      active = -1;
    }

    function lookup() {
      var q = text.value.trim();
      if (q === lastQuery && !list.hidden) return;
      lastQuery = q;
      // Read the role on every lookup so a form can switch it (e.g. a
      // payment's money in / money out) by changing data-role.
      var role = root.getAttribute('data-role') || '';
      var url = '/contacts/_lookup?q=' + encodeURIComponent(q) +
        (role ? '&role=' + encodeURIComponent(role) : '');
      fetch(url, { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : { items: [] }; })
        .then(function (data) {
          if (text.value.trim() !== q) return; // a newer keystroke won
          items = (data && data.items) || [];
          render(q);
        })
        .catch(function () { items = []; render(q); });
    }

    text.addEventListener('input', function () {
      if (text.value.trim() === '') {
        // Clearing the box clears the filter.
        var had = hidden.value;
        hidden.value = '';
        pickedName = '';
        close();
        if (had) submitForm();
        return;
      }
      if (text.value !== pickedName) hidden.value = '';
      clearTimeout(timer);
      timer = setTimeout(lookup, DEBOUNCE_MS);
    });

    text.addEventListener('focus', function () {
      if (!hidden.value) { lastQuery = null; lookup(); }
    });

    text.addEventListener('keydown', function (ev) {
      if (ev.key === 'ArrowDown') {
        ev.preventDefault();
        if (list.hidden) { lastQuery = null; lookup(); return; }
        highlight(Math.min(active + 1, items.length - 1));
      } else if (ev.key === 'ArrowUp') {
        ev.preventDefault();
        highlight(Math.max(active - 1, 0));
      } else if (ev.key === 'Enter') {
        if (!list.hidden && active >= 0) { ev.preventDefault(); pick(active); }
        else if (!list.hidden && items.length === 1) { ev.preventDefault(); pick(0); }
      } else if (ev.key === 'Escape') {
        close();
      }
    });

    text.addEventListener('blur', function () {
      setTimeout(function () {
        close();
        // Typed text that was never picked is not a filter: put the
        // current pick's name back so the box shows what is applied.
        if (!hidden.value && text.value.trim() !== '') text.value = pickedName;
      }, 150);
    });
  }

  function mountAll(scope) {
    (scope || document).querySelectorAll('[data-contact-picker]').forEach(mount);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { mountAll(); });
  } else {
    mountAll();
  }
  document.addEventListener('htmx:afterSwap', function (ev) { mountAll(ev.target); });
})();
