// ============================================================
// N5: комбо-цена от Price old (та же база, что в Car Search).
// Боевой код — flow/lib (= живые ноды 2026-09-30), правка — эта папка.
// Строки каталога — GEELY MONJARO из исполнения 5022.
// Запуск: node --test flow/cabinet-web-entry/n5-combo-price-old/*.test.mjs
// ============================================================
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const src = p => readFileSync(new URL(p, import.meta.url), "utf-8");
const LIVE_COMBO = src("../live/combo_prepare.pre-n5.js");
const LIVE_CAR = src("../live/car_search_prepare.prod-live.js");   // бой до правки стока
const N4N5 = src("./combo_prepare.js");
const N5 = src("./combo_prepare.n5-only.js");
const N4 = src("../n4-min-down-payment/combo_prepare.js");

const MONJARO = [
    { Id: 2, Model: "MONJARO", "Model id": "18", "Complectation id": "13", Complectation: "Эксклюзив",
      "Price old": 5052190, "Cash in hook": 40000, "Price new": 4502190, "Trade-in bonus": 250000 },
    { Id: 16, Model: "MONJARO", "Model id": "18", "Complectation id": "11", Complectation: "Люкс",
      "Price old": 4599990, "Price new": 4409990, "Trade-in bonus": 190000 },
    { Id: 17, Model: "MONJARO", "Model id": "18", "Complectation id": "14", Complectation: "Флагман СЕ",
      "Price old": 5052190, "Cash in hook": 100000, "Price new": 4502190, "Credit bonus": 200000, "Trade-in bonus": 250000 },
];
const refs = {
    "Execute Workflow Trigger": { qualification_context: { model: "MONJARO", model_id: "18", dealer_id: "99",
        request_type: "combo", payment: 60000 } },
    "Parse Input": {}, "Execute Workflow": { status: "SUCCESS", tradein_price_to: 830000 },
    "Get Catalog": MONJARO, "Get Stock": [{}], "Get Rates": [{}], "Get Program": [{}],
};
function run(code) {
    const $ = name => {
        const v = refs[name];
        if (v === undefined) throw new Error(`нет мока для ноды ${name}`);
        return { all: () => (Array.isArray(v) ? v : [v]).map(j => ({ json: j })),
                 first: () => ({ json: Array.isArray(v) ? v[0] : v }) };
    };
    return new Function("$", "$input", code)($, { first: () => ({ json: {} }) }).json;
}
const prices = code => Object.fromEntries(run(code).response.complectations.map(c => [c.id, c.price]));

test("живой код: база комбо — Price new", () => {
    const p = prices(LIVE_COMBO);
    assert.equal(p["11"].MSRP, 4410000);
    assert.equal(p["11"].combo_price, 4220000);          // 4 410 000 − 190 000
});

test("N5: база комбо = Price old − Cash in hook, как у Car Search", () => {
    const p = prices(N5);
    assert.deepEqual(p["11"], { MSRP: 4600000, cash_bonus: 0, base_price: 4600000,
                                credit_bonus: 0, tradein_bonus: 190000, combo_price: 4410000 });
    assert.equal(p["13"].combo_price, 5050000 - 40000 - 250000);
    assert.equal(p["14"].combo_price, 5050000 - 100000 - 200000 - 250000);
});

test("N5: MSRP / cash_bonus / base_price совпадают с Car Search, combo_price = tradein_price − credit_bonus", () => {
    const car = Object.fromEntries(run(LIVE_CAR).response.complectations.map(c => [c.id, c.price]));
    for (const [id, p] of Object.entries(prices(N5))) {
        assert.equal(p.MSRP, car[id].MSRP, id);
        assert.equal(p.cash_bonus, car[id].cash_bonus, id);
        assert.equal(p.base_price, car[id].base_price, id);
        assert.equal(p.combo_price, car[id].tradein_price - car[id].credit_bonus, id);
    }
});

test("N5: в Calc Engine уходит новая цена", () => {
    const items = Object.fromEntries(run(N5).calc_request.items.map(i => [i.id, i.price]));
    assert.equal(items["11"], 4410000);
});

test("N4+N5 = N4 с заменой цены: обе правки независимы", () => {
    const strip = s => s.replace(/        \/\/ \[N5\][\s\S]*?const basePrice = msrp - cashBonus;\n/, "")
                        .replace("        const basePrice = round10k(Number(trim['Price new']));\n", "")
                        .replace(/MSRP: (msrp|basePrice),\n(\s+cash_bonus: cashBonus,\n\s+base_price: basePrice,\n)?/, "MSRP,\n");
    assert.equal(strip(N4N5), strip(N4));
    assert.deepEqual(prices(N4N5), prices(N5));
});
