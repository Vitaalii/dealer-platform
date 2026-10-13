"""U10: передача заявки в Bitrix из кабинета (R39, R40, R41, R26 сигнал B).

Передача — новый триггер к существующей конверсии: attempt_handoff захватывает
handoff_at атомарно и вызывает run_conversion, инъецированный через setup().
Триггер 1 — явный сабмит (UF по карте); триггеры 2/3 — таймауты otp_worker
(черновик только комментарием + сигнал B)."""
import asyncio
import json
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

import database
from database import (CabinetEvent, CabinetForm, CabinetProfile, CabinetToken,
                      CabinetOtp, User)

import cabinet_service
import main as main_mod  # поведенческие тесты BitrixService (U2 плана 2026-09-01-001)

from conftest import CONSENT, PHONE

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAIN_PY = ROOT / "main.py"
CABINET_SERVICE_PY = ROOT / "cabinet_service.py"

# Фикстуры db/deps (чистый SQLite + setup-инъекция фейков) — общие, в conftest.py.


# =======================
# Хелперы данных (явные timestamp'ы)
# =======================

async def _mk_user(db, suffix="1", full_name="Иван"):
    async with db.AsyncSessionLocal() as session:
        user = User(platform_user_id=f"u-{suffix}", channel="max", full_name=full_name)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _mk_token(db, user_id, **kw):
    defaults = dict(user_id=user_id, dealer_code="01", deal_id="D-77",
                    token_hash=uuid.uuid4().hex + uuid.uuid4().hex[:32],
                    status="active", bound_phone=PHONE)
    defaults.update(kw)
    async with db.AsyncSessionLocal() as session:
        token = CabinetToken(**defaults)
        session.add(token)
        await session.commit()
        await session.refresh(token)
        return token


async def _mk_form(db, user_id, deal_id="D-77", **fields):
    async with db.AsyncSessionLocal() as session:
        form = CabinetForm(user_id=user_id, deal_id=deal_id, **fields)
        session.add(form)
        await session.commit()
        return form


async def _mk_otp(db, token_id, **kw):
    defaults = dict(token_id=token_id, phone=PHONE,
                    code_hmac=cabinet_service.compute_code_hmac("1234"),
                    external_id=str(uuid.uuid4()), channel="voice", status="expired")
    defaults.update(kw)
    async with db.AsyncSessionLocal() as session:
        otp = CabinetOtp(**defaults)
        session.add(otp)
        await session.commit()
        return otp


async def _get_token(db, user_id):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == user_id)
        )).scalars().first()


async def _get_form(db, user_id, deal_id):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(
            select(CabinetForm).where(CabinetForm.user_id == user_id,
                                      CabinetForm.deal_id == deal_id)
        )).scalar_one_or_none()


async def _event_count(db, event_type):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(
            select(func.count()).select_from(CabinetEvent)
            .where(CabinetEvent.event_type == event_type)
        )).scalar_one()


async def _backdate_token(db, user_id, **values):
    async with db.AsyncSessionLocal() as session:
        await session.execute(
            update(CabinetToken).where(CabinetToken.user_id == user_id).values(**values))
        await session.commit()


async def _authed(db, gw, suffix="1", deal="D-1"):
    """Полный цикл U4: выдача токена → request-code → verify → Bearer."""
    user = await _mk_user(db, suffix)
    url = await cabinet_service.issue_cabinet_token(user.id, "01", deal)
    assert url is not None
    token = url.split("#t=", 1)[1]
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    code = gw.calls[-1]["code"]
    result = await cabinet_service.verify_code(token, code)
    return user, f"Bearer {result['access_token']}"


def _draft_comments(bitrix):
    return [c for c in bitrix.by_method("crm.timeline.comment.add")
            if "Черновик" in c["params"]["fields"]["COMMENT"]]


# =======================
# Happy: триггер 1 — сабмит: одна конверсия, UF уходят, сигнала B нет
# =======================

@pytest.mark.asyncio
async def test_trigger1_submit_one_conversion_uf_pushed_no_signal_b(db, deps):
    user, bearer = await _authed(db, deps.gw)
    data = await cabinet_service.submit_form(bearer, {"credit_initial": 200000})
    assert data == {"status": "submitted"}

    assert len(deps.conversions) == 1, "ровно одна конверсия существующим путём"
    assert deps.conversions[0][0] == (user.id,)

    updates = deps.bitrix.by_method("crm.deal.update")
    assert len(updates) == 1, "поля анкеты ушли в UF по карте (триггер 1)"
    assert updates[0]["params"]["fields"]["UF_CRM_1770634607"] == 200000

    # [U7/R7] верификация даёт алерт-подтверждение телефона; иных алертов нет
    signals = [t for _, t in deps.dealer_alerts if "телефон подтверждён" not in t]
    assert signals == [], "сигнал B при штатной отправке не уходит (R26)"

    token = await _get_token(db, user.id)
    assert token.handoff_at is not None, "захват передачи проставлен"
    assert await _event_count(db, "handoff") == 1


# =======================
# Happy: триггер 2 — бездействие с черновиком: только комментарий + сигнал B
# =======================

@pytest.mark.asyncio
async def test_trigger2_draft_goes_as_comment_only_plus_signal_b(db, deps):
    now = datetime.now(timezone.utc)
    user = await _mk_user(db)
    await _mk_token(db, user.id, bound_at=now - timedelta(minutes=20),
                    last_seen_at=now - timedelta(minutes=11))
    await _mk_form(db, user.id, credit_term=36, tradein_condition="хорошее [почти]")

    stats = await cabinet_service.otp_worker_tick(now)
    assert stats["handoffs"] == 1

    assert len(deps.conversions) == 1, "конверсия одна — по таймауту бездействия"
    assert deps.bitrix.by_method("crm.deal.update") == [], \
        "незавершённая анкета в UF НЕ пишется (R41)"

    comments = deps.bitrix.by_method("crm.timeline.comment.add")
    assert len(comments) == 1, "черновик уходит ТОЛЬКО комментарием в таймлайн"
    comment = comments[0]["params"]["fields"]["COMMENT"]
    assert comments[0]["params"]["fields"]["ENTITY_ID"] == "D-77"
    assert "Черновик" in comment and "credit_term: 36" in comment
    assert "[" not in comment and "&#91;" in comment, "BBCode экранирован"

    assert len(deps.dealer_alerts) == 1
    dealer_code, text = deps.dealer_alerts[0]
    assert dealer_code == "01"
    assert "номер подтверждён, заявка передана в CRM, анкета не заполнена" in text
    assert "Черновик в комментарии сделки." in text, "вариант текста — по наличию черновика"
    assert "Иван" in text and "D-77" in text

    # Повторный тик — идемпотентность: ни второй конверсии, ни второго комментария
    stats = await cabinet_service.otp_worker_tick(now + timedelta(minutes=5))
    assert stats["handoffs"] == 0
    assert len(deps.conversions) == 1
    assert len(_draft_comments(deps.bitrix)) == 1
    assert len(deps.dealer_alerts) == 1


# =======================
# Happy: триггер 2 без черновика — отсчёт от bound_at, «Данных анкеты нет»
# =======================

@pytest.mark.asyncio
async def test_trigger2_no_draft_counts_from_bound_at(db, deps):
    now = datetime.now(timezone.utc)
    user = await _mk_user(db)
    await _mk_token(db, user.id, bound_at=now - timedelta(minutes=11),
                    last_seen_at=None)  # клиент ушёл сразу после верификации

    stats = await cabinet_service.otp_worker_tick(now)
    assert stats["handoffs"] == 1
    assert len(deps.conversions) == 1
    assert deps.bitrix.by_method("crm.timeline.comment.add") == [], \
        "черновика нет — комментарий не отправляется"
    assert len(deps.dealer_alerts) == 1
    assert "Данных анкеты нет." in deps.dealer_alerts[0][1]


# =======================
# Happy: триггер 3 — абсолютный потолок при живых пингах
# =======================

@pytest.mark.asyncio
async def test_trigger3_absolute_ceiling_despite_fresh_pings(db, deps):
    now = datetime.now(timezone.utc)
    old = await _mk_user(db, suffix="old")
    await _mk_token(db, old.id, bound_at=now - timedelta(minutes=61),
                    last_seen_at=now - timedelta(minutes=2))  # вкладка пингует
    fresh = await _mk_user(db, suffix="fresh")
    await _mk_token(db, fresh.id, deal_id="D-88",
                    bound_at=now - timedelta(minutes=30),
                    last_seen_at=now - timedelta(minutes=2))  # ни один триггер

    stats = await cabinet_service.otp_worker_tick(now)
    assert stats["handoffs"] == 1, "потолок сработал только у часового токена"
    assert len(deps.conversions) == 1
    assert (await _get_token(db, old.id)).handoff_at is not None
    assert (await _get_token(db, fresh.id)).handoff_at is None


# =======================
# Edge: поздняя отправка — без второй конверсии, UF дописаны, без дубля комментария
# =======================

@pytest.mark.asyncio
async def test_late_submit_appends_uf_without_second_conversion(db, deps):
    now = datetime.now(timezone.utc)
    user, bearer = await _authed(db, deps.gw)
    await cabinet_service.save_form_draft(bearer, {"credit_term": 36})
    await _backdate_token(db, user.id, bound_at=now - timedelta(minutes=20),
                          last_seen_at=now - timedelta(minutes=11))

    stats = await cabinet_service.otp_worker_tick(now)  # таймаут отработал
    assert stats["handoffs"] == 1
    assert len(deps.conversions) == 1
    assert len(_draft_comments(deps.bitrix)) == 1
    assert deps.bitrix.by_method("crm.deal.update") == []

    # Клиент всё же нажал «Отправить» после передачи
    data = await cabinet_service.submit_form(bearer, {"credit_initial": 200000})
    assert data == {"status": "submitted"}

    assert len(deps.conversions) == 1, "второй конверсии НЕТ — захват уже занят"
    updates = deps.bitrix.by_method("crm.deal.update")
    assert len(updates) == 1, "поля анкеты дописаны в UF"
    fields = updates[0]["params"]["fields"]
    assert fields["UF_CRM_1770634607"] == 200000
    assert fields["UF_CRM_1770634728"] == 36, "значение черновика тоже дописано"
    assert len(_draft_comments(deps.bitrix)) == 1, \
        "комментарий черновика не продублирован (идемпотентность)"
    # [U7/R7] минус алерт-подтверждение верификации: сигналов передачи ровно один
    signals = [t for _, t in deps.dealer_alerts if "телефон подтверждён" not in t]
    assert len(signals) == 1, "новых сигналов при поздней отправке нет"

    form = await _get_form(db, user.id, "D-1")
    assert form.submitted_at is not None, "поздняя отправка остаётся возможной"


# =======================
# Edge: гонка — два конкурентных attempt_handoff, конверсия ровно одна
# =======================

@pytest.mark.asyncio
async def test_race_concurrent_attempts_exactly_one_conversion(db, deps):
    now = datetime.now(timezone.utc)
    user = await _mk_user(db)
    await _mk_token(db, user.id, bound_at=now - timedelta(minutes=20),
                    last_seen_at=now - timedelta(minutes=11))

    results = await asyncio.gather(
        cabinet_service.attempt_handoff(user.id, "timeout", now=now),
        cabinet_service.attempt_handoff(user.id, "max_age", now=now),
    )
    assert sorted(results) == ["already_handed", "converted"], \
        "атомарный захват (rowcount): победитель ровно один"
    assert len(deps.conversions) == 1
    assert await _event_count(db, "handoff") == 1


# =======================
# Edge: R39 — без верификации не передаётся ни по одному триггеру
# =======================

@pytest.mark.asyncio
async def test_r39_unverified_never_handed_only_signal_a(db, deps):
    now = datetime.now(timezone.utc)
    user = await _mk_user(db)
    token = await _mk_token(db, user.id, bound_at=None, bound_phone=None)
    # code_requested 16 минут назад — зона сигнала A (U3)
    await _mk_otp(db, token.id, sent_at=now - timedelta(minutes=16))

    for trigger in ("submit", "timeout", "max_age"):
        outcome = await cabinet_service.attempt_handoff(user.id, trigger, now=now)
        assert outcome == "not_verified", f"R39: триггер {trigger} не передаёт"

    stats = await cabinet_service.otp_worker_tick(now)
    assert stats["handoffs"] == 0 and stats["signal_a"] == 1
    assert deps.conversions == [], "конверсии нет ни по одному триггеру"
    assert len(deps.dealer_alerts) == 1, "уходит только сигнал A"
    assert "не подтвердил номер" in deps.dealer_alerts[0][1]
    assert (await _get_token(db, user.id)).handoff_at is None


# =======================
# Integration: порядок сигналов — верификация → таймаут → сигнал B, сигнала A нет
# =======================

@pytest.mark.asyncio
async def test_signal_order_verified_then_timeout_gives_only_signal_b(db, deps):
    now = datetime.now(timezone.utc)
    user = await _mk_user(db)
    token = await _mk_token(db, user.id, bound_at=now - timedelta(minutes=11))
    # Код запрошен 12 минут назад и подтверждён — сигнал A отменён верификацией
    await _mk_otp(db, token.id, status="verified", sent_at=now - timedelta(minutes=12))

    stats = await cabinet_service.otp_worker_tick(now)
    assert stats["signal_a"] == 0, "сигнал A не отправлен ни разу"
    assert stats["handoffs"] == 1, "заявка ушла по таймауту 10 минут"
    assert len(deps.dealer_alerts) == 1
    text = deps.dealer_alerts[0][1]
    assert text.startswith("ℹ️") and "заявка передана в CRM" in text
    assert "не подтвердил" not in text
    assert await _event_count(db, "signal_a_sent") == 0


# =======================
# Edge: R34 — передача по таймауту не выставляет submitted_at
# =======================

@pytest.mark.asyncio
async def test_timeout_handoff_does_not_set_submitted_at(db, deps):
    now = datetime.now(timezone.utc)
    user = await _mk_user(db)
    await _mk_token(db, user.id, bound_at=now - timedelta(minutes=20),
                    last_seen_at=now - timedelta(minutes=11))
    await _mk_form(db, user.id, credit_term=36)

    stats = await cabinet_service.otp_worker_tick(now)
    assert stats["handoffs"] == 1
    form = await _get_form(db, user.id, "D-77")
    assert form.submitted_at is None, \
        "read-only только от сабмита клиента: фильтр R34 не включается"


# =======================
# Edge: рестарт между захватом и конверсией — заявка не теряется
# =======================

@pytest.mark.asyncio
async def test_crash_between_capture_and_conversion_is_recovered(db, deps, monkeypatch):
    """Захват handoff_at коммитится отдельно от конверсии: падение в этом окне
    раньше навсегда исключало заявку из всех триггеров (handoff_at IS NULL).
    Намерение ('handoff') и факт ('handoff_done') разведены, зависший захват
    подбирает otp_worker."""
    now = datetime.now(timezone.utc)
    user = await _mk_user(db)
    await _mk_token(db, user.id, bound_at=now - timedelta(minutes=20),
                    last_seen_at=now - timedelta(minutes=11))
    await _mk_form(db, user.id, credit_term=36)

    crashing = {"on": True}

    async def flaky_conversion(user_pk):
        if crashing["on"]:
            raise RuntimeError("рестарт посреди передачи")
        deps.conversions.append(((user_pk,), {}))

    monkeypatch.setattr(cabinet_service, "_run_conversion", flaky_conversion)

    stats = await cabinet_service.otp_worker_tick(now)
    assert stats["handoffs"] == 1, "захват прошёл"
    assert deps.conversions == [], "конверсия не выполнена — эмуляция падения"
    assert await _event_count(db, "handoff") == 1, "намерение записано"
    assert await _event_count(db, "handoff_done") == 0, "факт НЕ записан"
    assert (await _get_token(db, user.id)).handoff_at is not None
    assert _draft_comments(deps.bitrix) == [] and deps.dealer_alerts == []

    # Триггеры 2/3 такую заявку больше не видят — спасает только подбор зависших
    crashing["on"] = False
    later = now + timedelta(
        seconds=cabinet_service.HANDOFF_RECOVERY_DELAY_SECONDS + 60)
    stats = await cabinet_service.otp_worker_tick(later)
    assert stats["handoffs"] == 0, "таймаут-триггеры фильтруют по handoff_at"
    assert stats["recovered_handoffs"] == 1, "подбор довёл передачу"
    assert len(deps.conversions) == 1
    assert len(_draft_comments(deps.bitrix)) == 1
    assert len(deps.dealer_alerts) == 1 and "заявка передана в CRM" in deps.dealer_alerts[0][1]
    assert await _event_count(db, "handoff_done") == 1
    assert any("захвачена, но не завершена" in t for t in deps.tech_alerts), \
        "техалерт с идентификаторами — факт рестарта в окне виден"

    # Повторный тик после успеха — полный no-op
    stats = await cabinet_service.otp_worker_tick(later + timedelta(minutes=5))
    assert stats["recovered_handoffs"] == 0
    assert len(deps.conversions) == 1
    assert len(_draft_comments(deps.bitrix)) == 1
    assert len(deps.dealer_alerts) == 1
    assert len(deps.tech_alerts) == 1


@pytest.mark.asyncio
async def test_stuck_capture_not_recovered_before_delay(db, deps, monkeypatch):
    """Свежий захват (конверсия ещё идёт) подбором не трогается."""
    now = datetime.now(timezone.utc)
    user = await _mk_user(db)
    await _mk_token(db, user.id, bound_at=now - timedelta(minutes=20),
                    last_seen_at=now - timedelta(minutes=11))

    async def crashing(_user_pk):
        raise RuntimeError("рестарт посреди передачи")

    monkeypatch.setattr(cabinet_service, "_run_conversion", crashing)
    assert (await cabinet_service.otp_worker_tick(now))["handoffs"] == 1

    stats = await cabinet_service.otp_worker_tick(now + timedelta(minutes=5))
    assert stats["recovered_handoffs"] == 0, "порог подбора ещё не наступил"
    assert deps.tech_alerts == []


# =======================
# Edge: сделка появилась в конверсии — анкета уходит именно в неё
# =======================

@pytest.mark.asyncio
async def test_handoff_rereads_deal_created_by_conversion(db, deps, monkeypatch):
    """Токен выдан без сделки; сделку заводит сама конверсия — deal_id
    перечитывается ПОСЛЕ неё, иначе анкета осталась бы в локальной БД."""
    now = datetime.now(timezone.utc)
    user = await _mk_user(db)
    await _mk_token(db, user.id, deal_id=None, bound_at=now - timedelta(minutes=20),
                    last_seen_at=now - timedelta(minutes=11))
    await _mk_form(db, user.id, deal_id="", credit_initial=150000,
                   submitted_at=now - timedelta(minutes=15))

    async def conversion_creates_deal(user_pk):
        deps.conversions.append(((user_pk,), {}))
        async with db.AsyncSessionLocal() as session:
            await session.execute(
                update(User).where(User.id == user_pk).values(active_deal_id="D-NEW"))
            await session.commit()

    monkeypatch.setattr(cabinet_service, "_run_conversion", conversion_creates_deal)

    assert (await cabinet_service.otp_worker_tick(now))["handoffs"] == 1
    assert len(deps.conversions) == 1

    token = await _get_token(db, user.id)
    assert token.deal_id == "D-NEW", "сделка дописана в токен"
    assert (await _get_form(db, user.id, "D-NEW")).credit_initial == 150000

    updates = deps.bitrix.by_method("crm.deal.update")
    assert len(updates) == 1, "анкета ушла в UF появившейся сделки, без дублей"
    assert updates[0]["params"]["id"] == "D-NEW"
    assert updates[0]["params"]["fields"]["UF_CRM_1770634607"] == 150000


# =======================
# build_conversion_snapshot: прямые поведенческие тесты (не через фейк)
# =======================

@pytest.mark.asyncio
async def test_snapshot_merges_profile_without_overwriting_context(db, deps):
    user = await _mk_user(db)
    async with db.AsyncSessionLocal() as session:
        await session.execute(update(User).where(User.id == user.id).values(
            context_json=json.dumps({"scenario_type": "credit", "model": "Xray"},
                                    ensure_ascii=False)))
        session.add(CabinetProfile(user_id=user.id, scenario_type="tradein",
                                   qualification_payment=35000))
        await session.commit()

    platform_uid, snapshot = await cabinet_service.build_conversion_snapshot(user.id)
    assert platform_uid == user.platform_user_id
    assert snapshot["scenario_type"] == "credit", "кэш квалификации не перетирается"
    assert snapshot["payment"] == 35000, "пробел дозаполнен из cabinet_profile"
    assert snapshot["model"] == "Xray", "остальные поля контекста сохранены"


@pytest.mark.asyncio
async def test_snapshot_missing_user_and_broken_context_json(db, deps):
    assert await cabinet_service.build_conversion_snapshot(999_999) == (None, None)

    broken = await _mk_user(db, suffix="broken")
    not_dict = await _mk_user(db, suffix="notdict")
    async with db.AsyncSessionLocal() as session:
        await session.execute(update(User).where(User.id == broken.id).values(
            context_json="{не json"))
        await session.execute(update(User).where(User.id == not_dict.id).values(
            context_json='["список"]'))
        await session.commit()

    assert await cabinet_service.build_conversion_snapshot(broken.id) == (
        broken.platform_user_id, {})
    assert await cabinet_service.build_conversion_snapshot(not_dict.id) == (
        not_dict.platform_user_id, {})


@pytest.mark.asyncio
async def test_handoff_feeds_real_snapshot_into_conversion(db, deps, monkeypatch):
    """Интеграция: attempt_handoff → РЕАЛЬНЫЙ build_conversion_snapshot →
    застабленный final_sync_wrapper (роль обёртки main._cabinet_run_conversion)."""
    now = datetime.now(timezone.utc)
    user = await _mk_user(db)
    await _mk_token(db, user.id, bound_at=now - timedelta(minutes=20))
    async with db.AsyncSessionLocal() as session:
        await session.execute(update(User).where(User.id == user.id).values(
            context_json=json.dumps({"model": "Tiggo"}, ensure_ascii=False)))
        session.add(CabinetProfile(user_id=user.id, scenario_type="tradein",
                                   qualification_payment=25000))
        await session.commit()

    final_sync_calls = []

    async def run_conversion(user_pk):  # копия проводки main.py, без Bitrix
        platform_uid, snapshot = await cabinet_service.build_conversion_snapshot(user_pk)
        final_sync_calls.append((platform_uid, snapshot))

    monkeypatch.setattr(cabinet_service, "_run_conversion", run_conversion)
    assert await cabinet_service.attempt_handoff(user.id, "timeout", now=now) == "converted"

    assert final_sync_calls == [(user.platform_user_id,
                                 {"model": "Tiggo", "scenario_type": "tradein",
                                  "payment": 25000,
                                  # анкеты нет → исход «Авторизация» (план 2026-09-01-001)
                                  "cabinet_uf_type": "Авторизация трейд-ин"})]


# =======================
# Edge: имя клиента в HTML-алерте дилеру экранировано
# =======================

@pytest.mark.asyncio
async def test_signal_alerts_escape_client_values_for_html(db, deps):
    """Алерты уходят с format="html": < > & в имени/модели ломали сообщение."""
    now = datetime.now(timezone.utc)
    # Сигнал B — передача по таймауту
    handed = await _mk_user(db, suffix="b", full_name='Иван <b>&"Ко"')
    await _mk_token(db, handed.id, bound_at=now - timedelta(minutes=20),
                    last_seen_at=now - timedelta(minutes=11))
    # Сигнал A — код запрошен, номер не подтверждён
    waiting = await _mk_user(db, suffix="a", full_name="Пётр <script>")
    token_a = await _mk_token(db, waiting.id, deal_id="D-<99>", bound_at=None,
                              bound_phone=None)
    await _mk_otp(db, token_a.id, sent_at=now - timedelta(minutes=16))
    async with db.AsyncSessionLocal() as session:
        session.add(database.CabinetProfile(user_id=waiting.id,
                                            brand_model="Tiggo <8> & Pro"))
        await session.commit()

    stats = await cabinet_service.otp_worker_tick(now)
    assert stats["signal_a"] == 1 and stats["handoffs"] == 1

    texts = [t for _, t in deps.dealer_alerts]
    assert len(texts) == 2
    for text in texts:
        assert "<" not in text and ">" not in text, "сырой HTML в алерт не попадает"
    signal_b = next(t for t in texts if "заявка передана в CRM" in t)
    assert "Иван &lt;b&gt;&amp;&quot;Ко&quot;" in signal_b
    signal_a = next(t for t in texts if "не подтвердил номер" in t)
    assert "Пётр &lt;script&gt;" in signal_a
    assert "Tiggo &lt;8&gt; &amp; Pro" in signal_a
    assert "D-&lt;99&gt;" in signal_a


# =======================
# Source-пины (конвенция tests/test_payload.py): проводка и отсутствие
# параллельного пути конверсии
# =======================

def test_source_pins_run_conversion_wiring_and_no_parallel_path():
    src = MAIN_PY.read_text(encoding="utf-8")
    setup_src = src.split("def setup_cabinet_service", 1)[1]
    assert "run_conversion=_cabinet_run_conversion" in setup_src, \
        "run_conversion передаётся в setup обёрткой main.py"

    wrapper_src = src.split("async def _cabinet_run_conversion", 1)[1] \
                     .split("def setup_cabinet_service", 1)[0]
    assert "final_sync_wrapper" in wrapper_src, \
        "обёртка вызывает существующую конверсию — новых механизмов нет (R39)"
    assert "USER_CONTEXT[" not in wrapper_src and "USER_CONTEXT.get" not in wrapper_src, \
        "снапшот контекста строится ИЗ БД: кабинет живёт дольше сессии диалога"
    assert "cabinet_service.build_conversion_snapshot(" in wrapper_src, \
        "снапшот из БД собирает cabinet_service — main.py остаётся тонкой проводкой"

    svc_src = CABINET_SERVICE_PY.read_text(encoding="utf-8")
    snapshot_src = svc_src.split("async def build_conversion_snapshot", 1)[1] \
                          .split("\nasync def ", 1)[0]
    assert "context_json" in snapshot_src and "CabinetProfile" in snapshot_src, \
        "снапшот ИЗ БД: User.context_json + cabinet_profile"
    assert "USER_CONTEXT[" not in snapshot_src and "USER_CONTEXT.get" not in snapshot_src
    assert "final_sync_wrapper(" not in svc_src, \
        "cabinet_service не зовёт конверсию напрямую — только инъекция"
    assert "import main" not in svc_src, "строгий лист: обратных импортов нет"
    handoff_src = svc_src.split("async def attempt_handoff", 1)[1]
    assert "_run_conversion" in handoff_src, \
        "attempt_handoff — единственный потребитель инъецированной конверсии"


# =======================
# План 2026-08-14-001, U1: колбэк «передача доведена» + добор незакрытых сессий
# =======================

async def _drain_hook_tasks():
    """Дождаться fire-and-forget задач колбэка (в бою они фоновые)."""
    while cabinet_service._HOOK_TASKS:
        await asyncio.gather(*list(cabinet_service._HOOK_TASKS))


async def _backdate_event(db, event_type, delta):
    """Сдвинуть created_at события в прошлое: grace добора незакрытых сессий
    отсчитывается от факта 'handoff_done', не от handoff_at."""
    async with db.AsyncSessionLocal() as session:
        await session.execute(
            update(CabinetEvent).where(CabinetEvent.event_type == event_type)
            .values(created_at=datetime.now(timezone.utc) - delta))
        await session.commit()


class _HookRecorder:
    def __init__(self, fail_times=0):
        self.calls = []
        self._fail_times = fail_times

    async def __call__(self, user_id, trigger, deal_id, token_id):
        self.calls.append((user_id, trigger, deal_id, token_id))
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("hook boom")


@pytest.mark.asyncio
async def test_hook_fired_once_on_submit(db, deps, monkeypatch):
    hook = _HookRecorder()
    monkeypatch.setattr(cabinet_service, "_on_handoff_completed", hook)
    user, bearer = await _authed(db, deps.gw)
    await cabinet_service.submit_form(bearer, {"credit_initial": 200000})
    await _drain_hook_tasks()

    assert len(hook.calls) == 1, "колбэк вызван ровно один раз"
    called_user, trigger, _deal, token_id = hook.calls[0]
    assert called_user == user.id and trigger == "submit"
    token = await _get_token(db, user.id)
    assert token_id == token.id, "token_id колбэка — токен передачи"


@pytest.mark.asyncio
async def test_hook_trigger_timeout_and_max_age(db, deps, monkeypatch):
    hook = _HookRecorder()
    monkeypatch.setattr(cabinet_service, "_on_handoff_completed", hook)
    now = datetime.now(timezone.utc)

    # Триггер 2 (бездействие): 11 минут тишины от last_seen_at
    u_timeout = await _mk_user(db, "t2")
    await _mk_token(db, u_timeout.id, bound_at=now - timedelta(minutes=30),
                    last_seen_at=now - timedelta(minutes=11))
    # Триггер 3 (потолок): свежий last_seen_at, но верификация 2 часа назад
    u_maxage = await _mk_user(db, "t3")
    await _mk_token(db, u_maxage.id, deal_id="D-88",
                    bound_at=now - timedelta(hours=2),
                    last_seen_at=now - timedelta(seconds=30))

    await cabinet_service.otp_worker_tick(now)
    await _drain_hook_tasks()

    triggers = {user_id: trigger for user_id, trigger, _d, _t in hook.calls}
    assert triggers.get(u_timeout.id) == "timeout", "триггер бездействия"
    assert triggers.get(u_maxage.id) == "max_age", "триггер абсолютного потолка"


@pytest.mark.asyncio
async def test_hook_fired_via_recovery_path(db, deps, monkeypatch):
    """Рестарт между захватом и фактом: _recover_stuck_handoffs доводит
    передачу — колбэк обязан выстрелить и на этом пути."""
    hook = _HookRecorder()
    monkeypatch.setattr(cabinet_service, "_on_handoff_completed", hook)
    now = datetime.now(timezone.utc)
    user = await _mk_user(db)
    await _mk_token(db, user.id, bound_at=now - timedelta(hours=1),
                    handoff_at=now - timedelta(minutes=15))  # захват без факта

    await cabinet_service.otp_worker_tick(now)
    await _drain_hook_tasks()

    assert [c[1] for c in hook.calls] == ["recovery"]
    assert await _event_count(db, "handoff_done") == 1


@pytest.mark.asyncio
async def test_hook_failure_keeps_handoff_and_recovery_retries(db, deps, monkeypatch):
    """Сбой колбэка не роняет передачу; добор незакрытых сессий повторяет
    вызов, пока session_closed не записан."""
    hook = _HookRecorder(fail_times=1)
    monkeypatch.setattr(cabinet_service, "_on_handoff_completed", hook)
    user, bearer = await _authed(db, deps.gw)
    await cabinet_service.submit_form(bearer, {"credit_initial": 200000})
    await _drain_hook_tasks()
    assert len(hook.calls) == 1 and await _event_count(db, "handoff_done") == 1, \
        "первый вызов упал, но передача доведена"

    # Добор: факт доведения старше grace, session_closed нет → повтор
    now = datetime.now(timezone.utc)
    await _backdate_event(db, "handoff_done", timedelta(minutes=15))
    await cabinet_service.otp_worker_tick(now)
    await _drain_hook_tasks()
    assert len(hook.calls) == 2 and hook.calls[1][1] == "close_recovery"

    # Успешное завершение фиксируется — третьего вызова нет
    token = await _get_token(db, user.id)
    assert await cabinet_service.record_session_closed(user.id, token.id) is True
    await cabinet_service.otp_worker_tick(now)
    await _drain_hook_tasks()
    assert len(hook.calls) == 2, "session_closed остановил добор"


@pytest.mark.asyncio
async def test_hook_not_fired_until_conversion_succeeds(db, deps, monkeypatch):
    """Конверсия упала → передача не доведена → колбэк НЕ вызван."""
    hook = _HookRecorder()
    monkeypatch.setattr(cabinet_service, "_on_handoff_completed", hook)

    async def broken_conversion(user_id):
        raise RuntimeError("bitrix down")
    monkeypatch.setattr(cabinet_service, "_run_conversion", broken_conversion)

    user, bearer = await _authed(db, deps.gw)
    await cabinet_service.submit_form(bearer, {"credit_initial": 200000})
    await _drain_hook_tasks()
    assert hook.calls == [], "до успешной конверсии завершение не запускается"
    assert await _event_count(db, "handoff_done") == 0


@pytest.mark.asyncio
async def test_session_closed_dedup_and_empty_meta(db, deps):
    """Повтор завершения разными путями → событие session_closed одно
    (пустой meta — дедуп _record_event_once не зависит от пути повтора)."""
    user = await _mk_user(db)
    token = await _mk_token(db, user.id)
    assert await cabinet_service.record_session_closed(user.id, token.id) is True
    assert await cabinet_service.record_session_closed(user.id, token.id) is False
    assert await _event_count(db, "session_closed") == 1
    async with db.AsyncSessionLocal() as session:
        meta = (await session.execute(
            select(CabinetEvent.meta_json).where(
                CabinetEvent.event_type == "session_closed")
        )).scalar_one()
    assert json.loads(meta) == {}, "meta пустой — триггер только в лог"
    assert await cabinet_service.has_session_closed(token.id) is True


@pytest.mark.asyncio
async def test_unclosed_recovery_ignores_token_status(db, deps, monkeypatch):
    """Токен истёк/отозван ПОСЛЕ передачи → добор сессию не теряет
    (намеренно без фильтра по статусу — остаточное замечание ревью)."""
    hook = _HookRecorder()
    monkeypatch.setattr(cabinet_service, "_on_handoff_completed", hook)
    now = datetime.now(timezone.utc)
    user = await _mk_user(db)
    token = await _mk_token(db, user.id, status="expired",
                            bound_at=now - timedelta(hours=2),
                            handoff_at=now - timedelta(minutes=20))
    await cabinet_service._record_event_once(user.id, token.id, "handoff_done")
    await _backdate_event(db, "handoff_done", timedelta(minutes=15))

    await cabinet_service.otp_worker_tick(now)
    await _drain_hook_tasks()
    assert [c[1] for c in hook.calls] == ["close_recovery"], \
        "expired-токен не выпал из добора незакрытых сессий"


# =======================
# «Тип обращения» кабинетной конверсии (план 2026-09-01-001, решения O1–O3)
# =======================

async def _mk_type_fixture(db, scenario="credit", submitted=False,
                           with_form=True, **token_kw):
    """user + активный bound-токен (D-77) + профиль сценария + анкета
    (submitted/черновик/без анкеты) — база тестов «Типа обращения»."""
    now = datetime.now(timezone.utc)
    user = await _mk_user(db, suffix=uuid.uuid4().hex[:8])
    token_kw.setdefault("bound_at", now - timedelta(minutes=20))
    await _mk_token(db, user.id, **token_kw)
    async with db.AsyncSessionLocal() as session:
        session.add(CabinetProfile(user_id=user.id, scenario_type=scenario))
        if with_form:
            session.add(CabinetForm(user_id=user.id, deal_id="D-77",
                                    submitted_at=now if submitted else None,
                                    client_comment="тип-обращения"))
        await session.commit()
    return user


def test_cabinet_type_value_all_seven_scenarios():
    """Словарь O2: единый принцип для всех сценариев без исключений."""
    labels = {"credit": "кредит", "tradein": "трейд-ин", "combo": "комбо",
              "stock": "наличие", "consult": "консультация", "refusal": "отказ",
              "other_model": "другая модель"}
    assert cabinet_service.CABINET_TYPE_SCENARIO_LABELS == labels
    for scenario, label in labels.items():
        assert cabinet_service._cabinet_type_value(scenario, True) == f"Анкета {label}"
        assert cabinet_service._cabinet_type_value(scenario, False) == f"Авторизация {label}"


def test_cabinet_type_value_unknown_or_empty_is_none():
    """Вне словаря / пусто — решения владельца нет: значение не формируется,
    поле заполняет прежний фолбэк _map_type (см. отчёт 2026-09-01)."""
    for scenario in ("unknown", "", None, "нечто"):
        assert cabinet_service._cabinet_type_value(scenario, True) is None
        assert cabinet_service._cabinet_type_value(scenario, False) is None


@pytest.mark.asyncio
async def test_snapshot_type_submitted_form_is_anketa(db, deps):
    user = await _mk_type_fixture(db, submitted=True)
    _, snap = await cabinet_service.build_conversion_snapshot(user.id)
    assert snap[cabinet_service.CABINET_TYPE_KEY] == "Анкета кредит"


@pytest.mark.asyncio
async def test_snapshot_type_draft_or_missing_form_is_avtorizacia(db, deps):
    draft = await _mk_type_fixture(db, submitted=False)
    _, snap = await cabinet_service.build_conversion_snapshot(draft.id)
    assert snap[cabinet_service.CABINET_TYPE_KEY] == "Авторизация кредит"

    no_form = await _mk_type_fixture(db, with_form=False)
    _, snap2 = await cabinet_service.build_conversion_snapshot(no_form.id)
    assert snap2[cabinet_service.CABINET_TYPE_KEY] == "Авторизация кредит"


@pytest.mark.asyncio
async def test_snapshot_type_scenario_from_profile_not_context(db, deps):
    """O3: подпись — по сценарию выдачи ссылки (профиль); переквалификация в
    чате (context_json) на подпись не влияет, но остаётся приоритетной для
    scenario_type самого снапшота."""
    user = await _mk_type_fixture(db, scenario="credit", submitted=True)
    async with db.AsyncSessionLocal() as session:
        await session.execute(update(User).where(User.id == user.id).values(
            context_json=json.dumps({"scenario_type": "stock"})))
        await session.commit()
    _, snap = await cabinet_service.build_conversion_snapshot(user.id)
    assert snap[cabinet_service.CABINET_TYPE_KEY] == "Анкета кредит"
    assert snap["scenario_type"] == "stock", \
        "приоритет context_json в scenario_type снапшота не тронут"


@pytest.mark.asyncio
async def test_snapshot_type_unknown_scenario_no_key(db, deps):
    user = await _mk_type_fixture(db, scenario="unknown", submitted=True)
    _, snap = await cabinet_service.build_conversion_snapshot(user.id)
    assert cabinet_service.CABINET_TYPE_KEY not in snap


@pytest.mark.asyncio
async def test_snapshot_type_not_persisted_to_context_json(db, deps):
    """Ключ живёт только в памяти снапшота — context_json не загрязняется,
    чатовые контексты его не увидят (гарантия некабинетных сделок)."""
    user = await _mk_type_fixture(db, submitted=True)
    await cabinet_service.build_conversion_snapshot(user.id)
    async with db.AsyncSessionLocal() as session:
        fresh = await session.get(User, user.id)
    assert cabinet_service.CABINET_TYPE_KEY not in (fresh.context_json or "")


def _bitrix_capture(monkeypatch):
    """Подмена BitrixService._request с захватом вызовов; F_TYPE/F_PAYMENT
    пинятся на тестовые коды — env машины не влияет на ассерты."""
    captured = []

    async def fake_request(method, params, user_id=None, **kw):
        captured.append({"method": method, "params": params})
        return {"result": True}

    monkeypatch.setattr(main_mod.BitrixService, "_request", fake_request)
    monkeypatch.setattr(main_mod.BitrixService, "F_TYPE", "UF_TYPE_TEST")
    monkeypatch.setattr(main_mod.BitrixService, "F_PAYMENT", "UF_PAY_TEST")
    return captured


@pytest.mark.asyncio
async def test_update_deal_data_noncabinet_keeps_map_type(monkeypatch):
    """Некабинетная сделка: ключа cabinet_uf_type в контексте нет — поле
    заполняется прежним _map_type. Поведенческий тест неизменности."""
    captured = _bitrix_capture(monkeypatch)
    await main_mod.BitrixService.update_deal_data("D-9", {"scenario_type": "credit"})
    assert captured[-1]["method"] == "crm.deal.update"
    assert captured[-1]["params"]["fields"]["UF_TYPE_TEST"] == "Расчет кредита"


@pytest.mark.asyncio
async def test_update_deal_data_cabinet_value_overrides_map_type(monkeypatch):
    """Кабинетный снапшот несёт готовое значение — оно побеждает _map_type."""
    captured = _bitrix_capture(monkeypatch)
    await main_mod.BitrixService.update_deal_data(
        "D-9", {"cabinet_uf_type": "Анкета кредит", "scenario_type": "stock"})
    assert captured[-1]["params"]["fields"]["UF_TYPE_TEST"] == "Анкета кредит"


@pytest.mark.asyncio
async def test_update_deal_data_unknown_scenario_sends_no_type_key(monkeypatch):
    """Ни кабинетного значения, ни валидного сценария → ключ типа не уходит
    (None-фильтр сохраняет прежнюю семантику «поле не затирается»)."""
    captured = _bitrix_capture(monkeypatch)
    await main_mod.BitrixService.update_deal_data(
        "D-9", {"scenario_type": "unknown", "payment": 100})
    fields = captured[-1]["params"]["fields"]
    assert "UF_TYPE_TEST" not in fields
    assert fields["UF_PAY_TEST"] == 100


def test_type_override_present_in_both_deal_builders():
    """Пин: override с фолбэком стоит в create_deal_with_contact,
    update_deal_data и _create_web_deal (веб-вход, план 2026-09-30-001) —
    все точки записи F_TYPE конверсии."""
    src = MAIN_PY.read_text(encoding="utf-8")
    needle = "context.get('cabinet_uf_type') or cls._map_type(context.get('scenario_type'))"
    assert src.count(needle) == 3, \
        "override cabinet_uf_type должен стоять во всех точках сборки полей сделки"


def _capture_conversion_snapshots(monkeypatch):
    """Копия проводки main._cabinet_run_conversion: реальный
    build_conversion_snapshot, конверсия застаблена, снапшоты копятся."""
    snaps = []

    async def run_conversion(user_pk):
        _, snap = await cabinet_service.build_conversion_snapshot(user_pk)
        snaps.append(snap)

    monkeypatch.setattr(cabinet_service, "_run_conversion", run_conversion)
    return snaps


@pytest.mark.asyncio
async def test_submit_handoff_type_is_anketa(db, deps, monkeypatch):
    """Сабмит-триггер при отправленной анкете → «Анкета {сценарий}»."""
    snaps = _capture_conversion_snapshots(monkeypatch)
    user = await _mk_type_fixture(db, scenario="credit", submitted=True)
    assert await cabinet_service.attempt_handoff(user.id, trigger="submit") == "converted"
    assert snaps[0][cabinet_service.CABINET_TYPE_KEY] == "Анкета кредит"


@pytest.mark.asyncio
async def test_timeout_handoff_type_is_avtorizacia(db, deps, monkeypatch):
    """Таймаут-триггер при черновике → «Авторизация {сценарий}»."""
    snaps = _capture_conversion_snapshots(monkeypatch)
    user = await _mk_type_fixture(db, scenario="tradein", submitted=False)
    assert await cabinet_service.attempt_handoff(user.id, trigger="timeout") == "converted"
    assert snaps[0][cabinet_service.CABINET_TYPE_KEY] == "Авторизация трейд-ин"


@pytest.mark.asyncio
async def test_recovery_of_submit_capture_type_is_anketa(db, deps, monkeypatch):
    """Recovery сабмит-захвата → «Анкета {сценарий}»: признак — submitted_at
    в момент конверсии, НЕ триггер (recovery подменяет его на 'recovery')."""
    snaps = _capture_conversion_snapshots(monkeypatch)
    now = datetime.now(timezone.utc)
    # Захват состоялся (handoff_at), конверсия не доведена (нет handoff_done),
    # анкета отправлена — сценарий «рестарт между захватом сабмита и фактом»
    user = await _mk_type_fixture(db, scenario="combo", submitted=True,
                                  handoff_at=now - timedelta(minutes=15))
    recovered = await cabinet_service._recover_stuck_handoffs(now)
    assert recovered == 1
    assert snaps[0][cabinet_service.CABINET_TYPE_KEY] == "Анкета комбо"


@pytest.mark.asyncio
async def test_recovery_not_started_when_bitrix_off(db, deps, monkeypatch):
    """2026-10-02: пустой BITRIX_WEBHOOK_URL — подбор зависших захватов не
    запускается вовсе: ни конверсии, ни техалерта, захват остаётся как был."""
    snaps = _capture_conversion_snapshots(monkeypatch)
    alerts = []

    async def tech_alert(text):
        alerts.append(text)

    monkeypatch.setattr(cabinet_service, "_tech_alert", tech_alert)
    monkeypatch.setattr(cabinet_service, "_bitrix_enabled", lambda: False)
    now = datetime.now(timezone.utc)
    await _mk_type_fixture(db, scenario="combo", submitted=True,
                           handoff_at=now - timedelta(minutes=15))
    assert await cabinet_service._recover_stuck_handoffs(now) == 0
    assert snaps == [] and alerts == []

    # Битрикс включили — тот же захват подбирается штатно
    monkeypatch.setattr(cabinet_service, "_bitrix_enabled", lambda: True)
    assert await cabinet_service._recover_stuck_handoffs(now) == 1
    assert len(snaps) == 1


def test_main_wires_bitrix_enabled_from_webhook(monkeypatch):
    import main as main_mod
    main_mod.setup_cabinet_service()
    monkeypatch.setattr(main_mod.BitrixService, "WEBHOOK", "")
    assert cabinet_service._bitrix_enabled() is False
    monkeypatch.setattr(main_mod.BitrixService, "WEBHOOK", "https://example.invalid/rest/1/x/")
    assert cabinet_service._bitrix_enabled() is True


@pytest.mark.asyncio
async def test_late_submit_rewrites_type_to_anketa(db, deps, monkeypatch):
    """O1: поздний сабмит после таймаут-передачи дозаписывает поле на
    «Анкета {сценарий}» отдельным crm.deal.update."""
    monkeypatch.setenv("BITRIX_FIELD_TYPE", "UF_TYPE_TEST")
    now = datetime.now(timezone.utc)
    # Захват уже занят таймаутом (handoff_at стоит), анкета отправлена позже
    user = await _mk_type_fixture(db, scenario="credit", submitted=True,
                                  handoff_at=now - timedelta(minutes=10))
    assert await cabinet_service.attempt_handoff(user.id, trigger="submit") == "late_submit"

    type_updates = [c for c in deps.bitrix.by_method("crm.deal.update")
                    if "UF_TYPE_TEST" in c["params"]["fields"]]
    assert len(type_updates) == 1, "дозапись типа — ровно один deal.update"
    assert type_updates[0]["params"]["id"] == "D-77"
    assert type_updates[0]["params"]["fields"] == {"UF_TYPE_TEST": "Анкета кредит"}


@pytest.mark.asyncio
async def test_late_submit_unknown_scenario_leaves_type_untouched(db, deps, monkeypatch):
    """Вне словаря сценариев дозаписи нет: конверсия не повторяется, фолбэка
    _map_type у этой ветки нет — поле остаётся как было."""
    monkeypatch.setenv("BITRIX_FIELD_TYPE", "UF_TYPE_TEST")
    now = datetime.now(timezone.utc)
    user = await _mk_type_fixture(db, scenario="unknown", submitted=True,
                                  handoff_at=now - timedelta(minutes=10))
    assert await cabinet_service.attempt_handoff(user.id, trigger="submit") == "late_submit"
    assert [c for c in deps.bitrix.by_method("crm.deal.update")
            if "UF_TYPE_TEST" in c["params"]["fields"]] == []


# =======================
# Ускорение B (2026-10-03): ответ клиенту — сразу после захвата handoff_at,
# передача в Битрикс — в фоне
# =======================

def _blocking_conversion(monkeypatch, deps):
    """Конверсия, которая ждёт сигнала: имитирует медленный Битрикс."""
    gate = asyncio.Event()
    started = asyncio.Event()

    async def slow_conversion(user_id):
        started.set()
        await gate.wait()
        deps.conversions.append(((user_id,), {}))

    monkeypatch.setattr(cabinet_service, "_run_conversion", slow_conversion)
    return gate, started


@pytest.mark.asyncio
async def test_submit_answers_before_bitrix_and_handoff_captured(db, deps, monkeypatch):
    gate, started = _blocking_conversion(monkeypatch, deps)
    user, bearer = await _authed(db, deps.gw)
    data = await asyncio.wait_for(
        cabinet_service._submit_form_raw(bearer, {"credit_initial": 200000}), timeout=2)
    assert data == {"status": "submitted"}
    token = await _get_token(db, user.id)
    assert token.handoff_at is not None and await _event_count(db, "handoff") == 1
    await asyncio.wait_for(started.wait(), timeout=2)
    assert deps.conversions == [] and await _event_count(db, "handoff_done") == 0
    gate.set()
    await cabinet_service.drain_background_handoffs()
    assert len(deps.conversions) == 1 and await _event_count(db, "handoff_done") == 1
    assert deps.bitrix.by_method("crm.deal.update"), "поля анкеты дошли в фоне"


@pytest.mark.asyncio
async def test_double_submit_during_background_is_409_one_conversion(db, deps, monkeypatch):
    gate, started = _blocking_conversion(monkeypatch, deps)
    user, bearer = await _authed(db, deps.gw)
    await cabinet_service._submit_form_raw(bearer, {"credit_initial": 200000})
    with pytest.raises(cabinet_service.ApiError) as exc:
        await cabinet_service._submit_form_raw(bearer, {"credit_initial": 300000})
    assert exc.value.status == 409
    gate.set()
    await cabinet_service.drain_background_handoffs()
    assert len(deps.conversions) == 1 and await _event_count(db, "handoff") == 1


@pytest.mark.asyncio
async def test_restart_during_background_handoff_recovered(db, deps, monkeypatch):
    """Бот перезапустился, пока фоновая передача ждала Битрикс: захват записан,
    факта нет → подбор зависших захватов доводит передачу ровно один раз."""
    gate, started = _blocking_conversion(monkeypatch, deps)
    user, bearer = await _authed(db, deps.gw)
    await cabinet_service._submit_form_raw(bearer, {"credit_initial": 200000})
    await asyncio.wait_for(started.wait(), timeout=2)
    for task in list(cabinet_service._HANDOFF_TASKS):      # «перезапуск»
        task.cancel()
    await asyncio.gather(*cabinet_service._HANDOFF_TASKS, return_exceptions=True)
    assert await _event_count(db, "handoff_done") == 0 and deps.conversions == []

    async def fast_conversion(user_id):
        deps.conversions.append(((user_id,), {}))

    monkeypatch.setattr(cabinet_service, "_run_conversion", fast_conversion)
    later = datetime.now(timezone.utc) + timedelta(
        seconds=cabinet_service.HANDOFF_RECOVERY_DELAY_SECONDS + 60)
    assert await cabinet_service._recover_stuck_handoffs(later) == 1
    assert len(deps.conversions) == 1 and await _event_count(db, "handoff_done") == 1
    updates = [u for u in deps.bitrix.by_method("crm.deal.update")
               if "UF_CRM_1770634607" in u["params"]["fields"]]
    assert len(updates) == 1, "поля анкеты — один раз"


@pytest.mark.asyncio
async def test_late_submit_after_timeout_stays_synchronous(db, deps):
    """Поздний сабмит (захват занят таймаутом) дозаписывает поля до ответа:
    у этой ветки нет подбора после перезапуска."""
    user, bearer = await _authed(db, deps.gw)
    assert await cabinet_service.attempt_handoff(user.id, "timeout") == "converted"
    deps.bitrix.calls.clear()
    await cabinet_service._submit_form_raw(bearer, {"credit_initial": 250000})
    assert not cabinet_service._HANDOFF_TASKS
    assert any(c["params"]["fields"].get("UF_CRM_1770634607") == 250000
               for c in deps.bitrix.by_method("crm.deal.update"))
