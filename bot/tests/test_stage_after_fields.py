"""Исправление 2026-09-30: смена стадии «Обогащение» — ПОСЛЕДНЯЯ запись в
сделку. Робот стадии собирает «Текст письма (квалиф)» из полей сделки в момент
смены стадии; при порядке «стадия → поля» письмо дилеру уходило с пустыми
цифрами (живая сделка 52869). Порядок вызовов — для каждого пути."""
import asyncio
import json
import types

import pytest
from sqlalchemy import select

import cabinet_service
import database
import main as main_mod
from database import CabinetEvent, CabinetForm, CabinetToken, PendingSync, User

from conftest import PHONE
from test_web_entry_conversion import (  # noqa: F401 — фикстуры модуля
    _by, _capture, _web_session, dealers, nocodb_tables, real_conversion)

ENRICH = "C9:UC_0SOJJA"


def _stage_calls(calls, deal_id=None):
    return [i for i, c in enumerate(calls)
            if c["method"] == "crm.deal.update" and "STAGE_ID" in c["params"]["fields"]
            and (deal_id is None or str(c["params"]["id"]) == str(deal_id))]


def _index(calls, pred):
    return [i for i, c in enumerate(calls) if pred(c)]


def _upd_with(calls, key, deal_id=None):
    return _index(calls, lambda c: c["method"] == "crm.deal.update"
                  and key in c["params"]["fields"]
                  and (deal_id is None or str(c["params"]["id"]) == str(deal_id)))


# ---------------- Кабинет: сабмит анкеты ----------------

@pytest.mark.asyncio
async def test_cabinet_submit_found_deal_stage_after_form_fields(db, deps, real_conversion, monkeypatch):
    calls = _capture(monkeypatch, deal_found={"ID": "52673", "CONTACT_ID": None, "STAGE_ID": "C9:NEW"})
    user_id, bearer = await _web_session(db, deps)
    await cabinet_service.submit_form(bearer, {"marital_status": "Холост/не замужем",
                                               "credit_term": 84, "credit_initial": 500000,
                                               "credit_payment": 30000})
    stage = _stage_calls(calls, "52673")
    assert len(stage) == 1 and calls[stage[0]]["params"]["fields"] == {"STAGE_ID": ENRICH}
    base = _upd_with(calls, "UF_CRM_1706098533386", "52673")       # «Кредит - платеж» конверсии
    form = _upd_with(calls, "UF_CRM_1770634992", "52673")          # «SB Кредит Е-мес_платеж» анкеты
    assert base and form and max(base + form) < stage[0]
    assert stage[0] == max(i for i, c in enumerate(calls) if c["method"] == "crm.deal.update"
                           and str(c["params"]["id"]) == "52673")


@pytest.mark.asyncio
async def test_web_bot_deal_created_in_new_moved_after_form_fields(db, deps, real_conversion, monkeypatch):
    calls = _capture(monkeypatch)
    user_id, bearer = await _web_session(db, deps)
    await cabinet_service.submit_form(bearer, {"marital_status": "Холост/не замужем",
                                               "credit_term": 84})
    add = _index(calls, lambda c: c["method"] == "crm.deal.add")
    assert calls[add[0]]["params"]["fields"]["STAGE_ID"] == "C9:NEW"
    title = _upd_with(calls, "TITLE", "555")
    form = _upd_with(calls, "UF_CRM_1770634728", "555")            # «SB Кредит Срок»
    stage = _stage_calls(calls, "555")
    assert len(stage) == 1 and add[0] < title[0] < form[0] < stage[0]
    assert "STAGE_ID" not in calls[title[0]]["params"]["fields"]


@pytest.mark.asyncio
async def test_form_fields_enqueued_then_stage_enqueued_after_them(db, deps, real_conversion, monkeypatch):
    calls = _capture(monkeypatch, deal_found={"ID": "52673", "CONTACT_ID": None, "STAGE_ID": "C9:NEW"})
    real_request = main_mod.BitrixService._request

    async def failing_form(method, params, user_id=None, **kw):
        fields = params.get("fields") or {}
        if method == "crm.deal.update" and "UF_CRM_1785857378" in fields:
            calls.append({"method": method, "params": params, "kw": kw})
            return {}                                              # Битрикс не принял анкету
        return await real_request(method, params, user_id=user_id, **kw)

    monkeypatch.setattr(main_mod.BitrixService, "_request", failing_form)
    user_id, bearer = await _web_session(db, deps)
    await cabinet_service.submit_form(bearer, {"marital_status": "Холост/не замужем"})
    assert _stage_calls(calls, "52673") == []                      # напрямую стадия не ушла
    queued = [(m, p["fields"]) for m, p, _ in deps.enqueues]
    assert [list(f) for m, f in queued][-1] == ["STAGE_ID"]         # стадия — в очереди последней
    assert any("UF_CRM_1785857378" in f for m, f in queued[:-1])     # после полей анкеты


# ---------------- Кабинет: таймаут без анкеты ----------------

@pytest.mark.asyncio
async def test_cabinet_timeout_stage_after_draft_comment_once(db, deps, real_conversion, monkeypatch):
    calls = _capture(monkeypatch, deal_found={"ID": "52673", "CONTACT_ID": None, "STAGE_ID": "C9:NEW"})
    user_id, bearer = await _web_session(db, deps)
    await cabinet_service.save_form_draft(bearer, {"credit_term": 60})
    assert await cabinet_service.attempt_handoff(user_id, trigger="timeout") == "converted"
    comments = _index(calls, lambda c: c["method"] == "crm.timeline.comment.add"
                      and "Черновик" in c["params"]["fields"]["COMMENT"])
    stage = _stage_calls(calls, "52673")
    assert comments and len(stage) == 1 and comments[-1] < stage[0]
    # повторное доведение (подбор зависших захватов) стадию второй раз не шлёт
    await cabinet_service._complete_handoff(user_id, "timeout")
    assert len(_stage_calls(calls, "52673")) == 1


# ---------------- Чат: final_sync_wrapper ----------------

async def _chat_user(db, monkeypatch, *, deal_id="D-1", ctx=None):
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)

    async def fake_card(uid, c):
        return True

    monkeypatch.setattr(main_mod, "send_alert_to_max_group", fake_card)
    async with db.AsyncSessionLocal() as session:
        user = User(platform_user_id="chat-1", channel="max", full_name="Иван",
                    phone=PHONE, active_deal_id=deal_id,
                    context_json=json.dumps(ctx or {}, ensure_ascii=False))
        session.add(user)
        await session.commit()
    return "chat-1"


CHAT_CTX = {"payment": "27000", "scenario_type": "combo", "plate": "А123ВС77",
            "evaluation_car": {"make": "KIA", "model": "RIO", "year": "2019",
                               "mileage": "50000", "plate": "А123ВС77"},
            "evaluation_value": 900000}


@pytest.mark.asyncio
async def test_chat_final_sync_stage_after_base_and_ai_fields(db, monkeypatch):
    calls = _capture(monkeypatch, deal_found={"ID": "D-1", "CONTACT_ID": None, "STAGE_ID": "C9:UC_CKY0IR"})
    uid = await _chat_user(db, monkeypatch)
    await main_mod.BitrixService.final_sync_wrapper(uid, dict(CHAT_CTX))
    base = _upd_with(calls, "UF_CRM_1706098533386", "D-1")        # «Кредит - платеж»
    ai = _upd_with(calls, "UF_CRM_1788758195", "D-1")              # «SB Трейдин Оценка autoru»
    stage = _stage_calls(calls, "D-1")
    assert base and ai and len(stage) == 1 and max(base + ai) < stage[0]
    assert calls[stage[0]]["params"]["fields"] == {"STAGE_ID": ENRICH}
    assert all("STAGE_ID" not in calls[i]["params"]["fields"] for i in base + ai)


@pytest.mark.asyncio
async def test_chat_fields_failed_stage_goes_to_queue_bypassing_debounce(db, monkeypatch):
    calls = _capture(monkeypatch, deal_found={"ID": "D-1", "CONTACT_ID": None, "STAGE_ID": "C9:UC_CKY0IR"})
    real_request = main_mod.BitrixService._request

    async def failing_fields(method, params, user_id=None, **kw):
        if method == "crm.deal.update" and "UF_CRM_1706098533386" in (params.get("fields") or {}):
            calls.append({"method": method, "params": params, "kw": kw})
            return {}
        return await real_request(method, params, user_id=user_id, **kw)

    queued = []

    async def fake_enqueue(method, params, user_id=None, skip_debounce=False):
        queued.append((method, params, skip_debounce))

    monkeypatch.setattr(main_mod.BitrixService, "_request", failing_fields)
    monkeypatch.setattr(main_mod.BitrixService, "enqueue_failed_request", staticmethod(fake_enqueue))
    uid = await _chat_user(db, monkeypatch)
    await main_mod.BitrixService.final_sync_wrapper(uid, dict(CHAT_CTX))
    assert _stage_calls(calls, "D-1") == []
    # План 2026-10-12-001, U4 (решение владельца 2026-10-12): в задаче — стадия «было»
    assert queued[-1] == ("crm.deal.update", {"id": "D-1", "fields": {"STAGE_ID": ENRICH},
                                              "_expect_stage": "C9:UC_CKY0IR"}, True)


@pytest.mark.asyncio
async def test_chat_skip_stage_sends_no_stage(db, monkeypatch):
    calls = _capture(monkeypatch, deal_found={"ID": "D-1", "CONTACT_ID": None, "STAGE_ID": "C9:NEW"})
    uid = await _chat_user(db, monkeypatch)
    await main_mod.BitrixService.final_sync_wrapper(uid, dict(CHAT_CTX), skip_stage=True)
    assert _stage_calls(calls) == []


# ---------------- Охота за сделкой Tilda ----------------

@pytest.mark.asyncio
async def test_hunt_stage_after_fields_and_cabinet_rebind(db, monkeypatch):
    calls = _capture(monkeypatch, deal_found={"ID": "T-1", "CONTACT_ID": None, "STAGE_ID": "C9:NEW"})
    uid = await _chat_user(db, monkeypatch, deal_id="B-1")

    async def no_sleep(_):
        return None

    fake_asyncio = types.SimpleNamespace(**{k: getattr(asyncio, k) for k in dir(asyncio)
                                            if not k.startswith("_")})
    fake_asyncio.sleep = no_sleep
    monkeypatch.setattr(main_mod, "asyncio", fake_asyncio)

    async def fake_rebind(bot_deal, original, user):
        calls.append({"method": "rebind", "params": {"fields": {}}, "kw": {}})

    async def no_sync(*a, **k):
        return None

    monkeypatch.setattr(cabinet_service, "rebind_cabinet_on_merge", fake_rebind)
    monkeypatch.setattr(main_mod, "sync_dialog_deal_refs", no_sync)
    await main_mod.background_deal_hunt("B-1", "011512ABCD", dict(CHAT_CTX), uid,
                                        "Иван", PHONE, "")
    fields = _upd_with(calls, "UF_CRM_1706098533386", "T-1")
    rebind = _index(calls, lambda c: c["method"] == "rebind")
    stage = _stage_calls(calls, "T-1")
    assert fields and rebind and len(stage) == 1 and fields[0] < rebind[0] < stage[0]
    assert calls[stage[0]]["params"]["fields"] == {"STAGE_ID": ENRICH}


# ---------------- Колбэк n8n: поля и стадия одним вызовом ----------------

def test_n8n_callback_and_bridge_write_fields_with_stage_in_one_update():
    """Путь Александра через n8n уже атомарен: SB-поля и STAGE_ID уходят одним
    crm.deal.update (fields_to_update), гонки с роботом нет — пин регресса."""
    src = (main_mod.__file__ and open(main_mod.__file__, encoding="utf-8").read())
    for anchor in ("async def handle_n8n_callback", "АВАРИЙНЫЙ МОСТ ДЛЯ БИТРИКСА"):
        body = src[src.index(anchor):]
        body = body[:body.index("fields_to_update = await cabinet_service.filter_cabinet_owned_fields")]
        assert 'fields_to_update = {"STAGE_ID": new_stage}' in body
        assert "BITRIX_SMART_FIELDS_MAP.items()" in body


# ---------------- Реплей очереди: стадия не обгоняет поля ----------------

class _Resp:
    def __init__(self, ok):
        self.status = 200
        self._ok = ok

    async def json(self):
        return {"result": True} if self._ok else {"error": "QUERY_LIMIT_EXCEEDED"}


class _StageResp:
    def __init__(self, stage):
        self.status = 200
        self._stage = stage

    async def json(self):
        return {"result": {"ID": "D", "STAGE_ID": self._stage}}


class _Ctx:
    def __init__(self, r):
        self._r = r

    async def __aenter__(self):
        return self._r

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_replay_keeps_stage_behind_failed_fields_of_same_deal(db, monkeypatch):
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)
    sent = []

    class FakeSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None):
            if url.endswith("crm.deal.get"):
                # чтение стадии перед досылкой «Обогащения» (план 2026-10-12-002, В4)
                return _Ctx(_StageResp("C9:UC_CKY0IR"))
            sent.append(json)
            return _Ctx(_Resp("UF_FAIL" not in (json.get("fields") or {})))

    monkeypatch.setattr(main_mod, "aiohttp", types.SimpleNamespace(ClientSession=FakeSession))
    monkeypatch.setattr(main_mod, "BITRIX_RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(main_mod, "TECH_ADMIN_ID", "")
    monkeypatch.setattr(main_mod, "SUPPORT_GROUP_ID", "")
    rows = [({"id": "D-1", "fields": {"UF_FAIL": "1"}}),
            ({"id": "D-1", "fields": {"STAGE_ID": ENRICH}}),
            ({"id": "D-2", "fields": {"UF_OK": "1"}}),
            ({"id": "D-2", "fields": {"STAGE_ID": ENRICH}})]
    async with db.AsyncSessionLocal() as session:
        for p in rows:
            session.add(PendingSync(method="crm.deal.update", payload=json.dumps(p),
                                    user_id="u", status="pending"))
        await session.commit()
    async with db.AsyncSessionLocal() as session:
        await main_mod.replay_pending_sync(session)
    assert sent == [rows[0], rows[2], rows[3]]                     # стадия D-1 ждёт
    async with db.AsyncSessionLocal() as session:
        left = [json.loads(t.payload) for t in (await session.execute(
            select(PendingSync).order_by(PendingSync.id))).scalars()]
    assert left == [rows[0], rows[1]]
