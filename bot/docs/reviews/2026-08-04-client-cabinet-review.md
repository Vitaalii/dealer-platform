# Code Review — feat/client-cabinet (run 20260804-223027-846faae3)

Scope: `git diff 3349da9..HEAD`, 27 files, ~9175 diff lines. Branch `feat/client-cabinet`, HEAD `8fca2bb`.
Mode: `mode:agent` (report-only; caller applies fixes). Plan: `docs/plans/2026-07-31-001-feat-client-cabinet-plan.md` (plan_source: explicit).

Intent: клиентский личный кабинет поверх aiohttp-монолита — OTP-авторизация по индивидуальной ссылке с TOFU-привязкой номера (голос/SMS через ProstoSMS), анкета с черновиками и однократной отправкой, передача заявки в Bitrix существующим путём конверсии по трём триггерам с атомарным захватом, маскирование ПДн в логах, интеграция с ботом.

Review team (11): correctness, security, adversarial, testing, maintainability, project-standards, performance, api-contract, reliability, julik-frontend-races (conditional: async UI), ce-learnings-researcher, ce-agent-native-reviewer.

## Findings (validated)

### P0 — Critical

| # | File | Issue | Reviewer | Confidence |
|---|------|-------|----------|------------|
| 1 | `cabinet_service.py:1235` | CORS whitelist omits `X-Cabinet-Token` — cross-origin polling blocked | api-contract, security | 100 |

- **#1** — `cabinet_cors_headers` returns `Access-Control-Allow-Headers: "Authorization, Content-Type"`, but `app.js:548` sends the custom header `X-Cabinet-Token` on every `GET /auth/send-status` poll. A custom header makes the request non-simple, so the browser preflights it; in the real cross-domain deployment (dealer Tilda page → bot domain) the delivery-status polling loop never runs. Invisible to CI because the aiohttp test client does not enforce browser preflight. Fix: add the header to the whitelist and assert every header the frontend sends is allowed.

### P1 — High

| # | File | Issue | Reviewer | Confidence |
|---|------|-------|----------|------------|
| 2 | `cabinet_service.py:1561` | Cabinet Bitrix failures enqueue twice → duplicate timeline comments on replay | reliability | 100 |
| 3 | `cabinet_service.py:119` | Naive `utcnow()` vs `timestamptz` columns — every cabinet path raises on PostgreSQL | correctness, adversarial | 100 |
| 4 | `log_masking.py:94` | Masking filter skips tracebacks — phone and OTP leak via `exc_info` (R16) | security | 100 |
| 5 | `cabinet_service.py:1786` | `build_conversion_snapshot` (CRM payload assembly) never behaviorally tested | testing | 100 |
| 6 | `cabinet_service.py:1712` | Restart between handoff capture and conversion strands the lead forever | adversarial | 75 |
| 7 | `cabinet_service.py:1724` | Token issued before the deal exists never backfills `deal_id` — questionnaire never reaches CRM | correctness | 75 |
| 8 | `cabinet_service.py:564` | `otp_worker_tick` has no per-duty isolation — one failing duty blocks lead handoff | reliability | 75 |
| 9 | `app.js:408` | Request-code buttons lack a busy-guard — double OTP send; stale response reverts an authenticated screen | julik-frontend-races | 75 |
| 10 | `app.js:820` | Autosave has debounce but no in-flight lock — "Сохранено" can show for a non-final value | julik-frontend-races | 75 |

- **#2** — `BitrixService._request` already enqueues into `pending_sync` (debounced) after 3 failed attempts and returns `{}`; `_bitrix_call_or_enqueue` then sees `ok=False` and enqueues a second row via the nodebounce path. `replay_pending_sync` replays both without dedup: `crm.deal.update` is value-idempotent, but `crm.timeline.comment.add` posts two comments into the deal. Hidden from tests because every cabinet test injects a fake `bitrix_request`.
- **#3** — All cabinet time columns are `DateTime(timezone=True)`; production is PostgreSQL/asyncpg, which returns aware values, while `_utcnow()` is naive. Comparisons in `validate_bearer`, `_get_live_token`, and three `otp_worker_tick` duties raise `TypeError` → 500 on every `/cabinet/api/*` and a dead worker. SQLite returns naive values, so the whole suite passes.
- **#4** — The filter rewrites only `record.msg`/`args`; `Formatter` renders `exc_info`/`exc_text`/`stack_info` separately. Reproduced experimentally: `logger.exception` with a phone and `code=4821` in the exception text leaks both in cleartext. Reachable from several `except` blocks in `cabinet_service.py` wrapping DB/Bitrix/hook calls.
- **#6** — The capture `UPDATE ... SET handoff_at=now WHERE handoff_at IS NULL` commits, then several awaits later the conversion runs. A crash in that window leaves `handoff_at` set, and every trigger filters on `handoff_at IS NULL`, so the verified lead never reaches CRM and nothing reports it. No `handoff_done` marker or stale-capture sweep exists.
- **#7** — `prepare_cabinet_context` can issue a token with `deal_id=None`; reissue deliberately reuses the token's stored `deal_id` rather than fresh context, `rebind_cabinet_on_merge` only covers deal-hunt merges, and `attempt_handoff` reads `deal_id` before conversion without re-reading it after. `push_form_to_bitrix` then logs a warning and sends nothing.

### P2 — Moderate

| # | File | Issue | Reviewer | Confidence |
|---|------|-------|----------|------------|
| 11 | `cabinet_service.py:921` | Dealer daily-cap tech alert fires on every request after exhaustion — alert flood | security, adversarial | 100 |
| 12 | `cabinet_service.py:770` | R19 same-bundle reissue never checks derived token against stored hash — key rotation hands out dead links | correctness, adversarial | 100 |
| 13 | `cabinet_service.py:1207` | Dead `me()` alias — production callers use `get_me`, only tests call it | maintainability | 100 |
| 14 | `cabinet_service.py:1334` | Unbounded ints in questionnaire → `OverflowError` → 500 + tech alert per keystroke | adversarial | 75 |
| 15 | `cabinet_service.py:488` | Callback-triggered fallback that fails with `transport` can never retry (`delivery_state` already set) | adversarial | 75 |
| 16 | `main.py:3138` | `replay_pending_sync` not isolated — a DB fault skips cabinet duties for ~30 min | reliability | 75 |
| 17 | `cabinet_service.py:2041` | Cabinet context opens up to 5 DB sessions per chat turn | performance | 75 |

- **#15** — `process_status_callback` commits `delivery_state` before calling `trigger_sms_fallback`; on a `transport` failure the code clears `fallback_at` expecting a retry, but `_run_safety_fallbacks` filters on `delivery_state IS NULL`, so the row is permanently excluded. Side effect: `send_status` reports `sent` rather than `delivery_failed` because `channel` stays `voice`.

### P3 — Low

| # | File | Issue | Reviewer | Confidence |
|---|------|-------|----------|------------|
| 18 | `cabinet_service.py:69` | Client name interpolated into HTML dealer alerts without escaping | security | 75 |

## Dropped by validation (2)

- **Conversion failure swallowed with no retry** (reliability, P1) — refuted: `BitrixService._request` retries 3× and enqueues into `pending_sync`, which `sync_worker` replays with fatal escalation, so the lead is not lost. Narrowed residual: Signal B asserts "заявка передана в CRM" before delivery is confirmed.
- **`submitForm` leaves the autosave timer armed** (julik-frontend-races, P2) — refuted: `showCabinetScreen() → setScreen() → clearScreenTimers()` runs synchronously inside the promise handler and clears `timers.save` before the event loop can fire it; `formDirty` reset makes a stray call a no-op.

## Requirements Completeness (plan_source: explicit)

All ten implementation units (U1–U10) and the plan's R1–R41 have corresponding work in the diff; project-standards found no deviation from the plan's Key Technical Decisions (no-Alembic migration convention, strict-leaf cabinet modules, wiring-only `main.py` with source-grep pins, `ClientTimeout` discipline). Doc updates (`ARCHITECTURE.md`, `TECH_BREAKDOWN.md`, `CHANGELOG.md`) are unaddressed but explicitly listed under "Deferred to Follow-Up Work" — intentional, not a gap.

Unverifiable in-repo (external blockers per the plan): live ProstoSMS send test across four carriers (needs API key), PostgreSQL partial-index and timezone behavior (dev-Postgres run), Tilda page design and full UF field map, lawyer's consent text.

## Learnings & Past Solutions

`docs/solutions/` does not exist. Six documented lessons found in `AI_HANDOFF.md` / `docs/CHANGELOG.md` (single `request.json()` per handler; `ClientTimeout` as object not int; Atomic Background Sync — close the DB session before calling Bitrix; idempotent `ALTER` inside `init_db` instead of Alembic; pending_sync debounce policy; strict ID typing in background tasks). All six are honored by the diff. Recommendation: record the new decisions (nodebounce path R35, `CabinetEvent` idempotency registry, log-masking isolation) via `/ce-compound` after merge, and note the R35 debounce exception in `AI_HANDOFF.md` so a future reviewer does not read it as a violation.

## Agent-Native Gaps

- `cabinet_status` exposes only `not_visited` / `phone_confirmed` / `form_submitted`. "Requested a code but never confirmed" (the state Signal A already alerts the dealer about), "handoff already fired on timeout", and "locked after 5 wrong codes" are all indistinguishable from "never opened the link", so the bot agent cannot adapt the conversation or offer to take the phone in chat instead.
- `database.py:208-209` documents an event taxonomy (`link_opened`, `code_requested`, `delivery_failed`, `session_expired`) that is never written. Support cannot reconstruct whether a complaining client opened the link, how many rejected code requests they made, or whether SMS delivery failed.
- `CABINET_PROMPT_NOTE.md` gives no template reply for `form_submitted`, does not tell the agent to stop offering the cabinet after submission, and does not mention that dealer alerts already cover Signals A/B.

## Coverage

- Reviewers: 11 dispatched, 11 returned, 0 failed.
- Validation: 15 findings dispatched to independent per-finding validators, 13 confirmed, 2 refuted (listed above). 3 findings at anchor 100 verified directly by the orchestrator (dealer-cap alert, HMAC rotation, dead alias) per the P2/P3-anchor-100 rule.
- Suppressed below the confidence gate: 8 findings at anchor 50 (breaker probe-slot leak, `_record_event_once` check-then-insert race, `_rewrite_deal_refs` unfiltered scope, repeated-verify session survival, token-in-query fallback on send-status, unhandled `409 conflict` in `app.js`, submit-vs-merge race, form stranded under empty `deal_id`).
- Demoted to residual risks: "cabinet_service.py is 2262 lines, split it" (maintainability, P1/100) — no project standard codifies file size and the plan deliberately chose strict-leaf modules; kept as a structural note, not an actionable defect.
- Not independently validated (validator budget 15 exhausted): #17 (5 sessions per chat turn), #18 (HTML escaping in alerts), consent_text_hash provenance (P2/75), fallback restart window (P2/75).
- Test suite: 229 passing at review time.

## Residual Risks

- Signal B claims the lead was delivered to CRM before delivery is confirmed (narrowed from a dropped finding).
- `consent_text_hash` is computed server-side from the version string, not from the text the client actually saw — weak evidentiary value for consent records.
- Restart between the `fallback_at` capture and the SMS send shows the client "code sent by SMS" when none was sent; the safety net requires `fallback_at IS NULL`.
- `CabinetSession` rows and completed `CabinetOtp` rows are never pruned; `pending_sync` `fatal` rows are retained by design.
- Breaker, rate limiters, and alert dedup live in process memory — correct only for the single-process deployment the plan assumes; a restart mid-incident resets an open breaker.
- Dealer `Website_URL` changes apply only on bot restart; links already sent to the old domain die silently via CORS.
- `_record_event_once` is check-then-insert without a unique index — idempotency is probabilistic for future callers.

## Verdict

**Ready with fixes.** One P0 (cross-origin polling is broken in the real deployment) and four P1s that are invisible to the current SQLite-only, fake-injected test suite (PostgreSQL timezone crash, duplicate CRM comments, PII in tracebacks, stranded handoffs) must land before live traffic. Fix order: #1, #3, #2, #4, then #6/#7/#8, then the frontend races #9/#10, then the P2 tail.
