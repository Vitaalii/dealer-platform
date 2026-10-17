#!/bin/bash
# Раз в неделю: автоматическое пробное восстановление (ARCHITECTURE.md 7.5, «Проверка»).
# Копия pgBackRest + архив журнала восстанавливаются во ВРЕМЕННЫЙ том и временный контейнер,
# рабочие база, том и сервисы (NocoDB, n8n) не останавливаются и не трогаются.
#  1. В рабочей базе: число строк в таблицах платформы и точка восстановления check-<время>;
#     pgbackrest check переключает журнал и ждёт, пока сегмент с точкой попадёт в архив.
#  2. pgbackrest restore во временный том до этой точки, с --archive-mode=off: временная база
#     не пишет свой журнал в общий архив.
#  3. Временный PostgreSQL (без сети наружу на портах: listen_addresses пусто, ports не публикуются).
#  4. Проверки: (а) восстановление дошло до точки — архив журнала цел до текущего момента;
#     (б) схемы catalog, content, platform_ro на месте; (в) число строк совпадает с рабочей базой;
#     (г) pg_dump каждой базы проходит — все данные читаются; (д) pgbackrest verify — файлы копий целы.
#  5. Временный контейнер и том удаляются в любом случае (trap).
# Сигнал: <env>-restore-check (код выхода и краткий итог).
set -uo pipefail
. "$(dirname "$0")/lib.sh"
JOB=restore-check

IMAGE=${RESTORE_IMAGE:-dealers/postgres:17}
VOLUME=dealers_restore_check
CONTAINER=dealers-restore-check
RESTORE_PATH=/restore            # не путь рабочего тома /var/lib/postgresql/data
RESTORE_MEM=${RESTORE_MEM_LIMIT:-512m}
RECOVERY_TIMEOUT=${RESTORE_TIMEOUT_SEC:-3600}
DATABASES=(platform n8n nocodb_meta)
SCHEMAS="catalog,content,platform_ro"
PGBR_ENV=(PGBACKREST_REPO1_S3_BUCKET PGBACKREST_REPO1_S3_KEY PGBACKREST_REPO1_S3_KEY_SECRET PGBACKREST_REPO1_CIPHER_PASS)

# Число строк в каждой таблице платформы, одной строкой на таблицу: "схема.таблица число".
COUNT_SQL="select format('%s.%s %s', table_schema, table_name,
  (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', table_schema, table_name),
   false, true, '')))[1]::text)
from information_schema.tables
where table_schema in ('catalog', 'content', 'platform_ro', 'platform_meta') and table_type = 'BASE TABLE'
order by 1;"

cleanup() {
  docker rm -f "$CONTAINER" "${CONTAINER}-restore" >/dev/null 2>&1
  docker volume rm -f "$VOLUME" >/dev/null 2>&1
  echo "cleanup: temporary container and volume ${VOLUME} removed"
}

tmp_psql() {
  docker exec "$CONTAINER" psql -X -q -At -v ON_ERROR_STOP=1 "$@"
}

wait_promoted() {
  local waited=0 state
  while [ "$waited" -lt "$RECOVERY_TIMEOUT" ]; do
    if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ]; then
      echo "FAIL: temporary postgres stopped during recovery"
      docker logs --tail 30 "$CONTAINER" 2>&1
      return 1
    fi
    state=$(tmp_psql -d postgres -c 'select pg_is_in_recovery()' 2>/dev/null)
    [ "$state" = "f" ] && return 0
    sleep 10
    waited=$((waited + 10))
  done
  echo "FAIL: recovery not finished in ${RECOVERY_TIMEOUT}s"
  return 1
}

restore_check() {
  local target live restored tables db schemas name
  target="check-$(date -u +%Y%m%dT%H%M%SZ)"
  trap cleanup EXIT
  cleanup

  # Ключи хранилища — в окружение этого процесса; docker run -e ИМЯ берёт значение отсюда, в командной строке его нет.
  for name in "${PGBR_ENV[@]}"; do
    export "${name}=$(env_get "$name")"
    if [ -z "${!name}" ]; then echo "CONFIG ERROR: missing ${name} in .env"; return 1; fi
  done

  echo "1. live database: row counts and restore point ${target}"
  live=$(pg_exec psql -X -q -At -v ON_ERROR_STOP=1 -d platform <<SQL
begin isolation level repeatable read;
${COUNT_SQL}
select 'restore_point ' || pg_create_restore_point('${target}');
commit;
SQL
) || { echo "FAIL: live database query"; return 1; }
  echo "$live" | grep -q '^restore_point ' || { echo "FAIL: restore point not created"; return 1; }
  live=$(echo "$live" | grep -v '^restore_point ')
  pg_exec pgbackrest --stanza="$STANZA" check || { echo "FAIL: pgbackrest check (WAL archive)"; return 1; }

  echo "2. restore into temporary volume ${VOLUME}"
  docker volume create "$VOLUME" >/dev/null || return 1
  docker run --rm --name "${CONTAINER}-restore" -u postgres -v "${VOLUME}:${RESTORE_PATH}" \
    -e PGBACKREST_REPO1_S3_BUCKET -e PGBACKREST_REPO1_S3_KEY -e PGBACKREST_REPO1_S3_KEY_SECRET -e PGBACKREST_REPO1_CIPHER_PASS \
    "$IMAGE" pgbackrest --stanza="$STANZA" --pg1-path="$RESTORE_PATH" --type=name --target="$target" \
    --target-action=promote --archive-mode=off restore || { echo "FAIL: pgbackrest restore"; return 1; }

  echo "3. temporary postgres (memory ${RESTORE_MEM}, no listening ports)"
  docker run -d --name "$CONTAINER" -m "$RESTORE_MEM" -u postgres -v "${VOLUME}:${RESTORE_PATH}" \
    -e PGDATA="$RESTORE_PATH" \
    -e PGBACKREST_REPO1_S3_BUCKET -e PGBACKREST_REPO1_S3_KEY -e PGBACKREST_REPO1_S3_KEY_SECRET -e PGBACKREST_REPO1_CIPHER_PASS \
    "$IMAGE" postgres -D "$RESTORE_PATH" -c archive_mode=off -c shared_buffers=128MB -c listen_addresses= \
    >/dev/null || { echo "FAIL: temporary postgres did not start"; return 1; }
  wait_promoted || return 1

  echo "4a. recovery reached restore point"
  docker logs "$CONTAINER" 2>&1 | grep -F "recovery stopping at restore point \"${target}\"" \
    || { echo "FAIL: restore point ${target} not reached"; return 1; }

  echo "4b. schemas"
  schemas=$(tmp_psql -d platform -c "select string_agg(nspname, ',' order by nspname) from pg_namespace
    where nspname in ('catalog', 'content', 'platform_ro')")
  [ "$schemas" = "$SCHEMAS" ] || { echo "FAIL: schemas '${schemas}', expected '${SCHEMAS}'"; return 1; }

  echo "4c. row counts"
  restored=$(tmp_psql -d platform -c "$COUNT_SQL") || { echo "FAIL: restored database query"; return 1; }
  if [ "$restored" != "$live" ]; then
    echo "FAIL: row counts differ (live / restored):"
    diff <(echo "$live") <(echo "$restored")
    return 1
  fi
  tables=$(echo "$live" | grep -c . || true)

  echo "4d. pg_dump of every database"
  for db in "${DATABASES[@]}"; do
    docker exec "$CONTAINER" pg_dump -Fc -d "$db" -f /dev/null || { echo "FAIL: pg_dump ${db}"; return 1; }
  done

  echo "4e. pgbackrest verify"
  pg_exec pgbackrest --stanza="$STANZA" verify || { echo "FAIL: pgbackrest verify"; return 1; }

  echo "OK: restore point ${target}; schemas ${schemas}; ${tables} tables, row counts equal:"
  echo "$live"
  echo "OK: pg_dump ${DATABASES[*]}; pgbackrest verify"
}

run_job heavy restore_check
