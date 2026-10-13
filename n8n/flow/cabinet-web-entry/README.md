# Веб-вход в кабинет: поставка со стороны n8n

## Состояние в n8n на 2026-09-30 (записано через API по разрешению владельца)

| Что | id | Состояние |
|---|---|---|
| `[TEST] Tool - Car Search` | `o3vH8ZVKW5dNMkK2` | N4 (код Prepare Data, Where Get Program, Return All Get Rates); сверка с эталоном N2 — 85/85 |
| `[TEST] Tool: Combo Calculator` | `GjppGA6LqjbaqX4G` | N4 + N5; живой прогон Х318ТЕ750 (исп. 5175/5176, один запрос в auto.ru): 3/3 комплектации = эталон N5 и локальный пересчёт |
| `[TEST] WEBHOOK: Cabinet Tradein Eval` | `99SEbXjbQta3SGSH` | **активен**; 403 / 403 / 400 проверены |
| `WEBHOOK: Cabinet Tradein Eval` | `MMhfTj8QJe2UIMSB` | создан, **выключен** |
| credentials | `zFKZt2QNOgURSnX8` [TEST], `KhaneOxa4PaPKXOM` [PROD] | Header Auth `X-Cabinet-Eval-Secret` |
| Боевой `Tool - Car Search` | `mZqzmEnnQiDbvtir` | **не тронут** (N4 — вместе с веб-входом), бэкап — `flow/backups/2026-09-30-pre-n4n5/` |
| Боевой `Tool: Combo Calculator` | `GD3ZCj8GiVyB7YBw` | **N5 с 2026-09-30 08:18:39Z** (команда владельца): изменён только код Prepare Combo (+ явный callerPolicy). Бэкап — `flow/backups/2026-09-30-pre-n5-prod/`. Откат: `api/apply_workflow_patch.py --wf GD3ZCj8GiVyB7YBw --set prod-combo-n5-rollback --apply`. Проверка исполнений: `api/check_combo_executions.mjs` |

Скрипты — `api/`: `apply_workflow_patch.py` (правка по наборам, проверка «до»,
diff, откаты `*-rollback`), `install_eval_webhooks.py`,
`check_car_search_vs_reference.py` (сверка живого Car Search с эталоном N2).
`check_sources_vs_live.py` — исходники (генераторы и снимки) = живые воркфлоу,
только чтение; ожидаемые расхождения — `expected_divergences.json` (с 2026-10-09,
handoff §9).

`binaryMode`: API его не принимает, но при ОБНОВЛЕНИИ воркфлоу сервер сохраняет
прежнее значение. Теряется он только у воркфлоу, СОЗДАННЫХ через API (вебхуки);
на результат для JSON-воркфлоу не влияет — проверено на копии Car Search.

Срез 2026-09-30. Ничего из этой папки в n8n не импортировано и не включено.
Тесты: `node --test flow/cabinet-web-entry/*.test.mjs flow/cabinet-web-entry/*/*.test.mjs`.

## N1 — вебхук оценки трейд-ина

| Копия | Файл | Путь | Сабфлоу | Credential |
|---|---|---|---|---|
| Дев | `[TEST] WEBHOOK_ Cabinet Tradein Eval.json` | `POST /webhook/cabinet-tradein-eval-test` | `[TEST] TOOL: AUTO.RU EVALUATION` (`ToF5ApgK6qkk4mcH`) | Header Auth «Cabinet Eval Secret [TEST]» |
| Бой | `WEBHOOK_ Cabinet Tradein Eval.json` | `POST /webhook/cabinet-tradein-eval` | `TOOL: AUTO.RU EVALUATION` (`XCPe53sUwk8i9e3T`) | Header Auth «Cabinet Eval Secret [PROD]» — **не активировать без команды** |

JSON собирает `build_eval_webhooks.py` (идемпотентен), тесты — `eval_webhook.test.mjs`.

**Заголовок:** `X-Cabinet-Eval-Secret`. Секреты лежат только в credential n8n и в
`.env` ботов; в JSON и в репозитории их нет. Нет заголовка или он неверный —
встроенная Header Auth ноды Webhook отвечает **403** до старта исполнения
(принято владельцем 2026-09-30; бот считает отказом и 401, и 403).

**Запрос:** `{"plate": "А123ВС777", "mileage": 85000, "request_id": "cab-7f3c"}`.

| HTTP | status | Когда |
|---|---|---|
| 200 | `SUCCESS` / `IN_PROGRESS` / `NOT_FOUND` / `INVALID_DATA` / `NO_REGION` | `tradein_price_*` — при SUCCESS, иначе `null`; `found_info` — при SUCCESS, если auto.ru его отдал; `plate_seen` — при NO_REGION |
| 400 | `BAD_REQUEST` | пустой `plate` или `mileage` не целое > 0; auto.ru не вызывался |
| 403 | — (текст n8n) | нет секрета или он неверный |
| 502 | `ERROR` | сабфлоу упал |

`request_id` возвращается эхом и пишется в custom data исполнения. Обычно < 1 с,
при `IN_PROGRESS` от auto.ru ~32 с; таймаут бота — не меньше 60 с.

Контракт для бота: SUCCESS действителен только при `tradein_price_to > 0`
(решение 2026-09-30); SUCCESS без `found_info` — штатный (оценка по техпараметрам).

### Установка — когда бот дойдёт до вызова оценки

Секреты сгенерированы в `~/.config/autodc/cabinet-eval-webhook.secrets` (права 600,
вне репозитория и Яндекс.Диска).

Владелец, в интерфейсе n8n:
1. Credentials → Create → **Header Auth**: Name `X-Cabinet-Eval-Secret`, Value — `N8N_CABINET_EVAL_SECRET_TEST` из файла секретов; название «Cabinet Eval Secret [TEST]».
2. То же для PROD: `N8N_CABINET_EVAL_SECRET_PROD`, «Cabinet Eval Secret [PROD]».
3. Import from File → `[TEST] WEBHOOK_ Cabinet Tradein Eval.json`.
4. Нода Webhook → credential «Cabinet Eval Secret [TEST]».
5. Нода Auto.ru Evaluation → проверить, что выбран `[TEST] TOOL: AUTO.RU EVALUATION`. Save.
6. Import from File → `WEBHOOK_ Cabinet Tradein Eval.json`, credential «[PROD]», проверить сабфлоу `TOOL: AUTO.RU EVALUATION`, Save. **Active не включать.**
7. Дев-копия → Active.

Агент n8n (Claude), после шага 7:
- curl без заголовка → 403, нового исполнения нет;
- curl с неверным секретом → 403;
- curl с верным секретом без `mileage` → 400, нода Auto.ru Evaluation не отработала;
- curl с номером, который даст владелец (платный вызов) → 200;
- чтение исполнений через API: путь, статус, время, custom data `request_id`.

## N4 — программа кредитования и минимальный взнос (не включено)

`n4-min-down-payment/`: код нод `Prepare Data` (`car_search_prepare.js`) и
`Prepare Combo` (`combo_prepare.js`), генератор `../patches/apply_n4.py`, тест.
Правила: программа — строка, где Model id стоит отдельным элементом
`Model_Pattern`; взнос — только `Min_Down_Payment` (целые проценты), пусто → 20 %.

Плюс две правки параметров нод в `Tool - Car Search` и `Tool: Combo Calculator`:
- `Get Program` → Where: `(Model_Pattern,like,%{{ $('Get Catalog').first().json['Model id'] }}%)`;
- `Get Rates` → Return All: on (сейчас не больше 50 строк; в таблицах 43–45).

## N5 — комбо-цена от Price old (не включено)

`n5-combo-price-old/`: `combo_prepare.js` = N4 + N5 (общий выкат),
`combo_prepare.n5-only.js` = только N5. База комбо = `Price old − Cash in hook`,
как у кредита и трейд-ина в Car Search; в `price` добавлены `cash_bonus` и `base_price`.

Диагностика последствий текущей формулы (что Александр называл клиентам) —
`n5-combo-price-old/DIAG_combo_named_payments.md`.

## N2 — эталон цен

`n2-price-reference/`: `build_price_reference.mjs` исполняет код нод (live, N4, N5)
на живых каталогах NocoDB (только чтение) → `price_reference.json` / `.csv`
(подробный эталон). `build_dealer_files.mjs <раскладка.json>` — файл на дилера
в формате бота (`dealers/dealer_<code>.json`); раскладку «дилер → каталог»
присылает агент бота.

## Ошибки данных каталогов (отложены)

`DATA_ISSUES.md`.

## После включения на бою

Перенести файлы N4/N5 в `flow/lib/`, обновить `Get Program` / `Get Rates` в
`flow/Tool - Car Search.json` и `flow/Tool_ Combo Calculator.json`, прогнать
`node --test flow/lib/*.test.mjs`.
