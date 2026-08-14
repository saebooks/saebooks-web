/* Submit-once guard for plain (non-htmx) POST forms.
 *
 * Every state-changing action that is NOT an htmx request — void, post,
 * approve, delete — is a plain <form method="post">. A double-click sends
 * the request twice, and the engine's posting chokepoints are the last
 * line of defence rather than the first. htmx forms already have
 * hx-disabled-elt; this is the equivalent for the ones it cannot reach.
 *
 * One delegated listener rather than an attribute on each of the ~87
 * forms, so a new template gets the behaviour without remembering to ask
 * for it.
 *
 * Three details that are load-bearing:
 *
 *  - BUBBLE phase, and defaultPrevented is honoured. Inline
 *    onsubmit="return confirm(...)" is registered on the form and runs
 *    first; cancelling it marks the event defaultPrevented. A capture-phase
 *    listener would instead disable the button BEFORE the confirm, and a
 *    user who clicked Cancel would be left with a dead button.
 *
 *  - The button is disabled on the NEXT TICK, not synchronously. Form data
 *    (including the submitter's own name/value — five buttons in this app
 *    carry one) is serialised synchronously during the submit event; a
 *    button disabled before that point is omitted from the payload.
 *
 *  - pageshow re-enables. Coming Back to a page served from the bfcache
 *    restores the DOM as it was — mid-submit, with the button disabled —
 *    so without this the form is permanently dead.
 */
(function () {
  'use strict';

  var SUBMITTING = 'data-submitting';

  function isHtmxDriven(form) {
    // htmx handles its own in-flight state via hx-disabled-elt; leaving
    // these alone avoids fighting it over the same button.
    return (
      form.hasAttribute('hx-post') ||
      form.hasAttribute('hx-get') ||
      form.hasAttribute('hx-put') ||
      form.hasAttribute('hx-patch') ||
      form.hasAttribute('hx-delete') ||
      form.hasAttribute('data-hx-post') ||
      form.closest('[hx-boost="true"]') !== null
    );
  }

  function submitters(form) {
    return form.querySelectorAll('button[type="submit"], input[type="submit"]');
  }

  function release(form) {
    form.removeAttribute(SUBMITTING);
    var buttons = submitters(form);
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].disabled = false;
      buttons[i].removeAttribute('aria-busy');
    }
  }

  document.addEventListener(
    'submit',
    function (event) {
      var form = event.target;
      if (!form || form.tagName !== 'FORM') return;

      // A confirm() the user cancelled, or any other handler that stopped
      // the submission. Nothing was sent, so nothing should be locked.
      if (event.defaultPrevented) return;

      // GET forms are searches and filters — re-running one is harmless
      // and blocking it would be a regression.
      var method = (form.getAttribute('method') || 'get').toLowerCase();
      if (method !== 'post') return;

      if (isHtmxDriven(form)) return;

      if (form.hasAttribute(SUBMITTING)) {
        // Already in flight: this is the second click.
        event.preventDefault();
        return;
      }

      form.setAttribute(SUBMITTING, '');
      var buttons = submitters(form);
      window.setTimeout(function () {
        for (var i = 0; i < buttons.length; i++) {
          buttons[i].disabled = true;
          buttons[i].setAttribute('aria-busy', 'true');
        }
      }, 0);
    },
    false
  );

  // Restored from the back/forward cache with the DOM frozen mid-submit.
  window.addEventListener('pageshow', function (event) {
    if (!event.persisted) return;
    var forms = document.querySelectorAll('form[' + SUBMITTING + ']');
    for (var i = 0; i < forms.length; i++) release(forms[i]);
  });
})();
