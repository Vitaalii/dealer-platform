// ============================================================
// Колонка активности — настройка дилера (data_routing.catalog_active_field).
// was — текущий бой (../stock-slug-active/*.n4.js, *.n4n5.js);
// now — ./car_search_prepare.js, ./combo_prepare.js.
// Фикстуры — живые строки GEELY 2026-10-06: EX5 (19), NORDCROSS 001 (51),
// MONJARO (18) с Active и Active_1; HAVAL без колонки Active_1.
// Запуск: node --test flow/cabinet-web-entry/active-field/*.test.mjs
// ============================================================
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const engine = require("../../lib/calc_engine.js");
const src = p => readFileSync(new URL(p, import.meta.url), "utf-8");
const CODE = {
    car: { was: src("../stock-slug-active/car_search_prepare.n4.js"), now: src("./car_search_prepare.js") },
    combo: { was: src("../stock-slug-active/combo_prepare.n4n5.js"), now: src("./combo_prepare.js") },
};
const FX = JSON.parse(src("./fixtures_2026-10-06.json"));
const HAVAL = JSON.parse(src("../stock-slug-active/fixtures_2026-10-05.json")).haval_catalog;   // без Active_1
const PROGRAM = { Program_id: "4201", Model_Pattern: "12, 18, 14, 13, 19, 20, 11", Min_Down_Payment: 10, Max_Term_Months: 84 };
const RATES = [{ Term_From: 12, Term_To: 84, Downpayment_From: 0, Downpayment_To: 100, Rate: 16.5 }];

function run(kind, code, catalog, routing) {
    const qc = { model: catalog[0].Model, model_id: catalog[0]["Model id"], dealer_id: "radar",
                 request_type: kind === "car" ? "credit" : "combo", payment: kind === "car" ? 0 : 50000 };
    const refs = {
        // Car Search: data_routing приходит объектом на входе триггера;
        // Combo: строкой на входе, объектом — после Parse Input
        "Execute Workflow Trigger": kind === "car" ? { qualification_context: qc, data_routing: routing }
                                                   : { qualification_context: JSON.stringify(qc), data_routing: JSON.stringify(routing || {}) },
        "Parse Input": { qualification_context: qc, data_routing: routing },
        "Execute Workflow": { status: "SUCCESS", tradein_price_to: 2890000 },
        "Get Catalog": catalog, "Get Stock": [{}], "Get Rates": RATES, "Get Program": [PROGRAM],
    };
    const $ = n => ({ all: () => [].concat(refs[n]).map(j => ({ json: j })), first: () => ({ json: [].concat(refs[n])[0] }) });
    return new Function("$", "$input", code)($, { first: () => ({ json: {} }) }).json;
}
const names = out => (out.response.complectations || []).map(c => c.name).sort();
const R02 = { catalog_active_field: "Active_1" }, R09 = { catalog_active_field: "Active" };

for (const kind of ["car", "combo"]) {
    test(`${kind}: дилер 02 (Active_1) — EX5 и NORDCROSS скрыты, ответ «не найдены», summary показывает колонку`, () => {
        for (const mid of ["19", "51"]) {
            const out = run(kind, CODE[kind].now, FX[mid], R02);
            assert.equal((out.response.complectations || []).length, 0, mid);
            assert.equal(out.calc_request.operation, "noop");
            assert.equal(out.response.summary.catalog_active_field, "Active_1");
        }
    });

    test(`${kind}: дилер 09 (Active) и без ключа — EX5 виден, вывод побайтово как у боя + summary`, () => {
        for (const routing of [R09, undefined, {}]) {
            const now = run(kind, CODE[kind].now, FX["19"], routing), was = run(kind, CODE[kind].was, FX["19"], routing);
            assert.deepEqual(names(now), ["Макс", "Про"]);
            assert.equal(now.response.summary.catalog_active_field, "Active");
            delete now.response.summary.catalog_active_field;
            assert.deepEqual(now, was);
        }
    });

    test(`${kind}: MONJARO одинаков для 02 и 09 (Люкс, Флагман)`, () => {
        assert.deepEqual(names(run(kind, CODE[kind].now, FX["18"], R02)), ["Люкс", "Флагман"]);
        assert.deepEqual(names(run(kind, CODE[kind].now, FX["18"], R09)), ["Люкс", "Флагман"]);
    });

    test(`${kind}: недопустимое имя → Active`, () => {
        for (const bad of ["Price old", "active_1", "Active_", "Active_1; DROP", "Active_1234", 7, null]) {
            const out = run(kind, CODE[kind].now, FX["19"], { catalog_active_field: bad });
            assert.equal(out.response.summary.catalog_active_field, "Active", String(bad));
            assert.deepEqual(names(out), ["Макс", "Про"]);
        }
    });

    test(`${kind}: колонки Active_1 нет в таблице (HAVAL) → Active, каталог не пустой`, () => {
        const out = run(kind, CODE[kind].now, HAVAL, R02);
        assert.equal(out.response.summary.catalog_active_field, "Active");
        assert.equal(out.response.complectations.length, run(kind, CODE[kind].was, HAVAL, R02).response.complectations.length);
    });
}

test("combo: data_routing строкой (как в триггере) тоже читается", () => {
    const qc = { model: "EX5", model_id: "19", dealer_id: "radar", request_type: "combo", payment: 50000 };
    const refs = { "Execute Workflow Trigger": { qualification_context: JSON.stringify(qc) }, "Parse Input": { qualification_context: qc, data_routing: "=" + JSON.stringify(R02) },
                   "Execute Workflow": { status: "SUCCESS", tradein_price_to: 2890000 }, "Get Catalog": FX["19"], "Get Stock": [{}], "Get Rates": RATES, "Get Program": [PROGRAM] };
    const $ = n => ({ all: () => [].concat(refs[n]).map(j => ({ json: j })), first: () => ({ json: [].concat(refs[n])[0] }) });
    const out = new Function("$", "$input", CODE.combo.now)($, { first: () => ({ json: {} }) }).json;
    assert.equal(out.response.summary.catalog_active_field, "Active_1");
});

test("combo: расчёт активных MONJARO не меняется (Active_1 = Active для Люкс/Флагман)", () => {
    const a = run("combo", CODE.combo.now, FX["18"], R02), b = run("combo", CODE.combo.was, FX["18"], R02);
    assert.deepEqual(engine.runCalc(a.calc_request).results, engine.runCalc(b.calc_request).results);
});

test("правка — только колонка активности и summary", () => {
    for (const k of ["car", "combo"]) {
        const strip = s => s.replace(/\/\/ \[ACTIVE-FIELD\][\s\S]*?const catalogItems = rawCatalogItems\.filter\(isActiveRow\);\n/, "")
                            .replace(/function isActiveRow\(t\) \{\n    const a = t\.Active;[\s\S]*?\.filter\(isActiveRow\);\n/, "")
                            .replace(/,\n\s+catalog_active_field: ACTIVE_FIELD\n/, "\n")
                            .replace(/,\n\s+summary: \{ catalog_active_field: ACTIVE_FIELD \}\n/, "\n");
        assert.equal(strip(CODE[k].now), strip(CODE[k].was), k);
    }
});
