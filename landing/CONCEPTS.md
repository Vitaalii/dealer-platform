# Concepts

Shared domain vocabulary for this project — entities, named processes, and status concepts with project-specific meaning. Seeded with core domain vocabulary, then accretes as ce-compound and ce-compound-refresh process learnings; direct edits are fine. Glossary only, not a spec or catch-all.

## Car Catalog

### Complectation
A specific car trim-level configuration available in the dealership's catalog. The primary data entity in the project — each record represents one complectation with its model name, modification label, price, feature set, and gallery photos. Multiple complectations share a Model and may share a Modification.

### Model
A car model grouping (e.g. Haval F7, Haval Jolion). Groups one or more Complectations. Identified in the URL via a UTM slug and used to pre-select the configurator on page load.

### Modification
A sub-grouping within a Model (e.g. engine variant or drivetrain). Groups one or more Complectations that share the same modification label.

## Configurator Quiz

### Quiz Step
One of the three screens the visitor moves through. Exactly one is in the page flow at a time; the others are hidden.

- **Step 0 — Hero**: the landing screen, a native Tilda Zero Block rather than a custom block. Its call-to-action advances to step 1.
- **Step 1 — «Выбор Автомобиля»** (U3, with U4's popup reachable from it): the model carousel, the configuration tiles (engine → transmission → drivetrain), the complectation tiles, and the options indicator. «Показать →» opens U4's equipment popup over this screen; «ДАЛЕЕ →» advances to step 2.
- **Step 2 — «Ваш Выбор»** (U6): the photo gallery, the selected car's specs, and the contact form.

Navigation dispatches a `uc:step` browser CustomEvent from `window.ucQuiz.goTo(n)`; the blocks each listen and show or hide themselves accordingly. U4 never shows a section of its own — it listens only to force-close its popup when the visitor leaves step 1. Switching a step resets the document scroll to the top of the new screen; a screen may still be taller than the viewport and scroll within itself.

An Overlay is not a step: opening or closing one leaves the current step unchanged.

Was three steps of a different shape until the configuration and equipment screens were merged into step 1 — the equipment accordion became a popup rather than a screen of its own — and Hero was later folded in as step 0.

### Messenger
The contact channel a visitor selects in the step 2 contact form: «На сайте» (web chat widget), MAX (messaging app bot), or Telegram bot. The selection is preserved across form submission and used by the post-submit callback to open the appropriate channel or initiate a redirect.

### Cabinet Web Entry
The step 2 tab «В личном кабинете»: the visitor picks a purchase scenario (credit, trade-in, or both) and its parameters, the landing sends the Tilda form with source «ЛК с сайта», then asks the bot to "open a session" and follows the returned cabinet address in the same tab. Unlike the other two tabs, it hands the visitor to the client cabinet rather than to a messenger or a callback.

### LK Attempt
One try at Cabinet Web Entry, identified by its parameters (car, scenario, payment, plate, mileage). An attempt carries one lead code and one nonce; retrying the same attempt reuses both and does not resend the form, while changing any parameter starts a new attempt.

### Dealer Settings
The per-dealer configuration (catalog, features, rates and other table ids, plus public dealer fields) that the landing fetches from the bot by the dealer code in the form's hidden `dealer_id` field. The Dealers table in NocoDB is its single source; the site itself sets only the code.

## Technical Infrastructure

### Overlay
A full-viewport panel of our own that opens above the current Quiz Step without changing it — the equipment popup and the contacts popup are the two that exist. Distinct from a **native Tilda popup**, which belongs to Tilda's own block machinery and follows none of the rules below.

An Overlay is moved into the document body before its own section is hidden, because a fixed-position element inside a hidden subtree does not render. Overlays stack, and the browser back button closes only the topmost one. Scroll locking is not owned by the Overlay that opened it: a single owner recomputes the lock from whether any Overlay is currently open, so closing one while another is still open cannot release the page. Native Tilda popups lock scroll through a separate mechanism of their own, which that owner deliberately does not touch.

### Tilda Block
A standalone HTML file (CSS + HTML + JS in one file) deployed by pasting into Tilda's T123 custom block editor. No build step; Tilda minifies the content unless wrapped in `<!-- nominify begin/end -->`. Because Tilda runs all blocks in a shared page context, cross-block communication uses explicit `window.*` globals — `let`/`const` variables are scoped to each block's IIFE and are not visible to other blocks.
