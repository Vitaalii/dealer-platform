"""Выпуск 3.4.1 (решения владельца 2026-10-06):
- dealer_id в ходах чата к n8n — из карточки дилера (на бою в продолжении
  диалога уходил null, сток искался по пустому Dealer_id);
- колонка активности каталога — настройка дилера Catalog_Active_Field
  (Active / Active_<число>, пусто — Active): в db_config → корневой data_routing тела запроса к n8n,
  в GET /cabinet/api/dealer/{code} → tables.catalog.active_field для лендинга;
- web/open позиции, не активной у дилера, — сессия открывается, техалерт с дедупом."""
import pytest

import cabinet_service
import main as main_mod
from test_payload import TestDealerFieldsInRequestBody, _CaptureSession
from test_dealer_settings import ROW as DEALER_ROW, _dealers


# --- dealer_id в ходах чата ---

def _patch_chat(monkeypatch, card):
    helper = TestDealerFieldsInRequestBody()
    helper.CARD = card
    return helper._patch(monkeypatch)


@pytest.mark.asyncio
async def test_chat_turn_dealer_id_from_dealer_card_when_context_empty(monkeypatch):
    card = dict(TestDealerFieldsInRequestBody.CARD, dealer_id="avtomir-samara")
    _patch_chat(monkeypatch, card)
    ctx = {"start_code": "010101", "scenario_type": "credit", "chat_history": []}
    await main_mod.send_chat_message_to_n8n("u-341-1", "вопрос", "Имя", ctx)
    assert _CaptureSession.captured["qualification_context"]["dealer_id"] == "avtomir-samara"


@pytest.mark.asyncio
async def test_chat_turn_dealer_id_card_wins_over_context(monkeypatch):
    card = dict(TestDealerFieldsInRequestBody.CARD, dealer_id="avtomir-samara")
    _patch_chat(monkeypatch, card)
    ctx = {"start_code": "010101", "scenario_type": "credit", "chat_history": [],
           "dealer_id": "stale"}
    await main_mod.send_chat_message_to_n8n("u-341-2", "вопрос", "Имя", ctx)
    assert _CaptureSession.captured["qualification_context"]["dealer_id"] == "avtomir-samara"


@pytest.mark.asyncio
async def test_chat_turn_dealer_id_falls_back_to_context_without_card_value(monkeypatch):
    _patch_chat(monkeypatch, dict(TestDealerFieldsInRequestBody.CARD))
    ctx = {"start_code": "010101", "scenario_type": "credit", "chat_history": [],
           "dealer_id": "from-ctx"}
    await main_mod.send_chat_message_to_n8n("u-341-3", "вопрос", "Имя", ctx)
    assert _CaptureSession.captured["qualification_context"]["dealer_id"] == "from-ctx"


# --- Catalog_Active_Field: разбор значения ---

@pytest.mark.parametrize("raw,expected,bad", [
    (None, "Active", None), ("", "Active", None), ("  ", "Active", None),
    ("Active", "Active", None), ("Active_1", "Active_1", None), (" Active_12 ", "Active_12", None),
    ("active_1", "Active", "active_1"), ("Active-1", "Active", "Active-1"),
    ("Active_", "Active", "Active_"), ("Active_1a", "Active", "Active_1a"), ("Price old", "Active", "Price old"),
])
def test_catalog_active_field_parsing(raw, expected, bad):
    row = {} if raw is None else {"Catalog_Active_Field": raw}
    assert main_mod._dealer_catalog_active_field(row) == (expected, bad)


def test_catalog_active_field_column_name_normalized():
    assert main_mod._dealer_catalog_active_field({"catalog active field": "Active_1"})[0] == "Active_1"


# --- Catalog_Active_Field: загрузка дилеров → db_config (data_routing для n8n) ---

class _DealersSession:
    rows = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, headers=None, params=None, ssl=None):
        rows = self.rows

        class _Resp:
            status = 200

            async def json(self):
                return {"list": rows}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        return _Resp()


@pytest.mark.asyncio
async def test_dealer_loader_puts_active_field_to_db_config_and_alerts_bad_value(monkeypatch):
    alerts = []

    async def fake_tech_alert(key, text, cooldown=None):
        alerts.append((key, text))

    monkeypatch.setattr(main_mod, "NOCODB_TOKEN", "t")
    monkeypatch.setattr(main_mod.bot_registry, "tech_alert", fake_tech_alert)
    monkeypatch.setattr(main_mod, "_report_new_catalog_tables", lambda info: None)
    monkeypatch.setattr(main_mod, "CACHED_DEALERS", {})
    _DealersSession.rows = [
        dict(DEALER_ROW, Dealer_Code="02", Catalog_Active_Field="Active_1"),
        dict(DEALER_ROW, Dealer_Code="09", Catalog_Active_Field=""),
        dict(DEALER_ROW, Dealer_Code="05", Catalog_Active_Field="Активен"),
    ]
    monkeypatch.setattr(main_mod.aiohttp, "ClientSession", _DealersSession)
    await main_mod.load_data_from_nocodb()
    cfg = {code: main_mod.CACHED_DEALERS[code]["db_config"]["catalog_active_field"]
           for code in ("02", "09", "05")}
    assert cfg == {"02": "Active_1", "09": "Active", "05": "Active"}
    assert [k for k, _ in alerts if k.startswith("catalog-active-field")] == \
        ["catalog-active-field:05:Активен"]


@pytest.mark.asyncio
async def test_data_routing_carries_active_field_chat_and_handoff(monkeypatch):
    import database as db_mod
    card = dict(TestDealerFieldsInRequestBody.CARD,
                db_config={"catalog_table_id": "t1", "catalog_active_field": "Active_1"})
    _patch_chat(monkeypatch, card)
    ctx = {"start_code": "010101", "scenario_type": "credit", "chat_history": []}
    await main_mod.send_chat_message_to_n8n("u-341-4", "вопрос", "Имя", ctx)
    assert _CaptureSession.captured["data_routing"]["catalog_active_field"] == "Active_1"
    user = db_mod.User(platform_user_id="u-341-5", channel="max", full_name="Тест",
                       username=None, phone=None, active_deal_id=None)
    await main_mod.send_handoff_webhook("u-341-5", dict(ctx), user)
    assert _CaptureSession.captured["data_routing"]["catalog_active_field"] == "Active_1"


# --- GET /cabinet/api/dealer/{code}: tables.catalog.active_field ---

def _settings(monkeypatch, row):
    monkeypatch.setattr(cabinet_service, "_dealers_provider", lambda: _dealers(row))
    return cabinet_service.dealer_settings("99")


@pytest.mark.parametrize("value,expected", [("Active_1", "Active_1"), ("", "Active"),
                                            (None, "Active"), ("bad value", "Active")])
def test_dealer_settings_active_field(monkeypatch, value, expected):
    row = dict(DEALER_ROW)
    if value is not None:
        row["Catalog_Active_Field"] = value
    tables = _settings(monkeypatch, row)["tables"]
    assert tables["catalog"] == {"table_id": "mywf0f16ptn25gk", "active_field": expected}
    assert "catalog_active_field" not in tables


def test_dealer_settings_no_catalog_no_active_field(monkeypatch):
    row = dict(DEALER_ROW, **{"Sourse table id": None, "Catalog_Active_Field": "Active_1"})
    assert "catalog" not in _settings(monkeypatch, row)["tables"]


# --- web/open позиции, не активной у дилера ---

DEALERS_ACTIVE = {"01": {"db_config": {"catalog_active_field": "Active_1"}},
                  "09": {"db_config": {}}}


@pytest.fixture
def active_dealers(monkeypatch):
    monkeypatch.setattr(cabinet_service, "_dealers_provider", lambda: DEALERS_ACTIVE)
    alerts = []

    async def tech_alert(text):
        alerts.append(text)

    monkeypatch.setattr(cabinet_service, "_tech_alert", tech_alert)
    cabinet_service.reset_inactive_position_alerts()
    yield alerts
    cabinet_service.reset_inactive_position_alerts()


def test_dealer_catalog_active_field_default(active_dealers):
    assert cabinet_service.dealer_catalog_active_field("01") == "Active_1"
    assert cabinet_service.dealer_catalog_active_field("09") == "Active"
    assert cabinet_service.dealer_catalog_active_field("77") == "Active"


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [True, 1, "true", "1"])
async def test_active_position_no_alert(active_dealers, flag):
    await cabinet_service._alert_inactive_position("01", {"Model": "X", "Active_1": flag}, "15", "12")
    assert active_dealers == []


@pytest.mark.asyncio
async def test_inactive_position_alert_dedup_per_dealer_and_position(active_dealers):
    row = {"Model": "ATLAS PRO", "Complectation": "Люкс", "Active": True, "Active_1": False}
    for _ in range(3):
        await cabinet_service._alert_inactive_position("01", row, "15", "12")
    assert len(active_dealers) == 1
    assert "позиция не отмечена в колонке «Active_1»" in active_dealers[0]
    assert "ATLAS PRO · Люкс" in active_dealers[0]
    await cabinet_service._alert_inactive_position("01", row, "15", "13")   # другая позиция
    await cabinet_service._alert_inactive_position("09", {"Active": None}, "15", "12")  # другой дилер
    assert len(active_dealers) == 3


@pytest.mark.asyncio
async def test_missing_column_alert_reason(active_dealers):
    await cabinet_service._alert_inactive_position("01", {"Model": "X", "Active": True}, "15", "12")
    assert "в каталоге нет колонки «Active_1»" in active_dealers[0]


@pytest.mark.asyncio
async def test_alert_failure_does_not_raise(active_dealers, monkeypatch):
    async def boom(text):
        raise RuntimeError("MAX недоступен")

    monkeypatch.setattr(cabinet_service, "_tech_alert", boom)
    await cabinet_service._alert_inactive_position("01", {"Active_1": False}, "15", "12")
