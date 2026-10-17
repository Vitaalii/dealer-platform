#!/bin/bash
# Раз в час: архив журнала работает. pgbackrest check переключает журнал (pg_switch_wal) и ждёт,
# пока сегмент попадёт в архив (pgbackrest.org/command.html, check; archive-timeout). Проверка «возраста»
# последнего сегмента не годится: на пустой базе archive_timeout не срабатывает — была бы ложная тревога.
# Сигнал: <env>-wal.
set -uo pipefail
. "$(dirname "$0")/lib.sh"
JOB=wal

run_job light pg_exec pgbackrest --stanza="$STANZA" check
