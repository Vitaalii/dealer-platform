"""Точечная правка живого воркфлоу n8n через публичный API (разрешено владельцем 2026-09-30).

Порядок: чтение живого воркфлоу → проверка, что каждая правимая часть РАВНА
ожидаемому «до» (иначе стоп, ничего не пишется) → PUT → повторное чтение →
diff против состояния до записи. Без --apply — только проверка и план.

settings.binaryMode схема API не принимает — поле в PUT не передаётся. При
ОБНОВЛЕНИИ существующего воркфлоу сервер сохраняет прежнее значение (проверено
2026-09-30 на обеих [TEST]-копиях: binaryMode "separate" на месте). Сервер
явно проставляет callerPolicy "workflowsFromSameOwner" — это действовавшее
значение по умолчанию. Запись в АКТИВНЫЙ воркфлоу сразу становится рабочей
версией (activeVersionId следует за versionId), отдельной публикации нет.

Правки (PATCHES) — по имени ноды:
  code:<файл>          — jsCode целиком; «до» = flow/lib/<тот же файл> или --expect-code;
  where:<старое>|<новое> — options.where ноды NocoDB;
  returnAll / returnAllOff — parameters.returnAll = true / удалить ключ;
  webhookAuth / webhookAuthOff — Header Auth на ноде Webhook (credential) / снять;
  executeOnce / executeOnceOff — флаг ноды executeOnce = true / снять;
  httpHeaderCred / httpHeaderCredOff — заголовок из credential httpHeaderAuth в
                         HTTP-ноде (genericCredentialType) / снять.
Наборы *-rollback возвращают состояние до правки (обратная проверка «до»).

Примеры:
  python3 apply_workflow_patch.py --wf o3vH8ZVKW5dNMkK2 --set test-car-n4 [--apply]
  python3 apply_workflow_patch.py --wf GD3ZCj8GiVyB7YBw --set prod-combo-n5 [--apply]
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
ENTRY = HERE.parent

PROD_LIVE_CAR = ENTRY / "live" / "car_search_prepare.prod-live.js"   # бой до правки стока
# Состояния, которые до 2026-10-09 жили в flow/lib (теперь там — боевой код = active-field):
STOCK_CAR = ENTRY / "live" / "car_search_prepare.stock.js"           # бой «сток» 10-05 (до N4)
PRE_N5_COMBO = ENTRY / "live" / "combo_prepare.pre-n5.js"            # бой Combo до N5
N4_CAR = ENTRY / "n4-min-down-payment" / "car_search_prepare.js"
N4_STOCK_CAR = ENTRY / "stock-slug-active" / "car_search_prepare.n4.js"
OLD_WHERE = "=(Model_Pattern,like,%{{ $('Get Catalog').first().json.Id }}%)"
NEW_WHERE = "=(Model_Pattern,like,%{{ $('Get Catalog').first().json['Model id'] }}%)"

TEST_INBOUND = {"id": "8WHmaHbGppnSXL9F", "name": "N8N Inbound Secret [TEST]"}
PROD_INBOUND = {"id": "CfzDWuMkUZABPQnM", "name": "N8N Inbound Secret [PROD]"}
PROD_CALLBACK = {"id": "0XEKTDr5enPFJ3wg", "name": "Bot Callback Secret [PROD]"}
TEST_CALLBACK = {"id": "Xesa45sdzRipKset", "name": "Bot Callback Secret [TEST]"}

SETS = {
    # каналы бот ↔ n8n, ДЕВ-контур (docs/handoff/2026-10-03-n8n-webhook-auth-bot-plan.md)
    "test-agent-auth": [("Webhook", "webhookAuth", None, TEST_INBOUND)],          # [TEST] AI Agent v3.0
    "test-shadow-auth": [("Webhook", "webhookAuth", None, TEST_INBOUND)],         # AI Resolver (shadow), путь -test
    "test-save-crm-callback": [("HTTP Request", "httpHeaderCred", None, TEST_CALLBACK)],  # TOOL: Save to CRM_test
    "test-agent-auth-rollback": [("Webhook", "webhookAuthOff", TEST_INBOUND, None)],
    "test-shadow-auth-rollback": [("Webhook", "webhookAuthOff", TEST_INBOUND, None)],
    "test-save-crm-callback-rollback": [("HTTP Request", "httpHeaderCredOff", TEST_CALLBACK, None)],
    # [TEST] Tool - Car Search: N4
    "test-car-n4": [
        ("Prepare Data", "code", PROD_LIVE_CAR, N4_CAR),
        ("Get Program", "where", OLD_WHERE, NEW_WHERE),
        ("Get Rates", "returnAll", None, True),
    ],
    # [TEST] Tool: Combo Calculator: N4 + N5
    "test-combo-n4n5": [
        ("Prepare Combo", "code", PRE_N5_COMBO, ENTRY / "n5-combo-price-old" / "combo_prepare.js"),
        ("Get Program", "where", OLD_WHERE, NEW_WHERE),
        ("Get Rates", "returnAll", None, True),
    ],
    # боевой Tool: Combo Calculator: только N5 (по отдельной команде владельца)
    "prod-combo-n5": [
        ("Prepare Combo", "code", PRE_N5_COMBO, ENTRY / "n5-combo-price-old" / "combo_prepare.n5-only.js"),
    ],
    # === БОЙ, только по отдельной команде владельца ===
    # с релизом веб-входа бота: N4 на Car Search; на Combo — N4 поверх уже включённого N5
    # сток (2026-10-05): бой — до релиза веб-входа, по команде; [TEST] — сейчас поверх N4
    "prod-car-stock": [
        ("Prepare Data", "code", PROD_LIVE_CAR, STOCK_CAR),
        ("Get Stock", "executeOnce", None, True),
    ],
    "test-car-stock": [
        ("Prepare Data", "code", N4_CAR, N4_STOCK_CAR),
        ("Get Stock", "executeOnce", None, True),
    ],
    # Active в Combo (2026-10-05): [TEST] — поверх N4+N5; бой — поверх N5, по команде
    "test-combo-active": [
        ("Prepare Combo", "code", ENTRY / "n5-combo-price-old" / "combo_prepare.js",
         ENTRY / "stock-slug-active" / "combo_prepare.n4n5.js"),
    ],
    "prod-combo-active": [
        ("Prepare Combo", "code", ENTRY / "n5-combo-price-old" / "combo_prepare.n5-only.js",
         ENTRY / "stock-slug-active" / "combo_prepare.n5.js"),
    ],
    "test-combo-active-rollback": [
        ("Prepare Combo", "code", ENTRY / "stock-slug-active" / "combo_prepare.n4n5.js",
         ENTRY / "n5-combo-price-old" / "combo_prepare.js"),
    ],
    "prod-combo-active-rollback": [
        ("Prepare Combo", "code", ENTRY / "stock-slug-active" / "combo_prepare.n5.js",
         ENTRY / "n5-combo-price-old" / "combo_prepare.n5-only.js"),
    ],
    # N4 на бою — поверх уже включённого стока
    "prod-car-n4": [
        ("Prepare Data", "code", STOCK_CAR, N4_STOCK_CAR),
        ("Get Program", "where", OLD_WHERE, NEW_WHERE),
        ("Get Rates", "returnAll", None, True),
    ],
    # N4 на Combo — поверх уже включённых N5 и Active
    "prod-combo-n4": [
        ("Prepare Combo", "code", ENTRY / "stock-slug-active" / "combo_prepare.n5.js",
         ENTRY / "stock-slug-active" / "combo_prepare.n4n5.js"),
        ("Get Program", "where", OLD_WHERE, NEW_WHERE),
        ("Get Rates", "returnAll", None, True),
    ],
    # после релиза бота: защита каналов (docs/handoff/2026-10-03-n8n-webhook-auth-bot-plan.md)
    "prod-agent-auth": [("Webhook", "webhookAuth", None, PROD_INBOUND)],                 # AI Agent v3.0
    "prod-save-crm-callback": [("HTTP Request", "httpHeaderCred", None, PROD_CALLBACK)],  # TOOL: Save to CRM
    "prod-car-stock-rollback": [
        ("Prepare Data", "code", STOCK_CAR, PROD_LIVE_CAR),
        ("Get Stock", "executeOnceOff", True, None),
    ],
    "test-car-stock-rollback": [
        ("Prepare Data", "code", N4_STOCK_CAR, N4_CAR),
        ("Get Stock", "executeOnceOff", True, None),
    ],
    "prod-car-n4-rollback": [
        ("Prepare Data", "code", N4_STOCK_CAR, STOCK_CAR),
        ("Get Program", "where", NEW_WHERE, OLD_WHERE),
        ("Get Rates", "returnAllOff", True, None),
    ],
    "prod-combo-n4-rollback": [
        ("Prepare Combo", "code", ENTRY / "stock-slug-active" / "combo_prepare.n4n5.js",
         ENTRY / "stock-slug-active" / "combo_prepare.n5.js"),
        ("Get Program", "where", NEW_WHERE, OLD_WHERE),
        ("Get Rates", "returnAllOff", True, None),
    ],
    "prod-agent-auth-rollback": [("Webhook", "webhookAuthOff", PROD_INBOUND, None)],
    "prod-save-crm-callback-rollback": [("HTTP Request", "httpHeaderCredOff", PROD_CALLBACK, None)],
    # колонка активности — настройка дилера (data_routing.catalog_active_field, 2026-10-06);
    # [TEST] и бой сейчас на одних и тех же файлах (N4 + сток / N4 + N5 + Active)
    "test-car-active-field": [("Prepare Data", "code", N4_STOCK_CAR, ENTRY / "active-field" / "car_search_prepare.js")],
    "test-combo-active-field": [("Prepare Combo", "code", ENTRY / "stock-slug-active" / "combo_prepare.n4n5.js",
                                 ENTRY / "active-field" / "combo_prepare.js")],
    "prod-car-active-field": [("Prepare Data", "code", N4_STOCK_CAR, ENTRY / "active-field" / "car_search_prepare.js")],
    "prod-combo-active-field": [("Prepare Combo", "code", ENTRY / "stock-slug-active" / "combo_prepare.n4n5.js",
                                 ENTRY / "active-field" / "combo_prepare.js")],
    "test-car-active-field-rollback": [("Prepare Data", "code", ENTRY / "active-field" / "car_search_prepare.js", N4_STOCK_CAR)],
    "test-combo-active-field-rollback": [("Prepare Combo", "code", ENTRY / "active-field" / "combo_prepare.js",
                                          ENTRY / "stock-slug-active" / "combo_prepare.n4n5.js")],
    "prod-car-active-field-rollback": [("Prepare Data", "code", ENTRY / "active-field" / "car_search_prepare.js", N4_STOCK_CAR)],
    "prod-combo-active-field-rollback": [("Prepare Combo", "code", ENTRY / "active-field" / "combo_prepare.js",
                                          ENTRY / "stock-slug-active" / "combo_prepare.n4n5.js")],
    # откаты
    "test-car-n4-rollback": [
        ("Prepare Data", "code", N4_CAR, PROD_LIVE_CAR),
        ("Get Program", "where", NEW_WHERE, OLD_WHERE),
        ("Get Rates", "returnAllOff", True, None),
    ],
    "test-combo-n4n5-rollback": [
        ("Prepare Combo", "code", ENTRY / "n5-combo-price-old" / "combo_prepare.js", PRE_N5_COMBO),
        ("Get Program", "where", NEW_WHERE, OLD_WHERE),
        ("Get Rates", "returnAllOff", True, None),
    ],
    "prod-combo-n5-rollback": [
        ("Prepare Combo", "code", ENTRY / "n5-combo-price-old" / "combo_prepare.n5-only.js", PRE_N5_COMBO),
    ],
}


def load_env():
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v.strip().strip('"')
    return env


ENV = load_env()
API = ENV["N8N_API_URL"].rstrip("/")
HEADERS = {"X-N8N-API-KEY": ENV["N8N_API_KEY"], "Content-Type": "application/json"}


def call(method, path, body=None):
    req = urllib.request.Request(API + path, method=method, headers=HEADERS,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:500].decode("utf8", "replace")


def node(wf, name):
    found = [n for n in wf["nodes"] if n["name"] == name]
    if len(found) != 1:
        sys.exit(f"СТОП: нода «{name}» найдена {len(found)} раз")
    return found[0]


def plan(wf, patches):
    """Проверяет «до» и возвращает новые nodes. Любое расхождение — стоп."""
    nodes = json.loads(json.dumps(wf["nodes"]))
    wf2 = {"nodes": nodes}
    for name, kind, before, after in patches:
        p = node(wf2, name)["parameters"]
        if kind == "code":
            cur = p.get("jsCode", "")
            if cur.strip() != Path(before).read_text(encoding="utf-8").strip():
                sys.exit(f"СТОП: код «{name}» не совпадает с {Path(before).name} — живой воркфлоу правили вне репозитория")
            p["jsCode"] = Path(after).read_text(encoding="utf-8")
        elif kind == "where":
            cur = (p.get("options") or {}).get("where")
            if cur != before:
                sys.exit(f"СТОП: Where «{name}» = {cur!r}, ожидалось {before!r}")
            p["options"]["where"] = after
        elif kind == "returnAll":
            if p.get("returnAll"):
                sys.exit(f"СТОП: у «{name}» returnAll уже включён")
            p["returnAll"] = True
        elif kind == "executeOnce":
            n = node(wf2, name)
            if n.get("executeOnce") is True:
                sys.exit(f"СТОП: у «{name}» executeOnce уже включён")
            n["executeOnce"] = True
        elif kind == "executeOnceOff":
            n = node(wf2, name)
            if n.get("executeOnce") is not True:
                sys.exit(f"СТОП: у «{name}» executeOnce не включён — откатывать нечего")
            n.pop("executeOnce")
        elif kind == "webhookAuth":
            n = node(wf2, name)
            if p.get("authentication", "none") != "none" or n.get("credentials"):
                sys.exit(f"СТОП: у «{name}» аутентификация уже задана: {p.get('authentication')}")
            p["authentication"] = "headerAuth"
            n["credentials"] = {"httpHeaderAuth": dict(after)}
        elif kind == "webhookAuthOff":
            n = node(wf2, name)
            if p.get("authentication") != "headerAuth" or (n.get("credentials") or {}).get("httpHeaderAuth", {}).get("id") != before["id"]:
                sys.exit(f"СТОП: у «{name}» не та аутентификация — откатывать нечего")
            p.pop("authentication")
            n.pop("credentials")
        elif kind == "httpHeaderCred":
            n = node(wf2, name)
            if p.get("authentication", "none") != "none" or n.get("credentials"):
                sys.exit(f"СТОП: у «{name}» аутентификация уже задана: {p.get('authentication')}")
            p["authentication"] = "genericCredentialType"
            p["genericAuthType"] = "httpHeaderAuth"
            n["credentials"] = {"httpHeaderAuth": dict(after)}
        elif kind == "httpHeaderCredOff":
            n = node(wf2, name)
            if (n.get("credentials") or {}).get("httpHeaderAuth", {}).get("id") != before["id"]:
                sys.exit(f"СТОП: у «{name}» не тот credential — откатывать нечего")
            p.pop("authentication"); p.pop("genericAuthType", None)
            n.pop("credentials")
        elif kind == "returnAllOff":
            if p.get("returnAll") is not True:
                sys.exit(f"СТОП: у «{name}» returnAll не включён — откатывать нечего")
            p.pop("returnAll")
        else:
            sys.exit(f"неизвестная правка {kind}")
    return nodes


def diff(before, after):
    out = []
    bn = {n["name"]: n for n in before["nodes"]}
    an = {n["name"]: n for n in after["nodes"]}
    for name in sorted(set(bn) | set(an)):
        b, a = bn.get(name), an.get(name)
        if b is None or a is None:
            out.append(f"нода «{name}»: {'добавлена' if b is None else 'удалена'}")
            continue
        for key in sorted(set(b) | set(a)):
            if key == "parameters":
                for pk in sorted(set(b["parameters"]) | set(a["parameters"])):
                    if b["parameters"].get(pk) != a["parameters"].get(pk):
                        bv, av = b["parameters"].get(pk), a["parameters"].get(pk)
                        if pk == "jsCode":
                            out.append(f"нода «{name}».jsCode: {len(bv or '')} → {len(av or '')} симв.")
                        else:
                            out.append(f"нода «{name}».{pk}: {json.dumps(bv, ensure_ascii=False)} → {json.dumps(av, ensure_ascii=False)}")
            elif b.get(key) != a.get(key):
                out.append(f"нода «{name}».{key}: {b.get(key)!r} → {a.get(key)!r}")
    if before["connections"] != after["connections"]:
        out.append("connections: ИЗМЕНЕНЫ")
    for k in sorted(set(before["settings"]) | set(after["settings"])):
        if before["settings"].get(k) != after["settings"].get(k):
            out.append(f"settings.{k}: {before['settings'].get(k)!r} → {after['settings'].get(k)!r}")
    for k in ("name", "active"):
        if before.get(k) != after.get(k):
            out.append(f"{k}: {before.get(k)!r} → {after.get(k)!r}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wf", required=True)
    ap.add_argument("--set", required=True, choices=sorted(SETS))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    s, before = call("GET", f"/workflows/{args.wf}")
    if s != 200:
        sys.exit(f"GET {s}: {before}")
    print(f"{before['name']} ({args.wf}) active={before['active']} versionId={before['versionId'][:8]}")
    nodes = plan(before, SETS[args.set])
    print("проверка «до»: OK —", ", ".join(f"{n}:{k}" for n, k, *_ in SETS[args.set]))
    if not args.apply:
        print("dry-run: запись не выполнялась")
        return
    settings = {k: v for k, v in before["settings"].items() if k != "binaryMode"}
    s, r = call("PUT", f"/workflows/{args.wf}",
                {"name": before["name"], "nodes": nodes, "connections": before["connections"], "settings": settings})
    if s != 200:
        sys.exit(f"PUT {s}: {r}")
    s, after = call("GET", f"/workflows/{args.wf}")
    print(f"после: active={after['active']} versionId={after['versionId'][:8]} "
          f"activeVersionId={(after.get('activeVersionId') or '')[:8]}")
    print("diff:")
    for line in diff(before, after):
        print("  ", line)


if __name__ == "__main__":
    main()
