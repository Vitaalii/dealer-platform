"""Включение / выключение вебхука оценки и бесплатные проверки (auto.ru не вызывается).

  --check : без секрета → 403, неверный секрет → 403, верный секрет без mileage → 400
  --on    : активация (БОЙ — только по команде владельца) + --check
  --off   : деактивация (откат) + проверка 404

Секреты — ~/.config/autodc/cabinet-eval-webhook.secrets, значения не печатаются.
Запуск: python3 eval_webhook_switch.py --env prod --on
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apply_workflow_patch import API, call  # noqa: E402

ENVS = {"prod": ("MMhfTj8QJe2UIMSB", "cabinet-tradein-eval", "N8N_CABINET_EVAL_SECRET_PROD"),
        "test": ("99SEbXjbQta3SGSH", "cabinet-tradein-eval-test", "N8N_CABINET_EVAL_SECRET_TEST")}


def post(url, body, headers):
    req = urllib.request.Request(url, method="POST", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()[:150].decode("utf8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:150].decode("utf8", "replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", choices=ENVS, required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", action="store_true")
    g.add_argument("--off", action="store_true")
    g.add_argument("--check", action="store_true")
    args = ap.parse_args()
    wid, path, key = ENVS[args.env]
    url = f"{API.rsplit('/api/v1', 1)[0]}/webhook/{path}"
    secret = next(l.split("=", 1)[1].strip() for l in
                  (Path.home() / ".config" / "autodc" / "cabinet-eval-webhook.secrets").read_text().splitlines()
                  if l.startswith(key + "="))
    if args.on or args.off:
        s, w = call("POST", f"/workflows/{wid}/{'activate' if args.on else 'deactivate'}")
        print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "active =", (w or {}).get("active"), f"(HTTP {s})")
        time.sleep(2)
    if args.off:
        print("POST без тела →", post(url, {}, {})[0], "(ожидается 404)")
        return
    body = {"plate": "А123ВС777", "request_id": "switch-check"}            # без mileage — до auto.ru не дойдёт
    print("без секрета     →", post(url, body, {})[0], "(ожидается 403)")
    print("неверный секрет →", post(url, body, {"X-Cabinet-Eval-Secret": "wrong"})[0], "(ожидается 403)")
    s, b = post(url, body, {"X-Cabinet-Eval-Secret": secret})
    print("без mileage     →", s, b, "(ожидается 400 BAD_REQUEST)")


if __name__ == "__main__":
    main()
