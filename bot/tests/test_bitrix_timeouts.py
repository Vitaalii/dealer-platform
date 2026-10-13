"""2026-10-02: таймауты запросов к Битриксу (_request), поиск сделки при
открытии веб-сессии — одна попытка с коротким таймаутом; прогрев ставок и программ."""
import asyncio
import time

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

import main as main_mod


@pytest_asyncio.fixture
async def hung_bitrix():
    """Битрикс, который принимает соединение и не отвечает 5 с."""
    calls = []

    async def handler(request):
        calls.append(request.path)
        await asyncio.sleep(5)
        return web.json_response({"result": []})

    app = web.Application()
    app.router.add_post("/{method}", handler)
    server = TestServer(app)
    await server.start_server()
    yield f"http://{server.host}:{server.port}/", calls
    await server.close()


@pytest.mark.asyncio
async def test_request_times_out_each_attempt_and_retries(hung_bitrix, monkeypatch):
    url, calls = hung_bitrix
    monkeypatch.setattr(main_mod.BitrixService, "WEBHOOK", url)
    monkeypatch.setattr(main_mod, "BITRIX_TIMEOUT", aiohttp.ClientTimeout(total=0.3, connect=0.3))
    monkeypatch.setattr(main_mod, "BITRIX_RETRY_DELAYS", (0, 0, 0))
    t0 = time.monotonic()
    res = await main_mod.BitrixService._request("crm.deal.update", {"id": 1}, enqueue_on_failure=False)
    elapsed = time.monotonic() - t0
    assert res == {} and len(calls) == 3                 # три попытки, каждая оборвана
    assert elapsed < 2.0                                  # не 5 с на попытку


def test_request_timeout_defaults():
    assert main_mod.BITRIX_TIMEOUT.total == 10 and main_mod.BITRIX_TIMEOUT.connect == 3
    assert main_mod.BITRIX_RETRY_DELAYS == (0, 1, 3)
    src = open(main_mod.__file__, encoding="utf-8").read()
    assert "aiohttp.ClientSession(timeout=BITRIX_TIMEOUT)" in src


@pytest.mark.asyncio
async def test_deal_lookup_single_attempt_short_timeout(hung_bitrix, monkeypatch):
    url, calls = hung_bitrix
    monkeypatch.setattr(main_mod.BitrixService, "WEBHOOK", url)
    monkeypatch.setattr(main_mod, "BITRIX_LOOKUP_TIMEOUT", aiohttp.ClientTimeout(total=0.3))
    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await main_mod._cabinet_find_deal_by_code("991111ABCD")
    assert len(calls) == 1 and time.monotonic() - t0 < 1.5   # одна попытка, без повторов


def test_deal_lookup_default_timeout():
    assert main_mod.BITRIX_LOOKUP_TIMEOUT.total == 2.5


@pytest.mark.asyncio
async def test_deal_lookup_bitrix_off_returns_none(monkeypatch):
    monkeypatch.setattr(main_mod.BitrixService, "WEBHOOK", "")
    assert await main_mod._cabinet_find_deal_by_code("991111ABCD") is None


@pytest.mark.asyncio
async def test_warm_rates_programs_forces_refresh_of_unique_tables(monkeypatch):
    monkeypatch.setattr(main_mod, "CACHED_DEALERS", {
        "01": {"db_config": {"rates_table_id": "r1", "program_table_id": "p1"}},
        "02": {"db_config": {"rates_table_id": "r1", "program_table_id": "p2"}},
        "03": {"db_config": {}},
    })
    seen = []

    async def fake_loader(table_id, force=False):
        seen.append((table_id, force))
        return []

    monkeypatch.setattr(main_mod, "get_cars_from_nocodb_dynamic", fake_loader)
    assert await main_mod.warm_rates_programs_once() == 3
    assert sorted(seen) == [("p1", True), ("p2", True), ("r1", True)]


def test_warm_worker_registered_at_startup():
    src = open(main_mod.__file__, encoding="utf-8").read()
    assert "app['rates_programs_warm_worker'] = asyncio.create_task(rates_programs_warm_worker())" in src


# =======================
# 2026-10-02: тот же таймаут — в реплее очереди, колбэке n8n и аварийном мосте
# =======================

import json as _json

import database
from database import PendingSync, User


@pytest_asyncio.fixture
async def hung_any_path():
    """Зависший Битрикс на любом пути (мост и колбэк шлют `{url}/метод`)."""
    calls = []

    async def handler(request):
        calls.append(request.path)
        await asyncio.sleep(5)
        return web.json_response({"result": True})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    yield f"http://{server.host}:{server.port}", calls
    await server.close()


@pytest.fixture
def short_timeout(monkeypatch):
    monkeypatch.setattr(main_mod, "BITRIX_TIMEOUT", aiohttp.ClientTimeout(total=0.3, connect=0.3))


async def _mk_chat_user(uid, phone=None, ctx=None):
    async with database.AsyncSessionLocal() as session:
        session.add(User(platform_user_id=uid, channel="max", phone=phone,
                         context_json=_json.dumps(ctx or {}, ensure_ascii=False)))
        await session.commit()
    main_mod.USER_CONTEXT.pop(uid, None)


@pytest.mark.asyncio
async def test_replay_hung_task_times_out_and_counts_failure(db, hung_any_path, short_timeout, monkeypatch):
    url, calls = hung_any_path
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)
    monkeypatch.setattr(main_mod.BitrixService, "WEBHOOK", url + "/")
    async with database.AsyncSessionLocal() as session:
        session.add(PendingSync(method="crm.deal.update", payload=_json.dumps({"id": 1}),
                                user_id="u-hung", attempts=0, status="pending"))
        await session.commit()
    t0 = time.monotonic()
    async with database.AsyncSessionLocal() as session:
        await main_mod.replay_pending_sync(session)
    assert time.monotonic() - t0 < 2.0 and len(calls) == 1
    async with database.AsyncSessionLocal() as session:
        from sqlalchemy import select
        row = (await session.execute(select(PendingSync).where(
            PendingSync.user_id == "u-hung"))).scalar_one()
    assert row.status == "pending" and row.attempts == 1   # неуспех учтён, строка цела


@pytest.mark.asyncio
async def test_n8n_callback_background_update_times_out(db, hung_any_path, short_timeout, monkeypatch):
    url, calls = hung_any_path
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)
    monkeypatch.setattr(main_mod, "BITRIX_WEBHOOK_URL", url)
    errors = []
    monkeypatch.setattr(main_mod, "log_session",
                        lambda target, msg, level="INFO": errors.append(str(msg)) if level == "ERROR" else None)
    await _mk_chat_user("g-hung-cb")

    class _Req:
        async def json(self):
            return {"user_id": "g-hung-cb", "deal_id": "D-7", "status": "in_progress",
                    "ai_payload": "{}"}

    before = set(asyncio.all_tasks())
    t0 = time.monotonic()
    resp = await main_mod.handle_n8n_callback(_Req())
    assert resp.status == 200
    bg = [t for t in asyncio.all_tasks() - before if not t.done()]
    await asyncio.wait_for(asyncio.gather(*bg, return_exceptions=True), timeout=3)
    assert time.monotonic() - t0 < 2.0 and calls                    # не 5 с ожидания
    assert any("Фоновая отправка в Bitrix упала" in e for e in errors)


@pytest.mark.asyncio
async def test_emergency_bridge_times_out(db, hung_any_path, short_timeout, monkeypatch):
    url, calls = hung_any_path
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)
    monkeypatch.setattr(main_mod, "BITRIX_WEBHOOK_URL", url)
    errors = []
    monkeypatch.setattr(main_mod, "log_session",
                        lambda target, msg, level="INFO": errors.append(str(msg)) if level == "ERROR" else None)
    await _mk_chat_user("g-hung-br", ctx={"deal_id": "D-7", "channel": "max"})
    text = "<function_calls>ai_payload='" + _json.dumps({"status": "visit"}) + "'</function_calls>"
    t0 = time.monotonic()
    await asyncio.wait_for(main_mod.send_smartbot_reply("g-hung-br", {"text": text}), timeout=3)
    assert time.monotonic() - t0 < 2.0
    assert any("/crm.deal.update" in c for c in calls)
    assert any("аварийного моста" in e for e in errors)


def test_three_raw_bitrix_paths_use_timeout():
    src = open(main_mod.__file__, encoding="utf-8").read()
    assert src.count("aiohttp.ClientSession(timeout=BITRIX_TIMEOUT)") >= 4   # _request + 3 пути


# =======================
# 2026-10-03: таймаут в оставшихся прямых запросах — стенограмма при
# завершении диалога, смена стадии чата, закрытие брошенных диалогов 48 ч
# =======================

import types as _types

import cabinet_service as _cabinet_service


@pytest.mark.asyncio
async def test_finalize_transcript_times_out(db, hung_any_path, short_timeout, monkeypatch):
    url, calls = hung_any_path
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)
    monkeypatch.setattr(main_mod, "BITRIX_WEBHOOK_URL", url)
    await _mk_chat_user("g-hung-fin", ctx={"chat_history": [{"role": "user", "text": "привет"}],
                                           "channel": "max"})
    t0 = time.monotonic()
    ok = await asyncio.wait_for(main_mod.finalize_transcript_archive(
        "g-hung-fin", {"deal_id": "D-7", "status": "call", "report_text": "итог"}), timeout=3)
    assert ok is False and time.monotonic() - t0 < 2.0       # сбой — повтор у вызывающего
    assert any("/crm.timeline.comment.add" in c for c in calls)


@pytest.mark.asyncio
async def test_stage_direct_times_out(hung_any_path, short_timeout, monkeypatch):
    url, calls = hung_any_path
    monkeypatch.setattr(main_mod, "BITRIX_WEBHOOK_URL", url)
    t0 = time.monotonic()
    await asyncio.wait_for(main_mod.update_deal_stage_direct("D-7", "C9:UC_HW8Q2H"), timeout=3)
    assert time.monotonic() - t0 < 2.0 and any("/crm.deal.update" in c for c in calls)


@pytest.mark.asyncio
async def test_abandoned_dialog_48h_times_out(db, hung_any_path, short_timeout, monkeypatch):
    """Брошенный диалог старше 48 ч: стенограмма в таймлайн обрывается по
    таймауту, проход воркера не висит."""
    url, calls = hung_any_path
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)
    monkeypatch.setattr(main_mod.BitrixService, "WEBHOOK", url + "/")
    monkeypatch.setattr(main_mod, "TECH_ADMIN_ID", "")

    async def _noop(*a, **kw):
        return None

    async def _no_sender(*a, **kw):
        return None, None

    monkeypatch.setattr(main_mod, "upsert_dialogue_archive", _noop)
    monkeypatch.setattr(main_mod, "_worker_resolve_sender", _no_sender)
    monkeypatch.setattr(main_mod, "replay_pending_sync", _noop)
    monkeypatch.setattr(_cabinet_service, "sync_worker_duties", _noop)

    async with database.AsyncSessionLocal() as session:
        session.add(User(platform_user_id="g-hung-48", channel="max", active_deal_id="D-48",
                         is_smartbot_active=True,
                         context_json=_json.dumps({
                             "chat_history": [{"role": "user", "text": "привет"}],
                             "last_interaction_time": time.time() - 200000,
                             "nocodb_synced_index": 1, "channel": "max"})))
        await session.commit()

    real_sleep, ticks = asyncio.sleep, []

    async def fake_sleep(seconds, *a, **kw):
        if seconds == 1800:                     # интервал воркера: один проход, затем стоп
            ticks.append(seconds)
            if len(ticks) > 1:
                raise asyncio.CancelledError
            return
        return await real_sleep(seconds, *a, **kw)

    monkeypatch.setattr(main_mod, "asyncio",
                        _types.SimpleNamespace(**{**vars(asyncio), "sleep": fake_sleep}))
    t0 = time.monotonic()
    await asyncio.wait_for(main_mod.sync_worker(), timeout=4)
    assert time.monotonic() - t0 < 2.0
    assert any("/crm.timeline.comment.add" in c for c in calls)


def test_all_raw_bitrix_paths_use_timeout():
    src = open(main_mod.__file__, encoding="utf-8").read()
    # _request, реплей, колбэк n8n, мост, стенограмма, стадия чата, 48 ч (комментарий и LOSE)
    assert src.count("aiohttp.ClientSession(timeout=BITRIX_TIMEOUT)") == 8


# --- План 2026-10-12-001, U1: строка ретрая — тип ошибки, длительность, сделка ---

@pytest.mark.asyncio
async def test_retry_log_has_exception_type_duration_and_deal(hung_bitrix, monkeypatch, caplog):
    url, _calls = hung_bitrix
    monkeypatch.setattr(main_mod.BitrixService, "WEBHOOK", url)
    monkeypatch.setattr(main_mod, "BITRIX_TIMEOUT", aiohttp.ClientTimeout(total=0.3, connect=0.3))
    monkeypatch.setattr(main_mod, "BITRIX_RETRY_DELAYS", (0, 0, 0))
    caplog.set_level("WARNING")
    await main_mod.BitrixService._request("crm.deal.update", {"id": 53859}, enqueue_on_failure=False)
    lines = [r.getMessage() for r in caplog.records if "[Bitrix Retry]" in r.getMessage()]
    assert len(lines) == 3
    assert "Attempt 1/3 failed for crm.deal.update deal=53859: TimeoutError" in lines[0]
    assert lines[0].rstrip().endswith("с)") and "(0." in lines[0]


@pytest_asyncio.fixture
async def bitrix_500():
    calls = []

    async def handler(request):
        calls.append(request.path)
        return web.json_response({"error": "x"}, status=500)

    app = web.Application()
    app.router.add_post("/{method}", handler)
    server = TestServer(app)
    await server.start_server()
    yield f"http://{server.host}:{server.port}/", calls
    await server.close()


@pytest.mark.asyncio
async def test_retry_log_on_http_500(bitrix_500, monkeypatch, caplog):
    url, _calls = bitrix_500
    monkeypatch.setattr(main_mod.BitrixService, "WEBHOOK", url)
    monkeypatch.setattr(main_mod, "BITRIX_RETRY_DELAYS", (0, 0, 0))
    caplog.set_level("WARNING")
    await main_mod.BitrixService._request("crm.deal.get", {"id": 7}, enqueue_on_failure=False)
    line = next(r.getMessage() for r in caplog.records if "[Bitrix Retry]" in r.getMessage())
    assert "deal=7: Exception: API Error (Status 500)" in line


@pytest.mark.asyncio
async def test_single_attempt_with_own_timeout(hung_bitrix, monkeypatch):
    url, calls = hung_bitrix
    monkeypatch.setattr(main_mod.BitrixService, "WEBHOOK", url)
    t0 = time.monotonic()
    res = await main_mod.BitrixService._request(
        "crm.deal.update", {"id": 1}, enqueue_on_failure=False, attempts=1,
        timeout=aiohttp.ClientTimeout(total=0.3))
    assert res == {} and len(calls) == 1 and time.monotonic() - t0 < 1.5


def test_stage_timeout_only_for_stage_writes():
    assert main_mod.BITRIX_STAGE_TIMEOUT.total == 30
    assert main_mod.BITRIX_TIMEOUT.total == 10        # остальные запросы — прежние 10 с
