"""2026-10-02: очередь досылки — только записи; при выключенном Битриксе
(пустой BITRIX_WEBHOOK_URL) реплей не крутится и алертов не шлёт;
техалерты дев-стенда — с префиксом «[DEV]»."""
import json
import types
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

import bot_registry
import database
from database import PendingSync
import main


@pytest_asyncio.fixture
async def db(db, monkeypatch):
    monkeypatch.setattr(main, "AsyncSessionLocal", database.AsyncSessionLocal)
    yield db


async def _rows(db, uid):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(
            select(PendingSync).where(PendingSync.user_id == uid))).scalars().all()


class _DeadSession:
    """aiohttp.ClientSession, у которого любой запрос падает (Битрикс недоступен)."""
    posts = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, json=None):
        _DeadSession.posts.append(url)
        raise ConnectionError("Bitrix down")


@pytest.fixture
def dead_bitrix(monkeypatch):
    _DeadSession.posts = []
    monkeypatch.setattr(main.BitrixService, "WEBHOOK", "http://127.0.0.1:9/")
    monkeypatch.setattr(main, "BITRIX_RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(main.aiohttp, "ClientSession", _DeadSession)
    return _DeadSession.posts


# =======================
# 1. Чтение в очередь не ставится ни в одном пути
# =======================

@pytest.mark.parametrize("method,is_read", [
    ("crm.deal.list", True), ("crm.deal.get", True), ("crm.contact.list", True),
    ("crm.contact.get", True), ("crm.deal.fields", True), ("crm.status.list", True),
    ("crm.deal.update", False), ("crm.deal.add", False), ("crm.contact.add", False),
    ("crm.contact.update", False), ("crm.timeline.comment.add", False),
])
def test_read_method_predicate(method, is_read):
    assert main.is_bitrix_read_method(method) is is_read


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_debounce", [False, True])
async def test_enqueue_refuses_reads(db, skip_debounce):
    uid = f"webcab:{uuid.uuid4().hex}"
    for method in ("crm.deal.list", "crm.deal.get", "crm.contact.list", "crm.contact.get"):
        await main.BitrixService.enqueue_failed_request(
            method, {"filter": {}}, user_id=uid, skip_debounce=skip_debounce)
    assert await _rows(db, uid) == []
    await main.BitrixService.enqueue_failed_request(
        "crm.deal.update", {"id": 1}, user_id=uid, skip_debounce=skip_debounce)
    assert [r.method for r in await _rows(db, uid)] == ["crm.deal.update"]


@pytest.mark.asyncio
async def test_chat_paths_do_not_queue_reads_when_bitrix_down(db, dead_bitrix):
    """Поиск сделки конверсии/привязки/охоты и get по ID — чтение: сбой → None, без очереди."""
    uid = f"webcab:{uuid.uuid4().hex}"
    assert await main.BitrixService.find_deal_data_by_code(
        "991111ABCD", stage_id="C9:NEW", user_id=uid) is None
    assert await main.BitrixService.get_deal_by_id("42", user_id=uid) is None
    assert dead_bitrix, "запросы должны были уйти"
    assert await _rows(db, uid) == []


@pytest.mark.asyncio
async def test_write_still_queued_when_bitrix_down(db, dead_bitrix):
    uid = f"webcab:{uuid.uuid4().hex}"
    await main.BitrixService._request("crm.deal.update", {"id": 7, "fields": {}}, user_id=uid)
    assert [r.method for r in await _rows(db, uid)] == ["crm.deal.update"]


@pytest.mark.asyncio
async def test_cabinet_nodebounce_path_refuses_reads(db):
    import cabinet_service
    main.setup_cabinet_service()
    uid = f"webcab:{uuid.uuid4().hex}"
    await cabinet_service._enqueue_nodebounce("crm.deal.list", {"filter": {}}, user_id=uid)
    await cabinet_service._enqueue_nodebounce("crm.deal.update", {"id": 1}, user_id=uid)
    assert [r.method for r in await _rows(db, uid)] == ["crm.deal.update"]


# =======================
# 2. Битрикс выключен — реплей не крутится и алертов не шлёт
# =======================

@pytest.mark.asyncio
async def test_replay_idle_when_bitrix_off(db, monkeypatch):
    posts, alerts = [], []

    class _Session(_DeadSession):
        def post(self, url, json=None):
            posts.append(url)
            raise ConnectionError("must not be called")

    async def fake_alert(group_id=None, text=None, **kwargs):
        alerts.append(text)

    monkeypatch.setattr(main, "aiohttp", types.SimpleNamespace(ClientSession=_Session))
    monkeypatch.setattr(main.max_client, "send_alert_to_group", fake_alert)
    monkeypatch.setattr(main, "TECH_ADMIN_ID", "tech-group-1")
    monkeypatch.setattr(main.BitrixService, "WEBHOOK", "")
    monkeypatch.setenv("PENDING_SYNC_MAX_ATTEMPTS", "1")

    uid = f"u-{uuid.uuid4().hex}"
    async with db.AsyncSessionLocal() as session:
        session.add(PendingSync(method="crm.deal.update",
                                payload=json.dumps({"id": 1, "UF_CRM_1731438093": "991111ABCD"}),
                                user_id=uid, attempts=0, status="pending"))
        await session.commit()

    for _ in range(3):
        async with db.AsyncSessionLocal() as session:
            await main.replay_pending_sync(session)

    (row,) = await _rows(db, uid)
    assert row.status == "pending" and row.attempts == 0   # попытки не сжигаются
    assert posts == [] and alerts == []                    # ни запросов, ни отчёта, ни fatal


# =======================
# 3. Префикс «[DEV]» в техалертах дев-стенда
# =======================

class _AlertBot:
    def __init__(self):
        self.sent = []

    async def send_alert_to_group(self, group_id, text):
        self.sent.append((group_id, text))
        return {"ok": True}


@pytest.mark.asyncio
async def test_dev_alerts_prefixed(monkeypatch):
    bot = _AlertBot()
    monkeypatch.setattr(bot_registry, "get_bot_client", lambda key: bot)
    monkeypatch.setenv("BOT_ENV", "dev")
    client = main.service_alert_client()
    assert await client.send_alert_to_group(group_id="g", text="💀 CRM-очередь") == {"ok": True}
    await client.send_alert_to_group(group_id="g", text="[DEV] уже помечен")
    assert bot.sent == [("g", "[DEV] 💀 CRM-очередь"), ("g", "[DEV] уже помечен")]


@pytest.mark.asyncio
async def test_prod_alerts_unchanged(monkeypatch):
    bot = _AlertBot()
    monkeypatch.setattr(bot_registry, "get_bot_client", lambda key: bot)
    monkeypatch.setenv("BOT_ENV", "prod")
    assert main.service_alert_client() is bot
    await main.service_alert_client().send_alert_to_group(group_id="g", text="💀 CRM-очередь")
    assert bot.sent == [("g", "💀 CRM-очередь")]


def test_all_tech_alerts_go_through_service_client():
    """Единая точка: техалерты в тех-группу отправляются только через
    service_alert_client() — префикс не обходится."""
    src = open(main.__file__, encoding="utf-8").read()
    for group in ("TECH_ADMIN_ID", "target_group_id", "fatal_group_id", "SUPPORT_GROUP_ID"):
        for line in src.splitlines():
            if f"group_id={group}" in line and "send_alert_to_group" in line:
                assert "service_alert_client()" in line, line
