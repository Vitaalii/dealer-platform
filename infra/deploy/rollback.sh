#!/bin/bash
# Откат: ./deploy/rollback.sh [версия] (infra/README.md, раздел 17). Без аргумента — версия из
# ~/dealers-deploy/previous. Образы прошлой версии остаются на сервере, реестр для отката не обязателен.
# Схема базы не откатывается: миграции только добавляют (infra/db/migrate.py), прошлый код работает на новой схеме.
# Откаченная версия пишется в failed — update.sh не поставит её снова.
set -uo pipefail
. "$(dirname "$0")/lib.sh"
JOB=rollback

main() {
  local target cur
  target=${1:-$(state_get previous)}
  cur=$(state_get current)
  if [ -z "$target" ]; then
    log "no previous version in ${STATE_DIR}/previous: pass the version explicitly"
    return 1
  fi
  take_lock 600 || { log "another deploy is running: try later"; return 1; }
  log "rolling back from ${cur:-unknown} to ${target}"
  if ! deploy_version "$target"; then
    log "ROLLBACK FAILED ${target}"
    return 1
  fi
  state_set current "$target"
  if [ -n "$cur" ] && [ "$cur" != "$target" ]; then
    state_set previous "$cur"
    state_set failed "$cur"
  fi
  log "ROLLBACK OK ${target}"
}

# Весь скрипт прочитан до запуска: git checkout внутри main может заменить этот файл.
main "$@"; exit $?
