"""Сток в Car Search (решение владельца 2026-10-05, диагностика дилера 02).

Правки кода Prepare Data:
  1. каталог — только строки с Active = да;
  2. машина из стока привязывается к комплектации по ТОЧНОМУ совпадению slug
     каталога и slug стока; старое сравнение по названию — запасной вариант
     только для комплектаций, у которых в каталоге нет slug.
(Третья правка — executeOnce у ноды Get Stock — параметр ноды, не код.)

Выход:
  flow/lib/car_search_prepare.js                          = боевой код (live/…prod-live.js) + сток
  ../stock-slug-active/car_search_prepare.n4.js           = N4 + сток ([TEST] сейчас, бой — с релизом веб-входа)
Идемпотентен. Запуск: python3 patches/apply_stock.py
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENTRY = HERE.parent

OLD_CATALOG = "const catalogItems = getSafeAll('Get Catalog', 'Model');\n"
NEW_CATALOG = """// [STOCK] Каталог — только активные строки (решение владельца 2026-10-05):
// неактивные комплектации («Эксклюзив», «Флагман СЕ» у MONJARO дилера 02)
// попадали в список, который видит Александр.
function isActiveRow(t) {
    const a = t.Active;
    return a === true || a === 1 || a === "1" || a === "true";
}
const catalogItems = getSafeAll('Get Catalog', 'Model').filter(isActiveRow);
"""

OLD_LOOP_HEAD = """const grouped = {};
const calcItems = [];
"""
NEW_LOOP_HEAD = """const grouped = {};
const calcItems = [];
// [STOCK] slug каталога → комплектация: по нему привязывается сток
const slugToTrim = {};
const trimsWithSlug = new Set();
"""

OLD_TRIM_ID = """    const trimId = (trim['Complectation id'] || trim['Complectation_id'] || trim.Id || "").toString();
"""
NEW_TRIM_ID = """    const trimId = (trim['Complectation id'] || trim['Complectation_id'] || trim.Id || "").toString();
    const trimSlug = String(trim.slug || "").trim().toLowerCase();
    if (trimSlug) {
        if (!(trimSlug in slugToTrim)) slugToTrim[trimSlug] = trimId;
        trimsWithSlug.add(trimId);
    }
"""

OLD_MATCH = """        const carSlug = (car.slug || "").toLowerCase();
        if (!carSlug) continue;

        const matchedTrimKey = Object.keys(grouped).find(key => {
            const trimData = grouped[key];
            let searchKey = trimData.name.toLowerCase();
            searchKey = searchKey.replace('+', '-plus').replace(' ', '-');
            return carSlug.includes(searchKey);
        });
"""
NEW_MATCH = """        const carSlug = String(car.slug || "").trim().toLowerCase();
        if (!carSlug) continue;

        // [STOCK] Привязка по точному совпадению slug каталога и slug стока.
        // Прежнее сравнение по названию комплектации не работало для брендов с
        // кириллическими названиями («флагман» ⊄ «flagship-se-2-0t-…», дилер 02)
        // и путало одноимённые комплектации (Elite 2WD / Elite 4WD у HAVAL).
        // Оно оставлено запасным вариантом ТОЛЬКО для комплектаций без slug.
        let matchedTrimKey = slugToTrim[carSlug];
        if (!matchedTrimKey) {
            matchedTrimKey = Object.keys(grouped).find(key => {
                if (trimsWithSlug.has(key)) return false;
                const trimData = grouped[key];
                let searchKey = trimData.name.toLowerCase();
                searchKey = searchKey.replace('+', '-plus').replace(' ', '-');
                return carSlug.includes(searchKey);
            });
        }
"""


def patch(text: str) -> str:
    for old in (OLD_CATALOG, OLD_LOOP_HEAD, OLD_TRIM_ID, OLD_MATCH):
        assert text.count(old) == 1, f"фрагмент не найден или не один: {old[:60]!r}"
    return (text.replace(OLD_CATALOG, NEW_CATALOG).replace(OLD_LOOP_HEAD, NEW_LOOP_HEAD)
                .replace(OLD_TRIM_ID, NEW_TRIM_ID).replace(OLD_MATCH, NEW_MATCH))


if __name__ == "__main__":
    out = ENTRY / "stock-slug-active"
    out.mkdir(exist_ok=True)
    prod_live = (ENTRY / "live" / "car_search_prepare.prod-live.js").read_text(encoding="utf-8")
    # до 2026-10-09 писал в flow/lib; теперь flow/lib = боевой код (active-field), а
    # состояние «бой + сток» заморожено здесь — повторный запуск не откатит lib
    (ENTRY / "live" / "car_search_prepare.stock.js").write_text(patch(prod_live), encoding="utf-8")
    n4 = (ENTRY / "n4-min-down-payment" / "car_search_prepare.js").read_text(encoding="utf-8")
    (out / "car_search_prepare.n4.js").write_text(patch(n4), encoding="utf-8")
    print("сток -> live/car_search_prepare.stock.js (бой 10-05), stock-slug-active/car_search_prepare.n4.js (N4)")
