#!/bin/bash
# Общая часть скриптов infra/backup/*.sh. Скрипты запускаются на хосте из crontab пользователя deploy
# (infra/backup/crontab) и работают с контейнером postgres через docker compose.
# Сигналы — Healthchecks.io: https://hc-ping.com/<ping key>/<имя проверки>[/start|/fail|/<код выхода>]
# (healthchecks.io/docs/http_api/). Имя проверки: <PLATFORM_ENV>-<задание>, например test-backup-full.

INFRA_DIR=${INFRA_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
ENV_FILE=${ENV_FILE:-${INFRA_DIR}/.env}
HC_BASE_URL=${HC_BASE_URL:-https://hc-ping.com}
LOCK_FILE=${LOCK_FILE:-/tmp/dealers-backup.lock}
STANZA=dealers

# Значение переменной из infra/.env без выполнения файла как кода (последняя строка ИМЯ=...).
env_get() {
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
}

log() {
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ${JOB:-backup}: $*"
}

# Сигнал в Healthchecks. $1 — суффикс: start, fail, код выхода или пусто; $2 — файл с текстом (необязательно).
# Ключ передаётся curl через stdin (-K -), а не в командной строке: так его не видно в списке процессов.
# Ошибка сигнала не роняет задание: само задание важнее, а молчание Healthchecks и так поднимет тревогу.
hc_ping() {
  local suffix=$1 body=${2:-} key env url
  key=$(env_get HEALTHCHECKS_PING_KEY)
  env=$(env_get PLATFORM_ENV)
  if [ -z "$key" ] || [ -z "$env" ]; then
    log "HEALTHCHECKS_PING_KEY or PLATFORM_ENV is empty in .env: signal skipped"
    return 0
  fi
  url="${HC_BASE_URL}/${key}/${env}-${JOB}"
  if [ -n "$suffix" ]; then url="${url}/${suffix}"; fi
  local args=(-fsS -m 10 --retry 5 -o /dev/null -K -)
  # Healthchecks хранит до 100 кБ тела сигнала (healthchecks.io/docs/http_api/) — run_job шлёт хвост лога.
  if [ -n "$body" ] && [ -s "$body" ]; then
    args+=(--data-binary "@${body}")
  fi
  printf 'url = "%s"\n' "$url" | curl "${args[@]}" >/dev/null 2>&1 || log "healthchecks signal failed: ${suffix:-ok}"
}

compose() {
  docker compose --project-directory "$INFRA_DIR" "$@"
}

# Команда внутри работающего контейнера postgres под пользователем postgres.
pg_exec() {
  compose exec -T -u postgres postgres "$@"
}

# Запуск задания: start-сигнал, вывод в лог, по окончании — сигнал с кодом выхода и хвостом лога.
# Тяжёлые задания (копия, дамп, восстановление) идут по очереди через общий замок.
run_job() {
  local heavy=$1; shift
  local out rc
  out=$(mktemp)
  hc_ping start
  if [ "$heavy" = heavy ]; then
    ( flock -w 10800 9 || { echo "lock wait timeout: ${LOCK_FILE}"; exit 75; }; "$@" ) 9>"$LOCK_FILE" >"$out" 2>&1
    rc=$?
  else
    "$@" >"$out" 2>&1
    rc=$?
  fi
  cat "$out"
  tail -c 90000 "$out" > "${out}.tail"
  hc_ping "$rc" "${out}.tail"
  rm -f "$out" "${out}.tail"
  log "finished with exit code ${rc}"
  return "$rc"
}
