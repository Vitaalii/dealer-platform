#!/usr/bin/env python3
"""U2: генератор воркфлоу Calc Engine и перепрошивка Car Search / Combo Calculator.

Единый источник математики — calc_engine.js. После любой правки математики:
  1) node calc_engine.golden.test.mjs   (характеризация + перегенерация golden_cases.json)
  2) python3 build_calc_engine_workflow.py --browser-out <каталог widget-custom/cabinet-app бот-репо>
  3) ручной импорт изменённых JSON в n8n (см. вывод скрипта)

U3: --browser-out дополнительно получает браузерную копию движка (calc_engine.browser.js —
lib_code без n8n-диспетчера) и golden_cases.json в обёртке {"calc_engine_version", "cases"}.

Скрипт идемпотентен: повторный запуск даёт тот же результат.
"""
import argparse
import json
import re
import uuid
from pathlib import Path

parser = argparse.ArgumentParser(
    description="Генерация воркфлоу Calc Engine + браузерная сборка движка (U3).")
parser.add_argument(
    "--browser-out", required=True, metavar="DIR",
    help="каталог статики бота для calc_engine.browser.js и golden_cases.json "
         "(обязателен: дефолтный путь зашивал бы раскладку каталогов конкретной машины)")
args = parser.parse_args()
BROWSER_OUT = Path(args.browser_out)

LIB = Path(__file__).resolve().parent
FLOW = LIB.parent

CALC_ENGINE_NAME = "Tool: Calc Engine"
# Боевые id саб-воркфлоу (план 2026-10-09-001, KTD4): генерация = бой без ручной
# перепривязки. cachedResultName — боевые имена: по ним build_test_contour.py
# опознаёт ссылки и подменяет их [TEST]-копиями.
CALC_ENGINE_ID = "IzHcJxOxP9SI3LNv"
AUTO_RU_NAME = "TOOL: AUTO.RU EVALUATION"
AUTO_RU_ID = "XCPe53sUwk8i9e3T"
# workflowInputs, которые редактор n8n проставляет ноде Execute Workflow 1.3
# без входных полей (как на бою)
EMPTY_WORKFLOW_INPUTS = {"mappingMode": "defineBelow", "value": {}, "matchingColumns": [], "schema": [],
                         "attemptToConvertTypes": False, "convertFieldsToString": True}

lib_code = (LIB / "calc_engine.js").read_text(encoding="utf-8")
golden = json.loads((LIB / "golden_cases.json").read_text(encoding="utf-8"))

_version_match = re.search(r'CALC_ENGINE_VERSION\s*=\s*"([^"]+)"', lib_code)
if not _version_match:
    raise SystemExit("calc_engine.js: маркер CALC_ENGINE_VERSION не найден — браузерная сборка невозможна")
ENGINE_VERSION = _version_match.group(1)

dispatcher_code = lib_code + """

// --- n8n-диспетчер ---
return $input.all().map(i => ({ json: runCalc(i.json.calc_request || i.json) }));
"""


def node(name, ntype, type_version, params, pos):
    return {
        "parameters": params,
        "type": ntype,
        "typeVersion": type_version,
        "position": pos,
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"calc-engine/{name}")),
        "name": name,
    }


def execute_workflow_node(name, pos):
    return node(name, "n8n-nodes-base.executeWorkflow", 1.3, {
        "workflowId": {
            "__rl": True,
            "value": CALC_ENGINE_ID,
            "mode": "list",
            "cachedResultName": CALC_ENGINE_NAME,
        },
        "workflowInputs": dict(EMPTY_WORKFLOW_INPUTS),
        "options": {"waitForSubWorkflow": True},
    }, pos)


def wf_shell(name, nodes, connections):
    return {
        "name": name,
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
        "pinData": {},
        "meta": {"instanceId": "65c0178e84d5c3db112ec6489beae5f53a11ae74e53d6537d4d9a5cd140a483b"},
    }


def link(conns, src, dst, output_index=0):
    conns.setdefault(src, {}).setdefault("main", [])
    while len(conns[src]["main"]) <= output_index:
        conns[src]["main"].append([])
    conns[src]["main"][output_index].append({"node": dst, "type": "main", "index": 0})


# ---------- 1. Tool: Calc Engine ----------
ce_nodes = [
    node("Execute Workflow Trigger", "n8n-nodes-base.executeWorkflowTrigger", 1.1,
         {"inputSource": "passthrough"}, [0, 0]),
    node("Calc Dispatcher", "n8n-nodes-base.code", 2, {"jsCode": dispatcher_code}, [220, 0]),
]
ce_conns = {}
link(ce_conns, "Execute Workflow Trigger", "Calc Dispatcher")
(FLOW / "Tool_ Calc Engine.json").write_text(
    json.dumps(wf_shell(CALC_ENGINE_NAME, ce_nodes, ce_conns), ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8")

# ---------- 2. Test: Calc Engine (золотые кейсы) ----------
emit_code = "const cases = " + json.dumps([g["request"] for g in golden], ensure_ascii=False) + ";\n" + \
    "return cases.map(c => ({ json: c }));\n"

assert_code = "const golden = " + json.dumps([g["expected"] for g in golden], ensure_ascii=False) + ";\n" + """
function sortKeys(o) {
    if (Array.isArray(o)) return o.map(sortKeys);
    if (o && typeof o === 'object') {
        const out = {};
        for (const k of Object.keys(o).sort()) out[k] = sortKeys(o[k]);
        return out;
    }
    return o;
}
const norm = o => JSON.stringify(sortKeys(o));
const actual = $input.all().map(i => i.json);
if (actual.length !== golden.length) {
    throw new Error(`CALC ENGINE GOLDEN FAIL: получено ${actual.length} ответов, ожидалось ${golden.length}`);
}
const failures = [];
golden.forEach((exp, i) => {
    if (norm(actual[i]) !== norm(exp)) failures.push({ case: i, expected: exp, actual: actual[i] });
});
if (failures.length) {
    throw new Error(`CALC ENGINE GOLDEN FAIL (${failures.length} расхождений). Первое: ` +
        JSON.stringify(failures[0]).slice(0, 1500));
}
return [{ json: { status: "PASS", cases: golden.length } }];
"""

test_nodes = [
    node("When clicking Test", "n8n-nodes-base.manualTrigger", 1, {}, [0, 0]),
    node("Emit Golden Cases", "n8n-nodes-base.code", 2, {"jsCode": emit_code}, [220, 0]),
    execute_workflow_node("Calc Engine", [440, 0]),
    node("Assert Golden", "n8n-nodes-base.code", 2, {"jsCode": assert_code}, [660, 0]),
]
test_conns = {}
link(test_conns, "When clicking Test", "Emit Golden Cases")
link(test_conns, "Emit Golden Cases", "Calc Engine")
link(test_conns, "Calc Engine", "Assert Golden")
(FLOW / "Test_ Calc Engine.json").write_text(
    json.dumps(wf_shell("Test: Calc Engine", test_nodes, test_conns), ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8")

# ---------- 3. Перепрошивка Tool - Car Search ----------
def rewire(fname, old_node_name, new_node_name, prepare_js, merge_js, drop_orphans=()):
    path = FLOW / fname
    wf = json.loads(path.read_text(encoding="utf-8"))
    names = {n["name"] for n in wf["nodes"]}

    # идемпотентность: уже перепрошит
    if new_node_name in names:
        target = next(n for n in wf["nodes"] if n["name"] == new_node_name)
        target["parameters"]["jsCode"] = prepare_js
        merge = next(n for n in wf["nodes"] if n["name"] == "Merge Calc")
        merge["parameters"]["jsCode"] = merge_js
        path.write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{fname}: обновлены jsCode (уже перепрошит ранее)")
        return

    # 3a. защита: orphan-ноды не должны иметь живых связей
    conns = wf.get("connections", {})
    linked = set()
    for src, types in conns.items():
        if any(g for groups in types.values() for g in groups for g in (g or [])):
            linked.add(src)
        for groups in types.values():
            for group in groups:
                for tgt in group or []:
                    linked.add(tgt.get("node"))
    for orphan in drop_orphans:
        assert orphan not in linked, f"{orphan} имеет живые связи — не orphan!"
        wf["nodes"] = [n for n in wf["nodes"] if n["name"] != orphan]
        conns.pop(orphan, None)

    # 3b. переименование старой Code-ноды и замена кода
    target = next(n for n in wf["nodes"] if n["name"] == old_node_name)
    x, y = target["position"]
    target["name"] = new_node_name
    target["parameters"]["jsCode"] = prepare_js
    for src, types in list(conns.items()):
        for groups in types.values():
            for group in groups:
                for tgt in group or []:
                    if tgt.get("node") == old_node_name:
                        tgt["node"] = new_node_name
    if old_node_name in conns:
        conns[new_node_name] = conns.pop(old_node_name)

    # 3c. новые ноды Calc Engine + Merge Calc
    wf["nodes"].append(execute_workflow_node("Calc Engine", [x + 220, y]))
    wf["nodes"].append(node("Merge Calc", "n8n-nodes-base.code", 2, {"jsCode": merge_js}, [x + 440, y]))
    link(conns, new_node_name, "Calc Engine")
    link(conns, "Calc Engine", "Merge Calc")

    path.write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{fname}: {old_node_name} -> {new_node_name} + Calc Engine + Merge Calc"
          + (f"; удалены orphan'ы {list(drop_orphans)}" if drop_orphans else ""))


rewire("Tool - Car Search.json", "Code in JavaScript1", "Prepare Data",
       (LIB / "car_search_prepare.js").read_text(encoding="utf-8"),
       (LIB / "car_search_merge.js").read_text(encoding="utf-8"))



def execute_once(fname, node_name):
    """[STOCK] Нода NocoDB выполняется один раз на исполнение, а не на каждый
    входной item. Без этого Get Stock в Car Search получал по item на строку
    каталога и возвращал сток N раз (MONJARO дилера 02: 4 строки каталога ×
    6 машин = 24, решение владельца 2026-10-05). Как у Get Program / Get Rates."""
    path = FLOW / fname
    wf = json.loads(path.read_text(encoding="utf-8"))
    target = next(n for n in wf["nodes"] if n["name"] == node_name)
    if target.get("executeOnce") is not True:
        target["executeOnce"] = True
        path.write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{fname}: {node_name}.executeOnce = true")


execute_once("Tool - Car Search.json", "Get Stock")


# ---------- N4 и ссылки на саб-воркфлоу (план 2026-10-09-001, U3) ----------
# Правки, которые с 2026-09-30 вносились в бой набором apply_workflow_patch.py,
# — здесь, а не руками в базовом JSON: повторный запуск даёт тот же результат.
OLD_WHERE = "=(Model_Pattern,like,%{{ $('Get Catalog').first().json.Id }}%)"
NEW_WHERE = "=(Model_Pattern,like,%{{ $('Get Catalog').first().json['Model id'] }}%)"


def edit_node(fname, node_name, fn):
    path = FLOW / fname
    wf = json.loads(path.read_text(encoding="utf-8"))
    found = [n for n in wf["nodes"] if n["name"] == node_name]
    assert len(found) == 1, f"{fname}: нода «{node_name}» найдена {len(found)} раз"
    before = json.dumps(found[0], sort_keys=True)
    fn(found[0])
    if json.dumps(found[0], sort_keys=True) != before:
        path.write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{fname}: {node_name} обновлена")


def program_by_model_id(n):
    """[N4] Get Program ищет программу по точному Model id, а не по Id строки каталога."""
    opts = n["parameters"].setdefault("options", {})
    assert opts.get("where") in (OLD_WHERE, NEW_WHERE), f"Get Program: неожиданный Where {opts.get('where')!r}"
    opts["where"] = NEW_WHERE


def return_all(n):
    """[N4] Get Rates возвращает все ставки, а не первую страницу."""
    n["parameters"]["returnAll"] = True


def sub_workflow(wid, name):
    def fn(n):
        p = n["parameters"]
        p["workflowId"].update({"value": wid, "cachedResultName": name, "cachedResultUrl": f"/workflow/{wid}"})
        p.setdefault("workflowInputs", dict(EMPTY_WORKFLOW_INPUTS))
    return fn


for fname in ("Tool - Car Search.json",):
    edit_node(fname, "Get Program", program_by_model_id)
    edit_node(fname, "Get Rates", return_all)
    edit_node(fname, "Calc Engine", sub_workflow(CALC_ENGINE_ID, CALC_ENGINE_NAME))

rewire("Tool_ Combo Calculator.json", "Code in JavaScript1", "Prepare Combo",
       (LIB / "combo_prepare.js").read_text(encoding="utf-8"),
       (LIB / "combo_merge.js").read_text(encoding="utf-8"),
       drop_orphans=("Code in JavaScript", "HTTP Request"))
for fname in ("Tool_ Combo Calculator.json",):
    edit_node(fname, "Get Program", program_by_model_id)
    edit_node(fname, "Get Rates", return_all)
    edit_node(fname, "Calc Engine", sub_workflow(CALC_ENGINE_ID, CALC_ENGINE_NAME))
# Оценка трейд-ина в комбо — боевой AUTO.RU EVALUATION (в базовом JSON была
# майская копия ZYVac3Twp_ugyP3Jngwhj без found_info)
edit_node("Tool_ Combo Calculator.json", "Execute Workflow", sub_workflow(AUTO_RU_ID, AUTO_RU_NAME))

# ---------- 4. Браузерная сборка (U3, R12) ----------
# lib_code — ровно canon без n8n-диспетчера; golden-эталоны в обёртке с версией движка,
# чтобы стражи (run_golden.mjs, test_js_suite.py) ловили дрейф версий.
BROWSER_OUT.mkdir(parents=True, exist_ok=True)
(BROWSER_OUT / "calc_engine.browser.js").write_text(lib_code, encoding="utf-8")
(BROWSER_OUT / "golden_cases.json").write_text(
    json.dumps({"calc_engine_version": ENGINE_VERSION, "cases": golden},
               ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8")
print(f"Браузерная сборка v{ENGINE_VERSION}: "
      f"{BROWSER_OUT / 'calc_engine.browser.js'} + golden_cases.json ({len(golden)} кейсов)")

print("\nГотово. Ссылки на саб-воркфлоу проставлены боевыми id — ручная перепривязка не нужна.")
print("Сверка с боем (только чтение): python3 flow/cabinet-web-entry/api/check_sources_vs_live.py")
print("Test: Calc Engine вручную в n8n — ожидается PASS с", len(golden), "кейсами.")
