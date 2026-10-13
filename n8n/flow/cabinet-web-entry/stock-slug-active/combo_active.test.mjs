// ============================================================
// Combo Calculator: фильтр Active (2026-10-05).
// n4n5 — ../n5-combo-price-old/combo_prepare.js ([TEST] до правки);
// n4n5a — ./combo_prepare.n4n5.js; n5 / n5a — то же для боя (без N4).
// Каталог — живые строки MONJARO (fixtures_2026-10-05.json), эталон — N2.
// Запуск: node --test flow/cabinet-web-entry/stock-slug-active/*.test.mjs
// ============================================================
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const engine = require("../../lib/calc_engine.js");
const src = p => readFileSync(new URL(p, import.meta.url), "utf-8");
const CODE = { n4n5: src("../n5-combo-price-old/combo_prepare.js"), n4n5a: src("./combo_prepare.n4n5.js"),
               n5: src("../n5-combo-price-old/combo_prepare.n5-only.js"), n5a: src("./combo_prepare.n5.js") };
const FX = JSON.parse(src("./fixtures_2026-10-05.json"));
const REF = JSON.parse(src("../n2-price-reference/price_reference.json"));
const PROGRAM = { Program_id: "4201", Model_Pattern: "12, 18, 14, 13, 19, 20, 11", Min_Down_Payment: 10, Max_Term_Months: 84 };
const RATES = [{ Term_From: 12, Term_To: 84, Downpayment_From: 0, Downpayment_To: 100, Rate: 16.5 }];

function run(code) {
    const refs = {
        "Execute Workflow Trigger": { qualification_context: { model: "MONJARO", model_id: "18", dealer_id: "radar",
                                                               request_type: "combo", payment: 50000 } },
        "Parse Input": {}, "Execute Workflow": { status: "SUCCESS", tradein_price_to: 2890000 },
        "Get Catalog": FX.geely_catalog, "Get Stock": [{}], "Get Rates": RATES, "Get Program": [PROGRAM],
    };
    const $ = n => ({ all: () => [].concat(refs[n]).map(j => ({ json: j })), first: () => ({ json: [].concat(refs[n])[0] }) });
    const prep = new Function("$", "$input", code)($, { first: () => ({ json: {} }) }).json;
    return { prep, calc: engine.runCalc(prep.calc_request).results };
}
const byId = r => Object.fromEntries(r.prep.response.complectations.map(c => [c.id, c]));

test("до правки: неактивный «Эксклюзив» (id 13) в комбо-расчёте", () => {
    assert.deepEqual(Object.keys(byId(run(CODE.n4n5))).sort(), ["11", "12", "13"]);
});

for (const [a, b] of [["n4n5a", "n4n5"], ["n5a", "n5"]]) {
    test(`${a}: только активные — Люкс (11) и Флагман (12); цены и расчёт = ${b}`, () => {
        const after = run(CODE[a]), before = run(CODE[b]);
        const A = byId(after), B = byId(before);
        assert.deepEqual(Object.keys(A).sort(), ["11", "12"]);
        for (const id of ["11", "12"]) {
            assert.deepEqual(A[id].price, B[id].price, id);
            assert.deepEqual(after.calc[id], before.calc[id], id);
        }
        assert.deepEqual(after.prep.calc_request.items.map(i => i.id).sort(), ["11", "12"]);
        assert.ok(!after.prep.response.complectations.some(c => /Эксклюзив|СЕ/.test(c.name)));
    });
}

test("combo_price активных = эталону N5 (price_reference.json)", () => {
    const A = byId(run(CODE.n4n5a));
    for (const id of ["11", "12"]) {
        const ref = REF.rows.find(r => r.catalog_table_id === "m2flt4d2r5d9cpa" && r.model_id === "18" && r.complectation_id === id);
        assert.equal(A[id].price.combo_price, ref.combo_price.n5, id);
    }
});

test("правка — только загрузка каталога", () => {
    const strip = s => s.replace(/\/\/ \[STOCK\][\s\S]*?\.filter\(isActiveRow\);\n/, "const catalogItems = getSafeAll('Get Catalog', 'Model');\n");
    assert.equal(strip(CODE.n4n5a), CODE.n4n5);
    assert.equal(strip(CODE.n5a), CODE.n5);
});
