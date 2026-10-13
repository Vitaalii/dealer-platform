"""Проверка исполнений Car Search после правки стока (только чтение n8n API).

По каждому исполнению после --since с каталогом из --catalogs:
  - Get Stock выполнен один раз, строки без дублей;
  - сколько машин привязано (stock_count) из total_stock; непривязанные — почему
    (slug нет среди активных строк каталога / строка неактивна);
  - в списке комплектаций нет строк с Active ≠ да.
--wait N — ждать первого подходящего исполнения до N секунд (опрос раз в 60 с).

Запуск: python3 check_car_search_stock_exec.py --wf mZqzmEnnQiDbvtir --since 2026-10-05T14:17:45Z \
          --catalogs m2flt4d2r5d9cpa,mc5jh12niqx7kp7 [--wait 10800]
"""
import argparse
import collections
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apply_workflow_patch import call  # noqa: E402

ACTIVE = (True, 1, "1", "true")


def items(run, node):
    try:
        return [i["json"] for r in run[node] for i in r["data"]["main"][0]]
    except (KeyError, IndexError, TypeError):
        return []


def check(e, catalogs):
    run = call("GET", f"/executions/{e['id']}?includeData=true")[1]["data"]["resultData"]["runData"]
    pi = (items(run, "Parse Input") or [{}])[0]
    dr, qc = pi.get("data_routing") or {}, pi.get("qualification_context") or {}
    if dr.get("catalog_table_id") not in catalogs:
        return None
    cat, stock = items(run, "Get Catalog"), [s for s in items(run, "Get Stock") if s.get("Model")]
    resp = (items(run, "Merge Calc") or [{}])[0]
    comps = resp.get("complectations", [])
    act_slugs = {str(t.get("slug") or "").strip().lower() for t in cat if t.get("Active") in ACTIVE and t.get("slug")}
    inactive_ids = {str(t.get("Complectation id")) for t in cat if t.get("Active") not in ACTIVE} - \
                   {str(t.get("Complectation id")) for t in cat if t.get("Active") in ACTIVE}
    why = collections.Counter("slug нет среди активных строк каталога"
                              for s in stock if str(s.get("slug") or "").strip().lower() not in act_slugs)
    matched = sum(c.get("stock_count", 0) for c in comps)
    shown_inactive = [c["name"] for c in comps if str(c.get("id")) in inactive_ids]
    ids = [s.get("id") for s in stock]
    print(f"{e['id']} {e['startedAt'][:19]} {e['status']} | дилер {qc.get('dealer_id')!r} модель {qc.get('model') or qc.get('model_id')} "
          f"| Get Stock запусков {len(run.get('Get Stock', []))}, машин {len(stock)}"
          f"{'' if len(set(ids)) == len(ids) or None in ids else ' (ДУБЛИ!)'} | привязано {matched} из {resp.get('summary', {}).get('total_stock')} "
          f"{[(c['name'], c['stock_count']) for c in comps]} | неактивные в списке: {shown_inactive or 'нет'}"
          + (f" | не привязано: {dict(why)}" if why else ""))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wf", required=True)
    ap.add_argument("--since", required=True)
    ap.add_argument("--catalogs", required=True)
    ap.add_argument("--wait", type=int, default=0)
    args = ap.parse_args()
    catalogs = set(args.catalogs.split(","))
    deadline, seen = time.time() + args.wait, set()
    found = 0
    while True:
        for e in sorted(call("GET", f"/executions?workflowId={args.wf}&limit=50")[1]["data"], key=lambda e: int(e["id"])):
            if e["startedAt"] < args.since or e["id"] in seen:
                continue
            seen.add(e["id"])
            if check(e, catalogs):
                found += 1
        if found or time.time() >= deadline:
            break
        time.sleep(60)
    if not found:
        print(f"исполнений по каталогам {sorted(catalogs)} после {args.since} нет")


if __name__ == "__main__":
    main()
