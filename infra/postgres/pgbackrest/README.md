# infra/postgres/pgbackrest/

Место под настройки pgBackRest (непрерывный архив журнала и копии базы в Yandex Object Storage,
`docs/architecture/ARCHITECTURE.md`, 7.5). Настраивается в задаче 5.3. В 5.1 здесь ничего нет,
`archive_mode` в PostgreSQL выключен.
