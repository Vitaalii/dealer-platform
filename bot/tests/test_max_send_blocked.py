"""План 2026-10-12-002 (v3.4.3), часть C: закрытый диалог MAX и напоминания.

Бой 11–12.10: клиент приостановил диалог, MAX на каждую отправку отвечал 403
{'code': 'chat.denied', 'message': 'Key: error.dialog.suspended …'}; первое
напоминание каждый тик заново звало n8n и трижды стучалось в MAX — ≈ 660
вызовов n8n в сутки на одного клиента."""
import asyncio
import json
import time
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select

import cabinet_service
import database
import main as main_mod
from database import User
from max_client import MaxBotClient

UID = "16266646"
DENIED = {"code": "chat.denied", "message": "Key: error.dialog.suspended, args: [16266646,]."}


class FakeMax:
    """MAX API: ответы на POST /messages по сценарию (статус, тело)."""

    def __init__(self, script):
        self.script = list(script)
        self.posts = []

    async def handler(self, request):
        body = await request.json()
        self.posts.append(body)
        status, payload = self.script.pop(0) if self.script else (200, {"message": {"id": 1}})
        return web.json_response(payload, status=status)


@pytest_asyncio.fixture
async def max_api(monkeypatch):
    servers = []

    async def make(script=()):
        fake = FakeMax(script)
        app = web.Application()
        app.router.add_post("/messages", fake.handler)
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        client = MaxBotClient(token="t")
        client.base_url = f"http://{server.host}:{server.port}"
        client.supports_reminders = lambda: True
        monkeypatch.setattr(main_mod, "get_sender", lambda *a, **k: (client, UID))
        return fake

    real_sleep = asyncio.sleep

    async def fast_sleep(seconds, *a, **k):
        return await real_sleep(0)

    monkeypatch.setattr(main_mod.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)
    yield make
    monkeypatch.setattr(main_mod.asyncio, "sleep", real_sleep)
    for s in servers:
        await s.close()


async def _user(db, ctx_extra=None, silent_for=3600):
    ctx = {"channel": "max", "bot_key": "max-geely", "start_code": "99141611BY",
           "client_last_message_time": time.time() - silent_for, "reminder_level": 0,
           "chat_history": [{"time": "10:00", "role": "bot", "text": "Когда удобно приехать?"}]}
    ctx.update(ctx_extra or {})
    async with db.AsyncSessionLocal() as session:
        session.add(User(platform_user_id=UID, channel="max", full_name="Клиент",
                         is_smartbot_active=True, context_json=json.dumps(ctx, ensure_ascii=False)))
        await session.commit()
    main_mod.USER_CONTEXT.pop(UID, None)


async def _ctx():
    async with database.AsyncSessionLocal() as session:
        row = (await session.execute(select(User).where(User.platform_user_id == UID))).scalar_one()
    return json.loads(row.context_json)


def _n8n(monkeypatch, texts=None):
    calls = []
    texts = list(texts or [])

    async def fake(uid, text, name, ctx):
        calls.append(text)
        return {"text": texts.pop(0) if texts else f"Напоминание {len(calls)}"}

    monkeypatch.setattr(main_mod, "send_chat_message_to_n8n", fake)
    return calls


async def _ticks(monkeypatch, n=1):
    """n проходов reminder_worker: сон 60 с пропускается, (n+1)-й — остановка."""
    hour_offset = 12 - datetime.now(timezone.utc).hour           # у дилера полдень — не тихие часы
    monkeypatch.setitem(main_mod.CACHED_DEALERS, "99", {"UTC_Offset": hour_offset})

    async def not_suppressed(user_pk):
        return False

    monkeypatch.setattr(cabinet_service, "reminders_suppressed", not_suppressed)
    count = {"n": 0}
    patched = main_mod.asyncio.sleep

    async def tick_sleep(seconds, *a, **k):
        if seconds == 60:
            count["n"] += 1
            if count["n"] > n:
                raise asyncio.CancelledError()
        return await patched(0)

    monkeypatch.setattr(main_mod.asyncio, "sleep", tick_sleep)
    await main_mod.reminder_worker()
    monkeypatch.setattr(main_mod.asyncio, "sleep", patched)


# ---------------- U6: окончательные ошибки MAX ----------------

@pytest.mark.asyncio
async def test_client_classifies_403_chat_denied_permanent(max_api):
    await max_api([(403, DENIED)])
    client, _ = main_mod.get_sender(UID, {})
    res, err = await client.send_message_detailed(UID, "x")
    assert res is None and err == {"kind": "permanent", "status": 403, "code": "chat.denied"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404])
async def test_permanent_error_one_attempt_and_mark(db, max_api, status):
    fake = await max_api([(status, DENIED)] * 3)
    await _user(db)
    ok = await main_mod.send_smartbot_reply(UID, {"text": "Добрый день"})
    assert ok is False and len(fake.posts) == 1               # без повторов
    mark = (await _ctx())[main_mod.MAX_SEND_BLOCKED_KEY]
    assert mark["status"] == status


@pytest.mark.asyncio
async def test_temporary_error_three_attempts_no_mark(db, max_api):
    fake = await max_api([(500, {"code": "internal"})] * 3)
    await _user(db)
    assert await main_mod.send_smartbot_reply(UID, {"text": "Добрый день"}) is False
    assert len(fake.posts) == 3
    assert "max_send_blocked" not in await _ctx()             # регресс: временная — как раньше


@pytest.mark.asyncio
async def test_content_error_one_attempt_no_mark(db, max_api):
    fake = await max_api([(400, {"code": "proto.payload"})] * 3)
    await _user(db)
    assert await main_mod.send_smartbot_reply(UID, {"text": "Добрый день"}) is False
    assert len(fake.posts) == 1
    assert main_mod.MAX_SEND_BLOCKED_KEY not in await _ctx()


@pytest.mark.asyncio
async def test_reminder_tick_for_blocked_client_no_n8n_no_max(db, max_api, monkeypatch):
    fake = await max_api()
    await _user(db, {main_mod.MAX_SEND_BLOCKED_KEY: {"since": 1, "status": 403, "code": "chat.denied"}})
    calls = _n8n(monkeypatch)
    await _ticks(monkeypatch, 2)
    assert calls == [] and fake.posts == []


@pytest.mark.asyncio
async def test_reminder2_for_blocked_client_not_sent(db, max_api, monkeypatch):
    fake = await max_api()
    await _user(db, {main_mod.MAX_SEND_BLOCKED_KEY: {"since": 1}, "reminder_level": 1,
                     "reminder_1_time": time.time() - 86400})
    _n8n(monkeypatch)
    await _ticks(monkeypatch, 1)
    assert fake.posts == []


@pytest.mark.asyncio
async def test_suspended_dialog_loop_stops_after_first_tick(db, max_api, monkeypatch):
    """Случай 16266646: 403 chat.denied на каждую отправку → один вызов n8n и
    одна отправка за 3 тика (на v3.4.2 — 3 вызова n8n и 9 отправок)."""
    fake = await max_api([(403, DENIED)] * 20)
    await _user(db)
    calls = _n8n(monkeypatch)
    await _ticks(monkeypatch, 3)
    assert len(calls) == 1 and len(fake.posts) == 1
    assert (await _ctx())[main_mod.MAX_SEND_BLOCKED_KEY]["code"] == "chat.denied"


@pytest.mark.asyncio
async def test_client_message_clears_mark(db, max_api, monkeypatch):
    """Клиент снова пишет → пометка снята, reminder_level = 0."""
    await max_api()
    await _user(db, {main_mod.MAX_SEND_BLOCKED_KEY: {"since": 1}, "reminder_level": 2,
                     "reminder_1_pending": {"text": "x", "at": 1}, "reminder_1_failures": 2})
    seen = {}

    async def stop_after_reset(*a, **k):
        raise RuntimeError("стоп после сброса таймеров")

    real_save = main_mod.save_context_to_db

    async def spy_save(user_id, ctx):
        seen.update(ctx)
        return await real_save(user_id, ctx)

    monkeypatch.setattr(main_mod, "save_context_to_db", spy_save)
    monkeypatch.setattr(main_mod, "send_chat_message_to_n8n", stop_after_reset)
    try:
        await main_mod.process_input(UID, "Здравствуйте, я вернулся", "Клиент", "", channel="max",
                                     bot_key="max-geely")
    except Exception:
        pass
    ctx = main_mod.USER_CONTEXT.get(UID) or seen
    assert main_mod.MAX_SEND_BLOCKED_KEY not in ctx
    assert ctx.get("reminder_level") == 0
    assert "reminder_1_pending" not in ctx and "reminder_1_failures" not in ctx
    main_mod.USER_CONTEXT.pop(UID, None)


@pytest.mark.asyncio
async def test_delivered_reminder_unchanged(db, max_api, monkeypatch):
    """Регресс: доставка → reminder_level = 1, один вызов n8n, одна отправка."""
    fake = await max_api()
    await _user(db)
    calls = _n8n(monkeypatch)
    await _ticks(monkeypatch, 2)
    ctx = await _ctx()
    assert len(calls) == 1 and len(fake.posts) == 1 and ctx["reminder_level"] == 1


# ---------------- U7: потолок, кэш текста, защита от разметки ----------------

@pytest.mark.asyncio
async def test_temporary_failure_reuses_cached_text(db, max_api, monkeypatch):
    """Временная ошибка на тике 1 → тик 2 шлёт тот же текст из кэша, n8n — один раз."""
    fake = await max_api([(500, {})] * 3 + [(200, {"message": {"id": 1}})])
    await _user(db)
    calls = _n8n(monkeypatch, ["Подскажите, когда удобно приехать?"])
    await _ticks(monkeypatch, 2)
    assert len(calls) == 1
    texts = [p["text"] for p in fake.posts]
    assert len(texts) == 4 and len(set(texts)) == 1
    ctx = await _ctx()
    assert ctx["reminder_level"] == 1
    assert "reminder_1_pending" not in ctx and "reminder_1_failures" not in ctx


@pytest.mark.asyncio
async def test_three_failures_mute_reminder(db, max_api, monkeypatch):
    """Три неудачных тика → reminder_level = 2, четвёртый тик ничего не делает
    (на v3.4.2 — бесконечно)."""
    fake = await max_api([(500, {})] * 30)
    await _user(db)
    calls = _n8n(monkeypatch)
    await _ticks(monkeypatch, 5)
    assert len(calls) == 1 and len(fake.posts) == 9          # 3 тика × 3 попытки
    assert (await _ctx())["reminder_level"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "**Если клиенту выдана ссылка на личный кабинет…**\nНапомните о кабинете",
    "Подскажите, удобно ли завтра?\n---\nЕсли это обычная ситуация — напомни",
    "# Инструкция\nНапомни клиенту",
    "а" * 601,
])
async def test_markup_text_not_sent_counted_cache_dropped(db, max_api, monkeypatch, text):
    fake = await max_api()
    await _user(db)
    calls = _n8n(monkeypatch, [text, "Подскажите, когда удобно приехать?"])
    await _ticks(monkeypatch, 1)
    assert fake.posts == []
    ctx = await _ctx()
    assert ctx["reminder_1_failures"] == 1 and "reminder_1_pending" not in ctx
    await _ticks(monkeypatch, 1)                             # следующий тик зовёт n8n заново
    assert len(calls) == 2 and len(fake.posts) == 1
    assert (await _ctx())["reminder_level"] == 1


@pytest.mark.parametrize("text", [
    "Подскажите — удобно ли вам заехать завтра?",
    "Есть *акция* до конца недели",
    "Цена 2 500 000 ₽ - со скидкой",
])
def test_normal_text_passes_markup_guard(text):
    assert main_mod.reminder_markup_reason(text) is None


@pytest.mark.asyncio
async def test_silent_over_48h_untouched(db, max_api, monkeypatch):
    """Регресс: клиент молчит больше 48 ч — напоминаний нет (его закроет sync_worker)."""
    fake = await max_api()
    await _user(db, silent_for=172800 + 60)
    calls = _n8n(monkeypatch)
    await _ticks(monkeypatch, 1)
    assert calls == [] and fake.posts == []


# ---------------- Ревью v3.4.3: пометка только для недоступности клиента ----------------

@pytest.mark.asyncio
@pytest.mark.parametrize("status,payload", [
    (403, {"code": "access.denied", "message": "Bot has no rights"}),
    (403, {"code": "verify.token", "message": "Invalid access_token"}),
    (403, "Forbidden"),
])
async def test_other_403_is_not_dialog_closed(db, max_api, status, payload):
    """403 не про диалог клиента (права бота, токен) — не пометка «диалог
    закрыт»: иначе один общий сбой глушит всех живых клиентов. Повторы — как
    у временной ошибки."""
    fake = await max_api([(status, payload)] * 3)
    await _user(db)
    assert await main_mod.send_smartbot_reply(UID, {"text": "Добрый день"}) is False
    assert len(fake.posts) == 3
    assert "max_send_blocked" not in await _ctx()


@pytest.mark.asyncio
async def test_403_dialog_suspended_message_without_code_is_closed(db, max_api):
    fake = await max_api([(403, {"message": "Key: error.dialog.suspended, args: [1,]."})] * 3)
    await _user(db)
    assert await main_mod.send_smartbot_reply(UID, {"text": "Добрый день"}) is False
    assert len(fake.posts) == 1 and "max_send_blocked" in await _ctx()


# ---------------- v3.4.4: решения владельца по ревью v3.4.3 (P2.1, P2.2) ----------------

@pytest.mark.asyncio
async def test_client_writes_during_failed_tick_context_kept(db, max_api, monkeypatch):
    """P2.1: клиент пишет, пока тик ждёт n8n, затем доставка падает → его
    реплика и сброс reminder_level сохраняются (тик не перезаписывает контекст
    своей копией с начала прохода)."""
    await max_api([(500, {})] * 3)
    await _user(db)

    async def n8n_while_client_writes(uid, text, name, ctx):
        async with database.AsyncSessionLocal() as session:
            row = (await session.execute(select(User).where(User.platform_user_id == UID))).scalar_one()
            live = json.loads(row.context_json)
            live["chat_history"].append({"time": "10:31", "role": "user", "text": "Я тут, завтра удобно"})
            live["client_last_message_time"] = time.time()
            live["reminder_level"] = 0
            row.context_json = json.dumps(live, ensure_ascii=False)
            await session.commit()
        return {"text": "Подскажите, когда удобно приехать?"}

    monkeypatch.setattr(main_mod, "send_chat_message_to_n8n", n8n_while_client_writes)
    await _ticks(monkeypatch, 1)
    ctx = await _ctx()
    assert any(m.get("text") == "Я тут, завтра удобно" for m in ctx["chat_history"])
    assert ctx["reminder_level"] == 0
    assert "reminder_1_failures" not in ctx and "reminder_1_pending" not in ctx


@pytest.mark.asyncio
async def test_failed_tick_writes_only_reminder_keys(db, max_api, monkeypatch):
    """P2.1: неудачный тик пишет только свои ключи поверх свежего контекста —
    ключи, записанные за время тика (кэш n8n и т. п.), не теряются."""
    await max_api([(500, {})] * 3)
    await _user(db)

    async def n8n_adds_key(uid, text, name, ctx):
        async with database.AsyncSessionLocal() as session:
            row = (await session.execute(select(User).where(User.platform_user_id == UID))).scalar_one()
            live = json.loads(row.context_json)
            live["n8n_cache"] = {"k": "v"}
            row.context_json = json.dumps(live, ensure_ascii=False)
            await session.commit()
        return {"text": "Подскажите, когда удобно приехать?"}

    monkeypatch.setattr(main_mod, "send_chat_message_to_n8n", n8n_adds_key)
    await _ticks(monkeypatch, 1)
    ctx = await _ctx()
    assert ctx["n8n_cache"] == {"k": "v"}
    assert ctx["reminder_1_failures"] == 1 and ctx["reminder_1_pending"]["text"]


@pytest.mark.asyncio
async def test_n8n_down_five_ticks_not_muted_then_sent(db, max_api, monkeypatch):
    """P2.2: n8n недоступен 5 тиков → напоминание не заглушено (в потолок
    идут только отказы MAX и разметка); после восстановления уходит."""
    fake = await max_api()
    await _user(db)
    calls = []

    async def n8n_down(uid, text, name, ctx):
        calls.append(text)
        return None

    monkeypatch.setattr(main_mod, "send_chat_message_to_n8n", n8n_down)
    await _ticks(monkeypatch, 5)
    ctx = await _ctx()
    assert len(calls) == 5 and fake.posts == []
    assert ctx["reminder_level"] == 0 and "reminder_1_failures" not in ctx
    _n8n(monkeypatch, ["Подскажите, когда удобно приехать?"])
    await _ticks(monkeypatch, 1)
    assert len(fake.posts) == 1 and (await _ctx())["reminder_level"] == 1


# ---------------- Ревью v3.4.4: точечная запись и внешняя сессия прохода ----------------

async def _add_user(db, puid, ctx_extra=None):
    ctx = {"channel": "max", "bot_key": "max-geely", "start_code": "99141611BY",
           "client_last_message_time": time.time() - 3600, "reminder_level": 0,
           "chat_history": [{"time": "10:00", "role": "bot", "text": "x"}]}
    ctx.update(ctx_extra or {})
    async with db.AsyncSessionLocal() as session:
        session.add(User(platform_user_id=puid, channel="max", full_name="K",
                         is_smartbot_active=True, context_json=json.dumps(ctx, ensure_ascii=False)))
        await session.commit()


async def _ctx_of(puid):
    async with database.AsyncSessionLocal() as session:
        row = (await session.execute(select(User).where(User.platform_user_id == puid))).scalar_one()
    return json.loads(row.context_json)


@pytest.mark.asyncio
async def test_success_then_failure_in_one_pass_both_kept(db, max_api, monkeypatch):
    """Клиент A доставлен, у клиента B отказ MAX в том же проходе → оба
    состояния записаны (точечная запись B не ждёт незакоммиченный проход)."""
    await max_api([(200, {"message": {"id": 1}})] + [(500, {})] * 3)
    await _add_user(db, "A")
    await _add_user(db, "B")
    _n8n(monkeypatch, ["Привет A", "Привет B"])
    await asyncio.wait_for(_ticks(monkeypatch, 1), 15)
    assert (await _ctx_of("A"))["reminder_level"] == 1
    assert (await _ctx_of("B"))["reminder_1_failures"] == 1


@pytest.mark.asyncio
async def test_bot_reactivated_then_failure_same_row_no_hang(db, max_api, monkeypatch):
    """Бот сессии снова активен (проход снимает bot_send_blocked — запись в
    строку клиента), и в том же тике первое напоминание падает → точечная
    запись не висит на блокировке своей же строки."""
    await max_api([(500, {})] * 3)
    await _user(db, {"bot_send_blocked": {"bot_key": "max-geely", "since": 1}})
    _n8n(monkeypatch, ["Подскажите, когда удобно приехать?"])
    await asyncio.wait_for(_ticks(monkeypatch, 1), 15)
    ctx = await _ctx()
    assert "bot_send_blocked" not in ctx and ctx["reminder_1_failures"] == 1


@pytest.mark.asyncio
async def test_client_write_only_in_memory_during_tick_not_overwritten(db, max_api, monkeypatch):
    """Сообщение клиента пока только в USER_CONTEXT (сохранение в БД — после
    дебаунса) → неудачный тик не пишет ни в БД, ни в память клиента."""
    await max_api([(500, {})] * 3)
    await _user(db)

    async def n8n_client_writes_in_memory(uid, text, name, ctx):
        live = dict(ctx)
        live["client_last_message_time"] = time.time()
        live["reminder_level"] = 0
        main_mod.USER_CONTEXT[UID] = live
        return {"text": "Подскажите, когда удобно приехать?"}

    monkeypatch.setattr(main_mod, "send_chat_message_to_n8n", n8n_client_writes_in_memory)
    await _ticks(monkeypatch, 1)
    mem = main_mod.USER_CONTEXT.pop(UID)
    assert "reminder_1_failures" not in mem and "reminder_1_pending" not in mem
    assert "reminder_1_failures" not in await _ctx()


@pytest.mark.asyncio
async def test_bot_reactivated_then_403_mark_saved_no_hang(db, max_api, monkeypatch):
    """Бот снова активен (проход пишет строку клиента) и MAX отвечает 403 в
    том же тике → пометка «диалог закрыт» сохраняется своим соединением, без
    ожидания незакоммиченной строки прохода (PG — вечно, SQLite — locked)."""
    await max_api([(403, DENIED)] * 3)
    await _user(db, {"bot_send_blocked": {"bot_key": "max-geely", "since": 1}})
    _n8n(monkeypatch, ["Подскажите, когда удобно приехать?"])
    await asyncio.wait_for(_ticks(monkeypatch, 1), 15)
    ctx = await _ctx()
    assert "bot_send_blocked" not in ctx
    assert ctx[main_mod.MAX_SEND_BLOCKED_KEY]["code"] == "chat.denied"


@pytest.mark.asyncio
async def test_bot_reactivated_then_reply_with_json_cache_saved_no_hang(db, max_api, monkeypatch, caplog):
    """Бот снова активен и ответ n8n несёт json_cache (save_context_to_db своим
    соединением внутри send_smartbot_reply) → запись без блокировки, проход не
    висит. (Что успешная ветка затем пишет копию контекста с начала тика —
    прежнее поведение v3.4.2, вне этой правки.)"""
    await max_api()
    await _user(db, {"bot_send_blocked": {"bot_key": "max-geely", "since": 1}})

    async def n8n_with_cache(uid, text, name, ctx):
        return {"text": "Подскажите, когда удобно приехать?", "json_cache": {"k": "v"}}

    monkeypatch.setattr(main_mod, "send_chat_message_to_n8n", n8n_with_cache)
    await asyncio.wait_for(_ticks(monkeypatch, 1), 15)
    main_mod.USER_CONTEXT.pop(UID, None)
    ctx = await _ctx()
    assert ctx["reminder_level"] == 1 and "bot_send_blocked" not in ctx
    assert not [r for r in caplog.records if "database is locked" in r.getMessage()]
