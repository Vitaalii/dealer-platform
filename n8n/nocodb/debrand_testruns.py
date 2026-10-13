#!/usr/bin/env python3
"""Целевые прогоны приёмки де-брендинга на [TEST]-контуре (план U4, пункт 3).

План: docs/plans/2026-09-16-001-fix-debrand-prompt-blocks-plan.md

Что проверяем. После замены «Haval Dargo в комплектации Elite» на
«[Модель] в комплектации [Комплектация]» строка-образец в трёх REFERENCE-блоках
перестала быть готовой фразой и стала шаблоном. Вопрос ровно один: подставит ли
модель туда данные клиента — или выведет скобки дословно. Один прогон этого не
доказывает: на побайтово одинаковых входах модель расходится (инцидент #4084 —
2 расхождения из 6 при promptTokens 5152 во всех шести), поэтому прогонов по
десять на сценарий.

ИЗОЛЯЦИЯ. Прогоны идут в `[TEST] AI Agent v3.0`, чья цепочка CRM ведёт на
дев-бота, а не на боевого. Сверх этого:

  * `deal_id` НЕ передаётся. В обработчике `/n8n_callback` дев-бота весь блок
    работы с Bitrix обёрнут в `if bx_url and deal_id:` (main.py:889) — при
    пустом deal_id не выполняется ни `crm.deal.get`, ни `crm.deal.update`, ни
    обновление контакта. Создающих вызовов (`crm.deal.add`, `crm.contact.add`)
    в обработчике нет вовсе.
  * `user_id` заведомо тестовые, с префиксом `test-debrand-`: в БД бота их нет,
    поэтому `UPDATE User ... WHERE platform_user_id = ...` (main.py:856) не
    заденет ни одной строки.
  * Дилер 99 — тестовый дилер дев-стенда, во всех входах, включая сценарии с
    моделями Geely и Belgee: проверяется формулировка, а не сверка бренда.
  * `cached_json` заполнен, поэтому Cache Gate уводит ход мимо `Call Car Search`
    — обращений к складам и каталогам дилеров не будет.

Единственная возможная внешняя запись — алерт в группу (main.py:955), и только
если модель вызовет finalize_deal со статусом visit или call. Запретить вызов
входом нельзя: тулы привязаны к агенту связями графа. Скрипт считает такие
случаи и печатает их отдельно.

Запуск:
  python3 debrand_testruns.py --label runs-after
  python3 debrand_testruns.py --label runs-after --only credit --n 2   # проба
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ENV_PATH = ROOT / ".env"
OUT_DIR = ROOT / "flow" / "lib" / "snapshots"

WEBHOOK = "__OLD_N8N_WEBHOOK_URL_REMOVED__"
DEALER = "99"                      # тестовый дилер дев-стенда
START_CODE = "99TESTDB"            # первые два символа — код дилера

# Реалистичные модели: минимум по одной на Geely, Belgee и Haval.
CARS = [
    ("Geely Monjaro",  "Flagship",  3490000, 3290000, 3190000),
    ("Belgee X50",     "Comfort",   2090000, 1990000, 1930000),
    ("Haval Jolion",   "Elite",     2450000, 2330000, 2270000),
]

QUESTION = {
    "credit":  "а сколько эта машина будет стоить в кредит?",
    "tradein": "а какая цена будет по трейд-ин?",
    "combo":   "сколько выйдет с трейд-ин и в кредит?",
}


def load_env():
    env = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def cached_json(car):
    """Кэш калькулятора. Непустой cached_json уводит ход мимо Car Search."""
    if car is None:                                   # ветка «данных нет»
        return {"complectations": [], "model": "",
                "user_request": {"payment": 30000}, "user_data": {"name": ""}}
    name, trim, msrp, base, ti = car
    return {
        "model": name,
        "user_request": {"payment": 30000, "selected_complectation_id": "11"},
        "user_data": {"name": ""},
        "complectations": [{
            "id": "11", "name": trim, "full_name": f"{name} {trim}",
            "specs": {"engine": "2.0T", "transmission": "АКПП", "drive": "полный"},
            "price": {"MSRP": msrp, "cash_bonus": msrp - base, "base_price": base,
                      "credit_price": base - 100000, "credit_bonus": 100000,
                      "tradein_price": ti, "tradein_bonus": base - ti},
            "credit_calculation": {"status": "standard",
                                   "scenarios": {"минимальный взнос":
                                                 {"down_payment": 900000, "term": 84,
                                                  "monthly_payment": 30000}}},
        }],
    }


def payload(scenario, idx, car):
    uid = f"test-debrand-{scenario}-{idx:02d}"
    model = car[0] if car else ""
    return {
        "action": "chat",
        "user_id": uid,
        "session_id": f"sess_{uid}_{START_CODE}",
        "message": QUESTION[scenario],
        "user_data": {"name": ""},
        "recent_history": [
            {"role": "bot", "text": "Здравствуйте! Я ассистент отдела продаж."},
            {"role": "client", "text": "интересует машина"},
        ],
        "session_context": {
            "request_type": scenario,
            "current_stage": "data_collect",
            "msg_count": 3,
            "collected_data": {"payment": True},
            "flags": {},                               # телефона нет, стоп-слов нет
            "dealer_name": "Тестовый центр 99",
            "dealer_address": "г. Тестов, ул. Проверочная, 99",
            "dealer_hours": "ежедневно с 9:00 до 21:00",
        },
        "qualification_context": {
            "request_type": scenario,
            "payment": 30000,
            "model": model,
            "model_id": None,
            "complectation_id": "11" if car else None,
            "dealer_id": DEALER,
            "cached_json": cached_json(car),
            "deal_id": None,                           # НЕ заполнять: см. docstring
        },
        "data_routing": {},
    }


def post(body, timeout=180):
    req = urllib.request.Request(
        WEBHOOK, method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw, "_unparsed": True}
    return out[0] if isinstance(out, list) and out else out


def main():
    args = sys.argv[1:]
    if "--label" not in args:
        print(__doc__)
        sys.exit(2)
    label = args[args.index("--label") + 1]
    only = args[args.index("--only") + 1] if "--only" in args else None
    n_full = int(args[args.index("--n") + 1]) if "--n" in args else 10
    n_empty = int(args[args.index("--n-empty") + 1]) if "--n-empty" in args else 5

    scenarios = [only] if only else ["credit", "tradein", "combo"]
    rows = []
    idx = 0
    for scenario in scenarios:
        # Основная серия: данные заполнены, бренды чередуются.
        for i in range(n_full):
            idx += 1
            car = CARS[i % len(CARS)]
            rows.append(("full", scenario, idx, car))
        # Контрольная серия для решения по U6: модель и комплектация пусты.
        for i in range(n_empty):
            idx += 1
            rows.append(("empty", scenario, idx, None))

    print(f"прогонов: {len(rows)} "
          f"(с данными {sum(1 for r in rows if r[0] == 'full')}, "
          f"пустых {sum(1 for r in rows if r[0] == 'empty')})")
    print(f"вебхук: {WEBHOOK}\nдилер: {DEALER}\n")

    results = []
    started = time.time()
    for kind, scenario, i, car in rows:
        body = payload(scenario, i, car)
        t0 = time.time()
        try:
            resp = post(body)
            err = None
        except urllib.error.HTTPError as e:
            resp, err = {}, f"HTTP {e.code}: {e.read()[:200]!r}"
        except Exception as e:
            resp, err = {}, f"{type(e).__name__}: {e}"
        text = (resp or {}).get("text") or ""
        results.append({
            "kind": kind, "scenario": scenario, "idx": i,
            "user_id": body["user_id"],
            "car": car[0] if car else None,
            "trim": car[1] if car else None,
            "elapsed_s": round(time.time() - t0, 1),
            "error": err,
            "text": text,
            "session_context_update": (resp or {}).get("session_context_update"),
        })
        flag = "ERR" if err else ("пусто" if not text else f"{len(text)} симв.")
        print(f"  [{len(results):>2}/{len(rows)}] {kind:5} {scenario:8} "
              f"{(car[0] if car else '(без модели)'):16} {flag}")

    out = OUT_DIR / f"testruns_{label}.json"
    out.write_text(json.dumps(
        {"label": label, "webhook": WEBHOOK, "dealer": DEALER,
         "started": started, "results": results},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nсохранено: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
