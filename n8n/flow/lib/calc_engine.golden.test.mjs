// ============================================================
// U2: характеризационные (золотые) тесты Calc Engine.
// Сравнивают ОРИГИНАЛЬНЫЕ реализации (скопированы дословно из нод
// Tool - Car Search / Code in JavaScript1 и Tool_ Combo Calculator /
// Code in JavaScript1, замыкания заменены фабричными параметрами)
// с канонической библиотекой calc_engine.js на сетке кейсов.
// Запуск: node calc_engine.golden.test.mjs
// Побочный эффект: пишет golden_cases.json для Test_ Calc Engine.json.
// ============================================================
import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const lib = require("./calc_engine.js");

// ---------- ОРИГИНАЛ: Tool - Car Search / Code in JavaScript1 ----------
function originalCarSearch(ratesList, program) {
    function round10k(val) { return Math.round(val / 10000) * 10000; }
    function round1k(val) { return Math.round(val / 1000) * 1000; }
    function calculatePMT(loan, rateYear, months) {
        let rateMonth = rateYear / 12;
        if (rateMonth <= 0.00001) return loan / months;
        return (loan * rateMonth) / (1 - Math.pow(1 + rateMonth, -months));
    }
    function calculateMaxLoan(pmt, rateYear, months) {
        let rateMonth = rateYear / 12;
        if (rateMonth <= 0.00001) return pmt * months;
        return (pmt * (1 - Math.pow(1 + rateMonth, -months))) / rateMonth;
    }
    function getSmartRate(term, downPct) {
        if (!ratesList || ratesList.length === 0) return { val: 0.16, text: "16% (Default)" };
        const match = ratesList.find(r => {
            const tFrom = Number(r.Term_From);
            const tTo   = Number(r.Term_To);
            const dFrom = Number(r.Downpayment_From);
            const dTo   = (r.Downpayment_To !== undefined && r.Downpayment_To !== null && r.Downpayment_To !== "") ? Number(r.Downpayment_To) : 100;
            return term >= tFrom && term <= tTo && downPct >= dFrom && downPct <= dTo;
        });
        if (match) return { val: Number(match.Rate) / 100, text: `${match.Rate}%` };
        const termCandidates = ratesList.filter(r => {
            const tFrom = Number(r.Term_From);
            const tTo   = Number(r.Term_To);
            return term >= tFrom && term <= tTo;
        });
        if (termCandidates.length > 0) {
            termCandidates.sort((a, b) => Number(a.Downpayment_From) - Number(b.Downpayment_From));
            const best = termCandidates[0];
            return { val: Number(best.Rate) / 100, text: `~${best.Rate}%` };
        }
        return { val: 0.16, text: "~16% (Fallback)" };
    }
    function calculateCreditScenarios(price, targetPayment) {
        const maxTerm = program ? (Number(program.Max_Term_Months) || 84) : 84;
        const minTerm = 12;
        const minDownPctGlobal = program ? (Number(program.Min_Down_Payment_Pct) || 10) : 20;
        if (!targetPayment || targetPayment <= 0) {
            const r = getSmartRate(maxTerm, minDownPctGlobal);
            const downPayment = price * (minDownPctGlobal / 100);
            const loan = price - downPayment;
            const pmt = calculatePMT(loan, r.val, maxTerm);
            return {
                status: "standard",
                scenarios: {
                    standard: {
                        title: "Стандартный расчет",
                        down_payment: round10k(downPayment),
                        monthly_payment: round1k(pmt),
                        term: maxTerm,
                        rate_text: r.text
                    }
                }
            };
        }
        const realityDownPct = 70;
        const realityRate = getSmartRate(maxTerm, realityDownPct);
        const realityDownPayment = price * 0.7;
        const realityLoan = price - realityDownPayment;
        const minPossiblePayment = round1k(calculatePMT(realityLoan, realityRate.val, maxTerm));
        if (targetPayment < minPossiblePayment) {
            return {
                status: "impossible",
                message: `Платеж слишком мал. Минимум возможного: ${minPossiblePayment}`,
                min_possible_payment: minPossiblePayment,
                scenarios: {
                    reality_check: {
                        title: "Минимально возможный платеж",
                        description: `При взносе 70% и сроке ${maxTerm} мес.`,
                        down_payment: round10k(realityDownPayment),
                        monthly_payment: minPossiblePayment,
                        term: maxTerm,
                        rate_text: realityRate.text
                    }
                }
            };
        }
        let lightRateTemp = getSmartRate(maxTerm, minDownPctGlobal);
        let maxLoanLight = calculateMaxLoan(targetPayment, lightRateTemp.val, maxTerm);
        let lightDownPayment = price - maxLoanLight;
        const minProgramDownAbs = price * (minDownPctGlobal / 100);
        if (lightDownPayment < minProgramDownAbs) lightDownPayment = minProgramDownAbs;
        const lightDownPct = (lightDownPayment / price) * 100;
        const lightRateFinal = getSmartRate(maxTerm, lightDownPct);
        const lightFinalLoan = price - lightDownPayment;
        const lightFinalPmt = calculatePMT(lightFinalLoan, lightRateFinal.val, maxTerm);
        let fastRateTemp = getSmartRate(minTerm, 50);
        let maxLoanFast = calculateMaxLoan(targetPayment, fastRateTemp.val, minTerm);
        let fastDownPayment = price - maxLoanFast;
        if (fastDownPayment > price * 0.9) fastDownPayment = price * 0.9;
        const fastDownPct = (fastDownPayment / price) * 100;
        const fastRateFinal = getSmartRate(minTerm, fastDownPct);
        const fastFinalLoan = price - fastDownPayment;
        const fastFinalPmt = calculatePMT(fastFinalLoan, fastRateFinal.val, minTerm);
        return {
            status: "success",
            scenarios: {
                light_entry: {
                    title: "Минимальный взнос",
                    description: `Срок ${maxTerm} мес, минимальный взнос под платеж`,
                    down_payment: round10k(lightDownPayment),
                    monthly_payment: round1k(lightFinalPmt),
                    term: maxTerm,
                    rate_text: lightRateFinal.text
                },
                fast_pay: {
                    title: "Минимальный срок",
                    description: `Срок ${minTerm} мес, максимальный взнос`,
                    down_payment: round10k(fastDownPayment),
                    monthly_payment: round1k(fastFinalPmt),
                    term: minTerm,
                    rate_text: fastRateFinal.text
                }
            }
        };
    }
    return { getSmartRate, calculateCreditScenarios, calculatePMT };
}

// ---------- ОРИГИНАЛ: Tool_ Combo Calculator / Code in JavaScript1 ----------
function originalCombo(ratesList, program) {
    function round1k(val) { return Math.round(val / 1000) * 1000; }
    function calculatePMT(loan, rateYear, months) {
        let rateMonth = rateYear / 12;
        if (rateMonth <= 0.00001) return loan / months;
        return (loan * rateMonth) / (1 - Math.pow(1 + rateMonth, -months));
    }
    function getSmartRate(term, downPct) {
        if (!ratesList || ratesList.length === 0) return { val: 0.16, text: "16% (Default)" };
        const match = ratesList.find(r => {
            const tFrom = Number(r.Term_From);
            const tTo   = Number(r.Term_To);
            const dFrom = Number(r.Downpayment_From);
            const dTo   = (r.Downpayment_To !== undefined && r.Downpayment_To !== null && r.Downpayment_To !== "") ? Number(r.Downpayment_To) : 100;
            return term >= tFrom && term <= tTo && downPct >= dFrom && downPct <= dTo;
        });
        if (match) return { val: Number(match.Rate) / 100, text: `${match.Rate}%` };
        const termCandidates = ratesList.filter(r => term >= Number(r.Term_From) && term <= Number(r.Term_To));
        if (termCandidates.length > 0) {
            termCandidates.sort((a, b) => Number(a.Downpayment_From) - Number(b.Downpayment_From));
            const best = termCandidates[0];
            return { val: Number(best.Rate) / 100, text: `~${best.Rate}%` };
        }
        return { val: 0.16, text: "~16% (Fallback)" };
    }
    function calculateComboScenarios(price, targetPayment, tradeinValue, tradeinStatus) {
        if (tradeinStatus !== "SUCCESS") {
            return { status: "error", message: "Не удалось автоматически оценить старый автомобиль." };
        }
        if (tradeinValue >= price) {
            return { status: "full_coverage", message: "Стоимость старого авто полностью покрывает покупку нового!" };
        }
        const loan = price - tradeinValue;
        const downPct = (tradeinValue / price) * 100;
        const maxTerm = program ? (Number(program.Max_Term_Months) || 84) : 84;
        const terms = [12, 24, 36, 48, 60, 72, 84].filter(t => t <= maxTerm);
        let bestScenario = null;
        for (let term of terms) {
            let r = getSmartRate(term, downPct);
            let pmt = calculatePMT(loan, r.val, term);
            if (targetPayment > 0 && pmt <= targetPayment) {
                bestScenario = {
                    status: "success",
                    term: term,
                    monthly_payment: round1k(pmt),
                    down_payment: tradeinValue,
                    rate_text: r.text
                };
                break;
            }
        }
        if (!bestScenario) {
            let r = getSmartRate(maxTerm, downPct);
            let minPmt = calculatePMT(loan, r.val, maxTerm);
            bestScenario = {
                status: "impossible",
                term: maxTerm,
                monthly_payment: round1k(minPmt),
                down_payment: tradeinValue,
                rate_text: r.text,
                warning: "Желаемый платеж слишком мал. Расчет произведен на максимальный срок."
            };
        }
        return bestScenario;
    }
    return { getSmartRate, calculateComboScenarios };
}

// ---------- СЕТКА КЕЙСОВ ----------
const RATES_REAL = [
    { Rate: 8,  Term_From: 12, Term_To: 36, Downpayment_From: 50, Downpayment_To: "" },
    { Rate: 12, Term_From: 12, Term_To: 36, Downpayment_From: 20, Downpayment_To: 49.99 },
    { Rate: 14, Term_From: 37, Term_To: 84, Downpayment_From: 30, Downpayment_To: "" },
    { Rate: 18, Term_From: 37, Term_To: 84, Downpayment_From: 10, Downpayment_To: 29.99 },
];
const RATES_NARROW = [ // ни одна вилка не покрывает срок 12–36 → мягкий/жёсткий фолбэки
    { Rate: 15, Term_From: 48, Term_To: 84, Downpayment_From: 40, Downpayment_To: "" },
];
const RATES_SETS = { empty: [], real: RATES_REAL, narrow: RATES_NARROW };
const PROGRAMS = {
    none: null,
    p60: { Program_Name: "Base", Max_Term_Months: 60, Min_Down_Payment_Pct: 15 },
    pdef: { Program_Name: "Empty", Max_Term_Months: "", Min_Down_Payment_Pct: "" },
};

let checked = 0;
const golden = [];

// PMT: абсолютная проверка на известной тройке (1 млн, 16% годовых, 36 мес → 35 157.03)
assert.ok(Math.abs(lib.calculatePMT(1_000_000, 0.16, 36) - 35157.03) < 0.01, "PMT против аннуитетной формулы");
// Ставка ≈ 0 → линейное деление
assert.equal(lib.calculatePMT(1_200_000, 0, 24), 50_000);

// getSmartRate: точное попадание / мягкий фолбэк / жёсткий дефолт
assert.deepEqual(lib.getSmartRate(RATES_REAL, 24, 60), { val: 0.08, text: "8%" });
assert.deepEqual(lib.getSmartRate(RATES_NARROW, 24, 60), { val: 0.16, text: "~16% (Fallback)" });
assert.deepEqual(lib.getSmartRate([], 24, 60), { val: 0.16, text: "16% (Default)" });
assert.deepEqual(lib.getSmartRate(RATES_REAL, 48, 5), { val: 0.18, text: "~18%" }); // мягкий: срок попал, взнос ниже вилок

// Характеризация credit_scenarios: оригинал ≡ библиотека
for (const [rname, rates] of Object.entries(RATES_SETS)) {
    for (const [pname, program] of Object.entries(PROGRAMS)) {
        const orig = originalCarSearch(structuredClone(rates), structuredClone(program));
        for (const price of [1_500_000, 3_000_000]) {
            for (const payment of [0, 5_000, 30_000, 60_000, 200_000]) {
                const expected = orig.calculateCreditScenarios(price, payment);
                const actual = lib.calculateCreditScenarios(rates, program, price, payment);
                assert.deepEqual(actual, expected, `credit ${rname}/${pname}/${price}/${payment}`);
                checked++;
                if (rname === "real" && price === 3_000_000 && [0, 5_000, 60_000].includes(payment) && pname !== "pdef") {
                    golden.push({
                        request: { operation: "credit_scenarios_batch", target_payment: payment,
                                   items: [{ id: "t1", price }], rates, program },
                        expected: { operation: "credit_scenarios_batch", results: { t1: expected } },
                    });
                }
            }
        }
    }
}

// Характеризация combo_scenarios: оригинал ≡ библиотека
for (const [rname, rates] of Object.entries(RATES_SETS)) {
    for (const [pname, program] of Object.entries(PROGRAMS)) {
        const orig = originalCombo(structuredClone(rates), structuredClone(program));
        for (const price of [2_000_000]) {
            for (const tv of [0, 500_000, 1_990_000, 2_000_000, 2_100_000]) {
                for (const payment of [0, 25_000, 80_000]) {
                    for (const st of ["SUCCESS", "IN_PROGRESS", "NOT_FOUND"]) {
                        const expected = orig.calculateComboScenarios(price, payment, tv, st);
                        const actual = lib.calculateComboScenarios(rates, program, price, payment, tv, st);
                        assert.deepEqual(actual, expected, `combo ${rname}/${pname}/${tv}/${payment}/${st}`);
                        checked++;
                    }
                }
            }
        }
        if (rname === "real" && pname === "p60") {
            for (const [tv, payment, st] of [[500_000, 80_000, "SUCCESS"], [500_000, 25_000, "SUCCESS"],
                                             [2_100_000, 80_000, "SUCCESS"], [500_000, 80_000, "IN_PROGRESS"]]) {
                golden.push({
                    request: { operation: "combo_scenarios_batch", target_payment: payment,
                               tradein_value: tv, tradein_status: st,
                               items: [{ id: "t1", price: 2_000_000 }], rates, program },
                    expected: { operation: "combo_scenarios_batch",
                                results: { t1: orig.calculateComboScenarios(2_000_000, payment, tv, st) } },
                });
            }
        }
    }
}

// Max_Term ограничивает перебор сроков: при maxTerm 60 срок 72/84 недостижим
{
    const r = lib.calculateComboScenarios(RATES_REAL, PROGRAMS.p60, 2_000_000, 1, 500_000, "SUCCESS");
    assert.equal(r.status, "impossible");
    assert.equal(r.term, 60, "перебор сроков обрезан Max_Term_Months=60");
}

// ---------- U2: extraDown (доплата к взносу) в calculateComboScenarios ----------

// (г) Версионный маркер: экспортируется и непустой
assert.equal(typeof lib.CALC_ENGINE_VERSION, "string", "CALC_ENGINE_VERSION экспортируется строкой");
assert.ok(lib.CALC_ENGINE_VERSION.length > 0, "CALC_ENGINE_VERSION непустая");

// (а) extraDown опущен и extraDown=0 → результат бит-в-бит идентичен прежнему вызову
for (const [rname, rates] of Object.entries(RATES_SETS)) {
    for (const [pname, program] of Object.entries(PROGRAMS)) {
        for (const tv of [0, 500_000, 1_990_000, 2_000_000, 2_100_000]) {
            for (const payment of [0, 25_000, 80_000]) {
                for (const st of ["SUCCESS", "IN_PROGRESS", "NOT_FOUND"]) {
                    const omitted = lib.calculateComboScenarios(rates, program, 2_000_000, payment, tv, st);
                    const withZero = lib.calculateComboScenarios(rates, program, 2_000_000, payment, tv, st, 0);
                    assert.deepEqual(withZero, omitted, `extraDown=0 ≡ опущен: ${rname}/${pname}/${tv}/${payment}/${st}`);
                    checked++;
                }
            }
        }
    }
}

// (б) Типичный extraDown: price 2 490 000, оценка 890 000, доплата 200 000.
// Абсолютные ожидания посчитаны вручную через calculatePMT:
// эффективный взнос 1 090 000 (43.78%), заём 1 400 000;
// RATES_REAL: сроки 12/24/36 → 12% (PMT 124 388 / 65 903 / 46 500 > 40 000),
// срок 48 → 14% (PMT 38 257 ≤ 40 000) → success на 48 мес, платёж round1k = 38 000.
{
    const withExtra = lib.calculateComboScenarios(RATES_REAL, PROGRAMS.p60, 2_490_000, 40_000, 890_000, "SUCCESS", 200_000);
    assert.deepEqual(withExtra, {
        status: "success",
        term: 48,
        monthly_payment: 38_000,
        down_payment: 1_090_000,
        rate_text: "14%"
    }, "extraDown=200000: взнос = оценка + доплата, заём уменьшен, платёж пересчитан");
    // Без доплаты тот же запрос: взнос 890 000 (35.74%), заём 1 600 000;
    // PMT: 12/24/36 → 142 158 / 75 318 / 53 143, 48 → 43 722 (> 40 000), 60 → 37 229 ≤ 40 000 → success на 60 мес.
    const withoutExtra = lib.calculateComboScenarios(RATES_REAL, PROGRAMS.p60, 2_490_000, 40_000, 890_000, "SUCCESS");
    assert.deepEqual(withoutExtra, {
        status: "success",
        term: 60,
        monthly_payment: 37_000,
        down_payment: 890_000,
        rate_text: "14%"
    }, "без доплаты заём больше и срок длиннее — доплата действительно уменьшает заём");
    checked += 2;
}

// (в) Оценка + доплата ≥ цены → full_coverage (граница ровно price и с запасом)
{
    const exact = lib.calculateComboScenarios(RATES_REAL, PROGRAMS.p60, 2_490_000, 40_000, 890_000, "SUCCESS", 1_600_000);
    assert.equal(exact.status, "full_coverage", "оценка + доплата = price → full_coverage");
    const over = lib.calculateComboScenarios(RATES_REAL, PROGRAMS.p60, 2_490_000, 40_000, 2_300_000, "SUCCESS", 500_000);
    assert.equal(over.status, "full_coverage", "оценка + доплата > price → full_coverage");
    checked += 2;
}

// Golden-кейсы extraDown для билд-скрипта (U3): request несёт extra_down,
// expected — фактический результат канона (абсолютные значения запинены ассертами выше).
{
    const rates = RATES_SETS.real, program = PROGRAMS.p60;
    for (const [tv, payment, extra, st] of [[890_000, 40_000, 200_000, "SUCCESS"],
                                            [890_000, 40_000, 0, "SUCCESS"],
                                            [890_000, 40_000, 1_600_000, "SUCCESS"]]) {
        golden.push({
            request: { operation: "combo_scenarios_batch", target_payment: payment,
                       tradein_value: tv, tradein_status: st, extra_down: extra,
                       items: [{ id: "t1", price: 2_490_000 }], rates, program },
            expected: { operation: "combo_scenarios_batch",
                        results: { t1: lib.calculateComboScenarios(rates, program, 2_490_000, payment, tv, st, extra) } },
        });
    }
}

// Диспетчер: батч и noop
{
    const batch = lib.runCalc({ operation: "credit_scenarios_batch", target_payment: 60_000,
        items: [{ id: "a", price: 1_500_000 }, { id: "b", price: 3_000_000 }], rates: RATES_REAL, program: PROGRAMS.p60 });
    assert.deepEqual(Object.keys(batch.results), ["a", "b"]);
    assert.deepEqual(lib.runCalc({ operation: "noop" }), { operation: "noop" });
    assert.ok(lib.runCalc({ operation: "wat" }).error);
    // Проверяем golden через сам диспетчер
    for (const g of golden) assert.deepEqual(lib.runCalc(g.request), g.expected, "golden через runCalc");
}

writeFileSync(new URL("./golden_cases.json", import.meta.url),
    JSON.stringify(golden, null, 1), "utf-8");
console.log(`OK: ${checked} характеризационных сравнений + прямые проверки; golden-кейсов для n8n: ${golden.length}`);
