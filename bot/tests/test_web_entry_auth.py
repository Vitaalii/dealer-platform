"""Веб-вход (план 2026-09-30-001, U6; окно повтора — план 2026-10-11-001):
авторизация веб-сессии — только звонок, повторный звонок через 45 с, лимит
по номеру и по сессии, время следующего звонка и признак повтора,
восстановление экрана звонка (auth_init), алерт «телефон подтверждён»."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

import cabinet_service
from cabinet_service import ApiError
from database import CabinetOtp, CabinetToken, User

from conftest import CONSENT, PHONE
from sms_gateway import SendResult

OTHER_PHONE = "+79007654321"
DEALERS = {
    "01": {"dealer_name": "Тест Авто", "website": "https://dealer-one.ru/", "code": "01",
           "db_config": {"catalog_table_id": "cat01"}},
}
ROW = {"Id": 7, "Model id": "15", "Complectation id": "12", "Model": "ATLAS PRO",
       "Complectation": "Люкс", "Price old": 3026990}


@pytest.fixture
def dealers():
    return DEALERS


@pytest.fixture
def nocodb_tables():
    return {"cat01": [ROW]}


@pytest.fixture
def tilda_deal(monkeypatch):
    found = {}

    async def finder(code):
        return {"ID": found[code]} if code in found else None

    monkeypatch.setattr(cabinet_service, "_find_deal_by_code", finder)
    return found


async def _web_token(lead="011512ABCD", nonce="n" * 32):
    res = await cabinet_service.open_web_session(
        {"dealer_code": "01", "lead_code": lead, "nonce": nonce, "model_id": "15", "form": {},
         "complectation_id": "12", "scenario": "credit", "desired_payment": 30000})
    return res["cabinet_url"].split("#t=", 1)[1]


async def _chat_token(db):
    async with db.AsyncSessionLocal() as session:
        user = User(platform_user_id="max:1", channel="max", full_name="Иван")
        session.add(user)
        await session.commit()
        await session.refresh(user)
    url = await cabinet_service.issue_cabinet_token(user.id, "01", "D-1")
    return url.split("#t=", 1)[1]


async def _backdate(db, seconds):
    """Старит все коды: момент запроса = expires_at − OTP_TTL, плюс sent_at."""
    created = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    async with db.AsyncSessionLocal() as session:
        await session.execute(update(CabinetOtp).where(CabinetOtp.sent_at.is_not(None))
                              .values(sent_at=created))
        await session.execute(update(CabinetOtp)
                              .values(expires_at=created + cabinet_service.OTP_TTL))
        await session.commit()


@pytest.mark.asyncio
async def test_web_session_rejects_sms_before_any_send(db, deps, tilda_deal):
    token = await _web_token()
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(token, phone=PHONE, channel="sms", consent=CONSENT)
    assert e.value.status == 422 and e.value.error == "channel_unavailable"
    assert deps.gw.calls == []


@pytest.mark.asyncio
async def test_chat_session_keeps_sms(db, deps):
    token = await _chat_token(db)
    res = await cabinet_service.request_code(token, phone=PHONE, channel="sms", consent=CONSENT)
    assert res["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds,allowed", [(44, False), (45, True)])
async def test_second_call_after_45s(db, deps, tilda_deal, seconds, allowed):
    token = await _web_token()
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    await _backdate(db, seconds)
    if allowed:
        assert (await cabinet_service.request_code(token, phone=PHONE))["status"] == "ok"
    else:
        with pytest.raises(ApiError) as e:
            await cabinet_service.request_code(token, phone=PHONE)
        assert e.value.status == 429 and e.value.extra["limit_source"] == "phone"
        assert 1 <= e.value.extra["retry_after"] <= 2


@pytest.mark.asyncio
async def test_third_call_blocked_within_10_minutes_even_after_number_change(db, deps, tilda_deal):
    token = await _web_token()
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    await _backdate(db, 50)
    await cabinet_service.request_code(token, phone=PHONE)
    await _backdate(db, 330)   # оба звонка ещё в 10-минутном окне
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(token, phone=OTHER_PHONE)
    assert e.value.status == 429 and e.value.extra["limit_source"] == "link"
    await _backdate(db, 601)
    assert (await cabinet_service.request_code(token, phone=OTHER_PHONE))["status"] == "ok"


@pytest.mark.asyncio
async def test_chat_voice_window_unchanged(db, deps):
    token = await _chat_token(db)
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    await _backdate(db, 320)
    with pytest.raises(ApiError) as e:   # чат: 1 звонок на номер за 10 минут
        await cabinet_service.request_code(token, phone=PHONE)
    assert e.value.status == 429


@pytest.mark.asyncio
async def test_auth_init_restores_call_screen_for_web(db, deps, tilda_deal):
    token = await _web_token()
    assert (await cabinet_service.auth_init(token))["call"] is None
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    init = await cabinet_service.auth_init(token)
    assert init["state"] == "need_phone" and init["entry_source"] == "web"
    assert init["return_url"] == "https://dealer-one.ru/"
    call = init["call"]
    assert call["masked_phone"] == cabinet_service.mask_phone(PHONE)
    assert call["channel"] == "voice" and call["calls_left"] == 1
    assert call["recall"] is False
    next_at = datetime.fromisoformat(call["next_call_at"])
    left = (next_at - datetime.now(timezone.utc)).total_seconds()
    assert 40 < left <= 45


@pytest.mark.asyncio
async def test_auth_init_restores_call_screen_for_chat_client(db, deps):
    token = await _chat_token(db)
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    init = await cabinet_service.auth_init(token)
    assert init["entry_source"] == "chat" and "return_url" not in init
    assert init["call"]["masked_phone"] == cabinet_service.mask_phone(PHONE)
    assert "calls_left" not in init["call"]


@pytest.mark.asyncio
async def test_old_code_superseded_after_second_call(db, deps, tilda_deal):
    token = await _web_token()
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    first_code = deps.gw.calls[-1]["code"]
    await _backdate(db, 316)
    await cabinet_service.request_code(token, phone=PHONE)
    with pytest.raises(ApiError) as e:
        await cabinet_service.verify_code(token, first_code)
    assert e.value.error in ("code_superseded", "code_expired")


async def _verify(deps, token):
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    await cabinet_service.verify_code(token, deps.gw.calls[-1]["code"])


@pytest.mark.asyncio
async def test_web_alert_with_known_tilda_deal(db, deps, tilda_deal):
    tilda_deal["011512ABCD"] = "52673"
    await _verify(deps, await _web_token())
    texts = [t for _, t in deps.dealer_alerts if "телефон подтверждён" in t]
    assert texts == [f"✅ Личный кабинет: Клиент, сделка 52673 — телефон подтверждён: "
                     f"{cabinet_service.mask_phone(PHONE)}. Источник: личный кабинет с сайта."]


@pytest.mark.asyncio
async def test_web_alert_without_deal(db, deps, tilda_deal):
    await _verify(deps, await _web_token())
    texts = [t for _, t in deps.dealer_alerts if "телефон подтверждён" in t]
    assert texts == [f"✅ Личный кабинет: Клиент — телефон подтверждён: "
                     f"{cabinet_service.mask_phone(PHONE)}. Источник: личный кабинет с сайта."]


@pytest.mark.asyncio
async def test_chat_alert_unchanged_and_single(db, deps):
    token = await _chat_token(db)
    await _verify(deps, token)
    texts = [t for _, t in deps.dealer_alerts if "телефон подтверждён" in t]
    assert len(texts) == 1
    assert texts[0].startswith("✅ Личный кабинет: Иван, сделка D-1")
    assert texts[0].endswith("Источник: личный кабинет.")


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds,allowed", [(44, False), (45, True)])
async def test_second_call_boundary_on_aware_db(aware_db, deps, tilda_deal, seconds, allowed):
    """То же окно 45 с на timezone-aware времени (как PostgreSQL)."""
    token = await _web_token()
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    await _backdate(aware_db, seconds)
    if allowed:
        assert (await cabinet_service.request_code(token, phone=PHONE))["status"] == "ok"
    else:
        with pytest.raises(ApiError):
            await cabinet_service.request_code(token, phone=PHONE)


# ---------------- U8: ответ request-code веб-сессии ----------------

@pytest.mark.asyncio
async def test_web_request_code_returns_recall_window_and_calls_left(db, deps, tilda_deal):
    token = await _web_token()
    res = await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    assert res == {"status": "ok", "retry_after": 45, "calls_left": 1, "recall": False}
    await _backdate(db, 45)
    res = await cabinet_service.request_code(token, phone=PHONE)
    # В4: звонков в окне не осталось — retry_after до выхода первого из окна
    assert res["calls_left"] == 0 and res["recall"] is True
    assert 553 <= res["retry_after"] <= 555


@pytest.mark.asyncio
async def test_chat_request_code_keeps_30s_cooldown(db, deps):
    token = await _chat_token(db)
    res = await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    assert res == {"status": "ok", "retry_after": 30}


@pytest.mark.asyncio
async def test_web_cabinet_url_carries_web_marker_chat_does_not(db, deps, tilda_deal):
    body = {"dealer_code": "01", "lead_code": "011512ABCD", "nonce": "n" * 32, "form": {},
            "model_id": "15", "complectation_id": "12", "scenario": "credit",
            "desired_payment": 30000}
    web_url = (await cabinet_service.open_web_session(body))["cabinet_url"]
    assert "/cabinet_dev?src=web#t=" in web_url or "/cabinet?src=web#t=" in web_url
    # повтор с тем же nonce — тот же адрес с признаком
    assert (await cabinet_service.open_web_session(body))["cabinet_url"] == web_url
    async with db.AsyncSessionLocal() as session:
        user = User(platform_user_id="max:2", channel="max", full_name="Иван")
        session.add(user)
        await session.commit()
        await session.refresh(user)
    chat_url = await cabinet_service.issue_cabinet_token(user.id, "01", "D-2")
    assert "#t=" in chat_url and "src=web" not in chat_url


@pytest.mark.asyncio
async def test_web_call_state_survives_code_expiry_within_window(db, deps, tilda_deal):
    """8h-2б: после истечения кода (5:00) веб-сессия получает экран звонка с
    code_expired до конца окна звонков (10 мин); клиент Александра — нет."""
    token = await _web_token()
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    await _backdate(db, 330)
    init = await cabinet_service.auth_init(token)
    assert init["call"]["code_expired"] is True and init["call"]["calls_left"] == 1
    await _backdate(db, 601)
    assert (await cabinet_service.auth_init(token))["call"] is None


@pytest.mark.asyncio
async def test_chat_call_state_gone_after_code_expiry(db, deps):
    token = await _chat_token(db)
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    await _backdate(db, 330)
    assert (await cabinet_service.auth_init(token))["call"] is None


# ---------------- План 2026-10-11-001: окно 45 с, время следующего звонка (В4), признак повтора ----------------

def _left(iso):
    return (datetime.fromisoformat(iso) - datetime.now(timezone.utc)).total_seconds()


async def _shift(db, seconds):
    """Сдвигает все коды в прошлое на seconds, сохраняя интервалы между ними."""
    delta = timedelta(seconds=seconds)
    async with db.AsyncSessionLocal() as session:
        for otp in (await session.execute(select(CabinetOtp))).scalars():
            otp.expires_at = otp.expires_at - delta
            if otp.sent_at is not None:
                otp.sent_at = otp.sent_at - delta
        await session.commit()


@pytest.mark.asyncio
async def test_promised_time_matches_429_for_third_call(db, deps, tilda_deal):
    """Время из ответа на второй звонок = retry_after 429 на третий (В4)."""
    token = await _web_token()
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    await _backdate(db, 45)
    res = await cabinet_service.request_code(token, phone=PHONE)
    await _shift(db, 60)   # мимо кулдауна 30 с
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(token, phone=PHONE)
    assert e.value.status == 429 and e.value.extra["limit_source"] == "link"
    assert abs(e.value.extra["retry_after"] - (res["retry_after"] - 60)) <= 1


@pytest.mark.asyncio
async def test_third_call_allowed_exactly_at_promised_time(db, deps, tilda_deal):
    token = await _web_token()
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    await _backdate(db, 45)
    res = await cabinet_service.request_code(token, phone=PHONE)
    await _shift(db, res["retry_after"] - 2)   # за 2 с до обещанного
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(token, phone=PHONE)
    assert 1 <= e.value.extra["retry_after"] <= 3
    await _shift(db, 3)                        # обещанное время наступило
    assert (await cabinet_service.request_code(token, phone=PHONE))["status"] == "ok"


@pytest.mark.asyncio
async def test_retry_after_takes_later_of_window_and_45s(db, deps, tilda_deal):
    """Второй звонок в 9:30 от первого: в 10:00 окно освобождается, но до
    45 с от второго ещё 15 с — обещанное время берёт более позднее."""
    token = await _web_token()
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    await _backdate(db, 570)
    res = await cabinet_service.request_code(token, phone=PHONE)
    assert res["calls_left"] == 0 and 44 <= res["retry_after"] <= 45


@pytest.mark.asyncio
async def test_auth_init_after_recall_keeps_time_and_recall(db, deps, tilda_deal):
    """F5 после повтора: next_call_at есть и при calls_left = 0 (В4), recall сохраняется."""
    token = await _web_token()
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    await _backdate(db, 45)
    res = await cabinet_service.request_code(token, phone=PHONE)
    call = (await cabinet_service.auth_init(token))["call"]
    assert call["calls_left"] == 0 and call["recall"] is True
    assert abs(_left(call["next_call_at"]) - res["retry_after"]) <= 2


@pytest.mark.asyncio
async def test_new_session_same_phone_is_recall(db, deps, tilda_deal):
    """Новая вкладка на том же номере через минуту: звонок разрешён, но это
    повтор для провайдера — recall, calls_left = 0, время до выхода из окна."""
    first = await _web_token()
    await cabinet_service.request_code(first, phone=PHONE, consent=CONSENT)
    await _backdate(db, 60)
    second = await _web_token(lead="011512ABCE", nonce="m" * 32)
    res = await cabinet_service.request_code(second, phone=PHONE, consent=CONSENT)
    assert res["status"] == "ok" and res["recall"] is True and res["calls_left"] == 0
    assert 538 <= res["retry_after"] <= 540


@pytest.mark.asyncio
async def test_failed_voice_send_starts_45s_window(db, deps, tilda_deal):
    token = await _web_token()
    deps.gw.results.append(SendResult(ok=False, error_class="transport", retry_free=True))
    assert (await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT))["status"] == "retry_free"
    await _backdate(db, 30)
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(token, phone=PHONE)
    assert e.value.status == 429
    await _backdate(db, 45)
    assert (await cabinet_service.request_code(token, phone=PHONE))["status"] == "ok"


@pytest.mark.asyncio
async def test_old_code_after_recall_hint_without_attempt(db, deps, tilda_deal):
    """Решение 5: после повтора первый код — подсказка без траты попытки, второй — вход."""
    token = await _web_token()
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    first_code = deps.gw.calls[-1]["code"]
    await _backdate(db, 45)
    await cabinet_service.request_code(token, phone=PHONE)
    second_code = deps.gw.calls[-1]["code"]
    if second_code == first_code:
        pytest.skip("коды совпали случайно")
    with pytest.raises(ApiError) as e:
        await cabinet_service.verify_code(token, first_code)
    assert e.value.error == "code_superseded"
    async with db.AsyncSessionLocal() as session:
        tok = (await session.execute(select(CabinetToken))).scalars().first()
        assert (tok.failed_attempts or 0) == 0
    assert "access_token" in await cabinet_service.verify_code(token, second_code)


@pytest.mark.asyncio
async def test_chat_request_code_has_no_web_fields(db, deps):
    token = await _chat_token(db)
    res = await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    assert "recall" not in res and "calls_left" not in res
    assert "recall" not in (await cabinet_service.auth_init(token))["call"]


# --- Повтор после F5 (решение владельца 2026-10-12): фронту после перезагрузки
# доступна только маска, запрос приходит без номера — сервер берёт номер
# последнего запроса кода этой сессии; лимиты — по нему же.

@pytest.mark.asyncio
async def test_f5_recall_without_phone_after_45s_uses_same_number(db, deps, tilda_deal):
    token = await _web_token()
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    await _backdate(db, 45)
    res = await cabinet_service.request_code(token)   # после F5: без номера
    assert res["status"] == "ok" and res["recall"] is True and res["calls_left"] == 0
    assert [c["phone"] for c in deps.gw.calls] == [PHONE, PHONE]


@pytest.mark.asyncio
async def test_f5_recall_before_45s_rejected_with_same_time(db, deps, tilda_deal):
    token = await _web_token()
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    await _backdate(db, 40)
    with pytest.raises(ApiError) as with_phone:
        await cabinet_service.request_code(token, phone=PHONE)
    with pytest.raises(ApiError) as without_phone:
        await cabinet_service.request_code(token)
    assert without_phone.value.status == 429 and without_phone.value.error == "rate_limited"
    assert abs(without_phone.value.extra["retry_after"] - with_phone.value.extra["retry_after"]) <= 1
    assert len(deps.gw.calls) == 1


@pytest.mark.asyncio
async def test_f5_after_second_call_recall_allowed_at_promised_time(db, deps, tilda_deal):
    """Шаг 6 живой проверки: повтор → F5 → в ЧЧ:ММ «Позвонить ещё раз» без номера."""
    token = await _web_token()
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    await _backdate(db, 45)
    await cabinet_service.request_code(token, phone=PHONE)
    call = (await cabinet_service.auth_init(token))["call"]       # F5
    wait = round(_left(call["next_call_at"]))
    await _shift(db, wait - 2)
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(token)
    assert e.value.status == 429 and 1 <= e.value.extra["retry_after"] <= 3
    await _shift(db, 3)
    res = await cabinet_service.request_code(token)
    assert res["status"] == "ok" and deps.gw.calls[-1]["phone"] == PHONE
    code = deps.gw.calls[-1]["code"]
    assert "access_token" in await cabinet_service.verify_code(token, code)


@pytest.mark.asyncio
async def test_web_request_without_phone_and_without_prior_request_is_422(db, deps, tilda_deal):
    token = await _web_token()
    for phone in (None, ""):
        with pytest.raises(ApiError) as e:
            await cabinet_service.request_code(token, phone=phone, consent=CONSENT)
        assert e.value.status == 422 and e.value.error == "invalid_phone"
    assert deps.gw.calls == []


@pytest.mark.asyncio
async def test_f5_recall_uses_last_requested_number_after_change(db, deps, tilda_deal):
    """«Изменить номер» → звонок на другой номер → F5 → повтор идёт на последний."""
    token = await _web_token()
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    await _backdate(db, 45)
    await cabinet_service.request_code(token, phone=OTHER_PHONE)
    await _shift(db, 700)                                        # окно 10 мин прошло
    assert (await cabinet_service.request_code(token))["status"] == "ok"
    assert deps.gw.calls[-1]["phone"] == OTHER_PHONE


@pytest.mark.asyncio
async def test_chat_request_without_phone_still_422(db, deps):
    """Путь чата не затронут: несвязанная чат-сессия без номера — прежний 422."""
    token = await _chat_token(db)
    await cabinet_service.request_code(token, phone=PHONE, channel="sms", consent=CONSENT)
    async with db.AsyncSessionLocal() as session:
        tok = (await session.execute(select(CabinetToken))).scalar_one()
        bound = tok.bound_phone
    if bound:
        pytest.skip("чат-сессия привязывается к номеру при первом запросе")
    await _shift(db, 700)
    with pytest.raises(ApiError) as e:
        await cabinet_service.request_code(token, channel="sms")
    assert e.value.status == 422 and e.value.error == "invalid_phone"


@pytest.mark.asyncio
async def test_web_api_responses_never_carry_full_phone(db, deps, tilda_deal):
    token = await _web_token()
    digits = PHONE.lstrip("+")
    seen = [await cabinet_service.auth_init(token),
            await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT),
            await cabinet_service.auth_init(token),
            await cabinet_service.send_status(token)]
    await _backdate(db, 45)
    seen += [await cabinet_service.request_code(token),
             await cabinet_service.auth_init(token),
             await cabinet_service.send_status(token)]
    for payload in seen:
        dump = json.dumps(payload, ensure_ascii=False, default=str)
        assert digits not in dump and digits[1:] not in dump, dump
    assert "masked_phone" in seen[-2]["call"]
