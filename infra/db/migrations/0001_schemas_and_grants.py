"""Схемы catalog, content, platform_ro и права ролей (docs/architecture/ARCHITECTURE.md, 2.2, 1.4, 8.1).

Роли создаёт infra/postgres/init/10-roles.sh. Миграция выполняется под platform_admin — владельцем
базы platform. ALTER DEFAULT PRIVILEGES распространяет права на таблицы будущих миграций.
Запрет NocoDB на правку platform_ro держит база: у nocodb_src там только SELECT.
SQL хранится в .py, потому что корневой .gitignore исключает *.sql.
"""

SQL = """
CREATE SCHEMA catalog AUTHORIZATION platform_admin;
CREATE SCHEMA content AUTHORIZATION platform_admin;
CREATE SCHEMA platform_ro AUTHORIZATION platform_admin;

-- Код платформы: всё в трёх схемах.
GRANT USAGE ON SCHEMA catalog, content, platform_ro TO platform_app;
ALTER DEFAULT PRIVILEGES FOR ROLE platform_admin IN SCHEMA catalog, content, platform_ro
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO platform_app;
ALTER DEFAULT PRIVILEGES FOR ROLE platform_admin IN SCHEMA catalog, content, platform_ro
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO platform_app;

-- NocoDB: правка строк в catalog и content (без создания таблиц), в platform_ro только чтение.
GRANT USAGE ON SCHEMA catalog, content, platform_ro TO nocodb_src;
ALTER DEFAULT PRIVILEGES FOR ROLE platform_admin IN SCHEMA catalog, content
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO nocodb_src;
ALTER DEFAULT PRIVILEGES FOR ROLE platform_admin IN SCHEMA catalog, content
    GRANT USAGE, SELECT ON SEQUENCES TO nocodb_src;
ALTER DEFAULT PRIVILEGES FOR ROLE platform_admin IN SCHEMA platform_ro
    GRANT SELECT ON TABLES TO nocodb_src;

-- Бот и n8n: чтение каталога и контента (DEC:23).
GRANT USAGE ON SCHEMA catalog, content TO bot_app, n8n_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE platform_admin IN SCHEMA catalog, content
    GRANT SELECT ON TABLES TO bot_app, n8n_reader;

-- Проверочная таблица: на ней владелец убеждается, что NocoDB не может писать в platform_ro.
CREATE TABLE platform_ro.rights_check (
    id serial PRIMARY KEY,
    note text NOT NULL
);
INSERT INTO platform_ro.rights_check (note) VALUES ('nocodb_src: only SELECT here');
"""
