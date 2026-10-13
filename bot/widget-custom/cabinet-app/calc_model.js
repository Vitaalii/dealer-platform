// ============================================================
// CALC MODEL (U7) — модель зависимости трёх величин кредита.
// Чистые функции: ни DOM, ни сети, ни источников недетерминизма (текущее
// время, случайные числа) — прогон повторяем. Математику НЕ повторяет: канон —
// движок (calculatePMT / calculateMaxLoan / getSmartRate / round1k / round10k
// из calc_engine.browser.js, R11).
//
// Двойной формат, как у движка: файл исполняется и как браузерный скрипт в
// глобальной области (var + function declarations), и как CommonJS-модуль в
// Node-тестах (module-guard в конце файла).
//
// Подключение движка:
//   - браузер: движок загружен ПЕРВЫМ скриптом, его функции лежат в глобальной
//     области — модель берёт их оттуда (см. _engine);
//   - Node: глобалей нет, тест подключает движок через createRequire и отдаёт
//     его явно — CalcModel.setCalcEngine(require(".../calc_engine.browser.js")).
//
// Публичный API (namespace CalcModel — глобаль в браузере, module.exports в Node):
//   EXPECTED_CALC_ENGINE_VERSION — версия движка, на которой модель проверена;
//   initState(calc)              — состояние из снапшота /cabinet/api/me;
//   touch(state, field, value)   — новое состояние после прикосновения
//                                  (field: payment | term | down | extraDown);
//   setCalcEngine(engine)        — явная инъекция движка (Node-тесты).
// ============================================================

// Версия движка, под которую написана модель (R12, KTD «Защита от дрейфа»,
// рантайм-уровень): расхождение с CALC_ENGINE_VERSION загруженного движка →
// status "engine_mismatch", app.js деградирует в ручной ввод (R18).
var EXPECTED_CALC_ENGINE_VERSION = "2026-08-06.1";

var CALC_MODEL_TERM_MIN = 12;        // нижняя граница срока (зеркало движка)
var CALC_MODEL_TERM_STEP = 12;       // сетка срока: 12, 24, 36, … 84 (решение владельца 2026-10-01)
var CALC_MODEL_DOWN_STEP = 10000;    // шаг взноса (round10k)
var CALC_MODEL_PAYMENT_STEP = 1000;  // шаг платежа (round1k)
var CALC_MODEL_DOWN_MAX_PCT = 70;    // потолок взноса = порог impossible (KTD)
var CALC_MODEL_EPS = 1;              // допуск сравнения сумм, рубли

var CALC_MODEL_ENGINE = null;  // явная инъекция (Node); в браузере остаётся null

// Явная инъекция движка для Node-тестов. В браузере не вызывается.
function calcModelSetEngine(engine) {
    CALC_MODEL_ENGINE = engine || null;
}

// Движок: инъекция (Node) либо глобальная область (браузер, движок загружен
// первым скриптом). typeof-проверки — чтобы отсутствие движка не бросало
// ReferenceError, а давало управляемую деградацию.
function calcModelEngine() {
    if (CALC_MODEL_ENGINE) return CALC_MODEL_ENGINE;
    if (typeof calculatePMT === "function" && typeof calculateMaxLoan === "function" &&
        typeof getSmartRate === "function" && typeof round1k === "function" &&
        typeof round10k === "function") {
        return {
            CALC_ENGINE_VERSION: (typeof CALC_ENGINE_VERSION !== "undefined")
                ? CALC_ENGINE_VERSION : null,
            calculatePMT: calculatePMT,
            calculateMaxLoan: calculateMaxLoan,
            getSmartRate: getSmartRate,
            round1k: round1k,
            round10k: round10k
        };
    }
    return null;
}

// ── Утилиты ────────────────────────────────────────────────────────────────

function calcModelNum(value) {
    if (value === null || value === undefined || value === "") return null;
    if (typeof value === "boolean") return null;
    var n = Number(value);
    return isFinite(n) ? n : null;
}

function calcModelClamp(value, min, max) {
    if (value < min) return min;
    if (value > max) return max;
    return value;
}

// Округление ведущей величины к ближайшему шагу ВНУТРИ границ (KTD «Максимум
// шкалы взноса…»): кламп → округление → если шаг вынес за границу, ближайший
// шаг внутри неё. Границы уже кратны шагу для взноса и срока; для платежа
// (границы — произвольные PMT) шага внутри может не оказаться — тогда сырой
// кламп, иначе величина ушла бы за достижимый диапазон.
function calcModelRoundInside(value, step, min, max) {
    var clamped = calcModelClamp(value, min, max);
    var rounded = Math.round(clamped / step) * step;
    if (rounded < min) rounded = Math.ceil(min / step) * step;
    if (rounded > max) rounded = Math.floor(max / step) * step;
    if (rounded < min || rounded > max) return clamped;
    return rounded;
}

// Границы шкал. MinPct и Max_Term_Months — ЗЕРКАЛО тернарников движка
// (calc_engine.js:51–53): без строки программы 20% и 84 мес., с программой —
// значение поля. Минимальный взнос — Min_Down_Payment (план 2026-09-30-001,
// U2/R21): бэкенд подставляет 20 % при пустом значении и дублирует его в
// прежний ключ Min_Down_Payment_Pct для движка; снапшоты до U2 несут только
// прежний ключ (пустое поле → 10 %, как раньше) — до перезаморозки.
function calcModelBounds(calc) {
    var price = calc.price;
    var program = calc.program;
    var minPct = program
        ? (Number(program.Min_Down_Payment) || Number(program.Min_Down_Payment_Pct) || 10)
        : 20;
    var maxTermRaw = program ? (Number(program.Max_Term_Months) || 84) : 84;
    // Максимум срока прижимается к сетке шага 6 вниз: значение вне сетки
    // ползунком недостижимо, и модель не должна обещать недостижимое.
    var span = Math.max(0, maxTermRaw - CALC_MODEL_TERM_MIN);
    var termMax = CALC_MODEL_TERM_MIN +
        Math.floor(span / CALC_MODEL_TERM_STEP) * CALC_MODEL_TERM_STEP;
    var downMin = Math.ceil(price * minPct / 100 / CALC_MODEL_DOWN_STEP) * CALC_MODEL_DOWN_STEP;
    var downMax = Math.floor(price * CALC_MODEL_DOWN_MAX_PCT / 100 / CALC_MODEL_DOWN_STEP) *
        CALC_MODEL_DOWN_STEP;
    if (downMin > downMax) downMin = downMax;  // вырожденный случай MinPct > 70
    return {
        minPct: minPct, termMin: CALC_MODEL_TERM_MIN, termMax: termMax,
        downMin: downMin, downMax: downMax
    };
}

function calcModelPct(calc, down) {
    return (down / calc.price) * 100;
}

function calcModelRate(engine, calc, term, downPct) {
    return engine.getSmartRate(calc.rates || [], term, downPct);
}

// Платёж при заданных взносе и сроке. Ставка зависит только от них, поэтому
// второй проход вырожден (пересчитывать нечего) — «двухпроходность» работает
// там, где ведомая величина сама двигает взнос% (см. calcModelDownAt).
function calcModelPaymentAt(engine, calc, down, term) {
    var rate = calcModelRate(engine, calc, term, calcModelPct(calc, down));
    return {
        payment: engine.calculatePMT(calc.price - down, rate.val, term),
        rate: rate
    };
}

// Взнос при заданных платеже и сроке — ДВУХПРОХОДНАЯ ставка (R16, KTD
// «Разрешение противоречия…»): черновая ставка по текущему взносу% → взнос →
// финальная ставка по новому взносу% → сменилась вилка → взнос пересчитывается
// ещё раз (≤2 итераций, берётся последняя — детерминированный стоп осцилляции
// на границе вилки). Ведомая возвращается КАК ЕСТЬ, без округления и клампа.
// Возвращаемая ставка — та, которой посчитана итоговая ведомая: тождество
// calculatePMT(price − down, rate, term) === payment сохраняется.
function calcModelDownAt(engine, calc, payment, term, seedPct) {
    var draft = calcModelRate(engine, calc, term, seedPct);
    var down = calc.price - engine.calculateMaxLoan(payment, draft.val, term);
    var finalRate = calcModelRate(engine, calc, term, calcModelPct(calc, down));
    if (finalRate.val !== draft.val) {
        down = calc.price - engine.calculateMaxLoan(payment, finalRate.val, term);
    }
    return { down: down, rate: finalRate };
}

function calcModelTermGrid(bounds) {
    var terms = [];
    for (var t = bounds.termMin; t <= bounds.termMax; t += CALC_MODEL_TERM_STEP) {
        terms.push(t);
    }
    return terms;
}

// Диапазон платежа ПРИ ТЕКУЩЕМ СРОКЕ (R16): минимум — при максимальном взносе,
// максимум — при минимальном; ставки берутся по соответствующим вилкам.
// Кламп ведущего платежа к этому диапазону гарантирует, что ведомый взнос
// останется в границах, а тройка — согласованной.
function calcModelPaymentBounds(engine, calc, bounds, term) {
    var low = calcModelPaymentAt(engine, calc, bounds.downMax, term).payment;
    var high = calcModelPaymentAt(engine, calc, bounds.downMin, term).payment;
    return { min: Math.min(low, high), max: Math.max(low, high), step: CALC_MODEL_PAYMENT_STEP };
}

// Минимально возможный платёж — взнос 70%, максимальный срок (R16). round1k
// как в движке (calc_engine.js:78), чтобы порог impossible совпадал с ним.
function calcModelMinPayment(engine, calc, bounds) {
    var res = calcModelPaymentAt(engine, calc, bounds.downMax, bounds.termMax);
    return { payment: engine.round1k(res.payment), rate: res.rate };
}

// ── Blocked-зоны ───────────────────────────────────────────────────────────
// Доли (0..1) шкалы, недостижимые ПРИ ТЕКУЩЕМ ПЛАТЕЖЕ: граница достижимости
// считается через calculateMaxLoan (взнос = price − maxLoan). Шкала срока —
// перебор сетки (13 точек максимум); шкала взноса — концы сетки сроков
// (максимальный срок даёт минимальный взнос и наоборот).

function calcModelBlockedTerm(engine, calc, bounds, payment, seedPct) {
    var span = bounds.termMax - bounds.termMin;
    if (span <= 0) return [];
    var terms = calcModelTermGrid(bounds), lo = null, hi = null;
    for (var i = 0; i < terms.length; i++) {
        var d = calcModelDownAt(engine, calc, payment, terms[i], seedPct).down;
        if (d >= bounds.downMin - CALC_MODEL_EPS && d <= bounds.downMax + CALC_MODEL_EPS) {
            if (lo === null) lo = terms[i];
            hi = terms[i];
        }
    }
    if (lo === null) return [{ from: 0, to: 1 }];
    var zones = [];
    if (lo > bounds.termMin) zones.push({ from: 0, to: (lo - bounds.termMin) / span });
    if (hi < bounds.termMax) zones.push({ from: (hi - bounds.termMin) / span, to: 1 });
    return zones;
}

function calcModelBlockedDown(engine, calc, bounds, payment, seedPct) {
    var span = bounds.downMax - bounds.downMin;
    if (span <= 0) return [];
    var atMaxTerm = calcModelDownAt(engine, calc, payment, bounds.termMax, seedPct).down;
    var atMinTerm = calcModelDownAt(engine, calc, payment, bounds.termMin, seedPct).down;
    var reachMin = Math.min(atMaxTerm, atMinTerm);
    var reachMax = Math.max(atMaxTerm, atMinTerm);
    var zones = [];
    if (reachMin > bounds.downMin + CALC_MODEL_EPS) {
        zones.push({ from: 0, to: calcModelClamp((reachMin - bounds.downMin) / span, 0, 1) });
    }
    if (reachMax < bounds.downMax - CALC_MODEL_EPS) {
        zones.push({ from: calcModelClamp((reachMax - bounds.downMin) / span, 0, 1), to: 1 });
    }
    return zones;
}

// ── Сборка состояния ───────────────────────────────────────────────────────

function calcModelState(calc, fields) {
    var state = {
        status: fields.status,
        mode: fields.mode,
        payment: fields.payment,
        down: fields.down,
        term: fields.term,
        rate: fields.rate ? fields.rate.val : null,
        rateText: fields.rate ? fields.rate.text : null,
        loan: Math.max(0, calc.price - fields.down),
        price: calc.price,
        downPct: calcModelPct(calc, fields.down),
        bounds: fields.boundsOut,
        blocked: fields.blocked,
        calc: calc
    };
    if (fields.minPayment !== undefined) state.minPayment = fields.minPayment;
    if (fields.extraDown !== undefined) state.extraDown = fields.extraDown;
    return state;
}

// Кредитное состояние по согласованной тройке: границы, blocked-зоны, minPayment.
// Притяжение ведомой ручки в состоянии не отмечается: прикосновением оно не
// считается (R16) — клиентский touched-маркер живёт в app.js и моделью не трогается.
function calcModelCreditState(engine, calc, bounds, term, down, payment, rate) {
    var seedPct = calcModelPct(calc, down);
    var min = calcModelMinPayment(engine, calc, bounds);
    return calcModelState(calc, {
        status: "ok", mode: "credit",
        payment: payment, down: down, term: term, rate: rate,
        minPayment: min.payment,
        boundsOut: {
            down: { min: bounds.downMin, max: bounds.downMax, step: CALC_MODEL_DOWN_STEP },
            term: { min: bounds.termMin, max: bounds.termMax, step: CALC_MODEL_TERM_STEP },
            payment: calcModelPaymentBounds(engine, calc, bounds, term)
        },
        blocked: {
            down: calcModelBlockedDown(engine, calc, bounds, payment, seedPct),
            term: calcModelBlockedTerm(engine, calc, bounds, payment, seedPct)
        }
    });
}

// impossible (R16): платёж ниже минимально возможного → единственная точка
// (взнос 70%, максимальный срок), обе шкалы блокированы целиком, minPayment —
// для обязательного пояснения UI (фрейм 10).
function calcModelImpossibleState(engine, calc, bounds) {
    var min = calcModelMinPayment(engine, calc, bounds);
    return calcModelState(calc, {
        status: "impossible", mode: "credit",
        payment: min.payment, down: bounds.downMax, term: bounds.termMax,
        rate: min.rate, minPayment: min.payment,
        boundsOut: {
            down: { min: bounds.downMin, max: bounds.downMax, step: CALC_MODEL_DOWN_STEP },
            term: { min: bounds.termMin, max: bounds.termMax, step: CALC_MODEL_TERM_STEP },
            payment: calcModelPaymentBounds(engine, calc, bounds, bounds.termMax)
        },
        blocked: { down: [{ from: 0, to: 1 }], term: [{ from: 0, to: 1 }] }
    });
}

// ── Комбо (R17) ────────────────────────────────────────────────────────────
// Взнос = оценка трейд-ина + доплата, ползунка взноса нет; свободен только
// срок. Заём фиксирован при фиксированной доплате.

function calcModelComboState(engine, calc, bounds, term, extraDown) {
    var down = calc.ti_valuation + extraDown;
    var extraMax = Math.max(0, calc.price - calc.ti_valuation);
    var boundsOut = {
        down: { min: down, max: down, step: CALC_MODEL_DOWN_STEP },
        term: { min: bounds.termMin, max: bounds.termMax, step: CALC_MODEL_TERM_STEP },
        payment: { min: 0, max: 0, step: CALC_MODEL_PAYMENT_STEP },
        extraDown: { min: 0, max: extraMax, step: CALC_MODEL_DOWN_STEP }
    };
    if (calc.price - down <= 0) {
        return calcModelState(calc, {
            status: "no_credit_needed", mode: "combo",
            payment: 0, down: down, term: term, rate: null,
            extraDown: extraDown, boundsOut: boundsOut,
            blocked: { down: [], term: [] }
        });
    }
    var res = calcModelPaymentAt(engine, calc, down, term);
    boundsOut.payment = {
        min: calcModelPaymentAt(engine, calc, down, bounds.termMax).payment,
        max: calcModelPaymentAt(engine, calc, down, bounds.termMin).payment,
        step: CALC_MODEL_PAYMENT_STEP
    };
    return calcModelState(calc, {
        status: "ok", mode: "combo",
        payment: res.payment, down: down, term: term, rate: res.rate,
        extraDown: extraDown, boundsOut: boundsOut,
        // Взнос фиксирован, а платёж следует за сроком: недостижимых участков
        // на шкалах нет.
        blocked: { down: [], term: [] }
    });
}

// Ближайший достижимый платёж на сетке сроков (R17): перебор сетки, ближайший
// по модулю; при равном отклонении выигрывает меньший срок (первый в сетке).
function calcModelComboNearestTerm(engine, calc, bounds, down, target) {
    var terms = calcModelTermGrid(bounds);
    var best = bounds.termMax, bestDiff = Infinity;
    for (var i = 0; i < terms.length; i++) {
        var pmt = calcModelPaymentAt(engine, calc, down, terms[i]).payment;
        var diff = Math.abs(pmt - target);
        if (diff < bestDiff - 1e-9) { bestDiff = diff; best = terms[i]; }
    }
    return best;
}

// ── Публичный API ──────────────────────────────────────────────────────────

// Состояние из блока calc ответа /cabinet/api/me (контракт U5).
// Статусы-деградации: engine_mismatch (R18, рантайм-страж версии),
// no_calc (снапшота нет / он невалиден — блок калькулятора скрыт).
// Единственная точка проверки версии движка: валидный движок либо готовое
// состояние engine_mismatch (рантайм-страж дрейфа, R18).
function calcModelEngineOrMismatch() {
    var engine = calcModelEngine();
    if (!engine || engine.CALC_ENGINE_VERSION !== EXPECTED_CALC_ENGINE_VERSION) {
        return { engine: null, mismatch: {
            status: "engine_mismatch",
            expected: EXPECTED_CALC_ENGINE_VERSION,
            actual: engine ? engine.CALC_ENGINE_VERSION : null
        } };
    }
    return { engine: engine, mismatch: null };
}

// opts (веб-вход, план 2026-09-30-001, U9; все флаги необязательны):
//   combo       — сценарий «кредит + трейд-ин»: режим комбо ВСЕГДА (один
//                 взнос, R41), в том числе пока оценки нет (зачёт 0 —
//                 кредит от доплаты, 8a/8c–8e); цена — combo_price снапшота
//                 (R20), нет её (снапшот до v3) — price;
//   clampTarget — желаемый платёж с лендинга ниже минимально возможного
//                 прижимается к минимуму без статуса impossible (R22).
function calcModelInitState(calc, opts) {
    var check = calcModelEngineOrMismatch();
    if (check.mismatch) return check.mismatch;
    var engine = check.engine;
    opts = opts || {};
    if (!calc || typeof calc !== "object") return { status: "no_calc" };
    var price = calcModelNum(calc.price);
    if (price === null || price <= 0) return { status: "no_calc" };
    var comboPrice = calcModelNum(calc.combo_price);
    var forceCombo = !!opts.combo;
    if (forceCombo && comboPrice !== null && comboPrice > 0) price = comboPrice;

    var normalized = {
        price: price,
        rates: Array.isArray(calc.rates) ? calc.rates : [],
        program: (calc.program && typeof calc.program === "object") ? calc.program : null,
        ti_valuation: calcModelNum(calc.ti_valuation),
        combo_price: comboPrice,
        target_payment: calcModelNum(calc.target_payment),
        item_id: calc.item_id !== undefined ? calc.item_id : null,
        item_name: calc.item_name !== undefined ? calc.item_name : null
    };
    var bounds = calcModelBounds(normalized);
    if (forceCombo && !(normalized.ti_valuation !== null && normalized.ti_valuation > 0)) {
        normalized.ti_valuation = 0;   // оценки нет — зачёт 0, взнос = доплата
    }

    // Комбо-режим: оценка трейд-ина есть и положительна (R17) либо сценарий
    // комбо задан явно (opts.combo). Доплата в
    // снапшот не входит (это поле анкеты combo_extra_down) — инициализация
    // идёт с нулём, сохранённый черновик app.js применяет через
    // touch(state, "extraDown", …).
    if (forceCombo || (normalized.ti_valuation !== null && normalized.ti_valuation > 0)) {
        var comboTerm = bounds.termMax;
        var comboDown = normalized.ti_valuation;
        if (normalized.price - comboDown > 0 &&
            normalized.target_payment !== null && normalized.target_payment > 0) {
            comboTerm = calcModelComboNearestTerm(engine, normalized, bounds,
                comboDown, normalized.target_payment);
        }
        return calcModelComboState(engine, normalized, bounds, comboTerm, 0);
    }

    // Кредит с желаемым платежом из чата — это прикосновение к платежу
    // (стандартная тройка служит стартовой точкой: черновая ставка берётся
    // при минимальном взносе и максимальном сроке, как в движке).
    var baseTerm = bounds.termMax;
    var baseDown = bounds.downMin;
    var basePmt = calcModelPaymentAt(engine, normalized, baseDown, baseTerm);
    var base = calcModelCreditState(engine, normalized, bounds, baseTerm, baseDown,
        basePmt.payment, basePmt.rate);
    if (normalized.target_payment !== null && normalized.target_payment > 0) {
        var target = normalized.target_payment;
        if (opts.clampTarget) {
            target = Math.max(target, calcModelMinPayment(engine, normalized, bounds).payment);
        }
        return calcModelTouch(base, "payment", target);
    }
    // Стандартная инициализация: минимальный взнос, максимальный срок,
    // платёж вычисляется (ведущая — взнос, округлён; платёж как есть).
    return base;
}

// Прикосновение: field ∈ {payment, term, down, extraDown}. Возвращает НОВОЕ
// состояние (входное не мутируется). Алгоритм — HTD «Алгоритм прикосновения»:
// кламп → округление ведущей внутри границ → двухпроходная ставка → ведомая
// как есть → blocked-зоны → притяжение ведомой → impossible.
function calcModelTouch(state, field, value) {
    if (!state || !state.calc) return state;
    if (state.status === "no_calc" || state.status === "engine_mismatch") return state;
    var check = calcModelEngineOrMismatch();
    if (check.mismatch) return check.mismatch;
    var engine = check.engine;
    var v = calcModelNum(value);
    if (v === null) return state;
    var calc = state.calc;
    var bounds = calcModelBounds(calc);
    if (state.mode === "combo") return calcModelComboTouch(engine, calc, bounds, state, field, v);
    return calcModelCreditTouch(engine, calc, bounds, state, field, v);
}

function calcModelCreditTouch(engine, calc, bounds, state, field, v) {
    var term = state.term, down = state.down, payment = state.payment;
    var seedPct = calcModelPct(calc, down);

    if (field === "payment") {
        // Платёж ниже минимально возможного → impossible (R16): порог считается
        // так же, как в движке (round1k при взносе 70% и максимальном сроке).
        var min = calcModelMinPayment(engine, calc, bounds);
        if (v < min.payment) return calcModelImpossibleState(engine, calc, bounds);
        var pb = calcModelPaymentBounds(engine, calc, bounds, term);
        payment = calcModelRoundInside(v, CALC_MODEL_PAYMENT_STEP, pb.min, pb.max);
        var byPmt = calcModelDownAt(engine, calc, payment, term, seedPct);
        down = calcModelClamp(byPmt.down, bounds.downMin, bounds.downMax);
        return calcModelCreditState(engine, calc, bounds, term, down, payment, byPmt.rate);
    }

    if (field === "term") {
        term = calcModelRoundInside(v, CALC_MODEL_TERM_STEP, bounds.termMin, bounds.termMax);
        var byTerm = calcModelDownAt(engine, calc, payment, term, seedPct);
        if (byTerm.down < bounds.downMin - CALC_MODEL_EPS ||
            byTerm.down > bounds.downMax + CALC_MODEL_EPS) {
            // Ведомая ручка ушла в blocked-зону: притягиваем к ближайшей
            // достижимой точке (граница взноса) БЕЗ зачёта прикосновения, а
            // платёж следует за ней — ведущий срок неприкосновенен.
            down = calcModelClamp(byTerm.down, bounds.downMin, bounds.downMax);
            var pulled = calcModelPaymentAt(engine, calc, down, term);
            return calcModelCreditState(engine, calc, bounds, term, down,
                pulled.payment, pulled.rate);
        }
        return calcModelCreditState(engine, calc, bounds, term, byTerm.down,
            payment, byTerm.rate);
    }

    if (field === "down") {
        down = calcModelRoundInside(v, CALC_MODEL_DOWN_STEP, bounds.downMin, bounds.downMax);
        var byDown = calcModelPaymentAt(engine, calc, down, term);
        return calcModelCreditState(engine, calc, bounds, term, down,
            byDown.payment, byDown.rate);
    }

    return state;  // extraDown вне комбо и неизвестные поля игнорируются
}

function calcModelComboTouch(engine, calc, bounds, state, field, v) {
    var extraDown = state.extraDown || 0;
    var term = state.term;

    if (field === "extraDown") {
        var extraMax = Math.max(0, calc.price - calc.ti_valuation);
        extraDown = calcModelClamp(v, 0, extraMax);
        return calcModelComboState(engine, calc, bounds, term, extraDown);
    }

    if (field === "term") {
        term = calcModelRoundInside(v, CALC_MODEL_TERM_STEP, bounds.termMin, bounds.termMax);
        return calcModelComboState(engine, calc, bounds, term, extraDown);
    }

    if (field === "payment") {
        if (state.status === "no_credit_needed") return state;
        var down = calc.ti_valuation + extraDown;
        term = calcModelComboNearestTerm(engine, calc, bounds, down, v);
        return calcModelComboState(engine, calc, bounds, term, extraDown);
    }

    return state;  // ползунка взноса в комбо нет — touch("down") игнорируется
}

// Минимально возможный платёж кредит-режима состояния (порог impossible) —
// app.js прижимает к нему платёж с лендинга при восстановлении черновика
// (R22). Комбо и деградации — null.
function calcModelMinPaymentOf(state) {
    if (!state || !state.calc || state.mode !== "credit") return null;
    var check = calcModelEngineOrMismatch();
    if (check.mismatch) return null;
    return calcModelMinPayment(check.engine, state.calc, calcModelBounds(state.calc)).payment;
}

var CalcModel = {
    EXPECTED_CALC_ENGINE_VERSION: EXPECTED_CALC_ENGINE_VERSION,
    initState: calcModelInitState,
    touch: calcModelTouch,
    minPayment: calcModelMinPaymentOf,
    setCalcEngine: calcModelSetEngine
};

if (typeof module !== "undefined") {
    module.exports = {
        EXPECTED_CALC_ENGINE_VERSION: EXPECTED_CALC_ENGINE_VERSION,
        initState: calcModelInitState,
        touch: calcModelTouch,
        minPayment: calcModelMinPaymentOf,
        setCalcEngine: calcModelSetEngine,
        CalcModel: CalcModel
    };
}
