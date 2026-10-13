"""Блокер С4 дев-прогона 2026-09-22: таймаут-передача кабинета для сессии,
финализированной НЕ-кабинетным путём, дублировала менеджерский алерт и слала
дилеру «Личный кабинет…» без участия клиента (вечный rebind-токен). Гейт:
is_finalized/deal_finalized → токен отзывается без конверсии и алертов;
прод-механика для незакрытых сессий не меняется."""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import cabinet_service
import database
from database import CabinetEvent, CabinetToken, User

from conftest import PHONE


def _utcnow():
    return datetime.now(timezone.utc)


async def _mk_user(db, suffix, ctx=None):
    async with db.AsyncSessionLocal() as session:
        user = User(platform_user_id=f"fin-{suffix}", channel="max",
                    full_name="Виталий", phone=PHONE,
                    context_json=json.dumps(ctx or {}, ensure_ascii=False))
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _mk_bound_token(db, user_id, bound_ago=3600):
    async with db.AsyncSessionLocal() as session:
        token = CabinetToken(
            user_id=user_id, dealer_code="01", deal_id="D-52543",
            token_hash=uuid.uuid4().hex + uuid.uuid4().hex[:32],
            status="active", bound_phone=PHONE,
            bound_at=_utcnow() - timedelta(seconds=bound_ago))
        session.add(token)
        await session.commit()
        await session.refresh(token)
        return token


async def _token_status(db, token_id):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(
            select(CabinetToken.status, CabinetToken.handoff_at)
            .where(CabinetToken.id == token_id))).one()


async def _events(db, token_id, event_type):
    async with db.AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(CabinetEvent).where(CabinetEvent.token_id == token_id,
                                       CabinetEvent.event_type == event_type)
        )).scalars().all()
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize("closed_flag", ["is_finalized", "deal_finalized"])
async def test_finalized_session_timeout_no_conversion_no_alerts(db, deps, closed_flag):
    """Репро С4: Manager-путь закрыл сессию (флаг в ctx), вечный токен с
    подтверждённым номером висит → тик таймаутов НЕ передаёт заявку повторно."""
    user = await _mk_user(db, closed_flag, ctx={closed_flag: True,
                                                "start_code": "991111QU"})
    token = await _mk_bound_token(db, user.id)

    converted = await cabinet_service._check_handoff_timeouts(_utcnow())

    assert converted == 0
    assert deps.conversions == []                     # повторного алерта менеджеру нет
    assert deps.dealer_alerts == []                   # «Личный кабинет…» дилеру нет
    assert deps.enqueues == []                        # черновик в сделку не писался
    status, handoff_at = await _token_status(db, token.id)
    assert status == "revoked" and handoff_at is None  # токен отозван, не «передан»
    assert len(await _events(db, token.id, "session_closed")) == 1
    assert await _events(db, token.id, "handoff") == []

    # идемпотентность: следующий тик кандидата больше не видит
    assert await cabinet_service._check_handoff_timeouts(_utcnow()) == 0
    assert len(await _events(db, token.id, "session_closed")) == 1


@pytest.mark.asyncio
async def test_open_session_timeout_still_converts(db, deps):
    """Контроль прод-механики: без флагов финализации таймаут-передача
    работает как раньше (конверсия + события handoff)."""
    user = await _mk_user(db, "open", ctx={"start_code": "991111QU"})
    token = await _mk_bound_token(db, user.id)

    converted = await cabinet_service._check_handoff_timeouts(_utcnow())

    assert converted == 1
    assert len(deps.conversions) == 1                 # конверсия вызвана
    status, handoff_at = await _token_status(db, token.id)
    assert status == "active" and handoff_at is not None
    assert len(await _events(db, token.id, "handoff")) == 1


@pytest.mark.asyncio
async def test_fresh_bound_token_not_touched(db, deps):
    """Токен внутри таймаут-окна не трогается даже при финализированной
    сессии — гейт срабатывает только на кандидатах таймаута."""
    user = await _mk_user(db, "fresh", ctx={"is_finalized": True})
    token = await _mk_bound_token(db, user.id, bound_ago=10)

    assert await cabinet_service._check_handoff_timeouts(_utcnow()) == 0
    status, _ = await _token_status(db, token.id)
    assert status == "active"                          # ничего не изменилось
