---
title: Бэкап снят не с той базы — на одном PG-сервере четыре базы разных систем
date: 2026-09-25
category: database-issues
module: ops
problem_type: wrong_target
root_cause: implicit_default
resolution_type: process_fix
severity: high
component: infra
symptoms:
  - "pg_dump отработал без ошибок, но в оглавлении дампа чужие таблицы"
  - "бэкап лежит в /tmp контейнера и исчезает при пересоздании"
related_components:
  - infra
tags: [postgresql, backup, pg_dump, pg_restore, dockhost, multi-database]
---

# Проверять dbname в оглавлении дампа и класть бэкап на постоянный том

## Problem

На прод-PG-сервере ЧЕТЫРЕ базы разных систем: `root_db` (NocoDB),
`max_bot_db` (этот бот), `tg_bot_db`, `n8n`. Команда бэкапа с дефолтной/
переменной базой (`-d $POSTGRES_DB`) сняла дамп НЕ той базы — pg_dump
выполнился успешно, размер ненулевой, ошибки нет. Плюс первый дамп лежал в
`/tmp` контейнера — не переживает пересоздание.

## Solution

1. Базу указывать ЯВНО: `pg_dump -U $POSTGRES_USER -d max_bot_db -F c -f …`.
2. Проверять СОДЕРЖИМОЕ, а не факт создания: `pg_restore -l <файл> | head`
   — в оглавлении должны быть НАШИ таблицы (users, cabinet_tokens,
   bot_registry_snapshot). Чужие имена = не та база.
3. Класть на постоянный том: `/var/lib/postgresql/data/backups/` (данные PG
   и так на томе). Восстановление:
   `pg_restore -U $POSTGRES_USER -d max_bot_db --clean /var/lib/postgresql/data/backups/<файл>.dump`.

## Prevention

- «Бэкап есть» ≠ «бэкап той базы и читается»: непустота + оглавление +
  расположение на томе — три обязательные проверки, все в runbook.
- На multi-db серверах никогда не полагаться на $POSTGRES_DB/дефолт —
  только явное имя базы в команде.
