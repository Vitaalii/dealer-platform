#!/bin/bash
# Автообновление теста из main (ARCHITECTURE.md 7.4, В12). Запускает таймер dealers-update.timer раз в 5 минут.
#  1. git fetch main по Deploy key. Коммит тот же, что стоит, или уже не прошёл проверку — выход.
#  2. Образы sha-<коммит> из GHCR. Их ещё нет (Actions не дособрал) — выход, повтор через 5 минут.
#  3. Файлы из нового коммита, PLATFORM_TAG в .env, docker compose up -d, проверка /health и версии.
#  4. Проверка не прошла — возврат прошлой версии и строка UPDATE FAILED в журнале (journalctl -u dealers-update).
# Только для теста: на бою таймера нет, там release.sh.
set -uo pipefail
. "$(dirname "$0")/lib.sh"
JOB=update

main() {
  local commit new rc
  take_lock 0 || { log "another deploy is running: skipped"; return 0; }
  git -C "$REPO_DIR" fetch -q origin main || { log "git fetch failed"; return 1; }
  commit=$(git -C "$REPO_DIR" rev-parse --verify -q origin/main) || { log "origin/main not found"; return 1; }
  new="sha-${commit}"
  [ "$new" = "$(state_get current)" ] && return 0
  [ "$new" = "$(state_get failed)" ] && return 0
  log "new commit on main: ${commit}"
  switch_to "$new"
  rc=$?
  case "$rc" in
    0) log "UPDATE OK ${new}" ;;
    2) log "images for ${new} are not ready yet: retry in 5 minutes"; return 0 ;;
    *) log "UPDATE FAILED ${new}" ;;
  esac
  return "$rc"
}

# Весь скрипт прочитан до запуска: git checkout внутри main может заменить этот файл.
main "$@"; exit $?
