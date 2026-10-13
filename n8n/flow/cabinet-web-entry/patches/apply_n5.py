"""N5: комбо-цена от Price old — та же база, что у кредита и трейд-ина в Car Search.

Пишет в ../n5-combo-price-old/:
  combo_prepare.js          — N4 + N5 (на случай общего выката с веб-входом);
  combo_prepare.n5-only.js  — только N5 поверх боевого кода.
Идемпотентен. Запуск: python3 patches/apply_n5.py (после apply_n4.py не обязателен:
N4 применяется здесь же).
"""
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "n5-combo-price-old"

spec = importlib.util.spec_from_file_location("apply_n4", HERE / "apply_n4.py")
n4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(n4)

OLD_PRICE = """        const basePrice = round10k(Number(trim['Price new']));
        const creditBonus = round10k(Number(trim['Credit bonus']) || 0);
        const tradeinBonus = round10k(Number(trim['Trade-in bonus']) || 0);
"""
NEW_PRICE = """        // [N5] Комбо-цена считается от той же базы, что кредит и трейд-ин в
        // Car Search (car_search_prepare.js: MSRP = Price old, base_price =
        // MSRP − Cash in hook). До правки базой была колонка Price new — в
        // шаблоне бота это «цена с максимальной скидкой» (main.py:2180), —
        // и цена комбо не сходилась с credit_price той же комплектации.
        const msrp = round10k(Number(trim['Price old']) || 0);
        const cashBonus = round10k(Number(trim['Cash in hook']) || 0);
        const basePrice = msrp - cashBonus;
        const creditBonus = round10k(Number(trim['Credit bonus']) || 0);
        const tradeinBonus = round10k(Number(trim['Trade-in bonus']) || 0);
"""
OLD_OBJ = """            price: {
                MSRP: basePrice,
                credit_bonus: creditBonus,
"""
NEW_OBJ = """            price: {
                MSRP: msrp,
                cash_bonus: cashBonus,
                base_price: basePrice,
                credit_bonus: creditBonus,
"""


def patch(text: str) -> str:
    for old in (OLD_PRICE, OLD_OBJ):
        assert text.count(old) == 1, old[:60]
    return text.replace(OLD_PRICE, NEW_PRICE).replace(OLD_OBJ, NEW_OBJ)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    live = (HERE.parent / "live" / "combo_prepare.pre-n5.js").read_text(encoding="utf-8")  # бой до N5
    (OUT / "combo_prepare.js").write_text(patch(n4.patch(live)), encoding="utf-8")
    (OUT / "combo_prepare.n5-only.js").write_text(patch(live), encoding="utf-8")
    print("N5 ->", OUT.name, "(combo_prepare.js = N4+N5, combo_prepare.n5-only.js)")
