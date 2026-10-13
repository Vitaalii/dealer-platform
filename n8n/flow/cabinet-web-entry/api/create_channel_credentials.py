"""Credentials Header Auth для каналов бот ↔ n8n (план docs/handoff/2026-10-03-n8n-webhook-auth-bot-plan.md).

Значения — из ~/.config/autodc/n8n-channel-secrets (права 600), не печатаются.
  «N8N Inbound Secret [TEST|PROD]»  — X-N8N-Secret: проверка входящих запросов
                                      бота на вебхуках агента, shadow, логов сессии;
  «Bot Callback Secret [TEST|PROD]» — X-N8N-Callback-Secret: заголовок, который
                                      HTTP-нода Save to CRM шлёт в /n8n_callback бота.
Идемпотентен: существующие по имени не пересоздаются.
Запуск: python3 create_channel_credentials.py [--apply]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apply_workflow_patch import call  # noqa: E402

SECRETS = Path.home() / ".config" / "autodc" / "n8n-channel-secrets"
CREDS = [
    ("N8N Inbound Secret [TEST]", "X-N8N-Secret", "N8N_WEBHOOK_SECRET_TEST"),
    ("N8N Inbound Secret [PROD]", "X-N8N-Secret", "N8N_WEBHOOK_SECRET_PROD"),
    ("Bot Callback Secret [TEST]", "X-N8N-Callback-Secret", "N8N_CALLBACK_SECRET_TEST"),
    ("Bot Callback Secret [PROD]", "X-N8N-Callback-Secret", "N8N_CALLBACK_SECRET_PROD"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    sec = {}
    for line in SECRETS.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            sec[k.strip()] = v.strip()
    existing = {c["name"]: c for c in call("GET", "/credentials")[1]["data"]}
    for name, header, key in CREDS:
        if len(sec.get(key, "")) != 64:
            sys.exit(f"СТОП: {key} отсутствует или не 64 символа")
        if name in existing:
            print(f"«{name}»: уже есть ({existing[name]['id']})")
            continue
        if not args.apply:
            print(f"«{name}»: будет создан, заголовок {header}")
            continue
        s, r = call("POST", "/credentials", {"name": name, "type": "httpHeaderAuth",
                                             "data": {"name": header, "value": sec[key]}})
        if s != 200:
            sys.exit(f"СТОП: {name}: {s} {r}")
        print(f"«{name}»: создан ({r['id']}), заголовок {header}")


if __name__ == "__main__":
    main()
