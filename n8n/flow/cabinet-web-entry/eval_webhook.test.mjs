// ============================================================
// N1: вебхук оценки трейд-ина для веб-входа в кабинет.
// Код нод берётся из сгенерированных JSON (build_eval_webhooks.py),
// терминальная нода сабфлоу — из flow/TOOL_ AUTO.RU EVALUATION.json
// (сверен с живым XCPe53sUwk8i9e3T 2026-09-30: расхождений нет).
// N3: характеризация — при каких входах сабфлоу отдаёт SUCCESS без цены.
// Запуск: node --test flow/cabinet-web-entry/*.test.mjs
// ============================================================
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const load = f => JSON.parse(readFileSync(new URL(f, import.meta.url), "utf-8"));
const DEV = load("./[TEST] WEBHOOK_ Cabinet Tradein Eval.json");
const PROD = load("./WEBHOOK_ Cabinet Tradein Eval.json");
const EVAL = load("../TOOL_ AUTO.RU EVALUATION.json");

const codeOf = (wf, name) => wf.nodes.find(n => n.name === name).parameters.jsCode;

// Мок n8n: $(name) отдаёт заранее заданный json, $input — входной item.
function run(code, input, refs = {}, customData = {}) {
    const $ = name => {
        if (!(name in refs)) throw new Error(`нет мока для ноды ${name}`);
        return { first: () => ({ json: refs[name] }) };
    };
    const $input = { first: () => ({ json: input }), item: { json: input } };
    const $execution = { customData: { set: (k, v) => { customData[k] = v; } } };
    return new Function("$", "$input", "$execution", code)($, $input, $execution);
}
const validate = body => run(codeOf(DEV, "Validate Request"), { body })[0].json;
const shape = (subOut, req) => run(codeOf(DEV, "Shape Response"), subOut, { "Validate Request": req })[0].json;
const terminal = json => run(codeOf(EVAL, "Code in JavaScript"), json, {
    "Code in JavaScript1": { plate: "А123ВС777" }, "Execute Workflow Trigger": { plate: "А123ВС777" },
});

test("обе копии: секрет проверяет Header Auth ноды Webhook, секрета в JSON нет", () => {
    for (const wf of [DEV, PROD]) {
        const wh = wf.nodes.find(n => n.type === "n8n-nodes-base.webhook");
        assert.equal(wh.parameters.authentication, "headerAuth");
        assert.equal(wh.parameters.httpMethod, "POST");
        assert.equal(wh.parameters.responseMode, "responseNode");
        assert.deepEqual(Object.keys(wh.credentials), ["httpHeaderAuth"], "только ссылка на credential");
        assert.ok(!JSON.stringify(wh.credentials).includes("value"), "значения секрета в JSON нет");
        assert.equal(wf.active, false);
    }
    assert.equal(DEV.nodes.find(n => n.name === "Webhook").parameters.path, "cabinet-tradein-eval-test");
    assert.equal(PROD.nodes.find(n => n.name === "Webhook").parameters.path, "cabinet-tradein-eval");
    assert.equal(DEV.nodes.find(n => n.name === "Webhook").credentials.httpHeaderAuth.name, "Cabinet Eval Secret [TEST]");
    assert.equal(PROD.nodes.find(n => n.name === "Webhook").credentials.httpHeaderAuth.name, "Cabinet Eval Secret [PROD]");
    assert.notEqual(DEV.nodes.find(n => n.name === "Webhook").webhookId,
                    PROD.nodes.find(n => n.name === "Webhook").webhookId);
});

test("дев зовёт [TEST]-сабфлоу ToF5, бой — XCPe53; код и связи копий одинаковы", () => {
    const ex = wf => wf.nodes.find(n => n.name === "Auto.ru Evaluation");
    assert.equal(ex(DEV).parameters.workflowId.value, "ToF5ApgK6qkk4mcH");
    assert.equal(ex(PROD).parameters.workflowId.value, "XCPe53sUwk8i9e3T");
    for (const wf of [DEV, PROD]) assert.equal(ex(wf).onError, "continueErrorOutput");
    const code = wf => wf.nodes.filter(n => n.type === "n8n-nodes-base.code").map(n => n.parameters.jsCode);
    assert.deepEqual(code(DEV), code(PROD));
    assert.deepEqual(DEV.connections, PROD.connections);
});

test("проверка тела: плохой запрос не доходит до auto.ru", () => {
    assert.equal(validate({ plate: "", mileage: "50000", request_id: "r1" }).ok, false);
    assert.equal(validate({ plate: "А123ВС777", request_id: "r1" }).ok, false);
    assert.equal(validate({ plate: "А123ВС777", mileage: "0" }).ok, false);
    assert.equal(validate({ plate: "А123ВС777", mileage: "пятьдесят" }).ok, false);
    assert.equal(validate({}).ok, false);
});

test("проверка тела: пробег чистится до цифр, request_id уходит в custom data", () => {
    const cd = {};
    const out = run(codeOf(DEV, "Validate Request"),
        { body: { plate: " А123ВС777 ", mileage: "50 000", request_id: "abc-1" } }, {}, cd)[0].json;
    assert.deepEqual(out, { ok: true, request_id: "abc-1", plate: "А123ВС777", mileage: "50000", errors: [] });
    assert.equal(cd.request_id, "abc-1");
    assert.equal(validate({ plate: "А123ВС777", mileage: 123456 }).mileage, "123456");
});

const REQ = { ok: true, request_id: "abc-1", plate: "А123ВС777", mileage: "50000", errors: [] };
const FOUND = { mark: "MAZDA", model: "6", year: 2012, photos: { main: "//x/y" } };

test("ответ SUCCESS: цены, found_info с plate, без текстов для модели", () => {
    const sub = terminal({ price_range: { from: "622100", to: "832351" }, found_info: FOUND });
    const out = shape(sub, REQ);
    assert.deepEqual(Object.keys(out).sort(),
        ["found_info", "request_id", "status", "tradein_price_from", "tradein_price_to"]);
    assert.equal(out.status, "SUCCESS");
    assert.equal(out.tradein_price_from, 620000);
    assert.equal(out.tradein_price_to, 830000);
    assert.equal(out.found_info.plate, "А123ВС777");
    assert.equal(out.request_id, "abc-1");
});

test("ответ не-SUCCESS: цены null, found_info нет, plate_seen при NO_REGION", () => {
    for (const [sub, status] of [
        [terminal({ error: "IN_PROGRESS" }), "IN_PROGRESS"],
        [terminal({}), "NOT_FOUND"],
        [terminal({ is_valid: false, reason: "NOT_A_PLATE" }), "INVALID_DATA"],
        [terminal({ is_valid: false, reason: "NO_REGION", plate: "О427ВН" }), "NO_REGION"],
    ]) {
        const out = shape(sub, REQ);
        assert.equal(out.status, status);
        assert.equal(out.tradein_price_from, null);
        assert.equal(out.tradein_price_to, null);
        assert.equal("found_info" in out, false);
    }
    assert.equal(shape(terminal({ is_valid: false, reason: "NO_REGION", plate: "О427ВН" }), REQ).plate_seen, "О427ВН");
});

test("ошибка сабфлоу → ERROR, плохой запрос → BAD_REQUEST", () => {
    const err = run(codeOf(DEV, "Shape Error"), { error: { message: "boom" } }, { "Validate Request": REQ })[0].json;
    assert.deepEqual(err, { request_id: "abc-1", status: "ERROR", error: "boom" });
    const bad = run(codeOf(DEV, "Shape Bad Request"), { ...REQ, ok: false, errors: ["plate: пусто"] })[0].json;
    assert.equal(bad.status, "BAD_REQUEST");
});

// ---------- N3: SUCCESS без цены — характеризация сабфлоу как есть ----------

test("N3: ветка stats/predict — prices.tradein без to даёт SUCCESS с ценой 0", () => {
    const out = terminal({ prices: { tradein: { from: 500000 }, autoru: {} } });
    assert.equal(out.status, "SUCCESS");
    assert.equal(out.tradein_price_to, 0);
});

test("N3: ветка stats/predict — пустой prices.tradein {} даёт SUCCESS с нулями", () => {
    const out = terminal({ prices: { tradein: {}, autoru: {} }, found_info: FOUND });
    assert.equal(out.status, "SUCCESS");
    assert.equal(out.tradein_price_from, 0);
    assert.equal(out.tradein_price_to, 0);
    assert.equal(out.found_info.plate, "А123ВС777", "found_info при нулевой цене уезжает");
});

test("N3: ветка predict_by_vin_or_lp — from > 0, to пустой даёт SUCCESS с to = 0", () => {
    const out = terminal({ price_range: { from: "622100" }, found_info: FOUND });
    assert.equal(out.status, "SUCCESS");
    assert.equal(out.tradein_price_to, 0);
    assert.ok(out.found_info);
});

test("N3: цена меньше 5 000 после round10k превращается в 0", () => {
    const out = terminal({ price_range: { from: "4000", to: "4999" }, found_info: FOUND });
    assert.equal(out.status, "SUCCESS");
    assert.equal(out.tradein_price_to, 0);
});

test("N3: price_range.from = 0 до терминальной ноды SUCCESS не доходит", () => {
    // If/If1 пропускают в ветку price_range только from > 0; с from = 0 и без
    // found_info ответ уходит в Code как есть → NOT_FOUND.
    assert.equal(terminal({ price_range: { from: 0, to: 0 } }).status, "NOT_FOUND");
});
