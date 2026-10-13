"""Защита каналов бот ↔ n8n (план n8n 2026-10-03): X-N8N-Secret во всех
вызовах n8n, адрес логов сессии из env без затирания архива, /n8n_callback —
режимы off / soft / strict."""
import asyncio
import json
import logging

import pytest

import database
import log_masking
import n8n_auth
import shadow_resolver
from database import User
import main as main_mod
from test_payload import TestDealerFieldsInRequestBody, _CaptureSession

SECRET = "n8n-out-secret-0123456789"
CB_SECRET = "n8n-callback-secret-9876543210"


@pytest.fixture
def out_secret(monkeypatch):
    monkeypatch.setenv("N8N_WEBHOOK_SECRET", SECRET)


@pytest.fixture
def no_out_secret(monkeypatch):
    monkeypatch.delenv("N8N_WEBHOOK_SECRET", raising=False)


# ---------------- 1. заголовок во всех вызовах n8n ----------------

def test_headers_helper(monkeypatch):
    monkeypatch.setenv("N8N_WEBHOOK_SECRET", SECRET)
    assert n8n_auth.n8n_headers() == {"X-N8N-Secret": SECRET}
    monkeypatch.setenv("N8N_WEBHOOK_SECRET", "")
    assert n8n_auth.n8n_headers() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("with_secret", [True, False])
async def test_chat_turn_and_handoff_carry_secret(monkeypatch, with_secret):
    if with_secret:
        monkeypatch.setenv("N8N_WEBHOOK_SECRET", SECRET)
    else:
        monkeypatch.delenv("N8N_WEBHOOK_SECRET", raising=False)
    helper = TestDealerFieldsInRequestBody()
    helper._patch(monkeypatch)
    _CaptureSession.headers = "unset"
    await main_mod.send_chat_message_to_n8n(
        "u-auth-1", "вопрос", "Имя",
        {"start_code": "010101", "scenario_type": "credit", "chat_history": []})
    expected = {"X-N8N-Secret": SECRET} if with_secret else {}
    assert _CaptureSession.headers == expected

    _CaptureSession.headers = "unset"
    user = User(platform_user_id="u-auth-2", channel="max", full_name="Тест")
    await main_mod.send_handoff_webhook(
        "u-auth-2", {"start_code": "010101", "scenario_type": "credit", "chat_history": []}, user)
    assert _CaptureSession.headers == expected


class _RecSession:
    calls = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _rec(self, method, url, **kw):
        _RecSession.calls.append((method, url, kw))

        class _Resp:
            status = 200

            async def json(self):
                return {"logs": [{"node": "x"}]}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        return _Resp()

    def get(self, url, **kw):
        return self._rec("GET", url, **kw)

    async def post(self, url, **kw):
        self._rec("POST", url, **kw)


@pytest.mark.asyncio
async def test_shadow_resolver_carries_secret(monkeypatch, out_secret):
    _RecSession.calls = []
    monkeypatch.setattr(shadow_resolver.aiohttp, "ClientSession", _RecSession)
    await shadow_resolver._send("http://n8n.test/shadow", {"a": 1})
    assert _RecSession.calls[0][2]["headers"] == {"X-N8N-Secret": SECRET}


@pytest.mark.asyncio
async def test_session_logs_url_from_env_and_secret(monkeypatch, out_secret):
    _RecSession.calls = []
    monkeypatch.setattr(main_mod.aiohttp, "ClientSession", _RecSession)
    monkeypatch.setenv("N8N_SESSION_LOGS_URL", "https://n8n.test/webhook/get-session-logs")
    logs = await main_mod.fetch_n8n_logs_for_archive("sess_u1_991111ABCD")
    method, url, kw = _RecSession.calls[0]
    assert method == "GET" and url == "https://n8n.test/webhook/get-session-logs"
    assert kw["params"] == {"session_id": "u1_991111ABCD"}
    assert kw["headers"] == {"X-N8N-Secret": SECRET}
    assert json.loads(logs) == [{"node": "x"}]


@pytest.mark.asyncio
async def test_session_logs_empty_url_no_request(monkeypatch):
    _RecSession.calls = []
    monkeypatch.setattr(main_mod.aiohttp, "ClientSession", _RecSession)
    monkeypatch.setenv("N8N_SESSION_LOGS_URL", "")
    assert await main_mod.fetch_n8n_logs_for_archive("sess_u1") == ""
    assert _RecSession.calls == []


def test_logs_url_not_hardcoded():
    src = open(main_mod.__file__, encoding="utf-8").read()
    assert "webhook/get-session-logs" not in src


# ---------------- 2. архив: пустые логи n8n не затирают сохранённые ----------------

class _NocoSession:
    calls = []
    existing = True

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, **kw):
        class _Resp:
            status = 200

            async def json(self_inner):
                return {"list": [{"Id": 7}] if _NocoSession.existing else []}

            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *a):
                return False

        return _Resp()

    async def patch(self, url, **kw):
        _NocoSession.calls.append(("PATCH", kw["json"]))

    async def post(self, url, **kw):
        _NocoSession.calls.append(("POST", kw["json"]))


@pytest.fixture
def noco(monkeypatch):
    _NocoSession.calls = []
    monkeypatch.setattr(main_mod, "NOCODB_TOKEN", "t")
    monkeypatch.setattr(main_mod, "NOCODB_API_URL", "https://noco.test")

    async def no_logs(session_id):
        return ""

    monkeypatch.setattr(main_mod, "fetch_n8n_logs_for_archive", no_logs)
    monkeypatch.setattr(main_mod.aiohttp, "ClientSession", _NocoSession)
    return _NocoSession


@pytest.mark.asyncio
async def test_archive_patch_without_logs_key_when_empty(noco):
    noco.existing = True
    await main_mod.upsert_dialogue_archive({"session_id": "sess_u1", "user_id": "u1", "logs": "old?"})
    method, body = noco.calls[0]
    assert method == "PATCH" and "logs" not in body and body["Id"] == 7


@pytest.mark.asyncio
async def test_archive_post_new_record_marks_no_data(noco):
    noco.existing = False
    await main_mod.upsert_dialogue_archive({"session_id": "sess_u2", "user_id": "u2"})
    method, body = noco.calls[0]
    assert method == "POST" and body["logs"] == "no_n8n_data"


# ---------------- 3. /n8n_callback: off / soft / strict ----------------

class _Req:
    def __init__(self, body, header=None):
        self._body = body
        self.headers = {"X-N8N-Callback-Secret": header} if header is not None else {}
        self.remote = "203.0.113.5"

    async def json(self):
        return self._body


@pytest.fixture
def side_effects(monkeypatch, db):
    """Перехват всех побочных эффектов колбэка: HTTP (Битрикс), алерты, БД."""
    import test_n8n_status_gate as gate
    http_calls, logs, alerts = [], [], []
    gate._wire(monkeypatch, http_calls, logs, alerts)
    return http_calls, alerts


async def _user(uid, phone="+79001234567"):
    async with database.AsyncSessionLocal() as session:
        session.add(User(platform_user_id=uid, channel="max", phone=phone,
                         is_smartbot_active=True, context_json="{}"))
        await session.commit()
    main_mod.USER_CONTEXT.pop(uid, None)


async def _call(uid, header):
    resp = await main_mod.handle_n8n_callback(
        _Req({"user_id": uid, "deal_id": "D-9", "status": "call", "ai_payload": "{}"}, header))
    for _ in range(200):
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        if not pending:
            break
        await asyncio.sleep(0.02)
    return resp


async def _smartbot_active(uid):
    async with database.AsyncSessionLocal() as session:
        from sqlalchemy import select
        return (await session.execute(select(User.is_smartbot_active)
                                      .where(User.platform_user_id == uid))).scalar_one()


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [None, "wrong-secret"])
async def test_strict_rejects_without_side_effects(monkeypatch, side_effects, header, caplog):
    http_calls, alerts = side_effects
    monkeypatch.setenv("N8N_CALLBACK_AUTH_MODE", "strict")
    monkeypatch.setenv("N8N_CALLBACK_SECRET", CB_SECRET)
    await _user("cb-strict")
    with caplog.at_level(logging.INFO):
        resp = await _call("cb-strict", header)
    assert resp.status == 403
    assert http_calls == [] and alerts == []
    assert await _smartbot_active("cb-strict") is True
    expected = "missing" if header is None else "bad"
    assert any(f"N8N_CALLBACK_AUTH result={expected}" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_strict_ok_processed(monkeypatch, side_effects):
    http_calls, _ = side_effects
    monkeypatch.setenv("N8N_CALLBACK_AUTH_MODE", "strict")
    monkeypatch.setenv("N8N_CALLBACK_SECRET", CB_SECRET)
    await _user("cb-ok")
    resp = await _call("cb-ok", CB_SECRET)
    assert resp.status == 200 and any("crm.deal.update" in u for u, _ in http_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("header,expected", [(None, "missing"), ("wrong", "bad"), (CB_SECRET, "ok")])
async def test_soft_processes_and_logs(monkeypatch, side_effects, caplog, header, expected):
    http_calls, _ = side_effects
    monkeypatch.setenv("N8N_CALLBACK_AUTH_MODE", "soft")
    monkeypatch.setenv("N8N_CALLBACK_SECRET", CB_SECRET)
    await _user(f"cb-soft-{expected}")
    with caplog.at_level(logging.INFO):
        resp = await _call(f"cb-soft-{expected}", header)
    assert resp.status == 200 and any("crm.deal.update" in u for u, _ in http_calls)
    lines = [r.getMessage() for r in caplog.records if "N8N_CALLBACK_AUTH" in r.getMessage()]
    assert lines and f"result={expected}" in lines[-1] and "deal=D-9" in lines[-1]
    assert "ip=203.0.113.5" in lines[-1] and CB_SECRET not in lines[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [None, "wrong"])
async def test_off_processes_without_check(monkeypatch, side_effects, caplog, header):
    http_calls, _ = side_effects
    monkeypatch.setenv("N8N_CALLBACK_AUTH_MODE", "off")
    await _user("cb-off")
    with caplog.at_level(logging.INFO):
        resp = await _call("cb-off", header)
    assert resp.status == 200 and http_calls
    assert not any("N8N_CALLBACK_AUTH" in r.getMessage() for r in caplog.records)


def test_default_mode_is_soft(monkeypatch):
    monkeypatch.delenv("N8N_CALLBACK_AUTH_MODE", raising=False)
    assert n8n_auth.callback_auth_mode() == "soft"
    monkeypatch.setenv("N8N_CALLBACK_AUTH_MODE", "weird")
    assert n8n_auth.callback_auth_mode() == "soft"


def test_strict_without_secret_fails_startup(monkeypatch):
    monkeypatch.setenv("N8N_CALLBACK_AUTH_MODE", "strict")
    monkeypatch.delenv("N8N_CALLBACK_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        n8n_auth.validate_config()
    monkeypatch.setenv("N8N_CALLBACK_SECRET", CB_SECRET)
    n8n_auth.validate_config()
    src = open(main_mod.__file__, encoding="utf-8").read()
    assert "n8n_auth.validate_config()" in src.split("async def init_app", 1)[1].split("await init_db()", 1)[0]


def test_secrets_masked_in_logs():
    for line in (f"X-N8N-Secret: {SECRET}", f"X-N8N-Callback-Secret: {CB_SECRET}",
                 f"headers={{'X-N8N-Secret': '{SECRET}'}}"):
        masked = log_masking.mask_sensitive(line)
        assert SECRET not in masked and CB_SECRET not in masked, masked


@pytest.mark.asyncio
async def test_callback_log_ip_from_trusted_proxy(monkeypatch, side_effects, caplog):
    """IP в строке N8N_CALLBACK_AUTH — из X-Forwarded-For доверенного прокси."""
    monkeypatch.setenv("N8N_CALLBACK_AUTH_MODE", "strict")
    monkeypatch.setenv("N8N_CALLBACK_SECRET", CB_SECRET)
    monkeypatch.setenv("CABINET_TRUSTED_PROXIES", "10.0.0.1")
    req = _Req({"user_id": "cb-ip", "deal_id": "", "status": "in_progress", "ai_payload": "{}"})
    req.remote = "10.0.0.1"
    req.headers["X-Forwarded-For"] = "198.51.100.7, 10.0.0.1"
    with caplog.at_level(logging.INFO):
        resp = await main_mod.handle_n8n_callback(req)
    assert resp.status == 403
    line = [r.getMessage() for r in caplog.records if "N8N_CALLBACK_AUTH" in r.getMessage()][-1]
    assert "ip=198.51.100.7" in line

    req2 = _Req({"user_id": "cb-ip", "deal_id": "", "status": "in_progress", "ai_payload": "{}"})
    req2.remote = "203.0.113.9"                      # не доверенный — заголовку не верим
    req2.headers["X-Forwarded-For"] = "198.51.100.7"
    with caplog.at_level(logging.INFO):
        await main_mod.handle_n8n_callback(req2)
    line = [r.getMessage() for r in caplog.records if "N8N_CALLBACK_AUTH" in r.getMessage()][-1]
    assert "ip=203.0.113.9" in line
