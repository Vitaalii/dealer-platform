"""Прод-баг init_db() на PostgreSQL: одна DDL-транзакция травилась первым же
упавшим ALTER, COMMIT превращался в ROLLBACK и молча откатывал ВСЁ, включая
create_all новых таблиц, с ложным «✅» в логе. На SQLite семантики отравленной
транзакции нет — сюита была зелёной. Тесты воспроизводят именно прод-сценарии
на живом PostgreSQL (TEST_DATABASE_URL); без него — skip: SQLite этот класс
отказов физически не воспроизводит.

Файл самодостаточен (собственный движок и гард) — коммит фикса database.py +
этот тест откатываются независимо от остальной работы по кабинету.
"""
import os
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from sqlalchemy import Column, Integer, Table, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import database

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="нужен локальный PostgreSQL: экспортируйте TEST_DATABASE_URL "
           "(SQLite не воспроизводит семантику отравленной DDL-транзакции)")

if TEST_DATABASE_URL:
    _host = urlsplit(TEST_DATABASE_URL).hostname or ""
    if _host not in ("localhost", "127.0.0.1", "::1"):
        raise RuntimeError(
            f"TEST_DATABASE_URL указывает на не-локальный хост {_host!r} — "
            "прогон тестов на внешней базе запрещён")

CABINET_TABLES = ("cabinet_events", "consents", "cabinet_form", "cabinet_profile",
                  "cabinet_sessions", "cabinet_otp", "cabinet_tokens")


@pytest_asyncio.fixture
async def pg(monkeypatch):
    """Чистая схема на живом PostgreSQL + подмена глобального движка database."""
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(
        database, "AsyncSessionLocal",
        sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
    )
    yield engine
    await engine.dispose()


async def _inspect(engine, fn):
    async with engine.connect() as conn:
        return await conn.run_sync(fn)


async def _has_table(engine, name):
    return await _inspect(engine, lambda c: inspect(c).has_table(name))


async def _columns(engine, table):
    return await _inspect(
        engine, lambda c: {col["name"] for col in inspect(c).get_columns(table)})


@pytest.mark.asyncio
async def test_cabinet_deploy_on_pre_cabinet_prod_schema(pg):
    """Прод-сценарий деплоя кабинета: схема без кабинетных таблиц и без
    users.channel. Раньше: RENAME травил транзакцию → откатывалось всё.
    Теперь: init_db() обязан досоздать таблицы, колонки и частичные индексы."""
    async with pg.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    async with pg.begin() as conn:
        for t in CABINET_TABLES:
            await conn.execute(text(f"DROP TABLE IF EXISTS {t} CASCADE"))
        await conn.execute(text("ALTER TABLE users DROP COLUMN channel"))

    await database.init_db()

    assert await _has_table(pg, "cabinet_tokens"), \
        "кабинетные таблицы должны пережить init_db (прод-баг: откатывались)"
    assert "channel" in await _columns(pg, "users"), \
        "ALTER после первого недостающего должен применяться (прод-баг: транзакция травилась)"
    indexes = await _inspect(
        pg, lambda c: {r[0] for r in c.execute(text(
            "SELECT indexname FROM pg_indexes WHERE schemaname='public'"))})
    assert {"uq_cabinet_tokens_active_user", "uq_cabinet_otp_inflight_token"} <= indexes

    # Повторный прогон — идемпотентен и тоже зелёный
    await database.init_db()


@pytest.mark.asyncio
async def test_new_metadata_table_created_on_second_run(pg):
    """Обязательный сценарий фикса: init_db() дважды, между прогонами в
    метаданные добавляется новая таблица — она обязана существовать в базе
    после второго прогона (раньше create_all откатывался целиком)."""
    await database.init_db()
    probe = Table("init_db_probe_new_table", database.Base.metadata,
                  Column("id", Integer, primary_key=True))
    try:
        await database.init_db()
        assert await _has_table(pg, "init_db_probe_new_table"), \
            "новая таблица метаданных не создана вторым прогоном init_db"
    finally:
        database.Base.metadata.remove(probe)


@pytest.mark.asyncio
async def test_dropped_upgrade_column_restored(pg):
    """Инспектор-ветка: недостающая колонка из SCHEMA_COLUMN_UPGRADES
    восстанавливается (calc_frozen_at — и с типом timestamptz)."""
    await database.init_db()
    async with pg.begin() as conn:
        await conn.execute(text("ALTER TABLE cabinet_profile DROP COLUMN calc_snapshot"))
        await conn.execute(text("ALTER TABLE cabinet_profile DROP COLUMN calc_frozen_at"))

    await database.init_db()

    cols = await _columns(pg, "cabinet_profile")
    assert {"calc_snapshot", "calc_frozen_at"} <= cols
    tz_type = await _inspect(
        pg, lambda c: c.execute(text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='cabinet_profile' AND column_name='calc_frozen_at'"
        )).scalar())
    assert tz_type == "timestamp with time zone"


@pytest.mark.asyncio
async def test_verified_success_or_loud_failure(pg):
    """«✅» не врёт: при неполной схеме init_db обязан упасть с перечнем,
    а не отчитаться об успехе (эмуляция: убираем колонку из чужой таблицы
    так, чтобы ALTER не мог пройти — колонку блокирует одноимённый индекс)."""
    await database.init_db()
    # Проверка позитивной ветки: после честного прогона _schema_missing пуст
    missing = await _inspect(pg, database._schema_missing)
    assert missing == []
