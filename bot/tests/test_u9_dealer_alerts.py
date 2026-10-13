"""U9 (решения владельца 2026-09-22/23): алерты дилерам через MAX-бота БРЕНДА
дилера (фолбэка на чужой бренд НЕТ; отказ → дедуп-техалерт админам, заявка не
теряется), служебный бот техалертов (ALERT_BOT_KEY с фолбэками), find_brand_bot,
эндпоинт /bot-links (только brand/channel/link активных ботов)."""
import asyncio
import json

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


class FakeClient:
    """Отправитель дилер-алертов: записывает вызовы; delivered=False = отказ API."""

    def __init__(self, delivered=True):
        self.sent = []
        self.delivered = delivered

    async def send_alert_to_group(self, group_id, text):
        self.sent.append((group_id, text))
        return {"ok": True} if self.delivered else None


def rows_fetcher(rows):
    async def fetch():
        return rows
    return fetch


BRAND_ROWS = [
    {"Bot_Key": "max-geely", "Channel": "max", "Brands": "geely,belgee",
     "Token_Env": "MAX_BOT_TOKEN", "Environment": "all", "Active": True,
     "Link": "https://max.ru/geelybot"},
    {"Bot_Key": "max-haval", "Channel": "max", "Brands": "haval",
     "Token_Env": "BOT_TOKEN_MAX_HAVAL_T", "Environment": "all", "Active": True,
     "Link": "https://max.ru/havalbot"},
    {"Bot_Key": "tg-haval", "Channel": "telegram", "Brands": "haval",
     "Token_Env": "BOT_TOKEN_TG_HAVAL_T", "Environment": "all", "Active": True},
    {"Bot_Key": "max-off", "Channel": "max", "Brands": "tank",
     "Token_Env": "BOT_TOKEN_MAX_HAVAL_T", "Environment": "all", "Active": False,
     "Link": "https://max.ru/offbot"},
]


@pytest.fixture
def registry(db, monkeypatch):
    bot_registry.reset_for_tests()
    monkeypatch.setenv("BOT_ENV", "prod")
    monkeypatch.setenv("MAX_BOT_TOKEN", "legacy-max-token-0001")
    monkeypatch.setenv("TG_BOT_TOKEN", "legacy-tg-token-0002")
    monkeypatch.setenv("BOT_TOKEN_MAX_HAVAL_T", "pilot-max-token-0003")
    monkeypatch.setenv("BOT_TOKEN_TG_HAVAL_T", "pilot-tg-token-0004")
    yield
    bot_registry.reset_for_tests()


async def _load(alerts=None, rows=BRAND_ROWS):
    bot_registry.setup(alert=alerts or AlertRecorder(),
                       fetch_rows=rows_fetcher(rows))
    await bot_registry.load_bots_registry()


def _fake_bot(bot_key, delivered=True):
    """Подмена клиента бота в кэше реестра (get_bot_client берёт из _CLIENTS)."""
    client = FakeClient(delivered)
    bot_registry._CLIENTS[bot_key] = client
    return client


async def _drain():
    for _ in range(20):
        await asyncio.sleep(0)


# =======================
# find_brand_bot
# =======================

@pytest.mark.asyncio
async def test_find_brand_bot_matches_and_universal(registry):
    await _load()
    _, key = bot_registry.find_brand_bot("max", "haval")
    assert key == "max-haval"
    _, key = bot_registry.find_brand_bot("max", "geely")
    assert key == "max-geely"
    # канал учитывается
    _, key = bot_registry.find_brand_bot("telegram", "haval")
    assert key == "tg-haval"
    # неизвестный бренд / неактивный бот / пустой бренд → (None, None)
    assert bot_registry.find_brand_bot("max", "tank") == (None, None)
    assert bot_registry.find_brand_bot("max", "") == (None, None)


@pytest.mark.asyncio
async def test_find_brand_bot_universal_star(db, monkeypatch):
    monkeypatch.setenv("BOT_ENV", "dev")
    monkeypatch.setenv("MAX_BOT_TOKEN", "legacy-max-token-0001")
    monkeypatch.setenv("TG_BOT_TOKEN", "legacy-tg-token-0002")
    bot_registry.reset_for_tests()
    await _load(rows=[])                                  # env-минимум: max-test '*'
    _, key = bot_registry.find_brand_bot("max", "haval")
    assert key == "max-test"                              # '*' обслуживает любой бренд
    bot_registry.reset_for_tests()


# =======================
# _send_dealer_group_alert: бот бренда, без фолбэка на чужой бренд
# =======================

@pytest.mark.asyncio
async def test_dealer_alert_goes_via_brand_bot(registry):
    alerts = AlertRecorder()
    await _load(alerts)
    haval_bot = _fake_bot("max-haval")
    geely_bot = _fake_bot("max-geely")
    dealer = {"Max_Group_ID": "g-99", "brand": "haval"}
    ok = await main_mod._send_dealer_group_alert(dealer, "99", "Карточка", deal_id="D-1")
    await _drain()
    assert ok is True
    assert haval_bot.sent == [("g-99", "Карточка")]
    assert geely_bot.sent == []                           # чужой бренд не задет
    assert alerts.calls == []                             # техалертов нет


@pytest.mark.asyncio
async def test_dealer_alert_no_brand_bot_tech_alert_no_fallback(registry):
    """Бренд без бота: дилеру НИЧЕГО не уходит (от бота Geely — недопустимо),
    админам — техалерт с дилером/брендом/сделкой; вызов возвращает False,
    но заявка выше по стеку продолжает жить (R14)."""
    alerts = AlertRecorder()
    await _load(alerts)
    geely_bot = _fake_bot("max-geely")
    dealer = {"Max_Group_ID": "g-77", "brand": "tank"}
    ok = await main_mod._send_dealer_group_alert(dealer, "77", "Карточка", deal_id="D-2")
    await _drain()
    assert ok is False
    assert geely_bot.sent == []                           # фолбэка НЕТ
    assert len(alerts.calls) == 1
    assert "77" in alerts.calls[0] and "tank" in alerts.calls[0]
    assert "D-2" in alerts.calls[0]
    # дедуп: повтор той же пары дилер+бот в кулдауне не спамит
    await main_mod._send_dealer_group_alert(dealer, "77", "Карточка", deal_id="D-2")
    await _drain()
    assert len(alerts.calls) == 1


@pytest.mark.asyncio
async def test_dealer_alert_api_failure_tech_alert(registry):
    alerts = AlertRecorder()
    await _load(alerts)
    haval_bot = _fake_bot("max-haval", delivered=False)   # MAX API вернул None
    dealer = {"Max_Group_ID": "g-88", "brand": "haval"}
    ok = await main_mod._send_dealer_group_alert(dealer, "88", "Карточка")
    await _drain()
    assert ok is False
    assert haval_bot.sent == [("g-88", "Карточка")]       # попытка была
    assert len(alerts.calls) == 1
    assert "max-haval" in alerts.calls[0] and "88" in alerts.calls[0]


@pytest.mark.asyncio
async def test_dealer_without_group_goes_to_support_via_service_bot(registry, monkeypatch):
    alerts = AlertRecorder()
    await _load(alerts)
    monkeypatch.setattr(main_mod, "SUPPORT_GROUP_ID", "support-1")
    service_bot = _fake_bot("max-geely")                  # легаси-ключ prod = служебный дефолт
    ok = await main_mod._send_dealer_group_alert({"brand": "haval"}, "66", "Карточка")
    await _drain()
    assert ok is True
    assert service_bot.sent == [("support-1", "Карточка")]
    assert alerts.calls == []


# =======================
# service_alert_client (ALERT_BOT_KEY с фолбэками)
# =======================

@pytest.mark.asyncio
async def test_service_alert_client_explicit_key(registry, monkeypatch):
    await _load()
    haval_bot = _fake_bot("max-haval")
    monkeypatch.setenv("ALERT_BOT_KEY", "max-haval")
    assert main_mod.service_alert_client() is haval_bot


@pytest.mark.asyncio
async def test_service_alert_client_default_legacy_then_global(registry, monkeypatch):
    await _load()
    monkeypatch.delenv("ALERT_BOT_KEY", raising=False)
    legacy_bot = _fake_bot("max-geely")                   # легаси-ключ prod
    assert main_mod.service_alert_client() is legacy_bot
    # реестр пуст → финальный фолбэк: глобальный max_client (env-токен)
    bot_registry.reset_for_tests()
    assert main_mod.service_alert_client() is main_mod.max_client


# =======================
# /bot-links
# =======================

@pytest.mark.asyncio
async def test_bot_links_only_active_with_link_no_secrets(registry):
    await _load()
    resp = await main_mod.handle_bot_links(object())
    data = json.loads(resp.body)
    bots = data["bots"]
    # только активные с заполненной ссылкой; tg-haval (без Link) и
    # max-off (Active=False) не отдаются
    assert [b["link"] for b in bots] == ["https://max.ru/geelybot",
                                         "https://max.ru/havalbot"]
    for b in bots:
        assert set(b) == {"channel", "brands", "link"}    # ни Token_Env, ни Environment
    assert bots[0]["brands"] == ["belgee", "geely"]
    assert bots[0]["channel"] == "max"


@pytest.mark.asyncio
async def test_bot_links_route_registered():
    import inspect
    src = inspect.getsource(main_mod.init_app)
    assert "add_get('/bot-links', handle_bot_links)" in src
    assert "'OPTIONS', '/bot-links'" in src
