"""U3: колбэк статусов (запись-факт, поведение не меняет), otp_worker, breaker,
сигнал A (R26 сигнал A, R31, R32, R36). Автофолбэк голос→SMS снят
(план 2026-08-27-001, решение владельца)."""
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import make_mocked_request
from sqlalchemy import select

import database
from database import CabinetEvent, CabinetOtp, CabinetProfile, CabinetToken, User

import cabinet_service
from sms_gateway import BalanceResult

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"

def NOW():  # aware-UTC: naive в timestamptz PG интерпретируется по TZ машины
    return datetime.now(timezone.utc)


# =======================
# Фикстуры (общие db/deps — в conftest.py; здесь локальные отличия U3)
# =======================

@pytest.fixture
def dealers():
    """U3 не зависит от карты дилеров — setup() получает пустую."""
    return {}


@pytest.fixture
def deps(deps):
    """Общий deps + конвенция U3: bitrix_request всегда отвечает пустым dict."""
    deps.bitrix.fail = True
    return deps


# =======================
# Хелперы данных
# =======================

async def _mk_user(db, suffix="1", full_name="Иван", channel="telegram"):
    async with db.AsyncSessionLocal() as session:
        user = User(platform_user_id=f"u-{suffix}", channel=channel, full_name=full_name)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _mk_token(db, user_id, **kw):
    defaults = dict(user_id=user_id, dealer_code="01", deal_id="D-77",
                    token_hash=uuid.uuid4().hex + uuid.uuid4().hex[:32], status="active")
    defaults.update(kw)
    async with db.AsyncSessionLocal() as session:
        token = CabinetToken(**defaults)
        session.add(token)
        await session.commit()
        await session.refresh(token)
        return token


async def _mk_otp(db, token_id, code="1234", **kw):
    defaults = dict(
        token_id=token_id,
        phone="+79001234567",
        code_hmac=cabinet_service.compute_code_hmac(code),
        external_id=str(uuid.uuid4()),
        channel="voice",
        voice_msg_id="v-1",
        status="sent",
        sent_at=NOW(),
        expires_at=NOW() + timedelta(minutes=5),
    )
    defaults.update(kw)
    async with db.AsyncSessionLocal() as session:
        otp = CabinetOtp(**defaults)
        session.add(otp)
        await session.commit()
        await session.refresh(otp)
        return otp


async def _fetch_otp(db, otp_id):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(
            select(CabinetOtp).where(CabinetOtp.id == otp_id)
        )).scalar_one()


async def _fetch_token(db, token_id):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(
            select(CabinetToken).where(CabinetToken.id == token_id)
        )).scalar_one()


async def _events(db, event_type=None):
    async with db.AsyncSessionLocal() as session:
        q = select(CabinetEvent)
        if event_type:
            q = q.where(CabinetEvent.event_type == event_type)
        return (await session.execute(q)).scalars().all()


# =======================
# 1. Колбэк «не доставлено» по голосу — запись-факт, SMS не уходит (R1/R9)
# =======================

@pytest.mark.asyncio
async def test_voice_undelivered_records_state_without_sms(db, deps):
    plain = "plain-undelivered-token"
    user = await _mk_user(db)
    token = await _mk_token(db, user.id, token_hash=cabinet_service._hash_token(plain),
                            expires_at=NOW() + timedelta(hours=1))
    otp = await _mk_otp(db, token.id, code="4321")

    # Колбэк через реальный GET-хендлер; phone в запросе игнорируется (R36)
    req = make_mocked_request(
        "GET",
        f"/sms/status-callback?external_id={otp.external_id}&id_sms=v-1&state=2&phone=%2B79990000000",
    )
    resp = await cabinet_service.handle_status_callback(req)
    assert resp.status == 200

    assert deps.gw.calls == [], "автоотправки SMS больше нет — никаких вызовов шлюза"
    fresh = await _fetch_otp(db, otp.id)
    assert fresh.delivery_state == "2", "статус записан как факт"
    assert fresh.status == "sent", "статус кода не тронут"

    # send-status статусом доставки не управляется: код действителен → sent
    assert await cabinet_service.send_status(plain) == {"state": "sent"}

    fresh_token = await _fetch_token(db, token.id)
    assert fresh_token.failed_attempts == 0


# =======================
# 2. Колбэк по SMS-отправке: сверка id_sms с sms_msg_id, запись-факт
# =======================

@pytest.mark.asyncio
async def test_sms_callback_matches_sms_msg_id_and_records(db, deps):
    user = await _mk_user(db)
    token = await _mk_token(db, user.id)
    otp = await _mk_otp(db, token.id, channel="sms", voice_msg_id=None, sms_msg_id="s-1")

    assert await cabinet_service.process_status_callback(otp.external_id, "s-1", "1") == "recorded"
    fresh = await _fetch_otp(db, otp.id)
    assert fresh.delivery_state == "1"
    assert deps.gw.calls == []

    # Ложное «доставлено» (state=1 у недошедшей SMS — измерено) ни на что не влияет:
    # статус кода и жизненный цикл не меняются
    assert fresh.status == "sent"


# =======================
# 3. Тик воркера ничего не досылает: недоставленный голос доживает до expired
# =======================

@pytest.mark.asyncio
async def test_tick_never_resends_undelivered_voice(db, deps):
    user = await _mk_user(db)
    token = await _mk_token(db, user.id)
    otp = await _mk_otp(db, token.id, sent_at=NOW() - timedelta(seconds=90),
                        delivery_state="2")

    stats = await cabinet_service.otp_worker_tick()
    assert "fallbacks" not in stats, "обязанности фолбэка в тике больше нет"
    assert deps.gw.calls == [], "тик не отправляет ничего"
    assert (await _fetch_otp(db, otp.id)).status == "sent", "код живёт до TTL"

    # После TTL код штатно истекает — без единой отправки
    async with db.AsyncSessionLocal() as session:
        from sqlalchemy import update as sa_update
        await session.execute(sa_update(CabinetOtp).where(CabinetOtp.id == otp.id)
                              .values(expires_at=NOW() - timedelta(minutes=1)))
        await session.commit()
    stats = await cabinet_service.otp_worker_tick()
    assert stats["expired_codes"] == 1
    assert (await _fetch_otp(db, otp.id)).status == "expired"
    assert deps.gw.calls == []


# =======================
# 6. Колбэк: неизвестный external_id, чужой id_sms, незначимый state
# =======================

@pytest.mark.asyncio
async def test_unknown_external_id_200_no_changes(db, deps):
    await _mk_user(db)
    req = make_mocked_request(
        "GET", f"/sms/status-callback?external_id={uuid.uuid4()}&id_sms=x&state=2")
    resp = await cabinet_service.handle_status_callback(req)
    assert resp.status == 200
    assert deps.gw.calls == []
    async with db.AsyncSessionLocal() as session:
        assert (await session.execute(select(CabinetOtp))).scalars().all() == []


@pytest.mark.asyncio
async def test_foreign_id_sms_ignored(db, deps):
    user = await _mk_user(db)
    token = await _mk_token(db, user.id)
    otp = await _mk_otp(db, token.id, sent_at=NOW() - timedelta(seconds=60))

    # Верный external_id, но чужой id_sms → изменений нет
    outcome = await cabinet_service.process_status_callback(otp.external_id, "foreign-id", "2")
    assert outcome == "msg_id_mismatch"
    fresh = await _fetch_otp(db, otp.id)
    assert fresh.delivery_state is None
    assert deps.gw.calls == []


@pytest.mark.asyncio
async def test_insignificant_state_45_ignored_with_log(db, deps, caplog):
    user = await _mk_user(db)
    token = await _mk_token(db, user.id)
    otp = await _mk_otp(db, token.id)

    with caplog.at_level("INFO", logger="cabinet"):
        outcome = await cabinet_service.process_status_callback(otp.external_id, "v-1", "45")
    assert outcome == "ignored_state"
    assert any("state=45" in rec.getMessage() for rec in caplog.records), \
        "игнор незначимого state обязан логироваться"
    fresh = await _fetch_otp(db, otp.id)
    assert fresh.delivery_state is None
    assert deps.gw.calls == []


# =======================
# 7. Breaker: классификация отказов
# =======================

def test_five_subscriber_failures_do_not_open_breaker(deps):
    t0 = 1_000_000.0
    for i in range(5):
        cabinet_service.breaker_record_failure("subscriber", now=t0 + i)
    assert cabinet_service.breaker_state(now=t0 + 10) == "closed"
    assert cabinet_service.breaker_allows(now=t0 + 10) is True


def test_five_transport_failures_open_breaker(deps):
    t0 = 1_000_000.0
    for i in range(5):
        cabinet_service.breaker_record_failure("transport", now=t0 + i)
    assert cabinet_service.breaker_state(now=t0 + 10) == "open"
    assert cabinet_service.breaker_allows(now=t0 + 10) is False


# =======================
# 8. Breaker: half-open — ровно одна пробная отправка
# =======================

def test_half_open_probe_success_closes_breaker(deps):
    t0 = 1_000_000.0
    for i in range(5):
        cabinet_service.breaker_record_failure("transport", now=t0 + i)
    cooldown = 5 * 60
    t1 = t0 + 4 + cooldown + 1  # breaker открыт 5-м отказом (t0+4)
    assert cabinet_service.breaker_state(now=t1) == "half_open"
    assert cabinet_service.breaker_allows(now=t1) is True, "первая пробная отправка разрешена"
    assert cabinet_service.breaker_allows(now=t1 + 1) is False, "вторая проба запрещена"
    cabinet_service.breaker_record_success()
    assert cabinet_service.breaker_state(now=t1 + 2) == "closed"
    assert cabinet_service.breaker_allows(now=t1 + 2) is True


def test_half_open_probe_failure_returns_to_pause(deps):
    t0 = 1_000_000.0
    for i in range(5):
        cabinet_service.breaker_record_failure("transport", now=t0 + i)
    cooldown = 5 * 60
    t1 = t0 + 4 + cooldown + 1  # breaker открыт 5-м отказом (t0+4)
    assert cabinet_service.breaker_allows(now=t1) is True   # проба
    cabinet_service.breaker_record_failure("transport", now=t1 + 1)  # проба провалилась
    assert cabinet_service.breaker_state(now=t1 + 2) == "open", "неудача пробы возвращает паузу"
    assert cabinet_service.breaker_allows(now=t1 + 2) is False
    # После нового охлаждения — снова одна проба
    t2 = t1 + 1 + cooldown + 1
    assert cabinet_service.breaker_allows(now=t2) is True


# =======================
# 9. AE15: баланс/самопроверка в sync_worker-функции
# =======================

@pytest.mark.asyncio
async def test_ae15_balance_627_alerts_with_ip_and_resolve(db, deps):
    deps.gw.balance_result = BalanceResult(ok=False, err_code="627",
                                           error_class="service", description="Access denied")
    await cabinet_service.check_balance()
    assert len(deps.tech_alerts) == 1
    alert = deps.tech_alerts[0]
    assert "627" in alert
    # [Поправка U4 2026-09-08] внешний адрес изнутри не определить (два
    # исходящих канала Dockhost) — алерт объясняет это, а не показывает
    # адрес с чужой дороги
    assert "Внешний адрес на пути к ProstoSMS изнутри не определить" in alert
    assert "Внутренний IP контейнера" in alert, "внутренний IP помечен явно (R33/U4)"
    assert "Резолв" in alert, "алерт обязан содержать резолв ssl.bs00.ru (R33)"
    assert "Контекст: get_balance" in alert, "алерт называет операцию (U4)"


@pytest.mark.asyncio
async def test_low_balance_alert_and_token_expiry(db, deps):
    user = await _mk_user(db)
    stale = await _mk_token(db, user.id, expires_at=NOW() - timedelta(hours=1))
    user2 = await _mk_user(db, "2")
    alive = await _mk_token(db, user2.id, expires_at=NOW() + timedelta(days=1))

    deps.gw.balance_result = BalanceResult(ok=True, credits="123.45")
    result = await cabinet_service.sync_worker_duties()

    assert result["tokens_expired"] == 1
    assert (await _fetch_token(db, stale.id)).status == "expired"
    assert (await _fetch_token(db, alive.id)).status == "active"
    assert len(deps.tech_alerts) == 1 and "ниже порога" in deps.tech_alerts[0]


# =======================
# 10. Сигнал A: однократно через 15 минут; verified отменяет
# =======================

@pytest.mark.asyncio
async def test_signal_a_sent_once_after_15_minutes(db, deps):
    user = await _mk_user(db, full_name="Иван", channel="telegram")
    token = await _mk_token(db, user.id, deal_id="D-77")
    async with db.AsyncSessionLocal() as session:
        session.add(CabinetProfile(user_id=user.id, dealer_code="01",
                                   brand_model="Geely Monjaro"))
        await session.commit()
    # Код запрошен 16 минут назад и уже истёк (не in-flight — фолбэк не мешает)
    await _mk_otp(db, token.id, status="expired",
                  sent_at=NOW() - timedelta(minutes=16),
                  expires_at=NOW() - timedelta(minutes=11))

    stats = await cabinet_service.otp_worker_tick()
    assert stats["signal_a"] == 1
    assert len(deps.dealer_alerts) == 1
    dealer_code, text = deps.dealer_alerts[0]
    assert dealer_code == "01"
    assert text == ("⚠️ Личный кабинет: Иван (telegram), сделка D-77 (Geely Monjaro) — "
                    "запросил код, но не подтвердил номер. Заявка в CRM не передана. "
                    "Продолжите в чате вручную."), "текст сигнала A — строго по R26"

    # Однократность: второй тик не шлёт повторно
    stats = await cabinet_service.otp_worker_tick()
    assert stats["signal_a"] == 0
    assert len(deps.dealer_alerts) == 1
    assert len(await _events(db, "signal_a_sent")) == 1


@pytest.mark.asyncio
async def test_signal_a_cancelled_by_verification(db, deps):
    user = await _mk_user(db)
    token = await _mk_token(db, user.id, bound_at=NOW())  # верификация прошла
    await _mk_otp(db, token.id, status="verified",
                  sent_at=NOW() - timedelta(minutes=20),
                  expires_at=NOW() - timedelta(minutes=15))

    stats = await cabinet_service.otp_worker_tick()
    assert stats["signal_a"] == 0
    assert deps.dealer_alerts == [], "верификация отменяет сигнал A"


@pytest.mark.asyncio
async def test_signal_a_not_sent_before_15_minutes(db, deps):
    user = await _mk_user(db)
    token = await _mk_token(db, user.id)
    await _mk_otp(db, token.id, status="expired",
                  sent_at=NOW() - timedelta(minutes=5),
                  expires_at=NOW() - timedelta(minutes=1))

    stats = await cabinet_service.otp_worker_tick()
    assert stats["signal_a"] == 0
    assert deps.dealer_alerts == []


# =======================
# 11. Экспирация кодов в тике
# =======================

@pytest.mark.asyncio
async def test_tick_expires_stale_codes(db, deps):
    user = await _mk_user(db)
    token = await _mk_token(db, user.id)
    stale = await _mk_otp(db, token.id, expires_at=NOW() - timedelta(minutes=1),
                          sent_at=NOW() - timedelta(minutes=6))
    stats = await cabinet_service.otp_worker_tick()
    assert stats["expired_codes"] == 1
    assert (await _fetch_otp(db, stale.id)).status == "expired"
    assert deps.gw.calls == [], "истёкший код не досылается"


# =======================
# 13. Изоляция обязанностей тика: падение одной не отменяет остальные
# =======================

@pytest.mark.asyncio
async def test_tick_duties_isolated_early_failure_does_not_block_handoff(
        db, deps, monkeypatch):
    """Раньше исключение ранней обязанности прерывало тик до передачи заявок,
    а обёртка main.py лишь повторяла тот же падающий проход с бэкоффом."""
    now = NOW()
    user = await _mk_user(db)
    token = await _mk_token(db, user.id, bound_at=now - timedelta(minutes=20),
                            bound_phone="+79001234567",
                            last_seen_at=now - timedelta(minutes=11))
    await _mk_otp(db, token.id, expires_at=now - timedelta(minutes=1),
                  sent_at=now - timedelta(minutes=6))

    async def boom(_now):
        raise RuntimeError("сессия БД отвалилась")

    monkeypatch.setattr(cabinet_service, "_expire_otp_codes", boom)

    stats = await cabinet_service.otp_worker_tick(now)
    assert stats["expired_codes"] is None and stats["failed"] == ["expired_codes"], \
        "упавшая обязанность видна в статистике"
    assert stats["signal_a"] is not None, "следующие обязанности выполнены"
    assert stats["handoffs"] == 1, "передача заявок отработала несмотря на сбой"
    assert len(deps.conversions) == 1


# =======================
# 14. Source-grep: wiring U3 закреплён в main.py
# =======================

class TestSourceWiring:
    def test_status_callback_route_registered_get_and_post(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "add_get('/sms/status-callback', handle_sms_status_callback)" in src
        assert "add_post('/sms/status-callback', handle_sms_status_callback)" in src
        assert "cabinet_service.handle_status_callback(request)" in src, \
            "роут обязан быть тонкой обёрткой над cabinet_service"

    def test_setup_injects_dependencies(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "cabinet_service.setup(" in src
        assert "skip_debounce=True" in src, "enqueue_nodebounce обязан обходить дебаунс (R35)"
        # U10: конверсия передаётся обёрткой со снапшотом контекста из БД
        # (cabinet_service.build_conversion_snapshot; main.py — тонкая проводка)
        assert "run_conversion=_cabinet_run_conversion" in src
        assert "cabinet_service.build_conversion_snapshot(" in src
        assert "BitrixService.final_sync_wrapper(platform_uid" in src
        assert "dealer_alert=send_dealer_alert" in src
        assert "dealers_provider=lambda: CACHED_DEALERS" in src

    def test_otp_worker_started_and_thin(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "asyncio.create_task(otp_worker())" in src
        assert "cabinet_service.otp_worker_tick()" in src
        assert "CABINET_OTP_WORKER_INTERVAL" in src

    def test_sync_worker_calls_cabinet_duties(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "cabinet_service.sync_worker_duties()" in src
