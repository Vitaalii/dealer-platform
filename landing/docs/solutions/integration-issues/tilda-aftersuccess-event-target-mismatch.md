---
title: "tildaform:aftersuccess fires on <form>, not on wrapper — closest() required"
date: 2026-06-21
category: docs/solutions/integration-issues
module: U2 / tildaform-callback
problem_type: integration_issue
component: tooling
symptoms:
  - mySuccessFunction declared globally but never invoked after form submit
  - Messenger redirect (MAX / Telegram) does not happen after form submission
  - "e.target.classList check against 'uc-tildaform' always returns false"
root_cause: wrong_api
resolution_type: code_fix
severity: high
tags:
  - tilda
  - tildaform
  - aftersuccess
  - event-delegation
  - closest
  - form-callback
  - messenger-redirect
---

# tildaform:aftersuccess fires on `<form>`, not on wrapper — closest() required

## Problem

After subscribing to `tildaform:aftersuccess` on `document`, the callback `mySuccessFunction` was never invoked. Form submission succeeded (Tilda received the data), but the messenger redirect (MAX / Telegram) did not fire.

## Symptoms

- `mySuccessFunction` declared in global scope but never called after form submit
- `console.log` inserted at function start never appeared in the browser console
- Messenger redirect to MAX or Telegram did not happen after successful form send
- `e.target.classList` inspection in console showed `t-form js-form-proccess t-form_inputs-total_0` — no `uc-tildaform` class

## What Didn't Work

- **Declaring `mySuccessFunction` in global scope** — Tilda does not call the function by name; it dispatches a DOM event.
- **Checking `e.target.classList.contains('uc-tildaform')`** — the event fires on the `<form>` element itself, not on the `.uc-tildaform` wrapper `<div>`. The check always returned `false`.
- **Reading `messenger` from form inputs in `mySuccessFunction`** — Tilda clears form inputs before or during the callback. The field value was always empty string. Fix: store messenger choice in a global variable before submit.

## Solution

**Primary fix — use `.closest()` instead of direct `classList` check:**

```js
// BEFORE (broken — wrong DOM target)
document.addEventListener('tildaform:aftersuccess', function (e) {
    if (e.target && e.target.classList.contains('uc-tildaform')) {
        mySuccessFunction(e.target);
    }
});

// AFTER — event fires on <form>, use .closest() to find the wrapper
document.addEventListener('tildaform:aftersuccess', function (e) {
    if ($(e.target).closest('.uc-tildaform').length > 0) {
        mySuccessFunction(e.target);
    }
});
```

**Secondary fix — also register via `data-success-callback` (defence in depth):**

Tilda forms carry a `data-success-callback="<funcName>"` attribute. If Tilda invokes this function by name (in addition to the DOM event), `window[funcName]` must be defined. Read it dynamically at `ready` time so the code is not tied to the T-block ID:

```js
$(document).ready(function () {
    var $form = $('.uc-tildaform .js-form-proccess');
    if ($form.length) {
        var cbName = $form.attr('data-success-callback');
        if (cbName && typeof window[cbName] === 'undefined') {
            window[cbName] = function (form) { mySuccessFunction(form); };
        }
    }
});
```

**Also required — read messenger from a global, not from form inputs:**

```js
// Set global BEFORE triggering $submitBtn.click()
waMessenger = isMax ? 'MAX' : 'Telegram';
waSend = true;

// In mySuccessFunction — read global, not form input
function mySuccessFunction(form) {
    const messenger = waMessenger || '';  // NOT: $(form).find('input[name="messenger"]').val()
    ...
}
```

## Why This Works

Tilda CMS dispatches `tildaform:aftersuccess` on the **form element** (`<form class="t-form js-form-proccess ...">`) using `dispatchEvent`. The `.uc-tildaform` class is on a wrapper `<div>` — the event's `e.target` is the form, not the wrapper. jQuery's `.closest()` traverses up the DOM to the wrapper, which correctly identifies that the submitting form belongs to our block.

The dynamic `window[cbName]` registration covers the `data-success-callback` attribute path — Tilda may call this named function directly after successful submission as an alternative (or supplementary) mechanism.

Form input values are cleared by Tilda's own success-handling logic before the callback runs. Any state needed inside `mySuccessFunction` must be stored in a module-level global (e.g., `waMessenger`) and set synchronously in the click handler before triggering form submit.

## Prevention

- **Never check `e.target.classList` directly when targeting a Tilda CMS form wrapper** — use `$(e.target).closest('.your-wrapper')` instead.
- **Never read form input values inside `tildaform:aftersuccess`** — Tilda clears inputs during its own success handling. Persist all needed state in globals before the submit.
- **Verify the function is actually called** — add a temporary `console.log` at the top of the callback as the first diagnostic step when a Tilda after-submit hook seems broken.
- **Register both callback paths** — `tildaform:aftersuccess` event listener plus `window[data-success-callback]` dynamic registration gives two independent paths for Tilda to invoke the callback.

## Related Issues

- `docs/tilda-blocks/u2-unifiedbot-script.html` — contains the fix (lines 211-230)
- `docs/tilda-blocks/u5-quiz-choice.html` — sets `waMessenger` global before form submit (click handlers for MAX/Telegram buttons)
