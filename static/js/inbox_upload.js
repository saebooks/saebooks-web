/* Inbox photo upload on a phone (b15, gap P2-25).
 *
 * Photos from a phone camera are often 4 to 12 MB and coverage on site is
 * patchy, so a plain form post either took a minute or failed and the
 * photo was lost. This helper:
 *
 *   1. shrinks a large photo in the browser (longest side 2000 px, JPEG)
 *      before sending it. PDFs and small images are sent untouched;
 *   2. retries a failed send twice (after 2 and 5 seconds);
 *   3. if there is still no connection, keeps the photo on this phone
 *      (IndexedDB) and sends it by itself when the signal comes back or
 *      the next time the inbox is opened.
 *
 * Used by templates/inbox/list.html through window.sbInboxUpload(file,
 * form). If this file fails to load the page falls back to the normal form
 * post, so nothing depends on it.
 */
(function () {
  'use strict';

  var MAX_EDGE = 2000;
  var QUALITY = 0.85;
  var RESIZE_OVER = 1.5 * 1024 * 1024;
  var RETRY_DELAYS = [2000, 5000];
  var DB_NAME = 'sb-inbox-queue';
  var STORE = 'uploads';

  function status(msg) {
    var el = document.getElementById('inbox-upload-progress');
    if (!el) return;
    el.classList.remove('hidden');
    el.textContent = msg;
  }

  function resize(file) {
    return new Promise(function (resolve) {
      if (!/^image\/(jpeg|png|webp)$/.test(file.type || '') || file.size < RESIZE_OVER || !window.URL) {
        resolve(file);
        return;
      }
      var url = URL.createObjectURL(file);
      var img = new Image();
      img.onload = function () {
        try {
          var scale = Math.min(1, MAX_EDGE / Math.max(img.naturalWidth, img.naturalHeight));
          var canvas = document.createElement('canvas');
          canvas.width = Math.round(img.naturalWidth * scale);
          canvas.height = Math.round(img.naturalHeight * scale);
          canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
          URL.revokeObjectURL(url);
          canvas.toBlob(function (blob) {
            if (!blob || blob.size >= file.size) { resolve(file); return; }
            var name = (file.name || 'photo').replace(/\.[A-Za-z0-9]+$/, '') + '.jpg';
            try {
              resolve(new File([blob], name, { type: 'image/jpeg' }));
            } catch (e) {
              blob.name = name;
              resolve(blob);
            }
          }, 'image/jpeg', QUALITY);
        } catch (e) {
          URL.revokeObjectURL(url);
          resolve(file);
        }
      };
      img.onerror = function () { URL.revokeObjectURL(url); resolve(file); };
      img.src = url;
    });
  }

  function formFields(form) {
    var fields = {};
    Array.prototype.forEach.call(form.querySelectorAll('input[type=hidden]'), function (el) {
      if (el.name) fields[el.name] = el.value;
    });
    return fields;
  }

  function send(form, file, fields) {
    var fd = new FormData();
    Object.keys(fields).forEach(function (k) { fd.append(k, fields[k]); });
    fd.append('file', file, file.name || 'photo.jpg');
    return fetch(form.action, { method: 'POST', body: fd, credentials: 'same-origin' });
  }

  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function sendWithRetry(form, file, fields) {
    var attempt = 0;
    function tryOnce() {
      return send(form, file, fields).catch(function (err) {
        if (attempt >= RETRY_DELAYS.length) throw err;
        var delay = RETRY_DELAYS[attempt++];
        status('No connection. Trying again in ' + (delay / 1000) + ' seconds…');
        return wait(delay).then(tryOnce);
      });
    }
    return tryOnce();
  }

  // ── Offline queue (IndexedDB) ────────────────────────────────────────
  function openDb() {
    return new Promise(function (resolve, reject) {
      if (!window.indexedDB) { reject(new Error('no indexedDB')); return; }
      var req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = function () { req.result.createObjectStore(STORE, { autoIncrement: true }); };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
  }

  function queueAdd(file, companyId) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(STORE, 'readwrite');
        tx.objectStore(STORE).add({ blob: file, name: file.name || 'photo.jpg', company_id: companyId || '', at: Date.now() });
        tx.oncomplete = function () { resolve(); };
        tx.onerror = function () { reject(tx.error); };
      });
    });
  }

  function queueAll() {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var out = [];
        var tx = db.transaction(STORE, 'readonly');
        var req = tx.objectStore(STORE).openCursor();
        req.onsuccess = function () {
          var cur = req.result;
          if (cur) { out.push({ key: cur.key, value: cur.value }); cur.continue(); } else { resolve(out); }
        };
        req.onerror = function () { reject(req.error); };
      });
    });
  }

  function queueDelete(key) {
    return openDb().then(function (db) {
      return new Promise(function (resolve) {
        var tx = db.transaction(STORE, 'readwrite');
        tx.objectStore(STORE).delete(key);
        tx.oncomplete = function () { resolve(); };
        tx.onerror = function () { resolve(); };
      });
    });
  }

  var flushing = false;
  function flushQueue(form) {
    if (flushing || !form || !navigator.onLine) return Promise.resolve(0);
    flushing = true;
    return queueAll().then(function (items) {
      if (!items.length) return 0;
      status('Sending ' + items.length + ' photo(s) saved on this phone…');
      var sent = 0;
      return items.reduce(function (p, item) {
        return p.then(function () {
          var fields = formFields(form);
          if (item.value.company_id) fields.company_id = item.value.company_id;
          var file = item.value.blob;
          try { file = new File([item.value.blob], item.value.name, { type: item.value.blob.type }); } catch (e) { /* keep blob */ }
          return send(form, file, fields).then(function (resp) {
            if (resp.ok || resp.redirected) { sent++; return queueDelete(item.key); }
          }).catch(function () { /* still offline: keep it */ });
        });
      }, Promise.resolve()).then(function () { return sent; });
    }).then(function (sent) {
      flushing = false;
      if (sent) window.location.reload();
      return sent;
    }).catch(function () { flushing = false; return 0; });
  }

  function upload(file, form) {
    if (!file || !form || !window.fetch || !window.FormData) { form.submit(); return; }
    var fields = formFields(form);
    status('Preparing the photo…');
    resize(file).then(function (small) {
      status('Uploading and reading the document — this can take a few seconds…');
      return sendWithRetry(form, small, fields).then(function (resp) {
        // The server answers with a redirect to the new document (or back
        // to the inbox with a message); fetch has followed it already.
        window.location.href = resp.url || form.action.replace(/\/upload$/, '');
      }).catch(function () {
        return queueAdd(small, fields.company_id).then(function () {
          status('No signal. The photo is saved on this phone and will upload by itself when you are back online.');
        }).catch(function () {
          status('Could not upload, and this browser cannot keep the photo for later. Please try again when you have signal.');
        });
      });
    });
  }

  window.sbInboxUpload = upload;

  function init() {
    var form = document.getElementById('inbox-upload-form');
    if (!form) return;
    flushQueue(form);
    window.addEventListener('online', function () { flushQueue(form); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
