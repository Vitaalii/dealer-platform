// ============================================================
// CALC ENGINE — канонический источник кредитной математики (U2)
// Этот файл встраивается в Code-ноду воркфлоу "Tool: Calc Engine"
// скриптом build_calc_engine_workflow.py. Править математику — ТОЛЬКО здесь,
// затем перегенерировать воркфлоу и переимпортировать его в n8n.
// Семантика функций перенесена 1:1 из нод Car Search / Combo Calculator
// (подтверждено характеризационными тестами calc_engine.golden.test.mjs).
// ============================================================

// Версионный маркер (R12): var, не const — файл исполняется и как тело
// Code-ноды, и как браузерный скрипт в глобальной области.
var CALC_ENGINE_VERSION = "2026-08-06.1";

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

function getSmartRate(ratesList, term, downPct) {
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

// Кредитные сценарии (ветка credit) — перенос из Tool - Car Search / Code in JavaScript1
function calculateCreditScenarios(ratesList, program, price, targetPayment) {
    const maxTerm = program ? (Number(program.Max_Term_Months) || 84) : 84;
    const minTerm = 12;
    const minDownPctGlobal = program ? (Number(program.Min_Down_Payment_Pct) || 10) : 20;

    if (!targetPayment || targetPayment <= 0) {
        const r = getSmartRate(ratesList, maxTerm, minDownPctGlobal);
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
    const realityRate = getSmartRate(ratesList, maxTerm, realityDownPct);
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

    let lightRateTemp = getSmartRate(ratesList, maxTerm, minDownPctGlobal);
    let maxLoanLight = calculateMaxLoan(targetPayment, lightRateTemp.val, maxTerm);
    let lightDownPayment = price - maxLoanLight;
    const minProgramDownAbs = price * (minDownPctGlobal / 100);
    if (lightDownPayment < minProgramDownAbs) lightDownPayment = minProgramDownAbs;

    const lightDownPct = (lightDownPayment / price) * 100;
    const lightRateFinal = getSmartRate(ratesList, maxTerm, lightDownPct);
    const lightFinalLoan = price - lightDownPayment;
    const lightFinalPmt = calculatePMT(lightFinalLoan, lightRateFinal.val, maxTerm);

    let fastRateTemp = getSmartRate(ratesList, minTerm, 50);
    let maxLoanFast = calculateMaxLoan(targetPayment, fastRateTemp.val, minTerm);
    let fastDownPayment = price - maxLoanFast;
    if (fastDownPayment > price * 0.9) fastDownPayment = price * 0.9;

    const fastDownPct = (fastDownPayment / price) * 100;
    const fastRateFinal = getSmartRate(ratesList, minTerm, fastDownPct);
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

// Комбо-сценарии (трейд-ин как взнос) — перенос из Tool_ Combo Calculator / Code in JavaScript1
function calculateComboScenarios(ratesList, program, price, targetPayment, tradeinValue, tradeinStatus, extraDown = 0) {
    if (tradeinStatus !== "SUCCESS") {
        return { status: "error", message: "Не удалось автоматически оценить старый автомобиль." };
    }
    // U2 (R17): эффективный взнос = оценка трейд-ина + доплата; extraDown = 0 → прежнее поведение бит-в-бит.
    const effectiveDown = tradeinValue + extraDown;
    if (effectiveDown >= price) {
        return { status: "full_coverage", message: "Стоимость старого авто полностью покрывает покупку нового!" };
    }

    const loan = price - effectiveDown;
    const downPct = (effectiveDown / price) * 100;
    const maxTerm = program ? (Number(program.Max_Term_Months) || 84) : 84;
    const terms = [12, 24, 36, 48, 60, 72, 84].filter(t => t <= maxTerm);

    let bestScenario = null;
    for (let term of terms) {
        let r = getSmartRate(ratesList, term, downPct);
        let pmt = calculatePMT(loan, r.val, term);
        if (targetPayment > 0 && pmt <= targetPayment) {
            bestScenario = {
                status: "success",
                term: term,
                monthly_payment: round1k(pmt),
                down_payment: effectiveDown,
                rate_text: r.text
            };
            break;
        }
    }

    if (!bestScenario) {
        let r = getSmartRate(ratesList, maxTerm, downPct);
        let minPmt = calculatePMT(loan, r.val, maxTerm);
        bestScenario = {
            status: "impossible",
            term: maxTerm,
            monthly_payment: round1k(minPmt),
            down_payment: effectiveDown,
            rate_text: r.text,
            warning: "Желаемый платеж слишком мал. Расчет произведен на максимальный срок."
        };
    }
    return bestScenario;
}

// Диспетчер: один вызов Execute Workflow на обращение к инструменту (R31)
function runCalc(request) {
    const op = request && request.operation;
    if (op === "noop") {
        return { operation: "noop" };
    }
    if (op === "credit_scenarios_batch") {
        const results = {};
        for (const item of request.items || []) {
            results[item.id] = calculateCreditScenarios(
                request.rates || [], request.program || null,
                item.price, Number(request.target_payment) || 0
            );
        }
        return { operation: op, results };
    }
    if (op === "combo_scenarios_batch") {
        const results = {};
        for (const item of request.items || []) {
            results[item.id] = calculateComboScenarios(
                request.rates || [], request.program || null,
                item.price, Number(request.target_payment) || 0,
                Number(request.tradein_value) || 0, request.tradein_status,
                Number(request.extra_down) || 0
            );
        }
        return { operation: op, results };
    }
    return { operation: op || "unknown", error: `Неизвестная операция Calc Engine: ${op}` };
}

if (typeof module !== "undefined") {
    module.exports = {
        CALC_ENGINE_VERSION,
        round10k, round1k, calculatePMT, calculateMaxLoan, getSmartRate,
        calculateCreditScenarios, calculateComboScenarios, runCalc
    };
}
