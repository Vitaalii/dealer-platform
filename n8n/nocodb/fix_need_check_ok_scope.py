#!/usr/bin/env python3
"""Сужение интента need_check_ok до стока (ТЗ 2026-08-19, мина 4).

Вопрос комфортности выведен из кредита, трейд-ина и комбо, и все строки карты
need_check_ok в этих трёх сценариях уже сняты (ТЗ 2026-08-18, U4). Но гасить сам
интент нельзя: в СТОКЕ он живой вход в ST_STOCK_DATA_COLLECT (primary-строка
карты активна), а сток кабинетом не трогаем.

Поэтому интент не гасится, а сужается по колонке Scenarios:
    credit,tradein,combo,stock  ->  stock

Почему это работает. Резолвер отбирает кандидатов ровно по этому полю — нода
«Build Prompt» в flow/Tool_ Resolver Core.json (и в [TEST]-контуре, тексты нод
совпадают):

    const expand = (v, u) => (!v || v === "all") ? u : String(v).split(",").map(s => s.trim());
    const intents = $('Get Intents').all().map(i => i.json)
        .filter(i => i.Active)
        .filter(i => expand(i.Scenarios, SCENARIOS).includes(scenario) || i.Scenarios === "all")

После сужения в кредите, трейд-ине и комбо интент просто перестаёт быть
кандидатом, а в стоке продолжает работать как работал.

need_check_not_ok НЕ ТРОГАЕТСЯ ни при каком исходе: он теперь триггер трёх
переиспользованных блоков и должен остаться активным во всех сценариях.

Писатель — из apply_prompt_edits_v308: одна запись — один PATCH, чтение до и
после, стоп на первом расхождении.

Запуск:
  python3 fix_need_check_ok_scope.py            # diff, ничего не пишет
  python3 fix_need_check_ok_scope.py --apply
"""
import json
import sys
from pathlib import Path

from apply_prompt_edits_v308 import (
    IDS, WriteMismatch, fetch_all, load_env, write_verified,
)

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "flow" / "lib" / "snapshots" / "pre_need_check_ok_scope.json"

INTENT_COLS = ["Intent_ID", "Group", "Group_Name", "Description", "Example_Phrases",
               "Scenarios", "Stage_Context", "Priority", "Active", "Version",
               "Source", "Notes"]
TARGET = "need_check_ok"
WAS, NOW = "credit,tradein,combo,stock", "stock"
UNTOUCHED = "need_check_not_ok"


def main():
    apply_mode = "--apply" in sys.argv
    env = load_env()

    intents = fetch_all(env, IDS["Intents"])
    mapping = fetch_all(env, IDS["Intent_Block_Map"])
    by_intent = {i["Intent_ID"]: i for i in intents}

    row = by_intent.get(TARGET)
    if row is None:
        sys.exit(f"ОСТАНОВ: интента {TARGET} нет в таблице")
    cur = str(row.get("Scenarios") or "")
    if cur == NOW:
        print(f"{TARGET}.Scenarios уже {NOW!r} — нечего делать")
        return
    if cur != WAS:
        sys.exit(f"ОСТАНОВ: {TARGET}.Scenarios = {cur!r}, ожидалось {WAS!r}. "
                 f"Поле правили после снятия среза — сузить вслепую нельзя.")

    # Страховка: сужаем только при живой стоковой проводке. Если её нет,
    # значит модель работы интента изменилась и решение надо пересматривать.
    stock_rows = [m for m in mapping if m["Intent_ID"] == TARGET
                  and m.get("Active") and m.get("Scenario_Filter") == "stock"]
    other_rows = [m for m in mapping if m["Intent_ID"] == TARGET
                  and m.get("Active") and m.get("Scenario_Filter") != "stock"]
    print(f"{TARGET}: живых строк карты в стоке {len(stock_rows)}, вне стока {len(other_rows)}")
    for m in stock_rows:
        print(f"    сток: -> {m['Block_ID']} (role={m.get('Role')})")
    if not stock_rows:
        sys.exit(f"ОСТАНОВ: у {TARGET} нет живой строки карты в стоке — сужать не к чему")
    if other_rows:
        for m in other_rows:
            print(f"    ВНЕ СТОКА ЖИВАЯ: -> {m['Block_ID']} ({m.get('Scenario_Filter')})")
        sys.exit(f"ОСТАНОВ: у {TARGET} остались живые строки вне стока — "
                 f"сужение обесточило бы их")

    keep = by_intent.get(UNTOUCHED)
    print(f"{UNTOUCHED} (не трогаем): Active={keep.get('Active')} "
          f"Scenarios={keep.get('Scenarios')!r}")

    print(f"\nправка: {TARGET}.Scenarios  {cur!r} -> {NOW!r}")
    if not apply_mode:
        print("\n(diff-режим: ничего не записано; --apply)")
        return

    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps({"Intents": intents}, ensure_ascii=False),
                        encoding="utf-8")
    print(f"снапшот до записи: {SNAPSHOT}")

    done = []
    try:
        write_verified(env, IDS["Intents"], row, {"Scenarios": NOW},
                       INTENT_COLS, TARGET, done)
    except WriteMismatch as e:
        sys.exit(f"\nОСТАНОВ ПО РАСХОЖДЕНИЮ\n  {e}")
    print(f"записано и сверено обратным чтением: {len(done)} строка, расхождений 0")


if __name__ == "__main__":
    main()
