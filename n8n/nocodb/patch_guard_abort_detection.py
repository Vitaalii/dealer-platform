#!/usr/bin/env python3
# СТАТУС: ПРИМЕНЕНО И ПЕРЕНЕСЕНО В ГЕНЕРАТОР (2026-08-30).
# Содержимое этих узлов теперь печатает сам nocodb/build_v3_workflow.py,
# поэтому запускать скрипт повторно НЕ НУЖНО — в diff-режиме он честно
# показывает «нечего менять». Оставлен как след двух правок и как
# описание того, чем они проверялись; удалять не надо, но и применять
# больше незачем: единственный источник истины — генератор.
"""Tool Guard / Post Guard: оборванная генерация не уходит клиенту (ТЗ 2026-08-19).

Дефект (исполнение n8n 3272): модель оборвалась на 20 токенах,
`finish_reason = "error"`, текст пришёл без второй половины — без ссылки на
кабинет. Конвейер обрыва не заметил и отправил обрубок как готовый ответ.

=== ПОЧЕМУ НЕ ПО finish_reason ===============================================
Прямой признак лежит в выводе САБ-НОДЫ `Agent LM`
(`response.generations[0][0].generationInfo.finish_reason`). Из code-ноды он
недостижим: саб-нода отдаёт данные по связи ai_languageModel, а весь интерфейс
`$()` ходит только в `main`.

Проверено на живом инстансе 2026-08-19 пробником с боевой топологией
(agent 3.1 + lmChatOpenRouter), перебором 14 способов:
    $('LM').all() / .all(0,0) / .first()   -> Error: No data found from `main` input
    $items('LM') / $items('LM',0,0)        -> то же
    $node['LM'] / $node['LM'].json         -> то же
    $('LM').item                           -> Invalid expression
    $('LM').isExecuted -> true (булево)    $('LM').params -> статика
Ноды-обёртки не существует: у саб-ноды нет main-выхода по определению.

=== ЧТО РАБОТАЕТ ВМЕСТО ======================================================
Решение принимается по ФОРМЕ ответа. Оборванный текст кончается посреди фразы;
корректный по правилу G-02_VOICE_V2 п.6 заканчивается ровно одним вопросом, то
есть завершающим знаком. На всех 13 исполнениях 2026-08-19 правило разделило
выборку без ошибок: единственный обрыв пойман, 12 штатных ответов не задеты.

Прямой признак из конструкции НЕ выброшен: lmVerdict() остаётся и, если доступ
к саб-ноде когда-нибудь появится, начнёт срабатывать сам. Сегодня он всегда
пуст — это видно в диагностике `lm_signal`.

Точность эвристики калибруется постфактум: nocodb/guard_report.py сверяет её
вердикт с настоящим finish_reason по данным исполнений.

Что меняется:
  * Tool Guard  — обрыв (по форме либо по finish_reason) уводит ход на повтор;
  * Post Guard  — если оборвалась и вторая попытка, вместо обрубка идёт
                  служебная фраза;
  * обе ноды кладут диагностику в вывод.

Правится канонический flow/AI Agent v3.0.json; [TEST]-копия перегенерируется
через build_test_contour.py. Скрипт идемпотентен.

Запуск:
  python3 patch_guard_abort_detection.py           # показать, что изменится
  python3 patch_guard_abort_detection.py --apply
"""

raise SystemExit(
    "СКРИПТ ОБЕЗВРЕЖЕН — ЗАПУСКАТЬ НЕЛЬЗЯ.\n"
    "\n"
    "Правки Tool Guard и Post Guard применены 2026-08-19 и перенесены в генератор\n"
    "2026-08-30 (коммит a1bd6e7). С тех пор оба узла печатает генератор, а не этот\n"
    "файл. Скрипт остался заряженным: он перезаписывает flow/AI Agent v3.0.json\n"
    "телами узлов, вшитыми ниже, и молча откатывает всё, что вошло в генератор\n"
    "позже.\n"
    "\n"
    "Конкретно на 2026-09-07 здесь лежит снятая с эксплуатации служебная фраза\n"
    "«Извините, произошла небольшая техническая заминка…» — запуск вернул бы её\n"
    "клиенту, отменив решение владельца от 2026-09-07 (коммит 2a47052).\n"
    "\n"
    "ГДЕ ПРАВИТЬ НА САМОМ ДЕЛЕ: nocodb/build_v3_workflow.py, константы\n"
    "TOOL_GUARD_JS и POST_GUARD_JS. После правки — перегенерация:\n"
    "    python3 nocodb/build_v3_workflow.py && python3 nocodb/build_test_contour.py\n"
    "\n"
    "Файл сохранён ради докстроки выше: в ней разобрано, почему finish_reason\n"
    "саб-ноды LM недостижим из code-ноды (14 проверенных способов доступа,\n"
    "замер 2026-08-19). Удалять и переименовывать нельзя — на этот путь\n"
    "ссылаются docs/handoff/2026-08-30-combo-tool-name-and-generator.md и\n"
    "docs/SESSION_HANDOFF.md.\n"
    "\n"
    "Снять запрет — только осознанно и с ревизией вшитых тел узлов на актуальность."
)

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FLOW = HERE.parent / "flow" / "AI Agent v3.0.json"
MARK = "looksTruncated"          # признак применённой правки

# --------------------------------------------------------------------------
# Общий помощник. Дублируется в обеих нодах: code-ноды n8n изолированы.
# --------------------------------------------------------------------------
HELPER = r"""
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
"""

TOOL_GUARD = HELPER + r"""
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

POST_GUARD = HELPER + r"""
const out = $input.first().json;
let text = String(out.output ?? out.text ?? "").trim()
    .replace(/<function_calls>[\s\S]*?<\/function_calls>/g, "").trim();

// Повтор — ПОСЛЕДНЯЯ попытка: пути назад в Tool Guard нет, дальше только
// Final Response, поэтому зациклиться невозможно по построению графа.
// Если оборвалась и она, отдавать обрубок нельзя — он выглядит законченным
// ответом. Отдаём ту же служебную фразу, которой Final Response уже закрывает
// пустой текст: она согласована и ничего не выдумывает.
const RETRY_FALLBACK = "Извините, произошла небольшая техническая заминка. Уточните, пожалуйста, ваш вопрос — я на связи.";

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

# .strip() без завершающего перевода строки — ровно так же собирает узлы
# генератор. Иначе скрипт вечно показывал бы правку в один байт и приучал
# бы её игнорировать.
NEW_CODE = {"Tool Guard": TOOL_GUARD.strip(),
            "Post Guard": POST_GUARD.strip()}


def main():
    apply_mode = "--apply" in sys.argv
    wf = json.loads(FLOW.read_text(encoding="utf-8"))
    by_name = {n["name"]: n for n in wf["nodes"]}

    missing = [n for n in NEW_CODE if n not in by_name]
    if missing:
        sys.exit(f"ОСТАНОВ: в воркфлоу нет нод {missing}")

    changes = []
    for name, code in NEW_CODE.items():
        cur = (by_name[name].get("parameters") or {}).get("jsCode") or ""
        if cur == code:
            print(f"  {name}: правка уже на месте")
            continue
        if MARK in cur:
            print(f"  {name}: правка уже применялась, текст отличается — перезапишу")
        changes.append((name, len(cur), len(code)))

    if not changes:
        print("\nнечего менять")
        return

    print(f"\nнод к правке: {len(changes)}")
    for name, was, now in changes:
        print(f"  {name:12s} jsCode {was} -> {now} симв.")

    if not apply_mode:
        print("\n(diff-режим: ничего не записано; --apply)")
        return

    for name, _, _ in changes:
        by_name[name]["parameters"]["jsCode"] = NEW_CODE[name]
    FLOW.write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nзаписан {FLOW}")
    print("Дальше: python3 build_test_contour.py — перегенерировать [TEST]-копию.")


if __name__ == "__main__":
    main()
