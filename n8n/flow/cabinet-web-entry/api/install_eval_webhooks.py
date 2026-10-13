"""N1: установка вебхука оценки через API n8n (разрешено владельцем 2026-09-30).

1. Credentials Header Auth «Cabinet Eval Secret [TEST]» и «[PROD]»: имя
   заголовка X-Cabinet-Eval-Secret, значение — из
   ~/.config/autodc/cabinet-eval-webhook.secrets. Значения не печатаются.
2. Воркфлоу из ../[TEST] WEBHOOK_ … .json и ../WEBHOOK_ … .json, credential
   привязан к ноде Webhook. settings.binaryMode API не принимает, а при
   СОЗДАНИИ воркфлоу задать его нечем — у созданных через API копий его нет.
   Вебхук работает только с JSON; эквивалентность проверена 2026-09-30 на
   копии Car Search (4 входа, выходы 9 нод совпали).
3. Активируется ТОЛЬКО дев-копия. Боевая остаётся выключенной.

Идемпотентен: существующие credential / воркфлоу с тем же именем не
пересоздаются. Запуск: python3 install_eval_webhooks.py [--apply]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apply_workflow_patch import call  # noqa: E402

ENTRY = Path(__file__).resolve().parent.parent
SECRETS = Path.home() / ".config" / "autodc" / "cabinet-eval-webhook.secrets"
HEADER = "X-Cabinet-Eval-Secret"

COPIES = [
    {"file": "[TEST] WEBHOOK_ Cabinet Tradein Eval.json", "cred": "Cabinet Eval Secret [TEST]",
     "secret_key": "N8N_CABINET_EVAL_SECRET_TEST", "activate": True},
    {"file": "WEBHOOK_ Cabinet Tradein Eval.json", "cred": "Cabinet Eval Secret [PROD]",
     "secret_key": "N8N_CABINET_EVAL_SECRET_PROD", "activate": False},
]


def secrets():
    out = {}
    for line in SECRETS.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    sec = secrets()
    creds = {c["name"]: c for c in call("GET", "/credentials")[1]["data"]}
    wfs = {w["name"]: w for w in call("GET", "/workflows?limit=250")[1]["data"]}

    for c in COPIES:
        value = sec.get(c["secret_key"], "")
        if len(value) != 64:
            sys.exit(f"СТОП: {c['secret_key']} в файле секретов отсутствует или не 64 символа")
        wf = json.loads((ENTRY / c["file"]).read_text(encoding="utf-8"))

        cred = creds.get(c["cred"])
        if cred:
            print(f"credential «{c['cred']}»: уже есть ({cred['id']})")
        elif args.apply:
            s, cred = call("POST", "/credentials", {"name": c["cred"], "type": "httpHeaderAuth",
                                                    "data": {"name": HEADER, "value": value}})
            if s != 200:
                sys.exit(f"СТОП: создание credential {s}: {cred}")
            print(f"credential «{c['cred']}»: создан ({cred['id']})")
        else:
            print(f"credential «{c['cred']}»: будет создан")

        existing = wfs.get(wf["name"])
        if existing:
            print(f"воркфлоу «{wf['name']}»: уже есть ({existing['id']}, active={existing['active']})")
            continue
        if not args.apply:
            print(f"воркфлоу «{wf['name']}»: будет создан, активация: {c['activate']}")
            continue
        for n in wf["nodes"]:
            if n["type"] == "n8n-nodes-base.webhook":
                n["credentials"] = {"httpHeaderAuth": {"id": cred["id"], "name": c["cred"]}}
        settings = {k: v for k, v in wf["settings"].items() if k != "binaryMode"}
        s, created = call("POST", "/workflows", {"name": wf["name"], "nodes": wf["nodes"],
                                                 "connections": wf["connections"], "settings": settings})
        if s != 200:
            sys.exit(f"СТОП: создание воркфлоу {s}: {created}")
        print(f"воркфлоу «{wf['name']}»: создан ({created['id']})")
        if c["activate"]:
            s, r = call("POST", f"/workflows/{created['id']}/activate")
            print(f"   активация: {s}{'' if s == 200 else ' ' + str(r)}")


if __name__ == "__main__":
    main()
