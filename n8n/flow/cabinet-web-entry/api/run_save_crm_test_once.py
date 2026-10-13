"""Один ручной запуск TOOL: Save to CRM_test (12IRy1rcYOuZzV7m) на колбэк дев-бота.

Временная обёртка Webhook → Execute Workflow(Save to CRM_test), один вызов,
удаление. Данные только тестовые: deal_id пустой (иначе дев-бот пойдёт в
боевой Bitrix), user_id с префиксом test-. Печатает время запуска, ответ бота
и исполнение Save to CRM_test.

Запуск: python3 run_save_crm_test_once.py --user-id test-n8nauth-callback-03
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

TARGET = "12IRy1rcYOuZzV7m"
FIELDS = ("user_id", "deal_id", "ai_payload")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", required=True)
    args = ap.parse_args()
    if not args.user_id.startswith("test-"):
        sys.exit("СТОП: user_id обязан начинаться с test-")
    base = API.rsplit("/api/v1", 1)[0]
    path = f"scratch-{secrets.token_hex(8)}"
    nid = lambda: str(uuid.uuid4())
    wf = {"name": f"[SCRATCH] save-crm-test once {int(time.time())}",
          "settings": {"executionOrder": "v1", "availableInMCP": False},
          "nodes": [
              {"id": nid(), "name": "Webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2.1, "position": [0, 0],
               "webhookId": nid(), "parameters": {"httpMethod": "POST", "path": path, "responseMode": "lastNode", "options": {}}},
              {"id": nid(), "name": "Save to CRM_test", "type": "n8n-nodes-base.executeWorkflow", "typeVersion": 1.3,
               "position": [240, 0],
               "parameters": {"workflowId": {"__rl": True, "value": TARGET, "mode": "id"},
                              "workflowInputs": {"mappingMode": "defineBelow",
                                                 "value": {f: "={{ $json.body.%s }}" % f for f in FIELDS},
                                                 "matchingColumns": [],
                                                 "schema": [{"id": f, "displayName": f, "required": False, "defaultMatch": False,
                                                             "display": True, "canBeUsedToMatch": True, "type": "string",
                                                             "removed": False} for f in FIELDS],
                                                 "attemptToConvertTypes": False, "convertFieldsToString": False},
                              "options": {"waitForSubWorkflow": True}}}],
          "connections": {"Webhook": {"main": [[{"node": "Save to CRM_test", "type": "main", "index": 0}]]}}}
    s, w = call("POST", "/workflows", wf)
    if s != 200:
        sys.exit(f"обёртка: {s} {w}")
    try:
        call("POST", f"/workflows/{w['id']}/activate")
        time.sleep(3)
        body = {"user_id": args.user_id, "deal_id": "",
                "ai_payload": json.dumps({"status": "in_progress",
                                          "report": "Тест канала n8n→бот: проверка X-N8N-Callback-Secret. Не клиент."},
                                         ensure_ascii=False)}
        print("запуск:", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        req = urllib.request.Request(f"{base}/webhook/{path}", method="POST",
                                     headers={"Content-Type": "application/json"}, data=json.dumps(body).encode())
        with urllib.request.urlopen(req, timeout=120) as r:
            print("ответ обёртки:", r.status, r.read()[:200].decode("utf8", "replace"))
    finally:
        call("POST", f"/workflows/{w['id']}/deactivate")
        print("обёртка удалена:", call("DELETE", f"/workflows/{w['id']}")[0])
    time.sleep(2)
    e = max(call("GET", f"/executions?workflowId={TARGET}&limit=5")[1]["data"], key=lambda e: int(e["id"]))
    run = call("GET", f"/executions/{e['id']}?includeData=true")[1]["data"]["resultData"]["runData"]
    hr = run["HTTP Request"][0]
    out = hr["data"]["main"][0][0]["json"] if hr.get("data") else (hr.get("error") or {}).get("message")
    print(f"исполнение Save to CRM_test {e['id']} {e['startedAt'][11:19]} {e['status']} | ответ бота: "
          f"{json.dumps(out, ensure_ascii=False)[:200]}")


if __name__ == "__main__":
    main()
