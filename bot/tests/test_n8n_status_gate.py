"""План 2026-09-02-002: статусы n8n — Р1 логирование подстановки, Р2 working,
Р3/Р4 телефонные барьеры (стадия ОЖИДАНИЕ, подавление карточки дилеру).

Принцип конструкции: барьер переопределяет ТОЛЬКО стадию и алерт — смарт-поля,
«Тип обращения», стенограмма и архив пишутся полностью (решение В2); реестр
дедупа при Р3 получает сырой статус (В4); эвристика next_step — под тем же
телефонным гейтом, что и подстановка (обязательное требование владельца).
"""
import asyncio
import json

import pytest

import database
from database import User

import crm_mapping
from crm_mapping import WAITING_STAGE, callback_stage, resolve_callback_status
import main as main_mod

VALID_PHONE = "+79001234567"
ENRICH_STAGE = "C9:UC_0SOJJA"   # «Обогащение» — передано продавцу
UF_TYPE = "UF_CRM_666D69C9EEE1B"
UF_SB_BRAND = "UF_CRM_1770632572"


# =======================
# Чистые функции (Р1-Р3 без обвязки aiohttp)
# =======================

def test_recognized_statuses_pass_untouched():
    """Белый список — ключи карты стадий, включая working (Р2)."""
    for s in ["visit", "call", "refuse", "ban", "in_progress", "working"]:
        assert resolve_callback_status(s, {}, True) == (s, False, True)
        assert resolve_callback_status(s, {}, False) == (s, False, True)


def test_unrecognized_with_phone_substituted():
    for raw in [None, "", "cal", "Call", " call", "zzz", 0, {"x": 1}]:
        status, substituted, recognized = resolve_callback_status(raw, {}, True)
        assert (status, substituted, recognized) == ("call", True, False), raw


def test_next_step_heuristic_with_phone_gives_visit():
    for word in ["визит", "салон", "приедет"]:
        status, substituted, _ = resolve_callback_status(
            "zzz", {"next_step": f"Обсудили {word.upper()}"}, True)
        assert (status, substituted) == ("visit", True)


def test_unrecognized_without_phone_stays_raw():
    """Р3: без телефона ни подстановки, ни эвристики — статус сырой."""
    assert resolve_callback_status("zzz", {}, False) == ("zzz", False, False)
    assert resolve_callback_status(None, {}, False) == (None, False, False)
    # Обязательное требование: «визит в салон» в next_step без телефона
    # НЕ превращается в visit — иначе обход обоих барьеров
    assert resolve_callback_status(
        "zzz", {"next_step": "визит в салон"}, False) == ("zzz", False, False)


def test_callback_stage_barriers():
    # Р4: visit/call без телефона → ОЖИДАНИЕ; с телефоном — «Обогащение»
    assert callback_stage("visit", False) == WAITING_STAGE
    assert callback_stage("call", False) == WAITING_STAGE
    assert callback_stage("visit", True) == ENRICH_STAGE
    assert callback_stage("call", True) == ENRICH_STAGE
    # Прочие статусы телефоном не гейтятся (пины карты)
    assert callback_stage("refuse", False) == "C9:LOSE"
    assert callback_stage("ban", False) == "C9:APOLOGY"
    assert callback_stage("in_progress", False) == "C9:UC_MDA6RZ"
    assert callback_stage("working", True) == WAITING_STAGE
    # Р3: сырой нераспознанный без телефона падает в ОЖИДАНИЕ через DEFAULT
    assert callback_stage("zzz", False) == WAITING_STAGE


# =======================
# Обвязка интеграционных тестов обработчика
# =======================

class _FakeReq:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status = 200

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _wire(monkeypatch, http_calls, logs, alerts):
    """Общая подмена окружения обработчика: БД-имя main, aiohttp, алерт, лог."""
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)
    monkeypatch.setattr(main_mod, "BITRIX_WEBHOOK_URL", "http://bx.test")
    monkeypatch.setattr(main_mod, "SUPPORT_GROUP_ID", "grp-test")

    class _Sess:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **kw):
            # В проде оба пути делают `await session.post(...)` без async with
            http_calls.append((url, json))
            if "crm.deal.get" in url:
                return _FakeResp({"result": {"ID": "D-7", "CONTACT_ID": None}})
            return _FakeResp({"result": True})

    monkeypatch.setattr(main_mod.aiohttp, "ClientSession", _Sess)

    def _log(target, message, level="INFO"):
        logs.append((level, str(message)))
    monkeypatch.setattr(main_mod, "log_session", _log)

    async def _alert(group_id=None, text=None, **kw):
        alerts.append((group_id, text))
        return True
    monkeypatch.setattr(main_mod.max_client, "send_alert_to_group", _alert)


async def _mk_user(db, uid, phone=None, context_json=None):
    async with db.AsyncSessionLocal() as session:
        session.add(User(platform_user_id=uid, channel="max", phone=phone,
                         context_json=json.dumps(context_json or {},
                                                 ensure_ascii=False)))
        await session.commit()
    main_mod.USER_CONTEXT.pop(uid, None)


async def _call(uid, payload_extra=None, status=None, deal_id="D-7"):
    body = {"user_id": uid, "deal_id": deal_id,
            "ai_payload": json.dumps(payload_extra or {}, ensure_ascii=False)}
    if status is not None:
        body["status"] = status
    resp = await main_mod.handle_n8n_callback(_FakeReq(body))
    # Ждём ЗАВЕРШЕНИЯ фоновой задачи, а не фиксированный таймер: background_crm_update
    # ходит в БД, и на живом PostgreSQL (TEST_DATABASE_URL) первое подключение пула
    # занимало дольше 50 мс — фикс 0.05 давал гонку, зависящую от длины сюиты
    # (вскрылось при добавлении тестов мультибота, 2026-09-16).
    for _ in range(200):  # потолок ~4 с — защита от вечного ожидания
        pending = [t for t in asyncio.all_tasks()
                   if t is not asyncio.current_task() and not t.done()]
        if not pending:
            break
        await asyncio.sleep(0.02)
    return resp


def _update_fields(http_calls):
    updates = [j for (u, j) in http_calls if "crm.deal.update" in u]
    assert updates, "crm.deal.update не отправлен"
    return updates[-1]["fields"]


# =======================
# Обработчик: честные visit/call (Р4)
# =======================

@pytest.mark.asyncio
async def test_honest_visit_without_phone_goes_waiting(db, monkeypatch):
    http_calls, logs, alerts = [], [], []
    _wire(monkeypatch, http_calls, logs, alerts)
    await _mk_user(db, "g-v1", phone=None)
    await _call("g-v1", {"sb_brand": "LADA"}, status="visit")
    fields = _update_fields(http_calls)
    assert fields["STAGE_ID"] == WAITING_STAGE
    # В2: данные пишутся полностью — смарт-поля и «Тип обращения» на месте
    assert fields[UF_SB_BRAND] == "LADA"
    assert fields[UF_TYPE] == "Визит в салон"
    assert alerts == [], "карточка дилеру не должна отправляться"
    # Архивный слот заполнен как раньше
    assert main_mod.USER_CONTEXT["g-v1"]["pending_archive"]["status"] == "visit"


@pytest.mark.asyncio
async def test_honest_call_without_phone_goes_waiting(db, monkeypatch):
    http_calls, logs, alerts = [], [], []
    _wire(monkeypatch, http_calls, logs, alerts)
    await _mk_user(db, "g-c1", phone=None)
    await _call("g-c1", status="call")
    assert _update_fields(http_calls)["STAGE_ID"] == WAITING_STAGE
    assert alerts == []


@pytest.mark.asyncio
async def test_visit_and_call_with_phone_keep_old_behavior(db, monkeypatch):
    """Пин прежнего поведения: телефон есть → «Обогащение» и карточка."""
    http_calls, logs, alerts = [], [], []
    _wire(monkeypatch, http_calls, logs, alerts)
    await _mk_user(db, "g-p1", phone=VALID_PHONE)
    await _call("g-p1", status="call")
    assert _update_fields(http_calls)["STAGE_ID"] == ENRICH_STAGE
    assert len(alerts) == 1 and alerts[0][0] == "grp-test"

    http_calls2, logs2, alerts2 = [], [], []
    _wire(monkeypatch, http_calls2, logs2, alerts2)
    await _mk_user(db, "g-p2", phone=VALID_PHONE)
    await _call("g-p2", status="visit")
    assert _update_fields(http_calls2)["STAGE_ID"] == ENRICH_STAGE
    assert len(alerts2) == 1


# =======================
# Обработчик: нераспознанный статус (Р1/Р3)
# =======================

@pytest.mark.asyncio
async def test_unrecognized_with_phone_substituted_and_logged(db, monkeypatch):
    http_calls, logs, alerts = [], [], []
    _wire(monkeypatch, http_calls, logs, alerts)
    await _mk_user(db, "g-s1", phone=VALID_PHONE)
    await _call("g-s1", status="zzz-типа-звонок")
    # Подстановка в call: Обогащение + карточка (как раньше)
    assert _update_fields(http_calls)["STAGE_ID"] == ENRICH_STAGE
    assert len(alerts) == 1
    # Р1: WARNING с ИСХОДНЫМ значением
    warn = [m for (lvl, m) in logs if lvl == "WARNING" and "не распознан" in m]
    assert warn and "zzz-типа-звонок" in warn[0] and "подставлен" in warn[0]


@pytest.mark.asyncio
async def test_unrecognized_without_phone_waiting_no_substitution(db, monkeypatch):
    http_calls, logs, alerts = [], [], []
    _wire(monkeypatch, http_calls, logs, alerts)
    await _mk_user(db, "g-s2", phone=None)
    await _call("g-s2", status="мусор")
    fields = _update_fields(http_calls)
    assert fields["STAGE_ID"] == WAITING_STAGE
    assert UF_TYPE not in fields, "тип пишется только для visit/call"
    assert alerts == []
    warn = [m for (lvl, m) in logs if lvl == "WARNING" and "не распознан" in m]
    assert warn and "мусор" in warn[0] and "без подстановки" in warn[0]
    # Статус остался сырым вплоть до архива
    assert main_mod.USER_CONTEXT["g-s2"]["pending_archive"]["status"] == "мусор"


@pytest.mark.asyncio
async def test_next_step_heuristic_without_phone_gated(db, monkeypatch):
    """Обязательное требование: «визит/салон/приедет» без телефона → ОЖИДАНИЕ."""
    http_calls, logs, alerts = [], [], []
    _wire(monkeypatch, http_calls, logs, alerts)
    await _mk_user(db, "g-h1", phone=None)
    await _call("g-h1", {"next_step": "Визит в салон, приедет завтра"},
                status="га-га")
    assert _update_fields(http_calls)["STAGE_ID"] == WAITING_STAGE
    assert alerts == []


@pytest.mark.asyncio
async def test_working_recognized_no_substitution(db, monkeypatch):
    """Р2: working легален — стадия по карте, WARNING нет, карточки нет."""
    http_calls, logs, alerts = [], [], []
    _wire(monkeypatch, http_calls, logs, alerts)
    await _mk_user(db, "g-w1", phone=VALID_PHONE)
    await _call("g-w1", status="working")
    fields = _update_fields(http_calls)
    assert fields["STAGE_ID"] == WAITING_STAGE  # C9:UC_HW8Q2H по карте
    assert UF_TYPE not in fields
    assert alerts == []
    assert not [m for (lvl, m) in logs if lvl == "WARNING"]


# =======================
# Дедупликация (В4: сырой статус в реестре)
# =======================

@pytest.mark.asyncio
async def test_duplicate_raw_status_dropped(db, monkeypatch):
    http_calls, logs, alerts = [], [], []
    _wire(monkeypatch, http_calls, logs, alerts)
    await _mk_user(db, "g-d1", phone=None)
    await _call("g-d1", status="кракозябра")
    first_updates = len([1 for (u, _) in http_calls if "crm.deal.update" in u])
    resp = await _call("g-d1", status="кракозябра")
    assert json.loads(resp.body)["status"] == "duplicate_ignored"
    second_updates = len([1 for (u, _) in http_calls if "crm.deal.update" in u])
    assert first_updates == second_updates == 1, "ретрай не должен трогать сделку"


# =======================
# U5: аварийный мост
# =======================

async def _bridge(uid, status):
    text = ("<function_calls>ai_payload='"
            + json.dumps({"status": status}, ensure_ascii=False)
            + "'</function_calls>")
    await main_mod.send_smartbot_reply(uid, {"text": text})
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_bridge_visit_without_phone_goes_waiting(db, monkeypatch):
    http_calls, logs, alerts = [], [], []
    _wire(monkeypatch, http_calls, logs, alerts)
    await _mk_user(db, "g-b1", phone=None,
                   context_json={"deal_id": "D-7", "channel": "max"})
    await _bridge("g-b1", "visit")
    assert _update_fields(http_calls)["STAGE_ID"] == WAITING_STAGE


@pytest.mark.asyncio
async def test_bridge_call_with_phone_keeps_enrich(db, monkeypatch):
    http_calls, logs, alerts = [], [], []
    _wire(monkeypatch, http_calls, logs, alerts)
    await _mk_user(db, "g-b2", phone=VALID_PHONE,
                   context_json={"deal_id": "D-7", "channel": "max"})
    await _bridge("g-b2", "call")
    assert _update_fields(http_calls)["STAGE_ID"] == ENRICH_STAGE


# =======================
# План 2026-09-07-001 (U5/U6): сумма оценки трейд-ина UF_CRM_1788758195
# =======================

UF_TI_VALUATION = "UF_CRM_1788758195"


@pytest.mark.asyncio
async def test_callback_writes_valuation_from_memory_slot(db, monkeypatch):
    """Слот в живой памяти сессии → поле в crm.deal.update строкой-числом."""
    http_calls, logs, alerts = [], [], []
    _wire(monkeypatch, http_calls, logs, alerts)
    await _mk_user(db, "g-v1", phone=VALID_PHONE)
    main_mod.USER_CONTEXT["g-v1"] = {"evaluation_value": 1820000}
    await _call("g-v1", status="visit")
    assert _update_fields(http_calls)[UF_TI_VALUATION] == "1820000"


@pytest.mark.asyncio
async def test_callback_reads_slot_from_db_after_restart(db, monkeypatch):
    """Память пуста (рестарт между ходом и колбэком) → слот берётся из
    персистентного context_json строки User."""
    http_calls, logs, alerts = [], [], []
    _wire(monkeypatch, http_calls, logs, alerts)
    await _mk_user(db, "g-v2", phone=VALID_PHONE,
                   context_json={"evaluation_value": 1820000})
    await _call("g-v2", status="visit")
    assert _update_fields(http_calls)[UF_TI_VALUATION] == "1820000"


@pytest.mark.asyncio
async def test_callback_no_slot_no_key(db, monkeypatch):
    """Оценки не было → ключ не появляется вовсе (не пустая строка)."""
    http_calls, logs, alerts = [], [], []
    _wire(monkeypatch, http_calls, logs, alerts)
    await _mk_user(db, "g-v3", phone=VALID_PHONE)
    await _call("g-v3", status="visit")
    assert UF_TI_VALUATION not in _update_fields(http_calls)


@pytest.mark.asyncio
async def test_callback_barrier_does_not_drop_valuation(db, monkeypatch):
    """В2/§20.5: барьер Р4 (без телефона) переопределяет стадию, но сумма
    оценки пишется полностью — данные барьером не отбраковываются."""
    http_calls, logs, alerts = [], [], []
    _wire(monkeypatch, http_calls, logs, alerts)
    await _mk_user(db, "g-v4", phone=None,
                   context_json={"evaluation_value": 1820000})
    await _call("g-v4", status="visit")
    fields = _update_fields(http_calls)
    assert fields["STAGE_ID"] == WAITING_STAGE
    assert fields[UF_TI_VALUATION] == "1820000"


@pytest.mark.asyncio
async def test_bridge_writes_valuation_from_context(db, monkeypatch):
    """Аварийный мост — тот же путь завершения: слот из context_json."""
    http_calls, logs, alerts = [], [], []
    _wire(monkeypatch, http_calls, logs, alerts)
    await _mk_user(db, "g-v5", phone=VALID_PHONE,
                   context_json={"deal_id": "D-7", "channel": "max",
                                 "evaluation_value": 1820000})
    await _bridge("g-v5", "visit")
    assert _update_fields(http_calls)[UF_TI_VALUATION] == "1820000"


# =======================
# 2026-10-03: пробег — только клиента и в чат-пути
# =======================

UF_SB_MILEAGE = "UF_CRM_1770635257"


@pytest.mark.asyncio
async def test_callback_client_mileage_from_ai_payload_written(db, monkeypatch):
    """Пробег, названный клиентом в чате (ключ ai_payload), — пишется как раньше."""
    http_calls, logs, alerts = [], [], []
    _wire(monkeypatch, http_calls, logs, alerts)
    await _mk_user(db, "g-m1", phone=VALID_PHONE)
    await _call("g-m1", {"tradein_mileage": "50000"}, status="call")
    assert _update_fields(http_calls)[UF_SB_MILEAGE] == "50000"


@pytest.mark.asyncio
async def test_callback_slot_mileage_from_valuation_not_written(db, monkeypatch):
    """Слот оценки со старым пробегом Auto.ru — в «SB Трейдин Пробег» не уходит."""
    http_calls, logs, alerts = [], [], []
    _wire(monkeypatch, http_calls, logs, alerts)
    await _mk_user(db, "g-m2", phone=VALID_PHONE,
                   context_json={"evaluation_car": {"make": "MAZDA", "mileage": 10200}})
    await _call("g-m2", {}, status="call")
    fields = _update_fields(http_calls)
    assert UF_SB_MILEAGE not in fields and fields["UF_CRM_1770635063"] == "MAZDA"
