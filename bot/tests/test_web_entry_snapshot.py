"""Веб-вход (план 2026-09-30-001, U3): снапшот из каталога для обоих входов,
точечный запрос строки каталога, слияние оценки, /me."""
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select

import cabinet_service
import main as main_mod
from cabinet_service import ApiError, CatalogUnavailable
from database import CabinetProfile, CabinetToken, User

from conftest import CONSENT, PHONE

DEALERS = {
    "01": {"dealer_name": "Тест Авто", "website": "https://dealer-one.ru/", "code": "01",
           "db_config": {"catalog_table_id": "cat01", "rates_table_id": "rate01",
                         "program_table_id": "prog01"}},
}
ROW = {"Id": 7, "Model id": "15", "Complectation id": "12", "Model": "ATLAS PRO",
       "Complectation": "Люкс", "Price old": 3026990, "Price new": 2466990,
       "Cash in hook": 80000, "Credit bonus": 200000, "Trade-in bonus": 250000}
# Коллизия комплектационного id между моделями — поиск только по паре
OTHER_MODEL_ROW = dict(ROW, **{"Id": 8, "Model id": "18", "Model": "MONJARO",
                               "Price old": 5052190})
RATES = [{"Rate": 18, "Term_From": 12, "Term_To": 84,
          "Downpayment_From": 10, "Downpayment_To": 100}]
PROGRAMS = [{"Model_Pattern": "15", "Max_Term_Months": 84, "Min_Down_Payment": 10}]


@pytest.fixture
def dealers():
    return DEALERS


@pytest.fixture
def nocodb_tables():
    return {"cat01": [ROW, OTHER_MODEL_ROW], "rate01": RATES, "prog01": PROGRAMS}


async def _user(db, puid="web-1", channel="web"):
    async with db.AsyncSessionLocal() as session:
        user = User(platform_user_id=puid, channel=channel, full_name="Клиент")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _snapshot(db, user_id):
    async with db.AsyncSessionLocal() as session:
        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == user_id)
        )).scalar_one_or_none()
    return json.loads(profile.calc_snapshot) if profile and profile.calc_snapshot else None


@pytest.mark.asyncio
async def test_web_snapshot_from_catalog_row(db, deps):
    user = await _user(db)
    assert await cabinet_service.freeze_web_snapshot(user.id, "01", ROW, 30000)
    snap = await _snapshot(db, user.id)
    assert snap["schema_version"] == 3
    assert (snap["item_id"], snap["model_id"]) == ("12", "15")
    assert snap["item_name"] == "ATLAS PRO · Люкс"
    assert (snap["price"], snap["combo_price"]) == (2750000, 2500000)
    assert snap["target_payment"] == 30000
    assert snap["rates"] and snap["program"]["Min_Down_Payment"] == 10
    assert snap["found_info"] is None and snap["ti_valuation"] is None


@pytest.mark.asyncio
async def test_web_snapshot_rejects_unpriced_row(db, deps):
    user = await _user(db)
    assert not await cabinet_service.freeze_web_snapshot(
        user.id, "01", dict(ROW, **{"Price old": None}), 30000)
    assert await _snapshot(db, user.id) is None


@pytest.mark.asyncio
async def test_load_catalog_row_by_pair_avoids_model_collision(db, deps):
    row = await cabinet_service.load_catalog_row("01", "18", "12")
    assert row["Model"] == "MONJARO"
    assert await cabinet_service.load_catalog_row("01", "99", "12") is None


@pytest.mark.asyncio
async def test_load_catalog_row_uses_pinpoint_loader_and_maps_failure(db, deps, monkeypatch):
    calls = []

    async def pinpoint(table_id, model_id, compl_id):
        calls.append((table_id, model_id, compl_id))
        return ROW

    monkeypatch.setattr(cabinet_service, "_catalog_row_loader", pinpoint)
    assert await cabinet_service.load_catalog_row("01", "15", "12") is ROW
    assert calls == [("cat01", "15", "12")]

    async def broken(*_):
        raise RuntimeError("NocoDB HTTP 502")

    monkeypatch.setattr(cabinet_service, "_catalog_row_loader", broken)
    with pytest.raises(CatalogUnavailable):
        await cabinet_service.load_catalog_row("01", "15", "12")


def _cache(item="12", model="15", credit=2_999_000):
    return {"complectations": [{"id": item, "name": "Люкс",
                                "price": {"credit_price": credit}}],
            "user_request": {"selected_complectation_id": item},
            "qualification_context": {"model_id": model}}


@pytest.mark.asyncio
async def test_alexander_snapshot_prices_from_catalog(db, deps):
    user = await _user(db, "max:1", "max")
    assert await cabinet_service._freeze_calc_snapshot(user.id, _cache(), "01", 35000)
    snap = await _snapshot(db, user.id)
    assert (snap["price"], snap["combo_price"]) == (2750000, 2500000)   # не 2 999 000 из n8n
    assert deps.tech_alerts == []


@pytest.mark.asyncio
async def test_alexander_snapshot_falls_back_to_n8n_price_with_one_alert(db, deps):
    user = await _user(db, "max:2", "max")
    cache = _cache(item="12", model="99")         # пары нет в каталоге
    assert await cabinet_service._freeze_calc_snapshot(user.id, cache, "01", 35000)
    assert await cabinet_service._freeze_calc_snapshot(user.id, cache, "01", 35000)
    snap = await _snapshot(db, user.id)
    assert (snap["price"], snap["combo_price"]) == (2999000, None)
    assert len(deps.tech_alerts) == 1 and "не найдена в каталоге" in deps.tech_alerts[0]


@pytest.mark.asyncio
async def test_merge_valuation_keeps_item_and_price(db, deps):
    user = await _user(db)
    await cabinet_service.freeze_web_snapshot(user.id, "01", ROW, 30000)
    assert await cabinet_service.merge_valuation_into_snapshot(
        user.id, {"mark": "Kia", "photo": "//img/x.jpg"}, 850000)
    snap = await _snapshot(db, user.id)
    assert (snap["item_id"], snap["price"]) == ("12", 2750000)
    assert snap["ti_valuation"] == 850000
    assert snap["found_info"]["photo"] == "https://img/x.jpg"
    # Сверка сабмита после слияния — без 409
    await cabinet_service._verify_calc_check(user.id, {"item_id": "12", "price": 2750000})


@pytest.mark.asyncio
async def test_chat_refreeze_does_not_touch_web_token_snapshot(db, deps):
    user = await _user(db, "web-9")
    await cabinet_service.freeze_web_snapshot(user.id, "01", ROW, 30000)
    async with db.AsyncSessionLocal() as session:
        session.add(CabinetToken(user_id=user.id, dealer_code="01", token_hash="h" * 64,
                                 status="active", entry_source="web"))
        await session.commit()
    before = await _snapshot(db, user.id)
    assert await cabinet_service.refresh_calc_snapshot("web-9", _cache(credit=1)) is False
    assert await _snapshot(db, user.id) == before


@pytest.mark.asyncio
async def test_me_exposes_combo_price_entry_source_and_valuation(db, deps):
    user = await _user(db, "max:3", "max")
    ctx = await cabinet_service.prepare_cabinet_context(
        user.id, "01", deal_id="D-1", has_phone=False, scenario_type="combo",
        payment=35000, brand_model="ATLAS PRO", n8n_cache=_cache())
    token = ctx["cabinet_url"].split("#t=", 1)[1]
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    result = await cabinet_service.verify_code(token, deps.gw.calls[-1]["code"])
    me = await cabinet_service.get_me(f"Bearer {result['access_token']}")
    assert me["calc"]["combo_price"] == 2500000
    assert me["entry_source"] == "chat"
    assert me["valuation"] == {"status": "none", "reason": None}
    assert "model_id" not in me["calc"] and "schema_version" not in me["calc"]


# ---- main.get_catalog_row_by_pair против фейкового NocoDB ----

async def _fake_nocodb(handler):
    app = web.Application()
    app.router.add_get("/api/v2/tables/{t}/records", handler)
    server = TestServer(app)
    await server.start_server()
    return server


@pytest.mark.asyncio
async def test_pinpoint_loader_filters_by_pair(monkeypatch):
    seen = {}

    async def handler(request):
        seen["where"] = request.query.get("where")
        return web.json_response({"list": [dict(ROW, ncRecordHash="x",
                                                Pics=[{"url": "u", "thumbnails": {}}])]})

    server = await _fake_nocodb(handler)
    monkeypatch.setattr(main_mod, "NOCODB_API_URL", str(server.make_url("")).rstrip("/"))
    main_mod.CATALOG_ROW_CACHE.clear()
    try:
        row = await main_mod.get_catalog_row_by_pair("cat01", "15", "12")
    finally:
        await server.close()
    assert seen["where"] == "(Model id,eq,15)~and(Complectation id,eq,12)"
    assert row["Model"] == "ATLAS PRO" and "ncRecordHash" not in row
    assert "thumbnails" not in row["Pics"][0]


@pytest.mark.asyncio
async def test_pinpoint_loader_raises_on_http_error_and_none_on_empty(monkeypatch):
    status = {"code": 502}

    async def handler(request):
        if status["code"] != 200:
            return web.Response(status=status["code"])
        return web.json_response({"list": []})

    server = await _fake_nocodb(handler)
    monkeypatch.setattr(main_mod, "NOCODB_API_URL", str(server.make_url("")).rstrip("/"))
    main_mod.CATALOG_ROW_CACHE.clear()
    try:
        with pytest.raises(RuntimeError):
            await main_mod.get_catalog_row_by_pair("cat01", "15", "12")
        status["code"] = 200
        assert await main_mod.get_catalog_row_by_pair("cat01", "15", "12") is None
    finally:
        await server.close()


# ---- Дубли Complectation id внутри модели (поправка 2026-09-30) ----

@pytest.mark.asyncio
async def test_duplicate_pair_takes_first_row_in_nocodb_order(db, deps, monkeypatch):
    first = dict(ROW, **{"Id": 2, "Price old": 3_000_000})
    second = dict(ROW, **{"Id": 20, "Price old": 4_000_000})
    monkeypatch.setattr(cabinet_service, "_catalog_row_loader", None)

    async def loader(table_id):
        return [first, second] if table_id == "cat01" else []

    monkeypatch.setattr(cabinet_service, "_nocodb_table_loader", loader)
    row = await cabinet_service.load_catalog_row("01", "15", "12")
    assert row["Id"] == 2


@pytest.mark.asyncio
async def test_pinpoint_loader_takes_first_of_duplicates(monkeypatch):
    seen = {}

    async def handler(request):
        seen["limit"] = request.query.get("limit")
        return web.json_response({"list": [dict(ROW, Id=2), dict(ROW, Id=20)]})

    server = await _fake_nocodb(handler)
    monkeypatch.setattr(main_mod, "NOCODB_API_URL", str(server.make_url("")).rstrip("/"))
    main_mod.CATALOG_ROW_CACHE.clear()
    try:
        row = await main_mod.get_catalog_row_by_pair("cat01", "15", "12")
    finally:
        await server.close()
    assert row["Id"] == 2 and seen["limit"] == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize("price_old", [None, 0, ""])
async def test_alexander_row_without_price_old_uses_n8n_price_with_alert(db, deps, monkeypatch, price_old):
    async def pinpoint(table_id, model_id, compl_id):
        return dict(ROW, **{"Price old": price_old})

    monkeypatch.setattr(cabinet_service, "_catalog_row_loader", pinpoint)
    user = await _user(db, f"max:np{price_old!r}", "max")
    assert await cabinet_service._freeze_calc_snapshot(user.id, _cache(), "01", 35000)
    snap = await _snapshot(db, user.id)
    assert (snap["price"], snap["combo_price"]) == (2999000, None)
    assert len(deps.tech_alerts) == 1 and "Price old" in deps.tech_alerts[0]
