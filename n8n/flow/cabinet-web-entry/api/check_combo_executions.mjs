// ============================================================
// Проверка боевых (или [TEST]) исполнений Combo Calculator после выката N5 —
// только чтение n8n API. По каждому исполнению после --since:
//   1) у каждой комплектации price несёт MSRP / cash_bonus / base_price и
//      combo_price = base_price − credit_bonus − tradein_bonus;
//   2) результаты Calc Engine = локальный runCalc(calc_request) из той же ноды;
//   3) combo_price = эталону N5 (price_reference.json), если каталог и
//      Model id исполнения есть в эталоне.
// Исполнения с ранним выходом (оценка не SUCCESS) считаются и пропускаются.
//
// Запуск: node check_combo_executions.mjs --wf GD3ZCj8GiVyB7YBw --since 2026-09-30T08:18:39Z
// ============================================================
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const engine = require("../../lib/calc_engine.js");
const HERE = new URL("./", import.meta.url);
const arg = k => process.argv[process.argv.indexOf(k) + 1];
const WF = arg("--wf"), SINCE = arg("--since");
const REF = JSON.parse(readFileSync(new URL("../n2-price-reference/price_reference.json", HERE), "utf-8"));
const env = Object.fromEntries(readFileSync(new URL("../../../.env", HERE), "utf-8").split("\n")
    .map(l => l.trim()).filter(l => l && !l.startsWith("#") && l.includes("="))
    .map(l => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1).replace(/^"|"$/g, "")]));
const API = env.N8N_API_URL.replace(/\/$/, "");
const get = async p => (await fetch(API + p, { headers: { "X-N8N-API-KEY": env.N8N_API_KEY } })).json();
const out = (run, node) => (((run[node] || [])[0] || {}).data?.main?.[0] || []).map(i => i.json);

const list = (await get(`/executions?workflowId=${WF}&limit=50`)).data.filter(e => e.startedAt >= SINCE)
    .sort((a, b) => a.startedAt.localeCompare(b.startedAt));
console.log(`исполнений после ${SINCE}: ${list.length}`);
let bad = 0, checked = 0;
for (const e of list) {
    const d = await get(`/executions/${e.id}?includeData=true`);
    const run = d.data.resultData.runData;
    const prep = out(run, "Prepare Combo")[0];
    const pi = out(run, "Parse Input")[0] || {};
    const qc = pi.qualification_context || {}, dr = pi.data_routing || {};
    const head = `${e.id} ${e.startedAt.slice(0, 19)} ${e.status} ${qc.model || ""}`;
    if (!prep || !prep.response || !prep.response.complectations) {
        const early = prep?.response?.combo_calculation?.status || "нет Prepare Combo";
        console.log(`${head}: ранний выход (${early}) — пропуск`);
        continue;
    }
    checked++;
    const calcLive = out(run, "Calc Engine")[0]?.results || {};
    const calcLocal = engine.runCalc(prep.calc_request).results || {};
    const errs = [];
    for (const c of prep.response.complectations) {
        const p = c.price || {};
        if (!("cash_bonus" in p && "base_price" in p)) errs.push(`${c.name}: нет cash_bonus/base_price — код не N5`);
        if (p.combo_price !== p.base_price - p.credit_bonus - p.tradein_bonus) errs.push(`${c.name}: combo_price ≠ base − бонусы`);
        if (JSON.stringify(calcLive[c.id]) !== JSON.stringify(calcLocal[c.id])) errs.push(`${c.name}: Calc Engine ≠ локальный пересчёт`);
        const ref = REF.rows.find(r => r.catalog_table_id === dr.catalog_table_id && r.model_id === String(qc.model_id) && r.complectation_id === c.id);
        if (ref && ref.combo_price.n5 !== p.combo_price) errs.push(`${c.name}: combo_price ${p.combo_price} ≠ эталон ${ref.combo_price.n5} (каталог мог измениться после 30.09)`);
    }
    bad += errs.length;
    const root = out(run, "Merge Calc")[0]?.combo_calculation;
    console.log(`${head}: комплектаций ${prep.response.complectations.length}, оценка ${prep.calc_request.tradein_value}, ` +
        `корень ${root?.status} ${root?.term} мес. ${root?.monthly_payment} ₽ — ${errs.length ? "РАСХОЖДЕНИЯ" : "OK"}`);
    errs.forEach(x => console.log("   " + x));
}
console.log(checked ? (bad ? `ИТОГ: расхождений ${bad}` : `ИТОГ: проверено ${checked}, всё OK`) : "ИТОГ: полных исполнений пока нет");
