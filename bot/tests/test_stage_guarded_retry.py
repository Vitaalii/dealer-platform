"""План 2026-10-12-001: условная смена стадии. Битрикс выполняет роботов
стадии внутри crm.deal.update («Обогащение» → «Отправлено» + письма дилеру);
ложный таймаут клиента с повтором той же записи возвращал сделку в
«Обогащение», и письма уходили второй раз (53821, 53859, 53893).

Битрикс здесь — настоящий HTTP-сервер: в коде до правки видны собственные
ретраи _request, и случай 53859 воспроизводится как на бою."""
import asyncio
import json

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select

import cabinet_service
import database
import main as main_mod
from database import PendingSync, User

from conftest import PHONE
from test_web_entry_conversion import (  # noqa: F401 — фикстуры модуля
    _capture, _web_session, dealers, nocodb_tables, real_conversion)

ENRICH = "C9:UC_0SOJJA"        # «Обогащение»
QUALIF = "C9:PREPAYMENT_INVOICE"
SENT = "C9:SENT-BY-ROBOT"      # «Отправлено» — код в боте не нужен, ставит робот
HANG = 1.0                      # «Битрикс выполняет роботов» дольше таймаута клиента


def _robot(stage):
    return SENT if stage == ENRICH else stage


class FakeBitrix:
    """Сделка с одной стадией и сценарием ответов на записи стадии.

    Сценарии записи: ok | hang_apply (применилось, клиент не дождался) |
    hang_noapply (не применилось) | hang_apply_late (применится только
    после первого проверочного чтения — Д1) | manager (не применилось,
    менеджер сдвинул сделку)."""

    def __init__(self, stage, script=()):
        self.stage = stage
        self.script = list(script)
        self.stage_writes = []
        self.reads = 0
        self.read_fail = False
        self.late = None
        self.stage_bodies = []
        self.field_bodies = []
        self.fail_from_read = None     # чтения с этого номера падают
        self.others = {}               # стадии других сделок (охота: сделка бота)

    async def handler(self, request):
        method = request.match_info["method"]
        body = await request.json()
        if method == "crm.deal.get":
            self.reads += 1
            if self.late and self.reads >= self.late[1]:
                self.stage, self.late = _robot(self.late[0]), None
            if self.read_fail or (self.fail_from_read and self.reads >= self.fail_from_read):
                return web.json_response({"error": "x"}, status=503)
            return web.json_response({"result": {"ID": str(body.get("id")), "STAGE_ID": self.stage,
                                                 "CONTACT_ID": None}})
        fields = body.get("fields") or {}
        if method == "crm.deal.update" and "STAGE_ID" in fields and str(body.get("id")) in self.others:
            self.others[str(body.get("id"))] = fields["STAGE_ID"]
            self.stage_writes.append(fields["STAGE_ID"])
            return web.json_response({"result": True})
        if method == "crm.deal.update" and "STAGE_ID" in fields:
            target = fields["STAGE_ID"]
            self.stage_writes.append(target)
            self.stage_bodies.append(body)
            # сценарий — только для «Обогащения» (закрытие дубля охотой — штатно)
            action = self.script.pop(0) if self.script and target == ENRICH else "ok"
            if action == "ok":
                self.stage = _robot(target)
                return web.json_response({"result": True})
            if action == "hang_apply":
                self.stage = _robot(target)
            elif action == "hang_apply_late":
                # первое проверочное чтение ещё видит «было», второе — уже итог
                self.late = (target, self.reads + 2)
            elif action == "manager":
                self.stage = "C9:MANAGER-MOVED"
            await asyncio.sleep(HANG)
            return web.json_response({"result": True})
        if method == "crm.deal.update":
            self.field_bodies.append(body)
        return web.json_response({"result": True})


@pytest_asyncio.fixture
async def bitrix(monkeypatch):
    servers = []

    async def make(stage, script=()):
        fake = FakeBitrix(stage, script)
        app = web.Application()
        app.router.add_post("/{method}", fake.handler)
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        monkeypatch.setattr(main_mod.BitrixService, "WEBHOOK", f"http://{server.host}:{server.port}/")
        return fake

    fast = aiohttp.ClientTimeout(total=0.3, connect=0.3)
    monkeypatch.setattr(main_mod, "BITRIX_TIMEOUT", fast)
    monkeypatch.setattr(main_mod, "BITRIX_STAGE_TIMEOUT", fast, raising=False)
    monkeypatch.setattr(main_mod, "BITRIX_RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(main_mod, "BITRIX_STAGE_SETTLE_DELAYS", (0, 0), raising=False)
    monkeypatch.setattr(main_mod.BitrixService, "STAGE_QUALIFIED", ENRICH)
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)
    yield make
    for s in servers:
        await s.close()


async def _chat_user(db, monkeypatch, deal_id="53859"):
    async def fake_card(uid, c):
        return True

    monkeypatch.setattr(main_mod, "send_alert_to_max_group", fake_card)
    async with db.AsyncSessionLocal() as session:
        session.add(User(platform_user_id="chat-53859", channel="max", full_name="Юрий",
                         phone=PHONE, active_deal_id=deal_id, context_json="{}"))
        await session.commit()
    return "chat-53859"


CHAT_CTX = {"payment": "10000", "scenario_type": "combo", "plate": "У573кв37"}


async def _queued():
    async with database.AsyncSessionLocal() as session:
        rows = (await session.execute(select(PendingSync))).scalars().all()
    return [json.loads(r.payload) for r in rows]


# ---------------- Чат: финальная синхронизация (Р1) ----------------

@pytest.mark.asyncio
async def test_53859_applied_timeout_no_second_stage_write(db, bitrix, monkeypatch, caplog):
    """Случай 53859: запись «Обогащения» применилась, робот перевёл в
    «Отправлено», клиент получил таймаут → второй записи стадии нет."""
    fake = await bitrix(QUALIF, ["hang_apply"])
    uid = await _chat_user(db, monkeypatch)
    caplog.set_level("INFO")
    await main_mod.BitrixService.final_sync_wrapper(uid, dict(CHAT_CTX))
    assert fake.stage_writes == [ENRICH]                 # одна запись — одни письма
    assert fake.stage == SENT
    assert not [p for p in await _queued() if "STAGE_ID" in (p.get("fields") or {})]
    assert any("повтор пропущен" in r.getMessage() and "deal=53859" in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_d1_late_apply_after_first_read_no_second_write(db, bitrix, monkeypatch):
    """Д1: первое проверочное чтение ещё видит «было» (Битрикс не закончил),
    второе — уже «Отправлено» → повтора нет. Чтений 4: сделка в начале
    синхронизации, свежее чтение перед записью (план 2026-10-12-002, В4), два проверочных."""
    fake = await bitrix(QUALIF, ["hang_apply_late"])
    uid = await _chat_user(db, monkeypatch)
    await main_mod.BitrixService.final_sync_wrapper(uid, dict(CHAT_CTX))
    assert fake.stage_writes == [ENRICH] and fake.stage == SENT and fake.reads == 4


@pytest.mark.asyncio
async def test_d2_check_read_failed_no_blind_write_task_with_expect(db, bitrix, monkeypatch):
    """Д2: проверочное чтение после сбоя не удалось → записи вслепую нет,
    задача в pending_sync со стадией «было»."""
    fake = await bitrix(QUALIF, ["hang_noapply"])
    uid = await _chat_user(db, monkeypatch)
    # чтения 1–2 (сделка в начале синхронизации и свежее чтение перед записью,
    # план 2026-10-12-002, В4) проходят; проверочные после сбоя — падают
    fake.fail_from_read = 3
    await main_mod.BitrixService.final_sync_wrapper(uid, dict(CHAT_CTX))
    assert fake.stage_writes == [ENRICH]
    stage_tasks = [p for p in await _queued() if "STAGE_ID" in (p.get("fields") or {})]
    assert stage_tasks == [{"id": "53859", "fields": {"STAGE_ID": ENRICH},
                            main_mod.STAGE_EXPECT_KEY: QUALIF}]


@pytest.mark.asyncio
async def test_real_failure_not_applied_retry_succeeds(db, bitrix, monkeypatch):
    fake = await bitrix(QUALIF, ["hang_noapply", "ok"])
    uid = await _chat_user(db, monkeypatch)
    await main_mod.BitrixService.final_sync_wrapper(uid, dict(CHAT_CTX))
    assert fake.stage_writes == [ENRICH, ENRICH] and fake.stage == SENT
    assert not await _queued()


@pytest.mark.asyncio
async def test_two_failures_still_before_go_to_queue_with_expect(db, bitrix, monkeypatch):
    fake = await bitrix(QUALIF, ["hang_noapply", "hang_noapply"])
    uid = await _chat_user(db, monkeypatch)
    await main_mod.BitrixService.final_sync_wrapper(uid, dict(CHAT_CTX))
    assert fake.stage_writes == [ENRICH, ENRICH]          # не больше двух записей
    assert [p for p in await _queued() if "STAGE_ID" in (p.get("fields") or {})] == [
        {"id": "53859", "fields": {"STAGE_ID": ENRICH}, main_mod.STAGE_EXPECT_KEY: QUALIF}]


@pytest.mark.asyncio
async def test_manager_moved_deal_between_attempts_not_returned(db, bitrix, monkeypatch, caplog):
    fake = await bitrix(QUALIF, ["manager"])
    uid = await _chat_user(db, monkeypatch)
    caplog.set_level("INFO")
    await main_mod.BitrixService.final_sync_wrapper(uid, dict(CHAT_CTX))
    assert fake.stage_writes == [ENRICH] and fake.stage == "C9:MANAGER-MOVED"
    assert not await _queued()


@pytest.mark.asyncio
async def test_happy_path_chat_no_extra_reads(db, monkeypatch):
    """Штатный путь: чтение сделки в начале синхронизации и одно свежее чтение
    стадии перед записью «Обогащения» (план 2026-10-12-002, решение В4)."""
    calls = _capture(monkeypatch, deal_found={"ID": "53869", "CONTACT_ID": None, "STAGE_ID": QUALIF})
    uid = await _chat_user(db, monkeypatch, deal_id="53869")
    await main_mod.BitrixService.final_sync_wrapper(uid, dict(CHAT_CTX))
    assert len([c for c in calls if c["method"] == "crm.deal.get"]) == 2
    stage = [c for c in calls if c["method"] == "crm.deal.update"
             and "STAGE_ID" in c["params"]["fields"]]
    assert len(stage) == 1
    assert stage[0]["kw"]["timeout"] is main_mod.BITRIX_STAGE_TIMEOUT    # 30 с — только стадия
    fields = [c for c in calls if c["method"] == "crm.deal.update"
              and "STAGE_ID" not in c["params"]["fields"]]
    assert fields and all(c["kw"].get("timeout") is None for c in fields)


# ---------------- Охота за сделкой Тильды (Р2) ----------------

@pytest.mark.asyncio
async def test_hunt_applied_timeout_no_second_write(db, bitrix, monkeypatch):
    fake = await bitrix("C9:NEW", ["hang_apply"])
    fake.others["B-1"] = "C9:NEW"          # сделка бота — своя стадия (охота теперь читает T-1)
    uid = await _chat_user(db, monkeypatch, deal_id="B-1")

    async def found(code, stage_id=None, user_id=None):
        return {"ID": "T-1", "STAGE_ID": "C9:NEW", "CONTACT_ID": None}

    real_sleep = asyncio.sleep

    async def skip_hunt_waits(seconds, *a, **k):
        if seconds >= 900:                 # паузы охоты — мимо; «роботы» фейка — по-настоящему
            return None
        return await real_sleep(seconds, *a, **k)

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(main_mod.BitrixService, "find_deal_data_by_code", found)
    monkeypatch.setattr(asyncio, "sleep", skip_hunt_waits)
    monkeypatch.setattr(main_mod, "sync_dialog_deal_refs", noop)
    monkeypatch.setattr(cabinet_service, "rebind_cabinet_on_merge", noop)
    await main_mod.background_deal_hunt("B-1", "021811VO", dict(CHAT_CTX), uid, "Юрий", PHONE, "")
    assert fake.stage_writes.count(ENRICH) == 1 and fake.stage == SENT
    assert "C9:LOSE" in fake.stage_writes or main_mod.BitrixService.STAGE_LOSE in fake.stage_writes


# ---------------- Кабинет и веб-вход (Р3) ----------------

@pytest.fixture
def guarded_cabinet(monkeypatch):
    monkeypatch.setattr(cabinet_service, "_set_stage",
                        getattr(main_mod.BitrixService, "set_stage_guarded", None), raising=False)


@pytest.mark.asyncio
async def test_cabinet_submit_applied_timeout_no_second_write(db, deps, real_conversion,
                                                              guarded_cabinet, bitrix, monkeypatch):
    fake = await bitrix("C9:NEW", ["hang_apply"])
    async def found(code, stage_id=None, user_id=None):
        return {"ID": "52673", "STAGE_ID": "C9:NEW", "CONTACT_ID": None}

    monkeypatch.setattr(main_mod.BitrixService, "find_deal_data_by_code", found)
    _uid, bearer = await _web_session(db, deps)
    await cabinet_service.submit_form(bearer, {"marital_status": "Холост/не замужем",
                                               "credit_term": 84, "credit_payment": 30000})
    await _drain_handoff()
    assert fake.stage_writes == [ENRICH] and fake.stage == SENT
    assert not [p for p in await _queued() if "STAGE_ID" in (p.get("fields") or {})]


async def _drain_handoff():
    """Передача из кабинета идёт в фоне (HANDOFF_IN_BACKGROUND) — дождаться задач."""
    for _ in range(50):
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()
                   and "_complete_handoff_logged" in repr(t.get_coro())]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_cabinet_happy_path_one_read_before_stage(db, deps, real_conversion, guarded_cabinet,
                                                       monkeypatch):
    calls = _capture(monkeypatch, deal_found={"ID": "52673", "CONTACT_ID": None, "STAGE_ID": "C9:NEW"})
    monkeypatch.setattr(main_mod.BitrixService, "STAGE_QUALIFIED", ENRICH)
    _uid, bearer = await _web_session(db, deps)
    await cabinet_service.submit_form(bearer, {"marital_status": "Холост/не замужем",
                                               "credit_term": 84, "credit_payment": 30000})
    await _drain_handoff()
    stage = [i for i, c in enumerate(calls) if c["method"] == "crm.deal.update"
             and "STAGE_ID" in c["params"]["fields"] and str(c["params"]["id"]) == "52673"]
    assert len(stage) == 1
    # одно чтение «было» прямо перед записью стадии; стадия — последняя запись в сделку
    assert calls[stage[0] - 1]["method"] == "crm.deal.get"
    assert stage[0] == max(i for i, c in enumerate(calls) if c["method"] == "crm.deal.update"
                           and str(c["params"]["id"]) == "52673")


# ---------------- Досылка pending_sync (Р4, Р5) ----------------

async def _task(payload, attempts=0):
    async with database.AsyncSessionLocal() as session:
        session.add(PendingSync(method="crm.deal.update", payload=json.dumps(payload),
                                user_id="u-stage", attempts=attempts, status="pending"))
        await session.commit()


async def _replay():
    async with database.AsyncSessionLocal() as session:
        await main_mod.replay_pending_sync(session)
    async with database.AsyncSessionLocal() as session:
        return (await session.execute(select(PendingSync))).scalars().all()


@pytest.mark.asyncio
async def test_replay_stage_moved_task_dropped(db, bitrix, caplog):
    fake = await bitrix(SENT)
    await _task({"id": "53859", "fields": {"STAGE_ID": ENRICH}, main_mod.STAGE_EXPECT_KEY: QUALIF})
    caplog.set_level("INFO")
    assert await _replay() == []
    assert fake.stage_writes == [] and fake.stage == SENT
    assert any("стадия не досылается, задача снята" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_replay_stage_still_before_sent_without_key(db, bitrix):
    fake = await bitrix(QUALIF)
    await _task({"id": "53859", "fields": {"STAGE_ID": ENRICH}, main_mod.STAGE_EXPECT_KEY: QUALIF})
    assert await _replay() == []
    assert fake.stage_writes == [ENRICH] and fake.stage == SENT
    assert fake.stage_bodies == [{"id": "53859", "fields": {"STAGE_ID": ENRICH}}]   # без служебного ключа


@pytest.mark.asyncio
async def test_replay_old_task_without_key_previous_path(db, bitrix):
    """Старая задача без ключа: «Обогащение» досылается только из стадии до
    передачи — чтение перед записью (план 2026-10-12-002, В4/U3). Сделка уже
    в «Отправлено» → стадия не досылается, задача снята (в v3.4.2 — вслепую)."""
    fake = await bitrix(SENT)
    await _task({"id": "53859", "fields": {"STAGE_ID": ENRICH}})
    assert await _replay() == []
    assert fake.stage_writes == [] and fake.reads == 1


@pytest.mark.asyncio
async def test_replay_read_failed_task_stays_attempts_grow(db, bitrix):
    fake = await bitrix(QUALIF)
    fake.read_fail = True
    await _task({"id": "53859", "fields": {"STAGE_ID": ENRICH}, main_mod.STAGE_EXPECT_KEY: QUALIF})
    rows = await _replay()
    assert len(rows) == 1 and rows[0].attempts == 1 and rows[0].status == "pending"
    assert fake.stage_writes == []


@pytest.mark.asyncio
async def test_replay_p5_moved_sends_other_fields_without_stage(db, bitrix):
    """Р5: «Новая → Квалификация» из очереди, сделка уже ушла дальше — стадия
    не пишется, остальные поля задачи уходят."""
    fake = await bitrix(SENT)
    await _task({"id": "53859", "fields": {"UF_SITE": "https://offer.geely-radar.ru",
                                           "STAGE_ID": QUALIF},
                 main_mod.STAGE_EXPECT_KEY: "C9:NEW"})
    assert await _replay() == []
    assert fake.stage_writes == [] and fake.stage == SENT
    assert fake.field_bodies == [{"id": "53859", "fields": {"UF_SITE": "https://offer.geely-radar.ru"}}]


@pytest.mark.asyncio
async def test_p5_chat_start_failure_enqueues_with_expect_new(db, monkeypatch):
    queued = []

    async def fake_enqueue(method, params, user_id=None, skip_debounce=False):
        queued.append(params)

    async def failing(*a, **k):
        return {}

    monkeypatch.setattr(main_mod.BitrixService, "WEBHOOK", "http://127.0.0.1:9/")
    monkeypatch.setattr(main_mod.BitrixService, "enqueue_failed_request", staticmethod(fake_enqueue))
    monkeypatch.setattr(main_mod, "BITRIX_RETRY_DELAYS", (0, 0, 0))
    await main_mod.BitrixService.update_deal_data("53859", {"site_source": "https://x"},
                                                  QUALIF, user_id="u", expect_stage="C9:NEW")
    assert queued and queued[-1][main_mod.STAGE_EXPECT_KEY] == "C9:NEW"
    assert queued[-1]["fields"]["STAGE_ID"] == QUALIF
