"""Сверка «исходники = бой» (план docs/plans/2026-10-09-001, U1). Только чтение из n8n.

Копия рабочего дерева (flow/, nocodb/; без .env) → все генераторы во временной
папке → нормализованное сравнение по именам нод с живыми версиями воркфлоу
(GET /workflows/<id>). Рабочие файлы не трогаются, в n8n ничего не пишется.

Нормализация убирает только то, что выставляет сам n8n (KTD3 плана): серверные
поля верхнего уровня; id и position нод; settings.availableInMCP=false,
settings.callerPolicy=workflowsFromSameOwner, settings.binaryMode=separate (при
отсутствии с другой стороны); workflowId.cachedResultUrl и cachedResultName
(подпись в списке — Q5); пустые группы connections; responseMode=onReceived у
вебхука (значение по умолчанию). Сравнивается рабочая версия: activeVersionId ≠
versionId — ошибка, active ≠ ожидаемому — ошибка.

Известные расхождения — expected_divergences.json (KTD8): запись гасит
расхождение, пока обе стороны равны записанным; дрейф любой стороны — ошибка;
запись без расхождения — «устарела», тоже ошибка.

Запуск: python3 check_sources_vs_live.py [--only <id> ...] [--keep-tmp]
Код: 0 — расхождений нет (кроме ожидаемых), 1 — есть, 2 — ошибка запуска / API.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
EXPECTED = HERE / "expected_divergences.json"

# (файл относительно flow/, id живого воркфлоу, ожидаемый active, примечание)
PAIRS = [
    ("AI Agent v3.0.json", "fQAS3wmAjVqrKATM", True, ""),
    ("Tool - Car Search.json", "mZqzmEnnQiDbvtir", True, ""),
    ("Tool_ Combo Calculator.json", "GD3ZCj8GiVyB7YBw", True, ""),
    ("Tool_ Calc Engine.json", "IzHcJxOxP9SI3LNv", True, ""),
    ("Test_ Calc Engine.json", "a5ZW1e4HESAdOeNk", False, ""),
    ("Tool_ Resolver Core.json", "qsotmM7sbmyckg3E", True, ""),
    ("TOOL_ AUTO.RU EVALUATION.json", "XCPe53sUwk8i9e3T", True, "снимок"),
    ("TOOL_ Save to CRM.json", "vQ-eYIoZ_w85mu17FHGdJ", True, "снимок"),
    ("cabinet-web-entry/WEBHOOK_ Cabinet Tradein Eval.json", "MMhfTj8QJe2UIMSB", True, ""),
    ("cabinet-web-entry/session-logs/Get Session Logs (API).json", "I7r3lWLiKmW1HojQ", True, ""),
    ("test-contour/[TEST] AI Agent v3.0.json", "pkA3mmrvv9XqmZRE", True, ""),
    ("test-contour/[TEST] Tool - Car Search.json", "o3vH8ZVKW5dNMkK2", True, ""),
    ("test-contour/[TEST] Tool_ Combo Calculator.json", "GjppGA6LqjbaqX4G", True, ""),
    ("test-contour/[TEST] Tool_ Calc Engine.json", "1K9aRaJrWRCF4InF", True, ""),
    ("test-contour/[TEST] Tool_ Resolver Core.json", "phecB9jOVAR1DqnX", True, ""),
    ("test-contour/[TEST] TOOL_ AUTO.RU EVALUATION.json", "ToF5ApgK6qkk4mcH", True, ""),
    ("test-contour/[TEST] AI Resolver.json", "F3eluZCNKGAcPwOT", True, "shadow, исключение Q3"),
    ("test-contour/TOOL_ Save to CRM_test.json", "12IRy1rcYOuZzV7m", True, "снимок"),
    ("cabinet-web-entry/[TEST] WEBHOOK_ Cabinet Tradein Eval.json", "99SEbXjbQta3SGSH", True, ""),
    ("cabinet-web-entry/session-logs/[TEST] Get Session Logs (API).json", "7DKkilzDzHtRLkpt", True, ""),
]
# Сгенерированные файлы без живой пары — сознательно (план, таблица пар)
UNPAIRED = {
    "AI Resolver.json": "боевого shadow на пути ai-resolver-shadow нет; живой shadow — [TEST] (Q3)",
    "Tool_ Blocks Lint.json": "исключён (Q4): выключен, нигде не вызывается; генератор новее боя",
    "test-contour/[TEST] Tool_ Blocks Lint.json": "исключён (Q4) вместе с боевым",
    "test-contour/[TEST] Test_ Calc Engine.json": "активной [TEST]-копии в n8n нет",
}

TOP_KEYS = ("name", "nodes", "connections", "settings")
SKIP_NODE_KEYS = ("id", "position")
LONG = 200


# ------------------------------------------------------------ нормализация --
def normalize(wf: dict) -> dict:
    wf = json.loads(json.dumps({k: wf.get(k) for k in TOP_KEYS}))
    s = wf.get("settings") or {}
    if s.get("availableInMCP") is False:
        s.pop("availableInMCP")
    if s.get("callerPolicy") == "workflowsFromSameOwner":
        s.pop("callerPolicy")
    if s.get("binaryMode") == "separate":
        s.pop("binaryMode")
    wf["settings"] = s
    nodes = {}
    for n in wf.get("nodes") or []:
        n = {k: v for k, v in n.items() if k not in SKIP_NODE_KEYS}
        p = n.get("parameters") or {}
        wid = p.get("workflowId")
        if isinstance(wid, dict):
            wid.pop("cachedResultUrl", None)
            wid.pop("cachedResultName", None)
        if n.get("type") == "n8n-nodes-base.webhook" and p.get("responseMode") == "onReceived":
            p.pop("responseMode")
        nodes[n.pop("name")] = n
    wf["nodes"] = nodes
    conns = {}
    for src, types in (wf.get("connections") or {}).items():
        kept = {t: g for t, g in (types or {}).items() if any(grp for grp in g or [])}
        if kept:
            conns[src] = kept
    wf["connections"] = conns
    return wf


def fingerprint(v):
    """Короткое значение — как есть; длинное — sha256[:16] и длина (для файла ожиданий)."""
    s = json.dumps(v, ensure_ascii=False, sort_keys=True)
    if len(s) <= LONG:
        return v
    return {"sha256_16": hashlib.sha256(s.encode()).hexdigest()[:16], "len": len(s)}


def diff(gen, live, path="") -> list:
    """[(путь, ген, бой)] — по ключам словарей; списки и скаляры сравниваются целиком."""
    if isinstance(gen, dict) and isinstance(live, dict):
        out = []
        for k in sorted(set(gen) | set(live)):
            out += diff(gen.get(k, "<нет>"), live.get(k, "<нет>"), f"{path}.{k}" if path else k)
        return out
    return [] if gen == live else [(path, gen, live)]


def describe(path, gen, live) -> str:
    if isinstance(gen, str) and isinstance(live, str) and len(gen) + len(live) > LONG:
        gl, ll = gen.splitlines(), live.splitlines()
        i = next((k for k, (a, b) in enumerate(zip(gl, ll)) if a != b), min(len(gl), len(ll)))
        g = gl[i][:100] if i < len(gl) else "<конец>"
        b = ll[i][:100] if i < len(ll) else "<конец>"
        return f"{path}: строки различаются ({len(gen)} → {len(live)} симв.), строка {i + 1}: ген={g!r} | бой={b!r}"
    sg, sl = json.dumps(gen, ensure_ascii=False), json.dumps(live, ensure_ascii=False)
    cut = lambda x: x if len(x) <= 140 else x[:140] + f"…({len(x)})"
    return f"{path}: ген={cut(sg)} | бой={cut(sl)}"


def match_expected(wid: str, diffs: list, expected: list):
    """→ (неожиданные, погашенные, дрейф, устаревшие) для одного воркфлоу."""
    mine = {e["path"]: e for e in expected if e["id"] == wid}
    unexpected, suppressed, drift = [], [], []
    seen = set()
    for path, g, l in diffs:
        e = mine.get(path)
        if e is None:
            unexpected.append((path, g, l))
            continue
        seen.add(path)
        if fingerprint(g) == e["generated"] and fingerprint(l) == e["live"]:
            suppressed.append((path, e))
        else:
            drift.append((path, g, l, e))
    stale = [e for p, e in mine.items() if p not in seen]
    return unexpected, suppressed, drift, stale


# ------------------------------------------------------------- генерация --
def generate(tmp: Path, creds: dict, live_session_prod: dict):
    ignore = shutil.ignore_patterns("__pycache__", "backups", "archive", ".DS_Store", "node_modules")
    for d in ("flow", "nocodb"):
        shutil.copytree(ROOT / d, tmp / d, ignore=ignore)
    flow, nocodb = tmp / "flow", tmp / "nocodb"
    steps = [
        (flow / "lib", ["build_calc_engine_workflow.py", "--browser-out", str(tmp / "browser-out")]),
        (nocodb, ["build_v3_workflow.py"]),
        (nocodb, ["build_resolver_workflow.py"]),
        (nocodb, ["build_lint_workflow.py"]),
        (nocodb, ["build_test_contour.py"]),
        (flow / "cabinet-web-entry", ["build_eval_webhooks.py"]),
    ]
    for cwd, cmd in steps:
        r = subprocess.run([sys.executable, *cmd], cwd=cwd, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"генератор {cmd[0]} упал:\n{r.stdout[-800:]}\n{r.stderr[-1500:]}")
    # Get Session Logs: сборка из исходного дерева (скрипт импортирует .env —
    # в копию его не кладём), credentials и id нод боевой копии — из n8n.
    import build_session_logs as sl
    out = flow / "cabinet-web-entry" / "session-logs"
    for variant, cfg in sl.COPIES.items():
        wf = sl.build(variant, cfg, creds[cfg["cred"]], live_session_prod if variant == "prod" else None)
        (out / cfg["file"]).write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return flow


# ------------------------------------------------------------------ main --
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--keep-tmp", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, str(HERE))
    from apply_workflow_patch import call  # .env читается только здесь

    def get(path):
        s, r = call("GET", path)
        if s != 200:
            raise RuntimeError(f"GET {path}: {s} {str(r)[:200]}")
        return r

    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))["entries"]
    tmp = Path(tempfile.mkdtemp(prefix="sources-vs-live-"))
    try:
        creds = {c["name"]: c["id"] for c in get("/credentials")["data"]}
        flow = generate(tmp, creds, get("/workflows/I7r3lWLiKmW1HojQ"))
        bad = 0
        for fname, wid, want_active, note in PAIRS:
            if args.only and wid not in args.only:
                continue
            live = get(f"/workflows/{wid}")
            src_file = flow / fname
            if not src_file.exists():
                bad += 1
                print(f"\n[РАСХОЖДЕНИЯ] {live['name']} ({wid}) ← flow/{fname}\n    ✗ исходника нет")
                continue
            gen = json.loads(src_file.read_text(encoding="utf-8"))
            errors = []
            if live.get("active") != want_active:
                errors.append(f"active={live.get('active')}, ожидалось {want_active}")
            if live.get("active") and live.get("activeVersionId") != live.get("versionId"):
                errors.append(f"рабочая версия {str(live.get('activeVersionId'))[:8]} ≠ сохранённой "
                              f"{live['versionId'][:8]} (черновик в редакторе)")
            av = live.get("activeVersion") or {}
            src = dict(live, nodes=av["nodes"], connections=av["connections"]) if av.get("nodes") else live
            unexpected, suppressed, drift, stale = match_expected(wid, diff(normalize(gen), normalize(src)), expected)
            ok = not (errors or unexpected or drift or stale)
            bad += not ok
            tag = "OK" if ok else "РАСХОЖДЕНИЯ"
            print(f"\n[{tag}] {live['name']} ({wid}, {live['versionId'][:8]}) ← flow/{fname}"
                  + (f" — {note}" if note else ""))
            for e in errors:
                print(f"    ✗ {e}")
            for d in unexpected:
                print(f"    ✗ {describe(*d)}")
            for path, g, l, e in drift:
                print(f"    ✗ ожидание {e['decision']} сдвинулось: {describe(path, g, l)}")
            for e in stale:
                print(f"    ✗ ожидание {e['decision']} устарело: {e['path']} — расхождения больше нет, удалите запись")
            for path, e in suppressed:
                print(f"    ≈ ожидаемо ({e['decision']}): {path} — {e['reason']}")
        if not args.only:
            print("\nБез живой пары (сознательно):")
            for f, why in UNPAIRED.items():
                print(f"    · flow/{f} — {why}")
        print(f"\nИтог: {'расхождений нет' if not bad else f'воркфлоу с расхождениями: {bad}'}"
              " (кроме записанных в expected_divergences.json)")
        return 1 if bad else 0
    finally:
        if args.keep_tmp:
            print(f"временная папка: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # сеть, API, упавший генератор — не «зелёный» результат
        print(f"ОШИБКА: {e}", file=sys.stderr)
        sys.exit(2)
