> ⚠️ **Исторический снимок старой системы** (см. `SOURCE.md` в этой папке). Боевые адреса, ранбуки и команды ниже относятся к старой системе UnifiedBot. В новой платформе их не выполнять; правила — в корневом `CLAUDE.md`.

# Tilda Landing 2.0 — Project Guide

## Project Structure

Six custom Tilda T123 HTML blocks, each a standalone file. **The working version is `docs/tilda-blocks/Ver_4_dev/`** (since 2026-09-30, plan `docs/plans/2026-09-30-001-feat-lk-tab-cabinet-web-entry-plan.md`): full sources with comments. **What goes into Tilda is only a build** — the same file names, made by `npm run build:prod` without comments and extra whitespace: `Ver_4_dev/Production/` for production pages, `Ver_4_dev/Production-test/` for the test page `page154296216` (see Deployment). `Ver_3_ong/` is the previous working version, still the source of what runs on the other production pages; `Ver_1/` and `Ver_2/` are historical snapshots. Do not edit any of them.

**Production state (checked with `check-live` 2026-10-09; the test page re-checked 2026-10-12):** the LK tab is live on three production pages — https://demo.autodc.org/haval-demo-new (dealer `01`, Haval, colour `#6ACEC3` / `#5BB0A7`), https://offer.geely-radar.ru (`02`, Geely) and https://sale.belgee-radar.ru (`03`, Belgee; both `#2D5467` / `#3C728D`). On all three, **all six blocks match `Production/` at commit `3a41470`** (published by the owner 08–09.10), and each site's HEAD has `<link rel="preconnect" href="https://obut-5oaj-5i2u.gw-1a.dockhost.net" crossorigin>` (haval also keeps the old line without `crossorigin`). The test page `page154296216` (`Production-test/`, dealer `99`): all six blocks `ok` at commit `3a41470` (re-pasted 2026-10-12, colour `#2D5467` / `#3C728D`); its data appears only while the dev bot stand is up. Plan `2026-10-12-001` (Swiper and Tabler from the bot, U2 only, commit `61929b8`): **pasted 2026-10-13 on the test page and all three production pages** (agent's check ok on each; haval `uc:ready` median 1895 → 1926 ms) — so U2 on all four pages is `61929b8`, the other five blocks `3a41470`; rollback copies of the previous U2 in `docs/handoff/rollback-u2-2026-10-12/`. Production bot 3.4.1 serves `/bot-links` and `tables.catalog.active_field`. Where things stand and what is next: the section «Сводка на 2026-10-09» at the top of `docs/handoff/2026-10-06-release-lk-tab.md`. **Check with `tools/check-live.py` before trusting this paragraph.**

Live pages show two `<!-- nominify begin -->` in a row at the start of every block: one is ours (the first line of each build), the other is added by Tilda's T123 wrapper. Harmless (checked 2026-10-09 on haval and Geely), nothing to fix.

`archive/Ver_4_u6_calc_experiment/` is a dead branch of `u6-gallery.html` only (its u2/u3/u4 are byte-identical copies of the working version). Its calc UI — `#uc-calc-method-block`, `#uc-calc-types`, the disabled state of the submit button — is a **deliberately cancelled direction, not an unfinished task**: it never shipped and will not be ported. Do not resurrect it or raise it as an open question.

| Block | File | Purpose |
|-------|------|---------|
| U1 | `u1-screen-chrome.html` | Page chrome outside the quiz: shows and hides Hero as step 0, paints the accent band at the top of the quiz sections, intercepts the Hero entry button ahead of Tilda's native T270, neutralises the legacy `#filter` hash, owns the scroll lock for our overlays (`window.ucSyncScrollLock`) and their back-button history (`window.ucOverlayHistory`). Renders **no page section of its own**. Must sit after the native T809 block |
| U2 | `u2-unifiedbot-script.html` | Entry point: the TEST/PROD values (mode set by the build), dealer settings from the bot by the form's `dealer_id`, catalog/features/rates loading, globals, `initCards`/`initGallery`, `window.ucQuiz` step machine, Tilda form success callback and messenger routing (Telegram / MAX / web widget) |
| U3 | `u3-card-sliders.html` | **Quiz step 1.** Model carousel (desktop grid + mobile Swiper), configuration tiles (engine / transmission / drivetrain), complectation tiles, options indicator. Owns `selectComplectation` — the central state coordinator |
| U4 | `u4-accordion.html` | Equipment popup, by category, for the currently selected complectation. Renders **no page section of its own** — its only visible component is a `position: fixed` popup. Receives state via `window.updateFeatures(car)` |
| U6 | `u6-gallery.html` | **Quiz step 2.** Photo gallery with Swiper, thumbnails, lightbox, specs and the contact form with three tabs: «В чате» (first, opened on every entry to step 2; on a phone, under the MAX card, «Где открыть MAX» — app or web, plan `2026-10-08-003`), «В личном кабинете» (scenario, payment scale, plate, mileage, send → open cabinet session), «По телефону». Receives state via `window.updateGallery(car)` |
| U7 | `u7-contacts.html` | Contacts overlay: the dealer card plus a Yandex map this block builds itself, opened from the header's «КОНТАКТЫ» link via `window.openContactsPopup()`. Renders **no page section of its own**. Consumes `window._dealerReady` from U2 |

U5 (contact-channel quiz) was removed from the project; its `waMessenger` selection now lives in U6's calc section. The name U5 is burned — the contacts block deliberately took U7 instead, so the archived `u5-quiz-choice.html` is not mistaken for it.

The map is built by U7 rather than reusing Tilda's native T554 block: Tilda keeps its `ymaps.Map` instance in a local variable and never exposes it, so `container.fitToViewport()` cannot be called and a map re-parented into a resized popup keeps the wrong pane size. U7 carries its own Yandex API key for the same reason — the native block's key disappears with the block.

## Data Architecture

### Dealer settings and NocoDB tables
The site sets **only the dealer code** — the hidden field `dealer_id` of the Tilda form `.uc-tildaform` (99 on the test page). Everything else comes from the Dealers table in NocoDB through the bot: U2 calls `GET {bot}/cabinet/api/dealer/{dealer_code}` once — **as soon as U2 executes**, not on `DOMContentLoaded`: on every page the form sits in the HTML above U2, so the code is already readable (plan `2026-10-08-005`; if the field is missing at that moment — form below U2 — the requests wait for `DOMContentLoaded`, as before) — and gets
- `tables` — ids of the catalog, features and rates tables, read via `/catalog/{table_id}`;
- `dealer` — the 7 public dealer-card fields (`Dealer_Code`, `Dealer_name`, `Dealer_address`, `Dealer_location`, `Dealer_workinghours`, `Dealer_phone`, `Dealer_status`);
- `form_values` — `brand`, `dealer`, `website`, `letter_title`, `form_type`, written into the same-named hidden form fields on load (`form_type` is the default; the LK tab sets its own per scenario).

No table id or dealer code is hard-coded in any block (`TABLE_MAIN`, `TABLE_FEATURES`, `TABLE_DEALERS`, `DEALER_CODE` were removed on 2026-10-01). If the settings request fails, the page behaves as with an unavailable catalog. Contract: bot repo `docs/handoff/landing-brief.md` (L8).

### API Server
Bot URL comes from the TEST/PROD values of U2 (`window.UNIFIEDBOT_URL`): dev bot `https://sporophoric-devorah-remonstrative.ngrok-free.dev` (local stand behind ngrok, up only while the bot agent runs it) in the `Production-test/` build, `https://obut-5oaj-5i2u.gw-1a.dockhost.net` in the `Production/` build.

Endpoints used:
- `GET /cabinet/api/dealer/{dealer_code}` — dealer settings (U2; U7 on the Кабинет page)
- `GET /catalog/{table}` — returns `{ list: [...] }` or raw array
- `POST /cabinet/api/web/open` — open a cabinet session from the LK tab (U6), returns `cabinet_url`
- Widget JS served from same origin: `/widget-custom/widget.js` (for the ngrok bot fetched with the `ngrok-skip-browser-warning` header and injected as text)

### Key NocoDB Fields
- `Active` — boolean filter; only `true`/`1`/`"true"` rows are loaded. The column name is per dealer: `tables.catalog.active_field` of the dealer settings (bot 3.4.1; empty or invalid → `Active`), so one catalog serves dealers with different model sets (Geely `02` uses `Active_1`)
- `Model` / `Model id` — model grouping
- `Model utm` — UTM slug for URL-based model pre-selection (e.g. `"HAVALF7"`, `"HAVALJOLION"`). Matched case-insensitively via exact `===` against `?utm=` param
- `Model Modification` — modification tab label
- `Complectation` / `Complectation id` — complectation name and sort order
- `Pics` — array of photo objects `{ url, signedPath, path }` for the gallery

## Global Functions and Their Connections

```
U2: startRequests()  — when U2 executes: settings, /bot-links, then the tables
U2: buildPage()      — on $(document).ready: form values, then
  ├─► window.initGallery(airData)   [U6] — shows first car's gallery on load
  └─► window.initCards(airData)     [U3] — builds model carousel

U3: buildModelSlider(airData)
  └─► selectModel(name)
        └─► buildConfigurator(modelData)      — seeds selEngine/selTransm/selDrive
              └─► applyConfig()               — recomputes ucQuiz.matchingCars,
                    │                            keeps the trim by Complectation id
                    │                            if still available, else takes the first
                    ├─► renderTrims()
                    └─► selectComplectation(car)      ← central coordinator
                          ├─► window.ucQuiz.selectedCar = car   (read by U6 on step 2)
                          ├─► window.updateGallery(car)  [U6]
                          └─► window.updateFeatures(car) [U4]
                                └─► window.uc_updateOptsIndicator(x, y)  [U3]

U3: click on .opts-indicator («Показать →»)
  └─► window.openFeaturesPopup()   [U4] — renders the popup from the car
                                          selected at click time
```

### Cross-block contract for the equipment popup

U3 owns the *markup* of the options indicator; U4 owns the *data* (`_featCache`,
`buildModelData`). Both directions of the seam are **exported functions on `window`**,
never direct DOM writes into the other block:

| Direction | Function | Defined in | Called by |
|-----------|----------|-----------|-----------|
| open the popup | `window.openFeaturesPopup()` | U4 | U3 |
| report option counts | `window.uc_updateOptsIndicator(has, total)` | U3 | U4 |
| force-close the popup | `window.closeFeaturesPopup()` | U4 | U4's `uc:step` listener |

`openFeaturesPopup()` takes no arguments: U4 already holds the last car passed to
`updateFeatures(car)`. The popup rebuilds its contents on every open and does **not**
live-update while open — a configuration change behind the overlay is picked up on
the next open.

### Global Variables (declared in U2, shared across all blocks)
- `skuId` — composite ID sent to messenger bot on form submit
- `globalModelUtm` — current model's UTM slug (set by `selectComplectation`)
- `globalModeX` — current model name
- `globalFullCompl` — current complectation name
- `waMessenger` — messenger choice, set by U6's calc section (`'Telegram'` / `'MAX'` / `'web'` / `''`)
- `waSend` — whether to open messenger after form submit
- `allComplectations` — full loaded dataset, exposed for cross-block access
- `window._mainReady` — Promise that resolves when `allComplectations` is populated
- `window._swiperReady` / `window.UC_SWIPER_SOURCE` — Swiper is loaded by U2's loader, not a static tag: sources tried in order of `SWIPER_SOURCES` (since plan `2026-10-12-001`: the production bot `…/widget-custom/vendor/swiper/11.2.10/` first — bot 3.4.2, Russian hosting, immutable versioned addresses, the same address in both builds — then `cdn.jsdelivr.net`, then `fastly.jsdelivr.net`; if the bot is rolled back to 3.4.1 the address answers 404 and the loader moves to `cdn` at once. The loader listens with `addEventListener('load' / 'error')`, never `onload`/`onerror`: Tilda's `tilda-fallback-1.0.min.js` overwrites `onerror` of every `<script>` and `<link>` on the page when it loads and on `DOMContentLoaded`, with a function that does nothing for foreign hosts — with the property, a failed source waited for the 3 s timer (found 2026-10-12). With three sources on a slow path `fastly` would start at 6 s, i.e. never — the 6 s bound is kept deliberately, owner 2026-10-12). After 3 s the next source **starts in parallel** and the earlier one keeps loading: the first file to arrive wins; a load error moves on at once; 6 s in total (plan `2026-10-08-005` — on Slow 4G the primary delivers in ~3.0–3.1 s, and the old loader dropped it at 3 s and re-downloaded from the fallback, +1.4 s). A file arriving after the decision still executes and overwrites `window.Swiper`; the loader puts the chosen constructor back — the static one included. `integrity` on script and CSS, the CSS taken from the same source as the script. Resolves to the host it came from or `'static'` — then `window.Swiper` is a small static slider with the API U3/U6 use (arrows, thumbnails, model switching; no swipe, no animation). `buildPage()` awaits it together with the catalog, and wraps `initGallery`/`initCards` in `try/catch` → `catalogUnavailable()`, so the skeleton can never stay forever (plan `2026-10-08-002`). `new Swiper` is called only in U3 and U6, both from `buildPage()`
- `window._dealerConfigReady` — dealer settings (or `null`); `window._featuresReady`, `window._ratesReady`, `window._dealerReady` are derived from it, each resolves to data or `null`
- `window.UC_SITE` — the selected TEST/PROD values (`mode`, `botUrl`, `cabinetPath`, `isNgrok`, `blocked`)
- `window._botLinksReady` / `window.UC_BOTS` — Telegram and MAX bot names for the dealer's brand, `{ telegram, max }`, each a name or `null` (see «Messenger bots come from /bot-links» below)
- `ucLkSending` — set by U6 on every «Далее» click: `true` in the LK branch, where `mySuccessFunction` must neither reload nor open messengers
- `ucMsgMobile` — «open MAX through `max.ru`» (the name is left from its old meaning «phone», so the U2 build did not change). Set by U6 on every «Далее» click outside the LK branch: window width > `CALC_DESKTOP` (480 px, the same test that shows the «На сайте» card) → `false`; ≤ 480 → the visitor's choice in «Где открыть MAX» (U6's `calcMaxMode`: «Приложение» `'app'` → `true`, «WEB-версия» `'web'` → `false`). U2's `ucMaxUrl(bot, payload, mobile)` picks the MAX host by it. `calcMaxMode` starts as `'app'` and, like `calcMessenger`, is kept until reload: across Telegram and back, tab switches, re-entry to step 2 and `resize` (mobile browsers fire it when the address bar hides) — `initCalcState` only shows or hides the block (plan `2026-10-08-003`, owner decisions)
- `window.ucQuiz` — step machine: `{ step, matchingCars, selectedCar, goTo, next, back }`.
  `matchingCars` and `selectedCar` are written by U3 and read by U6.

## Architectural Decisions

### `var` and `window.*` instead of `let`/`const` for cross-block globals
Tilda concatenates all T123 block scripts into a single execution context. `let`/`const` are block-scoped and would be confined to each block's IIFE. `var` declarations hoist to function scope; cross-block globals are explicitly set on `window`.

### `waMessenger` stored in global variable
Tilda clears hidden form fields before the success callback fires. The messenger choice (set by U6's calc section) must survive the form submission cycle — storing it on `window` (not in a form field) ensures it's available in `mySuccessFunction`.

### Quiz: three steps, U3+U4 are one screen
`uc:step` carries `step: 0` for Hero (a native Zero Block, shown and hidden by U1), `step: 1` for the car-selection screen (U3's configurator, with U4's popup reachable from it), and `step: 2` for U6 (gallery + form). `ucQuiz.goTo` rejects anything outside `0..2`, and `uc:ready` enters at `goTo(0)`. Four blocks listen for `uc:step` — U1, U3, U4, U6 — and U2 is the dispatcher, not a listener. A mismatch in any one listener silently breaks the whole quiz, so the bound in `goTo` and every `e.detail.step === N` comparison must be changed together.

**The Hero button works before the page is ready** (plan `2026-10-08-004`). It is visible 2.5–3.1 s before `uc:ready` on a fast network and 4–5.4 s on Slow 4G (measured 2026-10-08, before plan `2026-10-08-005`: dealer settings and the catalog were requested only after `DOMContentLoaded`, one after the other, each behind an OPTIONS preflight; the plan's estimate after it is ≈ 1.0–1.6 s fast / 2.4–3.5 s Slow 4G — the after-measurement is in the handoff). A click in that window opens step 1 with U3's skeleton, and the cards are built in place. On `uc:ready`, U2 goes to Hero only if the visitor is still on step 0 or the catalog is unavailable (`catalogUnavailable()` sets `_catalogFailed` first); otherwise it re-dispatches the current step with `scroll: false`. U1's `$(document).ready` hides every quiz section and then repeats `goTo(step)` when the step is no longer 0 — without that, a click before `DOMContentLoaded` left an empty page with only the footer. U7 closes the contacts overlay only when the step number changes, so contacts opened before `uc:ready` stay open. Until 2026-10-08 `uc:ready` always went to Hero and sent such a visitor back. Harness: `npm run test:hero`.

Hero is never addressed by rec-ID: the native T809 block picks one Hero variant by UTM and **removes** the others from the DOM, so U1 finds it fresh on every use as "the `.t-rec` containing the entry button".

### Accent colour on steps 1 and 2 is `#51565C`
One accent covers the decorative band under the header (U1), the selected configuration tiles including their border and check glyph (U3), and both «ДАЛЕЕ →» buttons (U3 and U6). Hover on those buttons is `#686F77`.

The hover shade was derived by **lightness delta, not by ratio**: the old pair `#1a1a1a → #333333` differed by +9.8 HSL lightness points, so the new hover keeps that same delta while preserving the hue and saturation of `#51565C` exactly. Applying the old *ratio* (×1.96) would have produced `#9FA9B4` — a different colour, not "slightly lighter".

White text on `#51565C` measures **7.41:1** — WCAG AAA, but by a margin of only 0.41 over the 7:1 threshold. The previous `#1a1a1a` gave 17.4:1. The drop is accepted deliberately; treat any further lightening of this accent as a change that needs a fresh contrast check, because it will fall out of AAA. Hover `#686F77` measures 5.09:1 — AA, which is fine for a hover state.

Black elsewhere is deliberately untouched: footer, the `.bridge-bar` / `.uc-calc-bridge-bar` section ticks, «← НАЗАД» buttons, the active messenger card and contact-method tab, the agreement checkbox, pagination dots, and all body text remain `#1a1a1a` / `#111`.

### U4's popup is reparented into `<body>`
U4 has no visible page section: its `.t-rec` is hidden permanently on `$(document).ready`. The popup must be `position: fixed` (it cannot be `position: absolute` inside U3's card, because U3 and U4 are separate Tilda sections that know nothing of each other's height or position). But a `position: fixed` element inside a `display: none` ancestor does not render at all — so on init U4 moves `#uc-feat-popup` to `document.body` **before** hiding its own `.t-rec`. Order matters; hiding first would strand the popup inside a hidden subtree.

`z-index: 100000` (not the design export's `50`) — the export is a standalone demo page, whereas on a real Tilda page the popup must clear sticky headers and menus.

### Tilda forces `box-sizing: content-box` — our blocks must reset it
`tilda-grid-3.0.min.css` contains `*,*:before,*:after{box-sizing:content-box}`. The universal selector overrides the browser's UA default (which gives `<button>` and friends `border-box`), so it applies to our markup too.

Every value in `docs/designs/` was authored under the export's opening `* { box-sizing: border-box; }`. Porting the values without the reset leaves a trap: **any element with an explicit width and horizontal padding overflows its container on the live Tilda page while rendering correctly in a bare browser.** That is exactly how «ДАЛЕЕ →» ended up off-screen on mobile — `width: 100%` + `padding: 13px 32px` = container + 64px. It rendered fine locally because Chrome's UA stylesheet gives buttons `border-box`, masking it.

U3 and U4 therefore carry a `box-sizing: border-box` reset scoped to their own roots (`.uc-cards-block`, `#uc-skeleton`, `#uc-feat-popup`) — scoped so Tilda's own DOM is untouched. U6 is deliberately *not* reset: its layout predates this and was tuned under `content-box`.

When testing layout locally, inject `*{box-sizing:content-box}` first, or the bug will not reproduce.

### Never write `*/` inside a CSS or JS block comment
Block comments do not nest: a literal `*/` inside a comment closes it early, and the rest of the comment becomes code. It happened in U1 on 2026-10-06 — a header comment quoting «/*! … */» turned its tail into a CSS selector and silently killed the `#uc-chrome-wrap{display:none}` rule (and once more the same day in `tools/build-production.mjs`, where it was a syntax error). The build now fails if any block's CSS contains Cyrillic outside strings and `/*! */` comments — the usual symptom.

### CSS class names are page-global across blocks — scope shared names
Tilda concatenates every block's `<style>` into one document, so a class declared in two blocks is one selector, not two. At equal specificity the block that sits **lower on the page wins**, regardless of which block's markup the element belongs to.

This bit us twice with `.uc-quiz-nav`: U6 declared it with `justify-content: space-between` (and a comment claiming "дублируем из U3 — блоки независимы"), while U6's own nav markup actually uses `.uc-calc-nav`. The rule matched nothing in U6 and silently overrode U3's `flex-end`, pushing step 1's «ДАЛЕЕ →» to the left.

Rules for anything not deliberately shared:
- Scope block-local rules under the block's root (`.uc-cards-block .uc-quiz-nav`, `#uc-gallery-custom .uc-quiz-btn-back`), which also raises specificity to `(0,2,0)` and makes the block immune to a later block re-declaring the bare class.
- Declare every property explicitly in the scoped rule; don't rely on inheriting a value another block happens to set.
- If two blocks need the same visual component with different metrics (e.g. U6's calc heading reuses U3's `.bridge` look but has no 28px gutter), give it a distinct name (`.uc-calc-bridge-*`) rather than redeclaring `.bridge` with a different padding.

`.configurator` is the one intentionally shared class; U3 and U6 declare it with identical values.

### Tilda's own popups are closed, never removed
U2 closes whatever Tilda shows over the page before the MAX overlay and on returning to the tab (`ucCloseTildaOverlays`): a shown `.t-popup` loses `t-popup_show` and gets `display: none`, `body` loses `t-body_popupshowed` / `t390__body_popupshowed`; form success windows (`.t-form-success-popup`, `.t-form__successbox`) and `.t706__cartwin` are removed. **Never remove `.t-popup`**: T390 looks its popup up inside its own record on every click, and without the node `tilda-popup` calls `focus()` on `null` after it has already locked scrolling. Until 2026-10-08 the return handler removed `.t-popup`, so after any tab switch the «Политика» / «Согласие» links stopped opening until reload (diagnosis in `docs/handoff/2026-10-06-release-lk-tab.md`). U1's `healStuckTildaLock` stays as a backstop.

### `.uc-tildaform` class instead of ID
Tilda can duplicate pages or blocks. Using a class (not `#id`) prevents broken selectors when the form appears more than once in the DOM.

### Consent on step 2 is enforced by our own code, never by Tilda's validation
`ucConsentState()` in `u6-gallery.html` is the only thing that blocks a submit without consent. It runs inside the `#uc-gal-submit` handler **before every branch** — before the web-widget branch, before `waSend`/`waMessenger`, before `ucSetSending`, before `$submitBtn.click()` — so one `return` covers «По телефону», «В чате» and the Telegram/MAX variants at once.

| Fact | Where |
|------|-------|
| The consent check | `u6-gallery.html` — `ucConsentState()`, called from the `#uc-gal-submit` handler |
| The checkbox itself | Tilda form `.uc-tildaform`, field `name="Checkbox"` |
| `data-tilda-req="1"` on that checkbox | Tilda editor, page `page154296216` — **backstop only** |

**Do not delete the check on the grounds that "Tilda already blocks it".** Tilda's apparent block was an accident, not a rule: the checkbox carries no `data-tilda-req`, and `js-tilda-rule` alone does not make a field required. It blocked submission only because it was the **single** validated field in the form, so unchecking it made Tilda judge the whole form empty (`emptyfill`). Add any second `js-tilda-rule` field to that form and the old behaviour would have silently disappeared. The web-widget path never hit it at all, because the widget opened before the submit.

The editor's «Обязательное поле» flag is worth keeping, but it is a backstop: it cannot stop code paths that run before the submit. Full reasoning and the decompiled evidence: `docs/solutions/integration-issues/tilda-required-field-needs-data-tilda-req.md`.

### Validation errors and send failures are two different wires in U6
Validation (name, phone, consent) writes straight to `#uc-calc-error` — it works on both paths regardless of `_submitting`, and nothing needs unwinding because nothing was sent. Send failures go through `ucSendFailed()`, which also clears the «ОТПРАВКА…» spinner.

Keep them apart. `tildaform:aftererror` carries two unrelated meanings — Tilda dispatches it **synchronously** from `showErrors()` when its own validation refuses, and **asynchronously** from the XHR callback on a real failure. U6's listener answers it with the generic «Не удалось отправить заявку. Попробуйте ещё раз.», which is accurate *only* while consent is the form's single validated field and our own check guards it. **If a second `js-tilda-rule` field is ever added to the form, give it its own check next to name and phone** — otherwise that generic text will start standing in for a validation error again, which is exactly the bug fixed on 2026-09-18.

### Step 2 «В личном кабинете»: send flow and its seams with Tilda and U2
The second tab of step 2 (U6; «В чате» is first and open on entry since plan `2026-10-08-001`). «Далее» sends the **same Tilda form** with `offer` = «ЛК с сайта», `form_type` = «В кредит» / «В трейдин» / «Комбо» and a lead code `dealer_id + Model id + Complectation id + 4 random letters` in `sku-wa`, and **at the same moment** calls `POST {bot}/cabinet/api/web/open` (brief L4b, 2026-10-03 — the bot does not wait for the Tilda deal; the code and the `form` block are known before either request). `cabinet_url` is followed in the same tab **only once the form has finished** (success or error) — leaving the page aborts the form XHR and the Tilda deal is lost. Form error + bot success → navigate anyway (the bot creates the deal from `form`); bot error → the banner. Contract: bot repo `docs/handoff/landing-brief.md`; plan `docs/plans/2026-09-30-001-feat-lk-tab-cabinet-web-entry-plan.md` (U7–U8).

- **Attempt = parameters** (car, scenario, payment, plate, mileage), kept **only in page memory** (owner decision). Unchanged retry reuses the code and nonce and does not resend the form; any change starts a new attempt and a new form (a new Tilda deal — allowed).
- **One 15 s deadline** from the click covers the form XHR and the bot call. If `cabinet_url` has arrived by then but the form is still pending, U6 navigates (same rule as a form error); otherwise one banner (frame 5) with «↻ ПОВТОРИТЬ» and «В чате» / «По телефону». A retry resends the form only if it never succeeded.
- **Warm-up (brief L9).** Once per page load, on entering step 2 (whatever tab is open — «В чате» is the default) or on showing the LK tab, once dealer settings have arrived, U6 adds in `requestIdleCallback`: two `preconnect`s to the bot origin (with `crossorigin` for the `web/open` fetch, without for the cabinet's plain `<script>`s); a `prefetch as=document` of the cabinet page — `cabinet_page_url` from the settings, else `UC_SITE.cabinetPath + '?src=web'` (Chrome reuses a prefetch only on an exact URL without `#`, and `cabinet_url` carries `?src=web`; bare `/cabinet_dev` misses — measured 2026-10-04); and, **only when the bot is not behind ngrok** (`UC_SITE.isNgrok` false, i.e. the `Production/` build), a `prefetch as=script` for each path of `cabinet_assets` **verbatim with `?v=`**. Through ngrok the prefetch receives ngrok's HTML warning page, Chrome then serves it to the cabinet instead of its scripts (`ERR_BLOCKED_BY_ORB`) — owner decision 2026-10-04: off in the test build. U2 passes the fields through as `cabinetAssets` / `cabinetPageUrl` (root paths only).
- **Measuring this in Playwright:** any `page.route` disables the browser cache, so a run that substitutes blocks via `route` shows no prefetch benefit at all. Call `page.unroute_all()` after the page has loaded and before the warm-up.
- **Tilda clears the form after success, asynchronously, after `aftersuccess`** (`successEnd` → success box → `t_forms__clearForm`). U6 snapshots every named field before the submit and restores them once `dealer_id` empties — otherwise «В чате» / «По телефону» after an LK attempt would send a form without the car. The same snapshot is the `form` block for the bot (all fields as sent, `FORMID` = the form's `id`, `utm_*`, raw cookies `_ct_session_id` / `_ym_uid` / `WhiteCallback_visit`; never `tildaspec-*`, `formservices[]`, `name`, `phone`).
- **`ucLkSending` (U2 global)** is assigned on **every** «Далее» click — `true` in the LK branch, `false` elsewhere. `mySuccessFunction` returns early on it (no reload, no messengers). Setting it only in the LK branch would make it stick like `isMaxOverlayActive` and hang «По телефону» in «ОТПРАВКА…».
- **`cabinet_url` is followed only if** it is `https:`, on this page's host and its path equals `UC_SITE.cabinetPath`.
- Form outcome is bound to the attempt in flight; no new form while `.t-submit` still has `t-btn_sending`.

**Known behaviour — «Назад» from the cabinet does not always restore the page.** Returning restores the landing from the back/forward cache only when Chrome kept it there; then the attempt is still in memory and «Далее» reuses the code and nonce. Chrome sometimes refuses with `notRestoredReasons` = `outstanding-network-request` — a request started on leaving the page (most likely a statistics script on `pagehide`; at the moment of navigation only the cabinet document itself was pending). Measured 2026-10-02: 5 of 7 returns restored. On a miss the page reloads, the attempt is gone, and the next «Далее» creates a new attempt and a **second Tilda deal**. Accepted by the owner (requirements allow duplicate Tilda deals); do not "fix" it by persisting the attempt in `sessionStorage` without a new decision. Note that Playwright disables bfcache by default (`--disable-back-forward-cache`) — test this in real Chrome with that flag removed.

### Gallery: `swiper` class removed from container (`#uc-gal-swiper`)
The container originally had `class="swiper uc-gal-swiper"`. Tilda scans for `.swiper` elements and auto-initializes its own Swiper instance on them, creating two competing instances on the same DOM element. Touch events were handled by Tilda's instance (no listener registered), while arrow buttons called methods on our `galSwiper` variable. Removing the `swiper` class prevents Tilda's auto-init; our `new Swiper('#uc-gal-swiper', {...})` by ID continues to work.

### Gallery: loop disabled (`useLoop = false`)
`loop: true` in Swiper 11 triggers `loopCreate()` during init and requires `loopFix()` on touch boundaries. Combined with the destroy+recreate pattern used when switching complectations, this caused `slideChangeTransitionEnd` to stop firing (the loopFix transition consumed the `transitionend` listener). `loop: false` keeps navigation linear (first→last without wrap-around) and eliminates the event-firing issue entirely.

### U4: `TABLE_FEATURES` ID must never be hardcoded in U4
U4 previously had its own `var TABLE_FEATURES = '...'` and `fetchFeaturesTable()`. On a different project (e.g. Geely vs Haval), U2 loads a different table ID. U4's hardcoded fetch would silently load the wrong project's features, causing all equipment options to fall into "Прочее" (no category match). Fix: U4 was stripped of `TABLE_FEATURES`, `fetchFeaturesTable()`, and the Case 1 fallback that called it. (The hardcoded `var TABLE_FEATURES = 'mm8wldgk5vn9gwh'` and `fetchFeaturesTable()` are still present in the `Ver_1/` and `Ver_2/` snapshots — that is where the pattern came from; it was stripped in the current working version.) U4 now relies exclusively on `window._featuresReady` (set by U2 from the features table id in the dealer settings), through a single `withCache(cb)` helper covering all three cases:
- **Case 3**: `_featCache !== null` → invoke `cb` synchronously
- **Case 2**: `_fetchPromise !== null` (set in `$(document).ready` from `_featuresReady`) → await in-flight
- **Case 1**: `$(document).ready` hasn't fired yet when U3 calls `updateFeatures` → attach to `window._featuresReady` directly

`cb(ok)` receives `ok = false` when the table never arrived; callers then blank the options indicator and show "Нет данных об оснащении" in the popup rather than rendering `NaN`.

### The dealer is chosen by an explicit `Dealer_Code`, never by "the first one with coordinates"
The bot resolves the single row by the code from the form and returns it in `dealer`; U2 exposes it as `window._dealerReady` — one object, not a list. U7 consumes it and chooses nothing.

`Dealers` is the UnifiedBot tenant-configuration table shared by every site, not a directory of map pins. `Dealer_Code` is unique across all rows and never empty; `Dealer_id` is not — in NocoDB it is a slug that repeats between rows and is null in most of them. **Never select the row by `Dealer_id`.** The form field is named `dealer_id` but carries `Dealer_Code`, deliberately — do not "fix" the name.

On the Кабинет page (`page222565209`) there is no U2; U7 makes the same `/cabinet/api/dealer/{code}` request itself, with the bot URL from `window.UNIFIEDBOT_URL` set by the `#cabinet-root` block. On the quiz page U7 makes no request of its own. Multi-site (one page, many dealers) is deliberately not designed for.

### Per-site settings: what to change when moving to another site

| Setting | Where |
|---------|-------|
| Dealer code | hidden field `dealer_id` of the Tilda form `.uc-tildaform` — the only per-dealer setting on the site |
| Everything else per dealer (tables, dealer card, `brand`/`dealer`/`website`/`letter_title`/`form_type`) | Dealers table in NocoDB, delivered by the bot |
| «ДАЛЕЕ →» colour | **one line at the very top of U1** — `<style>:root{--uc-btn-next-bg:…;--uc-btn-next-bg-hover:…}</style>`, the first `<style>` of the build. The hint comment above it lives **only in the source** (an ordinary `/* … */`, which the build drops): owner decision 2026-10-08 — no comments about the internals in production code, so the build also fails on any `/*! … */` left in any block. The build is one file for every site, so the owner edits the two values **when pasting U1** (default `#2D5467` / `#3C728D`). The build recognises the line by its shape — right after `<!-- nominify begin -->`, a `<style>` holding only that `:root` with the two default values — and refuses to finish if it is missing or altered, or if any other block declares the colour; `check-live` ignores the values (and any comment before them, e.g. an old hint or an emptied `/**/`), prints them, and warns if a page still carries the old hint |
| Telegram / MAX bots | from the bot by the dealer's brand: `GET {bot}/bot-links` (see below) |
| Test vs production (bot URL, cabinet path) | the two value sets in the «ТЕСТ / БОЙ» block at the top of the U2 `<script>`. **The mode is chosen by the build, never by hand:** `tools/build-production.mjs` substitutes the build constant `UC_BUILD_MODE` (terser `global_defs`) and drops the other set as dead code — `Production/` carries only production values, `Production-test/` only test values. The raw source behaves as test |

The test build works only on `UC_TEST_HOSTS` (demo.autodc.org); pasted on any other host, U2 makes no bot API requests and logs a configuration error — it still loads the immutable Swiper and Tabler files from the production bot's `/widget-custom/vendor/` (plan `2026-10-12-001`). U3/U6 read the colour as `var(--uc-btn-next-bg, #2D5467)`, so a page without the U1 line gets the dark blue. No colour flash on load (measured 2026-10-06): the default exists ~30 ms, only while steps 1–2 are still hidden.

Current colours: haval-demo-new and the test page — `#6ACEC3` / `#5BB0A7`; offer.geely-radar.ru and sale.belgee-radar.ru — `#2D5467` / `#3C728D`. White on `#6ACEC3` is 1.87:1 (below AA) — the owner's choice; any new production colour needs a contrast check.

#### Messenger bots come from `/bot-links`, by the dealer's brand
U2 requests `GET {bot}/bot-links` in parallel with the dealer settings (`{ bots: [{ channel, brands, link }] }`). The brand is `form_values.brand` in lower case; a record whose `brands` lists that brand wins over a `"*"` record. Only `https://t.me/<name>` / `https://max.ru/<id>` links are accepted. The jump format: Telegram — `tg://resolve?domain=<name>&start=<payload>`, then `https://t.me/<name>?start=<payload>` after 1.5 s; MAX — `https://web.max.ru/<id>?start=` + `encodeURIComponent(payload)` on a computer; on a phone (≤ 480 px) the visitor chooses in «Где открыть MAX»: «Приложение» (default) → `https://max.ru/<id>?start=…`, «WEB-версия» → `https://web.max.ru/…` (same path and start); by `ucMsgMobile` (plans `2026-10-08-001`, `2026-10-08-003`; tablets and phones in landscape are wider than 480 px, get no choice and get `web.max.ru` — owner decision). The fallback sheet's button gets the same address. Its text depends on the host: for `max.ru` it is «📱 Открыть в MAX» / «Для перехода в приложение MAX нажмите на кнопку»; for `web.max.ru` it is «🌐 Открыть MAX в браузере» / «Для перехода в MAX нажмите на кнопку» (owner, 2026-10-08). The jump stays in the same tab.

Telegram and MAX are mandatory for every brand (owner, 2026-10-06): if `/bot-links` fails or has no record for the brand, U2 logs `⛔` to the console and the cards stay as they are — no fallback (in particular, no «На сайте» on mobile). On a send to a channel without a link the lead still goes out, there is no jump, and the page reloads as in «По телефону». Production bots today: Haval — `havalconsultantbot` / `id781424127394_2_bot`; Geely and Belgee — `vashassistentbot` / `id781424127394_bot`; the dev bot answers `"*"` with the test bots.

Still hardcoded and deliberately out of this list: the `#51565C` accent (band in U1, selected configuration tiles in U3). Moving it needs a fresh contrast check first — see the accent section above.

### CSS custom properties: generic names get scoped, prefixed page-wide ones go on `:root`

Tilda concatenates every block's CSS into one document, so a custom property declared on `:root` is page-global. Both practices in this project are the same rule applied to different names:

| Name | Declare on | Example |
|------|-----------|---------|
| Generic (`--accent`, `--ink`, `--line`) | the block's own root | U7's palette, scoped under `#uc-contacts-popup` — any block declaring its own `--accent` would otherwise silently repaint the contacts popup |
| Prefixed and genuinely page-wide | `:root` | `--uc-band` (U1, band height) and `--uc-btn-next-bg` / `--uc-btn-next-bg-hover` (U1's first line, button colour, read by U3 and U6) |

Paste order does not matter: custom properties resolve at computed-value time, not parse time, so a rule using `var()` that sits **above** the `:root` declaration in the concatenated document still gets the value.

### U4 does not build complectation tabs
Complectation choice lives solely in U3's `.trims-row`. `renderQuizTabs2` / `.uc-feat-tabs` were deleted, along with U4's self-init path (`uc_getDefaultSelection`, `_receivedExternalCar`) — that path existed only to render U4's now-nonexistent page section. `window.uc_getDefaultSelection` was removed from U3 in the same pass, since U4 was its only consumer.

### Configuration tiles vs complectation tiles: opposite semantics
Engine / transmission / drivetrain render the **full** option list; combinations that are unreachable get `.tile--unavailable` (grey, click ignored). Engine is never unavailable — every engine of the model is always selectable, and picking one cascades transmission then drivetrain to their first available value.

Complectations are the opposite: only the ones available under the current configuration are put in the DOM at all. There is no disabled state for a complectation. On a configuration change the previously selected complectation is preserved **by `Complectation id`** if it survives the new filter, otherwise the first is taken.

### Accordion U4: `max-height: none` instead of `grid-template-rows`
`grid-template-rows: 0fr → 1fr` transition was applied for GPU-composited animation (PageSpeed plan R7), but caused the accordion body not to collapse fully in some browser/OS combinations. Replaced with instant `max-height: 0 → none` (no transition) as the documented fallback. The popup's category accordion uses the same `.cat-body { max-height: 0 } / .cat.--open .cat-body { max-height: none }` structure, and categories toggle independently of one another.

## Workflow

### File locations
- Implementation plans: `docs/plans/`
- Brainstorm docs: `docs/brainstorms/`
- Block source files: `docs/tilda-blocks/`
- Documented solutions: `docs/solutions/` — past bug fixes and learnings by category; YAML frontmatter fields: `module`, `tags`, `problem_type`
- Handoff notes for the owner and the bot agent: `docs/handoff/`
- Build and checks: `tools/` (`npm run build:prod`, `check:prod`, `test:harness`, `test:visual`; `npm run measure:ready` — the Hero-to-`uc:ready` window in real Chrome, see «Where the time before `uc:ready` goes»; `python3 tools/check-live.py [URL --prod]`; `python3 tools/lk-states-shots.py` — screenshots of all LK-tab states into `docs/designs/uc-calc-lk-states/`)
- Domain vocabulary: `CONCEPTS.md` — shared project-specific terms (entities, processes, named concepts)

### Verification rule
After any `/ce-work` session, upload changed files to claude.ai for independent verification. **Never trust a CE self-report** — always confirm actual file contents by reading them directly.

### Deployment
**Into Tilda goes only a build: production pages — `docs/tilda-blocks/Ver_4_dev/Production/`, the test page — `docs/tilda-blocks/Ver_4_dev/Production-test/`.** Only U2 differs between them (its TEST/PROD values); every other block is byte-identical, and the build refuses to finish otherwise or if a foreign-mode value is left in U2. The one exception: the production bot host may appear in `Production-test/` U2 only in lower case and only when the path right after it starts with `/widget-custom/vendor/` and holds no `..` segment and no `%`-encoding (Swiper and Tabler files, plan `2026-10-12-001`); any other occurrence — `/cabinet/api/`, `/catalog/`, the bare host, a port, another case — still fails the build. On every run (so also in `--check` and the hook) the rule checks itself on fixed strings (`checkForeignRule()`) and on the real U2 source: `checkForeignOnRealU2()` injects `/cabinet/api/dealer/`, `/catalog/x`, `/widget-custom/widget.js` and a vendor path into U2's first `<script>`, builds the test mode and expects the first three rejected and the vendor path passed. Sources in `Ver_4_dev/` keep the comments; both builds are made from them by `npm run build:prod` (`tools/build-production.mjs`: html-minifier-terser with terser for JS and clean-css for CSS). Two reasons: Tilda refuses to save a T123 block larger than **100 000 bytes** («Ошибка: слишком много текста»; the figure is from ticode.dev, Tilda's docs give none — and U6's 146 KB source was refused while 74.5 KB was accepted), and comments are visible in the page source. The build keeps top-level names (cross-block globals), strings, URLs and `<!-- nominify -->`; any JS/CSS parse error stops it.

- **Every change to a source is followed by `npm run build:prod`, and the commit carries the source and both builds.** `npm run check:prod` rebuilds both in memory and fails if either is stale; the pre-commit hook in `.githooks/` runs it (enable once per clone: `git config core.hooksPath .githooks`). Setup: `npm install`.
- **Harnesses run on the builds**: `npm run test:harness` — LK tab at 1000/768/390, smooth (trackpad-like) wheel over the tab bar at 641–1000 and the phone mask on `Production-test/`, dealer settings on both U2 builds (test on a test host, the test build blocked on a foreign host, the production build carrying only production values; requests leave before `DOMContentLoaded`, no `Content-Type` on any bot GET, form below U2 falls back to `DOMContentLoaded`). Needs the python `playwright` package.
- **Swiper late or missing**: `npm run test:swiper` (`tools/swiper-fallback-check.py`) — live haval-demo-new, Geely, Belgee with U2 swapped for `Production/`; the bot is the primary source: bot ok (Swiper and Tabler from the bot), bot delayed 0.5 / 2.5 / 5 s, bot +3.5 s with `cdn` +3 s (the late bot wins, no second download), all three +7 s (static at 6 s, the late files do not replace it), bot 404 / aborted / tampered (integrity → `cdn` at once), bot 404 + `cdn` aborted (→ `fastly`), all aborted (static mode: arrows and thumbnails), Tabler from the bot 404 / +5 s / tampered (→ jsDelivr), the bot rolled back to 3.4.1 (404 on all of `/widget-custom/vendor/`), Tilda's `onerror` override (`tildaOverride`: every new `<script>`/`<link>` gets an empty `onerror` before the bot's 404 arrives — the move-on must still be immediate), a negative control with Tabler unavailable everywhere, `initCards` throwing; in every scenario the slider arrows of U3 and U6 must carry the Tabler glyph (U3 hides its arrows at ≤ 4 models — Belgee — so there only the glyph is checked); 1280 and 390 px. Run one scenario: `python3 tools/swiper-fallback-check.py botOk`. Needs the network, ~10 min; not part of `test:harness`.
- **Hero button before `uc:ready`**: `npm run test:hero` (`tools/hero-early-click-check.py`) — live haval-demo-new, Geely, Belgee with U1, U2, U7 swapped for `Production/` (U1 keeps the page's colour). Catalog +3 s, Swiper +5 s, both Swiper sources aborted, catalog 500. The click comes before `DOMContentLoaded` (a synthetic click), after it, after the catalog, and after `uc:ready`; there is also «КОНТАКТЫ» before `uc:ready`. Runs at 1280 and 390 px; on haval-demo-new at 390 px the tap goes to the left quarter of the button, because the T887 cookie banner covered its centre (measured 2026-10-08). Needs the network, ~6 min; not part of `test:harness`.
- **Visual check of the build**: `npm run test:visual` — the live test page with all six blocks swapped for the sources vs for `Production-test/`, key screens (step 1, equipment popup, step 2 with the LK / chat / phone tabs, contacts) at 1000 and 390 px, compared pixel by pixel; any difference fails. Run it after every change to the build tooling and before pasting. It caught the inline-style loss below. Needs the dev stand up (data) and Pillow.
- `<!-- nominify begin -->` / `<!-- nominify end -->` instruct Tilda's minifier to skip the block content.

Because the paste is manual, the working copy and the live page drift silently. **Never state what is or is not deployed from memory — check:** `python3 tools/check-live.py` compares each `Production-test/` block with the test page in 200-character chunks (`ok` = every chunk present); for a production page: `python3 tools/check-live.py <URL> --prod`.

The dev page is `https://demo.autodc.org/page154296216.html` (Tilda `page154296216`). Blocks there: U2 `rec2472574541`, U6 `rec2472574441`, U7 `rec3792600201`. The pilot production page `https://demo.autodc.org/haval-demo-new`: U1 `rec3912992401`, U2 `rec3912976101`, U3 `rec3912822401`, U4 `rec3912822501`, U6 `rec3912822601`, U7 `rec3912862801`, form `rec2434366341`. The older exports under `new page/`, `inprogress page/` and `sourse page/` belong to a different Tilda page (`page148433256`) and their rec-IDs do not apply.

#### Checklist: going live on a production page
- [ ] The site needs blocks built **after** plan `docs/plans/2026-10-06-002-feat-bots-by-brand-and-page-settings-plan.md` (bots by brand from `/bot-links`): older builds send every brand to the Geely/Belgee bots, so a Haval site must not get them.
- [ ] `GET {bot}/bot-links` has a Telegram and a MAX record for the dealer's brand (`form_values.brand`).
- [ ] **U1: set the site's button colour** in the first line of the block before saving (`--uc-btn-next-bg` and `--uc-btn-next-bg-hover`; default `#2D5467` / `#3C728D`); the line has no comment — change only the two values. Paste U1 and U2 together — U2 no longer sets the colour.
- [ ] If the dealer shows a subset of the catalog: the `Catalog_Active_Field` column in Dealers names its activity column (the bot sends it as `tables.catalog.active_field`; empty → `Active`). Never edit the filter in U2 on the page.
- [ ] The bot on production serves `/cabinet/api/dealer/{code}` and `/cabinet/api/web/open`; the dealer's `Website_URL` is in its whitelist.
- [ ] Paste files from **`Production/`** only (never `Production-test/`, never sources). Do not touch the mode by hand — it is set by the build. In the browser console `UC_SITE.mode` must read `"prod"`.
- [ ] The form's `dealer_id` on the production page is the production dealer's code.
- [ ] U2 and U6 are pasted together (U6 without the matching U2 gets a page reload in the middle of opening a cabinet session).
- [ ] U7 on the Кабинет page — only once the production bot serves dealer settings.
- [ ] Every block is under the T123 limit (the build prints the table).
- [ ] `python3 tools/check-live.py <URL> --prod` shows `ok` for every pasted block.
- [ ] After the bot release with the cabinet files: on the production page, step 2 with the LK tab, check that the `prefetch` of each `cabinet_assets` script returns JS (not HTML) and that the cabinet page takes those scripts from the prefetch cache (DevTools → Network: «(prefetch cache)»). Until that release the production bot answers 404 for them.

The test page `page154296216` takes files from **`Production-test/`**; `python3 tools/check-live.py` (no arguments) verifies it.

## PageSpeed Status

- **Round 1**: completed
- **Round 2**: completed (plan `docs/plans/2026-06-21-002-feat-pagespeed-round-2-render-fixes-plan.md`)
  - R1: Swiper + Tabler CSS non-render-blocking — Tabler via `preload` + `onload` (since plan `2026-10-12-001` from the bot with `integrity` / `crossorigin`; an `error` listener added by U2's script switches the link to jsDelivr once (the inline `onerror` stays only for an error before that script runs), and if the bot has not answered in 3 s U2 puts a jsDelivr stylesheet right after it); Swiper CSS (since plan `2026-10-08-002`) is a `<link>` inserted by U2's loader next to the script, from the same source
  - R2: `widget.js` deferred
  - R3: Swiper pinned to `@11.2.10`
  - R5: shimmer skeleton uses `transform: translateX` (compositor-safe)
  - R6: accordion shimmer added
  - R7: accordion fallback (`max-height: none`)
  - R8: progress bar uses `transform: scaleX` (compositor-safe)
  - R9: accordion icon `<img>` has explicit `width`/`height` attributes

- **Remaining blocker**: jQuery + Tilda's native CSS (~1600 ms render-blocking) — outside project control
- **R4 deferred**: `Cache-Control` headers — to be implemented in `main.py` of the bot project, not in Tilda blocks. Brief for `/catalog/{table}` and `/bot-links` (owner, 2026-10-09: next bot release, `max-age=300` with `stale-while-revalidate` and `ETag`): `docs/handoff/2026-10-09-bot-brief-cache-headers.md`; `/cabinet/api/dealer/{code}` already sends `max-age=300`. Originally scoped to `widget.js`; measurement on 2026-09-19 showed **`/catalog/{table}` and the photo responses carry no cache headers either** — no `Cache-Control`, no `ETag`, no `Expires`. Repeat visits re-fetch data that has not changed. Gzip on `/catalog` does work (22 KB vs 243 KB).

### Where the time before `uc:ready` goes (measured 2026-10-08)

From the Hero button appearing to `uc:ready`, first visit: 2.5–3.1 s on a fast network, 4.0–5.4 s on Slow 4G; on a repeat visit within 5 min only the catalog request remains (0.34–1.39 s). Almost all of it was the chain of bot requests, each step one RTT (~0.33–0.4 s from here): waiting for `DOMContentLoaded` (0.23–1.0 s), an OPTIONS preflight per request because of `Content-Type: application/json` on a GET, a new connection for the first `fetch` (~0.62 s — the HEAD preconnect without `crossorigin` serves only `<script>`s), settings → catalog in sequence; on Slow 4G also a second Swiper download. Plan `docs/plans/2026-10-08-005-perf-faster-ready-plan.md` removed the landing's part: requests at U2 execution, GET without headers on the production build (the ngrok header keeps preflights in the test build), the parallel Swiper fallback. Outside the blocks: a `preconnect … crossorigin` to the bot in each site's HEAD (owner, done 2026-10-09) and cache headers on `/catalog` and `/bot-links` (bot, brief `docs/handoff/2026-10-09-bot-brief-cache-headers.md` — written, **deliberately not handed over** for now: owner, 2026-10-09).

After publication (2026-10-09, live pages, 24 conditions × 3 runs): first visit 1.64–1.69 s on a fast network (was 2.5–3.1), 3.1–4.2 s on Slow 4G (was 4.0–5.4); repeat visit unchanged (0.39–1.31 s — the catalog request alone). Geely on Slow 4G (4.1–4.2 s) stays slowest: its large catalog shares bandwidth with Swiper and the features/rates tables — the deferred fix is to request features and rates after the catalog. The HEAD preconnect only helps when the connection is ready before U2 runs; on loads where Tilda's static files are cached it often is not (measured in a normal Chrome: 397–1079 ms for the settings request). The bot sends no `Timing-Allow-Origin`, so connection timing of its requests is hidden in Resource Timing (adding it is deferred, diagnostics only).

Measure with `npm run measure:ready` (`tools/ready-waterfall.py`: CDP with preflights, page marks, 3 pages × 1280/390 × fast/Slow 4G × cold/warm; `--build` swaps U2 for `Production/` via `page.route`, which disables the cache and makes preflights nearly free — compare variants within one series only). Chrome under automation ignores `preconnect` entirely; check it in a normal Chrome (DevTools → Network → Timing of the settings request: no «Initial connection»).

### Where the time on step 2 actually goes (measured 2026-09-19)

Asked to diagnose "the gallery got slower", measurement found no regression. The breakdown, to first photo on a cold cache:

| Stage | Time |
|-------|------|
| Tilda bootstrap before any data request fires | ~1790 ms |
| `/catalog/{TABLE_MAIN}` response | 620–830 ms |
| First photo request starts | ~2400 ms |
| **All blocks' synchronous work on the 1→2 transition, combined** | **8–10 ms** |

Block code accounts for a fraction of a percent. **Before blaming a recent edit for slowness, measure this split** — and note that the same version varies by ~1.2 s between runs depending on whether Tilda's static assets come from cache, which is an order of magnitude more than any code change here has produced. Method, the A/B-validation step, and the instrumentation trap that nearly produced a false finding: `docs/solutions/developer-experience/gallery-slowness-is-bootstrap-and-backend-not-block-code.md`.
