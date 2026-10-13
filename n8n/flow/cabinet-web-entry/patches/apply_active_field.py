"""Колонка активности каталога — настройка дилера (решение владельца 2026-10-06).

Бот 3.4.1 отдаёт имя колонки в data_routing.catalog_active_field (из карточки
дилера): у дилера 02 (Geely «Радар») — "Active_1", у остальных — "Active".
Каталог Geely общий для 02 и 09, наборы моделей разные.
Правила:
  - допустимо только Active / Active_<число>, иначе — "Active";
  - колонки нет в таблице дилера вовсе — "Active" (прежнее поведение, не пустой каталог);
  - ключа нет (бот до 3.4.1) — "Active": правка совместима с текущим ботом;
  - в выводе summary.catalog_active_field — какая колонка применена.

База — ТЕКУЩИЕ боевые файлы (перенос N4/N5 в flow/lib ещё не сделан):
  ../stock-slug-active/car_search_prepare.n4.js → ../active-field/car_search_prepare.js
  ../stock-slug-active/combo_prepare.n4n5.js    → ../active-field/combo_prepare.js
Идемпотентен. Запуск: python3 patches/apply_active_field.py
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENTRY = HERE.parent

OLD_FILTER = """function isActiveRow(t) {
    const a = t.Active;
    return a === true || a === 1 || a === "1" || a === "true";
}
const catalogItems = getSafeAll('Get Catalog', 'Model').filter(isActiveRow);
"""


def new_filter(routing_expr: str) -> str:
    return f"""// [ACTIVE-FIELD] Имя колонки активности — настройка дилера (бот 3.4.1,
// data_routing.catalog_active_field; решение владельца 2026-10-06): у дилера 02
// «Active_1», у остальных «Active». Недопустимое имя или нет ключа — «Active»;
// колонки нет в таблице вовсе — тоже «Active» (прежнее поведение, а не пустой каталог).
function catalogRouting() {{
    try {{
        let r = {routing_expr};
        if (typeof r === "string") r = JSON.parse(r.trim().replace(/^=/, ""));
        return (r && typeof r === "object") ? r : {{}};
    }} catch (e) {{ return {{}}; }}
}}
const ACTIVE_FIELD_RE = /^Active(_\\d{{1,3}})?$/;
const requestedActiveField = String(catalogRouting().catalog_active_field || "").trim();
const rawCatalogItems = getSafeAll('Get Catalog', 'Model');
let ACTIVE_FIELD = ACTIVE_FIELD_RE.test(requestedActiveField) ? requestedActiveField : "Active";
if (ACTIVE_FIELD !== "Active" && rawCatalogItems.length
        && !rawCatalogItems.some(t => Object.prototype.hasOwnProperty.call(t, ACTIVE_FIELD))) {{
    ACTIVE_FIELD = "Active";
}}
function isActiveRow(t) {{
    const a = t[ACTIVE_FIELD];
    return a === true || a === 1 || a === "1" || a === "true";
}}
const catalogItems = rawCatalogItems.filter(isActiveRow);
"""


OLD_SUMMARY = """                model_name: catalogItems[0].Model
            }"""
NEW_SUMMARY = """                model_name: catalogItems[0].Model,
                catalog_active_field: ACTIVE_FIELD
            }"""

CAR = {
    "routing": "triggerData.data_routing",
    "old_empty": """                complectations: []
            },
            calc_request: { operation: "noop" }""",
    "new_empty": """                complectations: [],
                summary: { catalog_active_field: ACTIVE_FIELD }
            },
            calc_request: { operation: "noop" }""",
}
COMBO = {
    "routing": "$('Parse Input').first().json.data_routing",
    "old_empty": """                    message: `По запросу "${query}" модели не найдены в каталоге.`
                }
            },""",
    "new_empty": """                    message: `По запросу "${query}" модели не найдены в каталоге.`
                },
                summary: { catalog_active_field: ACTIVE_FIELD }
            },""",
}


def patch(text: str, cfg: dict) -> str:
    for old in (OLD_FILTER, OLD_SUMMARY, cfg["old_empty"]):
        assert text.count(old) == 1, f"фрагмент не найден или не один: {old[:60]!r}"
    return (text.replace(OLD_FILTER, new_filter(cfg["routing"]))
                .replace(OLD_SUMMARY, NEW_SUMMARY)
                .replace(cfg["old_empty"], cfg["new_empty"]))


if __name__ == "__main__":
    src, out = ENTRY / "stock-slug-active", ENTRY / "active-field"
    out.mkdir(exist_ok=True)
    (out / "car_search_prepare.js").write_text(
        patch((src / "car_search_prepare.n4.js").read_text(encoding="utf-8"), CAR), encoding="utf-8")
    (out / "combo_prepare.js").write_text(
        patch((src / "combo_prepare.n4n5.js").read_text(encoding="utf-8"), COMBO), encoding="utf-8")
    print("active-field -> active-field/car_search_prepare.js, active-field/combo_prepare.js")
