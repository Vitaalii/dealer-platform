"""Код-ревью (план 2026-08-27-001): база, созданная ДО снятия автофолбэка,
несёт легаси-колонки, среди которых NOT NULL fallback_attempts. Модель колонку
больше не передаёт — без чистки в init_db каждая вставка OTP падала бы
IntegrityError, а обработчик гонки маскировал бы это под вечный 429.
init_db обязан удалить легаси-колонки (SCHEMA_COLUMN_DROPS), после чего
вставка работает; настоящая гонка по-прежнему отдаёт 429.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect as sa_inspect, text

import database
from database import CabinetOtp

# Легаси-схема — как её создал бы create_all старой моделью на каждом диалекте:
# SQLite — INTEGER PRIMARY KEY (неявный rowid) и DATETIME, PostgreSQL — SERIAL
# и timestamptz (DATETIME на PG не существует, а без автоинкремента id упала бы
# финальная вставка ORM).
LEGACY_CABINET_OTP = """CREATE TABLE cabinet_otp (
    id {id_pk}, token_id INTEGER NOT NULL, phone VARCHAR NOT NULL,
    code_hmac VARCHAR NOT NULL, external_id VARCHAR(36) UNIQUE,
    sms_external_id VARCHAR(36) UNIQUE, channel VARCHAR NOT NULL,
    status VARCHAR NOT NULL, voice_msg_id VARCHAR, sms_msg_id VARCHAR,
    delivery_state VARCHAR, fallback_at {dt},
    fallback_attempts INTEGER NOT NULL, sent_at {dt},
    expires_at {dt} NOT NULL)"""


def _legacy_ddl(dialect: str) -> str:
    if dialect == "postgresql":
        return LEGACY_CABINET_OTP.format(
            id_pk="SERIAL PRIMARY KEY", dt="TIMESTAMP WITH TIME ZONE")
    return LEGACY_CABINET_OTP.format(id_pk="INTEGER PRIMARY KEY", dt="DATETIME")


async def _cabinet_otp_columns() -> set:
    """Имена колонок cabinet_otp через инспектор (PRAGMA на PG не существует)."""
    def _read(sync_conn):
        return {c["name"] for c in sa_inspect(sync_conn).get_columns("cabinet_otp")}
    async with database.engine.connect() as conn:
        return await conn.run_sync(_read)


@pytest.mark.asyncio
async def test_init_db_drops_legacy_fallback_columns_and_inserts_work(db):
    # Пересоздать cabinet_otp старой схемой поверх свежей базы фикстуры
    async with database.engine.begin() as conn:
        await conn.execute(text("DROP TABLE cabinet_otp"))
        await conn.execute(text(_legacy_ddl(database.engine.dialect.name)))

    # init_db на легаси-базе: добирает недостающее и удаляет легаси-колонки
    await database.init_db()

    cols = await _cabinet_otp_columns()
    for legacy in ("fallback_at", "fallback_attempts"):
        assert legacy not in cols, f"легаси-колонка {legacy} пережила init_db"
    # sms_external_id остаётся на обоих диалектах: её нет в SCHEMA_COLUMN_DROPS
    # (SQLite не дропает UNIQUE-колонку); nullable — вставкам не мешает
    # (проверяется вставкой ниже)

    # Смысл чистки: вставка новой моделью проходит (раньше — NOT NULL violation)
    async with db.AsyncSessionLocal() as session:
        session.add(CabinetOtp(
            token_id=1, phone="+79990000001", code_hmac="h", external_id="lg-1",
            channel="voice", status="sending",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5)))
        await session.commit()
