#!/usr/bin/env python3
"""Частота обрывов генерации по исполнениям n8n (ТЗ 2026-08-19).

Считает, как часто ход завершается обрывом (`finish_reason` не из штатных), и
что при этом сделал сторож. Источник — данные исполнений n8n, поэтому работает
и ДО импорта правки Tool Guard: finish_reason лежит в выводе саб-ноды Agent LM
независимо от того, читает его кто-нибудь или нет.

После импорта правки дополнительно показывает диагностику сторожа
(`guard.aborted`, `guard.lm_signal`) — по ней видно, достаёт ли `$()` до
саб-ноды на этой версии n8n. Если в колонке «сигнал» стоит `empty` или
`unavailable:...` на ходе, где finish_reason реально был, — значит доступ не
работает и правка не срабатывает; поведение при этом остаётся прежним.

ОГРАНИЧЕНИЕ: ретенция исполнений n8n ~2.5 суток, дольше история не живёт.
Для долгого наблюдения нужен отдельный стор — см. раздел «Видимость» в отчёте.

Запуск:
  python3 guard_report.py [--limit 250] [--workflow <id>]
"""
import json
import re
import sys
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKFLOW = "fQAS3wmAjVqrKATM"          # [TEST] AI Agent v3.0
OK_REASONS = {"stop", "tool_calls", "function_call"}


def load_env():
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def api(env, path):
    req = urllib.request.Request(
        env["N8N_API_URL"].rstrip("/") + path,
        headers={"X-N8N-API-KEY": env["N8N_API_KEY"], "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def main():
    env = load_env()
    args = sys.argv[1:]
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 250
    wid = args[args.index("--workflow") + 1] if "--workflow" in args else DEFAULT_WORKFLOW

    lst = api(env, f"/executions?workflowId={wid}&limit={limit}")["data"]
    print(f"исполнений в выдаче: {len(lst)} (ретенция n8n ~2.5 суток)\n")

    rows, counts = [], Counter()
    for meta in lst:
        e = api(env, f"/executions/{meta['id']}?includeData=true")
        rd = (e.get("data") or {}).get("resultData", {}).get("runData", {})
        lm = json.dumps({k: v for k, v in rd.items() if k.startswith("Agent LM")},
                        ensure_ascii=False)
        reasons = re.findall(r'"finish_reason":\s*"(\w+)"', lm)
        last = reasons[-1] if reasons else ""
        aborted = bool(last) and last not in OK_REASONS

        guard = {}
        try:
            guard = rd["Tool Guard"][0]["data"]["main"][0][0]["json"].get("guard") or {}
        except Exception:
            pass
        retry = {}
        try:
            retry = rd["Post Guard"][0]["data"]["main"][0][0]["json"].get("retry_guard") or {}
        except Exception:
            pass

        body = {}
        try:
            w = rd["Webhook"][0]["data"]["main"][0][0]["json"]
            body = w.get("body") or w
        except Exception:
            pass

        # эвристика по форме — ровно та, что зашита в Tool Guard
        try:
            answer = (rd["Agent"][0]["data"]["main"][0][0]["json"].get("output") or "").strip()
        except Exception:
            answer = ""
        shape = bool(answer) and not (
            re.search(r"[.!?…»)\"']$", answer) or re.search(r"https?://\S+$", answer))

        counts["всего"] += 1
        counts["обрыв" if aborted else "штатно"] += 1
        if aborted and guard.get("action") == "retry":
            counts["обрыв → повтор"] += 1
        if aborted and guard and guard.get("action") != "retry":
            counts["обрыв → УШЁЛ КЛИЕНТУ"] += 1
        if retry.get("used_fallback"):
            counts["повтор тоже оборвался → служебная фраза"] += 1
        # калибровка эвристики против настоящего finish_reason
        if answer:
            key = ("совпало" if shape == aborted else
                   ("ложное срабатывание" if shape else "пропущенный обрыв"))
            counts["эвристика: " + key] += 1

        rows.append((meta["id"], (e.get("startedAt") or "")[11:19],
                     str(body.get("message") or "")[:18], ",".join(reasons) or "—",
                     "ДА" if aborted else "нет", "ДА" if shape else "нет",
                     guard.get("action", "—"), str(guard.get("lm_signal", "—"))[:16]))

    print(f"{'exec':>6} {'UTC':9} {'сообщение':18} {'finish_reason':22} {'обрыв':5} "
          f"{'форма':5} {'сторож':7} сигнал")
    for r in rows:
        print(f"{r[0]:>6} {r[1]:9} {r[2]:18} {r[3]:22} {r[4]:5} {r[5]:5} {r[6]:7} {r[7]}")

    print("\nитог:")
    total = counts["всего"] or 1
    for k in ("всего", "штатно", "обрыв", "обрыв → повтор", "обрыв → УШЁЛ КЛИЕНТУ",
              "повтор тоже оборвался → служебная фраза",
              "эвристика: совпало", "эвристика: ложное срабатывание",
              "эвристика: пропущенный обрыв"):
        if k in counts:
            share = f"  ({100 * counts[k] / total:.1f}%)" if k != "всего" else ""
            print(f"  {k:42s} {counts[k]:>4}{share}")
    if not any(r[6] not in ("—", "") for r in rows):
        print("\n  диагностики сторожа нет ни в одном исполнении — правка Tool Guard "
              "ещё не импортирована в n8n.")


if __name__ == "__main__":
    main()
