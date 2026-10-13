"""Гейт /catalog и /catalog-colors (решение владельца 2026-09-23): allowlist
таблиц, whitelist публичных полей дилеров, неизменность формата и полей
каталога (Active, вложения)."""
import pytest

import main as main_mod


class FakeReq:
    def __init__(self, table_id=None, query_table=None):
        self.match_info = {"dealer_code": table_id} if table_id else {}
        self.query = {"table_id": query_table} if query_table else {}
        self.headers = {}


DEALER_ROW = {
    "Dealer_Code": "99", "Dealer_name": "Тестовый дилер DEV",
    "Dealer_location": "Самара", "Dealer_status": "Официальный дилер HAVAL",
    "Dealer_address": "ул. Тестовая, 1", "Dealer_workinghours": "09-21",
    "Dealer_phone": "+78460000000",
    # всё ниже обязано вырезаться
    "Max_Group_ID": "-720000", "Telegram_Group_ID": "-100999",
    "Business_Connection_ID": "bc-1", "Dealer_id": "D-99",
    "Prompt_Teg": "base", "Sourse table id": "mywf0f16ptn25gk",
    "Policy": "текст", "Brand": "Haval",
}

MAIN_ROW = {
    "Model": "HAVAL JOLION", "Active": True, "Price new": 2000000,
    "Pics": [{"url": "https://x/1.jpg", "signedPath": "sp/1", "path": "p/1"}],
    "Title_pic": [{"url": "https://x/t.jpg", "signedPath": "sp/t", "path": "p/t"}],
}


@pytest.fixture
def catalog_env(monkeypatch):
    # U1 (план 2026-09-25-001): allowlist читает per-dealer набор
    # catalog_tables, собранный из `* table id`-колонок строки Dealers
    main_mod.CACHED_DEALERS["99"] = {
        "code": "99", "table_id": "dlrcat99tbl0001",
        "catalog_tables": {"dlrcat99tbl0001", "dlrstk99tbl0002",
                           "dlrprg99tbl0003", "dlrrat99tbl0004"},
    }
    calls = []

    async def fake_fetch(table_id):
        calls.append(table_id)
        if table_id == main_mod.NOCODB_TABLE_ID_DEALERS:
            return [dict(DEALER_ROW)]
        return [dict(MAIN_ROW)]
    monkeypatch.setattr(main_mod, "get_cars_from_nocodb_dynamic", fake_fetch)
    yield calls
    main_mod.CACHED_DEALERS.pop("99", None)


# =======================
# Allowlist
# =======================

@pytest.mark.asyncio
@pytest.mark.parametrize("table_id", [
    "mywf0f16ptn25gk",      # TABLE_MAIN (статичный)
    "mm8wldgk5vn9gwh",      # TABLE_FEATURES (статичный)
    "muruk10ipof33m5",      # TABLE_DEALERS (статичный)
    "dlrcat99tbl0001",      # каталог дилера (catalog_tables)
    "dlrstk99tbl0002",      # сток дилера
    "dlrprg99tbl0003",      # программы
    "dlrrat99tbl0004",      # ставки
])
async def test_allowed_tables_pass(catalog_env, table_id):
    resp = await main_mod.handle_catalog(FakeReq(table_id))
    assert resp.status == 200
    assert catalog_env == [table_id]                     # запрос дошёл до NocoDB


@pytest.mark.asyncio
@pytest.mark.parametrize("table_id", [
    "mnivr6uyf5ckzru",      # архив диалогов
    "ma0nf1e807qb9e0",      # Bots
    "mbbecemi1ac6awi",      # Prompt_Blocks
    "mzzzunknown0000",
])
async def test_forbidden_tables_404_without_details(catalog_env, table_id):
    resp = await main_mod.handle_catalog(FakeReq(table_id))
    assert resp.status == 404
    assert catalog_env == []                             # до NocoDB не дошло
    assert table_id not in resp.text                     # без подробностей
    assert resp.text == '{"error": "not found"}'


@pytest.mark.asyncio
async def test_colors_endpoint_same_gate(catalog_env):
    ok = await main_mod.handle_catalog_colors(FakeReq(query_table="dlrstk99tbl0002"))
    assert ok.status == 200
    bad = await main_mod.handle_catalog_colors(FakeReq(query_table="mnivr6uyf5ckzru"))
    assert bad.status == 404
    assert catalog_env == ["dlrstk99tbl0002"]


# =======================
# U1 (план 2026-09-25-001): сборка по `* table id`-колонкам, второй дилер,
# техалерт о расширении allowlist
# =======================

def test_dealer_table_columns_pattern():
    """КТД-1: ловятся ВСЕ колонки `* table id` — включая «Features table id»
    и «Color table id» (единственное число), будущие имена; базы — нет."""
    row = {
        "Sourse table id": "mcat00000000001",
        "Features table id": "mfea00000000002",
        "Color table id": "mcol00000000003",       # единственное число (В1)
        "Colors table id": "mcls00000000004",      # будущая колонка
        "Stock_Table_ID": "mstk00000000005",
        "Program_Table_ID": "", "Rates_Table_ID": None,   # пустые — норма
        "Sourse base id": "pbas00000000006",       # база — НЕ таблица
        "Stock_Base_ID": "pbas00000000007",
        "UTC_Offset": 8, "Reminder_Image": [{"url": "x"}],  # не строки — мимо
        "Dealer_name": "Тест",
    }
    cols = main_mod._dealer_table_columns(row)
    assert set(cols) == {"mcat00000000001", "mfea00000000002",
                         "mcol00000000003", "mcls00000000004", "mstk00000000005"}
    assert cols["mcol00000000003"] == "Color table id"
    assert main_mod._dealer_table_columns({"Dealer_name": "x"}) == {}


@pytest.mark.asyncio
async def test_incident_second_dealer_with_other_tables(catalog_env):
    """Случай инцидента 2026-09-25: второй дилер (Geely) со СВОИМ набором
    таблиц — они должны отдавать 200, а не 404."""
    main_mod.CACHED_DEALERS["77"] = {
        "code": "77",
        "catalog_tables": {"geelycat0000001", "geelyfea0000002"},
    }
    try:
        for tid in ("geelycat0000001", "geelyfea0000002"):
            resp = await main_mod.handle_catalog(FakeReq(tid))
            assert resp.status == 200, f"{tid}: инцидент Geely повторился"
        # дилер без catalog_tables (старый кэш) — не роняет сборку
        main_mod.CACHED_DEALERS["88"] = {"code": "88"}
        assert (await main_mod.handle_catalog(FakeReq("geelycat0000001"))).status == 200
    finally:
        main_mod.CACHED_DEALERS.pop("77", None)
        main_mod.CACHED_DEALERS.pop("88", None)


@pytest.mark.asyncio
async def test_new_table_tech_alert_dedup(monkeypatch):
    """Новая таблица в последующем прочтении → один техалерт с колонкой и
    дилером; повтор той же таблицы не алертит; первое прочтение молчит."""
    import asyncio
    import bot_registry
    alerts = []

    async def rec(key, text, cooldown=900):
        alerts.append((key, text))
        return True
    monkeypatch.setattr(bot_registry, "tech_alert", rec)
    monkeypatch.setattr(main_mod, "_CATALOG_TABLES_SEEN", None)

    async def drain():
        for _ in range(10):
            await asyncio.sleep(0)

    base = {"mcat00000000001": ("Sourse table id", "99")}
    main_mod._report_new_catalog_tables(base)             # первое прочтение
    await drain()
    assert alerts == []                                   # молчит (инициализация)

    with_new = dict(base, **{"mfea00000000002": ("Features table id", "77")})
    main_mod._report_new_catalog_tables(with_new)
    await drain()
    assert len(alerts) == 1
    key, text = alerts[0]
    assert key == "catalog-allow-new:mfea00000000002"
    assert "Features table id" in text and "77" in text   # колонка и дилер

    main_mod._report_new_catalog_tables(with_new)         # дедуп
    await drain()
    assert len(alerts) == 1


# =======================
# Whitelist полей дилеров
# =======================

@pytest.mark.asyncio
async def test_dealers_response_has_only_seven_public_fields(catalog_env):
    resp = await main_mod.handle_catalog(FakeReq("muruk10ipof33m5"))
    import json
    row = json.loads(resp.text)["list"][0]
    assert set(row.keys()) == {
        "Dealer_Code", "Dealer_location", "Dealer_name", "Dealer_status",
        "Dealer_address", "Dealer_workinghours", "Dealer_phone",
    }                                                     # исходные имена
    assert "Max_Group_ID" not in resp.text
    assert "Sourse" not in resp.text and "Prompt" not in resp.text


def test_field_whitelist_matching_is_normalized():
    """Сайт читает имена регистронезависимо и без '_'/пробелов."""
    row = {"DEALER CODE": "01", "dealer_phone": "+7", "Dealer Working Hours": "9-21",
           "MaxGroupID": "-1", "db_config": {}}
    public = main_mod._dealer_public_row(row)
    assert public == {"DEALER CODE": "01", "dealer_phone": "+7",
                      "Dealer Working Hours": "9-21"}


# =======================
# Каталог не режется: Active и вложения нетронуты
# =======================

@pytest.mark.asyncio
async def test_main_table_rows_untouched(catalog_env):
    resp = await main_mod.handle_catalog(FakeReq("mywf0f16ptn25gk"))
    import json
    row = json.loads(resp.text)["list"][0]
    assert row["Active"] is True                          # фильтр сайта жив
    assert row["Pics"][0] == {"url": "https://x/1.jpg",
                              "signedPath": "sp/1", "path": "p/1"}
    assert row["Title_pic"][0]["signedPath"] == "sp/t"    # вложенные ключи целы


@pytest.mark.asyncio
async def test_response_format_is_list_wrapper(catalog_env):
    import json
    resp = await main_mod.handle_catalog(FakeReq("mm8wldgk5vn9gwh"))
    assert set(json.loads(resp.text).keys()) == {"list"}  # формат не менялся
