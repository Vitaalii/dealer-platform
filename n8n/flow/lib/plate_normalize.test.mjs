// ============================================================
// Нормализация госномера в TOOL: AUTO.RU EVALUATION.
// Код узлов берётся ИЗ flow/TOOL_ AUTO.RU EVALUATION.json —
// у этого воркфлоу генератора нет, файл и есть источник правды
// (план 2026-09-08-001, КТР-5).
// Запуск: node flow/lib/plate_normalize.test.mjs
//
// Раздел ИНВАРИАНТЫ обязан быть зелёным и до, и после правки:
// это характеризация того, что ломать нельзя.
// ============================================================
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const WF = JSON.parse(readFileSync(new URL("../TOOL_ AUTO.RU EVALUATION.json", import.meta.url), "utf-8"));
const code = name => WF.nodes.find(n => n.name === name).parameters.jsCode;

// Узлы этого воркфлоу обращаются только к $input.item.json — мок минимальный.
function runNode(nodeName, json) {
    const $input = { item: { json }, first: () => ({ json }) };
    const $ = (name) => { throw new Error(`узел ${nodeName} обратился к ноде ${name} — мока нет`); };
    return new Function("$", "$input", code(nodeName))($, $input);
}

const norm = (plate, mileage = "100000") => runNode("Code in JavaScript1", { plate, mileage });

let passed = 0;
const T = (name, fn) => { fn(); passed++; };

// ---------- ИНВАРИАНТЫ: не ломать ----------

// Пять контрольных номеров из требования Т7 плана. Они снимались с живых
// исполнений TOOL: AUTO.RU EVALUATION и обязаны валидироваться всегда.
const CONTROL = ["Р491НК37", "в295мт37", "о427вн37", "Х318ТЕ750", "Р836ВЕ33"];

T("инвариант: пять контрольных номеров валидны", () => {
    for (const p of CONTROL)
        assert.equal(norm(p).is_valid, true, `${p} перестал валидироваться`);
});

T("инвариант: результат приводится к верхнему регистру", () => {
    assert.equal(norm("в295мт37").plate, "В295МТ37");
});

T("инвариант: пробелы и дефисы вычищаются", () => {
    assert.equal(norm("Р491НК 37").plate, "Р491НК37");
    assert.equal(norm("р491нк-37").plate, "Р491НК37");
});

T("инвариант: трёхзначный регион 750 остаётся валидным", () => {
    // хвост региона режется только с конца строки: иначе 'RU' вырезалось бы
    // из середины и Х318ТЕ750 потерял бы букву
    assert.equal(norm("Х318ТЕ750").plate, "Х318ТЕ750");
    assert.equal(norm("Х318ТЕ750").is_valid, true);
});

T("инвариант: пробег вместо номера отвергается", () => {
    assert.equal(norm("96000").is_valid, false);
});

T("инвариант: номер, слипшийся с пробегом, отвергается", () => {
    assert.equal(norm("О427ВН3396000").is_valid, false);
    assert.equal(norm("в295мт37 96000").is_valid, false);
});

T("инвариант: пробег пробрасывается дальше без изменений", () => {
    assert.equal(norm("Р491НК37", "96000").mileage, "96000");
});

T("инвариант: пустой вход не роняет узел", () => {
    assert.equal(norm("").is_valid, false);
    assert.equal(runNode("Code in JavaScript1", { mileage: "1" }).is_valid, false);
});

// ---------- U5: хвост RUS / РУС / RU ----------

T("U5: латинский хвост rus отрезается — кейс А, исполнение #3932", () => {
    // Буквы номера в этом исполнении КИРИЛЛИЧЕСКИЕ (С=U+0421, У=U+0423),
    // латинскими были только три символа хвоста
    const r = norm("Т582ХР 33 rus");
    assert.equal(r.plate, "Т582ХР33");
    assert.equal(r.is_valid, true);
});

T("U5: хвост без пробела, нижний регистр — исполнение #3937", () => {
    const r = norm("т582хр 33rus");
    assert.equal(r.plate, "Т582ХР33");
    assert.equal(r.is_valid, true);
});

T("U5: кириллический хвост РУС отрезается", () => {
    assert.equal(norm("Т582ХР33РУС").plate, "Т582ХР33");
    assert.equal(norm("Т582ХР33РУС").is_valid, true);
});

T("U5: короткий хвост RU отрезается", () => {
    assert.equal(norm("Т582ХР33RU").plate, "Т582ХР33");
    assert.equal(norm("Т582ХР33RU").is_valid, true);
});

T("U5: точка и запятая — тоже разделители", () => {
    assert.equal(norm("Р491.НК,37").plate, "Р491НК37");
});

// ---------- U6: код региона ----------

T("U6: несуществующий трёхзначный регион 211 отвергается — кейс Б, #3974", () => {
    // Сегодня этот номер валиден, уходит в auto.ru и возвращается NOT_FOUND.
    // После правки клиент получает конкретный вопрос про код региона.
    const r = norm("К647МУ211");
    assert.equal(r.is_valid, false);
    assert.equal(r.reason, "BAD_REGION");
});

T("U6: трёхзначные регионы с ведущей 1, 7, 9 валидны", () => {
    for (const p of ["Х318ТЕ177", "Х318ТЕ750", "Х318ТЕ799", "Х318ТЕ102", "Х318ТЕ977"])
        assert.equal(norm(p).is_valid, true, `${p} отвергнут`);
});

T("U6: трёхзначный регион с иной ведущей цифрой отвергается", () => {
    for (const p of ["Х318ТЕ250", "Х318ТЕ311", "Х318ТЕ450"]) {
        assert.equal(norm(p).is_valid, false, `${p} принят`);
        assert.equal(norm(p).reason, "BAD_REGION");
    }
});

T("U6: границы двузначного диапазона", () => {
    assert.equal(norm("А123ВС01").is_valid, true);
    assert.equal(norm("А123ВС99").is_valid, true);
    assert.equal(norm("А123ВС00").is_valid, false);
    assert.equal(norm("А123ВС00").reason, "BAD_REGION");
});

T("U6: отсутствие региона отличимо от прочего мусора", () => {
    assert.equal(norm("о427вн").is_valid, false);
    assert.equal(norm("о427вн").reason, "NO_REGION");
    assert.equal(norm("96000").reason, "NOT_A_PLATE");
    assert.equal(norm("О427ВН3396000").reason, "NOT_A_PLATE");
});

// ---------- U9: гомоглифы и защита VIN ----------

T("U9: латинская запись госномера приводится к кириллице", () => {
    // Рабочее допущение (решение владельца 2026-09-08): auto.ru ищет
    // по кириллической записи. Опровергается латинским номером,
    // который сервис нашёл бы как есть.
    const r = norm("P491HK37");
    assert.equal(r.plate, "Р491НК37");
    assert.equal(r.is_valid, true);
    // проверяем кодовые точки, а не начертание: Р=U+0420, Н=U+041D, К=U+041A
    assert.deepEqual([...r.plate].map(c => c.codePointAt(0)),
        [0x420, 0x34, 0x39, 0x31, 0x41D, 0x41A, 0x33, 0x37]);
});

T("U9: смешанная запись приводится к кириллице целиком", () => {
    // В — кириллическая, P и X — латинские
    const r = norm("Р491HK37");
    assert.equal(r.plate, "Р491НК37");
    assert.equal(r.is_valid, true);
    assert.ok(![...r.plate].some(c => c.codePointAt(0) < 0x400 && /[A-Z]/.test(c)),
        "в результате осталась латиница");
});

T("U9: латинский хвост при латинском теле — и хвост, и тело обработаны", () => {
    const r = norm("T582XP 33 RUS");
    assert.equal(r.plate, "Т582ХР33");
    assert.equal(r.is_valid, true);
});

T("U9: VIN проходит нормализацию НЕИЗМЕНЁННЫМ", () => {
    // Поле auto.ru называется vin_or_lp и принимает оба вида идентификатора.
    // VIN пишется ТОЛЬКО латиницей — слепая конвертация его уничтожила бы.
    for (const vin of ["XTA219990Y2755454", "X9L21230080123456", "WVWZZZ1JZXW000001"]) {
        const r = norm(vin);
        assert.equal(r.plate, vin, `VIN ${vin} изменён нормализацией`);
        assert.equal(r.reason, "VIN_UNSUPPORTED");
    }
});

T("U9: VIN с хвостом-омонимом не режется как регион", () => {
    // 17 знаков, последние два — RU: проверка VIN идёт ДО отрезания хвоста
    const vin = "WVWZZZ1JZXW0000RU";
    assert.equal(norm(vin).plate, vin);
});

T("U9: кириллица без латиницы не задета конвертацией", () => {
    for (const p of CONTROL) assert.equal(norm(p).plate, norm(p).plate.toUpperCase());
    assert.equal(norm("Р836ВЕ33").plate, "Р836ВЕ33");
});

// ---------- U7: различимые статусы на выходе инструмента ----------

const terminal = json => runNode("Code in JavaScript", json);

T("U7: отсутствие региона даёт собственный статус и разобранный номер", () => {
    const out = terminal(norm("о427вн"));
    assert.equal(out.status, "NO_REGION");
    assert.equal(out.plate_seen, "О427ВН");
    assert.ok(/регион/i.test(out.message), "в message нет указания на код региона");
});

T("U7: неверный регион и не-номер дают INVALID_DATA с разными message", () => {
    const bad = terminal(norm("К647МУ211"));
    const junk = terminal(norm("96000"));
    assert.equal(bad.status, "INVALID_DATA");
    assert.equal(junk.status, "INVALID_DATA");
    assert.notEqual(bad.message, junk.message, "причины отказа неразличимы по message");
    assert.ok(/регион/i.test(bad.message), "message про неверный регион не называет регион");
});

T("U7: VIN отдаётся как INVALID_DATA — новых статусов наружу не добавилось", () => {
    assert.equal(terminal(norm("XTA219990Y2755454")).status, "INVALID_DATA");
});

T("U7: ветка успеха терминального узла не тронута", () => {
    // is_valid=true в этот узел не приходит вовсе (ветвление на If2), но и
    // прежний перехват по is_valid===false обязан продолжать работать
    const out = terminal({ is_valid: false, reason: "NOT_A_PLATE", plate: "96000" });
    assert.equal(out.status, "INVALID_DATA");
    assert.ok(typeof out.message === "string" && out.message.length > 0);
});

console.log(`OK: нормализация госномера, ${passed} сценариев пройдено`);
