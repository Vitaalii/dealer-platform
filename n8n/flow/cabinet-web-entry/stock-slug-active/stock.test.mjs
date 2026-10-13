// ============================================================
// Сток в Car Search: привязка по slug, только Active (решение 2026-10-05).
// old  — боевой код до правки (../live/car_search_prepare.prod-live.js);
// new  — ../live/car_search_prepare.stock.js (бой + сток, 10-05);
// n4   — ./car_search_prepare.n4.js (N4 + сток, [TEST]).
// Фикстуры — живые строки NocoDB 2026-10-05 (fixtures_2026-10-05.json):
// GEELY MONJARO (Model id 18) + сток radar; HAVAL JOLION (14) + сток askar.
// Запуск: node --test flow/cabinet-web-entry/stock-slug-active/*.test.mjs
// ============================================================
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const src = p => readFileSync(new URL(p, import.meta.url), "utf-8");
const CODE = { old: src("../live/car_search_prepare.prod-live.js"), new: src("../live/car_search_prepare.stock.js"),
               n4: src("./car_search_prepare.n4.js") };
const FX = JSON.parse(src("./fixtures_2026-10-05.json"));

function run(code, catalog, stock) {
    const refs = {
        "Execute Workflow Trigger": { qualification_context: { model: catalog[0].Model, model_id: catalog[0]["Model id"],
                                                               dealer_id: "x", request_type: "stock", payment: 0 } },
        "Get Catalog": catalog, "Get Stock": stock.length ? stock : [{}], "Get Rates": [{}], "Get Program": [{}],
    };
    const $ = n => ({ all: () => [].concat(refs[n]).map(j => ({ json: j })), first: () => ({ json: [].concat(refs[n])[0] }) });
    return new Function("$", "$input", code)($, { first: () => ({ json: {} }) }).json.response;
}
const counts = r => Object.fromEntries(r.complectations.map(c => [`${c.id} ${c.name}`, c.stock_count]));

test("GEELY MONJARO: было — 0 привязок, неактивные в списке", () => {
    const r = run(CODE.old, FX.geely_catalog, FX.geely_stock);
    assert.deepEqual(counts(r), { "11 Люкс": 0, "12 Флагман": 0, "13 Эксклюзив": 0 });
});

for (const v of ["new", "n4"]) {
    test(`GEELY MONJARO (${v}): 6 машин у «Флагмана» (Id 17), неактивных нет, total_stock 6`, () => {
        const r = run(CODE[v], FX.geely_catalog, FX.geely_stock);
        assert.deepEqual(counts(r), { "11 Люкс": 0, "12 Флагман": 6 });
        assert.equal(r.summary.total_stock, 6);
        const fl = r.complectations.find(c => c.id === "12");
        assert.equal(fl.stock_details.in_stock + fl.stock_details.other + fl.stock_details.in_transit, 6);
        assert.ok(!r.complectations.some(c => /Эксклюзив|СЕ/.test(c.name)));
    });

    test(`HAVAL JOLION (${v}): на фикстуре askar (без 2026MY) — привязано не меньше, Elite/Premium разведены по slug; потери 2026MY — DATA_ISSUES §6`, () => {
        const before = run(CODE.old, FX.haval_catalog, FX.haval_stock);
        const after = run(CODE[v], FX.haval_catalog, FX.haval_stock);
        const sum = r => r.complectations.reduce((a, c) => a + c.stock_count, 0);
        assert.ok(sum(after) >= sum(before), `${sum(after)} < ${sum(before)}`);
        // каждая привязанная машина — с точным slug своей строки каталога
        const slugOf = Object.fromEntries(FX.haval_catalog.map(t => [String(t["Complectation id"]), String(t.slug).toLowerCase()]));
        const bySlug = {};
        for (const car of FX.haval_stock) bySlug[car.slug.toLowerCase()] = (bySlug[car.slug.toLowerCase()] || 0) + 1;
        for (const c of after.complectations) assert.equal(c.stock_count, bySlug[slugOf[c.id]] || 0, c.name);
        assert.equal(after.complectations.length, before.complectations.length, "все строки HAVAL активны — список тот же");
    });
}

test("запасной вариант по названию — только для комплектаций без slug", () => {
    const cat = [{ ...FX.geely_catalog[1], slug: null, Complectation: "Luxury" }];   // «Люкс» без slug, латинское имя
    const r = run(CODE.new, cat, [{ ...FX.geely_stock[0], slug: "luxury-2-0t-x" }]);
    assert.equal(r.complectations[0].stock_count, 1);
    const withSlug = [{ ...FX.geely_catalog[1], Complectation: "Luxury" }];          // со slug — имя уже не используется
    assert.equal(run(CODE.new, withSlug, [{ ...FX.geely_stock[0], slug: "luxury-2-0t-x" }]).complectations[0].stock_count, 0);
});

test("правка — только сток: n4 = new + блок N4", () => {
    const n4base = src("../n4-min-down-payment/car_search_prepare.js");
    const strip = s => s.replace(/\/\/ \[N4\][\s\S]*?const program = pickProgram\([^;]*;\n/, "")
                        .replace("const program = getSafeFirst('Get Program', 'Program_Name');\n", "");
    assert.equal(strip(CODE.n4), strip(CODE.new));
    assert.notEqual(n4base, CODE.n4);
});
