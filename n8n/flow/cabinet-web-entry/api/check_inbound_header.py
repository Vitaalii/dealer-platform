"""Есть ли заголовок X-N8N-Secret во входящих запросах вебхука и совпадает ли он с секретом.

Читает исполнения (только чтение API), значение заголовка НЕ выводит: печатает
«нет» / «есть, совпадает» / «есть, НЕ совпадает». Секрет — из
~/.config/autodc/n8n-channel-secrets.

Запуск: python3 check_inbound_header.py --wf pkA3mmrvv9XqmZRE --after 5238 --secret-key N8N_WEBHOOK_SECRET_TEST
        [--wait 3600]   — ждать первого исполнения новее --after (опрос раз в 20 с)
"""
import argparse
import hmac
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apply_workflow_patch import call  # noqa: E402

HEADER = "x-n8n-secret"


def secret(key):
    for line in (Path.home() / ".config" / "autodc" / "n8n-channel-secrets").read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"нет {key}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wf", required=True)
    ap.add_argument("--after", type=int, required=True)
    ap.add_argument("--secret-key", required=True)
    ap.add_argument("--wait", type=int, default=0)
    args = ap.parse_args()
    expected = secret(args.secret_key)
    deadline = time.time() + args.wait
    while True:
        ex = [e for e in call("GET", f"/executions?workflowId={args.wf}&limit=50")[1]["data"] if int(e["id"]) > args.after]
        if ex or time.time() >= deadline:
            break
        time.sleep(20)
    if not ex:
        print(f"{args.wf}: исполнений новее {args.after} нет")
        return
    for e in sorted(ex, key=lambda e: int(e["id"])):
        d = call("GET", f"/executions/{e['id']}?includeData=true")[1]
        try:
            hdrs = d["data"]["resultData"]["runData"]["Webhook"][0]["data"]["main"][0][0]["json"]["headers"]
        except (KeyError, IndexError, TypeError):
            print(f"{e['id']} {e['startedAt'][:19]} {e['status']}: нет данных ноды Webhook")
            continue
        got = hdrs.get(HEADER)
        verdict = "нет" if got is None else ("есть, совпадает" if hmac.compare_digest(str(got), expected) else "есть, НЕ совпадает")
        print(f"{e['id']} {e['startedAt'][:19]} {e['status']}: {HEADER} — {verdict}")


if __name__ == "__main__":
    main()
