/* Phone helpers (b15). Loaded on every page from base.html.
 *
 * 1. Stacked list tables (gap P1-77): every list table with four or more
 *    columns gets .stack-on-phone and a data-label on each cell copied from
 *    its column heading, so static/css/phone.css can show each row as a
 *    card on a narrow screen. Tables marked data-no-stack, line-item entry
 *    tables and tables without a <thead> are left alone. Runs again after
 *    every HTMX swap so filtered and paged lists stay stacked.
 *
 * 2. Date display preference (gap P2-9): when the user picked a date style
 *    in Preferences (html[data-date-format]), cells and values whose whole
 *    text is an ISO date (2026-11-13) are shown in that style. The ISO date
 *    stays in the title so it is never lost, and form fields are untouched.
 */
(function () {
  'use strict';

  function headingText(th) {
    return (th.getAttribute('data-label') || th.textContent || '')
      .replace(/[↑↓]/g, '').replace(/\s+/g, ' ').trim();
  }

  function stackTables(root) {
    var tables = (root || document).querySelectorAll('table');
    tables.forEach(function (table) {
      if (table.hasAttribute('data-no-stack') || table.classList.contains('line-items-table')) return;
      if (table.dataset.stackDone === '1') return;
      var head = table.tHead;
      if (!head || !head.rows.length) return;
      var labels = [];
      Array.prototype.forEach.call(head.rows[head.rows.length - 1].cells, function (th) {
        var span = th.colSpan || 1;
        for (var i = 0; i < span; i++) labels.push(headingText(th));
      });
      if (labels.length < 4) return; // short tables already fit a phone
      if (table.querySelector('tbody input:not([type=checkbox]):not([type=hidden]), tbody select, tbody textarea')) return;
      Array.prototype.forEach.call(table.tBodies, function (tbody) {
        Array.prototype.forEach.call(tbody.rows, function (tr) {
          var col = 0;
          Array.prototype.forEach.call(tr.cells, function (td) {
            if (!td.hasAttribute('data-label') && labels[col]) td.setAttribute('data-label', labels[col]);
            col += td.colSpan || 1;
          });
        });
      });
      table.classList.add('stack-on-phone');
      table.dataset.stackDone = '1';
    });
  }

  var ISO = /^\s*(\d{4})-(\d{2})-(\d{2})\s*$/;
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  function formatDate(y, m, d, style) {
    if (style === 'dmy') return d + '/' + m + '/' + y;
    if (style === 'long') return (+d) + ' ' + MONTHS[(+m) - 1] + ' ' + y;
    return y + '-' + m + '-' + d;
  }

  function applyDateFormat(root) {
    var style = document.documentElement.getAttribute('data-date-format');
    if (!style || style === 'iso') return;
    var nodes = (root || document).querySelectorAll('td, dd, time, .num, span');
    nodes.forEach(function (el) {
      if (el.children.length || el.dataset.isoDate) return;
      if (el.closest('form, script, style, code, pre, textarea, select')) return;
      var m = ISO.exec(el.textContent);
      if (!m) return;
      el.dataset.isoDate = m[1] + '-' + m[2] + '-' + m[3];
      if (!el.title) el.title = el.dataset.isoDate;
      el.textContent = formatDate(m[1], m[2], m[3], style);
    });
  }

  function run(root) {
    try { stackTables(root); } catch (e) { /* never break the page */ }
    try { applyDateFormat(root); } catch (e) { /* never break the page */ }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { run(document); });
  } else {
    run(document);
  }
  document.addEventListener('htmx:afterSwap', function (ev) { run(ev.target || document); });

  window.sbPhone = { stackTables: stackTables, applyDateFormat: applyDateFormat, formatDate: formatDate };
})();

/* Account default tax code (b15, gap P2-2). Choosing an account on a line
 * fills that line's tax code from the account's default (Chart of accounts
 * "default tax code") — unless the user has already picked one by hand. */
(function () {
  'use strict';
  document.addEventListener('change', function (ev) {
    var el = ev.target;
    if (!el || el.tagName !== 'SELECT') return;
    if (el.hasAttribute('data-tax-code-select')) {
      el.dataset.auto = '0';
      return;
    }
    if (!el.hasAttribute('data-sets-tax-code')) return;
    var row = el.closest('tr') || el.closest('[data-line]') || el.parentElement;
    var tax = row && row.querySelector('select[data-tax-code-select]');
    if (!tax) return;
    if (tax.value && tax.dataset.auto !== '1') return; // hand-picked: leave it
    var opt = el.options[el.selectedIndex];
    var code = opt ? (opt.getAttribute('data-tax-code') || '') : '';
    if (!code) return;
    for (var i = 0; i < tax.options.length; i++) {
      if (tax.options[i].getAttribute('data-code') === code) {
        tax.selectedIndex = i;
        tax.dataset.auto = '1';
        break;
      }
    }
  });
})();
