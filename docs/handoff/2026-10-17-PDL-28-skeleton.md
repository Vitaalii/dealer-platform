# PDL-28 — этап 5.1: каркас платформы на сервере, итог

Дата: 2026-10-17. План — PDL-27 (комментарий «ИТОГ PDL-27»), одобрен Координатором 17.10 после ответов владельца
(«1 — А, 2 — А, 3 — А»). План этапа — `docs/coordination/plans/2026-10-16-этап-5-фундамент.md`, задача 5.1.

## Что сделано

- `infra/compose.yaml` — 7 сервисов: `caddy`, `postgres`, `migrate`, `platform`, `nocodb`, `n8n`, `bot`
  (заглушка). Боевые лимиты по `ARCHITECTURE.md` 7.3, у каждого `restart: unless-stopped` (у разового `migrate` —
  `"no"`), у долгоживущих — `healthcheck`. Порты наружу (80, 443) — только у Caddy. Ключи — `${VAR:?}`: compose не
  стартует без ключа.
- `infra/compose.test.yaml` — лимиты теста (сумма 4 480 МиБ). Выбор файлов — `COMPOSE_FILE` в `infra/.env`.
- `infra/.env.example` — 17 переменных без значений.
- `infra/caddy/Caddyfile` — три адреса из окружения: платформа, n8n, NocoDB.
- `infra/postgres/` — образ `FROM postgres:17`, скрипт первого запуска `init/10-roles.sh` (7 ролей, базы
  `platform`, `n8n`, `nocodb_meta`; пароли через `\getenv`, не в командной строке), место под pgBackRest.
- `infra/db/` — исполнитель миграций (`platform_meta.schema_migrations`, advisory-блокировка, транзакция на
  миграцию) и миграция `0001`: схемы `catalog`, `content`, `platform_ro`, права через `ALTER DEFAULT PRIVILEGES`,
  проверочная таблица `platform_ro.rights_check`.
- `infra/platform/Dockerfile` (+ `Dockerfile.dockerignore`) — образ платформы из корня репозитория.
- `infra/bot-stub/` — заглушка бота на стандартной библиотеке, `/health`.
- `core/platform_core/` — ядро: настройки и проверка ключей (`CONFIG ERROR: missing ИМЯ`, код 1), `/health`
  (200/503), пул asyncpg, подключение модулей через `setup(app)`. `core/requirements.txt`: `aiohttp==3.13.3`,
  `asyncpg==0.30.0`.
- `leads/leads/`, `dealer-cabinet/dealer_cabinet/`, `billing/billing/` — пустые пакеты с `setup(app)`.
- `infra/README.md` — инструкция владельцу: DNS и проверка `dig`, вход по ключу, обновление, пользователь
  `deploy`, запрет паролей и root, ufw, Docker, копия образа диска Hostkey, Deploy key и `git clone`, `.env`,
  первый запуск, проверка (включая запрет записи `nocodb_src` в `platform_ro`), что делать при ошибках.
- README в `core/`, `leads/`, `dealer-cabinet/`, `billing/`.

Отличия от плана PDL-27 (мелкие, без новых решений):

- `COMPOSE_PROJECT_NAME` не в `.env`: имя проекта задано в `compose.yaml` (`name: dealers`).
- Добавлена переменная `PLATFORM_TIMEZONE` (часовой пояс n8n, `GENERIC_TIMEZONE`/`TZ`): значение выбирает
  владелец, в README пример `Europe/Moscow`.
- NocoDB: вместо `NC_PUBLIC_URL` — `NC_SITE_URL` (по документации NocoDB `NC_PUBLIC_URL` — устаревшее имя).
- n8n: `N8N_WEBHOOK_URL` вместо `WEBHOOK_URL` (по документации n8n `WEBHOOK_URL` устарел с 2.35.0).
- Файл SSH называется `00-platform.conf`, а не `99-…`: в `sshd_config.d` действует первое найденное значение.
- Пароли в `.env` заполняются одной командой (цикл `openssl rand -hex 32` + `sed`), без вывода на экран.

## Что проверено и как

- `python3 -m unittest discover -s core/tests -t core` — 15 тестов, все прошли: проверка ключей (нет переменной
  → код 1, в логе только имя), пароль не попадает в `repr`, `/health` 200/503 без адресов, подключение модулей.
- `python3 -m unittest discover -s infra/tests -t infra` — 33 теста, все прошли: состав сервисов, лимиты боя
  = 7.3, лимиты теста ≤ боя и сумма < 5 ГиБ, порты только у Caddy, healthcheck, порядок `migrate` → `platform`,
  настройки n8n по 7.3, нет паролей открытым текстом; `.env.example` без значений и совпадает с переменными
  compose; пароли ролей переданы в контейнер; миграции (порядок, дубликаты, у `nocodb_src` в `platform_ro`
  только `SELECT`, у бота и n8n только чтение); заглушка бота отвечает на `/health`; `bash -n` скрипта ролей и
  его отказ без пароля.
- `python3 -m py_compile` всех новых `.py` — без ошибок (Python 3.11.9 у исполнителя).
- Названия переменных и команды сверены с официальной документацией (разрешено в «Границах»): n8n — database,
  executions, deployment, endpoints, binary-data, monitoring (`/healthz/readiness`), установка Docker и Compose;
  NocoDB — environment-variables (`NC_DB`, `NC_AUTH_JWT_SECRET`, `NC_SITE_URL`, `NC_DISABLE_TELE`), `/api/v1/health`,
  порт 8080; Docker — установка на Ubuntu (команды в README дословно), ignore-файл рядом с Dockerfile,
  интерполяция `${VAR:?}`/`${VAR:-}`, `COMPOSE_FILE` из `.env`; Caddy — глобальная опция `email`, `{$VAR}`.
  Адрес docs.nocodb.com перенаправляет на nocodb.com/docs — читал там.
- Зона: `git diff --name-only main...HEAD` — только `core/`, `infra/`, `leads/`, `dealer-cabinet/`, `billing/`,
  `docs/handoff/`.
- Секреты: просмотр диффа `grep` по `password|secret|token|key` с присваиванием — только имена переменных и
  тестовые фиктивные строки.

## Что не проверено

- gitleaks не установлен (`command -v gitleaks` — пусто); ставить нельзя. Проверка — только `grep`.
- `docker compose config`, сборка образов, запуск стека, выпуск сертификатов, реальные права ролей — нельзя:
  у исполнителя нет плагина compose (`docker: unknown command: docker compose`) и доступа к демону. Это шаги 10–11
  README у владельца.
- psql `\getenv` (PostgreSQL 15+) — по памяти, документацию PostgreSQL не открывал (её нет в разрешённом списке).
  Если не сработает — `postgres` упадёт на первом запуске с ошибкой в логе (README, раздел 12).
- Проверка в README `psql -U nocodb_src` внутри контейнера без пароля опирается на то, что в образе `postgres`
  локальный вход — `trust` [не проверено].
- Путь данных NocoDB `/usr/app/data` и наличие `node` в образе NocoDB (для healthcheck) — в документации NocoDB не
  нашёл. Если путь другой — загрузки файлов NocoDB не переживут пересоздание контейнера (метаданные в PostgreSQL
  не пострадают).
- Healthcheck Caddy через `wget` на `127.0.0.1:2019` — не проверено, что `wget` есть в образе `caddy:2`.
  Если нет — Caddy будет `unhealthy`, но работать (от его здоровья ничего не зависит).
- Тег `n8nio/n8n:2.43.0`: версия 2.43 — последняя в release notes n8n (2026-10-06), точный тег на Docker Hub не
  проверял. NocoDB — `latest`: последнюю версию по документации установить не удалось. Обе версии можно задать
  в `.env` (`N8N_VERSION`, `NOCODB_VERSION`) без правки кода; закрепление — в 5.2.
- Тесты ядра на `aiohttp` (`app.py`, `db.py`) не запускались: пакетов у исполнителя нет. `/health` с базой
  проверяет владелец (шаг 11).
- Текст ответов команд Ubuntu (`ufw`, `adduser`, `ssh -T git@github.com`) в README — по известному поведению, не
  с этого сервера.

## Открытые вопросы

- NocoDB как внешний источник: у `nocodb_src` нет права создавать таблицы в `catalog`/`content` (только правка
  строк). Если на стенде 5.6 грабер или владелец должны создавать таблицы через NocoDB — нужна миграция с `CREATE`.
- Часовой пояс n8n (`PLATFORM_TIMEZONE`) — выбирает владелец при заполнении `.env`.

## Что дальше

- Ревьюер проверяет PR; затем Координатор.
- Владелец проходит `infra/README.md` на тестовом сервере, по одной команде; ошибки — в задачу (без паролей).
- 5.2 — выкатка и закрепление версий образов; 5.3 — pgBackRest и мониторинг; 5.5 — каталог марок в `catalog`.
