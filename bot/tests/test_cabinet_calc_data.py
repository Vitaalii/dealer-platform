"""U5 (план 2026-08-06): данные расчёта — заморозка снапшота калькулятора,
NocoDB-ставки/программа с проекцией полей, /me.calc, сверка calc при сабмите,
GET /cabinet/api/car (R13–R15, R18 серверная часть, R20)."""
import json

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import CabinetEvent, CabinetForm, CabinetProfile, User

import cabinet_api
import cabinet_service
from cabinet_service import ApiError

import main as main_mod

from conftest import CONSENT, PHONE

# =======================
# Фикстуры (общие db/deps — в conftest.py; здесь дилер с db_config и таблицы
# фейкового NocoDB-загрузчика — тем же setup())
# =======================

DEALERS = {
    "01": {
        "dealer_name": "Тест Авто", "website": "https://dealer-one.ru/", "code": "01",
        "db_config": {
            "catalog_table_id": "cat01",
            "rates_table_id": "rate01",
            "program_table_id": "prog01",
        },
    },
}

# Каталог: поля — как их читает car_search_prepare.js (Complectation id,
# Model title, Engine, Transmission_ru, Drivetrain_ru, Acceleration,
# «Сonsumption» с КИРИЛЛИЧЕСКОЙ «С» — реальное имя колонки NocoDB).
# Пара (Model id, Complectation id) уникальна по таблице; Complectation id
# сам по себе уникален только внутри модели.
CATALOG = [
    {"Id": 7, "Model id": "02", "Complectation id": "0203",
     "Model": "LADA Vesta", "Complectation": "Comfort",
     "Model title": "LADA Vesta Comfort", "Engine": "1.6 л, 106 л.с.",
     "Transmission_ru": "Механика", "Drivetrain_ru": "Передний",
     "Acceleration": "12.1", "Сonsumption": "6.9", "clearance": "178 мм",
     "Price new": 1_853_000,  # розничная цена карточки (R16, U9 плана v3)
     "Title_pic": [{"path": "download/vesta-title.jpg"}],  # в карусель НЕ идёт
     "Pics": [{"signedUrl": "https://cdn.noco/vesta-1.jpg"},
              {"path": "//img.example.com/vesta-2.jpg"},
              {"signedPath": "download/vesta-3.jpg"}]},
    {"Id": 9, "Model id": "02", "Complectation id": "0204",
     "Complectation": "Luxe",
     "Model title": "LADA Vesta Luxe", "Engine": "1.8 л, 122 л.с.",
     "Transmission_ru": "Вариатор", "Drivetrain_ru": "Передний",
     "Acceleration": "11.0", "Сonsumption": "7.2", "Pics": []},
]

# Ставки: строка с «16,5», строка с нечисловой ставкой (выбрасывается целиком),
# служебные колонки (Service/Bank — не должны попасть в снапшот), НЕ отсортированы
RATES = [
    {"Rate": "18", "Term_From": "36", "Term_To": 84, "Downpayment_From": "30",
     "Downpayment_To": 70, "Service": "внутренняя колонка"},
    {"Rate": "16,5", "Term_From": 12, "Term_To": "36", "Downpayment_From": "20",
     "Downpayment_To": 50, "Bank": "X"},
    {"Rate": "нет данных", "Term_From": 12, "Term_To": 84,
     "Downpayment_From": 0, "Downpayment_To": 100},
]

EXPECTED_RATES = [
    {"Rate": 16.5, "Term_From": 12, "Term_To": 36,
     "Downpayment_From": 20, "Downpayment_To": 50},
    {"Rate": 18, "Term_From": 36, "Term_To": 84,
     "Downpayment_From": 30, "Downpayment_To": 70},
]

# Программы: Model_Pattern — список Model id (решение 2026-09-30); позиция
# 0203 модели «02» → вторая строка
PROGRAMS = [
    {"Model_Pattern": "01, 03, 12", "Program_Name": "Другая",
     "Max_Term_Months": 60, "Min_Down_Payment": 30},
    {"Model_Pattern": "05, 02, 11", "Program_Name": "Стандарт",
     "Max_Term_Months": "96", "Min_Down_Payment": "15,5",
     "Internal_Col": "служебная"},
]

EXPECTED_PROGRAM = {"Program_Name": "Стандарт", "Max_Term_Months": 96,
                    "Min_Down_Payment": 15.5, "Min_Down_Payment_Pct": 15.5}

COMP_A = {"id": "0203", "name": "Comfort", "full_name": "LADA Vesta Comfort",
          "price": {"MSRP": 2_600_000, "credit_price": 2_490_000}}
COMP_B = {"id": "0204", "name": "Luxe", "full_name": "LADA Vesta Luxe",
          "price": {"MSRP": 2_800_000, "credit_price": 2_690_000}}


def make_cache(selected="0203", comps=(COMP_A, COMP_B), payment=35000,
               model_id="02", **extra):
    """json_cache в форме ответа Car Search (car_search_prepare.js):
    selected=None → «00» («Любая») — явного выбора нет. model_id — эхо
    qualification_context внутри ответа (car_search_prepare.js:220);
    model_id=None имитирует старый кэш без эха (снапшот схемы v1)."""
    cache = {
        "user_request": {"selected_complectation_id": selected or "00",
                         "payment": payment},
        "complectations": [dict(c) for c in comps],
    }
    if model_id is not None:
        cache["qualification_context"] = {"model_id": model_id}
    cache.update(extra)
    return cache


@pytest.fixture
def dealers():
    return DEALERS


@pytest.fixture
def nocodb_tables():
    # dict мутируется в тестах ДО заморозки — загрузчик conftest читает его живьём
    return {"cat01": list(CATALOG), "rate01": list(RATES), "prog01": list(PROGRAMS)}


@pytest_asyncio.fixture
async def client(db, deps, monkeypatch):
    """aiohttp test client с БОЕВЫМИ middleware из main.py (CORS-интеграция)."""
    monkeypatch.setattr(main_mod, "TECH_ADMIN_ID", "")
    app = web.Application(middlewares=[main_mod.error_middleware, main_mod.cors_middleware])
    cabinet_api.register_routes(app)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    yield test_client
    await test_client.close()


# =======================
# Хелперы
# =======================

async def _mk_user(db, suffix="1"):
    async with db.AsyncSessionLocal() as session:
        user = User(platform_user_id=f"u-{suffix}", channel="max", full_name="Иван")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _prepare(user, cache, payment=35000, deal="D-1"):
    return await cabinet_service.prepare_cabinet_context(
        user.id, "01", deal_id=deal, has_phone=False, scenario_type="credit",
        payment=payment, brand_model="LADA Vesta", n8n_cache=cache)


async def _verify(gw, url):
    """Полный цикл U4 по выданной ссылке: request-code → verify → Bearer."""
    token = url.split("#t=", 1)[1]
    await cabinet_service.request_code(token, phone=PHONE, consent=CONSENT)
    code = gw.calls[-1]["code"]
    result = await cabinet_service.verify_code(token, code)
    return f"Bearer {result['access_token']}"


async def _snapshot(db, user_id):
    async with db.AsyncSessionLocal() as session:
        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == user_id)
        )).scalar_one_or_none()
    snap = json.loads(profile.calc_snapshot) \
        if profile is not None and profile.calc_snapshot else None
    return snap, (profile.calc_frozen_at if profile is not None else None)


# =======================
# Заморозка при выдаче ссылки (точка «а»)
# =======================

@pytest.mark.asyncio
async def test_freeze_on_link_issue_with_selected_then_me(db, deps):
    user = await _mk_user(db)
    ctx = await _prepare(user, make_cache(selected="0203"))
    assert ctx["cabinet_url"] is not None

    snap, frozen = await _snapshot(db, user.id)
    assert snap["schema_version"] == cabinet_service.CALC_SNAPSHOT_SCHEMA_VERSION
    assert snap["item_id"] == "0203"
    assert snap["model_id"] == "02", \
        "v2: модельная часть пары — из эха qualification_context кэша"
    assert snap["item_name"] == "LADA Vesta Comfort"
    assert snap["price"] == 2_490_000, "price = complectations[].price.credit_price"
    assert snap["target_payment"] == 35000
    assert snap["rates"] == EXPECTED_RATES
    assert snap["program"] == EXPECTED_PROGRAM
    assert snap["found_info"] is None and snap["ti_valuation"] is None, \
        "ключи зарезервированы (блокер n8n) — сейчас null"
    assert frozen is not None, "frozen_at пишется в колонку calc_frozen_at"

    bearer = await _verify(deps.gw, ctx["cabinet_url"])
    me = await cabinet_service.get_me(bearer)
    calc = me["calc"]
    assert calc["item_id"] == "0203" and calc["price"] == 2_490_000
    assert calc["rates"] == EXPECTED_RATES and calc["program"] == EXPECTED_PROGRAM
    assert calc["frozen_at"].endswith("+00:00"), "frozen_at — aware-ISO"
    assert "model_id" not in calc and "schema_version" not in calc, \
        "/me.calc — allow-list: служебные ключи снапшота наружу не идут"


@pytest.mark.asyncio
async def test_reissue_after_selected_change_refreezes(db, deps):
    user = await _mk_user(db)
    await _prepare(user, make_cache(selected="0203"))
    _, frozen1 = await _snapshot(db, user.id)

    ctx = await _prepare(user, make_cache(selected="0204"))
    assert ctx["cabinet_url"] is not None
    snap, frozen2 = await _snapshot(db, user.id)
    assert snap["item_id"] == "0204" and snap["price"] == 2_690_000
    assert snap["program"]["Program_Name"] == "Стандарт", \
        "программа выбирается по Model id: 0204 той же модели «02» — та же программа"
    assert frozen2 != frozen1, "frozen_at обновлён перезаморозкой"


@pytest.mark.asyncio
async def test_empty_profile_single_comp_without_selected_frozen(db, deps):
    user = await _mk_user(db)
    await _prepare(user, make_cache(selected=None, comps=(COMP_A,)))
    snap, _ = await _snapshot(db, user.id)
    assert snap is not None and snap["item_id"] == "0203", \
        "нет selected, снапшот пуст, комплектация ровно одна → она"


@pytest.mark.asyncio
async def test_empty_profile_many_comps_without_selected_skips_but_link_issued(db, deps):
    user = await _mk_user(db)
    ctx = await _prepare(user, make_cache(selected=None))
    assert ctx["cabinet_url"] is not None, "неудачная заморозка не ломает выдачу"
    snap, frozen = await _snapshot(db, user.id)
    assert snap is None and frozen is None


@pytest.mark.asyncio
async def test_existing_snapshot_not_replaced_by_single_other_comp(db, deps):
    user = await _mk_user(db)
    await _prepare(user, make_cache(selected="0203"))
    _, frozen1 = await _snapshot(db, user.id)

    await _prepare(user, make_cache(selected=None, comps=(COMP_B,)))
    snap, frozen2 = await _snapshot(db, user.id)
    assert snap["item_id"] == "0203" and frozen2 == frozen1, \
        "фолбэк «единственная» действует только при ПУСТОМ снапшоте"


@pytest.mark.asyncio
async def test_unfreezable_cache_never_clears_snapshot(db, deps):
    user = await _mk_user(db)
    await _prepare(user, make_cache(selected="0203"))
    _, frozen1 = await _snapshot(db, user.id)

    # несколько комплектаций без selected
    await _prepare(user, make_cache(selected=None))
    # price <= 0 при явном selected
    bad = dict(COMP_B, price={"credit_price": 0})
    await _prepare(user, make_cache(selected="0204", comps=(COMP_A, bad)))
    # кэша нет вовсе
    await _prepare(user, None)

    snap, frozen2 = await _snapshot(db, user.id)
    assert snap["item_id"] == "0203" and snap["price"] == 2_490_000
    assert frozen2 == frozen1, "существующий снапшот не затёрт, frozen_at не сдвинут"


@pytest.mark.asyncio
async def test_no_cache_no_snapshot_me_calc_null(db, deps):
    """Tradein-сценарий: ссылка выдана до выбора комплектации (R18)."""
    user = await _mk_user(db)
    ctx = await _prepare(user, None)
    snap, _ = await _snapshot(db, user.id)
    assert snap is None
    bearer = await _verify(deps.gw, ctx["cabinet_url"])
    me = await cabinet_service.get_me(bearer)
    assert me["calc"] is None


# =======================
# Вторая точка заморозки: сохранение свежего json_cache (refresh_calc_snapshot)
# =======================

@pytest.mark.asyncio
async def test_refresh_on_cache_save_refreezes_same_turn(db, deps):
    user = await _mk_user(db)
    await _prepare(user, make_cache(selected="0203"))

    ok = await cabinet_service.refresh_calc_snapshot(
        user.platform_user_id, make_cache(selected="0204", payment=42000))
    assert ok is True
    snap, _ = await _snapshot(db, user.id)
    assert snap["item_id"] == "0204", \
        "выбор комплектации попадает в снапшот тем же ходом, без лага"
    assert snap["target_payment"] == 42000, "target_payment — из user_request.payment кэша"


@pytest.mark.asyncio
async def test_refresh_without_active_token_is_noop(db, deps):
    user = await _mk_user(db, "no-token")
    ok = await cabinet_service.refresh_calc_snapshot(
        user.platform_user_id, make_cache(selected="0203"))
    assert ok is False
    snap, _ = await _snapshot(db, user.id)
    assert snap is None, "заморозка только при наличии active-токена"


# =======================
# _calc_num: пробел/NBSP в числах NocoDB
# =======================

@pytest.mark.parametrize("raw, expected", [
    ("2 490 000", 2_490_000),      # обычный пробел (U+0020)
    ("2\xa0490\xa0000", 2_490_000),  # неразрывный пробел (U+00A0), как в NocoDB
])
def test_calc_num_strips_regular_space_and_nbsp(raw, expected):
    assert cabinet_service._calc_num(raw) == expected


# =======================
# Нормализация ставок/программы (проекция полей)
# =======================

@pytest.mark.asyncio
async def test_rates_comma_decimal_sorting_and_projection(db, deps):
    user = await _mk_user(db)
    await _prepare(user, make_cache(selected="0203"))
    snap, _ = await _snapshot(db, user.id)

    assert snap["rates"][0]["Rate"] == 16.5, "«16,5» → 16.5"
    assert [r["Term_From"] for r in snap["rates"]] == [12, 36], \
        "сортировка по (Term_From, Downpayment_From)"
    assert len(snap["rates"]) == 2, "строка с нечисловой ставкой выброшена целиком"
    for row in snap["rates"]:
        assert set(row) == set(cabinet_service.CALC_RATES_FIELDS), \
            "служебные колонки таблицы дилера не попадают в снапшот"
    # + прежний ключ движка (дубль Min_Down_Payment, план 2026-09-30-001 U2)
    assert set(snap["program"]) == set(cabinet_service.CALC_PROGRAM_FIELDS) | {"Min_Down_Payment_Pct"}


@pytest.mark.asyncio
async def test_empty_rates_table_freezes_empty_matrix(db, deps, nocodb_tables):
    nocodb_tables["rate01"] = []
    user = await _mk_user(db)
    await _prepare(user, make_cache(selected="0203"))
    snap, _ = await _snapshot(db, user.id)
    assert snap is not None and snap["rates"] == [], \
        "пустая матрица — штатно, движок дофолбэчит 16%"


@pytest.mark.asyncio
async def test_loader_failure_empty_snapshot_freezes_empty_and_car_404(db, deps, monkeypatch):
    """Транзиентный сбой NocoDB (исключение загрузчика) на ПУСТОМ существующем
    снапшоте — не 500, заморозка проходит со штатной деградацией (пустого
    результата и «сбоя» тут различать некому, старый снапшот терять нечего);
    /car при сохраняющемся сбое каталога — 503 catalog_unavailable (#16:
    различимо с «позиция пропала», которая 404 not_found), а не 500."""
    user = await _mk_user(db)

    async def failing_loader(table_id):
        raise RuntimeError("NocoDB недоступна")

    monkeypatch.setattr(cabinet_service, "_nocodb_table_loader", failing_loader)
    ctx = await _prepare(user, make_cache(selected="0203"))
    assert ctx["cabinet_url"] is not None, "сбой NocoDB не должен ломать выдачу ссылки"

    snap, frozen = await _snapshot(db, user.id)
    assert snap is not None and frozen is not None, \
        "снапшота ещё не было — сбою нечего защищать, заморозка проходит"
    assert snap["rates"] == [] and snap["program"] is None

    bearer = await _verify(deps.gw, ctx["cabinet_url"])
    with pytest.raises(ApiError) as e:
        await cabinet_service.get_car_card(bearer)
    assert e.value.status == 503 and e.value.error == "catalog_unavailable", \
        "сбой загрузки каталога отличим от «позиции нет» (#16): 503, не 404/500"


@pytest.mark.asyncio
async def test_loader_failure_nonempty_snapshot_preserved(db, deps, monkeypatch):
    """Транзиентный сбой NocoDB на НЕПУСТОМ существующем снапшоте не затирает
    валидные rates/program пустыми (#7): заморозка целиком пропускается,
    прежняя позиция/ставки/программа сохраняются."""
    user = await _mk_user(db)
    await _prepare(user, make_cache(selected="0203"))
    snap1, frozen1 = await _snapshot(db, user.id)
    assert snap1["rates"] == EXPECTED_RATES and snap1["program"] == EXPECTED_PROGRAM

    async def failing_loader(table_id):
        raise RuntimeError("NocoDB недоступна")

    monkeypatch.setattr(cabinet_service, "_nocodb_table_loader", failing_loader)
    # другая позиция — иначе short-circuit по идентичному содержимому скрыл бы эффект
    ok = await cabinet_service.refresh_calc_snapshot(
        user.platform_user_id, make_cache(selected="0204"))
    assert ok is False, "заморозка пропущена целиком из-за сбоя NocoDB"

    snap2, frozen2 = await _snapshot(db, user.id)
    assert snap2["item_id"] == "0203", "существующая позиция не подменена пустыми данными"
    assert snap2["rates"] == EXPECTED_RATES and snap2["program"] == EXPECTED_PROGRAM, \
        "сбой NocoDB на непустом снапшоте не затирает валидные rates/program"
    assert frozen2 == frozen1, "frozen_at не сдвинут — заморозки не было"


@pytest.mark.asyncio
async def test_freeze_profile_creation_race_rollback_returns_false(db, deps, monkeypatch):
    """Гонка создания профиля (KTD-комментарий в _freeze_calc_snapshot):
    к моменту INSERT новый CabinetProfile уже создан конкурентно (unique
    user_id) — IntegrityError перехватывается rollback'ом, функция отдаёт
    False, наружу ничего не течёт. SELECT первого чтения профиля подменяется
    на «строки нет», хотя строка уже существует — это и есть гонка."""
    user = await _mk_user(db)
    async with db.AsyncSessionLocal() as session:
        session.add(CabinetProfile(user_id=user.id, dealer_code="01"))
        await session.commit()

    orig_execute = AsyncSession.execute
    hit = {"done": False}

    async def racy_execute(self, statement, *args, **kwargs):
        result = await orig_execute(self, statement, *args, **kwargs)
        # Гонка моделируется на чтении профиля внутри записи снапшота
        # (_store_snapshot); предварительное чтение существующего снапшота
        # (_existing_snapshot) идёт штатно
        in_store = any(f.function == "_store_snapshot" for f in __import__("inspect").stack()[:6])
        if in_store and not hit["done"] and "cabinet_profile" in str(statement).lower():
            hit["done"] = True

            class _NoRow:
                def scalar_one_or_none(self_inner):
                    return None

            return _NoRow()
        return result

    monkeypatch.setattr(AsyncSession, "execute", racy_execute)

    ok = await cabinet_service._freeze_calc_snapshot(
        user.id, make_cache(selected="0203"), "01", 35000)
    assert ok is False, "IntegrityError на INSERT — гонка перехвачена, без исключения наружу"

    async with db.AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == user.id)
        )).scalars().all()
    assert len(rows) == 1, "дублирующая вставка не прошла — исходный профиль цел"


@pytest.mark.asyncio
async def test_found_info_proto_relative_urls_normalized(db, deps):
    """Структурная готовность к снятию блокера n8n: found_info из кэша,
    протокол-относительные URL → https: (нормализация на сервере)."""
    user = await _mk_user(db)
    cache = make_cache(selected="0203",
                       found_info={"steering_wheel": "LEFT",
                                   "photos": {"main": "//avatars.mds.yandex.net/get/img.jpg"}},
                       ti_valuation=850_000)
    ctx = await _prepare(user, cache)
    snap, _ = await _snapshot(db, user.id)
    assert snap["found_info"]["photos"]["main"] == \
        "https://avatars.mds.yandex.net/get/img.jpg"
    assert snap["ti_valuation"] == 850_000

    bearer = await _verify(deps.gw, ctx["cabinet_url"])
    me = await cabinet_service.get_me(bearer)
    assert me["calc"]["found_info"]["photos"]["main"].startswith("https://")


# =======================
# Сверка calc при сабмите (409 conflict — до любых эффектов)
# =======================

@pytest.mark.asyncio
async def test_submit_calc_mismatch_409_before_effects(db, deps):
    user = await _mk_user(db)
    ctx = await _prepare(user, make_cache(selected="0203"))
    bearer = await _verify(deps.gw, ctx["cabinet_url"])
    deps.bitrix.calls.clear()

    for bad_calc in ({"item_id": "0204", "price": 2_490_000},   # другой item_id
                     {"item_id": "0203", "price": 1}):          # другая цена
        with pytest.raises(ApiError) as e:
            await cabinet_service.submit_form(
                bearer, {"credit_initial": 500_000, "calc": bad_calc})
        assert e.value.status == 409 and e.value.error == "conflict"

    async with db.AsyncSessionLocal() as session:
        form = (await session.execute(
            select(CabinetForm).where(CabinetForm.user_id == user.id)
        )).scalar_one_or_none()
        assert form is None or form.submitted_at is None, "409 — ДО любых эффектов"
        events = (await session.execute(
            select(CabinetEvent).where(CabinetEvent.event_type == "form_submitted")
        )).scalars().all()
        assert events == []
    assert deps.bitrix.calls == [], "Bitrix не вызывался"

    # Совпадающий блок → сабмит проходит
    result = await cabinet_service.submit_form(
        bearer, {"credit_initial": 500_000,
                 "calc": {"item_id": "0203", "price": 2_490_000}})
    assert result == {"status": "submitted"}


@pytest.mark.asyncio
async def test_submit_calc_against_missing_snapshot_conflicts(db, deps):
    user = await _mk_user(db)
    ctx = await _prepare(user, None)  # снапшота нет
    bearer = await _verify(deps.gw, ctx["cabinet_url"])
    with pytest.raises(ApiError) as e:
        await cabinet_service.submit_form(
            bearer, {"credit_initial": 100_000,
                     "calc": {"item_id": "0203", "price": 2_490_000}})
    assert e.value.status == 409 and e.value.error == "conflict"


@pytest.mark.asyncio
async def test_refreeze_same_data_between_load_and_submit_ok(db, deps):
    """Сверка по содержанию, не по времени: перезаморозка идёт на каждом ходе
    с активным токеном — иначе 409 получал бы любой клиент, параллельно
    переписывающийся с агентом."""
    user = await _mk_user(db)
    ctx = await _prepare(user, make_cache(selected="0203"))
    bearer = await _verify(deps.gw, ctx["cabinet_url"])
    me = await cabinet_service.get_me(bearer)
    page_calc = {"item_id": me["calc"]["item_id"], "price": me["calc"]["price"]}
    _, frozen1 = await _snapshot(db, user.id)

    # перезаморозка теми же данными (новый ход диалога): short-circuit —
    # содержимое не изменилось, БД не трогается (simplify-pass)
    assert await cabinet_service.refresh_calc_snapshot(
        user.platform_user_id, make_cache(selected="0203")) is True
    _, frozen2 = await _snapshot(db, user.id)
    assert frozen2 == frozen1, "идентичное содержимое не должно перезаписывать снапшот"

    result = await cabinet_service.submit_form(
        bearer, {"credit_initial": 500_000, "calc": page_calc})
    assert result == {"status": "submitted"}


@pytest.mark.asyncio
async def test_submit_without_calc_block_skips_check(db, deps):
    """Старый фронт (до U9) блока calc не шлёт — проверка пропускается."""
    user = await _mk_user(db)
    ctx = await _prepare(user, make_cache(selected="0203"))
    bearer = await _verify(deps.gw, ctx["cabinet_url"])
    result = await cabinet_service.submit_form(bearer, {"credit_initial": 500_000})
    assert result == {"status": "submitted"}


# =======================
# Доступ: calc не утекает до верификации
# =======================

@pytest.mark.asyncio
async def test_me_unavailable_before_verification(db, deps):
    user = await _mk_user(db)
    await _prepare(user, make_cache(selected="0203"))
    for bad in (None, "Bearer garbage"):
        with pytest.raises(ApiError) as e:
            await cabinet_service.get_me(bad)
        assert e.value.status == 401


# =======================
# GET /cabinet/api/car
# =======================

@pytest.mark.asyncio
async def test_car_card_specs_and_normalized_images(db, deps):
    user = await _mk_user(db)
    ctx = await _prepare(user, make_cache(selected="0203"))
    bearer = await _verify(deps.gw, ctx["cabinet_url"])

    car = await cabinet_service.get_car_card(bearer)
    # «Model · Complectation» — заголовок карточки по макету v3
    assert car["name"] == "LADA Vesta · Comfort"
    assert car["specs"] == {
        "engine": "1.6 л, 106 л.с.",
        "transmission": "Механика",
        "drive": "Передний",
        "acceleration": "12.1",
        "consumption": "6.9",  # колонка «Сonsumption» с кириллической С
        "clearance": "178 мм",  # значение уже с «мм» — passthrough, как у всех
    }
    # Только Pics (u6-gallery.html:775) — Title_pic в карусели не участвует
    assert car["images"] == [
        "https://cdn.noco/vesta-1.jpg",                # абсолютный — как есть
        "https://img.example.com/vesta-2.jpg",         # протокол-относительный → https:
        "https://noco.test/download/vesta-3.jpg",      # относительный signedPath → база NocoDB
    ]


# =======================
# GET /cabinet/api/car: name = «Model · Complectation» (макет v3)
# =======================

@pytest.mark.asyncio
async def test_car_name_single_part_without_dot(db, deps):
    """Заполнена только одна часть (Luxe: Complectation без Model) —
    показывается без средней точки."""
    user = await _mk_user(db)
    ctx = await _prepare(user, make_cache(selected="0204"))
    bearer = await _verify(deps.gw, ctx["cabinet_url"])
    car = await cabinet_service.get_car_card(bearer)
    assert car["name"] == "Luxe"


@pytest.mark.asyncio
async def test_car_name_fallback_when_both_empty(db, deps, nocodb_tables):
    """Обе части пустые → прежний фолбэк: Model title, затем item_name."""
    row = dict(CATALOG[0])
    row.pop("Model")
    row["Complectation"] = ""  # пустая строка — тоже «не заполнено»
    nocodb_tables["cat01"] = [row]
    user = await _mk_user(db)
    ctx = await _prepare(user, make_cache(selected="0203"))
    bearer = await _verify(deps.gw, ctx["cabinet_url"])
    car = await cabinet_service.get_car_card(bearer)
    assert car["name"] == "LADA Vesta Comfort"  # Model title

    row.pop("Model title")
    car = await cabinet_service.get_car_card(bearer)
    assert car["name"] == "LADA Vesta Comfort"  # item_name снапшота (full_name COMP_A)


# =======================
# GET /cabinet/api/car: price_new (R16/KTD8 плана 2026-09-26-001)
# =======================

@pytest.mark.asyncio
async def test_car_price_new_from_catalog_column(db, deps):
    """Колонка каталога «Price new» → число в ответе /car. Цена расчёта
    (me.calc.price = credit_price снапшота) не затрагивается — это витринная
    цена карточки, не цена калькулятора."""
    user = await _mk_user(db)
    ctx = await _prepare(user, make_cache(selected="0203"))
    bearer = await _verify(deps.gw, ctx["cabinet_url"])
    car = await cabinet_service.get_car_card(bearer)
    assert car["price_new"] == 1_853_000
    me = await cabinet_service.get_me(bearer)
    assert me["calc"]["price"] != car["price_new"]  # источники независимы


@pytest.mark.asyncio
async def test_car_price_new_null_when_column_absent(db, deps):
    """Строка каталога без колонки «Price new» (Luxe) → price_new: null."""
    user = await _mk_user(db)
    ctx = await _prepare(user, make_cache(selected="0204"))
    bearer = await _verify(deps.gw, ctx["cabinet_url"])
    car = await cabinet_service.get_car_card(bearer)
    assert car["price_new"] is None


@pytest.mark.asyncio
async def test_car_price_new_null_when_value_invalid(db, deps, nocodb_tables):
    """Нечисловое значение колонки → null; форма ответа — ровно
    {name, specs, images, price_new}, остальные ключи не меняются."""
    rows = [dict(CATALOG[0])]
    rows[0]["Price new"] = "договорная"
    nocodb_tables["cat01"] = rows
    user = await _mk_user(db)
    ctx = await _prepare(user, make_cache(selected="0203"))
    bearer = await _verify(deps.gw, ctx["cabinet_url"])
    car = await cabinet_service.get_car_card(bearer)
    assert car["price_new"] is None
    assert set(car.keys()) == {"name", "specs", "images", "price_new"}


# Строка чужой модели с ТЕМ ЖЕ Complectation id и ТЕМ ЖЕ Model title —
# отличается только Model id: идентичность обязана держаться на паре
# (Model id, Complectation id), не на отображаемых строках
COLLISION_ROW = {"Id": 21, "Model id": "05", "Complectation id": "0203",
                 "Complectation": "Comfort",
                 "Model title": "LADA Vesta Comfort",  # намеренно то же имя
                 "Engine": "1.2 л, 90 л.с.", "Transmission_ru": "Механика",
                 "Drivetrain_ru": "Передний",
                 "Acceleration": "13.5", "Сonsumption": "6.1",
                 "Pics": [{"signedUrl": "https://cdn.noco/iskra-1.jpg"}]}


@pytest.mark.asyncio
async def test_car_complectation_id_collision_resolved_by_pair(db, deps,
                                                               nocodb_tables):
    """Complectation id НЕ уникален по таблице каталога (уникален только
    внутри модели): по выгрузке дилера 99 id 11 несут 7 строк разных моделей.
    /car обязан найти строку по паре (Model id, Complectation id) из
    снапшота — имя модели в тесте у обеих строк одинаковое, так что пройти
    он может только парой, не названием."""
    # Чужая модель — ПЕРВОЙ в каталоге: поиск первого совпадения по одному
    # комплектационному id вернул бы именно её
    nocodb_tables["cat01"] = [COLLISION_ROW] + list(CATALOG)

    user = await _mk_user(db)
    ctx = await _prepare(user, make_cache(selected="0203", model_id="02"))
    bearer = await _verify(deps.gw, ctx["cabinet_url"])

    car = await cabinet_service.get_car_card(bearer)
    assert car["specs"]["engine"] == "1.6 л, 106 л.с.", \
        "при коллизии Complectation id вернулась строка чужой модели"
    assert car["images"] == [
        "https://cdn.noco/vesta-1.jpg",
        "https://img.example.com/vesta-2.jpg",
        "https://noco.test/download/vesta-3.jpg",
    ]


@pytest.mark.asyncio
async def test_car_pair_gone_from_catalog_404(db, deps, nocodb_tables):
    """Пара снапшота (Model id 02, Complectation id 0203) из каталога
    пропала, но чужая строка с тем же комплектационным id осталась:
    честный 404 not_found, а не первое совпадение по id."""
    user = await _mk_user(db)
    ctx = await _prepare(user, make_cache(selected="0203", model_id="02"))
    bearer = await _verify(deps.gw, ctx["cabinet_url"])

    nocodb_tables["cat01"] = [COLLISION_ROW] + [
        row for row in CATALOG if row["Complectation id"] != "0203"]
    with pytest.raises(ApiError) as e:
        await cabinet_service.get_car_card(bearer)
    assert e.value.status == 404 and e.value.error == "not_found"


@pytest.mark.asyncio
async def test_car_v1_snapshot_without_model_id_unique_id_ok(db, deps):
    """Совместимость: снапшот, замороженный до схемы v2 (кэш без эха
    qualification_context → model_id нет), при уникальном Complectation id
    работает как раньше — деградация к поиску по одной комплектационной
    части."""
    user = await _mk_user(db)
    ctx = await _prepare(user, make_cache(selected="0203", model_id=None))
    snap, _ = await _snapshot(db, user.id)
    assert snap["model_id"] is None

    bearer = await _verify(deps.gw, ctx["cabinet_url"])
    car = await cabinet_service.get_car_card(bearer)
    assert car["specs"]["engine"] == "1.6 л, 106 л.с."


@pytest.mark.asyncio
async def test_car_v1_snapshot_without_model_id_collision_404(db, deps,
                                                              nocodb_tables):
    """Совместимость: старый снапшот без model_id при коллизии
    комплектационного id даёт честный 404 (чужая модель не показывается);
    ближайшая перезаморозка со свежим кэшем добавит model_id."""
    nocodb_tables["cat01"] = [COLLISION_ROW] + list(CATALOG)
    user = await _mk_user(db)
    ctx = await _prepare(user, make_cache(selected="0203", model_id=None))
    bearer = await _verify(deps.gw, ctx["cabinet_url"])
    with pytest.raises(ApiError) as e:
        await cabinet_service.get_car_card(bearer)
    assert e.value.status == 404 and e.value.error == "not_found"


@pytest.mark.asyncio
async def test_car_item_gone_from_catalog_404_not_found(db, deps, nocodb_tables):
    user = await _mk_user(db)
    ctx = await _prepare(user, make_cache(selected="0203"))
    bearer = await _verify(deps.gw, ctx["cabinet_url"])
    nocodb_tables["cat01"] = [row for row in CATALOG
                              if row["Complectation id"] != "0203"]
    with pytest.raises(ApiError) as e:
        await cabinet_service.get_car_card(bearer)
    assert e.value.status == 404 and e.value.error == "not_found"


@pytest.mark.asyncio
async def test_car_without_snapshot_404_no_snapshot(db, deps):
    user = await _mk_user(db)
    ctx = await _prepare(user, None)
    bearer = await _verify(deps.gw, ctx["cabinet_url"])
    with pytest.raises(ApiError) as e:
        await cabinet_service.get_car_card(bearer)
    assert e.value.status == 404 and e.value.error == "no_snapshot"


@pytest.mark.asyncio
async def test_car_without_bearer_401(db, deps):
    with pytest.raises(ApiError) as e:
        await cabinet_service.get_car_card(None)
    assert e.value.status == 401


@pytest.mark.asyncio
async def test_car_route_registered_over_http(client):
    resp = await client.get('/cabinet/api/car')
    assert resp.status == 401, "роут /cabinet/api/car идёт через register_routes"
    assert (await resp.json())["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_cors_preflight_car_echoes_dealer_origin(client):
    """Урок CORS: новый кросс-доменный роут обязан проходить preflight
    (существующая ветка /cabinet/api/* покрывает его автоматически)."""
    resp = await client.options('/cabinet/api/car', headers={
        "Origin": "https://dealer-one.ru",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization",
    })
    assert resp.status == 200
    assert resp.headers.get("Access-Control-Allow-Origin") == "https://dealer-one.ru"
    assert resp.headers.get("Vary") == "Origin"
    assert "authorization" in resp.headers.get("Access-Control-Allow-Headers", "").lower()
    assert "GET" in resp.headers.get("Access-Control-Allow-Methods", "")


# =======================
# Integration: aware_db (боевой PostgreSQL отдаёт aware-время — R20)
# =======================

@pytest.mark.asyncio
async def test_aware_db_freeze_and_read(aware_db, deps):
    user = await _mk_user(aware_db)
    ctx = await _prepare(user, make_cache(selected="0203"))
    snap, frozen = await _snapshot(aware_db, user.id)
    assert snap["item_id"] == "0203"
    assert frozen is not None and frozen.tzinfo is not None

    bearer = await _verify(deps.gw, ctx["cabinet_url"])
    me = await cabinet_service.get_me(bearer)
    assert me["calc"]["frozen_at"].endswith("+00:00")
    car = await cabinet_service.get_car_card(bearer)
    assert car["name"] == "LADA Vesta · Comfort"  # «Model · Complectation» (v3)

    # перезаморозка поверх aware-значения (сравнение aware vs aware)
    assert await cabinet_service.refresh_calc_snapshot(
        user.platform_user_id, make_cache(selected="0204")) is True
    snap2, _ = await _snapshot(aware_db, user.id)
    assert snap2["item_id"] == "0204"


@pytest.mark.asyncio
async def test_car_success_response_carries_cors(db, deps, client):
    """Success-CORS для /cabinet/api/car (регрессия «ACAO только на preflight»):
    успешный ответ карточки обязан нести echo-Origin."""
    user = await _mk_user(db)
    ctx = await _prepare(user, make_cache(selected="0203"))
    bearer = await _verify(deps.gw, ctx["cabinet_url"])
    resp = await client.get('/cabinet/api/car',
                            headers={"Origin": "https://dealer-one.ru",
                                     "Authorization": bearer})
    assert resp.status == 200
    assert resp.headers.get("Access-Control-Allow-Origin") == "https://dealer-one.ru"
    assert "Origin" in resp.headers.get("Vary", "")
