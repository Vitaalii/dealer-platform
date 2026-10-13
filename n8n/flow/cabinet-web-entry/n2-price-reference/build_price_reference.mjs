// ============================================================
// N2: эталон цен и кредитного расчёта n8n для сверки с ботом.
//
// Эталон считается НЕ формулами, переписанными сюда, а исполнением настоящего
// кода нод на живых данных NocoDB (только чтение):
//   live — ../live/car_search_prepare.prod-live.js, ../live/combo_prepare.pre-n5.js
//          (= боевые ноды Prepare Data / Prepare Combo на 2026-09-30) + flow/lib/calc_engine.js;
//   n4   — выбор программы и Min_Down_Payment (../n4-min-down-payment);
//   n5   — комбо-цена от Price old (../n5-combo-price-old, сборка N4+N5).
// Группировка повторяет ноду Get Catalog: строки одной модели (Model id, а при
// пустом — Model), по возрастанию Id; дубль Complectation id внутри модели
// отбрасывается нодой — такие строки перечислены в ignored_row_ids.
//
// Запуск из корня репозитория: node flow/cabinet-web-entry/n2-price-reference/build_price_reference.mjs
// Пишет price_reference.json и price_reference.csv рядом с собой.
// ============================================================
import { readFileSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const HERE = new URL("./", import.meta.url);
const ROOT = new URL("../../../", import.meta.url);
const engine = require("../../lib/calc_engine.js");

const read = p => readFileSync(new URL(p, HERE), "utf-8");
const CODE = {
    live_car: read("../live/car_search_prepare.prod-live.js"),   // бой до правки стока (цены она не меняет)
    live_combo: read("../live/combo_prepare.pre-n5.js"),
    n4_car: read("../n4-min-down-payment/car_search_prepare.js"),
    n5_combo: read("../n5-combo-price-old/combo_prepare.js"),
    // текущий код нод с колонкой активности дилера (бой — после prod-*-active-field)
    cur_car: read("../active-field/car_search_prepare.js"),
    cur_combo: read("../active-field/combo_prepare.js"),
};

// Основные каталоги брендов. parsed_prices и базы-копии не входят.
const SOURCES = [
    { brand: "ALLCAR", catalog: "mjcsjc2rtbm94yf" },
    { brand: "HAVAL", catalog: "mywf0f16ptn25gk", program: "my7rg0l7j6varzn", rates: "mfocbgvi50mqm7c" },
    { brand: "CHERY", catalog: "m3swq2h1g0eikyc" },
    { brand: "CHERY", catalog: "mjhd30hsi50lvcm", note: "CHERY ATK" },
    { brand: "GEELY", catalog: "m2flt4d2r5d9cpa", program: "msa0pe85fmadg8n", rates: "mup1zkbxxaa6jha" },
    { brand: "BELGEE", catalog: "mc5jh12niqx7kp7", program: "m7a1rkmtih8o9yo", rates: "mmfiib16bjgllw1" },
    { brand: "TANK", catalog: "moitytlvaj7uclx" },
    { brand: "TANK", catalog: "m6co3bo2qsgt5bf", note: "TANK NOCO" },
    { brand: "GAC", catalog: "mshoisjxdjsckt9" },
    { brand: "CHANGAN", catalog: "mggqtmqjhz0enw5" },
    { brand: "SOLARIS", catalog: "mzsrh7tu1e0fsev" },
];

const env = Object.fromEntries(readFileSync(new URL(".env", ROOT), "utf-8").split("\n")
    .map(l => l.trim()).filter(l => l && !l.startsWith("#") && l.includes("="))
    .map(l => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1).replace(/^"|"$/g, "")]));
const API = env.NOCODB_API_URL.replace(/\/$/, "");

async function rows(tableId) {
    const out = [];
    // бережное чтение NocoDB: страницы по 25, пауза 1 с (CLAUDE.md)
    for (let offset = 0; ; offset += 25) {
        await new Promise(r => setTimeout(r, 1000));
        const res = await fetch(`${API}/api/v2/tables/${tableId}/records?limit=25&offset=${offset}`,
            { headers: { "xc-token": env.NOCODB_TOKEN } });
        if (!res.ok) throw new Error(`NocoDB ${tableId}: HTTP ${res.status}`);
        const d = await res.json();
        out.push(...d.list);
        if (d.pageInfo.isLastPage !== false) return out;
    }
}
async function meta(tableId) {
    const res = await fetch(`${API}/api/v2/meta/tables/${tableId}`, { headers: { "xc-token": env.NOCODB_TOKEN } });
    return (await res.json()).title;
}

function runNode(code, refs) {
    const $ = name => {
        const v = refs[name];
        if (v === undefined) throw new Error(`нет мока для ноды ${name}`);
        return { all: () => (Array.isArray(v) ? v : [v]).map(j => ({ json: j })),
                 first: () => ({ json: Array.isArray(v) ? v[0] : v }) };
    };
    return new Function("$", "$input", code)($, { first: () => ({ json: {} }) }).json;
}
const std = r => r && r.scenarios && r.scenarios.standard
    ? { down_payment: r.scenarios.standard.down_payment, monthly_payment: r.scenarios.standard.monthly_payment,
        term: r.scenarios.standard.term, rate_text: r.scenarios.standard.rate_text }
    : null;
const num = v => (v === undefined || v === null || v === "" ? null : Number(v));
const compId = t => String(t["Complectation id"] || t["Complectation_id"] || t.Id || "");

const result = [];
// позиции дилера по колонке активности: {catalog_table_id: {Active: [...], Active_1: [...]}}
const byActiveField = {};
const ACTIVE_FIELD_RE = /^Active(_\d{1,3})?$/;
for (const src of SOURCES) {
    const catalog = await rows(src.catalog);
    const programs = src.program ? await rows(src.program) : [];
    const rates = src.rates ? await rows(src.rates) : [];
    const title = await meta(src.catalog);
    const activeFields = [...new Set(catalog.flatMap(r => Object.keys(r).filter(k => ACTIVE_FIELD_RE.test(k))))].sort();
    byActiveField[src.catalog] = Object.fromEntries((activeFields.length ? activeFields : ["Active"]).map(f => [f, []]));
    const groups = new Map();
    for (const r of [...catalog].sort((a, b) => a.Id - b.Id)) {
        const key = r["Model id"] ? `id:${r["Model id"]}` : `name:${r.Model}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(r);
    }
    for (const group of groups.values()) {
        const base = {
            "Execute Workflow Trigger": { qualification_context: { model: group[0].Model, model_id: group[0]["Model id"],
                dealer_id: "99", request_type: "credit", payment: 0 } },
            "Parse Input": {}, "Execute Workflow": { status: "SUCCESS", tradein_price_to: 0 },
            "Get Catalog": group, "Get Stock": [{}], "Get Rates": rates.length ? rates : [{}],
        };
        // live: нода Get Program отдаёт строку при like по Id строки каталога, но код
        // всё равно её отбрасывает (Program_Name) — для live подаём пустой item.
        const liveCar = runNode(CODE.live_car, { ...base, "Get Program": [{}] });
        const liveCombo = runNode(CODE.live_combo, { ...base, "Get Program": [{}] });
        // n4: like по Model id — надмножество точного совпадения, код отбирает сам.
        const n4Car = runNode(CODE.n4_car, { ...base, "Get Program": programs.length ? programs : [{}] });
        const n5Combo = runNode(CODE.n5_combo, { ...base, "Get Program": programs.length ? programs : [{}] });
        const liveCalc = engine.runCalc(liveCar.calc_request).results || {};
        const n4Calc = engine.runCalc(n4Car.calc_request).results || {};
        const comboLive = Object.fromEntries(liveCombo.response.complectations.map(c => [c.id, c.price]));
        const comboN5 = Object.fromEntries(n5Combo.response.complectations.map(c => [c.id, c.price]));
        const prog = n4Car.calc_request.program;
        for (const field of Object.keys(byActiveField[src.catalog])) {
            const routing = { catalog_active_field: field };
            const refs = { ...base, "Get Program": programs.length ? programs : [{}],
                           "Execute Workflow Trigger": { ...base["Execute Workflow Trigger"], data_routing: routing },
                           "Parse Input": { data_routing: routing } };
            const car = runNode(CODE.cur_car, refs).response, combo = runNode(CODE.cur_combo, refs).response;
            const comboPrice = Object.fromEntries((combo.complectations || []).map(c => [c.id, c.price.combo_price]));
            for (const c of car.complectations || []) {
                const used = group.find(t => compId(t) === c.id && [true, 1, "1", "true"].includes(t[car.summary.catalog_active_field]));
                byActiveField[src.catalog][field].push({ row_id: used ? used.Id : null, model_id: used ? (used["Model id"] ?? null) : null,
                    model: used ? used.Model : null, complectation_id: c.id, complectation: c.name,
                    credit_price: c.price.credit_price, combo_price: comboPrice[c.id] ?? null,
                    applied_active_field: car.summary.catalog_active_field });
            }
        }
        for (const c of liveCar.response.complectations) {
            const rowsOf = group.filter(t => compId(t) === c.id);
            const used = rowsOf[0];
            result.push({
                brand: src.brand, catalog_table_id: src.catalog, catalog_title: title,
                row_id: used.Id, ignored_row_ids: rowsOf.slice(1).map(t => t.Id),
                model_id: used["Model id"] ?? null, model: used.Model, complectation_id: c.id, complectation: c.name,
                input: { price_old: num(used["Price old"]), cash_in_hook: num(used["Cash in hook"]),
                         price_new: num(used["Price new"]), credit_bonus: num(used["Credit bonus"]),
                         trade_in_bonus: num(used["Trade-in bonus"]) },
                price: c.price,                                    // MSRP … tradein_bonus — Car Search, не меняется
                combo_price: { live: comboLive[c.id].combo_price, n5: comboN5[c.id].combo_price },
                program: {
                    live: null,
                    n4: prog ? { program_id: prog.Program_id, min_down_pct: prog.Min_Down_Payment_Pct,
                                 max_term_months: num(prog.Max_Term_Months) } : null,
                },
                credit_standard_payment0: { live: std(liveCalc[c.id]), n4: std(n4Calc[c.id]) },
            });
        }
    }
    console.log(`${src.brand} ${title}: ${catalog.length} строк`);
}

const sha = s => createHash("sha256").update(s).digest("hex").slice(0, 12);
const doc = {
    generated_at: new Date().toISOString(),
    calc_engine_version: engine.CALC_ENGINE_VERSION,
    code_sha256_12: Object.fromEntries(Object.entries(CODE).map(([k, v]) => [k, sha(v)])),
    rules: {
        rounding: "round10k = Math.round(x / 10000) * 10000 (половина — вверх), применяется к КАЖДОЙ колонке отдельно до вычитаний",
        msrp: "round10k(Price old)", cash_bonus: "round10k(Cash in hook)", base_price: "MSRP − cash_bonus",
        credit_price: "base_price − round10k(Credit bonus)", tradein_price: "base_price − round10k(Trade-in bonus)",
        combo_price_live: "round10k(Price new) − credit_bonus − tradein_bonus",
        combo_price_n5: "base_price − credit_bonus − tradein_bonus",
        positions_by_active_field: "текущий код нод (active-field/*): только строки с Active-колонкой дилера = да, привязка как в Car Search; credit_price и combo_price (N5)",
        program_n4: "строка [brand]_Credit_Programs, где Model id — отдельный элемент Model_Pattern; min_down_pct = Min_Down_Payment, пусто → 20",
        credit_standard: "Calc Engine calculateCreditScenarios при payment = 0: взнос min_down_pct, срок max_term (без программы 20 % / 84)",
    },
    rows: result,
    positions_by_active_field: byActiveField,
};
writeFileSync(new URL("price_reference.json", HERE), JSON.stringify(doc, null, 2) + "\n");

const cols = ["brand", "catalog_title", "row_id", "model_id", "model", "complectation_id", "complectation",
    "price_old", "cash_in_hook", "price_new", "credit_bonus_in", "trade_in_bonus_in",
    "MSRP", "cash_bonus", "base_price", "credit_price", "tradein_price", "combo_price_live", "combo_price_n5",
    "min_down_pct_live", "min_down_pct_n4", "max_term_n4",
    "std_down_live", "std_monthly_live", "std_down_n4", "std_monthly_n4", "std_rate_n4", "ignored_row_ids"];
const csvCell = v => (v === null || v === undefined ? "" : /[",;\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
const lines = [cols.join(",")].concat(result.map(r => [
    r.brand, r.catalog_title, r.row_id, r.model_id, r.model, r.complectation_id, r.complectation,
    r.input.price_old, r.input.cash_in_hook, r.input.price_new, r.input.credit_bonus, r.input.trade_in_bonus,
    r.price.MSRP, r.price.cash_bonus, r.price.base_price, r.price.credit_price, r.price.tradein_price,
    r.combo_price.live, r.combo_price.n5,
    20, r.program.n4 ? r.program.n4.min_down_pct : 20, r.program.n4 ? r.program.n4.max_term_months : 84,
    r.credit_standard_payment0.live?.down_payment, r.credit_standard_payment0.live?.monthly_payment,
    r.credit_standard_payment0.n4?.down_payment, r.credit_standard_payment0.n4?.monthly_payment,
    r.credit_standard_payment0.n4?.rate_text, r.ignored_row_ids.join(" "),
].map(csvCell).join(",")));
writeFileSync(new URL("price_reference.csv", HERE), lines.join("\n") + "\n");
console.log(`строк эталона: ${result.length}`);
