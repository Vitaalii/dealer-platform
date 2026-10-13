// ============================================================
// Сверка живого ответа Combo Calculator (run_combo_once.py) с независимым
// локальным пересчётом: код Prepare Combo (--code, по умолчанию N4+N5) +
// flow/lib/calc_engine.js на ЖИВЫХ данных NocoDB (только чтение) и с той же
// оценкой трейд-ина, что вернул живой ответ. Плюс combo_price против эталона N2.
//
// Запуск: node check_combo_vs_local.mjs <ответ.json> [--code n5-only]
// ============================================================
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const engine = require("../../lib/calc_engine.js");
const HERE = new URL("./", import.meta.url);
const variant = process.argv.includes("--code") ? process.argv[process.argv.indexOf("--code") + 1] : "n4n5";
const CODE_FILES = { "n4n5": "../n5-combo-price-old/combo_prepare.js", "n5-only": "../n5-combo-price-old/combo_prepare.n5-only.js",
                     "n4n5-active": "../stock-slug-active/combo_prepare.n4n5.js", "n5-active": "../stock-slug-active/combo_prepare.n5.js" };
const CODE = readFileSync(new URL(CODE_FILES[variant], HERE), "utf-8");
const REF = JSON.parse(readFileSync(new URL("../n2-price-reference/price_reference.json", HERE), "utf-8"));
const { request, response } = JSON.parse(readFileSync(process.argv[2], "utf-8"));

const env = Object.fromEntries(readFileSync(new URL("../../../.env", HERE), "utf-8").split("\n")
    .map(l => l.trim()).filter(l => l && !l.startsWith("#") && l.includes("="))
    .map(l => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1).replace(/^"|"$/g, "")]));
const API = env.NOCODB_API_URL.replace(/\/$/, "");
async function rows(table, where = "") {
    const out = [];
    for (let off = 0; ; off += 200) {
        const q = `${API}/api/v2/tables/${table}/records?limit=200&offset=${off}${where ? "&where=" + encodeURIComponent(where) : ""}`;
        const d = await (await fetch(q, { headers: { "xc-token": env.NOCODB_TOKEN } })).json();
        out.push(...d.list);
        if (d.pageInfo.isLastPage !== false) return out;
    }
}

const dr = JSON.parse(request.data_routing);
const qc = request.qualification_context;
const te = response.tradein_evaluation || {};
if (te.status !== "SUCCESS") {
    console.log(`ОЦЕНКА НЕ SUCCESS: ${JSON.stringify(response).slice(0, 300)} — сверка невозможна`);
    process.exit(2);
}
const catalog = (await rows(dr.catalog_table_id, `(Model id,eq,${qc.model_id})`)).sort((a, b) => a.Id - b.Id);
const rates = await rows(dr.rates_table_id);
const programs = await rows(dr.program_table_id, `(Model_Pattern,like,%${qc.model_id}%)`);

const refs = {
    "Execute Workflow Trigger": { qualification_context: qc, query: request.query, plate: request.plate },
    "Parse Input": { qualification_context: qc, data_routing: dr },
    "Execute Workflow": { status: "SUCCESS", tradein_price_to: te.estimated_value, found_info: te.found_info,
                          tradein_make: te.tradein_make, tradein_model: te.tradein_model, tradein_year: te.tradein_year,
                          tradein_horse_power: te.tradein_horse_power, tradein_transmission: te.tradein_transmission,
                          tradein_steering_wheel: te.tradein_steering_wheel },
    "Get Catalog": catalog, "Get Stock": [{}], "Get Rates": rates, "Get Program": programs.length ? programs : [{}],
};
const $ = n => ({ all: () => [].concat(refs[n]).map(j => ({ json: j })), first: () => ({ json: [].concat(refs[n])[0] }) });
const prep = new Function("$", "$input", CODE)($, { first: () => ({ json: {} }) }).json;
const calc = engine.runCalc(prep.calc_request).results;

let bad = 0;
const live = Object.fromEntries(response.complectations.map(c => [c.id, c]));
console.log(`оценка трейд-ина: ${te.estimated_value}; платёж ${qc.payment}; программа: ` +
    `${prep.calc_request.program ? prep.calc_request.program.Min_Down_Payment_Pct + " % / " + prep.calc_request.program.Max_Term_Months + " мес." : "нет"}`);
for (const c of prep.response.complectations) {
    const l = live[c.id];
    const ref = REF.rows.find(r => r.catalog_table_id === dr.catalog_table_id && r.model_id === String(qc.model_id) && r.complectation_id === c.id);
    const okPrice = l && JSON.stringify(l.price) === JSON.stringify(c.price);
    const okRef = ref && l && l.price.combo_price === ref.combo_price.n5;
    const okCalc = l && JSON.stringify(l.combo_calculation) === JSON.stringify(calc[c.id]);
    if (!(okPrice && okRef && okCalc)) bad++;
    console.log(`${c.name.padEnd(12)} combo_price живой ${l?.price.combo_price} / локально ${c.price.combo_price} / эталон N5 ${ref?.combo_price.n5}` +
        ` | price ${okPrice ? "=" : "≠"} эталон ${okRef ? "=" : "≠"} расчёт ${okCalc ? "=" : "≠"} ` +
        `| ${l?.combo_calculation?.status} ${l?.combo_calculation?.term} мес. ${l?.combo_calculation?.monthly_payment} ₽ ${l?.combo_calculation?.rate_text}`);
    if (!okCalc) console.log(`   живой ${JSON.stringify(l?.combo_calculation)}\n   локал ${JSON.stringify(calc[c.id])}`);
}
const okRoot = JSON.stringify(response.combo_calculation) === JSON.stringify(calc[prep.root_calc_id]);
if (!okRoot) bad++;
console.log(`корневой combo_calculation: ${okRoot ? "=" : "≠"}; комплектаций живых ${response.complectations.length}, локальных ${prep.response.complectations.length}`);
if (response.complectations.length !== prep.response.complectations.length) bad++;
console.log(bad ? `ИТОГ: РАСХОЖДЕНИЙ ${bad}` : "ИТОГ: всё совпало");
process.exit(bad ? 1 : 0);
