#!/usr/bin/env python3
"""Расширение справочника интентов (план 2026-07-17-001): черновики + точечные патчи.

Операции над ЖИВОЙ таблицей Intents (канонический источник с 2026-07-13):
  1. POST 4 новых интентов черновиками (Active=false, Version v3.02):
     request_human_handoff, payment_method_change, data_already_provided,
     polite_close_satisfied. Идемпотентно по Intent_ID.
  2. PATCH Example_Phrases десяти существующих интентов — ДОПИСЫВАНИЕ реальных
     фраз из логов к живому значению (уже присутствующие пропускаются;
     правки владельца в живой таблице не затираются).
  3. PATCH Scenarios (+credit) у objection_data_privacy и tool_rerun_needed —
     отдельный флаг --apply-scenarios. Действует на живую таблицу сразу, но
     таблицы v3 читают только v3/[TEST]-воркфлоу — прод (AI Agent v2.0,
     таблица Prompts) не затрагивается.
  4. POST черновых строк Intent_Block_Map (Active=false) для новых интентов —
     маппинг на существующие блоки (U4); идемпотентно по (Intent_ID, Block_ID).
  5. --activate-drafts: включить Active=true 4 новым интентам и их map-строкам
     v3.02 — решение 2026-07-21: схема разворачивается целиком на изолированном
     [TEST]-контуре, без поэтапной активации; прод не трогается вообще.

Без флагов печатает diff и ничего не пишет.
Снапшот живых таблиц (Intents + Prompt_Blocks + Intent_Block_Map) — для
оффлайн-симуляций flow/lib/*.test.mjs — пишется в стабильный путь
flow/lib/snapshots/v3_tables.json (add_v301_draft_blocks.py снимал только
blocks+map в эфемерный scratchpad; этот скрипт — теперь основной снапшотер).

Запуск:
  python3 add_intents_v2.py                  # diff, ничего не пишет
  python3 add_intents_v2.py --snapshot-only  # только свежий снапшот
  python3 add_intents_v2.py --apply          # черновики интентов + PATCH фраз
  python3 add_intents_v2.py --apply-map      # черновые map-строки (U4)
  python3 add_intents_v2.py --apply-scenarios  # +credit (читают только v3/[TEST])
  python3 add_intents_v2.py --activate-drafts  # включить новые интенты + map-строки
  ... [--snapshot PATH]                      # переопределить путь снапшота
"""
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE.parent / ".env"
IDS = json.loads((HERE / "table_ids.json").read_text(encoding="utf-8"))
SNAPSHOT_DEFAULT = HERE.parent / "flow" / "lib" / "snapshots" / "v3_tables.json"
VERSION = "v3.02"

# --- 1. Новые интенты: реальные формулировки из логов (origin-документ 2026-07-16) ---
NEW_INTENTS = [
    {"Intent_ID": "request_human_handoff", "Group": "F", "Group_Name": "Системные",
     "Description": "Клиент просит передать диалог живому менеджеру/сотруднику",
     "Example_Phrases": "очень хорошо переключите на менеджера;переключите на менеджера",
     "Scenarios": "all", "Stage_Context": "all", "Priority": 12,
     "Active": False, "Version": VERSION, "Source": "logs",
     "Notes": "Черновик v3.02 (план 2026-07-17-001). Активация: после map-строки и валидации "
              "[TEST]-контура; до появления собственного блока ответа даёт дефолтный ST_{SCEN}_{STAGE}."},
    {"Intent_ID": "payment_method_change", "Group": "B", "Group_Name": "Вопросы и справочник",
     "Description": "Клиент хочет другой способ оплаты: наличные, без кредита, рассрочка",
     "Example_Phrases": "а можно за наличные купить?;за наличку;без кредита;оплата сразу;"
                        "два миллиона деньгами остальное рассрочка",
     "Scenarios": "all", "Stage_Context": "all", "Priority": 8,
     "Active": False, "Version": VERSION, "Source": "logs",
     "Notes": "Черновик v3.02 (план 2026-07-17-001). Активация: после map-строки и валидации [TEST]-контура."},
    {"Intent_ID": "data_already_provided", "Group": "E", "Group_Name": "Возражения",
     "Description": "Клиент считает, что уже передал запрашиваемые данные ранее в диалоге",
     "Example_Phrases": "почитайте диалог выше. Там все уже есть;все данные я вроде предоставил;"
                        "зачем это делать второй раз?;расчет уже есть",
     "Scenarios": "all", "Stage_Context": "data_collect", "Priority": 9,
     "Active": False, "Version": VERSION, "Source": "logs",
     "Notes": "Черновик v3.02 (план 2026-07-17-001). Активация: после map-строки и валидации [TEST]-контура."},
    {"Intent_ID": "polite_close_satisfied", "Group": "D", "Group_Name": "Закрытие",
     "Description": "Клиент вежливо завершает диалог: удовлетворён ответом, отказа нет",
     "Example_Phrases": "спасибо, я удовлетворён вашим ответом;спасибо, пока это все, что мне нужно;"
                        "я все уже поняла;да я вроде все уточнил",
     "Scenarios": "all", "Stage_Context": "closing", "Priority": 7,
     "Active": False, "Version": VERSION, "Source": "logs",
     "Notes": "Черновик v3.02 (план 2026-07-17-001). Переводит стадию в closing (CLOSE_INTENTS). "
              "Активация: после map-строки и валидации [TEST]-контура."},
]

# --- 2. Дописывание Example_Phrases: только реальные фразы из логов (R5) ---
PHRASE_PATCHES = {
    "pause_requested": ["напишу позже, когда смогу подъехать", "после 15 мая",
                        "ориентируюсь на осень", "выяснится в начале июля"],
    "objection_trust": ["оценка занижена значительно", "вы озвучили сумму минимальную",
                        "проверку по базам сделали за 1 сек?"],
    "objection_price": ["по первоначальному взносу не прохожу", "пока нет первоначального взноса",
                        "рассматриваю без первоначального"],
    "objection_no_phone": ["да причём здесь номер телефона, чтобы вы названивали",
                           "пока не могу разговаривать по телефону"],
    "objection_distance": ["а онлайн можно посчитать?", "дистанционно можно сделать расчёт?",
                           "я сейчас в другом городе, вернусь позже"],
    "competitor_comparison": ["на озоне цена такая, а у вас ещё бонусы и скидки?",
                              "смотрю авто в Москве, предложения по цене интереснее"],
    "cross_scenario_question": ["на тест-драйв сразу записаться можно?", "дайте стоимость киа спортейдж",
                                "стоимость всех комплектаций"],
    "price_hierarchy_question": ["какая скидка за трейд-ин?", "какие скидки персональные есть?",
                                 "сколько переплата в деньгах от цены?"],
    "collect_plate": ["не помню", "марки и года выпуска недостаточно?",
                      "я на работе, машина на стоянке"],
    "schedule_appointment": ["в пятницу", "завтра", "удобно подъехать", "я сама приеду в салон"],
}

# --- 3. Scenarios: возражение о данных и пересчёт возможны и в credit (R3) ---
SCENARIO_PATCHES = {"objection_data_privacy": "credit", "tool_rerun_needed": "credit"}

# --- 4. Черновые map-строки (U4): маппинг на существующие блоки живой таблицы ---
# Block_ID выбраны по живому снапшоту 2026-07-19 (в живой таблице primary-блоки
# мигрировали на BLK_*; локальный CSV отстал):
#   request_human_handoff  -> ST_{SCEN}_CLOSING (primary): передача менеджеру =
#       закрытие + сбор телефона; собственный блок — контент владельца (Deferred).
#   polite_close_satisfied -> ST_{SCEN}_CLOSING (primary): как у других
#       закрывающих интентов (propose_visit и др.).
#   data_already_provided  -> ST_{SCEN}_DATA_COLLECT (primary, только на
#       data_collect): явный безопасный дефолт, пока владелец не наполнит
#       отдельный блок «не переспрашивать, взять из истории».
#   payment_method_change  -> BLK_{SCEN}_REFERENCE (additional, credit/tradein/
#       combo): паттерн справочных вопросов (как price_hierarchy_question);
#       в stock/consult справочных BLK-блоков нет — там дефолт стадии.
_SCEN_ALL = ("credit", "tradein", "combo", "stock", "consult")


def _map_row(intent, block, role, scen, stage="all"):
    return {"Intent_ID": intent, "Block_ID": block, "Role": role,
            "Scenario_Filter": scen, "Stage_Filter": stage,
            "Version_Set": VERSION, "Active": False}


MAP_DRAFTS = (
    [_map_row("request_human_handoff", f"ST_{s.upper()}_CLOSING", "primary", s) for s in _SCEN_ALL]
    + [_map_row("polite_close_satisfied", f"ST_{s.upper()}_CLOSING", "primary", s) for s in _SCEN_ALL]
    + [_map_row("data_already_provided", f"ST_{s.upper()}_DATA_COLLECT", "primary", s, "data_collect")
       for s in _SCEN_ALL]
    + [_map_row("payment_method_change", f"BLK_{s.upper()}_REFERENCE", "additional", s)
       for s in ("credit", "tradein", "combo")]
)


def load_env():
    env = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def api(env, method, path, body=None):
    req = urllib.request.Request(
        env["NOCODB_API_URL"].rstrip("/") + path, method=method,
        headers={"xc-token": env["NOCODB_TOKEN"], "Content-Type": "application/json"},
        data=json.dumps(body).encode("utf-8") if body is not None else None)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def fetch_all(env, table_id):
    rows, offset = [], 0
    while True:
        page = api(env, "GET", f"/api/v2/tables/{table_id}/records?limit=200&offset={offset}")
        chunk = page.get("list", [])
        rows.extend(chunk)
        if page.get("pageInfo", {}).get("isLastPage", True) or not chunk:
            return rows
        offset += len(chunk)


def split_phrases(value):
    return [p.strip() for p in str(value or "").split(";") if p.strip()]


def norm(phrase):
    return " ".join(phrase.casefold().split())


def diff_intents(intents):
    """Возвращает (новые строки, фразовые PATCH'и, сценарные PATCH'и)."""
    by_id = {i["Intent_ID"]: i for i in intents}

    to_post = [r for r in NEW_INTENTS if r["Intent_ID"] not in by_id]

    phrase_updates = []  # (Id, Intent_ID, старое, новое, добавленные)
    for intent_id, phrases in PHRASE_PATCHES.items():
        row = by_id.get(intent_id)
        if row is None:
            print(f"  ⚠️ {intent_id}: нет в живой таблице — фразы пропущены")
            continue
        live = split_phrases(row.get("Example_Phrases"))
        have = {norm(p) for p in live}
        missing = [p for p in phrases if norm(p) not in have]
        if missing:
            phrase_updates.append((row["Id"], intent_id, row.get("Example_Phrases") or "",
                                   ";".join(live + missing), missing))

    scenario_updates = []  # (Id, Intent_ID, старое, новое)
    for intent_id, scen in SCENARIO_PATCHES.items():
        row = by_id.get(intent_id)
        if row is None:
            print(f"  ⚠️ {intent_id}: нет в живой таблице — Scenarios пропущен")
            continue
        live = [s.strip() for s in str(row.get("Scenarios") or "").split(",") if s.strip()]
        if scen not in live and str(row.get("Scenarios")) != "all":
            scenario_updates.append((row["Id"], intent_id, row.get("Scenarios") or "",
                                     ",".join(live + [scen])))
    return to_post, phrase_updates, scenario_updates


def diff_map(mapping):
    have = {(m["Intent_ID"], m["Block_ID"]) for m in mapping}
    return [r for r in MAP_DRAFTS if (r["Intent_ID"], r["Block_ID"]) not in have]


def main():
    env = load_env()
    args = sys.argv[1:]
    snap_path = Path(args[args.index("--snapshot") + 1]) if "--snapshot" in args else SNAPSHOT_DEFAULT

    intents = fetch_all(env, IDS["Intents"])
    print(f"живых интентов: {len(intents)}")

    if "--snapshot-only" not in args:
        to_post, phrase_updates, scenario_updates = diff_intents(intents)
        mapping = fetch_all(env, IDS["Intent_Block_Map"])
        map_to_post = diff_map(mapping)

        print(f"\n— новые интенты (POST, Active=false): {len(to_post)}")
        for r in to_post:
            print(f"    {r['Intent_ID']} (prio {r['Priority']}, {r['Scenarios']}/{r['Stage_Context']})")
        print(f"— PATCH Example_Phrases: {len(phrase_updates)}")
        for _, intent_id, _, _, missing in phrase_updates:
            print(f"    {intent_id}: +{len(missing)}: {'; '.join(missing)}")
        print(f"— PATCH Scenarios (действует сразу; читают только v3/[TEST]): {len(scenario_updates)}")
        for _, intent_id, old, new in scenario_updates:
            print(f"    {intent_id}: {old} -> {new}")
        print(f"— черновые map-строки (POST, Active=false): {len(map_to_post)}")
        for r in map_to_post:
            print(f"    {r['Intent_ID']} -> {r['Block_ID']} ({r['Role']})")

        wrote = False
        if "--apply" in args:
            if to_post:
                api(env, "POST", f"/api/v2/tables/{IDS['Intents']}/records", to_post)
                print(f"\nдобавлено интентов: {len(to_post)} (все Active=false)")
            if phrase_updates:
                api(env, "PATCH", f"/api/v2/tables/{IDS['Intents']}/records",
                    [{"Id": rid, "Example_Phrases": new} for rid, _, _, new, _ in phrase_updates])
                print(f"фразы дописаны: {len(phrase_updates)} интентов")
            wrote = True
        if "--apply-map" in args:
            if map_to_post:
                api(env, "POST", f"/api/v2/tables/{IDS['Intent_Block_Map']}/records", map_to_post)
                print(f"добавлено map-строк: {len(map_to_post)} (все Active=false)")
            wrote = True
        if "--apply-scenarios" in args:
            if scenario_updates:
                api(env, "PATCH", f"/api/v2/tables/{IDS['Intents']}/records",
                    [{"Id": rid, "Scenarios": new} for rid, _, _, new in scenario_updates])
                print(f"Scenarios обновлён: {len(scenario_updates)} интентов")
            wrote = True
        if "--activate-drafts" in args:
            new_ids = {r["Intent_ID"] for r in NEW_INTENTS}
            by_id = {i["Intent_ID"]: i for i in intents}
            intent_patch = [{"Id": by_id[iid]["Id"], "Active": True,
                             "Notes": "Активирован 2026-07-21 для [TEST]-контура "
                                      "(план 2026-07-17-001); прод не затронут."}
                            for iid in sorted(new_ids)
                            if iid in by_id and not by_id[iid].get("Active")]
            if intent_patch:
                api(env, "PATCH", f"/api/v2/tables/{IDS['Intents']}/records", intent_patch)
            map_pairs = {(r["Intent_ID"], r["Block_ID"]) for r in MAP_DRAFTS}
            map_patch = [{"Id": m["Id"], "Active": True} for m in mapping
                         if (m["Intent_ID"], m["Block_ID"]) in map_pairs and not m.get("Active")]
            if map_patch:
                api(env, "PATCH", f"/api/v2/tables/{IDS['Intent_Block_Map']}/records", map_patch)
            print(f"активировано: интентов {len(intent_patch)}, map-строк {len(map_patch)}")
            wrote = True
        if not wrote:
            print("\n(diff-режим: ничего не записано; --apply / --apply-map / --apply-scenarios)")
            snap_path = None  # без записи не пересобираем снапшот
        else:
            intents = fetch_all(env, IDS["Intents"])

    if snap_path:
        snap = {"intents": intents,
                "blocks": fetch_all(env, IDS["Prompt_Blocks"]),
                "map": fetch_all(env, IDS["Intent_Block_Map"])}
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        print(f"снапшот таблиц: {snap_path} ({len(snap['intents'])} интентов, "
              f"{len(snap['blocks'])} блоков, {len(snap['map'])} строк карты)")


if __name__ == "__main__":
    main()
