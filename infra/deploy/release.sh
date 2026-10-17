#!/bin/bash
# Выкатка релиза: ./deploy/release.sh vX.Y.Z (ARCHITECTURE.md 7.4, В12; infra/README.md, раздел 16).
# Запускает владелец — это второе подтверждение после публикации релиза в GitHub.
# Метка vX.Y.Z на образах есть, только если её поставил .github/workflows/release.yml (проверки там).
#  1. git fetch меток, коммит метки.
#  2. Образы vX.Y.Z из GHCR, файлы из коммита метки, PLATFORM_TAG в .env, up -d, проверка /health и версии.
#  3. Проверка не прошла — возврат прошлой версии, строка RELEASE FAILED.
set -uo pipefail
. "$(dirname "$0")/lib.sh"
JOB=release

main() {
  local version=${1:-} rc
  if ! [[ "$version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "usage: ./deploy/release.sh vX.Y.Z"
    return 64
  fi
  take_lock 600 || { log "another deploy is running: try later"; return 1; }
  git -C "$REPO_DIR" fetch -q --tags origin || { log "git fetch failed"; return 1; }
  version_commit "$version" >/dev/null || { log "tag ${version} not found in GitHub"; return 1; }
  switch_to "$version"
  rc=$?
  case "$rc" in
    0) log "RELEASE OK ${version}" ;;
    2) log "RELEASE FAILED ${version}: images not found, check the release.yml run in GitHub Actions"; return 1 ;;
    *) log "RELEASE FAILED ${version}" ;;
  esac
  return "$rc"
}

# Весь скрипт прочитан до запуска: git checkout внутри main может заменить этот файл.
main "$@"; exit $?
