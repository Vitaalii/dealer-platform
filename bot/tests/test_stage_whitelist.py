"""План 2026-10-12-002 (v3.4.3), часть A: «Обогащение» только из стадий до
передачи. Сделка, ушедшая на «Отправлено» и дальше, ботом в «Обогащение» не
возвращается — иначе робот стадии разошлёт письма дилеру повторно.

Битрикс — настоящий HTTP-фейк из test_stage_guarded_retry (робот: запись
«Обогащения» переводит сделку в «Отправлено»)."""
import asyncio
import json

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from sqlalchemy import select

import cabinet_service
import database
import main as main_mod
from database import PendingSync

from test_stage_guarded_retry import (  # noqa: F401 — фикстуры и фейк
    CHAT_CTX, ENRICH, QUALIF, SENT, FakeBitrix, _chat_user, _drain_handoff, _queued,
    _replay, _task, bitrix, guarded_cabinet)
from test_web_entry_conversion import (  # noqa: F401
    _web_session, dealers, nocodb_tables, real_conversion)

SOURCES = ["C9:NEW", "C9:PREPAYMENT_INVOICE", "C9:UC_CKY0IR", "C9:UC_HW8Q2H",
           "C9:UC_MDA6RZ", "C9:LOSE", "C9:APOLOGY"]


class SlowBitrix(FakeBitrix):
    """Запись «Обогащения» идёт delay секунд (роботы), но укладывается в таймаут."""

    delay = 0.15

    async def handler(self, request):
        if request.match_info["method"] == "crm.deal.update":
            body = await request.json()
            if (body.get("fields") or {}).get("STAGE_ID") == ENRICH:
                self.stage_writes.append(ENRICH)
                self.stage_bodies.append(body)
                await asyncio.sleep(self.delay)
                self.stage = SENT
                return web.json_response({"result": True})

            async def _json():
                return body
            request.json = _json
        return await super().handler(request)


@pytest_asyncio.fixture
async def slow_bitrix(bitrix, monkeypatch):
    async def make(stage):
        fake = await bitrix(stage)
        slow = SlowBitrix(stage)
        app = web.Application()
        app.router.add_post("/{method}", slow.handler)
        from aiohttp.test_utils import TestServer
        server = TestServer(app)
        await server.start_server()
        monkeypatch.setattr(main_mod.BitrixService, "WEBHOOK", f"http://{server.host}:{server.port}/")
        make.servers.append(server)
        return slow
    make.servers = []
    yield make
    for s in make.servers:
        await s.close()


def _stage_logs(caplog, text):
    return [r.getMessage() for r in caplog.records if "[Bitrix Stage]" in r.getMessage()
            and text in r.getMessage()]


# ---------------- U1: белый список и свежее чтение ----------------

@pytest.mark.asyncio
async def test_sent_deal_final_sync_fields_written_stage_not(db, bitrix, monkeypatch, caplog):
    """В1/P2.6: сделка уже в «Отправлено» → поля пишутся, стадия — нет."""
    fake = await bitrix(SENT)
    uid = await _chat_user(db, monkeypatch)
    caplog.set_level("INFO")
    await main_mod.BitrixService.final_sync_wrapper(uid, dict(CHAT_CTX))
    assert fake.stage_writes == [] and fake.stage == SENT
    assert fake.field_bodies                                   # поля ушли
    assert _stage_logs(caplog, "не из стадии до передачи")
    assert not [p for p in await _queued() if "STAGE_ID" in (p.get("fields") or {})]


@pytest.mark.asyncio
async def test_lose_deal_final_sync_enrich_once(db, bitrix, monkeypatch):
    """В1: сделка в LOSE (бот закрыл по 48 ч), клиент вернулся → «Обогащение»
    ровно один раз, робот — одно «Отправлено» (одни письма)."""
    fake = await bitrix("C9:LOSE")
    uid = await _chat_user(db, monkeypatch)
    await main_mod.BitrixService.final_sync_wrapper(uid, dict(CHAT_CTX))
    assert fake.stage_writes == [ENRICH] and fake.stage == SENT


@pytest.mark.asyncio
@pytest.mark.parametrize("source", SOURCES)
async def test_whitelist_sources_write_once(db, bitrix, source):
    fake = await bitrix(source)
    assert await main_mod.BitrixService.set_stage_guarded("D-1", ENRICH, None, user_id="u")
    assert fake.stage_writes == [ENRICH] and fake.stage == SENT


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [ENRICH, "C9:UC_3NL90C", "UC_3NL90C", "C9:WON", "C9:OTHER"])
async def test_not_in_whitelist_no_write(db, bitrix, stage):
    fake = await bitrix(stage)
    assert await main_mod.BitrixService.set_stage_guarded("D-1", ENRICH, QUALIF, user_id="u")
    assert fake.stage_writes == [] and fake.stage == stage
    assert not await _queued()


@pytest.mark.asyncio
async def test_stale_expect_fresh_read_wins(db, bitrix, monkeypatch):
    """Вызывающий передал устаревшее «было» (Квалификация), в Битриксе уже
    «Отправлено» → записи нет."""
    fake = await bitrix(SENT)
    uid = await _chat_user(db, monkeypatch)

    async def stale(deal_id, user_id=None):
        return {"ID": deal_id, "STAGE_ID": QUALIF, "CONTACT_ID": None}

    monkeypatch.setattr(main_mod.BitrixService, "get_deal_by_id", stale)
    await main_mod.BitrixService.final_sync_wrapper(uid, dict(CHAT_CTX))
    assert fake.stage_writes == [] and fake.stage == SENT


@pytest.mark.asyncio
async def test_fresh_read_failed_no_blind_write_queued(db, bitrix, monkeypatch, caplog):
    """P2.3: чтение перед записью не удалось → записи нет, задача в очередь."""
    fake = await bitrix(QUALIF)
    uid = await _chat_user(db, monkeypatch)
    fake.fail_from_read = 2                    # сделка в начале читается, свежее чтение — нет
    caplog.set_level("INFO")
    await main_mod.BitrixService.final_sync_wrapper(uid, dict(CHAT_CTX))
    assert fake.stage_writes == []
    assert [p for p in await _queued() if "STAGE_ID" in (p.get("fields") or {})] == [
        {"id": "53859", "fields": {"STAGE_ID": ENRICH}, main_mod.STAGE_EXPECT_KEY: QUALIF}]
    assert _stage_logs(caplog, "чтение перед записью не удалось")


@pytest.mark.asyncio
async def test_other_targets_no_extra_read(db, bitrix):
    """Цели кроме «Обогащения» — прежний путь v3.4.2, без чтения (регресс)."""
    fake = await bitrix(QUALIF)
    assert await main_mod.BitrixService.set_stage_guarded("D-1", "C9:LOSE", None, user_id="u")
    assert fake.stage_writes == ["C9:LOSE"] and fake.reads == 0


@pytest.mark.asyncio
async def test_concurrent_writes_same_deal_one_enrich(db, slow_bitrix):
    """Две записи «Обогащения» в одну сделку одновременно (чат и кабинет) →
    одна запись: вторая под замком читает «Отправлено»."""
    fake = await slow_bitrix(QUALIF)
    await asyncio.gather(
        main_mod.BitrixService.set_stage_guarded("D-1", ENRICH, QUALIF, user_id="chat"),
        main_mod.BitrixService.set_stage_guarded("D-1", ENRICH, QUALIF, user_id="cab"))
    assert fake.stage_writes == [ENRICH] and fake.stage == SENT


@pytest.mark.asyncio
async def test_cabinet_handoff_on_sent_deal_no_stage(db, deps, real_conversion, guarded_cabinet,
                                                     bitrix, monkeypatch, caplog):
    """Передача кабинета на сделке в «Отправлено» → стадия не пишется,
    гард handoff_stage записан (подбор её не повторит)."""
    fake = await bitrix(SENT)

    async def found(code, stage_id=None, user_id=None):
        return {"ID": "52673", "STAGE_ID": "C9:NEW", "CONTACT_ID": None}

    monkeypatch.setattr(main_mod.BitrixService, "find_deal_data_by_code", found)
    caplog.set_level("INFO")
    _uid, bearer = await _web_session(db, deps)
    await cabinet_service.submit_form(bearer, {"marital_status": "Холост/не замужем",
                                               "credit_term": 84, "credit_payment": 30000})
    await _drain_handoff()
    assert ENRICH not in fake.stage_writes and fake.stage == SENT
    assert _stage_logs(caplog, "не из стадии до передачи")
    async with database.AsyncSessionLocal() as session:
        events = (await session.execute(select(database.CabinetEvent.event_type))).scalars().all()
    assert "handoff_stage" in events


# ---------------- U2: финальная синхронизация пользователя — по одной ----------------

@pytest.mark.asyncio
async def test_two_final_syncs_one_enrich_and_serialized(db, slow_bitrix, monkeypatch):
    """P2.1: вторая финальная синхронизация (второе сообщение клиента) во время
    первой → одна запись «Обогащения»; вторая начинается после первой."""
    fake = await slow_bitrix(QUALIF)
    uid = await _chat_user(db, monkeypatch)
    events = []
    real_get = main_mod.BitrixService.get_deal_by_id
    real_guarded = main_mod.BitrixService.set_stage_guarded

    async def get_spy(deal_id, user_id=None):
        events.append("read")
        return await real_get(deal_id, user_id=user_id)

    async def guarded_spy(deal_id, stage_id, expect_stage, user_id=None):
        res = await real_guarded(deal_id, stage_id, expect_stage, user_id=user_id)
        events.append("stage-done")
        return res

    monkeypatch.setattr(main_mod.BitrixService, "get_deal_by_id", get_spy)
    monkeypatch.setattr(main_mod.BitrixService, "set_stage_guarded", guarded_spy)
    await asyncio.gather(main_mod.BitrixService.final_sync_wrapper(uid, dict(CHAT_CTX)),
                         main_mod.BitrixService.final_sync_wrapper(uid, dict(CHAT_CTX)))
    assert fake.stage_writes == [ENRICH] and fake.stage == SENT
    assert events == ["read", "stage-done", "read", "stage-done"]


@pytest.mark.asyncio
async def test_final_sync_lock_per_user_not_global(db, monkeypatch):
    """Разные пользователи — параллельно; исключение освобождает замок."""
    started = []
    gate = asyncio.Event()

    async def fake_sync(uid, ctx, skip_tg=False, skip_stage=False):
        started.append(uid)
        if uid == "boom":
            raise RuntimeError("x")
        await gate.wait()

    monkeypatch.setattr(main_mod.BitrixService, "_final_sync", fake_sync)
    t1 = asyncio.create_task(main_mod.BitrixService.final_sync_wrapper("a", {}))
    t2 = asyncio.create_task(main_mod.BitrixService.final_sync_wrapper("b", {}))
    await asyncio.sleep(0.05)
    assert sorted(started) == ["a", "b"]                     # не ждут друг друга
    gate.set()
    await asyncio.gather(t1, t2)
    with pytest.raises(RuntimeError):
        await main_mod.BitrixService.final_sync_wrapper("boom", {})
    assert not main_mod._FINAL_SYNC_LOCKS["boom"].locked()


# ---------------- U4: отмена во время записи стадии ----------------

async def _stage_tasks():
    return [p for p in await _queued() if "STAGE_ID" in (p.get("fields") or {})]


@pytest.mark.asyncio
async def test_cancel_during_settle_pause_queued_with_expect(db, bitrix, monkeypatch):
    """P2.2: рестарт во время паузы после сбоя → задача в очередь с «было»."""
    fake = await bitrix(QUALIF, ["hang_noapply"])
    monkeypatch.setattr(main_mod, "BITRIX_STAGE_SETTLE_DELAYS", (30, 30))
    task = asyncio.create_task(main_mod.BitrixService.set_stage_guarded("D-1", ENRICH, QUALIF, user_id="u"))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if fake.stage_writes:
            break
    await asyncio.sleep(0.4)                         # таймаут 0.3 с прошёл — идёт пауза 30 с
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _stage_tasks() == [{"id": "D-1", "fields": {"STAGE_ID": ENRICH},
                                     main_mod.STAGE_EXPECT_KEY: QUALIF}]


@pytest.mark.asyncio
async def test_cancel_during_write_queued_with_expect(db, bitrix, monkeypatch):
    fake = await bitrix(QUALIF, ["hang_apply"])
    monkeypatch.setattr(main_mod, "BITRIX_STAGE_TIMEOUT", aiohttp.ClientTimeout(total=5))
    task = asyncio.create_task(main_mod.BitrixService.set_stage_guarded("D-1", ENRICH, QUALIF, user_id="u"))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if fake.stage_writes:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _stage_tasks() == [{"id": "D-1", "fields": {"STAGE_ID": ENRICH},
                                     main_mod.STAGE_EXPECT_KEY: QUALIF}]
    # досылка: запись применилась (робот — «Отправлено») → стадия не повторяется
    await asyncio.sleep(1.1)
    assert await _replay() == [] and fake.stage_writes == [ENRICH]


@pytest.mark.asyncio
async def test_cancel_before_write_nothing_queued(db, bitrix, monkeypatch):
    """Отмена во время свежего чтения (запись не начиналась) — задачи нет."""
    await bitrix(QUALIF)
    gate = asyncio.Event()

    async def slow_read(deal_id, user_id=None):
        await gate.wait()

    monkeypatch.setattr(main_mod.BitrixService, "_read_stage_once", slow_read)
    task = asyncio.create_task(main_mod.BitrixService.set_stage_guarded("D-1", ENRICH, QUALIF, user_id="u"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _stage_tasks() == []


@pytest.mark.asyncio
async def test_request_more_attempts_than_delays_no_index_error(db, bitrix):
    """P3: attempts > len(BITRIX_RETRY_DELAYS) — все попытки идут (без IndexError)."""
    fake = await bitrix(QUALIF)
    fake.read_fail = True
    res = await main_mod.BitrixService._request("crm.deal.get", {"id": "D-1"}, user_id="u",
                                                enqueue_on_failure=False, attempts=5)
    assert res == {} and fake.reads == 5


# ---------------- В3: колбэк n8n и аварийный мост ----------------

class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status = 200

    async def json(self):
        return self._payload


def _bx_session(monkeypatch, stage, posts):
    class _Sess:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **kw):
            posts.append((url, json))
            if "crm.deal.get" in url:
                return _Resp({"result": {"ID": "D-7", "CONTACT_ID": None, "STAGE_ID": stage}})
            return _Resp({"result": True})

    monkeypatch.setattr(main_mod.aiohttp, "ClientSession", _Sess)
    monkeypatch.setattr(main_mod, "BITRIX_WEBHOOK_URL", "http://bx.test")
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)


async def _bridge(monkeypatch, db, stage):
    posts = []
    _bx_session(monkeypatch, stage, posts)
    async with db.AsyncSessionLocal() as session:
        session.add(database.User(platform_user_id="br-1", channel="max", phone="+79001234567",
                                  context_json=json.dumps({"deal_id": "D-7"})))
        await session.commit()
    class _Client:
        async def send_message(self, **kw):
            return {"ok": True}

        def supports_contact_button(self):
            return False

    monkeypatch.setattr(main_mod, "get_sender", lambda *a, **k: (_Client(), "br-1"))
    main_mod.USER_CONTEXT["br-1"] = {"deal_id": "D-7", "channel": "max"}
    reply = ("<function_calls>ai_payload='" + json.dumps({"status": "visit", "report": "r"})
             + "'</function_calls>")
    await main_mod.send_smartbot_reply("br-1", {"text": reply})
    main_mod.USER_CONTEXT.pop("br-1", None)
    return [j for u, j in posts if "crm.deal.update" in u]


@pytest.mark.asyncio
async def test_bridge_sent_deal_fields_without_stage(db, monkeypatch):
    updates = await _bridge(monkeypatch, db, SENT)
    assert updates and "STAGE_ID" not in updates[0]["fields"]
    assert updates[0]["fields"].get("UF_CRM_666D69C9EEE1B") == "Визит в салон"   # поля пишутся


@pytest.mark.asyncio
async def test_bridge_whitelisted_deal_stage_written(db, monkeypatch):
    updates = await _bridge(monkeypatch, db, "C9:UC_CKY0IR")
    assert updates and updates[0]["fields"]["STAGE_ID"] == ENRICH


def test_enrich_fields_allowed_rules():
    fields = {"STAGE_ID": ENRICH, "UF_X": "1"}
    assert main_mod.enrich_fields_allowed(fields, SENT, "D", "t") == {"UF_X": "1"}
    assert main_mod.enrich_fields_allowed(fields, "C9:LOSE", "D", "t") == fields
    assert main_mod.enrich_fields_allowed(fields, None, "D", "t") == fields       # не прочитана — как раньше
    other = {"STAGE_ID": "C9:LOSE"}
    assert main_mod.enrich_fields_allowed(other, SENT, "D", "t") == other


async def _callback_fields(db, monkeypatch, stage):
    from test_n8n_status_gate import VALID_PHONE, _call, _mk_user, _update_fields, _wire
    http_calls, logs, alerts = [], [], []
    _wire(monkeypatch, http_calls, logs, alerts)
    _bx_session(monkeypatch, stage, http_calls)
    await _mk_user(db, f"cb-{stage}", phone=VALID_PHONE)
    await _call(f"cb-{stage}", {"sb_brand": "LADA"}, status="visit")
    return _update_fields(http_calls)


@pytest.mark.asyncio
async def test_callback_visit_on_sent_deal_fields_without_stage(db, monkeypatch):
    """В3: колбэк visit, сделка уже в «Отправлено» → стадия не пишется, поля пишутся."""
    fields = await _callback_fields(db, monkeypatch, SENT)
    assert "STAGE_ID" not in fields
    assert fields["UF_CRM_1770632572"] == "LADA" and fields["UF_CRM_666D69C9EEE1B"] == "Визит в салон"


@pytest.mark.asyncio
async def test_callback_visit_from_whitelist_writes_enrich(db, monkeypatch):
    fields = await _callback_fields(db, monkeypatch, "C9:UC_CKY0IR")
    assert fields["STAGE_ID"] == ENRICH


# ---------------- U3: досылка — белый список и таймаут стадии ----------------

@pytest.mark.asyncio
async def test_replay_keyless_from_whitelist_sent(db, bitrix):
    fake = await bitrix("C9:NEW")
    await _task({"id": "53859", "fields": {"STAGE_ID": ENRICH}})
    assert await _replay() == []
    assert fake.stage_writes == [ENRICH] and fake.reads == 1


@pytest.mark.asyncio
async def test_replay_keyless_stage_task_uses_stage_timeout(db, bitrix, monkeypatch):
    """P2.4: задача со STAGE_ID без ключа — таймаут стадии, а не 10 с. Роботы
    идут 1 с: с таймаутом стадии (2 с) задача доходит; с обычным (0.3 с) — нет."""
    fake = await bitrix("C9:NEW", ["hang_apply"])
    monkeypatch.setattr(main_mod, "BITRIX_STAGE_TIMEOUT", aiohttp.ClientTimeout(total=2))
    await _task({"id": "53859", "fields": {"STAGE_ID": ENRICH}})
    assert await _replay() == []
    assert fake.stage_writes == [ENRICH] and fake.stage == SENT


@pytest.mark.asyncio
async def test_replay_merged_task_with_key_checks_main_deal(db, bitrix):
    """Склейка: задача сделки бота переписана на основную (ключ «было» сохранён) →
    досылка читает основную сделку; та в «Отправлено» → стадия не пишется."""
    fake = await bitrix(SENT)
    params = {"id": "B-1", "fields": {"STAGE_ID": ENRICH}, main_mod.STAGE_EXPECT_KEY: "C9:NEW"}
    assert cabinet_service._rewrite_deal_refs(params, "B-1", "T-1")
    assert params[main_mod.STAGE_EXPECT_KEY] == "C9:NEW"
    await _task(params)
    assert await _replay() == []
    assert fake.stage_writes == []
