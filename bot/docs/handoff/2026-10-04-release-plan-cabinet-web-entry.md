# Ранбук релиза 3.4.0: кабинет v3 + вход с сайта

Дата выката: **2026-10-06**. Пилот веб-входа — `https://demo.autodc.org/haval-demo-new`
(тестовый сайт на боевом боте, дилер **01**).
Ветка: `feat/cabinet-web-entry`. Прод сейчас: образ `vitaln/unified-bot:multy-v3.3.3`
(код `d2e7489`, ветка `feat/multibot-brand-routing`) — он же образ отката.
Боевой бот: `https://obut-5oaj-5i2u.gw-1a.dockhost.net` (далее `$P`).

Правила выполнения: шаги — по одному, по команде владельца; каждая команда отдельно,
результат сверяется с «ожидается». Не сошлось — стоп, без самостоятельных правок.
Токены и секреты в командах не печатаются (берутся из env или файлов секретов).

---

## 1. Перед выкатом

Результаты предпроверки 2026-10-06 (выполнены агентом бота, только чтение):

### 1.1. Тесты и ветка — ✓

| Проверка | Результат |
|---|---|
| `venv/bin/python -m pytest -q -p no:randomly` (SQLite) | 1333 passed, 4 skipped (4 — PostgreSQL-only) |
| `TEST_DATABASE_URL=postgresql+asyncpg://cabtest:cabtest@localhost:5433/unifiedbot_test venv/bin/python -m pytest -q -p no:randomly` | 1337 passed |
| `npm run check:cabinet` | «Сборка кабинета актуальна» |
| Ветка | закоммичена; `d2e7489` + 65 коммитов, линейна (`git rev-list feat/cabinet-web-entry..feat/multibot-brand-routing` пусто) |
| Неотслеживаемые файлы | только `scripts/{main,database,widget}_{old,new}.*` — слепки для сравнения, исключены `.dockerignore` |
| Python образа 3.10 | весь код компилируется интерпретатором образа 3.3.3 (3.10.21), 3.11-only вызовов нет; `requirements.txt` не менялся |
| Новые env в коде | ровно список раздела 2.6 |

### 1.2. Локальные скрипты с токеном Битрикса — решено: исключены (`.dockerignore`, коммит c470028)

Образ собирается `COPY . .` из рабочей папки. В него попадают игнорируемые гитом
`check_bitrix_full.py` и `check_stages.py` (август) — в них **зашит текущий вебхук
Битрикса с токеном**. Они уже лежат в `multy-v3.3.3` (вместе с `check_noco_keys.py`);
репозиторий Docker Hub `vitaln/unified-bot` приватный (анонимно не виден).
`credentials.json` (ключ Dialogflow) — нужен боту, остаётся, как в 3.3.3.

Решение владельца 2026-10-06: `check_*.py` — в `.dockerignore` (под шаблон попадает и отслеживаемый
`check_noco_keys.py` — без секретов, ботом не импортируется); проверка в образе — шаг 2.2.6.
Вебхук Битрикса меняется после релиза (раздел 5, п. 2).

### 1.3. Совместимость старых лендингов (Ver_3, без вкладки ЛК) — ✓

- Обработчики `/catalog/{table_id}`, `/catalog-colors`, `/bot-links`, `/chat_message`,
  `/chat_stream/{session_id}`, CORS-preflight — **без изменений** с 3.3.3 (сравнение
  функций `d2e7489` ↔ HEAD); загрузчик NocoDB — только добавлен необязательный параметр `force`.
- `widget-custom/widget.js` (чат-виджет) — без изменений; в `widget-custom/` изменился только кабинет.
- Новое для всех путей: сжатие JSON gzip, если клиент его принимает (браузеры —
  прозрачно); `/catalog` и раньше отдавал gzip сам.
- Живая проверка на дев-стенде (код релиза): `/catalog/muruk10ipof33m5` (дилеры, 15 строк),
  `/catalog/mywf0f16ptn25gk` (каталог, 26), `/catalog/mm8wldgk5vn9gwh` (оснащение, 88) —
  200, gzip, `Access-Control-Allow-Origin: *`; `/bot-links` — 200; `widget.js` — 200.
- Пилотная страница `haval-demo-new` при загрузке вызывает у боевого бота ровно
  `widget.js` и три `/catalog` из списка выше.

### 1.4. Страницы `/cabinet` со старыми строками `<script>` — ✓

- Боевая `https://demo.autodc.org/cabinet` подключает исходники с боевого бота:
  `calc_engine.browser.js?v=2026-08-06.1`, `calc_model.js?v=1`, `app.js?v=6`
  (**эти строки — эталон для полного отката**, раздел 4.1). Контейнер `#cabinet-root`
  на странице есть — v3 `app.js` монтируется в него же.
- Исходники отдаются с `Cache-Control: no-cache` + ETag и в 3.3.3 (проверено на `$P`),
  и в релизе: браузер перепроверяет файл при каждом заходе → **сразу после выката
  страница получает кабинет v3**, без замены строк.
- Исходники и сборка в образе: `widget-custom/cabinet-app/{app.js,calc_model.js,calc_engine.browser.js}`
  и `Production/` (+ `.br`/`.gz`, `manifest.json`) — в гите, `.dockerignore` их не исключает;
  проверка в образе — шаг 2.2.
- `Production/…?v=<версия>` отдаётся с `public, max-age=31536000, immutable` (только при совпадении версии).

### 1.5. Пилот: дилер 01 — ✓

- Код дилера страницы `haval-demo-new`: скрытое поле `dealer_id=01` (`dealer=Demo`); бот на странице — боевой.
- Строка Dealers (`muruk10ipof33m5`, одна строка с кодом 01):
  - `Website_URL` = `https://demo.autodc.org/haval-demo` — https; домен `demo.autodc.org` совпадает с пилотом → CORS кабинета (`Origin: https://demo.autodc.org`) пройдёт;
  - `Brand` = Haval, `Dealer_name` = «Автомир Самара»;
  - `letter_title` = «Новый лид», `form_type` = «ВЫБОР АВТО»; `Policy` и `Agreement` заполнены;
  - каталог `mywf0f16ptn25gk` (26 строк), программы `my7rg0l7j6varzn` (1 строка), ставки `mfocbgvi50mqm7c` (43 строки), оснащение `mm8wldgk5vn9gwh`.
- Программа найдена для всех 7 Model id каталога (11, 12, 13, 14, 19, 20, 21):
  `Min_Down_Payment` = 10, `Max_Term_Months` = 84 → техалерта «20 % по умолчанию» не будет.
- Ставка для шкалы лендинга (строка с минимальным `Downpayment_From`): Rate 5, 12 мес, от 10 %.
- Адрес кабинета, который выдаст бот: `cabinet_page_url` = **`https://demo.autodc.org/cabinet?src=web`**
  (хост `Website_URL` + `/cabinet`; на бою `CABINET_PAGE_PATH` не задан). Страница существует
  (заголовок «HAVAL - ДЕМО Кабинет»), строки `<script>` — п. 1.4.

---

## 2. Выкат по шагам

Команды 2.1–2.4 — на машине разработки, из папки репозитория.

### 2.1. Слияние и тег

1. `git status --short` — ожидается: только `?? scripts/*_old.*` / `*_new.*` (6 файлов).
2. `git checkout feat/multibot-brand-routing` — «Switched to branch…».
3. `git merge --ff-only feat/cabinet-web-entry` — «Fast-forward», без merge-коммита.
4. `git log -1 --format='%h %s'` — тот же коммит, что HEAD `feat/cabinet-web-entry`.
5. `git tag v3.4.0` — без вывода.
6. `git push origin feat/multibot-brand-routing v3.4.0` — ветка и тег отправлены.

### 2.2. Сборка и проверка образа

1. `docker build --platform linux/amd64 -t vitaln/unified-bot:multy-v3.4.0 .` — «naming to docker.io/vitaln/unified-bot:multy-v3.4.0», без ошибок pip.
2. `docker run --rm --platform linux/amd64 --entrypoint ls vitaln/unified-bot:multy-v3.4.0 widget-custom/cabinet-app/Production` — `app.js app.js.br app.js.gz calc_engine.browser.js(.br/.gz) calc_model.js(.br/.gz) manifest.json`.
3. `docker run --rm --platform linux/amd64 --entrypoint cat vitaln/unified-bot:multy-v3.4.0 widget-custom/cabinet-app/Production/manifest.json` — версии `calc_engine` 26b2fb5198, `calc_model` d7828a95bc, `app` 968695878b.
4. `docker run --rm --platform linux/amd64 --entrypoint ls vitaln/unified-bot:multy-v3.4.0 widget-custom/cabinet-app widget-custom/widget.js` — исходники `app.js calc_model.js calc_engine.browser.js` и `widget.js` на месте.
5. `docker run --rm --platform linux/amd64 --entrypoint ls vitaln/unified-bot:multy-v3.4.0 activate_webhook.py n8n_auth.py valuation_client.py` — `activate_webhook.py: No such file`, два других есть.
6. `docker run --rm --platform linux/amd64 --entrypoint sh vitaln/unified-bot:multy-v3.4.0 -c 'ls check_*.py'` — «No such file» (решение 1.2: `check_bitrix_full.py`, `check_stages.py`, `check_noco_keys.py` в образ не идут).
7. `docker run --rm --platform linux/amd64 --entrypoint ls vitaln/unified-bot:multy-v3.4.0 tests docs node_modules .env` — все четыре «No such file».

### 2.3. Push

1. `docker push vitaln/unified-bot:multy-v3.4.0` — `multy-v3.4.0: digest: sha256:…`.

### 2.4. Бэкап прод-PostgreSQL (веб-консоль контейнера БД, Dockhost)

**База бота — `max_bot_db`.** `$POSTGRES_DB` в контейнере БД указывает на `root_db` (NocoDB) —
для бэкапа и запросов бота его не использовать (уточнение владельца 2026-10-06).

**Бэкапы — на постоянный том** `/var/lib/postgresql/data/backups/` контейнера `nocodb-postgres`
(решение владельца 2026-10-06): `/tmp` не переживает пересоздание контейнера, скачать наружу некуда.
Имя файла — `backup_bot_pre_v<версия>_<ГГГГММДД>.dump`.

1. `env | grep POSTGRES` — имя пользователя (`POSTGRES_USER`).
2. `mkdir -p /var/lib/postgresql/data/backups` — без вывода (папка уже есть с 3.4.1).
3. `pg_dump -U $POSTGRES_USER -d max_bot_db -F c -f /var/lib/postgresql/data/backups/backup_bot_pre_v<версия>_<дата>.dump` — без вывода.
4. `ls -l /var/lib/postgresql/data/backups/` — новый файл, размер больше нуля.
5. `pg_restore -l /var/lib/postgresql/data/backups/backup_bot_pre_v<версия>_<дата>.dump | grep -E 'TABLE public (users|cabinet_tokens|pending_sync) '` — три строки (в `root_db` этих таблиц нет).

Выполнено (контейнер `nocodb-postgres`):
- **3.4.1, 2026-10-06:** `max_bot_db` → `/var/lib/postgresql/data/backups/backup_bot_pre_v341_20261006.dump` (88 записей `cabinet_*`);
- **3.4.0, 2026-10-06:** `max_bot_db` → `backup_bot_pre_v340_20261006.dump` (5 МБ, 68 записей `cabinet_*`) — перенесён на том;
  `root_db` → `/tmp/backup_pre_v340_20261006.dump` — остался в `/tmp`;
- на томе также бэкап от 25.09 (до мультибота).

Схема: при старте `init_db` только добавляет — таблицы `cabinet_web_opens`, `cabinet_valuations`;
колонки `cabinet_profile.{entry_source, lead_code, lead_nonce_hash, link_fallback, return_url,
web_form, web_mileage, web_plate, valuation_id}`, `cabinet_tokens.entry_source`,
`cabinet_form.tradein_steering`. Откат кода на 3.3.3 с этой схемой безопасен.

### 2.5. ▶ Команда агенту n8n №1 — боевой вебхук оценки (можно за минуты до бота)

Включить боевой вебхук `…/webhook/cabinet-tradein-eval` с Header Auth `X-Cabinet-Eval-Secret`
(секрет — `N8N_CABINET_EVAL_SECRET_PROD`). Бот 3.3.3 его не вызывает — деградации нет.

Проверка после ответа агента (с любой машины, без секрета, Auto.ru не вызывается):
`curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' -d '{}' __OLD_N8N_CABINET_EVAL_URL_PROD_REMOVED__` — ожидается `403`
(404 — вебхук не импортирован/не активен: стоп, назад к агенту).

### 2.6. Боевые env (Dockhost, приложение бота; добавить, ничего не удалять)

| Ключ | Значение / откуда |
|---|---|
| `N8N_CABINET_EVAL_URL_PROD` | `__OLD_N8N_CABINET_EVAL_URL_PROD_REMOVED__` (`~/.config/autodc/cabinet-eval-webhook.secrets`, тот же ключ) |
| `N8N_CABINET_EVAL_SECRET_PROD` | `~/.config/autodc/cabinet-eval-webhook.secrets`, ключ `N8N_CABINET_EVAL_SECRET_PROD` |
| `N8N_WEBHOOK_SECRET` | `~/.config/autodc/n8n-channel-secrets`, ключ `N8N_WEBHOOK_SECRET_PROD` |
| `N8N_CALLBACK_SECRET` | `~/.config/autodc/n8n-channel-secrets`, ключ `N8N_CALLBACK_SECRET_PROD` |
| `N8N_CALLBACK_AUTH_MODE` | `soft` (`strict` — по разделу 5, п. 4) |
| `N8N_SESSION_LOGS_URL` | `https://mgak-wacd-ospe.gw-1a.dockhost.net/webhook/get-session-logs` |
| `CABINET_TRUSTED_PROXIES` | не задавать до шага 3.1 |
| `CABINET_WEB_IP_HOURLY_LIMIT` | не задавать (по умолчанию 5/час на IP) — решение в шаге 3.1 |
| `CABINET_WEB_DEALER_DAILY_CAP` | не задавать (100/сутки на дилера); **`0` — выключатель веб-входа** |

Проверить, что на бою **нет** (только дев): `CABINET_EVAL_FAKE`, `CABINET_EVAL_FAKE_DELAY`,
`CABINET_OTP_DEV_LOG`, `CABINET_DEV_WEB_OPEN_FAULT_FILE`, `CABINET_DEAL_TEST_MARK`,
`CABINET_PAGE_PATH`, `CABINET_HANDOFF_TIMEOUT`.
Без изменений с 3.3.3: `BOT_ENV=prod`, `ENVIRONMENT=production`, `PUBLIC_BASE_URL`,
`CABINET_HMAC_KEY`, `BITRIX_*`, `N8N_WEBHOOK_URL`, `NOCODB_*`, токены ботов.

### 2.7. ▶ Команда агенту n8n №2 — N4, одновременно с шагом 2.8

N4 на боевых Car Search и Combo Calculator: взнос из `Min_Down_Payment` (пусто → 20 %),
программа по точному Model id. Бот 3.4.0 считает по тем же правилам — минуты между
2.7 и 2.8 возможен рассинхрон чата и кабинета.

### 2.8. Смена образа (владелец, Dockhost)

Образ приложения → `vitaln/unified-bot:multy-v3.4.0` → рестарт. Запомнить время старта (UTC) — для шага 3.5.

### 2.9. Проверка старта (логи приложения и `$P`)

1. В логе: `UNIFIED BOT STARTED`, `Loaded N bots … env=prod`, `Loaded … dealers`.
   Нет: `N8N_WEBHOOK_SECRET не задан`, `CABINET_EVAL_FAKE задан вне дев-окружения`,
   `N8N_CALLBACK_AUTH_MODE=strict без N8N_CALLBACK_SECRET`, `Traceback`.
2. `curl -s -o /dev/null -w '%{http_code}\n' $P/catalog/mywf0f16ptn25gk` — `200`.
3. `curl -s -o /dev/null -w '%{http_code}\n' $P/catalog/mm8wldgk5vn9gwh` — `200`.
4. `curl -s -o /dev/null -w '%{http_code}\n' $P/catalog/muruk10ipof33m5` — `200`.
5. `curl -s $P/catalog/muruk10ipof33m5 | python3 -c "import sys,json;r=json.load(sys.stdin)['list'];print(sorted(set().union(*[set(x) for x in r])))"` — ровно 7 полей `Dealer_*`.
6. `curl -s -o /dev/null -w '%{http_code}\n' $P/catalog/mnivr6uyf5ckzru` — `404` (архив закрыт).
7. `curl -s -o /dev/null -w '%{http_code}\n' $P/bot-links` — `200`.
8. `curl -s -o /dev/null -w '%{http_code} %header{cache-control}\n' $P/widget-custom/widget.js` — `200 no-cache`.
9. `curl -s $P/cabinet/api/dealer/01 | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['cabinet_page_url']);print(*d['cabinet_assets'],sep='\n')"` — `https://demo.autodc.org/cabinet?src=web` и три пути `Production/…?v=26b2fb5198 / d7828a95bc / 968695878b`.
10. `curl -s -o /dev/null -H 'Accept-Encoding: br' -w '%header{content-encoding} %header{cache-control}\n' "$P/widget-custom/cabinet-app/Production/app.js?v=968695878b"` — `br public, max-age=31536000, immutable`.
11. `curl -s -o /dev/null -w '%{http_code} %header{cache-control}\n' "$P/widget-custom/cabinet-app/app.js?v=6"` — `200 no-cache` (старые строки `/cabinet` получают v3).
12. Колбэк n8n пустым запросом на бою не проверять (в `soft` он уйдёт в обработку) — строка `N8N_CALLBACK_AUTH` проверяется на живом диалоге в 3.2.8.
13. Браузер: `https://demo.autodc.org/haval-demo-new` — карточки машин, поп-ап оснащения, контакты дилера; чат-виджет открывается.

Любой из пп. 2–8 и 13 не сошёлся → **немедленный полный откат** (раздел 4.1).

---

## 3. Сразу после выката

### 3.1. IP клиента за прокси Dockhost (первым делом)

1. Узнать свой внешний IP (например, `curl -s https://ifconfig.me` с той же машины, с которой открыт браузер).
2. Открыть кабинет с этой машины (по ссылке из чата в шаге 3.2 или вкладкой ЛК в 3.3).
3. В логе приложения — первая колонка строки `aiohttp.access` с `/cabinet/api/…` и `ip=` в строке `N8N_CALLBACK_AUTH`.

| Что в логе | Действие |
|---|---|
| Ваш внешний IP | ничего не делать |
| Адрес прокси + есть `X-Forwarded-For` | адрес прокси → `CABINET_TRUSTED_PROXIES`, рестарт, повторить проверку |
| Адрес прокси, `X-Forwarded-For` нет | все клиенты делят один IP: сразу `CABINET_WEB_IP_HOURLY_LIMIT=1000`, рестарт; защита — суточный лимит дилера (100); вопрос заголовка — в Dockhost |

Пока вопрос не решён: тесты пилота с одного IP упираются в лимит 5 открытий сессии в час.

**Итог 2026-10-06:** лог запросов — все запросы с `10.160.46.96` (прокси Dockhost). После
`CABINET_TRUSTED_PROXIES=10.160.46.96` и рестарта строка колбэка
`N8N_CALLBACK_AUTH result=missing mode=soft deal=53345 ip=45.92.179.194` — прокси передаёт
`X-Forwarded-For`. `CABINET_TRUSTED_PROXIES=10.160.46.96` оставлен, лимит 5/час — по настоящим
адресам. Бот сравнивает адрес прокси точным совпадением (подсети не поддерживаются): если в первой
колонке лога появится другой адрес прокси — добавить его через запятую и перезапустить.

### 3.2. Смоук: чат и кабинет клиента Александра (дилер 01, боевой)

1. `haval-demo-new` → шаг 2, вкладка «В чате» → «На сайте» → «Далее» — чат-виджет: «Запрос обработан», цена позиции.
2. «В кредит» → платёж `30000` → «Принято!», затем Александр (n8n) отвечает.
3. «Хочу подобрать сам в личном кабинете» → ссылка `https://demo.autodc.org/cabinet#t=…`.
4. Вход **настоящим звонком** (на бою кода в логе нет) → кабинет v3, цена = цена бота, срок кратен 12.
5. Анкета → «Отправить» → «Заявка отправлена».
6. Битрикс: сделка Tilda по коду `01…` в воронке 9 → «Обогащение» → «Отправлено», письмо с платежом/взносом/сроком.
7. n8n (сторона n8n, чтением): во входе агента есть `x-n8n-secret`.
8. Лог: `N8N_CALLBACK_AUTH result=ok` (если Save to CRM уже шлёт секрет) или `missing` (ещё нет — штатно в `soft`).

### 3.3. Вкладка ЛК на пилоте (после вставки блоков агентом лендинга)

Предусловие: агент лендинга выложил сборку `Production/` пилота с адресом боевого бота
(третья вкладка, L4b, L9), владелец опубликовал страницу.

1. `haval-demo-new` → вкладка «В личном кабинете» → кредит → «Далее» → экран входа кабинета `https://demo.autodc.org/cabinet?src=web`.
2. Вход звонком → анкета → «Заявка отправлена».
3. Битрикс: сделка `01…` воронка 9, «Тип лида 1» = «ЛК с сайта», «Тип обращения» = «Анкета кредит», «Обогащение» → «Отправлено», письмо с цифрами.
4. Контрольная заявка трейд-ин или комбо (по решению владельца; настоящая оценка — 10–30 ₽): в кабинете ожидание оценки → данные появляются без перезагрузки; в сделке номер и пробег клиента.
5. БД бота (веб-консоль контейнера `nocodb-postgres`, база `max_bot_db`), по одной:
   1. Открытия сессии с пилота:
      `psql -U $POSTGRES_USER -d max_bot_db -c "select id, ip, dealer_code, kind, created_at from cabinet_web_opens where dealer_code='01' order by id desc limit 5;"`
      — по строке на каждое «Далее»; `kind` = `open`; `ip` — внешний адрес тестирующего (**не** `10.160.46.96`).
   2. Сессия и передача:
      `psql -U $POSTGRES_USER -d max_bot_db -c "select t.id as token, t.status, t.bound_at, t.handoff_at, p.lead_code, p.deal_id, p.scenario_type, p.web_plate, p.web_mileage, p.valuation_id from cabinet_tokens t join cabinet_profile p on p.user_id=t.user_id where t.dealer_code='01' and t.entry_source='web' order by t.id desc limit 3;"`
      — `lead_code` = код сделки Tilda (`01…`), `deal_id` = ID этой сделки, `bound_at` и `handoff_at` заполнены; для трейд-ина/комбо — номер, пробег и `valuation_id`.
   3. События последней веб-сессии дилера 01:
      `psql -U $POSTGRES_USER -d max_bot_db -c "select event_type, created_at from cabinet_events where token_id=(select max(id) from cabinet_tokens where dealer_code='01' and entry_source='web') order by id;"`
      — есть `code_sent`, `verified`, `form_submitted`, `handoff`, `handoff_form_push`, `handoff_done`; для трейд-ина/комбо — `valuation_to_deal`; нет `handoff_stuck_alert`.
   4. Оценка (трейд-ин / комбо):
      `psql -U $POSTGRES_USER -d max_bot_db -c "select id, status, our_failure, price_from, price_to, reason, finished_at from cabinet_valuations where dealer_code='01' order by id desc limit 3;"`
      — `status` = `success` (или `not_found`/`found_no_price` — штатные кадры), `our_failure` = `f`; `failed` + `our_failure` = `t` — сбой вебхука/секрета: стоп, разбор.
   5. Очередь досылки:
      `psql -U $POSTGRES_USER -d max_bot_db -c "select status, count(*) from pending_sync group by status;"` — нет новых `fatal`.
   6. Незавершённые передачи (должно быть пусто):
      `psql -U $POSTGRES_USER -d max_bot_db -c "select t.id, t.user_id, t.dealer_code from cabinet_tokens t where t.status='active' and t.handoff_at is not null and not exists (select 1 from cabinet_events e where e.token_id=t.id and e.event_type='handoff_done');"`
6. Контрольные сделки 3.2 и 3.3 — удаление по команде владельца, после пробного прогона.

### 3.4. Страница `/cabinet` на Tilda — переход на `Production/` (владелец, после 3.2–3.3)

До замены кабинет работает на исходниках — исправен, только медленнее. В блоке T123 страницы
`https://demo.autodc.org/cabinet` заменить содержимое целиком (как на `/cabinet_dev`, адрес — боевой бот):

```html
<div id="cabinet-root"></div>
<script>
  window.UNIFIEDBOT_URL = 'https://obut-5oaj-5i2u.gw-1a.dockhost.net';
</script>
<script src="https://obut-5oaj-5i2u.gw-1a.dockhost.net/widget-custom/cabinet-app/Production/calc_engine.browser.js?v=26b2fb5198"></script>
<script src="https://obut-5oaj-5i2u.gw-1a.dockhost.net/widget-custom/cabinet-app/Production/calc_model.js?v=d7828a95bc"></script>
<script src="https://obut-5oaj-5i2u.gw-1a.dockhost.net/widget-custom/cabinet-app/Production/app.js?v=968695878b"></script>
```

Остальное на странице не трогать. Опубликовать. Проверка:
1. `curl -s -A 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36' https://demo.autodc.org/cabinet | grep -o 'cabinet-app/[A-Za-z_/.]*js?v=[0-9a-z]*'` — три строки `Production/…` с версиями выше (короткий User-Agent Tilda иногда отбивает 403; надёжнее — «Просмотр кода» страницы в браузере).
2. Прямой заход на `/cabinet` — «Кабинет открывается по персональной ссылке из чата» (норма без токена).
3. Повторить вход по ссылке из чата (3.2, пп. 3–5) — кабинет v3, анкета отправляется.
Откат строк — раздел 4.1, п. 1.

### 3.5. ▶ Сообщить агенту n8n время выката (UTC) из шага 2.8

От него сторона n8n отсчитывает сутки наблюдения (раздел 5, п. 4).

---

## 4. Откат

### 4.1. Полный

1. Если страница `/cabinet` уже переведена на `Production/` (шаг 3.4) — **сначала** вернуть строки
   (в 3.3.3 нет `Production/`, будет 404):
   ```
   <script src="https://obut-5oaj-5i2u.gw-1a.dockhost.net/widget-custom/cabinet-app/calc_engine.browser.js?v=2026-08-06.1"></script>
   <script src="https://obut-5oaj-5i2u.gw-1a.dockhost.net/widget-custom/cabinet-app/calc_model.js?v=1"></script>
   <script src="https://obut-5oaj-5i2u.gw-1a.dockhost.net/widget-custom/cabinet-app/app.js?v=6"></script>
   ```
   (порядок и атрибуты — как на странице до замены).
2. Dockhost: образ `vitaln/unified-bot:multy-v3.3.3` → рестарт. Env новых переменных можно оставить — старый код их не читает. БД не откатывать (схема только расширена).
3. Лендинг пилота — снять публикацию третьей вкладки (агент лендинга / владелец).
4. ▶ Агенту n8n: N4 вернуть, если откат из-за расчётов (иначе чат и кабинет 3.3.3 разойдутся по взносу); вебхук оценки можно оставить — 3.3.3 его не вызывает.
5. Проверки 2.9 пп. 2–8 и 13 на 3.3.3 — те же ожидания.
6. Открытые на момент отката веб-сессии теряются — клиент уходит в «В чате» / «По телефону».

Восстановление БД из бэкапа — только при повреждении данных (приложение бота на время восстановления остановить):
`pg_restore -U $POSTGRES_USER -d max_bot_db --clean /var/lib/postgresql/data/backups/backup_bot_pre_v<версия>_<дата>.dump`
— бэкап перед выпуском, который откатывается (3.4.1 → `backup_bot_pre_v341_20261006.dump`, 3.4.0 → `backup_bot_pre_v340_20261006.dump`).
База — `max_bot_db`, не `$POSTGRES_DB` (= `root_db`, NocoDB). Бэкап `root_db` релизами не затрагивается — восстанавливать не нужно.

### 4.2. Только веб-вход

1. Dockhost: `CABINET_WEB_DEALER_DAILY_CAP=0` → рестарт (образ тот же).
2. «Открыть сессию» отвечает отказом → лендинг показывает «кабинет открыть не удалось» и уводит в «В чате» / «По телефону». Чат и кабинет из чата работают.
3. Проверка: вкладка ЛК на пилоте → экран отказа с двумя кнопками.
4. По желанию — снять публикацию вкладки на пилоте.

### 4.3. Только колбэк n8n

При сбоях после перехода в `strict` — `N8N_CALLBACK_AUTH_MODE=soft` → рестарт.

---

## 5. Шаги после релиза

Выкат: образ `multy-v3.4.0`, старт **2026-10-06 05:07:02 UTC**; N4 — 05:05 UTC. Пилот веб-входа
(дилер 01, `haval-demo-new`) прошёл 2026-10-06: комбо, настоящая оценка, сделка Tilda 53347
(`012012HLTV`) → «Обогащение» 08:54:22 → «Отправлено» 08:54:23 (время портала), письмо с цифрами и машиной.

### 5.1. План по датам (UTC)

| Когда | Кто | Шаг | Готово, если |
|---|---|---|---|
| 06.10 05:07 → 07.10 05:07 | n8n (чтением) | Сутки наблюдения: во входе исполнений боевого агента и shadow есть `x-n8n-secret` | агент n8n подтвердил; в логе бота нет техалертов по каналам n8n |
| 07.10, после наблюдения | ▶ агент n8n | Боевой `TOOL: Save to CRM` шлёт `X-N8N-Callback-Secret` (`N8N_CALLBACK_SECRET_PROD`) | в логе бота все новые строки `N8N_CALLBACK_AUTH result=ok` (бот в `soft`) |
| 07.10, через 2–3 ч одних `ok` | владелец, Dockhost | `N8N_CALLBACK_AUTH_MODE=strict`, рестарт | колбэк без секрета → `403`; живой диалог — `result=ok`, ответ клиенту доходит |
| 08.10 | ▶ агент n8n | Header Auth (`X-N8N-Secret`) на боевом агенте и shadow | живой диалог в чате отвечает; запрос к вебхуку агента без секрета — отказ |
| 08.10, после Header Auth | владелец | Замена вебхука Битрикса: новый входящий вебхук → `BITRIX_WEBHOOK_URL` на бою и деве → рестарт → старый отозвать; повторный скан архива NocoDB (`mnivr6uyf5ckzru`, шаблон `/rest/<id>/<токен>/`) | смоук: диалог создаёт/находит сделку; скан — 0 вхождений |
| 08.10 | агент бота по команде | Fast-forward `main` до последнего выпуска: `git checkout main && git merge --ff-only v3.4.1 && git push origin main` | `main` = `v3.4.1` |
| 09.10 (релиз принят, откат не нужен) | владелец | Бэкапы: на томе `/var/lib/postgresql/data/backups/` (25.09, перед 3.4.0, перед 3.4.1) оставить последний, остальные — по решению; `root_db` перед 3.4.0 в `/tmp` — удалить или перенести на том | по решению |
| 09.10 | агент бота по команде, пробный прогон → удаление | Тестовые сделки и контакты пилота и смоука на портале (список — по коду и ID, сверка «Чат ID», дилер 01) | пробный прогон сверен владельцем, удалены, проверка «осталось: ничего» |
| после пилота, по решению владельца | агент лендинга + владелец | Остальные дилеры: лендинги с вкладкой ЛК — по одному, с контрольной заявкой; у дилера должна быть страница `/cabinet` (5.2) | контрольная заявка → «Отправлено», письмо |
| 07.10 и далее | владелец | Страницы `/cabinet` сайтов дилеров → `Production/` (5.2) | на каждой странице три строки `Production/…` |
| 06.10–09.10, раз в день | владелец | Первая колонка лога запросов — по-прежнему `10.160.46.96`; новый адрес прокси — дописать в `CABINET_TRUSTED_PROXIES` через запятую, рестарт | — |

### 5.2. Страницы `/cabinet` сайтов дилеров → `Production/`

Проверка 2026-10-06 (`https://<домен Website_URL>/cabinet`, чтением):

| Сайт | Дилеры | Сейчас | Действие |
|---|---|---|---|
| `https://demo.autodc.org/cabinet` | 99, 98, 01 | `Production/` (сделано 06.10) | — |
| `https://offer.geely-radar.ru/cabinet` | 02 Geely «Радар» | исходники `?v=2026-08-06.1 / 1 / 6` | заменить блок |
| `https://sale.belgee-radar.ru/cabinet` | 03 Belgee «Радар» | исходники `?v=2026-08-06.1 / 1 / 6` (живой клиент 06.10 — работает) | заменить блок |
| `chery.atkmotors-vostok.ru`, `tenet.atkmotors-vostok.ru`, `mg.tank-borishof.ru`, `offer.km4-changanauto.ru`, `offer.solaris-vamauto.ru`, `sales.asavto-{samaraug,saratov,tlt}-chery.ru`, `sales.geely-vsk.ru` | 04, 05, 10, 12, 13, 06, 07, 08, 09 | страницы `/cabinet` нет (404) | не требуется; если бот выдаёт этим дилерам ссылку на кабинет — она ведёт на 404 (как и в 3.3.3) |
| `offer.gac-marsmotors.ru` | 11 GAC | сайт не ответил (ошибка соединения) | проверить вручную |

Блок T123 одинаковый для всех сайтов (бот один — боевой): заменить содержимое блока целиком,
остальное на странице не трогать, опубликовать.

```html
<div id="cabinet-root"></div>
<script>
  window.UNIFIEDBOT_URL = 'https://obut-5oaj-5i2u.gw-1a.dockhost.net';
</script>
<script src="https://obut-5oaj-5i2u.gw-1a.dockhost.net/widget-custom/cabinet-app/Production/calc_engine.browser.js?v=26b2fb5198"></script>
<script src="https://obut-5oaj-5i2u.gw-1a.dockhost.net/widget-custom/cabinet-app/Production/calc_model.js?v=d7828a95bc"></script>
<script src="https://obut-5oaj-5i2u.gw-1a.dockhost.net/widget-custom/cabinet-app/Production/app.js?v=968695878b"></script>
```

Проверка после публикации каждой страницы — как в 3.4 (три строки `Production/…`, прямой заход —
«Кабинет открывается по персональной ссылке из чата»). Откат — старые строки из 4.1, п. 1, с тем же
адресом бота. При следующем изменении кабинета — новые `?v=` из `npm run build:cabinet` на всех этих страницах.
