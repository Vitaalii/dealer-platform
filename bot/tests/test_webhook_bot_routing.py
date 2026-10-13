"""U3 (план 2026-09-15-001): приём — URL на бота, проверка секрета, составной
внутренний ID, фиксация бота в ctx/users, поведение KTD-4 (200-дроп/503)."""
import asyncio
import json
import time

import pytest
from sqlalchemy import select

import bot_identity
import bot_registry
import database
import main as main_mod


class AlertRecorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, text):
        self.calls.append(text)
        return True


class FakeReq:
    def __init__(self, payload, bot_key=None, headers=None):
        self._payload = payload
        self.match_info = {"bot_key": bot_key} if bot_key else {}
        self.headers = headers or {}

    async def json(self):
        return self._payload


def max_message(user_id="123", text="привет"):
    return {"update_type": "message_created",
            "timestamp": time.time() * 1000,
            "message": {"sender": {"user_id": user_id, "name": "Тест"},
                        "body": {"text": text},
                        "recipient": {"chat_id": 555}}}


def tg_message(user_id="777", text="привет"):
    return {"message": {"from": {"id": int(user_id), "first_name": "Тест"},
                        "text": text}}


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
    """Реестр с легаси + пилотными ботами, загруженный из фейкового NocoDB."""
    bot_registry.reset_for_tests()
    monkeypatch.setenv("BOT_ENV", "dev")
    monkeypatch.setenv("MAX_BOT_TOKEN", "legacy-max-token-0001")
    monkeypatch.setenv("TG_BOT_TOKEN", "legacy-tg-token-0002")
    monkeypatch.setenv("BOT_TOKEN_MAX_HAVAL_T", "pilot-max-token-0003")
    monkeypatch.setenv("BOT_TOKEN_TG_HAVAL_T", "pilot-tg-token-0004")
    bot_registry.setup(alert=alerts, fetch_rows=rows_fetcher(PILOT_ROWS))
    # main держит собственную ссылку на AsyncSessionLocal (import-binding) —
    # переводим на per-test БД, как это делает _wire в test_n8n_status_gate
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)
    yield
    bot_registry.reset_for_tests()


@pytest.fixture
def capture_pi(monkeypatch):
    """Перехват process_input: хендлеры создают fire-and-forget задачу."""
    calls = []

    async def fake_pi(user_id, user_text, user_name, user_username,
                      input_type="text", channel="max", bot_key=None):
        calls.append({"user_id": user_id, "text": user_text, "channel": channel,
                      "bot_key": bot_key, "input_type": input_type})
    monkeypatch.setattr(main_mod, "process_input", fake_pi)
    return calls


async def _drain():
    for _ in range(5):
        await asyncio.sleep(0)


# =======================
# MAX: маршруты и секрет
# =======================

@pytest.mark.asyncio
async def test_new_max_route_builds_composite_id(loaded_registry, capture_pi):
    await bot_registry.load_bots_registry()
    resp = await main_mod.handle_max_webhook(
        FakeReq(max_message("123"), bot_key="max-haval"))
    await _drain()
    assert resp.status == 200
    assert capture_pi == [{"user_id": "max-haval:123", "text": "привет",
                           "channel": "max", "bot_key": "max-haval",
                           "input_type": "text"}]


@pytest.mark.asyncio
async def test_legacy_max_route_parity_bare_id(loaded_registry, capture_pi):
    await bot_registry.load_bots_registry()
    resp = await main_mod.handle_max_webhook(FakeReq(max_message("456")))
    await _drain()
    assert resp.status == 200
    assert capture_pi[0]["user_id"] == "456"            # голый ID (R7/KTD-5)
    assert capture_pi[0]["bot_key"] == "max-test"       # U9: легаси-ключ окружения (dev)


@pytest.mark.asyncio
async def test_max_deeplink_builds_start_text(loaded_registry, capture_pi):
    await bot_registry.load_bots_registry()
    payload = {"update_type": "bot_started",
               "timestamp": time.time() * 1000,
               "user": {"user_id": "321", "name": "Тест"},
               "payload": "99ABCDEF_Хавал_М6"}
    await main_mod.handle_max_webhook(FakeReq(payload, bot_key="max-haval"))
    await _drain()
    assert capture_pi[0]["user_id"] == "max-haval:321"
    assert capture_pi[0]["text"] == "start 99ABCDEF Хавал М6"
    assert capture_pi[0]["input_type"] == "deeplink"


@pytest.mark.asyncio
async def test_wrong_secret_dropped_with_deduped_alert(loaded_registry, capture_pi, alerts):
    await bot_registry.load_bots_registry()
    bot_registry.CACHED_BOTS["max-haval"].secret_enforced = True
    req = FakeReq(max_message(), bot_key="max-haval",
                  headers={"X-Max-Bot-Api-Secret": "wrong-secret-value"})
    resp = await main_mod.handle_max_webhook(req)
    await _drain()
    assert resp.status == 200                            # KTD-4: дроп, не 500/403
    assert capture_pi == []
    secret_alerts = [a for a in alerts.calls if "секрет" in a]
    assert len(secret_alerts) == 1
    # повторное событие — алерт в кулдауне не дублируется
    await main_mod.handle_max_webhook(req)
    await _drain()
    assert len([a for a in alerts.calls if "секрет" in a]) == 1


@pytest.mark.asyncio
async def test_missing_secret_dropped_when_enforced(loaded_registry, capture_pi, alerts):
    await bot_registry.load_bots_registry()
    bot_registry.CACHED_BOTS["max-haval"].secret_enforced = True
    resp = await main_mod.handle_max_webhook(
        FakeReq(max_message(), bot_key="max-haval"))
    await _drain()
    assert resp.status == 200 and capture_pi == []
    assert any("отсутствует" in a for a in alerts.calls)


@pytest.mark.asyncio
async def test_correct_secret_passes(loaded_registry, capture_pi):
    await bot_registry.load_bots_registry()
    entry = bot_registry.CACHED_BOTS["max-haval"]
    entry.secret_enforced = True
    resp = await main_mod.handle_max_webhook(
        FakeReq(max_message(), bot_key="max-haval",
                headers={"X-Max-Bot-Api-Secret": entry.webhook_secret()}))
    await _drain()
    assert resp.status == 200
    assert len(capture_pi) == 1


@pytest.mark.asyncio
async def test_unknown_bot_full_registry_200_drop(loaded_registry, capture_pi, alerts):
    await bot_registry.load_bots_registry()          # источник nocodb — полный
    resp = await main_mod.handle_max_webhook(
        FakeReq(max_message(), bot_key="max-ghost"))
    await _drain()
    assert resp.status == 200
    assert capture_pi == []
    assert any("max-ghost" in a for a in alerts.calls)


@pytest.mark.asyncio
async def test_unknown_bot_degraded_registry_503(db, monkeypatch, capture_pi, alerts):
    bot_registry.reset_for_tests()
    monkeypatch.setenv("MAX_BOT_TOKEN", "legacy-max-token-0001")
    monkeypatch.setenv("TG_BOT_TOKEN", "legacy-tg-token-0002")

    async def failing():
        raise RuntimeError("NocoDB down")
    bot_registry.setup(alert=alerts, fetch_rows=failing)
    await bot_registry.load_bots_registry()          # env-минимум → деградация
    assert bot_registry.is_degraded()
    resp = await main_mod.handle_max_webhook(
        FakeReq(max_message(), bot_key="max-haval"))
    assert resp.status == 503                        # платформа ретраит (KTD-4)
    assert capture_pi == []


@pytest.mark.asyncio
async def test_legacy_route_works_without_registry(capture_pi):
    bot_registry.reset_for_tests()                   # реестр пуст, источник none
    main_mod.USER_CONTEXT["9"] = {}                  # не ходить в БД за контекстом
    try:
        resp = await main_mod.handle_max_webhook(FakeReq(max_message("9")))
        await _drain()
        assert resp.status == 200
        assert capture_pi[0]["user_id"] == "9"       # R7: легаси живёт всегда
    finally:
        main_mod.USER_CONTEXT.pop("9", None)


@pytest.mark.asyncio
async def test_inactive_bot_event_still_processed(loaded_registry, capture_pi):
    """Д1: Active=✗ — полный пайплайн, блокируется только отправка (KTD-6)."""
    await bot_registry.load_bots_registry()
    bot_registry.CACHED_BOTS["max-haval"].active = False
    await main_mod.handle_max_webhook(FakeReq(max_message(), bot_key="max-haval"))
    await _drain()
    assert len(capture_pi) == 1


# =======================
# Telegram: составной ID, chat_id, контакт
# =======================

@pytest.mark.asyncio
async def test_tg_new_route_chat_id_from_raw(loaded_registry, capture_pi):
    await bot_registry.load_bots_registry()
    resp = await main_mod.handle_tg_webhook(
        FakeReq(tg_message("777"), bot_key="tg-haval"))
    await _drain()
    assert resp.status == 200
    assert capture_pi[0]["user_id"] == "tg-haval:777"
    assert capture_pi[0]["bot_key"] == "tg-haval"
    # chat_id — из сырого TG-ID (int составного упал бы 500-кой)
    assert main_mod.USER_CONTEXT["tg-haval:777"]["chat_id"] == 777
    main_mod.USER_CONTEXT.pop("tg-haval:777", None)


@pytest.mark.asyncio
async def test_tg_plain_text_full_path_fallback_via_brand_client(loaded_registry, monkeypatch):
    """Honest-repro инцидента дев-стенда 2026-09-17: «привет» новому TG-боту
    проходит ВЕСЬ путь (handle_tg_webhook → process_input → get_sender) и
    фолбэк уходит клиентом бота tg-haval. Тело — реальный формат Telegram
    (снято с панели ngrok)."""
    await bot_registry.load_bots_registry()
    brand_client = bot_registry.get_bot_client("tg-haval")
    sent = []

    async def fake_send(user_id=None, text=None, keyboard=None, attachments=None):
        sent.append({"user_id": user_id, "text": text})
        return {"ok": True}
    monkeypatch.setattr(brand_client, "send_message", fake_send)

    body = {"update_id": 280195612,
            "message": {"message_id": 2,
                        "from": {"id": 200376170, "is_bot": False,
                                 "first_name": "Vitaly", "username": "NVitalii"},
                        "chat": {"id": 200376170, "type": "private"},
                        "date": 1789622050, "text": "привет"}}
    resp = await main_mod.handle_tg_webhook(FakeReq(body, bot_key="tg-haval"))
    assert resp.status == 200
    # дождаться fire-and-forget process_input (реального, не мока)
    for _ in range(200):
        pending = [t for t in asyncio.all_tasks()
                   if t is not asyncio.current_task() and not t.done()]
        if not pending:
            break
        await asyncio.sleep(0.02)
    assert len(sent) == 1
    assert sent[0]["user_id"] == "200376170"          # сырой TG-ID
    assert "выберите" in sent[0]["text"].lower()      # фолбэк «залётного»
    main_mod.USER_CONTEXT.pop("tg-haval:200376170", None)


@pytest.mark.asyncio
async def test_tg_bare_start_deleted_and_routed_to_gate(loaded_registry, capture_pi, monkeypatch):
    """U7a: голый /start (реальное тело из ngrok) удаляется ботом-получателем
    (UX прод-3.1.2 сохранён) и уходит в единый гейт process_input как
    «start » deeplink — паритет с MAX bot_started без payload."""
    await bot_registry.load_bots_registry()
    brand_client = bot_registry.get_bot_client("tg-haval")
    deleted = []

    async def fake_delete(chat_id=None, message_id=None):
        deleted.append((chat_id, message_id))
    monkeypatch.setattr(brand_client, "delete_message", fake_delete)

    body = {"update_id": 280195611,
            "message": {"message_id": 1,
                        "from": {"id": 200376170, "is_bot": False,
                                 "first_name": "Vitaly", "username": "NVitalii"},
                        "chat": {"id": 200376170, "type": "private"},
                        "date": 1789622045, "text": "/start",
                        "entities": [{"offset": 0, "length": 6, "type": "bot_command"}]}}
    resp = await main_mod.handle_tg_webhook(FakeReq(body, bot_key="tg-haval"))
    await _drain()
    assert resp.status == 200
    assert deleted == [("200376170", 1)]               # /start удалён ботом-получателем
    assert capture_pi == [{"user_id": "tg-haval:200376170", "text": "start ",
                           "channel": "telegram", "bot_key": "tg-haval",
                           "input_type": "deeplink"}]
    main_mod.USER_CONTEXT.pop("tg-haval:200376170", None)


@pytest.mark.asyncio
async def test_tg_contact_first_event_full_identity(loaded_registry, capture_pi):
    """Контакт как первое событие: users-строка с полной идентичностью бота."""
    await bot_registry.load_bots_registry()
    payload = {"message": {"from": {"id": 314, "first_name": "Иван"},
                           "contact": {"phone_number": "+79001234567"}}}
    await main_mod.handle_tg_webhook(FakeReq(payload, bot_key="tg-haval"))
    await _drain()
    async with database.AsyncSessionLocal() as session:
        user = (await session.execute(
            select(database.User)
            .where(database.User.platform_user_id == "tg-haval:314"))).scalar_one()
    assert user.channel == "telegram"
    assert user.bot_key == "tg-haval"
    assert user.raw_platform_id == "314"
    assert user.phone == "+79001234567"
    main_mod.USER_CONTEXT.pop("tg-haval:314", None)


# =======================
# process_input: ctx/users получают идентичность
# =======================

@pytest.mark.asyncio
async def test_process_input_sets_ctx_and_user_identity(loaded_registry, monkeypatch):
    await bot_registry.load_bots_registry()
    sent = []

    async def fake_send(user_id=None, text=None, **kw):
        sent.append((user_id, text))
        return {"ok": True}
    monkeypatch.setattr(main_mod.max_client, "send_message", fake_send)

    uid = "max-haval:9001"
    main_mod.USER_CONTEXT.pop(uid, None)
    # «залётный» без диплинка: ранний return через fallback-текст
    await main_mod.process_input(uid, "привет", "Тест", "", channel="max",
                                 bot_key="max-haval")
    assert main_mod.USER_CONTEXT[uid]["bot_key"] == "max-haval"
    async with database.AsyncSessionLocal() as session:
        user = (await session.execute(
            select(database.User)
            .where(database.User.platform_user_id == uid))).scalar_one()
    assert user.bot_key == "max-haval" and user.raw_platform_id == "9001"
    main_mod.USER_CONTEXT.pop(uid, None)


@pytest.mark.asyncio
async def test_deeplink_context_reset_keeps_bot_key(loaded_registry, monkeypatch):
    """Пересоздание контекста при активации кода (категория А, main.py:2621)."""
    await bot_registry.load_bots_registry()
    main_mod.CACHED_DEALERS["99"] = {"code": "99", "dealer_id": "D99",
                                     "dealer_name": "Хавал Тест", "brand": "haval"}

    class Stop(Exception):
        pass

    async def stop_here(session_id):
        raise Stop()
    # стоп сразу ПОСЛЕ сброса контекста и save_context_to_db (main.py:2650)
    monkeypatch.setattr(main_mod, "reset_dialogflow_context", stop_here)

    uid = "max-haval:9002"
    main_mod.USER_CONTEXT.pop(uid, None)
    with pytest.raises(Stop):
        await main_mod.process_input(uid, "start 99ABCDEF Хавал", "Тест", "",
                                     input_type="deeplink", channel="max",
                                     bot_key="max-haval")
    assert main_mod.USER_CONTEXT[uid]["bot_key"] == "max-haval"
    assert main_mod.USER_CONTEXT[uid]["start_code"] == "99ABCDEF"
    async with database.AsyncSessionLocal() as session:
        user = (await session.execute(
            select(database.User)
            .where(database.User.platform_user_id == uid))).scalar_one()
    saved_ctx = json.loads(user.context_json)
    assert saved_ctx.get("bot_key") == "max-haval"
    main_mod.USER_CONTEXT.pop(uid, None)
    main_mod.CACHED_DEALERS.pop("99", None)
