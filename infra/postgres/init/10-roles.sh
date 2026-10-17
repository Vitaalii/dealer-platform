#!/bin/bash
# Первый запуск PostgreSQL: роли и базы платформы (docs/architecture/ARCHITECTURE.md, 2.2, 1.4, 8.1).
# Выполняется образом postgres один раз, когда том данных пустой.
# Пароли берутся из окружения контейнера через \getenv и не попадают ни в лог, ни в командную строку.
# Схемы и права внутри базы platform создают миграции (infra/db/migrations).
set -euo pipefail

for name in PLATFORM_ADMIN_PASSWORD PLATFORM_APP_PASSWORD NOCODB_SRC_PASSWORD BOT_APP_PASSWORD \
            N8N_READER_PASSWORD N8N_DB_PASSWORD NOCODB_META_PASSWORD; do
  if [ -z "${!name:-}" ]; then
    echo "CONFIG ERROR: missing ${name}" >&2
    exit 1
  fi
done

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<'EOSQL'
\getenv platform_admin_pw PLATFORM_ADMIN_PASSWORD
\getenv platform_app_pw PLATFORM_APP_PASSWORD
\getenv nocodb_src_pw NOCODB_SRC_PASSWORD
\getenv bot_app_pw BOT_APP_PASSWORD
\getenv n8n_reader_pw N8N_READER_PASSWORD
\getenv n8n_pw N8N_DB_PASSWORD
\getenv nocodb_meta_pw NOCODB_META_PASSWORD

-- Владелец базы platform и её схем; под этой ролью идут только миграции.
CREATE ROLE platform_admin LOGIN PASSWORD :'platform_admin_pw';
-- Код платформы: чтение и запись в схемах платформы.
CREATE ROLE platform_app LOGIN PASSWORD :'platform_app_pw';
-- NocoDB как внешний источник: правка catalog и content, в platform_ro только чтение.
CREATE ROLE nocodb_src LOGIN PASSWORD :'nocodb_src_pw';
-- Бот: чтение catalog и content (DEC:23).
CREATE ROLE bot_app LOGIN PASSWORD :'bot_app_pw';
-- n8n читает каталог и контент из базы платформы (DEC:23).
CREATE ROLE n8n_reader LOGIN PASSWORD :'n8n_reader_pw';
-- Владельцы служебных баз n8n и NocoDB (ARCHITECTURE.md 2.2, «отдельные базы»; 7.3).
CREATE ROLE n8n LOGIN PASSWORD :'n8n_pw';
CREATE ROLE nocodb_meta LOGIN PASSWORD :'nocodb_meta_pw';

CREATE DATABASE platform OWNER platform_admin;
CREATE DATABASE n8n OWNER n8n;
CREATE DATABASE nocodb_meta OWNER nocodb_meta;

-- Подключаться к базе может только тот, кому это явно разрешено.
REVOKE ALL ON DATABASE platform FROM PUBLIC;
REVOKE ALL ON DATABASE n8n FROM PUBLIC;
REVOKE ALL ON DATABASE nocodb_meta FROM PUBLIC;
GRANT CONNECT ON DATABASE platform TO platform_app, nocodb_src, bot_app, n8n_reader;
EOSQL

# В базе platform схема public не нужна никому, кроме владельца.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname platform <<'EOSQL'
ALTER SCHEMA public OWNER TO platform_admin;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
EOSQL

echo "platform roles and databases created"
