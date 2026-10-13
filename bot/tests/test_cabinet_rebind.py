"""План 2026-08-21-002: кабинет следует за новой сделкой (перепривязка).

U1 — флейвор «новая заявка» (sync_cabinet_deal): идентичность (токен, URL,
верификация) сохраняется, жизненный цикл заявки (submitted_at, handoff_at,
снапшот, qualification_payment) сбрасывается. U2 — зеркальный гард
согласования сделки диалога при склейке. U4 — проводка ветки B.
Проверка — по фактическому содержимому таблиц после сценария
(конвенция tests/test_bot_integration.py).
"""
import json
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

import cabinet_service
from database import (CabinetEvent, CabinetForm, CabinetProfile, CabinetToken,
                      PendingSync, User)
import main as main_mod

from conftest import PHONE

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"


# =======================
# Хелперы (по образцу test_bot_integration)
# =======================

async def _mk_user(db, suffix, phone=None):
    async with db.AsyncSessionLocal() as session:
        user = User(platform_user_id=f"u-rb-{suffix}", channel="max",
                    full_name="Иван", phone=phone)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _bind_token(db, user_id, bound_at=None):
    async with db.AsyncSessionLocal() as session:
        await session.execute(update(CabinetToken)
                              .where(CabinetToken.user_id == user_id)
                              .values(bound_phone=PHONE,
                                      bound_at=bound_at or datetime.now(timezone.utc)))
        await session.commit()


async def _token(db, user_id):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == user_id,
                                       CabinetToken.status == "active")
        )).scalars().first()


async def _profile(db, user_id):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == user_id)
        )).scalar_one_or_none()


async def _setup_first_application(db, user, deal="A-1", payment=25000):
    """Первая квалификация: выдача ссылки + профиль + снапшот + верификация."""
    url = await cabinet_service.issue_cabinet_token(user.id, "01", deal)
    assert url
    await cabinet_service.upsert_cabinet_profile(
        user.id, dealer_code="01", deal_id=deal, payment=payment)
    async with db.AsyncSessionLocal() as session:
        await session.execute(update(CabinetProfile)
                              .where(CabinetProfile.user_id == user.id)
                              .values(calc_snapshot=json.dumps({"item_id": "OLD"}),
                                      calc_frozen_at=datetime.now(timezone.utc)))
        await session.commit()
    await _bind_token(db, user.id)
    return url


# =======================
# U1: sync_cabinet_deal — флейвор «новая заявка»
# =======================

class TestSyncCabinetDeal:
    @pytest.mark.asyncio
    async def test_acceptance_scenario_rebind_keeps_url_resets_lifecycle(self, db, deps):
        user = await _mk_user(db, "acc")
        url_a = await _setup_first_application(db, user)

        assert await cabinet_service.sync_cabinet_deal(
            user.id, "01", "B-2", payment=30000) is True

        token = await _token(db, user.id)
        assert token.deal_id == "B-2"
        assert token.bound_phone == PHONE  # верификация пережила перепривязку
        assert token.handoff_at is None
        # URL прежний: вывод по сделке момента выдачи (issue_deal_id)
        assert await cabinet_service.issue_cabinet_token(user.id, "01", "B-2") == url_a

        profile = await _profile(db, user.id)
        assert profile.deal_id == "B-2"
        assert profile.calc_snapshot is None and profile.calc_frozen_at is None
        assert profile.qualification_payment == 30000

    @pytest.mark.asyncio
    async def test_submitted_form_travels_as_draft_copy(self, db, deps):
        user = await _mk_user(db, "form")
        await _setup_first_application(db, user)
        async with db.AsyncSessionLocal() as session:
            session.add(CabinetForm(user_id=user.id, deal_id="A-1",
                                    credit_term=60, client_comment="жду звонка",
                                    submitted_at=datetime.now(timezone.utc)))
            await session.commit()

        assert await cabinet_service.sync_cabinet_deal(user.id, "01", "B-2")

        async with db.AsyncSessionLocal() as session:
            forms = (await session.execute(
                select(CabinetForm).where(CabinetForm.user_id == user.id)
            )).scalars().all()
        by_deal = {f.deal_id: f for f in forms}
        # Копия-черновик под новой сделкой: данные перенесены, submitted_at
        # сброшен — анкета редактируема/отправляема, фильтр R34 не включён
        assert by_deal["B-2"].credit_term == 60
        assert by_deal["B-2"].client_comment == "жду звонка"
        assert by_deal["B-2"].submitted_at is None
        # Строка старой заявки — история, отправленность нетронута
        assert by_deal["A-1"].submitted_at is not None

    @pytest.mark.asyncio
    async def test_handoff_reset_restarts_clock_no_resend(self, db, deps):
        user = await _mk_user(db, "handoff")
        await _setup_first_application(db, user)
        stale = datetime.now(timezone.utc) - timedelta(hours=2)
        async with db.AsyncSessionLocal() as session:
            await session.execute(update(CabinetToken)
                                  .where(CabinetToken.user_id == user.id)
                                  .values(bound_at=stale,
                                          handoff_at=stale + timedelta(minutes=30)))
            await session.commit()

        assert await cabinet_service.sync_cabinet_deal(user.id, "01", "B-2")

        token = await _token(db, user.id)
        assert token.handoff_at is None  # таймаут-каналы передачи ожили
        # Часы перезапущены: иначе триггер max_age (bound_at 2ч назад)
        # выстрелил бы конверсией мгновенно
        age = datetime.now(timezone.utc) - token.bound_at.replace(
            tzinfo=token.bound_at.tzinfo or timezone.utc)
        assert age < timedelta(minutes=1)
        # Переотправки результата старой заявки не было
        async with db.AsyncSessionLocal() as session:
            resend = (await session.execute(
                select(CabinetEvent).where(
                    CabinetEvent.user_id == user.id,
                    CabinetEvent.event_type.in_(
                        ("merge_resend_form", "merge_resend_draft")))
            )).scalars().all()
        assert resend == []

    @pytest.mark.asyncio
    async def test_pending_sync_of_old_deal_not_rewritten(self, db, deps):
        user = await _mk_user(db, "pending")
        await _setup_first_application(db, user)
        async with db.AsyncSessionLocal() as session:
            session.add(PendingSync(method="crm.deal.update",
                                    payload=json.dumps({"id": "A-1", "fields": {}}),
                                    status="pending"))
            await session.commit()

        assert await cabinet_service.sync_cabinet_deal(user.id, "01", "B-2")

        async with db.AsyncSessionLocal() as session:
            row = (await session.execute(select(PendingSync))).scalars().first()
        assert json.loads(row.payload)["id"] == "A-1"  # хвосты старой сделки — при ней

    @pytest.mark.asyncio
    async def test_rollback_guard_skips_former_deal(self, db, deps):
        user = await _mk_user(db, "guard")
        await _setup_first_application(db, user)
        assert await cabinet_service.sync_cabinet_deal(user.id, "01", "B-2")
        # Устаревший ctx конкурентного хода приносит прежнюю сделку A-1
        assert await cabinet_service.sync_cabinet_deal(user.id, "01", "A-1") is False
        assert (await _token(db, user.id)).deal_id == "B-2"

    @pytest.mark.asyncio
    async def test_noop_paths(self, db, deps):
        user = await _mk_user(db, "noop")
        await _setup_first_application(db, user)
        assert await cabinet_service.sync_cabinet_deal(user.id, "01", "A-1") is False
        assert await cabinet_service.sync_cabinet_deal(user.id, "01", None) is False
        assert await cabinet_service.sync_cabinet_deal(user.id, "02", "B-2") is False
        assert (await _token(db, user.id)).deal_id == "A-1"

    @pytest.mark.asyncio
    async def test_empty_token_deal_goes_adopt_path(self, db, deps):
        user = await _mk_user(db, "adopt")
        url = await cabinet_service.issue_cabinet_token(user.id, "01", None)
        assert url
        assert await cabinet_service.sync_cabinet_deal(user.id, "01", "B-2") is True
        token = await _token(db, user.id)
        assert token.deal_id == "B-2"
        # adopt: URL прежний (issue_deal_id зафиксирован пустой сделкой выдачи)
        assert await cabinet_service.issue_cabinet_token(user.id, "01", "B-2") == url

    @pytest.mark.asyncio
    async def test_rebind_without_payment_nulls_qualification(self, db, deps):
        user = await _mk_user(db, "nopay")
        await _setup_first_application(db, user, payment=25000)
        assert await cabinet_service.sync_cabinet_deal(user.id, "01", "B-2")
        assert (await _profile(db, user.id)).qualification_payment is None

    @pytest.mark.asyncio
    async def test_second_handoff_cycle_guards_freed(self, db, deps):
        # Находка ревью (P1): однократные «на токен» события освобождаются
        # пометкой старой сделкой — второй цикл передачи полноценен
        user = await _mk_user(db, "cycle2")
        await _setup_first_application(db, user)
        tok = await _token(db, user.id)
        for ev in ("handoff_done", "handoff_signal_b",
                   "session_closed", "session_farewell_sent"):
            assert await cabinet_service._record_event_once(user.id, tok.id, ev)
            # дедуп реестра работает до перепривязки
            assert not await cabinet_service._record_event_once(user.id, tok.id, ev)

        assert await cabinet_service.sync_cabinet_deal(user.id, "01", "B-2")

        async with db.AsyncSessionLocal() as session:
            events = (await session.execute(
                select(CabinetEvent).where(
                    CabinetEvent.token_id == tok.id,
                    CabinetEvent.event_type == "handoff_done")
            )).scalars().all()
        assert json.loads(events[0].meta_json)["archived_deal"] == "A-1"
        # Гарды свободны: события нового цикла записываются заново
        for ev in ("handoff_done", "handoff_signal_b",
                   "session_closed", "session_farewell_sent"):
            assert await cabinet_service._record_event_once(user.id, tok.id, ev), \
                f"гард {ev} должен быть свободен для второго цикла"

    @pytest.mark.asyncio
    async def test_expires_at_extended_with_bound_clock(self, db, deps):
        user = await _mk_user(db, "ttl")
        await _setup_first_application(db, user)
        soon = datetime.now(timezone.utc) + timedelta(days=1)
        async with db.AsyncSessionLocal() as session:
            await session.execute(update(CabinetToken)
                                  .where(CabinetToken.user_id == user.id)
                                  .values(expires_at=soon))
            await session.commit()
        assert await cabinet_service.sync_cabinet_deal(user.id, "01", "B-2")
        token = await _token(db, user.id)
        expires = token.expires_at.replace(
            tzinfo=token.expires_at.tzinfo or timezone.utc)
        assert expires > datetime.now(timezone.utc) + timedelta(days=29)

    @pytest.mark.asyncio
    async def test_rollback_guard_expires_after_window(self, db, deps):
        # Гард от отката оконный: спустя окно охоты возврат сделки легитимен
        user = await _mk_user(db, "window")
        await _setup_first_application(db, user)
        assert await cabinet_service.sync_cabinet_deal(user.id, "01", "B-2")
        stale = datetime.now(timezone.utc) - timedelta(hours=4)
        async with db.AsyncSessionLocal() as session:
            await session.execute(update(CabinetEvent)
                                  .where(CabinetEvent.event_type == "deal_rebound")
                                  .values(created_at=stale))
            await session.commit()
        assert await cabinet_service.sync_cabinet_deal(user.id, "01", "A-1") is True
        assert (await _token(db, user.id)).deal_id == "A-1"

    @pytest.mark.asyncio
    async def test_empty_dealer_code_fail_closed(self, db, deps):
        user = await _mk_user(db, "nodlr")
        await _setup_first_application(db, user)
        assert await cabinet_service.sync_cabinet_deal(user.id, "", "B-2") is False
        assert (await _token(db, user.id)).deal_id == "A-1"


# =======================
# U2: зеркальный гард согласования при склейке
# =======================

class TestSyncDialogDealRefs:
    @pytest.mark.asyncio
    async def test_updates_when_pointing_at_duplicate(self, db, monkeypatch):
        monkeypatch.setattr(main_mod, "AsyncSessionLocal", db.AsyncSessionLocal)
        user = await _mk_user(db, "hunt1")
        async with db.AsyncSessionLocal() as session:
            await session.execute(update(User).where(User.id == user.id)
                                  .values(active_deal_id="BOT-1"))
            await session.commit()
        main_mod.USER_CONTEXT.clear()
        main_mod.USER_CONTEXT[user.platform_user_id] = {"deal_id": "BOT-1",
                                                        "chat_history": []}
        try:
            await main_mod.sync_dialog_deal_refs(user.platform_user_id, "BOT-1", "ORIG-9")
            async with db.AsyncSessionLocal() as session:
                fresh = await session.get(User, user.id)
            assert fresh.active_deal_id == "ORIG-9"
            assert main_mod.USER_CONTEXT[user.platform_user_id]["deal_id"] == "ORIG-9"
        finally:
            main_mod.USER_CONTEXT.clear()

    @pytest.mark.asyncio
    async def test_mirror_guard_keeps_fresh_requalification(self, db, monkeypatch):
        # Охота финишировала ПОСЛЕ новой квалификации: сделка C свежее —
        # поздний финиш не перетирает её
        monkeypatch.setattr(main_mod, "AsyncSessionLocal", db.AsyncSessionLocal)
        user = await _mk_user(db, "hunt2")
        async with db.AsyncSessionLocal() as session:
            await session.execute(update(User).where(User.id == user.id)
                                  .values(active_deal_id="C-3"))
            await session.commit()
        main_mod.USER_CONTEXT.clear()
        main_mod.USER_CONTEXT[user.platform_user_id] = {"deal_id": "C-3",
                                                        "chat_history": []}
        try:
            await main_mod.sync_dialog_deal_refs(user.platform_user_id, "BOT-1", "ORIG-9")
            async with db.AsyncSessionLocal() as session:
                fresh = await session.get(User, user.id)
            assert fresh.active_deal_id == "C-3"
            assert main_mod.USER_CONTEXT[user.platform_user_id]["deal_id"] == "C-3"
        finally:
            main_mod.USER_CONTEXT.clear()


# =======================
# Проводка: порядок вызовов (source-стражи, конвенция test_payload)
# =======================

def _body(src: str, container: str) -> str:
    """Срез тела функции-контейнера — якоря стражей не смещаются одноимёнными
    вызовами раньше по файлу (находка ревью о хрупкости index-стражей)."""
    start = src.index(container)
    nxt = src.find("\nasync def ", start + 1)
    return src[start:nxt if nxt != -1 else len(src)]


class TestWiring:
    def test_hunt_syncs_refs_before_rebind(self):
        body = _body(MAIN_PY.read_text(encoding="utf-8"),
                     "async def background_deal_hunt")
        sync_refs = body.index("await sync_dialog_deal_refs(")
        rebind = body.index("await cabinet_service.rebind_cabinet_on_merge(")
        assert sync_refs < rebind, "согласование сделки диалога — ДО rebind токена"

    def test_branch_b_calls_cabinet_sync(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        branch_b = src.index("ВЕТКА B: Стандартная обработка")
        segment = src[branch_b:src.index("АРХИВАЦИЯ DIALOGFLOW-ДИАЛОГА", branch_b)]
        final_sync = segment.index("await BitrixService.final_sync_wrapper(")
        cab_sync = segment.index("await cabinet_service.sync_cabinet_deal(")
        assert final_sync < cab_sync, \
            "ветка B синхронизирует сделку кабинета после появления сделки"

    def test_prepare_rereads_active_after_rebind(self):
        body = _body(
            pathlib.Path(cabinet_service.__file__).read_text(encoding="utf-8"),
            "async def prepare_cabinet_context")
        sync_call = body.index("if await sync_cabinet_deal(user_id, active.dealer_code, deal_id,")
        reread = body.index("active = (await session.execute(", sync_call)
        issue = body.index("url = await issue_cabinet_token(user_id, active.dealer_code, active.deal_id)")
        assert sync_call < reread < issue, \
            "после перепривязки active перечитывается до выдачи (иначе revoke)"
