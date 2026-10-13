// U3 (R12, R21): golden-прогон математики на браузерной копии движка.
// Запуск: node tests/js/run_golden.mjs (из корня бот-репо; pytest-обёртка — tests/test_js_suite.py).
// Голый node без npm-зависимостей — осознанно (KTD «Node-прогон без стека»).
// Выход ненулевой при любом расхождении; финальная строка — число прогнанных кейсов.
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import assert from "node:assert/strict";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const appDir = path.join(here, "..", "..", "widget-custom", "cabinet-app");

const engine = require(path.join(appDir, "calc_engine.browser.js"));

// Страж 1: версия движка объявлена и непуста.
if (typeof engine.CALC_ENGINE_VERSION !== "string" || engine.CALC_ENGINE_VERSION.trim() === "") {
    console.error("FAIL: CALC_ENGINE_VERSION в calc_engine.browser.js пуст или отсутствует");
    process.exit(1);
}

// Страж 2: n8n-диспетчер не просочился в браузерную копию.
const browserSource = readFileSync(path.join(appDir, "calc_engine.browser.js"), "utf-8");
if (browserSource.includes("$input.all()")) {
    console.error("FAIL: в calc_engine.browser.js просочился n8n-диспетчер ($input.all())");
    process.exit(1);
}

// Страж 3: golden_cases.json собран для этой же версии движка.
const goldenWrapper = JSON.parse(readFileSync(path.join(appDir, "golden_cases.json"), "utf-8"));
if (goldenWrapper.calc_engine_version !== engine.CALC_ENGINE_VERSION) {
    console.error(`FAIL: дрейф версий — golden_cases.json: "${goldenWrapper.calc_engine_version}", ` +
        `движок: "${engine.CALC_ENGINE_VERSION}". Запусти build_calc_engine_workflow.py.`);
    process.exit(1);
}
const cases = goldenWrapper.cases;
if (!Array.isArray(cases) || cases.length === 0) {
    console.error("FAIL: golden_cases.json не содержит кейсов (ожидался объект {calc_engine_version, cases})");
    process.exit(1);
}

// Golden-прогон: каждый кейс через runCalc, сравнение с эталоном.
let failures = 0;
cases.forEach((c, i) => {
    let actual;
    try {
        actual = engine.runCalc(c.request);
        assert.deepStrictEqual(actual, c.expected);
    } catch (e) {
        failures += 1;
        console.error(`FAIL кейс ${i}: ${e.message.slice(0, 1500)}`);
    }
});

if (failures > 0) {
    console.error(`GOLDEN FAIL: расхождений ${failures} из ${cases.length}`);
    process.exit(1);
}
console.log(`GOLDEN PASS: прогнано кейсов — ${cases.length}`);
