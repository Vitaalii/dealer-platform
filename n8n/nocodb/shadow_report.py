#!/usr/bin/env python3
"""U9: отчёт по shadow-логам резолвера — материал для ворот фазы D.

Показывает: долю вердиктов (ok/parse_error/unknown_intent/llm_error),
распределение интентов по частоте, p50/p95 латентности, разбивку по сценариям.
Ворота D (см. план): точность на топ-интентах, p95 латентности и стоимость —
в бюджете R23.

Запуск: python3 shadow_report.py [--days 7]
"""
import argparse
import json
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_env():
    env = {}
    for line in (HERE.parent / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def fetch_all(env, table_id):
    rows, offset = [], 0
    while True:
        req = urllib.request.Request(
            f"{env['NOCODB_API_URL'].rstrip('/')}/api/v2/tables/{table_id}/records?limit=200&offset={offset}",
            headers={"xc-token": env["NOCODB_TOKEN"]})
        with urllib.request.urlopen(req, timeout=30) as r:
            batch = json.loads(r.read()).get("list", [])
        rows.extend(batch)
        if len(batch) < 200:
            return rows
        offset += 200


def pct(part, total):
    return f"{100 * part / total:.1f}%" if total else "n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    env = load_env()
    ids = json.loads((HERE / "table_ids.json").read_text())
    rows = fetch_all(env, ids["Resolver_Shadow_Log"])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    rows = [r for r in rows if (r.get("ts") or "") >= cutoff]
    if not rows:
        print(f"нет записей за последние {args.days} дн. — shadow ещё не собирает данные")
        return

    n = len(rows)
    verdicts = Counter(r.get("verdict") for r in rows)
    print(f"ходов за {args.days} дн.: {n}")
    print("вердикты:", {k: f"{v} ({pct(v, n)})" for k, v in verdicts.most_common()})

    lat = sorted(int(r.get("latency_ms") or 0) for r in rows if r.get("latency_ms"))
    if lat:
        print(f"латентность резолвера: p50={lat[len(lat)//2]}мс p95={lat[min(len(lat)-1, int(len(lat)*0.95))]}мс")

    ok = [r for r in rows if r.get("verdict") == "ok"]
    print(f"\nтоп интентов (ok-ходы, {len(ok)}):")
    for intent, cnt in Counter(r["dialog_intent"] for r in ok).most_common(20):
        print(f"  {intent:<28} {cnt:>4} ({pct(cnt, len(ok))})")

    print("\nпо сценариям:")
    for scen, cnt in Counter(r.get("scenario") for r in rows).most_common():
        scen_ok = sum(1 for r in rows if r.get("scenario") == scen and r.get("verdict") == "ok")
        print(f"  {scen:<10} ходов {cnt:>4}, ok {pct(scen_ok, cnt)}")

    print("\nдля ручной разметки точности: выгрузите ok-ходы и сверьте dialog_intent")
    print("с фактическим смыслом сообщения (цель ≥100 ходов до ворот D).")


if __name__ == "__main__":
    main()
