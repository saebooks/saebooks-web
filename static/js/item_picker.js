/*
 * item_picker.js — product / service type-ahead on document line rows
 * (batch 12, P0-42).
 *
 * Each line row carries:
 *   <input class="item-picker" data-side="sale|purchase" list="sb-item-options">
 *   <input type="hidden" class="item-picker-id" name="lines[N][item_id]">
 *
 * The item list is fetched once per page from /items/_options and put in
 * a shared <datalist>. Picking an item fills the row's description, unit
 * price, account and GST code from the item (sale side: sale price,
 * income account, sale GST code; purchase side: purchase price, stock or
 * purchase account, purchase GST code). Clearing the box unlinks the item.
 * Works for rows added later by HTMX (event delegation).
 */
(function () {
  if (window.__sbItemPicker) return;
  window.__sbItemPicker = true;

  var items = null;
  var byLabel = {};
  var byId = {};

  function label(it) {
    return it.sku + " — " + it.name;
  }

  function ensureDatalist() {
    var dl = document.getElementById("sb-item-options");
    if (!dl) {
      dl = document.createElement("datalist");
      dl.id = "sb-item-options";
      document.body.appendChild(dl);
    }
    return dl;
  }

  function load() {
    if (items !== null) return Promise.resolve(items);
    return fetch("/items/_options", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : { items: [] }; })
      .catch(function () { return { items: [] }; })
      .then(function (data) {
        items = data.items || [];
        var dl = ensureDatalist();
        dl.innerHTML = "";
        items.forEach(function (it) {
          var l = label(it);
          byLabel[l] = it;
          byId[it.id] = it;
          var opt = document.createElement("option");
          opt.value = l;
          dl.appendChild(opt);
        });
        return items;
      });
  }

  function rowOf(el) {
    return el.closest("tr") || el.closest(".line-row") || el.parentElement;
  }

  function field(row, name) {
    return row.querySelector('[name$="[' + name + ']"]');
  }

  function setSelect(sel, value) {
    if (!sel || !value) return;
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === value) {
        sel.value = value;
        sel.dispatchEvent(new Event("change", { bubbles: true }));
        return;
      }
    }
  }

  function fill(picker, it) {
    var row = rowOf(picker);
    var hidden = row.querySelector(".item-picker-id");
    if (hidden) hidden.value = it ? it.id : "";
    if (!it) return;
    var side = picker.getAttribute("data-side") || "sale";
    var desc = field(row, "description");
    if (desc && (!desc.value || desc.dataset.autofilled === "1")) {
      desc.value = it.description || it.name;
      desc.dataset.autofilled = "1";
    }
    var price = field(row, "unit_price");
    var p = side === "purchase" ? it.purchase_price : it.default_sale_price;
    if (price && p !== null && p !== undefined && Number(p) !== 0) {
      price.value = Number(p).toFixed(2);
      price.dispatchEvent(new Event("input", { bubbles: true }));
    }
    var acct;
    if (side === "purchase") {
      acct = it.item_type === "inventory" ? it.inventory_account_id : it.purchase_account_id;
    } else {
      acct = it.income_account_id;
    }
    setSelect(field(row, "account_id"), acct);
    setSelect(
      field(row, "tax_code_id"),
      side === "purchase" ? it.purchase_tax_code_id : it.sale_tax_code_id
    );
  }

  function labelExisting(root) {
    (root || document).querySelectorAll(".item-picker").forEach(function (picker) {
      var row = rowOf(picker);
      var hidden = row.querySelector(".item-picker-id");
      if (hidden && hidden.value && byId[hidden.value] && !picker.value) {
        picker.value = label(byId[hidden.value]);
      }
    });
  }

  document.addEventListener("focusin", function (e) {
    if (e.target.classList && e.target.classList.contains("item-picker")) load();
  });

  document.addEventListener("change", function (e) {
    var t = e.target;
    if (!t.classList || !t.classList.contains("item-picker")) return;
    load().then(function () {
      var v = t.value.trim();
      if (!v) { fill(t, null); return; }
      var it = byLabel[v];
      if (!it) {
        var lower = v.toLowerCase();
        var matches = items.filter(function (x) {
          return x.sku.toLowerCase() === lower || label(x).toLowerCase().indexOf(lower) !== -1;
        });
        if (matches.length === 1) { it = matches[0]; t.value = label(it); }
      }
      if (it) {
        t.removeAttribute("aria-invalid");
        fill(t, it);
      } else {
        t.setAttribute("aria-invalid", "true");
        fill(t, null);
      }
    });
  });

  function init() {
    if (document.querySelector(".item-picker")) {
      load().then(function () { labelExisting(document); });
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
  document.addEventListener("htmx:afterSwap", function (e) {
    if (items !== null) labelExisting(e.target);
    else init();
  });
})();
