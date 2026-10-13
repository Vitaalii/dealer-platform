#!/usr/bin/env python3
"""U8: генератор flow/Tool_ Blocks Lint.json — n8n-линт таблиц v3.00.

Логика проверок зеркалит nocodb/lint_check.py (он — референс). Запуск линта:
вручную в n8n (Manual Trigger). Падает с ошибкой при битых ссылках; отчёт с
предупреждениями — в выходном item'е.
"""
import json
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
FLOW = HERE.parent / "flow"
IDS = json.loads((HERE / "table_ids.json").read_text())
BASE_ID = "pqw76hd7h1lztww"
CREDENTIAL = {"nocoDbApiToken": {"id": "ijM4z0UsgIXr8sqx", "name": "NocoDB Token account"}}

LINT_JS = r"""
const BUDGET = 2500;
// Tokens_est = len/4; русский текст ≈ 2.5 симв/токен => реальные ≈ est × 1.6
const TOKENS_EST_TO_REAL = 1.6;
// Вход резолвера (R8, план 2026-07-17-001): ~2700 ток. × 3.17 симв./ток. (замер
// 2026-07-17); фикс-часть промпта Build Prompt (шаблон + история 8×150 + сообщение)
const RESOLVER_PROMPT_CHAR_BUDGET = 8560;
// [U5, план 2026-08-14-001] было 2433; из шаблона Build Prompt удалены
// client_mode и client_signal, замеренная дельта -440. Зеркалит
// nocodb/lint_check.py — менять только вместе с ним.
const RESOLVER_FIXED_CHARS = 1993;
const RESOLVER_CHARS_PER_TOKEN = 3.17;
const SCENARIOS = ["credit","tradein","combo","stock","consult"];
const STAGES = ["greeting","data_collect","tool_needed","closing"];

const intents = $('Get Intents').all().map(i => i.json);
const blocks  = $('Get Blocks').all().map(i => i.json);
const mapping = $('Get Map').all().map(i => i.json);

const intentIds = new Set(intents.map(i => i.Intent_ID));
const blockById = Object.fromEntries(blocks.map(b => [b.Block_ID, b]));
const expand = (v, u) => (!v || v === "all") ? u : v.split(",").map(s => s.trim()).filter(Boolean);

const errors = [], warnings = [];

for (const m of mapping) {
    if (!intentIds.has(m.Intent_ID)) errors.push(`карта → несуществующий интент ${m.Intent_ID}`);
    const blk = blockById[m.Block_ID];
    if (!blk) errors.push(`карта ${m.Intent_ID} → несуществующий блок ${m.Block_ID}`);
    else if (m.Active && !blk.Active) warnings.push(`привязка ${m.Intent_ID} → ${m.Block_ID}: блок неактивен`);
}

// Зеркалит выбор основного блока в Prompt Compiler:
//   pick(id) = active(id + "_V2") ? id + "_V2" : id, иначе WF_{СЦЕН}_LEGACY.
// Прежняя редакция смотрела только на базовый блок и ругалась на каждый,
// вытесненный своей версией _V2. Менять только вместе с nocodb/lint_check.py.
const activeId = (bid) => Boolean(blockById[bid] && blockById[bid].Active);
for (const scen of SCENARIOS) for (const stage of STAGES) {
    const bid = `ST_${scen.toUpperCase()}_${stage.toUpperCase()}`;
    if (!blockById[bid] && !blockById[`${bid}_V2`]) { warnings.push(`нет дефолтного блока ${bid}`); continue; }
    if (activeId(`${bid}_V2`) || activeId(bid)) continue;
    const legacy = `WF_${scen.toUpperCase()}_LEGACY`;
    warnings.push(activeId(legacy)
        ? `дефолтный блок ${bid} и его _V2 неактивны — ход поедет на ${legacy}`
        : `дефолтный блок ${bid}: неактивны ни он, ни _V2, ни фолбэк ${legacy} — основной блок хода будет ПУСТЫМ`);
}

const core = blocks.filter(b => b.Block_ID.startsWith("G-") && b.Active && !b.Intent_Triggers)
    .reduce((s, b) => s + (Number(b.Tokens_est) || 0), 0);
const tools = Number((blockById["T-01_TOOLS"] || {}).Tokens_est) || 0;
const over = [];
for (const it of intents) {
    if (!it.Active) continue;
    for (const scen of expand(it.Scenarios, SCENARIOS)) {
        // R1 (план 2026-07-17-001): Stage_Context — справочное поле, не фильтр
        for (const stage of STAGES) {
            const rows = mapping.filter(m => m.Intent_ID === it.Intent_ID && m.Active
                && expand(m.Scenario_Filter, SCENARIOS).includes(scen)
                && expand(m.Stage_Filter, STAGES).includes(stage));
            const primary = (rows.find(m => m.Role === "primary") || {}).Block_ID
                || `ST_${scen.toUpperCase()}_${stage.toUpperCase()}`;
            const ids = [`CS_${scen.toUpperCase()}`, `D_${scen.toUpperCase()}`, primary,
                         ...rows.filter(m => m.Role === "additional").map(m => m.Block_ID)];
            const est = core + tools + ids.reduce((s, i) => s + (Number((blockById[i] || {}).Tokens_est) || 0), 0);
            const total = Math.round(est * TOKENS_EST_TO_REAL);
            if (total > BUDGET) over.push([it.Intent_ID, scen, stage, total]);
        }
    }
}
if (over.length) {
    over.sort((a, b) => b[3] - a[3]);
    warnings.push(`бюджет >${BUDGET} ток. у ${over.length} комбинаций; худшие: `
        + over.slice(0, 5).map(o => `${o[0]}/${o[1]}/${o[2]}=${o[3]}`).join(", "));
}

// вход резолвера (R8): фикс-часть + строки `Intent_ID | Description | Примеры`
// всех активных интентов сценария (стадия список не сужает после R1)
for (const scen of SCENARIOS) {
    const rows = intents.filter(i => i.Active && expand(i.Scenarios, SCENARIOS).includes(scen));
    const listChars = rows.reduce((s, i) =>
        s + `${i.Intent_ID} | ${i.Description || ""} | Примеры: ${i.Example_Phrases || ""}`.length + 1, 0);
    const total = RESOLVER_FIXED_CHARS + listChars;
    if (total > RESOLVER_PROMPT_CHAR_BUDGET) {
        warnings.push(`вход резолвера ${scen}: ~${total} симв. `
            + `(~${Math.round(total / RESOLVER_CHARS_PER_TOKEN)} ток., ${rows.length} интентов) `
            + `> бюджета R8 ${RESOLVER_PROMPT_CHAR_BUDGET} симв.`);
    }
}

if (errors.length) {
    throw new Error(`BLOCKS LINT FAIL: ${errors.length} ошибок. ` + errors.slice(0, 10).join(" | "));
}
return [{ json: { status: "OK", errors: 0, warnings: warnings.length,
                  warning_list: warnings,
                  counts: { intents: intents.length, blocks: blocks.length, map: mapping.length } } }];
"""


def node(name, ntype, tv, params, pos, creds=None):
    n = {"parameters": params, "type": ntype, "typeVersion": tv, "position": pos,
         "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"blocks-lint/{name}")), "name": name}
    if creds:
        n["credentials"] = creds
    return n


def noco(name, table_id, pos):
    n = node(name, "n8n-nodes-base.nocoDb", 3, {
        "authentication": "nocoDbApiToken", "operation": "getAll",
        "projectId": BASE_ID, "table": table_id, "returnAll": True, "options": {},
    }, pos, CREDENTIAL)
    # без executeOnce нода n8n выполняется по разу на КАЖДЫЙ входной item:
    # Get Blocks после 32 интентов дал бы 32×54 item'ов, Get Map — 1728×108
    n["executeOnce"] = True
    return n


nodes = [
    node("When clicking Lint", "n8n-nodes-base.manualTrigger", 1, {}, [0, 0]),
    noco("Get Intents", IDS["Intents"], [220, 0]),
    noco("Get Blocks", IDS["Prompt_Blocks"], [440, 0]),
    noco("Get Map", IDS["Intent_Block_Map"], [660, 0]),
    node("Lint", "n8n-nodes-base.code", 2, {"jsCode": LINT_JS.strip()}, [880, 0]),
]
conns = {}
chain = ["When clicking Lint", "Get Intents", "Get Blocks", "Get Map", "Lint"]
for a, b in zip(chain, chain[1:]):
    conns[a] = {"main": [[{"node": b, "type": "main", "index": 0}]]}

wf = {"name": "Tool: Blocks Lint", "nodes": nodes, "connections": conns,
      "settings": {"executionOrder": "v1"}, "pinData": {},
      "meta": {"instanceId": "65c0178e84d5c3db112ec6489beae5f53a11ae74e53d6537d4d9a5cd140a483b"}}
out = FLOW / "Tool_ Blocks Lint.json"
out.write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"OK: {out}")
