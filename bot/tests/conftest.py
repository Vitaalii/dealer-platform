"""Общие фикстуры и фейки тестов кабинета.

Конвенция репозитория: чистый SQLite на файл (`db`) + подмена зависимостей
cabinet_service тем же setup() (`deps`). Модули переопределяют фикстуру
`dealers` (карта дилеров для setup) и/или расширяют `db`/`deps` локальными
override'ами, не копируя общий код.
"""
import os
import pathlib
import sys
import types
from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite.base import DATETIME as SQLITE_DATETIME
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import cabinet_service
import database
from database import CabinetToken
from sms_gateway import BalanceResult, SendResult

# =======================
# Общие константы
# =======================

CONSENT = {"version": "v1", "accepted": True}
PHONE = "+79001234567"
DEALERS = {
    "01": {"dealer_name": "Тест Авто", "website": "https://dealer-one.ru/", "code": "01"},
}


# =======================
# Общие фейки
# =======================

class FakeGateway:
    """Скриптуемый шлюз (SmsGateway-совместимый): очередь SendResult'ов
    в `results`; при пустой очереди — успех. Код фиксируется в calls."""

    def __init__(self):
        self.results = []
        self.calls = []
        self.balance_result = BalanceResult(ok=True, credits="1000")

    async def send_otp(self, phone, code, channel, external_id):
        self.calls.append({"phone": phone, "code": code,
                           "channel": channel, "external_id": external_id})
        if self.results:
            return self.results.pop(0)
        return SendResult(ok=True, provider_msg_id=f"prov-{len(self.calls)}")

    async def get_balance(self):
        return self.balance_result

    def supports_voice(self):
        return True


class FakeBitrix:
    """Скриптуемый bitrix_request: конвенция BitrixService._request —
    сбой (5xx/ретраи исчерпаны) наружу выглядит как пустой dict."""

    def __init__(self):
        self.calls = []
        self.fail = False

    async def __call__(self, method, params, user_id=None):
        self.calls.append({"method": method, "params": params, "user_id": user_id})
        if self.fail:
            return {}
        return {"result": 1}

    def by_method(self, method):
        return [c for c in self.calls if c["method"] == method]


# =======================
# Общие фикстуры
# =======================

# Прогон сюиты на живом PostgreSQL (внешний блокер релиза): переменная
# TEST_DATABASE_URL переключает фикстуры db/aware_db с per-test SQLite на
# реальную базу (чистая схема на каждый тест). Без переменной поведение
# прежнее — SQLite, ничего не меняется.
#
# ГАРД БЕЗОПАСНОСТИ: только localhost — боевая база (Dockhost) недостижима
# из тестового режима ни при какой комбинации env/.env.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()

if TEST_DATABASE_URL:
    _test_db_host = urlsplit(TEST_DATABASE_URL).hostname or ""
    if _test_db_host not in ("localhost", "127.0.0.1", "::1"):
        raise RuntimeError(
            f"TEST_DATABASE_URL указывает на не-локальный хост {_test_db_host!r} — "
            "прогон тестов на внешней базе запрещён")


async def _fresh_pg_engine():
    """Движок на TEST_DATABASE_URL с чистой схемой — эквивалент нового SQLite-файла."""
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    return engine


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    """Чистая БД на тест + подмена глобального engine, чтобы init_db() работал как в бою.

    SQLite по умолчанию; при TEST_DATABASE_URL — живой PostgreSQL."""
    if TEST_DATABASE_URL:
        engine = await _fresh_pg_engine()
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(
        database, "AsyncSessionLocal",
        sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
    )
    await database.init_db()
    yield database
    await engine.dispose()


async def _aware_probe(database_mod, user_id):
    """Самопроверка aware-чтения: без неё тест тихо выродился бы в наивный режим."""
    async with database_mod.AsyncSessionLocal() as session:
        session.add(CabinetToken(user_id=user_id, dealer_code="probe",
                                 token_hash="probe", status="probe",
                                 expires_at=datetime.now(timezone.utc)))
        await session.commit()
    async with database_mod.AsyncSessionLocal() as session:
        read_back = (await session.execute(
            select(CabinetToken).where(CabinetToken.token_hash == "probe")
        )).scalar_one().expires_at
    assert read_back is not None and read_back.tzinfo is not None, \
        "aware-чтение времени не работает — тест не проверяет боевой PostgreSQL"


@pytest_asyncio.fixture
async def aware_db(tmp_path, monkeypatch):
    """БД, отдающая время timezone-aware, как боевой PostgreSQL/asyncpg.

    При TEST_DATABASE_URL — сам живой PostgreSQL (aware по природе, патч не
    нужен). Иначе — SQLite с патчем result_processor: все временные колонки
    кабинета объявлены DateTime(timezone=True), в бою значения приходят aware,
    на SQLite — наивные, и обычный `db` этот класс отказов не воспроизводит.
    Наивное внутреннее время роняло бы TypeError'ом validate_bearer,
    _get_live_token и otp_worker — то есть весь кабинет."""
    if TEST_DATABASE_URL:
        engine = await _fresh_pg_engine()
        monkeypatch.setattr(database, "engine", engine)
        monkeypatch.setattr(
            database, "AsyncSessionLocal",
            sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
        )
        await database.init_db()
        # PG применяет FK (SQLite в тестах — нет): пробе нужен реальный user
        async with database.AsyncSessionLocal() as session:
            probe_user = database.User(platform_user_id="aware-probe", channel="max")
            session.add(probe_user)
            await session.commit()
            await session.refresh(probe_user)
        await _aware_probe(database, probe_user.id)
        yield database
        await engine.dispose()
        return

    orig_processor = SQLITE_DATETIME.result_processor

    def aware_result_processor(self, dialect, coltype):
        inner = orig_processor(self, dialect, coltype)

        def process(value):
            parsed = inner(value) if inner is not None else value
            if isinstance(parsed, datetime) and parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed

        return process

    monkeypatch.setattr(SQLITE_DATETIME, "result_processor", aware_result_processor)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'aware.db'}",
                                 query_cache_size=0)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(
        database, "AsyncSessionLocal",
        sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
    )
    await database.init_db()

    # Самопроверка эмуляции: без неё тест тихо выродился бы в обычный SQLite
    # (user_id=0 допустим только тут: SQLite в тестах FK не применяет)
    await _aware_probe(database, 0)

    yield database
    await engine.dispose()


@pytest.fixture
def dealers():
    """Карта дилеров, отдаваемая dealers_provider'ом в setup(); модули переопределяют."""
    return DEALERS


@pytest.fixture
def nocodb_tables():
    """Таблицы фейкового NocoDB-загрузчика (U5, table_id → строки);
    модули переопределяют. Загрузчик уходит в setup() как nocodb_table_loader."""
    return {}


@pytest.fixture
def deps(monkeypatch, dealers, nocodb_tables):
    """Подмена зависимостей тем же setup() + фейковый шлюз + чистое состояние."""
    monkeypatch.setenv("CABINET_HMAC_KEY", "test-hmac-key")
    monkeypatch.setenv("CABINET_ALERT_RETRY_DELAY", "0")  # [Р11] без пауз в тестах
    calls = types.SimpleNamespace(enqueues=[], tech_alerts=[], dealer_alerts=[],
                                  conversions=[], loader_calls=[])
    bitrix = FakeBitrix()

    async def enqueue_nodebounce(method, params, user_id=None):
        calls.enqueues.append((method, params, user_id))

    async def run_conversion(*args, **kwargs):
        calls.conversions.append((args, kwargs))

    async def dealer_alert(dealer_code, text):
        calls.dealer_alerts.append((dealer_code, text))

    async def tech_alert(text):
        calls.tech_alerts.append(text)

    async def nocodb_table_loader(table_id):
        calls.loader_calls.append(table_id)
        return nocodb_tables.get(table_id, [])

    cabinet_service.setup(bitrix, enqueue_nodebounce, run_conversion,
                          dealer_alert, tech_alert, lambda: dealers,
                          nocodb_table_loader=nocodb_table_loader,
                          nocodb_base_url="https://noco.test")
    gw = FakeGateway()
    cabinet_service.set_gateway(gw)
    cabinet_service.reset_breaker()
    cabinet_service.reset_send_status_limiter()
    cabinet_service.reset_dealer_cap_alerts()
    cabinet_service.reset_config_alert_dedup()
    cabinet_service.reset_min_down_alerts()
    cabinet_service.reset_price_fallback_alerts()
    cabinet_service.reset_inactive_position_alerts()
    cabinet_service.set_valuation_client(None)
    calls.gw = gw
    calls.bitrix = bitrix
    yield calls
    cabinet_service.set_gateway(None)
    cabinet_service.set_valuation_client(None)
    cabinet_service.reset_breaker()
    cabinet_service.reset_send_status_limiter()
    cabinet_service.reset_dealer_cap_alerts()
    cabinet_service.reset_config_alert_dedup()


@pytest.fixture(autouse=True)
def _drain_background_handoffs(monkeypatch):
    """Ускорение B (2026-10-03): передача после отправки анкеты идёт в фоне.
    Тесты существующих путей проверяют Битрикс сразу после submit_form —
    обёртка дожидается фоновых передач, сам фоновый путь остаётся включённым.
    Тесты ответа ДО завершения передачи берут cabinet_service._submit_form_raw."""
    import cabinet_service
    raw = cabinet_service.submit_form

    async def submit_and_drain(*args, **kwargs):
        result = await raw(*args, **kwargs)
        await cabinet_service.drain_background_handoffs()
        return result

    monkeypatch.setattr(cabinet_service, "_submit_form_raw", raw, raising=False)
    monkeypatch.setattr(cabinet_service, "submit_form", submit_and_drain)


@pytest.fixture(autouse=True)
def _isolate_n8n_channel_env(monkeypatch):
    """Каналы бот ↔ n8n: тесты не зависят от .env стенда (там может стоять
    strict и настоящие секреты [TEST]); нужные значения тесты задают сами."""
    for name in ("N8N_CALLBACK_AUTH_MODE", "N8N_CALLBACK_SECRET",
                 "N8N_WEBHOOK_SECRET", "N8N_SESSION_LOGS_URL"):
        monkeypatch.delenv(name, raising=False)
