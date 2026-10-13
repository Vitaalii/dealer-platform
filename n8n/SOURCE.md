# Источник снимка: n8n/

| Поле | Значение |
| --- | --- |
| Исходный проект | n8n-оркестрация бота (Alex_refactoring), GitHub `Vitaalii/unifiedbot-n8n` |
| Локальный путь | `DEV/BOT/N8N/Alex_refactoring/` |
| Ветка | `feat/cabinet-web-entry` (есть в GitHub: `origin/feat/cabinet-web-entry`) |
| Коммит | `b78884e48f18be10719bb9b7b38fc8be1cac6de2` (2026-10-09 14:42 +0800) |
| Способ | `git archive b78884e` — без истории, без незакоммиченных и неотслеживаемых файлов |
| Дата снимка | 2026-10-13 |

Рабочая (боевая) версия — эта ветка: сверка с боем 13.10 — 10 из 10 боевых воркфлоу совпадают.
**`main` в этой папке старого репозитория не является рабочей версией** (2026-08-13, отстаёт на 72 коммита).
Более поздний локальный коммит `5a0641f` не взят: в нём план с данными клиента.

## Что исключено и почему

| Путь | Причина |
| --- | --- |
| `main.py` | старая копия кода бота |
| `tests/test3.md` | старый лог с текстом диалога |
| `eval/dialogues_archive.json`, `eval/golden_set.jsonl`, `eval/baseline_report.json` | реальные диалоги клиентов и отчёт по ним |
| `doc/Dialogues_Archive_exported.csv`, `doc/Dialogues_Archive.numbers` | выгрузка архива диалогов |
| `flow/archive/`, `flow/backups/` | старые копии боевых воркфлоу; кроме двух файлов ниже |

Из `flow/archive/2026-07-05-baseline/` оставлены `Tool - Car Search.json` и `Tool_ Combo Calculator.json` —
их читает тест `flow/lib/pipeline_sim.test.mjs`.

Не попали как неотслеживаемые: `.env`, `.claude/settings.local.json`, `__pycache__/`, `.pytest_cache/`.

## Что изменено в снимке

- `pinData` заменён на `{}` в `flow/AI Agent v2.0 MAX.json`, `flow/TOOL_ AUTO.RU EVALUATION.json`,
  `flow/test-contour/[TEST] TOOL_ AUTO.RU EVALUATION.json` (в первом — данные реального пользователя).
  Генераторы `pinData` не читают и сами пишут `{}`.
- Ключ Auto.ru в заголовке `x-authorization` заменён на `__SET_IN_N8N_CREDENTIALS__` — 9 мест:
  `flow/TOOL_ AUTO.RU EVALUATION.json` (4), `flow/test-contour/[TEST] TOOL_ AUTO.RU EVALUATION.json` (4),
  `flow/archive/2026-07-05-baseline/Tool_ Combo Calculator.json` (1). **Ключ не перевыпускается**: в новой системе
  он подставляется из хранилища ключей (credentials) n8n, не из репозитория.
- `doc/N8N_ARCHITECTURE.md`: строки 22 (`session_id`) и 26 (`phone`) — заглушки `<SESSION_ID>`, `<PHONE>`.
- Адрес вебхука старого n8n заменён на `__OLD_N8N_WEBHOOK_URL_REMOVED__` в `nocodb/debrand_testruns.py` и
  `flow/lib/snapshots/testruns_after.json`.
- Адрес логов сессий старого n8n в `docs/handoff/2026-10-03-n8n-webhook-auth-bot-plan.md:80` — заглушка
  `__OLD_N8N_SESSION_LOGS_URL_REMOVED__`.
- **Госномера**, по виду реальные (14 номеров), заменены на вымышленные того же формата; регион и форма записи
  сохранены, номерам, которые есть и в боте, даны те же вымышленные, что в `bot/`. 184 места в документах и коде вне
  `flow/*.json`: `docs/plans/2026-09-08-001-…`, `docs/plans/2026-08-10-001-…`, `docs/handoff/2026-09-09-…`,
  `docs/handoff/2026-08-30-…`, `docs/SESSION_HANDOFF.md`, `docs/test-contour-checklist.md`,
  `docs/brainstorms/2026-07-16-…`, `flow/lib/plate_normalize.test.mjs`, `flow/lib/v3_pipeline_sim.test.mjs`,
  `flow/cabinet-web-entry/eval_webhook.test.mjs`, `flow/cabinet-web-entry/build_eval_webhooks.py`,
  `flow/cabinet-web-entry/README.md`, `flow/cabinet-web-entry/api/run_combo_once.py`,
  `nocodb/apply_plate_error_branches.py`. В тесте «U9» (`plate_normalize.test.mjs`) пересчитаны и массив кодов
  символов `0x…`, и коды `U+…` в комментарии. Плюс 14 мест в комментариях `jsCode` (см. ниже).
- В начало `CLAUDE.md` добавлена пометка «исторический снимок».

## Отличия `flow/*.json` от боевого n8n на 13.10

Учитывать при сверке `check_sources_vs_live.py` в новой системе. Код узлов (кроме комментариев) не менялся.

| Файл | Отличие |
| --- | --- |
| `flow/TOOL_ AUTO.RU EVALUATION.json` | комментарии с госномерами: «Code in JavaScript» стр. 30; «Code in JavaScript1» стр. 5, 24, 25, 32, 57; значение `x-authorization` — заглушка; `pinData` → `{}` |
| `flow/test-contour/[TEST] TOOL_ AUTO.RU EVALUATION.json` | то же |
| `flow/cabinet-web-entry/WEBHOOK_ Cabinet Tradein Eval.json` | комментарий с госномером: «Shape Response» стр. 21 |
| `flow/cabinet-web-entry/[TEST] WEBHOOK_ Cabinet Tradein Eval.json` | то же |
| `flow/AI Agent v2.0 MAX.json` | `pinData` → `{}` |

Оба `WEBHOOK_ Cabinet Tradein Eval.json` совпадают байт в байт с выводом генератора `build_eval_webhooks.py`.

Проверка после всех замен: `node --test` по всем 12 файлам `*.test.mjs` — 64 из 64; pytest
(`test_check_sources_vs_live.py`, `test_build_test_contour.py`) — 14 из 14; как в нетронутом снимке.

## Скрипты с записью во внешние сервисы

В новой системе запускать только с её адресами и ключами, по задаче владельца.

**n8n** (`flow/cabinet-web-entry/api/`): `apply_workflow_patch.py`, `build_session_logs.py`,
`create_channel_credentials.py`, `install_eval_webhooks.py`, `eval_webhook_switch.py`, `run_combo_once.py`,
`check_car_search_vs_reference.py`, `run_save_crm_test_once.py`.

**NocoDB** (`nocodb/`): `push_tables.py`, `add_intents_v2.py`, `add_v301_draft_blocks.py`, `add_needcheck_v303.py`,
`add_greeting_missingdata_v304.py`, `fix_credit_goal_v305.py`, `add_cabinet_v306.py`, `add_cabinet_tactic_v307.py`,
`apply_prompt_edits_v308.py` и обёртки `apply_prompt_edits_dedup.py`, `apply_prompt_edits_link.py`,
`apply_prompt_edits_marker.py`, `apply_prompt_edits_mirror.py`, `apply_prompt_edits_prinyal.py`,
`apply_prompt_edits_verbatim.py`, `cleanup_stale_notes.py`, `fix_need_check_ok_scope.py`,
`apply_cabinet_finalize_ban.py`, `apply_debrand_blocks.py`, `apply_plate_error_branches.py`.

**Косвенно до Битрикса:** `flow/cabinet-web-entry/api/run_save_crm_test_once.py`, `nocodb/debrand_testruns.py`.

## Ранбуки старого боя

`CLAUDE.md`, `docs/handoff/` и `docs/plans/` описывают боевую старую систему (versionId, бэкапы, выкатки).
К новой платформе не относятся. Хост старого n8n оставлен в исторических документах
`docs/handoff/2026-10-03-n8n-webhook-auth-bot-plan.md` и `doc/N8N_ARCHITECTURE.md`.
