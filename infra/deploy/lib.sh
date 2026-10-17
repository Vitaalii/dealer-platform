#!/bin/bash
# Общая часть скриптов infra/deploy/*.sh (выкатка и откат, ARCHITECTURE.md 7.4, решение В12).
# Скрипты запускаются на сервере под deploy: update.sh — таймером на тесте, release.sh и rollback.sh —
# владельцем вручную (infra/README.md, разделы 15–17). GitHub на сервер не заходит: сервер сам забирает
# код по Deploy key и образы из GHCR по токену в ~/.docker/config.json.
#
# Версия — метка образов в GHCR: sha-<полный коммит> (каждое слияние в main) или vX.Y.Z (релиз).
# Она же пишется в infra/.env как PLATFORM_TAG, её читает compose.yaml.
# Состояние — в ~/dealers-deploy: current (что стоит), previous (что стояло до), failed (что не прошло
# проверку или откачено: update.sh его повторно не ставит).

INFRA_DIR=${INFRA_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
REPO_DIR=${REPO_DIR:-$(cd "${INFRA_DIR}/.." && pwd)}
ENV_FILE=${ENV_FILE:-${INFRA_DIR}/.env}
STATE_DIR=${STATE_DIR:-${HOME}/dealers-deploy}
HEALTH_TIMEOUT=${HEALTH_TIMEOUT_SEC:-300}
# Образы, которые собирает .github/workflows/build.yml; остальные (caddy, nocodb, n8n) — с Docker Hub
# по версиям из compose.yaml, их скрипты не трогают.
IMAGES=(platform bot-stub postgres)

log() {
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ${JOB:-deploy}: $*"
}

# Значение переменной из infra/.env без выполнения файла как кода (последняя строка ИМЯ=...).
env_get() {
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
}

# Записать ИМЯ=значение в infra/.env: заменить строку или дописать в конец.
# cat > сохраняет владельца и права файла (chmod 600); значения других строк не выводятся.
env_set() {
  local tmp
  tmp=$(mktemp)
  awk -v k="$1" -v v="$2" 'BEGIN { done = 0 }
    index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }' "$ENV_FILE" > "$tmp" && cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
}

state_get() {
  cat "${STATE_DIR}/$1" 2>/dev/null
}

state_set() {
  echo "$2" > "${STATE_DIR}/$1"
}

# Замок: одна выкатка за раз. $1 — сколько ждать (секунд); 0 — не ждать.
take_lock() {
  mkdir -p "$STATE_DIR"
  exec 9>"${STATE_DIR}/lock"
  if [ "$1" = 0 ]; then flock -n 9; else flock -w "$1" 9; fi
}

compose() {
  docker compose --project-directory "$INFRA_DIR" "$@"
}

# Коммит, из которого собрана версия: sha-<коммит> или метка vX.Y.Z (её коммит — из git).
version_commit() {
  case "$1" in
    sha-*) git -C "$REPO_DIR" rev-parse --verify -q "${1#sha-}^{commit}" ;;
    v*) git -C "$REPO_DIR" rev-parse --verify -q "refs/tags/$1^{commit}" ;;
    *) return 1 ;;
  esac
}

# Скачать три образа версии. Если реестр недоступен, но образ уже есть на сервере (откат) — годится он.
pull_images() {
  local prefix img ref
  prefix=$(env_get IMAGE_PREFIX)
  if [ -z "$prefix" ]; then
    log "CONFIG ERROR: missing IMAGE_PREFIX in .env"
    return 1
  fi
  for img in "${IMAGES[@]}"; do
    ref="${prefix}/${img}:$1"
    if ! docker pull -q "$ref" >/dev/null && ! docker image inspect "$ref" >/dev/null 2>&1; then
      log "image not found: ${ref}"
      return 1
    fi
  done
}

# Ждать, пока /health платформы ответит status=ok с версией, равной коммиту. Запрос — изнутри контейнера:
# так проверка не зависит от DNS и сертификата.
wait_health() {
  local expected="ok $1" waited=0 got=""
  while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    got=$(compose exec -T platform python -c "import json, urllib.request; d = json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)); print(d['status'], d['version'])" 2>/dev/null)
    if [ "$got" = "$expected" ]; then
      log "health ok, version $1"
      return 0
    fi
    sleep 5
    waited=$((waited + 5))
  done
  log "health check failed after ${HEALTH_TIMEOUT}s: expected '${expected}', got '${got:-no answer}'"
  return 1
}

# Поставить версию: образы, файлы compose из того же коммита, метка в .env, запуск, проверка.
# Код 2 — образов нет (ничего не менялось), 1 — сбой при запуске или проверке.
deploy_version() {
  local version=$1 commit
  commit=$(version_commit "$version") || { log "unknown version ${version}: no such commit or tag in git"; return 1; }
  pull_images "$version" || return 2
  git -C "$REPO_DIR" checkout -q --detach "$commit" || { log "git checkout ${commit} failed"; return 1; }
  env_set PLATFORM_TAG "$version"
  log "starting ${version} (${commit})"
  compose up -d --no-build || { log "docker compose up failed"; return 1; }
  wait_health "$commit"
}

# Переход на новую версию с возвратом прошлой при сбое. Коды — как у deploy_version.
switch_to() {
  local new=$1 cur rc
  cur=$(state_get current)
  deploy_version "$new"
  rc=$?
  if [ "$rc" = 0 ]; then
    if [ -n "$cur" ] && [ "$cur" != "$new" ]; then state_set previous "$cur"; fi
    state_set current "$new"
    log "DEPLOY OK ${new}"
    return 0
  fi
  [ "$rc" = 2 ] && return 2
  state_set failed "$new"
  log "DEPLOY FAILED ${new}"
  if [ -z "$cur" ]; then
    log "no previous version recorded: nothing to roll back to, see infra/README.md, section 17"
    return 1
  fi
  log "rolling back to ${cur}"
  if deploy_version "$cur"; then
    log "rolled back to ${cur}"
  else
    log "ROLLBACK FAILED ${cur}: manual action needed, see infra/README.md, section 17"
  fi
  return 1
}
