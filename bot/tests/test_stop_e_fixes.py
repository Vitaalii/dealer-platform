"""Правки по итогам проверки E (решения владельца 2026-10-03):
1) госномер и пробег, которых нет в анкете, — из сессии (лендинг);
2) пробег — только клиента, ни из ответа оценки;
3) чистый трейд-ин — без кредитных полей;
4) после отправки анкеты — итог кабинета в «Кредит - …» сделки веб-входа;
5) секреты (адрес вебхука Битрикса, токены) маскируются во всех логах."""
import json
import logging
import pathlib
from datetime import datetime, timezone

import pytest

import cabinet_service
import log_masking
import main as main_mod
from database import CabinetForm, CabinetProfile, User

SB = {"plate": "UF_CRM_1770635310", "mileage": "UF_CRM_1770635257",
      "make": "UF_CRM_1770635063", "credit_initial": "UF_CRM_1770634607",
      "credit_term": "UF_CRM_1770634728", "credit_payment": "UF_CRM_1770634992"}
DEAL = {"payment": "UF_CRM_1706098533386", "term": "UF_CRM_1706098512256",
        "initial": "UF_CRM_65B1EC7CC365E", "price": "UF_CRM_1706668886227"}
FOUND_AUTORU = {"mark": "MAZDA", "model": "CX_5", "year": 2018, "mileage": 10200,
                "plate": "Х318ТЕ750", "steering_wheel": "LEFT"}
NOW = datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)


async def _user(db, *, scenario, form, found=None, web=True, suffix, snap_extra=None):
    async with db.AsyncSessionLocal() as session:
        user = User(platform_user_id=f"webcab:e-{suffix}", channel="web")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        snap = {"schema_version": 1, "item_id": "11", "price": 1880000, "combo_price": 1830000}
        if found is not None:
            snap["found_info"] = dict(found)
        snap.update(snap_extra or {})
        prof = dict(user_id=user.id, deal_id="D-9", scenario_type=scenario,
                    calc_snapshot=json.dumps(snap))
        if web:
            prof.update(lead_code=f"991111{suffix.upper()[:4]:A<4}", web_plate="А123ВС77",
                        web_mileage=50000, web_form=json.dumps({"fields": {}}))
        session.add(CabinetProfile(**prof))
        session.add(CabinetForm(user_id=user.id, deal_id="D-9", **form))
        await session.commit()
    return user


# ---------------- 1. госномер и пробег из сессии ----------------

@pytest.mark.asyncio
async def test_plate_and_mileage_from_session_when_not_in_form(db, deps):
    """Кадр 8b с подменной оценкой: в ответе нет ни номера, ни пробега."""
    user = await _user(db, scenario="tradein", form={"submitted_at": NOW},
                       found={"mark": "Kia", "model": "Rio", "year": 2019}, suffix="s1")
    uf = (await cabinet_service.push_form_to_bitrix(user.id, "D-9"))["uf"]
    assert uf[SB["plate"]] == "А123ВС77" and uf[SB["mileage"]] == "50000"
    assert uf[SB["make"]] == "Kia"


@pytest.mark.asyncio
async def test_plate_and_mileage_from_session_without_valuation(db, deps):
    """Сценарий d: анкета ушла до оценки — found_info ещё нет."""
    user = await _user(db, scenario="combo", form={"submitted_at": NOW}, suffix="s2")
    uf = (await cabinet_service.push_form_to_bitrix(user.id, "D-9"))["uf"]
    assert uf[SB["plate"]] == "А123ВС77" and uf[SB["mileage"]] == "50000"


@pytest.mark.asyncio
async def test_cabinet_edit_beats_session(db, deps):
    user = await _user(db, scenario="tradein", suffix="s3",
                       form={"submitted_at": NOW, "tradein_plate": "Е963ТА99",
                             "tradein_mileage": 61000})
    uf = (await cabinet_service.push_form_to_bitrix(user.id, "D-9"))["uf"]
    assert uf[SB["plate"]] == "Е963ТА99" and uf[SB["mileage"]] == 61000


# ---------------- 2. пробег — только клиента ----------------

@pytest.mark.asyncio
async def test_autoru_mileage_never_used(db, deps):
    """Сценарий c: Auto.ru вернул 10 200, клиент ввёл 50 000."""
    user = await _user(db, scenario="combo", form={"submitted_at": NOW},
                       found=FOUND_AUTORU, suffix="m1")
    uf = (await cabinet_service.push_form_to_bitrix(user.id, "D-9"))["uf"]
    assert uf[SB["mileage"]] == "50000"
    assert "10200" not in json.dumps(uf)


@pytest.mark.asyncio
async def test_no_client_mileage_no_key(db, deps):
    """Не веб-сессия, пробега в анкете нет — ответ оценки пробег не даёт."""
    user = await _user(db, scenario="tradein", form={"submitted_at": NOW},
                       found=FOUND_AUTORU, web=False, suffix="m2")
    uf = (await cabinet_service.push_form_to_bitrix(user.id, "D-9"))["uf"]
    assert SB["mileage"] not in uf
    assert uf[SB["plate"]] == "Х318ТЕ750"            # номер из оценки по-прежнему можно


def test_card_shows_client_mileage_only():
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "widget-custom" / "cabinet-app" / "app.js").read_text(encoding="utf-8")
    assert "function withClientMileage(found, values)" in src
    assert "renderOwnBlock(withClientMileage(found, values), tiValuation)" in src
    assert "found = withClientMileage(found, values);" in src
    body = src.split("function withClientMileage(found, values)", 1)[1].split("\n  }\n", 1)[0]
    assert "delete merged.mileage" in body and "prof.web_mileage" in body


# ---------------- 3. чистый трейд-ин без кредита ----------------

CREDIT_FORM = {"credit_initial": 194464, "credit_term": 84, "credit_payment": 33000}


@pytest.mark.asyncio
async def test_pure_tradein_writes_no_credit_fields(db, deps):
    user = await _user(db, scenario="tradein", suffix="t1",
                       form={"submitted_at": NOW, **CREDIT_FORM})
    uf = (await cabinet_service.push_form_to_bitrix(user.id, "D-9"))["uf"]
    for key in ("credit_initial", "credit_term", "credit_payment"):
        assert SB[key] not in uf
    assert not set(DEAL.values()) & set(uf)


@pytest.mark.asyncio
async def test_combo_keeps_credit_fields(db, deps):
    user = await _user(db, scenario="combo", suffix="t2",
                       form={"submitted_at": NOW, **CREDIT_FORM})
    uf = (await cabinet_service.push_form_to_bitrix(user.id, "D-9"))["uf"]
    assert uf[SB["credit_term"]] == 84 and uf[SB["credit_payment"]] == 33000


# ---------------- 4. итог кабинета в «Кредит - …» ----------------

@pytest.mark.asyncio
async def test_submitted_web_credit_writes_final_cabinet_values(db, deps):
    user = await _user(db, scenario="credit", suffix="c1",
                       form={"submitted_at": NOW, **CREDIT_FORM})
    uf = (await cabinet_service.push_form_to_bitrix(user.id, "D-9"))["uf"]
    assert uf[DEAL["payment"]] == "33000" and uf[DEAL["term"]] == "84"
    assert uf[DEAL["initial"]] == "194464" and uf[DEAL["price"]] == "1880000"


@pytest.mark.asyncio
async def test_combo_price_is_combo_price(db, deps):
    user = await _user(db, scenario="combo", suffix="c2",
                       form={"submitted_at": NOW, **CREDIT_FORM})
    uf = (await cabinet_service.push_form_to_bitrix(user.id, "D-9"))["uf"]
    assert uf[DEAL["price"]] == "1830000"


@pytest.mark.asyncio
async def test_not_submitted_or_chat_profile_no_final_values(db, deps):
    draft = await _user(db, scenario="credit", suffix="c3", form=dict(CREDIT_FORM))
    uf = (await cabinet_service.push_form_to_bitrix(draft.id, "D-9"))["uf"]
    assert not set(DEAL.values()) & set(uf)
    chat = await _user(db, scenario="credit", suffix="c4", web=False,
                       form={"submitted_at": NOW, **CREDIT_FORM})
    uf = (await cabinet_service.push_form_to_bitrix(chat.id, "D-9"))["uf"]
    assert not set(DEAL.values()) & set(uf)


# ---------------- 5. секреты в логах ----------------

HOOK = "https://b24-mxici0.bitrix24.ru/rest/1/FAKEtoken0000000/crm.contact.get"


def test_bitrix_webhook_masked_in_text():
    msg = f"Attempt 1/3 failed for crm.contact.get: Connection timeout to host {HOOK}"
    masked = log_masking.mask_sensitive(msg)
    assert "FAKEtoken0000000" not in masked and "/rest/1/•••/crm.contact.get" in masked
    assert "FAKEtoken0000000" not in log_masking.mask_secrets(msg)


@pytest.mark.parametrize("line", [
    "xc-token: abcdefghijklmnop", "X-Cabinet-Eval-Secret: abcdefghijklmnop",
    "x-authorization: autodc-bcb3de0792bf10d5", "token=abcdefghijklmnop",
])
def test_secret_headers_masked(line):
    assert "abcdefghijklmnop" not in log_masking.mask_secrets(line)
    assert "bcb3de0792bf10d5" not in log_masking.mask_secrets(line)


def test_mask_secrets_keeps_phone():
    """Журнал сессии (архив диалога) — телефон нужен по делу, маскируются только секреты."""
    assert "+79001234567" in log_masking.mask_secrets(f"tel +79001234567 {HOOK}")


def test_masking_filter_masks_exception_text():
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "failed: %s",
                               (f"Connection timeout to host {HOOK}",), None)
    log_masking.MaskingFilter().filter(record)
    assert "FAKEtoken0000000" not in record.getMessage()


def test_log_session_python_logs_masked():
    ctx = {}
    main_mod.log_session(ctx, f"❌ Фоновая отправка в Bitrix упала: {HOOK}", "ERROR")
    assert ctx["python_logs"] and "FAKEtoken0000000" not in ctx["python_logs"][-1]
