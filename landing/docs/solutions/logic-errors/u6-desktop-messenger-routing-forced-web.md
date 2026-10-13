---
title: "U6 desktop messenger routing always forced to web widget"
date: 2026-07-03
category: logic-errors
module: u6-gallery
problem_type: logic_error
component: frontend_stimulus
severity: high
symptoms:
  - On desktop, clicking ДАЛЕЕ always opened the TD web widget regardless of which messenger card was selected
  - MAX card selection on desktop was silently ignored and routed to web widget instead
  - Telegram card selection on desktop was silently ignored and routed to web widget instead
root_cause: logic_error
resolution_type: code_fix
related_components:
  - u2-unifiedbot-script
  - u5-quiz-choice
tags:
  - messenger-routing
  - desktop-only
  - u6-gallery
  - wa-messenger
  - contact-quiz
  - tilda-blocks
---

# U6 desktop messenger routing always forced to web widget

## Problem

The `#uc-gal-submit` click handler in `docs/tilda-blocks/Ver_3_ong/u6-gallery.html` always routed desktop users to the TD web widget regardless of which messenger card they had selected in the contact quiz. The messenger selection was effectively ignored on desktop.

## Symptoms

- On desktop, selecting the MAX or Telegram card in Step 3 of the gallery quiz and clicking ДАЛЕЕ opened the TD web chat widget instead of routing to the selected messenger
- The `waMessenger` global was set to `'web'` in all desktop cases, so `mySuccessFunction` in U2 never received the MAX or Telegram choice
- Mobile was unaffected — messenger routing worked correctly there because the mobile branch already read `calcMessenger` directly

## What Didn't Work

The fix was direct once the logic was read. No failed investigation attempts. The mobile path was already correct and required no changes.

## Solution

Two targeted changes in the `#uc-gal-submit` click handler inside `docs/tilda-blocks/Ver_3_ong/u6-gallery.html`.

**Change 1 — messenger variable assignment**

Remove the `isMobile` guard that forced `'web'` on desktop. `calcMessenger` already holds the correct value for both platforms:

```javascript
// Before (broken):
var messenger = '';
if (isChat) {
    messenger = isMobile ? calcMessenger : 'web';
}

// After (fixed):
var messenger = '';
if (isChat) {
    messenger = calcMessenger;
}
```

**Change 2 — TD widget open condition**

Replace the `!isMobile` guard with a check against the actual messenger value. The TD widget should only open when the user chose «На сайте», not whenever the device is desktop:

```javascript
// Before (broken):
if (isChat && !isMobile) {
    // ... opens TD widget
}

// After (fixed):
if (isChat && messenger === 'web') {
    // ... opens TD widget
}
```

## Why This Works

The original code conflated two independent concerns: device type and messenger choice. The intent was that «На сайте» (the default active card, `calcMessenger = 'web'`) should open the TD widget on desktop — but the implementation hard-coded `'web'` for all desktop users regardless of card selection, then opened the TD widget based on `!isMobile` rather than the actual messenger value.

`calcMessenger` is updated correctly when any card is clicked on both platforms; the bug was that desktop never read it. After the fix, `messenger` is always read from `calcMessenger`, and the TD widget opens only when that value is `'web'`. The `waMessenger` global — which U2's `mySuccessFunction` reads after form submit to route to MAX or Telegram — now receives the correct value on desktop as well.

## Prevention

- When a variable is updated correctly by user interaction (`calcMessenger` via card clicks), routing logic must read that variable directly rather than substituting a platform-based constant
- Device type (`isMobile`) is the right condition for **hiding UI elements** (e.g., the «На сайте» card is hidden on mobile); it is **not** a proxy for the user's choice
- Conditions that gate behavior after a selection should be expressed in terms of the selection value (`messenger === 'web'`), not the device type (`!isMobile`)

## Related Issues

- [`docs/solutions/integration-issues/tilda-aftersuccess-event-target-mismatch.md`](../integration-issues/tilda-aftersuccess-event-target-mismatch.md) — related: messenger routing in the same Tilda quiz submit flow, but addresses callback invocation failure rather than routing logic
