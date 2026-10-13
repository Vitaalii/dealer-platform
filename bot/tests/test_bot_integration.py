"""U7: интеграция кабинета с ботом (R7, R8/AE6, R25, R34, склейка deal hunt).

Проверяется логика cabinet_service (prepare_cabinet_context, пост-верификация,
страж телефона, фильтр R34, rebind_cabinet_on_merge) плюс source-grep-пины
wiring'а в main.py (конвенция tests/test_payload.py): фильтр в обеих точках
карты, выдача токена в обеих точках сборки qualification_context, rebind
в завершении deal hunt.
"""
import json
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

import database
from database import (CabinetEvent, CabinetForm, CabinetProfile, CabinetToken,
                      PendingSync, User)

import cabinet_service

from conftest import CONSENT, PHONE

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAIN_PY = ROOT / "main.py"
CABINET_SERVICE_PY = ROOT / "cabinet_service.py"

OTHER_PHONE = "+79005550000"
DEALERS = {
    "01": {"dealer_name": "Тест Авто", "website": "https://dealer-one.ru/", "code": "01"},
    "03": {"dealer_name": "Плохой Авто", "website": "http://insecure.ru/", "code": "03"},
}


# Фикстуры db/deps — общие, в conftest.py; U7 отличается только картой дилеров.

@pytest.fixture
def dealers():
    return DEALERS


# =======================
# Хелперы
# =======================

async def _mk_user(db, suffix="1", full_name="Иван", phone=None):
    async with db.AsyncSessionLocal() as session:
        user = User(platform_user_id=f"u-{suffix}", channel="max",
                    full_name=full_name, phone=phone)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _mk_token(db, user_id, **kw):
    defaults = dict(user_id=user_id, dealer_code="01", deal_id="A-1",
                    token_hash=uuid.uuid4().hex + uuid.uuid4().hex[:32],
                    status="active", bound_phone=PHONE)
    defaults.update(kw)
    async with db.AsyncSessionLocal() as session:
        token = CabinetToken(**defaults)
        session.add(token)
        await session.commit()
        await session.refresh(token)
        return token


async def _mk_form(db, user_id, deal_id="A-1", **fields):
    async with db.AsyncSessionLocal() as session:
        form = CabinetForm(user_id=user_id, deal_id=deal_id, **fields)
        session.add(form)
        await session.commit()
        return form


async def _get_user(db, user_id):
    async with db.AsyncSessionLocal() as session:
        return await session.get(User, user_id)


async def _get_profile(db, user_id):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == user_id)
        )).scalar_one_or_none()


async def _get_form(db, user_id, deal_id):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(
            select(CabinetForm).where(CabinetForm.user_id == user_id,
                                      CabinetForm.deal_id == deal_id)
        )).scalar_one_or_none()


async def _all_forms(db, user_id):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(
            select(CabinetForm).where(CabinetForm.user_id == user_id)
        )).scalars().all()


async def _token_count(db, user_id):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(
            select(func.count()).select_from(CabinetToken)
            .where(CabinetToken.user_id == user_id)
        )).scalar_one()


async def _backdate_sends(db, seconds=61):
    """Старит записи кодов: sent_at и момент создания (expires_at − OTP_TTL).
    seconds>600 снимает и голосовое окно номера (O1)."""
    created = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    async with db.AsyncSessionLocal() as session:
        await session.execute(
            update(database.CabinetOtp).where(database.CabinetOtp.sent_at.is_not(None))
            .values(sent_at=created))
        await session.execute(
            update(database.CabinetOtp)
            .values(expires_at=created + cabinet_service.OTP_TTL))
        await session.commit()


async def _bind(gw, token_plain, phone=PHONE):
    await cabinet_service.request_code(token_plain, phone=phone, consent=CONSENT)
    code = gw.calls[-1]["code"]
    return await cabinet_service.verify_code(token_plain, code)


async def _authed(db, gw, suffix="1", deal="A-1"):
    """Выдача → верификация → Bearer (полный путь клиента без телефона)."""
    user = await _mk_user(db, suffix)
    url = await cabinet_service.issue_cabinet_token(user.id, "01", deal)
    assert url is not None
    token_plain = url.split("#t=", 1)[1]
    result = await _bind(gw, token_plain)
    return user, url, token_plain, f"Bearer {result['access_token']}"


def _confirm_alerts(deps):
    return [t for _, t in deps.dealer_alerts if "телефон подтверждён" in t]


def _mismatch_alerts(deps):
    return [t for _, t in deps.dealer_alerts if "расхождение" in t or "отличный от" in t]


def _comments(bitrix, needle=None):
    rows = bitrix.by_method("crm.timeline.comment.add")
    if needle is None:
        return rows
    return [c for c in rows if needle in c["params"]["fields"]["COMMENT"]]


# =======================
# Happy: cabinet_url + cabinet_status на каждом этапе
# =======================

@pytest.mark.asyncio
async def test_happy_cabinet_context_status_stages(db, deps):
    user = await _mk_user(db)

    # Этап 1: первый AI handoff — клиент без телефона, ссылка выдана
    ctx1 = await cabinet_service.prepare_cabinet_context(
        user.id, "01", deal_id="A-1", has_phone=False,
        scenario_type="credit", payment=35000, brand_model="Xray")
    assert ctx1["cabinet_status"] == "not_visited"
    url = ctx1["cabinet_url"]
    assert url is not None and url.startswith("https://dealer-one.ru/cabinet#t=")

    # Профиль заполнен из контекста квалификации (R25: платёж — первичная копия)
    profile = await _get_profile(db, user.id)
    assert profile.qualification_payment == 35000
    assert profile.scenario_type == "credit"
    assert profile.brand_model == "Xray"
    assert profile.deal_id == "A-1" and profile.dealer_code == "01"

    # Повторная сборка — тот же URL (R19), статус прежний
    ctx1b = await cabinet_service.prepare_cabinet_context(
        user.id, "01", deal_id="A-1", has_phone=False)
    assert ctx1b["cabinet_url"] == url and ctx1b["cabinet_status"] == "not_visited"

    # Этап 2: верификация → phone_confirmed; телефон появился, но URL сохраняется
    token_plain = url.split("#t=", 1)[1]
    result = await _bind(deps.gw, token_plain)
    ctx2 = await cabinet_service.prepare_cabinet_context(
        user.id, "01", deal_id="A-1", has_phone=True)
    assert ctx2["cabinet_status"] == "phone_confirmed"
    assert ctx2["cabinet_url"] == url, "после верификации ссылка не пропадает из контекста"

    # Этап 3: сабмит анкеты → form_submitted
    await cabinet_service.submit_form(f"Bearer {result['access_token']}",
                                      {"credit_initial": 100000})
    ctx3 = await cabinet_service.prepare_cabinet_context(
        user.id, "01", deal_id="A-1", has_phone=True)
    assert ctx3["cabinet_status"] == "form_submitted"
    assert ctx3["cabinet_url"] == url


@pytest.mark.asyncio
async def test_link_not_issued_when_phone_already_in_chat(db, deps):
    """План U7: телефон уже есть в чате → штатная конверсия отработала, ссылка не выдаётся."""
    user = await _mk_user(db, phone=PHONE)
    ctx = await cabinet_service.prepare_cabinet_context(
        user.id, "01", deal_id="A-1", has_phone=True)
    assert ctx["cabinet_url"] is None
    assert ctx["cabinet_status"] == "not_visited"
    assert await _token_count(db, user.id) == 0


# =======================
# Пост-верификация (R7): save_user_phone + алерт дилеру, идемпотентно
# =======================

@pytest.mark.asyncio
async def test_post_verify_saves_phone_and_alerts_dealer_once(db, deps):
    user, url, token_plain, _ = await _authed(db, deps.gw)

    fresh = await _get_user(db, user.id)
    assert fresh.phone == PHONE, "R7: подтверждённый номер записан в User.phone"
    assert len(_confirm_alerts(deps)) == 1
    assert "01" == deps.dealer_alerts[0][0]
    assert "A-1" in _confirm_alerts(deps)[0]

    # Пометка источника «кабинет»
    async with db.AsyncSessionLocal() as session:
        saved_events = (await session.execute(
            select(CabinetEvent).where(CabinetEvent.event_type == "cabinet_phone_saved")
        )).scalars().all()
    assert len(saved_events) == 1
    assert json.loads(saved_events[0].meta_json)["source"] == "cabinet"

    # Повторный вход (второй verify) — алерт не дублируется (идемпотентно по сделке)
    await _backdate_sends(db, seconds=601)  # снимает кулдаун и голосовое окно (O1)
    await _bind(deps.gw, token_plain)
    assert len(_confirm_alerts(deps)) == 1, "алерт о подтверждении — один на сделку"


@pytest.mark.asyncio
async def test_verify_mismatch_keeps_chat_phone_comment_and_alert(db, deps):
    """Расхождение при верификации: в чате уже другой номер → User.phone
    не перезаписывается, комментарий в сделку + алерт."""
    chat_phone = "+79005556677"
    user = await _mk_user(db, phone=chat_phone)
    url = await cabinet_service.issue_cabinet_token(user.id, "01", "A-1")
    token_plain = url.split("#t=", 1)[1]
    await _bind(deps.gw, token_plain, phone=PHONE)  # клиент ввёл другой номер

    fresh = await _get_user(db, user.id)
    assert fresh.phone == chat_phone, "User.phone НЕ перезаписан кабинетным номером"
    assert len(_comments(deps.bitrix, "Номер из чата сохранён")) == 1
    assert len(_mismatch_alerts(deps)) == 1
    assert "A-1" in _mismatch_alerts(deps)[0]


# =======================
# AE6: другой номер в чате после привязки — страж save_user_phone
# =======================

@pytest.mark.asyncio
async def test_ae6_chat_phone_after_binding_not_overwritten(db, deps):
    """AE6: vCard/сообщение с другим номером после привязки. Приём контакта
    в боте (tg contact / рефлекс) сводится к save_user_phone — страж
    встроен туда и тестируется напрямую."""
    user, _, _, _ = await _authed(db, deps.gw)
    assert (await _get_user(db, user.id)).phone == PHONE

    outcome = await database.save_user_phone(user.platform_user_id, OTHER_PHONE)
    assert outcome is False, "запись отклонена стражем (R8)"
    assert (await _get_user(db, user.id)).phone == PHONE, "User.phone не изменился"
    assert len(_comments(deps.bitrix, "отличный от")) == 1, "комментарий в таймлайн"
    assert len(_mismatch_alerts(deps)) == 1, "алерт дилеру о расхождении"

    # Идемпотентность: повтор того же расхождения не спамит
    await database.save_user_phone(user.platform_user_id, OTHER_PHONE)
    assert len(_comments(deps.bitrix, "отличный от")) == 1
    assert len(_mismatch_alerts(deps)) == 1

    # Тот же номер в другом формате — запись разрешена (безвредна)
    ok = await database.save_user_phone(user.platform_user_id, "8" + PHONE[2:])
    assert ok is True


# =======================
# R34: фильтр «кабинет перезаписывает»
# =======================

CHAT_FIELDS = {
    "STAGE_ID": "C9:UC_0SOJJA",
    "UF_CRM_1770635063": "Lada",       # tradein_make — заполненный код карты кабинета
    "UF_CRM_1770634607": 100000,       # credit_initial — заполненный код
    "UF_CRM_1770636065": "отчёт ИИ",   # report — НЕ поле кабинета
}


@pytest.mark.asyncio
async def test_r34_filters_after_submit_same_deal(db, deps):
    user = await _mk_user(db)
    await _mk_form(db, user.id, deal_id="D-9", tradein_make="Lada",
                   submitted_at=datetime.now(timezone.utc))

    out = await cabinet_service.filter_cabinet_owned_fields(dict(CHAT_FIELDS), "D-9")
    assert "UF_CRM_1770635063" not in out, "tradein_make отфильтрован (R34)"
    assert "UF_CRM_1770634607" not in out
    assert out["STAGE_ID"] == "C9:UC_0SOJJA", "стадия и не-кабинетные поля проходят"
    assert out["UF_CRM_1770636065"] == "отчёт ИИ"


@pytest.mark.asyncio
async def test_r34_passes_before_submit_and_for_other_deal(db, deps):
    user = await _mk_user(db)
    await _mk_form(db, user.id, deal_id="D-10", tradein_make="Lada")  # черновик

    out_draft = await cabinet_service.filter_cabinet_owned_fields(dict(CHAT_FIELDS), "D-10")
    assert out_draft == CHAT_FIELDS, "до сабмита чат-путь пишет всё"

    # Новая сделка при старой отправленной анкете — ключи проходят (проверка по сделке)
    await _mk_form(db, user.id, deal_id="D-11", tradein_make="Kia",
                   submitted_at=datetime.now(timezone.utc))
    out_new = await cabinet_service.filter_cabinet_owned_fields(dict(CHAT_FIELDS), "D-12")
    assert out_new == CHAT_FIELDS


# =======================
# Integration: ссылка выдана ДО появления сделки — анкета доезжает в неё
# =======================

@pytest.mark.asyncio
async def test_token_issued_without_deal_adopts_deal_and_delivers_form(db, deps):
    """Токен без сделки: клиент отправил анкету раньше, чем сделка появилась.

    Анкета лежит под пустым deal_id и без дозаполнения не уехала бы никуда
    (push_form_to_bitrix без сделки только логировал предупреждение)."""
    user = await _mk_user(db)
    ctx = await cabinet_service.prepare_cabinet_context(
        user.id, "01", deal_id=None, has_phone=False)
    url = ctx["cabinet_url"]
    assert url is not None
    token_plain = url.split("#t=", 1)[1]
    result = await _bind(deps.gw, token_plain)

    await cabinet_service.submit_form(f"Bearer {result['access_token']}",
                                      {"credit_initial": 100000})
    assert len(deps.conversions) == 1, "конверсия ушла — передача состоялась"
    assert deps.bitrix.by_method("crm.deal.update") == [], "сделки ещё нет — слать некуда"
    assert (await _get_form(db, user.id, "")).submitted_at is not None

    # Сделка появилась и пришла в контексте следующего вызова
    ctx2 = await cabinet_service.prepare_cabinet_context(
        user.id, "01", deal_id="A-9", has_phone=True)
    assert ctx2["cabinet_url"] == url, "R19: дозаполнение сделки не меняет ссылку"
    assert ctx2["cabinet_status"] == "form_submitted"

    async with db.AsyncSessionLocal() as session:
        token = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == user.id,
                                       CabinetToken.status == "active")
        )).scalars().one()
    assert token.deal_id == "A-9", "сделка дописана в токен"
    assert (await _get_profile(db, user.id)).deal_id == "A-9"
    assert await _get_form(db, user.id, "") is None, "строка анкеты переехала на сделку"
    assert (await _get_form(db, user.id, "A-9")).credit_initial == 100000

    updates = deps.bitrix.by_method("crm.deal.update")
    assert len(updates) == 1, "анкета доехала в UF появившейся сделки"
    assert updates[0]["params"]["id"] == "A-9"
    assert updates[0]["params"]["fields"]["UF_CRM_1770634607"] == 100000
    assert len(deps.conversions) == 1, "повторной конверсии нет"

    # Ссылка по-прежнему живая, а повторная сборка контекста дублей не делает
    assert await cabinet_service.validate_bearer(
        f"Bearer {result['access_token']}") is not None
    ctx3 = await cabinet_service.prepare_cabinet_context(
        user.id, "01", deal_id="A-9", has_phone=True)
    assert ctx3["cabinet_url"] == url
    assert len(deps.bitrix.by_method("crm.deal.update")) == 1, "дублей отправки нет"


@pytest.mark.asyncio
async def test_adopt_deal_merges_draft_row_by_merge_rule(db, deps):
    """Коллизия (user_id, deal_id) при дозаполнении решается тем же правилом
    слияния, что и склейка deal hunt: побеждает строка с submitted_at."""
    user = await _mk_user(db)
    await _mk_token(db, user.id, deal_id=None, bound_at=datetime.now(timezone.utc))
    await _mk_form(db, user.id, deal_id="", credit_initial=200000,
                   submitted_at=datetime.now(timezone.utc))
    await _mk_form(db, user.id, deal_id="A-9", credit_term=36)  # черновик по сделке

    outcome = await cabinet_service.adopt_deal_id(user.id, "A-9")
    assert outcome["token_updated"] is True
    assert outcome["form"] == "merged_old_won"
    winner = await _get_form(db, user.id, "A-9")
    assert winner.credit_initial == 200000 and winner.submitted_at is not None

    # Повторный вызов — no-op: сделка уже проставлена, авторитетен токен
    assert (await cabinet_service.adopt_deal_id(user.id, "B-9"))["token_updated"] is False
    async with db.AsyncSessionLocal() as session:
        token = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == user.id))).scalars().one()
    assert token.deal_id == "A-9"


# =======================
# Edge: дилер с http:// в Website_URL
# =======================

@pytest.mark.asyncio
async def test_http_website_no_link_and_tech_alert(db, deps):
    user = await _mk_user(db)
    ctx = await cabinet_service.prepare_cabinet_context(
        user.id, "03", deal_id="A-1", has_phone=False)
    assert ctx["cabinet_url"] is None, "http:// — ссылка не выдаётся"
    assert ctx["cabinet_status"] == "not_visited"
    assert await _token_count(db, user.id) == 0
    assert any("Website_URL" in t and "03" in t for t in deps.tech_alerts), \
        "техалерт о неверной конфигурации дилера"


# =======================
# Integration: сабмит на A → склейка в B
# =======================

@pytest.mark.asyncio
async def test_merge_after_submit_resends_uf_to_main_deal(db, deps):
    user, _, _, bearer = await _authed(db, deps.gw, deal="A-1")
    await cabinet_service.submit_form(bearer, {"tradein_make": "Lada",
                                               "credit_initial": 5000})
    assert len(deps.conversions) == 1
    assert any(c["params"]["id"] == "A-1"
               for c in deps.bitrix.by_method("crm.deal.update"))

    # Сбойная постановка в очередь со старой сделкой (params содержат deal_id)
    async with db.AsyncSessionLocal() as session:
        session.add(PendingSync(method="crm.deal.update",
                                payload=json.dumps({"id": "A-1", "fields": {"X": 1}}),
                                user_id=user.platform_user_id, status="pending"))
        session.add(PendingSync(method="crm.timeline.comment.add",
                                payload=json.dumps({"fields": {"ENTITY_ID": "A-1",
                                                               "ENTITY_TYPE": "DEAL",
                                                               "COMMENT": "x"}}),
                                user_id=user.platform_user_id, status="pending"))
        await session.commit()

    outcome = await cabinet_service.rebind_cabinet_on_merge("A-1", "B-2", user.id)
    assert outcome["token_rebound"] is True
    assert outcome["resent"] == "form"
    assert outcome["pending_rewritten"] == 2

    # Токен сохранён, deal_id обновлён; анкета переехала на B-2
    async with db.AsyncSessionLocal() as session:
        token = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == user.id,
                                       CabinetToken.status == "active")
        )).scalars().one()
    assert token.deal_id == "B-2" and token.bound_phone == PHONE
    assert (await _get_form(db, user.id, "B-2")).submitted_at is not None

    # push вызван для B — UF-значения на основной сделке
    updates_b = [c for c in deps.bitrix.by_method("crm.deal.update")
                 if c["params"]["id"] == "B-2"]
    assert len(updates_b) == 1
    assert updates_b[0]["params"]["fields"]["UF_CRM_1770635063"] == "Lada"
    assert updates_b[0]["params"]["fields"]["UF_CRM_1770634607"] == 5000

    # Строки pending_sync переписаны на B
    async with db.AsyncSessionLocal() as session:
        rows = (await session.execute(select(PendingSync))).scalars().all()
    payloads = [json.loads(r.payload) for r in rows]
    assert {p.get("id") or p["fields"].get("ENTITY_ID") for p in payloads} == {"B-2"}

    # Чат-путь на B теперь фильтруется (R34), повторной конверсии нет
    out = await cabinet_service.filter_cabinet_owned_fields(dict(CHAT_FIELDS), "B-2")
    assert "UF_CRM_1770635063" not in out
    assert len(deps.conversions) == 1, "повторная конверсия при склейке не запускается"


# =======================
# Integration: передача по таймауту на A → склейка в B
# =======================

@pytest.mark.asyncio
async def test_merge_after_timeout_resends_draft_comment_no_second_conversion(db, deps):
    now = datetime.now(timezone.utc)
    user = await _mk_user(db)
    await _mk_token(db, user.id, deal_id="A-1",
                    bound_at=now - timedelta(minutes=20),
                    last_seen_at=now - timedelta(minutes=11))
    await _mk_form(db, user.id, deal_id="A-1", credit_term=36)

    stats = await cabinet_service.otp_worker_tick(now)
    assert stats["handoffs"] == 1 and len(deps.conversions) == 1
    assert len(_comments(deps.bitrix, "Черновик")) == 1

    outcome = await cabinet_service.rebind_cabinet_on_merge("A-1", "B-2", user.id)
    assert outcome["token_rebound"] is True
    assert outcome["resent"] == "draft"

    comments_b = [c for c in _comments(deps.bitrix, "Черновик")
                  if c["params"]["fields"]["ENTITY_ID"] == "B-2"]
    assert len(comments_b) == 1, "комментарий черновика переотправлен на основную сделку"
    assert "credit_term: 36" in comments_b[0]["params"]["fields"]["COMMENT"]
    assert len(deps.conversions) == 1, "второй конверсии нет"

    # Повторная склейка — идемпотентна (нет дубля комментария)
    outcome2 = await cabinet_service.rebind_cabinet_on_merge("A-1", "B-2", user.id)
    assert outcome2["resent"] is None
    assert len([c for c in _comments(deps.bitrix, "Черновик")
                if c["params"]["fields"]["ENTITY_ID"] == "B-2"]) == 1


# =======================
# Edge: коллизия cabinet_form при склейке — побеждает submitted_at
# =======================

@pytest.mark.asyncio
async def test_merge_collision_submitted_row_wins_no_integrity_error(db, deps):
    user = await _mk_user(db)
    await _mk_token(db, user.id, deal_id="A-1", bound_at=datetime.now(timezone.utc))
    await _mk_form(db, user.id, deal_id="A-1", credit_initial=200000,
                   submitted_at=datetime.now(timezone.utc))
    await _mk_form(db, user.id, deal_id="B-2", credit_term=36)  # черновик основной

    outcome = await cabinet_service.rebind_cabinet_on_merge("A-1", "B-2", user.id)
    assert outcome["form"] == "merged_old_won"

    winner = await _get_form(db, user.id, "B-2")
    assert winner.submitted_at is not None and winner.credit_initial == 200000
    displaced = [f for f in await _all_forms(db, user.id)
                 if str(f.deal_id).startswith("displaced:")]
    assert len(displaced) == 1, "проигравшая строка помечена вытесненной, не удалена"
    assert displaced[0].credit_term == 36


@pytest.mark.asyncio
async def test_merge_collision_main_submitted_row_survives(db, deps):
    user = await _mk_user(db)
    await _mk_token(db, user.id, deal_id="A-1", bound_at=datetime.now(timezone.utc))
    await _mk_form(db, user.id, deal_id="A-1", credit_term=12)  # черновик временной
    await _mk_form(db, user.id, deal_id="B-2", credit_initial=300000,
                   submitted_at=datetime.now(timezone.utc))

    outcome = await cabinet_service.rebind_cabinet_on_merge("A-1", "B-2", user.id)
    assert outcome["form"] == "merged_new_won"
    winner = await _get_form(db, user.id, "B-2")
    assert winner.credit_initial == 300000 and winner.submitted_at is not None


# =======================
# Source-grep-пины wiring'а main.py (конвенция tests/test_payload.py)
# =======================

def test_source_pins_r34_filter_in_both_map_points():
    src = MAIN_PY.read_text(encoding="utf-8")
    assert src.count("cabinet_service.filter_cabinet_owned_fields(fields_to_update") == 2, \
        "фильтр R34 обязан вызываться в ОБЕИХ точках применения BITRIX_SMART_FIELDS_MAP"


def test_source_pins_token_issue_in_both_payload_points():
    src = MAIN_PY.read_text(encoding="utf-8")
    # Общий блок обоих payload-путей: prepare_cabinet_context + ключи контекста
    helper_src = src.split("async def _attach_cabinet_context", 1)[1] \
                    .split("\nasync def ", 1)[0]
    assert "prepare_cabinet_context(" in helper_src
    assert '"cabinet_url"' in helper_src and '"cabinet_status"' in helper_src

    chat_src = src.split("async def send_chat_message_to_n8n", 1)[1] \
                  .split("async def send_handoff_webhook", 1)[0]
    assert "_attach_cabinet_context(" in chat_src

    handoff_src = src.split("async def send_handoff_webhook", 1)[1] \
                     .split("\nasync def ", 1)[0]
    assert "_attach_cabinet_context(" in handoff_src


def test_source_pins_rebind_in_deal_hunt_completion():
    src = MAIN_PY.read_text(encoding="utf-8")
    hunt_src = src.split("async def background_deal_hunt", 1)[1] \
                  .split("\nasync def ", 1)[0]
    assert "cabinet_service.rebind_cabinet_on_merge(" in hunt_src, \
        "склейка кабинета вызывается из завершения deal hunt"
    assert "fresh_phone" in hunt_src, \
        "подтверждённый номер (свежий из БД) входит в перенос склейки"


def test_source_pins_phone_guard_installed_via_setup():
    svc = CABINET_SERVICE_PY.read_text(encoding="utf-8")
    assert "database.PHONE_WRITE_GUARD = chat_phone_guard" in svc, \
        "страж R8/AE6 устанавливается через setup(), не импортом main"
    assert "import main" not in svc


# =======================
# Перенос прод-3.1.2 (план 2026-09-01-002, U2): пины ханков main.py
# =======================

def test_312_smart_intercept_uses_unified_predicate():
    """Самодельный предикат перехвата заменён единым is_valid_phone."""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert "looks_like_phone = is_valid_phone(user_text)" in src
    assert "_len_ok" not in src and "_is_pure" not in src, \
        "самодельный блок _len_ok/_starts_ok/_is_pure перехвата должен быть удалён"
    assert "already_had_phone = is_valid_phone(db_user.phone)" in src
    # Замечание: блок starts_correctly/digits_only (интент 10_Contact_Phone,
    # ~2079) остаётся самодельным и В ПРОДЕ 3.1.2 — дифф его не трогал; его
    # правило уже совпадает с предикатом (11 цифр с 7/8 либо 10 с 9)


def test_312_prinyato_only_inside_successful_save():
    """«Принято!» отвечается ТОЛЬКО внутри ветки успешной записи номера:
    отказ save_user_phone (невалидность или страж кабинета R8/AE6) оставляет
    диалог без ложного подтверждения. Поведение самого отказа доказано
    tests/test_phone_validation.py; здесь — структура чат-пути (конвенция
    source-grep для main.py)."""
    src = MAIN_PY.read_text(encoding="utf-8")
    start = src.index("if looks_like_phone and not already_had_phone:")
    block = src[start:start + 800]
    assert "if await save_user_phone(" in block, "возврат записи обязан проверяться"
    assert block.index("if await save_user_phone(") < \
        block.index("Принято! В ближайшее время"), \
        "текст «Принято!» должен жить внутри ветки успешной записи"


def test_312_web_field_bare_username():
    """WEB контакта Bitrix — голый username, без префикса https://max.ru/."""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert "https://max.ru/" not in src
    assert src.count('"WEB": [{"VALUE": username if username else ""') == 2, \
        "обе точки сборки контакта несут новый формат WEB"


def test_312_send_alert_delivery_checked():
    src = MAIN_PY.read_text(encoding="utf-8")
    assert "_ok = await send_alert_to_max_group" in src
    assert "NOT delivered" in src, "недоставка карточки менеджеру обязана логироваться"


def test_312_no_selfmade_phone_checks_left():
    """Все самодельные проверки телефона заменены is_valid_phone."""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert "Скрыт клиентом" not in src
    assert 'u_phone = str(u.phone or "")' not in src
    assert "len(usr.phone) >= 10" not in src
    assert "len(re.sub(r'\\D', '', usr.phone)) >= 10" not in src
    assert "is_valid_phone" in src.split("from database import", 1)[1].split("\n", 1)[0], \
        "is_valid_phone обязан импортироваться из database"


def test_312_post_verify_hook_checks_save_result():
    """Защитный лог кабинета: возврат save_user_phone в _post_verify_hook
    проверяется — расхождение валидаторов не пройдёт молча."""
    src = (MAIN_PY.parent / "cabinet_service.py").read_text(encoding="utf-8")
    assert "saved = await database.save_user_phone" in src
    assert "OTP-подтверждённый номер НЕ записан" in src


# =======================
# План 2026-09-08-002: правдивые логи (U1-U3, U6) и устойчивость БД (U5)
# =======================

class TestTruthfulSendStatuses:
    """U2: отправки возвращают фактический статус доставки."""

    @pytest.mark.asyncio
    async def test_send_alert_to_max_group_returns_delivery_status(self, db, deps, monkeypatch):
        import main as main_mod
        monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)
        monkeypatch.setattr(main_mod, "SUPPORT_GROUP_ID", "g-support")

        async def ok_send(group_id, text):
            return {"ok": True}
        monkeypatch.setattr(main_mod.max_client, "send_alert_to_group", ok_send)
        assert await main_mod.send_alert_to_max_group(
            "no-such-user", {"start_code": "XX123"}) is True

        async def fail_send(group_id, text):
            return None
        monkeypatch.setattr(main_mod.max_client, "send_alert_to_group", fail_send)
        assert await main_mod.send_alert_to_max_group(
            "no-such-user", {"start_code": "XX123"}) is False

    @pytest.mark.asyncio
    async def test_send_smartbot_reply_reports_fatal_and_success(self, db, deps, monkeypatch):
        import main as main_mod
        monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)

        async def no_sleep(_):
            return None
        monkeypatch.setattr(main_mod.asyncio, "sleep", no_sleep)

        async def dead_send(user_id, text, keyboard=None, attachments=None):
            return None
        monkeypatch.setattr(main_mod.max_client, "send_message", dead_send)
        assert await main_mod.send_smartbot_reply("u2-smoke", {"text": "привет"}) is False

        async def ok_send(user_id, text, keyboard=None, attachments=None):
            return {"message_id": 1}
        monkeypatch.setattr(main_mod.max_client, "send_message", ok_send)
        assert await main_mod.send_smartbot_reply("u2-smoke", {"text": "привет"}) is True

    @pytest.mark.asyncio
    async def test_tech_alert_writes_paired_log(self, monkeypatch, caplog):
        """U1: TECH_ALERT-строка пишется ДО отправки в чат и с тем же текстом."""
        import logging

        import main as main_mod
        sent = []

        async def fake_group(group_id, text):
            sent.append(text)
            return {"ok": True}
        monkeypatch.setattr(main_mod.max_client, "send_alert_to_group", fake_group)
        monkeypatch.setattr(main_mod, "TECH_ADMIN_ID", "g-tech")
        monkeypatch.setenv("BOT_ENV", "prod")   # без префикса «[DEV]» дев-стенда
        with caplog.at_level(logging.ERROR):
            await main_mod._sms_tech_alert("🚨 тестовый алерт 627")
        paired = [r.getMessage() for r in caplog.records
                  if "TECH_ALERT" in r.getMessage()]
        assert paired and "тестовый алерт 627" in paired[0]
        assert sent == ["🚨 тестовый алерт 627"]


class TestDbResilience:
    """U5: pre-ping/recycle и ретрай сохранения контекста."""

    def test_engine_kwargs_pre_ping_only_for_non_sqlite(self):
        pg = database.engine_kwargs_for("postgresql+asyncpg://u:p@h/db")
        assert pg["pool_pre_ping"] is True and pg["pool_recycle"] == 1800
        lite = database.engine_kwargs_for("sqlite+aiosqlite:///data/x.db")
        assert "pool_pre_ping" not in lite and "pool_recycle" not in lite

    @pytest.mark.asyncio
    async def test_save_context_retries_on_connection_drop(self, db, monkeypatch):
        """Первый заход — обрыв соединения (ночное окно PG), второй — успех."""
        from sqlalchemy.exc import InterfaceError

        import main as main_mod
        real_factory = database.AsyncSessionLocal
        calls = {"n": 0}

        class FlakyFactory:
            def __init__(self):
                self._real = real_factory()

            async def __aenter__(self):
                calls["n"] += 1
                sess = await self._real.__aenter__()
                if calls["n"] == 1:
                    raise InterfaceError("stmt", {}, Exception("connection was closed"))
                return sess

            async def __aexit__(self, *exc):
                return await self._real.__aexit__(*exc)

        monkeypatch.setattr(main_mod, "AsyncSessionLocal", FlakyFactory)
        assert await main_mod.save_context_to_db("u5-любой", {"a": 1}) is True
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_save_context_gives_up_on_persistent_failure(self, db, monkeypatch):
        from sqlalchemy.exc import InterfaceError

        import main as main_mod

        class DeadFactory:
            async def __aenter__(self):
                raise InterfaceError("stmt", {}, Exception("connection was closed"))

            async def __aexit__(self, *exc):
                return False
        monkeypatch.setattr(main_mod, "AsyncSessionLocal", DeadFactory)
        assert await main_mod.save_context_to_db("u5-любой", {"a": 1}) is False


class TestTruthfulLogPins:
    """U6: source-пины принципа «лог результата — только по подтверждённому
    результату» (docs/solutions/logic-errors/)."""

    def test_send_alert_returns_result_pin(self):
        # U9: дилер-алерты идут через _send_dealer_group_alert (бот бренда);
        # инвариант тот же — результат отправки проверяется, не предполагается
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "_res = await client.send_alert_to_group(group_id=dealer_group, text=text)" in src
        assert "return _res is not None" in src, \
            "_send_dealer_group_alert обязан возвращать результат доставки (U2/U9)"
        assert "return await _send_dealer_group_alert(dealer_config, prefix, final_text" in src, \
            "send_alert_to_max_group обязан отдавать результат хелпера, а не True"

    def test_reminder_sent_log_after_confirmed_delivery_pin(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        i_delivered = src.index("delivered = await send_smartbot_reply(u.platform_user_id, ai_resp)")
        i_level = src.index("ctx['reminder_level'] = 1")
        i_sent = src.index("[Reminder 1 - AI] Sent to")
        assert i_delivered < i_level < i_sent, \
            "порядок ремайндера: отправка → фиксация уровня/истории → «Sent» (U3)"
        assert "if delivered:" in src

    def test_tech_alert_paired_log_pin(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        assert 'logger.error("TECH_ALERT %s", text)' in src, \
            "техалерт без парной строки в логе уже дважды уводил диагностику (U1)"

    def test_logic_b_saves_state_before_deal_close_pin(self):
        """U5: Логика Б фиксирует контекст с коммитом ДО закрытия сделки;
        при сбое сохранения — continue (закрытие переносится на следующий тик)."""
        src = MAIN_PY.read_text(encoding="utf-8")
        block = src[src.index('ЛОГИКА Б: "СМЕРТЬ" ДИАЛОГА'):
                    src.index("2. ОБРАБОТКА ОЧЕРЕДИ СИНХРОНИЗАЦИИ")]
        assert "закрытие перенесено на следующий тик" in block
        assert block.index("await session.commit()") < block.index("crm.deal.update"), \
            "стейт и контекст коммитятся раньше смены стадии (прецедент 5488361)"

    def test_max_api_error_logged_with_status_pin(self):
        src = (MAIN_PY.parent / "max_client.py").read_text(encoding="utf-8")
        assert src.count("MAX_API_ERROR") >= 4, \
            "не-200 и исключения MAX обязаны логировать статус и тело (U2)"
