"""Сверка живого Car Search с эталоном N2 (price_reference.json).

Создаёт временную обёртку «Webhook → Execute Workflow(<Car Search>) → Respond»
на случайном пути, прогоняет каждую модель каталогов с программами (HAVAL,
GEELY, BELGEE) при платеже 0 и сравнивает по каждой комплектации:
credit_price и стандартный расчёт (взнос, платёж, срок, ставка) — с вариантом
эталона --variant (n4 после правки, live до неё). Обёртка удаляется в конце.
Car Search только читает NocoDB и зовёт Calc Engine: платных вызовов нет.

Запуск: python3 check_car_search_vs_reference.py --wf o3vH8ZVKW5dNMkK2 --variant n4
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

REF = Path(__file__).resolve().parent.parent / "n2-price-reference" / "price_reference.json"
BASE = API.rsplit("/api/v1", 1)[0]
STOCK = {"stock_base_id": "p96xnewh6m79beu", "stock_table_id": "mtsf9afpiqe1yr1"}   # склад на цены не влияет
ROUTING = {
    "mywf0f16ptn25gk": {"catalog_base_id": "p96xnewh6m79beu", "program_table_id": "my7rg0l7j6varzn", "rates_table_id": "mfocbgvi50mqm7c"},
    "m2flt4d2r5d9cpa": {"catalog_base_id": "plun2iyp47ghhwn", "program_table_id": "msa0pe85fmadg8n", "rates_table_id": "mup1zkbxxaa6jha"},
    "mc5jh12niqx7kp7": {"catalog_base_id": "pjj37udcy4j4m4u", "program_table_id": "m7a1rkmtih8o9yo", "rates_table_id": "mmfiib16bjgllw1"},
}


def wrapper(name, target, path):
    nid = lambda: str(uuid.uuid4())
    return {
        "name": name, "settings": {"executionOrder": "v1", "availableInMCP": False},
        "nodes": [
            {"id": nid(), "name": "Webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2.1, "position": [0, 0],
             "webhookId": nid(), "parameters": {"httpMethod": "POST", "path": path, "responseMode": "responseNode", "options": {}}},
            {"id": nid(), "name": "Input", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [220, 0],
             "parameters": {"jsCode": "const b = $input.first().json.body;\nreturn [{ json: { qualification_context: b.qualification_context, data_routing: b.data_routing } }];"}},
            {"id": nid(), "name": "Car Search", "type": "n8n-nodes-base.executeWorkflow", "typeVersion": 1.3, "position": [440, 0],
             "parameters": {"workflowId": {"__rl": True, "value": target, "mode": "id"},
                            "workflowInputs": {"mappingMode": "defineBelow", "value": {}, "matchingColumns": [], "schema": [],
                                               "attemptToConvertTypes": False, "convertFieldsToString": True},
                            "options": {"waitForSubWorkflow": True}}},
            {"id": nid(), "name": "Respond", "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.5, "position": [660, 0],
             "parameters": {"respondWith": "firstIncomingItem", "options": {}}},
        ],
        "connections": {"Webhook": {"main": [[{"node": "Input", "type": "main", "index": 0}]]},
                        "Input": {"main": [[{"node": "Car Search", "type": "main", "index": 0}]]},
                        "Car Search": {"main": [[{"node": "Respond", "type": "main", "index": 0}]]}},
    }


def post(url, body):
    req = urllib.request.Request(url, method="POST", headers={"Content-Type": "application/json"},
                                 data=json.dumps(body).encode())
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wf", required=True)
    ap.add_argument("--variant", choices=["n4", "live"], required=True)
    args = ap.parse_args()
    ref = json.loads(REF.read_text(encoding="utf-8"))
    rows = [r for r in ref["rows"] if r["catalog_table_id"] in ROUTING and r["model_id"]]
    models = {}
    for r in rows:
        models.setdefault((r["catalog_table_id"], r["model_id"]), []).append(r)

    path = f"scratch-{secrets.token_hex(8)}"
    s, w = call("POST", "/workflows", wrapper(f"[SCRATCH] check Car Search {int(time.time())}", args.wf, path))
    if s != 200:
        sys.exit(f"обёртка: {s} {w}")
    checked = mismatches = 0
    try:
        call("POST", f"/workflows/{w['id']}/activate")
        time.sleep(3)
        for (cat, mid), refs in models.items():
            routing = {**ROUTING[cat], **STOCK, "catalog_table_id": cat, "credit_table_id": ROUTING[cat]["rates_table_id"]}
            qc = {"model": refs[0]["model"], "model_id": mid, "dealer_id": "99", "complectation_id": "00",
                  "request_type": "credit", "payment": 0}
            resp = post(f"{BASE}/webhook/{path}", {"qualification_context": qc, "data_routing": routing})
            got = {c["id"]: c for c in resp.get("complectations", [])}
            for r in refs:
                checked += 1
                c = got.get(r["complectation_id"])
                exp_std = r["credit_standard_payment0"][args.variant]
                act_std = (((c or {}).get("credit_calculation") or {}).get("scenarios") or {}).get("standard")
                act = None if act_std is None else {k: act_std.get(k) for k in ("down_payment", "monthly_payment", "term", "rate_text")}
                ok = c is not None and c["price"]["credit_price"] == r["price"]["credit_price"] and act == exp_std
                if not ok:
                    mismatches += 1
                    print(f"  РАСХОЖДЕНИЕ {r['catalog_title']} {r['model']} {r['complectation']}: "
                          f"credit_price {None if c is None else c['price']['credit_price']} vs {r['price']['credit_price']}; "
                          f"standard {act} vs {exp_std}")
            print(f"{refs[0]['catalog_title']:12} {refs[0]['model']:22} комплектаций {len(refs)}: "
                  f"программа {'есть' if refs[0]['program']['n4'] else 'нет'}")
    finally:
        call("POST", f"/workflows/{w['id']}/deactivate")
        s, _ = call("DELETE", f"/workflows/{w['id']}")
        print(f"обёртка удалена: {s}")
    print(f"ИТОГ: комплектаций {checked}, расхождений {mismatches}")


if __name__ == "__main__":
    main()
