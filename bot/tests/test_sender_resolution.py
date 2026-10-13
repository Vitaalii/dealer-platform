"""U4 (план 2026-09-15-001): выбор отправителя по боту сессии — get_sender
(KTD-6), паритет легаси-путей, политика R14 (не отправлять, не падать),
grep-стражи на прямые вызовы TG API и расползание get_client."""
import asyncio
import pathlib

import pytest

import bot_identity
import bot_registry
import database
import main as main_mod

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"


class AlertRecorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, text):
        self.calls.append(text)
        return True


def rows_fetcher(rows):
    async def fetch():
        return rows
    return fetch


PILOT_ROWS = [
    {"Bot_Key": "max-haval", "Channel": "max", "Brands": "haval",
     "Token_Env": "BOT_TOKEN_MAX_HAVAL_T", "Environment": "dev", "Active": True},
    {"Bot_Key": "tg-haval", "Channel": "telegram", "Brands": "haval",
     "Token_Env": "BOT_TOKEN_TG_HAVAL_T", "Environment": "dev", "Active": True},
]


@pytest.fixture
def alerts():
    return AlertRecorder()


@pytest.fixture
def loaded_registry(db, monkeypatch, alerts):
    bot_registry.reset_for_tests()
    monkeypatch.setenv("BOT_ENV", "dev")
    monkeypatch.setenv("MAX_BOT_TOKEN", "legacy-max-token-0001")
    monkeypatch.setenv("TG_BOT_TOKEN", "legacy-tg-token-0002")
    monkeypatch.setenv("BOT_TOKEN_MAX_HAVAL_T", "pilot-max-token-0003")
    monkeypatch.setenv("BOT_TOKEN_TG_HAVAL_T", "pilot-tg-token-0004")
    bot_registry.setup(alert=alerts, fetch_rows=rows_fetcher(PILOT_ROWS))
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)
    yield
    bot_registry.reset_for_tests()


# =======================
# get_sender: резолв
# =======================

@pytest.mark.asyncio
async def test_legacy_parity_same_client_same_id(loaded_registry):
    await bot_registry.load_bots_registry()
    cl, raw = main_mod.get_sender("456", {"channel": "max"})
    assert cl is main_mod.max_client and raw == "456"      # паритет (R7)
    cl, raw = main_mod.get_sender("456", {"channel": "telegram"})
    assert cl is main_mod.tg_client and raw == "456"
    cl, raw = main_mod.get_sender("sess-1", {"channel": "web"})
    assert cl is main_mod.web_client and raw == "sess-1"   # Р6: web не меняется


@pytest.mark.asyncio
async def test_brand_bot_resolved_from_ctx(loaded_registry):
    await bot_registry.load_bots_registry()
    cl, raw = main_mod.get_sender("max-haval:123",
                                  {"channel": "max", "bot_key": "max-haval"})
    assert cl is bot_registry.get_bot_client("max-haval")
    assert cl is not main_mod.max_client
    assert raw == "123"                                    # сырой ID для API


@pytest.mark.asyncio
async def test_brand_bot_self_healing_from_composite_id(loaded_registry):
    """Запись с потерянным bot_key: parse составного ID до легаси-фолбэка."""
    await bot_registry.load_bots_registry()
    cl, raw = main_mod.get_sender("tg-haval:777", {"channel": "telegram"})
    assert cl is bot_registry.get_bot_client("tg-haval")
    assert raw == "777"


@pytest.mark.asyncio
async def test_disabled_bot_returns_none_with_deduped_alert(loaded_registry, alerts):
    await bot_registry.load_bots_registry()
    bot_registry.CACHED_BOTS["max-haval"].active = False
    ctx = {"channel": "max", "bot_key": "max-haval",
           "start_code": "99ABCDEF", "deal_id": "D-42"}
    cl, raw = main_mod.get_sender("max-haval:123", ctx)
    assert cl is None and raw == "123"                     # R14: не шлём, не падаем
    for _ in range(5):
        await asyncio.sleep(0)
    blocked = [a for a in alerts.calls if "max-haval" in a and "вручную" in a]
    assert len(blocked) == 1
    assert "99" in blocked[0] and "D-42" in blocked[0]     # дилер и сделка в алерте
    # повторный резолв — алерт в кулдауне
    main_mod.get_sender("max-haval:123", ctx)
    for _ in range(5):
        await asyncio.sleep(0)
    assert len([a for a in alerts.calls if "вручную" in a]) == 1


@pytest.mark.asyncio
async def test_send_smartbot_reply_blocked_returns_false(loaded_registry):
    await bot_registry.load_bots_registry()
    bot_registry.CACHED_BOTS["max-haval"].active = False
    uid = "max-haval:555"
    main_mod.USER_CONTEXT[uid] = {"channel": "max", "bot_key": "max-haval",
                                  "chat_history": []}
    try:
        ok = await main_mod.send_smartbot_reply(uid, {"text": "Привет"})
        assert ok is False                                  # доставки не было
    finally:
        main_mod.USER_CONTEXT.pop(uid, None)


@pytest.mark.asyncio
async def test_send_smartbot_reply_uses_brand_client_and_raw_id(loaded_registry, monkeypatch):
    await bot_registry.load_bots_registry()
    uid = "max-haval:556"
    main_mod.USER_CONTEXT[uid] = {"channel": "max", "bot_key": "max-haval",
                                  "chat_history": []}
    brand_client = bot_registry.get_bot_client("max-haval")
    sent = []

    async def fake_send(user_id=None, text=None, keyboard=None, attachments=None):
        sent.append({"user_id": user_id, "text": text})
        return {"ok": True}
    monkeypatch.setattr(brand_client, "send_message", fake_send)
    try:
        ok = await main_mod.send_smartbot_reply(uid, {"text": "Привет [SPLIT] Ещё"})
        assert ok is True
        assert len(sent) == 2                               # [SPLIT] через того же бота
        assert all(s["user_id"] == "556" for s in sent)     # сырой ID, не составной
    finally:
        main_mod.USER_CONTEXT.pop(uid, None)


@pytest.mark.asyncio
async def test_typing_helper_uses_brand_client(loaded_registry, monkeypatch):
    await bot_registry.load_bots_registry()
    brand_client = bot_registry.get_bot_client("tg-haval")
    typed = []

    async def fake_typing(chat_id=None):
        typed.append(chat_id)
    monkeypatch.setattr(brand_client, "send_typing", fake_typing)
    await main_mod._send_typing_for({"channel": "telegram", "bot_key": "tg-haval"}, 777)
    assert typed == [777]


@pytest.mark.asyncio
async def test_tg_service_calls_have_bot_token_in_url(loaded_registry):
    """Служебные вызовы TG (answer_callback/delete_message/remove_keyboard)
    идут клиентом бота-получателя — токен в BASE_URL клиента."""
    await bot_registry.load_bots_registry()
    brand = bot_registry.get_bot_client("tg-haval")
    assert "pilot-tg-token-0004" in brand.BASE_URL
    assert hasattr(brand, "answer_callback") and hasattr(brand, "delete_message")


# =======================
# Grep-стражи (категория А закрыта)
# =======================

def test_no_direct_tg_api_calls_left_in_main():
    src = MAIN_PY.read_text(encoding="utf-8")
    assert "api.telegram.org/bot{os.getenv" not in src


def test_get_client_confined_to_definition_and_get_sender():
    """get_client(channel) остаётся только определением и двумя вызовами
    внутри get_sender (web-ветка + легаси-фолбэк, KTD-6) — расползание по
    точкам отправки закрыто."""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert src.count("get_client(") == 3
