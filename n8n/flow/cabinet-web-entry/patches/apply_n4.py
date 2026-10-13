"""N4: выбор программы кредитования в Prepare-нодах Car Search / Combo.

Берёт боевой код из flow/lib (побайтово равен живым нодам на 2026-09-30) и
заменяет одну строку выбора программы. Результат пишет в ../n4-min-down-payment/.
Идемпотентен. Запуск: python3 patches/apply_n4.py
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "n4-min-down-payment"

OLD = "const program = getSafeFirst('Get Program', 'Program_Name');\n"
NEW = """// [N4] Программа кредитования дилера. До правки программа выбиралась
// проверкой колонки Program_Name, которой нет ни в одной таблице
// [brand]_Credit_Programs, — найденная строка превращалась в null, и движок
// брал умолчания 84 мес. / 20 %.
//  - признак строки программы — Program_id;
//  - Model_Pattern — список Model id («11, 12, 13»); программа применяется,
//    только если Model id модели стоит в нём ОТДЕЛЬНЫМ элементом (решение
//    владельца 2026-09-30). Прежний фильтр сравнивал с Id строки каталога
//    подстрокой: Id 1 совпадал с «11», модель из списка с первой строкой Id 6
//    программу не получала;
//  - минимальный взнос — ТОЛЬКО Min_Down_Payment, целые проценты (10 = 10 %),
//    во всех трёх таблицах (HAVAL, GEELY, BELGEE — срез 2026-09-30). Пустое
//    значение → 20 %, как в боте (решение владельца 2026-09-30). Движок
//    читает поле Min_Down_Payment_Pct и сам пустое заменил бы на 10 %
//    (calc_engine.js:53), поэтому 20 подставляется здесь явно.
const MIN_DOWN_DEFAULT_PCT = 20;
function pickProgram(rows, modelId) {
    const id = String(modelId == null ? "" : modelId).trim();
    if (!id) return null;
    const row = rows.find(r => r && r.Program_id != null && r.Program_id !== "" &&
        String(r.Model_Pattern || "").split(",").map(s => s.trim()).includes(id));
    if (!row) return null;
    const raw = row.Min_Down_Payment;
    const pct = (raw === undefined || raw === null || String(raw).trim() === "" || isNaN(Number(raw)))
        ? MIN_DOWN_DEFAULT_PCT : Number(raw);
    return Object.assign({}, row, { Min_Down_Payment_Pct: pct });
}
const program = pickProgram(getSafeAll('Get Program', 'Program_id'),
                            catalogItems.length ? catalogItems[0]['Model id'] : null);
"""


def patch(text: str) -> str:
    assert text.count(OLD) == 1, "строка выбора программы не найдена или не одна"
    return text.replace(OLD, NEW)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name in ("car_search_prepare.js", "combo_prepare.js"):
        # базы — замороженный боевой код до N4 (flow/lib с 2026-10-09 = бой с N4/N5)
        base = (HERE.parent / "live" / "car_search_prepare.prod-live.js") if name == "car_search_prepare.js" else (HERE.parent / "live" / "combo_prepare.pre-n5.js")
        (OUT / name).write_text(patch(base.read_text(encoding="utf-8")), encoding="utf-8")
        print("N4 ->", OUT.name + "/" + name)
