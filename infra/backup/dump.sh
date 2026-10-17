#!/bin/bash
# Раз в сутки: зашифрованный дамп всех баз и ролей в Object Storage (infra/postgres/bin/dealers-dump.sh).
# Сигнал: <env>-dump.
set -uo pipefail
. "$(dirname "$0")/lib.sh"
JOB=dump

run_job heavy pg_exec dealers-dump
