"""Один живой вызов Combo Calculator через временную обёртку (ПЛАТНО: auto.ru).

Webhook → Execute Workflow(<Combo>, входы plate/mileage/payment/query/
qualification_context/data_routing) → Respond. Обёртка создаётся, вызывается
РОВНО ОДИН раз и удаляется. Ответ пишется в --out.

Запуск: python3 run_combo_once.py --wf GjppGA6LqjbaqX4G --plate Х318ТЕ750 --mileage 35000 \
          --model MONJARO --model-id 18 --payment 50000 --out /tmp/combo.json
"""
import argparse
import json
import secrets
import sys
import time
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apply_workflow_patch import API, call  # noqa: E402

BASE = API.rsplit("/api/v1", 1)[0]
GEELY = {"catalog_base_id": "plun2iyp47ghhwn", "catalog_table_id": "m2flt4d2r5d9cpa", "stock_base_id": "p96xnewh6m79beu",
         "stock_table_id": "mrbc470pmkn9f14", "program_table_id": "msa0pe85fmadg8n", "rates_table_id": "mup1zkbxxaa6jha",
         "credit_table_id": "mup1zkbxxaa6jha"}
FIELDS = ("plate", "mileage", "payment", "query", "qualification_context", "data_routing")


def wrapper(name, target, path):
    nid = lambda: str(uuid.uuid4())
    return {
        "name": name, "settings": {"executionOrder": "v1", "availableInMCP": False},
        "nodes": [
            {"id": nid(), "name": "Webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2.1, "position": [0, 0],
             "webhookId": nid(), "parameters": {"httpMethod": "POST", "path": path, "responseMode": "responseNode", "options": {}}},
            {"id": nid(), "name": "Combo", "type": "n8n-nodes-base.executeWorkflow", "typeVersion": 1.3, "position": [240, 0],
             "parameters": {
                 "workflowId": {"__rl": True, "value": target, "mode": "id"},
                 "workflowInputs": {
                     "mappingMode": "defineBelow",
                     "value": {f: "={{ $json.body.%s }}" % f for f in FIELDS},
                     "matchingColumns": [],
                     "schema": [{"id": f, "displayName": f, "required": False, "defaultMatch": False, "display": True,
                                 "canBeUsedToMatch": True, "type": "string", "removed": False} for f in FIELDS],
                     "attemptToConvertTypes": False, "convertFieldsToString": False},
                 "options": {"waitForSubWorkflow": True}}},
            {"id": nid(), "name": "Respond", "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.5, "position": [480, 0],
             "parameters": {"respondWith": "firstIncomingItem", "options": {}}},
        ],
        "connections": {"Webhook": {"main": [[{"node": "Combo", "type": "main", "index": 0}]]},
                        "Combo": {"main": [[{"node": "Respond", "type": "main", "index": 0}]]}},
    }


def main():
    ap = argparse.ArgumentParser()
    for a in ("wf", "plate", "mileage", "model", "model-id", "payment", "out"):
        ap.add_argument(f"--{a}", required=True)
    args = ap.parse_args()
    qc = {"model": args.model, "model_id": args.model_id, "dealer_id": "99", "complectation_id": "00",
          "request_type": "combo", "payment": int(args.payment)}
    body = {"plate": args.plate, "mileage": args.mileage, "payment": args.payment, "query": args.model,
            "qualification_context": json.dumps(qc, ensure_ascii=False), "data_routing": json.dumps(GEELY)}
    path = f"scratch-{secrets.token_hex(8)}"
    s, w = call("POST", "/workflows", wrapper(f"[SCRATCH] combo once {int(time.time())}", args.wf, path))
    if s != 200:
        sys.exit(f"обёртка: {s} {w}")
    try:
        call("POST", f"/workflows/{w['id']}/activate")
        time.sleep(3)
        t0 = time.time()
        req = urllib.request.Request(f"{BASE}/webhook/{path}", method="POST",
                                     headers={"Content-Type": "application/json"}, data=json.dumps(body).encode())
        with urllib.request.urlopen(req, timeout=150) as r:
            resp = json.loads(r.read())
        print(f"ответ за {time.time() - t0:.1f} с")
        Path(args.out).write_text(json.dumps({"request": {**body, "qualification_context": qc}, "response": resp},
                                             ensure_ascii=False, indent=1))
    finally:
        call("POST", f"/workflows/{w['id']}/deactivate")
        s, _ = call("DELETE", f"/workflows/{w['id']}")
        print(f"обёртка удалена: {s}")


if __name__ == "__main__":
    main()
