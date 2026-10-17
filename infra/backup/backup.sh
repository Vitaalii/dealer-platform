#!/bin/bash
# Копия базы pgBackRest: full — раз в неделю, diff — раз в сутки (ARCHITECTURE.md 7.5; расписание — crontab).
# После копии pgBackRest сам удаляет копии и журнал старше срока хранения (repo1-retention-full=30, дни).
# Сигналы: <env>-backup-full / <env>-backup-diff.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

type=${1:-}
case "$type" in
  full|diff) ;;
  *) echo "usage: $0 full|diff" >&2; exit 2 ;;
esac
JOB="backup-${type}"

do_backup() {
  pg_exec pgbackrest --stanza="$STANZA" --type="$type" backup && pg_exec pgbackrest --stanza="$STANZA" info
}

run_job heavy do_backup
