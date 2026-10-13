// ============================================================
// U10: оффлайн-симуляция ключевых Code-нод конвейера v3.0
// (Stage Classifier → Data Selector → Prompt Compiler → Tool Guard →
// Final Response, плюс Bypass) на реальных данных таблиц v3.00.
// Код нод берётся ИЗ СГЕНЕРИРОВАННОГО flow/AI Agent v3.0.json.
// Запуск: node v3_pipeline_sim.test.mjs
// ============================================================
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// снапшот живых таблиц: python3 nocodb/add_intents_v2.py --snapshot-only
const WF = JSON.parse(readFileSync(new URL("../AI Agent v3.0.json", import.meta.url), "utf-8"));
const TABLES = JSON.parse(readFileSync(new URL("./snapshots/v3_tables.json", import.meta.url), "utf-8"));

const code = name => WF.nodes.find(n => n.name === name).parameters.jsCode;

function runNode(nodeName, data, inputItems) {
    const $ = (name) => {
        if (!(name in data)) throw new Error(`нет мока для ноды ${name}`);
        const val = data[name];
        return {
            // all(branch, run): второй аргумент нужен саб-нодам LM — у них прогонов
            // несколько за ход (агент, повтор, rewrite). Мок с ключом __runs отдаёт
            // каждый прогон отдельно; без него прогон 0 ведёт себя как раньше, а
            // всё, что дальше, пусто — иначе цикл по прогонам не завершился бы.
            all: (branch, run) => {
                if (val && val.__runs) {
                    return (val.__runs[run === undefined ? 0 : run] || []).map(j => ({ json: j }));
                }
                if (run !== undefined && run > 0) return [];
                return (Array.isArray(val) ? val : [val]).map(j => ({ json: j }));
            },
            first: () => ({ json: Array.isArray(val) ? val[0] : val }),
        };
    };
    const $input = {
        all: () => inputItems.map(j => ({ json: j })),
        first: () => ({ json: inputItems[0] }),
        item: { json: inputItems[0] },
    };
    return new Function("$", "$input", code(nodeName))($, $input);
}

const CACHE = {
    complectations: [
        { id: "11", name: "Comfort", full_name: "Xray Comfort", features: ["a"], images: { x: 1 },
          specs: { engine: "1.6" }, stock_count: 2, stock_details: { in_stock: 2 }, stock_colors: [],
          price: { MSRP: 2150000, cash_bonus: 100000, base_price: 2050000, credit_price: 1900000,
                   credit_bonus: 150000, tradein_price: 1970000, tradein_bonus: 80000 },
          credit_calculation: { status: "standard" } },
    ],
    user_request: { selected_complectation_id: "11", type: "credit" },
    user_data: { name: "Иван" },
};

function baseItem(overrides = {}) {
    return {
        user_id: "u1", session_id: "s1", message: "хочу взнос поменьше", chatInput: "хочу взнос поменьше",
        deal_id: 555,
        qualification_context: { request_type: "credit", cached_json: CACHE, complectation_id: "11" },
        recent_history: [{ role: "client", text: "привет" }, { role: "bot", text: "здравствуйте!" }],
        session_context: {
            request_type: "credit", current_stage: "data_collect", msg_count: 3,
            phone_ask_count: 0, deal_finalized: false,
            collected_data: { payment: 0, phone_collected: false },
            flags: { has_valid_phone_in_msg: false, has_valid_plate: false, stop_word_detected: false },
        },
        resolver: { verdict: "ok", dialog_intent: "collect_down_payment", client_mode: "collector",
                    client_signal: "hesitating", key_data_extracted: {}, reasoning: "" },
        ...overrides,
    };
}

let passed = 0;
const T = (name, fn) => { fn(); passed++; };

// ---------- Stage Classifier ----------
T("обычный ход: интент резолвера принят, стадия из Python", () => {
    const out = runNode("Stage Classifier", {}, [baseItem()])[0].json;
    assert.equal(out.turn.intent, "collect_down_payment");
    assert.equal(out.turn.stage, "data_collect");
    assert.equal(out.turn.bypass, null);
    assert.equal(out.turn.tools_open, false);
});

T("телефон в сообщении переопределяет резолвер → finalize", () => {
    const item = baseItem();
    item.session_context.flags.has_valid_phone_in_msg = true;
    const out = runNode("Stage Classifier", {}, [item])[0].json;
    assert.equal(out.turn.intent, "finalize_agreed");
    assert.equal(out.turn.stage, "closing");
    assert.equal(out.turn.finalize_required, true);
    assert.equal(out.turn.tools_open, true);
});

T("персистентное условие: phone_collected && !deal_finalized", () => {
    const item = baseItem();
    item.session_context.collected_data.phone_collected = true;
    const out = runNode("Stage Classifier", {}, [item])[0].json;
    assert.equal(out.turn.finalize_required, true);
});

T("стоп-слово → bypass refuse даже при другом интенте", () => {
    const item = baseItem();
    item.session_context.flags.stop_word_detected = true;
    const out = runNode("Stage Classifier", {}, [item])[0].json;
    assert.equal(out.turn.bypass, "refuse");
});

T("INIT после воронки: greeting_first форсирует стадию greeting (баг exec 2339)", () => {
    const item = baseItem({ resolver: { verdict: "preset", dialog_intent: "greeting_first",
                                        key_data_extracted: {}, reasoning: "" } });
    item.session_context.current_stage = "tool_needed";     // эвристика Python: платёж собран
    item.session_context.collected_data.payment = 20000;
    item.session_context.msg_count = 3;                     // кнопки/платёж из воронки в истории
    const out = runNode("Stage Classifier", {}, [item])[0].json;
    assert.equal(out.turn.stage, "greeting", "INIT обязан идти по приветственному протоколу");
    assert.equal(out.turn.tools_open, false, "на приветствии инструменты закрыты (без T-01_TOOLS)");
});

T("невалидный вердикт резолвера → пустой интент (дефолтная сборка)", () => {
    const item = baseItem({ resolver: { verdict: "parse_error" } });
    const out = runNode("Stage Classifier", {}, [item])[0].json;
    assert.equal(out.turn.intent, "");
    assert.equal(out.turn.stage, "data_collect");
});

T("polite_close_satisfied — закрывающий интент, стадия → closing (план 2026-07-17-001)", () => {
    const item = baseItem({ resolver: { verdict: "ok", dialog_intent: "polite_close_satisfied",
                                        client_mode: "direct_agree", client_signal: "satisfied_done",
                                        key_data_extracted: {}, reasoning: "" } });
    const out = runNode("Stage Classifier", {}, [item])[0].json;
    assert.equal(out.turn.intent, "polite_close_satisfied");
    assert.equal(out.turn.stage, "closing");
});

T("request_human_handoff стадию не двигает (falls through к стадии Python)", () => {
    const item = baseItem({ resolver: { verdict: "ok", dialog_intent: "request_human_handoff",
                                        client_mode: "secretary", client_signal: "handoff_requested",
                                        key_data_extracted: {}, reasoning: "" } });
    const out = runNode("Stage Classifier", {}, [item])[0].json;
    assert.equal(out.turn.intent, "request_human_handoff");
    assert.equal(out.turn.stage, "data_collect", "стадия должна остаться от эвристики Python");
});

// ---------- Data Selector (legacy trim) ----------
const classified = runNode("Stage Classifier", {}, [baseItem()])[0].json;
T("credit-срез: тяжёлые поля удалены, price полный, фильтр по id", () => {
    const out = runNode("Data Selector", { "Stage Classifier": classified }, [classified])[0].json;
    const comp = out.ai_clean_context.complectations[0];
    assert.ok(!("features" in comp) && !("images" in comp) && !("specs" in comp));
    assert.equal(comp.price.credit_price, 1900000);
    assert.equal(out.ai_clean_context.complectations.length, 1);
});

// ---------- Prompt Compiler ----------
const selected = runNode("Data Selector", { "Stage Classifier": classified }, [classified])[0].json;
const tablesMock = { "Get Blocks": TABLES.blocks, "Get Map": TABLES.map };

T("сборка: ядро+CS+D+primary+REQUIRED, без TOOLS, с данными", () => {
    const out = runNode("Prompt Compiler", tablesMock, [selected])[0].json;
    const s = out.system_prompt_compiled;
    const b = id => TABLES.blocks.find(x => x.Block_ID === id).Content.slice(0, 60);
    // с 2026-07-16 владелец активировал ST_CREDIT_DATA_COLLECT и выключил WF_CREDIT_LEGACY —
    // primary теперь живой ST-блок, legacy-фолбэк в credit больше не задействован
    for (const id of ["G-01_ROLE", "CS_CREDIT", "D_CREDIT", "ST_CREDIT_DATA_COLLECT", "ST_CREDIT_REQUIRED_DATA", "G-06_CHECKLIST"])
        assert.ok(s.includes(b(id)), `в промпте нет ${id}`);
    assert.ok(!s.includes(b("WF_CREDIT_LEGACY")), "legacy-блок выключен владельцем и не должен попадать");
    assert.ok(!s.includes(b("T-01_TOOLS")), "TOOLS не должен попасть: инструменты закрыты");
    assert.ok(!s.includes(b("OBJ_CREDIT")), "OBJ не должен попасть: интент не возражение");
    assert.ok(s.includes("<DATA_FROM_DATABASE>"));
    assert.ok(out.agent_input.includes("История диалога:"));
    assert.ok(out.agent_input.includes("Сообщение пользователя: хочу взнос поменьше"));
});

T("возражение подключает блок отработки; finalize-ход подключает TOOLS и напоминание", () => {
    const item = structuredClone(selected);
    item.turn.intent = "objection_price";
    let out = runNode("Prompt Compiler", tablesMock, [item])[0].json;
    // с 2026-07-16 владелец выключил монолитные OBJ_* и ведёт возражения primary-блоками
    // (objection_price/credit -> BLK_CREDIT_ADVOCATE); регресс exec 2347 держим на выключенных OBJ
    assert.ok(out.system_prompt_compiled.includes(TABLES.blocks.find(x => x.Block_ID === "BLK_CREDIT_ADVOCATE").Content.slice(0, 60)),
        "нет primary-блока отработки возражения");
    const objTexts = ["OBJ_CREDIT", "OBJ_TRADEIN", "OBJ_COMBO", "OBJ_STOCK", "OBJ_CONSULT"]
        .map(id => TABLES.blocks.find(x => x.Block_ID === id).Content.trim());
    const spNorm = out.system_prompt_compiled;
    const foreign = objTexts.filter(t => spNorm.includes(t));
    assert.equal(foreign.length, 0, "выключенные OBJ-блоки не должны попадать в промпт");

    const fin = structuredClone(selected);
    fin.turn.intent = "finalize_agreed"; fin.turn.stage = "closing";
    fin.turn.tools_open = true; fin.turn.finalize_required = true;
    out = runNode("Prompt Compiler", tablesMock, [fin])[0].json;
    assert.ok(out.system_prompt_compiled.includes("ОБЯЗАН ВЫЗВАТЬ ИНСТРУМЕНТ FINALIZE_DEAL"));
    assert.ok(out.system_prompt_compiled.includes(TABLES.blocks.find(x => x.Block_ID === "T-01_TOOLS").Content.slice(0, 60)));
});

// ---------- Prompt Compiler: v3.01 нарезка TOOLS + VOICE V2 ----------
// состояние после активации владельцем: T_FINALIZE и VOICE_V2 включены, T-01 и старый VOICE выключены
const v301 = structuredClone(TABLES);
const flip = (id, v) => { v301.blocks.find(x => x.Block_ID === id).Active = v; };
flip("T_FINALIZE", true); flip("T-01_TOOLS", false);
flip("G-02_VOICE_V2", true); flip("G-02_VOICE", false);
const v301Mock = { "Get Blocks": v301.blocks, "Get Map": v301.map };
const snip = (id, t = TABLES) => t.blocks.find(x => x.Block_ID === id).Content.trim().slice(0, 60);

T("v3.01: credit/tool_needed — finalize-правила больше не таскаются каждым ходом", () => {
    const item = structuredClone(selected);
    item.turn = { ...item.turn, intent: "ready_for_tool", stage: "tool_needed", tools_open: true, finalize_required: false };
    const out = runNode("Prompt Compiler", v301Mock, [item])[0].json;
    assert.ok(!out.system_prompt_compiled.includes(snip("T_FINALIZE", v301)),
        "правила finalize_deal не должны попадать на tool_needed");
});

T("v3.01: tradein/tool_needed — инструкции evaluate_auto из ST-блока, без finalize-правил", () => {
    const item = structuredClone(selected);
    item.session_context = { ...item.session_context, request_type: "tradein" };
    item.turn = { ...item.turn, intent: "ready_for_tool", stage: "tool_needed", tools_open: true, finalize_required: false };
    const out = runNode("Prompt Compiler", v301Mock, [item])[0].json;
    const s = out.system_prompt_compiled;
    assert.ok(s.includes(snip("ST_TRADEIN_TOOL_NEEDED", v301)), "нет инструкций вызова evaluate_auto");
    assert.ok(!s.includes(snip("T_FINALIZE", v301)), "finalize-правила лишние на tool_needed");
});

T("v3.01: closing/finalize — T_FINALIZE ровно один раз", () => {
    const fin = structuredClone(selected);
    fin.turn = { ...fin.turn, intent: "finalize_agreed", stage: "closing", tools_open: true, finalize_required: true };
    const out = runNode("Prompt Compiler", v301Mock, [fin])[0].json;
    const s = out.system_prompt_compiled;
    const tf = v301.blocks.find(x => x.Block_ID === "T_FINALIZE").Content.trim();
    assert.equal(s.split(tf).length - 1, 1, "T_FINALIZE должен входить ровно один раз");
    assert.ok(s.includes("ОБЯЗАН ВЫЗВАТЬ ИНСТРУМЕНТ FINALIZE_DEAL"));
});

T("v3.01: активный VOICE_V2 вытесняет старый G-02_VOICE", () => {
    const out = runNode("Prompt Compiler", v301Mock, [structuredClone(selected)])[0].json;
    const s = out.system_prompt_compiled;
    assert.ok(s.includes(snip("G-02_VOICE_V2", v301)), "нет сжатого VOICE");
    assert.ok(!s.includes(snip("G-02_VOICE")), "старый VOICE не должен попадать при активном V2");
});

T("переходное состояние (черновики v3.01 выключены — мутация снапшота): поведение старое", () => {
    // с 2026-07-17 владелец активировал T_FINALIZE/VOICE_V2 в живой таблице —
    // легаси-состояние держим регрессом через обратную мутацию снапшота
    const legacy = structuredClone(TABLES);
    const flipBack = (id, v) => { legacy.blocks.find(x => x.Block_ID === id).Active = v; };
    flipBack("T_FINALIZE", false); flipBack("T-01_TOOLS", true);
    flipBack("G-02_VOICE_V2", false); flipBack("G-02_VOICE", true);
    const legacyMock = { "Get Blocks": legacy.blocks, "Get Map": legacy.map };
    const fin = structuredClone(selected);
    fin.turn = { ...fin.turn, intent: "finalize_agreed", stage: "closing", tools_open: true, finalize_required: true };
    const out = runNode("Prompt Compiler", legacyMock, [fin])[0].json;
    const s = out.system_prompt_compiled;
    const t01 = legacy.blocks.find(x => x.Block_ID === "T-01_TOOLS").Content.trim();
    assert.equal(s.split(t01).length - 1, 1, "фолбэк: T-01 ровно один раз");
    assert.ok(s.includes(snip("G-02_VOICE")), "старый VOICE, пока V2 не активирован");
});

// ---------- Tool Guard ----------
const compiled = runNode("Prompt Compiler", tablesMock, [selected])[0].json;
T("нормальный ответ → ok; утечка function_calls вырезана", () => {
    const out = runNode("Tool Guard", { "Prompt Compiler": compiled },
        [{ output: "Отличный вопрос! <function_calls>тех-мусор</function_calls> Давайте посчитаем.", intermediateSteps: [] }])[0].json;
    assert.equal(out.guard.action, "ok");
    assert.ok(!out.draft_text.includes("function_calls"));
});

T("finalize требуется, но не вызван → retry; вызван → ok", () => {
    const fin = structuredClone(compiled);
    fin.turn.finalize_required = true;
    let out = runNode("Tool Guard", { "Prompt Compiler": fin },
        [{ output: "Спасибо, передал менеджеру!", intermediateSteps: [] }])[0].json;
    assert.equal(out.guard.action, "retry");
    out = runNode("Tool Guard", { "Prompt Compiler": fin },
        [{ output: "Спасибо, передал менеджеру!", intermediateSteps: [{ action: { tool: "finalize_deal" } }] }])[0].json;
    assert.equal(out.guard.action, "ok");
});

T("пустой ответ → retry; сверхдлинный → rewrite", () => {
    let out = runNode("Tool Guard", { "Prompt Compiler": compiled }, [{ output: "" }])[0].json;
    assert.equal(out.guard.action, "retry");
    out = runNode("Tool Guard", { "Prompt Compiler": compiled }, [{ output: "х".repeat(4500), intermediateSteps: [] }])[0].json;
    assert.equal(out.guard.action, "rewrite");
});

// ---------- Tool Guard: обрыв генерации (n8n exec 3272, 2026-08-19) ----------
// Модель оборвалась на 20 токенах, finish_reason="error", ответ пришёл без
// второй половины — без ссылки на кабинет. Сторож обрыва не видел и отдал
// обрубок клиенту как готовый ответ.
const lmRuns = (...reasons) => ({
    __runs: reasons.map(r => [{ response: { generations: [[{ generationInfo: { finish_reason: r } }]] } }]),
});
const guardWithLM = (lm, items) => runNode("Tool Guard",
    { "Prompt Compiler": compiled, "Agent LM": lm, "Agent LM Fallback": { __runs: [] } },
    items)[0].json;
const halfAnswer = [{ output: "Отлично! Кабинет персональный, доступ будет только у вас", intermediateSteps: [] }];

T("обрыв генерации (finish_reason=error) → retry, а не выдача клиенту", () => {
    const out = guardWithLM(lmRuns("error"), halfAnswer);
    assert.equal(out.guard.action, "retry");
    assert.equal(out.guard.aborted, true);
    assert.equal(out.guard.finish_reason, "error");
});

T("упёрлись в лимит (length) → retry", () => {
    assert.equal(guardWithLM(lmRuns("length"), halfAnswer).guard.action, "retry");
});

// --- обрыв по ФОРМЕ текста: рабочий признак, пока саб-нода недостижима ------
// exec 3272 в точности: текст кончился посреди фразы, даже без финальной точки.
T("обрыв по форме текста → retry, хотя finish_reason недоступен", () => {
    const out = runNode("Tool Guard", { "Prompt Compiler": compiled }, halfAnswer)[0].json;
    assert.equal(out.guard.action, "retry");
    assert.equal(out.guard.aborted, true);
    assert.equal(out.guard.abort_source, "shape");
    assert.equal(out.guard.lm_signal, "empty");      // прямого признака нет — и не будет
});

T("нормальный ответ с вопросом на конце → ok", () => {
    const out = runNode("Tool Guard", { "Prompt Compiler": compiled },
        [{ output: "Вот ваша ссылка: https://x/y Получилось открыть?", intermediateSteps: [] }])[0].json;
    assert.equal(out.guard.action, "ok");
    assert.equal(out.guard.truncated_shape, false);
});

T("ссылка в самом конце не считается обрывом", () => {
    // Знак препинания после адреса ЗАПРЕЩЁН правилом BLK_*_CABINET_CHOSEN,
    // поэтому «нет завершающего знака» тут штатно и повтор был бы ложным.
    const out = runNode("Tool Guard", { "Prompt Compiler": compiled },
        [{ output: "Вот ваша ссылка: https://demo.autodc.org/cabinet#t=abc", intermediateSteps: [] }])[0].json;
    assert.equal(out.guard.action, "ok");
    assert.equal(out.guard.truncated_shape, false);
});

T("эмодзи и закрывающая кавычка на конце не считаются обрывом", () => {
    for (const tail of ["Заявка передана специалисту ⚡", "Он ответит на вопрос «как это работает»"]) {
        const out = runNode("Tool Guard", { "Prompt Compiler": compiled },
            [{ output: tail, intermediateSteps: [] }])[0].json;
        assert.equal(out.guard.action, "ok", tail);
    }
});

T("сверхдлинный обрубок лечится сжатием, а не повтором", () => {
    // Ответ длиннее 4000 знаков обрывается на лимите токенов: повтор
    // воспроизвёл бы ту же простыню. Поэтому rewrite приоритетнее обрыва.
    const out = runNode("Tool Guard", { "Prompt Compiler": compiled },
        [{ output: "х".repeat(4500), intermediateSteps: [] }])[0].json;
    assert.equal(out.guard.action, "rewrite");
    assert.equal(out.guard.truncated_shape, true);   // обрыв замечен, но лечится иначе
});

T("пропущенный finalize приоритетнее обрыва", () => {
    const fin = structuredClone(compiled);
    fin.turn.finalize_required = true;
    const out = runNode("Tool Guard", { "Prompt Compiler": fin }, halfAnswer)[0].json;
    assert.equal(out.guard.action, "retry");
    assert.equal(out.guard.need_finalize, true);
});

T("форма и finish_reason расходятся: прямой признак старше", () => {
    // Текст выглядит целым, но провайдер сообщил об обрыве — верим ему.
    const out = guardWithLM(lmRuns("error"),
        [{ output: "Всё готово. Что уточнить?", intermediateSteps: [] }]);
    assert.equal(out.guard.action, "retry");
    assert.equal(out.guard.abort_source, "finish_reason");
    assert.equal(out.guard.truncated_shape, false);
});

T("ход с вызовом инструмента: tool_calls → stop не даёт ложного повтора", () => {
    // Штатная последовательность прогонов на ходе с инструментом. Решение
    // принимается по ПОСЛЕДНЕМУ прогону, иначе каждый такой ход уходил бы в повтор.
    const out = guardWithLM(lmRuns("tool_calls", "stop"),
        [{ output: "Расчёт готов, платёж 25000 руб.", intermediateSteps: [{ action: { tool: "evaluate_auto" } }] }]);
    assert.equal(out.guard.action, "ok");
    assert.equal(out.guard.aborted, false);
    assert.deepEqual(out.guard.finish_reasons, ["tool_calls", "stop"]);
});

T("неизвестное значение finish_reason считается штатным", () => {
    // Текст целый — решает только прямой признак, а незнакомое значение
    // трактуется как штатное: ошибаемся в сторону молчания, не повтора.
    const out = guardWithLM(lmRuns("что-то новое"), [{ output: "Готово. Что уточнить?", intermediateSteps: [] }]);
    assert.equal(out.guard.action, "ok");
    assert.equal(out.guard.aborted, false);
});

T("прямой признак недоступен → решает форма, недоступность видна в lm_signal", () => {
    // Саб-нода отдаёт данные по связи ai_languageModel, и $() до неё не
    // достаёт (проверено на живом инстансе). Целый текст при этом обязан
    // проходить без ложного повтора, а факт недоступности — попадать в лог.
    const out = runNode("Tool Guard", { "Prompt Compiler": compiled },
        [{ output: "Готово. Что уточнить?", intermediateSteps: [] }])[0].json;
    assert.equal(out.guard.action, "ok");
    assert.equal(out.guard.aborted, false);
    assert.equal(out.guard.lm_signal, "empty");
});

T("фолбэк-модель отработала после основной — её вердикт старше", () => {
    // Основная оборвалась, фолбэк доотвечал штатно и текст целый: повтор не нужен.
    const out = runNode("Tool Guard",
        { "Prompt Compiler": compiled, "Agent LM": lmRuns("error"), "Agent LM Fallback": lmRuns("stop") },
        [{ output: "Готово. Что уточнить?", intermediateSteps: [] }])[0].json;
    assert.equal(out.guard.action, "ok");
    assert.equal(out.guard.finish_reason, "stop");
});

// ---------- Post Guard: обрыв ПОВТОРА ----------
const postGuard = (lm, items) => runNode("Post Guard",
    { "Agent LM": lm, "Agent LM Fallback": { __runs: [] } }, items)[0].json;
// Фразу берём ИЗ КОДА НОДЫ, а не дублируем строкой: текст утверждает владелец,
// и захардкоженная копия протухает при первой же правке, хотя поведение верно.
// Тот же приём, что и для заглушек данных дилера ниже по файлу.
const FALLBACK = (code("Post Guard").match(/const RETRY_FALLBACK = "([^"]+)";/) || [])[1];
assert.ok(FALLBACK, "не разобрана служебная фраза из кода Post Guard");
// Final Response несёт вторую копию той же фразы — они обязаны совпадать,
// иначе клиент получит разные формулировки на одну ситуацию.
assert.ok(code("Final Response").includes(FALLBACK),
    "служебная фраза в Final Response разошлась с Post Guard");

T("повтор отработал штатно → в чат идёт текст модели", () => {
    const out = postGuard(lmRuns("error", "stop"), [{ output: "Вот ваша ссылка: https://x/y Получилось открыть?" }]);
    assert.ok(out.final_text.includes("https://x/y"));
    assert.equal(out.retry_guard.aborted, false);
});

T("повтор тоже оборвался → служебная фраза вместо обрубка", () => {
    // Повтор — последняя попытка: пути назад в Tool Guard нет, зациклиться
    // невозможно. Обрубок отдавать нельзя, молчать — тоже.
    const out = postGuard(lmRuns("error", "error"), [{ output: "Отлично! Кабинет персональный" }]);
    assert.equal(out.final_text, FALLBACK);
    assert.equal(out.retry_guard.aborted, true);
    assert.equal(out.retry_guard.used_fallback, true);
});

T("повтор вернул пустоту → та же служебная фраза", () => {
    assert.equal(postGuard(lmRuns("stop"), [{ output: "" }]).final_text, FALLBACK);
});

// ---------- Bypass ----------
T("bypass refuse: текст из BYPASS_REFUSE, ai_payload со статусом", () => {
    const item = baseItem();
    item.session_context.flags.stop_word_detected = true;
    const cls = runNode("Stage Classifier", {}, [item])[0].json;
    const out = runNode("Bypass Build", { "Stage Classifier": cls, "Get Blocks": TABLES.blocks }, [cls])[0].json;
    assert.ok(out.bypass_text.includes("Понял вас"));
    assert.equal(JSON.parse(out.ai_payload).status, "refuse");
    const resp = runNode("Bypass Respond", { "Bypass Build": out }, [out])[0].json;
    assert.ok(resp.text && resp.json_cache);
    assert.equal(resp.session_context_update.dialog_intent, "final_refusal");
});

// ---------- Final Response ----------
T("ok-путь: ответ + json_cache + session_context_update", () => {
    const g = runNode("Tool Guard", { "Prompt Compiler": compiled },
        [{ output: "Вот расчёт по Comfort.", intermediateSteps: [] }])[0].json;
    const out = runNode("Final Response", { "Tool Guard": g, "Post Guard": undefined, "Post Rewrite": undefined }, [g])[0].json;
    assert.equal(out.text, "Вот расчёт по Comfort.");
    assert.ok(out.json_cache.complectations, "json_cache — полный кэш, не срез");
    assert.equal(out.session_context_update.stage, "data_collect");
    assert.equal(out.session_context_update.dialog_intent, "collect_down_payment");
});

T("retry-путь берёт текст из Post Guard", () => {
    const fin = structuredClone(compiled);
    fin.turn.finalize_required = true;
    const g = runNode("Tool Guard", { "Prompt Compiler": fin }, [{ output: "без finalize", intermediateSteps: [] }])[0].json;
    const out = runNode("Final Response",
        { "Tool Guard": g, "Post Guard": { final_text: "Готово, заявка передана менеджеру!" } }, [g])[0].json;
    assert.equal(out.text, "Готово, заявка передана менеджеру!");
});

// ---------- v3.03: need-check (план 2026-07-21-001) ----------
// Состояние «после активации» — та же мутация, что делает add_needcheck_v303.py
// --activate-drafts: черновики v3.03 включены, замещённые старые primary-строки
// выключены (V2 tool-блоков достигается дефолтом стадии + pick()-предпочтением).
const V303_BLOCKS = ["BLK_CREDIT_APPROVAL_BRIDGE", "BLK_TRADEIN_TESTDRIVE_BRIDGE",
    "BLK_TRADEIN_VALUE_DEFENSE", "BLK_COMBO_NEEDCHECK_ROUTER", "BLK_STOCK_NEED_CHECK",
    "BLK_CREDIT_PAYMENT_ACCEPTED_V2", "ST_TRADEIN_TOOL_NEEDED_V2", "ST_COMBO_TOOL_NEEDED_V2",
    "ST_STOCK_GREETING_V2", "CS_CREDIT_V2", "CS_TRADEIN_V2", "CS_COMBO_V2"];
const REPLACED_ROWS = [
    ["data_received_complete", "BLK_CREDIT_PAYMENT_ACCEPTED", "credit"],
    ["data_received_complete", "ST_TRADEIN_TOOL_NEEDED", "tradein"],
    ["data_received_complete", "ST_COMBO_TOOL_NEEDED", "combo"],
    ["ready_for_tool", "ST_TRADEIN_TOOL_NEEDED", "tradein"],
    ["ready_for_tool", "ST_COMBO_TOOL_NEEDED", "combo"],
    ["tool_rerun_needed", "ST_TRADEIN_TOOL_NEEDED", "tradein"],
    ["tool_rerun_needed", "ST_COMBO_TOOL_NEEDED", "combo"],
    ["tool_error_handling", "ST_TRADEIN_TOOL_NEEDED", "tradein"],
    ["tool_error_handling", "ST_COMBO_TOOL_NEEDED", "combo"],
];
const v303 = structuredClone(TABLES);
for (const id of V303_BLOCKS) {
    const b = v303.blocks.find(x => x.Block_ID === id);
    assert.ok(b, `${id} нет в снапшоте — пересоберите: add_needcheck_v303.py --snapshot-only`);
    b.Active = true;
}
v303.map.forEach(m => { if (m.Version_Set === "v3.03") m.Active = true; });
v303.map.forEach(m => {
    if (REPLACED_ROWS.some(([i, b, s]) => m.Intent_ID === i && m.Block_ID === b
        && m.Scenario_Filter === s)) m.Active = false;
});
const v303Mock = { "Get Blocks": v303.blocks, "Get Map": v303.map };

// Ассерты «какой блок попал в промпт» — по СОДЕРЖИМОМУ блока из снапшота,
// а не по дословной цитате из него. Контент правит владелец прямо в NocoDB,
// и захардкоженная фраза протухает при первой же переформулировке, хотя
// поведение остаётся верным (инцидент 2026-08-10: кейс AE1 упал на фразе
// «задай вопрос комфортности» после того, как владелец переписал
// BLK_CREDIT_PAYMENT_ACCEPTED_V2 в рамках v3.05 — блок выбирался правильно).
const contentOf = (tables, id) => {
    const b = tables.blocks.find(x => x.Block_ID === id);
    assert.ok(b, `${id} нет в снапшоте — пересоберите: add_needcheck_v303.py --snapshot-only`);
    const c = String(b.Content || "").trim();
    assert.ok(c, `${id}: пустой Content в снапшоте — пересоберите снапшот`);
    return c;
};
// Обратная мутация: доактивационное состояние (черновики v3.03 выключены,
// замещённые старые строки включены) — снапшот после --activate-drafts живой,
// поэтому «переходное состояние» держим регрессом, как legacy-кейс v3.01.
const pre303 = structuredClone(TABLES);
for (const id of V303_BLOCKS) pre303.blocks.find(x => x.Block_ID === id).Active = false;
pre303.map.forEach(m => { if (m.Version_Set === "v3.03") m.Active = false; });
pre303.map.forEach(m => {
    if (REPLACED_ROWS.some(([i, b, s]) => m.Intent_ID === i && m.Block_ID === b
        && m.Scenario_Filter === s)) m.Active = true;
});
const pre303Mock = { "Get Blocks": pre303.blocks, "Get Map": pre303.map };
const turnFor = (item, intent, stage, scenario) => {
    const it = structuredClone(item);
    if (scenario) it.session_context = { ...it.session_context, request_type: scenario };
    it.turn = { ...it.turn, intent, stage, tools_open: stage !== "greeting", finalize_required: false };
    return it;
};

T("v3.03/AE9: телефон в сообщении при need-check ходе → принудительный finalize", () => {
    const item = baseItem({ resolver: { verdict: "ok", dialog_intent: "need_check_ok",
                                        key_data_extracted: {}, reasoning: "" } });
    item.session_context.flags.has_valid_phone_in_msg = true;
    const out = runNode("Stage Classifier", {}, [item])[0].json;
    assert.equal(out.turn.intent, "finalize_agreed");
    assert.equal(out.turn.finalize_required, true);
});

T("v3.03: need_check_ok/not_ok стадию не двигают (falls through к эвристике Python)", () => {
    for (const intent of ["need_check_ok", "need_check_not_ok"]) {
        const item = baseItem({ resolver: { verdict: "ok", dialog_intent: intent,
                                            key_data_extracted: {}, reasoning: "" } });
        item.session_context.current_stage = "tool_needed";
        const out = runNode("Stage Classifier", {}, [item])[0].json;
        assert.equal(out.turn.intent, intent);
        assert.equal(out.turn.stage, "tool_needed", "стадия должна остаться от эвристики Python");
    }
});

T("v3.03/AE1: credit data_received_complete → BLK_CREDIT_PAYMENT_ACCEPTED_V2 (need-check вместо визита)", () => {
    const out = runNode("Prompt Compiler", v303Mock,
        [turnFor(selected, "data_received_complete", "tool_needed")])[0].json;
    const s = out.system_prompt_compiled;
    assert.ok(s.includes(contentOf(v303, "BLK_CREDIT_PAYMENT_ACCEPTED_V2")),
        "нет V2-сборщика с need-check вопросом");
    assert.ok(!s.includes(contentOf(v303, "BLK_CREDIT_PAYMENT_ACCEPTED")),
        "старый BLK_CREDIT_PAYMENT_ACCEPTED не должен попадать после активации");
});

T("v3.03/AE4: tradein ready_for_tool → ST_TRADEIN_TOOL_NEEDED_V2 через дефолт+pick", () => {
    const out = runNode("Prompt Compiler", v303Mock,
        [turnFor(selected, "ready_for_tool", "tool_needed", "tradein")])[0].json;
    const s = out.system_prompt_compiled;
    assert.ok(s.includes(contentOf(v303, "ST_TRADEIN_TOOL_NEEDED_V2")), "нет V2 SUCCESS-шаблона");
    assert.ok(!s.includes(contentOf(v303, "ST_TRADEIN_TOOL_NEEDED")),
        "старый SUCCESS-хвост с приглашением не должен попадать");
});

T("v3.03/AE7: combo need_check_* → BLK_COMBO_NEEDCHECK_ROUTER (оценка первым, один барьер)", () => {
    for (const intent of ["need_check_ok", "need_check_not_ok"]) {
        const out = runNode("Prompt Compiler", v303Mock,
            [turnFor(selected, intent, "tool_needed", "combo")])[0].json;
        assert.ok(out.system_prompt_compiled.includes("МАРШРУТИЗАТОР NEED-CHECK"),
            `нет router-блока на ${intent}`);
    }
});

T("v3.03: credit need_check_not_ok → существующий BLK_CREDIT_ADVOCATE (переиспользование)", () => {
    const out = runNode("Prompt Compiler", v303Mock,
        [turnFor(selected, "need_check_not_ok", "tool_needed")])[0].json;
    assert.ok(out.system_prompt_compiled.includes(snip("BLK_CREDIT_ADVOCATE")),
        "нет тактики адвоката (трейд-ин/субсидии)");
});

// Переписан в v3.07 (план 2026-08-14-001, U5). Прежняя редакция утверждала
// фолбэк на старый CS_CREDIT при выключенном V2 — ровно то поведение, которое
// архивация и убирает: базовый блок недостижим только потому, что компилятор
// предпочитает _V2, и при переименовании или удалении _V2 старый текст молча
// вернулся бы в промпт. Теперь он выключен, и возврата не будет.
// Ассерты переведены с дословных цитат на содержимое снапшота — формулировки
// правит владелец, и захардкоженная фраза протухает при первой же правке.
T("v3.03/v3.07: CS_CREDIT_V2 действует, архивный CS_CREDIT не возвращается", () => {
    const withV2 = runNode("Prompt Compiler", v303Mock, [structuredClone(selected)])[0].json;
    assert.ok(withV2.system_prompt_compiled.includes(contentOf(v303, "CS_CREDIT_V2")),
        "нет V2-ядра");
    assert.ok(!withV2.system_prompt_compiled.includes(contentOf(v303, "CS_CREDIT")),
        "архивный CS_CREDIT попал в промпт при активном V2");
    const withoutV2 = runNode("Prompt Compiler", pre303Mock, [structuredClone(selected)])[0].json;
    assert.ok(!withoutV2.system_prompt_compiled.includes(contentOf(pre303, "CS_CREDIT")),
        "при выключенном V2 вернулся архивный CS_CREDIT — архивация не сработала");
});

T("v3.03/AE8: stock greeting — V2-приветствие с need-check, без «менеджер+телефон» одним ходом", () => {
    const item = turnFor(selected, "greeting_first", "greeting", "stock");
    const out = runNode("Prompt Compiler", v303Mock, [item])[0].json;
    const s = out.system_prompt_compiled;
    assert.ok(s.includes("готовы ли вы подождать поставку"), "нет need-check вопроса в Сценарии В");
    assert.ok(s.includes("NEED-CHECK «НАЛИЧИЕ»"), "нет additional-блока BLK_STOCK_NEED_CHECK");
    assert.ok(!s.includes("Оставите номер телефона для связи?"),
        "старый Сценарий В (менеджер+телефон) не должен попадать");
});

T("v3.03: stock need_check_not_ok → BLK_STOCK_NEED_CHECK primary", () => {
    const out = runNode("Prompt Compiler", v303Mock,
        [turnFor(selected, "need_check_not_ok", "data_collect", "stock")])[0].json;
    assert.ok(out.system_prompt_compiled.includes("NEED-CHECK «НАЛИЧИЕ»"), "нет stock need-check блока");
});

// Переписан в v3.07 (U5) по той же причине, что и кейс CS_CREDIT выше:
// BLK_CREDIT_PAYMENT_ACCEPTED архивирован, поэтому выключение V2 больше не
// возвращает старый текст — это и есть смысл архивации. Переходное состояние
// теперь означает «ни того, ни другого», а не «старое поведение».
T("v3.03/v3.07: выключение V2 не возвращает архивный BLK_CREDIT_PAYMENT_ACCEPTED", () => {
    const s = runNode("Prompt Compiler", pre303Mock,
        [turnFor(selected, "data_received_complete", "tool_needed")])[0].json.system_prompt_compiled;
    assert.ok(!s.includes(contentOf(pre303, "BLK_CREDIT_PAYMENT_ACCEPTED")),
        "вернулся архивный BLK_CREDIT_PAYMENT_ACCEPTED — архивация не сработала");
    assert.ok(!s.includes(contentOf(pre303, "BLK_CREDIT_PAYMENT_ACCEPTED_V2")),
        "V2-текст не должен попадать до активации");
});

// ============================================================
// U7 (план 2026-08-10-001): данные оценки в json_cache + переменные кабинета
// ============================================================

// Оба состояния — двусторонней мутацией клона снапшота (конвенция
// plan-prompt-block-changes-from-live-nocodb): сырое содержимое снапшота не
// считаем ни «до активации», ни «после» — после --activate-drafts оно меняется.
const V306_BLOCKS = ["G-07_CABINET", "BLK_EVAL_REUSE"];
const mutate306 = (on) => {
    const t = structuredClone(TABLES);
    for (const id of V306_BLOCKS) {
        const b = t.blocks.find(x => x.Block_ID === id);
        assert.ok(b, `${id} нет в снапшоте — пересоберите: add_cabinet_v306.py --snapshot-only`);
        b.Active = on;
    }
    return t;
};
const v306 = mutate306(true), pre306 = mutate306(false);
const v306Mock = { "Get Blocks": v306.blocks, "Get Map": v306.map };
const pre306Mock = { "Get Blocks": pre306.blocks, "Get Map": pre306.map };

const CACHE_EVAL = { found_info: { mark: "KIA", model: "Rio", year: 2019, mileage: 87000,
                                   photos: { main: "//avatars/x" }, plate: "А123БВ777" },
                     ti_valuation: 890000 };
const dsItem = (qc, scenario = "tradein") => ({
    ...baseItem(), session_context: { ...baseItem().session_context, request_type: scenario },
    qualification_context: { request_type: scenario, cached_json: CACHE, ...qc },
});
const dsRun = (item) => runNode("Data Selector", { "Stage Classifier": item }, [item])[0].json.ai_clean_context;

// ---------- Data Selector: переменные кабинета ----------
T("U7/ж: cabinet_url и cabinet_status доезжают в ai_clean_context всех пяти веток", () => {
    for (const scen of ["credit", "tradein", "combo", "stock", "consult"]) {
        const a = dsRun(dsItem({ cabinet_url: "https://cab/abc", cabinet_status: "phone_confirmed" }, scen));
        assert.equal(a.cabinet_url, "https://cab/abc", `${scen}: нет cabinet_url`);
        assert.equal(a.cabinet_status, "phone_confirmed", `${scen}: нет cabinet_status`);
    }
});

T("U7/ж: ключей кабинета нет в контексте вовсе (сбой выдачи токена) → пустые строки, не падение", () => {
    for (const scen of ["credit", "tradein", "combo", "stock", "consult"]) {
        const a = dsRun(dsItem({}, scen));
        assert.equal(a.cabinet_url, "", `${scen}: ожидалась пустая строка`);
        assert.equal(a.cabinet_status, "", `${scen}: ожидалась пустая строка`);
    }
});

// ---------- Data Selector: имя модели на ходе передачи ----------
// Дефект приёмки 2026-08-21: в приветствии «получил вашу заявку на покупку
// автомобиля Elite» — без модели. Бот шлёт qualification_context.model только
// со ВТОРОГО хода; на INIT_AI_HANDOFF в payload лишь числовой model_id. Имя при
// этом уже лежит в кэше и в свежем результате поиска — нода туда не смотрела.
// Замер по логам: model был пуст на ВСЕХ девяти INIT-ходах и заполнен на всех
// последующих.
T("имя модели берётся из кэша, когда бот его ещё не прислал (все пять веток)", () => {
    for (const scen of ["credit", "tradein", "combo", "stock", "consult"]) {
        const item = dsItem({ model: undefined }, scen);
        item.qualification_context.cached_json = { ...CACHE, model: "HAVAL JOLION" };
        assert.equal(dsRun(item).model, "HAVAL JOLION", `${scen}: имя не подтянулось из cached_json`);
    }
});

T("имя модели берётся из fresh_cache при холодном кэше (все пять веток)", () => {
    for (const scen of ["credit", "tradein", "combo", "stock", "consult"]) {
        const item = dsItem({ model: undefined }, scen);
        item.qualification_context.cached_json = { ...CACHE };   // без model
        item.fresh_cache = { model: "НОВЫЙ HAVAL F7" };
        assert.equal(dsRun(item).model, "НОВЫЙ HAVAL F7", `${scen}: имя не подтянулось из fresh_cache`);
    }
});

T("присланное ботом имя модели старше запасных источников", () => {
    for (const scen of ["credit", "tradein", "combo", "stock", "consult"]) {
        const item = dsItem({ model: "HAVAL DARGO" }, scen);
        item.qualification_context.cached_json = { ...CACHE, model: "НЕ ЭТО" };
        item.fresh_cache = { model: "И НЕ ЭТО" };
        assert.equal(dsRun(item).model, "HAVAL DARGO", `${scen}: перебит запасным источником`);
    }
});

T("имени нет нигде → пустая строка, а не падение и не undefined", () => {
    for (const scen of ["credit", "tradein", "combo", "stock", "consult"]) {
        const item = dsItem({ model: undefined }, scen);
        item.qualification_context.cached_json = { ...CACHE };
        assert.equal(dsRun(item).model, "", `${scen}: ожидалась пустая строка`);
    }
});

// ---------- Data Selector: оценка в контексте модели (спутник BLK_EVAL_REUSE) ----------
T("U7: ti_valuation и ti_evaluated_car из carryover попадают в контекст модели", () => {
    const a = dsRun(dsItem({ evaluation_carryover: CACHE_EVAL }));
    assert.equal(a.ti_valuation, 890000);
    assert.deepEqual(a.ti_evaluated_car, { plate: "А123БВ777", mileage: 87000 });
});

T("U7: оценка берётся из cached_json, если carryover ещё не пришёл", () => {
    const a = dsRun(dsItem({ cached_json: { ...CACHE, ...CACHE_EVAL } }));
    assert.equal(a.ti_valuation, 890000);
    assert.equal(a.ti_evaluated_car.plate, "А123БВ777");
});

T("U7: оценки не было → ключей ti_* в контексте нет (модель не увидит ложной суммы)", () => {
    const a = dsRun(dsItem({}));
    assert.ok(!("ti_valuation" in a), "ti_valuation не должен появляться без оценки");
    assert.ok(!("ti_evaluated_car" in a), "ti_evaluated_car не должен появляться без оценки");
    assert.equal(a.complectations.length, CACHE.complectations.length, "каталог не пострадал");
});

// ---------- Prompt Compiler: черновики v3.06 ----------
// v3.07 (план 2026-08-14-001, U1): G-07_CABINET убран из жёсткого списка
// general-блоков и переведён на Intent_Triggers. Одной активации теперь мало —
// нужны заполненный Intent_Triggers и подходящий Scenario. Колонка Scenario
// служит рубильником поэтапного выката, поэтому проверяется отдельно.
const withCabinetWiring = (tables, scenario, triggers) => {
    const t = structuredClone(tables);
    const b = t.blocks.find(x => x.Block_ID === "G-07_CABINET");
    assert.ok(b, "G-07_CABINET нет в снапшоте — пересоберите: add_cabinet_v306.py --snapshot-only");
    b.Active = true; b.Scenario = scenario; b.Intent_Triggers = triggers;
    return { "Get Blocks": t.blocks, "Get Map": t.map, __tables: t };
};

T("U1: активации G-07_CABINET без Intent_Triggers недостаточно", () => {
    // Триггеры очищаем явно, а не полагаемся на то, что они пусты в снапшоте:
    // с 2026-08-15 они заполнены проводкой, и кейс, опирающийся на сырое
    // состояние снапшота, ломался бы молча при каждой правке таблицы.
    const bare = withCabinetWiring(TABLES, "all", "");   // возвращает мок напрямую
    const item = turnFor(selected, "data_received_complete", "tool_needed");
    const s = runNode("Prompt Compiler", bare, [item])[0].json.system_prompt_compiled;
    assert.ok(!s.includes(contentOf(bare.__tables, "G-07_CABINET")),
        "блок подключился без Intent_Triggers — жёсткий список не убран");
});

T("U1: G-07_CABINET подключается через Intent_Triggers при совпадении Scenario", () => {
    const mock = withCabinetWiring(TABLES, "tradein", "data_received_complete");
    const body = contentOf(mock.__tables, "G-07_CABINET");
    const hit = turnFor(selected, "data_received_complete", "tool_needed", "tradein");
    assert.ok(runNode("Prompt Compiler", mock, [hit])[0].json.system_prompt_compiled.includes(body),
        "блок не подключился при совпадении сценария и триггера");
});

T("U1: колонка Scenario работает рубильником этапа", () => {
    const mock = withCabinetWiring(TABLES, "tradein", "data_received_complete");
    const body = contentOf(mock.__tables, "G-07_CABINET");
    for (const scen of ["credit", "combo", "stock", "consult"]) {
        const item = turnFor(selected, "data_received_complete", "tool_needed", scen);
        assert.ok(!runNode("Prompt Compiler", mock, [item])[0].json.system_prompt_compiled.includes(body),
            `${scen}: кабинет протёк в невключённый сценарий`);
    }
});

T("U1: нецелевой интент кабинет не подключает", () => {
    const mock = withCabinetWiring(TABLES, "tradein", "data_received_complete");
    const body = contentOf(mock.__tables, "G-07_CABINET");
    const item = turnFor(selected, "pause_requested", "closing", "tradein");
    assert.ok(!runNode("Prompt Compiler", mock, [item])[0].json.system_prompt_compiled.includes(body),
        "кабинет подключился на интенте, которого нет в Intent_Triggers");
});

T("U7: BLK_EVAL_REUSE подключается Intent_Triggers на tool-интентах tradein/combo", () => {
    const body = contentOf(v306, "BLK_EVAL_REUSE");
    for (const scen of ["tradein", "combo"]) {
        for (const intent of ["ready_for_tool", "tool_rerun_needed", "tool_result_received"]) {
            const item = turnFor(selected, intent, "tool_needed", scen);
            assert.ok(runNode("Prompt Compiler", v306Mock, [item])[0].json.system_prompt_compiled.includes(body),
                `${scen}/${intent}: нет запрета повторного вызова оценки`);
            assert.ok(!runNode("Prompt Compiler", pre306Mock, [item])[0].json.system_prompt_compiled.includes(body),
                `${scen}/${intent}: блок не должен попадать до активации`);
        }
    }
});

T("U7: BLK_EVAL_REUSE не протекает в чужие сценарии и на нецелевые интенты", () => {
    const body = contentOf(v306, "BLK_EVAL_REUSE");
    for (const scen of ["credit", "stock", "consult"]) {
        const item = turnFor(selected, "ready_for_tool", "tool_needed", scen);
        assert.ok(!runNode("Prompt Compiler", v306Mock, [item])[0].json.system_prompt_compiled.includes(body),
            `${scen}: блок tradein/combo не должен попадать`);
    }
    const greeting = turnFor(selected, "greeting_first", "greeting", "tradein");
    assert.ok(!runNode("Prompt Compiler", v306Mock, [greeting])[0].json.system_prompt_compiled.includes(body),
        "на greeting-ходе запрет повторного вызова не нужен");
});

// ---------- Final Response: ключи оценки в json_cache ----------
const FOUND_TI = { mark: "KIA", model: "Rio", year: 2019, mileage: 87000,
                   photos: { main: "//avatars/x" }, owners_count: 2, plate: "А123БВ777" };
const stepOf = (tool, observation, plate) =>
    ({ action: { tool, toolInput: plate ? { plate } : {} }, observation });
const guardItem = (over = {}) => ({
    draft_text: "текст ответа", guard: { action: "ok" },
    turn: { stage: "tool_needed", intent: "tool_result_received" }, resolver: {},
    fresh_cache: null, qualification_context: { cached_json: CACHE }, ...over,
});
const frRun = (g, agent = [], retry = []) => runNode("Final Response", {
    "Tool Guard": g, "Agent": { intermediateSteps: agent }, "Agent Retry": { intermediateSteps: retry },
}, [{}])[0].json;

T("U7/а: ход без тулов оценки — json_cache идентичен прежнему поведению, ключей нет", () => {
    const out = frRun(guardItem());
    assert.deepEqual(out.json_cache, CACHE, "кэш Car Search изменён на ходе без оценки");
    assert.ok(!("found_info" in out.json_cache) && !("ti_valuation" in out.json_cache));
});

T("U7/б: evaluate_auto SUCCESS → оба ключа рядом с данными Car Search", () => {
    const out = frRun(guardItem(), [stepOf("evaluate_auto",
        { status: "SUCCESS", tradein_price_to: 890000, found_info: FOUND_TI })]);
    assert.equal(out.json_cache.ti_valuation, 890000);
    assert.equal(out.json_cache.found_info.plate, "А123БВ777");
    assert.equal(out.json_cache.found_info.photos.main, "//avatars/x", "URL фото не нормализуем");
    assert.deepEqual(out.json_cache.complectations, CACHE.complectations, "каталог не пострадал");
});

// Форма observation взята дословно из живого исполнения [TEST] AI Agent v3.0 #2999
// (2026-08-11, госномер х318те750): n8n сериализует выход тула-саб-воркфлоу как
// МАССИВ item'ов в строке. Первая версия U3 разворачивала только объект, поэтому
// obs.status был undefined и ключи молча не появлялись — баг дошёл до стенда,
// потому что все кейсы кормили сюда объект. Кейс закрывает именно это.
T("U7/regression #2999: observation — строка с МАССИВОМ item'ов → ключи извлечены", () => {
    const real = JSON.stringify([{
        status: "SUCCESS", car_info: "MAZDA CX_5 (2018 г.)",
        tradein_price_from: 2470000, tradein_price_to: 2760000,
        tradein_make: "MAZDA", tradein_model: "CX_5", tradein_year: 2018,
        found_info: { tech_param_id: "21026226", owners_count: 1, year: 2018, pts_original: true,
                      steering_wheel: "LEFT", mark: "MAZDA", model: "CX_5", mileage: 35000,
                      photos: { main: "//avatars.mds.yandex.net/get-verba/1672712/x/auto_main" },
                      plate: "Х318ТЕ750" },
    }]);
    const out = frRun(guardItem(), [stepOf("evaluate_auto", real, "х318те750")]);
    assert.equal(out.json_cache.ti_valuation, 2760000, "сумма оценки не извлечена из массива");
    assert.equal(out.json_cache.found_info.plate, "Х318ТЕ750", "госномер не извлечён");
    assert.equal(out.json_cache.found_info.mark, "MAZDA");
    assert.equal(out.json_cache.found_info.photos.main,
        "//avatars.mds.yandex.net/get-verba/1672712/x/auto_main", "URL фото не нормализуем");
    assert.deepEqual(out.json_cache.complectations, CACHE.complectations, "каталог не пострадал");
});

T("U7/regression #2999: массив с неуспешным статусом ключей не создаёт", () => {
    const out = frRun(guardItem(), [stepOf("evaluate_auto",
        JSON.stringify([{ status: "NOT_FOUND", message: "не найден" }]))]);
    assert.deepEqual(out.json_cache, CACHE, "кэш не должен меняться");
});

// Форма взята дословно из живого исполнения [TEST] AI Agent v3.0 #3048
// (2026-08-12, комбо, дилер 99). Три отличия от того, что предполагал U3:
// нода Merge Calc СНИМАЕТ обёртку response, поэтому tradein_evaluation лежит
// на верхнем уровне; тул назывался Combo_Calculator с подчёркиванием; вызов
// пришёл с Agent Retry, а не с Agent. Кейс закрывает все три сразу.
//
// Про имя тула: с 2026-08-30 узел переименован в combo_calculator (расхождение
// с промптом привело к UNEXPECTED_TOOL_CALL, исполнение 3517). Исторические
// имена в кейсах ниже оставлены СОЗНАТЕЛЬНО: извлечение в Final Response идёт
// по ФОРМЕ данных, а не по имени инструмента, и эти кейсы как раз доказывают
// независимость от имени. Отдельный кейс ниже гоняет ту же форму под новым
// именем — если однажды извлечение начнут привязывать к имени, упадут оба.
T("U7/regression #3048: комбо без обёртки response, тул Combo_Calculator, вызов с retry", () => {
    const real = JSON.stringify([{
        model: "GWM POER KINGKONG", dealer_id_used: "unknown",
        user_request: { payment: 30000, type: "combo", selected_complectation_id: "11",
                        selected_complectation_name: "Comfort", plate: "х318те750" },
        tradein_evaluation: {
            status: "SUCCESS", estimated_value: 2760000, tradein_make: "MAZDA",
            found_info: { mark: "MAZDA", model: "CX_5", year: 2018, mileage: 35000,
                          steering_wheel: "LEFT", plate: "Х318ТЕ750",
                          photos: { main: "//avatars.mds.yandex.net/get-verba/x/auto_main" } },
        },
        combo_calculation: { status: "success", term: 24, monthly_payment: 33000 },
        complectations: [{ id: "11", name: "Comfort" }], summary: { total_stock: 1 },
    }]);
    const out = frRun(guardItem(), [], [stepOf("Combo_Calculator", real, "х318те750")]);
    assert.equal(out.json_cache.ti_valuation, 2760000, "сумма оценки не извлечена из комбо-ответа");
    assert.equal(out.json_cache.found_info.plate, "Х318ТЕ750", "госномер не извлечён");
    assert.equal(out.json_cache.found_info.mark, "MAZDA");
    assert.deepEqual(out.json_cache.complectations, CACHE.complectations, "каталог не пострадал");
});

T("имя тула на извлечение не влияет: та же форма под combo_calculator", () => {
    // Новое имя узла после переименования 2026-08-30. Форма ответа не менялась.
    const out = frRun(guardItem(), [], [stepOf("combo_calculator", JSON.stringify([
        { tradein_evaluation: { status: "SUCCESS", estimated_value: 2760000,
                                found_info: { mark: "MAZDA", plate: "Х318ТЕ750" } } }]), "х318те750")]);
    assert.equal(out.json_cache.ti_valuation, 2760000);
    assert.equal(out.json_cache.found_info.plate, "Х318ТЕ750");
});

T("U7/regression #3048: комбо с неуспешной оценкой ключей не создаёт", () => {
    const out = frRun(guardItem(), [], [stepOf("Combo_Calculator", JSON.stringify([
        { tradein_evaluation: { status: "NOT_FOUND", estimated_value: 0 },
          combo_calculation: { status: "error" } }]))]);
    assert.deepEqual(out.json_cache, CACHE, "кэш не должен меняться");
});

T("U7/regression #3048: обёртка response всё ещё поддерживается (Merge Calc может вернуться)", () => {
    const out = frRun(guardItem(), [stepOf("Combo Calculator", JSON.stringify([
        { response: { tradein_evaluation: { status: "SUCCESS", estimated_value: 2760000,
                                            found_info: { mark: "MAZDA", plate: "Х318ТЕ750" } } } }]))]);
    assert.equal(out.json_cache.ti_valuation, 2760000);
    assert.equal(out.json_cache.found_info.plate, "Х318ТЕ750");
});

T("U7/в: Combo Calculator SUCCESS → ключи из tradein_evaluation", () => {
    const out = frRun(guardItem(), [stepOf("Combo Calculator",
        { response: { tradein_evaluation: { status: "SUCCESS", estimated_value: 890000, found_info: FOUND_TI } } })]);
    assert.equal(out.json_cache.ti_valuation, 890000);
    assert.equal(out.json_cache.found_info.mark, "KIA");
});

T("U7/е: неуспешные статусы оценки ключей не создают", () => {
    for (const status of ["NOT_FOUND", "IN_PROGRESS", "INVALID_DATA"]) {
        const out = frRun(guardItem(), [stepOf("evaluate_auto", { status, message: "m" })]);
        assert.deepEqual(out.json_cache, CACHE, `${status}: кэш не должен меняться`);
    }
});

T("U7/г: ход N+1 без тулов — ключи на месте из carryover", () => {
    const out = frRun(guardItem({ qualification_context: { cached_json: CACHE, evaluation_carryover: CACHE_EVAL } }));
    assert.equal(out.json_cache.ti_valuation, 890000);
    assert.equal(out.json_cache.found_info.plate, "А123БВ777");
});

T("U7/д: ГЛАВНЫЙ R7 — пересборка кэша после TTL (cached_json=null, отработал Car Search)", () => {
    const out = frRun(guardItem({ fresh_cache: CACHE,
        qualification_context: { cached_json: null, evaluation_carryover: CACHE_EVAL } }));
    assert.equal(out.json_cache.ti_valuation, 890000, "оценка исчезла на пересборке кэша");
    assert.equal(out.json_cache.found_info.plate, "А123БВ777");
    assert.deepEqual(out.json_cache.complectations, CACHE.complectations, "каталог не подтянулся");
});

T("U7: свежая оценка перебивает carryover, retry-путь не теряет данные", () => {
    const fresh = { status: "SUCCESS", tradein_price_to: 950000, found_info: FOUND_TI };
    const qc = { cached_json: CACHE, evaluation_carryover: CACHE_EVAL };
    assert.equal(frRun(guardItem({ qualification_context: qc }), [stepOf("evaluate_auto", fresh)]).json_cache.ti_valuation,
        950000, "свежая оценка должна побеждать carryover");
    assert.equal(frRun(guardItem(), [], [stepOf("evaluate_auto", fresh)]).json_cache.ti_valuation,
        950000, "оценка на Agent Retry потеряна");
});

T("U7: вырожденный carryover ключей не создаёт и не роняет ход", () => {
    for (const carry of [null, "мусор", {}, { ti_valuation: 0 }, { found_info: {} }]) {
        const out = frRun(guardItem({ qualification_context: { cached_json: CACHE, evaluation_carryover: carry } }));
        assert.deepEqual(out.json_cache, CACHE, `carryover=${JSON.stringify(carry)}: кэш изменён`);
    }
});

// ============================================================
// U6 (план 2026-08-14-001): тактика развилки «визит или кабинет»
// ============================================================

const V307_SCEN = ["tradein", "credit", "combo"];
const V307_SUFFIX = ["FORK", "CABINET_CHOSEN", "CABINET_FALLBACK", "CHAT_ONLY", "CABINET_HELP"];
const V307_RETIRE_BLOCKS = ["BLK_CREDIT_APPROVAL_BRIDGE", "BLK_TRADEIN_TESTDRIVE_BRIDGE",
                            "BLK_COMBO_NEEDCHECK_ROUTER"];

// Состояние этапа выводится мутацией клона снапшота, как требует конвенция:
// сырой снапшот не считаем ни «до», ни «после» — после --activate-scenario он
// меняется, и кейс, ассертящий сырое состояние, ломается молча.
const stageOf = (enabled) => {
    const t = structuredClone(TABLES);
    const need = (id) => {
        const b = t.blocks.find(x => x.Block_ID === id);
        assert.ok(b, `${id} нет в снапшоте — пересоберите: add_cabinet_tactic_v307.py --snapshot-only`);
        return b;
    };
    for (const scen of V307_SCEN) {
        const on = enabled.includes(scen);
        for (const suf of V307_SUFFIX) need(`BLK_${scen.toUpperCase()}_${suf}`).Active = on;
    }
    need("BLK_COMBO_VALUE_DEFENSE").Active = enabled.includes("combo");
    const cab = need("G-07_CABINET");
    cab.Active = enabled.length > 0;
    cab.Scenario = enabled.length ? enabled.join(",") : "all";
    cab.Intent_Triggers = "tool_result_received,data_received_complete,greeting_first,"
        + "choose_cabinet,prefer_chat_over_call,cabinet_stuck,objection_distance,propose_call";
    // общий слой снимается вместе с первым этапом
    const common = enabled.length > 0;
    for (const id of V307_RETIRE_BLOCKS) need(id).Active = !common;
    for (const m of t.map) {
        if (m.Version_Set === "v3.07") m.Active = common && enabled.includes(m.Scenario_Filter);
        if (m.Intent_ID === "need_check_ok" || m.Intent_ID === "need_check_not_ok") m.Active = !common;
    }
    return { mock: { "Get Blocks": t.blocks, "Get Map": t.map }, tables: t };
};

const OFF = stageOf([]);

// Кейс проверяет ФАКТИЧЕСКУЮ проводку блока в снапшоте, а не мок теста.
// Пропуск 2026-08-15: U1 убрал G-07_CABINET из жёсткого списка компилятора, но
// Intent_Triggers ему никто не заполнил — все четыре способа подключения были
// закрыты, и после включения этапа блок не попал бы в промпт вовсе. Кейсы это
// не поймали, потому что stageOf() проставляет триггеры сам. Тест, который
// проверяет собственную фикстуру вместо продовой проводки, не тест.
T("U6/проводка: G-07_CABINET подключён в живых данных, а не только в моке", () => {
    const cab = TABLES.blocks.find(b => b.Block_ID === "G-07_CABINET");
    assert.ok(cab, "G-07_CABINET нет в снапшоте — пересоберите: add_cabinet_tactic_v307.py --snapshot-only");
    const triggers = String(cab.Intent_Triggers || "").split(",").map(s => s.trim()).filter(Boolean);
    assert.ok(triggers.length > 0,
        "Intent_Triggers пуст: блок не подключается ни одним из четырёх способов "
        + "(из жёсткого списка убран, строк карты нет, имя не под шаблон)");
    for (const must of ["choose_cabinet", "prefer_chat_over_call", "cabinet_stuck",
                        "objection_distance", "propose_call", "tool_result_received",
                        "data_received_complete", "greeting_first"]) {
        assert.ok(triggers.includes(must), `в Intent_Triggers нет хода развилки ${must}`);
    }
    for (const never of ["reminder_response", "final_refusal", "ban_trigger"]) {
        assert.ok(!triggers.includes(never),
            `${never} до Prompt Compiler не доходит — в триггерах ему делать нечего`);
    }
    assert.ok(!String(cab.Notes || "").includes("Подключён списком general-блоков"),
        "примечание описывает подключение, отменённое юнитом U1");
});

T("U6: до включения ни один блок развилки в промпт не попадает", () => {
    for (const scen of V307_SCEN) {
        for (const suf of V307_SUFFIX) {
            const body = contentOf(OFF.tables, `BLK_${scen.toUpperCase()}_${suf}`);
            for (const intent of ["tool_result_received", "data_received_complete", "greeting_first",
                                  "choose_cabinet", "prefer_chat_over_call", "cabinet_stuck"]) {
                for (const stage of ["greeting", "data_collect", "tool_needed", "closing"]) {
                    const item = turnFor(selected, intent, stage, scen);
                    assert.ok(!runNode("Prompt Compiler", OFF.mock, [item])[0].json
                        .system_prompt_compiled.includes(body),
                        `${scen}/${suf} протёк до включения на ${intent}/${stage}`);
                }
            }
        }
    }
});

T("U6: этап трейд-ина не задевает кредит и комбо", () => {
    const { mock, tables } = stageOf(["tradein"]);
    const hit = turnFor(selected, "tool_result_received", "closing", "tradein");
    assert.ok(runNode("Prompt Compiler", mock, [hit])[0].json.system_prompt_compiled
        .includes(contentOf(tables, "BLK_TRADEIN_FORK")), "трейд-ин не включился");
    for (const scen of ["credit", "combo"]) {
        const item = turnFor(selected, "tool_result_received", "closing", scen);
        const s = runNode("Prompt Compiler", mock, [item])[0].json.system_prompt_compiled;
        assert.ok(!s.includes(contentOf(tables, `BLK_${scen.toUpperCase()}_FORK`)),
            `${scen}: развилка протекла в невключённый сценарий`);
        assert.ok(!s.includes(contentOf(tables, "G-07_CABINET")),
            `${scen}: блок кабинета протёк — колонка Scenario не сработала рубильником`);
    }
});

T("U6: развилка кредита стоит в первом сообщении, трейд-ина и комбо — после оценки", () => {
    const { mock, tables } = stageOf(V307_SCEN);
    const greeting = (scen) => runNode("Prompt Compiler", mock,
        [turnFor(selected, "greeting_first", "greeting", scen)])[0].json.system_prompt_compiled;
    assert.ok(greeting("credit").includes(contentOf(tables, "BLK_CREDIT_FORK")),
        "кредит: развилки нет в первом сообщении");
    for (const scen of ["tradein", "combo"]) {
        assert.ok(!greeting(scen).includes(contentOf(tables, `BLK_${scen.toUpperCase()}_FORK`)),
            `${scen}: развилка появилась на приветствии, а должна после оценки`);
    }
});

T("U6: ветви развилки подключаются своими интентами", () => {
    const { mock, tables } = stageOf(V307_SCEN);
    const pairs = [
        ["choose_cabinet", "CABINET_CHOSEN"],
        ["objection_distance", "CABINET_FALLBACK"],
        ["propose_call", "CABINET_FALLBACK"],
        ["prefer_chat_over_call", "CHAT_ONLY"],
        ["cabinet_stuck", "CABINET_HELP"],
    ];
    for (const scen of V307_SCEN) {
        for (const [intent, suf] of pairs) {
            const item = turnFor(selected, intent, "closing", scen);
            assert.ok(runNode("Prompt Compiler", mock, [item])[0].json.system_prompt_compiled
                .includes(contentOf(tables, `BLK_${scen.toUpperCase()}_${suf}`)),
                `${scen}: ${intent} не подключил ${suf}`);
        }
    }
});

T("U6: блоки развилки не протекают в stock и consult", () => {
    const { mock, tables } = stageOf(V307_SCEN);
    for (const scen of ["stock", "consult"]) {
        for (const intent of ["choose_cabinet", "prefer_chat_over_call", "cabinet_stuck",
                              "objection_distance", "tool_result_received"]) {
            const s = runNode("Prompt Compiler", mock,
                [turnFor(selected, intent, "closing", scen)])[0].json.system_prompt_compiled;
            for (const src of V307_SCEN) {
                for (const suf of V307_SUFFIX) {
                    assert.ok(!s.includes(contentOf(tables, `BLK_${src.toUpperCase()}_${suf}`)),
                        `${scen}/${intent}: протёк BLK_${src.toUpperCase()}_${suf}`);
                }
            }
        }
    }
});

T("U6: барьер переехал на objection_price, блоки вопроса о комфортности выведены", () => {
    const { mock, tables } = stageOf(V307_SCEN);
    assert.ok(runNode("Prompt Compiler", mock,
        [turnFor(selected, "objection_price", "closing", "tradein")])[0].json
        .system_prompt_compiled.includes(contentOf(tables, "BLK_TRADEIN_VALUE_DEFENSE")),
        "tradein: objection_price не подключил защиту оценки");
    assert.ok(runNode("Prompt Compiler", mock,
        [turnFor(selected, "objection_price", "closing", "combo")])[0].json
        .system_prompt_compiled.includes(contentOf(tables, "BLK_COMBO_VALUE_DEFENSE")),
        "combo: objection_price не подключил защиту оценки");
    for (const id of V307_RETIRE_BLOCKS) {
        const body = contentOf(tables, id);
        for (const scen of V307_SCEN) {
            for (const intent of ["need_check_ok", "need_check_not_ok", "objection_price"]) {
                assert.ok(!runNode("Prompt Compiler", mock,
                    [turnFor(selected, intent, "closing", scen)])[0].json
                    .system_prompt_compiled.includes(body), `${id} остался в промпте на ${intent}`);
            }
        }
    }
});

T("U6: need-check блоки stock не тронуты — они про наличие, не про комфортность", () => {
    const { mock, tables } = stageOf(V307_SCEN);
    const body = contentOf(tables, "BLK_STOCK_NEED_CHECK");
    assert.ok(runNode("Prompt Compiler", mock,
        [turnFor(selected, "greeting_first", "greeting", "stock")])[0].json
        .system_prompt_compiled.includes(body),
        "BLK_STOCK_NEED_CHECK потерял подключение через greeting_first");
});

// --- расчистка (U5) ---
const cleaned = () => {
    const t = structuredClone(TABLES);
    for (const m of t.map) {
        if (String(m.Stage_Filter) === "data_collect" && m.Role === "additional") m.Stage_Filter = "all";
    }
    return { mock: { "Get Blocks": t.blocks, "Get Map": t.map }, tables: t };
};

T("U6/расчистка: снятие Stage_Filter аддитивно — основной блок стадии не тронут", () => {
    const { mock, tables } = cleaned();
    for (const scen of ["tradein", "credit", "combo"]) {
        for (const stage of ["tool_needed", "closing"]) {
            const item = turnFor(selected, "collect_plate", stage, scen);
            const s = runNode("Prompt Compiler", mock, [item])[0].json.system_prompt_compiled;
            assert.ok(s.includes(contentOf(tables, `ST_${scen.toUpperCase()}_REQUIRED_DATA`)),
                `${scen}/${stage}: блок сбора не добавился после снятия фильтра`);
            // блок стадии выбирается тем же правилом, что и в компиляторе:
            // черновик _V2 вытесняет базовый, если активен. У credit версии _V2
            // для tool_needed нет — хардкодить имя нельзя.
            const base = `ST_${scen.toUpperCase()}_${stage.toUpperCase()}`;
            const v2 = tables.blocks.find(b => b.Block_ID === `${base}_V2` && b.Active);
            const stageBlock = v2 ? `${base}_V2` : base;
            assert.ok(s.includes(contentOf(tables, stageBlock)),
                `${scen}/${stage}: основной блок стадии вытеснен — фильтр снят не с той роли`);
        }
    }
});

T("U6/расчистка: primary-строки data_already_provided сохранили фильтр", () => {
    const { mock, tables } = cleaned();
    for (const scen of ["tradein", "stock", "consult"]) {
        const s = runNode("Prompt Compiler", mock,
            [turnFor(selected, "data_already_provided", "closing", scen)])[0].json.system_prompt_compiled;
        assert.ok(s.includes(contentOf(tables, `ST_${scen.toUpperCase()}_CLOSING`)),
            `${scen}: блок закрытия вытеснен блоком сбора — фильтр снят с primary-строки`);
    }
});

// ---------- Prompt Compiler: данные дилера (2026-09-03) ----------
// Регресс исполнения 3758: клиент спросил адрес и получил ответ «находится по
// адресу: [dealer_address]. Мы работаем с [dealer_hours]». В v2 плейсхолдеры
// подставлял бот перед отправкой; v3 собирает промпт сам, и подстановки не было.
// Контракт: бот кладёт три поля в session_context (коммит бота 5b559da).
const DEALER = {
    dealer_name: "Автомир Самара",
    dealer_address: "Самара, ул. Авроры, д. 150Е",
    dealer_hours: "пн-вс: 09:00-21:00",
};
const DEALER_TOKENS = ["[dealer_name]", "[dealer_address]", "[dealer_hours]"];
// Заглушки берём из самого кода ноды, а не дублируем строкой: формулировку
// утверждает владелец, и захардкоженная копия протухла бы при первой правке.
const FALLBACK_OF = Object.fromEntries(
    [...code("Prompt Compiler").matchAll(/^\s{4}(dealer_\w+): "([^"]+)",$/gm)].map(m => [m[1], m[2]]));
assert.equal(Object.keys(FALLBACK_OF).length, 3, "не разобраны заглушки из кода ноды");

const dealerItem = (fields, base = selected) => {
    const it = structuredClone(base);
    delete it.session_context.dealer_name;
    delete it.session_context.dealer_address;
    delete it.session_context.dealer_hours;
    if (fields) Object.assign(it.session_context, fields);
    return it;
};
const compileWith = (item, mock = tablesMock) => runNode("Prompt Compiler", mock, [item])[0].json;
const tokensLeft = s => DEALER_TOKENS.filter(t => s.includes(t));

T("дилер: значения из session_context подставлены, плейсхолдеров не осталось", () => {
    const out = compileWith(dealerItem(DEALER));
    const s = out.system_prompt_compiled;
    for (const v of Object.values(DEALER)) assert.ok(s.includes(v), `в промпте нет значения «${v}»`);
    assert.deepEqual(tokensLeft(s), [], "плейсхолдеры дилера остались в промпте");
    assert.equal(out.dealer_fields_source, "payload");
    assert.deepEqual(out.dealer_fields_missing, []);
});

T("дилер: null в полях → заглушка по политике, а не пустая строка", () => {
    const out = compileWith(dealerItem({ dealer_name: null, dealer_address: null, dealer_hours: null }));
    const s = out.system_prompt_compiled;
    assert.deepEqual(tokensLeft(s), [], "плейсхолдеры дилера остались в промпте");
    for (const [f, text] of Object.entries(FALLBACK_OF))
        assert.ok(s.includes(text), `нет заглушки для ${f}`);
    // пустая строка читалась бы моделью как «данных нет, придумай» —
    // рядом в G-01_ROLE стоит запрет выдумывать адрес
    assert.ok(!/— Адрес:\s*\n/.test(s), "адрес подставлен пустой строкой");
    assert.equal(out.dealer_fields_source, "fallback");
    assert.deepEqual(out.dealer_fields_missing.sort(), Object.keys(DEALER).sort());
});

T("дилер: ключей нет вовсе (бот без 5b559da) → та же заглушка, без падения", () => {
    const noKeys = compileWith(dealerItem(null));
    const nulls = compileWith(dealerItem({ dealer_name: null, dealer_address: null, dealer_hours: null }));
    assert.equal(noKeys.system_prompt_compiled, nulls.system_prompt_compiled,
        "отсутствие ключей и null должны давать один и тот же промпт");
    assert.equal(noKeys.dealer_fields_source, "fallback");
});

T("дилер: пустая строка и пробелы считаются пустым значением", () => {
    const out = compileWith(dealerItem({ dealer_name: "", dealer_address: "   ", dealer_hours: DEALER.dealer_hours }));
    assert.equal(out.dealer_fields_source, "partial");
    assert.deepEqual(out.dealer_fields_missing.sort(), ["dealer_address", "dealer_name"]);
    const s = out.system_prompt_compiled;
    assert.ok(s.includes(FALLBACK_OF.dealer_name) && s.includes(FALLBACK_OF.dealer_address));
    assert.ok(s.includes(DEALER.dealer_hours), "заполненное поле должно подставиться как есть");
    assert.deepEqual(tokensLeft(s), []);
});

T("дилер: класс B не тронут — [Модель], [Комплектация], [Сумма], [Имя клиента] остаются модели", () => {
    // greeting-ход: ST_CREDIT_GREETING_V2 — единственный носитель этих шаблонов
    const item = turnFor(dealerItem(DEALER), "greeting_first", "greeting", "credit");
    const s = compileWith(item).system_prompt_compiled;
    assert.ok(s.includes(contentOf(TABLES, "ST_CREDIT_GREETING_V2")),
        "greeting-блок не попал в промпт — кейс ничего не проверяет");
    for (const t of ["[Модель]", "[Комплектация]", "[Сумма]", "[Имя клиента]"])
        assert.ok(s.includes(t), `шаблон ${t} исчез — подстановка задела класс B`);
    assert.deepEqual(tokensLeft(s), [], "плейсхолдеры дилера остались");
});

T("дилер: класс C не тронут — [SPLIT] и [CABINET_LINK] доезжают до бота", () => {
    // [CABINET_LINK] появился в блоках 2026-08-21, снапшот старше — вносим его
    // в клон вместе с токеном дилера: блок подключается Intent_Triggers, а эта
    // ветка берёт Content мимо content() и требует отдельной проверки.
    const t = structuredClone(TABLES);
    const cab = t.blocks.find(x => x.Block_ID === "BLK_CREDIT_CABINET_CHOSEN");
    cab.Active = true;
    cab.Intent_Triggers = "choose_cabinet";
    cab.Content = "КЛИЕНТ ВЫБРАЛ КАБИНЕТ. Ссылка: [CABINET_LINK] [SPLIT] "
        + "Ждём вас по адресу [dealer_address], график [dealer_hours].";
    const mock = { "Get Blocks": t.blocks, "Get Map": t.map };

    const splitBefore = (compileWith(dealerItem(DEALER)).system_prompt_compiled.match(/\[SPLIT\]/g) || []).length;
    assert.ok(splitBefore > 0, "в промпте нет ни одного [SPLIT] — кейс ничего не проверяет");

    const s = compileWith(turnFor(dealerItem(DEALER), "choose_cabinet", "closing", "credit"), mock)
        .system_prompt_compiled;
    assert.ok(s.includes("[CABINET_LINK]"), "[CABINET_LINK] подменён — сломан контракт с ботом");
    assert.ok((s.match(/\[SPLIT\]/g) || []).length >= splitBefore, "[SPLIT] пропал из промпта");
    // а дилерские токены в блоке, подключённом триггером, подставлены
    assert.ok(s.includes(`по адресу ${DEALER.dealer_address}, график ${DEALER.dealer_hours}`),
        "ветка Intent_Triggers прошла мимо подстановки");
    assert.deepEqual(tokensLeft(s), []);
});

T("дилер: подстановка точечная — обратная замена даёт исходные блоки байт в байт", () => {
    const s = compileWith(dealerItem(DEALER)).system_prompt_compiled;
    const restored = Object.entries(DEALER)
        .reduce((acc, [f, v]) => acc.split(v).join(`[${f}]`), s);
    assert.ok(restored.includes(contentOf(TABLES, "G-01_ROLE")),
        "после обратной замены блок роли не совпал с содержимым таблицы");
    // всё, кроме блока роли, обязано лежать в промпте дословно из таблицы.
    // Имя блока берём тем же правилом, что и компилятор: активный _V2 вытесняет
    // базовый (в снапшоте так у CS_CREDIT и G-02_VOICE) — хардкодить нельзя.
    const picked = id => {
        const v2 = TABLES.blocks.find(b => b.Block_ID === `${id}_V2` && b.Active);
        return v2 ? `${id}_V2` : id;
    };
    for (const id of ["CS_CREDIT", "G-02_VOICE", "D_CREDIT", "ST_CREDIT_DATA_COLLECT",
                      "ST_CREDIT_REQUIRED_DATA", "G-06_CHECKLIST"])
        assert.ok(s.includes(contentOf(TABLES, picked(id))), `${picked(id)} изменён подстановкой`);
});

console.log(`OK: v3-конвейер, ${passed} сценариев симуляции пройдено`);
