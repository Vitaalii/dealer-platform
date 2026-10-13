"""U7a (план 2026-09-15-001): единое поведение «Старт без диплинка» для MAX и
TG — фолбэк для нового/закрытого, повтор карточки для незакрытой заявки,
анти-дубль окно 120с, TTL-закрытие как финализация. Решение — только по ctx,
без Dialogflow; сделки/алерты/архив на голом старте не создаются."""
import asyncio
import inspect
import json
import time

import pytest
from sqlalchemy import select

import bot_registry
import database
import main as main_mod
from database import User


class AlertRecorder:
    async def __call__(self, text):
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


class Stop(Exception):
    """Маркер «конвейер дошёл до Dialogflow» (повтор карточки)."""


@pytest.fixture
def env(db, monkeypatch):
    bot_registry.reset_for_tests()
    monkeypatch.setenv("BOT_ENV", "dev")
    monkeypatch.setenv("MAX_BOT_TOKEN", "legacy-max-token-0001")
    monkeypatch.setenv("TG_BOT_TOKEN", "legacy-tg-token-0002")
    monkeypatch.setenv("BOT_TOKEN_MAX_HAVAL_T", "pilot-max-token-0003")
    monkeypatch.setenv("BOT_TOKEN_TG_HAVAL_T", "pilot-tg-token-0004")
    bot_registry.setup(alert=AlertRecorder(), fetch_rows=rows_fetcher(PILOT_ROWS))
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)

    async def df_stop(session_id, text, language_code="ru"):
        raise Stop()
    monkeypatch.setattr(main_mod, "get_dialogflow_response", df_stop)

    async def df_reset(session_id):
        pass
    monkeypatch.setattr(main_mod, "reset_dialogflow_context", df_reset)
    yield
    bot_registry.reset_for_tests()


async def _seed(uid, ctx, channel="max", bot_key=None):
    async with database.AsyncSessionLocal() as session:
        session.add(User(platform_user_id=uid, channel=channel, bot_key=bot_key,
                         context_json=json.dumps(ctx, ensure_ascii=False)))
        await session.commit()
    main_mod.USER_CONTEXT.pop(uid, None)


def _capture_send(monkeypatch, client):
    sent = []

    async def fake_send(user_id=None, text=None, keyboard=None, attachments=None):
        sent.append({"user_id": user_id, "text": text})
        return {"ok": True}
    monkeypatch.setattr(client, "send_message", fake_send)
    return sent


async def _bare_start(uid, channel="max", bot_key=None):
    await main_mod.process_input(uid, "start ", "Тест", "",
                                 input_type="deeplink", channel=channel,
                                 bot_key=bot_key)


OLD = time.time() - 3600  # активность заведомо вне окна 120с


# =======================
# Новый пользователь / пустой контекст → фолбэк (без DF)
# =======================

@pytest.mark.asyncio
@pytest.mark.parametrize("uid,channel,bot_key,client_key", [
    ("max-haval:9101", "max", "max-haval", "max-haval"),        # составной ID
    ("9102", "max", None, None),                                 # легаси ID
    ("tg-haval:9103", "telegram", "tg-haval", "tg-haval"),
])
async def test_new_user_gets_fallback_without_df(env, monkeypatch, uid, channel, bot_key, client_key):
    await bot_registry.load_bots_registry()
    client = (bot_registry.get_bot_client(client_key) if client_key
              else main_mod.get_client(channel))
    sent = _capture_send(monkeypatch, client)
    await _bare_start(uid, channel, bot_key)          # Stop НЕ поднят — DF не звали
    assert len(sent) == 1
    assert "выберите интересующий вас автомобиль" in sent[0]["text"]
    main_mod.USER_CONTEXT.pop(uid, None)


# =======================
# Незакрытая заявка → повтор карточки штатным конвейером (DF)
# =======================

@pytest.mark.asyncio
@pytest.mark.parametrize("uid,channel,bot_key", [
    ("max-haval:9111", "max", "max-haval"),
    ("9112", "max", None),                             # легаси ID
    ("tg-haval:9113", "telegram", "tg-haval"),
])
async def test_open_deal_repeats_card_via_pipeline(env, uid, channel, bot_key):
    await bot_registry.load_bots_registry()
    await _seed(uid, {"start_code": "99ABCDEF", "chat_history": [],
                      "channel": channel, "last_interaction_time": OLD},
                channel=channel, bot_key=bot_key)
    with pytest.raises(Stop):                          # дошли до DF → карточка
        await _bare_start(uid, channel, bot_key)
    main_mod.USER_CONTEXT.pop(uid, None)


# =======================
# Закрытая заявка (флаги/TTL) → фолбэк, карточки нет
# =======================

@pytest.mark.asyncio
@pytest.mark.parametrize("closed_flag", ["is_finalized", "deal_finalized"])
@pytest.mark.parametrize("uid,channel,bot_key,client_key", [
    ("max-haval:9121", "max", "max-haval", "max-haval"),
    ("9122", "max", None, None),
])
async def test_closed_deal_gets_fallback(env, monkeypatch, closed_flag, uid, channel, bot_key, client_key):
    await bot_registry.load_bots_registry()
    client = (bot_registry.get_bot_client(client_key) if client_key
              else main_mod.get_client(channel))
    sent = _capture_send(monkeypatch, client)
    await _seed(uid, {"start_code": "99ABCDEF", "chat_history": [],
                      "channel": channel, "last_interaction_time": OLD,
                      closed_flag: True},
                channel=channel, bot_key=bot_key)
    await _bare_start(uid, channel, bot_key)          # Stop НЕ поднят — DF не звали
    assert len(sent) == 1
    assert "выберите интересующий вас автомобиль" in sent[0]["text"]
    main_mod.USER_CONTEXT.pop(uid, None)


# =======================
# Анти-дубль окно 120с
# =======================

@pytest.mark.asyncio
async def test_window_silences_repeats_then_rule_applies(env, monkeypatch):
    await bot_registry.load_bots_registry()
    client = bot_registry.get_bot_client("max-haval")
    sent = _capture_send(monkeypatch, client)
    uid = "max-haval:9131"
    await _seed(uid, {"start_code": "99ABCDEF", "chat_history": [],
                      "channel": "max",
                      "last_interaction_time": time.time() - 10},
                bot_key="max-haval")
    # внутри окна: молчание (ни фолбэка, ни DF), дважды подряд
    await _bare_start(uid, "max", "max-haval")
    await _bare_start(uid, "max", "max-haval")
    assert sent == []
    # серия «Старт» сама продлевает окно (активность бота-получателя):
    # значение last_interaction_time обновлено самим входом
    assert main_mod.USER_CONTEXT[uid]["last_interaction_time"] > time.time() - 5
    # пауза «прошла» (правим снимок активности) → правило: незакрытая → карточка
    main_mod.USER_CONTEXT[uid]["last_interaction_time"] = OLD
    with pytest.raises(Stop):
        await _bare_start(uid, "max", "max-haval")
    main_mod.USER_CONTEXT.pop(uid, None)


# =======================
# Диплинк с payload не меняется; TTL ставит финализацию
# =======================

@pytest.mark.asyncio
async def test_deeplink_with_payload_bypasses_gate(env):
    """«start 99ABCDEF Хавал» гейтом не перехватывается — активация кода
    (сброс контекста) идёт как раньше и доходит до DF-конвейера."""
    await bot_registry.load_bots_registry()
    main_mod.CACHED_DEALERS["99"] = {"code": "99", "dealer_id": "D99",
                                     "dealer_name": "Тест", "brand": "haval"}
    uid = "max-haval:9141"
    with pytest.raises(Stop):
        await main_mod.process_input(uid, "start 99ABCDEF Хавал", "Тест", "",
                                     input_type="deeplink", channel="max",
                                     bot_key="max-haval")
    assert main_mod.USER_CONTEXT[uid]["start_code"] == "99ABCDEF"
    main_mod.USER_CONTEXT.pop(uid, None)
    main_mod.CACHED_DEALERS.pop("99", None)


def test_ttl_close_marks_finalized():
    """U7a: Логика Б (TTL 48ч) выставляет is_finalized — гейт голого старта
    видит TTL-закрытую заявку как закрытую."""
    src = inspect.getsource(main_mod.sync_worker)
    ttl_block = src.split("ЛОГИКА Б")[1]
    assert "ctx['is_finalized'] = True" in ttl_block
