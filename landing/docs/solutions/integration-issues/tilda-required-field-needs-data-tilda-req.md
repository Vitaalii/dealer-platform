---
title: "Tilda does not enforce a required field without data-tilda-req — a working block may be an accident"
date: 2026-09-18
category: docs/solutions/integration-issues
module: U6 / calc-submit
problem_type: integration_issue
component: tooling
symptoms:
  - Unchecking a consent checkbox blocks form submission, yet nothing in our code checks it
  - The same unchecked checkbox does not block a second path that opens a widget before submitting
  - "Tilda shows an 'empty form' error instead of a 'required field' error"
  - A validation refusal surfaces to the user as a generic network-failure message
root_cause: wrong_assumption
resolution_type: code_fix
severity: high
tags:
  - tilda
  - tildaform
  - validation
  - required-field
  - data-tilda-req
  - js-tilda-rule
  - consent
  - personal-data
---

# Tilda does not enforce a required field without `data-tilda-req` — a working block may be an accident

## Problem

A consent-to-process-personal-data checkbox appeared to be protected: unchecking it stopped the form from submitting. Nothing in our code checked it. The protection came entirely from Tilda — and it was not the protection anyone thought it was.

The checkbox was **not** marked required in Tilda. It blocked submission only because it happened to be the **only validated field in the form**, so unchecking it made Tilda judge the entire form empty. Any second `js-tilda-rule` field added to that form would have silently removed the block.

The same form had a second submit path that opened a chat widget *before* calling Tilda's submit. That path was never protected at all.

## Symptoms

- Unchecking the consent box blocks submission, but `grep` finds no consent check anywhere in our code
- A second path (opening a widget, a messenger, a redirect) sails past the same checkbox untouched
- Tilda's own error is `emptyfill` ("form is empty"), not `req` ("field is required") — visible in `window.tildaForm.validate()`'s return value
- The user sees a generic "could not submit, try again" message for what is actually a validation refusal
- Inspecting the field in the browser: `document.querySelectorAll('.your-form .js-tilda-rule').length === 1` and `getAttribute('data-tilda-req') === null`

## Root Cause

Two independent facts in `tilda-forms-1.0.min.js`.

**1. Required-ness is one attribute, and nothing else.** In `window.tildaForm.validate`:

```js
var d = !!parseInt(u.getAttribute("data-tilda-req") || 0, 10);
…
if (d && (E || F && !A)) v.type.push("req");
```

`js-tilda-rule` only enrolls a field into validation. It does **not** make it required. Without `data-tilda-req="1"` the `req` branch can never fire, whatever the field looks like in the editor.

**2. An all-empty form is refused separately.** The same function ends with:

```js
return a && !n.length && o.length && (n = [{obj:"none", type:["emptyfill"]}]), n
```

`a` stays `true` while no validated field holds a value (`h.length && !F`) and no checkbox/radio is checked (`F && A`). So when the only validated field is an unchecked checkbox, the form falls into `emptyfill` and is refused — an accident that looks exactly like enforcement.

**Bonus trap:** `window.tildaForm.showErrors` ends with

```js
t_triggerEvent(o, "tildaform:aftererror"), !0
```

`t_triggerEvent` is a plain `dispatchEvent`. So `tildaform:aftererror` is dispatched **synchronously, inside the `.click()` call**, for a validation refusal — and also **asynchronously from the XHR callback** for a genuine server failure. One event, two meanings. A single handler on it cannot tell a rejected form from a dead network, which is how a validation refusal ended up wearing a network-error message.

## How to Recognise It

Ask one question about any Tilda form you rely on: **where is the check written down?**

If the answer is "it just works", inspect the form in the browser:

```js
var f = document.querySelector('.your-form');
[...f.querySelectorAll('.js-tilda-rule')].map(el => ({
    name: el.getAttribute('name'),
    required: el.getAttribute('data-tilda-req'),   // null → NOT required
    type: el.getAttribute('type')
}));
```

Red flags:
- exactly **one** entry in that list — the whole form's validation rests on a single field
- `required: null` on a field you believe is mandatory
- Tilda's refusal shows up in `.js-rule-error-all` (empty form) rather than `.js-rule-error-req` (required field)

## Solution

**Write the check in your own code, before handing anything to Tilda.** Do not let a third party's validation be the only thing standing between a user and a submitted personal-data form.

```js
// Читает визуальные пункты — то, с чем взаимодействовал человек.
function ucConsentState() {
    var $items = $('#uc-calc-agreements .uc-calc-agr-item');
    if (!$items.length) return 'empty';                        // форма не пришла
    return $items.not('.uc-agr-checked').length ? 'unchecked' : 'ok';
}

// В обработчике кнопки — ДО всех ветвлений: до открытия виджета,
// до выставления флагов, до сабмита Tilda.
var consent = ucConsentState();
if (consent !== 'ok') {
    показатьПричину(consent);
    return;                       // ни виджета, ни отправки
}
syncCalcAgreements();             // запись в форму Tilda — только после чтения
```

Three details that matter:

- **Put the gate before every branch**, not next to the submit call. A second entry point (widget, messenger, redirect) that runs earlier will otherwise slip past it — that was the actual second symptom here.
- **Read before you write.** The gate reads the visual state; the sync writes it into Tilda's field. Syncing first corrupts the recovery path: on an empty container the sync writes "unchecked" into the real checkbox, and a subsequent re-render reads that back and reports "not checked" out of nowhere.
- **Keep validation errors and send failures on separate wires.** Validation writes its message directly to the error element (it works regardless of any in-flight state, and nothing needs unwinding). Only real failures go through the send-failure path that clears the sending spinner.

**Optionally also tick "Обязательное поле" in the Tilda editor** so `data-tilda-req="1"` appears on the input. Treat it as a cheap backstop, never as the protection — it does not stop code paths that run before the submit.

## Why This Works

The check no longer depends on how many other fields the form happens to contain, on whether someone ticks a box in the Tilda editor, or on Tilda's internal ordering between validation and event dispatch. It is a plain conditional in code that a reader can find by grepping for the message text.

It also makes the generic failure message honest: once consent is enforced upstream, a `tildaform:aftererror` can only mean a genuine send failure, so "could not submit, try again" stops impersonating a validation error.

## Prevention

- **`js-tilda-rule` ≠ required.** Only `data-tilda-req="1"` makes Tilda enforce a field.
- **A form whose validation rests on a single field is one field away from having none.** Adding any validated field to such a form silently lifts the `emptyfill` refusal.
- **Never let consent or any legally meaningful confirmation be enforced only by a CMS.** Write it in code, next to the submit it guards.
- **`tildaform:aftererror` means two different things** — validation refusal (synchronous, before the request) and send failure (asynchronous, from XHR). Do not give both the same user-facing message.
- **When a test stub models a third party, verify it against that party's source.** The harness here stubbed "validation refused → no class, no event", which Tilda never does; that fiction is why the defect passed tests green.

## Related Issues

- `docs/plans/2026-09-18-001-fix-consent-gate-both-paths-plan.md` — the full investigation and fix
- `docs/tilda-blocks/Ver_3_ong/u6-gallery.html` — `ucConsentState()`, the gate in the `#uc-gal-submit` handler
- `docs/solutions/integration-issues/tilda-aftersuccess-event-target-mismatch.md` — the other half of the `tildaform:*` contract (`closest()` on the wrapper)
- `docs/testing/phone-validation-harness/` — the Tilda stub, now reconciled with `tilda-forms-1.0.min.js`
- Verified against `https://static.tildacdn.com/js/tilda-forms-1.0.min.js` (`t_forms__initBtnClick`, `window.tildaForm.validate`, `window.tildaForm.showErrors`, `t_triggerEvent`), 2026-09-18
