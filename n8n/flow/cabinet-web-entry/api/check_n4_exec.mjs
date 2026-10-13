// ============================================================
// Проверка боевых исполнений Car Search / Combo после N4 (только чтение API).
// По каждому исполнению после --since:
//   - программа: найдена ли (Program_id, Min_Down_Payment_Pct, Max_Term_Months)
//     и совпадает ли с правилом N4 (Model id — отдельный элемент Model_Pattern,
//     взнос = Min_Down_Payment, пусто → 20);
//   - Calc Engine = локальный runCalc(calc_request) той же ноды;
//   - для Car Search: стандартный взнос при платеже 0 = round10k(credit_price × %).
// --wait N — ждать, пока появится хотя бы одно исполнение (опрос раз в 60 с).
// Запуск: node check_n4_exec.mjs --since 2026-10-06T05:05:00Z [--wait 10800]
// ============================================================
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const engine = require("../../lib/calc_engine.js");
const arg = (k, d) => (process.argv.includes(k) ? process.argv[process.argv.indexOf(k) + 1] : d);
const SINCE = arg("--since"), WAIT = Number(arg("--wait", 0));
const env = Object.fromEntries(readFileSync(new URL("../../../.env", import.meta.url), "utf-8").split("\n")
    .map(l => l.trim()).filter(l => l && !l.startsWith("#") && l.includes("="))
    .map(l => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1).replace(/^"|"$/g, "")]));
const API = env.N8N_API_URL.replace(/\/$/, "");
const get = async p => (await fetch(API + p, { headers: { "X-N8N-API-KEY": env.N8N_API_KEY } })).json();
const out = (run, n) => (run[n] || []).flatMap(r => r.data?.main?.[0] || []).map(i => i.json);
const round10k = v => Math.round(v / 10000) * 10000;
const WFS = { mZqzmEnnQiDbvtir: ["Car Search", "Prepare Data"], GD3ZCj8GiVyB7YBw: ["Combo", "Prepare Combo"] };

const deadline = Date.now() + WAIT * 1000;
let found = 0;
const seen = new Set();
while (true) {
    for (const [wid, [label, prepNode]] of Object.entries(WFS)) {
        const list = (await get(`/executions?workflowId=${wid}&limit=50`)).data
            .filter(e => e.startedAt >= SINCE && !seen.has(e.id)).sort((a, b) => a.id - b.id);
        for (const e of list) {
            seen.add(e.id);
            const d = await get(`/executions/${e.id}?includeData=true`);
            const run = d.data.resultData.runData;
            const pi = out(run, "Parse Input")[0] || {};
            const qc = pi.qualification_context || {};
            const prep = out(run, prepNode)[0];
            const head = `${label} ${e.id} ${e.startedAt.slice(0, 19)} ${e.status} | model_id ${qc.model_id} dealer ${JSON.stringify(qc.dealer_id)}`;
            if (!prep || !prep.calc_request || prep.calc_request.operation === "noop") {
                console.log(`${head}: без расчёта (${prep?.response?.combo_calculation?.status || prep?.response?.result_text || "нет данных"})`);
                found++; continue;
            }
            found++;
            const errs = [];
            const p = prep.calc_request.program;
            const rows = out(run, "Get Program").filter(r => r.Program_id);
            const exp = rows.find(r => String(r.Model_Pattern || "").split(",").map(s => s.trim()).includes(String(qc.model_id ?? out(run, "Get Catalog")[0]?.["Model id"])));
            const expPct = exp ? ((exp.Min_Down_Payment === null || exp.Min_Down_Payment === undefined || exp.Min_Down_Payment === "") ? 20 : Number(exp.Min_Down_Payment)) : null;
            if (!!p !== !!exp) errs.push(`программа: ${p ? "есть" : "нет"}, по правилу ожидалась ${exp ? "есть" : "нет"}`);
            if (p && exp && Number(p.Min_Down_Payment_Pct) !== expPct) errs.push(`взнос ${p.Min_Down_Payment_Pct} ≠ ${expPct}`);
            const live = out(run, "Calc Engine")[0]?.results || {};
            const local = engine.runCalc(prep.calc_request).results || {};
            if (JSON.stringify(live) !== JSON.stringify(local)) errs.push("Calc Engine ≠ локальный пересчёт");
            if (label === "Car Search" && !Number(prep.calc_request.target_payment)) {
                const pct = p ? (Number(p.Min_Down_Payment_Pct) || 10) : 20;
                for (const c of prep.response.complectations) {
                    const std = local[c.id]?.scenarios?.standard;
                    if (std && std.down_payment !== round10k(c.price.credit_price * pct / 100)) errs.push(`${c.name}: взнос ${std.down_payment}`);
                }
            }
            const stock = label === "Car Search"
                ? ` | сток: привязано ${prep.response.complectations.reduce((a, c) => a + c.stock_count, 0)} из ${prep.response.summary?.total_stock}` : "";
            console.log(`${head} | программа ${p ? `${p.Program_id}: ${p.Min_Down_Payment_Pct} % / ${p.Max_Term_Months} мес.` : "нет (20 % / 84)"}${stock} — ${errs.length ? "РАСХОЖДЕНИЯ: " + errs.join("; ") : "OK"}`);
        }
    }
    if (found || Date.now() >= deadline) break;
    await new Promise(r => setTimeout(r, 60000));
}
if (!found) console.log(`исполнений Car Search / Combo после ${SINCE} нет`);
