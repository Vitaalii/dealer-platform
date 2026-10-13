"""Веб-вход (план 2026-09-30-001, U2): цены позиции каталога (R17) и
минимальный первый взнос из Min_Down_Payment (R21)."""
import pytest

import cabinet_service
from cabinet_service import compute_prices, round10k

# Живые строки каталога (NocoDB, 2026-09-30): Geely и тестовый дилер 99
GEELY_ATLAS_PRO = {"Model id": "15", "Complectation id": "12",
                   "Price old": 3026990, "Price new": 2466990,
                   "Cash in hook": 80000, "Credit bonus": 200000,
                   "Trade-in bonus": 250000}
TEST_JOLION = {"Model id": "14", "Complectation id": "16",
               "Price old": 2949000, "Price new": 2849000,
               "Cash in hook": 200000, "Credit bonus": None,
               "Trade-in bonus": None}


def test_round10k_half_up_like_js_math_round():
    assert round10k(3026990) == 3030000
    assert round10k(3025000) == 3030000     # ровно половина — вверх
    assert round10k(3024999) == 3020000
    assert round10k(0) == 0


def test_credit_and_combo_price_from_price_old():
    assert compute_prices(GEELY_ATLAS_PRO) == {"credit_price": 2750000,
                                               "combo_price": 2500000}


def test_price_new_does_not_affect_result():
    changed = dict(GEELY_ATLAS_PRO, **{"Price new": 1})
    assert compute_prices(changed) == compute_prices(GEELY_ATLAS_PRO)


def test_missing_discounts_count_as_zero():
    assert compute_prices(TEST_JOLION) == {"credit_price": 2750000,
                                           "combo_price": 2750000}


def test_string_values_with_spaces_parsed():
    row = {"Price old": "2 149 000", "Cash in hook": "100 000",
           "Credit bonus": "", "Trade-in bonus": "50 000"}
    assert compute_prices(row) == {"credit_price": 2050000, "combo_price": 2000000}


@pytest.mark.parametrize("price_old", [None, "", "нет", 0, -5])
def test_invalid_base_price_makes_position_unavailable(price_old):
    assert compute_prices({"Price old": price_old}) is None


def test_non_positive_result_makes_position_unavailable():
    assert compute_prices({"Price old": 100000, "Cash in hook": 100000}) is None
    assert compute_prices({"Price old": 300000, "Trade-in bonus": 300000}) is None


# ---- Минимальный взнос (R21) через _match_program ----

DEALERS = {
    "01": {"code": "01", "website": "https://dealer-one.ru/",
           "db_config": {"catalog_table_id": "cat01", "program_table_id": "prog01"}},
    "04": {"code": "04", "website": "https://dealer-four.ru/",
           "db_config": {"catalog_table_id": "cat01"}},
}
CATALOG = [{"Id": 7, "Model id": "02", "Complectation id": "0203"}]


@pytest.fixture
def dealers():
    return DEALERS


@pytest.fixture
def nocodb_tables(request):
    programs = getattr(request, "param", [])
    return {"cat01": CATALOG, "prog01": programs}


@pytest.mark.asyncio
@pytest.mark.parametrize("nocodb_tables", [[{"Model_Pattern": "02", "Max_Term_Months": 84,
                                             "Min_Down_Payment": 12}]], indirect=True)
async def test_min_down_payment_read_from_new_column(deps):
    program = await cabinet_service._match_program("0203", "01", "02")
    assert program["Min_Down_Payment"] == 12
    assert program["Min_Down_Payment_Pct"] == 12      # дубль для движка
    assert deps.tech_alerts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("nocodb_tables", [[{"Model_Pattern": "02", "Max_Term_Months": 84,
                                             "Min_Down_Payment_Pct": 30}],
                                           [{"Model_Pattern": "02", "Max_Term_Months": 84,
                                             "Min_Down_Payment": ""}]], indirect=True)
async def test_missing_or_empty_column_defaults_to_20_with_one_alert(deps):
    first = await cabinet_service._match_program("0203", "01", "02")
    second = await cabinet_service._match_program("0203", "01", "02")
    assert first["Min_Down_Payment"] == 20 and second["Min_Down_Payment"] == 20
    assert len(deps.tech_alerts) == 1                  # дедуп: раз в сутки
    assert "01" in deps.tech_alerts[0] and "20 %" in deps.tech_alerts[0]


@pytest.mark.asyncio
async def test_dealer_without_program_table_alerts_once(deps):
    assert await cabinet_service._match_program("0203", "04", "02") is None
    assert await cabinet_service._match_program("0203", "04", "02") is None
    assert len(deps.tech_alerts) == 1 and "нет таблицы программ" in deps.tech_alerts[0]


# ---- Выбор программы по списку Model id (поправка 2026-09-30) ----

PROGRAM_TABLE = [
    {"Model_Pattern": "12, 18, 14, 13, 19, 20, 11", "Program_Name": "Geely/Haval",
     "Max_Term_Months": 84, "Min_Down_Payment": 10},
    {"Model_Pattern": "13, 14,15", "Program_Name": "Belgee", "Max_Term_Months": 60,
     "Min_Down_Payment": 12},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("nocodb_tables", [PROGRAM_TABLE], indirect=True)
@pytest.mark.parametrize("model_id,expected", [
    ("15", "Belgee"),   # «15» есть только во второй строке («14,15» без пробела)
    ("1", None),        # подстрока «1» в «12, 18, …» — НЕ совпадение (была ошибка)
    ("5", None),        # подстрока «5» в «14,15» — НЕ совпадение
    ("14", "Geely/Haval"),  # первая подходящая строка
])
async def test_program_matched_by_exact_model_id(deps, model_id, expected):
    program = await cabinet_service._match_program("0203", "01", model_id)
    assert (program or {}).get("Program_Name") == expected


def test_model_pattern_ids_split():
    assert cabinet_service._model_pattern_ids("13, 14,15") == {"13", "14", "15"}
    assert cabinet_service._model_pattern_ids("5|7|11") == {"5", "7", "11"}
    assert cabinet_service._model_pattern_ids(None) == set()
