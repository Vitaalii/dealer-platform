"""U1: схема БД кабинета — 7 таблиц и частичные уникальные индексы (R12, R17, R19, R22, R40).
План 2026-08-06 U1: новые колонки cabinet_form (состав анкеты по дизайну) и
cabinet_profile.calc_snapshot/calc_frozen_at (снапшот калькулятора)."""
import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect as sa_inspect, select, text
from sqlalchemy.exc import IntegrityError

import database
from database import (
    CabinetEvent,
    CabinetForm,
    CabinetOtp,
    CabinetProfile,
    CabinetSession,
    CabinetToken,
    Consent,
    User,
    ensure_single_active,
)

CABINET_TABLES = {
    "cabinet_tokens", "cabinet_otp", "cabinet_sessions", "cabinet_profile",
    "cabinet_form", "consents", "cabinet_events",
}
PARTIAL_INDEXES = {"uq_cabinet_tokens_active_user", "uq_cabinet_otp_inflight_token"}


# Диалект-нейтральные хелперы схемы (SQLite и PostgreSQL): сюита гоняется на
# обоих диалектах (TEST_DATABASE_URL), sqlite_master/PRAGMA на PG не существуют.

async def _schema_names(engine):
    def _read(sync_conn):
        insp = sa_inspect(sync_conn)
        tables = set(insp.get_table_names())
        indexes = set()
        for t in tables:
            indexes |= {ix["name"] for ix in insp.get_indexes(t)}
        return tables, indexes
    async with engine.connect() as conn:
        return await conn.run_sync(_read)


async def _table_columns(engine, table):
    def _read(sync_conn):
        return {c["name"] for c in sa_inspect(sync_conn).get_columns(table)}
    async with engine.connect() as conn:
        return await conn.run_sync(_read)


async def _index_ddl(engine, names):
    """DDL индексов: pg_indexes на PostgreSQL, sqlite_master на SQLite."""
    in_list = ", ".join(f"'{n}'" for n in sorted(names))
    if engine.dialect.name == "postgresql":
        q = f"SELECT indexname, indexdef FROM pg_indexes WHERE indexname IN ({in_list})"
    else:
        q = f"SELECT name, sql FROM sqlite_master WHERE type='index' AND name IN ({in_list})"
    async with engine.connect() as conn:
        return dict((await conn.execute(text(q))).all())

# Фикстура db (чистый SQLite + подмена глобального engine) — общая, в conftest.py.


async def _make_user(db, suffix="1"):
    async with db.AsyncSessionLocal() as session:
        user = User(platform_user_id=f"u-{suffix}", channel="max")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def _token(user_id, **kw):
    defaults = dict(
        user_id=user_id,
        dealer_code="01",
        deal_id="D-1",
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex[:32],
        status="active",
    )
    defaults.update(kw)
    return CabinetToken(**defaults)


def _otp(token_id, **kw):
    defaults = dict(
        token_id=token_id,
        phone="+79001234567",
        code_hmac="a" * 64,
        external_id=str(uuid.uuid4()),
        channel="voice",
        status="sending",
    )
    defaults.update(kw)
    return CabinetOtp(**defaults)


# ---------------------------------------------------------------
# 1. init_db(): таблицы, частичные индексы, идемпотентность
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_init_db_creates_tables_and_partial_indexes(db):
    tables, indexes = await _schema_names(db.engine)

    assert CABINET_TABLES <= tables, f"не хватает таблиц: {CABINET_TABLES - tables}"
    assert PARTIAL_INDEXES <= indexes, f"не хватает индексов: {PARTIAL_INDEXES - indexes}"

    # Индексы действительно частичные: WHERE-предикат в DDL (формулировку
    # диалекты нормализуют по-разному — проверяем предикат по содержанию)
    ddl = await _index_ddl(db.engine, PARTIAL_INDEXES)
    active_ddl = ddl["uq_cabinet_tokens_active_user"]
    inflight_ddl = ddl["uq_cabinet_otp_inflight_token"]
    assert "WHERE" in active_ddl and "'active'" in active_ddl
    assert "WHERE" in inflight_ddl and "'sending'" in inflight_ddl \
        and "'sent'" in inflight_ddl


@pytest.mark.asyncio
async def test_init_db_is_idempotent(db):
    # Повторный вызов не падает и не плодит объектов (PG-инспектор видит
    # и юник-констрейнты как индексы — например uq_cabinet_form_user_deal,
    # поэтому проверка подмножеством, как в тесте создания)
    await db.init_db()
    _, indexes = await _schema_names(db.engine)
    assert PARTIAL_INDEXES <= indexes


# ---------------------------------------------------------------
# 2. cabinet_tokens: один active на user_id; уникальность external_id
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_second_active_token_same_user_rejected_by_index(db):
    user = await _make_user(db)
    async with db.AsyncSessionLocal() as session:
        session.add(_token(user.id))
        await session.commit()

    async with db.AsyncSessionLocal() as session:
        assert not await ensure_single_active(session, user.id), \
            "предпроверка видит существующий active-токен"
        session.add(_token(user.id))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    # Не-active статусы сосуществуют: индекс частичный
    async with db.AsyncSessionLocal() as session:
        session.add(_token(user.id, status="revoked"))
        session.add(_token(user.id, status="expired"))
        await session.commit()
        assert await ensure_single_active(session, user.id) is False  # active всё ещё один


@pytest.mark.asyncio
async def test_active_tokens_of_different_users_coexist(db):
    u1 = await _make_user(db, "a")
    u2 = await _make_user(db, "b")
    async with db.AsyncSessionLocal() as session:
        session.add(_token(u1.id))
        session.add(_token(u2.id))
        await session.commit()
        assert await ensure_single_active(session, u1.id) is False
        assert await ensure_single_active(session, u2.id) is False


@pytest.mark.asyncio
async def test_duplicate_external_id_rejected(db):
    user = await _make_user(db)
    async with db.AsyncSessionLocal() as session:
        tok = _token(user.id)
        session.add(tok)
        await session.commit()
        await session.refresh(tok)

    ext = str(uuid.uuid4())
    async with db.AsyncSessionLocal() as session:
        session.add(_otp(tok.id, external_id=ext, status="verified"))
        await session.commit()

    # Дубль external_id
    async with db.AsyncSessionLocal() as session:
        session.add(_otp(tok.id, external_id=ext, status="superseded"))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    # Разные external_id сосуществуют в терминальных статусах
    async with db.AsyncSessionLocal() as session:
        session.add(_otp(tok.id, status="expired"))
        session.add(_otp(tok.id, status="failed_send"))
        await session.commit()


# ---------------------------------------------------------------
# 3. cabinet_otp: одна запись в полёте (sending/sent) на токен
# ---------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("existing_status", ["sending", "sent"])
async def test_second_inflight_otp_rejected(db, existing_status):
    user = await _make_user(db)
    async with db.AsyncSessionLocal() as session:
        tok = _token(user.id)
        session.add(tok)
        await session.commit()
        await session.refresh(tok)

    async with db.AsyncSessionLocal() as session:
        session.add(_otp(tok.id, status=existing_status))
        await session.commit()

    async with db.AsyncSessionLocal() as session:
        session.add(_otp(tok.id, status="sending"))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    # Завершённые статусы индексом не блокируются
    async with db.AsyncSessionLocal() as session:
        session.add(_otp(tok.id, status="verified"))
        await session.commit()


@pytest.mark.asyncio
async def test_inflight_otp_allowed_after_terminal_status(db):
    user = await _make_user(db)
    async with db.AsyncSessionLocal() as session:
        tok = _token(user.id)
        session.add(tok)
        await session.commit()
        await session.refresh(tok)

    async with db.AsyncSessionLocal() as session:
        otp = _otp(tok.id, status="sent")
        session.add(otp)
        await session.commit()
        # Ручной повтор: старая запись → superseded, новая — sending
        otp.status = "superseded"
        session.add(_otp(tok.id, status="sending"))
        await session.commit()

    async with db.AsyncSessionLocal() as session:
        statuses = list((await session.execute(
            select(CabinetOtp.status).where(CabinetOtp.token_id == tok.id)
        )).scalars())
    assert sorted(statuses) == ["sending", "superseded"]


# ---------------------------------------------------------------
# 4. cabinet_form: уникальность (user_id, deal_id)
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_cabinet_form_unique_per_user_and_deal(db):
    user = await _make_user(db)

    # Разные deal_id одного user_id сосуществуют
    async with db.AsyncSessionLocal() as session:
        session.add(CabinetForm(user_id=user.id, deal_id="D-1", credit_term=36))
        session.add(CabinetForm(user_id=user.id, deal_id="D-2", credit_term=48))
        await session.commit()

    # Тот же (user_id, deal_id) — конфликт
    async with db.AsyncSessionLocal() as session:
        session.add(CabinetForm(user_id=user.id, deal_id="D-1"))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


# ---------------------------------------------------------------
# U1 (план 2026-08-06): новые колонки анкеты и снапшота калькулятора
# ---------------------------------------------------------------

CABINET_FORM_NEW_COLUMNS = {
    "marital_status", "education", "owners_by_pts", "pts_type",
    "keys_count", "client_comment", "combo_extra_down",
}


@pytest.mark.asyncio
async def test_new_form_and_snapshot_columns_exist_after_init_db(db):
    """Колонки живут и в моделях, и в таблицах (create_all + идемпотентные
    ALTER для существующих БД дают один и тот же итог)."""
    form_cols = await _table_columns(db.engine, "cabinet_form")
    profile_cols = await _table_columns(db.engine, "cabinet_profile")
    assert CABINET_FORM_NEW_COLUMNS <= form_cols, \
        f"не хватает колонок анкеты: {CABINET_FORM_NEW_COLUMNS - form_cols}"
    assert {"calc_snapshot", "calc_frozen_at"} <= profile_cols
    # Выведенные из анкеты поля остаются колонками БД (R1)
    assert {"income", "work_experience", "family_composition", "tradein_damage",
            "tradein_engine", "tradein_vin", "tradein_owners"} <= form_cols

    # Повторный init_db идемпотентен: инспектор видит существующие колонки,
    # ни один ALTER не выполняется, состав не меняется
    await db.init_db()
    form_cols_again = await _table_columns(db.engine, "cabinet_form")
    assert form_cols_again == form_cols


@pytest.mark.asyncio
async def test_form_new_fields_and_profile_snapshot_roundtrip(db):
    user = await _make_user(db)
    snapshot = {"schema_version": 1, "item_id": "C-42", "price": 2490000,
                "found_info": {"steering_wheel": "LEFT"}, "ti_valuation": 850000}
    frozen_at = datetime(2026, 8, 6, 12, 30, tzinfo=timezone.utc)

    async with db.AsyncSessionLocal() as session:
        session.add(CabinetForm(
            user_id=user.id, deal_id="D-1",
            marital_status="Женат/замужем", education="Высшее",
            owners_by_pts="2", pts_type="Оригинал", keys_count="Более 2",
            client_comment="Есть скол", combo_extra_down=150000))
        # JSON-снапшот — конвенция Text+JSON (json.dumps в колонку Text)
        session.add(CabinetProfile(user_id=user.id, deal_id="D-1",
                                   dealer_code="01",
                                   calc_snapshot=json.dumps(snapshot),
                                   calc_frozen_at=frozen_at))
        await session.commit()

    async with db.AsyncSessionLocal() as session:
        form = (await session.execute(
            select(CabinetForm).where(CabinetForm.user_id == user.id)
        )).scalar_one()
        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == user.id)
        )).scalar_one()
    assert form.marital_status == "Женат/замужем"
    assert form.combo_extra_down == 150000
    assert json.loads(profile.calc_snapshot) == snapshot
    assert profile.calc_frozen_at is not None


# ---------------------------------------------------------------
# Смоук: остальные модели вставляются с ожидаемыми дефолтами
# ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_remaining_models_insert_with_defaults(db):
    user = await _make_user(db)
    async with db.AsyncSessionLocal() as session:
        tok = _token(user.id)
        session.add(tok)
        await session.commit()
        await session.refresh(tok)

    async with db.AsyncSessionLocal() as session:
        session.add(CabinetSession(token_id=tok.id, user_id=user.id, session_hash="b" * 64))
        session.add(CabinetProfile(user_id=user.id, dealer_code="01", qualification_payment=35000))
        session.add(Consent(user_id=user.id, token_id=tok.id,
                            consent_version="v1", consent_text_hash="c" * 64))
        session.add(CabinetEvent(user_id=user.id, token_id=tok.id, event_type="link_opened"))
        await session.commit()

    async with db.AsyncSessionLocal() as session:
        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == user.id)
        )).scalar_one()
        assert profile.steps_status == "{}"
        event = (await session.execute(select(CabinetEvent))).scalar_one()
        assert event.created_at is not None and event.meta_json == "{}"
        otp_defaults = CabinetOtp(token_id=tok.id, phone="+79001234567",
                                  code_hmac="d" * 64, external_id=str(uuid.uuid4()),
                                  channel="sms")
        session.add(otp_defaults)
        await session.commit()
        await session.refresh(otp_defaults)
        assert otp_defaults.status == "sending", "жизненный цикл OTP начинается со sending"


# ---------------------------------------------------------------
# Веб-вход (план 2026-09-30-001, U1): колонки, таблицы, индекс оценки
# ---------------------------------------------------------------

WEB_PROFILE_COLUMNS = {"lead_code", "lead_nonce_hash", "return_url", "web_plate",
                       "web_mileage", "link_fallback", "valuation_id"}


@pytest.mark.asyncio
async def test_web_entry_schema_present_after_init_db(db):
    tables, indexes = await _schema_names(db.engine)
    assert {"cabinet_web_opens", "cabinet_valuations"} <= tables
    assert "uq_cabinet_valuations_pending" in indexes
    assert "entry_source" in await _table_columns(db.engine, "cabinet_tokens")
    profile_cols = await _table_columns(db.engine, "cabinet_profile")
    assert WEB_PROFILE_COLUMNS <= profile_cols
    ddl = await _index_ddl(db.engine, {"uq_cabinet_valuations_pending"})
    assert "WHERE" in ddl["uq_cabinet_valuations_pending"] \
        and "'pending'" in ddl["uq_cabinet_valuations_pending"]


@pytest.mark.asyncio
async def test_web_entry_columns_added_to_existing_tables(db):
    """База, созданная до веб-входа: init_db добавляет колонки ALTER-ами без
    потери строк (create_all существующие таблицы не трогает)."""
    user = await _make_user(db)
    async with db.AsyncSessionLocal() as session:
        session.add(_token(user.id))
        session.add(CabinetProfile(user_id=user.id, deal_id="D-1", dealer_code="01"))
        await session.commit()
    async with db.engine.begin() as conn:
        for col in ("entry_source",):
            await conn.execute(text(f"ALTER TABLE cabinet_tokens DROP COLUMN {col}"))
        await conn.execute(text("DROP INDEX IF EXISTS ix_cabinet_profile_lead_code"))
        for col in sorted(WEB_PROFILE_COLUMNS):
            await conn.execute(text(f"ALTER TABLE cabinet_profile DROP COLUMN {col}"))

    await db.init_db()

    assert "entry_source" in await _table_columns(db.engine, "cabinet_tokens")
    assert WEB_PROFILE_COLUMNS <= await _table_columns(db.engine, "cabinet_profile")
    async with db.AsyncSessionLocal() as session:
        token = (await session.execute(select(CabinetToken))).scalar_one()
        profile = (await session.execute(select(CabinetProfile))).scalar_one()
    assert token.entry_source is None          # старые строки — чатовые
    assert profile.dealer_code == "01" and profile.lead_code is None


@pytest.mark.asyncio
async def test_new_token_defaults_to_chat_entry(db):
    user = await _make_user(db)
    async with db.AsyncSessionLocal() as session:
        session.add(_token(user.id))
        await session.commit()
        token = (await session.execute(select(CabinetToken))).scalar_one()
    assert token.entry_source == "chat"


@pytest.mark.asyncio
async def test_second_pending_valuation_same_key_rejected(db):
    async with db.AsyncSessionLocal() as session:
        session.add(database.CabinetValuation(dealer_code="01", plate="А123ВС77"))
        await session.commit()
    async with db.AsyncSessionLocal() as session:
        session.add(database.CabinetValuation(dealer_code="01", plate="А123ВС77"))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_finished_valuation_does_not_block_new_pending(db):
    async with db.AsyncSessionLocal() as session:
        session.add(database.CabinetValuation(dealer_code="01", plate="А123ВС77",
                                              status="success"))
        session.add(database.CabinetValuation(dealer_code="01", plate="А123ВС77"))
        session.add(database.CabinetValuation(dealer_code="02", plate="А123ВС77"))
        await session.commit()
        rows = (await session.execute(select(database.CabinetValuation))).scalars().all()
    assert len(rows) == 3
    assert all(r.our_failure is False for r in rows)


@pytest.mark.asyncio
async def test_web_open_log_row_defaults(db):
    async with db.AsyncSessionLocal() as session:
        session.add(database.CabinetWebOpen(ip="1.2.3.4", dealer_code="01"))
        await session.commit()
        row = (await session.execute(select(database.CabinetWebOpen))).scalar_one()
    assert row.kind == "open" and row.created_at is not None
