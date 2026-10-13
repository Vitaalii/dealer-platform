#!/usr/bin/env python3
"""Разбор целевых прогонов де-брендинга по трём осям приёмки (план U4).

Читает результат `debrand_testruns.py --label <name>` и сводит его в таблицу
критериев плана. Дополнительно поднимает исполнения `[TEST] AI Agent v3.0`
через API n8n и считает, сколько ходов вызвали `finalize_deal` — это
единственная предпосылка внешней записи (алерта в группу).

Только чтение.

Запуск:
  python3 debrand_runs_report.py --label after
"""
import json
import re
import statistics
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ENV_PATH = ROOT / ".env"
OUT_DIR = ROOT / "flow" / "lib" / "snapshots"
TEST_WORKFLOW = "pkA3mmrvv9XqmZRE"

LEAK = re.compile(r"\[(Модель|Комплектация|Сумма|Имя клиента|credit_price|"
                  r"base_price|tradein_price|down_payment|monthly_payment|term|"
                  r"dealer_name|dealer_address|dealer_hours)\]")

# Детектор чужой марки — общий с debrand_observe.py, чтобы приёмка на [TEST] и
# замеры на боевом трафике считали одно и то же. Упоминание марки клиента
# нарушением НЕ является; нарушение — «Dargo» либо марка, которой нет в
# контексте хода и рядом с которой не стоит машина клиента.
sys.path.insert(0, str(HERE))
from debrand_observe import brand_violations, RETIRED_MODEL   # noqa: E402

BRANDS = {"Geely": r"geely", "Belgee": r"belgee", "Haval": r"haval"}
# База сравнения — срез «до» правки (observe_before.json).
BASE_LEN, BASE_SPLIT = 302.0, 1.0


def load_env():
    env = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def api(env, path, timeout=60):
    req = urllib.request.Request(
        env["N8N_API_URL"].rstrip("/") + path,
        headers={"X-N8N-API-KEY": env["N8N_API_KEY"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def own_brand(car):
    for label, pat in BRANDS.items():
        if car and label.lower() in car.lower():
            return label, pat
    return None, None


def check_full(r):
    """Ось 1: назвала ли модель именно машину клиента, а не образец."""
    t = r["text"] or ""
    car, trim = r["car"], r["trim"]
    label, pat = own_brand(car)
    model_named = bool(re.search(pat, t, re.I)) if pat else False
    trim_named = bool(trim) and trim.lower() in t.lower()
    violations = brand_violations(t, car or "", [trim] if trim else [])
    return {
        "ok_format": model_named and trim_named,
        "model_named": model_named, "trim_named": trim_named,
        "foreign_brand": [v for v in violations if v.startswith("чужая марка")],
        "dargo": bool(RETIRED_MODEL.search(t)),
        "leaks": sorted(set(LEAK.findall(t))),
        "chars": len(t), "splits": t.count("[SPLIT]"),
        "empty": not t.strip(),
    }


def finalize_calls(env, user_ids):
    """Сколько прогонов дошло до вызова finalize_deal (предпосылка алерта)."""
    try:
        execs = api(env, f"/executions?workflowId={TEST_WORKFLOW}&limit=120")["data"]
    except Exception as e:
        return None, f"исполнения не прочитаны: {e}"
    hits, checked, started = [], 0, time.time()
    for e in execs:
        if time.time() - started > 240:
            return hits, f"остановка по времени, проверено {checked} исполнений"
        try:
            full = api(env, f"/executions/{e['id']}?includeData=true", timeout=60)
        except Exception:
            continue
        blob = json.dumps(full, ensure_ascii=False)
        uid = next((u for u in user_ids if f'"{u}"' in blob), None)
        if uid is None:
            continue
        checked += 1
        rd = full.get("data", {}).get("resultData", {}).get("runData", {})
        called = False
        for node in ("Agent", "Agent Retry"):
            for run in rd.get(node, []) or []:
                for arr in (run.get("data", {}) or {}).get("main", []) or []:
                    for item in arr or []:
                        for st in (item.get("json") or {}).get("intermediateSteps", []) or []:
                            tool = str(((st or {}).get("action") or {}).get("tool") or "")
                            if "finalize" in tool.lower():
                                called = True
        if rd.get("Save CRM Direct"):
            called = True
        if called:
            hits.append({"exec": e["id"], "user_id": uid})
    return hits, f"сопоставлено исполнений: {checked} из {len(user_ids)} прогонов"


def main():
    args = sys.argv[1:]
    label = args[args.index("--label") + 1] if "--label" in args else "after"
    data = json.loads((OUT_DIR / f"testruns_{label}.json").read_text(encoding="utf-8"))
    res = data["results"]
    full = [r for r in res if r["kind"] == "full"]
    empty = [r for r in res if r["kind"] == "empty"]
    for r in res:
        r["check"] = check_full(r)

    print("=" * 74)
    print("ОСНОВНАЯ СЕРИЯ (данные заполнены) — три оси критериев плана")
    print("=" * 74)
    hdr = f"{'сценарий':10} {'прог':>4} {'формат ok':>10} {'утечки':>7} {'чужая марка':>12} {'Dargo':>6} {'мед.длина':>10} {'мед.SPLIT':>10}"
    print(hdr)
    total = {"n": 0, "ok": 0, "leak": 0, "foreign": 0, "dargo": 0}
    lens_all, spl_all = [], []
    for scen in ("credit", "tradein", "combo"):
        rows = [r for r in full if r["scenario"] == scen]
        if not rows:
            continue
        ok = sum(1 for r in rows if r["check"]["ok_format"])
        leak = sum(1 for r in rows if r["check"]["leaks"])
        frg = sum(1 for r in rows if r["check"]["foreign_brand"])
        dg = sum(1 for r in rows if r["check"]["dargo"])
        lens = [r["check"]["chars"] for r in rows]
        spl = [r["check"]["splits"] for r in rows]
        lens_all += lens
        spl_all += spl
        total["n"] += len(rows); total["ok"] += ok
        total["leak"] += leak; total["foreign"] += frg; total["dargo"] += dg
        print(f"{scen:10} {len(rows):>4} {ok:>7}/{len(rows):<2} {leak:>7} {frg:>12} {dg:>6} "
              f"{statistics.median(lens):>10.0f} {statistics.median(spl):>10.1f}")
    if lens_all:
        ml, ms = statistics.median(lens_all), statistics.median(spl_all)
        dl = (ml - BASE_LEN) / BASE_LEN * 100
        ds = (ms - BASE_SPLIT) / BASE_SPLIT * 100
        print(f"\n{'ИТОГО':10} {total['n']:>4} {total['ok']:>7}/{total['n']:<2} "
              f"{total['leak']:>7} {total['foreign']:>12} {total['dargo']:>6} "
              f"{ml:>10.0f} {ms:>10.1f}")
        print(f"\nбаза «до»: длина {BASE_LEN:.0f} симв., [SPLIT] {BASE_SPLIT:.1f}")
        print(f"отклонение: длина {dl:+.1f} %, [SPLIT] {ds:+.1f} %  (порог ±20 %)")

        print("\nКРИТЕРИИ ПЛАНА:")
        c1 = all(sum(1 for r in full if r["scenario"] == s and r["check"]["ok_format"]) >= 9
                 for s in ("credit", "tradein", "combo"))
        c1b = total["dargo"] == 0
        c2 = total["leak"] == 0
        c3 = abs(dl) <= 20 and abs(ds) <= 20
        print(f"  формат: >=9 из 10 на каждый сценарий .......... {'ДА' if c1 else 'НЕТ'}")
        print(f"  формат: «Dargo» 0 из {total['n']} ..................... {'ДА' if c1b else 'НЕТ'}")
        print(f"  утечка шаблонов: 0 из {total['n']} .................... {'ДА' if c2 else 'НЕТ'}")
        print(f"  длина и [SPLIT]: отклонение <=20 % ........... {'ДА' if c3 else 'НЕТ'}")
        print(f"\n  ВЫВОД «СТАЛО НЕ ХУЖЕ»: {'ДА' if (c1 and c1b and c2 and c3) else 'НЕТ'}")

    print("\n" + "=" * 74)
    print("КОНТРОЛЬНАЯ СЕРИЯ (model и комплектация пусты) — данные для решения по U6")
    print("=" * 74)
    print(f"{'сценарий':10} {'прог':>4} {'утечка скобок':>14} {'выдуманная марка':>18} {'мед.длина':>10}")
    for scen in ("credit", "tradein", "combo"):
        rows = [r for r in empty if r["scenario"] == scen]
        if not rows:
            continue
        leak = sum(1 for r in rows if r["check"]["leaks"])
        inv = sum(1 for r in rows if r["check"]["foreign_brand"] or r["check"]["dargo"])
        print(f"{scen:10} {len(rows):>4} {leak:>10}/{len(rows):<2} {inv:>14}/{len(rows):<2} "
              f"{statistics.median([r['check']['chars'] for r in rows]):>10.0f}")
    leaked = [r for r in empty if r["check"]["leaks"]]
    if leaked:
        print("\nходы с дословными скобками (аргумент ЗА U6):")
        for r in leaked:
            print(f"  {r['user_id']}: {r['check']['leaks']}")
            print(f"    {r['text'][:200]}")
    else:
        print("\nдословных скобок при пустых данных нет ни в одном ходе "
              "(аргумент ПРОТИВ немедленного U6)")

    errs = [r for r in res if r["error"] or r["check"]["empty"]]
    if errs:
        print(f"\nсбойных или пустых ответов: {len(errs)}")
        for r in errs:
            print(f"  {r['user_id']}: error={r['error']} chars={r['check']['chars']}")

    env = load_env()
    print("\n" + "=" * 74)
    print("ВНЕШНИЕ ЗАПИСИ: вызовы finalize_deal (единственная предпосылка алерта)")
    print("=" * 74)
    hits, note = finalize_calls(env, [r["user_id"] for r in res])
    print(f"  {note}")
    if hits is None:
        print("  НЕ УСТАНОВЛЕНО")
    else:
        print(f"  ходов с вызовом finalize_deal / Save CRM: {len(hits)}")
        for h in hits:
            print(f"    exec {h['exec']} user_id={h['user_id']}")


if __name__ == "__main__":
    main()
