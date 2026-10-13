"""2026-10-03: Битрикс не вернул ID сделки → сделки нет (None), а не строка
«None»: active_deal_id пустой, в n8n deal_id null, охоты и обращений к сделке
с ID «None» нет; старые строки «None» в БД читаются как отсутствие сделки."""
import json

import pytest
from sqlalchemy import select

import database
from database import User
import main as main_mod
from test_payload import TestDealerFieldsInRequestBody, _CaptureSession


@pytest.mark.parametrize("value,expected", [
    (None, None), ("", None), ("None", None), ("null", None), (" none ", None), (0, None),
    (False, None), (53109, "53109"), ("53109", "53109"), ("D-7", "D-7"),
])
def test_deal_ref(value, expected):
    assert main_mod.deal_ref(value) == expected


def _bitrix(monkeypatch, deal_add_result):
    calls = []

    async def fake_request(method, params, user_id=None, **kw):
        calls.append(method)
        if method == "crm.contact.list":
            return {"result": []}
        if method == "crm.contact.add":
            return {"result": 777}
        if method == "crm.deal.add":
            return deal_add_result
        return {"result": True}

    monkeypatch.setattr(main_mod.BitrixService, "_request", fake_request)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [{}, {"result": None}, {"result": False}, {"error": "x"}])
async def test_create_deal_without_id_returns_none(monkeypatch, answer):
    _bitrix(monkeypatch, answer)
    deal_id = await main_mod.BitrixService.create_deal_with_contact(
        "Тест", "", "", {"model": "HAVAL M6"}, "991111QA", user_id="test-u1")
    assert deal_id is None


@pytest.mark.asyncio
async def test_create_deal_with_id_returns_string(monkeypatch):
    _bitrix(monkeypatch, {"result": 555})
    assert await main_mod.BitrixService.create_deal_with_contact(
        "Тест", "", "", {}, "991111QA", user_id="test-u2") == "555"


async def _user(db, monkeypatch, uid, active_deal_id=None):
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)

    async def no_alert(*a, **k):
        return True

    monkeypatch.setattr(main_mod, "send_alert_to_max_group", no_alert)
    async with db.AsyncSessionLocal() as session:
        session.add(User(platform_user_id=uid, channel="web", full_name="Тест",
                         active_deal_id=active_deal_id, context_json="{}"))
        await session.commit()


async def _active(db, uid):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(select(User.active_deal_id)
                                      .where(User.platform_user_id == uid))).scalar_one()


@pytest.mark.asyncio
async def test_final_sync_without_deal_id_keeps_active_empty(db, deps, monkeypatch):
    calls = _bitrix(monkeypatch, {})
    await _user(db, monkeypatch, "test-fs1")
    created = await main_mod.BitrixService.final_sync_wrapper(
        "test-fs1", {"scenario_type": "credit", "chat_history": [{"role": "user", "text": "привет"}]},
        skip_tg=True, skip_stage=True)
    assert created is None, "нет ID — охота не запускается (вызывающий проверяет результат)"
    assert await _active(db, "test-fs1") is None
    assert "crm.timeline.comment.add" not in calls, "комментарий в сделку «None» не пишется"


@pytest.mark.asyncio
async def test_legacy_none_string_treated_as_no_deal(db, deps, monkeypatch):
    """Строка «None» от старой ошибки: crm.deal.get по ней не вызывается."""
    calls = _bitrix(monkeypatch, {"result": 555})
    await _user(db, monkeypatch, "test-fs2", active_deal_id="None")
    created = await main_mod.BitrixService.final_sync_wrapper(
        "test-fs2", {"scenario_type": "credit", "chat_history": []}, skip_tg=True, skip_stage=True)
    assert "crm.deal.get" not in calls
    assert created == "555" and await _active(db, "test-fs2") == "555"


@pytest.mark.asyncio
async def test_n8n_payloads_carry_null_deal_id(monkeypatch):
    TestDealerFieldsInRequestBody()._patch(monkeypatch)
    await main_mod.send_chat_message_to_n8n(
        "test-pl1", "вопрос", "Имя",
        {"start_code": "010101", "scenario_type": "credit", "chat_history": [], "deal_id": "None"})
    assert _CaptureSession.captured["qualification_context"]["deal_id"] is None

    user = User(platform_user_id="test-pl2", channel="web", full_name="Тест", active_deal_id="None")
    await main_mod.send_handoff_webhook(
        "test-pl2", {"start_code": "010101", "scenario_type": "credit", "chat_history": []}, user)
    assert _CaptureSession.captured["qualification_context"]["deal_id"] is None


def test_handoff_branch_uses_deal_ref():
    """Ветка передачи ИИ: проверка «сделка есть» и перевод стадии — по deal_ref,
    строка «None» не уходит в update_deal_stage_direct."""
    src = open(main_mod.__file__, encoding="utf-8").read()
    assert "if not deal_ref(db_user.active_deal_id):" in src
    assert "deal_id_str = deal_ref(db_user.active_deal_id)" in src
    assert "deal_id = str(res_d.get('result'))" not in src
