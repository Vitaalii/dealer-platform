# Источник снимка: bot/

| Поле | Значение |
| --- | --- |
| Исходный проект | UnifiedBot (бот), GitHub `Vitaalii/unifiedbot` |
| Локальный путь | `DEV/BOT/UNIFIEDBOT/MULTY/unifiedbot_dev_cabinet_2_0/` |
| Версия | релиз 3.4.4 — тег `v3.4.4` |
| Коммит | `727d6c1d02a9cce33277234b997f17c911565a4` (2026-10-12 19:53 +0800) |
| Ветка, где лежит тег | `feat/v3.4.3` (в `main` старого репозитория — v3.4.2, не 3.4.4) |
| Способ | `git archive 727d6c1` — без истории, без незакоммиченных и неотслеживаемых файлов |
| Дата снимка | 2026-10-13 |

Снимок совпадает с кодом боевого образа старой системы `multy-v3.4.4-k` (без `credentials.json`).

## Что не попало и почему

`git archive` берёт только файлы коммита. Поэтому не попали: `.env`, `credentials.json`, `google_key_dev.json`
(секреты), `venv/`, `node_modules/`, `__pycache__/`, `.pytest_cache/` (окружения и кэши), `data/`, `back_up/`
(данные и бэкапы), `docs/logs/` (логи боя с телефонами клиентов), `.claude/settings.local.json`,
неотслеживаемые `scripts/*_new.*` и `scripts/*_old.*`. Из дерева коммита ничего не исключено.

## Что изменено в снимке

- **Телефон клиента** (вероятно реальный) заменён на вымышленный того же формата: код оператора и длина
  сохранены, последние 7 цифр — вымышленные; согласованно во всех формах записи. 66 мест:
  `tests/test_phone_extract.py` (48), `docs/plans/2026-09-07-001-…` (6), `payload_v2.py` (5),
  `docs/plans/2026-09-07-002-…` (5), `docs/handoff/2026-09-09-observability-prostosms-delivery-handoff.md` (1),
  `docs/solutions/logic-errors/digit-window-parity-money-pairs.md` (1).
- **Второй телефон** в комментарии `main.py:4429` — так же, 1 место.
- **Третий телефон**, по виду реальный, в `tests/test_phone_extract.py` — так же, 3 места.
- **Госномера**, по виду не синтетические (8 номеров), заменены на вымышленные того же формата; коды регионов
  (37, 33, 750, 211) и форма записи (кириллица/латиница, регистр, пробелы, суффикс) сохранены. 52 места в
  `tests/test_ai_tradein_fields.py`, `tests/test_payload.py`, `tests/test_phone_extract.py`,
  `tests/test_stop_e_fixes.py`, `tests/test_stage_guarded_retry.py`, `payload_v2.py`, `main.py`,
  `docs/plans/2026-09-08-001-…`, `docs/plans/2026-09-07-001-…`, `docs/handoff/2026-09-09-…`,
  `docs/solutions/logic-errors/digit-window-parity-money-pairs.md`.
- **Госномер с регионом 99** в тестах — на вымышленный того же формата, 6 мест: `tests/test_stop_e_fixes.py`,
  `tests/test_web_entry_conversion.py`, `tests/test_web_entry_valuation.py` (по 2).
- Те же вымышленные госномера использованы в `n8n/` (согласованно; см. `n8n/SOURCE.md`).
- **Адрес боевого вебхука оценки n8n** в `docs/handoff/2026-10-04-release-plan-cabinet-web-entry.md`, строки 147 и 154 —
  заглушка `__OLD_N8N_CABINET_EVAL_URL_PROD_REMOVED__`. Путь вебхука без хоста (строка 143 и другие документы,
  тест `tests/test_web_entry_valuation.py`) оставлен: это читаемое имя, оно же задано в самом воркфлоу.
- В начало `CLAUDE.md` добавлена пометка «исторический снимок».
- ID пользователей MAX, номера сессий и цитаты реплик в документах оставлены как есть.

Проверка после всех замен: pytest на системном Python 3.11 (SQLite, без `.env`) — 1490 прошли, 29 упали, 5 пропущены;
набор упавших тестов совпадает с нетронутым снимком `v3.4.4` один в один (падения от окружения, не от замены).

## Скрипты с записью во внешние сервисы

В новой системе запускать только с её адресами и ключами, по задаче владельца. `.env` старой системы рядом с ними
не класть.

| Скрипт | Что пишет |
| --- | --- |
| `scripts/register_webhooks.py` | вебхуки ботов MAX и Telegram (с `--apply`) |
| `scripts/bx_delete_test_deals.py` | удаляет сделки и контакты в Битриксе (с `--apply`) |
| `scripts/issue_cabinet_link.py` | пишет в базу бота (только localhost) |

## Ранбуки старого боя

Ранбуки в `CLAUDE.md` и `docs/handoff/` содержат готовые команды к боевой старой системе (Dockhost, n8n, боевая
база, Битрикс). К новой платформе они не относятся и не выполняются.

Хост старого n8n оставлен в исторических документах: `docs/N8N_ARCHITECTURE.md`,
`docs/plans/2026-09-30-001-feat-cabinet-web-entry-plan.md`, `docs/handoff/2026-10-04-release-plan-cabinet-web-entry.md`,
`docs/handoff/2026-10-06-release-v3.4.0-handoff.md`.
