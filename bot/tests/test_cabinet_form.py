"""U5: анкета кабинета — черновики, сабмит, UF-маппинг (R24, R25, R41-граница;
R34 — согласованность кодов карты с чат-путём). U1 (план 2026-08-06): состав
анкеты по дизайну, канонические словари плиток, профильные UF из снапшота,
маркер позиции расчёта."""
import asyncio
import json
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select

import database
from database import CabinetEvent, CabinetForm, CabinetProfile, CabinetSession, CabinetToken, PendingSync, User

import cabinet_api
import cabinet_service
from cabinet_service import ApiError

import main as main_mod

from conftest import CONSENT, DEALERS, PHONE

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAIN_PY = ROOT / "main.py"
CABINET_API_PY = ROOT / "cabinet_api.py"
UF_MAP_JSON = ROOT / "cabinet_uf_map.json"


# =======================
# Фикстуры (общие db/deps — в conftest.py; здесь локальное отличие U5)
# =======================

@pytest_asyncio.fixture
async def db(db, monkeypatch):
    # Реальный enqueue_failed_request из main пишет через main.AsyncSessionLocal
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", database.AsyncSessionLocal)
    yield db


@pytest_asyncio.fixture
async def client(db, deps):
    """Тонкий HTTP-клиент: проверка, что новые роуты идут через register_routes."""
    app = web.Application()
    cabinet_api.register_routes(app)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    yield test_client
    await test_client.close()


# =======================
# Хелперы
# =======================

async def _mk_user(db, suffix="1", phone=None):
    async with db.AsyncSessionLocal() as session:
        user = User(platform_user_id=f"u-{suffix}", channel="max",
                    full_name="Иван", phone=phone)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _mk_profile(db, user_id, deal="D-1", **kwargs):
    async with db.AsyncSessionLocal() as session:
        profile = CabinetProfile(user_id=user_id, deal_id=deal, dealer_code="01",
                                 brand_model="Kia Rio", **kwargs)
        session.add(profile)
        await session.commit()
        return profile


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


async def _get_form(db, user_id, deal="D-1"):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(
            select(CabinetForm).where(CabinetForm.user_id == user_id,
                                      CabinetForm.deal_id == deal)
        )).scalar_one_or_none()


# =======================
# Happy: черновик частями, только БД
# =======================

@pytest.mark.asyncio
async def test_draft_saved_in_parts_only_db(db, deps):
    user, bearer = await _authed(db, deps.gw)
    await cabinet_service.save_form_draft(bearer, {"credit_initial": "100000",
                                                   "credit_term": 36})
    await cabinet_service.save_form_draft(bearer, {"tradein_make": "Kia",
                                                   "tradein_year": 2018})
    form = await _get_form(db, user.id)
    assert form.credit_initial == 100000, "число приводимо из строки"
    assert form.credit_term == 36
    assert form.tradein_make == "Kia" and form.tradein_year == 2018, \
        "второй PUT не затирает поля первого"
    assert form.submitted_at is None
    assert deps.bitrix.calls == [], "черновик — только локальная БД (R24)"
    assert deps.enqueues == []


@pytest.mark.asyncio
async def test_draft_soft_validation_garbage_422_per_field(db, deps):
    user, bearer = await _authed(db, deps.gw)
    with pytest.raises(ApiError) as e:
        await cabinet_service.save_form_draft(
            bearer, {"credit_term": "тридцать шесть", "tradein_make": "Kia"})
    assert e.value.status == 422 and e.value.error == "validation"
    assert e.value.extra["fields"] == {"credit_term": "not_a_number"}
    assert await _get_form(db, user.id) is None, "битый запрос не сохраняет ничего"

    with pytest.raises(ApiError) as e:
        await cabinet_service.save_form_draft(bearer, {"no_such_field": 1})
    assert e.value.extra["fields"] == {"no_such_field": "unknown_field"}


@pytest.mark.asyncio
async def test_draft_int_out_of_range_is_422_not_500(db, deps):
    """Целое без верхней границы уходило в commit и давало OverflowError мимо
    ApiError: 500 + техалерт на КАЖДОЕ автосохранение (фронт шлёт по дебаунсу).
    Диапазон проверяется в валидации, ответ — обычный 422 с указанием поля."""
    user, bearer = await _authed(db, deps.gw)

    for bad in (10 ** 30, str(10 ** 30), 2_000_000_001, -1, -5.0):
        with pytest.raises(ApiError) as e:
            await cabinet_service.save_form_draft(
                bearer, {"credit_initial": bad, "tradein_make": "Kia"})
        assert e.value.status == 422 and e.value.error == "validation"
        assert e.value.extra["fields"] == {"credit_initial": "out_of_range"}, bad
        assert await _get_form(db, user.id) is None, \
            "запрос вне диапазона не создаёт и не портит строку анкеты"

    # Границы включительно, сабмит вне диапазона — тот же 422
    await cabinet_service.save_form_draft(bearer, {"credit_initial": 2_000_000_000,
                                                   "credit_term": 0})
    form = await _get_form(db, user.id)
    assert form.credit_initial == 2_000_000_000 and form.credit_term == 0

    with pytest.raises(ApiError) as e:
        await cabinet_service.submit_form(bearer, {"combo_extra_down": 10 ** 30})
    assert e.value.status == 422 and e.value.extra["fields"] == {"combo_extra_down": "out_of_range"}
    form = await _get_form(db, user.id)
    assert form.submitted_at is None and form.credit_initial == 2_000_000_000


@pytest.mark.asyncio
async def test_per_field_ranges_tradein_year_and_combo_extra_down(db, deps):
    """План U1: tradein_year=1899 и combo_extra_down=-1 → out_of_range."""
    user, bearer = await _authed(db, deps.gw)
    with pytest.raises(ApiError) as e:
        await cabinet_service.save_form_draft(bearer, {"tradein_year": 1899})
    assert e.value.status == 422 and e.value.error == "validation"
    assert e.value.extra["fields"] == {"tradein_year": "out_of_range"}

    with pytest.raises(ApiError) as e:
        await cabinet_service.save_form_draft(bearer, {"combo_extra_down": -1})
    assert e.value.extra["fields"] == {"combo_extra_down": "out_of_range"}

    # Нижняя граница года включительно; валидные значения проходят
    await cabinet_service.save_form_draft(bearer, {"tradein_year": 1900,
                                                   "combo_extra_down": 0})
    form = await _get_form(db, user.id)
    assert form.tradein_year == 1900 and form.combo_extra_down == 0


# =======================
# Happy: submit — финальные значения из тела, UF по карте, фолбэк в комментарий
# =======================

@pytest.mark.asyncio
async def test_submit_takes_final_values_from_body_not_draft(db, deps):
    user, bearer = await _authed(db, deps.gw)
    await cabinet_service.save_form_draft(
        bearer, {"credit_initial": 100000, "tradein_make": "Kia"})

    data = await cabinet_service.submit_form(
        bearer, {"credit_initial": 200000,          # отличается от черновика
                 "employment": "ИП"})               # канон плитки → реальный UF
    assert data == {"status": "submitted"}

    updates = deps.bitrix.by_method("crm.deal.update")
    assert len(updates) == 1
    fields = updates[0]["params"]["fields"]
    assert updates[0]["params"]["id"] == "D-1"
    assert fields["UF_CRM_1770634607"] == 200000, \
        "в Bitrix уходит финальное значение ИЗ ТЕЛА, не из черновика"
    assert fields["UF_CRM_1770635063"] == "Kia", \
        "поле черновика, не переопределённое телом, тоже уходит"
    assert fields["UF_CRM_1785858432"] == "ИП", \
        "employment сопоставлен реальному коду — не комментарий (R1)"

    assert deps.bitrix.by_method("crm.timeline.comment.add") == [], \
        "карта полная и снапшота нет — комментарию таймлайна не с чего появиться"

    form = await _get_form(db, user.id)
    assert form.submitted_at is not None
    assert form.credit_initial == 200000

    # Событие аудита и steps_status
    async with db.AsyncSessionLocal() as session:
        events = (await session.execute(
            select(func.count()).select_from(CabinetEvent)
            .where(CabinetEvent.event_type == "form_submitted"))).scalar_one()
    assert events == 1
    me = await cabinet_service.get_me(bearer)
    assert me["submitted"] is True
    assert me["steps_status"]["form_submitted"] is True
    assert me["form"]["credit_initial"] == 200000

    # U10, триггер 1: сабмит запускает ровно одну конверсию существующим путём
    assert len(deps.conversions) == 1, \
        "сабмит — триггер 1 передачи заявки: ровно одна конверсия (U10)"
    assert deps.conversions[0][0] == (user.id,)


# =======================
# Edge: пустая форма, поздний PUT, повторный submit
# =======================

@pytest.mark.asyncio
async def test_empty_submit_422(db, deps):
    user, bearer = await _authed(db, deps.gw)
    with pytest.raises(ApiError) as e:
        await cabinet_service.submit_form(bearer, {})
    assert e.value.status == 422 and e.value.error == "validation"
    assert deps.bitrix.calls == []

    # Все поля пустые — тоже 422
    with pytest.raises(ApiError) as e:
        await cabinet_service.submit_form(bearer, {"credit_term": None,
                                                   "tradein_make": ""})
    assert e.value.status == 422


@pytest.mark.asyncio
async def test_late_put_and_resubmit_already_submitted(db, deps):
    user, bearer = await _authed(db, deps.gw)
    await cabinet_service.submit_form(bearer, {"credit_term": 36})

    with pytest.raises(ApiError) as e:
        await cabinet_service.save_form_draft(bearer, {"credit_term": 48})
    assert e.value.status == 409 and e.value.error == "already_submitted"

    with pytest.raises(ApiError) as e:
        await cabinet_service.submit_form(bearer, {"credit_term": 48})
    assert e.value.status == 409 and e.value.error == "already_submitted"

    form = await _get_form(db, user.id)
    assert form.credit_term == 36, "read-only после submitted_at: значения целы"
    assert len(deps.bitrix.by_method("crm.deal.update")) == 1, \
        "повторной отправки в Bitrix нет"


# =======================
# Edge: qualification_payment (R25)
# =======================

@pytest.mark.asyncio
async def test_qualification_payment_prefills_draft_not_overwritten_by_submit(db, deps):
    user, bearer = await _authed(db, deps.gw)
    await _mk_profile(db, user.id, qualification_payment=25000)

    me = await cabinet_service.get_me(bearer)
    assert me["form"]["credit_payment"] == 25000, \
        "первый GET /me предзаполняет платёж из квалификации"
    form = await _get_form(db, user.id)
    assert form.credit_payment == 25000, "предзаполнение сохранено в черновик"

    # Клиент скорректировал платёж и отправил
    await cabinet_service.submit_form(bearer, {"credit_payment": 30000})
    async with db.AsyncSessionLocal() as session:
        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == user.id)
        )).scalar_one()
    assert profile.qualification_payment == 25000, \
        "сабмит НЕ перезаписывает qualification_payment в профиле (R25)"
    form = await _get_form(db, user.id)
    assert form.credit_payment == 30000

    # /me после сабмита не возвращает предзаполнение поверх отправленного
    me = await cabinet_service.get_me(bearer)
    assert me["form"]["credit_payment"] == 30000 and me["submitted"] is True


@pytest.mark.asyncio
async def test_prefill_does_not_override_client_draft_value(db, deps):
    user, bearer = await _authed(db, deps.gw)
    await _mk_profile(db, user.id, qualification_payment=25000)
    await cabinet_service.save_form_draft(bearer, {"credit_payment": 17000})
    me = await cabinet_service.get_me(bearer)
    assert me["form"]["credit_payment"] == 17000, \
        "предзаполнение — только при пустом поле черновика"


# =======================
# Error: Bitrix 5xx → pending_sync недебаунсимым путём (R35, конвенция U6)
# =======================

@pytest.mark.asyncio
async def test_bitrix_failure_enqueues_despite_chat_debounce(db, deps):
    """Сбой Bitrix при сабмите ставит задачу в pending_sync, даже если чат-путь
    того же пользователя ставил задачу тем же методом секунду назад — проверка
    через РЕАЛЬНЫЙ enqueue_failed_request из main с его 30-секундным дебаунсом."""
    user, bearer = await _authed(db, deps.gw, suffix=f"dbn-{uuid.uuid4()}")
    uid = user.platform_user_id
    method = "crm.deal.update"

    # Кабинет ставит через недебаунсимый путь main (как в setup_cabinet_service)
    cabinet_service.setup(
        deps.bitrix,
        lambda m, p, user_id=None: main_mod.BitrixService.enqueue_failed_request(
            m, p, user_id=user_id, skip_debounce=True),
        None, None, None, lambda: DEALERS)

    async def rows():
        async with db.AsyncSessionLocal() as session:
            return (await session.execute(
                select(PendingSync).where(PendingSync.user_id == uid)
            )).scalars().all()

    # Чат-путь того же пользователя поставил задачу «секунду назад»
    await main_mod.BitrixService.enqueue_failed_request(method, {"chat": 1}, user_id=uid)
    assert len(await rows()) == 1

    deps.bitrix.fail = True
    data = await cabinet_service.submit_form(bearer, {"credit_term": 36})
    assert data == {"status": "submitted"}, "сбой Bitrix не роняет сабмит"

    all_rows = await rows()
    assert len(all_rows) == 2, "постановка кабинета прошла МИМО дебаунса (R35)"
    cabinet_row = [r for r in all_rows if json.loads(r.payload) != {"chat": 1}][0]
    assert cabinet_row.method == method
    assert "UF_CRM_1770634728" in json.loads(cabinet_row.payload)["fields"]

    # Чат-путь после этого по-прежнему дебаунсится — окно не тронуто
    await main_mod.BitrixService.enqueue_failed_request(method, {"chat": 2}, user_id=uid)
    assert len(await rows()) == 2


@pytest.mark.asyncio
async def test_bitrix_exception_also_enqueues_nodebounce(db, deps):
    user, bearer = await _authed(db, deps.gw)
    # Снапшот с item_id даёт маркер → второй вызов (комментарий таймлайна)
    await _mk_profile(db, user.id,
                      calc_snapshot=json.dumps({"item_id": "C-1", "price": 1}),
                      calc_frozen_at=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc))

    async def boom(method, params, user_id=None):
        raise ConnectionError("Bitrix down")

    cabinet_service.setup(boom, cabinet_service._enqueue_nodebounce, None, None,
                          None, lambda: DEALERS)
    await cabinet_service.submit_form(bearer, {"credit_term": 36,
                                               "employment": "Работа по найму"})
    methods = [m for m, _, _ in deps.enqueues]
    assert methods == ["crm.deal.update", "crm.timeline.comment.add"], \
        "оба сорвавшихся вызова поставлены в очередь недебаунсимым путём"


@pytest.mark.asyncio
async def test_real_wiring_enqueues_exactly_one_row_per_cabinet_call(db, deps, monkeypatch):
    """БОЕВАЯ проводка целиком (setup_cabinet_service + реальный
    BitrixService._request, а не фейк): один сорвавшийся вызов кабинета даёт
    РОВНО ОДНУ строку pending_sync.

    Раньше _request после трёх неудач ставил свою (дебаунсимую) строку и
    возвращал {}, а кабинет ставил вторую недебаунсимым путём — реплей обеих
    создавал ДВА комментария в таймлайне сделки (crm.deal.update идемпотентен,
    comment.add — нет). Фейковый bitrix_request этот дефект не показывает."""
    # Ретраи _request идут в недоступный порт; паузы между попытками схлопываем
    monkeypatch.setattr(main_mod.BitrixService, "WEBHOOK", "http://127.0.0.1:9/")
    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *args, **kwargs):
        return await real_sleep(0, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(main_mod, "CACHED_DEALERS", DEALERS)
    main_mod.setup_cabinet_service()

    async def rows_for(uid):
        async with db.AsyncSessionLocal() as session:
            return (await session.execute(
                select(PendingSync).where(PendingSync.user_id == uid)
            )).scalars().all()

    cabinet_uid = f"cab-{uuid.uuid4()}"
    await cabinet_service._send_timeline_comment("D-77", "черновик анкеты", cabinet_uid)
    cabinet_rows = await rows_for(cabinet_uid)
    assert len(cabinet_rows) == 1, \
        f"кабинетный вызов поставил {len(cabinet_rows)} строк вместо одной"
    assert cabinet_rows[0].method == "crm.timeline.comment.add"

    # Чат-путь своё поведение сохраняет: _request по умолчанию ставит сам
    chat_uid = f"chat-{uuid.uuid4()}"
    assert await main_mod.BitrixService._request(
        "crm.timeline.comment.add", {"chat": 1}, user_id=chat_uid) == {}
    assert len(await rows_for(chat_uid)) == 1, \
        "чат-путь обязан ставить в очередь сам — поведение не менялось"


def test_source_pin_cabinet_bitrix_request_has_no_own_enqueue():
    src = MAIN_PY.read_text(encoding="utf-8")
    setup_src = src.split("def setup_cabinet_service", 1)[1]
    assert "bitrix_request=_cabinet_bitrix_request" in setup_src, \
        "кабинет ходит в Bitrix обёрткой без собственной постановки в очередь"
    wrapper_src = src.split("async def _cabinet_bitrix_request", 1)[1] \
                     .split("\ndef setup_cabinet_service", 1)[0]
    assert "enqueue_on_failure=False" in wrapper_src, \
        "единственная постановка кабинета — недебаунсимая (R35), без дублей"
    assert "BitrixService._request(" in wrapper_src, \
        "обёртка вызывает существующий _request — параллельного клиента нет"


# =======================
# Edge: BBCode-экранирование и лимит комментария 500
# =======================

@pytest.mark.asyncio
async def test_bbcode_escaped_and_comment_trimmed_to_500(db, deps, monkeypatch, tmp_path):
    """Правило фолбэка живёт: карта недоступна → ВСЕ поля уходят комментарием
    таймлайна с BBCode-экранированием и лимитом 500 (U1 сохраняет механизм)."""
    user, bearer = await _authed(db, deps.gw)
    monkeypatch.setattr(cabinet_service, "UF_MAP_FILE",
                        str(tmp_path / "нет-такого-файла.json"))
    long_text = "вмятина [левое крыло] и скол [капот] " * 20  # длинный свободный текст
    await cabinet_service.submit_form(
        bearer, {"client_comment": long_text})

    assert deps.bitrix.by_method("crm.deal.update") == [], \
        "карта не прочитана — UF не пишутся, отправка не падает"
    comments = deps.bitrix.by_method("crm.timeline.comment.add")
    assert len(comments) == 1
    comment = comments[0]["params"]["fields"]["COMMENT"]
    assert "[" not in comment and "]" not in comment, \
        "[ и ] обязаны быть экранированы (BBCode)"
    assert "&#91;" in comment and "&#93;" in comment
    assert len(comment) <= 500, "комментарий обрезается до 500 символов"

    # Свободный текст ограничен по длине и в БД
    form = await _get_form(db, user.id)
    assert len(form.client_comment) <= 500


# =======================
# Edge: /me — сигнал жизни (продление сессии, last_seen_at)
# =======================

@pytest.mark.asyncio
async def test_me_extends_session_and_updates_last_seen(db, deps):
    user, bearer = await _authed(db, deps.gw)
    async with db.AsyncSessionLocal() as session:
        token = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == user.id)
        )).scalar_one()
        assert token.last_seen_at is None

    me = await cabinet_service.get_me(bearer)
    assert me["state"] == "ok"
    assert me["profile"]["name"] == "Иван"
    assert set(cabinet_service.FORM_FIELDS) == set(me["form"].keys())
    assert me["submitted"] is False and me["submitted_at"] is None
    assert me["steps_status"]["phone_confirmed"] is True
    assert me["steps_status"]["form_submitted"] is False

    async with db.AsyncSessionLocal() as session:
        token = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == user.id)
        )).scalar_one()
        sess = (await session.execute(select(CabinetSession))).scalar_one()
    assert token.last_seen_at is not None, "/me — сигнал жизни"
    assert cabinet_service._aware(sess.expires_at) > datetime.now(timezone.utc) + timedelta(minutes=39), \
        "скользящее окно сессии продлено"


# =======================
# HTTP-проводка: роуты идут через register_routes (пин main.py — в U4-тестах)
# =======================

@pytest.mark.asyncio
async def test_http_flow_put_draft_submit_me(db, deps, client):
    user, bearer = await _authed(db, deps.gw, suffix="http")
    headers = {"Authorization": bearer}

    resp = await client.put('/cabinet/api/form',
                            json={"credit_initial": 150000}, headers=headers)
    assert resp.status == 200 and (await resp.json())["status"] == "draft_saved"

    resp = await client.get('/cabinet/api/me', headers=headers)
    body = await resp.json()
    assert resp.status == 200 and body["form"]["credit_initial"] == 150000

    resp = await client.post('/cabinet/api/form/submit',
                             json={"credit_initial": 160000}, headers=headers)
    assert resp.status == 200 and (await resp.json())["status"] == "submitted"

    resp = await client.post('/cabinet/api/form/submit',
                             json={"credit_initial": 160000}, headers=headers)
    assert resp.status == 409 and (await resp.json())["error"] == "already_submitted"

    resp = await client.put('/cabinet/api/form', json={"credit_term": 1},
                            headers=headers)
    assert resp.status == 409

    resp = await client.put('/cabinet/api/form', json={"credit_term": 1})
    assert resp.status == 401, "без Bearer — unauthorized"


# =======================
# Карта UF и source-пины (конвенция tests/test_payload.py)
# =======================

def test_uf_map_json_shape_and_r34_alignment():
    """U1/R1: карта без плейсхолдеров, ключи = FORM_FIELDS; пересекающиеся с
    BITRIX_SMART_FIELDS_MAP ключи совпадают кодами (R34), новые коды 1785… —
    эксклюзив кабинета, в чат-карте их нет."""
    raw = json.loads(UF_MAP_JSON.read_text(encoding="utf-8"))
    assert "_comment" in raw, "карта обязана нести _comment о правиле фолбэка"
    assert "UF_CRM_CABINET_" not in UF_MAP_JSON.read_text(encoding="utf-8"), \
        "плейсхолдеров в карте не осталось (R1)"
    data = {k: v for k, v in raw.items() if not k.startswith("_")}
    assert set(data.keys()) == set(cabinet_service.FORM_FIELDS), \
        "карта заведена по всем полям действующего состава анкеты"
    for field, code in data.items():
        assert isinstance(code, str) and code.startswith("UF_CRM_1"), \
            f"{field}: код обязан быть реальным UF-кодом"
        if field in main_mod.BITRIX_SMART_FIELDS_MAP:
            assert main_mod.BITRIX_SMART_FIELDS_MAP[field] == code, \
                f"{field}: код кабинета обязан совпадать с чат-путём (R34)"
        else:
            assert code not in main_mod.BITRIX_SMART_FIELDS_MAP.values(), \
                f"{field}: эксклюзивный код кабинета не должен жить в чат-карте"


def test_r34_owned_codes_include_new_and_overlap_unchanged():
    """R4: фильтр R34 сам вычисляется из карты — новые коды 1785… попадают в
    _cabinet_owned_codes (ок: чат-путь их не пишет, пересечение пустое), а
    пересечение с BITRIX_SMART_FIELDS_MAP — прежние восемь кодов."""
    owned = cabinet_service._cabinet_owned_codes()
    new_codes = {
        "UF_CRM_1785857378", "UF_CRM_1785858399", "UF_CRM_1785858432",
        "UF_CRM_1785858481", "UF_CRM_1785857412", "UF_CRM_1785857424",
        "UF_CRM_1785857440", "UF_CRM_1785857476", "UF_CRM_1785857545",
    }
    assert new_codes <= owned, "новые коды карты — в фильтре (он читает карту)"
    chat_codes = set(main_mod.BITRIX_SMART_FIELDS_MAP.values())
    assert owned & chat_codes == {
        "UF_CRM_1770634607", "UF_CRM_1770634728", "UF_CRM_1770634992",
        "UF_CRM_1770635063", "UF_CRM_1770635116", "UF_CRM_1770635208",
        "UF_CRM_1770635257", "UF_CRM_1770635310",
    }, "пересечение с чат-картой не изменилось: фильтр вырезает прежнее множество"
    # Профильные коды (steering / ti_valuation) в карте формы не живут —
    # они не поля анкеты и фильтром R34 не управляются
    assert cabinet_service.PROFILE_UF_MAP == {
        "steering": "UF_CRM_1785857507",
        "ti_valuation": "UF_CRM_1785857524",
    }


# =======================
# U1 (план 2026-08-06): состав анкеты по дизайну и закрытие UF-карты
# =======================

SNAPSHOT_FROZEN_AT = datetime(2026, 8, 6, 12, 30, tzinfo=timezone.utc)


def _snapshot(**extra):
    data = {"schema_version": 1, "item_id": "C-42", "price": 2490000}
    data.update(extra)
    return json.dumps(data)


@pytest.mark.asyncio
async def test_full_credit_form_three_new_codes_canonical_no_placeholders(db, deps):
    """Полная кредитная анкета → ровно три новых UF-кода с каноническими
    строками плиток; ни одного плейсхолдера в payload."""
    user, bearer = await _authed(db, deps.gw)
    await cabinet_service.submit_form(bearer, {
        "credit_initial": 500000, "credit_term": 60, "credit_payment": 35000,
        "marital_status": "Женат/замужем",
        "education": "Высшее",
        "employment": "Работа по найму",
    })
    updates = deps.bitrix.by_method("crm.deal.update")
    assert len(updates) == 1
    fields = updates[0]["params"]["fields"]
    assert fields["UF_CRM_1785857378"] == "Женат/замужем"
    assert fields["UF_CRM_1785858399"] == "Высшее"
    assert fields["UF_CRM_1785858432"] == "Работа по найму"
    new_codes = {k for k in fields if k.startswith("UF_CRM_1785")}
    assert new_codes == {"UF_CRM_1785857378", "UF_CRM_1785858399",
                        "UF_CRM_1785858432"}, "ровно три новых кода"
    assert not any(k.startswith("UF_CRM_CABINET_") for k in fields), \
        "плейсхолдеров в payload нет (R1)"
    assert deps.bitrix.by_method("crm.timeline.comment.add") == [], \
        "все поля сопоставлены — комментария нет"


@pytest.mark.asyncio
async def test_tradein_form_five_new_codes_client_comment_in_uf(db, deps):
    """Трейд-ин: пять новых кодов + существующие tradein_*; client_comment
    уходит в UF_CRM_1785857476, а не в комментарий таймлайна."""
    user, bearer = await _authed(db, deps.gw)
    await cabinet_service.submit_form(bearer, {
        "tradein_make": "Nissan", "tradein_model": "Almera",
        "tradein_year": 2014, "tradein_mileage": 66000,
        "tradein_plate": "А123ВС154",
        "tradein_condition": "Хорошее",
        "owners_by_pts": "2",
        "pts_type": "Оригинал",
        "keys_count": "Более 2",
        "client_comment": "Есть скол на капоте",
    })
    updates = deps.bitrix.by_method("crm.deal.update")
    assert len(updates) == 1
    fields = updates[0]["params"]["fields"]
    # Пять новых кодов трейд-ин блока
    assert fields["UF_CRM_1785858481"] == "Хорошее"
    assert fields["UF_CRM_1785857412"] == "2"
    assert fields["UF_CRM_1785857424"] == "Оригинал"
    assert fields["UF_CRM_1785857440"] == "Более 2"
    assert fields["UF_CRM_1785857476"] == "Есть скол на капоте"
    # Существующие коды переиспользованы как есть (R4)
    assert fields["UF_CRM_1770635063"] == "Nissan"
    assert fields["UF_CRM_1770635116"] == "Almera"
    assert fields["UF_CRM_1770635208"] == 2014
    assert fields["UF_CRM_1770635257"] == 66000
    assert fields["UF_CRM_1770635310"] == "А123ВС154"
    assert deps.bitrix.by_method("crm.timeline.comment.add") == [], \
        "client_comment сопоставлен UF — в комментарий таймлайна не уходит"


@pytest.mark.asyncio
async def test_combo_extra_down_written_as_string(db, deps):
    user, bearer = await _authed(db, deps.gw)
    await cabinet_service.submit_form(bearer, {"credit_initial": 900000,
                                               "combo_extra_down": 150000})
    fields = deps.bitrix.by_method("crm.deal.update")[0]["params"]["fields"]
    assert fields["UF_CRM_1785857545"] == "150000", \
        "доплата к взносу уходит СТРОКОЙ (план U1: число → строкой)"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,canon", [("LEFT", "Левый"), ("RIGHT", "Правый"),
                                       ("Левый", "Левый")])
async def test_profile_snapshot_steering_and_ti_valuation(db, deps, raw, canon):
    """Профиль со снапшотом: steering из found_info.steering_wheel (канон
    макета «Левый»/«Правый» — маппинг из кодов auto.ru LEFT/RIGHT) и
    ti_valuation — оба кода заполнены строками."""
    user, bearer = await _authed(db, deps.gw)
    await _mk_profile(
        db, user.id,
        calc_snapshot=_snapshot(found_info={"steering_wheel": raw},
                                ti_valuation=850000),
        calc_frozen_at=SNAPSHOT_FROZEN_AT)
    await cabinet_service.submit_form(bearer, {"credit_term": 36})
    fields = deps.bitrix.by_method("crm.deal.update")[0]["params"]["fields"]
    assert fields["UF_CRM_1785857507"] == canon, \
        "в UF — формулировка, которую клиент видит в блоке «Ваш автомобиль»"
    assert fields["UF_CRM_1785857524"] == "850000", "оценка — строкой"


@pytest.mark.asyncio
async def test_profile_without_snapshot_codes_absent_submit_ok(db, deps):
    user, bearer = await _authed(db, deps.gw)
    await _mk_profile(db, user.id)  # профиль есть, снапшота нет
    data = await cabinet_service.submit_form(bearer, {"credit_term": 36})
    assert data == {"status": "submitted"}, "отсутствие снапшота не роняет отправку"
    fields = deps.bitrix.by_method("crm.deal.update")[0]["params"]["fields"]
    assert "UF_CRM_1785857507" not in fields
    assert "UF_CRM_1785857524" not in fields


@pytest.mark.asyncio
async def test_calc_marker_in_timeline_comment(db, deps):
    """Маркер позиции расчёта: item_id и frozen_at снапшота — строкой в
    комментарий таймлайна при сабмите; UF-поля под маркер нет."""
    user, bearer = await _authed(db, deps.gw)
    await _mk_profile(db, user.id, calc_snapshot=_snapshot(),
                      calc_frozen_at=SNAPSHOT_FROZEN_AT)
    await cabinet_service.submit_form(bearer, {"credit_term": 36})

    comments = deps.bitrix.by_method("crm.timeline.comment.add")
    assert len(comments) == 1
    comment = comments[0]["params"]["fields"]["COMMENT"]
    assert "item_id=C-42" in comment
    assert "2026-08-06" in comment and "frozen_at=" in comment
    fields = deps.bitrix.by_method("crm.deal.update")[0]["params"]["fields"]
    assert not any("item_id" in str(v) for v in fields.values()), \
        "маркер не живёт в UF-полях"


@pytest.mark.asyncio
async def test_no_snapshot_no_marker_comment(db, deps):
    user, bearer = await _authed(db, deps.gw)
    await _mk_profile(db, user.id)
    await cabinet_service.submit_form(bearer, {"credit_term": 36})
    assert deps.bitrix.by_method("crm.timeline.comment.add") == [], \
        "снапшота нет → маркера нет, комментарий не создаётся"


@pytest.mark.asyncio
async def test_tile_value_not_in_dictionary_422_not_allowed(db, deps):
    user, bearer = await _authed(db, deps.gw)
    for field, bad in [("marital_status", "женат"), ("education", "ПТУ"),
                       ("employment", "найм"), ("tradein_condition", "хорошее"),
                       ("owners_by_pts", "5"), ("pts_type", "оригинал"),
                       ("keys_count", "0")]:
        with pytest.raises(ApiError) as e:
            await cabinet_service.save_form_draft(bearer, {field: bad})
        assert e.value.status == 422 and e.value.error == "validation", field
        assert e.value.extra["fields"] == {field: "not_allowed"}, field
    # Пустая строка/None — допустимы (черновик), очищают поле
    await cabinet_service.save_form_draft(bearer, {"marital_status": "",
                                                   "pts_type": None})
    form = await _get_form(db, user.id)
    assert form.marital_status is None and form.pts_type is None


@pytest.mark.asyncio
async def test_retired_fields_put_422_unknown_field(db, deps):
    """Выведенные из оборота поля в PUT → 422 unknown_field (R1)."""
    user, bearer = await _authed(db, deps.gw)
    for retired in ("income", "work_experience", "family_composition",
                    "tradein_damage", "tradein_engine", "tradein_vin",
                    "tradein_owners"):
        with pytest.raises(ApiError) as e:
            await cabinet_service.save_form_draft(bearer, {retired: "x"})
        assert e.value.status == 422, retired
        assert e.value.extra["fields"] == {retired: "unknown_field"}, retired
        assert retired not in cabinet_service.FORM_FIELDS, retired


@pytest.mark.asyncio
async def test_full_cycle_draft_submit_on_aware_db(aware_db, deps):
    """Integration (R20): полный цикл draft → submit на aware-стороне —
    включая чтение calc_frozen_at и submitted_at как timezone-aware."""
    user, bearer = await _authed(aware_db, deps.gw, suffix="tz-form")
    await _mk_profile(aware_db, user.id,
                      calc_snapshot=_snapshot(found_info={"steering_wheel": "LEFT"},
                                              ti_valuation=700000),
                      calc_frozen_at=SNAPSHOT_FROZEN_AT)

    await cabinet_service.save_form_draft(bearer, {"credit_initial": 300000,
                                                   "marital_status": "В разводе"})
    me = await cabinet_service.get_me(bearer)
    assert me["form"]["marital_status"] == "В разводе"
    assert me["submitted"] is False

    await cabinet_service.submit_form(bearer, {"credit_term": 48,
                                               "keys_count": "2"})
    fields = deps.bitrix.by_method("crm.deal.update")[0]["params"]["fields"]
    assert fields["UF_CRM_1785857378"] == "В разводе"
    assert fields["UF_CRM_1785857440"] == "2"
    assert fields["UF_CRM_1785857507"] == "Левый"
    assert fields["UF_CRM_1785857524"] == "700000"
    comment = deps.bitrix.by_method("crm.timeline.comment.add")[0]["params"]["fields"]["COMMENT"]
    assert "item_id=C-42" in comment

    me = await cabinet_service.get_me(bearer)
    assert me["submitted"] is True and me["submitted_at"] is not None
    form = await _get_form(aware_db, user.id)
    assert form.submitted_at.tzinfo is not None, "aware-БД отдаёт aware-время"


def test_source_pins_routes_and_stub():
    api_src = CABINET_API_PY.read_text(encoding="utf-8")
    assert 'add_put("/cabinet/api/form", handle_form_save)' in api_src
    assert 'add_post("/cabinet/api/form/submit", handle_form_submit)' in api_src
    src = MAIN_PY.read_text(encoding="utf-8")
    assert "cabinet_api.register_routes(app)" in src, \
        "новые роуты обязаны попадать в app через register_routes"

    svc_src = (ROOT / "cabinet_service.py").read_text(encoding="utf-8")
    assert "attempt_handoff" in svc_src.split("async def on_form_submitted", 1)[1], \
        "on_form_submitted запускает передачу заявки через attempt_handoff (U10)"
