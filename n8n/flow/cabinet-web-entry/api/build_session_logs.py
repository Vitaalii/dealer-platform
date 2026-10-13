"""Get Session Logs: защищённые боевая и [TEST]-копии (решение владельца 2026-10-03).

Было: GET без аутентификации, session_id подставлялся в SQL строкой
(LIKE '%sess_<ввод>%'), поиск по исполнениям ВСЕХ воркфлоу.
Стало:
  Webhook (GET, Header Auth X-N8N-Secret) → Validate (формат session_id) →
  Postgres (параметр $1, точное совпадение строки "sess_<id>" в данных
  исполнения, только исполнения «своего» агента) → Respond {logs}.
  Плохой session_id → 400 без запроса к БД.

| Копия | Путь | Credential | Агент |
| бой   | get-session-logs      | N8N Inbound Secret [PROD] | AI Agent v3.0 (fQAS3wmAjVqrKATM) |
| тест  | get-session-logs-test | N8N Inbound Secret [TEST] | [TEST] AI Agent v3.0 (pkA3mmrvv9XqmZRE) |

Контракт ответа бот уже понимает: {"logs": [...] | null} (main.py fetch_n8n_logs_for_archive).
Запуск: python3 build_session_logs.py            — только пишет JSON в ../session-logs/
        python3 build_session_logs.py --apply    — боевая копия: PUT в I7r3lWLiKmW1HojQ
                                                   (id нод и webhookId сохраняются) + активация;
                                                   тестовая: создание + активация.
"""
import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apply_workflow_patch import call  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "session-logs"
PROD_ID = "I7r3lWLiKmW1HojQ"
POSTGRES_CRED = {"postgres": {"id": "3ZfZUULMAZLBq8iw", "name": "Postgres account"}}

COPIES = {
    "prod": {"name": "Get Session Logs (API)", "path": "get-session-logs", "agent": "fQAS3wmAjVqrKATM",
             "cred": "N8N Inbound Secret [PROD]", "file": "Get Session Logs (API).json"},
    "test": {"name": "[TEST] Get Session Logs (API)", "path": "get-session-logs-test", "agent": "pkA3mmrvv9XqmZRE",
             "cred": "N8N Inbound Secret [TEST]", "file": "[TEST] Get Session Logs (API).json"},
}

VALIDATE = r"""// Формат session_id: бот шлёт его без префикса sess_ (main.py,
// fetch_n8n_logs_for_archive): «<user_id>_<start_code>». Кавычки, %, пробелы
// и прочее запрещены — значение дальше идёт параметром $1 и ищется как
// точная строка "sess_<id>" в данных исполнения (кавычки добавляет SQL).
const raw = String(($input.first().json.query || {}).session_id || "").trim();
const sid = raw.startsWith("sess_") ? raw.slice(5) : raw;
const ok = /^[A-Za-z0-9_.:@-]{1,128}$/.test(sid);
return [{ json: { ok, needle: ok ? `sess_${sid}` : "" } }];
"""


def query(agent_id: str) -> str:
    return (
        "SELECT json_agg(row_to_json(t)) AS logs\n"
        "FROM (\n"
        "    SELECT e.id, e.\"workflowId\", e.\"startedAt\", e.\"stoppedAt\", d.data\n"
        "    FROM execution_entity e\n"
        "    JOIN execution_data d ON e.id = d.\"executionId\"\n"
        f"    WHERE e.\"workflowId\" = '{agent_id}'\n"
        "      AND strpos(d.data::text, '\"' || $1::text || '\"') > 0\n"
        "    ORDER BY e.\"startedAt\" DESC\n"
        "    LIMIT 3\n"
        ") t;"
    )


def build(variant: str, cfg: dict, cred_id: str, keep: dict | None = None) -> dict:
    """keep — живой воркфлоу: id нод Webhook/Postgres и webhookId сохраняются."""
    old = {n["name"]: n for n in (keep or {}).get("nodes", [])}
    nid = lambda name: old.get(name, {}).get("id") or str(uuid.uuid5(uuid.NAMESPACE_URL, f"session-logs/{variant}/{name}"))
    webhook_id = old.get("Webhook", {}).get("webhookId") or str(uuid.uuid5(uuid.NAMESPACE_URL, f"session-logs/{variant}/webhookId"))
    nodes = [
        {"id": nid("Webhook"), "name": "Webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2.1,
         "position": [0, 0], "webhookId": webhook_id,
         "parameters": {"httpMethod": "GET", "path": cfg["path"], "authentication": "headerAuth",
                        "responseMode": "responseNode", "options": {}},
         "credentials": {"httpHeaderAuth": {"id": cred_id, "name": cfg["cred"]}}},
        {"id": nid("Validate"), "name": "Validate", "type": "n8n-nodes-base.code", "typeVersion": 2,
         "position": [220, 0], "parameters": {"jsCode": VALIDATE}},
        {"id": nid("OK?"), "name": "OK?", "type": "n8n-nodes-base.if", "typeVersion": 2.3, "position": [440, 0],
         "parameters": {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 3},
                                       "conditions": [{"id": nid("cond"), "leftValue": "={{ $json.ok }}", "rightValue": True,
                                                       "operator": {"type": "boolean", "operation": "equals"}}],
                                       "combinator": "and"}, "options": {}}},
        {"id": nid("Postgres (n8n DB)"), "name": "Postgres (n8n DB)", "type": "n8n-nodes-base.postgres", "typeVersion": 2.2,
         "position": [660, -80],
         "parameters": {"operation": "executeQuery", "query": query(cfg["agent"]),
                        "options": {"queryReplacement": "={{ $json.needle }}"}},
         "credentials": POSTGRES_CRED},
        {"id": nid("Respond"), "name": "Respond", "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.5,
         "position": [880, -80], "parameters": {"respondWith": "firstIncomingItem", "options": {"responseCode": 200}}},
        {"id": nid("Bad Request"), "name": "Bad Request", "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.5,
         "position": [660, 100],
         "parameters": {"respondWith": "json", "responseBody": "={\"error\": \"bad session_id\"}",
                        "options": {"responseCode": 400}}},
    ]
    m = lambda *t: {"main": [[{"node": x, "type": "main", "index": 0}] for x in t]}
    return {"name": cfg["name"], "nodes": nodes,
            "connections": {"Webhook": m("Validate"), "Validate": m("OK?"),
                            "OK?": m("Postgres (n8n DB)", "Bad Request"), "Postgres (n8n DB)": m("Respond")},
            "settings": {"executionOrder": "v1", "availableInMCP": False}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    creds = {c["name"]: c["id"] for c in call("GET", "/credentials")[1]["data"]}
    OUT.mkdir(exist_ok=True)
    live_prod = call("GET", f"/workflows/{PROD_ID}")[1]
    wfs = {w["name"]: w for w in call("GET", "/workflows?limit=250")[1]["data"]}
    for variant, cfg in COPIES.items():
        wf = build(variant, cfg, creds[cfg["cred"]], live_prod if variant == "prod" else None)
        (OUT / cfg["file"]).write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{variant}: {OUT.name}/{cfg['file']} → /webhook/{cfg['path']}")
        if not args.apply:
            continue
        if variant == "prod":
            if live_prod["active"]:
                sys.exit("СТОП: боевой Get Session Logs активен — ожидался выключенным")
            s, r = call("PUT", f"/workflows/{PROD_ID}", wf)
            wid = PROD_ID
        else:
            if cfg["name"] in wfs:
                sys.exit(f"СТОП: «{cfg['name']}» уже существует ({wfs[cfg['name']]['id']})")
            s, r = call("POST", "/workflows", wf)
            wid = r["id"] if s == 200 else None
        if s != 200:
            sys.exit(f"СТОП: запись {variant}: {s} {r}")
        s, r = call("POST", f"/workflows/{wid}/activate")
        print(f"   записан {wid}, активация {s}{'' if s == 200 else ' ' + str(r)}")


if __name__ == "__main__":
    main()
