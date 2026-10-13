"""Колонка активности на бою: проверка исполнений (только чтение n8n API).

Агент (AI Agent v3.0): тип хода (INIT / чат), заполнен ли qualification_context.dealer_id,
пришёл ли data_routing.catalog_active_field.
Car Search / Combo: дилер, модель, data_routing.catalog_active_field (что прислал бот),
summary.catalog_active_field (что применено), комплектации; флаг, если у колонки
Active_1 в списке оказались EX5 / EX5 ГИБРИД / NORDCROSS 001 (Model id 19, 20, 51).
--wait N — ждать первого исполнения Car Search / Combo (опрос раз в 60 с).

Запуск: python3 check_active_field_exec.py --since 2026-10-06T08:14:13Z [--wait 10800]
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apply_workflow_patch import call  # noqa: E402

AGENT, CAR, COMBO = "fQAS3wmAjVqrKATM", "mZqzmEnnQiDbvtir", "GD3ZCj8GiVyB7YBw"
HIDDEN_FOR_02 = {"19", "20", "51"}


def items(run, node):
    try:
        return [i["json"] for r in run[node] for i in r["data"]["main"][0]]
    except (KeyError, IndexError, TypeError):
        return []


def obj(v):
    if isinstance(v, str):
        try:
            return json.loads(v.strip().lstrip("="))
        except ValueError:
            return {}
    return v or {}


def agent_line(e):
    run = call("GET", f"/executions/{e['id']}?includeData=true")[1]["data"]["resultData"]["runData"]
    body = (items(run, "Webhook") or [{}])[0].get("body") or {}
    qc, dr = obj(body.get("qualification_context")), obj(body.get("data_routing"))
    kind = "INIT" if body.get("message") == "INIT_AI_HANDOFF" else ("REMINDER" if body.get("message") == "__SYSTEM_REMINDER__" else "чат")
    return (f"агент {e['id']} {e['startedAt'][11:19]} {kind:8} | dealer_id {qc.get('dealer_id')!r:22} "
            f"| catalog {dr.get('catalog_table_id')} | catalog_active_field {dr.get('catalog_active_field')!r}")


def tool_line(e, label, prep_node):
    run = call("GET", f"/executions/{e['id']}?includeData=true")[1]["data"]["resultData"]["runData"]
    pi = (items(run, "Parse Input") or [{}])[0]
    qc, dr = obj(pi.get("qualification_context")), obj(pi.get("data_routing"))
    resp = ((items(run, prep_node) or [{}])[0].get("response")) or {}
    applied = (resp.get("summary") or {}).get("catalog_active_field")
    names = [c.get("name") for c in resp.get("complectations") or []]
    flag = ""
    if applied == "Active_1" and str(qc.get("model_id")) in HIDDEN_FOR_02 and names:
        flag = "  ← ОШИБКА: скрытая модель видна у Active_1"
    return (f"{label:10} {e['id']} {e['startedAt'][11:19]} {e['status']} | dealer_id {qc.get('dealer_id')!r} model_id {qc.get('model_id')} "
            f"| прислано {dr.get('catalog_active_field')!r} → применено {applied!r} | {names or resp.get('result_text') or (resp.get('combo_calculation') or {}).get('message')}{flag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--wait", type=int, default=0)
    args = ap.parse_args()
    deadline = time.time() + args.wait
    while True:
        tools = []
        for wid, label, node in ((CAR, "Car Search", "Prepare Data"), (COMBO, "Combo", "Prepare Combo")):
            tools += [(e, label, node) for e in call("GET", f"/executions?workflowId={wid}&limit=50")[1]["data"]
                      if e["startedAt"] >= args.since]
        if tools or time.time() >= deadline:
            break
        time.sleep(60)
    agents = [e for e in call("GET", f"/executions?workflowId={AGENT}&limit=50")[1]["data"] if e["startedAt"] >= args.since]
    for e in sorted(agents, key=lambda e: int(e["id"])):
        print(agent_line(e))
    for e, label, node in sorted(tools, key=lambda t: int(t[0]["id"])):
        print(tool_line(e, label, node))
    if not tools:
        print(f"исполнений Car Search / Combo после {args.since} нет")


if __name__ == "__main__":
    main()
