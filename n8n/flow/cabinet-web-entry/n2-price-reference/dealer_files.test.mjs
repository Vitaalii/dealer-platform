// N2: подилерные файлы — формат бота на текущем эталоне price_reference.json.
// Запуск: node --test flow/cabinet-web-entry/n2-price-reference/*.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dealerFile, moscowIso, SOURCE } from "./build_dealer_files.mjs";

const REF = JSON.parse(readFileSync(new URL("price_reference.json", import.meta.url), "utf-8"));

test("формат: ключи, типы, время +03:00", () => {
    const { file } = dealerFile(REF, "02", "m2flt4d2r5d9cpa");
    assert.deepEqual(Object.keys(file), ["dealer_code", "generated_at", "source", "positions"]);
    assert.equal(file.dealer_code, "02");
    assert.equal(file.source, SOURCE);
    assert.match(file.generated_at, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+03:00$/);
    for (const p of file.positions) {
        assert.deepEqual(Object.keys(p), ["model_id", "complectation_id", "credit_price", "combo_price"]);
        assert.equal(typeof p.model_id, "string");
        assert.equal(typeof p.complectation_id, "string");
        assert.ok(Number.isInteger(p.credit_price) && p.credit_price > 0);
        assert.ok(Number.isInteger(p.combo_price) && p.combo_price > 0);
    }
});

test("GEELY: позиции 02 (Active_1) = позиции 09 (Active) без скрытых на сайте 02 моделей", () => {
    const f02 = dealerFile(REF, "02", { catalog: "m2flt4d2r5d9cpa", active_field: "Active_1" });
    const f09 = dealerFile(REF, "09", "m2flt4d2r5d9cpa");
    assert.equal(f02.active_field, "Active_1");
    assert.equal(f09.active_field, "Active");
    const key = p => `${p.model_id}/${p.complectation_id}`;
    const k02 = new Set(f02.file.positions.map(key));
    const only09 = f09.file.positions.filter(p => !k02.has(key(p))).map(key).sort();
    assert.deepEqual(only09, ["19/11", "19/12", "20/11", "20/12", "51/11"]);   // EX5, EX5 ГИБРИД, NORDCROSS 001
    for (const p of f02.file.positions) assert.deepEqual(p, f09.file.positions.find(q => key(q) === key(p)));
    const pairs = f02.file.positions.map(key);
    assert.equal(new Set(pairs).size, pairs.length);
});

test("GEELY: MONJARO «Люкс» — credit_price и combo_price по N5; неактивных нет", () => {
    const { file } = dealerFile(REF, "02", { catalog: "m2flt4d2r5d9cpa", active_field: "Active_1" });
    const lux = file.positions.find(p => p.model_id === "18" && p.complectation_id === "11");
    const row = REF.rows.find(r => r.catalog_table_id === "m2flt4d2r5d9cpa" && r.model_id === "18" && r.complectation_id === "11");
    assert.equal(lux.credit_price, row.price.credit_price);
    assert.equal(lux.combo_price, row.price.base_price - row.price.credit_bonus - row.price.tradein_bonus);
    assert.ok(!file.positions.some(p => p.model_id === "18" && p.complectation_id === "13"), "неактивные «Эксклюзив»/«Флагман СЕ»");
});

test("колонки нет в каталоге → Active (как в n8n)", () => {
    assert.equal(dealerFile(REF, "01", { catalog: "mywf0f16ptn25gk", active_field: "Active_1" }).active_field, "Active");
});

test("дубли комплектаций схлопнуты, нулевые цены в позиции не попадают", () => {
    // При дубле Complectation id берётся первая АКТИВНАЯ строка (как в Car Search):
    // ignored_row_ids старого эталона (по всем строкам) тут не применимы.
    const { file } = dealerFile(REF, "04", "m3swq2h1g0eikyc");   // CHERY: TIGGO 4 PRO без Price old
    assert.ok(file.positions.every(p => p.credit_price > 0 && p.combo_price > 0));
    for (const [cat, f] of [["mjcsjc2rtbm94yf", "Active"], ["m2flt4d2r5d9cpa", "Active_1"]]) {
        const pairs = REF.positions_by_active_field[cat][f].map(p => `${p.model_id}/${p.complectation_id}`);
        assert.equal(new Set(pairs).size, pairs.length, cat);
    }
});

test("неизвестный каталог — ошибка со списком известных", () => {
    assert.throws(() => dealerFile(REF, "77", "mNOPE"), /каталога mNOPE нет в эталоне/);
});

test("moscowIso", () => {
    assert.equal(moscowIso("2026-10-01T09:00:00.123Z"), "2026-10-01T12:00:00+03:00");
});
