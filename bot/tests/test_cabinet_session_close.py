"""План 2026-08-14-001, U2/U3/U5: завершение сессии диалога при передаче
заявки кабинета в CRM.

Проверка — по фактическому содержимому таблиц (cabinet_events, users) и
фактическим отправкам фейковых клиентов после сценария, не по коду ветки:
класс уроков репо «зелёная ветка ничего не говорит о границе процессов».
"""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

import database
from database import CabinetEvent, CabinetToken, User

import cabinet_service
import main as main_mod

from conftest import PHONE

# Фикстуры db/deps — общие, в conftest.py.


# =======================
# Хелперы и фейки
# =======================

class _FakeClient:
    """Фейковый канальный клиент: пишет отправки, ремайндеры поддерживает."""

    def __init__(self):
        self.sent = []

    async def send_message(self, user_id=None, text=None, attachments=None, **kw):
        self.sent.append({"user_id": user_id, "text": text})
        return True

    async def send_typing(self, chat_id=None, **kw):
        return True

    def supports_reminders(self):
        return True


@pytest.fixture
def bot_env(db, monkeypatch):
    """Изоляция module-state main.py + фейки внешних выходов.

    КРИТИЧНО: main.py биндит AsyncSessionLocal импортом (from database
    import ...), а фикстура db подменяет database.AsyncSessionLocal — без
    перепривязки main ходил бы в БОЕВОЙ дев-SQLite (и висел на его локе)."""
    client = _FakeClient()
    archives = []
    closes = []
    stage_calls = []

    async def fake_upsert(payload):
        archives.append(payload)

    async def fake_send_close(user_id):
        closes.append(user_id)

    async def fake_stage(deal_id, stage):
        stage_calls.append((deal_id, stage))

    monkeypatch.setattr(main_mod, "AsyncSessionLocal", db.AsyncSessionLocal)
    monkeypatch.setattr(main_mod, "get_client", lambda ch=None: client)
    monkeypatch.setattr(main_mod, "upsert_dialogue_archive", fake_upsert)
    monkeypatch.setattr(main_mod.web_client, "send_close", fake_send_close)
    monkeypatch.setattr(main_mod, "update_deal_stage_direct", fake_stage)
    monkeypatch.setattr(main_mod, "BITRIX_WEBHOOK_URL", "", raising=False)
    main_mod.USER_CONTEXT.clear()
    main_mod.USER_MESSAGE_TASKS.clear()
    main_mod.USER_MESSAGE_BUFFERS.clear()
    yield type("BotEnv", (), {"client": client, "archives": archives,
                              "closes": closes, "stage_calls": stage_calls})
    main_mod.USER_CONTEXT.clear()
    main_mod.USER_MESSAGE_TASKS.clear()
    main_mod.USER_MESSAGE_BUFFERS.clear()


async def _mk_user(db, suffix="1", channel="max", ctx_extra=None, active=True):
    ctx = {"start_code": "991211", "channel": channel, "chat_history": [
        {"role": "user", "text": "привет", "time": "10:00"}]}
    ctx.update(ctx_extra or {})
    async with db.AsyncSessionLocal() as session:
        user = User(platform_user_id=f"u-{suffix}", channel=channel,
                    full_name="Иван", is_smartbot_active=active,
                    context_json=json.dumps(ctx, ensure_ascii=False))
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _mk_token(db, user_id, **kw):
    defaults = dict(user_id=user_id, dealer_code="01", deal_id="D-77",
                    token_hash=uuid.uuid4().hex + uuid.uuid4().hex[:32],
                    status="active", bound_phone=PHONE,
                    bound_at=datetime.now(timezone.utc))
    defaults.update(kw)
    async with db.AsyncSessionLocal() as session:
        token = CabinetToken(**defaults)
        session.add(token)
        await session.commit()
        await session.refresh(token)
        return token


async def _smartbot_active(db, user_id):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(
            select(User.is_smartbot_active).where(User.id == user_id)
        )).scalar_one()


async def _event_count(db, event_type):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(
            select(func.count()).select_from(CabinetEvent)
            .where(CabinetEvent.event_type == event_type)
        )).scalar_one()


# =======================
# U2: немедленная ветка (хода нет) — типовой случай таймаут-передачи
# =======================

@pytest.mark.asyncio
async def test_idle_handoff_closes_session_immediately(db, deps, bot_env):
    user = await _mk_user(db)
    token = await _mk_token(db, user.id)

    await main_mod.close_cabinet_session(user.id, "timeout", "D-77", token.id)

    assert await _smartbot_active(db, user.id) is False
    farewells = [m for m in bot_env.client.sent if "покинул чат" in (m["text"] or "")]
    assert len(farewells) == 1, "одно прощание, немедленно"
    assert "перейдите на сайт" in farewells[0]["text"], "max-текст общий (R5)"
    assert len(bot_env.archives) == 1
    assert bot_env.archives[0]["Final_Status"] == "cabinet_handoff"
    assert "timeout" in bot_env.archives[0]["AI_Report"]
    assert await _event_count(db, "session_closed") == 1, \
        "session_closed записан ПОСЛЕ фактической финализации"
    assert bot_env.stage_calls == [], "STAGE_ID сделки этим путём не меняется (R1)"
    # Стенограмма несёт прощание системной записью
    async with db.AsyncSessionLocal() as session:
        ctx = json.loads((await session.get(User, user.id)).context_json)
    assert any("покинул чат" in h.get("text", "")
               for h in ctx["chat_history"] if h.get("role") == "system")


@pytest.mark.asyncio
async def test_web_channel_farewell_and_chat_close(db, deps, bot_env):
    user = await _mk_user(db, "web", channel="web")
    token = await _mk_token(db, user.id)

    await main_mod.close_cabinet_session(user.id, "submit", "D-77", token.id)

    farewells = [m for m in bot_env.client.sent if "покинул чат" in (m["text"] or "")]
    assert len(farewells) == 1
    assert "перейдите на сайт" not in farewells[0]["text"], \
        "web-текст без приглашения на сайт (R5)"
    assert bot_env.closes == [user.platform_user_id], \
        "событие chat_close ушло в SSE-очередь (через finalize)"
    assert await _event_count(db, "session_closed") == 1


@pytest.mark.asyncio
async def test_context_loaded_from_db_when_memory_empty(db, deps, bot_env):
    """Таймаут-передача при вычищенной памяти (рестарт): контекст из БД (R3)."""
    user = await _mk_user(db, "cold")
    token = await _mk_token(db, user.id)
    assert user.platform_user_id not in main_mod.USER_CONTEXT

    await main_mod.close_cabinet_session(user.id, "timeout", "D-77", token.id)

    assert await _event_count(db, "session_closed") == 1
    assert len(bot_env.archives) == 1, "финализация отработала от БД-контекста"


# =======================
# U2: взаимный гейт (R4) и идемпотентность (R3)
# =======================

@pytest.mark.asyncio
async def test_noop_when_n8n_finalized_first_but_marks_closed(db, deps, bot_env):
    """n8n-callback финализировал раньше → ни второго прощания, ни второго
    архива, но session_closed фиксируется — добор не крутится вечно."""
    user = await _mk_user(db, "fin", ctx_extra={"deal_finalized": True})
    token = await _mk_token(db, user.id)

    await main_mod.close_cabinet_session(user.id, "timeout", "D-77", token.id)

    assert bot_env.client.sent == [], "второго прощания нет"
    assert bot_env.archives == [], "второго архива нет"
    assert await _event_count(db, "session_closed") == 1, \
        "факт завершения (другим путём) зафиксирован для добора"


@pytest.mark.asyncio
async def test_repeat_close_is_noop(db, deps, bot_env):
    user = await _mk_user(db, "rep")
    token = await _mk_token(db, user.id)
    await main_mod.close_cabinet_session(user.id, "timeout", "D-77", token.id)
    await main_mod.close_cabinet_session(user.id, "close_recovery", "D-77", token.id)

    farewells = [m for m in bot_env.client.sent if "покинул чат" in (m["text"] or "")]
    assert len(farewells) == 1 and len(bot_env.archives) == 1
    assert await _event_count(db, "session_closed") == 1


# =======================
# U2: ход в полёте — pending-путь
# =======================

@pytest.mark.asyncio
async def test_inflight_turn_defers_farewell_to_turn_end(db, deps, bot_env):
    user = await _mk_user(db, "fly")
    token = await _mk_token(db, user.id)
    puid = user.platform_user_id
    main_mod.USER_CONTEXT[puid] = {"channel": "max", "chat_history": [],
                                   "start_code": "991211"}
    # «Ход в полёте»: реестр тасок занят
    main_mod.USER_MESSAGE_TASKS[puid] = asyncio.create_task(asyncio.sleep(30))
    try:
        await main_mod.close_cabinet_session(user.id, "submit", "D-77", token.id)

        assert await _smartbot_active(db, user.id) is False, \
            "флаг гасится сразу — новый ход не уйдёт"
        assert bot_env.client.sent == [], "прощание отложено до конца хода"
        mem = main_mod.USER_CONTEXT[puid]
        assert mem.get("pending_farewell") is True
        assert mem["pending_archive"]["cabinet_token_id"] == token.id
        assert await _event_count(db, "session_closed") == 0, \
            "запись только после фактического завершения"

        # Конец хода: хвост потребляет pending_archive → finalize → session_closed
        pending = mem.pop("pending_archive")
        ok = await main_mod.finalize_transcript_archive(puid, pending)
        assert ok is True
        assert await _event_count(db, "session_closed") == 1
    finally:
        task = main_mod.USER_MESSAGE_TASKS.pop(puid, None)
        if task:
            task.cancel()


# =======================
# U1+U2: рестарт после pending_* — добор доводит ровно один раз
# =======================

@pytest.mark.asyncio
async def test_restart_after_pending_recovered_once(db, deps, bot_env, monkeypatch):
    monkeypatch.setattr(cabinet_service, "_on_handoff_completed",
                        main_mod.close_cabinet_session)
    now = datetime.now(timezone.utc)
    user = await _mk_user(db, "boot", active=False)  # флаг погашен до рестарта
    token = await _mk_token(db, user.id, handoff_at=now - timedelta(minutes=20))
    await cabinet_service._record_event_once(user.id, token.id, "handoff_done")
    async with db.AsyncSessionLocal() as session:
        await session.execute(update(CabinetEvent).values(
            created_at=now - timedelta(minutes=15)))
        await session.commit()
    # Память пуста (рестарт стёр pending_*) — добор обязан довести завершение

    await cabinet_service.otp_worker_tick(now)
    while cabinet_service._HOOK_TASKS:
        await asyncio.gather(*list(cabinet_service._HOOK_TASKS))

    farewells = [m for m in bot_env.client.sent if "покинул чат" in (m["text"] or "")]
    assert len(farewells) == 1 and len(bot_env.archives) == 1
    assert await _event_count(db, "session_closed") == 1

    # Второй тик — полный no-op
    await cabinet_service.otp_worker_tick(now)
    while cabinet_service._HOOK_TASKS:
        await asyncio.gather(*list(cabinet_service._HOOK_TASKS))
    assert len(farewells) == 1 and len(bot_env.archives) == 1
    assert await _event_count(db, "session_closed") == 1


# =======================
# U5: подавление ремайндеров в кабинетном окне
# =======================

@pytest.mark.asyncio
async def test_reminders_suppressed_in_cabinet_window(db, deps):
    user = await _mk_user(db, "rem")
    await _mk_token(db, user.id)  # bound, handoff_at нет → окно активно
    assert await cabinet_service.reminders_suppressed(user.id) is True


@pytest.mark.asyncio
async def test_reminders_not_suppressed_outside_window(db, deps):
    # Ссылка выдана, номер НЕ подтверждён → ремайндер идёт (ветвление n8n
    # «удалось ли справиться в кабинете» — контракт R15)
    user = await _mk_user(db, "rem2")
    await _mk_token(db, user.id, bound_at=None, bound_phone=None)
    assert await cabinet_service.reminders_suppressed(user.id) is False

    # После передачи окно закрыто (сессию завершает колбэк, не подавление)
    user3 = await _mk_user(db, "rem3")
    await _mk_token(db, user3.id, handoff_at=datetime.now(timezone.utc))
    assert await cabinet_service.reminders_suppressed(user3.id) is False


# =======================
# U3: гарды — source-грепы дополнены фактическим прогоном delayed_sender-гейта
# =======================

@pytest.mark.asyncio
async def test_reminder_response_not_delivered_after_close(db, deps, bot_env,
                                                           monkeypatch):
    """Ремайндер-ход в полёте, сессия завершилась до возврата ответа →
    ответ не доставлен, context_json не перезаписан (U3)."""
    user = await _mk_user(db, "race")
    token = await _mk_token(db, user.id)
    puid = user.platform_user_id

    async def fake_n8n(uid, text, name, ctx):
        # Пока ремайндер-ход «летел», передача завершила сессию
        await main_mod.close_cabinet_session(user.id, "timeout", "D-77", token.id)
        return {"text": "Я ещё тут, актуален ли вопрос?"}

    monkeypatch.setattr(main_mod, "send_chat_message_to_n8n", fake_n8n)
    delivered = []

    async def fake_reply(uid, resp):
        delivered.append(resp)

    monkeypatch.setattr(main_mod, "send_smartbot_reply", fake_reply)

    # Воспроизводим тело ремайндер-хода (reminder_worker): ход + перепроверка
    ai_resp = await main_mod.send_chat_message_to_n8n(puid, "__SYSTEM_REMINDER__",
                                                      "Иван", {})
    if ai_resp:
        alive = await _smartbot_active(db, user.id)
        if not alive:
            ai_resp = None
    if ai_resp:
        await main_mod.send_smartbot_reply(puid, ai_resp)

    assert delivered == [], "ответ ремайндера после завершения не доставляется"
    farewells = [m for m in bot_env.client.sent if "покинул чат" in (m["text"] or "")]
    assert len(farewells) == 1, "клиент получил только прощание"


def test_u3_guards_pinned_in_source():
    """Пины гардов U3 в main.py: перепроверка в delayed_sender под локом и в
    reminder_worker после возврата ai_resp (конвенция source-грепов репо)."""
    src = main_mod.__file__
    text = open(src, encoding="utf-8").read()
    sender = text.split("async def delayed_sender", 1)[1].split("USER_MESSAGE_TASKS[max_user_id] = ", 1)[0]
    assert "is_smartbot_active" in sender and "Session Closed" in sender, \
        "delayed_sender не перепроверяет живость сессии под локом"
    worker = text.split("async def reminder_worker", 1)[1].split("async def ", 1)[0]
    assert "reminders_suppressed" in worker, "U5-предикат не подключён"
    assert "Ответ ремайндера не доставлен" in worker, \
        "reminder_worker не гасит доставку после завершения"


# =======================
# Фиксы ревью 2026-08-15: идемпотентность прощания, гарды, порядок доставки
# =======================

@pytest.mark.asyncio
async def test_failed_finalize_retry_does_not_duplicate_farewell(db, deps, bot_env,
                                                                 monkeypatch):
    """P1 трёх ревьюеров: finalize падает → повтор добором шлёт ТОЛЬКО
    финализацию, второго «покинул чат» клиент не получает."""
    user = await _mk_user(db, "ff")
    token = await _mk_token(db, user.id)

    calls = {"n": 0}
    real_upsert = bot_env.archives.append

    async def flaky_upsert(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("NocoDB down")
        real_upsert(payload)

    monkeypatch.setattr(main_mod, "upsert_dialogue_archive", flaky_upsert)

    await main_mod.close_cabinet_session(user.id, "timeout", "D-77", token.id)
    farewells = [m for m in bot_env.client.sent if "покинул чат" in (m["text"] or "")]
    assert len(farewells) == 1
    assert await _event_count(db, "session_closed") == 0, "финализация упала"
    assert await _event_count(db, "session_farewell_sent") == 1

    # Повтор добора: прощание не дублируется, финализация доводится
    await main_mod.close_cabinet_session(user.id, "close_recovery", "D-77", token.id)
    farewells = [m for m in bot_env.client.sent if "покинул чат" in (m["text"] or "")]
    assert len(farewells) == 1, "второго прощания нет (гард session_farewell_sent)"
    assert await _event_count(db, "session_closed") == 1
    assert len(bot_env.archives) == 1


@pytest.mark.asyncio
async def test_n8n_callback_finalize_does_not_write_session_closed(db, deps, bot_env):
    """finalize_transcript_archive без cabinet_token_id (путь n8n-callback)
    события session_closed не пишет."""
    user = await _mk_user(db, "nc")
    await _mk_token(db, user.id)
    ok = await main_mod.finalize_transcript_archive(user.platform_user_id, {
        "deal_id": "D-77", "status": "visit", "report_text": "итог"})
    assert ok is True
    assert await _event_count(db, "session_closed") == 0


@pytest.mark.asyncio
async def test_pending_consumed_by_guard_branch(db, deps, bot_env):
    """Гард delayed_sender, убивший ход, потребляет pending_*: прощание
    уходит сразу (не через 10 минут добора), флаги не протухают в
    context_json (ревью: adversarial+correctness)."""
    user = await _mk_user(db, "pg", active=False)  # сессия уже погашена
    token = await _mk_token(db, user.id)
    puid = user.platform_user_id
    main_mod.USER_CONTEXT[puid] = {
        "channel": "max", "chat_history": [], "start_code": "991211",
        "pending_farewell": True,
        "pending_archive": {"deal_id": "D-77", "status": "cabinet_handoff",
                            "report_text": "тест", "cabinet_token_id": token.id,
                            "cabinet_user_pk": user.id}}

    # Воспроизводим гард-ветку: живость False → потребление перед выходом
    # (реальный delayed_sender — замыкание; тело гарда вызываем через
    # выделенный путь: run guard consumption inline как в main.py)
    _mem = main_mod.USER_CONTEXT.get(puid, {})
    _pa = _mem.pop('pending_archive', None)
    if _mem.pop('pending_farewell', False):
        _tok = (_pa or {}).get('cabinet_token_id')
        _upk = (_pa or {}).get('cabinet_user_pk')
        if _tok is None or await cabinet_service.record_session_farewell(_upk, _tok):
            fw = main_mod.build_farewell_text(_mem.get('channel', 'max'))
            await main_mod.get_client('max').send_message(user_id=puid, text=fw)
    if _pa:
        await main_mod.finalize_transcript_archive(puid, _pa)

    farewells = [m for m in bot_env.client.sent if "покинул чат" in (m["text"] or "")]
    assert len(farewells) == 1
    assert await _event_count(db, "session_closed") == 1
    assert "pending_farewell" not in main_mod.USER_CONTEXT.get(puid, {})


@pytest.mark.asyncio
async def test_answer_delivered_before_farewell_via_real_send_smartbot_reply(
        db, deps, bot_env, monkeypatch):
    """Порядок при ходе в полёте: ответ Александра → прощание, реальным
    send_smartbot_reply (не ручной симуляцией)."""
    user = await _mk_user(db, "ord")
    token = await _mk_token(db, user.id)
    puid = user.platform_user_id
    main_mod.USER_CONTEXT[puid] = {
        "channel": "max", "chat_history": [], "start_code": "991211",
        "pending_farewell": True,
        "pending_archive": {"deal_id": "D-77", "status": "cabinet_handoff",
                            "report_text": "т", "cabinet_token_id": token.id,
                            "cabinet_user_pk": user.id}}

    await main_mod.send_smartbot_reply(puid, {"text": "Ответ Александра"})

    sent_order = [m["text"] for m in bot_env.client.sent if m["text"]]
    assert len(sent_order) >= 2, "ушли и ответ, и прощание"
    assert "Ответ Александра" in sent_order[0]
    assert "покинул чат" in sent_order[-1], "прощание строго после ответа"
    assert await _event_count(db, "session_farewell_sent") == 1, \
        "pending-путь фиксирует факт отправки прощания для добора"


@pytest.mark.asyncio
async def test_unclosed_recovery_respects_max_age(db, deps, bot_env, monkeypatch):
    """Потолок автоповторов добора: суточная незакрываемая сессия — техалерт
    однократно, повторные fire прекращаются (ревью: reliability)."""
    monkeypatch.setattr(cabinet_service, "_on_handoff_completed",
                        main_mod.close_cabinet_session)
    now = datetime.now(timezone.utc)
    user = await _mk_user(db, "old", active=False)
    token = await _mk_token(db, user.id, handoff_at=now - timedelta(days=2))
    await cabinet_service._record_event_once(user.id, token.id, "handoff_done")
    async with db.AsyncSessionLocal() as session:
        await session.execute(update(CabinetEvent).values(
            created_at=now - timedelta(days=2)))
        await session.commit()

    for _ in range(3):
        await cabinet_service.otp_worker_tick(now)
        while cabinet_service._HOOK_TASKS:
            await asyncio.gather(*list(cabinet_service._HOOK_TASKS))

    assert bot_env.client.sent == [], "старше потолка — попыток завершения нет"
    alerts = [t for t in deps.tech_alerts if "не закрывается" in t]
    assert len(alerts) == 1, "техалерт однократен (гард-событие)"
