"""U6 (план 2026-09-15-001): сверка «бренд дилера ∈ бренды бота» (R3/R15/R16,
KTD-7) — дедуп-алерты, регистронезависимость, web вне сверки, повтор при
недоставленном алерте."""
import inspect
import time

import pytest

import bot_registry
import main as main_mod


class AlertRecorder:
    def __init__(self, delivered=True):
        self.calls = []
        self.delivered = delivered

    async def __call__(self, text):
        self.calls.append(text)
        return self.delivered


def rows_fetcher(rows):
    async def fetch():
        return rows
    return fetch


# U9: брендовая сверка живёт на прод-паре (max-geely); dev-пара — «все
# марки» ('*'), у неё сверка молчит (отдельный тест ниже)
GEELY_BOT_ROWS = [
    {"Bot_Key": "max-geely", "Channel": "max", "Brands": "geely,belgee",
     "Token_Env": "MAX_BOT_TOKEN", "Environment": "all", "Active": True},
]


@pytest.fixture
def registry(db, monkeypatch):
    bot_registry.reset_for_tests()
    monkeypatch.setenv("BOT_ENV", "prod")
    monkeypatch.setenv("MAX_BOT_TOKEN", "legacy-max-token-0001")
    monkeypatch.setenv("TG_BOT_TOKEN", "legacy-tg-token-0002")
    yield
    bot_registry.reset_for_tests()


async def _load(alerts, rows=GEELY_BOT_ROWS):
    bot_registry.setup(alert=alerts, fetch_rows=rows_fetcher(rows))
    await bot_registry.load_bots_registry()


def dealer(code="02", brand="Geely", name="Тест Дилер"):
    # brand в CACHED_DEALERS уже нормализован загрузчиком (strip+lower) —
    # эмулируем это здесь; регистр исходной строки проверяется отдельно
    return {"code": code, "brand": str(brand or "").strip().lower(),
            "dealer_name": name}


@pytest.mark.asyncio
async def test_brand_match_including_case_is_silent(registry):
    alerts = AlertRecorder()
    await _load(alerts)
    # исторический ключ max-main резолвится алиасом в max-geely (U9-переход)
    ctx = {"channel": "max", "bot_key": "max-main"}
    await main_mod._brand_check(ctx, dealer(brand="Geely"), "02ABCDEF")
    await main_mod._brand_check(ctx, dealer(brand="BELGEE"), "03ABCDEF")
    assert alerts.calls == []


@pytest.mark.asyncio
async def test_brand_mismatch_alerts_once_per_hour_per_pair(registry):
    alerts = AlertRecorder()
    await _load(alerts)
    ctx = {"channel": "max", "bot_key": "max-geely", "deal_id": "D-11"}
    tank = dealer(code="10", brand="Tank", name="Танк Центр")
    await main_mod._brand_check(ctx, tank, "10ABCDEF")
    await main_mod._brand_check(ctx, tank, "10ABCDEF")   # дедуп пары в кулдауне
    mism = [a for a in alerts.calls if "Несоответствие бренда" in a]
    assert len(mism) == 1
    # состав алерта: дилер, код, бот, бренд дилера, бренды бота, сделка (R3)
    assert "Танк Центр" in mism[0] and "10" in mism[0]
    assert "max-geely" in mism[0] and "tank" in mism[0]
    assert "geely" in mism[0] and "belgee" in mism[0]
    assert "D-11" in mism[0]
    # другая пара (другой дилер) — отдельный ключ дедупа
    await main_mod._brand_check(ctx, dealer(code="12", brand="Changan"), "12ABCDEF")
    assert len([a for a in alerts.calls if "Несоответствие бренда" in a]) == 2


@pytest.mark.asyncio
async def test_empty_brand_skips_check_daily_alert(registry):
    alerts = AlertRecorder()
    await _load(alerts)
    ctx = {"channel": "max", "bot_key": "max-geely"}
    await main_mod._brand_check(ctx, dealer(code="07", brand=""), "07ABCDEF")
    await main_mod._brand_check(ctx, dealer(code="07", brand=""), "07ABCDEF")
    empty = [a for a in alerts.calls if "не заполнен Brand" in a]
    assert len(empty) == 1                                # раз в сутки (R16)
    assert not [a for a in alerts.calls if "Несоответствие" in a]


@pytest.mark.asyncio
async def test_web_channel_not_checked(registry):
    alerts = AlertRecorder()
    await _load(alerts)
    await main_mod._brand_check({"channel": "web", "bot_key": "web"},
                                dealer(brand="Tank"), "10ABCDEF")
    assert alerts.calls == []                             # R6


@pytest.mark.asyncio
async def test_undelivered_alert_retries_next_event(registry):
    alerts = AlertRecorder(delivered=False)               # API вернул None
    await _load(alerts)
    ctx = {"channel": "max", "bot_key": "max-geely"}
    tank = dealer(code="10", brand="Tank")
    await main_mod._brand_check(ctx, tank, "10ABCDEF")
    await main_mod._brand_check(ctx, tank, "10ABCDEF")
    # доставка не подтверждена → кулдаун не зафиксирован → повторная попытка
    assert len([a for a in alerts.calls if "Несоответствие" in a]) == 2


@pytest.mark.asyncio
async def test_unknown_bot_or_empty_registry_no_crash(registry):
    alerts = AlertRecorder()
    await _load(alerts)
    await main_mod._brand_check({"channel": "max", "bot_key": "max-ghost"},
                                dealer(brand="Tank"), "10ABCDEF")
    bot_registry.reset_for_tests()
    await main_mod._brand_check({"channel": "max", "bot_key": "max-geely"},
                                dealer(brand="Tank"), "10ABCDEF")
    assert alerts.calls == []                             # сверять не с чем


@pytest.mark.asyncio
async def test_universal_test_bot_silent(db, monkeypatch):
    """U9: dev-бот «все марки» ('*') — сверка молчит и на несоответствии,
    и на пустом Brand (решение владельца 2026-09-22)."""
    bot_registry.reset_for_tests()
    monkeypatch.setenv("BOT_ENV", "dev")
    monkeypatch.setenv("MAX_BOT_TOKEN", "legacy-max-token-0001")
    monkeypatch.setenv("TG_BOT_TOKEN", "legacy-tg-token-0002")
    alerts = AlertRecorder()
    await _load(alerts, rows=[])
    ctx = {"channel": "max", "bot_key": "max-test"}
    await main_mod._brand_check(ctx, dealer(code="10", brand="Tank"), "10ABCDEF")
    await main_mod._brand_check(ctx, dealer(code="07", brand=""), "07ABCDEF")
    assert alerts.calls == []
    bot_registry.reset_for_tests()


def test_brand_check_wired_into_code_activation():
    """Вставка в точку активации кода (KTD-7) — fire-and-forget."""
    src = inspect.getsource(main_mod.process_input)
    assert "_spawn_bg(_brand_check(USER_CONTEXT[max_user_id], dealer_data, code_found))" in src


def test_dealer_loader_normalizes_brand():
    """load_data_from_nocodb кладёт нормализованный ключ бренда (R15)."""
    src = inspect.getsource(main_mod.load_data_from_nocodb)
    assert "\"brand\": str(row.get('Brand') or '').strip().lower()" in src
