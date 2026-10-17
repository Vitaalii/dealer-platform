# PostgreSQL платформы со своим образом: pgBackRest, rclone и скрипт дампа (задача 5.3).
FROM postgres:17
# Привязка пакета GHCR к репозиторию (docs.github.com, «Working with the Container registry»).
LABEL org.opencontainers.image.source=https://github.com/Vitaalii/dealer-platform

# pgbackrest — из репозитория PGDG, он уже подключён в базовом образе postgres:17;
# rclone и openssl — для ежедневного зашифрованного дампа (infra/README.md, раздел 13).
RUN apt-get update \
 && apt-get install -y --no-install-recommends pgbackrest rclone openssl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /var/spool/pgbackrest /var/log/pgbackrest /var/lib/pgbackrest /var/backups/dealers-dump /restore \
 && chown postgres:postgres /var/spool/pgbackrest /var/log/pgbackrest /var/lib/pgbackrest /var/backups/dealers-dump /restore \
 && chmod 700 /var/backups/dealers-dump /restore

# Настройки pgBackRest без секретов; ключи хранилища и пароль шифрования — переменными PGBACKREST_* из infra/.env.
COPY pgbackrest/pgbackrest.conf /etc/pgbackrest/pgbackrest.conf
# Ежедневный дамп всех баз и ролей (запускает infra/backup/dump.sh с хоста).
COPY bin/dealers-dump.sh /usr/local/bin/dealers-dump
RUN chmod 755 /usr/local/bin/dealers-dump

# Скрипт первого запуска: базы и роли. Выполняется один раз, когда том данных пустой.
COPY init/ /docker-entrypoint-initdb.d/
