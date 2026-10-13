"""Веб-вход (план 2026-09-30-001, блок C: U10/U11/U12): конверсия веб-сессии,
сделка бота в «Обогащение», правило имени и контакта, алерт, закрытие
сессии, оценка в сделке."""
import json
import pathlib
from datetime import timedelta

import pytest
from sqlalchemy import select, update

import cabinet_service
import database
import main as main_mod
from database import (CabinetEvent, CabinetForm, CabinetProfile, CabinetToken,
                      CabinetValuation, User)

from conftest import CONSENT, PHONE

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"
DEALERS = {
    "01": {"dealer_name": "Тест Авто", "website": "https://dealer-one.ru/", "code": "01",
           "db_config": {"catalog_table_id": "cat01"}},
}
ROW = {"Id": 7, "Model id": "15", "Complectation id": "12", "Model": "ATLAS PRO",
       "Complectation": "Люкс", "Model Modification": "1.5T AT 2WD",
       "Price old": 3026990, "Price new": 2466990, "Cash in hook": 80000,
       "Credit bonus": 200000, "Trade-in bonus": 250000, "ID": "011512",
       "Link": "/price?filters=abc"}


@pytest.fixture
def dealers():
    return DEALERS


@pytest.fixture
def nocodb_tables():
    return {"cat01": [ROW]}


def _capture(monkeypatch, *, deal_found=None, contact=None, deal_add_id=555,
             contact_add_id=777):
    """Скриптуемый BitrixService._request: фиксирует method/params/kwargs."""
    calls = []

    async def fake_request(method, params, user_id=None, **kw):
        calls.append({"method": method, "params": params, "kw": kw})
        if method == "crm.deal.list":
            return {"result": [deal_found] if deal_found else []}
        if method == "crm.deal.get":
            return {"result": deal_found or {}}
        if method == "crm.contact.list":
            return {"result": [{"ID": "C-1"}] if contact is not None else []}
        if method == "crm.contact.get":
            return {"result": contact or {}}
        if method == "crm.contact.add":
            return {"result": contact_add_id}
        if method == "crm.deal.add":
            return {"result": deal_add_id}
        return {"result": True}

    monkeypatch.setattr(main_mod.BitrixService, "_request", fake_request)
    monkeypatch.setattr(main_mod.BitrixService, "STAGE_QUALIFIED", "C9:UC_0SOJJA")
    monkeypatch.setattr(main_mod.BitrixService, "F_CODE", "UF_CRM_1731438093")
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)
    return calls


def _by(calls, method):
    return [c for c in calls if c["method"] == method]


# ---------------- update_contact: R35 / R54 (все пути) ----------------

@pytest.mark.asyncio
async def test_client_placeholder_does_not_overwrite_real_name(monkeypatch):
    calls = _capture(monkeypatch, contact={"NAME": "Иван Петров",
                                            "PHONE": [{"VALUE": "+7 (900) 123-45-67"}]})
    await main_mod.BitrixService.update_contact("C-1", "Клиент", PHONE, "")
    fields = _by(calls, "crm.contact.update")[0]["params"]["fields"]
    assert "NAME" not in fields and "LAST_NAME" not in fields     # Covers AE9


@pytest.mark.asyncio
async def test_contact_with_other_phone_is_not_touched(monkeypatch):
    calls = _capture(monkeypatch, contact={"NAME": "Иван", "PHONE": [{"VALUE": "+79110000000"}]})
    await main_mod.BitrixService.update_contact("C-1", "Клиент", PHONE, "")
    assert _by(calls, "crm.contact.update") == []                  # R54


@pytest.mark.asyncio
async def test_chat_real_name_still_written(monkeypatch):
    calls = _capture(monkeypatch, contact={"NAME": "Старое", "PHONE": []})
    await main_mod.BitrixService.update_contact("C-1", "Иван", PHONE, "ivan")
    fields = _by(calls, "crm.contact.update")[0]["params"]["fields"]
    assert fields["NAME"] == "Иван" and fields["PHONE"][0]["VALUE"] == PHONE


@pytest.mark.asyncio
async def test_unreadable_contact_keeps_name_placeholder_out(monkeypatch):
    calls = _capture(monkeypatch, contact=None)   # crm.contact.get → пусто
    await main_mod.BitrixService.update_contact("C-1", "Клиент", PHONE, "")
    assert "NAME" not in _by(calls, "crm.contact.update")[0]["params"]["fields"]


# ---------------- Сделка бота (R34, R61) ----------------

def _web_ctx(mark="TEST"):
    return {"payment": "30000", "scenario_type": "combo", "plate": "А123ВС77",
            "cabinet_uf_type": "Анкета комбо", "channel_val": "По телефону",
            "site_source": "https://dealer-one.ru",
            cabinet_service.WEB_CONVERSION_KEY: {
                "dealer_code": "01", "lead_type": "ЛК с сайта", "test_mark": mark,
                "car_fields": cabinet_service.web_deal_car_fields(ROW, None)}}


@pytest.mark.asyncio
async def test_web_deal_in_funnel_9_enrichment_with_car_fields(monkeypatch):
    calls = _capture(monkeypatch)
    deal_id = await main_mod.BitrixService.create_deal_with_contact(
        "Клиент", PHONE, "", _web_ctx(), "011512ABCD")
    assert deal_id == "555"
    fields = _by(calls, "crm.deal.add")[0]["params"]["fields"]
    assert fields["CATEGORY_ID"] == 9 and fields["STAGE_ID"] == "C9:NEW"      # R34: создаётся в «Новая»
    assert fields["UF_CRM_65D5BD2A777E5"] == "ЛК с сайта"
    assert fields["UF_CRM_1731438093"] == "011512ABCD" and fields["UF_CRM_I2CRM_CHAT"] == "011512ABCD"
    assert fields["UF_CRM_65B0A4605FA7C"] == "ATLAS PRO"
    assert fields["UF_CRM_65B0A4606E9CA"] == "1.5T AT 2WD"
    assert fields["UF_CRM_65B0A46077BCD"] == "Люкс"
    assert fields["UF_CRM_67E4E4691A18E"] == "3026990"
    assert fields["UF_CRM_1745996090"] == "3026990|RUB"
    assert fields["UF_CRM_67E4E4692881A"] == "2466990"
    assert fields["UF_CRM_1708662321"] == "/price?filters=abc"
    assert fields["UF_CRM_66068DB231026"] == "011512"
    assert "Заявка из MAX" not in fields["TITLE"]
    title = _by(calls, "crm.deal.update")[-1]
    # Covers AE11; стадию «Обогащение» ставит кабинет ПОСЛЕ полей анкеты
    assert title["params"]["fields"] == {"TITLE": "TEST Сделка #555"}
    assert all(c["kw"].get("enqueue_on_failure") is False for c in calls
               if c["method"] in ("crm.deal.add", "crm.contact.add"))
    # заголовок — с постановкой в pending_sync: сбой не порождает вторую сделку
    assert title["kw"].get("enqueue_on_failure", True) is True


@pytest.mark.asyncio
async def test_web_deal_title_without_mark_on_prod(monkeypatch):
    calls = _capture(monkeypatch)
    await main_mod.BitrixService.create_deal_with_contact(
        "Клиент", PHONE, "", _web_ctx(mark=""), "011512ABCD")
    assert _by(calls, "crm.deal.update")[-1]["params"]["fields"]["TITLE"] == "Сделка #555"


@pytest.mark.asyncio
async def test_empty_deal_add_raises_and_no_none_title(monkeypatch):
    calls = _capture(monkeypatch, deal_add_id=None)
    with pytest.raises(RuntimeError):
        await main_mod.BitrixService.create_deal_with_contact(
            "Клиент", PHONE, "", _web_ctx(), "011512ABCD")
    assert _by(calls, "crm.deal.update") == []                      # нет «Сделка #None»


def test_link_fallback_used_only_when_catalog_link_empty():
    fields = cabinet_service.web_deal_car_fields(dict(ROW, Link=""), "/price?x=1")
    assert fields["UF_CRM_1708662321"] == "/price?x=1"
    fields = cabinet_service.web_deal_car_fields(dict(ROW, Link=""), None)
    assert "UF_CRM_1708662321" not in fields


# ---------------- Полный путь веб-сессии через final_sync_wrapper ----------------

async def _web_session(db, deps, scenario="credit", plate=None, lead="011512ABCD",
                       form=None, ip=None):
    body = {"dealer_code": "01", "lead_code": lead, "nonce": "n" * 32, "model_id": "15",
            "form": form or {},
            "complectation_id": "12", "scenario": scenario, "desired_payment": 30000}
    if plate:
        body.update(plate=plate, mileage=50000)
    url = (await cabinet_service.open_web_session(body, ip=ip))["cabinet_url"]
    token = url.split("#t=", 1)[1]
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    res = await cabinet_service.verify_code(token, deps.gw.calls[-1]["code"])
    async with db.AsyncSessionLocal() as session:
        tok = (await session.execute(select(CabinetToken))).scalars().first()
    return tok.user_id, f"Bearer {res['access_token']}"


@pytest.fixture
def real_conversion(monkeypatch):
    """Настоящая конверсия main (final_sync_wrapper) вместо фейка deps; охота —
    с флагом вызова."""
    hunts = []
    monkeypatch.setattr(cabinet_service, "_run_conversion", main_mod._cabinet_run_conversion)
    monkeypatch.setattr(cabinet_service, "_handoff_stage", "C9:UC_0SOJJA")

    async def via_main(method, params, user_id=None):
        # Кабинетные вызовы — в тот же перехватчик, что и у main (_capture):
        # порядок записей в сделку виден одним списком
        return await main_mod.BitrixService._request(method, params, user_id=user_id,
                                                     enqueue_on_failure=False)

    monkeypatch.setattr(cabinet_service, "_bitrix_request", via_main)

    async def no_hunt(*a, **k):
        hunts.append(a)

    monkeypatch.setattr(main_mod, "background_deal_hunt", no_hunt)
    alerts = []

    async def fake_card(uid, ctx):
        alerts.append(ctx)
        return True

    monkeypatch.setattr(main_mod, "send_alert_to_max_group", fake_card)
    monkeypatch.setenv("CABINET_DEAL_TEST_MARK", "TEST")
    return hunts, alerts


@pytest.mark.asyncio
async def test_conversion_found_tilda_deal_goes_to_enrichment(db, deps, real_conversion, monkeypatch):
    hunts, alerts = real_conversion
    calls = _capture(monkeypatch, deal_found={"ID": "52673", "CONTACT_ID": None, "STAGE_ID": "C9:NEW"})
    user_id, bearer = await _web_session(db, deps)
    await cabinet_service.submit_form(bearer, {"marital_status": "Холост/не замужем"})
    upd = [c for c in _by(calls, "crm.deal.update") if c["params"]["id"] == "52673"]
    assert "STAGE_ID" not in upd[0]["params"]["fields"]               # поля — без стадии
    assert upd[-1]["params"]["fields"] == {"STAGE_ID": "C9:UC_0SOJJA"}  # Covers AE12 — последней
    assert all("UF_CRM_65D5BD2A777E5" not in c["params"]["fields"] for c in upd)  # «Тип лида 1» не трогаем
    assert _by(calls, "crm.deal.add") == [] and hunts == []
    comments = [c["params"]["fields"]["COMMENT"] for c in _by(calls, "crm.timeline.comment.add")]
    assert any(c.startswith("TEST:") for c in comments)
    assert alerts and alerts[0][cabinet_service.WEB_CONVERSION_KEY]["dealer_code"] == "01"


@pytest.mark.asyncio
async def test_conversion_without_tilda_deal_creates_bot_deal_no_hunt(db, deps, real_conversion, monkeypatch):
    hunts, _ = real_conversion
    calls = _capture(monkeypatch)
    user_id, bearer = await _web_session(db, deps)
    await cabinet_service.submit_form(bearer, {"marital_status": "Холост/не замужем"})
    add = _by(calls, "crm.deal.add")
    assert len(add) == 1 and add[0]["params"]["fields"]["CATEGORY_ID"] == 9      # Covers AE8
    assert hunts == []
    async with db.AsyncSessionLocal() as session:
        token = (await session.execute(select(CabinetToken))).scalars().first()
    assert token.deal_id == "555"


@pytest.mark.asyncio
async def test_bitrix_down_on_deal_add_retries_next_tick(db, deps, real_conversion, monkeypatch):
    calls = _capture(monkeypatch, deal_add_id=None)
    user_id, bearer = await _web_session(db, deps)
    await cabinet_service.submit_form(bearer, {"marital_status": "Холост/не замужем"})
    async with db.AsyncSessionLocal() as session:
        done = (await session.execute(select(CabinetEvent).where(
            CabinetEvent.event_type == "handoff_done"))).scalars().all()
        user = await session.get(User, user_id)
    assert done == [] and not user.active_deal_id          # передача не засчитана
    assert deps.enqueues == []                              # в pending_sync ничего
    # Битрикс ожил — подбор зависших захватов доводит передачу
    calls2 = _capture(monkeypatch)
    async with db.AsyncSessionLocal() as session:
        await session.execute(update(CabinetToken).values(
            handoff_at=cabinet_service._utcnow() - timedelta(hours=1)))
        await session.commit()
    await cabinet_service.otp_worker_tick()
    assert len(_by(calls2, "crm.deal.add")) == 1


def test_cabinet_conversion_never_starts_hunt():
    """Охота запускается только из чатовой передачи (регресс Александра)."""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert src.count("background_deal_hunt(") == 3     # def + две точки чата
    start = src.index("async def final_sync_wrapper")
    end = src.index("async def close_as_duplicate")
    assert "background_deal_hunt" not in src[start:end]


# ---------------- Закрытие веб-сессии и алерт (R33, R34) ----------------

@pytest.mark.asyncio
async def test_close_web_session_no_farewell_no_archive(db, deps, monkeypatch):
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)
    user_id, _ = await _web_session(db, deps)
    called = []

    async def spy(*a, **k):
        called.append(a)

    monkeypatch.setattr(main_mod, "finalize_transcript_archive", spy)
    monkeypatch.setattr(main_mod, "get_sender", lambda *a, **k: called.append(a) or (None, None))
    async with db.AsyncSessionLocal() as session:
        token = (await session.execute(select(CabinetToken))).scalars().first()
    await main_mod.close_cabinet_session(user_id, "submit", "555", token.id)
    assert called == []
    async with db.AsyncSessionLocal() as session:
        ev = (await session.execute(select(CabinetEvent).where(
            CabinetEvent.event_type == "session_closed"))).scalars().all()
    assert len(ev) == 1


@pytest.mark.asyncio
async def test_web_card_alert_text_and_dealer(db, monkeypatch):
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)
    monkeypatch.setattr(main_mod, "CACHED_DEALERS", {"01": {"code": "01", "comm_type": "Chat"}})
    sent = []

    async def fake_group(cfg, code, text, deal_id=None):
        sent.append((code, text))
        return True

    monkeypatch.setattr(main_mod, "_send_dealer_group_alert", fake_group)
    ctx = dict(_web_ctx(), start_code="", model="ATLAS PRO")
    await main_mod.send_alert_to_max_group("webcab:x", ctx)
    code, text = sent[0]
    assert code == "01" and "личный кабинет с сайта" in text and "MAX" not in text


# ---------------- Оценка в сделке (U12: R57, R58, R67) ----------------

async def _handed_off(db, deps, monkeypatch, *, valuation, submitted_first=True):
    """Веб-сессия трейд-ина, переданная в сделку 555, с оценкой в заданном состоянии."""
    cabinet_service.set_valuation_client(None)
    _capture(monkeypatch)
    monkeypatch.setattr(cabinet_service, "_run_conversion", main_mod._cabinet_run_conversion)

    async def no_hunt(*a, **k):
        return None

    async def no_card(*a, **k):
        return True

    monkeypatch.setattr(main_mod, "background_deal_hunt", no_hunt)
    monkeypatch.setattr(main_mod, "send_alert_to_max_group", no_card)

    async def pending_forever(*a):
        import asyncio
        await asyncio.Event().wait()

    cabinet_service.set_valuation_client(pending_forever)
    user_id, bearer = await _web_session(db, deps, scenario="combo", plate="А123ВС77")
    await cabinet_service.submit_form(bearer, {"marital_status": "Холост/не замужем"})
    return user_id


@pytest.mark.asyncio
async def test_late_success_written_once_with_combo_note(db, deps, monkeypatch):
    user_id = await _handed_off(db, deps, monkeypatch, valuation="late")
    deps.bitrix.calls.clear()
    import valuation_client
    async with db.AsyncSessionLocal() as session:
        vid = (await session.execute(select(CabinetValuation.id))).scalar_one()
    await cabinet_service._finish_valuation(vid, valuation_client.ValuationResult(
        "success", None, False, {"mark": "Kia", "model": "Rio", "year": 2019,
                                 "steering_wheel": "LEFT"}, 900000, 1000000))
    await cabinet_service._finish_valuation(vid, valuation_client.ValuationResult(
        "success", None, False, None, 1, 2))            # повтор — не пишет
    upd = [c for c in deps.bitrix.calls if c["method"] == "crm.deal.update"
           and "UF_CRM_1785857524" in c["params"]["fields"]]
    assert len(upd) == 1
    f = upd[0]["params"]["fields"]
    assert f["UF_CRM_1785857524"] == "1000000" and f["UF_CRM_1785857507"] == "Левый"
    assert f["UF_CRM_1770635063"] == "Kia" and f["UF_CRM_1770635208"] == "2019"
    comments = [c["params"]["fields"]["COMMENT"] for c in deps.bitrix.calls
                if c["method"] == "crm.timeline.comment.add"]
    assert comments == ["Оценка трейд-ина получена после передачи заявки: 1000000 ₽. "
                        "Расчёт в анкете — без зачёта."]            # Covers AE13


@pytest.mark.asyncio
async def test_failure_writes_r58_comment(db, deps, monkeypatch):
    await _handed_off(db, deps, monkeypatch, valuation="late")
    deps.bitrix.calls.clear()
    import valuation_client
    async with db.AsyncSessionLocal() as session:
        vid = (await session.execute(select(CabinetValuation.id))).scalar_one()
    await cabinet_service._finish_valuation(vid, valuation_client.ValuationResult(
        "not_found", "автомобиль не найден по госномеру"))
    comments = [c["params"]["fields"]["COMMENT"] for c in deps.bitrix.calls
                if c["method"] == "crm.timeline.comment.add"]
    assert comments == ["Оценка не получена: автомобиль не найден по госномеру"]


# ---------------- Блок form лендинга в сделке бота (R61–R62) ----------------

MAPPING = json.loads((MAIN_PY.parent / "cabinet_web_deal_map.json").read_text(encoding="utf-8"))
LANDING_FORM = {
    "Model": "ЧУЖАЯ МОДЕЛЬ", "Price_ol": "1", "Link": "https://evil.example/x",
    "offer": "ЛК с сайта", "form_type": "Комбо", "sku-wa": "999999XXXX",
    "MONTHLY_PAYMENT": "99999", "CAR_NUMBER": "Х000ХХ00",
    "BRAND": "HAVAL", "DEALER": "Тест", "FORMID": "form2472574461",
    "WEBSITE": "https://demo.autodc.org", "LETTER_TITLE": "Тестовая заявка",
    "messenger": "Telegram", "color-select": "Белый",
    "utm_source": "yandex_rsya", "utm_medium": "cpc", "utm_campaign": "3312_rsy_vse",
    "utm_content": "17718375261", "utm_term": "---autotargeting",
    "_ct_session_id": "463702346", "_ym_uid": "1713946040997310148",
    "WhiteCallback_visit": "39532017878;", "unknown_field": "x",
}


def _stored(form, ip="203.0.113.7"):
    return json.dumps({"fields": form, "client_ip": ip})


def test_form_block_mapped_by_integration_table():
    block, bot = cabinet_service.web_deal_form_fields(MAPPING, _stored(LANDING_FORM), {})
    assert block["UF_CRM_66068DB229299"] == "form2472574461"          # Форма
    assert block["UF_CRM_66068DB216D55"] == "HAVAL" and block["UF_CRM_66068DB220F43"] == "Тест"
    assert block["UF_CRM_681317C7B3CB1"] == "https://demo.autodc.org"
    assert block["UF_CRM_1759808650"] == "Тестовая заявка"
    assert block["UF_CRM_65B0A5E1003B2"] == "Telegram" and block["UF_CRM_65B0A46085731"] == "Белый"
    assert {k: block[k] for k in ("UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN", "UTM_CONTENT",
                                  "UTM_TERM")} == {
        "UTM_SOURCE": "yandex_rsya", "UTM_MEDIUM": "cpc", "UTM_CAMPAIGN": "3312_rsy_vse",
        "UTM_CONTENT": "17718375261", "UTM_TERM": "---autotargeting"}
    assert bot == {"UF_CRM_1764493358": "203.0.113.7"}               # IP клиента — не из блока


def test_cookies_like_integration():
    block, _ = cabinet_service.web_deal_form_fields(MAPPING, _stored(LANDING_FORM), {})
    assert block["UF_CRM_65D8414464AF0"] == "463702346;"              # CT_sessionId
    assert block["UF_CRM_1765187737"] == "463702346"                  # CT_sessionId_num — целое
    assert block["UF_CRM_65D841446D593"] == "1713946040997310148;"    # Ya_id
    assert block["UF_CRM_1764591234"] == "39532017878;"               # «;» не удваивается
    block, _ = cabinet_service.web_deal_form_fields(
        MAPPING, _stored({"_ct_session_id": "abc"}), {})
    assert block == {"UF_CRM_65D8414464AF0": "abc;"}                  # нечисловой — без _num


def test_bot_owned_block_values_ignored_and_unknown_keys_dropped():
    block, _ = cabinet_service.web_deal_form_fields(MAPPING, _stored(LANDING_FORM), {})
    for code in ("UF_CRM_65B0A4605FA7C", "UF_CRM_67E4E4691A18E", "UF_CRM_1708662321",
                 "UF_CRM_65D5BD2A777E5", "UF_CRM_666D69C9EEE1B", "UF_CRM_I2CRM_CHAT",
                 "UF_CRM_1706098533386", "UF_CRM_1706098554832"):
        assert code not in block
    assert "x" not in block.values() and "UF_CRM_1788758195" not in block


def test_bot_values_by_tilda_names_and_no_mapping():
    _, bot = cabinet_service.web_deal_form_fields(MAPPING, _stored({}, ip=None), {
        "CREDIT_DURATION": 84, "DOWN_PAYMENT_RUB": 600000, "CREDIT_SUMM": 2750000,
        "MILEAGE": 50000})
    assert bot == {"UF_CRM_1706098512256": "84", "UF_CRM_65B1EC7CC365E": "600000",
                   "UF_CRM_1706668886227": "2750000", "UF_CRM_65B0A4607EC41": "50000"}
    assert cabinet_service.web_deal_form_fields(None, _stored(LANDING_FORM), {"MILEAGE": 1}) == ({}, {})
    assert cabinet_service.web_deal_form_fields(MAPPING, None, {}) == ({}, {})


@pytest.mark.asyncio
async def test_bot_deal_order_block_over_soft_owned_over_block(monkeypatch):
    calls = _capture(monkeypatch)
    ctx = _web_ctx()
    ctx[cabinet_service.WEB_CONVERSION_KEY]["form_fields"] = {
        "UF_CRM_65B0A5E1003B2": "Telegram", "UF_CRM_681317C7B3CB1": "https://demo.autodc.org",
        "UF_CRM_66068DB229299": "form2472574461", "UF_CRM_65B0A4605FA7C": "ЧУЖАЯ",
        "STAGE_ID": "C9:WON"}
    ctx[cabinet_service.WEB_CONVERSION_KEY]["bot_fields"] = {"UF_CRM_1764493358": "203.0.113.7"}
    await main_mod.BitrixService.create_deal_with_contact("Клиент", PHONE, "", ctx, "011512ABCD")
    fields = _by(calls, "crm.deal.add")[0]["params"]["fields"]
    assert fields["UF_CRM_65B0A5E1003B2"] == "Telegram"               # форма уточняет канал
    assert fields["UF_CRM_681317C7B3CB1"] == "https://demo.autodc.org"
    assert fields["UF_CRM_66068DB229299"] == "form2472574461"
    assert fields["UF_CRM_65B0A4605FA7C"] == "ATLAS PRO"              # машина — из каталога
    assert fields["UF_CRM_1764493358"] == "203.0.113.7"
    assert fields["STAGE_ID"] == "C9:NEW" and fields["CATEGORY_ID"] == 9


@pytest.mark.asyncio
async def test_conversion_bot_deal_filled_from_form_block(db, deps, real_conversion, monkeypatch):
    calls = _capture(monkeypatch)
    user_id, bearer = await _web_session(db, deps, scenario="combo", plate="А123ВС77",
                                         form=LANDING_FORM, ip="203.0.113.7")
    await cabinet_service.submit_form(bearer, {"marital_status": "Холост/не замужем",
                                               "credit_term": 84, "credit_initial": 600000})
    fields = _by(calls, "crm.deal.add")[0]["params"]["fields"]
    assert fields["UF_CRM_66068DB229299"] == "form2472574461"
    assert fields["UTM_SOURCE"] == "yandex_rsya" and fields["UF_CRM_65D8414464AF0"] == "463702346;"
    assert fields["UF_CRM_1764493358"] == "203.0.113.7"
    assert fields["UF_CRM_65B0A4605FA7C"] == "ATLAS PRO"
    assert fields["UF_CRM_1708662321"] == "/price?filters=abc"        # каталог, не evil из блока
    assert fields["UF_CRM_I2CRM_CHAT"] == "011512ABCD"
    assert fields["UF_CRM_1706098533386"] == "30000"                  # платёж кабинета
    assert fields["UF_CRM_1706098554832"] == "А123ВС77"
    assert fields["UF_CRM_1706098512256"] == "84" and fields["UF_CRM_65B1EC7CC365E"] == "600000"
    assert fields["UF_CRM_1706668886227"] == "2500000"                # комбо-цена
    assert fields["UF_CRM_65B0A4607EC41"] == "50000"
    assert "UF_CRM_1788758195" not in fields                          # оценка autoru — не пишется
    assert fields["STAGE_ID"] == "C9:NEW"
    assert _by(calls, "crm.deal.update")[-1]["params"]["fields"] == {"STAGE_ID": "C9:UC_0SOJJA"}


@pytest.mark.asyncio
async def test_found_tilda_deal_gets_no_form_block(db, deps, real_conversion, monkeypatch):
    calls = _capture(monkeypatch, deal_found={"ID": "52673", "CONTACT_ID": None, "STAGE_ID": "C9:NEW"})
    user_id, bearer = await _web_session(db, deps, form=LANDING_FORM, ip="203.0.113.7")
    await cabinet_service.submit_form(bearer, {"marital_status": "Холост/не замужем"})
    written = {k for c in _by(calls, "crm.deal.update") for k in c["params"]["fields"]}
    assert not written & {"UF_CRM_66068DB229299", "UTM_SOURCE", "UF_CRM_65D8414464AF0",
                          "UF_CRM_1764493358"}
    assert _by(calls, "crm.deal.add") == []


@pytest.mark.asyncio
async def test_broken_map_bot_deal_without_block_and_alert(db, deps, real_conversion, monkeypatch):
    calls = _capture(monkeypatch)
    monkeypatch.setattr(cabinet_service, "WEB_DEAL_MAP_FILE", "/nonexistent/map.json")
    monkeypatch.setitem(cabinet_service._web_deal_map_alerted, "done", False)
    user_id, bearer = await _web_session(db, deps, form=LANDING_FORM)
    await cabinet_service.submit_form(bearer, {"marital_status": "Холост/не замужем"})
    fields = _by(calls, "crm.deal.add")[0]["params"]["fields"]
    assert "UF_CRM_66068DB229299" not in fields and fields["UF_CRM_65B0A4605FA7C"] == "ATLAS PRO"
    assert any("cabinet_web_deal_map.json" in a for a in deps.tech_alerts)


# ---------------- 2026-10-03: госномер и пробег в найденную сделку Tilda ----------------

@pytest.mark.asyncio
async def test_found_tilda_deal_gets_plate_and_mileage(db, deps, real_conversion, monkeypatch):
    calls = _capture(monkeypatch, deal_found={"ID": "52673", "CONTACT_ID": None, "STAGE_ID": "C9:NEW"})
    user_id, bearer = await _web_session(db, deps, scenario="tradein", plate="А123ВС77")
    await cabinet_service.submit_form(bearer, {"marital_status": "Холост/не замужем"})
    fields = {}
    for c in _by(calls, "crm.deal.update"):
        if c["params"]["id"] == "52673":
            fields.update(c["params"]["fields"])
    assert fields["UF_CRM_1706098554832"] == "А123ВС77"       # «Trade-in - номер»
    assert fields["UF_CRM_65B0A4607EC41"] == "50000"          # «Trade-in - пробег»


@pytest.mark.asyncio
async def test_cabinet_plate_and_mileage_over_landing(db, deps, real_conversion, monkeypatch):
    calls = _capture(monkeypatch, deal_found={"ID": "52673", "CONTACT_ID": None, "STAGE_ID": "C9:NEW"})
    user_id, bearer = await _web_session(db, deps, scenario="combo", plate="А123ВС77")
    await cabinet_service.submit_form(bearer, {"marital_status": "Холост/не замужем",
                                               "tradein_plate": "Е963ТА99",
                                               "tradein_mileage": 61000})
    fields = {}
    for c in _by(calls, "crm.deal.update"):
        if c["params"]["id"] == "52673":
            fields.update(c["params"]["fields"])
    assert fields["UF_CRM_1706098554832"] == "Е963ТА99"
    assert fields["UF_CRM_65B0A4607EC41"] == "61000"


@pytest.mark.asyncio
async def test_bot_deal_mileage_from_cabinet_over_landing(db, deps, real_conversion, monkeypatch):
    calls = _capture(monkeypatch)
    user_id, bearer = await _web_session(db, deps, scenario="tradein", plate="А123ВС77")
    await cabinet_service.submit_form(bearer, {"marital_status": "Холост/не замужем",
                                               "tradein_mileage": 61000})
    fields = _by(calls, "crm.deal.add")[0]["params"]["fields"]
    assert fields["UF_CRM_65B0A4607EC41"] == "61000" and fields["UF_CRM_1706098554832"] == "А123ВС77"


@pytest.mark.asyncio
async def test_credit_web_deal_no_tradein_fields(db, deps, real_conversion, monkeypatch):
    calls = _capture(monkeypatch, deal_found={"ID": "52673", "CONTACT_ID": None, "STAGE_ID": "C9:NEW"})
    user_id, bearer = await _web_session(db, deps)
    await cabinet_service.submit_form(bearer, {"marital_status": "Холост/не замужем"})
    written = {k for c in _by(calls, "crm.deal.update") for k in c["params"]["fields"]}
    assert not written & {"UF_CRM_1706098554832", "UF_CRM_65B0A4607EC41"}


def test_tilda_lowercase_car_number_and_mileage_resolve_case_insensitive():
    """Форма Tilda называет поля car_number и mileage (строчными): таблица
    сопоставляет их без учёта регистра с «Trade-in - номер» / «Trade-in - пробег».
    Обе переменные — bot_owned: значение блока не берётся, в поле пишет бот."""
    fields = MAPPING["fields"]
    assert cabinet_service._mapped(fields, "car_number") == "UF_CRM_1706098554832"
    assert cabinet_service._mapped(fields, "mileage") == "UF_CRM_65B0A4607EC41"
    block, bot = cabinet_service.web_deal_form_fields(
        MAPPING, _stored({"car_number": "Х999ХХ99", "mileage": "1"}), {"MILEAGE": 50000})
    assert "UF_CRM_1706098554832" not in block and "UF_CRM_65B0A4607EC41" not in block
    assert bot["UF_CRM_65B0A4607EC41"] == "50000"


@pytest.mark.asyncio
async def test_bot_deal_lowercase_tilda_tradein_names_get_bot_values(db, deps, real_conversion, monkeypatch):
    calls = _capture(monkeypatch)
    form = dict(LANDING_FORM, car_number="Х999ХХ99", mileage="1")
    user_id, bearer = await _web_session(db, deps, scenario="combo", plate="А123ВС77", form=form)
    await cabinet_service.submit_form(bearer, {"marital_status": "Холост/не замужем"})
    fields = _by(calls, "crm.deal.add")[0]["params"]["fields"]
    assert fields["UF_CRM_1706098554832"] == "А123ВС77"     # госномер — из сессии, не из блока
    assert fields["UF_CRM_65B0A4607EC41"] == "50000"        # пробег — из сессии, не из блока
