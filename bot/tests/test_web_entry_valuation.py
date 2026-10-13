"""Веб-вход (план 2026-09-30-001, U7): оценка трейд-ина — вебхук n8n,
кэш 24 ч, раздача результата ожидающим сессиям, зависшие оценки,
хранение данных, подменный ответ дев-стенда."""
import asyncio
import json
from datetime import timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select, update

import cabinet_service
import valuation_client
from valuation_client import ValuationResult, parse_response
from database import CabinetProfile, CabinetValuation, CabinetWebOpen

from conftest import CONSENT, PHONE

DEALERS = {
    "01": {"dealer_name": "Тест Авто", "website": "https://dealer-one.ru/", "code": "01",
           "db_config": {"catalog_table_id": "cat01"}},
}
ROW = {"Id": 7, "Model id": "15", "Complectation id": "12", "Model": "ATLAS PRO",
       "Complectation": "Люкс", "Price old": 3026990, "Trade-in bonus": 250000}
PLATE = "А123ВС77"
SUCCESS = ValuationResult("success", None, False,
                          {"mark": "Kia", "photo": "//img/x.jpg"}, 900000, 1000000)


@pytest.fixture
def dealers():
    return DEALERS


@pytest.fixture
def nocodb_tables():
    return {"cat01": [ROW]}


@pytest.fixture
def evaluator():
    """Скриптуемый клиент вебхука: calls, result, gate (ожидание ответа)."""
    class Evaluator:
        def __init__(self):
            self.calls = []
            self.result = SUCCESS
            self.gate = None

        async def __call__(self, plate, mileage, request_id):
            self.calls.append((plate, mileage, request_id))
            if self.gate is not None:
                await self.gate.wait()
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    ev = Evaluator()
    cabinet_service.set_valuation_client(ev)
    return ev


async def _session(scenario="combo", lead="011512ABCD", nonce="n" * 32, plate=PLATE,
                   phone=PHONE, deps=None):
    body = {"dealer_code": "01", "lead_code": lead, "nonce": nonce, "model_id": "15", "form": {},
            "complectation_id": "12", "scenario": scenario, "desired_payment": 30000,
            "plate": plate, "mileage": 50000}
    url = (await cabinet_service.open_web_session(body))["cabinet_url"]
    token = url.split("#t=", 1)[1]
    await cabinet_service.request_code(token, phone=phone, consent=CONSENT)
    res = await cabinet_service.verify_code(token, deps.gw.calls[-1]["code"])
    return token, f"Bearer {res['access_token']}"


async def _drain():
    for _ in range(20):
        pending = [t for t in cabinet_service._HOOK_TASKS if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


async def _me(bearer):
    return await cabinet_service.get_me(bearer)


@pytest.mark.asyncio
async def test_success_merges_into_snapshot_and_me(db, deps, evaluator):
    _, bearer = await _session(deps=deps)
    await _drain()
    assert evaluator.calls == [(PLATE, 50000, evaluator.calls[0][2])]
    me = await _me(bearer)
    assert me["valuation"] == {"status": "success", "reason": None}
    assert me["calc"]["ti_valuation"] == 1000000
    assert me["calc"]["found_info"]["photo"] == "https://img/x.jpg"
    assert me["calc"]["price"] == 3030000          # item_id/price не тронуты (без скидок)


@pytest.mark.asyncio
async def test_success_without_found_info(db, deps, evaluator):
    evaluator.result = ValuationResult("success", None, False, None, 700000, 800000)
    _, bearer = await _session(deps=deps)
    await _drain()
    me = await _me(bearer)
    assert me["valuation"]["status"] == "success"
    assert me["calc"]["ti_valuation"] == 800000 and me["calc"]["found_info"] is None


@pytest.mark.asyncio
async def test_pending_visible_before_answer(db, deps, evaluator):
    evaluator.gate = asyncio.Event()
    _, bearer = await _session(deps=deps)
    await asyncio.sleep(0)
    assert (await _me(bearer))["valuation"]["status"] == "pending"
    evaluator.gate.set()
    await _drain()
    assert (await _me(bearer))["valuation"]["status"] == "success"


@pytest.mark.asyncio
async def test_no_valuation_for_credit_scenario(db, deps, evaluator):
    _, bearer = await _session(scenario="credit", deps=deps)
    await _drain()
    assert evaluator.calls == []
    assert (await _me(bearer))["valuation"]["status"] == "none"


@pytest.mark.asyncio
async def test_not_before_authorization(db, deps, evaluator):
    body = {"dealer_code": "01", "lead_code": "011512ABCD", "nonce": "n" * 32, "form": {},
            "model_id": "15", "complectation_id": "12", "scenario": "tradein",
            "plate": PLATE, "mileage": 1}
    await cabinet_service.open_web_session(body)
    await _drain()
    assert evaluator.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("result,status", [
    (ValuationResult("not_found", "автомобиль не найден по госномеру"), "not_found"),
    (ValuationResult("failed", "отчёт Auto.ru ещё формируется"), "failed"),
])
async def test_terminal_statuses_no_retry(db, deps, evaluator, result, status):
    evaluator.result = result
    _, bearer = await _session(deps=deps)
    await _drain()
    assert (await _me(bearer))["valuation"] == {"status": status, "reason": result.reason}
    assert len(evaluator.calls) == 1


@pytest.mark.asyncio
async def test_cache_24h_same_dealer_and_plate(db, deps, evaluator):
    await _session(deps=deps)
    await _drain()
    _, bearer2 = await _session(lead="011512BBBB", nonce="b" * 32,
                                phone="+79007654321", deps=deps)
    await _drain()
    assert len(evaluator.calls) == 1
    me = await _me(bearer2)
    assert me["valuation"]["status"] == "success" and me["calc"]["ti_valuation"] == 1000000
    # через 25 ч кэш не действует
    async with db.AsyncSessionLocal() as session:
        await session.execute(update(CabinetValuation).values(
            finished_at=cabinet_service._utcnow() - timedelta(hours=25)))
        await session.commit()
    await _session(lead="011512CCCC", nonce="c" * 32, phone="+79001112233", deps=deps)
    await _drain()
    assert len(evaluator.calls) == 2


@pytest.mark.asyncio
async def test_cached_failure_reused_but_our_failure_not(db, deps, evaluator):
    evaluator.result = ValuationResult("not_found", "автомобиль не найден по госномеру")
    await _session(deps=deps)
    await _drain()
    await _session(lead="011512BBBB", nonce="b" * 32, phone="+79007654321", deps=deps)
    await _drain()
    assert len(evaluator.calls) == 1                  # неуспех Auto.ru кэшируется

    evaluator.result = ValuationResult("failed", "отказ по секрету вебхука", True)
    await _session(lead="011512CCCC", nonce="c" * 32, plate="Е963ТА99",
                   phone="+79001112233", deps=deps)
    await _drain()
    await _session(lead="011512DDDD", nonce="d" * 32, plate="Е963ТА99",
                   phone="+79004445566", deps=deps)
    await _drain()
    assert len(evaluator.calls) == 3                  # наш сбой — вызов снова


@pytest.mark.asyncio
async def test_two_sessions_share_one_call(db, deps, evaluator):
    evaluator.gate = asyncio.Event()
    _, bearer1 = await _session(deps=deps)
    _, bearer2 = await _session(lead="011512BBBB", nonce="b" * 32,
                                phone="+79007654321", deps=deps)
    evaluator.gate.set()
    await _drain()
    assert len(evaluator.calls) == 1
    for bearer in (bearer1, bearer2):
        me = await _me(bearer)
        assert me["valuation"]["status"] == "success" and me["calc"]["ti_valuation"] == 1000000


@pytest.mark.asyncio
async def test_client_exception_is_our_failure(db, deps, evaluator):
    evaluator.result = RuntimeError("boom")
    _, bearer = await _session(deps=deps)
    await _drain()
    me = await _me(bearer)
    assert me["valuation"]["status"] == "failed"
    async with db.AsyncSessionLocal() as session:
        row = (await session.execute(select(CabinetValuation))).scalar_one()
    assert row.our_failure is True


@pytest.mark.asyncio
async def test_stale_pending_becomes_failed_without_new_call(db, deps, evaluator):
    evaluator.gate = asyncio.Event()          # вызов «завис» (рестарт бота)
    _, bearer = await _session(deps=deps)
    async with db.AsyncSessionLocal() as session:
        await session.execute(update(CabinetValuation).values(
            started_at=cabinet_service._utcnow() - timedelta(seconds=120)))
        await session.commit()
    stats = await cabinet_service.otp_worker_tick()
    assert stats["stale_valuations"] == 1
    me = await _me(bearer)
    assert me["valuation"]["status"] == "failed"
    evaluator.gate.set()                      # поздний ответ не переписывает закрытую
    await _drain()
    assert (await _me(bearer))["valuation"]["status"] == "failed"
    assert len(evaluator.calls) == 1


@pytest.mark.asyncio
async def test_retention_cleanup(db, deps):
    now = cabinet_service._utcnow()
    async with db.AsyncSessionLocal() as session:
        session.add_all([
            CabinetWebOpen(ip="1", dealer_code="01", created_at=now - timedelta(hours=49)),
            CabinetWebOpen(ip="2", dealer_code="01", created_at=now - timedelta(hours=47)),
            CabinetValuation(dealer_code="01", plate="А1", status="success",
                             found_info='{"a": 1}', started_at=now - timedelta(days=31)),
            CabinetValuation(dealer_code="01", plate="А2", status="success",
                             found_info='{"a": 1}', started_at=now - timedelta(days=29)),
        ])
        await session.commit()
    stats = await cabinet_service.otp_worker_tick()
    assert stats["web_entry_cleanup"] == {"opens_deleted": 1, "valuations_anonymized": 1,
                                          "web_forms_cleared": 0}
    async with db.AsyncSessionLocal() as session:
        opens = [o.ip for o in (await session.execute(select(CabinetWebOpen))).scalars()]
        vals = {v.id: (v.plate, v.found_info) for v in
                (await session.execute(select(CabinetValuation))).scalars()}
    assert opens == ["2"]
    assert sorted(vals.values(), key=str) == sorted([(None, None), ("А2", '{"a": 1}')], key=str)


# ---- valuation_client: разбор ответов, конфигурация, подмена ----

@pytest.mark.parametrize("http,body,status,our", [
    (200, {"status": "SUCCESS", "tradein_price_to": 1000000, "found_info": {"m": 1}}, "success", False),
    (200, {"status": "SUCCESS", "tradein_price_to": "800000"}, "success", False),
    (200, {"status": "SUCCESS", "found_info": {"m": 1}}, "found_no_price", False),
    (200, {"status": "SUCCESS"}, "failed", False),
    (200, {"status": "NOT_FOUND"}, "not_found", False),
    (200, {"status": "INVALID_DATA"}, "not_found", False),
    (200, {"status": "IN_PROGRESS"}, "failed", False),
    (200, {"status": "NO_REGION"}, "failed", False),
    (400, {"status": "BAD_REQUEST"}, "failed", True),
    (401, None, "failed", True),
    (403, None, "failed", True),
    (502, {"status": "ERROR"}, "failed", True),
    (503, None, "failed", True),
    (200, "not json", "failed", True),
    (200, {"status": "WHAT"}, "failed", True),
])
def test_parse_response_table(http, body, status, our):
    r = parse_response(http, body)
    assert (r.status, r.our_failure) == (status, our)


@pytest.mark.parametrize("bot_env,environment,flag,enabled", [
    ("dev", "", "1", True),
    ("dev", "production", "1", False),
    ("prod", "", "1", False),
    ("", "", "1", False),
    ("dev", "", "", False),
])
def test_fake_flag_closed_by_default(monkeypatch, bot_env, environment, flag, enabled):
    monkeypatch.setenv("BOT_ENV", bot_env)
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("CABINET_EVAL_FAKE", flag)
    assert valuation_client.fake_enabled() is enabled
    assert valuation_client.fake_flag_misconfigured() is (flag == "1" and not enabled)


@pytest.mark.asyncio
async def test_fake_ignored_in_prod_uses_real_config(monkeypatch):
    monkeypatch.setenv("BOT_ENV", "prod")
    monkeypatch.setenv("CABINET_EVAL_FAKE", "1")
    monkeypatch.delenv("N8N_CABINET_EVAL_URL_PROD", raising=False)
    r = await valuation_client.evaluate("А001АА77", 1, "rid")
    assert (r.status, r.our_failure, r.reason) == ("failed", True, "вебхук оценки не настроен (env)")


@pytest.mark.asyncio
@pytest.mark.parametrize("plate,status", [("А001АА77", "success"), ("А002АА77", "success"),
                                          ("А003АА77", "found_no_price"), ("А004АА77", "not_found"),
                                          ("А006АА77", "failed"), ("А008АА77", "failed")])
async def test_fake_plates_on_dev(monkeypatch, plate, status):
    monkeypatch.setenv("BOT_ENV", "dev")
    monkeypatch.setenv("ENVIRONMENT", "")
    monkeypatch.setenv("CABINET_EVAL_FAKE", "1")
    monkeypatch.setenv("CABINET_EVAL_FAKE_DELAY", "0")
    assert (await valuation_client.evaluate(plate, 1, "rid")).status == status


@pytest.mark.asyncio
async def test_real_call_sends_secret_header_and_payload(monkeypatch):
    seen = {}

    async def handler(request):
        seen["secret"] = request.headers.get("X-Cabinet-Eval-Secret")
        seen["body"] = await request.json()
        return web.json_response({"status": "SUCCESS", "tradein_price_to": 500000})

    app = web.Application()
    app.router.add_post("/webhook/cabinet-tradein-eval-test", handler)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("BOT_ENV", "dev")
    monkeypatch.delenv("CABINET_EVAL_FAKE", raising=False)
    monkeypatch.setenv("N8N_CABINET_EVAL_URL_TEST",
                       str(server.make_url("/webhook/cabinet-tradein-eval-test")))
    monkeypatch.setenv("N8N_CABINET_EVAL_SECRET_TEST", "s3cret")
    try:
        r = await valuation_client.evaluate("А123ВС77", 50000, "rid-1")
    finally:
        await server.close()
    assert r.status == "success" and r.price_to == 500000
    assert seen == {"secret": "s3cret",
                    "body": {"plate": "А123ВС77", "mileage": 50000, "request_id": "rid-1"}}


@pytest.mark.asyncio
async def test_real_call_timeout_is_our_failure(monkeypatch):
    async def handler(request):
        await asyncio.sleep(2)
        return web.json_response({"status": "SUCCESS"})

    app = web.Application()
    app.router.add_post("/hook", handler)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("BOT_ENV", "dev")
    monkeypatch.delenv("CABINET_EVAL_FAKE", raising=False)
    monkeypatch.setenv("N8N_CABINET_EVAL_URL_TEST", str(server.make_url("/hook")))
    monkeypatch.setenv("N8N_CABINET_EVAL_SECRET_TEST", "s")
    monkeypatch.setattr(valuation_client, "TIMEOUT_SECONDS", 0.2)
    try:
        r = await valuation_client.evaluate("А123ВС77", 1, "rid")
    finally:
        await server.close()
    assert r.status == "failed" and r.our_failure and "таймаут" in r.reason


@pytest.mark.asyncio
async def test_me_profile_carries_web_plate_mileage_and_return_url(db, deps, evaluator):
    """U8/U9: /me веб-сессии отдаёт госномер и пробег с лендинга (предзаполнение
    8a/8c/8e) и адрес возврата (8f)."""
    _, bearer = await _session(deps=deps)
    await _drain()
    prof = (await _me(bearer))["profile"]
    assert prof["web_plate"] == PLATE and prof["web_mileage"] == 50000
    assert "return_url" in prof


@pytest.mark.asyncio
async def test_steering_field_saved_validated_and_pushed(db, deps, evaluator):
    """U9 (8c/8e): «Руль» — плитки анкеты; только канонические значения;
    уходит в «Кабинет: Руль» по карте полей."""
    _, bearer = await _session(deps=deps)
    await _drain()
    await cabinet_service.save_form_draft(bearer, {"tradein_steering": "Правый"})
    assert (await _me(bearer))["form"]["tradein_steering"] == "Правый"
    with pytest.raises(cabinet_service.ApiError) as e:
        await cabinet_service.save_form_draft(bearer, {"tradein_steering": "RIGHT"})
    assert e.value.extra["fields"] == {"tradein_steering": "not_allowed"}
    assert cabinet_service.load_uf_map()["tradein_steering"] == "UF_CRM_1785857507"
