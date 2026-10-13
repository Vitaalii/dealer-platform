"""U2 (план 2026-09-15-001): миграция users — колонки bot_key/raw_platform_id,
бэкфилл на каждом старте (в init_db, после ALTER-блока), дозаполнение
идентичности в get_or_create_user/save_user_phone. Диалект-зависимая
легаси-схема — по образцу tests/test_legacy_schema_upgrade.py (a32f0b4)."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect as sa_inspect, select, text

import database
from database import User, get_or_create_user, save_user_phone

# users, как его создал бы create_all моделью ДО U2 (без bot_key/raw_platform_id)
LEGACY_USERS = """CREATE TABLE users (
    id {id_pk}, platform_user_id VARCHAR NOT NULL UNIQUE,
    channel VARCHAR DEFAULT 'max' NOT NULL, full_name VARCHAR,
    username VARCHAR, phone VARCHAR, active_deal_id VARCHAR,
    context_json TEXT, is_smartbot_active BOOLEAN)"""


@pytest.fixture(autouse=True)
def _bot_env(monkeypatch):
    # U9: бэкфилл и шаг 2d зависят от BOT_ENV — фиксируем dev (как на стенде),
    # чтобы тесты не зависели от .env машины
    monkeypatch.setenv("BOT_ENV", "dev")


def _legacy_ddl(dialect: str) -> str:
    if dialect == "postgresql":
        return LEGACY_USERS.format(id_pk="SERIAL PRIMARY KEY")
    return LEGACY_USERS.format(id_pk="INTEGER PRIMARY KEY")


async def _recreate_legacy_users(rows):
    """Пересоздать users легаси-схемой и залить строки сырым INSERT.

    PG: у cabinet-таблиц FK на users — DROP ... CASCADE снимает эти
    constraints (сами таблицы остаются); для проверок миграции они не нужны."""
    dialect = database.engine.dialect.name
    drop = "DROP TABLE users CASCADE" if dialect == "postgresql" else "DROP TABLE users"
    async with database.engine.begin() as conn:
        await conn.execute(text(drop))
        await conn.execute(text(_legacy_ddl(dialect)))
        for puid, channel in rows:
            await conn.execute(
                text("INSERT INTO users (platform_user_id, channel, context_json) "
                     "VALUES (:p, :c, '{}')"), {"p": puid, "c": channel})


async def _users_map():
    async with database.AsyncSessionLocal() as session:
        users = (await session.execute(select(User))).scalars().all()
    return {u.platform_user_id: u for u in users}


@pytest.mark.asyncio
async def test_init_db_adds_columns_and_backfills_by_channel(db):
    await _recreate_legacy_users([
        ("111", "max"), ("222", "telegram"), ("sess-3", "web"),
        ("444", "tiledesk"),                       # неизвестный канал
    ])
    await database.init_db()

    def _cols(sync_conn):
        return {c["name"] for c in sa_inspect(sync_conn).get_columns("users")}
    async with database.engine.connect() as conn:
        cols = await conn.run_sync(_cols)
    assert {"bot_key", "raw_platform_id"} <= cols

    users = await _users_map()
    # U9: бэкфилл — ключами окружения (BOT_ENV=dev → max-test/tg-test)
    assert (users["111"].bot_key, users["111"].raw_platform_id) == ("max-test", "111")
    assert (users["222"].bot_key, users["222"].raw_platform_id) == ("tg-test", "222")
    assert (users["sess-3"].bot_key, users["sess-3"].raw_platform_id) == ("web", "sess-3")
    # неизвестный канал — NULL: легальное состояние, легаси-фолбэк KTD-6
    assert users["444"].bot_key is None and users["444"].raw_platform_id is None


@pytest.mark.asyncio
async def test_backfill_idempotent_and_runs_every_start(db):
    await _recreate_legacy_users([("111", "max")])
    await database.init_db()
    # повторный старт ничего не меняет
    await database.init_db()
    users = await _users_map()
    assert (users["111"].bot_key, users["111"].raw_platform_id) == ("max-test", "111")

    # строка, созданная ПОСЛЕ первого прогона без bot_key (окно U2→U3 /
    # вставка «старой моделью» при откате) — подметается следующим стартом
    async with database.engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (platform_user_id, channel, context_json) "
                 "VALUES ('555', 'telegram', '{}')"))
    await database.init_db()
    users = await _users_map()
    assert (users["555"].bot_key, users["555"].raw_platform_id) == ("tg-test", "555")


@pytest.mark.asyncio
async def test_backfill_skips_composite_anomaly(db):
    """Аномалия: составной platform_user_id без bot_key — бэкфилл НЕ портит
    её легаси-значениями (raw_platform_id не должен стать составным)."""
    async with database.engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (platform_user_id, channel, context_json) "
                 "VALUES ('max-haval:123', 'max', '{}')"))
    await database.init_db()
    users = await _users_map()
    anomaly = users["max-haval:123"]
    assert anomaly.bot_key is None
    assert anomaly.raw_platform_id is None


@pytest.mark.asyncio
async def test_backfill_preserves_existing_values(db):
    """Строки с заполненным bot_key не трогаются: raw_platform_id не
    перезаписывается (WHERE bot_key IS NULL сцепляет оба присвоения)."""
    async with database.AsyncSessionLocal() as session:
        session.add(User(platform_user_id="max-haval:777", channel="max",
                         bot_key="max-haval", raw_platform_id="777"))
        session.add(User(platform_user_id="778", channel="max",
                         bot_key="max-haval", raw_platform_id="778"))
        await session.commit()
    await database.init_db()
    users = await _users_map()
    assert users["max-haval:777"].bot_key == "max-haval"
    assert users["max-haval:777"].raw_platform_id == "777"
    assert users["778"].bot_key == "max-haval"          # не перетёрт легаси-ключом


@pytest.mark.asyncio
async def test_coexistence_legacy_and_composite_same_raw_id(db):
    """R4: тот же человек в легаси-боте и в новом — две независимые строки."""
    legacy = await get_or_create_user("123", channel="telegram",
                                     bot_key="tg-main", raw_platform_id="123")
    brand = await get_or_create_user("tg-haval:123", channel="telegram",
                                     bot_key="tg-haval", raw_platform_id="123")
    assert legacy.id != brand.id
    users = await _users_map()
    assert users["123"].bot_key == "tg-main"
    assert users["tg-haval:123"].bot_key == "tg-haval"
    assert users["tg-haval:123"].raw_platform_id == "123"


@pytest.mark.asyncio
async def test_get_or_create_user_fills_nulls_never_overwrites(db):
    # пользователь без идентичности (создан старым путём)
    await get_or_create_user("999", channel="max")
    users = await _users_map()
    assert users["999"].bot_key is None

    # дозаполнение NULL при следующем событии с идентичностью
    user = await get_or_create_user("999", channel="max",
                                    bot_key="max-main", raw_platform_id="999")
    assert user.bot_key == "max-main" and user.raw_platform_id == "999"

    # непустые значения НЕ перезаписываются (R12)
    user = await get_or_create_user("999", channel="max",
                                    bot_key="max-haval", raw_platform_id="000")
    assert user.bot_key == "max-main" and user.raw_platform_id == "999"
    users = await _users_map()
    assert users["999"].bot_key == "max-main"


@pytest.mark.asyncio
async def test_save_user_phone_contact_first_event(db):
    """«Контакт как первое событие» TG (main.py: save_user_phone ДО
    process_input): пользователь создаётся с корректной идентичностью,
    а не с дефолтным channel='max'."""
    ok = await save_user_phone("tg-haval:314", "+79001234567", "Иван", "ivan",
                               channel="telegram", bot_key="tg-haval",
                               raw_platform_id="314")
    assert ok is True
    users = await _users_map()
    u = users["tg-haval:314"]
    assert u.channel == "telegram"
    assert u.bot_key == "tg-haval"
    assert u.raw_platform_id == "314"
    assert u.phone == "+79001234567"


@pytest.mark.asyncio
async def test_historic_keys_migrated_to_env_keys(db):
    """U9 шаг 2d: исторические max-main/tg-main переименовываются в ключи
    окружения на каждом старте; сами platform_user_id не меняются (нулевая
    миграция ID — решение владельца 2026-09-23, п. е)."""
    async with database.AsyncSessionLocal() as session:
        session.add(User(platform_user_id="700", channel="max",
                         bot_key="max-main", raw_platform_id="700"))
        session.add(User(platform_user_id="701", channel="telegram",
                         bot_key="tg-main", raw_platform_id="701"))
        await session.commit()
    await database.init_db()
    users = await _users_map()
    assert users["700"].bot_key == "max-test"
    assert users["701"].bot_key == "tg-test"
    assert users["700"].platform_user_id == "700"       # голый ID не тронут
    # идемпотентность
    await database.init_db()
    users = await _users_map()
    assert users["700"].bot_key == "max-test"


@pytest.mark.asyncio
async def test_historic_keys_not_migrated_without_bot_env(db, monkeypatch):
    """BOT_ENV не задан → окружение неизвестно, угадывать нельзя: шаг 2d
    не выполняется, исторический ключ остаётся (alias-резолв прикрывает)."""
    monkeypatch.delenv("BOT_ENV", raising=False)
    async with database.AsyncSessionLocal() as session:
        session.add(User(platform_user_id="702", channel="max",
                         bot_key="max-main", raw_platform_id="702"))
        await session.commit()
    await database.init_db()
    users = await _users_map()
    assert users["702"].bot_key == "max-main"


@pytest.mark.asyncio
async def test_old_model_insert_works_after_migration(db):
    """Симуляция отката кода ниже U2: вставка без новых колонок проходит
    (обе nullable) — откат кода не требует отката схемы."""
    async with database.engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (platform_user_id, channel, context_json) "
                 "VALUES ('rollback-1', 'max', '{}')"))
    users = await _users_map()
    assert users["rollback-1"].bot_key is None