# Инструкция выката мультибота на прод (ветка feat/multibot-brand-routing, U1–U9)

**СТАТУС 2026-09-25:** этапы 0–2.3 ПРОЙДЕНЫ (прод на `multy-v3.3.1`,
стартовал чисто — Loaded 2 bots env=prod, сайт и /catalog проверены, боты
отвечают на старых адресах, ничего не перерегистрировано). Продолжение —
этап 2.4 на новом образе **`multy-v3.3.2`** (добавлена U9a: Webhook_Base —
своя база вебхука бота для Cloudflare-воркера TG); перед 2.4 владелец
обновляет код воркера (сохранение пути, см. врезку U9a в 2.4).

Дата составления: 2026-09-23. Прод на момент составления: образ `vitaln/unified-bot:multy-v3.2.2`
(= код 40ca973, ДО мультибота), MAX/TG подписаны на голые `/max_webhook` и
`/tg_webhook` (проверит `--check` на шаге 6). Все команды — по одной,
однострочные (веб-терминал Dockhost), токены нигде не вводятся руками и не
печатаются. Прод-БД — PostgreSQL 16.8.

**Предусловия (обязательные):** дев-стенд перенастроен под U9 (снятие дев-ботов
Haval, `*_TEST`-переменные, записи Bots, перерегистрация `max-test`/`tg-test`)
и ВСЕ живые дев-проверки пройдены (диплинк 99, алерт дилеру от бота бренда,
сверка бренда + дедуп, ремайндер, /catalog, гейт С4) — инструкции сессии
2026-09-23. Выкат — в период низкого трафика (ориентир — ночь/раннее утро по
часовым поясам дилеров). Ориентировочная длительность: этап 1 — 15–30 мин
(сборка, пуш, рестарт), этап 2 — 30–45 мин (проверки + поэтапная
перерегистрация с живыми сообщениями); окно деградации по плану — ноль
(голые адреса работают до и после перерегистрации).

Ключевой принцип перехода: боевые боты Geely НЕ молчат ни на одном шаге —
голые адреса вебхуков остаются алиасами (R7), новые имена env-переменных
добавляются ПАРАЛЛЕЛЬНО старым (фолбэк в коде), перерегистрация — по одному
боту с живой проверкой после каждого шага.

---

## Этап 0. Подготовка (до сборки образа)

### 0.1. Бэкап прод-PostgreSQL (веб-консоль контейнера БД)

1. `env | grep POSTGRES` — узнать имена переменных (обычно POSTGRES_USER/POSTGRES_DB).
2. `pg_dump -U $POSTGRES_USER -d $POSTGRES_DB -F c -f /tmp/backup_pre_multibot_20260923.dump`
3. `ls -l /tmp/backup_pre_multibot_20260923.dump` — размер БОЛЬШЕ нуля (пустой файл = бэкапа нет).
4. `pg_restore -l /tmp/backup_pre_multibot_20260923.dump | head -20` — оглавление читается, таблицы видны.
5. По возможности скачать файл из контейнера наружу (файловый менеджер Dockhost) — контейнерный /tmp не переживает пересоздание.

### 0.2. Env-переменные прод-приложения (Dockhost, добавить ПАРАЛЛЕЛЬНО, ничего не удалять)

- `BOT_ENV` = `prod`
- `PUBLIC_BASE_URL` = `https://obut-5oaj-5i2u.gw-1a.dockhost.net`
- `MAX_BOT_TOKEN_GEELY` = значение текущего `MAX_BOT_TOKEN` (тот же токен, новое имя)
- `TG_BOT_TOKEN_GEELY` = значение текущего `TG_BOT_TOKEN`
- `ALERT_BOT_KEY` = `max-geely` (служебный бот техалертов; без переменной код и так возьмёт легаси-ключ окружения — задать для явности)
- `MAX_BOT_TOKEN_HAVAL` / `TG_BOT_TOKEN_HAVAL` — ТОЛЬКО когда созданы боевые боты Haval (этап 3); раньше не заводить.

Старые `MAX_BOT_TOKEN`/`TG_BOT_TOKEN` НЕ удалять до конца этапа 2 (фолбэк).

### 0.3. NocoDB, таблица Bots (id ma0nf1e807qb9e0) — целевая схема 6 записей

Прод на старом образе таблицу Bots не читает — правки безопасны; дев после
правок поднимет env-минимум `max-test`/`tg-test` (штатно). Изменить руками в UI:

| Строка сейчас | Стало |
|---|---|
| `max-main` (Environment=all) | Bot_Key=`max-geely`, Token_Env=`MAX_BOT_TOKEN_GEELY`, Brands=`geely,belgee`, Environment=`prod`, Active=✓, Link=ссылка MAX-бота Geely |
| `tg-main` (all) | Bot_Key=`tg-geely`, Token_Env=`TG_BOT_TOKEN_GEELY`, Brands=`geely,belgee`, Environment=`prod`, Active=✓, Link=ссылка TG-бота Geely |
| `max-haval` (dev, тестовый пилот) | на этапе 3: Token_Env=`MAX_BOT_TOKEN_HAVAL`, Environment=`prod`, Link=ссылка MAX-бота «HAVAL Консультант»; ДО этапа 3 — Active=✗, Environment ОСТАВИТЬ `dev` |
| `tg-haval` (dev) | аналогично на этапе 3 (Link=`https://t.me/havalconsultantbot`); до него Active=✗, Environment `dev` |

**Порядок для haval-строк (важно):** сначала снять дев-подписки
(`--deregister`, п. 0.4), ПОТОМ править строки. `--deregister` работает и при
Active=✗, но откажет, если запись уже сменила Environment на `prod` (пропадает
из дев-реестра) или из дев-`.env` удалена переменная токена.
| — новая | Bot_Key=`max-test`, Channel=`max`, Brands=`*`, Token_Env=`MAX_BOT_TOKEN_TEST`, Environment=`dev`, Active=✓ |
| — новая | Bot_Key=`tg-test`, Channel=`telegram`, Brands=`*`, Token_Env=`TG_BOT_TOKEN_TEST`, Environment=`dev`, Active=✓ |

### 0.4. Дев-машина, `.env` (выполнить ДО правок haval-строк в 0.3)

Порядок строго такой (см. примечание в 0.3):

1. Снять дев-подписки Haval-ботов, по одной: `python scripts/register_webhooks.py --deregister max-haval --confirm-env=sporophoric-devorah-remonstrative.ngrok-free.dev`, затем то же с `tg-haval`. Это ОБЯЗАТЕЛЬНО до этапа 3: у MAX подписки складываются — не снятая дев-подписка означает дубли событий на два сервера; у TG прод-`setWebhook` молча перещёлкнет вебхук с дева.
2. Добавить `MAX_BOT_TOKEN_TEST` = значение `MAX_BOT_TOKEN`, `TG_BOT_TOKEN_TEST` = значение `TG_BOT_TOKEN`; старые имена удалить после успешных живых дев-проверок (до того код падает на фолбэк).
3. Удалить `BOT_TOKEN_MAX_HAVAL`/`BOT_TOKEN_TG_HAVAL` — значения переезжают в env прода на этапе 3 под именами `MAX_BOT_TOKEN_HAVAL`/`TG_BOT_TOKEN_HAVAL` (боевыми становятся ТЕ ЖЕ боты: TG `@havalconsultantbot` и MAX «HAVAL Консультант»; решение владельца 2026-09-23).

### 0.5. Группы дилеров (КРИТИЧНО — до этапа 3)

Новое правило: алерт дилеру уходит ТОЛЬКО от MAX-бота бренда дилера, фолбэка
нет. Для Geely/Belgee ничего не меняется — `max-geely` это тот же бот, что
слал алерты раньше, он уже состоит в группах. MAX-бота «HAVAL Консультант»
владелец добавляет во ВСЕ группы Haval-дилеров (у тестового дилера 01 —
группа `-72121297562985`) ДО включения Haval-записей в Bots (этап 3), иначе
алерты этих дилеров будут падать в техалерт админам.

---

## Этап 1. Сборка и выкат образа

1. Локально, из ветки `feat/multibot-brand-routing` (актуальный HEAD с U9a): `docker build --platform linux/amd64 -t vitaln/unified-bot:multy-v3.3.2 .`
   — НОВЫЙ тег; предыдущие теги (`multy-v3.3.1`, `multy-v3.2.2`) не перезаписывать — это образы отката.
2. Проверить, что в образ не попал легаси-скрипт с токеном: `docker run --rm vitaln/unified-bot:multy-v3.3.2 ls activate_webhook.py` — должно ответить «No such file or directory».
3. `docker push vitaln/unified-bot:multy-v3.3.2`
4. В Dockhost: убедиться, что env из 0.2 сохранены → сменить образ приложения на `multy-v3.3.2` → рестарт.

## Этап 2. Проверки после старта и перерегистрация Geely

### 2.1. Сразу после старта (логи приложения)

- строка `Loaded N bots from NocoDB (env=prod)` — N=2 (max-geely, tg-geely; haval пока Active=✗);
- НЕТ техалертов `bot-env-missing`, `token-missing:max-geely`, `token-missing:tg-geely`;
- живое сообщение боту Geely в MAX и в TG (подписки ещё на голых URL — алиасы работают) → бот отвечает.

### 2.2. Сайт и /catalog — СТОП-проверка (сразу после старта, ДО перерегистрации)

В образе едет allowlist `/catalog` — ошибка в нём ломает боевые лендинги.
Проверить с любого компьютера (по одной команде):

1. `curl -s -o /dev/null -w '%{http_code}\n' https://obut-5oaj-5i2u.gw-1a.dockhost.net/catalog/mywf0f16ptn25gk` — ожидание `200` (каталог машин).
2. `curl -s -o /dev/null -w '%{http_code}\n' https://obut-5oaj-5i2u.gw-1a.dockhost.net/catalog/mm8wldgk5vn9gwh` — `200` (оснащение).
3. `curl -s -o /dev/null -w '%{http_code}\n' https://obut-5oaj-5i2u.gw-1a.dockhost.net/catalog/muruk10ipof33m5` — `200` (дилеры).
4. `curl -s https://obut-5oaj-5i2u.gw-1a.dockhost.net/catalog/muruk10ipof33m5 | python3 -c "import sys,json;rows=json.load(sys.stdin)['list'];print(sorted(set().union(*[set(r) for r in rows])))"` — в выводе ТОЛЬКО 7 полей: `Dealer_Code`, `Dealer_address`, `Dealer_location`, `Dealer_name`, `Dealer_phone`, `Dealer_status`, `Dealer_workinghours`; никаких Max_Group_ID/Policy/Prompt_*/Business_Connection_ID.
5. `curl -s -o /dev/null -w '%{http_code}\n' https://obut-5oaj-5i2u.gw-1a.dockhost.net/catalog/mnivr6uyf5ckzru` — `404` (архив диалогов ЗАКРЫТ).
6. Открыть боевой лендинг в браузере: карточки машин рисуются, поп-ап оснащения открывается, блок контактов дилера заполнен.

Любой пункт не сошёлся → НЕМЕДЛЕННЫЙ откат на `multy-v3.2.2` (раздел «Откат»,
случай «до перерегистрации»), дальше по инструкции не идти.

### 2.3. SQL-проверки (веб-консоль контейнера БД, по одной)

1. `psql -U $POSTGRES_USER -d $POSTGRES_DB -c "SELECT count(*) AS total, count(*) FILTER (WHERE bot_key IS NULL) AS null_bot_key FROM users;"` — total прежний, null_bot_key = только строки неизвестных каналов (ожидаемо 0 или единицы).
2. `psql -U $POSTGRES_USER -d $POSTGRES_DB -c "SELECT bot_key, count(*) FROM users GROUP BY 1 ORDER BY 2 DESC;"` — ключи `max-geely`/`tg-geely`/`web`; строк `max-main`/`tg-main` НЕТ (шаг 2d переписал на старте).
3. `psql -U $POSTGRES_USER -d $POSTGRES_DB -c "SELECT bot_key, active, environment, link IS NOT NULL AS has_link FROM bot_registry_snapshot ORDER BY bot_key;"` — снапшот заполнен.

### 2.4. Перерегистрация вебхуков — по одному боту, TG первым (веб-консоль контейнера приложения)

**Предусловие (урок дев-прогона 2026-09-24):** приложение на `multy-v3.3.2`
успешно СТАРТОВАЛО и `init_db` отработал (разделы 2.1–2.3 пройдены, в т.ч.
SQL 2.3.3 показывает снапшот с колонкой link). Скрипт регистрации НЕ выполняет
миграцию схемы — запуск до первого старта нового образа падает на
`no such column: bot_registry_snapshot.link`. Ручной ALTER на проде не
выполнять — только штатная миграция стартом приложения.

**U9a (факты прод-прогона 2026-09-25):** боевой TG-бот подписан на
Cloudflare Worker `https://sparkling-sun-8c0c.vitalnmsk-cb1.workers.dev/tg_webhook`
(обход блокировки Telegram; воркер пересылает на наш хост, заголовки — а
значит и производный секрет — передаёт целиком). У бота теперь есть своя
база вебхука: колонка `Webhook_Base` в Bots (для `tg-geely` заполнена
воркером) — `--apply`/`--check` работают с ней автоматически (образ
`multy-v3.3.2`+). ПЕРЕД шагом 2 владелец обновляет код воркера: он должен
пересылать НЕ только `/tg_webhook`, а И `/tg_webhook/<bot_key>` — на тот же
путь нашего хоста (сохранение пути). Порядок строго: сначала воркер (новая
версия обратно совместима — старый путь продолжает работать), потом
`--apply`; наоборот нельзя — старый воркер отвечает 404 на путь с bot_key,
и TG-бот замолчит. Проверка после обновления воркера, ДО --apply: живое
сообщение TG-боту (идёт ещё по старому пути) — бот отвечает.

У MAX-бота при `--apply` будет удалена вторая, МУСОРНАЯ подписка на хост
n8n (`…/max_webhook`): по заключению n8n-агента 2026-09-25 такого вебхука
нет ни в одном из 43 воркфлоу (POST отвечает 404 до вебхук-роутера,
исполнений из неизвестного источника нет, в снапшоте БД n8n от 18.04.2026
записи не было) — удаление ожидаемо и безопасно, режим «добавить не удаляя»
не нужен.

1. `python scripts/register_webhooks.py --check` — сверить: BOT_ENV=prod, PUBLIC_BASE_URL прода, боты max-geely/tg-geely; у tg-geely в строке видна «база: https://sparkling-sun-8c0c…workers.dev», ожидание — воркерный URL с путём бота; фактические URL: TG — воркер `/tg_webhook`, MAX — голый `/max_webhook` + мусорная n8n-подписка.
2. `python scripts/register_webhooks.py --apply --confirm-env=obut-5oaj-5i2u.gw-1a.dockhost.net --bot tg-geely` — setWebhook на `<воркер>/tg_webhook/tg-geely` с производным секретом.
3. Проверка доставки TG: живое сообщение боту → ответ пришёл; в логе приложения события идут через `/tg_webhook/tg-geely`; `curl -s "https://api.telegram.org/bot$TG_BOT_TOKEN_GEELY/getWebhookInfo"` → `url` = воркер с путём `/tg_webhook/tg-geely`, `pending_update_count` = 0, `last_error_message` пуст.
4. `python scripts/register_webhooks.py --apply --confirm-env=obut-5oaj-5i2u.gw-1a.dockhost.net --bot max-geely` — POST подписки `/max_webhook/max-geely` + DELETE голой и DELETE мусорной n8n-подписки (ожидаемо, см. выше).
5. СРАЗУ живое сообщение MAX-боту (фильтр свежести 60 сек) → ответ пришёл; `--check` показывает ОДНУ подписку на `/max_webhook/max-geely`.
6. `python scripts/register_webhooks.py --enforce tg-geely` → **ЖДАТЬ подхвата** (см. ниже) → живое сообщение → бот ответил, в логе нет WARN о секрете.
7. `python scripts/register_webhooks.py --enforce max-geely` → **ЖДАТЬ подхвата** → живое сообщение → чисто.

**Промежуточное состояние «воркер не обновлён» — допустимо:** если новый код
воркера не готов, TG-бота НЕ перерегистрировать — он остаётся на воркере
`/tg_webhook` (легаси-алиас, резолвится в tg-geely), а `--apply` выполняется
только для max-geely. Это НЕ хуже текущего прода: TG-приём работает, но без
проверки секрета (`--enforce tg-geely` в этом состоянии НЕ включать —
подтверждённой регистрации нет, скрипт и сам откажет), сторож TG-подписку
не контролирует, intent-блок tg-geely пуст. Шаги 2–3 и enforce tg-geely
выполняются позже, после обновления воркера, отдельно.

**Задержка подхвата enforce (урок дев-прогона 2026-09-24):** работающее
приложение применяет включённую проверку секрета только после очередной
перезагрузки реестра фоновым воркером — интервал 600 секунд, в коде
захардкожен (bot_registry.registry_worker), на проде так же. После КАЖДОЙ
команды `--enforce` дождаться в логе приложения свежей строки
`✅ [bot_registry] Loaded N bots from NocoDB (env=prod)` (с временем ПОЗЖЕ
команды) и только потом делать живую проверку. Живая проверка сразу после
`--enforce` проходит по СТАРЫМ правилам — шаг будет засчитан ошибочно, а
через ≤10 минут бот может начать молча дропать события уже на реальных
клиентах. Если после подхвата бот НЕ отвечает: немедленно
`python scripts/register_webhooks.py --unenforce <bot_key>` и снова дождаться
строки перезагрузки реестра — приём вернётся к работе без проверки секрета;
дальше разбираться (`--check`: подтверждён ли секрет, совпадает ли URL).

### 2.5. Функциональная прод-проверка (тестовый дилер 01, бренд Haval)

Рабочий код заявки дилера 01: **`011416T1`** (01 + Model id 14 +
Complectation id 16 + произвольный хвост; каталог дилера 01 — та же таблица
`mywf0f16ptn25gk`, что у дев-дилера 99, у которого проверен код `991416T1` —
Haval Jolion). Payload: **`011416T1_HAVALJOLION`**.

Ссылки (диплинк — в ботов Geely, клиент входит через них):
- TG: `https://t.me/<username_боевого_TG-бота_Geely>?start=011416T1_HAVALJOLION` — username бота не установлен локально (боевые боты только в Dockhost); взять со ссылки лендинга Geely.
- MAX: ссылка боевого MAX-бота Geely с лендинга + тот же start-параметр (формат — как в U8-проверках на деве, только код `01…` вместо `99…`).
- После этапа 3 проверка дилера 01 идёт уже в Haval-бота: `https://t.me/havalconsultantbot?start=011416T1_HAVALJOLION` и ссылка MAX-бота «HAVAL Консультант».

Диплинк дилера 01 в бота Geely: диалог отвечает, сделка в Bitrix создаётся.

**Проверка фикса С4 (здесь же, на этой сессии; вживую на деве не проверялся —
решение владельца 2026-09-25, фикс покрыт 4 тестами):** довести сессию
дилера 01 до финализации Manager-путём (воронка → передача менеджеру,
алерт-карточка дилеру пришла ОДИН раз), убедиться, что кабинетный токен
авто-выдан (`psql … -c "SELECT id,status,bound_at FROM cabinet_tokens ORDER BY id DESC LIMIT 1;"`),
затем подождать >10 минут БЕЗ действий. Ожидание: НЕТ повторного алерта
менеджеру, НЕТ сообщения «Личный кабинет…» дилеру; в логе строка INFO
`cabinet: таймаут-передача (…) пропущена для user=… — сессия …`; токен в
статусе `revoked`. Повторный алерт или кабинетное сообщение = регресс С4 —
откат по инструкции.
Ожидаемые техалерты админам (подтверждают новые правила, НЕ ошибки):
`brand-mismatch:01:max-geely` (дилер Haval в боте Geely) и
`dealer-alert-fail:01:...` (бота бренда Haval ещё нет — алерт дилеру не
отправлен, без фолбэка). Карточка дилеру 01 придёт после этапа 3. Ремайндер/
farewell проверить на этой же сессии (приходят от бота Geely).

После стабилизации (сутки без техалертов token-missing): старые
`MAX_BOT_TOKEN`/`TG_BOT_TOKEN` можно удалить из env прода.

## Этап 3. Подключение боевых Haval-ботов

Решение владельца 2026-09-23: боевыми становятся ТЕ ЖЕ боты, что работали на
деве — TG `@havalconsultantbot` и MAX «HAVAL Консультант» (токены — бывшие
дев-`BOT_TOKEN_TG_HAVAL`/`BOT_TOKEN_MAX_HAVAL`). Новых ботов создавать не
нужно: по механике обоих мессенджеров бот привязан к токену, а «окружение» —
только к зарегистрированному вебхуку; перенос = снять дев-подписки и
зарегистрировать прод-URL. По коду ограничений тоже нет.

1. Убедиться, что дев-подписки этих ботов СНЯТЫ (`--deregister` из 0.4) и переменные удалены из дев-`.env`. Без этого: MAX будет слать события и на дев, и на прод (подписки складываются — дубли диалогов), TG-вебхук молча перещёлкнется и дев перестанет получать события без следа.
2. Env прода: `MAX_BOT_TOKEN_HAVAL` = значение бывшего дев-`BOT_TOKEN_MAX_HAVAL`, `TG_BOT_TOKEN_HAVAL` = значение `BOT_TOKEN_TG_HAVAL` (перенос значений — только через панель Dockhost, не через чат/логи) → рестарт приложения.
3. Владелец добавляет MAX-бота «HAVAL Консультант» во все группы Haval-дилеров (см. 0.5), включая группу дилера 01 `-72121297562985`.
4. NocoDB Bots: `max-haval` → Token_Env=`MAX_BOT_TOKEN_HAVAL`, Environment=`prod`, Active=✓, Link=ссылка MAX-бота; `tg-haval` → Token_Env=`TG_BOT_TOKEN_HAVAL`, Environment=`prod`, Active=✓, Link=`https://t.me/havalconsultantbot`, **Webhook_Base=`https://sparkling-sun-8c0c.vitalnmsk-cb1.workers.dev`** (TG-боту Haval нужен тот же обход блокировки; воркер сохраняет путь — один воркер обслуживает оба TG-бота, путь `/tg_webhook/<bot_key>` их различает). Реестр подхватит воркером ≤10 мин или очередным рестартом.
5. `python scripts/register_webhooks.py --check` → `--apply --confirm-env=obut-5oaj-5i2u.gw-1a.dockhost.net --bot tg-haval` → живое сообщение → то же для `max-haval` → живые проверки → `--enforce` по одному.
6. Повторить диплинк дилера 01 (теперь в Haval-бота: `https://t.me/havalconsultantbot?start=011416T1_HAVALJOLION`): карточка дилеру 01 приходит от MAX-бота Haval, техалертов dealer-alert-fail нет.
7. Ссылки на лендингах Tilda Haval — на этих ботов (payload не меняется); `/bot-links` отдаёт ссылки активных ботов для сайта.

## Откат

**Откат — временная мера.** Первая цель отката теперь — `multy-v3.3.1`
(в нём уже есть гейт /catalog и фикс С4, нет только U9a). Глубокий резерв
`multy-v3.2.2` возвращает ОТКРЫТЫЙ `/catalog` (архив диалогов и любые
таблицы снова читаются публично серверным токеном) и НЕ содержит фикса С4 —
на нём нельзя оставаться дольше, чем нужно для устранения причины отката.

- **До перерегистрации (этап 2.4 не начат):** в Dockhost вернуть образ `multy-v3.3.1` (резерв — `multy-v3.2.2`) → рестарт. Старый код игнорирует новые колонки users (`bot_key` и др. nullable), таблицу `bot_registry_snapshot`, записи Bots и колонку Webhook_Base — совместимо, схему БД откатывать не нужно.
- **После перерегистрации:** сначала вернуть подписки на голые URL (старый образ не имеет роутов `/..._webhook/{bot_key}`), по одной команде из веб-консоли приложения (токены — из env, в открытом виде не появляются):
  1. `curl -s "https://api.telegram.org/bot$TG_BOT_TOKEN_GEELY/setWebhook?url=https://sparkling-sun-8c0c.vitalnmsk-cb1.workers.dev/tg_webhook"` — ВАЖНО: TG возвращаем на ВОРКЕР (голый путь работает и в старой, и в новой версии воркера), НЕ на прямой хост — прямая доставка TG и есть причина обхода.
  2. `curl -s -X POST -H "Authorization: $MAX_BOT_TOKEN_GEELY" -H "Content-Type: application/json" -d "{\"url\":\"$PUBLIC_BASE_URL/max_webhook\"}" https://platform-api.max.ru/subscriptions`
  3. `curl -s -X DELETE -H "Authorization: $MAX_BOT_TOKEN_GEELY" "https://platform-api.max.ru/subscriptions?url=$PUBLIC_BASE_URL/max_webhook/max-geely"`
  4. живые сообщения обоим ботам — отвечают;
  затем вернуть образ `multy-v3.2.2` → рестарт. (Секрет старый образ не проверяет; `--unenforce` не требуется, но и не мешает.)
- **Восстановление БД из бэкапа** — только при повреждении данных (не при откате кода): `pg_restore -U $POSTGRES_USER -d $POSTGRES_DB --clean /tmp/backup_pre_multibot_20260923.dump`.

## Критерии успеха

1. Живые диалоги Geely в MAX и TG работают на каждом шаге (ни одного «немого» окна).
2. `--check` без расхождений; секреты enforced у max-geely/tg-geely (после 2.4) и haval-пары (после этапа 3).
3. `users`: ключи только `max-geely`/`tg-geely`/`web` (+составные новых ботов позже), `max-main`/`tg-main` отсутствуют.
4. Алерт дилеру Geely — от `max-geely`; дилеру Haval — от `max-haval` (после этапа 3); при отсутствии бота бренда — техалерт админам, сделка создаётся.
5. Ремайндеры/farewell приходят от бота сессии.
6. Нет техалертов `bot-env-missing`/`token-missing`/`dealer-alert-fail` в штатном режиме (после этапа 3).
