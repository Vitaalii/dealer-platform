"""План 2026-09-08-001: SB-поля трейд-ина на всех путях завершения.

Замер стенда ДО правок (сделки 51509/51511/51517 дев-портала): SB-семья и
оценка autoru пусты на путях сабмита и таймаута кабинета; при живом found_info
фронт поля не рендерит (read-only), None из формы не уходит. Две точки записи
(KTD-12): хелпер из слотов защёлки в final_sync (точка 1) и снапшот-фолбэк в
push_form_to_bitrix (точка 2, форма главнее — П3)."""
import json

import pytest
from sqlalchemy import select

import database
from database import CabinetForm, CabinetProfile, User

import cabinet_service
import payload_v2
import main as main_mod
from main import build_ai_tradein_fields

UF = {
    "make": "UF_CRM_1770635063", "model": "UF_CRM_1770635116",
    "year": "UF_CRM_1770635208", "mileage": "UF_CRM_1770635257",
    "plate": "UF_CRM_1770635310", "valuation": "UF_CRM_1788758195",
}
FOUND = {"mark": "Kia", "model": "Sorento", "year": 2014,
         "plate": "Р491НК37", "mileage": 46600, "steering_wheel": "LEFT"}
CACHE = {"found_info": dict(FOUND), "ti_valuation": 1680000}


def _latched_ctx():
    ctx = {"scenario_type": "tradein", "chat_history": [
        {"role": "user", "text": "оцените"}], "n8n_cache": dict(CACHE)}
    payload_v2.register_evaluation(ctx)
    return ctx


# =======================
# U1: слот evaluation_car в защёлке
# =======================

class TestEvaluationCarSlot:
    def test_slot_set_with_latch(self):
        car = _latched_ctx()["evaluation_car"]
        # 2026-10-03: пробег из ответа оценки в слот не берётся
        assert car == {"make": "Kia", "model": "Sorento", "year": 2014,
                       "plate": "Р491НК37"}

    def test_slot_survives_cache_rebuild(self):
        ctx = _latched_ctx()
        ctx["n8n_cache"] = {"complectations": []}
        payload_v2.register_evaluation(ctx)
        assert ctx["evaluation_car"]["plate"] == "Р491НК37"

    def test_partial_found_info_keeps_only_present_keys(self):
        ctx = {"n8n_cache": {"ti_valuation": 500000,
                             "found_info": {"plate": "А123БВ77", "mark": "Lada"}}}
        payload_v2.register_evaluation(ctx)
        assert ctx["evaluation_car"] == {"make": "Lada", "plate": "А123БВ77"}

    def test_unlatch_clears_car_slot(self, monkeypatch):
        monkeypatch.setenv("STAGE_EVAL_RULE", "1")
        ctx = _latched_ctx()
        payload_v2.build_session_context(ctx, "другая машина х777хх77", "base_v01")
        assert "evaluation_car" not in ctx


# =======================
# U2: хелпер полей из слотов
# =======================

class TestBuildAiTradeinFields:
    def test_full_slots(self):
        fields = build_ai_tradein_fields(_latched_ctx())
        assert fields == {UF["make"]: "Kia", UF["model"]: "Sorento",
                          UF["year"]: "2014",
                          UF["plate"]: "Р491НК37", UF["valuation"]: "1680000"}

    def test_legacy_slot_mileage_not_written(self):
        """Старый контекст, где слот уже несёт пробег Auto.ru, — в SB не уходит."""
        ctx = {"evaluation_car": {"make": "MAZDA", "mileage": 10200, "plate": "Х318ТЕ750"}}
        fields = build_ai_tradein_fields(ctx)
        assert UF["mileage"] not in fields and fields[UF["make"]] == "MAZDA"

    def test_empty_slots_no_keys(self):
        assert build_ai_tradein_fields({}) == {}
        assert build_ai_tradein_fields(None) == {}

    def test_valuation_only(self):
        assert build_ai_tradein_fields({"evaluation_value": 500000}) == {
            UF["valuation"]: "500000"}


# =======================
# U2 интеграция: final_sync пишет поля из слотов (точка 1)
# =======================

def _bitrix_capture(monkeypatch):
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
    return captured


async def _mk_sync_user(db, monkeypatch, context_json, suffix="1"):
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)

    async def fake_alert(uid, ctx):
        return True
    monkeypatch.setattr(main_mod, "send_alert_to_max_group", fake_alert)
    async with db.AsyncSessionLocal() as session:
        user = User(platform_user_id=f"ai-tf-{suffix}", channel="max",
                    full_name="Тест", phone=None, active_deal_id="D-1",
                    context_json=json.dumps(context_json, ensure_ascii=False))
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


def _updates(captured):
    return [c["params"] for c in captured if c["method"] == "crm.deal.update"]


@pytest.mark.asyncio
async def test_final_sync_writes_slots_fields(db, deps, monkeypatch):
    """Конверсия (таймаут кабинета/воронковый финал) со слотами → SB-семья
    и оценка в crm.deal.update; воронковая семья не затронута."""
    captured = _bitrix_capture(monkeypatch)
    ctx = _latched_ctx()
    user = await _mk_sync_user(db, monkeypatch, ctx, "slots")
    await main_mod.BitrixService.final_sync_wrapper(user.platform_user_id, dict(ctx))
    ai_updates = [u for u in _updates(captured) if UF["plate"] in u.get("fields", {})]
    assert ai_updates, "запись AI-полей из слотов не ушла"
    fields = ai_updates[-1]["fields"]
    assert fields[UF["make"]] == "Kia" and fields[UF["valuation"]] == "1680000"
    assert "UF_CRM_1706098554832" not in fields, "воронковая семья не наша (Ф5)"


@pytest.mark.asyncio
async def test_final_sync_empty_slots_no_ai_update(db, deps, monkeypatch):
    """Хендоф до оценки: слотов нет → AI-записи нет вовсе."""
    captured = _bitrix_capture(monkeypatch)
    ctx = {"scenario_type": "credit", "chat_history": []}
    user = await _mk_sync_user(db, monkeypatch, ctx, "empty")
    await main_mod.BitrixService.final_sync_wrapper(user.platform_user_id, dict(ctx))
    assert not [u for u in _updates(captured) if UF["plate"] in u.get("fields", {})]


@pytest.mark.asyncio
async def test_final_sync_r34_after_submit_drops_owned_keeps_valuation(db, deps, monkeypatch):
    """R34/KTD-15: после сабмита анкеты SB-семьёй владеет кабинет — хелпер
    дропает её коды; поле оценки не в карте кабинета и остаётся."""
    captured = _bitrix_capture(monkeypatch)
    ctx = _latched_ctx()
    user = await _mk_sync_user(db, monkeypatch, ctx, "r34")
    async with db.AsyncSessionLocal() as session:
        from datetime import datetime, timezone
        session.add(CabinetForm(user_id=user.id, deal_id="D-1",
                                submitted_at=datetime.now(timezone.utc)))
        await session.commit()
    await main_mod.BitrixService.final_sync_wrapper(user.platform_user_id, dict(ctx))
    ai_updates = [u for u in _updates(captured)
                  if UF["valuation"] in u.get("fields", {})]
    assert ai_updates, "оценка обязана дойти и после сабмита"
    fields = ai_updates[-1]["fields"]
    assert UF["make"] not in fields and UF["plate"] not in fields, \
        "SB-семья после сабмита принадлежит кабинету (R34)"


# =======================
# U7: снапшот-фолбэк в push_form_to_bitrix (точка 2) + П3 + KTD-17
# =======================

async def _mk_cabinet_user(db, form_kwargs, with_snapshot=True, suffix="c1"):
    async with db.AsyncSessionLocal() as session:
        user = User(platform_user_id=f"ai-cab-{suffix}", channel="web")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        snap = json.dumps({"schema_version": 1, "found_info": dict(FOUND),
                           "ti_valuation": 1680000, "item_id": "11"}) \
            if with_snapshot else None
        session.add(CabinetProfile(user_id=user.id, deal_id="D-7",
                                   scenario_type="tradein", calc_snapshot=snap))
        session.add(CabinetForm(user_id=user.id, deal_id="D-7", **form_kwargs))
        await session.commit()
    return user


@pytest.mark.asyncio
async def test_push_form_snapshot_fallback_fills_empty_tradein(db, deps, caplog):
    """Поля формы None + found_info в снапшоте → SB-семья уходит из снапшота;
    KTD-17: состав виден строкой CABINET_UF_PUSH."""
    import logging
    user = await _mk_cabinet_user(db, {}, suffix="fb")
    with caplog.at_level(logging.INFO, logger="cabinet"):
        outcome = await cabinet_service.push_form_to_bitrix(user.id, "D-7")
    uf = outcome["uf"]
    assert uf[UF["make"]] == "Kia" and uf[UF["plate"]] == "Р491НК37"
    assert uf[UF["year"]] == "2014"
    # 2026-10-03: пробег из ответа оценки не берётся — только клиента
    assert UF["mileage"] not in uf
    push_logs = [r.getMessage() for r in caplog.records
                 if "CABINET_UF_PUSH" in r.getMessage()]
    assert push_logs and "snapshot_fill" in push_logs[-1] \
        and UF["make"] in push_logs[-1]


@pytest.mark.asyncio
async def test_push_form_client_value_beats_snapshot(db, deps):
    """П3 (обязательный): клиент исправил пробег руками → уходит его значение,
    не снапшотное."""
    user = await _mk_cabinet_user(db, {"tradein_mileage": 50000}, suffix="p3")
    outcome = await cabinet_service.push_form_to_bitrix(user.id, "D-7")
    assert outcome["uf"][UF["mileage"]] == 50000
    assert outcome["uf"][UF["make"]] == "Kia", "остальное добрано из снапшота"


@pytest.mark.asyncio
async def test_push_form_both_sources_empty_no_key(db, deps):
    """П3 (обязательный): пусто у обоих источников → ключ не появляется
    вовсе (не пустая строка)."""
    user = await _mk_cabinet_user(db, {}, with_snapshot=False, suffix="mt")
    outcome = await cabinet_service.push_form_to_bitrix(user.id, "D-7")
    for code in (UF["make"], UF["model"], UF["year"], UF["mileage"], UF["plate"]):
        assert code not in outcome["uf"]


# =======================
# U5: канонизация воронкового слота номера
# =======================

class TestNormalizePlateInput:
    @pytest.mark.parametrize("raw,canon", [
        ("T582XP 33 rus", "Т582ХР33"),        # кейс А: латиница, пробел, хвост
        ("t582xp 33rus", "Т582ХР33"),
        ("х318те750", "Х318ТЕ750"),            # тест владельца: нижний регистр
        ("А123ВЕ77", "А123ВЕ77"),
        ("Р491НК37 RUS", "Р491НК37"),
        ("а 123 ве 777", "А123ВЕ777"),         # 3-значный регион с ведущей 7
    ])
    def test_canonized(self, raw, canon):
        assert payload_v2.normalize_plate_input(raw) == canon

    @pytest.mark.parametrize("raw", [
        "К647МУ211",          # кейс Б-класс: 211 регионом быть не может
        "А123ВЕ00",           # регион 00 не существует
        "просто текст", "", None,
        "12345",
    ])
    def test_not_canonized_returns_none(self, raw):
        assert payload_v2.normalize_plate_input(raw) is None

    def test_handoff_wires_canonization(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent / "main.py") \
            .read_text(encoding="utf-8")
        assert "payload_v2.normalize_plate_input(user_context.get('plate'))" in src, \
            "хендоф обязан канонизировать воронковый слот (U5/KTD-16)"
