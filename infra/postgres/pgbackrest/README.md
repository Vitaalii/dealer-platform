# infra/postgres/pgbackrest/

Настройки pgBackRest (`docs/architecture/ARCHITECTURE.md`, 7.5): непрерывный архив журнала и копии базы
в Yandex Object Storage, хранение 30 дней, шифрование на нашей стороне.

- `pgbackrest.conf` — копируется в образ как `/etc/pgbackrest/pgbackrest.conf`. Секретов в нём нет.
- Бакет, ключи и пароль шифрования — переменные `PGBACKREST_*` из `infra/.env` (`infra/.env.example`).
- Архив журнала включён в `command` сервиса `postgres` в **обоих** compose-файлах (`archive_mode=on`,
  `archive_timeout=60`, `archive_command=pgbackrest --stanza=dealers archive-push %p`).
- Расписание копий, дамп и пробное восстановление — `infra/backup/`, команды для владельца —
  `infra/README.md`, раздел 13.
