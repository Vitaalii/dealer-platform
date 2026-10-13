"""Веб-вход (план 2026-09-30-001, U5): эндпоинт «открыть сессию»
POST /cabinet/api/web/open — контракт, повтор по коду+nonce, лимиты, CORS."""
import asyncio
import json
from datetime import timedelta

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select

import cabinet_api
import cabinet_service
import main as main_mod
from cabinet_service import ApiError
from database import CabinetProfile, CabinetToken, CabinetWebOpen, User

ORIGIN = "https://dealer-one.ru"
DEALERS = {
    "01": {"dealer_name": "Тест Авто", "website": "https://dealer-one.ru/", "code": "01",
           "db_config": {"catalog_table_id": "cat01", "rates_table_id": "rate01",
                         "program_table_id": "prog01"}},
    "02": {"dealer_name": "Без сайта", "website": "http://no-https.ru", "code": "02",
           "db_config": {"catalog_table_id": "cat01"}},
}
ROW = {"Id": 7, "Model id": "15", "Complectation id": "12", "Model": "ATLAS PRO",
       "Complectation": "Люкс", "Price old": 3026990, "Price new": 2466990,
       "Cash in hook": 80000, "Credit bonus": 200000, "Trade-in bonus": 250000,
       "Link": "/price?filters=abc"}
NOPRICE = dict(ROW, **{"Id": 8, "Model id": "16", "Price old": None})
NONCE = "n" * 32


@pytest.fixture
def dealers():
    return DEALERS


@pytest.fixture
def nocodb_tables():
    return {"cat01": [ROW, NOPRICE],
            "rate01": [{"Rate": 18, "Term_From": 12, "Term_To": 84,
                        "Downpayment_From": 10, "Downpayment_To": 100}],
            "prog01": [{"Model_Pattern": "15", "Max_Term_Months": 84, "Min_Down_Payment": 10}]}


@pytest.fixture
def deal_lookup(monkeypatch):
    """Фейковый поиск сделки Tilda по коду: {code: deal_id}."""
    found = {}
    calls = []

    async def finder(code):
        calls.append(code)
        return {"ID": found[code], "STAGE_ID": "C9:NEW"} if code in found else None

    monkeypatch.setattr(cabinet_service, "_find_deal_by_code", finder)
    return found, calls


@pytest_asyncio.fixture
async def client(db, deps, deal_lookup, monkeypatch):
    monkeypatch.setattr(main_mod, "TECH_ADMIN_ID", "")
    monkeypatch.setenv("CABINET_PAGE_PATH", "/cabinet_dev")
    cabinet_service.reset_web_limit_alerts()
    app = web.Application(middlewares=[main_mod.error_middleware, main_mod.cors_middleware])
    cabinet_api.register_routes(app)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    yield test_client
    await test_client.close()


def _body(**over):
    body = {"dealer_code": "01", "lead_code": "011512ABCD", "nonce": NONCE,
            "model_id": "15", "complectation_id": "12", "scenario": "credit",
            "desired_payment": 30000,
            "form": {"Model": "ATLAS PRO", "Link": "/price?filters=abc", "FORMID": "form2472574461"}}
    body.update(over)
    return body


async def _open(client, headers=None, **over):
    return await client.post("/cabinet/api/web/open", json=_body(**over),
                             headers={"Origin": ORIGIN, **(headers or {})})


async def _count(db, model):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar()


@pytest.mark.asyncio
async def test_open_credit_returns_cabinet_dev_url(client, db, deps):
    resp = await _open(client)
    assert resp.status == 200
    url = (await resp.json())["cabinet_url"]
    assert url.startswith("https://dealer-one.ru/cabinet_dev?src=web#t=")   # признак веб-входа (U8)
    async with db.AsyncSessionLocal() as session:
        user = (await session.execute(select(User))).scalar_one()
        token = (await session.execute(select(CabinetToken))).scalar_one()
        prof = (await session.execute(select(CabinetProfile))).scalar_one()
    assert user.full_name == "Клиент" and user.channel == "web"
    assert user.platform_user_id.startswith("webcab:")
    assert token.entry_source == "web"
    expires = cabinet_service._aware(token.expires_at)
    assert timedelta(minutes=39) < expires - cabinet_service._utcnow() <= timedelta(minutes=40)
    assert prof.lead_code == "011512ABCD" and prof.lead_nonce_hash != NONCE
    assert prof.scenario_type == "credit" and prof.link_fallback == "/price?filters=abc"
    assert prof.calc_snapshot and '"combo_price": 2500000' in prof.calc_snapshot


@pytest.mark.asyncio
async def test_found_tilda_deal_remembered_without_bitrix_writes(client, db, deps, deal_lookup):
    found, calls = deal_lookup
    found["011512ABCD"] = "52673"
    assert (await _open(client)).status == 200
    async with db.AsyncSessionLocal() as session:
        token = (await session.execute(select(CabinetToken))).scalar_one()
    assert token.deal_id == "52673" and calls == ["011512ABCD"]
    assert deps.bitrix.calls == []   # ни crm.deal.update, ни crm.contact.update (AE12)


@pytest.mark.asyncio
async def test_repeat_same_nonce_same_url_no_limit_spent(client, db):
    first = await (await _open(client)).json()
    second = await (await _open(client)).json()
    assert first == second
    assert await _count(db, User) == 1
    assert await _count(db, CabinetWebOpen) == 1


@pytest.mark.asyncio
async def test_repeat_after_phone_bound_same_url(client, db):
    first = await (await _open(client)).json()
    async with db.AsyncSessionLocal() as session:
        token = (await session.execute(select(CabinetToken))).scalar_one()
        token.bound_phone = "+79001234567"
        token.bound_at = cabinet_service._utcnow()
        await session.commit()
    assert await (await _open(client)).json() == first


@pytest.mark.asyncio
async def test_repeat_foreign_nonce_rejected_and_limited(client, db):
    await _open(client)
    resp = await _open(client, nonce="x" * 32)
    assert resp.status == 422
    assert (await resp.json()) == {"error": "invalid_input", "fields": {"nonce": "mismatch"}}
    for _ in range(cabinet_service.WEB_BAD_NONCE_IP_HOURLY_LIMIT - 1):
        await _open(client, nonce="x" * 32)
    resp = await _open(client, nonce="x" * 32)
    assert resp.status == 429 and (await resp.json())["limit_source"] == "ip"


@pytest.mark.asyncio
async def test_found_deal_of_other_code_is_not_used(client, db, deal_lookup):
    found, _ = deal_lookup
    found["019999ZZZZ"] = "1"
    await _open(client)
    async with db.AsyncSessionLocal() as session:
        token = (await session.execute(select(CabinetToken))).scalar_one()
    assert token.deal_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("plate,expected", [("a123bc77", "А123ВС77"), ("А 123 ВС 777", "А123ВС777")])
async def test_plate_normalized(client, db, plate, expected):
    resp = await _open(client, scenario="tradein", plate=plate, mileage=50000)
    assert resp.status == 200
    async with db.AsyncSessionLocal() as session:
        prof = (await session.execute(select(CabinetProfile))).scalar_one()
    assert prof.web_plate == expected and prof.web_mileage == 50000


@pytest.mark.asyncio
@pytest.mark.parametrize("plate", ["А123ВС00", "АБ123В77", "D123BC77", ""])
async def test_invalid_plate_rejected(client, plate):
    resp = await _open(client, scenario="combo", plate=plate, mileage=1)
    assert resp.status == 422 and (await resp.json())["error"] == "invalid_plate"


@pytest.mark.asyncio
@pytest.mark.parametrize("mileage,ok", [(0, True), (1_000_000, True), (1_000_001, False),
                                        (-1, False), ("abc", False), (None, False)])
async def test_mileage_bounds(client, mileage, ok):
    resp = await _open(client, scenario="tradein", plate="А123ВС77", mileage=mileage,
                       lead_code=f"0115{12:02d}ABC{'DEFGHI'[abs(hash(str(mileage))) % 6]}")
    if ok:
        assert resp.status == 200
    else:
        assert resp.status == 422
        assert (await resp.json())["fields"] == {"mileage": "invalid"}


@pytest.mark.asyncio
async def test_dealer_code_must_match_lead_code_prefix(client):
    resp = await _open(client, lead_code="021512ABCD")
    assert resp.status == 422
    assert (await resp.json())["fields"] == {"lead_code": "dealer_mismatch"}


@pytest.mark.asyncio
@pytest.mark.parametrize("over,field", [({"scenario": "lease"}, "scenario"),
                                        ({"nonce": "short"}, "nonce"),
                                        ({"lead_code": "011512AB"}, "lead_code"),
                                        ({"desired_payment": 0}, "desired_payment")])
async def test_invalid_fields(client, over, field):
    resp = await _open(client, **over)
    assert resp.status == 422 and field in (await resp.json())["fields"]


@pytest.mark.asyncio
async def test_unknown_dealer_and_position(client):
    resp = await _open(client, dealer_code="03", lead_code="031512ABCD")
    assert resp.status == 404 and (await resp.json())["error"] == "unknown_dealer"
    resp = await _open(client, model_id="99")
    assert resp.status == 404 and (await resp.json())["error"] == "unknown_position"


@pytest.mark.asyncio
async def test_cabinet_unavailable_cases(client, monkeypatch):
    # позиция без цены — недоступна (поправка 2026-09-30)
    resp = await _open(client, model_id="16")
    assert resp.status == 404 and (await resp.json())["error"] == "unknown_position"
    # дилер без https-сайта
    resp = await client.post("/cabinet/api/web/open",
                             json=_body(dealer_code="02", lead_code="021512ABCD"))
    assert resp.status == 503
    # NocoDB недоступна
    async def broken(*_):
        raise RuntimeError("NocoDB HTTP 502")
    monkeypatch.setattr(cabinet_service, "_catalog_row_loader", broken)
    resp = await _open(client)
    assert resp.status == 503 and (await resp.json())["error"] == "cabinet_unavailable"


@pytest.mark.asyncio
async def test_ip_hourly_limit_with_single_alert(client, deps):
    for i in range(cabinet_service.WEB_OPEN_IP_HOURLY_LIMIT):
        resp = await _open(client, lead_code=f"011512ABC{'ABCDE'[i]}", nonce=f"{i}" * 32)
        assert resp.status == 200
    for _ in range(2):
        resp = await _open(client, lead_code="011512ZZZZ", nonce="z" * 32)
        assert resp.status == 429 and (await resp.json())["limit_source"] == "ip"
    assert len([a for a in deps.tech_alerts if "IP" in a]) == 1


@pytest.mark.asyncio
async def test_dealer_daily_cap_from_env(client, monkeypatch):
    monkeypatch.setenv("CABINET_WEB_DEALER_DAILY_CAP", "1")
    assert (await _open(client)).status == 200
    resp = await _open(client, lead_code="011512WXYZ", nonce="w" * 32,
                       headers={"X-Forwarded-For": "9.9.9.9"})
    assert resp.status == 429 and (await resp.json())["limit_source"] == "dealer"


@pytest.mark.asyncio
async def test_zero_cap_is_kill_switch_with_single_alert(client, db, deps, monkeypatch):
    """R43: CABINET_WEB_DEALER_DAILY_CAP=0 — веб-вход остановлен для всех
    дилеров (как OTP-кап), лендинг показывает ошибку L5; техалерт один."""
    assert (await _open(client, lead_code="011512CCCC", nonce="c" * 32)).status == 200
    monkeypatch.setenv("CABINET_WEB_DEALER_DAILY_CAP", "0")
    # повтор уже открытой сессии тоже остановлен
    resp = await _open(client, lead_code="011512CCCC", nonce="c" * 32)
    assert resp.status == 429
    for lead, nonce in (("011512AAAA", "a" * 32), ("011512BBBB", "b" * 32)):
        resp = await _open(client, lead_code=lead, nonce=nonce)
        assert resp.status == 429 and (await resp.json())["limit_source"] == "dealer"
    assert await _count(db, User) == 1 and await _count(db, CabinetWebOpen) == 1
    assert len([a for a in deps.tech_alerts if "выключен" in a]) == 1


@pytest.mark.asyncio
async def test_link_fallback_validated(client, db):
    await _open(client, form={"Link": "https://evil.example/x"})
    await _open(client, lead_code="011512BBBB", nonce="b" * 32,
                form={"LINK": "https://dealer-one.ru/price?x=1"})
    async with db.AsyncSessionLocal() as session:
        links = [p.link_fallback for p in (await session.execute(
            select(CabinetProfile).order_by(CabinetProfile.id))).scalars()]
    assert links == [None, "https://dealer-one.ru/price?x=1"]


@pytest.mark.asyncio
async def test_return_url_from_referer_whitelisted_only(client, db):
    await _open(client, headers={"Referer": "https://dealer-one.ru/page154296216#x"})
    await _open(client, lead_code="011512CCCC", nonce="c" * 32,
                headers={"Referer": "https://evil.example/p"})
    async with db.AsyncSessionLocal() as session:
        urls = [p.return_url for p in (await session.execute(
            select(CabinetProfile).order_by(CabinetProfile.id))).scalars()]
    assert urls == ["https://dealer-one.ru/page154296216", "https://dealer-one.ru/"]


@pytest.mark.asyncio
async def test_cors_whitelist_and_foreign_origin(client):
    pre = await client.options("/cabinet/api/web/open",
                               headers={"Origin": ORIGIN,
                                        "Access-Control-Request-Method": "POST"})
    assert pre.headers.get("Access-Control-Allow-Origin") == ORIGIN
    assert "Origin" in pre.headers.get("Vary", "")
    resp = await client.post("/cabinet/api/web/open", json=_body(),
                             headers={"Origin": "https://evil.example"})
    assert resp.status == 403
    assert "Access-Control-Allow-Origin" not in resp.headers


@pytest.mark.asyncio
async def test_unbound_web_token_expires_after_40_minutes(client, db):
    url = (await (await _open(client)).json())["cabinet_url"]
    token = url.split("#t=", 1)[1]
    assert (await cabinet_service.auth_init(token))["state"] == "need_phone"
    async with db.AsyncSessionLocal() as session:
        row = (await session.execute(select(CabinetToken))).scalar_one()
        row.expires_at = cabinet_service._utcnow() - timedelta(seconds=1)
        await session.commit()
    with pytest.raises(ApiError) as e:
        await cabinet_service.auth_init(token)
    assert e.value.status == 410


def _req(remote, xff=None):
    class R:
        pass
    r = R()
    r.remote = remote
    r.headers = {"X-Forwarded-For": xff} if xff else {}
    return r


def test_client_ip_trusts_forwarded_only_from_trusted_proxy(monkeypatch):
    monkeypatch.delenv("CABINET_TRUSTED_PROXIES", raising=False)
    assert cabinet_api.client_ip(_req("10.0.0.1", "1.2.3.4")) == "10.0.0.1"
    monkeypatch.setenv("CABINET_TRUSTED_PROXIES", "10.0.0.1, 10.0.0.2")
    assert cabinet_api.client_ip(_req("10.0.0.1", "1.2.3.4, 10.0.0.2")) == "1.2.3.4"
    assert cabinet_api.client_ip(_req("5.5.5.5", "1.2.3.4")) == "5.5.5.5"
    assert cabinet_api.client_ip(_req("10.0.0.1")) == "10.0.0.1"


# ---------------- Блок form (R62) ----------------

async def _profile(db):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(select(CabinetProfile))).scalars().first()


@pytest.mark.asyncio
async def test_form_block_saved_as_is_with_client_ip(client, db, monkeypatch):
    monkeypatch.setenv("CABINET_TRUSTED_PROXIES", "127.0.0.1")
    form = {"FORMID": "form2472574461", "utm_source": "yandex", "_ym_uid": "171394",
            "Link": "/price?filters=abc", "Пустое": ""}
    resp = await _open(client, form=form, headers={"X-Forwarded-For": "203.0.113.7"})
    assert resp.status == 200
    stored = json.loads((await _profile(db)).web_form)
    assert stored == {"fields": form, "client_ip": "203.0.113.7"}


@pytest.mark.asyncio
@pytest.mark.parametrize("form,err", [
    (None, "required"), ("строка", "invalid"), ([], "invalid"),
    ({"FORMID": 123}, "invalid"), ({"a": None}, "invalid"), ({"a": {"b": "c"}}, "invalid"),
    ({f"k{i}": "v" for i in range(81)}, "invalid"), ({"a": "x" * 2001}, "invalid"),
    ({"k" * 65: "v"}, "invalid"), ({f"k{i}": "я" * 1000 for i in range(10)}, "too_large"),
])
async def test_form_block_types_and_size(client, db, form, err):
    body = _body()
    if form is None:
        body.pop("form")
    else:
        body["form"] = form
    resp = await client.post("/cabinet/api/web/open", json=body, headers={"Origin": ORIGIN})
    assert resp.status == 422
    assert (await resp.json()) == {"error": "invalid_input", "fields": {"form": err}}
    assert await _count(db, CabinetToken) == 0


@pytest.mark.asyncio
async def test_form_block_edge_sizes_accepted(client, db):
    form = {f"k{i}": "v" for i in range(80)}
    form["k0"] = "x" * 2000
    assert (await _open(client, form=form)).status == 200


@pytest.mark.asyncio
async def test_repeat_does_not_overwrite_form_block(client, db):
    await _open(client, form={"FORMID": "form1"})
    resp = await _open(client, form={"FORMID": "form2"})
    assert resp.status == 200
    assert json.loads((await _profile(db)).web_form)["fields"] == {"FORMID": "form1"}


@pytest.mark.asyncio
async def test_car_block_no_longer_used_for_link(client, db):
    await _open(client, form={}, car={"Link": "/price?from=car"})
    assert (await _profile(db)).link_fallback is None


@pytest.mark.asyncio
async def test_cleanup_clears_form_block_after_30_days(client, db):
    await _open(client, form={"FORMID": "form1"})
    await _open(client, form={"FORMID": "form2"}, lead_code="011512ABCE", nonce="m" * 32)
    now = cabinet_service._utcnow()
    async with db.AsyncSessionLocal() as session:
        first = (await session.execute(select(CabinetToken).order_by(CabinetToken.id))).scalars().first()
        first.created_at = now - timedelta(days=31)
        await session.commit()
    stats = await cabinet_service._cleanup_web_entry_data(now)
    assert stats["web_forms_cleared"] == 1
    async with db.AsyncSessionLocal() as session:
        forms = {p.lead_code: p.web_form for p in (await session.execute(select(CabinetProfile))).scalars()}
    assert forms["011512ABCD"] is None and "form2" in forms["011512ABCE"]


async def _expire_unbound_token(db, *, swept: bool):
    async with db.AsyncSessionLocal() as session:
        row = (await session.execute(select(CabinetToken))).scalar_one()
        row.expires_at = cabinet_service._utcnow() - timedelta(seconds=1)
        await session.commit()
    if swept:  # фоновая уборка успела
        await cabinet_service.expire_tokens()


@pytest.mark.asyncio
@pytest.mark.parametrize("swept", [False, True])
async def test_repeat_after_unbound_expiry_opens_new_session(client, db, deal_lookup, swept):
    """Решение 2026-10-02: повтор с тем же кодом и nonce после 40 мин — всегда
    новая сессия (новый токен, та же сделка Tilda), независимо от уборки."""
    found, _ = deal_lookup
    found["011512ABCD"] = "52673"
    first = (await (await _open(client)).json())["cabinet_url"]
    await _expire_unbound_token(db, swept=swept)
    resp = await _open(client)
    assert resp.status == 200
    second = (await resp.json())["cabinet_url"]
    assert second != first
    new_token = second.split("#t=", 1)[1]
    assert (await cabinet_service.auth_init(new_token))["state"] == "need_phone"
    async with db.AsyncSessionLocal() as session:
        tokens = (await session.execute(select(CabinetToken).order_by(CabinetToken.id))).scalars().all()
        opens = await _count(db, CabinetWebOpen)
    assert len(tokens) == 2 and tokens[1].deal_id == "52673" and tokens[1].entry_source == "web"
    assert opens == 2                                    # расход лимита как у нового открытия
    # и повтор новой сессии с тем же nonce — снова её адрес
    assert (await (await _open(client)).json())["cabinet_url"] == second


@pytest.mark.asyncio
async def test_ip_hourly_limit_configurable_by_env_default_5(client, monkeypatch):
    monkeypatch.setenv("CABINET_WEB_IP_HOURLY_LIMIT", "7")
    for i in range(7):
        assert (await _open(client, lead_code=f"011512AA{chr(65 + i)}A",
                            nonce=f"{'p' * 30}{i:02d}")).status == 200
    assert (await _open(client, lead_code="011512ZZZZ", nonce="z" * 32)).status == 429
    monkeypatch.setenv("CABINET_WEB_IP_HOURLY_LIMIT", "мусор")
    assert cabinet_service._web_ip_hourly_limit() == 5
    monkeypatch.delenv("CABINET_WEB_IP_HOURLY_LIMIT")
    assert cabinet_service._web_ip_hourly_limit() == 5


@pytest.mark.asyncio
async def test_dev_fault_flag_returns_503_only_on_dev(client, monkeypatch, tmp_path):
    flag = tmp_path / "fault"
    monkeypatch.setenv("CABINET_DEV_WEB_OPEN_FAULT_FILE", str(flag))
    monkeypatch.setenv("BOT_ENV", "dev")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    assert (await _open(client)).status == 200            # файла нет — работает
    flag.write_text("1")
    resp = await _open(client, lead_code="011512FFFF", nonce="f" * 32)
    assert resp.status == 503 and (await resp.json()) == {"error": "cabinet_unavailable"}
    monkeypatch.setenv("BOT_ENV", "prod")                  # не дев — флаг игнорируется
    assert (await _open(client, lead_code="011512GGGG", nonce="g" * 32)).status == 200
    monkeypatch.setenv("BOT_ENV", "dev")
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert (await _open(client, lead_code="011512HHHH", nonce="h" * 32)).status == 200


# ---------------- 2026-10-02: идемпотентность и параллельные шаги ----------------

@pytest.mark.asyncio
async def test_two_simultaneous_opens_same_code_nonce_one_session(client, db, monkeypatch):
    real = cabinet_service.load_catalog_row

    async def slow_row(*a, **k):
        await asyncio.sleep(0.3)
        return await real(*a, **k)

    monkeypatch.setattr(cabinet_service, "load_catalog_row", slow_row)
    r1, r2 = await asyncio.gather(_open(client), _open(client))
    assert r1.status == 200 and r2.status == 200
    assert (await r1.json())["cabinet_url"] == (await r2.json())["cabinet_url"]
    assert await _count(db, CabinetToken) == 1 and await _count(db, User) == 1
    assert await _count(db, CabinetWebOpen) == 1          # лимит — один раз


@pytest.mark.asyncio
async def test_slow_steps_run_in_parallel(client, monkeypatch):
    real = cabinet_service.load_catalog_row

    async def slow_row(*a, **k):
        await asyncio.sleep(0.4)
        return await real(*a, **k)

    async def slow_deal(code):
        await asyncio.sleep(0.4)
        return None

    monkeypatch.setattr(cabinet_service, "load_catalog_row", slow_row)
    monkeypatch.setattr(cabinet_service, "_find_deal_by_code", slow_deal)
    t0 = asyncio.get_event_loop().time()
    assert (await _open(client)).status == 200
    assert asyncio.get_event_loop().time() - t0 < 0.75      # не 0.8+ последовательно


@pytest.mark.asyncio
async def test_failed_deal_lookup_opens_session_without_deal(client, db, monkeypatch):
    async def broken(code):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(cabinet_service, "_find_deal_by_code", broken)
    assert (await _open(client)).status == 200
    async with db.AsyncSessionLocal() as session:
        tok = (await session.execute(select(CabinetToken))).scalar_one()
    assert tok.deal_id is None


@pytest.mark.asyncio
async def test_reservation_released_on_failure_retry_opens(client, db, monkeypatch):
    async def missing(*a, **k):
        return None

    real = cabinet_service.load_catalog_row
    monkeypatch.setattr(cabinet_service, "load_catalog_row", missing)
    assert (await _open(client)).status == 404
    async with db.AsyncSessionLocal() as session:
        prof = (await session.execute(select(CabinetProfile))).scalar_one()
    assert prof.lead_code is None and prof.lead_nonce_hash is None
    monkeypatch.setattr(cabinet_service, "load_catalog_row", real)
    assert (await _open(client)).status == 200
    assert not cabinet_service._WEB_OPEN_LOCKS             # блокировки убраны


@pytest.mark.asyncio
async def test_open_inactive_position_opens_with_single_tech_alert(client, deps, nocodb_tables):
    """3.4.1, вариант (б): позиция без флажка колонки дилера (по умолчанию Active)
    — сессия открывается, техалерт один раз на дилера+позицию."""
    first = await _open(client)
    second = await _open(client, nonce="m" * 32, lead_code="011512ABCE")
    assert first.status == 200 and second.status == 200
    alerts = [a for a in deps.tech_alerts if "Вход с сайта" in a]
    assert len(alerts) == 1 and "«Active»" in alerts[0] and "ATLAS PRO" in alerts[0]


@pytest.mark.asyncio
async def test_open_active_position_no_tech_alert(client, deps, nocodb_tables):
    nocodb_tables["cat01"][0] = dict(ROW, Active=True)
    resp = await _open(client)
    assert resp.status == 200
    assert not [a for a in deps.tech_alerts if "Вход с сайта" in a]
