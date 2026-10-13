#!/usr/bin/env python3
"""U7: парсер транскриптов Dialogues_Archive → золотой набор (teacher-forcing).

Вход:  eval/dialogues_archive.json (экспорт из doc/Dialogues_Archive.numbers)
Выход: eval/golden_set.jsonl — по одной записи на каждый ответ ИИ-ассистента:
       {session_id, scenario, turn, history[], client_msg, actual_reply, meta}

AI-секция диалога определяется по (в порядке приоритета):
  1) клиентское событие-кнопка с __SYSTEM_* (например __SYSTEM_TIMEOUT_HANDOFF__);
  2) первое сообщение бота с приветствием ассистента («Я ассистент отдела продаж»).
Диалоги без AI-секции (чисто скриптовая воронка, Teg «no dialog») в набор не входят.

Запуск: python3 parse_transcripts.py [--stats]
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent

TURN_RE = re.compile(r'^(⚙️|🤖|👤)\s+(\d{2}:\d{2})\s+', re.M)
ROLE = {"⚙️": "system", "🤖": "bot", "👤": "client"}
AI_GREETING = "Я ассистент отдела продаж"


def clean_text(role: str, raw: str) -> str:
    text = raw.strip()
    if role == "bot":
        text = re.sub(r'^\[B\]Бот:\[/B\]\s*', '', text)
    elif role == "client":
        text = re.sub(r'^\[B\]Клиент:\[/B\]\s*', '', text)
    else:
        text = re.sub(r'^\[I\]|\[/I\]$', '', text.strip())
    return text.strip()


def turn_type(role: str, text: str) -> str:
    if role == "client" and text.startswith("[Нажал кнопку:"):
        return "button"
    if role == "bot" and text.startswith("[Отправлено"):
        return "reminder_event"
    return "message"


def parse_transcript(transcript: str) -> list:
    turns = []
    matches = list(TURN_RE.finditer(transcript))
    for i, m in enumerate(matches):
        role = ROLE[m.group(1)]
        end = matches[i + 1].start() if i + 1 < len(matches) else len(transcript)
        text = clean_text(role, transcript[m.end():end])
        if not text:
            continue
        turns.append({"role": role, "time": m.group(2), "type": turn_type(role, text), "text": text})
    return turns


def find_ai_start(turns: list):
    """Индекс первого хода AI-секции или None."""
    for i, t in enumerate(turns):
        if t["role"] == "client" and t["type"] == "button" and "__SYSTEM" in t["text"]:
            return i + 1
    for i, t in enumerate(turns):
        if t["role"] == "bot" and AI_GREETING in t["text"]:
            return i
    return None


def teacher_forcing_units(rec: dict) -> list:
    turns = parse_transcript(rec["Transcript"])
    ai_start = find_ai_start(turns)
    if ai_start is None:
        return []
    units = []
    for i, t in enumerate(turns):
        if i < ai_start or t["role"] != "bot" or t["type"] != "message":
            continue
        history = [
            {"role": h["role"], "type": h["type"], "text": h["text"]}
            for h in turns[:i] if h["role"] in ("client", "bot")
        ]
        last_client = next((h["text"] for h in reversed(turns[:i]) if h["role"] == "client"), None)
        units.append({
            "session_id": rec.get("session_id", ""),
            "scenario": rec.get("Scenario", "unknown"),
            "dealer_code": rec.get("Dealer_Code", ""),
            "prompt_version": rec.get("Prompt_Version", ""),
            "turn": i,
            "history": history,
            "client_msg": last_client,
            "actual_reply": t["text"],
            "meta": {"final_status": rec.get("Final_Status", ""), "teg": rec.get("Teg", ""),
                     "ai_report": bool((rec.get("AI_Report") or "").strip())},
        })
    return units


def main():
    recs = json.load(open(HERE / "dialogues_archive.json", encoding="utf-8"))
    all_units, with_ai, without_ai = [], 0, 0
    for rec in recs:
        units = teacher_forcing_units(rec)
        if units:
            with_ai += 1
            all_units.extend(units)
        else:
            without_ai += 1

    out = HERE / "golden_set.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for u in all_units:
            f.write(json.dumps(u, ensure_ascii=False) + "\n")

    print(f"диалогов: {len(recs)}; с AI-секцией: {with_ai}; без (воронка): {without_ai}")
    print(f"teacher-forcing юнитов (ответов ИИ): {len(all_units)} → {out.name}")
    print("по сценариям:", dict(Counter(u["scenario"] for u in all_units)))
    if "--stats" in sys.argv:
        dlg_per_scen = Counter()
        seen = set()
        for u in all_units:
            key = (u["session_id"], u["scenario"])
            if key not in seen:
                seen.add(key)
                dlg_per_scen[u["scenario"]] += 1
        print("AI-диалогов по сценариям:", dict(dlg_per_scen))


if __name__ == "__main__":
    main()
