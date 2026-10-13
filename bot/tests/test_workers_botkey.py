"""U5 (план 2026-09-15-001): воркеры через бота сессии — проверяемый маркер
bot_send_blocked (R14), Bot_Key на всех путях архивации (R19), констатация
незатронутости pending-очередей/otp_worker."""
import inspect
import json
import pathlib

import pytest
from sqlalchemy import select

import bot_registry
import database
import main as main_mod
from database import User

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"


class AlertRecorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, text):
        self.calls.append(text)
        return True


def rows_fetcher(rows):
    async def fetch():
        return rows
    return fetch


PILOT_ROWS = [
    {"Bot_Key": "max-haval", "Channel": "max", "Brands": "haval",
     "Token_Env": "BOT_TOKEN_MAX_HAVAL_T", "Environment": "dev", "Active": True},
]


@pytest.fixture
def loaded_registry(db, monkeypatch):
    bot_registry.reset_for_tests()
    monkeypatch.setenv("BOT_ENV", "dev")
    monkeypatch.setenv("MAX_BOT_TOKEN", "legacy-max-token-0001")
    monkeypatch.setenv("TG_BOT_TOKEN", "legacy-tg-token-0002")
    monkeypatch.setenv("BOT_TOKEN_MAX_HAVAL_T", "pilot-max-token-0003")
    bot_registry.setup(alert=AlertRecorder(), fetch_rows=rows_fetcher(PILOT_ROWS))
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)
    yield
    bot_registry.reset_for_tests()


async def _mk_user(uid, ctx):
    async with database.AsyncSessionLocal() as session:
        user = User(platform_user_id=uid, channel=ctx.get("channel", "max"),
                    bot_key=ctx.get("bot_key"), is_smartbot_active=True,
                    context_json=json.dumps(ctx, ensure_ascii=False))
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


async def _ctx_from_db(uid):
    async with database.AsyncSessionLocal() as session:
        user = (await session.execute(
            select(User).where(User.platform_user_id == uid))).scalar_one()
    return json.loads(user.context_json)


# =======================
# _worker_resolve_sender: маркер проверяемый, не терминальный
# =======================

@pytest.mark.asyncio
async def test_blocked_bot_sets_marker_once_then_resumes(loaded_registry):
    await bot_registry.load_bots_registry()
    uid = "max-haval:800"
    ctx = {"channel": "max", "bot_key": "max-haval"}
    u = await _mk_user(uid, ctx)

    # бот выключен → маркер записан в контекст (однократно)
    bot_registry.CACHED_BOTS["max-haval"].active = False
    async with database.AsyncSessionLocal() as session:
        cl, raw = await main_mod._worker_resolve_sender(session, u, ctx, 1000.0)
        await session.commit()
    assert cl is None and raw == "800"
    saved = await _ctx_from_db(uid)
    assert saved["bot_send_blocked"]["bot_key"] == "max-haval"
    assert saved["bot_send_blocked"]["since"] == 1000.0

    # повторный цикл: маркер уже стоит — контекст не перезаписывается
    async with database.AsyncSessionLocal() as session:
        cl, _ = await main_mod._worker_resolve_sender(session, u, ctx, 2000.0)
        await session.commit()
    assert cl is None
    assert (await _ctx_from_db(uid))["bot_send_blocked"]["since"] == 1000.0

    # бот включён обратно → маркер снят, клиент вернулся, рассылка возобновима
    bot_registry.CACHED_BOTS["max-haval"].active = True
    async with database.AsyncSessionLocal() as session:
        cl, raw = await main_mod._worker_resolve_sender(session, u, ctx, 3000.0)
        await session.commit()
    assert cl is bot_registry.get_bot_client("max-haval") and raw == "800"
    assert "bot_send_blocked" not in ctx
    assert "bot_send_blocked" not in await _ctx_from_db(uid)


@pytest.mark.asyncio
async def test_legacy_user_resolves_as_before(loaded_registry):
    await bot_registry.load_bots_registry()
    uid = "900"
    ctx = {"channel": "telegram"}
    u = await _mk_user(uid, ctx)
    async with database.AsyncSessionLocal() as session:
        cl, raw = await main_mod._worker_resolve_sender(session, u, ctx, 1.0)
    assert cl is main_mod.tg_client and raw == "900"      # паритет (R7)
    assert "bot_send_blocked" not in ctx


# =======================
# Bot_Key в архиве (R19)
# =======================

def test_archive_bot_key_resolution_order(monkeypatch):
    monkeypatch.setenv("BOT_ENV", "dev")                 # U9: легаси-ключ по окружению
    f = main_mod._archive_bot_key
    assert f({"bot_key": "max-haval"}) == "max-haval"                    # ctx
    u = type("U", (), {"bot_key": "tg-haval"})()
    assert f({}, db_user=u) == "tg-haval"                                # users
    assert f({}, user_id="max-haval:1") == "max-haval"                   # parse
    assert f({"channel": "telegram"}, user_id="123") == "tg-test"        # легаси
    assert f({"channel": "web"}) == "web"
    assert f(None, user_id="123") == "max-test"                          # дефолт max
    # исторические ключи из старых ctx/users резолвятся в ключ окружения
    assert f({"bot_key": "max-main"}) == "max-test"
    u_hist = type("U", (), {"bot_key": "tg-main"})()
    assert f({}, db_user=u_hist) == "tg-test"


def test_all_four_archive_sites_write_bot_key():
    """Learning ai-fields-only-on-ai-payload-paths: все пути сборки
    archive_payload несут Bot_Key — финализация, DF-закрытие, бекап 30м,
    TTL 48ч."""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert src.count('"Bot_Key": _archive_bot_key(') == 4
    assert src.count("archive_payload = {") == 4


# =======================
# Констатация: очереди и вспомогательные воркеры не пишут клиенту
# =======================

def test_pending_and_aux_workers_untouched():
    for fn in (main_mod.replay_pending_sync, main_mod.otp_worker):
        src = inspect.getsource(fn)
        assert "send_message(user_id" not in src
        assert "get_sender(" not in src
    # farewell TTL и ремайндеры — через _worker_resolve_sender (бот сессии)
    assert "_worker_resolve_sender(session, u, ctx" in inspect.getsource(main_mod.sync_worker)
    assert "_worker_resolve_sender(session, u, ctx" in inspect.getsource(main_mod.reminder_worker)
    # отправка ремайндера-2 — резолвнутым клиентом, не глобальным
    assert "_r_cl.send_message(user_id=_r_raw" in inspect.getsource(main_mod.reminder_worker)
    assert "_fw_cl.send_message(user_id=_fw_raw" in inspect.getsource(main_mod.sync_worker)
