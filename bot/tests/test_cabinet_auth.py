"""U4: auth-API кабинета — токены, коды, сессии, лимиты, CORS
(R1–R6, R13, R19–R22, R27, R36–R38; AE1–AE3, AE7, AE8/AE13)."""
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select, update

from database import CabinetEvent, CabinetOtp, CabinetProfile, CabinetSession, CabinetToken, Consent, User

import cabinet_api
import cabinet_service
from cabinet_service import ApiError
from sms_gateway import SendResult

import main as main_mod

from conftest import CONSENT, PHONE

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"

DEALERS = {
    "01": {"dealer_name": "Тест Авто", "website": "https://dealer-one.ru/", "code": "01",
           # П.3: тексты согласия (Policy/Agreement из NocoDB) — простой текст с переносами
           "policy_text": "Политика конфиденциальности.\nВторая строка.",
           "agreement_text": "Согласие на обработку."},
    "02": {"dealer_name": "Второй Авто", "website": "https://dealer-two.ru/lp?utm=1", "code": "02",
           # В3: пробельное значение эквивалентно отсутствию; agreement_text не задан вовсе
           "policy_text": "  \n "},
}


# =======================
# Фикстуры (общие db/deps — в conftest.py; здесь локальные отличия U4)
# =======================

@pytest.fixture
def dealers():
    return DEALERS


@pytest.fixture
def deps(deps, monkeypatch):
    """Общий deps + конвенция U4: bitrix_request всегда отвечает пустым dict,
    коды детерминированы."""
    deps.bitrix.fail = True

    # Детерминированные коды: 0001, 0002, ... (неверный ввод в тестах — "9999")
    counter = {"n": 0}

    def fake_code():
        counter["n"] += 1
        return f"{counter['n']:04d}"

    monkeypatch.setattr(cabinet_service, "_generate_code", fake_code)
    return deps


@pytest_asyncio.fixture
async def client(db, deps, monkeypatch):
    """aiohttp test client с БОЕВЫМИ middleware из main.py (CORS-интеграция)."""
    monkeypatch.setattr(main_mod, "TECH_ADMIN_ID", "")
    app = web.Application(middlewares=[main_mod.error_middleware, main_mod.cors_middleware])
    cabinet_api.register_routes(app)

    async def catalog_stub(request):
        return web.json_response({"list": []})

    async def boom(request):
        raise RuntimeError("искусственный 500")

    app.router.add_get('/catalog/{dealer_code}', catalog_stub)
    app.router.add_get('/cabinet/api/_boom', boom)

    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    yield test_client
    await test_client.close()


# Фикстура aware_db (SQLite с timezone-aware временем, как боевой PostgreSQL)
# перенесена в conftest.py — общая для auth- и form-тестов (U1, R20).


# =======================
# Хелперы
# =======================

async def _mk_user(db, suffix="1", phone=None):
    async with db.AsyncSessionLocal() as session:
        user = User(platform_user_id=f"u-{suffix}", channel="max",
                    full_name="Иван", phone=phone)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _issue(user_id, dealer="01", deal="D-1"):
    url = await cabinet_service.issue_cabinet_token(user_id, dealer, deal)
    assert url is not None
    return url, url.split("#t=", 1)[1]


async def _backdate_sends(db, seconds=61):
    """Старит все записи кодов на `seconds` назад: sent_at и момент создания
    (created-колонки нет, создание восстанавливается как expires_at − OTP_TTL,
    поэтому старится и expires_at). seconds=61 снимает кулдаун (30 с), не
    выводя записи из часового/суточных окон; seconds>600 снимает и голосовое
    окно номера (VOICE_PHONE_WINDOW), но делает код истёкшим (TTL 5 мин)."""
    created = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    async with db.AsyncSessionLocal() as session:
        await session.execute(
            update(CabinetOtp).where(CabinetOtp.sent_at.is_not(None))
            .values(sent_at=created)
        )
        await session.execute(
            update(CabinetOtp).values(expires_at=created + cabinet_service.OTP_TTL)
        )
        await session.commit()


async def _get_token_row(db, token_id):
    async with db.AsyncSessionLocal() as session:
        return await session.get(CabinetToken, token_id)


async def _token_row_by_hash_of(db, token_plain):
    import hashlib
    h = hashlib.sha256(token_plain.encode()).hexdigest()
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(
            select(CabinetToken).where(CabinetToken.token_hash == h)
        )).scalar_one()


async def _bind(db, token_plain, gw):
    """Полный цикл: request-code → verify фактически отправленным кодом."""
    await cabinet_service.request_code(token_plain, phone=PHONE, consent=CONSENT)
    code = gw.calls[-1]["code"]
    return await cabinet_service.verify_code(token_plain, code)


def _api_error(exc_info):
    return exc_info.value


# =======================
# Выдача токена (AE7, R19)
# =======================

@pytest.mark.asyncio
async def test_ae7_same_bundle_same_url_other_deal_or_dealer_revokes(db, deps):
    user = await _mk_user(db)
    url1, _ = await _issue(user.id, "01", "D-1")
    url2, _ = await _issue(user.id, "01", "D-1")
    assert url1 == url2, "R19: активный токен той же связки → тот же URL"
    assert url1.startswith("https://dealer-one.ru/cabinet#t=")

    # Другая сделка → старый revoked, новый выдан
    url3, _ = await _issue(user.id, "01", "D-2")
    assert url3 != url1
    async with db.AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == user.id)
        )).scalars().all()
    statuses = sorted(t.status for t in rows)
    assert statuses == ["active", "revoked"]

    # Другой дилер → снова revoke, URL от Website_URL нового дилера
    url4, _ = await _issue(user.id, "02", "D-2")
    assert url4.startswith("https://dealer-two.ru/cabinet#t=")
    async with db.AsyncSessionLocal() as session:
        active = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == user.id,
                                       CabinetToken.status == "active")
        )).scalars().all()
    assert len(active) == 1 and active[0].dealer_code == "02"


@pytest.mark.asyncio
async def test_issue_idempotent_across_turns_single_row_same_hash(db, deps):
    """Инцидент дев-стенда 2026-08-12 (регрессия идемпотентности): несколько
    последовательных ходов диалога по одной связке обязаны давать РОВНО ОДНУ
    строку cabinet_tokens — тот же token_hash, статус active, bound_phone
    сохранён. Проверка по фактическому содержимому таблицы, не по URL."""
    user = await _mk_user(db, "idem")
    r1 = await cabinet_service.prepare_cabinet_context(user.id, "01", deal_id="D-7")
    r2 = await cabinet_service.prepare_cabinet_context(user.id, "01", deal_id="D-7")
    assert r1["cabinet_url"] == r2["cabinet_url"] is not None

    async with db.AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == user.id)
        )).scalars().all()
    assert len(rows) == 1 and rows[0].status == "active", \
        "повторный ход создал новый токен вместо возврата того же URL"
    hash_before = rows[0].token_hash

    # Верификация фиксирует номер (TOFU); следующий ход не должен его терять
    token_plain = r1["cabinet_url"].split("#t=", 1)[1]
    await _bind(db, token_plain, deps.gw)
    r3 = await cabinet_service.prepare_cabinet_context(user.id, "01", deal_id="D-7")
    assert r3["cabinet_url"] == r1["cabinet_url"]

    async with db.AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == user.id)
        )).scalars().all()
    assert len(rows) == 1 and rows[0].status == "active"
    assert rows[0].token_hash == hash_before
    assert rows[0].bound_phone == PHONE, \
        "ход диалога после верификации потерял подтверждённый номер (TOFU)"


@pytest.mark.asyncio
async def test_hmac_key_mismatch_keeps_token_and_bound_phone(db, deps, monkeypatch):
    """URL той же связки невоспроизводим текущим CABINET_HMAC_KEY (ротация
    ключа или разные ключи у процессов — инцидент дев-стенда 2026-08-12:
    скрипт выдачи со своим дев-ключом и бот взаимно отзывали токены).
    Отзыв — только по исключениям R19 (другая сделка/дилер/склейка):
    здесь токен ОБЯЗАН сохраниться вместе с bound_phone, ссылка на этот
    ход не выдаётся, а ранее отправленная продолжает работать."""
    user = await _mk_user(db, "keymm")
    url_old, token_old = await _issue(user.id, "01", "D-key")
    await _bind(db, token_old, deps.gw)

    monkeypatch.setenv("CABINET_HMAC_KEY", "rotated-key")
    url_new = await cabinet_service.issue_cabinet_token(user.id, "01", "D-key")
    assert url_new is None, "на несовпадении ключа выпущен новый токен"

    async with db.AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == user.id)
        )).scalars().all()
    assert len(rows) == 1 and rows[0].status == "active", \
        "токен отозван вне исключений R19 (другая сделка/дилер/склейка)"
    assert rows[0].bound_phone == PHONE, "потерян подтверждённый номер (TOFU)"

    # Ранее отправленная ссылка работает: /auth/init сверяет sha256 самого
    # токена, ключ HMAC в проверке не участвует
    assert (await cabinet_service.auth_init(token_old))["state"] == "bound"

    # Оператор уведомлён (ротация/расхождение ключей — операционное событие)
    assert any("CABINET_HMAC_KEY" in msg for msg in deps.tech_alerts), \
        "нет техалерта о невоспроизводимом токене"


# =======================
# init (R5/AE3, R27)
# =======================

@pytest.mark.asyncio
async def test_init_need_phone_and_bound_states(db, deps):
    user = await _mk_user(db)
    _, token = await _issue(user.id)
    data = await cabinet_service.auth_init(token)
    assert data["state"] == "need_phone"
    assert data["dealer"]["name"] == "Тест Авто"

    await _bind(db, token, deps.gw)
    data = await cabinet_service.auth_init(token)
    assert data["state"] == "bound"
    assert data["masked_phone"] == "+7" + "•" * 7 + "567"


@pytest.mark.asyncio
async def test_init_dealer_consent_texts_in_both_states(db, deps):
    """П.3 (2026-08-31): Policy/Agreement дилера приходят в dealer-части init
    в обеих ветках ответа (need_phone и bound) — экран согласия строит по ним
    кликабельные фразы."""
    user = await _mk_user(db)
    _, token = await _issue(user.id)  # дилер 01 — оба текста заданы
    data = await cabinet_service.auth_init(token)
    assert data["state"] == "need_phone"
    # Ускорение A6 (2026-10-03): в init — признаки наличия и код дилера, без текстов
    assert data["dealer"]["policy_available"] is True and data["dealer"]["agreement_available"] is True
    assert data["dealer"]["code"] == "01"
    assert "policy_text" not in data["dealer"] and "agreement_text" not in data["dealer"]

    await _bind(db, token, deps.gw)
    data = await cabinet_service.auth_init(token)
    assert data["state"] == "bound"
    assert data["dealer"]["policy_available"] is True and data["dealer"]["agreement_available"] is True


@pytest.mark.asyncio
async def test_init_dealer_consent_texts_blank_or_missing_are_none(db, deps):
    """В3: пустое/пробельное/отсутствующее поле → None (фраза без ссылки);
    поля независимы: у дилера 02 policy пробельный, agreement не задан."""
    user = await _mk_user(db)
    _, token = await _issue(user.id, dealer="02")
    data = await cabinet_service.auth_init(token)
    assert data["dealer"]["policy_available"] is False
    assert data["dealer"]["agreement_available"] is False


def test_legal_text_endpoint_service(deps):
    data = cabinet_service.dealer_legal_text("01", "policy")
    assert data == {"dealer_code": "01", "kind": "policy",
                    "text": "Политика конфиденциальности.\nВторая строка."}
    assert cabinet_service.dealer_legal_text("01", "agreement")["text"] == "Согласие на обработку."
    for code, kind, status in (("02", "policy", 404), ("02", "agreement", 404),
                               ("77", "policy", 404), ("01", "terms", 422), ("1", "policy", 422)):
        with pytest.raises(cabinet_service.ApiError) as exc:
            cabinet_service.dealer_legal_text(code, kind)
        assert exc.value.status == status, (code, kind)


def test_main_projects_policy_agreement_into_dealers_cache():
    """Проекция CACHED_DEALERS в main.py — явная: без этих ключей новые колонки
    NocoDB до кабинета не доедут (кэш строится перечислением полей)."""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert '"policy_text": row.get(\'Policy\')' in src, \
        "main.py не проецирует колонку Policy в кэш дилеров"
    assert '"agreement_text": row.get(\'Agreement\')' in src, \
        "main.py не проецирует колонку Agreement в кэш дилеров"


@pytest.mark.asyncio
async def test_init_known_phone_offers_mask_and_use_known(db, deps):
    user = await _mk_user(db, "known", phone="8 (900) 123-45-67")
    _, token = await _issue(user.id)
    data = await cabinet_service.auth_init(token)
    assert data["state"] == "need_phone"
    assert data["known_phone_masked"] == "+7" + "•" * 7 + "567"
    assert data["use_known"] is True


@pytest.mark.asyncio
async def test_ae3_dead_tokens_identical_and_no_db_writes(db, deps, client):
    """Несуществующий, отозванный и истёкший токен → побайтно идентичный
    link_expired; записи в БД нет."""
    user = await _mk_user(db)
    revoked_plain, expired_plain = "revoked-plain-token", "expired-plain-token"
    import hashlib as _hl
    async with db.AsyncSessionLocal() as session:
        session.add(CabinetToken(user_id=user.id, dealer_code="01", deal_id="D-1",
                                 token_hash=_hl.sha256(revoked_plain.encode()).hexdigest(),
                                 status="revoked"))
        session.add(CabinetToken(user_id=user.id, dealer_code="01", deal_id="D-2",
                                 token_hash=_hl.sha256(expired_plain.encode()).hexdigest(),
                                 status="active",
                                 expires_at=datetime.now(timezone.utc) - timedelta(hours=1)))
        await session.commit()

    bodies, statuses = [], []
    for tok in ("no-such-token", revoked_plain, expired_plain):
        resp = await client.post('/cabinet/api/auth/init', json={"token": tok})
        statuses.append(resp.status)
        bodies.append(await resp.read())
    assert statuses == [410, 410, 410]
    assert bodies[0] == bodies[1] == bodies[2], "ответы обязаны быть идентичны побайтно (R5)"

    async with db.AsyncSessionLocal() as session:
        for model in (CabinetEvent, CabinetOtp, CabinetSession, Consent):
            count = (await session.execute(
                select(func.count()).select_from(model))).scalar_one()
            assert count == 0, f"{model.__tablename__}: записи в БД быть не должно"
        tokens = (await session.execute(
            select(func.count()).select_from(CabinetToken))).scalar_one()
        assert tokens == 2


# =======================
# request-code: валидация, TOFU, согласие, лимиты
# =======================

@pytest.mark.asyncio
async def test_kazakh_number_rejected_before_gateway(db, deps):
    user = await _mk_user(db)
    _, token = await _issue(user.id)
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(token, phone="+77001234567", consent=CONSENT)
    assert e.value.status == 422 and e.value.error == "invalid_phone"
    assert deps.gw.calls == [], "шлюз не должен вызываться до валидации номера"


@pytest.mark.asyncio
async def test_consent_required_then_recorded_once(db, deps):
    user = await _mk_user(db)
    _, token = await _issue(user.id)
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(token, phone=PHONE)
    assert e.value.error == "consent_required" and deps.gw.calls == []

    assert (await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT))["status"] == "ok"
    await _backdate_sends(db, seconds=601)  # снимает кулдаун и голосовое окно номера
    # Повторный запрос без consent проходит — запись уже есть (R13)
    assert (await cabinet_service.request_code(token, phone=PHONE))["status"] == "ok"
    async with db.AsyncSessionLocal() as session:
        count = (await session.execute(select(func.count()).select_from(Consent))).scalar_one()
    assert count == 1, "INSERT в consents — только при отсутствии записи по токену"


@pytest.mark.asyncio
async def test_ae1_bound_token_foreign_number_mismatch_no_send(db, deps):
    user = await _mk_user(db)
    _, token = await _issue(user.id)
    await _bind(db, token, deps.gw)
    sends_before = len(deps.gw.calls)

    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(token, phone="+79995556677")
    assert e.value.status == 409 and e.value.error == "phone_mismatch"
    assert len(deps.gw.calls) == sends_before, "отправки нет"

    row = await _token_row_by_hash_of(db, token)
    assert (row.failed_attempts or 0) == 0, "счётчики целы"


@pytest.mark.asyncio
async def test_cooldown_and_manual_resend_supersedes(db, deps):
    """AE8/AE13: два ручных кода → прежний superseded, проходит только последний."""
    user = await _mk_user(db)
    _, token = await _issue(user.id)
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    code1 = deps.gw.calls[-1]["code"]

    # Кулдаун 30 с на токене
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(token, phone=PHONE)
    assert e.value.status == 429 and e.value.error == "rate_limited"
    assert e.value.extra["retry_after"] >= 1

    await _backdate_sends(db, seconds=601)  # снимает и голосовое окно номера (O1)
    await cabinet_service.request_code(token, phone=PHONE)
    code2 = deps.gw.calls[-1]["code"]
    assert code1 != code2

    async with db.AsyncSessionLocal() as session:
        statuses = sorted((await session.execute(select(CabinetOtp.status))).scalars())
    assert statuses == ["sent", "superseded"]

    # Первый код погашен: отдельная ошибка БЕЗ траты попытки (R8, O8),
    # последний проходит
    with pytest.raises(ApiError) as e:
        await cabinet_service.verify_code(token, code1)
    assert e.value.status == 400 and e.value.error == "code_superseded"
    row = await _token_row_by_hash_of(db, token)
    assert (row.failed_attempts or 0) == 0, "попытка не потрачена"
    result = await cabinet_service.verify_code(token, code2)
    assert "access_token" in result


@pytest.mark.asyncio
async def test_hourly_limit_five_per_token(db, deps):
    """5/час на токен; номера разные, чтобы не задевать голосовое окно (O1)."""
    user = await _mk_user(db)
    _, token = await _issue(user.id)
    for i in range(5):
        await cabinet_service.request_code(token, phone=f"+7900123456{i}", consent=CONSENT)
        await _backdate_sends(db, seconds=61)  # снимаем только кулдаун, не часовое окно
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(token, phone="+79001234569")
    assert e.value.error == "rate_limited" and e.value.extra["retry_after"] > 60
    assert e.value.extra["limit_source"] == "link"


@pytest.mark.asyncio
async def test_dealer_daily_cap(db, deps, monkeypatch):
    monkeypatch.setenv("CABINET_DEALER_DAILY_CAP", "2")
    u1 = await _mk_user(db, "c1")
    u2 = await _mk_user(db, "c2")
    u3 = await _mk_user(db, "c3")
    for i, u in enumerate((u1, u2)):
        _, tok = await _issue(u.id, "01", f"D-{u.id}")
        await cabinet_service.request_code(tok, phone=f"+7900123457{i}", consent=CONSENT)
    _, tok3 = await _issue(u3.id, "01", f"D-{u3.id}")
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(tok3, phone="+79001234572", consent=CONSENT)
    assert e.value.error == "rate_limited"
    assert e.value.extra["limit_source"] == "dealer"
    assert e.value.extra["retry_after"] > 3600, "честный остаток суточного окна"
    assert any("кап" in t for t in deps.tech_alerts)

    # Кап дилера '01' не задевает дилера '02'
    _, tok_other = await _issue(u3.id, "02", "D-x")
    assert (await cabinet_service.request_code(tok_other, phone="+79001234573", consent=CONSENT))["status"] == "ok"


@pytest.mark.asyncio
async def test_dealer_cap_zero_is_kill_switch_not_crash(db, deps, monkeypatch):
    """Код-ревью: кап 0 (выключение отправок конфигом) — 429 с окном целиком,
    а не TypeError на пустом min(sent_at)."""
    monkeypatch.setenv("CABINET_DEALER_DAILY_CAP", "0")
    user = await _mk_user(db, "ks")
    _, token = await _issue(user.id)
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    assert e.value.error == "rate_limited"
    assert e.value.extra["limit_source"] == "dealer"
    assert e.value.extra["retry_after"] == 86400


@pytest.mark.asyncio
async def test_dealer_cap_alert_deduplicated_while_429_stays(db, deps, monkeypatch):
    """После исчерпания капа 429 отдаётся КАЖДОМУ запросу, а техалерт — один
    на дилера в час: иначе любой держатель ссылки заливает админа алертами."""
    monkeypatch.setenv("CABINET_DEALER_DAILY_CAP", "1")
    u1 = await _mk_user(db, "cap-a")
    _, tok1 = await _issue(u1.id, "01", "D-cap-a")
    await cabinet_service.request_code(tok1, phone="+79001111111", consent=CONSENT)  # кап выбран

    u2 = await _mk_user(db, "cap-b")
    _, tok2 = await _issue(u2.id, "01", "D-cap-b")
    for _ in range(10):
        with pytest.raises(ApiError) as e:
            await cabinet_service.request_code(tok2, phone="+79002222222", consent=CONSENT)
        assert e.value.status == 429 and e.value.error == "rate_limited"

    cap_alerts = [t for t in deps.tech_alerts if "кап" in t]
    assert len(cap_alerts) == 1, f"техалерт о капе не дедуплицирован: {len(cap_alerts)}"

    # Другой дилер дедупом первого не заглушён: дедуп — по dealer_code
    u3 = await _mk_user(db, "cap-c")
    _, tok3 = await _issue(u3.id, "02", "D-cap-c")
    await cabinet_service.request_code(tok3, phone="+79003333333", consent=CONSENT)  # кап '02' выбран
    u4 = await _mk_user(db, "cap-d")
    _, tok4 = await _issue(u4.id, "02", "D-cap-d")
    with pytest.raises(ApiError):
        await cabinet_service.request_code(tok4, phone="+79004444444", consent=CONSENT)
    assert len([t for t in deps.tech_alerts if "кап" in t]) == 2

    # Окно истекло — алерт по тому же дилеру снова разрешён
    cabinet_service.reset_dealer_cap_alerts()
    with pytest.raises(ApiError):
        await cabinet_service.request_code(tok2, phone="+79005555555", consent=CONSENT)
    assert len([t for t in deps.tech_alerts if "кап" in t]) == 3


@pytest.mark.asyncio
async def test_breaker_pause_returns_retry_free_without_otp(db, deps):
    user = await _mk_user(db)
    _, token = await _issue(user.id)
    for _ in range(5):
        cabinet_service.breaker_record_failure("transport")
    assert cabinet_service.breaker_state() == "open"
    data = await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    assert data["status"] == "retry_free"
    assert deps.gw.calls == []
    async with db.AsyncSessionLocal() as session:
        count = (await session.execute(select(func.count()).select_from(CabinetOtp))).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_failed_send_is_retry_free(db, deps):
    """Неуспех отправки не расходует SMS-лимиты и кулдаун (R11): статус
    failed_send, наружу retry_free. Исключение (O1): непринятый запрос ЗВОНКА
    всё равно занимает голосовое окно номера — провайдерский триггер подмены
    взводится независимо от принятости запроса."""
    user = await _mk_user(db)
    _, token = await _issue(user.id)
    deps.gw.results.append(SendResult(ok=False, error_class="service", retry_free=True,
                                      err_code="623", description="нет средств"))
    data = await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    assert data["status"] == "retry_free"
    assert (await cabinet_service.send_status(token))["state"] == "retry_free"
    # Немедленный повтор ЗВОНКОМ закрыт голосовым окном (O1: запрос звонка был)
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(token, phone=PHONE)
    assert e.value.error == "rate_limited" and e.value.extra["limit_source"] == "phone"
    # SMS-каналу неуспех звонка не мешает: попытка не зачтена, кулдауна нет
    assert (await cabinet_service.request_code(token, phone=PHONE, channel="sms"))["status"] == "ok"


@pytest.mark.asyncio
async def test_failed_sms_does_not_consume_sms_limits(db, deps):
    """Неуспешная SMS бесплатна: ни кулдаун, ни лимиты ссылки/номера не тратятся."""
    user = await _mk_user(db, "fs")
    _, token = await _issue(user.id)
    deps.gw.results.append(SendResult(ok=False, error_class="service", retry_free=True,
                                      err_code="623", description="нет средств"))
    data = await cabinet_service.request_code(token, phone=PHONE, channel="sms", consent=CONSENT)
    assert data["status"] == "retry_free"
    # Немедленный повтор SMS разрешён — неуспех не занял ни одно окно
    assert (await cabinet_service.request_code(token, phone=PHONE, channel="sms"))["status"] == "ok"


# =======================
# Лимиты по номеру и по ссылке (R4/R5, решения O1/O2)
# =======================

@pytest.mark.asyncio
async def test_voice_window_blocks_across_tokens_and_clears(db, deps):
    """R4/O1: один запрос звонка на номер за 10 минут — лимит именно по номеру,
    держит и другие токены; другой номер свободен; спустя окно — проходит."""
    u1 = await _mk_user(db, "v1")
    _, tok1 = await _issue(u1.id)
    await cabinet_service.request_code(tok1, phone=PHONE, consent=CONSENT)

    u2 = await _mk_user(db, "v2")
    _, tok2 = await _issue(u2.id, "01", "D-v2")
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(tok2, phone=PHONE, consent=CONSENT)
    assert e.value.status == 429 and e.value.error == "rate_limited"
    assert e.value.extra["limit_source"] == "phone"

    assert (await cabinet_service.request_code(
        tok2, phone="+79007654321", consent=CONSENT))["status"] == "ok"

    await _backdate_sends(db, seconds=601)  # оба запроса старше окна
    assert (await cabinet_service.request_code(tok1, phone=PHONE))["status"] == "ok"


@pytest.mark.asyncio
async def test_voice_window_retry_after_honest(db, deps):
    """429 голосового окна несёт честный остаток окна (≈420 с при запросе
    трёхминутной давности), не дефолт часа."""
    user = await _mk_user(db, "vr")
    _, token = await _issue(user.id)
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    await _backdate_sends(db, seconds=180)
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(token, phone=PHONE)
    ra = e.value.extra["retry_after"]
    assert 380 <= ra <= 421, f"ожидался остаток ≈420 с, получено {ra}"
    assert e.value.extra["limit_source"] == "phone"


@pytest.mark.asyncio
async def test_sms_link_daily_limit_two(db, deps):
    """R5/O2: не более двух принятых SMS на ссылку за сутки; retry_after —
    остаток суточного окна; тот же номер с другой ссылки пока проходит."""
    user = await _mk_user(db, "sl")
    _, token = await _issue(user.id)
    for _ in range(2):
        assert (await cabinet_service.request_code(
            token, phone=PHONE, channel="sms", consent=CONSENT))["status"] == "ok"
        await _backdate_sends(db, seconds=31)
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(token, phone=PHONE, channel="sms")
    assert e.value.extra["limit_source"] == "link"
    assert e.value.extra["retry_after"] > 3600, "остаток суточного окна, не часовой дефолт"

    u2 = await _mk_user(db, "sl2")
    _, tok2 = await _issue(u2.id, "01", "D-sl2")
    assert (await cabinet_service.request_code(
        tok2, phone=PHONE, channel="sms", consent=CONSENT))["status"] == "ok"


@pytest.mark.asyncio
async def test_sms_phone_daily_limit_five_across_tokens(db, deps):
    """R5: шестая принятая SMS на номер за сутки — 429 phone (с любых ссылок);
    запись старше суток из окна выпадает."""
    toks = []
    for i, n_sms in ((0, 2), (1, 2), (2, 1)):
        u = await _mk_user(db, f"sp{i}")
        _, tok = await _issue(u.id, "01", f"D-sp{i}")
        toks.append(tok)
        for _ in range(n_sms):
            assert (await cabinet_service.request_code(
                tok, phone=PHONE, channel="sms", consent=CONSENT))["status"] == "ok"
            await _backdate_sends(db, seconds=31)

    # На ссылке toks[2] лишь одна SMS — держит именно лимит номера (5/сутки)
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(toks[2], phone=PHONE, channel="sms")
    assert e.value.extra["limit_source"] == "phone"

    # Старейшая запись уезжает за сутки — та же отправка проходит
    async with db.AsyncSessionLocal() as session:
        row_id = (await session.execute(
            select(CabinetOtp.id).where(CabinetOtp.sent_at.is_not(None))
            .order_by(CabinetOtp.id).limit(1))).scalar_one()
        await session.execute(update(CabinetOtp).where(CabinetOtp.id == row_id)
                              .values(sent_at=datetime.now(timezone.utc) - timedelta(hours=25)))
        await session.commit()
    assert (await cabinet_service.request_code(
        toks[2], phone=PHONE, channel="sms"))["status"] == "ok"


@pytest.mark.asyncio
async def test_voice_and_sms_limits_independent(db, deps):
    """Голосовое окно не мешает SMS на тот же номер, и наоборот."""
    user = await _mk_user(db, "vi")
    _, token = await _issue(user.id)
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)  # звонок
    await _backdate_sends(db, seconds=31)  # только кулдаун; голосовое окно ещё занято
    assert (await cabinet_service.request_code(
        token, phone=PHONE, channel="sms"))["status"] == "ok"

    # И наоборот: две SMS на другом номере не мешают звонку на него
    u2 = await _mk_user(db, "vi2")
    _, tok2 = await _issue(u2.id, "01", "D-vi2")
    phone2 = "+79008881122"
    for _ in range(2):
        assert (await cabinet_service.request_code(
            tok2, phone=phone2, channel="sms", consent=CONSENT))["status"] == "ok"
        await _backdate_sends(db, seconds=31)
    assert (await cabinet_service.request_code(tok2, phone=phone2))["status"] == "ok"


@pytest.mark.asyncio
async def test_cooldown_thirty_seconds_boundary(db, deps):
    """R6: пауза 30 с — 29 с ещё рано (retry_after без limit_source), 31 с можно."""
    user = await _mk_user(db, "cd")
    _, token = await _issue(user.id)
    await cabinet_service.request_code(token, phone=PHONE, channel="sms", consent=CONSENT)
    await _backdate_sends(db, seconds=29)
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(token, phone=PHONE, channel="sms")
    assert e.value.error == "rate_limited" and 1 <= e.value.extra["retry_after"] <= 2
    assert "limit_source" not in e.value.extra, "кулдаун — без источника лимита"
    await _backdate_sends(db, seconds=31)
    assert (await cabinet_service.request_code(
        token, phone=PHONE, channel="sms"))["status"] == "ok"


@pytest.mark.asyncio
async def test_voice_window_on_aware_db(aware_db, deps):
    """R4 на aware-БД: восстановление момента создания из aware expires_at."""
    user = await _mk_user(aware_db, "tzv")
    _, token = await _issue(user.id)
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    await _backdate_sends(aware_db, seconds=31)  # кулдаун снят, окно номера занято
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(token, phone=PHONE)
    assert e.value.extra["limit_source"] == "phone"
    await _backdate_sends(aware_db, seconds=601)
    assert (await cabinet_service.request_code(token, phone=PHONE))["status"] == "ok"


# =======================
# verify: блокировка (AE2), инкремент без активного кода
# =======================

@pytest.mark.asyncio
async def test_ae2_five_wrong_attempts_lock_survives_resend(db, deps):
    user = await _mk_user(db)
    _, token = await _issue(user.id)
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)

    for expected_left in (4, 3, 2, 1):
        with pytest.raises(ApiError) as e:
            await cabinet_service.verify_code(token, "9999")
        assert e.value.error == "invalid_code"
        assert e.value.extra["attempts_left"] == expected_left

    await _backdate_sends(db, seconds=601)  # снимает кулдаун и голосовое окно
    await cabinet_service.request_code(token, phone=PHONE)  # resend НЕ сбрасывает счётчик (R6)

    with pytest.raises(ApiError) as e:
        await cabinet_service.verify_code(token, "9999")
    assert e.value.status == 423 and e.value.error == "locked"
    assert e.value.extra["retry_after"] == 30 * 60

    # request-code в блоке → тот же locked
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(token, phone=PHONE)
    assert e.value.status == 423 and e.value.error == "locked"

    # verify в блоке → тоже locked
    with pytest.raises(ApiError) as e:
        await cabinet_service.verify_code(token, deps.gw.calls[-1]["code"])
    assert e.value.error == "locked"


@pytest.mark.asyncio
async def test_verify_without_active_code_increments_counter(db, deps):
    user = await _mk_user(db)
    _, token = await _issue(user.id)
    with pytest.raises(ApiError) as e:
        await cabinet_service.verify_code(token, "1234")
    assert e.value.error == "invalid_code" and e.value.extra["attempts_left"] == 4
    row = await _token_row_by_hash_of(db, token)
    assert row.failed_attempts == 1


@pytest.mark.asyncio
async def test_expired_code_returns_code_expired_without_attempt(db, deps):
    """R8/O8: истёкший, но верно введённый код — code_expired, попытка цела."""
    user = await _mk_user(db)
    _, token = await _issue(user.id)
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    code = deps.gw.calls[-1]["code"]

    async with db.AsyncSessionLocal() as session:
        await session.execute(update(CabinetOtp).values(
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
        await session.commit()

    # sent с истёкшим expires_at (воркер ещё не перевёл)
    with pytest.raises(ApiError) as e:
        await cabinet_service.verify_code(token, code)
    assert e.value.status == 400 and e.value.error == "code_expired"

    # то же после перевода воркером в expired
    async with db.AsyncSessionLocal() as session:
        await session.execute(update(CabinetOtp).values(status="expired"))
        await session.commit()
    with pytest.raises(ApiError) as e:
        await cabinet_service.verify_code(token, code)
    assert e.value.error == "code_expired"

    row = await _token_row_by_hash_of(db, token)
    assert (row.failed_attempts or 0) == 0, "попытки не потрачены"


@pytest.mark.asyncio
async def test_verified_code_reuse_gives_superseded_no_second_session(db, deps):
    """R8/O8: повторный ввод использованного кода — code_superseded, второй сессии нет."""
    user = await _mk_user(db)
    _, token = await _issue(user.id)
    await _bind(db, token, deps.gw)
    code = deps.gw.calls[-1]["code"]

    with pytest.raises(ApiError) as e:
        await cabinet_service.verify_code(token, code)
    assert e.value.error == "code_superseded"
    async with db.AsyncSessionLocal() as session:
        sessions = (await session.execute(
            select(func.count()).select_from(CabinetSession))).scalar_one()
    assert sessions == 1, "второй сессии нет"


@pytest.mark.asyncio
async def test_lock_is_not_bypassed_by_dead_code_hint(db, deps):
    """R8/O8: в блокировке ввод погашенного кода даёт locked, не code_superseded."""
    user = await _mk_user(db)
    _, token = await _issue(user.id)
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    code1 = deps.gw.calls[-1]["code"]
    await _backdate_sends(db, seconds=601)  # снимает кулдаун и голосовое окно
    await cabinet_service.request_code(token, phone=PHONE)  # code1 → superseded

    async with db.AsyncSessionLocal() as session:
        await session.execute(update(CabinetToken).values(
            failed_attempts=5,
            attempts_window_start=datetime.now(timezone.utc)))
        await session.commit()

    with pytest.raises(ApiError) as e:
        await cabinet_service.verify_code(token, code1)
    assert e.value.status == 423 and e.value.error == "locked"


@pytest.mark.asyncio
async def test_dead_code_hints_on_aware_db(aware_db, deps):
    """R8/O8 на aware-БД: истёкший код различается по aware expires_at."""
    user = await _mk_user(aware_db, "tz2")
    _, token = await _issue(user.id)
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    code = deps.gw.calls[-1]["code"]
    async with aware_db.AsyncSessionLocal() as session:
        await session.execute(update(CabinetOtp).values(
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
        await session.commit()
    with pytest.raises(ApiError) as e:
        await cabinet_service.verify_code(token, code)
    assert e.value.error == "code_expired"
    row = await _token_row_by_hash_of(aware_db, token)
    assert (row.failed_attempts or 0) == 0


@pytest.mark.asyncio
async def test_verify_success_binds_and_extends_token(db, deps):
    user = await _mk_user(db)
    _, token = await _issue(user.id)
    result = await _bind(db, token, deps.gw)
    assert result["expires_in"] == 40 * 60
    row = await _token_row_by_hash_of(db, token)
    assert row.bound_phone == PHONE and row.bound_at is not None
    assert cabinet_service._aware(row.expires_at) > datetime.now(timezone.utc) + timedelta(days=29), "продление токена после привязки"
    async with db.AsyncSessionLocal() as session:
        otp_status = (await session.execute(select(CabinetOtp.status))).scalar_one()
        sessions = (await session.execute(select(func.count()).select_from(CabinetSession))).scalar_one()
    assert otp_status == "verified" and sessions == 1


# =======================
# send-status: маппинг состояний и лимит 60/мин
# =======================

@pytest.mark.asyncio
async def test_send_status_states(db, deps):
    user = await _mk_user(db)
    _, token = await _issue(user.id)
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    assert (await cabinet_service.send_status(token))["state"] == "sent"

    async def set_otp(**values):
        async with db.AsyncSessionLocal() as session:
            await session.execute(update(CabinetOtp).values(**values))
            await session.commit()

    # Статус доставки — запись-факт: «не доставлено» НЕ меняет send-status (O3)
    await set_otp(channel="sms", delivery_state="2")
    assert (await cabinet_service.send_status(token))["state"] == "sent"
    await set_otp(status="failed_send")
    assert (await cabinet_service.send_status(token))["state"] == "retry_free"
    await set_otp(status="sending")
    assert (await cabinet_service.send_status(token))["state"] == "sending"


@pytest.mark.asyncio
async def test_send_status_ignores_query_token(db, deps, client):
    """U4/R8: токен принимается ТОЛЬКО заголовком X-Cabinet-Token. Валидный
    токен в ?token= (без заголовка) неотличим от отсутствующего токена:
    410 link_expired — иначе токен утекает в access-логи и Referer."""
    user = await _mk_user(db, "qtok")
    _, token = await _issue(user.id, "01", "D-qtok")
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)

    # Валидный токен в query, заголовка нет → как будто токена нет вовсе
    resp_query = await client.get('/cabinet/api/auth/send-status',
                                  params={"token": token})
    assert resp_query.status == 410
    body_query = await resp_query.read()

    # Ответ побайтно совпадает с запросом вообще без токена
    resp_none = await client.get('/cabinet/api/auth/send-status')
    assert resp_none.status == 410
    assert body_query == await resp_none.read()
    assert (await resp_query.json())["error"] == "link_expired"

    # Тот же токен в заголовке работает — сам токен жив
    resp_hdr = await client.get('/cabinet/api/auth/send-status',
                                headers={"X-Cabinet-Token": token})
    assert resp_hdr.status == 200 and (await resp_hdr.json())["state"] == "sent"


def test_cabinet_api_source_has_no_query_token_fallback():
    """Source-grep U4: фолбэк ?token= в cabinet_api.py не должен вернуться."""
    src = (MAIN_PY.parent / "cabinet_api.py").read_text(encoding="utf-8")
    assert 'request.query.get("token")' not in src, \
        "send-status обязан принимать токен только заголовком X-Cabinet-Token"


@pytest.mark.asyncio
async def test_send_status_rate_limit_60_per_minute(db, deps):
    user = await _mk_user(db)
    _, token = await _issue(user.id)
    for _ in range(60):
        await cabinet_service.send_status(token)
    with pytest.raises(ApiError) as e:
        await cabinet_service.send_status(token)
    assert e.value.status == 429 and e.value.error == "rate_limited"


# =======================
# Bearer: R37, скользящее окно, last_seen_at
# =======================

@pytest.mark.asyncio
async def test_r37_expired_token_rejected_immediately(db, deps):
    """Токен с прошедшим expires_at, ещё не помеченный воркером → Bearer отклонён."""
    user = await _mk_user(db)
    _, token = await _issue(user.id)
    result = await _bind(db, token, deps.gw)
    bearer = f"Bearer {result['access_token']}"
    assert (await cabinet_service.get_me(bearer))["state"] == "ok"

    async with db.AsyncSessionLocal() as session:
        await session.execute(update(CabinetToken).values(
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))  # статус остаётся active
        await session.commit()
    with pytest.raises(ApiError) as e:
        await cabinet_service.get_me(bearer)
    assert e.value.status == 401 and e.value.error == "unauthorized"


@pytest.mark.asyncio
async def test_authenticated_call_updates_last_seen_and_slides_session(db, deps):
    user = await _mk_user(db)
    _, token = await _issue(user.id)
    result = await _bind(db, token, deps.gw)
    row = await _token_row_by_hash_of(db, token)
    assert row.last_seen_at is None

    await cabinet_service.get_me(f"Bearer {result['access_token']}")
    row = await _token_row_by_hash_of(db, token)
    assert row.last_seen_at is not None, "любой аутентифицированный вызов — сигнал жизни"

    async with db.AsyncSessionLocal() as session:
        sess = (await session.execute(select(CabinetSession))).scalar_one()
    assert cabinet_service._aware(sess.expires_at) > datetime.now(timezone.utc) + timedelta(minutes=39), "скользящее окно продлено"


@pytest.mark.asyncio
async def test_bad_bearer_rejected(db, deps):
    with pytest.raises(ApiError):
        await cabinet_service.validate_bearer(None)
    with pytest.raises(ApiError):
        await cabinet_service.validate_bearer("Bearer nonexistent-value")


# =======================
# Секреты (production) и R38
# =======================

def test_check_secrets_production_requires_hmac_key(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("CABINET_HMAC_KEY", raising=False)
    with pytest.raises(RuntimeError, match="CABINET_HMAC_KEY"):
        cabinet_service.check_secrets()

    monkeypatch.setenv("CABINET_HMAC_KEY", "prod-key")
    cabinet_service.check_secrets()  # с ключом старт проходит

    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.delenv("CABINET_HMAC_KEY", raising=False)
    cabinet_service.check_secrets()  # вне прода действует dev-дефолт


@pytest.mark.asyncio
async def test_r38_empty_dealers_alert_and_retry():
    alerts, loads, holder = [], [], {}

    async def tech_alert(text):
        alerts.append(text)

    async def noop(*a, **kw):
        pass

    cabinet_service.setup(noop, noop, noop, noop, tech_alert, lambda: holder)
    try:
        async def reload_dealers():
            loads.append(1)
            if len(loads) >= 2:
                holder["01"] = {"website": "https://x.ru"}

        ok = await cabinet_service.ensure_dealers_loaded(reload_dealers, interval=0, max_attempts=5)
        assert ok is True
        assert len(alerts) == 1, "техалерт уходит один раз"
        assert len(loads) == 2, "повторные попытки идут до успеха"

        # Непустой справочник — немедленный выход без алерта
        ok = await cabinet_service.ensure_dealers_loaded(reload_dealers, interval=0, max_attempts=1)
        assert ok is True and len(alerts) == 1 and len(loads) == 2
    finally:
        cabinet_service.setup(None, None, None, None, None, None)


# =======================
# CORS-интеграция (боевые middleware из main.py)
# =======================

ORIGIN = "https://dealer-one.ru"


@pytest.mark.asyncio
async def test_cors_preflight_dealer_origin_echoed(client):
    resp = await client.options('/cabinet/api/auth/init', headers={"Origin": ORIGIN})
    assert resp.status == 200
    assert resp.headers.get("Access-Control-Allow-Origin") == ORIGIN
    assert resp.headers.get("Vary") == "Origin"
    allow = resp.headers.get("Access-Control-Allow-Headers", "")
    assert "Authorization" in allow and "Content-Type" in allow

    # Второй дилерский origin (Website_URL с путём и query) тоже в whitelist
    resp = await client.options('/cabinet/api/auth/init',
                                headers={"Origin": "https://dealer-two.ru"})
    assert resp.headers.get("Access-Control-Allow-Origin") == "https://dealer-two.ru"


@pytest.mark.asyncio
async def test_cors_preflight_allows_x_cabinet_token(client):
    """Опрос send-status шлёт токен заголовком X-Cabinet-Token — кастомный
    заголовок делает запрос непростым и включает preflight. Без него в
    Allow-Headers кросс-доменный опрос статуса доставки (Tilda дилера → домен
    бота) не работает вовсе."""
    resp = await client.options('/cabinet/api/auth/send-status', headers={
        "Origin": ORIGIN,
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "x-cabinet-token",
    })
    assert resp.status == 200
    assert resp.headers.get("Access-Control-Allow-Origin") == ORIGIN
    allow = resp.headers.get("Access-Control-Allow-Headers", "").lower()
    assert "x-cabinet-token" in allow, "preflight не разрешает заголовок опроса"
    assert "authorization" in allow and "content-type" in allow


@pytest.mark.asyncio
async def test_cors_foreign_origin_gets_no_cors_headers(client):
    resp = await client.options('/cabinet/api/auth/init',
                                headers={"Origin": "https://evil.example"})
    assert resp.status == 200
    assert "Access-Control-Allow-Origin" not in resp.headers

    resp = await client.post('/cabinet/api/auth/init', json={"token": "x"},
                             headers={"Origin": "https://evil.example"})
    assert resp.status == 410
    assert "Access-Control-Allow-Origin" not in resp.headers


@pytest.mark.asyncio
async def test_cors_catalog_keeps_wildcard(client):
    resp = await client.get('/catalog/xx', headers={"Origin": "https://anything.example"})
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"
    resp = await client.options('/catalog/xx', headers={"Origin": "https://anything.example"})
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


@pytest.mark.asyncio
async def test_cors_error_statuses_carry_echo_not_wildcard(db, deps, client):
    user = await _mk_user(db, "cors")
    _, token = await _issue(user.id)
    headers = {"Origin": ORIGIN}

    # 410
    resp = await client.post('/cabinet/api/auth/init', json={"token": "bad"}, headers=headers)
    assert resp.status == 410
    assert resp.headers.get("Access-Control-Allow-Origin") == ORIGIN

    # 422 (казахстанский номер)
    resp = await client.post('/cabinet/api/auth/request-code',
                             json={"token": token, "phone": "+77001234567",
                                   "channel": "sms", "consent": CONSENT}, headers=headers)
    assert resp.status == 422
    assert resp.headers.get("Access-Control-Allow-Origin") == ORIGIN

    # 429 (кулдаун после принятой отправки)
    body = {"token": token, "phone": PHONE, "channel": "sms", "consent": CONSENT}
    resp = await client.post('/cabinet/api/auth/request-code', json=body, headers=headers)
    assert resp.status == 200
    resp = await client.post('/cabinet/api/auth/request-code', json=body, headers=headers)
    assert resp.status == 429
    assert resp.headers.get("Access-Control-Allow-Origin") == ORIGIN
    assert resp.headers.get("Vary") == "Origin"

    # Искусственный 500 — echo в error_middleware, не '*'
    resp = await client.get('/cabinet/api/_boom', headers=headers)
    assert resp.status == 500
    assert resp.headers.get("Access-Control-Allow-Origin") == ORIGIN


@pytest.mark.asyncio
async def test_http_flow_verify_and_me(db, deps, client):
    """Смоук полного цикла через HTTP: request-code → verify → /me."""
    user = await _mk_user(db, "http")
    _, token = await _issue(user.id)
    headers = {"Origin": ORIGIN}
    resp = await client.post('/cabinet/api/auth/request-code',
                             json={"token": token, "phone": PHONE,
                                   "channel": "voice", "consent": CONSENT}, headers=headers)
    assert resp.status == 200 and (await resp.json())["status"] == "ok"

    resp = await client.get('/cabinet/api/auth/send-status',
                            headers={**headers, "X-Cabinet-Token": token})
    assert (await resp.json())["state"] == "sent"

    code = deps.gw.calls[-1]["code"]
    resp = await client.post('/cabinet/api/auth/verify',
                             json={"token": token, "code": code}, headers=headers)
    assert resp.status == 200
    access_token = (await resp.json())["access_token"]

    resp = await client.get('/cabinet/api/me',
                            headers={**headers, "Authorization": f"Bearer {access_token}"})
    assert resp.status == 200 and (await resp.json())["state"] == "ok"

    resp = await client.get('/cabinet/api/me', headers=headers)
    assert resp.status == 401 and (await resp.json())["error"] == "unauthorized"


# =======================
# Время: aware-колонки боевого PostgreSQL против внутреннего времени кабинета
# =======================

def test_utcnow_is_aware_and_aware_normalizes_only_naive():
    assert cabinet_service._utcnow().tzinfo is not None, \
        "внутреннее время кабинета — aware UTC (колонки timezone=True)"
    naive = datetime(2026, 1, 1, 12, 0)
    assert cabinet_service._aware(naive) == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    already = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert cabinet_service._aware(already) is already
    assert cabinet_service._aware(None) is None


def test_time_comparison_identical_for_aware_and_naive_db_values():
    """PostgreSQL отдаёт aware, SQLite — наивное значение: обе ветки должны
    сравниваться с _utcnow() без TypeError и давать один результат."""
    now = cabinet_service._utcnow()
    window_start = now - timedelta(minutes=5)

    def _token(ws):
        return CabinetToken(user_id=1, dealer_code="01", token_hash="h",
                            status="active", failed_attempts=5,
                            attempts_window_start=ws)

    until_pg = cabinet_service._locked_until(_token(window_start), now)
    until_sqlite = cabinet_service._locked_until(_token(window_start.replace(tzinfo=None)), now)
    assert until_pg is not None and until_pg == until_sqlite

    # Счётчик неверных вводов: окно из БД тоже приходит обоими способами
    for ws in (window_start, window_start.replace(tzinfo=None)):
        token = _token(ws)
        token.failed_attempts = 1
        cabinet_service._register_failed_attempt(token, now)
        assert token.failed_attempts == 2, "окно не истекло — счётчик продолжается"


@pytest.mark.asyncio
async def test_full_auth_flow_survives_timezone_aware_db(aware_db, deps):
    """Сквозной проход кабинета на БД, отдающей aware-время (бой = PostgreSQL):
    выдача → init → код → verify → validate_bearer/me → проход otp_worker."""
    user = await _mk_user(aware_db, "tz")
    _, token = await _issue(user.id, "01", "D-tz")
    assert (await cabinet_service.auth_init(token))["state"] == "need_phone"

    data = await _bind(aware_db, token, deps.gw)
    bearer = f"Bearer {data['access_token']}"
    assert (await cabinet_service.validate_bearer(bearer))["user_id"] == user.id
    assert (await cabinet_service.get_me(bearer))["state"] == "ok"
    assert (await cabinet_service.send_status(token))["state"] == "sent"

    # Воркеры: страховочный фолбэк, экспирация, сигнал A, триггеры передачи
    stats = await cabinet_service.otp_worker_tick()
    assert stats["handoffs"] == 0, "свежая сессия — таймауты передачи не сработали"
    assert await cabinet_service.expire_tokens() == 0

    # Истёкшая сессия на aware-БД по-прежнему даёт 401, а не 500
    async with aware_db.AsyncSessionLocal() as session:
        await session.execute(update(CabinetSession).values(
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
        await session.commit()
    with pytest.raises(ApiError) as e:
        await cabinet_service.validate_bearer(bearer)
    assert e.value.status == 401


@pytest.mark.asyncio
async def test_handoff_timeout_triggers_on_timezone_aware_db(aware_db, deps):
    """Триггеры передачи заявки считают now − bound_at/last_seen_at из БД —
    на aware-значениях воркер обязан работать, а не падать TypeError'ом."""
    user = await _mk_user(aware_db, "tz-handoff")
    _, token = await _issue(user.id, "01", "D-tz-h")
    await _bind(aware_db, token, deps.gw)

    async with aware_db.AsyncSessionLocal() as session:
        stale = datetime.now(timezone.utc) - timedelta(hours=2)
        await session.execute(update(CabinetToken).values(bound_at=stale,
                                                          last_seen_at=stale))
        await session.commit()

    stats = await cabinet_service.otp_worker_tick()
    assert stats["handoffs"] == 1, "передача по абсолютному потолку не сработала"


# =======================
# Source-grep: проводка main.py (конвенция tests/test_payload.py)
# =======================

def test_main_py_wiring_source_pins():
    src = MAIN_PY.read_text(encoding="utf-8")

    # Регистрация роутов кабинета и стартовые проверки
    assert "cabinet_api.register_routes(app)" in src, "роуты /cabinet/api/* не зарегистрированы"
    assert "cabinet_service.check_secrets()" in src, "нет проверки секретов при старте"
    assert "cabinet_service.ensure_dealers_loaded" in src, "нет R38-вотчдога справочника дилеров"

    # Ветка кабинета в cors_middleware стоит ДО wildcard-возврата в OPTIONS
    mw = src.index("async def cors_middleware")
    cabinet_branch = src.index("startswith(CABINET_API_PREFIX)", mw)
    wildcard = src.index('"Access-Control-Allow-Origin": "*"', mw)
    assert cabinet_branch < wildcard, "ветка /cabinet/api/ обязана стоять до wildcard в OPTIONS"

    # 500-путь error_middleware несёт echo-CORS кабинета
    err = src.index("async def error_middleware")
    err_block = src[err:mw]
    assert "_cabinet_cors(request)" in err_block, "500-путь error_middleware без CORS-ветки кабинета"


class TestCorsOnSuccessfulResponses:
    """Регрессия класса, дважды пропущенного сюитой (находка #1 ревью
    2026-08-04 и дев-стенд 2026-08-10): aiohttp-клиент не проверяет CORS
    по-браузерному, и УСПЕШНЫЙ ответ без Access-Control-Allow-Origin оставался
    невидимым — тесты крыли только preflight и ошибочные ветки. Каждый
    успешный ответ кабинетного эндпоинта обязан нести echo-Origin."""

    @staticmethod
    def _assert_cors(resp, where):
        acao = resp.headers.get("Access-Control-Allow-Origin")
        assert acao == ORIGIN, \
            f"{where}: успешный ответ без echo-Origin (ACAO={acao!r}) — браузер заблокирует"
        assert "Origin" in resp.headers.get("Vary", ""), f"{where}: нет Vary: Origin"

    @pytest.mark.asyncio
    async def test_every_successful_cabinet_response_carries_cors(self, db, deps, client):
        user = await _mk_user(db, "cors-ok")
        _, token = await _issue(user.id)
        headers = {"Origin": ORIGIN}

        resp = await client.post('/cabinet/api/auth/init',
                                 json={"token": token}, headers=headers)
        assert resp.status == 200
        self._assert_cors(resp, "auth/init")

        resp = await client.post('/cabinet/api/auth/request-code',
                                 json={"token": token, "phone": PHONE,
                                       "channel": "voice", "consent": CONSENT},
                                 headers=headers)
        assert resp.status == 200
        self._assert_cors(resp, "auth/request-code")

        resp = await client.get('/cabinet/api/auth/send-status',
                                headers={**headers, "X-Cabinet-Token": token})
        assert resp.status == 200
        self._assert_cors(resp, "auth/send-status")

        code = deps.gw.calls[-1]["code"]
        resp = await client.post('/cabinet/api/auth/verify',
                                 json={"token": token, "code": code}, headers=headers)
        assert resp.status == 200
        self._assert_cors(resp, "auth/verify")
        auth = {**headers,
                "Authorization": f"Bearer {(await resp.json())['access_token']}"}

        resp = await client.get('/cabinet/api/me', headers=auth)
        assert resp.status == 200
        self._assert_cors(resp, "me")

        resp = await client.put('/cabinet/api/form',
                                json={"credit_payment": 40000}, headers=auth)
        assert resp.status == 200
        self._assert_cors(resp, "form PUT")

        resp = await client.post('/cabinet/api/form/submit',
                                 json={"credit_initial": 500000}, headers=auth)
        assert resp.status == 200
        self._assert_cors(resp, "form/submit")
