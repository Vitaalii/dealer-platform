#!/usr/bin/env python3
"""U10: генератор flow/AI Agent v3.0.json — конвейер Resolver → Stage Classifier →
Data Selector → Prompt Compiler → Agent → Tool Guard.

Принципы:
- отдельный webhook-путь (ai-handoff-v3), старый воркфлоу не трогается (R24);
- тул-ноды (finalize_deal, evaluate_auto, Combo Calculator) КЛОНИРУЮТСЯ из
  боевого AI Agent v2.0 MAX.json дословно (их выражения ссылаются на 'Edit
  Fields' — нода нормализации в v3 названа так же);
- Data Selector использует ДОСЛОВНО старые trim-функции пяти веток (гарантия
  идентичности срезов данных);
- Prompt Compiler читает Prompt_Blocks/Intent_Block_Map на каждый ход; пока
  ST-каркас не наполнен — фолбэк на WF_{SCEN}_LEGACY (старое поведение);
- один retry Tool Guard при пропущенном finalize; формат — rewrite без тулов;
- Prompt Compiler подставляет данные дилера в [dealer_name]/[dealer_address]/
  [dealer_hours] из session_context (в v2 это делал бот, в v3 делать было некому);
- ответ: {text, json_cache, session_context_update} (стадия/интент для Python).

После импорта вручную выбрать саб-воркфлоу в нодах: Run Resolver → 'Tool:
Resolver Core'; Call Car Search / Save CRM Direct подтянутся по сохранённым ID.
"""
import json
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
FLOW = HERE.parent / "flow"
IDS = json.loads((HERE / "table_ids.json").read_text())
BASE_ID = "pqw76hd7h1lztww"
NOCO_CRED = {"nocoDbApiToken": {"id": "ijM4z0UsgIXr8sqx", "name": "NocoDB Token account"}}
OPENROUTER_CRED = {"openRouterApi": {"id": "hrFobDiOtSwC9E0F", "name": "OpenRouter account"}}
OPENAI_VIA_OPENROUTER_CRED = {"openAiApi": {"id": "PtI7CvwiH2WYU1pM", "name": "OpenRouter Account"}}

# --------------------------------------------------------------- модели ----
# ЗНАЧЕНИЯ ОБЯЗАНЫ СОВПАДАТЬ С ЖИВЫМ ВОРКФЛОУ. Импорт сгенерированного файла
# перетирает то, что выставлено в интерфейсе n8n, и делает это молча.
#
# Инцидент #3871 (2026-09-03): импорт в 11:18 вернул Agent LM с 3.1 на 2.5,
# в 14:52 клиент получил служебную фразу вместо ответа — 2.5-flash-lite с
# подключёнными инструментами интермиттентно завершается с finish_reason=stop
# и пустым текстом, потратив ответ на внутренние размышления (607 и 816
# токенов на двух попытках подряд). Владелец вернул 3.1 руками в 14:59.
MODEL_AGENT = "google/gemini-3.1-flash-lite"
MODEL_AGENT_FALLBACK = "openai/gpt-4.1-mini"
# 2.5 здесь — ОСОЗНАННОЕ ОТЛОЖЕННОЕ РЕШЕНИЕ владельца (2026-09-07), а не забытый
# хвост: ветка напоминаний остаётся на этой модели до проработки сложных
# сценариев ремайндеров. Не «чинить» заодно с Agent LM.
MODEL_REMINDER = "google/gemini-2.5-flash-lite"

OLD_WF = json.loads((FLOW / "AI Agent v2.0 MAX.json").read_text(encoding="utf-8"))
OLD_NODES = {n["name"]: n for n in OLD_WF["nodes"]}

# ------------------------------------------------- привязки саб-воркфлоу ----
# Идентификаторы БОЕВЫХ саб-воркфлоу, снятые запросом к API n8n 2026-09-07.
#
# Это НЕ исторические прод-тулы (qunUsxh…/ZYVac3…/y4ee1N5…): боевой v3 — это
# переименованный тестовый контур, и он ходит в бывшие [TEST]-копии, которые
# несут рефакторинг Calc Engine и правку U1 (проброс found_info в кабинет).
# Старые прод-тулы остались обслуживать живой v2.0 MAX и здесь не при чём.
#
# Раньше эти четыре значения были плейсхолдерами и подставлялись руками после
# импорта. Ручной шаг убран сознательно: он уже выполнялся неполно, а n8n при
# перепривязке переименовывает узел по имени выбранного воркфлоу — имена узлов
# читает модель, и расхождение даёт UNEXPECTED_TOOL_CALL (инцидент 3517).
#
# cachedResultName МЕНЯТЬ НЕЛЬЗЯ: по нему build_test_contour.py опознаёт ссылки
# и подменяет их id [TEST]-копий (ключи FORK_REFS). Сменишь подпись —
# боевые адреса протекут в тестовый контур.
RESOLVER_CORE_WF = {"__rl": True, "value": "qsotmM7sbmyckg3E", "mode": "list",
                    "cachedResultName": "Tool: Resolver Core"}
CAR_SEARCH_WF = {"__rl": True, "value": "mZqzmEnnQiDbvtir", "mode": "list",
                 "cachedResultName": "Tool - Car Search"}
SAVE_CRM_WF = {"__rl": True, "value": "vQ-eYIoZ_w85mu17FHGdJ", "mode": "list",
               "cachedResultName": "TOOL: Save to CRM"}
# Тул-ноды клонируются из v2 вместе с их workflowId, а он там указывает на
# исторические прод-тулы. Перебиваем по ИМЕНИ УЗЛА В v3; finalize_deal не
# трогаем — у него и в v2, и в бою один и тот же TOOL: Save to CRM.
TOOL_WF_OVERRIDE = {
    "evaluate_auto": "XCPe53sUwk8i9e3T",
    "combo_calculator": "GD3ZCj8GiVyB7YBw",
}

SCENARIO_TRIM_NODES = {  # сценарий -> (нода старого воркфлоу, имя переменной входа)
    "credit": ("Code in JavaScript", "data"),
    "consult": ("Code in JavaScript1", "input"),
    "stock": ("Code in JavaScript2", "data"),
    "tradein": ("Code in JavaScript6", "data"),
    "combo": ("Code in JavaScript7", "data"),
}

# ---------------------------------------------------------------- JS bodies --

EDIT_FIELDS_JS = r"""
const raw = $input.first().json.body || $input.first().json;
function forceParse(v) {
    if (typeof v !== 'string') return v;
    let c = v.trim();
    if (c.startsWith('=')) c = c.substring(1);
    try {
        let p = JSON.parse(c);
        if (typeof p === 'string') p = JSON.parse(p);
        return p;
    } catch (e) {
        try { return Function('"use strict";return (' + c + ')')(); }
        catch (e2) { return v; }
    }
}
const item = Object.assign({}, raw);
item.qualification_context = forceParse(item.qualification_context) || {};
item.data_routing = forceParse(item.data_routing) || {};
item.session_context = forceParse(item.session_context) || {};
item.recent_history = forceParse(item.recent_history) || [];
item.chatInput = item.message || "";
item.deal_id = item.qualification_context.deal_id || item.deal_id || null;
return [{ json: item }];
"""

PRESET_GREETING_JS = r"""
const item = $('Edit Fields').first().json;
return [{ json: Object.assign({}, item, { resolver: {
    verdict: "preset", dialog_intent: "greeting_first",
    client_mode: "collector", client_signal: "neutral",
    key_data_extracted: {}, reasoning: "INIT-ход: детерминированный greeting_first",
}})}];
"""

ATTACH_RESOLVER_JS = r"""
const item = $('Edit Fields').first().json;
const r = $input.first().json;
return [{ json: Object.assign({}, item, { resolver: r }) }];
"""

ATTACH_CACHE_JS = r"""
const ctx = $('Cache Gate').first().json;
const fresh = $input.first().json;
const merged = Object.assign({}, ctx, { fresh_cache: fresh });
merged.qualification_context = Object.assign({}, ctx.qualification_context, { cached_json: fresh });
return [{ json: merged }];
"""

STAGE_CLASSIFIER_JS = r"""
const item = $input.first().json;
const sc = item.session_context || {};
const flags = sc.flags || {};
const collected = sc.collected_data || {};
const r = item.resolver || { verdict: "missing" };

let intent = (r.verdict === "ok" || r.verdict === "preset") ? r.dialog_intent : "";

// Детерминированные триггеры Python переопределяют резолвер (R5)
let bypass = null;
if (flags.stop_word_detected || intent === "final_refusal") bypass = "refuse";
if (intent === "ban_trigger") bypass = "ban";
const finalizeRequired = Boolean(flags.has_valid_phone_in_msg)
    || (Boolean(collected.phone_collected) && !sc.deal_finalized);
if (finalizeRequired && !bypass) intent = "finalize_agreed";

let stage = sc.current_stage || "data_collect";
const TOOL_INTENTS = ["data_received_complete","ready_for_tool","tool_rerun_needed","tool_error_handling"];
const CLOSE_INTENTS = ["tool_result_received","propose_visit","propose_call","request_phone",
                       "finalize_agreed","schedule_appointment","competitor_comparison",
                       "polite_close_satisfied"];
if (TOOL_INTENTS.includes(intent)) stage = "tool_needed";
if (CLOSE_INTENTS.includes(intent)) stage = "closing";
if ((sc.msg_count || 0) === 0 && !finalizeRequired) stage = "greeting";
// INIT/первое сообщение: приветственный протокол обязателен, даже если воронка
// уже собрала слоты и эвристика Python говорит tool_needed
if (intent === "greeting_first" && !finalizeRequired) stage = "greeting";

const toolsOpen = stage === "tool_needed" || stage === "closing" || finalizeRequired;
return [{ json: Object.assign({}, item, { turn: {
    intent: intent || "", stage, bypass,
    finalize_required: finalizeRequired, tools_open: toolsOpen,
    resolver_verdict: r.verdict,
}})}];
"""

BYPASS_BUILD_JS = r"""
const item = $('Stage Classifier').first().json;
const blocks = Object.fromEntries($('Get Blocks').all().map(i => [i.json.Block_ID, i.json]));
const kind = item.turn.bypass; // refuse | ban
const bid = kind === "ban" ? "BYPASS_BAN" : "BYPASS_REFUSE";
const text = ((blocks[bid] || {}).Content || "Диалог завершён. Всего доброго!").trim();
const ai_payload = JSON.stringify({
    status: kind,
    report: kind === "ban" ? "Автозавершение: агрессия/бан-триггер"
                           : "Автозавершение: категорический отказ клиента",
});
return [{ json: Object.assign({}, item, {
    bypass_text: text, ai_payload,
    user_id: item.user_id || "", deal_id: item.deal_id || null,
})}];
"""

# [U3] Данные оценки трейд-ина для личного кабинета. Кабинет замораживает снапшот
# калькулятора из n8n_cache и ждёт там два ключа верхнего уровня: found_info (сырой
# блок auto.ru плюс госномер) и ti_valuation (число). Хелпер подмешивается во ВСЕ
# три выхода, возвращающие json_cache (Final Response, Bypass Respond, Reminder
# Respond) — снапшот замораживается на каждом ходе, поэтому ход, вернувший кэш без
# ключей, дал бы кабинету пустой блок «Ваш автомобиль».
#
# Источник, переживающий пересборку кэша, — qualification_context.evaluation_carryover:
# бот шлёт его на КАЖДОМ ходе отдельным узким ключом. Через сам json_cache данные не
# переживают пересборку: бот перезаписывает контекст целиком и на rebuild-ходе (TTL 600 с
# либо переход к стадии расчёта) присылает cached_json = null — прежнего кэша в этот
# момент нет ни под каким ключом. Ключа carryover может не быть вовсе — терпим отсутствие.
#
# ВАЖНО: подмешивать только ПОСЛЕ Cache Gate. Гейт различает ходы по непустоте
# qualification_context.cached_json; если влить carryover в него до гейта, rebuild-ход
# увидел бы непустой объект, ушёл бы в обход Car Search и каталог перестал бы обновляться.
EVAL_KEYS_JS = r"""
// --- [U3] ключи оценки для личного кабинета ---
function evalFromCarryover(qc) {
    const c = (qc || {}).evaluation_carryover;
    const out = {};
    if (!c || typeof c !== "object" || Array.isArray(c)) return out;
    const fi = c.found_info;
    if (fi && typeof fi === "object" && !Array.isArray(fi) && Object.keys(fi).length) out.found_info = fi;
    if (Number(c.ti_valuation) > 0) out.ti_valuation = Number(c.ti_valuation);
    return out;
}
// Пустой набор ключей обязан оставлять кэш нетронутым: контракт кабинета —
// «ключей просто нет», а не пустой объект (сценарии без трейд-ина штатны).
function withEvalKeys(cache, keys) {
    if (!Object.keys(keys).length) return cache || null;
    return Object.assign({}, cache || {}, keys);
}
"""

BYPASS_RESPOND_JS = EVAL_KEYS_JS + r"""
const b = $('Bypass Build').first().json;
const base = b.fresh_cache || (b.qualification_context || {}).cached_json || null;
const cache = withEvalKeys(base, evalFromCarryover(b.qualification_context));
return [{ json: {
    text: b.bypass_text, json_cache: cache,
    session_context_update: {
        stage: "closing",
        dialog_intent: b.turn.bypass === "ban" ? "ban_trigger" : "final_refusal",
        key_data_extracted: {}, resolver_verdict: (b.resolver || {}).verdict || "",
    },
}}];
"""

PROMPT_COMPILER_JS = r"""
const item = $input.first().json;
const turn = item.turn;
const sc = item.session_context || {};
const scenario = sc.request_type || (item.qualification_context || {}).request_type || "consult";
const up = scenario.toUpperCase();
const stage = turn.stage;
const intent = turn.intent;

const blocks = Object.fromEntries($('Get Blocks').all().map(i => [i.json.Block_ID, i.json]));
const map = $('Get Map').all().map(i => i.json).filter(m => m.Active);
const expand = (v, u) => (!v || v === "all") ? u : String(v).split(",").map(s => s.trim());
const SCEN = ["credit","tradein","combo","stock","consult"];
const STAGES = ["greeting","data_collect","tool_needed","closing"];

const rows = map.filter(m => m.Intent_ID === intent
    && expand(m.Scenario_Filter, SCEN).includes(scenario)
    && expand(m.Stage_Filter, STAGES).includes(stage));
// [2026-09-03] Данные дилера. Плейсхолдеры [dealer_name]/[dealer_address]/
// [dealer_hours] живут в тексте блоков (сегодня — только G-01_ROLE) и до этой
// правки уезжали к модели сырыми: в исполнении 3758 клиент получил ответ
// «находится по адресу: [dealer_address]. Мы работаем с [dealer_hours]».
// В v2 подстановку делал бот перед отправкой; v3 собирает промпт сам, и делать
// её стало некому.
//
// Источник — ТОЛЬКО поля session_context (контракт бота, коммит 5b559da).
// Разбор склеенного qualification_context.system_prompt отклонён владельцем:
// формат этой строки задаёт таблица Prompts, её правит владелец контента, и
// переформулировка «— Адрес:» ломала бы подстановку молча.
//
// Подставляются ТОЛЬКО эти три токена и только по точному совпадению. Прочие
// шаблоны в скобках трогать нельзя: [Модель]/[Сумма]/[Имя клиента] и подобные
// заполняет сама модель из <DATA_FROM_DATABASE>, а [SPLIT] и [CABINET_LINK] —
// контракт с ботом, он разбирает их в тексте ответа.
// Формулировки утверждены владельцем 2026-09-03. Вариант «уточнит менеджер»
// отклонён сознательно: ассистент, не знающий адреса собственного салона,
// читается клиентом как мошенник. Заглушка вместо этого держит уверенный тон и
// переводит разговор на действие.
//
// ВНИМАНИЕ: механизма отправки ссылки на карту в боте нет — обещание останется
// невыполненным. Владелец предупреждён, решение принято осознанно как меньшее
// зло на время, пока карточки дилеров в Dealers не заполнены.
const DEALER_FALLBACK = {
    dealer_name: "НЕ УКАЗАНО. Не придумывай название: говори «наш дилерский центр», без конкретного имени",
    dealer_address: "НЕ УКАЗАН. Не называй город и улицу: скажи, что адрес пришлешь ссылкой на карту",
    dealer_hours: "НЕ УКАЗАН. Не называй часы: скажи, что точный график пришлешь вместе со ссылкой на карту",
};
// Пустое значение НЕ заменяется пустой строкой. Рядом в G-01_ROLE стоит
// «отвечай СТРОГО на основании этих данных. Запрещено выдумывать» — пустая
// строка читается как «данных нет, придумай». Заглушка сама диктует поведение.
const dealerValues = {};
const dealerMissing = [];
for (const f of Object.keys(DEALER_FALLBACK)) {
    const raw = sc[f];
    const val = typeof raw === "string" ? raw.trim() : "";
    dealerValues[f] = val || DEALER_FALLBACK[f];
    if (!val) dealerMissing.push(f);   // null, "", пробелы, не-строка ИЛИ ключа нет вовсе
}
// Один проход и функция-заменитель: значение из карточки дилера подставляется
// буквально, спецпоследовательности вида $& в нём не интерпретируются, и уже
// подставленный текст повторно под замену не попадает.
const subst = text => text.replace(/\[(dealer_name|dealer_address|dealer_hours)\]/g,
    (_, f) => dealerValues[f]);

const active = id => Boolean(blocks[id] && blocks[id].Active);
const content = id => subst(((blocks[id] || {}).Content || "").trim());
// v3.03 (need-check): черновик <ID>_V2 вытесняет базовый блок, как только владелец
// его активирует. Оборачиваем только name-selected выборы (CS_{SCEN} и дефолтный
// ST_{SCEN}_{STAGE}); map-managed блоки катятся черновыми map-строками, D_{SCEN}
// не оборачиваем — V2 для D-блоков нет.
const pick = id => active(id + "_V2") ? id + "_V2" : id;

// v3.01: finalize-правила нужны только на closing/finalize_required, а не на каждом
// tools_open-ходе (на tool_needed вызов evaluate/combo уже ведут ST_*_TOOL_NEEDED)
const finalizeRulesDue = stage === "closing" || turn.finalize_required;
const useTFinalize = finalizeRulesDue && active("T_FINALIZE");

// primary: переопределение из карты -> дефолт по стадии -> WF_LEGACY (переходный фолбэк)
let primaryId = (rows.find(m => m.Role === "primary") || {}).Block_ID
    || pick(`ST_${up}_${stage.toUpperCase()}`);
let primaryContent = active(primaryId) ? content(primaryId) : "";
if (!primaryContent) {
    const def = pick(`ST_${up}_${stage.toUpperCase()}`);
    primaryContent = active(def) ? content(def) : content(`WF_${up}_LEGACY`);
}

const parts = [];
// черновик сжатого VOICE вытесняет старый блок, как только владелец его активирует
const voiceId = active("G-02_VOICE_V2") ? "G-02_VOICE_V2" : "G-02_VOICE";
// Жёсткий список general-блоков: подключения «по уровню» компилятор не делает,
// колонки Level и Stage он не читает вовсе. Всё, чего здесь нет, подключается
// строкой Intent_Block_Map либо собственным полем Intent_Triggers блока.
//
// v3.07 (план 2026-08-14-001, U1): G-07_CABINET убран отсюда и переведён на
// Intent_Triggers — иначе владелец не может включить или выключить кабинет без
// правки кода и переимпорта воркфлоу. Рубильник поэтапного выката — колонка
// Scenario этого блока: цикл Intent_Triggers ниже её читает.
for (const gid of ["G-01_ROLE", voiceId, "G-03_GOAL","G-04_FORMAT"]) {
    if (active(gid)) parts.push(content(gid));
}
const csId = pick(`CS_${up}`);
if (active(csId)) parts.push(content(csId));
if (active(`D_${up}`)) parts.push(content(`D_${up}`));
if (primaryContent) parts.push(primaryContent);
for (const m of rows.filter(m => m.Role === "additional")) {
    if (active(m.Block_ID)) parts.push(content(m.Block_ID));
}
for (const b of Object.values(blocks)) { // Intent_Triggers (например G-05_DEFENSE на off_topic)
    if (b.Active && b.Intent_Triggers
        && String(b.Intent_Triggers).split(",").map(s => s.trim()).includes(intent)
        // блок обязан подходить сценарию хода: без этого OBJ всех пяти сценариев
        // въезжали в промпт разом (баг exec 2347: +12.7k символов и чужой стиль)
        && (!b.Scenario || b.Scenario === "all" || expand(b.Scenario, SCEN).includes(scenario))) {
        // subst() и здесь: эта ветка берёт Content напрямую, мимо content(),
        // и без неё блок, подключённый триггером, увёз бы плейсхолдеры сырыми
        const c = subst(String(b.Content || "").trim());
        if (c && !parts.includes(c)) parts.push(c);
    }
}
if (useTFinalize) parts.push(content("T_FINALIZE"));
// переходный фолбэк: пока T_FINALIZE не активирован владельцем — старый T-01 по tools_open
else if (turn.tools_open && active("T-01_TOOLS")) parts.push(content("T-01_TOOLS"));
if (active("G-06_CHECKLIST")) parts.push(content("G-06_CHECKLIST"));

let system = parts.filter(Boolean).join("\n\n");
system += "\n\n<DATA_FROM_DATABASE>\nЭто актуальные данные по автомобилям и запросу клиента. Используй их для ответов:\n"
    + JSON.stringify(item.ai_clean_context, null, 2) + "\n</DATA_FROM_DATABASE>";
if (turn.finalize_required) {
    system += "\n\n[СИСТЕМНОЕ НАПОМИНАНИЕ]: ТЕЛЕФОН КЛИЕНТА ПОЛУЧЕН. ТЫ ОБЯЗАН ВЫЗВАТЬ ИНСТРУМЕНТ FINALIZE_DEAL В ЭТОМ ХОДЕ.";
}

const transcript = (item.recent_history || []).slice(-8)
    .map(m => `${m.role === "client" ? "Клиент" : "Ассистент"}: ${m.text}`).join("\n");
const agentInput = (transcript ? `История диалога:\n${transcript}\n\n` : "")
    + `Сообщение пользователя: ${item.chatInput || item.message || ""}`;

return [{ json: Object.assign({}, item, {
    system_prompt_compiled: system,
    agent_input: agentInput,
    prompt_tokens_est: Math.round(system.length / 4),
    // Диагностика источника данных дилера: видно прямо в списке исполнений,
    // у кого карточка в Dealers пустая и кто ещё сидит на боте без 5b559da.
    // "payload" — все три пришли полями; "fallback" — ни одного; "partial" — часть.
    dealer_fields_source: dealerMissing.length === 0 ? "payload"
        : (dealerMissing.length === Object.keys(DEALER_FALLBACK).length ? "fallback" : "partial"),
    dealer_fields_missing: dealerMissing,
})}];
"""

TOOL_GUARD_JS = r"""
// --- признак обрыва генерации ---------------------------------------------
// Прямой признак — finish_reason саб-ноды LM — из code-ноды недостижим
// (проверено перебором 14 способов доступа, 2026-08-19). Оставлен в
// конструкции: заработает сам, если доступ появится. Сегодня всегда пуст,
// это видно в диагностике lm_signal.
var LM_ABORT = ["error", "length", "content_filter"];

function lmFinish(nodeName) {
    var reasons = [], signal = "empty";
    try {
        for (var run = 0; run < 12; run++) {
            var items = null;
            try { items = $(nodeName).all(0, run); } catch (e) { break; }
            if (!items || !items.length) break;
            for (var i = 0; i < items.length; i++) {
                var resp = ((items[i] || {}).json || {}).response || {};
                var gens = resp.generations || [];
                for (var g = 0; g < gens.length; g++) {
                    var grp = Array.isArray(gens[g]) ? gens[g] : [gens[g]];
                    for (var k = 0; k < grp.length; k++) {
                        var fr = ((grp[k] || {}).generationInfo || {}).finish_reason;
                        if (fr) reasons.push(String(fr));
                    }
                }
            }
        }
        signal = reasons.length ? "ok" : "empty";
    } catch (e) {
        signal = "unavailable:" + String((e && e.message) || e).slice(0, 80);
    }
    return { reasons: reasons, signal: signal };
}

// Берём ПОСЛЕДНИЙ прогон: на ходах с инструментом их несколько и штатная
// последовательность выглядит как ["tool_calls", "stop"]. Правило «хоть один
// не stop» давало бы ложный повтор на каждом таком ходе.
// Фолбэк-модель отрабатывает после основной, её вердикт старше.
function lmVerdict() {
    var main = lmFinish("Agent LM");
    var fb = lmFinish("Agent LM Fallback");
    var reasons = fb.reasons.length ? fb.reasons : main.reasons;
    var last = reasons.length ? reasons[reasons.length - 1] : "";
    return {
        last: last, reasons: reasons,
        aborted: LM_ABORT.indexOf(last) >= 0,
        signal: fb.reasons.length ? fb.signal : main.signal,
    };
}

// Оборванный ответ кончается посреди фразы. Корректный по G-02_VOICE_V2 п.6
// заканчивается ровно одним вопросом — то есть завершающим знаком.
// Три исключения, где отсутствие знака штатно:
//   * адрес в конце: ставить после него знак ЗАПРЕЩЕНО (BLK_*_CABINET_CHOSEN);
//   * эмодзи в конце;
//   * закрывающая кавычка или скобка.
// Пустой текст здесь НЕ считается обрывом — его ловит отдельное правило.
function looksTruncated(s) {
    var t = String(s || "").trim();
    if (!t) return false;
    if (/[.!?…»)"']$/.test(t)) return false;
    if (/https?:\/\/\S+$/.test(t)) return false;
    try { if (/\p{Extended_Pictographic}$/u.test(t)) return false; } catch (e) {}
    return true;
}

function abortVerdict(text) {
    var lm = lmVerdict();
    var shape = looksTruncated(text);
    return {
        aborted: lm.aborted || shape,
        source: lm.aborted ? "finish_reason" : (shape ? "shape" : ""),
        truncated_shape: shape,
        finish_reason: lm.last, finish_reasons: lm.reasons, lm_signal: lm.signal,
    };
}

const item = $('Prompt Compiler').first().json;
const out = $input.first().json;
let text = String(out.output ?? out.text ?? "").trim();
text = text.replace(/<function_calls>[\s\S]*?<\/function_calls>/g, "").trim();

const steps = out.intermediateSteps || [];
const calledFinalize = steps.some(s => String((s.action || {}).tool || "").toLowerCase().includes("finalize"));
const needFinalize = item.turn.finalize_required && !calledFinalize;

const ab = abortVerdict(text);

// Порядок правил сохранён как был, новое добавлено ПОСЛЕДНИМ — так прежнее
// поведение не меняется нигде, кроме случая, который раньше не ловился совсем.
// Почему обрыв стоит ПОСЛЕ длины: ответ длиннее 4000 знаков обрывается на
// лимите токенов, и повтор воспроизвёл бы ту же простыню; сжатие лечит, повтор
// нет. Тест «сверхдлинный → rewrite» ловит эту перестановку, если её сдвинуть.
let action = "ok";
if (out.error || !text) action = "retry";            // пустой/ошибочный ответ — один повтор
else if (needFinalize) action = "retry";             // пропущен обязательный finalize
else if (text.length > 4000) action = "rewrite";     // грубое нарушение формата — дешёвый rewrite
else if (ab.aborted) action = "retry";               // генерация оборвалась — текст неполный

return [{ json: Object.assign({}, item, {
    guard: {
        action, called_finalize: calledFinalize, need_finalize: needFinalize,
        // диагностика: по ней guard_report.py сверяет эвристику с finish_reason
        aborted: ab.aborted, abort_source: ab.source, truncated_shape: ab.truncated_shape,
        finish_reason: ab.finish_reason, finish_reasons: ab.finish_reasons,
        lm_signal: ab.lm_signal,
    },
    draft_text: text,
})}];
"""

POST_GUARD_JS = r"""
// --- признак обрыва генерации ---------------------------------------------
// Прямой признак — finish_reason саб-ноды LM — из code-ноды недостижим
// (проверено перебором 14 способов доступа, 2026-08-19). Оставлен в
// конструкции: заработает сам, если доступ появится. Сегодня всегда пуст,
// это видно в диагностике lm_signal.
var LM_ABORT = ["error", "length", "content_filter"];

function lmFinish(nodeName) {
    var reasons = [], signal = "empty";
    try {
        for (var run = 0; run < 12; run++) {
            var items = null;
            try { items = $(nodeName).all(0, run); } catch (e) { break; }
            if (!items || !items.length) break;
            for (var i = 0; i < items.length; i++) {
                var resp = ((items[i] || {}).json || {}).response || {};
                var gens = resp.generations || [];
                for (var g = 0; g < gens.length; g++) {
                    var grp = Array.isArray(gens[g]) ? gens[g] : [gens[g]];
                    for (var k = 0; k < grp.length; k++) {
                        var fr = ((grp[k] || {}).generationInfo || {}).finish_reason;
                        if (fr) reasons.push(String(fr));
                    }
                }
            }
        }
        signal = reasons.length ? "ok" : "empty";
    } catch (e) {
        signal = "unavailable:" + String((e && e.message) || e).slice(0, 80);
    }
    return { reasons: reasons, signal: signal };
}

// Берём ПОСЛЕДНИЙ прогон: на ходах с инструментом их несколько и штатная
// последовательность выглядит как ["tool_calls", "stop"]. Правило «хоть один
// не stop» давало бы ложный повтор на каждом таком ходе.
// Фолбэк-модель отрабатывает после основной, её вердикт старше.
function lmVerdict() {
    var main = lmFinish("Agent LM");
    var fb = lmFinish("Agent LM Fallback");
    var reasons = fb.reasons.length ? fb.reasons : main.reasons;
    var last = reasons.length ? reasons[reasons.length - 1] : "";
    return {
        last: last, reasons: reasons,
        aborted: LM_ABORT.indexOf(last) >= 0,
        signal: fb.reasons.length ? fb.signal : main.signal,
    };
}

// Оборванный ответ кончается посреди фразы. Корректный по G-02_VOICE_V2 п.6
// заканчивается ровно одним вопросом — то есть завершающим знаком.
// Три исключения, где отсутствие знака штатно:
//   * адрес в конце: ставить после него знак ЗАПРЕЩЕНО (BLK_*_CABINET_CHOSEN);
//   * эмодзи в конце;
//   * закрывающая кавычка или скобка.
// Пустой текст здесь НЕ считается обрывом — его ловит отдельное правило.
function looksTruncated(s) {
    var t = String(s || "").trim();
    if (!t) return false;
    if (/[.!?…»)"']$/.test(t)) return false;
    if (/https?:\/\/\S+$/.test(t)) return false;
    try { if (/\p{Extended_Pictographic}$/u.test(t)) return false; } catch (e) {}
    return true;
}

function abortVerdict(text) {
    var lm = lmVerdict();
    var shape = looksTruncated(text);
    return {
        aborted: lm.aborted || shape,
        source: lm.aborted ? "finish_reason" : (shape ? "shape" : ""),
        truncated_shape: shape,
        finish_reason: lm.last, finish_reasons: lm.reasons, lm_signal: lm.signal,
    };
}

const out = $input.first().json;
let text = String(out.output ?? out.text ?? "").trim()
    .replace(/<function_calls>[\s\S]*?<\/function_calls>/g, "").trim();

// Повтор — ПОСЛЕДНЯЯ попытка: пути назад в Tool Guard нет, дальше только
// Final Response, поэтому зациклиться невозможно по построению графа.
// Если оборвалась и она, отдавать обрубок нельзя — он выглядит законченным
// ответом. Отдаём ту же служебную фразу, которой Final Response уже закрывает
// пустой текст: она согласована и ничего не выдумывает.
const RETRY_FALLBACK = "Прошу прощения, ваше сообщение до меня не дошло — видимо, сбой на нашей стороне. Напишите ещё раз, пожалуйста.";

const ab = abortVerdict(text);
const failed = ab.aborted || !text;
if (failed) text = RETRY_FALLBACK;

return [{ json: {
    final_text: text,
    retry_guard: {
        aborted: failed, used_fallback: failed, abort_source: ab.source,
        truncated_shape: ab.truncated_shape, finish_reason: ab.finish_reason,
        finish_reasons: ab.finish_reasons, lm_signal: ab.lm_signal,
    },
}}];
"""

POST_REWRITE_JS = r"""
const out = $input.first().json;
return [{ json: { final_text: String(out.text ?? out.output ?? "").trim() } }];
"""

FINAL_RESPONSE_JS = EVAL_KEYS_JS + r"""
// [U3] Свежая оценка этого хода — из intermediateSteps агента. Канал уже
// используется Tool Guard для детекции finalize, у обоих агентов включён
// returnIntermediateSteps. Читаем ОБА: Post Guard шагов не переносит, поэтому
// оценка, выполненная на retry-ходе, иначе потерялась бы.
//
// Извлечение — ПО ФОРМЕ ДАННЫХ, а не по фиксированному пути и не по имени тула.
// Причина: путь и имена на практике оказались нестабильны трижды подряд —
//   #2999: observation приходит массивом item'ов в строке, а не объектом;
//   #3050: нода Merge Calc снимает обёртку response, и tradein_evaluation
//          лежит на верхнем уровне, а не в item.response;
//   #3048: тул называется Combo_Calculator (подчёркивание), а не Combo Calculator.
// Поэтому ниже сканируются оба возможных контейнера (сам item и item.response)
// и обе известные формы записи оценки. Форма — единственное, что стабильно.
function evalRecordsFrom(node) {
    const recs = [];
    if (!node || typeof node !== "object" || Array.isArray(node)) return recs;
    // Комбо-форма: оценка вложена в tradein_evaluation.
    const te = node.tradein_evaluation;
    if (te && typeof te === "object" && !Array.isArray(te)) {
        recs.push({ status: te.status, value: Number(te.estimated_value), found: te.found_info });
    }
    // Форма чистого трейд-ина: воркфлоу оценки отдаёт поля верхним уровнем.
    if (node.status !== undefined && node.tradein_price_to !== undefined) {
        recs.push({ status: node.status, value: Number(node.tradein_price_to), found: node.found_info });
    }
    return recs;
}

function evalFromSteps() {
    const stepsOf = (name) => {
        try { return $(name).first().json.intermediateSteps || []; } catch (e) { return []; }
    };
    const out = {};
    for (const st of stepsOf('Agent').concat(stepsOf('Agent Retry'))) {
        const action = (st || {}).action || {};
        let obs = (st || {}).observation;
        if (typeof obs === "string") { try { obs = JSON.parse(obs); } catch (e) { continue; } }
        if (!obs || typeof obs !== "object") continue;

        const items = Array.isArray(obs) ? obs : [obs];
        for (const item of items) {
            if (!item || typeof item !== "object" || Array.isArray(item)) continue;
            // Оба возможных контейнера: сам item и обёртка response, если она есть.
            const records = evalRecordsFrom(item).concat(evalRecordsFrom(item.response));
            for (const rec of records) {
                if (rec.status !== "SUCCESS") continue;   // NOT_FOUND / IN_PROGRESS / INVALID_DATA
                const fi = rec.found;
                if (fi && typeof fi === "object" && !Array.isArray(fi) && Object.keys(fi).length) {
                    // Госномер уже вложен воркфлоу оценки (U1); toolInput — фолбэк на
                    // случай, если пришёл found_info без него.
                    const rawPlate = (action.toolInput || {}).plate;
                    out.found_info = (!fi.plate && rawPlate)
                        ? Object.assign({}, fi, { plate: String(rawPlate) })
                        : fi;
                }
                if (rec.value > 0) out.ti_valuation = rec.value;
            }
        }
    }
    return out;                                              // последний успешный вызов побеждает
}

function safe(name) { try { return $(name).first().json; } catch (e) { return null; } }
const g = $('Tool Guard').first().json;
let text = g.draft_text;
if (g.guard.action === "retry") {
    const post = safe('Post Guard');
    if (post && post.final_text) text = post.final_text;
} else if (g.guard.action === "rewrite") {
    const rew = safe('Post Rewrite');
    if (rew && rew.final_text) text = rew.final_text;
}
const base = g.fresh_cache || (g.qualification_context || {}).cached_json || null;
// Приоритет у свежей оценки: если она отработала на этом ходе, в кэш идёт её
// результат, иначе — carryover. На первом же ходе с оценкой carryover ещё пуст
// (бот его только получит с этого ответа) — данные берутся из observation тула.
const cache = withEvalKeys(base, Object.assign(
    {}, evalFromCarryover(g.qualification_context), evalFromSteps()));
const r = g.resolver || {};
return [{ json: {
    text: text || "Прошу прощения, ваше сообщение до меня не дошло — видимо, сбой на нашей стороне. Напишите ещё раз, пожалуйста.",
    json_cache: cache,
    session_context_update: {
        stage: g.turn.stage, dialog_intent: g.turn.intent,
        key_data_extracted: r.key_data_extracted || {},
        resolver_verdict: r.verdict || "", guard_action: g.guard.action,
    },
}}];
"""

# Служебная фраза при неустранимо пустом ответе живёт в ДВУХ узлах: Post Guard
# подставляет её после провалившегося повтора, Final Response — как последний
# рубеж, если текст всё-таки пуст. Тексты обязаны совпадать: клиент не должен
# получать две разные формулировки на одну и ту же ситуацию. Правка одного из
# двух мест роняет сборку здесь, а не тихо расходится в проде.
RETRY_FALLBACK_TEXT = ("Прошу прощения, ваше сообщение до меня не дошло — "
                       "видимо, сбой на нашей стороне. Напишите ещё раз, пожалуйста.")
for _body_name, _body in (("POST_GUARD_JS", POST_GUARD_JS), ("FINAL_RESPONSE_JS", FINAL_RESPONSE_JS)):
    if _body.count(RETRY_FALLBACK_TEXT) != 1:
        raise SystemExit(f"{_body_name}: служебная фраза расходится с RETRY_FALLBACK_TEXT "
                         f"(найдено вхождений: {_body.count(RETRY_FALLBACK_TEXT)}). "
                         f"Правьте обе копии одновременно.")

# Ход-напоминание идёт мимо Cache Gate (Route -> Reminder Chain), поэтому влить
# carryover здесь безопасно: гейт на этом пути не исполняется вовсе. Без этого
# напоминание вернуло бы кэш без ключей оценки, и снапшот кабинета, который бот
# морозит на каждом ходе, обнулился бы на ровном месте.
REMINDER_RESPOND_JS = EVAL_KEYS_JS + r"""
const item = $('Edit Fields').first().json;
const base = (item.qualification_context || {}).cached_json || null;
return [{ json: {
    text: String($input.first().json.text ?? "").trim(),
    json_cache: withEvalKeys(base, evalFromCarryover(item.qualification_context)),
}}];
"""


# [2026-08-21] Имя модели на ходе передачи. Бот шлёт
# qualification_context.model только со ВТОРОГО хода; на INIT_AI_HANDOFF в
# payload лишь числовой model_id, и приветствие выходило как «получил вашу
# заявку на покупку автомобиля Elite» — без модели. Замер по логам: на всех
# девяти INIT-ходах поле было пустым, на всех последующих — заполненным.
#
# Имя при этом уже лежало внутри n8n дважды: Call Car Search его вернул, Attach
# Cache положил и в fresh_cache, и в qualification_context.cached_json. Трим-ветки
# читали только data.model || qc.model и в кэш не заглядывали — при том что
# соседние комплектации запасную цепочку имеют.
#
# Правка живёт ЗДЕСЬ, а не в замороженной копии кода: трим-логика выводится из
# боевого v2 дословно (гарантия паритета срезов), и подменять её константой
# значило бы потерять эту гарантию. Поэтому — точечная замена одной строки в
# каждой ветке, с жёсткой проверкой попадания: если v2 изменит форму строки,
# генератор упадёт, а не тихо выпустит воркфлоу без фолбэка.
MODEL_FALLBACK = {
    # сценарий: (что ищем, чем заменяем)
    "stock": ('"model": data.model || (data.qualification_context ? data.qualification_context.model : ""),',
              '"model": data.model'
              ' || (data.qualification_context ? data.qualification_context.model : "")'
              ' || ((data.qualification_context || {}).cached_json || {}).model'
              ' || (data.fresh_cache || {}).model || "",'),
}


def apply_model_fallback(code, scen, param):
    """Добавляет кэш и свежий поиск в цепочку источников имени модели."""
    if scen in MODEL_FALLBACK:
        old, new = MODEL_FALLBACK[scen]
    else:
        old = f'"model": {param}.model || qc.model || "",'
        new = (f'"model": {param}.model || qc.model || cached.model'
               f' || ({param}.fresh_cache || {{}}).model || "",')
    if new in code:
        return code
    if code.count(old) != 1:
        raise RuntimeError(
            f"trim_{scen}: строка выбора модели не найдена или неоднозначна "
            f"({code.count(old)} вхождений). Форма в боевом v2 изменилась — "
            f"почините MODEL_FALLBACK, не отключайте проверку.")
    return code.replace(old, new)


def build_data_selector_js():
    funcs = []
    for scen, (node_name, param) in SCENARIO_TRIM_NODES.items():
        code = OLD_NODES[node_name]["parameters"]["jsCode"]
        first_line_variants = (f"let {param} = $input.item.json;", f"const {param} = $input.item.json;")
        for fl in first_line_variants:
            if fl in code:
                code = code.replace(fl, "", 1)
                break
        else:
            raise RuntimeError(f"не найдена строка входа в {node_name}")
        code = apply_model_fallback(code, scen, param)
        funcs.append(f"function trim_{scen}({param}) {{\n{code}\n}}")
    dispatcher = r"""
const item = $('Stage Classifier').first().json;
const scenario = (item.session_context || {}).request_type
    || (item.qualification_context || {}).request_type || "consult";
const fn = { credit: trim_credit, consult: trim_consult, stock: trim_stock,
             tradein: trim_tradein, combo: trim_combo }[scenario] || trim_consult;
const res = fn(Object.assign({}, item));

// [U5] Переменные личного кабинета — единой точкой, а не правкой пяти веток:
// trim-логика перенесена дословно из legacy и является гарантией паритета.
// Ключей может не быть в qualification_context ВОВСЕ (на стороне бота выдача
// токена обёрнута в try/except), поэтому || "" — он нормализует и отсутствие
// ключа, и пустое значение в одно: кабинет не упоминаем в обоих случаях.
const qcCab = item.qualification_context || {};
const cabinet = {
    cabinet_url: qcCab.cabinet_url || "",
    cabinet_status: qcCab.cabinet_status || "",
};

// [U6] Оценка трейд-ина — в контекст модели. Без этого правило «оценка уже есть,
// повторно инструмент не вызывай» неисполнимо: активные D_TRADEIN / D_COMBO прямо
// говорят «JSON — твой ЕДИНСТВЕННЫЙ источник правды», а суммы в JSON не было вовсе,
// поэтому модель обязана была звать платный evaluate_auto заново на каждом
// возврате к цифрам. Источники — те же, что у U3, в том же приоритете.
// Госномер и пробег отданы вместе с суммой: без них «повторный вызов только при
// смене номера или пробега» модели не на чем проверить.
const evalSrc = qcCab.evaluation_carryover || {};
const cachedCab = qcCab.cached_json || {};
const tiValue = Number(evalSrc.ti_valuation) > 0 ? Number(evalSrc.ti_valuation)
              : (Number(cachedCab.ti_valuation) > 0 ? Number(cachedCab.ti_valuation) : 0);
const tiCar = (evalSrc.found_info && evalSrc.found_info.plate) ? evalSrc.found_info
            : ((cachedCab.found_info && cachedCab.found_info.plate) ? cachedCab.found_info : null);
const evaluation = {};
if (tiValue > 0) evaluation.ti_valuation = tiValue;
if (tiCar) evaluation.ti_evaluated_car = { plate: tiCar.plate || "", mileage: tiCar.mileage || 0 };

const out = (res && res.ai_clean_context)
    ? Object.assign({}, res, {
        ai_clean_context: Object.assign({}, res.ai_clean_context, cabinet, evaluation) })
    : res;
return [{ json: Object.assign({}, out) }];
"""
    return "// [U10] Дословный перенос trim-логики пяти веток старого воркфлоу\n" + \
        "\n\n".join(funcs) + "\n" + dispatcher


# Защита вебхука от чужих вызовов (docs/handoff/2026-10-03-n8n-webhook-auth-bot-plan.md, шаг 7).
# Боевой credential включён по команде владельца 2026-10-07 13:34Z (prod-agent-auth,
# 9ea3fc0d → 4fbba141); None снимет защиту с боевого агента при следующей записи.
# [TEST]-копию build_test_contour.py защищает тестовым секретом всегда.
INBOUND_AUTH = {"id": "CfzDWuMkUZABPQnM", "name": "N8N Inbound Secret [PROD]"}


def webhook_auth(params):
    """Header Auth по INBOUND_AUTH: (параметры ноды, credentials или None)."""
    if not INBOUND_AUTH:
        return params, None
    return dict(params, authentication="headerAuth"), {"httpHeaderAuth": dict(INBOUND_AUTH)}


def node(name, ntype, tv, params, pos, creds=None, extra=None):
    n = {"parameters": params, "type": ntype, "typeVersion": tv, "position": pos,
         "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"v3/{name}")), "name": name}
    if creds:
        n["credentials"] = creds
    if extra:
        n.update(extra)
    return n


# Тул-ноды: (имя в боевом v2 — откуда клонируем, имя узла в v3 — как назовём).
#
# ИМЕНА РАСХОДЯТСЯ СОЗНАТЕЛЬНО, это не опечатка и «чинить» обратно нельзя.
# n8n объявляет модели имя функции по имени УЗЛА, заменяя пробелы на
# подчёркивания: узел «Combo Calculator» превращается в функцию
# `Combo_Calculator`. А промпт-блоки комбо-сценария (CS_COMBO_V2,
# ST_COMBO_TOOL_NEEDED_V2) предписывают вызвать `combo_calculator` — регистр
# не совпадает. gemini-2.5-flash-lite следовала промпту буквально, просила
# необъявленную функцию и получала от Google UNEXPECTED_TOOL_CALL: инструмент
# не вызывался, агент возвращал пустой текст, клиенту уходила служебная фраза
# про техническую заминку (исполнение n8n 3517, 2026-08-28; трейд-ин в тот же
# день работал, потому что там узел зовётся evaluate_auto и расхождения нет).
#
# Решение владельца: переименовать УЗЕЛ, а не править семь блоков. Заодно все
# три инструмента становятся единообразными: finalize_deal, evaluate_auto,
# combo_calculator.
#
# Клонируем при этом по-прежнему по имени v2: в боевом AI Agent v2.0 MAX узел
# называется «Combo Calculator», и его мы не трогаем. Позиция и id узла тоже
# считаются от имени v2 — id остаётся стабильным между перегенерациями.
TOOL_NODES = (
    ("finalize_deal", "finalize_deal"),
    ("evaluate_auto", "evaluate_auto"),
    ("Combo Calculator", "combo_calculator"),
)
TOOL_SRC_NAMES = tuple(src for src, _ in TOOL_NODES)


def clone_tool(src_name, node_name=None):
    src = OLD_NODES[src_name]
    params = json.loads(json.dumps(src["parameters"]))
    # v2 указывает на исторические прод-тулы; боевой v3 ходит в другие (см.
    # TOOL_WF_OVERRIDE). Подпись cachedResultName остаётся от v2 — она совпадает
    # с текущим именем целевого воркфлоу и служит ключом для build_test_contour.
    override = TOOL_WF_OVERRIDE.get(node_name or src_name)
    if override:
        wid = params.get("workflowId")
        if not isinstance(wid, dict) or "value" not in wid:
            raise SystemExit(f"clone_tool: у узла {src_name} нет workflowId — "
                             f"форма ноды в боевом v2 изменилась, почините TOOL_WF_OVERRIDE")
        wid["value"] = override
    return {"parameters": params, "type": src["type"],
            "typeVersion": src["typeVersion"],
            "position": [1500, 560 + 160 * TOOL_SRC_NAMES.index(src_name)],
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"v3/tool/{src_name}")),
            "name": node_name or src_name,
            **({"credentials": src["credentials"]} if "credentials" in src else {})}


def switch_rule(right):
    return {"conditions": {"options": {"caseSensitive": True, "leftValue": "",
                                       "typeValidation": "loose", "version": 3},
                           "conditions": [{"leftValue": "={{ $json.message }}", "rightValue": right,
                                           "operator": {"type": "string", "operation": "equals"}}],
                           "combinator": "and"}}


# [U1, план 2026-08-14-001] Ветка напоминаний идёт мимо Prompt Compiler
# (Route -> Reminder Chain), блоки туда не собираются в принципе — поэтому
# различить случай «ссылка на кабинет выдана, клиент не авторизовался» можно
# только правкой текста промпта. Обе переменные уже приходят в вебхуке на этой
# ветке (проверено на живых исполнениях), новых входов не требуется.
#
# Вставка дописывается к унаследованному legacy-тексту, а не заменяет его:
# при пустом cabinet_status выражение отдаёт пустую строку и промпт остаётся
# прежним символ в символ.
REMINDER_CABINET_BRANCH = (
    "\n\n{{ ((($json.qualification_context || {}).cabinet_status || \"\") === \"not_visited\""
    " && (($json.qualification_context || {}).cabinet_url || \"\"))"
    " ? \"ОСОБЫЙ СЛУЧАЙ: клиенту уже выдана ссылка на личный кабинет, и он ещё не"
    " подтвердил там номер. Не перефразируй прошлое сообщение — вместо этого спроси,"
    " удалось ли ему разобраться в кабинете и нужна ли помощь. Одно предложение.\""
    " : \"\" }}"
)

REMINDER_PROMPT = OLD_NODES["Basic LLM Chain"]["parameters"]["text"] + REMINDER_CABINET_BRANCH

nodes = [
    node("Webhook", "n8n-nodes-base.webhook", 2.1,
         webhook_auth({"httpMethod": "POST", "path": "ai-handoff-v3", "responseMode": "responseNode", "options": {}})[0],
         [0, 0], creds=webhook_auth({})[1],
         extra={"webhookId": str(uuid.uuid5(uuid.NAMESPACE_URL, "v3/webhook-id"))}),
    node("Edit Fields", "n8n-nodes-base.code", 2, {"jsCode": EDIT_FIELDS_JS.strip()}, [200, 0]),
    node("Route", "n8n-nodes-base.switch", 3.2,
         {"rules": {"values": [switch_rule("INIT_AI_HANDOFF"), switch_rule("__SYSTEM_REMINDER__")]},
          "options": {"fallbackOutput": "extra"}}, [400, 0]),
    # INIT
    node("Preset Greeting", "n8n-nodes-base.code", 2, {"jsCode": PRESET_GREETING_JS.strip()}, [620, -200]),
    # reminder
    node("Reminder Chain", "@n8n/n8n-nodes-langchain.chainLlm", 1.9,
         {"promptType": "define", "text": REMINDER_PROMPT, "batching": {}}, [620, 260]),
    node("Reminder LM", "@n8n/n8n-nodes-langchain.lmChatOpenRouter", 1,
         {"model": MODEL_REMINDER, "options": {}}, [620, 430], OPENROUTER_CRED),
    node("Reminder Respond", "n8n-nodes-base.code", 2, {"jsCode": REMINDER_RESPOND_JS.strip()}, [820, 260]),
    # normal
    node("Run Resolver", "n8n-nodes-base.executeWorkflow", 1.3,
         {"workflowId": RESOLVER_CORE_WF,
          # как проставляет редактор n8n у ноды без входных полей (= бой, план 2026-10-09-001)
          "workflowInputs": {"mappingMode": "defineBelow", "value": {}, "matchingColumns": [], "schema": [],
                             "attemptToConvertTypes": False, "convertFieldsToString": True},
          "options": {"waitForSubWorkflow": True}}, [620, 60]),
    node("Attach Resolver", "n8n-nodes-base.code", 2, {"jsCode": ATTACH_RESOLVER_JS.strip()}, [800, 60]),
    node("Cache Gate", "n8n-nodes-base.if", 2.3,
         {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 3},
                         "conditions": [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "v3/cachecond")),
                                         "leftValue": "={{ $json.qualification_context.cached_json ? \"YES\" : \"\" }}",
                                         "rightValue": "", "operator": {"type": "string", "operation": "notEmpty",
                                                                        "singleValue": True}}],
                         "combinator": "and"}, "options": {}}, [1000, -60]),
    node("Call Car Search", "n8n-nodes-base.executeWorkflow", 1.3,
         {"workflowId": CAR_SEARCH_WF, "options": {"waitForSubWorkflow": True}}, [1180, 60]),
    node("Attach Cache", "n8n-nodes-base.code", 2, {"jsCode": ATTACH_CACHE_JS.strip()}, [1360, 60]),
    node("Stage Classifier", "n8n-nodes-base.code", 2, {"jsCode": STAGE_CLASSIFIER_JS.strip()}, [1560, -60]),
    node("Get Blocks", "n8n-nodes-base.nocoDb", 3,
         {"authentication": "nocoDbApiToken", "operation": "getAll", "projectId": BASE_ID,
          "table": IDS["Prompt_Blocks"], "returnAll": True, "options": {}}, [1740, -60], NOCO_CRED,
         extra={"retryOnFail": True, "maxTries": 2, "waitBetweenTries": 1000,
                "executeOnce": True}),  # иначе нода бежит по разу на каждый входной item
    node("Get Map", "n8n-nodes-base.nocoDb", 3,
         {"authentication": "nocoDbApiToken", "operation": "getAll", "projectId": BASE_ID,
          "table": IDS["Intent_Block_Map"], "returnAll": True, "options": {}}, [1920, -60], NOCO_CRED,
         extra={"retryOnFail": True, "maxTries": 2, "waitBetweenTries": 1000,
                "executeOnce": True}),  # 54 входных item'а дали бы 54 чтения и 54×108 строк
    node("Bypass Gate", "n8n-nodes-base.if", 2.3,
         {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 3},
                         "conditions": [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "v3/bypasscond")),
                                         "leftValue": "={{ $('Stage Classifier').first().json.turn.bypass || \"\" }}",
                                         "rightValue": "", "operator": {"type": "string", "operation": "notEmpty",
                                                                        "singleValue": True}}],
                         "combinator": "and"}, "options": {}}, [2100, -60]),
    node("Bypass Build", "n8n-nodes-base.code", 2, {"jsCode": BYPASS_BUILD_JS.strip()}, [2280, -220]),
    node("Save CRM Direct", "n8n-nodes-base.executeWorkflow", 1.3,
         {"workflowId": SAVE_CRM_WF,
          "workflowInputs": {"mappingMode": "defineBelow",
                             "value": {"ai_payload": "={{ $json.ai_payload }}",
                                       "user_id": "={{ $json.user_id }}",
                                       "deal_id": "={{ $json.deal_id }}"},
                             # schema — как у боевой ноды (её заполняет редактор n8n)
                             "matchingColumns": [], "schema": [{"id": k, "displayName": k, "required": False, "defaultMatch": False,
                                         "display": True, "canBeUsedToMatch": True, "type": "string"}
                                        for k in ("user_id", "deal_id", "ai_payload")],
                             "attemptToConvertTypes": False, "convertFieldsToString": True},
          "options": {"waitForSubWorkflow": True}}, [2460, -220],
         extra={"onError": "continueRegularOutput"}),
    node("Bypass Respond", "n8n-nodes-base.code", 2, {"jsCode": BYPASS_RESPOND_JS.strip()}, [2640, -220]),
    node("Data Selector", "n8n-nodes-base.code", 2, {"jsCode": build_data_selector_js().strip()}, [2280, 40]),
    node("Prompt Compiler", "n8n-nodes-base.code", 2, {"jsCode": PROMPT_COMPILER_JS.strip()}, [2460, 40]),
    node("Agent", "@n8n/n8n-nodes-langchain.agent", 3.1,
         {"promptType": "define", "text": "={{ $json.agent_input }}", "needsFallback": True,
          "options": {"systemMessage": "={{ $json.system_prompt_compiled }}",
                      "returnIntermediateSteps": True}}, [2660, 40],
         extra={"onError": "continueRegularOutput"}),
    node("Agent LM", "@n8n/n8n-nodes-langchain.lmChatOpenRouter", 1,
         {"model": MODEL_AGENT, "options": {}}, [2620, 240], OPENROUTER_CRED),
    node("Agent LM Fallback", "@n8n/n8n-nodes-langchain.lmChatOpenAi", 1.3,
         {"model": {"__rl": True, "value": MODEL_AGENT_FALLBACK, "mode": "list",
                    "cachedResultName": MODEL_AGENT_FALLBACK},
          "responsesApiEnabled": False, "options": {}}, [2760, 240], OPENAI_VIA_OPENROUTER_CRED),
    node("Tool Guard", "n8n-nodes-base.code", 2, {"jsCode": TOOL_GUARD_JS.strip()}, [2900, 40]),
    node("Guard Route", "n8n-nodes-base.switch", 3.2,
         {"rules": {"values": [
             {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 3},
                             "conditions": [{"leftValue": "={{ $json.guard.action }}", "rightValue": "retry",
                                             "operator": {"type": "string", "operation": "equals"}}],
                             "combinator": "and"}},
             {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 3},
                             "conditions": [{"leftValue": "={{ $json.guard.action }}", "rightValue": "rewrite",
                                             "operator": {"type": "string", "operation": "equals"}}],
                             "combinator": "and"}},
         ]}, "options": {"fallbackOutput": "extra"}}, [3080, 40]),
    node("Agent Retry", "@n8n/n8n-nodes-langchain.agent", 3.1,
         {"promptType": "define", "text": "={{ $json.agent_input }}", "needsFallback": True,
          "options": {"systemMessage": "={{ $json.system_prompt_compiled + \"\\n\\n[СИСТЕМНОЕ НАПОМИНАНИЕ]: ПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН КОНТРОЛЕМ КАЧЕСТВА. \" + ($json.guard.need_finalize ? \"ТЫ ОБЯЗАН ВЫЗВАТЬ ИНСТРУМЕНТ FINALIZE_DEAL ПРЯМО СЕЙЧАС.\" : \"ДАЙ КОРРЕКТНЫЙ, ПОЛНЫЙ ТЕКСТОВЫЙ ОТВЕТ КЛИЕНТУ.\") }}",
                      "returnIntermediateSteps": True}}, [3260, -140],
         extra={"onError": "continueRegularOutput"}),
    node("Post Guard", "n8n-nodes-base.code", 2, {"jsCode": POST_GUARD_JS.strip()}, [3460, -140]),
    node("Rewrite Chain", "@n8n/n8n-nodes-langchain.chainLlm", 1.9,
         {"promptType": "define",
          "text": "=Перепиши ответ ассистента автосалона, соблюдая формат: не длиннее 800 символов, без технических вставок и кода, дружелюбный тон, сохрани все цифры и факты, заверши уместным вопросом клиенту.\n\nОтвет:\n{{ $json.draft_text }}",
          "batching": {}}, [3260, 160], extra={"onError": "continueRegularOutput"}),
    node("Post Rewrite", "n8n-nodes-base.code", 2, {"jsCode": POST_REWRITE_JS.strip()}, [3460, 160]),
    node("Final Response", "n8n-nodes-base.code", 2, {"jsCode": FINAL_RESPONSE_JS.strip()}, [3660, 40]),
    node("Respond to Webhook", "n8n-nodes-base.respondToWebhook", 1.5, {"options": {}}, [3860, 40]),
    *[clone_tool(src, dst) for src, dst in TOOL_NODES],
]

conns = {}


def link(src, dst, out=0, ctype="main", target_index=0):
    # target_index — номер ВХОДНОГО порта целевой ноды: у агента с needsFallback
    # порт 0 = основная модель, порт 1 = fallback-модель
    conns.setdefault(src, {}).setdefault(ctype, [])
    while len(conns[src][ctype]) <= out:
        conns[src][ctype].append([])
    conns[src][ctype][out].append({"node": dst, "type": ctype, "index": target_index})


link("Webhook", "Edit Fields")
link("Edit Fields", "Route")
link("Route", "Preset Greeting", 0)
link("Route", "Reminder Chain", 1)
link("Route", "Run Resolver", 2)
link("Reminder Chain", "Reminder Respond")
link("Reminder Respond", "Respond to Webhook")
link("Reminder LM", "Reminder Chain", 0, "ai_languageModel")
link("Preset Greeting", "Cache Gate")
link("Run Resolver", "Attach Resolver")
link("Attach Resolver", "Cache Gate")
link("Cache Gate", "Stage Classifier", 0)
link("Cache Gate", "Call Car Search", 1)
link("Call Car Search", "Attach Cache")
link("Attach Cache", "Stage Classifier")
link("Stage Classifier", "Get Blocks")
link("Get Blocks", "Get Map")
link("Get Map", "Bypass Gate")
link("Bypass Gate", "Bypass Build", 0)
link("Bypass Gate", "Data Selector", 1)
link("Bypass Build", "Save CRM Direct")
link("Save CRM Direct", "Bypass Respond")
link("Bypass Respond", "Respond to Webhook")
link("Data Selector", "Prompt Compiler")
link("Prompt Compiler", "Agent")
link("Agent", "Tool Guard")
link("Tool Guard", "Guard Route")
link("Guard Route", "Agent Retry", 0)
link("Guard Route", "Rewrite Chain", 1)
link("Guard Route", "Final Response", 2)
link("Agent Retry", "Post Guard")
link("Post Guard", "Final Response")
link("Rewrite Chain", "Post Rewrite")
link("Post Rewrite", "Final Response")
link("Final Response", "Respond to Webhook")
link("Agent LM", "Agent", 0, "ai_languageModel")
link("Agent LM Fallback", "Agent", 0, "ai_languageModel", target_index=1)
# Повтор идёт на ЗАПАСНОЙ модели, а не на той же (решение владельца 2026-09-07).
# Первый круг — MODEL_AGENT, и если он вернул пустоту, второй бросок той же
# монеты бессмыслен: в инциденте #3871 подряд пусто вернули обе попытки одной
# модели. gpt-4.1-mini размышлений не тратит, отказы становятся независимыми.
# Порядок связей у самого Agent (выше) НЕ меняется — основной ответ прежний.
link("Agent LM Fallback", "Agent Retry", 0, "ai_languageModel")
link("Agent LM", "Agent Retry", 0, "ai_languageModel", target_index=1)
link("Agent LM", "Rewrite Chain", 0, "ai_languageModel")
# связи идут по имени УЗЛА в v3, а не по имени источника в v2
for _, tool in TOOL_NODES:
    link(tool, "Agent", 0, "ai_tool")
    link(tool, "Agent Retry", 0, "ai_tool")

wf = {"name": "AI Agent v3.0", "nodes": nodes, "connections": conns,
      "settings": {"executionOrder": "v1"}, "pinData": {},
      "meta": {"instanceId": "65c0178e84d5c3db112ec6489beae5f53a11ae74e53d6537d4d9a5cd140a483b"}}
out = FLOW / "AI Agent v3.0.json"
out.write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"OK: {out} ({len(nodes)} нод)")
print("Webhook: /webhook/ai-handoff-v3; ручная перепривязка саб-воркфлоу НЕ требуется — "
      "все шесть ссылок проставлены боевыми ID (см. RESOLVER_CORE_WF/CAR_SEARCH_WF/"
      "SAVE_CRM_WF/TOOL_WF_OVERRIDE). После импорта сверьте модели и привязки.")
