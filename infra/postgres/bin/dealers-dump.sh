#!/bin/bash
# Ежедневный дамп всех баз и ролей (ARCHITECTURE.md 7.5, слой 2). Работает внутри контейнера postgres
# под пользователем postgres; запускает его infra/backup/dump.sh с хоста.
# 1) роли (pg_dumpall --globals-only) и каждая база (pg_dump -Fc) шифруются openssl сразу при записи —
#    незашифрованного дампа на диске нет (в дампах персональные данные);
# 2) папка дня загружается rclone в тот же бакет, что у pgBackRest: dump/<дата>/;
# 3) в хранилище удаляются дампы старше DUMP_RETENTION_DAYS дней, на диске остаётся только последний.
set -euo pipefail

DATABASES=(platform n8n nocodb_meta)   # базы из infra/postgres/init/10-roles.sh
DUMP_ROOT=${DUMP_ROOT:-/var/backups/dealers-dump}
RETENTION_DAYS=${DUMP_RETENTION_DAYS:-30}

for name in PGBACKREST_REPO1_S3_BUCKET PGBACKREST_REPO1_S3_KEY PGBACKREST_REPO1_S3_KEY_SECRET DUMP_CIPHER_PASS; do
  if [ -z "${!name:-}" ]; then
    echo "CONFIG ERROR: missing ${name}" >&2
    exit 1
  fi
done
if ! [[ "$RETENTION_DAYS" =~ ^[1-9][0-9]*$ ]]; then
  echo "CONFIG ERROR: DUMP_RETENTION_DAYS must be a positive number of days" >&2
  exit 1
fi

# Хранилище для rclone задаётся переменными (rclone.org/docs, «Config file» → переменные RCLONE_CONFIG_*);
# ключи — те же, что у pgBackRest, отдельных секретов нет.
export RCLONE_CONFIG_YOS_TYPE=s3
export RCLONE_CONFIG_YOS_PROVIDER=Other
export RCLONE_CONFIG_YOS_ENDPOINT=storage.yandexcloud.net
export RCLONE_CONFIG_YOS_REGION=ru-central1
export RCLONE_CONFIG_YOS_FORCE_PATH_STYLE=true
export RCLONE_CONFIG_YOS_NO_CHECK_BUCKET=true
export RCLONE_CONFIG_YOS_ACCESS_KEY_ID="$PGBACKREST_REPO1_S3_KEY"
export RCLONE_CONFIG_YOS_SECRET_ACCESS_KEY="$PGBACKREST_REPO1_S3_KEY_SECRET"
REMOTE="yos:${PGBACKREST_REPO1_S3_BUCKET}/dump"

day=$(date -u +%Y-%m-%d)
work="${DUMP_ROOT}/.partial-${day}"
rm -rf "$work"
mkdir -m 700 "$work"

# Пароль шифрования передаётся через окружение (-pass env:), а не в командной строке.
encrypt() {
  openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:DUMP_CIPHER_PASS -out "$1"
}

pg_dumpall --globals-only | encrypt "${work}/globals.sql.enc"
for db in "${DATABASES[@]}"; do
  pg_dump -Fc -d "$db" | encrypt "${work}/${db}.dump.enc"
done
(cd "$work" && sha256sum -- *.enc > SHA256SUMS)

rclone copy --retries 3 "$work" "${REMOTE}/${day}"

# Загрузка прошла: папка дня становится «последним дампом», более старые с диска удаляются.
find "$DUMP_ROOT" -mindepth 1 -maxdepth 1 ! -name ".partial-${day}" -exec rm -rf {} +
mv "$work" "${DUMP_ROOT}/${day}"

rclone delete --min-age "${RETENTION_DAYS}d" "$REMOTE"

echo "dump ${day} uploaded: globals ${DATABASES[*]}; retention ${RETENTION_DAYS}d"
