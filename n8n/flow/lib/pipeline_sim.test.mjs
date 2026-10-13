// ============================================================
// U2: сквозная симуляция новой цепочки Prepare → Calc Engine → Merge
// против ОРИГИНАЛЬНОЙ ноды из baseline-снапшота, на одинаковых моках
// n8n-нод. Запуск: node pipeline_sim.test.mjs
//
// С 2026-10-09 (план 2026-10-09-001, KTD9) Prepare-код берётся из замороженных
// состояний до N4 (flow/cabinet-web-entry/live/), а не из flow/*.json: N4 / N5 /
// колонка активности меняют выбор программы и цену комбо НАМЕРЕННО, их поведение
// покрыто тестами в flow/cabinet-web-entry/*/. Здесь остаётся то, ради чего тест
// писался, — вынос математики в Calc Engine не изменил результат исходных нод.
// ============================================================
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const lib = require("./calc_engine.js");

const FLOW = new URL("../", import.meta.url);
const BASELINE = new URL("../archive/2026-07-05-baseline/", import.meta.url);
const FROZEN = new URL("../cabinet-web-entry/live/", import.meta.url);
const frozen = (name) => readFileSync(new URL(name, FROZEN), "utf-8");

function nodeCode(wfUrl, nodeName) {
    const wf = JSON.parse(readFileSync(wfUrl, "utf-8"));
    const n = wf.nodes.find(n => n.name === nodeName);
    if (!n) throw new Error(`нода ${nodeName} не найдена в ${wfUrl}`);
    return n.parameters.jsCode;
}

function runNode(code, data, inputItems = []) {
    const $ = (name) => {
        if (!(name in data)) throw new Error(`нет мока для ноды ${name}`);
        const val = data[name];
        return {
            all: () => (Array.isArray(val) ? val : [val]).map(j => ({ json: j })),
            first: () => ({ json: Array.isArray(val) ? val[0] : val }),
        };
    };
    const $input = {
        all: () => inputItems.map(j => ({ json: j })),
        first: () => ({ json: inputItems[0] }),
    };
    const fn = new Function("$", "$input", code);
    return fn($, $input);
}

// ---------- Общие моки данных ----------
const CATALOG = [
    // Active: true — с 2026-10-05 Car Search берёт только активные строки каталога;
    // slug в моках нет, поэтому сток привязывается запасным сравнением по названию,
    // и паритет с baseline проверяется по-прежнему.
    { Model: "Xray", Active: true, Complectation: "Comfort", "Complectation id": "11", "Model title": "Xray Comfort 1.6",
      "Price old": 2_150_000, "Cash in hook": 100_000, "Credit bonus": 150_000, "Trade-in bonus": 80_000,
      "Price new": 2_050_000, Engine: "1.6", Transmission_ru: "МКПП", Drivetrain_ru: "Передний",
      Acceleration: "11.5", "Сonsumption": "7.1",
      Features: "Кондиционер\nТоповые опции пакета\nПодогрев сидений\nКамера",
      "Minus Features": "камера",
      Pics: [{ title: "ext1.jpg", url: "/img/ext1.jpg" }, { title: "int1.jpg", signedUrl: "https://cdn/int1.jpg" }] },
    { Model: "Xray", Active: true, Complectation: "Luxe+", "Complectation id": "12", "Model title": "Xray Luxe+ 1.8",
      "Price old": 2_690_000, "Cash in hook": 0, "Credit bonus": 200_000, "Trade-in bonus": 120_000,
      "Price new": 2_590_000, Engine: "1.8", Transmission: "AT", Drivetrain: "FWD",
      Acceleration: "10.2", "Сonsumption": "7.8", Features: "", "Minus Features": "",
      Pics: [] },
    // дубль комплектации — должен схлопнуться, как в оригинале
    { Model: "Xray", Active: true, Complectation: "Comfort", "Complectation id": "11", "Price old": 2_150_000 },
];
const STOCK = [
    { slug: "xray-comfort-red", Status: "У дилера", Color_ext: "Красный", Model: "Xray" },
    { slug: "xray-comfort-white", Status: "В пути", Color_ext: "Белый", Model: "Xray" },
    { slug: "xray-luxe-plus-black", Status: "stock", Color_ext: "Чёрный", Model: "Xray" },
    { slug: "unknown-model", Status: "У дилера", Color_ext: "Синий", Model: "Xray" },
];
const RATES = [
    { Rate: 8,  Term_From: 12, Term_To: 36, Downpayment_From: 50, Downpayment_To: "" },
    { Rate: 12, Term_From: 12, Term_To: 36, Downpayment_From: 20, Downpayment_To: 49.99 },
    { Rate: 14, Term_From: 37, Term_To: 84, Downpayment_From: 30, Downpayment_To: "" },
    { Rate: 18, Term_From: 37, Term_To: 84, Downpayment_From: 10, Downpayment_To: 29.99 },
];
const PROGRAM = { Program_Name: "Base", Max_Term_Months: 84, Min_Down_Payment_Pct: 10 };

function newChain(prepareCode, mergeCode, prepareName, data) {
    const prep = runNode(prepareCode, data).json;
    const calc = lib.runCalc(prep.calc_request);
    const merged = runNode(mergeCode, { ...data, [prepareName]: prep }, [calc]);
    return merged.json;
}

let passed = 0;

// ---------- CAR SEARCH ----------
{
    const origCode = nodeCode(new URL("Tool - Car Search.json", BASELINE), "Code in JavaScript1");
    const prepCode = frozen("car_search_prepare.stock.js");   // бой «сток» 10-05, до N4
    const mergeCode = nodeCode(new URL("Tool - Car Search.json", FLOW), "Merge Calc");

    for (const payment of [0, 5_000, 45_000, 120_000]) {
        const data = {
            "Execute Workflow Trigger": {
                user_id: "u1", session_id: "s1", chatInput: "хочу xray", user_data: { name: "Иван" },
                data_routing: { catalog_table_id: "t1" },
                qualification_context: { model: "Xray", dealer_id: "77", payment, request_type: "credit",
                                         complectation_id: "12", deal_id: 555, plate: "" },
            },
            "Get Catalog": CATALOG, "Get Stock": STOCK, "Get Rates": RATES, "Get Program": PROGRAM,
        };
        const expected = runNode(origCode, data).json;
        const actual = newChain(prepCode, mergeCode, "Prepare Data", data);
        assert.deepEqual(actual, expected, `car search, payment=${payment}`);
        passed++;
    }

    // пустой каталог
    const dataEmpty = {
        "Execute Workflow Trigger": { qualification_context: { model: "Vesta", request_type: "credit" } },
        "Get Catalog": [{ NoModel: true }], "Get Stock": [], "Get Rates": RATES, "Get Program": PROGRAM,
    };
    assert.deepEqual(
        newChain(prepCode, mergeCode, "Prepare Data", dataEmpty),
        runNode(nodeCode(new URL("Tool - Car Search.json", BASELINE), "Code in JavaScript1"), dataEmpty).json,
        "car search, пустой каталог");
    passed++;
}

// ---------- COMBO CALCULATOR ----------
{
    const origCode = nodeCode(new URL("Tool_ Combo Calculator.json", BASELINE), "Code in JavaScript1");
    const prepCode = frozen("combo_prepare.pre-n5.js");       // бой до N4 / N5
    const mergeCode = nodeCode(new URL("Tool_ Combo Calculator.json", FLOW), "Merge Calc");

    const evalOk = { status: "SUCCESS", tradein_price_to: 830_000, tradein_make: "Kia", tradein_model: "Rio",
                     tradein_year: 2018, tradein_horse_power: 123, tradein_transmission: "AT", tradein_steering_wheel: "LEFT" };

    for (const [payment, evalData, label] of [
        [40_000, { ...evalOk, tradein_price_to: 834_567 }, "combo некруглая оценка"],
        [40_000, evalOk, "combo success"],
        [0, evalOk, "combo payment=0"],
        [15_000, evalOk, "combo impossible"],
        [40_000, { ...evalOk, tradein_price_to: 5_000_000 }, "combo full coverage"],
        [40_000, { status: "IN_PROGRESS", message: "Отчет формируется" }, "combo eval in progress"],
        [40_000, { status: "NOT_FOUND" }, "combo eval not found"],
    ]) {
        const data = {
            "Execute Workflow Trigger": {
                qualification_context: JSON.stringify({ model: "Xray", dealer_id: "77", payment: String(payment),
                                                        request_type: "combo", selected_complectation_id: "11", plate: "А123АА77" }),
            },
            "Execute Workflow": evalData,
            "Get Catalog": CATALOG, "Get Stock": STOCK, "Get Rates": RATES, "Get Program": PROGRAM,
        };
        const expected = runNode(origCode, data).json;
        const actual = newChain(prepCode, mergeCode, "Prepare Combo", data);
        assert.deepEqual(actual, expected, label);
        passed++;
    }
}

console.log(`OK: сквозная симуляция, ${passed} сценариев — новая цепочка идентична оригинальным нодам`);
