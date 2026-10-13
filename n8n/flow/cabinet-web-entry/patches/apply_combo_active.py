"""Combo Calculator: только строки каталога с Active = да (решение владельца 2026-10-05).

Неактивные комплектации MONJARO дилера 02 («Эксклюзив», «Флагман СЕ») попадали
в комбо-расчёт. Та же функция isActiveRow, что в Car Search (apply_stock.py).
Выход (идемпотентен):
  ../stock-slug-active/combo_prepare.n4n5.js — N4 + N5 + Active ([TEST]; бой — с релизом веб-входа)
  ../stock-slug-active/combo_prepare.n5.js   — N5 + Active (бой, по команде)
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENTRY = HERE.parent
OLD = "const catalogItems = getSafeAll('Get Catalog', 'Model');\n"
NEW = """// [STOCK] Каталог — только активные строки (решение владельца 2026-10-05): неактивные
// комплектации («Эксклюзив», «Флагман СЕ» у MONJARO дилера 02) попадали в комбо-расчёт.
// Как в Car Search (car_search_prepare.js).
function isActiveRow(t) {
    const a = t.Active;
    return a === true || a === 1 || a === "1" || a === "true";
}
const catalogItems = getSafeAll('Get Catalog', 'Model').filter(isActiveRow);
"""


def patch(text):
    assert text.count(OLD) == 1, "строка загрузки каталога не найдена или не одна"
    return text.replace(OLD, NEW)


if __name__ == "__main__":
    src = ENTRY / "n5-combo-price-old"
    out = ENTRY / "stock-slug-active"
    (out / "combo_prepare.n4n5.js").write_text(patch((src / "combo_prepare.js").read_text(encoding="utf-8")), encoding="utf-8")
    (out / "combo_prepare.n5.js").write_text(patch((src / "combo_prepare.n5-only.js").read_text(encoding="utf-8")), encoding="utf-8")
    print("Active -> stock-slug-active/combo_prepare.n4n5.js, combo_prepare.n5.js")
