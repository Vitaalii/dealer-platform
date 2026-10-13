# Защита каналов бот ↔ n8n: план для агента бота

2026-10-03. Автор — сторона n8n (репозиторий `unifiedbot-n8n`). Для агента бота.
Строки кода — по `unifiedbot_prd_ong_3.3.3`; в рабочей ветке бота сверить.

## Зачем

Ни один канал между ботом и n8n не проверяет отправителя (аудит 2026-10-03,
только чтение):

| Канал | Куда | Сейчас | Чем опасен |
|---|---|---|---|
| бот → агент | `POST /webhook/ai-handoff-v3` (бой), `…-test` | без защиты, путь угадываем | расход LLM и платного auto.ru; `deal_id` берётся из тела → через `finalize_deal` чужая сделка в Bitrix |
| бот → shadow-резолвер | `POST /webhook/ai-resolver-shadow-test` | без защиты | спам в NocoDB `Resolver_Shadow_Log` |
| бот → логи сессии | `GET /webhook/get-session-logs` | без защиты; SQL собирается строкой | выдача данных исполнений n8n (ПДн клиентов) и класс SQL-инъекции в БД n8n. **Выключен 2026-10-03 13:43Z** |
| n8n → бот | `POST <бот>/n8n_callback` | без защиты | по любому `deal_id` — смена стадии и полей сделки, выключение бота у пользователя, карточки менеджерам |

Защищённый образец уже есть: вебхук оценки (заголовок `X-Cabinet-Eval-Secret`,
Header Auth в n8n).

## Принцип без простоя

Сначала отправитель начинает слать секрет, потом получатель начинает его
требовать. Лишний заголовок никому не мешает, поэтому каждый шаг безопасен.

## Изменения в боте (один релиз)

### 1. Заголовок во все вызовы n8n

Один помощник, например `n8n_headers()`, и его вызов во **всех четырёх** местах:

| Место | Вызов |
|---|---|
| `main.py:1688` `send_chat_message_to_n8n` | POST агента (каждый ход) |
| `main.py:1800` `send_handoff_webhook` | POST агента (`INIT_AI_HANDOFF`) |
| `shadow_resolver.py:36` `_send` | POST shadow-резолвера |
| `main.py:2323` `fetch_n8n_logs_for_archive` | GET логов сессии |

Заголовок `X-N8N-Secret: <N8N_WEBHOOK_SECRET>`. Секрет не задан → заголовок не
отправляется (поведение как сейчас), в лог — одно предупреждение на старте.

Вызовы вебхука оценки (`X-Cabinet-Eval-Secret`) не трогать — у них свой секрет.

### 2. Логи сессии — адрес из env и без затирания

- `main.py:2326`: адрес зашит в код → `N8N_SESSION_LOGS_URL` (пусто — функция
  возвращает `""` без запроса).
- `upsert_dialogue_archive` (`main.py:2349–2356`): при пустом ответе сейчас пишется
  `logs = "no_n8n_data"`, и **PATCH существующей записи затирает ранее
  сохранённые логи n8n** этой сессии. Нужно: пустой ответ → ключ `logs` в
  payload не класть (при POST новой записи — можно `no_n8n_data`, при PATCH —
  не трогать поле).

С 2026-10-03 13:43Z воркфлоу логов выключен: бот получает 404, пишет
`no_n8n_data`, архивация остального работает. Пункт про затирание актуален
уже сейчас.

### 3. `/n8n_callback` — проверка секрета с режимами

`main.py:1422` `handle_n8n_callback`, до любых побочных эффектов:

- заголовок `X-N8N-Callback-Secret`, сравнение `hmac.compare_digest` с
  `N8N_CALLBACK_SECRET`;
- `N8N_CALLBACK_AUTH_MODE`:
  - `off` — как сейчас (по умолчанию в первом релизе не использовать);
  - `soft` — запрос обрабатывается как раньше, в лог строка
    `N8N_CALLBACK_AUTH result=ok|missing|bad deal=<id> ip=<remote>`;
  - `strict` — `ok` обрабатывается, `missing`/`bad` → 403 **без** записи в Bitrix,
    без смены `is_smartbot_active`, без алертов; в лог та же строка;
- `strict` при незаданном `N8N_CALLBACK_SECRET` → ошибка старта (не молча
  открытый канал).

### 4. Переменные окружения

| Переменная | Бой | Дев |
|---|---|---|
| `N8N_WEBHOOK_SECRET` | свой | свой |
| `N8N_CALLBACK_SECRET` | свой | свой |
| `N8N_CALLBACK_AUTH_MODE` | `soft` → позже `strict` | `soft` → позже `strict` |
| `N8N_SESSION_LOGS_URL` | `https://mgak-wacd-ospe.gw-1a.dockhost.net/webhook/get-session-logs` | `__OLD_N8N_SESSION_LOGS_URL_REMOVED__` |

Значения секретов генерирует сторона n8n в
`~/.config/autodc/n8n-channel-secrets` (права 600, вне репозиториев), как для
вебхука оценки. В репозиторий и логи — никогда.

### 5. Тесты бота

- каждый из четырёх вызовов несёт `X-N8N-Secret` при заданном секрете и не
  несёт при пустом (мок `aiohttp.ClientSession`);
- `/n8n_callback`: режимы `off`/`soft`/`strict` × `ok`/`missing`/`bad`;
  в `strict` при `missing`/`bad` — 403 и ни одного вызова Bitrix / записи в БД /
  алерта;
- `strict` без секрета → ошибка инициализации;
- архив: пустые логи + существующая запись → в PATCH нет ключа `logs`;
- секрет не попадает в логи (`log_masking`).

## Порядок и моменты переключения

| # | Кто | Что | Простой |
|---|---|---|---|
| 0 | n8n | ✅ `Get Session Logs` выключен (13:43Z), бэкап `flow/backups/2026-10-03-get-session-logs/` | логов в архиве нет |
| 1 | n8n | Генерирует секреты, создаёт credentials в n8n | нет |
| 2 | **бот** | Релиз: пп. 1–4, `N8N_CALLBACK_AUTH_MODE=soft` (дев, затем бой) | нет — n8n заголовки пока игнорирует |
| 3 | n8n | Чтением: в исполнениях агента и shadow во входе Webhook есть `x-n8n-secret` (сутки трафика) | нет |
| 4 | n8n | ✅ 2026-10-03: `Get Session Logs` (бой, боевой секрет, только исполнения `AI Agent v3.0`) и `[TEST] Get Session Logs` (`get-session-logs-test`, тестовый секрет, только `[TEST] AI Agent v3.0`) — Header Auth, проверка формата, параметр `$1`, точное совпадение; включены. Без секрета / с чужим — 403; чужая сессия — `logs: null`. Боевой бот без заголовка получает 403 вместо прежних 404 — для него ничего не меняется | нет |
| 5 | n8n, **по команде для боя** | `TOOL: Save to CRM` (бой) и `_test`: заголовок `X-N8N-Callback-Secret` через credential в HTTP-ноде | нет — бот в `soft` |
| 6 | **бот** | Сутки логов: все `N8N_CALLBACK_AUTH result=ok` → `N8N_CALLBACK_AUTH_MODE=strict` (env + рестарт) | нет |
| 7 | n8n, **по команде для боя** | Header Auth на вебхуках агента и shadow: сначала `[TEST]`, потом бой. Правка — в генераторах (`nocodb/build_v3_workflow.py`, `build_test_contour.py`, `build_resolver_workflow.py`), не только в живом воркфлоу | нет — бот шлёт заголовок с шага 2 |

Откаты: шаги 4, 5, 7 — снять аутентификацию в ноде (секунды, через API);
шаг 6 — `N8N_CALLBACK_AUTH_MODE=soft`.

## Что нужно от агента бота

1. Релиз по пп. 1–5 в дев-боте, затем в боевом, с `soft`.
2. Сообщить момент выката (время UTC) — от него сторона n8n отсчитывает сутки
   наблюдения на шагах 3 и 6.
3. Подтвердить имена переменных или предложить свои — сторона n8n подстроит
   имена заголовков в credentials.

## Статус ДЕВ-контура на 2026-10-03

| Что | Результат |
|---|---|
| Дев-бот с секретами (`soft`) | `X-N8N-Secret` совпадает: агент — исп. 5257, 5262; shadow — 5260. `deal_id` пустой (сессия `test-n8nauth-03`) |
| Header Auth `[TEST] AI Agent v3.0` и `AI Resolver (shadow)` | включён 14:59:30Z; без секрета и с боевым секретом — 403; ход после включения (5262, shadow 5260) прошёл |
| `TOOL: Save to CRM_test` → `/n8n_callback` | заголовок `X-N8N-Callback-Secret` («Bot Callback Secret [TEST]»); ручной прогон 15:21:04Z, исп. 5267, ответ бота `{"status":"ok"}` (`user_id test-n8nauth-callback-01`, `deal_id` пустой) |
| Генераторы | `build_test_contour.py` — Header Auth на всех `[TEST]`-вебхуках; `build_v3_workflow.py`, `build_resolver_workflow.py` — `INBOUND_AUTH = None` до боевого шага |
| Откаты | `apply_workflow_patch.py --set test-agent-auth-rollback / test-shadow-auth-rollback / test-save-crm-callback-rollback --apply` |

Бой не тронут: вебхук агента, shadow-пути у боевого бота нет, `TOOL: Save to CRM` — без изменений.
