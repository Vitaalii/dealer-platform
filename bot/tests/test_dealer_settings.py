"""U13a (план 2026-09-30-001): GET /cabinet/api/dealer/{dealer_code} — настройки
дилера для лендинга из кэша дилеров бота (whitelist, решение владельца 2026-10-01)."""
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import cabinet_api
import cabinet_service
import main as main_mod

ORIGIN = "https://demo.autodc.org"

# Строка Dealers как в NocoDB: публичные, внутренние и новые колонки владельца
ROW = {
    "Id": 14, "CreatedAt": "2026-02-03", "Name": "Тестовый дилер DEV",
    "Telegram_Group_ID": "-100370", "Max_Group_ID": "-7201427", "Business_Connection_ID": "biz-1",
    "Website_URL": "https://demo.autodc.org", "Brand": "Haval", "Dealer_Code": "99",
    "Sourse table id": "mywf0f16ptn25gk", "Features table id": "mm8wldgk5vn9gwh",
    "Color table id": "mwyu1qq92hm96o9", "Sourse base id": "p96xnewh6m79beu",
    "Stock_Base_ID": "p96xnewh6m79beu", "Stock_Table_ID": "mtsf9afpiqe1yr1",
    "Program_Table_ID": "my7rg0l7j6varzn", "Rates_Table_ID": "mfocbgvi50mqm7c",
    "Communication_Type": "Smartbot", "UTC_Offset": 8, "Dealer_id": "avtomir-samara",
    "Dealer_name": "Автомир Самара", "Dealer_address": "Самара, ул. Авроры, д. 150Е",
    "Dealer_location": "53.21, 50.18", "Dealer_workinghours": "пн-вс: 09:00-21:00",
    "Dealer_phone": "+7 (999) 555 5555", "Dealer_status": "Официальный дилер HAVAL",
    "Prompt_Ver": "v00.00", "Prompt_Teg": "base", "Policy": "ПОЛИТИКА…", "Agreement": "СОГЛАСИЕ…",
    "letter_title": "Тестовая заявка", "form_type": "ВЫБОР АВТО",
    "Secret_New_Column": "не должно уйти наружу",
}
INTERNAL_VALUES = ("-100370", "-7201427", "biz-1", "Smartbot", "avtomir-samara", "v00.00",
                   "ПОЛИТИКА…", "СОГЛАСИЕ…", "mwyu1qq92hm96o9", "p96xnewh6m79beu",
                   "mtsf9afpiqe1yr1", "my7rg0l7j6varzn", "не должно уйти наружу",
                   "Тестовый дилер DEV")


def _dealers(row=ROW, code="99"):
    return {code: {"website": row.get("Website_URL"), "code": code,
                   "landing": main_mod._dealer_landing_settings(row)}}


@pytest.fixture
def dealers():
    return _dealers()


@pytest.fixture
def nocodb_tables():
    return {}


@pytest_asyncio.fixture
async def client(db, deps, monkeypatch):
    monkeypatch.setattr(main_mod, "TECH_ADMIN_ID", "")
    app = web.Application(middlewares=[main_mod.error_middleware, main_mod.cors_middleware])
    cabinet_api.register_routes(app)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    yield test_client
    await test_client.close()


@pytest.mark.asyncio
async def test_settings_from_cache_whitelist_only(client):
    resp = await client.get("/cabinet/api/dealer/99", headers={"Origin": ORIGIN})
    assert resp.status == 200
    data = await resp.json()
    assert data == {
        "dealer_code": "99",
        "dealer": {"Dealer_Code": "99", "Dealer_name": "Автомир Самара",
                   "Dealer_address": "Самара, ул. Авроры, д. 150Е", "Dealer_location": "53.21, 50.18",
                   "Dealer_workinghours": "пн-вс: 09:00-21:00", "Dealer_phone": "+7 (999) 555 5555",
                   "Dealer_status": "Официальный дилер HAVAL"},
        "tables": {"catalog": {"table_id": "mywf0f16ptn25gk", "active_field": "Active"},
                   "features": {"table_id": "mm8wldgk5vn9gwh"},
                   "rates": {"table_id": "mfocbgvi50mqm7c"}},
        "form_values": {"brand": "Haval", "dealer": "Автомир Самара",
                        "website": "https://demo.autodc.org", "letter_title": "Тестовая заявка",
                        "form_type": "ВЫБОР АВТО"},
        "cabinet_assets": cabinet_service.cabinet_build_assets(),
        "cabinet_page_url": "https://demo.autodc.org/cabinet?src=web",
    }
    body = await resp.text()
    for value in INTERNAL_VALUES:
        assert value not in body, value


@pytest.mark.asyncio
async def test_cache_control_and_cors_own_origin(client):
    resp = await client.get("/cabinet/api/dealer/99", headers={"Origin": ORIGIN})
    assert resp.headers["Cache-Control"] == "public, max-age=300"
    assert resp.headers.get("Access-Control-Allow-Origin") == ORIGIN
    assert "Origin" in resp.headers.get("Vary", "")


@pytest.mark.asyncio
async def test_foreign_origin_gets_no_cors(client):
    resp = await client.get("/cabinet/api/dealer/99", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in resp.headers
    pre = await client.options("/cabinet/api/dealer/99",
                               headers={"Origin": "https://evil.example",
                                        "Access-Control-Request-Method": "GET"})
    assert "Access-Control-Allow-Origin" not in pre.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["9", "999", "ab", "9a"])
async def test_invalid_code(client, code):
    resp = await client.get(f"/cabinet/api/dealer/{code}")
    assert resp.status == 422
    assert await resp.json() == {"error": "invalid_input", "fields": {"dealer_code": "invalid"}}


@pytest.mark.asyncio
async def test_unknown_dealer(client):
    resp = await client.get("/cabinet/api/dealer/42")
    assert resp.status == 404 and (await resp.json()) == {"error": "unknown_dealer"}


@pytest.mark.asyncio
async def test_dealers_not_loaded(client, monkeypatch):
    monkeypatch.setattr(cabinet_service, "_dealers_provider", lambda: {})
    resp = await client.get("/cabinet/api/dealer/99")
    assert resp.status == 503 and (await resp.json()) == {"error": "dealers_unavailable"}
    assert "Cache-Control" not in resp.headers


@pytest.mark.asyncio
async def test_empty_columns_omitted(client, monkeypatch):
    row = dict(ROW, letter_title="", form_type=None, Dealer_phone="  ", **{"Features table id": ""})
    monkeypatch.setattr(cabinet_service, "_dealers_provider", lambda: _dealers(row))
    data = await (await client.get("/cabinet/api/dealer/99")).json()
    assert "letter_title" not in data["form_values"] and "form_type" not in data["form_values"]
    assert "Dealer_phone" not in data["dealer"] and "features" not in data["tables"]


def test_owner_columns_read_case_insensitive():
    row = dict(ROW)
    row.pop("letter_title"), row.pop("form_type")
    row.update({"Letter_Title": "Новый лид", "Form Type": "Расчет кредита"})
    fv = main_mod._dealer_landing_settings(row)["form_values"]
    assert fv["letter_title"] == "Новый лид" and fv["form_type"] == "Расчет кредита"


def test_dealer_cache_carries_landing_block():
    src = open(main_mod.__file__, encoding="utf-8").read()
    assert '"landing": _dealer_landing_settings(row)' in src


# ---------------- Ускорение A6: текст документа по открытию поп-апа ----------------

@pytest.mark.asyncio
async def test_legal_endpoint_text_cache_cors(client, monkeypatch):
    dealers = _dealers()
    dealers["99"]["policy_text"] = "Политика дилера"
    monkeypatch.setattr(cabinet_service, "_dealers_provider", lambda: dealers)
    resp = await client.get("/cabinet/api/dealer/99/legal/policy", headers={"Origin": ORIGIN})
    assert resp.status == 200
    assert (await resp.json()) == {"dealer_code": "99", "kind": "policy", "text": "Политика дилера"}
    assert resp.headers["Cache-Control"] == "public, max-age=300"
    assert resp.headers.get("Access-Control-Allow-Origin") == ORIGIN
    missing = await client.get("/cabinet/api/dealer/99/legal/agreement")
    assert missing.status == 404 and (await missing.json()) == {"error": "not_found"}


def test_cabinet_assets_from_build_manifest():
    """Ускорение A4: пути сборки с ?v= — ровно как в строках <script> страницы кабинета."""
    import json, pathlib
    manifest = json.loads((pathlib.Path(cabinet_service.CABINET_BUILD_DIR) / "manifest.json")
                          .read_text(encoding="utf-8"))
    assets = cabinet_service.cabinet_build_assets()
    assert assets == [f"/widget-custom/cabinet-app/Production/{n}?v={manifest['files'][n]['version']}"
                      for n in ("calc_engine.browser.js", "calc_model.js", "app.js")]


def test_cabinet_assets_absent_without_build(monkeypatch, tmp_path):
    monkeypatch.setattr(cabinet_service, "CABINET_BUILD_DIR", str(tmp_path))
    assert cabinet_service.cabinet_build_assets() == []


@pytest.mark.asyncio
async def test_cabinet_page_url_matches_web_open_prefix(db, deps, monkeypatch):
    """cabinet_page_url — ровно начало cabinet_url из «открыть сессию» до #t=."""
    monkeypatch.setenv("CABINET_PAGE_PATH", "/cabinet_dev")
    data = cabinet_service.dealer_settings("99")
    assert data["cabinet_page_url"] == "https://demo.autodc.org/cabinet_dev?src=web"
    base = cabinet_service._cabinet_base_url("https://demo.autodc.org")
    url = cabinet_service._cabinet_url(base, "TOKEN", "web")
    assert url.split("#t=", 1)[0] == data["cabinet_page_url"]


def test_cabinet_page_url_absent_without_https_website(monkeypatch):
    row = dict(ROW, Website_URL="http://demo.autodc.org")
    monkeypatch.setattr(cabinet_service, "_dealers_provider", lambda: _dealers(row))
    assert "cabinet_page_url" not in cabinet_service.dealer_settings("99")
