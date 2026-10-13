// U7 (R11, R16–R18, R21): тесты модели величин calc_model.js.
// Запуск: node tests/js/calc_model.test.mjs (из корня бот-репо;
// pytest-обёртка — tests/test_js_suite.py). Голый node, assert/strict,
// без npm и фреймворков (KTD «Node-прогон без стека»).
//
// Движок подключается через createRequire и отдаётся модели явно
// (CalcModel.setCalcEngine) — в браузере тот же файл берёт функции движка из
// глобальной области; глобальный путь проверяется отдельным тестом через
// node:vm (см. «браузерный путь»).
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const srcDir = path.join(here, "..", "..", "widget-custom", "cabinet-app");
// CABINET_BUILD=1 — тот же прогон на сборке Production (минификация не меняет расчёт)
const appDir = process.env.CABINET_BUILD === "1" ? path.join(srcDir, "Production") : srcDir;

const E = require(path.join(appDir, "calc_engine.browser.js"));
const M = require(path.join(appDir, "calc_model.js"));
M.setCalcEngine(E);

const GOLDEN = JSON.parse(readFileSync(path.join(srcDir, "golden_cases.json"), "utf-8"));

// ── микро-раннер: считает ассерты, копит падения, код выхода ненулевой ──────
let asserts = 0;
let failed = 0;
const failures = [];

function eq(actual, expected, msg) { asserts += 1; assert.equal(actual, expected, msg); }
function deep(actual, expected, msg) { asserts += 1; assert.deepEqual(actual, expected, msg); }
function ok(cond, msg) { asserts += 1; assert.ok(cond, msg); }
function near(actual, expected, tol, msg) {
    asserts += 1;
    assert.ok(Math.abs(actual - expected) <= tol,
        `${msg || "near"}: ${actual} != ${expected} (допуск ${tol})`);
}

function test(name, fn) {
    try {
        fn();
    } catch (e) {
        failed += 1;
        failures.push(`FAIL ${name}: ${e.message}`);
    }
}

// ── общие данные ───────────────────────────────────────────────────────────
// Матрица со сменой вилки по взносу: на длинных сроках 18% до 30% взноса и
// 14% выше — прикосновение к платежу перебрасывает ведомый взнос через границу
// вилки, что и проверяет двухпроходность.
const RATES = [
    { Rate: 8, Term_From: 12, Term_To: 36, Downpayment_From: 50, Downpayment_To: 100 },
    { Rate: 12, Term_From: 12, Term_To: 36, Downpayment_From: 20, Downpayment_To: 49.99 },
    { Rate: 14, Term_From: 37, Term_To: 84, Downpayment_From: 30, Downpayment_To: 100 },
    { Rate: 18, Term_From: 37, Term_To: 84, Downpayment_From: 10, Downpayment_To: 29.99 }
];

const PRICE = 3000000;

// Снапшот в формате контракта U5 (/cabinet/api/me → calc).
function mkCalc(over) {
    return Object.assign({
        schema_version: 1,
        item_id: "cmp-1",
        item_name: "Тестовая комплектация",
        price: PRICE,
        target_payment: null,
        rates: RATES,
        program: null,
        found_info: null,
        ti_valuation: null
    }, over || {});
}

// Тождество PMT: тройка (платёж, взнос, срок) согласована при отданной ставке.
function assertPmtIdentity(s, tol, label) {
    near(E.calculatePMT(s.price - s.down, s.rate, s.term), s.payment, tol,
        `${label}: тождество calculatePMT`);
}
// Обратное тождество: maxLoan(платёж, ставка, срок) === заём.
function assertMaxLoanIdentity(s, tol, label) {
    near(E.calculateMaxLoan(s.payment, s.rate, s.term), s.price - s.down, tol,
        `${label}: тождество calculateMaxLoan`);
}

function numbersOf(s) {
    return {
        status: s.status, mode: s.mode, payment: s.payment, down: s.down,
        term: s.term, rate: s.rate, rateText: s.rateText, loan: s.loan
    };
}

const base = M.initState(mkCalc());

// ───────────────────────────────────────────────────────────────────────────
// 1. Стандартная инициализация и границы
// ───────────────────────────────────────────────────────────────────────────
test("стандартная инициализация: минимальный взнос, максимальный срок", () => {
    eq(base.status, "ok", "status");
    eq(base.mode, "credit", "mode");
    eq(base.down, 600000, "взнос = ceil10k(price·20%)");
    eq(base.term, 84, "срок = максимальный");
    eq(base.rate, 0.18, "ставка вилки 10–29.99%");
    eq(base.rateText, "18%", "текст ставки");
    eq(base.loan, PRICE - 600000, "заём");
    assertPmtIdentity(base, 1e-6, "стандартная инициализация");
    assertMaxLoanIdentity(base, 1e-6, "стандартная инициализация");
});

test("границы шкал: взнос MinPct..70%, срок 12..Max_Term_Months, шаги", () => {
    deep(base.bounds.down, { min: 600000, max: 2100000, step: 10000 }, "границы взноса");
    deep(base.bounds.term, { min: 12, max: 84, step: 12 }, "границы срока");
    eq(base.bounds.payment.step, 1000, "шаг платежа");
    // Диапазон платежа — ПРИ ТЕКУЩЕМ СРОКЕ (84): min при взносе 70%, max при MinPct.
    near(base.bounds.payment.min,
        E.calculatePMT(PRICE - 2100000, E.getSmartRate(RATES, 84, 70).val, 84), 1e-9,
        "min платежа при взносе 70%");
    near(base.bounds.payment.max,
        E.calculatePMT(PRICE - 600000, E.getSmartRate(RATES, 84, 20).val, 84), 1e-9,
        "max платежа при минимальном взносе");
});

test("MinPct-дефолт без программы = 20 (сверка с движком)", () => {
    const engineStd = E.calculateCreditScenarios(RATES, null, PRICE, 0);
    eq(engineStd.scenarios.standard.down_payment, base.bounds.down.min,
        "нижняя граница взноса = взнос стандартного сценария движка");
    eq(base.bounds.down.min, PRICE * 20 / 100, "20% от цены");
    eq(engineStd.scenarios.standard.term, base.bounds.term.max, "максимальный срок движка");
});

test("границы по программе: MinPct/Max_Term_Months из строки, пустые поля → 10/84", () => {
    const withProgram = M.initState(mkCalc({
        program: { Program_Name: "P1", Max_Term_Months: 60, Min_Down_Payment_Pct: 30 }
    }));
    deep(withProgram.bounds.down, { min: 900000, max: 2100000, step: 10000 }, "взнос от 30%");
    eq(withProgram.bounds.term.max, 60, "срок до 60");
    eq(withProgram.term, 60, "инициализация на максимальном сроке программы");
    // Зеркало тернарника движка: пустое поле в найденной строке → 10% и 84 мес.
    const emptyFields = M.initState(mkCalc({
        program: { Program_Name: "P2", Max_Term_Months: null, Min_Down_Payment_Pct: null }
    }));
    eq(emptyFields.bounds.down.min, PRICE * 10 / 100, "пустой MinPct → 10%");
    eq(emptyFields.bounds.term.max, 84, "пустой Max_Term_Months → 84");
});

test("Min_Down_Payment (план 2026-09-30-001 U2): новый ключ главнее прежнего", () => {
    const s = M.initState(mkCalc({
        program: { Program_Name: "P3", Max_Term_Months: 84, Min_Down_Payment: 12, Min_Down_Payment_Pct: 12 }
    }));
    eq(s.bounds.down.min, PRICE * 12 / 100, "взнос от 12%");
    const onlyNew = M.initState(mkCalc({ program: { Max_Term_Months: 84, Min_Down_Payment: 25 } }));
    eq(onlyNew.bounds.down.min, PRICE * 25 / 100, "только новый ключ — 25%");
});

test("максимум срока прижимается к сетке шага 12 вниз", () => {
    const s = M.initState(mkCalc({ program: { Max_Term_Months: 80, Min_Down_Payment_Pct: 20 } }));
    eq(s.bounds.term.max, 72, "80 → 72 (сетка 12,24,…)");
    eq(s.term, 72, "инициализация на достижимом максимуме");
});

// ───────────────────────────────────────────────────────────────────────────
// 2. Три направления touch + матрица со сменой вилки (двухпроходность)
// ───────────────────────────────────────────────────────────────────────────
test("touch платежа: платёж ведущий (round1k), пересчитан взнос", () => {
    const s = M.touch(base, "payment", 39000);
    eq(s.status, "ok", "status");
    eq(s.payment, 39000, "ведущий платёж неприкосновенен");
    eq(s.term, 84, "срок не тронут");
    ok(s.down !== base.down, "взнос пересчитан");
    assertPmtIdentity(s, 1e-6, "touch платежа");
    assertMaxLoanIdentity(s, 1e-6, "touch платежа");
});

test("touch платежа: смена вилки взносом (двухпроходная ставка, стабилизация)", () => {
    // Черновая ставка берётся при взносе 20.0% → 18%; ведомый взнос уезжает
    // выше 30% → финальная вилка 14%; пересчёт даёт взнос 30.6% — вилка та же,
    // осцилляции нет, ставка итоговая.
    const s = M.touch(base, "payment", 39000);
    eq(E.getSmartRate(RATES, 84, base.downPct).val, 0.18, "черновая ставка — 18%");
    eq(s.rate, 0.14, "финальная ставка — 14% (второй проход)");
    ok(s.downPct > 30, "взнос перешёл в вилку 30%+");
    // Однопроходный результат (взнос по черновой ставке) заметно больше —
    // значит второй проход действительно применён.
    const onePass = PRICE - E.calculateMaxLoan(39000, 0.18, 84);
    ok(Math.abs(onePass - s.down) > 100000, "второй проход изменил ведомую");
    assertPmtIdentity(s, 1e-6, "двухпроходность");
});

test("touch платежа: осцилляция на границе вилки — ровно 2 итерации, берётся последняя", () => {
    // Черновая 18% → взнос 35% → вилка 14% → пересчёт → взнос 27% (снова вилка
    // 18%). Стоп после второй итерации: ставка и взнос — от последней.
    const s = M.touch(base, "payment", 41000);
    eq(s.rate, 0.14, "берётся ставка последней итерации");
    ok(s.downPct < 30, "взнос вернулся в вилку 18% — модель не осциллирует дальше");
    eq(E.getSmartRate(RATES, 84, s.downPct).val, 0.18, "вилка по итоговому взносу — 18%");
    assertPmtIdentity(s, 1e-6, "осцилляция: тождество по отданной ставке");
    // Детерминизм: повторный вызов с тем же входом — тот же результат.
    deep(numbersOf(M.touch(base, "payment", 41000)), numbersOf(s), "детерминизм повтора");
    deep(numbersOf(M.touch(M.touch(base, "payment", 41000), "payment", 41000)), numbersOf(s),
        "детерминизм при повторном прикосновении тем же значением");
});

test("touch срока: срок ведущий (сетка 12), пересчитан взнос при текущем платеже", () => {
    const mid = M.touch(base, "payment", 39000);
    const s = M.touch(mid, "term", 60);
    eq(s.term, 60, "ведущий срок неприкосновенен");
    eq(s.payment, mid.payment, "платёж не тронут");
    ok(s.down !== mid.down, "взнос пересчитан при новом сроке");
    assertPmtIdentity(s, 1e-6, "touch срока");
    assertMaxLoanIdentity(s, 1e-6, "touch срока");
    // Смена вилки сроком: 36 мес. — другая половина матрицы.
    const short = M.touch(mid, "term", 36);
    eq(short.term, 36, "срок 36");
    ok(short.rate === 0.12 || short.rate === 0.08, "ставка из вилки сроков 12–36");
    assertPmtIdentity(short, 1e-6, "touch срока со сменой вилки");
    deep(numbersOf(M.touch(mid, "term", 36)), numbersOf(short), "детерминизм");
});

test("touch срока: округление к сетке шага 12 внутри границ", () => {
    eq(M.touch(base, "term", 15).term, 12, "15 → 12 (ближайший)");
    eq(M.touch(base, "term", 19).term, 24, "19 → 24 (ближайший)");
    eq(M.touch(base, "term", 18).term, 24, "18 → 24 (половина — вверх)");
    eq(M.touch(base, "term", 1).term, 12, "ниже границы → 12");
    eq(M.touch(base, "term", 9999).term, 84, "выше границы → 84");
});

test("touch взноса: взнос ведущий (round10k), пересчитан платёж", () => {
    const s = M.touch(base, "down", 1000000);
    eq(s.down, 1000000, "ведущий взнос неприкосновенен");
    eq(s.term, 84, "срок не тронут");
    eq(s.rate, 0.14, "смена вилки взносом: 33.3% → 14%");
    ok(s.payment < base.payment, "платёж пересчитан вниз");
    assertPmtIdentity(s, 1e-9, "touch взноса");
    assertMaxLoanIdentity(s, 1e-6, "touch взноса");
    // Ведомая — как есть, без округления до 1000.
    ok(s.payment % 1000 !== 0, "ведомый платёж не округляется");
    deep(numbersOf(M.touch(base, "down", 1000000)), numbersOf(s), "детерминизм");
});

test("touch взноса: округление к 10 000 и кламп у нижней границы (не под MinPct)", () => {
    eq(M.touch(base, "down", 1004000).down, 1000000, "1 004 000 → 1 000 000");
    eq(M.touch(base, "down", 1006000).down, 1010000, "1 006 000 → 1 010 000");
    eq(M.touch(base, "down", 592000).down, 600000,
        "округление вниз провалило бы под MinPct → шаг ВНУТРИ границ");
    eq(M.touch(base, "down", 1).down, 600000, "ниже MinPct → нижняя граница");
    eq(M.touch(base, "down", 2098000).down, 2100000, "у верхней границы → 70%");
    eq(M.touch(base, "down", 9e9).down, 2100000, "выше 70% → потолок");
    ok(M.touch(base, "down", 592000).downPct >= 20, "взнос не ниже MinPct");
});

// ───────────────────────────────────────────────────────────────────────────
// 3. Инварианты «ручка не убегает» и округления туда-обратно
// ───────────────────────────────────────────────────────────────────────────
test("инвариант «ручка не убегает»: payment === round1k(clamp(X)) при текущем сроке", () => {
    const pb = base.bounds.payment;
    [20000, 25500, 30400, 39000, 41000, 44499].forEach(x => {
        const s = M.touch(base, "payment", x);
        const expected = E.round1k(Math.min(Math.max(x, pb.min), pb.max));
        eq(s.payment, expected, `платёж для X=${x}`);
        assertPmtIdentity(s, 1e-6, `X=${x}`);
    });
});

test("X в глобальных границах, но недостижим при текущем сроке → кламп + тождество", () => {
    // На сроке 12 мес. платёж 120 000 достижим, на 84 — нет: кламп к максимуму
    // диапазона ПРИ ТЕКУЩЕМ СРОКЕ, тождество сохраняется.
    ok(E.calculatePMT(PRICE - 600000, E.getSmartRate(RATES, 12, 20).val, 12) > 120000,
        "120 000 достижим на коротком сроке (в глобальных границах)");
    ok(base.bounds.payment.max < 120000, "и недостижим при сроке 84");
    const s = M.touch(base, "payment", 120000);
    eq(s.payment, E.round1k(base.bounds.payment.max), "кламп к достижимому максимуму");
    eq(s.term, 84, "срок не тронут");
    ok(s.down >= s.bounds.down.min - 1e-6, "взнос не ниже MinPct");
    assertPmtIdentity(s, 1e-6, "кламп сверху");
});

test("инвариант округления: touch туда-обратно даёт идентичное состояние", () => {
    const a = M.touch(base, "down", 1000000);
    deep(numbersOf(M.touch(a, "down", 600000)), numbersOf(M.touch(base, "down", 600000)),
        "взнос: туда-обратно");
    const p1 = M.touch(base, "payment", 39000);
    const p2 = M.touch(p1, "payment", 45000);
    deep(numbersOf(M.touch(p2, "payment", 39000)), numbersOf(p1), "платёж: туда-обратно");
    const t1 = M.touch(M.touch(base, "payment", 39000), "term", 36);
    deep(numbersOf(M.touch(t1, "term", 84)), numbersOf(M.touch(base, "payment", 39000)),
        "срок: туда-обратно (двухпроходность возвращает ту же вилку)");
    // Округление ведущей идемпотентно: повтор с округлённым значением ничего не меняет.
    const r = M.touch(base, "down", 1004000);
    deep(numbersOf(M.touch(r, "down", r.down)), numbersOf(r), "повтор округлённым значением");
});

// ───────────────────────────────────────────────────────────────────────────
// 4. Blocked-зоны и притяжение ведомой ручки
// ───────────────────────────────────────────────────────────────────────────
test("blocked-зоны: недостижимые при текущем платеже доли шкал", () => {
    // При платеже стандартной инициализации короткие сроки требуют взноса выше
    // 70% — низ шкалы срока блокирован.
    ok(Array.isArray(base.blocked.term) && base.blocked.term.length > 0,
        "низ шкалы срока блокирован");
    eq(base.blocked.term[0].from, 0, "зона от начала шкалы");
    ok(base.blocked.term[0].to > 0 && base.blocked.term[0].to < 1, "доля в (0,1)");
    // Граница зоны — доля первого достижимого срока: (24-12)/72.
    near(base.blocked.term[0].to, (24 - 12) / 72, 1e-12, "первый достижимый срок — 24");
    const firstOk = 12 + base.blocked.term[0].to * 72;
    const dOk = PRICE - E.calculateMaxLoan(base.payment,
        E.getSmartRate(RATES, firstOk, base.downPct).val, firstOk);
    ok(dOk <= base.bounds.down.max + 1, "на первом достижимом сроке взнос в границах");
    // Все зоны корректны структурно.
    [].concat(base.blocked.term, base.blocked.down).forEach(z => {
        ok(z.from >= 0 && z.to <= 1 && z.from < z.to, "зона в (0..1) и непуста");
    });
});

test("смена срока увела ведомую в blocked → притянута без зачёта прикосновения", () => {
    const mid = M.touch(base, "payment", 50000);
    // Срок 12 лежит в blocked-зоне шкалы срока при текущем платеже.
    const frac12 = 0;
    ok(mid.blocked.term.some(z => frac12 >= z.from && frac12 < z.to),
        "срок 12 блокирован при текущем платеже");
    const s = M.touch(mid, "term", 12);
    eq(s.term, 12, "ведущий срок неприкосновенен");
    eq(s.down, s.bounds.down.max, "ведомый взнос притянут к ближайшей достижимой точке (70%)");
    near(s.payment, s.bounds.payment.min, 1e-6,
        "платёж следует за притянутой ручкой (минимум диапазона при этом сроке)");
    assertPmtIdentity(s, 1e-6, "притяжение");
    ok(!("touched" in s), "притяжение не выставляет маркер прикосновения");
    eq(s.status, "ok", "статус остаётся ok");
    deep(numbersOf(M.touch(mid, "term", 12)), numbersOf(s), "детерминизм притяжения");
});

test("blocked-зоны шкалы взноса при недостижимом снизу платеже", () => {
    // При платеже около минимума достижим только верх шкалы взноса.
    const low = M.touch(base, "payment", 20000);
    ok(low.blocked.down.length > 0, "часть шкалы взноса блокирована");
    ok(low.blocked.down.some(z => z.from === 0), "блокирован низ шкалы взноса");
    const reachFrac = (low.down - low.bounds.down.min) /
        (low.bounds.down.max - low.bounds.down.min);
    ok(low.blocked.down.every(z => reachFrac >= z.to - 1e-9 || reachFrac <= z.from + 1e-9),
        "ручка взноса не стоит внутри blocked-зоны");
});

// ───────────────────────────────────────────────────────────────────────────
// 5. impossible — точка движка
// ───────────────────────────────────────────────────────────────────────────
test("impossible: платёж ниже минимума → единственная точка, совпадает с движком", () => {
    const engineRes = E.calculateCreditScenarios(RATES, null, PRICE, 5000);
    eq(engineRes.status, "impossible", "движок: impossible");
    const rc = engineRes.scenarios.reality_check;

    const s = M.initState(mkCalc({ target_payment: 5000 }));
    eq(s.status, "impossible", "модель: impossible");
    eq(s.payment, rc.monthly_payment, "минимальный платёж = движок");
    eq(s.down, rc.down_payment, "взнос 70% = движок");
    eq(s.term, rc.term, "максимальный срок = движок");
    eq(s.rateText, rc.rate_text, "текст ставки = движок");
    eq(s.minPayment, rc.monthly_payment, "minPayment для пояснения UI");
    deep(s.blocked, { down: [{ from: 0, to: 1 }], term: [{ from: 0, to: 1 }] },
        "обе шкалы блокированы целиком (ползунки в одной точке)");
    assertPmtIdentity(s, 500, "impossible: тождество с точностью округления ведущей");

    // touch платежа ниже порога — тот же результат.
    const t = M.touch(base, "payment", 5000);
    deep(numbersOf(t), numbersOf(s), "touch ниже порога = impossible");
    // Порог — ровно round1k движка.
    eq(M.touch(base, "payment", s.minPayment).status, "ok", "на минимуме — ok");
    eq(M.touch(base, "payment", s.minPayment - 1).status, "impossible", "на рубль ниже — impossible");
});

test("impossible: выход прикосновением к взносу возвращает ok", () => {
    const imp = M.initState(mkCalc({ target_payment: 5000 }));
    const out = M.touch(imp, "down", 1000000);
    eq(out.status, "ok", "статус восстановлен");
    eq(out.down, 1000000, "взнос применён");
    assertPmtIdentity(out, 1e-9, "выход из impossible");
});

// ───────────────────────────────────────────────────────────────────────────
// 6. Матрица ставок: пустая и «~»-вилка
// ───────────────────────────────────────────────────────────────────────────
test("пустая матрица → 16% (Default), состояния работают", () => {
    const s = M.initState(mkCalc({ rates: [] }));
    eq(s.status, "ok", "status");
    eq(s.rate, 0.16, "дефолтная ставка движка");
    eq(s.rateText, "16% (Default)", "текст дефолта");
    assertPmtIdentity(s, 1e-6, "пустая матрица");
    const t = M.touch(s, "payment", 40000);
    eq(t.rate, 0.16, "ставка не меняется");
    assertPmtIdentity(t, 1e-6, "пустая матрица: touch");
    eq(M.touch(s, "payment", 100).status, "impossible", "impossible работает и без матрицы");
});

test("«~»-вилка: rateText отдаётся с «~» как есть", () => {
    const s = M.initState(mkCalc({
        rates: [{ Rate: 9, Term_From: 12, Term_To: 84, Downpayment_From: 50, Downpayment_To: 100 }]
    }));
    eq(s.rate, 0.09, "ставка из ближайшей вилки по сроку");
    eq(s.rateText, "~9%", "«~» сохранён");
    ok(s.rateText.indexOf("~") === 0, "текст начинается с «~»");
    const t = M.touch(s, "down", 1800000);
    eq(t.rateText, "9%", "точное попадание в вилку — без «~»");
});

// ───────────────────────────────────────────────────────────────────────────
// 7. Комбо (R17)
// ───────────────────────────────────────────────────────────────────────────
test("комбо: взнос = оценка + доплата, свободен только срок", () => {
    const s = M.initState(mkCalc({ ti_valuation: 900000 }));
    eq(s.mode, "combo", "режим combo");
    eq(s.status, "ok", "status");
    eq(s.down, 900000, "взнос = оценка (доплата 0)");
    eq(s.extraDown, 0, "доплата по умолчанию 0");
    eq(s.term, 84, "срок — максимальный");
    eq(s.loan, PRICE - 900000, "заём");
    deep(s.bounds.down, { min: 900000, max: 900000, step: 10000 }, "ползунка взноса нет");
    deep(s.bounds.extraDown, { min: 0, max: PRICE - 900000, step: 10000 }, "потолок доплаты");
    deep(s.blocked, { down: [], term: [] }, "blocked-зон в комбо нет");
    assertPmtIdentity(s, 1e-9, "комбо");
    eq(M.touch(s, "down", 1500000).down, 900000, "touch взноса в комбо игнорируется");
});

test("комбо: touch срока пересчитывает платёж", () => {
    const s = M.initState(mkCalc({ ti_valuation: 900000 }));
    const t = M.touch(s, "term", 36);
    eq(t.term, 36, "срок ведущий");
    eq(t.down, 900000, "взнос фиксирован");
    ok(t.payment > s.payment, "платёж вырос на коротком сроке");
    assertPmtIdentity(t, 1e-9, "комбо: touch срока");
    eq(M.touch(s, "term", 40).term, 36, "округление к сетке 12");
});

test("комбо: touch платежа притягивает к ближайшему достижимому на сетке сроков", () => {
    const s = M.initState(mkCalc({ ti_valuation: 900000 }));
    const grid = [];
    for (let t = 12; t <= 84; t += 12) {
        grid.push({ term: t, pmt: M.touch(s, "term", t).payment });
    }
    const target = 50000;
    let best = grid[0];
    grid.forEach(g => {
        if (Math.abs(g.pmt - target) < Math.abs(best.pmt - target)) best = g;
    });
    const t = M.touch(s, "payment", target);
    eq(t.term, best.term, "выбран срок с ближайшим по модулю платежом");
    near(t.payment, best.pmt, 1e-9, "платёж — достижимый, не запрошенный");
    assertPmtIdentity(t, 1e-9, "комбо: touch платежа");
    // Инициализация с желаемым платежом идёт тем же путём.
    const init = M.initState(mkCalc({ ti_valuation: 900000, target_payment: target }));
    deep(numbersOf(init), numbersOf(t), "initState(target_payment) = touch платежа");
});

test("комбо: доплата — типичная, кламп потолком и нулём", () => {
    const s = M.initState(mkCalc({ ti_valuation: 900000 }));
    const typical = M.touch(s, "extraDown", 300000);
    eq(typical.down, 1200000, "взнос = оценка + доплата");
    eq(typical.extraDown, 300000, "доплата принята");
    eq(typical.loan, PRICE - 1200000, "заём уменьшен");
    ok(typical.payment < s.payment, "платёж снижен");
    assertPmtIdentity(typical, 1e-9, "комбо: доплата");
    eq(M.touch(s, "extraDown", -5).extraDown, 0, "отрицательная доплата → 0");
    eq(M.touch(s, "extraDown", 9e9).extraDown, PRICE - 900000, "кламп потолком price − оценка");
});

test("комбо: полное покрытие → no_credit_needed, возврат доплатой", () => {
    const s = M.initState(mkCalc({ ti_valuation: 900000 }));
    const full = M.touch(s, "extraDown", PRICE - 900000);
    eq(full.status, "no_credit_needed", "кредит не нужен");
    eq(full.loan, 0, "заём нулевой");
    eq(full.payment, 0, "платёжа нет");
    eq(full.rate, null, "ставки нет");
    eq(M.touch(full, "payment", 30000).status, "no_credit_needed",
        "touch платежа в этом состоянии — no-op");
    const back = M.touch(full, "extraDown", 0);
    eq(back.status, "ok", "снижение доплаты возвращает расчёт");
    eq(back.down, 900000, "взнос вернулся к оценке");
    // Оценка сама по себе покрывает цену.
    const covered = M.initState(mkCalc({ ti_valuation: PRICE }));
    eq(covered.status, "no_credit_needed", "оценка ≥ цены при инициализации");
});

// ───────────────────────────────────────────────────────────────────────────
// 8. Golden-сверка с движком
// ───────────────────────────────────────────────────────────────────────────
test("golden-сверка: PMT/maxLoan модели совпадают с движком на кейсах golden_cases.json", () => {
    let checked = 0;
    GOLDEN.cases.forEach((c) => {
        const req = c.request;
        if (req.operation !== "credit_scenarios_batch") return;
        (req.items || []).forEach(item => {
            const expected = c.expected.results[item.id];
            const calc = mkCalc({
                price: item.price, rates: req.rates, program: req.program || null,
                target_payment: req.target_payment || null
            });
            const s = M.initState(calc);
            if (expected.status === "standard") {
                // Стандартная инициализация модели = сценарий `standard` движка
                // (взнос кратен 10 000 → ceil10k == round10k движка).
                const std = expected.scenarios.standard;
                eq(s.down, std.down_payment, "golden standard: взнос");
                eq(E.round1k(s.payment), std.monthly_payment, "golden standard: платёж");
                eq(s.term, std.term, "golden standard: срок");
                eq(s.rateText, std.rate_text, "golden standard: ставка");
                checked += 1;
            } else if (expected.status === "impossible") {
                const rc = expected.scenarios.reality_check;
                eq(s.status, "impossible", "golden impossible: статус");
                eq(s.down, rc.down_payment, "golden impossible: взнос 70%");
                eq(s.payment, rc.monthly_payment, "golden impossible: минимальный платёж");
                eq(s.term, rc.term, "golden impossible: срок");
                eq(s.rateText, rc.rate_text, "golden impossible: ставка");
                checked += 1;
            } else {
                // success: модель ведёт платёж (округляя ведущую), движок —
                // сценарий; сверяются тождества, а не совпадение точек.
                eq(s.status, "ok", "golden success: статус ok");
                assertPmtIdentity(s, 1e-6, "golden success");
                assertMaxLoanIdentity(s, 1e-6, "golden success");
                checked += 1;
            }
        });
    });
    ok(checked >= 6, `сверено кейсов: ${checked}`);
});

// ───────────────────────────────────────────────────────────────────────────
// 9. Деградации: engine_mismatch, no_calc, чистота модели
// ───────────────────────────────────────────────────────────────────────────
test("engine_mismatch при подмене версии движка", () => {
    const fake = Object.assign({}, E, { CALC_ENGINE_VERSION: "1999-01-01.0" });
    M.setCalcEngine(fake);
    try {
        const s = M.initState(mkCalc());
        eq(s.status, "engine_mismatch", "initState деградирует");
        eq(s.expected, M.EXPECTED_CALC_ENGINE_VERSION, "ожидаемая версия в состоянии");
        eq(s.actual, "1999-01-01.0", "фактическая версия в состоянии");
        eq(M.touch(base, "payment", 40000).status, "engine_mismatch", "touch тоже деградирует");
        eq(M.touch(s, "payment", 40000).status, "engine_mismatch", "touch по такому состоянию");
        M.setCalcEngine(null);
        eq(M.initState(mkCalc()).status, "engine_mismatch", "движка нет вовсе → mismatch");
        eq(M.initState(mkCalc()).actual, null, "фактической версии нет");
    } finally {
        M.setCalcEngine(E);
    }
    eq(M.EXPECTED_CALC_ENGINE_VERSION, E.CALC_ENGINE_VERSION,
        "модель написана под версию загруженного движка");
});

test("no_calc: снапшота нет или он невалиден", () => {
    eq(M.initState(null).status, "no_calc", "null");
    eq(M.initState(undefined).status, "no_calc", "undefined");
    eq(M.initState("нет").status, "no_calc", "не объект");
    eq(M.initState(mkCalc({ price: 0 })).status, "no_calc", "price = 0");
    eq(M.initState(mkCalc({ price: -1 })).status, "no_calc", "price < 0");
    eq(M.initState(mkCalc({ price: null })).status, "no_calc", "price отсутствует");
    eq(M.initState(mkCalc({ price: "нечисло" })).status, "no_calc", "price не парсится");
    const s = M.initState(null);
    eq(M.touch(s, "payment", 40000), s, "touch по no_calc возвращает то же состояние");
});

test("устойчивость touch: неизвестное поле и нечисловое значение", () => {
    eq(M.touch(base, "unknown", 1), base, "неизвестное поле — то же состояние");
    eq(M.touch(base, "payment", "abc"), base, "нечисловое значение — то же состояние");
    eq(M.touch(base, "payment", null), base, "null — то же состояние");
    eq(M.touch(base, "extraDown", 100000), base, "extraDown вне комбо игнорируется");
    // Иммутабельность: исходное состояние не мутировано.
    const before = numbersOf(base);
    M.touch(base, "down", 1500000);
    deep(numbersOf(base), before, "touch не мутирует вход");
});

test("модель чистая: ни DOM, ни сети, ни недетерминированных источников", () => {
    const src = readFileSync(path.join(srcDir, "calc_model.js"), "utf-8");
    [/\bfetch\s*\(/, /XMLHttpRequest/, /\bdocument\./, /\bwindow\./,
     /Date\.now/, /new Date\b/, /Math\.random/].forEach(re => {
        ok(!re.test(src), `в calc_model.js нет ${re}`);
    });
    ok(/if \(typeof module !== "undefined"\)/.test(src), "module-guard на месте");
    ok(/^var EXPECTED_CALC_ENGINE_VERSION/m.test(src), "версия объявлена через var (глобаль)");
});

test("браузерный путь: движок и модель в общей глобальной области, без инъекции", () => {
    const sandbox = {};
    vm.createContext(sandbox);
    vm.runInContext(readFileSync(path.join(appDir, "calc_engine.browser.js"), "utf-8"), sandbox);
    vm.runInContext(readFileSync(path.join(appDir, "calc_model.js"), "utf-8"), sandbox);
    ok(typeof sandbox.CalcModel === "object", "глобаль CalcModel объявлена");
    eq(sandbox.EXPECTED_CALC_ENGINE_VERSION, E.CALC_ENGINE_VERSION, "версии сходятся");
    const s = sandbox.CalcModel.initState(mkCalc());
    eq(s.status, "ok", "глобальный движок подхвачен без setCalcEngine");
    eq(s.down, base.down, "результат совпадает с Node-путём");
    near(s.payment, base.payment, 1e-9, "платёж совпадает");
    const t = sandbox.CalcModel.touch(s, "payment", 39000);
    eq(t.payment, 39000, "touch работает в глобальной области");
    eq(t.rate, 0.14, "двухпроходность работает и в браузерном пути");
});

// ── Веб-вход (план 2026-09-30-001, U9): комбо — один взнос, цена combo_price ──
const COMBO_PRICE = 2750000;

test("U9: комбо без оценки — режим комбо, один взнос, кредит от доплаты по combo_price", () => {
    const s = M.initState(mkCalc({ combo_price: COMBO_PRICE, ti_valuation: null }), { combo: true });
    eq(s.mode, "combo", "режим комбо и без оценки");
    eq(s.status, "ok", "статус ok");
    eq(s.price, COMBO_PRICE, "цена — combo_price");
    eq(s.down, 0, "взнос = доплата (0), зачёта нет");
    eq(s.bounds.extraDown.max, COMBO_PRICE, "доплата до полной цены");
    const t = M.touch(s, "extraDown", 500000);
    eq(t.down, 500000, "доплата — единственный взнос");
    near(E.calculatePMT(COMBO_PRICE - 500000, t.rate, t.term), t.payment, 1e-6, "кредит от доплаты");
});

test("U9: без opts поведение прежнее — комбо только при оценке, цена price", () => {
    const s = M.initState(mkCalc({ combo_price: COMBO_PRICE, ti_valuation: null }));
    eq(s.mode, "credit", "без флага и без оценки — кредит, как раньше");
    eq(s.price, PRICE, "цена — price");
    const c = M.initState(mkCalc({ combo_price: COMBO_PRICE, ti_valuation: 900000 }));
    eq(c.mode, "combo", "с оценкой — комбо");
    eq(c.price, PRICE, "без флага цена price (клиенты до U9)");
});

test("U9: приход оценки — платёж 25 000 сохраняется или прижимается к новым границам (AE7)", () => {
    const calc0 = mkCalc({ combo_price: COMBO_PRICE, ti_valuation: null });
    let s = M.initState(calc0, { combo: true });
    s = M.touch(s, "extraDown", 200000);
    s = M.touch(s, "payment", 25000);
    // оценка пришла: новое состояние, перенос доплаты и платежа
    const calc1 = mkCalc({ combo_price: COMBO_PRICE, ti_valuation: 900000 });
    let n = M.initState(calc1, { combo: true });
    n = M.touch(n, "extraDown", s.extraDown);
    n = M.touch(n, "payment", 25000);
    eq(n.mode, "combo", "режим комбо");
    eq(n.down, 1100000, "взнос = зачёт + доплата");
    ok(n.payment >= n.bounds.payment.min - 1 && n.payment <= n.bounds.payment.max + 1,
        "платёж в новых границах");
    // платёж, недостижимый после оценки, прижимается к ближайшему (срок у края)
    const hi = M.touch(n, "payment", 1000000);
    eq(hi.term, hi.bounds.term.min, "слишком большой платёж — минимальный срок");
    const lo = M.touch(n, "payment", 1000);
    eq(lo.term, lo.bounds.term.max, "слишком маленький платёж — максимальный срок");
});

test("U9: платёж с лендинга ниже минимума — равен минимуму, не impossible (AE2)", () => {
    const calc = mkCalc({ target_payment: 1000 });
    const plain = M.initState(calc);
    eq(plain.status, "impossible", "без флага — impossible, как раньше");
    const s = M.initState(calc, { clampTarget: true });
    ok(s.status !== "impossible", "с clampTarget статус не impossible");
    eq(s.payment, M.minPayment(plain.status === "impossible" ? M.initState(mkCalc()) : plain),
        "платёж = минимально возможный");
    eq(M.minPayment(M.initState(mkCalc({ combo_price: COMBO_PRICE }), { combo: true })), null,
        "minPayment комбо — null");
});

test("U9: комбо с оценкой IN_PROGRESS/неуспехом — один взнос (AE6)", () => {
    const s = M.initState(mkCalc({ combo_price: COMBO_PRICE, ti_valuation: 0 }), { combo: true });
    eq(s.mode, "combo", "режим комбо");
    eq(s.down, 0, "зачёта нет — только доплата");
    eq(s.price, COMBO_PRICE, "цена combo_price");
});

// ── Шаг срока 12 мес (решение владельца 2026-10-01): оба входа ──
test("шаг 12: все сроки, которые модель выбирает сама, кратны 12", () => {
    const pays = [15000, 25000, 39000, 52000, 80000, 150000];
    const states = [];
    pays.forEach(p => {
        states.push(M.initState(mkCalc({ target_payment: p })));                       // кредит
        states.push(M.initState(mkCalc({ ti_valuation: 900000, target_payment: p })));  // комбо
        states.push(M.initState(mkCalc({ combo_price: 2750000, target_payment: p }), { combo: true }));
        states.push(M.touch(M.initState(mkCalc({ ti_valuation: 900000 })), "payment", p));
    });
    states.forEach(s => {
        if (s.status === "no_credit_needed") return;
        eq(s.term % 12, 0, `срок ${s.term} кратен 12`);
        eq(s.bounds.term.step, 12, "шаг шкалы 12");
    });
});

test("шаг 12: черновик со сроком не кратным 12 приводится к ближайшему кратному в шкале", () => {
    const b = M.initState(mkCalc());
    eq(M.touch(b, "term", 18).term, 24, "18 → 24");
    eq(M.touch(b, "term", 66).term, 72, "66 → 72");
    eq(M.touch(b, "term", 90).term, 84, "90 → 84 (максимум шкалы)");
    const combo = M.initState(mkCalc({ ti_valuation: 900000 }));
    eq(M.touch(combo, "term", 54).term, 60, "комбо: 54 → 60");
});

test("шаг 12: максимум программы не кратен 12 — максимум шкалы вниз до кратного", () => {
    eq(M.initState(mkCalc({ program: { Max_Term_Months: 66 } })).bounds.term.max, 60, "66 → 60");
    eq(M.initState(mkCalc({ program: { Max_Term_Months: 84 } })).bounds.term.max, 84, "84 → 84");
    eq(M.initState(mkCalc({ program: { Max_Term_Months: 12 } })).bounds.term.max, 12, "12 → 12");
});

// ───────────────────────────────────────────────────────────────────────────
if (failed > 0) {
    failures.forEach(f => console.error(f));
    console.error(`CALC MODEL FAIL: провалено тестов — ${failed}`);
    process.exit(1);
}
console.log(`CALC MODEL PASS: ассертов — ${asserts}`);
