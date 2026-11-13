/* Foreign currency fields (batch 10).
 *
 * Wires every [data-fx-root] block rendered by _currency_macros.html:
 *  - shows the rate row only for a currency other than the company's;
 *  - looks the rate up (/fx/lookup) for the document date and fills it,
 *    with its source ("Reserve Bank of Australia, 4 Mar 2031" or
 *    "Your rate for ..."); a rate the user types is never overwritten;
 *  - shows the company-currency value live;
 *  - picks the customer's or supplier's billing currency when the contact
 *    changes (new documents only);
 *  - on payments, works out the rate from the amount that actually left
 *    or reached the bank.
 */
(function () {
  "use strict";

  function num(text) {
    if (text === null || text === undefined) return null;
    var v = parseFloat(String(text).replace(/[^0-9.\-]/g, ""));
    return isNaN(v) ? null : v;
  }

  function money(n) {
    return n.toLocaleString("en-AU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function linesTotal(form) {
    var rows = {};
    form.querySelectorAll("input[name^='lines[']").forEach(function (el) {
      var m = el.name.match(/^lines\[(\d+)\]\[(quantity|unit_price|discount_pct)\]$/);
      if (!m) return;
      rows[m[1]] = rows[m[1]] || {};
      rows[m[1]][m[2]] = num(el.value);
    });
    var total = 0;
    var any = false;
    Object.keys(rows).forEach(function (k) {
      var r = rows[k];
      if (r.quantity === null || r.unit_price === null || r.quantity === undefined || r.unit_price === undefined) return;
      var disc = r.discount_pct ? r.discount_pct : 0;
      total += r.quantity * r.unit_price * (1 - disc / 100);
      any = true;
    });
    return any ? total : null;
  }

  function init(root) {
    var form = root.closest("form") || document;
    var base = root.dataset.base || "AUD";
    var sel = root.querySelector("[data-fx-currency]");
    var rate = root.querySelector("[data-fx-rate]");
    var src = root.querySelector("[data-fx-source]");
    var baseOut = root.querySelector("[data-fx-base-total]");
    var bank = root.querySelector("[data-fx-bank-amount]");
    var dateInput = root.dataset.dateInput ? document.getElementById(root.dataset.dateInput) : null;
    var totalSource = root.dataset.totalSource || "";
    var contactSel = root.dataset.contactSelect ? document.getElementById(root.dataset.contactSelect) : null;
    var contactCcy = {};
    try { contactCcy = JSON.parse(root.dataset.contactCurrencies || "{}"); } catch (e) { contactCcy = {}; }
    var currencyChosenByUser = root.dataset.locked === "1";
    var auto = !(rate && rate.value);

    function foreign() { return sel && sel.value && sel.value.toUpperCase() !== base.toUpperCase(); }

    function docTotal() {
      if (totalSource === "lines") return linesTotal(form);
      if (!totalSource) return null;
      var el = document.querySelector(totalSource);
      if (!el) return null;
      return num(el.tagName === "INPUT" ? el.value : (el.dataset.value || el.textContent));
    }

    function say(text) { if (src) src.textContent = text || ""; }

    function updateBase() {
      var t = docTotal();
      var r = num(rate && rate.value);
      if (bank && foreign()) {
        var b = num(bank.value);
        if (b !== null && t) {
          // Payment: the bank amount sets the rate.
          var implied = b / t;
          if (rate) { rate.value = implied.toFixed(8); auto = false; }
          say("Worked out from the bank amount: 1 " + sel.value + " = " + implied.toFixed(6) + " " + base);
          r = implied;
        } else if (r !== null && t) {
          bank.placeholder = money(Math.round(t * r * 100) / 100);
        }
      }
      if (!baseOut) return;
      if (!foreign() || t === null || r === null) { baseOut.textContent = ""; return; }
      var label = totalSource === "lines" ? " before GST" : "";
      baseOut.textContent = sel.value + " " + money(t) + " = " + base + " " + money(Math.round(t * r * 100) / 100) + label;
    }

    function sync() {
      var show = foreign();
      root.querySelectorAll("[data-fx-foreign-only]").forEach(function (el) {
        el.style.display = show ? "" : "none";
      });
      root.querySelectorAll("[data-fx-ccy-label]").forEach(function (el) {
        el.textContent = sel ? sel.value : "";
      });
      if (!show) say("");
      updateBase();
    }

    function lookup() {
      if (!foreign() || !rate) return;
      if (!auto && rate.value) return;
      if (bank && num(bank.value) !== null) return;
      var d = dateInput && dateInput.value ? dateInput.value : "";
      say("Looking up the rate…");
      fetch("/fx/lookup?currency=" + encodeURIComponent(sel.value) + (d ? "&on=" + encodeURIComponent(d) : ""), {
        credentials: "same-origin",
        headers: { "Accept": "application/json" }
      })
        .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, b: b }; }); })
        .then(function (res) {
          if (res.ok && res.b.rate) {
            rate.value = res.b.rate;
            auto = true;
            say(res.b.source_label || "");
          } else {
            if (auto) rate.value = "";
            say(res.b.error || "No rate found for this date. Enter the rate.");
          }
          updateBase();
        })
        .catch(function () { say("Could not look up the rate. Enter it yourself."); });
    }

    if (sel) sel.addEventListener("change", function () {
      currencyChosenByUser = true;
      if (rate && auto) rate.value = "";
      sync();
      lookup();
    });
    if (rate) rate.addEventListener("input", function () {
      auto = false;
      say(rate.value ? "Rate entered by you" : "");
      updateBase();
    });
    if (bank) bank.addEventListener("input", function () {
      if (num(bank.value) === null) { auto = true; lookup(); }
      updateBase();
    });
    if (dateInput) dateInput.addEventListener("change", function () { if (auto) lookup(); });
    if (contactSel && sel) contactSel.addEventListener("change", function () {
      var c = contactCcy[contactSel.value];
      if (!c || currencyChosenByUser) return;
      var has = Array.prototype.some.call(sel.options || [], function (o) { return o.value === c; });
      if (!has) return;
      sel.value = c;
      if (rate) { rate.value = ""; auto = true; }
      sync();
      lookup();
    });
    form.addEventListener("input", updateBase);
    form.addEventListener("change", updateBase);
    // Line rows added by HTMX don't always fire input events on the form.
    window.setInterval(updateBase, 1500);

    sync();
    if (foreign() && rate && !rate.value) lookup();
  }

  function boot() { document.querySelectorAll("[data-fx-root]").forEach(init); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
