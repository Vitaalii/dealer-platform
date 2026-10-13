// ============================================================
// N4: программа кредитования в Car Search / Combo Calculator.
// Сравнивает боевой код (flow/lib/*_prepare.js — побайтово равен живым
// нодам 2026-09-30) с подготовленной правкой в этой папке, прогоняя обе
// версии через Calc Engine (flow/lib/calc_engine.js).
// Фикстуры — живые строки NocoDB (срез 2026-09-30) и строки каталога GEELY
// MONJARO из исполнения 5022.
// Запуск: node --test flow/cabinet-web-entry/n4-min-down-payment/*.test.mjs
// ============================================================
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const engine = require("../../lib/calc_engine.js");
const src = p => readFileSync(new URL(p, import.meta.url), "utf-8");
const CODE = {
    // live — код боя до правки стока (2026-10-05 flow/lib ушёл вперёд: lib = бой + сток)
    car: { live: src("../live/car_search_prepare.prod-live.js"), n4: src("./car_search_prepare.js") },
    combo: { live: src("../live/combo_prepare.pre-n5.js"), n4: src("./combo_prepare.js") },
};

// --- живые строки таблиц программ (срез 2026-09-30) ---
const PROGRAM = {
    GEELY: { Program_id: "4201", Bank_name: "ВТБ", Bank_id: "42", Model_Pattern: "12, 18, 14, 13, 19, 20, 11",
             Min_Down_Payment: 10, Max_Term_Months: 84, Base_Rate: null },
    BELGEE: { Program_id: "5101", Bank_name: "Совкомбанк", Bank_id: "51", Model_Pattern: "13, 14,15",
              Min_Down_Payment: 12, Max_Term_Months: 84, Base_Rate: null },
    HAVAL: { Id: 1, Program_id: "0101", Bank_name: "VTB", Model_Pattern: "11, 12, 13, 14, 19, 20, 21",
             Min_Down_Payment: 10, Max_Term_Months: 84, Base_Rate: null },   // колонка переименована владельцем 2026-09-30
};
// строки каталога MONJARO (Model id 18), цены из исполнения 5022
const MONJARO = [
    { Id: 2, Model: "MONJARO", "Model id": "18", "Complectation id": "13", Complectation: "Эксклюзив",
      "Price old": 5052190, "Cash in hook": 40000, "Price new": 4502190, "Trade-in bonus": 250000 },
    { Id: 16, Model: "MONJARO", "Model id": "18", "Complectation id": "11", Complectation: "Люкс",
      "Price old": 4599990, "Price new": 4409990, "Trade-in bonus": 190000 },
];
const RATES = [
    { Term_From: 12, Term_To: 84, Downpayment_From: 0, Downpayment_To: 100, Rate: 16.5 },
];
const withModel = (rows, modelId) => rows.map(r => ({ ...r, "Model id": modelId }));

function run(code, refs) {
    const $ = name => {
        if (!(name in refs)) throw new Error(`нет мока для ноды ${name}`);
        const v = refs[name];
        return { all: () => (Array.isArray(v) ? v : [v]).map(j => ({ json: j })),
                 first: () => ({ json: Array.isArray(v) ? v[0] : v }) };
    };
    return new Function("$", "$input", code)($, { first: () => ({ json: {} }) }).json;
}
function carRefs(program, catalog = MONJARO, payment = 0) {
    return {
        "Execute Workflow Trigger": { qualification_context: { model: "MONJARO", model_id: catalog[0]["Model id"],
            dealer_id: "99", request_type: "credit", payment } },
        "Get Catalog": catalog, "Get Stock": [{}], "Get Rates": RATES,
        "Get Program": program === undefined ? [{}] : [program],   // alwaysOutputData: пустой item
    };
}
const calc = prep => engine.runCalc(prep.calc_request);

test("живой код: программа GEELY отбрасывается — умолчания 84 мес. / 20 %", () => {
    const prep = run(CODE.car.live, carRefs(PROGRAM.GEELY));
    assert.equal(prep.calc_request.program, null);
    const std = calc(prep).results["11"].scenarios.standard;
    assert.equal(std.down_payment, 920000);            // 20 % от 4 600 000
});

test("N4 GEELY: Min_Down_Payment 10 → взнос 10 %, срок 84", () => {
    const prep = run(CODE.car.n4, carRefs(PROGRAM.GEELY));
    assert.equal(prep.calc_request.program.Min_Down_Payment_Pct, 10);
    const std = calc(prep).results["11"].scenarios.standard;
    assert.equal(std.down_payment, 460000);            // 10 % от 4 600 000
    assert.equal(std.term, 84);
});

test("N4 BELGEE: Min_Down_Payment 12 → 12 %; модель вне Model_Pattern — умолчания", () => {
    const s50 = withModel(MONJARO, "13");
    let prep = run(CODE.car.n4, carRefs(PROGRAM.BELGEE, s50));
    assert.equal(prep.calc_request.program.Min_Down_Payment_Pct, 12);
    assert.equal(calc(prep).results["11"].scenarios.standard.down_payment, 550000);   // round10k(552 000)
    prep = run(CODE.car.n4, carRefs(PROGRAM.BELGEE, withModel(MONJARO, "11")));
    assert.equal(prep.calc_request.program, null);
    assert.equal(calc(prep).results["11"].scenarios.standard.down_payment, 920000);
});

test("N4 HAVAL: Min_Down_Payment после переименования читается", () => {
    const prep = run(CODE.car.n4, carRefs(PROGRAM.HAVAL, withModel(MONJARO, "11")));
    assert.equal(prep.calc_request.program.Min_Down_Payment_Pct, 10);
});

test("N4: старая колонка Min_Down_Payment_Pct не читается — только Min_Down_Payment", () => {
    const legacy = { Program_id: "0101", Model_Pattern: "18", Min_Down_Payment_Pct: 10, Max_Term_Months: 84 };
    const prep = run(CODE.car.n4, carRefs(legacy));
    assert.equal(prep.calc_request.program.Min_Down_Payment_Pct, 20);
});

test("N4: Model id совпадает с элементом списка целиком, не подстрокой", () => {
    for (const mid of ["1", "2", "8", "118"]) {
        const prep = run(CODE.car.n4, carRefs(PROGRAM.GEELY, withModel(MONJARO, mid)));
        assert.equal(prep.calc_request.program, null, `Model id ${mid}`);
    }
});

test("N4: нет строки программы / пустой Min_Down_Payment → 20 %", () => {
    assert.equal(run(CODE.car.n4, carRefs(undefined)).calc_request.program, null);
    for (const empty of [null, "", "  ", undefined, "abc"]) {
        const prep = run(CODE.car.n4, carRefs({ ...PROGRAM.GEELY, Min_Down_Payment: empty }));
        assert.equal(prep.calc_request.program.Min_Down_Payment_Pct, 20, String(empty));
        assert.equal(calc(prep).results["11"].scenarios.standard.down_payment, 920000);
    }
    // строкой из NocoDB тоже читается
    const prep = run(CODE.car.n4, carRefs({ ...PROGRAM.GEELY, Min_Down_Payment: "12" }));
    assert.equal(prep.calc_request.program.Min_Down_Payment_Pct, 12);
});

test("N4: без программы вывод побайтово равен живому (Car Search и Combo)", () => {
    const refs = carRefs(PROGRAM.GEELY, withModel(MONJARO, "99"), 60000);
    assert.deepEqual(run(CODE.car.n4, refs), run(CODE.car.live, refs));
    const combo = { ...refs, "Parse Input": {}, "Execute Workflow": { status: "SUCCESS", tradein_price_to: 830000 } };
    assert.deepEqual(run(CODE.combo.n4, combo), run(CODE.combo.live, combo));
});

test("N4 Combo: программа доезжает до комбо-расчёта", () => {
    const refs = { ...carRefs(PROGRAM.GEELY, MONJARO, 60000), "Parse Input": {},
                   "Execute Workflow": { status: "SUCCESS", tradein_price_to: 830000 } };
    const prep = run(CODE.combo.n4, refs);
    assert.equal(prep.calc_request.program.Min_Down_Payment_Pct, 10);
    assert.equal(prep.calc_request.program.Max_Term_Months, 84);
    assert.equal(run(CODE.combo.live, refs).calc_request.program, null);
});

test("N4: правка — только выбор программы, остальной код нод не тронут", () => {
    for (const k of ["car", "combo"]) {
        const strip = s => s.replace(/\/\/ \[N4\][\s\S]*?const program = pickProgram\([^;]*;\n/, "")
                            .replace("const program = getSafeFirst('Get Program', 'Program_Name');\n", "");
        assert.equal(strip(CODE[k].n4), strip(CODE[k].live), k);
    }
});
