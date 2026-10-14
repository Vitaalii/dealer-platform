---
title: "feat: Redis для self-hosted Multica v0.6.2 — лимиты на /auth до подключения runtime"
type: feat
status: executed-2026-10-14 (V4 — за владельцем)
date: 2026-10-14
target_host: root@65.109.167.1 (multica.autodc.org)
related: docs/plans/2026-10-14-001-chore-multica-upgrade-v0.6.2-plan.md
---

# Redis для Multica v0.6.2: лимиты на вход до подключения runtime

**Статус:** план. На сервере выполнялись только команды на чтение. Выполнение — после решения владельца.
Ссылки на код — upstream `multica-ai/multica`, тег v0.6.2 (`7ab92d76`), пути от корня репозитория.

**Зачем.** После подключения runtime аккаунт Multica даёт возможность запускать агентов на машине-исполнителе.
Вход по коду сейчас без лимита по IP. Анализ — `docs/handoff/2026-10-14-multica-upgrade-plan.md`,
раздел «Защита входа по коду без Redis».

---

## 1. Что меняется при непустом `REDIS_URL`

| Компонент | Без Redis (сейчас) | С Redis | Код |
|---|---|---|---|
| Лимиты `/auth/send-code`, `/auth/google`, `/auth/verify-code`, `/api/contact-sales` | выключены (`return next`) | включены, фиксированное окно по IP | `server/internal/middleware/ratelimit.go:60-64`, `server/cmd/server/router.go:1500-1509` |
| Лимит Plugin API | выключен | 120/мин на bearer-токен | `server/internal/middleware/plugin_ratelimit.go:18-40`, `router.go:1618` |
| Лимиты autopilot-вебхуков | в памяти процесса | в Redis (те же значения) | `server/internal/handler/handler.go:511-513`, `router.go:515-517`, `autopilot_webhook.go:357-363` |
| Лимиты приглашений | в памяти процесса | в Redis (те же значения) | `router.go:449-453`, `router.go:518` |
| Realtime (WebSocket интерфейса) | in-memory hub | hub + relay через Redis Streams, режим `sharded` по умолчанию | `server/cmd/server/main.go:112-124`, `main.go:515-594` |
| Пробуждение daemon по WebSocket | только локально | локально + через relay | `server/internal/daemonws/notifier.go:32-45`, `main.go:541,548` |
| Liveness runtime (heartbeat) | каждый heartbeat пишет `last_seen_at` в БД | TTL-ключ в Redis, БД — авторитет и fallback | `server/internal/handler/runtime_liveness_store.go:13-22`, `main.go:735-737`, `router.go:514` |
| Кэши токенов и членства | нет, каждый запрос в БД | PAT 10 мин, daemon-токен, членство 5 мин; **попадание в кэш PAT аутентифицирует без запроса в БД** | `server/internal/auth/pat_cache.go:19`, `membership_cache.go:13-24`, `router.go:1369-1373`, `server/internal/middleware/auth.go:208-214`, `daemon_auth.go:204-212` |
| Кэши daemon-опроса задач | нет | empty-claim и reclaim-check | `router.go:1390,1394` |
| Сторы списков (обновления, модели, local skills) | в процессе | в Redis | `router.go:508-513` |
| WeCom, channel leases | не используются у вас | не используются (`CHANNEL_WS_LEASE_BACKEND` не задан) | `main.go:555-567,596-604` |

**Риски для интерфейса — низкие, по коду:**
- Realtime сначала доставляет событие локально, потом пишет в Redis; петля по тому же id отбрасывается
  (`server/internal/realtime/redis_relay.go:669-676`). Если Redis упадёт, интерфейс продолжит получать
  события от локального hub.
- Liveness и кэши при недоступном Redis откатываются на БД (комментарии в `runtime_liveness_store.go:13-22`,
  `membership_cache.go:25-26`).
- Лимиты при ошибке Redis пропускают запрос (`ratelimit.go:71-74`).
- `/readyz` Redis **не проверяет** (`server/cmd/server/health.go:68-71`), поэтому состояние Redis
  проверяется отдельно: лог, healthcheck контейнера.

**Что меняется по поведению:**
- Отзыв членства в workspace может действовать до 5 минут, отзыв PAT — до 10 минут, если Redis был
  недоступен в момент отзыва. При доступном Redis кэш инвалидируется сразу
  (`server/internal/handler/personal_access_token.go:274`, `workspace_revoke.go:260`, `workspace.go:639,699,743`).
- Данные в Redis временные, том не нужен (раздел 4). При перезапуске Redis сбрасываются счётчики лимитов,
  кэши остывают, теряется окно повторной доставки realtime (5 мин). Liveness-ключи теряются: пустой
  Redis считается доступным, поэтому до следующего heartbeat sweeper может кратковременно пометить
  runtime offline (`runtime_liveness_store.go:121-130`, `runtime_sweeper.go:376-389`). Fallback на БД
  срабатывает только при ошибке Redis.
- **Redis становится хранилищем, влияющим на аутентификацию.** Тот, кто может писать в Redis, может
  положить в `mul:auth:pat:<sha256>` свой токен с `user_id` владельца и войти от его имени. Поэтому
  у Redis обязателен пароль (раздел 4).

---

## 2. Какие эндпоинты попадают под лимиты

| Эндпоинт | Значение по умолчанию | Ключ | Переменная |
|---|---|---|---|
| `POST /auth/send-code` | 5/мин | путь + IP | `RATE_LIMIT_AUTH` |
| `POST /auth/google` | 5/мин | путь + IP | `RATE_LIMIT_AUTH` |
| `POST /auth/verify-code` | 20/мин | путь + IP | `RATE_LIMIT_AUTH_VERIFY` |
| `POST /api/contact-sales` | 5/час | путь + IP | `RATE_LIMIT_CONTACT_SALES` |
| Plugin API | 120/мин | sha256 bearer-токена | `RATE_LIMIT_PLUGIN_API` |
| Autopilot-вебхуки | по триггеру и IP | IP по `MULTICA_TRUSTED_PROXIES` | — |
| Создание приглашений | 10 за 10 мин / 50 и 6 за сутки | инициатор / workspace / получатель | `RATE_LIMIT_INVITATION_*` |

Ключ лимита по IP — `mul:ratelimit:<путь без ведущего / и с : вместо />:<ip>`, например
`mul:ratelimit:auth:verify-code:65.109.167.1` (`ratelimit.go:130-134`).

**Daemon, CLI и агенты не ограничиваются.** Других вызовов `middleware.RateLimit` в v0.6.2 нет.
Регистрация runtime, heartbeat, claim задач, комментарии и токены идут через `/api/...` и WebSocket
под PAT или daemon-токеном, без лимита по IP. Plugin API считается по токену, а не по IP.

**Какой IP видит backend:**
- Браузер и daemon ходят по цепочке `Caddy → frontend (Next.js, 172.18.0.4) → backend`.
  `REMOTE_API_URL=http://backend:8080`; в production Next.js проксирует `/api/:path*` и `/auth/:path*`
  через `apps/web/proxy.ts` (`runtimeRewriteDestination`; в `next.config.ts` то же правило для сборки). `RemoteAddr` у backend — адрес контейнера frontend.
- `/ws` идёт по цепочке `Caddy → 127.0.0.1:8080` через docker-proxy. На сервере 2 процесса
  `docker-proxy`, поэтому `RemoteAddr` — шлюз `172.18.0.1`.
- Сеть `multica_default` — `172.18.0.0/16` (`docker network inspect`).

**Без `RATE_LIMIT_TRUSTED_PROXIES` все клиенты попадают в одну корзину.** Лимит становится общим на
весь сайт; upstream прямо предупреждает об этом (`.env.example:528-536`). Последствие для вас:
посторонний может каждую минуту тратить 5 запросов на `send-code` и 20 на `verify-code` и этим не
давать владельцу войти по коду. Действующие сессии это не затрагивает: cookie живёт 30 дней
(`server/internal/auth/cookie.go:47`). Daemon и агентов не затрагивает тоже.

**`RATE_LIMIT_TRUSTED_PROXIES` задавать нужно, значение — `172.18.0.0/16`:**
- `extractIP` доверяет `X-Forwarded-For` только при `RemoteAddr` из доверенных сетей и берёт самый
  правый адрес, не входящий в них (`ratelimit.go:91-112`).
- Caddy `reverse_proxy` по умолчанию передаёт в upstream `X-Forwarded-For` с адресом клиента.
- Next.js 16.3.8 проксирует с `xfwd` выключенным (`proxy-request.js` в контейнере frontend: в опциях
  только `changeOrigin`, `x-forwarded-host`). Значит, входящий `X-Forwarded-For` от Caddy уходит в
  backend как есть, и правее адреса клиента в нём ничего нет.
- Подделка заголовка клиентом не помогает: даже если Caddy допишет, а не заменит его, самым правым
  недоверенным останется реальный адрес клиента.
- Это вывод по коду. Шаг V3 проверяет его фактически: видно, какой IP backend записал в ключ.

**Переменная должна попасть в контейнер.** `docker-compose.selfhost.yml` v0.6.2 передаёт в backend
`REDIS_URL` и `REDIS_CLUSTER_MODE` (строки 70-71), но **не** `RATE_LIMIT_*`. Поэтому
`RATE_LIMIT_TRUSTED_PROXIES` (и `RATE_LIMIT_AUTH_VERIFY`, если решите его менять) задаётся в
`environment:` сервиса backend в `compose.override.yml`. В `.env` эта переменная ничего не даст.

**Вариант через Caddy** — не часть этого плана. Если V3 покажет, что backend видит не адрес клиента,
выполняется откат U6, а этот вариант выносится на отдельное решение владельца. Штатная директива без
пересборки: в `reverse_proxy 127.0.0.1:3000` добавить `header_up X-Forwarded-For {remote_host}`.
Сейчас по коду он не требуется.

**Autopilot-вебхуки** берут IP по отдельной переменной `MULTICA_TRUSTED_PROXIES` (пуста). У вас
вебхуков нет; их лимиты и сейчас работают в памяти с общей корзиной. Вне рамок.

---

## 3. Закрывает ли лимит гонку при проверке кода

**По коду: не закрывает, а ограничивает сверху.**
- Лимит срабатывает в middleware **до** обработчика. `INCR` и `EXPIRE` выполняются атомарно одним
  Lua-скриптом (`ratelimit.go:15-26`). При `count > limit` возвращается 429, обработчик не вызывается
  (`ratelimit.go:76-82`). Значит, за окно 60 с обработчик `VerifyCode` вызывается не больше `limit` раз
  на один ключ (путь + IP), сколько бы параллельных запросов ни пришло.
- Сама гонка в обработчике остаётся. Сначала выбирается код с `attempts < 5`
  (`server/pkg/db/queries/verification_code.sql:6-13`), потом сравнение, потом отдельный инкремент
  (`server/internal/handler/auth.go:418-426`). Внутри пропущенных лимитом запросов параллельные всё
  ещё могут обойти счётчик 5.
- Итог по попыткам на один IP: не больше `RATE_LIMIT_AUTH_VERIFY` в минуту.
  - По умолчанию 20 → ~28 800 в сутки, ~2,9 % шанса подобрать код за сутки.
  - При `RATE_LIMIT_AUTH_VERIFY=5` → не больше 5 в минуту, ~0,7 % за сутки. Это равно задуманной
    защите «5 попыток на код, 1 код в минуту на email»: ограничение на email — в обработчике
    `send-code` (`auth.go:364-366`), независимо от лимита 5/мин по IP.
  - Атакующий с многими IP получает лимит на каждый IP. Полностью гонку закрывает только атомарный
    инкремент в коде Multica — это upstream, вне рамок.
- Рекомендация: `RATE_LIMIT_AUTH_VERIFY=5` (В1). Владельцу при входе нужно 1–2 проверки.

---

## 4. Изменения

**`/opt/multica/compose.override.yml`** — добавить сервис `redis` и дополнить `backend`; healthcheck'и
backend и frontend остаются как есть. Схема:

```yaml
services:
  redis:
    image: redis:7.4.11-alpine3.21@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499
    environment:
      REDIS_PASSWORD: ${REDIS_PASSWORD:?REDIS_PASSWORD must be set}
      REDISCLI_AUTH: ${REDIS_PASSWORD}          # для healthcheck и redis-cli внутри контейнера
    command: ["sh", "-c", "exec redis-server --save '' --appendonly no --maxmemory 128mb --maxmemory-policy noeviction --requirepass \"$$REDIS_PASSWORD\""]
    mem_limit: 256m
    tmpfs:
      - /data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5
    restart: unless-stopped
  backend:
    depends_on:
      redis:
        condition: service_healthy
    environment:
      REDIS_URL: redis://:${REDIS_PASSWORD:?REDIS_PASSWORD must be set}@redis:6379/0
      RATE_LIMIT_TRUSTED_PROXIES: "172.18.0.0/16"
      RATE_LIMIT_AUTH_VERIFY: "5"        # по решению В1
    healthcheck: ...                     # без изменений
  frontend:
    healthcheck: ...                     # без изменений
```

**Обоснование настроек:**
- **Образ.** `redis:7.4` — та же ветка, что в upstream CI (`.github/workflows/ci.yml:327`,
  `redis:7-alpine`). 7.4.11-alpine3.21 — текущий `7-alpine`. Index digest
  `sha256:858f009f…3499`, есть `linux/amd64` (`sha256:ca0acbb1…01af2`), проверено через registry.
  Требование upstream — Redis 6.2+ для `XTRIM MINID` (`.env.example:461`).
- **Без тома, персистентность выключена** (`--save ""`, `--appendonly no`, `/data` в tmpfs). Всё в Redis
  временное (раздел 1). tmpfs заменяет анонимный том, который образ объявляет для `/data`.
- **Память.** Relay: 8 шардов × до 2000 записей (`server/internal/realtime/sharded_stream_relay.go:18,22`)
  — порядок десятков MB. `maxmemory 128mb`, лимит контейнера 256m. Свободной памяти на сервере ~3 GB.
- **`noeviction`**, а не LRU: вытеснение молча сбрасывало бы счётчики лимитов. При переполнении запись
  падает, лимит пропускает запрос с предупреждением в логе (fail-open) — это видно в мониторинге.
- **Без публикации портов и с паролем.** Redis доступен только в `multica_default` (после изменения
  там четыре контейнера: postgres, backend, frontend, redis). Пароль обязателен из-за кэша PAT
  (раздел 1): иначе любой процесс в сети — например, после взлома интернет-доступного frontend —
  может подделать вход владельца или сбросить счётчики лимитов. Пароль лежит только в `.env`
  (права 600). В `command` стоит `$$REDIS_PASSWORD`, который раскрывается внутри контейнера, поэтому
  в `docker inspect` пароль виден в env — так же, как `JWT_SECRET` у backend.
  `CONFIG`/`FLUSHALL` доступны держателю пароля; переименование команд — вне рамок.
- **`depends_on` с `service_healthy`** — порядок старта. Backend и без него переживает недоступность
  Redis на старте (`main.go:500-503`).

**`/opt/multica/.env`** — добавить строку `REDIS_PASSWORD=<openssl rand -hex 32>`; значение не выводится.
Сейчас в `.env` нет строк `REDIS_URL=` и `RATE_LIMIT_*` (`grep -c` → 0).

**Отличие от постановки (`REDIS_URL` в `.env`):** `REDIS_URL` задаётся в `environment:` backend в
override и собирается из `${REDIS_PASSWORD}`. Так пароль хранится в `.env` в одном месте, а не
дублируется в URL. Ключ `REDIS_URL` в override заменяет проброс `${REDIS_URL:-}` из upstream
compose (слияние `environment` по ключу). Если хотите строго в `.env` — можно положить туда полный URL
с паролем; это две строки с одним секретом.

---

## 5. Шаги выполнения

`$` — сервер, `mac$` — Mac. Одна строка — одна команда. Окно не на 03:30 UTC. Простой: только
пересоздание backend, без миграций — ~20–40 с до ответа на запросы; статус `healthy` — до 3 мин.

### U1. Дамп и копии перед изменением

1. `$ /opt/multica/backup.sh; echo "exit=$?"` → `exit=0`.
2. `$ ls -lt /var/backups/multica | head -3` → `<STAMP>` свежего дампа.
3. `$ mkdir -m 700 /var/backups/multica-pre-redis`
4. `$ cp -p /var/backups/multica/db-<STAMP>.dump /var/backups/multica-pre-redis/`
5. `$ docker exec -i multica-postgres-1 pg_restore -f /dev/null < /var/backups/multica-pre-redis/db-<STAMP>.dump; echo "exit=$?"` → `exit=0`.
6. `$ cp -p /opt/multica/compose.override.yml /opt/multica/compose.override.yml.bak-pre-redis`
7. `$ cp -p /opt/multica/.env /opt/multica/.env.bak-pre-redis`
8. `$ sha256sum /opt/multica/compose.override.yml /opt/multica/.env /etc/caddy/Caddyfile | tee /var/backups/multica-pre-redis/checksums.txt`

Миграций и изменений схемы нет. Дамп — страховка.

### U2. Образ Redis

9. `$ docker pull redis:7.4.11-alpine3.21@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499`
10. `$ docker image inspect --format '{{index .RepoDigests 0}}' redis@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499`
    → `redis@sha256:858f009f…3499`. Иначе стоп.

### U3. Конфигурация

11. Записать новый `compose.override.yml` по схеме из раздела 4. Файл готовлю я, загружаю `scp`,
    ставлю через `install -m 644` — права как у текущего. В файле только ссылка `${REDIS_PASSWORD}`, секрета нет.
12. `$ grep -c '^REDIS_PASSWORD=' /opt/multica/.env` → `0` (строки ещё нет).
12a. `$ sed -i "\$a REDIS_PASSWORD=$(openssl rand -hex 32)" /opt/multica/.env` — значение не выводится.
13. `$ grep -cE '^REDIS_PASSWORD=[0-9a-f]{64}$' /opt/multica/.env` → `1`; `$ stat -c '%a' /opt/multica/.env` → `600`.
14. `$ /opt/multica/mc config --quiet; echo "exit=$?"` → `exit=0`.
15. `$ /opt/multica/mc config --images` → образы `multica-backend`/`multica-web` v0.6.2, pgvector, redis с digest.
16. `$ /opt/multica/mc config | grep -E '^\s+(RATE_LIMIT_TRUSTED_PROXIES|RATE_LIMIT_AUTH_VERIFY):'` → две строки.
16a. `$ /opt/multica/mc config | grep -cE '^\s+REDIS_URL: redis://:[0-9a-f]{64}@redis:6379/0$'` → `1`
    (выводится счётчик; сам URL с паролем не печатать).

### U4. Применение

17. `$ /opt/multica/mc up -d` → создан `multica-redis-1`, пересоздан `multica-backend-1`; postgres и frontend — `Running`.
    Если healthcheck redis не пройдёт, backend не стартует (`depends_on: service_healthy`) — сразу U6.
18. `$ docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'` → четыре контейнера `healthy`; backend — до 3 мин.

### U5. Проверки

- **V1. Redis подключён:**
  `$ /opt/multica/mc logs --no-log-prefix backend 2>&1 | grep -E "Redis relay enabled|REDIS_URL is unset|auth rate limiting disabled|invalid REDIS_URL|redis error"`
  → только `realtime: Redis relay enabled … mode=sharded`. Строк про отключение и ошибки нет.
- **V2. readyz:** `$ curl -fsS 127.0.0.1:8080/readyz` → `db:ok`, `migrations:ok`.
- **V3. Лимит срабатывает, ключ — по адресу клиента; владельца не задевает.** Тест идёт с самого
  сервера (для Caddy клиент — 65.109.167.1), на несуществующий email. `verify-code` без строки кода в
  базе возвращает 400 и ничего не пишет (`auth.go:418-421`).
  - `$ for i in $(seq 1 7); do curl -s -o /dev/null -w '%{http_code} ' -X POST https://multica.autodc.org/auth/verify-code -H 'Content-Type: application/json' -d '{"email":"ratelimit-probe@example.invalid","code":"000000"}'; done; echo`
    → при `RATE_LIMIT_AUTH_VERIFY=5`: `400 400 400 400 400 429 429`; при 20 — повторить 21 раз.
  - `$ docker exec multica-redis-1 redis-cli --scan --pattern 'mul:ratelimit:auth:verify-code:*'`
    → один ключ `mul:ratelimit:auth:verify-code:65.109.167.1` (домен на сервере резолвится в
    65.109.167.1, `/etc/hosts` его не переопределяет — проверено). Если IP другой (`172.18.x.x`,
    `127.0.0.1`) — доверие к прокси не сработало: откат U6, вариант Caddy — отдельным решением.
  - Подделка заголовка: та же команда, один запрос с `-H 'X-Forwarded-For: 203.0.113.9'`, затем снова
    `--scan` → нового ключа с `203.0.113.9` нет.
  - Контроль, что владелец не заблокирован: `mac$ curl -s -o /dev/null -w '%{http_code}\n' -X POST https://multica.autodc.org/auth/verify-code -H 'Content-Type: application/json' -d '{"email":"ratelimit-probe@example.invalid","code":"000000"}'`
    → `400`, не 429. Ключи живут 60 с.
- **V4. Интерфейс и realtime:** владелец входит в UI, открывает AUTO-1 во **второй вкладке**, меняет
  что-то в первой (комментарий или статус) — вторая обновляется без перезагрузки. Затем вернуть изменение.
- **V4a. Redis закрыт без пароля:**
  `$ docker run --rm --network multica_default redis@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499 redis-cli -h redis ping`
  → `NOAUTH Authentication required.`
- **V5. Порты снаружи:** `mac$ nc -z -G 5 65.109.167.1 6379` → закрыт. Плюс 3000/8080/5432, как в D7 прошлого плана.
- **V6. Redis здоров:**
  - `$ docker exec multica-redis-1 redis-cli INFO memory | grep -E '^used_memory_human|^maxmemory_human'`
  - `$ docker exec multica-redis-1 redis-cli CONFIG GET maxmemory-policy` → `noeviction`.
- **V7. Бэкап работает:** `$ /opt/multica/backup.sh; echo "exit=$?"` → `exit=0`.
- **V8. VERSIONS.md:** добавить строку `redis: redis@sha256:858f009f…3499 (7.4.11-alpine3.21)` и заметку.
- **V9. UPGRADE.md:** в раздел 0 добавить проверку, что новая версия compose не добавила свой сервис
  `redis` и не сменила проброс `RATE_LIMIT_*`.

### U6. Откат (если любая из V1–V4a не прошла или redis не стал healthy)

1. `$ cp -p /opt/multica/compose.override.yml.bak-pre-redis /opt/multica/compose.override.yml`
2. `$ cp -p /opt/multica/.env.bak-pre-redis /opt/multica/.env`
3. `$ /opt/multica/mc up -d --remove-orphans` → backend пересоздан без Redis, контейнер `multica-redis-1` удалён
   (`--remove-orphans` убирает и другие осиротевшие контейнеры проекта `multica`, если они есть).
4. `$ sha256sum -c /var/backups/multica-pre-redis/checksums.txt` → `OK` для override и `.env`.
5. Проверки: V2, лог `realtime: REDIS_URL is unset`, сайт 200.

Данные в БД Redis не меняет, поэтому дамп при откате не нужен; он лежит на крайний случай.

---

## 6. Решения владельца (2026-10-14)

В1 — 5/мин на IP; В2 — пароль в `.env`, URL в override; В3 — выполняет агент целиком.

### Варианты, которые рассматривались

- **В1. `RATE_LIMIT_AUTH_VERIFY`:**
  - `5` на IP — рекомендую: соответствует задуманной защите, владельцу хватает;
  - `20` на IP — значение upstream по умолчанию;
  - общая корзина без `RATE_LIMIT_TRUSTED_PROXIES` — самая жёсткая защита от перебора, но посторонний
    может постоянно не давать владельцу войти. Не рекомендую.
- **В2. Где хранить `REDIS_URL`.** Рекомендую: в `.env` только `REDIS_PASSWORD`, URL собирается в
  override (раздел 4). Альтернатива — полный URL с паролем в `.env`, как в постановке.
- **В3. Кто выполняет** — вы по шагам или я целиком, с теми же стоп-условиями, что в прошлый раз.

---

## 7. Риски

| Риск | Вероятность | Мера |
|---|---|---|
| backend видит IP прокси, лимит общий на всех | низкая по коду | V3 показывает IP в ключе; при провале — откат U6 |
| Запись в Redis посторонним процессом (подделка входа через кэш PAT, сброс счётчиков) | низкая при пароле | Пароль обязателен, V4a |
| Redis недоступен | низкая | fail-open: лимиты пропускают, realtime и liveness работают от локального hub и БД; healthcheck и лог |
| Переполнение памяти Redis | низкая (десятки MB) | `noeviction` + V6; при переполнении лимит пропускает, в логе `redis error` |
| Задержка отзыва PAT до 10 мин, членства до 5 мин, если Redis недоступен в момент отзыва | низкая | Отзывать при здоровом Redis; после экстренного отзыва — перезапуск redis |
| Ложная блокировка владельца | низкая | Лимит по IP; тест с сервера; ключи живут 60 с |
| Гонка в `VerifyCode` | остаётся в пределах лимита | `RATE_LIMIT_AUTH_VERIFY=5`; атомарный инкремент — upstream |

## Что не проверено

- Фактическая передача `X-Forwarded-For` по цепочке Caddy → Next.js → backend: вывод по коду; проверка — V3.
- Синтаксис `--requirepass "$$REDIS_PASSWORD"` в `sh -c` проверяется при U4/V4a; при ошибке redis не станет healthy → U6.
- Нагрузочного теста гонки нет.
