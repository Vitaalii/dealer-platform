"""План 2026-09-02-001: гейт поля «Канал коммуникации (квалиф)» (F_CHANNEL).

Правило владельца (ОВ-1): критерий один — валидный телефон. Есть телефон →
«По телефону»; нет — ключ в CRM не уходит, значение Тильды (web/max/telegram)
в сделке остаётся нетронутым. Правило единое для чата и кабинетной конверсии:
оба пути идут через final_sync_wrapper, телефон — из строки User.

Защита двухуровневая: пересчёт channel_val от предиката телефона
(final_sync_wrapper, background_deal_hunt) + гейт в билдерах
(BitrixService._channel_field) — унаследованное «В MAX» из персистентных
context_json не уходит в CRM ни по одному пути.
"""
import json
import pathlib
import uuid

import pytest

import database
from database import CabinetProfile, CabinetToken, User

import cabinet_service
import main as main_mod

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAIN_PY = ROOT / "main.py"

VALID_PHONE = "+79001234567"


# =======================
# Хелперы
# =======================

def _bitrix_capture(monkeypatch):
    """Подмена BitrixService._request с захватом вызовов; F_CHANNEL пинится
    на тестовый код — env машины не влияет на ассерты (паттерн
    test_cabinet_handoff._bitrix_capture)."""
    captured = []

    async def fake_request(method, params, user_id=None, **kw):
        captured.append({"method": method, "params": params})
        if method == "crm.contact.list":
            return {"result": []}
        if method == "crm.contact.add":
            return {"result": 777}
        if method == "crm.deal.add":
            return {"result": 555}
        if method == "crm.deal.get":
            return {"result": {"ID": "D-1", "CONTACT_ID": None,
                               "STAGE_ID": "C9:NEW"}}
        return {"result": True}

    monkeypatch.setattr(main_mod.BitrixService, "_request", fake_request)
    monkeypatch.setattr(main_mod.BitrixService, "F_CHANNEL", "UF_CHANNEL_TEST")
    return captured


def _fields(captured, method):
    """Поля последнего вызова данного метода CRM (кроме отдельной смены
    стадии — она идёт последней записью, исправление 2026-09-30)."""
    calls = [c for c in captured if c["method"] == method
             and set(c["params"].get("fields") or {}) != {"STAGE_ID"}]
    assert calls, f"вызов {method} не случился"
    return calls[-1]["params"]["fields"]


async def _mk_sync_user(db, monkeypatch, phone=None, context_json=None,
                        deal_id="D-1", suffix="1"):
    """Пользователь для final_sync_wrapper + допатч имени AsyncSessionLocal
    в main (фикстура db патчит только database.*, main держит своё имя
    с импорта)."""
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)

    async def fake_alert(uid, ctx):
        return True
    monkeypatch.setattr(main_mod, "send_alert_to_max_group", fake_alert)

    async with db.AsyncSessionLocal() as session:
        user = User(platform_user_id=f"gate-{suffix}", channel="max",
                    full_name="Тест", phone=phone, active_deal_id=deal_id,
                    context_json=json.dumps(context_json or {},
                                            ensure_ascii=False))
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


# =======================
# U2: гейт в билдерах — унаследованные значения не уходят в CRM
# =======================

@pytest.mark.asyncio
async def test_update_deal_stale_v_max_not_sent(monkeypatch):
    """Контекст с унаследованным «В MAX» → crm.deal.update без ключа канала
    (значение Тильды в сделке не затирается)."""
    captured = _bitrix_capture(monkeypatch)
    monkeypatch.setattr(main_mod.BitrixService, "F_PAYMENT", "UF_PAY_TEST")
    await main_mod.BitrixService.update_deal_data(
        "D-9", {"channel_val": "В MAX", "payment": 100})
    fields = _fields(captured, "crm.deal.update")
    assert "UF_CHANNEL_TEST" not in fields
    assert fields["UF_PAY_TEST"] == 100


@pytest.mark.asyncio
async def test_update_deal_phone_value_sent(monkeypatch):
    captured = _bitrix_capture(monkeypatch)
    await main_mod.BitrixService.update_deal_data(
        "D-9", {"channel_val": "По телефону"})
    assert _fields(captured, "crm.deal.update")["UF_CHANNEL_TEST"] == "По телефону"


@pytest.mark.asyncio
async def test_update_deal_no_key_sends_nothing(monkeypatch):
    """Пин прежнего поведения: без channel_val ключ не отправляется."""
    captured = _bitrix_capture(monkeypatch)
    await main_mod.BitrixService.update_deal_data("D-9", {"payment": 100})
    assert "UF_CHANNEL_TEST" not in _fields(captured, "crm.deal.update")


@pytest.mark.asyncio
async def test_create_deal_stale_v_max_not_sent(monkeypatch):
    """То же для create_deal_with_contact."""
    captured = _bitrix_capture(monkeypatch)
    await main_mod.BitrixService.create_deal_with_contact(
        "Тест", VALID_PHONE, "", {"channel_val": "В MAX"}, "01TEST01")
    assert "UF_CHANNEL_TEST" not in _fields(captured, "crm.deal.add")


@pytest.mark.asyncio
async def test_create_deal_phone_value_sent(monkeypatch):
    captured = _bitrix_capture(monkeypatch)
    await main_mod.BitrixService.create_deal_with_contact(
        "Тест", VALID_PHONE, "", {"channel_val": "По телефону"}, "01TEST01")
    assert _fields(captured, "crm.deal.add")["UF_CHANNEL_TEST"] == "По телефону"


# =======================
# Единое правило в final_sync_wrapper (чат: ветки A и B)
# =======================

@pytest.mark.asyncio
async def test_final_sync_valid_phone_gives_po_telefonu(db, monkeypatch):
    """Финализация с валидным телефоном → «По телефону» в crm.deal.update."""
    captured = _bitrix_capture(monkeypatch)
    user = await _mk_sync_user(db, monkeypatch, phone=VALID_PHONE)
    await main_mod.BitrixService.final_sync_wrapper(user.platform_user_id, {})
    assert _fields(captured, "crm.deal.update")["UF_CHANNEL_TEST"] == "По телефону"


@pytest.mark.asyncio
async def test_final_sync_no_phone_stale_v_max_dropped(db, monkeypatch):
    """Без телефона ключ не уходит даже при унаследованном «В MAX» в
    контексте — покрывает таймаут-хендофф SmartBot, форсинг Manager и интент
    мессенджера: после снятия присваиваний все три ветки финализируются без
    значения, а этот тест гарантирует то же и для старых персистентных
    контекстов."""
    captured = _bitrix_capture(monkeypatch)
    user = await _mk_sync_user(db, monkeypatch, phone=None)
    await main_mod.BitrixService.final_sync_wrapper(
        user.platform_user_id, {"channel_val": "В MAX"})
    updates = [c for c in captured if c["method"] == "crm.deal.update"]
    assert updates and all("UF_CHANNEL_TEST" not in c["params"]["fields"] for c in updates)


# =======================
# Кабинетная конверсия — то же правило, тот же механизм (ОВ-1)
# =======================

async def _mk_cabinet_user(db, monkeypatch, phone, context_json=None, suffix="c1"):
    user = await _mk_sync_user(db, monkeypatch, phone=phone,
                               context_json=context_json, suffix=suffix)
    async with db.AsyncSessionLocal() as session:
        session.add(CabinetProfile(user_id=user.id, scenario_type="credit"))
        session.add(CabinetToken(
            user_id=user.id, dealer_code="01", deal_id="D-1",
            token_hash=uuid.uuid4().hex + uuid.uuid4().hex[:32],
            status="active", bound_phone=phone or VALID_PHONE))
        await session.commit()
    return user


@pytest.mark.asyncio
async def test_cabinet_conversion_otp_phone_gives_po_telefonu(db, monkeypatch):
    """Конверсия с OTP-подтверждённым телефоном (User.phone) → «По телефону»."""
    captured = _bitrix_capture(monkeypatch)
    user = await _mk_cabinet_user(db, monkeypatch, phone=VALID_PHONE)
    await main_mod._cabinet_run_conversion(user.id)
    assert _fields(captured, "crm.deal.update")["UF_CHANNEL_TEST"] == "По телефону"


@pytest.mark.asyncio
async def test_cabinet_conversion_without_phone_sends_no_key(db, monkeypatch):
    """Без подтверждённого телефона конверсия ключа не даёт — даже если
    в context_json лежит унаследованное «В MAX»."""
    captured = _bitrix_capture(monkeypatch)
    user = await _mk_cabinet_user(db, monkeypatch, phone=None,
                                  context_json={"channel_val": "В MAX"},
                                  suffix="c2")
    await main_mod._cabinet_run_conversion(user.id)
    assert "UF_CHANNEL_TEST" not in _fields(captured, "crm.deal.update")


@pytest.mark.asyncio
async def test_cabinet_conversion_keeps_chat_po_telefonu(db, monkeypatch):
    """Чатовое «По телефону» кабинетом не перетирается: значение то же."""
    captured = _bitrix_capture(monkeypatch)
    user = await _mk_cabinet_user(db, monkeypatch, phone=VALID_PHONE,
                                  context_json={"channel_val": "По телефону"},
                                  suffix="c3")
    await main_mod._cabinet_run_conversion(user.id)
    assert _fields(captured, "crm.deal.update")["UF_CHANNEL_TEST"] == "По телефону"


# =======================
# U1: пины источника — «В MAX» из логики удалён
# =======================

def test_no_v_max_assignment_left_in_source():
    """Ни одна ветка (таймаут-хендофф, интент 11, Manager-форсинг) больше не
    присваивает «В MAX»; финализация этих веток живёт (is_final_step), но
    значения для Битрикса не даёт."""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert '= "В MAX"' not in src, "присваивание «В MAX» вернулось в main.py"


def test_po_telefonu_written_only_at_phone_predicate_sites():
    """Три точки записи «По телефону»: финализация чата (has_valid_phone),
    final_sync_wrapper (u_phone) и background_deal_hunt (fresh_phone) — все
    за предикатом телефона. Появление четвёртой — сигнал ревизии правила."""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert src.count("channel_val'] = \"По телефону\"") == 3
