#!/usr/bin/env python3
"""U7: скоринг ответов ассистента (базлайн старой архитектуры и прогоны v3.0).

Вход: golden_set.jsonl. По умолчанию оценивается поле actual_reply (базлайн);
для прогона v3.0 тот же файл дополняется полем candidate_reply и оценивается
через --field candidate_reply — обе стороны считаются на идентичном контексте
(teacher-forcing).

Метрики (детерминированные):
  reply-уровень:
    tech_leak    — утечка техники в текст клиента (function_calls, ai_payload, [SPLIT], ```)
    re_greeting  — повторное приветствие «Я ассистент...» не первым ответом AI-секции
                   (симптом потери контекста)
    over_length  — ответ длиннее LENGTH_LIMIT символов
  dialogue-уровень:
    tool_discipline — клиент дал валидный телефон в AI-секции → диалог финализирован
                      (терминальный статус или непустой AI_Report)

LLM-judge (шаги воронки, тон) — отдельный этап, см. docs/eval-methodology.md;
здесь не реализован, чтобы базлайн был воспроизводим без ключей API.

Запуск: python3 score.py golden_set.jsonl [--field candidate_reply] [--json out.json]
"""
import argparse
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path

LENGTH_LIMIT = 800  # символов; щедрый потолок — p95 базлайна = 446 (см. docs/eval-methodology.md)
AI_GREETING = "Я ассистент отдела продаж"
TECH_MARKERS = ("<function_calls", "ai_payload", "[SPLIT]", "```")
TERMINAL_STATUSES = {"visit", "call", "refuse", "ban", "in_progress"}
# тот же регэксп телефона, что в payload_v2 боевого проекта
PHONE_RE = re.compile(r'(\+7|8|7)?[\s\-]?\(?[489][0-9]{2}\)?[\s\-]?[0-9]{3}[\s\-]?[0-9]{2}[\s\-]?[0-9]{2}')


def score_reply(reply: str, is_first_ai_reply: bool) -> dict:
    return {
        "tech_leak": any(m in reply for m in TECH_MARKERS),
        "re_greeting": (AI_GREETING in reply) and not is_first_ai_reply,
        "over_length": len(reply) > LENGTH_LIMIT,
        "length": len(reply),
    }


def score(units: list, field: str) -> dict:
    by_scenario = defaultdict(lambda: defaultdict(list))
    dialogues = defaultdict(list)
    skipped = 0
    for u in units:
        reply = u.get(field)
        if reply is None:
            skipped += 1
            continue
        first = not any(v["turn"] < u["turn"] for v in dialogues[u["session_id"]])
        r = score_reply(reply, is_first_ai_reply=first)
        r["turn"] = u["turn"]
        r["unit"] = u
        dialogues[u["session_id"]].append(r)
        for k in ("tech_leak", "re_greeting", "over_length"):
            by_scenario[u["scenario"]][k].append(r[k])
        by_scenario[u["scenario"]]["length"].append(r["length"])

    # диалоговый уровень: дисциплина финализации при полученном телефоне
    tool_ok, tool_total = defaultdict(int), defaultdict(int)
    for sid, replies in dialogues.items():
        u0 = replies[0]["unit"]
        scen = u0["scenario"]
        client_texts = " ".join(
            h["text"] for h in replies[-1]["unit"]["history"] if h["role"] == "client"
        ) + " " + (replies[-1]["unit"]["client_msg"] or "")
        if PHONE_RE.search(client_texts):
            tool_total[scen] += 1
            finalized = (u0["meta"]["final_status"] in TERMINAL_STATUSES
                         or u0["meta"]["ai_report"])
            if finalized:
                tool_ok[scen] += 1

    report = {"field": field, "skipped_units": skipped, "scenarios": {}}
    for scen, metrics in sorted(by_scenario.items()):
        n = len(metrics["length"])
        lengths = sorted(metrics["length"])
        report["scenarios"][scen] = {
            "replies": n,
            "tech_leak_rate": sum(metrics["tech_leak"]) / n,
            "re_greeting_rate": sum(metrics["re_greeting"]) / n,
            "over_length_rate": sum(metrics["over_length"]) / n,
            "length_p50": lengths[n // 2],
            "length_p95": lengths[min(n - 1, int(n * 0.95))],
            "tool_discipline": (
                f"{tool_ok[scen]}/{tool_total[scen]}" if tool_total[scen] else "n/a"
            ),
        }
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--field", default="actual_reply")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    units = [json.loads(line) for line in open(args.dataset, encoding="utf-8")]
    report = score(units, args.field)

    print(f"поле: {report['field']}; пропущено юнитов без поля: {report['skipped_units']}")
    cols = ["replies", "tech_leak_rate", "re_greeting_rate", "over_length_rate",
            "length_p50", "length_p95", "tool_discipline"]
    print(f"{'scenario':<10}" + "".join(f"{c:>18}" for c in cols))
    for scen, m in report["scenarios"].items():
        row = [f"{m[c]:.3f}" if isinstance(m[c], float) else str(m[c]) for c in cols]
        print(f"{scen:<10}" + "".join(f"{v:>18}" for v in row))

    if args.json:
        json.dump(report, open(args.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"JSON-отчёт: {args.json}")


if __name__ == "__main__":
    main()
