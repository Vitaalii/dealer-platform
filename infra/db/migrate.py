"""Исполнитель миграций базы platform.

Миграции — файлы infra/db/migrations/NNNN_имя.py со строкой SQL. Применённые версии хранятся
в platform_meta.schema_migrations. Каждая миграция — в своей транзакции; одновременный запуск
двух исполнителей исключает advisory-блокировка. Запуск: python db/migrate.py (сервис migrate).

Ключи только из окружения: PLATFORM_DB_HOST, PLATFORM_DB_NAME, MIGRATE_DB_USER, MIGRATE_DB_PASSWORD.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
FILE_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.py$")
REQUIRED_VARS = ("PLATFORM_DB_HOST", "PLATFORM_DB_NAME", "MIGRATE_DB_USER", "MIGRATE_DB_PASSWORD")
LOCK_ID = 5_1_0001  # постоянный номер advisory-блокировки миграций

BOOTSTRAP_SQL = """
CREATE SCHEMA IF NOT EXISTS platform_meta;
CREATE TABLE IF NOT EXISTS platform_meta.schema_migrations (
    version integer PRIMARY KEY,
    name text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Все миграции по возрастанию номера. Повтор номера или пустой SQL — ошибка."""
    found: dict[int, Migration] = {}
    for path in sorted(directory.iterdir()):
        match = FILE_RE.match(path.name)
        if not match:
            continue
        version = int(match.group(1))
        if version in found:
            raise ValueError(f"duplicate migration version {version}")
        spec = importlib.util.spec_from_file_location(f"migration_{version}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sql = getattr(module, "SQL", "")
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError(f"migration {path.name} has no SQL")
        found[version] = Migration(version, path.stem, sql)
    return [found[v] for v in sorted(found)]


def pending(migrations: list[Migration], applied: set[int]) -> list[Migration]:
    return [m for m in migrations if m.version not in applied]


def missing_vars(environ) -> list[str]:
    return [name for name in REQUIRED_VARS if not environ.get(name, "").strip()]


async def apply_all(environ) -> list[str]:
    import asyncpg  # есть в образе платформы; тестам не нужен

    conn = await asyncpg.connect(
        host=environ["PLATFORM_DB_HOST"],
        port=int(environ.get("PLATFORM_DB_PORT") or 5432),
        database=environ["PLATFORM_DB_NAME"],
        user=environ["MIGRATE_DB_USER"],
        password=environ["MIGRATE_DB_PASSWORD"],
        timeout=10,
    )
    done: list[str] = []
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK_ID)
        await conn.execute(BOOTSTRAP_SQL)
        applied = {r["version"] for r in await conn.fetch("SELECT version FROM platform_meta.schema_migrations")}
        for m in pending(discover(), applied):
            async with conn.transaction():
                await conn.execute(m.sql)
                await conn.execute(
                    "INSERT INTO platform_meta.schema_migrations (version, name) VALUES ($1, $2)",
                    m.version, m.name,
                )
            done.append(m.name)
            print(f"migration applied: {m.name}", flush=True)
    finally:
        await conn.close()
    return done


def main() -> int:
    missing = missing_vars(os.environ)
    if missing:
        print("CONFIG ERROR: missing " + ", ".join(missing), file=sys.stderr, flush=True)
        return 1
    done = asyncio.run(apply_all(os.environ))
    print(f"migrations done: {len(done)} applied", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
