// ============================================================
// Оффлайн-симуляция ноды Build Prompt резолвера (план 2026-07-17-001).
// Код ноды берётся ИЗ СГЕНЕРИРОВАННОГО flow/Tool_ Resolver Core.json,
// интенты — из снапшота живой таблицы (nocodb/add_intents_v2.py --snapshot-only).
// Проверяет семантику видимости R1/R3 (стадия — контекст, не фильтр),
// сохранение фильтров Active/Scenarios и бюджет размера промпта R8.
// Запуск: node resolver_prompt_sim.test.mjs
// ============================================================
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const WF = JSON.parse(readFileSync(new URL("../Tool_ Resolver Core.json", import.meta.url), "utf-8"));
const TABLES = JSON.parse(readFileSync(new URL("./snapshots/v3_tables.json", import.meta.url), "utf-8"));

// Порог R8: ~2700 входных токенов. Фактический замер 2026-07-17 (n8n exec
// 2449–2494, [TEST] Tool: Resolver Core): 3.17–3.33 симв./токен; берём
// консервативные 3.17 => бюджет в символах 2700 × 3.17 ≈ 8560.
const PROMPT_CHAR_BUDGET = 8560;

const code = WF.nodes.find(n => n.name === "Build Prompt").parameters.jsCode;

// Шаблон после отложенной выкатки f2de42d (удаление client_mode / client_signal,
// план 2026-10-09-001, Q2): бой и генератор пока работают со старым шаблоном.
// Строки вырезаются из кода ноды, а не дублируются — дрейф шаблона всплывёт здесь.
const CLIENT_FIELD_LINES = [/^Допустимые client_mode: .*\n/m, /^Допустимые client_signal: .*\n\n/m,
                            /^  "client_mode": .*\n/m, /^  "client_signal": .*\n/m];
const codeAfterF2de42d = CLIENT_FIELD_LINES.reduce((c, re) => {
    if (!re.test(c)) throw new Error(`в шаблоне Build Prompt нет строки ${re}`);
    return c.replace(re, "");
}, code);

function runBuildPrompt(body, intents, src = code) {
    const data = { "Execute Workflow Trigger": body, "Get Intents": intents };
    const $ = (name) => {
        if (!(name in data)) throw new Error(`нет мока для ноды ${name}`);
        const val = data[name];
        return {
            all: () => (Array.isArray(val) ? val : [val]).map(j => ({ json: j })),
            first: () => ({ json: Array.isArray(val) ? val[0] : val }),
        };
    };
    return new Function("$", src)($)[0].json;
}

function body(overrides = {}) {
    return {
        session_id: "s1", user_id: "u1", message: "тестовое сообщение",
        recent_history: [{ role: "client", text: "привет" }, { role: "bot", text: "здравствуйте!" }],
        session_context: {
            request_type: "credit", current_stage: "data_collect", msg_count: 3,
            phone_ask_count: 0, collected_data: {},
        },
        ...overrides,
    };
}

const withCtx = (scenario, stage, extra = {}) => body({
    session_context: { request_type: scenario, current_stage: stage, msg_count: 3,
                       phone_ask_count: 0, collected_data: {}, ...extra },
});

const LIVE = TABLES.intents;

// AE3: патч Scenarios (+credit) применён 2026-07-21 (--apply-scenarios,
// [TEST]-контур) — мутация стала no-op и остаётся страховкой от дрейфа/отката.
function withPendingScenarioPatch(intents) {
    const out = structuredClone(intents);
    for (const iid of ["objection_data_privacy", "tool_rerun_needed"]) {
        const row = out.find(i => i.Intent_ID === iid);
        if (row && row.Scenarios !== "all" && !row.Scenarios.includes("credit")) {
            row.Scenarios += ",credit";
            console.log(`  (симуляция отложенного патча U6: ${iid} +credit)`);
        }
    }
    return out;
}

let passed = 0;
const T = (name, fn) => { fn(); passed++; };

// ---------- R1: стадия не сужает список ----------
T("AE1: data_collect/combo — schedule_appointment и propose_visit в offered", () => {
    const out = runBuildPrompt(withCtx("combo", "data_collect"), LIVE);
    assert.ok(out.offered.includes("schedule_appointment"), "нет schedule_appointment (closing-интент скрыт стадийным фильтром)");
    assert.ok(out.offered.includes("propose_visit"), "нет propose_visit");
});

T("AE2: msg_count=0, greeting/credit — data_received_complete в offered", () => {
    const out = runBuildPrompt(withCtx("credit", "greeting", { msg_count: 0 }), LIVE);
    assert.ok(out.offered.includes("data_received_complete"), "нет data_received_complete на greeting");
});

T("AE3: credit/closing — tool_rerun_needed в offered (после патча Scenarios)", () => {
    const out = runBuildPrompt(withCtx("credit", "closing"), withPendingScenarioPatch(LIVE));
    assert.ok(out.offered.includes("tool_rerun_needed"), "нет tool_rerun_needed в credit");
});

// ---------- сохранённые фильтры ----------
T("сценарный фильтр сохранён: offered ⊆ активные интенты сценария", () => {
    // живая таблица дрейфует (владелец уже расширил часть Scenarios на credit),
    // поэтому проверяем инвариант по данным снапшота, а не конкретный интент
    for (const scenario of ["credit", "stock"]) {
        const out = runBuildPrompt(withCtx(scenario, "data_collect"), LIVE);
        const inScen = v => v === "all" || String(v).split(",").map(s => s.trim()).includes(scenario);
        for (const id of out.offered) {
            const row = LIVE.find(i => i.Intent_ID === id);
            assert.ok(inScen(row.Scenarios), `${id} (Scenarios: ${row.Scenarios}) не должен показываться в ${scenario}`);
        }
        const excluded = LIVE.filter(i => i.Active && !inScen(i.Scenarios));
        assert.ok(excluded.length > 0, `нет ни одного интента вне сценария ${scenario} — фильтр нечем проверить`);
        assert.ok(!out.offered.includes(excluded[0].Intent_ID),
            `${excluded[0].Intent_ID} вне сценария ${scenario}, но попал в offered`);
    }
});

T("фильтр Active сохранён: Active=false отсутствует в offered", () => {
    const draft = { Intent_ID: "___draft_test", Description: "черновик", Example_Phrases: "тест",
                    Scenarios: "all", Stage_Context: "all", Priority: 50, Active: false };
    const out = runBuildPrompt(withCtx("credit", "data_collect"), [...LIVE, draft]);
    assert.ok(!out.offered.includes("___draft_test"), "синтетический черновик попал в offered");
    for (const i of LIVE.filter(i => !i.Active)) {
        assert.ok(!out.offered.includes(i.Intent_ID), `неактивный ${i.Intent_ID} попал в offered`);
    }
});

// ---------- стадия остаётся контекстом ----------
T("строка «Текущая стадия системы: <stage>» присутствует в промпте", () => {
    const out = runBuildPrompt(withCtx("tradein", "tool_needed"), LIVE);
    assert.ok(out.prompt.includes("Текущая стадия системы: tool_needed"), "нет строки стадии в промпте");
});

// ---------- R8: бюджет размера промпта ----------
const WIDE_HISTORY = Array.from({ length: 8 }, (_, k) => ({
    role: k % 2 ? "bot" : "client", text: "х".repeat(150),
}));
const wideBody = () => body({
    recent_history: WIDE_HISTORY,
    session_context: { request_type: "combo", current_stage: "data_collect",
                       msg_count: 9, phone_ask_count: 1, collected_data: {} },
});

T("размер промпта: текущие интенты на боевом шаблоне — в бюджете R8", () => {
    const out = runBuildPrompt(wideBody(), withPendingScenarioPatch(LIVE));
    assert.ok(out.prompt.length <= PROMPT_CHAR_BUDGET,
        `промпт ${out.prompt.length} симв. > бюджета ${PROMPT_CHAR_BUDGET} (R8 ~2700 ток. × 3.17)`);
});

T("размер промпта: все черновики активны — в бюджете R8 только после выкатки f2de42d", () => {
    // худший случай: черновики активированы владельцем + отложенный патч Scenarios.
    // На боевом шаблоне (с client_mode / client_signal) он не влезает (8881 > 8560
    // на 2026-10-09): активировать все черновики разом — только после выкатки f2de42d.
    const widest = withPendingScenarioPatch(LIVE).map(i => ({ ...i, Active: true }));
    const out = runBuildPrompt(wideBody(), widest, codeAfterF2de42d);
    assert.ok(out.prompt.length <= PROMPT_CHAR_BUDGET,
        `промпт ${out.prompt.length} симв. > бюджета ${PROMPT_CHAR_BUDGET} (R8 ~2700 ток. × 3.17)`);
    assert.ok(runBuildPrompt(wideBody(), widest).prompt.length > PROMPT_CHAR_BUDGET,
        "боевой шаблон уже влезает в бюджет — условие «только после f2de42d» устарело, обновите тест");
});

// ---------- v3.03: need-check интенты (план 2026-07-21-001) ----------
T("need-check: черновики не видны до активации; после — в 4 сценариях, но не в consult", () => {
    // обе стороны — мутацией снапшота, независимо от живого состояния Active
    const pre = structuredClone(LIVE);
    const act = structuredClone(LIVE);
    for (const iid of ["need_check_ok", "need_check_not_ok"]) {
        const preRow = pre.find(i => i.Intent_ID === iid);
        assert.ok(preRow, `${iid} нет в снапшоте — пересоберите: add_needcheck_v303.py --snapshot-only`);
        preRow.Active = false;
        act.find(i => i.Intent_ID === iid).Active = true;
    }
    const out0 = runBuildPrompt(withCtx("credit", "tool_needed"), pre);
    assert.ok(!out0.offered.includes("need_check_ok") && !out0.offered.includes("need_check_not_ok"),
        "черновики v3.03 не должны попадать в offered до активации");
    for (const scen of ["credit", "tradein", "combo", "stock"]) {
        const out = runBuildPrompt(withCtx(scen, "tool_needed"), act);
        assert.ok(out.offered.includes("need_check_ok") && out.offered.includes("need_check_not_ok"),
            `need_check_* нет в offered сценария ${scen}`);
    }
    const outC = runBuildPrompt(withCtx("consult", "data_collect"), act);
    assert.ok(!outC.offered.includes("need_check_ok") && !outC.offered.includes("need_check_not_ok"),
        "need_check_* не должны показываться в consult");
});

console.log(`OK: Build Prompt резолвера, ${passed} сценариев симуляции пройдено`);
