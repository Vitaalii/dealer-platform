#!/bin/bash
# Раз в час: диск занят меньше DISK_MAX_PERCENT процентов (по умолчанию 80, ARCHITECTURE.md 7.5).
# Проверяется раздел, где Docker хранит тома (/var/lib/docker), а если его нет — корень.
# Сигнал: <env>-disk; при заполнении — код выхода 1, Healthchecks поднимает тревогу.
set -uo pipefail
. "$(dirname "$0")/lib.sh"
JOB=disk
MAX=${DISK_MAX_PERCENT:-80}
DISK_PATH=${DISK_PATH:-/var/lib/docker}
[ -d "$DISK_PATH" ] || DISK_PATH=/

check_disk() {
  local used
  used=$(df -P "$DISK_PATH" | awk 'NR == 2 { sub("%", "", $5); print $5 }')
  if ! [[ "$used" =~ ^[0-9]+$ ]]; then
    echo "cannot read disk usage for ${DISK_PATH}"
    return 1
  fi
  echo "disk ${DISK_PATH}: used ${used}%, limit ${MAX}%"
  [ "$used" -lt "$MAX" ]
}

run_job light check_disk
