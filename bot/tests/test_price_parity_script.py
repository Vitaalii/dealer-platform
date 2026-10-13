"""U4 (план 2026-09-30-001): скрипт паритета цен бота с эталоном n8n (R48)."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "check_price_parity", Path(__file__).resolve().parent.parent / "scripts" / "check_price_parity.py")
parity = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(parity)

ROWS = [
    {"Model id": "15", "Complectation id": "12", "Model": "ATLAS PRO", "Complectation": "Люкс",
     "Price old": 3026990, "Cash in hook": 80000, "Credit bonus": 200000, "Trade-in bonus": 250000},
    {"Model id": "16", "Complectation id": "15", "Model": "ATLAS", "Complectation": "Флагман",
     "Price old": 4119990, "Trade-in bonus": 90000},
    {"Model id": "99", "Complectation id": "01", "Model": "Без цены", "Price old": None},
]


def _ref(**overrides):
    positions = [
        {"model_id": "15", "complectation_id": "12", "credit_price": 2750000, "combo_price": 2500000},
        {"model_id": "16", "complectation_id": "15", "credit_price": 4120000, "combo_price": 4030000},
    ]
    for pos in positions:
        pos.update(overrides.get(pos["model_id"], {}))
    return {"dealer_code": "02", "positions": positions}


def test_matching_reference_is_parity():
    report = parity.compare(ROWS, _ref())
    assert parity.is_ok(report)
    assert report["checked"] == 2
    assert report["skipped_unpriced"] == [("99", "01")]


def test_mismatch_by_10k_fails_with_position_in_report():
    report = parity.compare(ROWS, _ref(**{"15": {"credit_price": 2760000}}))
    assert not parity.is_ok(report)
    assert report["mismatches"] == [{
        "position": ("15", "12"), "field": "credit_price", "bot": 2750000,
        "n8n": 2760000, "model": "ATLAS PRO", "complectation": "Люкс"}]


def test_position_without_reference_fails():
    ref = _ref()
    ref["positions"] = ref["positions"][:1]
    report = parity.compare(ROWS, ref)
    assert not parity.is_ok(report)
    assert report["missing_reference"] == [("16", "15")]


def test_only_credit_ignores_combo_until_n8n_fixed():
    # Нода Combo Calculator считает от «Price new» — до правки комбо расходится
    ref = _ref(**{"15": {"combo_price": 2020000}})
    assert not parity.is_ok(parity.compare(ROWS, ref))
    assert parity.is_ok(parity.compare(ROWS, ref, only="credit"))


def test_ids_compared_as_trimmed_strings():
    ref = _ref()
    ref["positions"][0].update(model_id=15, complectation_id=" 12 ")
    assert parity.is_ok(parity.compare(ROWS, ref))


def test_reference_rows_absent_in_catalog_are_reported_not_failed():
    ref = _ref()
    ref["positions"].append({"model_id": "77", "complectation_id": "77",
                             "credit_price": 1, "combo_price": 1})
    report = parity.compare(ROWS, ref)
    assert parity.is_ok(report) and report["extra_reference"] == [("77", "77")]


def test_duplicate_pair_compares_first_row_only():
    # Как у Geely 02/09: пара (18, 13) повторена — бот и n8n берут первую строку
    dup = {"Id": 20, "Model id": "15", "Complectation id": "12", "Model": "ATLAS PRO",
           "Complectation": "Люкс СЕ", "Price old": 3026990, "Cash in hook": 500000}
    report = parity.compare(ROWS + [dup], _ref())
    assert parity.is_ok(report) and report["checked"] == 2
    assert report["duplicates"] == [{"position": ("15", "12"), "id": 20, "complectation": "Люкс СЕ"}]


def test_duplicate_first_row_mismatch_still_fails():
    dup = {"Id": 20, "Model id": "15", "Complectation id": "12", "Price old": 3026990,
           "Cash in hook": 80000, "Credit bonus": 200000}
    report = parity.compare(ROWS + [dup], _ref(**{"15": {"credit_price": 2740000}}))
    assert not parity.is_ok(report)
    assert [m["field"] for m in report["mismatches"]] == ["credit_price"]


def test_rows_without_model_id_are_skipped_not_failed():
    no_id = {"Model id": None, "Complectation id": "", "Model": "GEELY", "Price old": 2000000}
    report = parity.compare(ROWS + [no_id, dict(no_id, **{"Model id": " "})], _ref())
    assert parity.is_ok(report) and report["skipped_no_model_id"] == 2
    assert report["missing_reference"] == []


def test_active_rows_filter_by_dealer_column():
    """3.4.1, --active-only: только позиции, где взведена колонка дилера."""
    rows = [dict(ROWS[0], Active=True, Active_1=True), dict(ROWS[1], Active=True, Active_1=False),
            dict(ROWS[2], Active=False)]
    assert [r["Model id"] for r in parity.active_rows(rows, "Active_1")] == ["15"]
    assert [r["Model id"] for r in parity.active_rows(rows, "Active")] == ["15", "16"]
    report = parity.compare(parity.active_rows(rows, "Active_1"), _ref())
    assert parity.is_ok(report) and report["extra_reference"] == [("16", "15")]


def test_parity_pages_are_small():
    assert parity.PAGE <= 25 and parity.PAGE_PAUSE > 0
