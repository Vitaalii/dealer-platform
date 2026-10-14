---
title: "chore: Обновление self-hosted Multica v0.4.32 → v0.6.2 и системные обновления Ubuntu"
type: chore
status: approved-pending-execution
date: 2026-10-14
target_host: root@65.109.167.1 (multica.autodc.org)
---

# Обновление Multica v0.4.32 → v0.6.2 и системные обновления Ubuntu

**Статус:** план. На сервере ничего не меняется до команды владельца «Выполняй».
Все факты ниже подтверждены командами на чтение (2026-10-14) или кодом upstream
(`multica-ai/multica`, чекаут тега v0.6.2 в scratchpad). Пути к файлам upstream даны
относительно корня репозитория `multica-ai/multica`.

---

## 1. Целевая версия — v0.6.2

| Проверка | Результат |
|---|---|
| GitHub API `releases/latest` | `v0.6.2`, опубликован 2026-10-13T10:48Z, `prerelease=false`, `draft=false` |
| Список релизов | v0.6.2 (10-13) > v0.6.1 (10-01) > v0.6.0 (09-28); у всех `prerelease=false` |
| GHCR `multica-backend:v0.6.2` | multi-arch index `sha256:b2ef064d197561cb9d32c6b0e6f7635195891adf74f0dbd3e289bc760efcb7d9`, есть `linux/amd64` |
| GHCR `multica-web:v0.6.2` | index `sha256:08c480f25f3370af018d70c0d92111add03c37a245d55c05a729c8f01076ba51`, есть `linux/amd64` |
| Коммит тега | `7ab92d7622ecd81d179a8d83ae2dea74835a5069` |

**Расхождение с заданием:** в задании подтверждённой назван v0.6.1. По API последний стабильный
релиз — v0.6.2 (вышел вчера). v0.6.0 был Latest до 2026-10-01, это объясняет, что вы видели раньше.
В v0.6.2, среди прочего, исправлена уязвимость Next.js (MUL-7842, GHSA-vcvr-r3jv-pc5j, #9007) в образе web.
Релизу один день — см. «Открытые вопросы», В1.

---

## 2. Нужен ли промежуточный шаг — **нет**

Вывод сделан по коду раннера миграций и SQL, а не по описаниям релизов.

1. **Миграции применятся все подряд, включая вставленные «задним числом».** Раннер
   (`server/cmd/migrate/main.go`, `runMigrations`) хранит в `schema_migrations` имя каждой
   миграции. Для каждого файла по порядку он проверяет `EXISTS` и применяет всё, чего нет.
   Номер «последней миграции» раннер не сравнивает, контрольных сумм не ведёт. На сервере
   применено 429 миграций, последняя `397_plugin_installation_package_version_index`. В v0.6.2
   598 up-файлов: все 429 старых на месте, к применению 169. Среди них 377–379: эти файлы
   добавлены в upstream уже после v0.4.32, хотя номера у них ниже 397. Раннер применит их
   штатно (`379_channel_context_mixed_version_guard` прямо исходит из того, что старый сервер
   остановлен до старта нового).
2. **Бэкфилл для self-host выполняется сам, внутри миграции.**
   - `469_issue_status_lifecycle_categories` при прямом прыжке **пропускается** условием
     `skipMigration("superseded by 478 expansion and 491 convergence")` (`upMigrationConditions`
     в `main.go`). Миграция записывается как применённая, её SQL не выполняется.
   - `478_issue_status_category_expand` — expand: расширяет CHECK до старых и новых категорий (`NOT VALID`).
   - `491_issue_status_category_backfill` — сам бэкфилл, один `UPDATE issue_status`. В комментарии
     к файлу: «Self-host upgrades converge automatically. SaaS must finish the bounded maintenance
     job BEFORE deploying». Обновляется только справочник статусов, задачи (`issue`) не трогаются.
   - `492` (contract, `NOT VALID`) → `493` (`VALIDATE`) → `494` (функция `issue_effective_status`).
   - Resumable backfill API (`server/internal/maintenance/`, PR #8436) — это инструмент для SaaS.
     Он поднимается на отдельном листенере `127.0.0.1:$MAINTENANCE_PORT` только при непустом
     `MAINTENANCE_PORT` (`maintenance/http.go`: «Empty port disables it»). Вызывать его не нужно.
   - Upstream тестирует именно этот путь: `TestStatusCategoryContractUpgradePaths`, кейс
     `skipped_469` (`server/cmd/migrate/status_category_contract_test.go`).
3. **Если contract-миграции 492–493 выполнятся раньше бэкфилла 491.** В одном прогоне 491 всегда идёт перед 492–493.
   Если строки в старых категориях всё же останутся, `493 VALIDATE CONSTRAINT` упадёт. Тогда
   раннер остановится до записи версии, backend не стартует, данные не испорчены. Обычный файл
   выполняется одной неявной транзакцией, но запись в `schema_migrations` идёт отдельным
   оператором после него. Файлы с `CREATE INDEX CONCURRENTLY` нетранзакционны; upstream чистит
   невалидные индексы хуками. На вашей базе в `issue_status` 7 системных строк,
   все в старых категориях (`backlog`, `todo`, `in_progress`, `in_review`, `blocked`, `done`,
   `cancelled`); 491 переведёт их за миллисекунды.
4. **Другие миграции, требующие остановки или ручного шага.** Ручных шагов нет. Что нашёл:
   - `446_issue_properties_bigm_index` требует `pg_bigm`, которого нет в `pgvector:pg17`. Раннер
     записывает её с пропуском SQL (`whenOperatorClassAvailable`). Это штатно, поиск работает без индекса.
   - `468_drop_reference_only_column` (DROP COLUMN) и `534_drop_comment_agent_delivery`
     (DROP TABLE) **необратимы**. Код v0.4.32 после них работать не будет. Отсюда раздел «Откат»:
     только через восстановление из дампа.
   - `472`/`463`/`464`/`454`/`455`: upstream описывает предварительную сборку и удаление индексов
     для больших таблиц (`server/cmd/migrate/README.md`). У вас 1 задача и база 21 MB, это не нужно.
   - `lock_timeout = '2s'` стоит в 478, 483, 490, 492, 493, 536, 549, 550, 561. Со стопнутым
     приложением других держателей блокировок нет. Если таймаут всё же сработает, backend
     перезапустится, и раннер повторит упавшую версию.

---

## 3. Что нашёл: несовместимости

### 3.1 `.env` против `.env.example` v0.6.2 (только имена; значения не читались)

Имена переменных в вашем `.env` (94) **полностью совпадают** с `.env.example` v0.4.32.

| Изменение в upstream | Переменные | Что делать |
|---|---|---|
| Добавлены (10) | `DO_NOT_TRACK`, `MAINTENANCE_PORT`, `MULTICA_CLOUD_URL`, `MULTICA_DAEMON_SERVER_URL`, `MULTICA_DAEMON_WS_CLAIM_POLL_INTERVAL`, `MULTICA_LLM_DISABLE_THINKING`, `MULTICA_PLUGIN_API_URL`, `MULTICA_PLUGIN_SURFACE_ORIGIN`, `MULTICA_SLACK_SECRET_KEY`, `MULTICA_TELEGRAM_SECRET_KEY` | Добавить только `DO_NOT_TRACK=1`. Остальные необязательные: в compose у всех дефолт `${VAR:-}`, пустое значение равно отсутствию |
| Удалены (7) | `MULTICA_CODEX_TIMEOUT`, `MULTICA_ENTITLEMENT_{POLICY_ENABLED,POLICY_URL,SERVICE_TOKEN,POLICY_TIMEOUT,STALE_GRACE,EMERGENCY_DISABLED}` | Безвредны: compose v0.6.2 их больше не передаёт. Оставить, чистка — отдельно (В3) |
| Переименования | не найдено | — |
| Обязательные (`:?`) | только `JWT_SECRET`, как и в v0.4.32; на сервере значение задано | — |

### 3.2 `docker-compose.selfhost.yml` v0.4.32 → v0.6.2

- **Сервисы, тома, образы, порты, `restart`** не изменились: `pgvector/pgvector:pg17`, тома
  `pgdata` и `backend_uploads`, привязка к `127.0.0.1:8080` / `127.0.0.1:3000`.
- **Healthcheck'и** upstream для backend и frontend **не добавил**. Свой есть только у postgres.
  Ваш `compose.override.yml` ничего не переопределяет и не конфликтует.
- Инструменты для ваших healthcheck'ов остаются в образах. Backend собран `FROM alpine:3.21`
  (в busybox есть `wget`), web — `FROM node:22-alpine` (есть `node`, `fetch`). Это проверяется
  отдельной командой после pull (B2).
- В окружении backend добавлены только необязательные переменные: `DATABASE_SEARCH_WORK_MEM_MB`
  (дефолт 64), `DATABASE_REPLICA_*`, `REDIS_URL`, `REDIS_CLUSTER_MODE`, `MAINTENANCE_PORT`,
  `DO_NOT_TRACK`, `MULTICA_CLOUD_URL`, `MULTICA_PLUGIN_*`, `MULTICA_DAEMON_SERVER_URL`,
  `MULTICA_LLM_DISABLE_THINKING`, `MULTICA_DINGTALK_SECRET_KEY`, `MULTICA_TELEGRAM_SECRET_KEY`.
  Блок `MULTICA_ENTITLEMENT_*` удалён.

### 3.3 Postgres

`SELF_HOSTING_ADVANCED.md` (v0.6.2): «Multica requires PostgreSQL 17. It does not use pgvector».
Обязательны `pgcrypto` и `pg_trgm`, оба установлены. На сервере PostgreSQL 17.11, расширения
`pg_trgm`, `pgcrypto`, `plpgsql`. **`pgvector:pg17` достаточно.**

### 3.4 Redis — по-прежнему необязателен

MUL-7281 (#8300, первый тег v0.4.43) — расширение. `REDIS_URL` по умолчанию пустой. При пустом
значении `server/cmd/server/main.go` логирует «REDIS_URL is unset — using in-memory hub
(single-node mode)» и продолжает работу.

### 3.5 Регистрация: `ALLOW_SIGNUP=false` (MUL-7607, #8706, первый тег v0.5.2)

Посторонним регистрация по-прежнему закрыта: `checkSignupAllowed` → `ErrSignupProhibited`,
`POST /auth/send-code` → **403**. **Изменение поведения:** теперь действующее приглашение на
конкретный email разрешает регистрацию этого email независимо от `ALLOW_SIGNUP`
(`server/internal/handler/auth.go`, шаг 4). В v0.4.32 приглашённый новый пользователь при
`ALLOW_SIGNUP=false` получал отказ. Сейчас в `workspace_invitation` 0 строк, так что
изменение пока ничего не открывает. Маршруты `/auth/send-code`, `/auth/verify-code`,
`/auth/google` не изменились. Next.js по-прежнему проксирует `/auth/:path*` в backend
(`apps/web/next.config.ts`), так что проверка «как в прошлый раз» через домен остаётся корректной.

### 3.6 `/opt/multica/UPGRADE.md` — где устарел для этого прыжка

1. **Шаг 5 `mc pull`** тянет все сервисы, включая `pgvector/pgvector:pg17`. Плавающий тег может
   подтянуть новую минорную версию Postgres и пересоздать контейнер базы — лишний подозреваемый.
   В плане вместо этого два явных `docker pull` образов backend и web.
2. **Бэкап делается до остановки приложения**, поэтому записи между дампом и стопом теряются.
   В плане: сначала остановить backend/frontend, потом сделать дамп.
3. **Нет шага сверки `.env` с `.env.example`** и шага `DO_NOT_TRACK`. Телеметрию нужно выключить
   до первого старта новой версии (см. §4).
4. **Раздел Rollback**: «вернуть тег, `up`; при миграциях — дамп». Для этого прыжка миграции
   применятся гарантированно, и 468/534 необратимы. Откат возможен только через восстановление
   дампа, а сама процедура восстановления в `UPGRADE.md` не записана.
5. **Ротация**: `backup.sh` рекурсивно удаляет `*.dump`/`*.tar.gz` старше 7 дней во всём
   `/var/backups/multica`. Копии, сохранённые для отката внутри этой папки, исчезнут через
   неделю. В плане они лежат в `/var/backups/multica-pre-v0.6.2/`.
6. `/readyz` и разница `/health` / `/readyz` по-прежнему верны (`server/cmd/server/router.go`,
   поля `db`, `migrations` в `health.go`).

Обновить `UPGRADE.md` предлагаю отдельным шагом после успешного обновления (B17).

---

## 4. Телеметрия

- **Переменная:** `DO_NOT_TRACK`. Отключают только значения `1` или `true` (без учёта регистра,
  пробелы обрезаются). Любое другое значение, включая пустое, оставляет телеметрию включённой
  (`server/internal/selfhosttelemetry/config.go`, `ConfigFromDoNotTrack`). `ANALYTICS_DISABLED`
  отвечает за отдельную интеграцию с PostHog и эту телеметрию **не отключает** (`SELF_HOSTING.md:21`).
- **Что отправляется:** раз в UTC-сутки POST на `https://telemetry.multica.ai/v1/telemetry/events`.
  Адрес зашит в код и не настраивается (`client.go`). Поля (`event.go`): `instance_id` (случайный
  UUID), `server_version`, корзины числа workspace / людей / агентов / активных daemon за 24 ч,
  счётчики задач started/completed/failed/cancelled за 24 ч. Имён, email, контента нет.
- **Достаточно ли `.env`:** да. Compose v0.6.2 передаёт `DO_NOT_TRACK: ${DO_NOT_TRACK:-}` в
  backend, а `--env-file /opt/multica/.env` в вашем `mc` подставляет значение. При отключённой
  телеметрии воркер не создаётся вовсе (`worker.go`: «returns before constructing any collector
  or HTTP client»). **Важно:** переменную нужно добавить **до** первого `up` v0.6.2. Воркер
  отправляет снимок за текущие сутки вскоре после старта.
- **Чем подтверждается отключение** (D8): (а) в логе backend строка
  `self-host telemetry disabled via DO_NOT_TRACK`; (б) в `instance_telemetry_state`
  `last_successful_day IS NULL`, `pending_day IS NULL`, `attempt_count = 0`. Сама строка с
  `instance_id` в таблице появится всё равно: её вставляет миграция 479. Это не признак отправки.
- PostHog: `POSTHOG_API_KEY` пустой, клиент работает как no-op (`.env.example`). Отдельных действий не нужно.

---

## 5. Совместимость с пилотом

- **Claude Code (`claude`)** и **Antigravity CLI (`agy`)** в v0.6.2 поддерживаются: оба в списке
  `SELF_HOSTING.md:122-123` и `CLI_AND_DAEMON.md:274-275`. Antigravity есть в коде агента с
  v0.3.11 (первый тег с ним в `server/pkg/agent`) и в схеме `runtime_profile` с v0.3.24
  (миграция 120). Claude Code поддерживался и раньше; точную первую версию не выяснял, для
  пилота это неважно. Переменные daemon: `MULTICA_CLAUDE_ARGS`, `MULTICA_ANTIGRAVITY_PATH`,
  `MULTICA_ANTIGRAVITY_MODEL`.
- **Версия CLI/daemon против версии сервера.** Требования точного совпадения и задокументированного
  диапазона **не нашёл**. Сервер проверяет минимальные версии по отдельным функциям
  (`server/pkg/agent/version.go`): quick-create ≥ 0.2.21, поля quick-create ≥ 0.4.3,
  local worktree ≥ 0.4.24. Для `repo checkout` нужен CLI ≥ v0.4.30
  (`server/internal/daemon/health.go`). При более старом CLI сервер отвечает
  `daemon_version_unsupported`. Рекомендация: ставить CLI той же версии, что сервер (v0.6.2).
  Автообновление CLI для self-host по умолчанию выключено (`CLI_AND_DAEMON.md:34-37`).
- **Подключение self-hosted runtime в v0.6.2** (`CLI_AND_DAEMON.md`, «Self-Hosted Server»):
  `multica setup self-host --server-url <URL> --app-url <URL>` настраивает CLI, открывает браузер
  для входа и запускает daemon. Вручную: `multica config set server_url …`,
  `multica config set app_url …`, `multica login`, `multica daemon start`. У вас один домен:
  Caddy отправляет `/ws` в backend, остальное во frontend, а frontend проксирует `/api` и `/auth`.
  Поэтому оба URL — `https://multica.autodc.org`. Работу daemon через single-domain схему
  **не проверял**, это проверка первого дня пилота. Для отдельного адреса API у сервера есть
  `MULTICA_DAEMON_SERVER_URL`.
- Daemon и агентские CLI на сервер **не ставятся**.

---

## 6. Решения владельца (2026-10-14)

- **В1.** Целевая версия — v0.6.2.
- **В2.** Caddy обновляется на этапе A (`--force-confold`, контрольная сумма Caddyfile до и после).
- **В3.** Устаревшие переменные `MULTICA_ENTITLEMENT_*` и `MULTICA_CODEX_TIMEOUT` остаются в `.env`.
- **В4.** Проверенную процедуру восстановления владелец пришлёт отдельно. До этого действует
  процедура R из раздела C; её проверяет репетиция B0.
- **В5.** Шаг B5a обязателен: дамп на Mac в ту же папку, что и копия `.env` (вне облачных папок и
  git, исключена из Time Machine, `chmod 600`).
- **В6.** Репетиция B0 обязательна: свежий дамп, восстановление в `multica_rehearsal`, прогон
  только `./migrate up` с явной базой `multica_rehearsal` и `DO_NOT_TRACK=1`, ручное подтверждение
  имени базы перед запуском, затем `dropdb` и проверка, что рабочая база осталась на 429.

---

## 7. Шаги

Соглашения: `$` — команда на сервере (`ssh root@65.109.167.1`), `mac$` — на вашем Mac. Одна
строка — одна команда. `; echo "exit=$?"` добавлен там, где без него не виден результат.
Окно лучше выбирать не на 03:30 UTC: в это время cron запускает `backup.sh`, часовой пояс сервера UTC.

### Этап A — системные обновления (отдельное окно, до Multica)

Порядок такой, чтобы при поломке подозреваемый был один: ядро, Docker и библиотеки — отдельно
от новой версии Multica.

**A0. Базовая линия (только чтение)**

1. `$ docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'` — три контейнера `healthy`.
2. `$ curl -fsS 127.0.0.1:8080/readyz` — `db:ok`, `migrations:ok`.
3. `mac$ curl -sS -o /dev/null -w '%{http_code}\n' https://multica.autodc.org/` — запишите код, после перезагрузки должен быть тот же.
4. `$ sha256sum /etc/caddy/Caddyfile` — запишите.
4a. `$ sha256sum /etc/ssh/sshd_config` — запишите.
4b. `$ ufw status verbose` — запишите.
4c. `$ ss -tlnH` — запишите (базовая линия слушающих портов).
5. `$ dpkg -l > /root/dpkg-before-stageA-$(date +%F).txt` — список версий пакетов для отката.
6. `$ /opt/multica/backup.sh; echo "exit=$?"` — внеочередной дамп перед этапом A. Ожидается `exit=0`.
7. `$ ls -lt /var/backups/multica | head -3` — новый `db-…dump` размером около 1.7 MB.

**A1. Обновление пакетов**

8. `$ apt update`
9. `$ apt list --upgradable` — сверьте со списком от 2026-10-14 (39 пакетов): ядро `linux-image-virtual`
   6.8.0-146 → **6.8.0-147** (noble-security), `docker-ce`/`docker-ce-cli` 29.7.2 → 29.9.0,
   `containerd.io` 2.3.3 → 2.4.1, `docker-compose-plugin` 5.5.0 → 5.6.0, `docker-buildx-plugin`,
   `caddy` 2.11.4 → 2.11.7, `libkrb5*`/`libgssapi-krb5-2` (1.20.1-6ubuntu2.7 → .10),
   `apparmor`/`libapparmor1`, `libaudit*`, `netplan*`, `procps`/`libproc2-0`, `snapd`, `open-iscsi`,
   `open-vm-tools`, `sosreport`, `base-files`, `python3-apt`, `ubuntu-release-upgrader-core` и мелочь.
   Ubuntu раскатывает часть обновлений постепенно (phased updates), поэтому отдельные пакеты могут
   остаться «kept back». Это нормально.
10. `$ apt-get -o Dpkg::Options::=--force-confold --with-new-pkgs upgrade` — интерактивно: проверьте
    список и подтвердите `Y`. `--with-new-pkgs` нужен, чтобы поставилось новое ядро
    `linux-image-6.8.0-147-generic`. `--force-confold` сохраняет изменённые конфиги, включая Caddyfile.
    - **Ожидаемо:** при обновлении `docker-ce` перезапустится dockerd (`live-restore` выключен), и
      контейнеры Multica остановятся и поднимутся сами (`restart: unless-stopped`). Простой начинается здесь.
    - Если `needrestart` покажет диалог перезапуска сервисов — оставьте выбор по умолчанию: сервер
      всё равно перезагружается.
11. `$ cat /var/run/reboot-required.pkgs` — в списке есть `linux-image-6.8.0-147-generic`.
12. `$ ls /boot | grep vmlinuz` — есть `vmlinuz-6.8.0-147-generic`; 137 и 146 остаются для отката.
13. `$ sha256sum /etc/caddy/Caddyfile` — совпадает с A0.4.

**A2. Перезагрузка**

14. `$ reboot`
15. Через 1–2 минуты: `mac$ ssh root@65.109.167.1`

**A3. Проверка, что стек поднялся сам**

16. `$ uname -r` → `6.8.0-147-generic`.
17. `$ systemctl is-active docker caddy` → `active`, `active`.
18. `$ docker ps --format '{{.Names}}\t{{.Status}}'` — три контейнера; backend до `healthy` может
    идти до 3 минут (`start_period: 180s`).
19. `$ curl -fsS 127.0.0.1:8080/readyz` → `db:ok`, `migrations:ok`.
20. `mac$ curl -sS -o /dev/null -w '%{http_code}\n' https://multica.autodc.org/` — тот же код, что в A0.3.
21. Вход в интерфейс, AUTO-1 открывается.
22. `$ ss -tlnH` — как в A0.4c (Docker обновлялся и управляет iptables).

**Критерий перехода к этапу B:** все пункты A3 зелёные. Иначе этап B не начинаем.
**Откат этапа A:**
- Ядро: в консоли Hetzner (VNC) в GRUB → «Advanced options» выбрать 6.8.0-146 или 137.
- Docker: `apt install` прежних версий из `/root/dpkg-before-stageA-*.txt`
  (`docker-ce=5:29.7.2-1~ubuntu.24.04~noble` и т. д.).
- Данные: дамп из A0.6.

### Этап B0 — репетиция на копии (без простоя, до окна B)

Цель: проверить на реальных данных прыжок 429 → 598 и процедуру восстановления R. Рабочая база не
меняется; создаётся и удаляется отдельная база `multica_rehearsal` в том же контейнере postgres.
Пароль нигде не выводится: строка подключения копируется из окружения работающего backend в файл с
правами 600, в ней меняется только имя базы, а контейнер миграций получает её через `--env-file`.
Аварийная остановка на любом шаге: `docker rm -f multica-rehearsal-migrate`, затем шаг 0.17.

**Подготовка**

0.1. `$ docker pull ghcr.io/multica-ai/multica-backend:v0.6.2`
0.2. `$ docker image inspect --format '{{index .RepoDigests 0}}' ghcr.io/multica-ai/multica-backend:v0.6.2` → `…@sha256:b2ef064d197561cb9d32c6b0e6f7635195891adf74f0dbd3e289bc760efcb7d9`. Другой digest — стоп.

**Свежий дамп рабочей базы**

0.3. `$ /opt/multica/backup.sh; echo "exit=$?"` → `exit=0`.
0.4. `$ ls -lt /var/backups/multica | head -3` — запишите `<STAMP>` свежего `db-<STAMP>.dump`.
0.5. `$ docker exec -i multica-postgres-1 pg_restore -f /dev/null < /var/backups/multica/db-<STAMP>.dump; echo "exit=$?"` → `exit=0`.
0.6. `$ docker exec multica-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "select count(*) from schema_migrations"'` → `429` (рабочая база, до репетиции).

**Восстановление в `multica_rehearsal` (это проверка шагов R.5–R.7)**

0.7. `$ docker exec multica-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d postgres -At -c "select count(*) from pg_database where datname = '\''multica_rehearsal'\''"'` → `0`.
0.8. `$ docker exec multica-postgres-1 sh -c 'createdb -U "$POSTGRES_USER" -O "$POSTGRES_USER" multica_rehearsal'`
0.9. `$ docker exec -i multica-postgres-1 sh -c 'pg_restore -U "$POSTGRES_USER" -d multica_rehearsal --exit-on-error' < /var/backups/multica/db-<STAMP>.dump; echo "exit=$?"` → `exit=0`.
0.10. `$ docker exec multica-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d multica_rehearsal -At -c "select count(*) from schema_migrations"'` → `429`.

**Строка подключения для контейнера миграций**

0.11. `$ (umask 077; docker exec multica-backend-1 printenv DATABASE_URL | sed -e 's|@postgres:5432/[^?]*?|@postgres:5432/multica_rehearsal?|' -e 's|^|DATABASE_URL=|' > /root/rehearsal.env)`
      Конвейер в одной строке нарочно: строка с паролем не должна появиться в терминале.
0.12. `$ echo 'DO_NOT_TRACK=1' >> /root/rehearsal.env`
0.13. **Проверка имени базы — подтвердите вручную:**
      `$ sed -n 's|.*@\(postgres:5432\)/\([^?]*\)?.*|host=\1 db=\2|p' /root/rehearsal.env`
      → ровно одна строка `host=postgres:5432 db=multica_rehearsal`. Если строк нет, их больше
      одной или `db` другой — **стоп**, шаг 0.14 не выполнять.
0.13a. `$ grep -c '' /root/rehearsal.env` → `2`; `$ grep -c '^DO_NOT_TRACK=1$' /root/rehearsal.env` → `1` (две отдельные команды).
0.13b. `$ stat -c '%a' /root/rehearsal.env` → `600`.

**Прогон миграций (только `./migrate up`, сервер не запускается)**

0.14. `$ (umask 077; docker run --rm --name multica-rehearsal-migrate --network multica_default --env-file /root/rehearsal.env --entrypoint ./migrate ghcr.io/multica-ai/multica-backend:v0.6.2 up > /root/rehearsal-migrate.log 2>&1; echo "exit=$?")`
      → `exit=0`. `--entrypoint ./migrate` заменяет `entrypoint.sh`, поэтому `./server` не стартует.
0.15. `$ tail -3 /root/rehearsal-migrate.log` — предпоследняя строка `  up  572_channel_typing_abandoned_idx`, последняя `Done.`.
0.15a. `$ grep -c '^  up  ' /root/rehearsal-migrate.log` → `169`.
0.15b. `$ grep 'SQL skipped' /root/rehearsal-migrate.log` → две строки: `446_issue_properties_bigm_index`, `469_issue_status_lifecycle_categories`.
0.15c. `$ grep -iE 'level=ERROR|ERR |migration run failed' /root/rehearsal-migrate.log` → пусто (при ошибке `migrate` пишет `migration run failed` и выходит с кодом 1).

**Проверка результата на `multica_rehearsal`**

0.16. `$ docker exec multica-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d multica_rehearsal -At -c "select count(*) from schema_migrations"'` → `598`.
0.16a. `$ docker exec multica-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d multica_rehearsal -At -c "select key, category from issue_status order by key"'` → маппинг как в D5.
0.16b. `$ docker exec multica-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d multica_rehearsal -At -c "select count(*) from issue"'` → `1`.

**Уборка и контроль рабочей базы**

0.17. `$ docker exec multica-postgres-1 sh -c 'dropdb -U "$POSTGRES_USER" multica_rehearsal'`
0.18. `$ docker exec multica-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d postgres -At -c "select count(*) from pg_database where datname = '\''multica_rehearsal'\''"'` → `0`.
0.19. `$ docker exec multica-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "select count(*) from schema_migrations"'` → `429`: рабочая база не тронута.
0.20. `$ curl -fsS 127.0.0.1:8080/readyz` → `db:ok`, `migrations:ok`.
0.21. `$ rm /root/rehearsal.env`
0.22. Лог `/root/rehearsal-migrate.log` (права 600) оставить до разбора; присылать только вывод шагов 0.15–0.15c.

**Критерий перехода к окну B:** 0.9 `exit=0`, 0.14 `exit=0`, 0.16 = 598, 0.16a совпадает с D5, 0.19 = 429.
Если упало — присылаете вывод, разбираем до окна B.

### Этап B — обновление Multica до v0.6.2

**B1. Предварительно, без простоя: скачать образы и сверить digest**

1. `$ docker pull ghcr.io/multica-ai/multica-backend:v0.6.2`
2. `$ docker pull ghcr.io/multica-ai/multica-web:v0.6.2`
3. `$ docker image inspect --format '{{index .RepoDigests 0}}' ghcr.io/multica-ai/multica-backend:v0.6.2`
   → `…@sha256:b2ef064d197561cb9d32c6b0e6f7635195891adf74f0dbd3e289bc760efcb7d9`
4. `$ docker image inspect --format '{{index .RepoDigests 0}}' ghcr.io/multica-ai/multica-web:v0.6.2`
   → `…@sha256:08c480f25f3370af018d70c0d92111add03c37a245d55c05a729c8f01076ba51`
   - **Если digest другой** (тег перезалит), остановиться и сообщить мне. Не продолжать.

**B2. Проверка инструментов для ваших healthcheck'ов в новых образах**

5. `$ docker run --rm --entrypoint wget ghcr.io/multica-ai/multica-backend:v0.6.2 --help 2>&1 | head -1` — строка BusyBox.
6. `$ docker run --rm --entrypoint node ghcr.io/multica-ai/multica-web:v0.6.2 -e "console.log(typeof fetch)"` → `function`.

**B3. Копия `.env` на Mac** (можно заранее, до окна)

`.env` не входит в бэкапы намеренно: `backup.sh` сохраняет только базу и вложения, а в `.env` лежат
`JWT_SECRET` и `POSTGRES_PASSWORD`. Держать секреты рядом с дампами на том же диске — плохая идея.
Без `.env` восстановленная база бесполезна: при потере `JWT_SECRET` перестанут действовать все
сессии и токены. Поэтому копия хранится отдельно и вне сервера.

7. `mac$ mkdir -p ~/.multica-server-backup`
8. `mac$ chmod 700 ~/.multica-server-backup`
8a. `mac$ tmutil addexclusion ~/.multica-server-backup`
8b. `mac$ tmutil isexcluded ~/.multica-server-backup` → `[Excluded]`.
9. `mac$ scp -p root@65.109.167.1:/opt/multica/.env ~/.multica-server-backup/multica.env.v0.4.32`
10. `mac$ chmod 600 ~/.multica-server-backup/multica.env.v0.4.32`
11. `mac$ shasum -a 256 ~/.multica-server-backup/multica.env.v0.4.32`
11b. `$ sha256sum /opt/multica/.env` — хэш совпадает с п. 11.

Почему именно эта папка: `~/.multica-server-backup` лежит вне Yandex.Disk и вне
`~/Desktop`/`~/Documents`, которые синхронизирует iCloud. Домашняя папка — не git-репозиторий
(проверено). На Mac настроен Time Machine и домашняя папка в него входит, поэтому папка исключается
из него до первого копирования (п. 8a). Если вам удобнее, перенесите содержимое `.env` в менеджер
паролей и удалите файл: `mac$ rm ~/.multica-server-backup/multica.env.v0.4.32`.

**B4. Остановка приложения (начало простоя)**

Отличие от вашего порядка: я останавливаю backend/frontend **до** дампа, а Postgres оставляю
работать. Так дамп консистентен и не теряет записей, сделанных между дампом и стопом. Дамп на
1.7 MB занимает секунды.

12. `$ /opt/multica/mc stop frontend backend`
13. `$ docker ps --format '{{.Names}}\t{{.Status}}'` — работает только `multica-postgres-1`.

**B5. Внеочередной бэкап**

14. `$ /opt/multica/backup.sh; echo "exit=$?"` → `exit=0`.
15. `$ ls -lt /var/backups/multica | head -3` — свежие `db-<STAMP>.dump` (около 1.7 MB) и `uploads-<STAMP>.tar.gz`. Запишите `<STAMP>`.
16. `$ mkdir -m 700 /var/backups/multica-pre-v0.6.2` — вне папки, которую чистит ротация `backup.sh`.
17. `$ cp -p /var/backups/multica/db-<STAMP>.dump /var/backups/multica/uploads-<STAMP>.tar.gz /var/backups/multica-pre-v0.6.2/`
18. `$ docker exec -i multica-postgres-1 pg_restore -f /dev/null < /var/backups/multica-pre-v0.6.2/db-<STAMP>.dump; echo "exit=$?"`
    → `exit=0`. pg_restore читает архив целиком, поэтому усечённый файл здесь даст ошибку.
19. `$ gzip -t /var/backups/multica-pre-v0.6.2/uploads-<STAMP>.tar.gz; echo "exit=$?"` → `exit=0`.
20. `$ tar tzf /var/backups/multica-pre-v0.6.2/uploads-<STAMP>.tar.gz` — сейчас ожидается только `./`, вложений нет.

**B5a. Копия дампа на Mac** (обязательно, В5)

21. `mac$ scp -p root@65.109.167.1:/var/backups/multica-pre-v0.6.2/db-<STAMP>.dump ~/.multica-server-backup/`
21a. `mac$ chmod 600 ~/.multica-server-backup/db-<STAMP>.dump` — на сервере дамп создаётся с правами 644 (папка 700).
21b. `mac$ shasum -a 256 ~/.multica-server-backup/db-<STAMP>.dump` и отдельно `$ sha256sum /var/backups/multica-pre-v0.6.2/db-<STAMP>.dump` — совпадают.
В дампе email, содержимое задач и токены. После подтверждения, что D зелёный, удалите копию на Mac
или зашифруйте её (age/gpg).

**B6. Фиксация состояния для отката**

22. `$ docker image inspect --format '{{index .RepoTags 0}} {{index .RepoDigests 0}}' ghcr.io/multica-ai/multica-backend:v0.4.32 ghcr.io/multica-ai/multica-web:v0.4.32 pgvector/pgvector:pg17 | tee /var/backups/multica-pre-v0.6.2/images.txt`
    — digest'ы совпадают с `VERSIONS.md` (`508de17e…`, `c6976fbe…`, `cf134a76…`).
23. `$ sha256sum /opt/multica/.env /opt/multica/compose.override.yml /opt/multica/mc /opt/multica/backup.sh /opt/multica/VERSIONS.md /opt/multica/UPGRADE.md /etc/caddy/Caddyfile | tee /var/backups/multica-pre-v0.6.2/checksums.txt`
24. `$ git -C /opt/multica/src rev-parse HEAD | tee /var/backups/multica-pre-v0.6.2/git-head.txt` → `d60775aa9394b911b18701a326f655465604e7d1`.
25. `$ docker exec multica-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "select version from schema_migrations order by version"' > /var/backups/multica-pre-v0.6.2/migrations-before.txt`
26. `$ wc -l < /var/backups/multica-pre-v0.6.2/migrations-before.txt` → `429`.
27. `$ cp -p /opt/multica/.env /opt/multica/.env.bak-v0.4.32` — права 600 сохраняются (`-p`).
28. `$ cp -p /opt/multica/VERSIONS.md /opt/multica/VERSIONS.md.bak-v0.4.32`

Не выполнять `mc config` целиком: эта команда выводит все переменные окружения, включая секреты.

**B7. Обновление чекаута `src/`**

29. `$ git -C /opt/multica/src status --short` — пусто: локальных изменений нет.
30. `$ git -C /opt/multica/src fetch --tags`
31. `$ git -C /opt/multica/src rev-parse 'v0.6.2^{commit}'` → `7ab92d7622ecd81d179a8d83ae2dea74835a5069`.
32. `$ git -C /opt/multica/src checkout v0.6.2`
33. `$ git -C /opt/multica/src describe --tags` → `v0.6.2`.
34. `$ sha256sum -c /var/backups/multica-pre-v0.6.2/checksums.txt` — все `OK`: ваши файлы в корне `/opt/multica` не затронуты.

**B8. Сверка `.env` (только имена) и отключение телеметрии**

35. `$ grep -E '^[A-Za-z_][A-Za-z0-9_]*=' /opt/multica/src/.env.example | cut -d= -f1 | sort -u > /tmp/ex.names`
36. `$ grep -E '^[A-Za-z_][A-Za-z0-9_]*=' /opt/multica/.env | cut -d= -f1 | sort -u > /tmp/env.names`
37. `$ comm -13 /tmp/env.names /tmp/ex.names` — ровно 10 имён из §3.1. Если список другой — остановиться.
38. `$ comm -23 /tmp/env.names /tmp/ex.names` — ровно 7 устаревших имён из §3.1.
39. `$ sed -i '$a DO_NOT_TRACK=1' /opt/multica/.env`
40. `$ grep -c '^DO_NOT_TRACK=1$' /opt/multica/.env` → `1` (выводится счётчик, не значение).
40a. `$ grep -c '^ALLOW_SIGNUP=false$' /opt/multica/.env` → `1`. В compose v0.6.2 дефолт `${ALLOW_SIGNUP:-true}`: если переменная пропадёт, регистрация откроется.
41. `$ rm /tmp/ex.names /tmp/env.names`

**B9. Тег образа**

42. `$ sed -i 's|^MULTICA_IMAGE_TAG=.*|MULTICA_IMAGE_TAG=v0.6.2|' /opt/multica/.env`
43. `$ grep -c '^MULTICA_IMAGE_TAG=v0.6.2$' /opt/multica/.env` → `1`.
44. `$ stat -c '%a %U' /opt/multica/.env` → `600 root`.
45. `$ /opt/multica/mc config --quiet; echo "exit=$?"` → `exit=0`: итоговый compose валиден, вывода нет.
46. `$ /opt/multica/mc config --images` → `multica-backend:v0.6.2`, `multica-web:v0.6.2`, `pgvector/pgvector:pg17`.

**B10. Запуск и миграции**

47. `$ /opt/multica/mc up -d` — postgres не пересоздаётся (образ тот же), backend и frontend пересоздаются на v0.6.2.
48. `$ /opt/multica/mc logs -f backend` — наблюдать до `Starting server...`, затем Ctrl-C. Ожидаемо:
    - `Running database migrations...`;
    - `skip <версия> (already applied)` × 429;
    - `up  377_…`, `up  378_…`, `up  379_…`;
    - `up  469_issue_status_lifecycle_categories (SQL skipped: superseded by 478 expansion and 491 convergence (MUL-7365))`;
    - `up  446_issue_properties_bigm_index (SQL skipped: …)`;
    - `up  478…`, `up  491…`, `up  492…`, `up  493…`, `up  494…`, …, `up  572_channel_typing_abandoned_idx`;
    - `Starting server...`, затем `self-host telemetry disabled via DO_NOT_TRACK`
      и `realtime: REDIS_URL is unset — using in-memory hub (single-node mode)`.
    - Строки `INF running pre-migration hook version=…` **нормальны**: они идут перед 431 и
      перед каждой миграцией с `CREATE INDEX CONCURRENTLY`.
    - Сигнал к сценарию C2: `migration run failed`, `apply migration "…"`, `pre-migration hook for "…"`,
      `ERR` или перезапуск контейнера backend.
49. `$ docker exec multica-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "select count(*) from schema_migrations"'` → `598`.

**B11. VERSIONS.md**

50. `$ docker image inspect --format '{{index .RepoDigests 0}}' ghcr.io/multica-ai/multica-backend:v0.6.2 ghcr.io/multica-ai/multica-web:v0.6.2 pgvector/pgvector:pg17`
51. `$ nano /opt/multica/VERSIONS.md` — заменить `Deployed:` (текущая дата UTC), `Git ref: v0.6.2 (7ab92d762)`,
    `MULTICA_IMAGE_TAG=v0.6.2` и две строки digest'ов backend/web значениями из п. 50. Digest
    postgres не меняется. В Notes добавить: «2026-10-xx: upgraded from v0.4.32; DO_NOT_TRACK=1;
    migration 446 recorded with SQL skipped (no pg_bigm)».

Затем этап D. Пункт B17 — после того как D полностью зелёный:

**B17. Обновление `UPGRADE.md`** по пунктам §3.6 (явные pull вместо `mc pull`, стоп до дампа,
сверка `.env`, копии для отката вне ротации, процедура восстановления). Я подготовлю текст, вы вставите.

### Этап C — откат

**Точка невозврата.** Процедура R возвращает базу к дампу B5 и **удаляет всё, что записано после
него**: задачи, комментарии, подключённые runtime. Откат через R допустим до первых пользовательских
записей на v0.6.2, то есть до окончания этапа D. После этого остаётся чинить вперёд или вручную
выгрузить новые данные перед R.

**Прямо: откатиться на образ v0.4.32 поверх уже применённых миграций нельзя.** Миграция 468
удаляет колонку `reference_only`, 534 — таблицу `comment_agent_delivery`, а код v0.4.32 рассчитан
на старую схему. Down-миграции есть только у части файлов (509+), и путь через них upstream для
self-host не описывает. Единственный надёжный откат — восстановление из дампа B5 вместе с
возвратом тега, чекаута и `.env`.

**C1. Миграции применились, но приложение не работает**

1. `$ (umask 077; /opt/multica/mc logs --no-log-prefix backend > /root/multica-v0.6.2-failure.log)` — сохранить лог для разбора.
1a. `$ grep -viE 'verification code|password|secret|token|postgres://' /root/multica-v0.6.2-failure.log | tail -80` —
   **в чат только этот отфильтрованный вывод**. Почтовый провайдер не настроен (`SMTP_HOST` и
   `RESEND_API_KEY` пустые), поэтому коды входа пишутся в лог backend строкой
   `[DEV] Verification code for …` (`server/internal/service/email.go`). Сырой лог не пересылать;
   удалить после разбора.
2. Прислать мне отфильтрованный лог и вывод `docker ps`. Если причина в конфигурации, а не в данных (например,
   переменная окружения), чиним вперёд без отката. Решение за вами.
3. Если решено откатываться — процедура R ниже.

**C2. Миграции упали посередине**

Как это выглядит: backend выходит с ошибкой `apply migration "…"`, Docker его перезапускает,
и миграция повторяется по кругу. Каждый файл атомарен (неявная транзакция), поэтому база
находится на последней успешной версии, без «половины» файла.

1. `$ /opt/multica/mc stop backend` — остановить цикл перезапусков.
2. `$ (umask 077; /opt/multica/mc logs --no-log-prefix backend > /root/multica-v0.6.2-migrate-failure.log)`
2a. `$ grep -viE 'verification code|password|secret|token|postgres://' /root/multica-v0.6.2-migrate-failure.log | tail -80` — в чат только это.
3. `$ docker exec multica-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "select version from schema_migrations order by applied_at desc, version desc limit 5"'` — на какой версии остановились.
4. Прислать мне вывод п. 2a и п. 3.
   - Если ошибка транзиентная (`canceling statement due to lock timeout` или `statement timeout`
     в любой миграции) — `$ /opt/multica/mc up -d`: раннер продолжит с упавшей версии.
   - Если повтор падает с `already exists`, значит SQL применился, а запись версии — нет
     (процесс убит между ними). Прислать лог до выбора R.
   - Иначе — процедура R.

**R. Восстановление на v0.4.32 из дампа**

Проверенную процедуру владелец пришлёт отдельно (В4). До этого действует процедура ниже: она совместима
с форматом `backup.sh`, а шаги R.5–R.7 проверяются репетицией B0.

1. `$ /opt/multica/mc stop frontend backend`
2. `$ cp -p /opt/multica/.env.bak-v0.4.32 /opt/multica/.env` — возвращает `MULTICA_IMAGE_TAG=v0.4.32` и убирает `DO_NOT_TRACK`.
3. `$ git -C /opt/multica/src checkout v0.4.32`
4. `$ docker exec multica-postgres-1 sh -c 'dropdb -U "$POSTGRES_USER" "$POSTGRES_DB"'`
   Если ругается на активные подключения: убедитесь, что backend остановлен, и повторите.
5. `$ docker exec multica-postgres-1 sh -c 'createdb -U "$POSTGRES_USER" -O "$POSTGRES_USER" "$POSTGRES_DB"'`
6. `$ docker exec -i multica-postgres-1 sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --exit-on-error' < /var/backups/multica-pre-v0.6.2/db-<STAMP>.dump; echo "exit=$?"` → `exit=0`.
7. `$ docker exec multica-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "select count(*) from schema_migrations"'` → `429`.
8. Вложения: архив пустой, восстанавливать нечего. Если к моменту отката в `tar tzf` появятся
   файлы — распаковать в том `multica_backend_uploads` (тем же `alpine`, что в `backup.sh`).
9. `$ /opt/multica/mc up -d` — образы v0.4.32 локально на месте (их не удаляем).
10. `$ cp -p /opt/multica/VERSIONS.md.bak-v0.4.32 /opt/multica/VERSIONS.md`
11. Проверки D1–D5.

Переменные `$POSTGRES_USER`/`$POSTGRES_DB` берутся из окружения контейнера postgres, поэтому
значения не выводятся. Их же compose передаёт в контейнер из `.env`.

### Этап D — проверка после обновления

1. **Контейнеры:** `$ docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'` — backend и web на `v0.6.2`, все `healthy` (backend — до 3 мин).
2. **readyz:** `$ curl -fsS 127.0.0.1:8080/readyz` → `db:ok`, `migrations:ok`.
3. **HTTPS:** `mac$ curl -sS -o /dev/null -w '%{http_code} verify=%{ssl_verify_result}\n' https://multica.autodc.org/`
   — тот же код, что в A0.3, `verify=0`.
3b. `mac$ echo | openssl s_client -connect multica.autodc.org:443 -servername multica.autodc.org 2>/dev/null | openssl x509 -noout -enddate`
   → `notAfter` около 21.11.2026 (по вашим данным).
4. **Интерфейс:** вход, workspace AutOnline, задача AUTO-1 открывается. Её статус отображается
   так же, как до обновления: проверка бэкфилла 491.
5. **Статусы в БД:** `$ docker exec multica-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "select key, category from issue_status order by key"'`
   → `backlog|unstarted`, `blocked|started`, `cancelled|closed`, `done|done`, `in_progress|started`, `in_review|started`, `todo|unstarted`.
6. **Регистрация закрыта (с контролем):**
   - `mac$ curl -sS -w '\n%{http_code}\n' -X POST https://multica.autodc.org/auth/send-code -H 'Content-Type: application/json' -d '{"email":"probe-'"$(date +%s)"'@example.com"}'`
     → **403**, сообщение о запрете регистрации.
   - Контроль: та же команда с вашим email → **200**. Код придёт не на почту, а в лог backend
     (почтовый провайдер не настроен). При повторе в течение минуты возможен 429. Если контроль
     не даёт 200, проверка 403 ничего не доказывает.
   - Повторить пробу 403 через сутки, вместе с повтором D8.
   - `$ docker exec multica-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "select count(*) from workspace_invitation where status = '\''pending'\'' and expires_at > now()"'`
     → `0`. Действующие приглашения теперь разрешают регистрацию (§3.5); условие то же, что в
     `HasPendingInvitationForEmail` (`server/pkg/db/queries/invitation.sql`).
7. **Порты снаружи закрыты:**
   - `mac$ nc -zv -G 5 65.109.167.1 3000` → `refused` или таймаут.
   - `mac$ nc -zv -G 5 65.109.167.1 8080` → `refused` или таймаут.
   - `mac$ nc -zv -G 5 65.109.167.1 5432` → `refused` или таймаут.
   - `mac$ nc -zv -G 5 65.109.167.1 443` → `succeeded` (контроль).
   - `$ ss -tlnH` — на внешних адресах только 22, 80, 443; 8080 и 3000 только на `127.0.0.1`; 5432 наружу не слушает.
   - `$ ufw status verbose` и `$ sha256sum /etc/ssh/sshd_config` — совпадают с A0.
8. **Телеметрия отключена:**
   - `$ /opt/multica/mc logs --no-log-prefix backend | grep -i "self-host telemetry"` → `self-host telemetry disabled via DO_NOT_TRACK`.
   - `$ docker exec multica-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "select last_successful_day, pending_day, attempt_count from instance_telemetry_state"'` → `||0`.
   - Через сутки повторить запрос — по-прежнему `||0`.
9. **Бэкап на новой версии:** `$ /opt/multica/backup.sh; echo "exit=$?"` → `exit=0`. Затем
   `$ ls -lt /var/backups/multica | head -3` (новый дамп немного больше прежнего — новые таблицы) и
   `$ docker exec -i multica-postgres-1 pg_restore -f /dev/null < /var/backups/multica/db-<NEW>.dump; echo "exit=$?"` → `exit=0`.
   Утром следующего дня — дамп от cron 03:30.
10. **Не удалять** образы v0.4.32, `/var/backups/multica-pre-v0.6.2/`, `.env.bak-v0.4.32`,
    `VERSIONS.md.bak-v0.4.32` до вашего подтверждения. Удаление — отдельной командой после него.

---

## 8. Риски на базе с данными

| Риск | Вероятность | Последствие | Мера |
|---|---|---|---|
| Телеметрия уйдёт до отключения | Низкая при соблюдении порядка | Один анонимный снимок | `DO_NOT_TRACK=1` в B8, до `up` в B10; проверка в D8 |
| Необратимые миграции 468/534 | Сработают обязательно | Откат образа без дампа невозможен | Дамп после стопа (B5), проверка `pg_restore` (B5.18), процедура R |
| Ротация удалит копии для отката | Высокая, если хранить в `/var/backups/multica` | Через 7 дней отката не будет | Копии в `/var/backups/multica-pre-v0.6.2/` и на Mac |
| `mc pull` подтянет новый Postgres | Возможна (плавающий тег `pg17`) | Лишний подозреваемый, пересоздание БД-контейнера | Явный pull только backend и web |
| Тег v0.6.2 перезалит, digest отличается | Низкая | Неожиданный образ | Сверка digest в B1, стоп при расхождении |
| `lock_timeout` 2s (478, 483, 490, 492, 493, 536, 549, 550, 561) | Очень низкая (приложение стоит) | Падение миграции, цикл рестартов | C2: стоп, лог, повторный `up` |
| Fail-closed `VALIDATE` в 493 | Очень низкая (491 в том же прогоне) | Backend не стартует, данные целы | C2 |
| Все бэкапы на одном диске | — | Потеря сервера = потеря данных | B5a: дамп на Mac |
| Перезапуск контейнеров при `apt upgrade` | Ожидаемо | Простой несколько минут уже на шаге A1.10 | Окно этапа A |
| Новое ядро не загрузится | Низкая | Сервер недоступен по SSH | Консоль Hetzner, GRUB → 146/137 |
| Приглашение открывает регистрацию | Только при наличии приглашений | Посторонний email с приглашением регистрируется | Сейчас 0 приглашений; учитывать при пилоте |
| Откат через R после начала работы на v0.6.2 | Средняя, если тянуть с решением | Потеря данных, записанных после B5 | Точка невозврата — конец этапа D (раздел C) |
| Коды входа в логах backend | Высокая (почта не настроена) | Утечка кода при пересылке лога | Фильтр перед отправкой (C1.1a, C2.2a) |
| Свежий релиз (1 день) | Неизвестна | Регрессии без hotfix | В1: альтернатива v0.6.1 |

---

## 9. Оценка времени и простоя

| Этап | Длительность окна | Простой |
|---|---|---|
| A (обновления + перезагрузка + проверки) | 30–40 мин | около 5–10 мин: перезапуск dockerd на A1.10 плюс перезагрузка и старт backend (до 3 мин) |
| B1–B3 (pull, проверки образов, копия `.env`) | 10 мин | нет |
| B4–B10 (стоп → бэкап → чекаут → `.env` → `up` → миграции) | 15–20 мин | **10–15 мин**: 169 миграций на базе 21 MB — секунды; основное время уходит на ручные шаги и `start_period` healthcheck'а |
| D (проверки) | 15–20 мин | нет |
| Итого | этапы A и B в разные окна; B с проверками — около 1 ч | около 25 мин суммарно |

---

## Что не проверял

- Работу daemon с этим сервером в single-domain схеме: выполнится на пилоте.
- Процедуру восстановления владельца (В4): ждём от владельца. Процедура R проверяется на B0, кроме шага `dropdb` рабочей базы.
- Что `mc up -d` после обновления `docker-compose-plugin` 5.5 → 5.6 не пересоздаст postgres. Если пересоздаст — это безвредно, том сохраняется.
- Точную первую версию Multica с поддержкой Claude Code: для пилота не нужна.
- `apt list --upgradable` снят 2026-10-14 по кэшу от 2026-10-13; к окну этапа A список может вырасти.

## Sources

- GitHub API: `repos/multica-ai/multica/releases`, `releases/latest`; GHCR: manifests `multica-backend`, `multica-web` для тегов v0.6.2, v0.6.1, v0.5.0, v0.4.44.
- upstream (тег v0.6.2): `server/cmd/migrate/main.go`, `server/cmd/migrate/README.md`,
  `server/cmd/migrate/status_category_contract_test.go`, `server/migrations/{377,378,379,446,447,468,469,478,479,491,492,493,494,534}_*.up.sql`,
  `server/internal/maintenance/{http.go,status_category.go}`, `server/internal/selfhosttelemetry/{config.go,client.go,event.go,worker.go}`,
  `server/internal/handler/auth.go`, `server/cmd/server/{main.go,router.go,health.go}`, `server/pkg/agent/version.go`,
  `server/internal/daemon/health.go`, `docker-compose.selfhost.yml`, `.env.example`, `Dockerfile`, `Dockerfile.web`,
  `docker/entrypoint.sh`, `apps/web/next.config.ts`, `SELF_HOSTING.md`, `SELF_HOSTING_ADVANCED.md`, `CLI_AND_DAEMON.md`.
- Коммиты: MUL-7281 da7fc439a (v0.4.43); MUL-7236 dee1ed6bf (#8252, v0.4.44); MUL-7240 7dafc0cd6 (#8261, v0.4.44);
  MUL-7365 9d18186e6 (#8407), 02a8ff3e9 (#8436) — v0.4.44, d95c20aab (#8466, v0.5.0); MUL-7607 540308342 (#8706, v0.5.2).
- Сервер (read-only): `/opt/multica/{UPGRADE.md,VERSIONS.md,compose.override.yml,mc,backup.sh}`, `/etc/cron.d/multica-backup`,
  `docker ps`, `docker volume ls`, `schema_migrations`, `issue_status`, `workspace_invitation`, `pg_extension`,
  `apt list --upgradable`, `dpkg -l`, `/etc/docker/daemon.json`, Caddyfile (только чтение), `ufw status`.
