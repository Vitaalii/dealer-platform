#!/usr/bin/env python3
"""Пакет docs/promts/prompt-edits-link.json — выдача ссылки (ТЗ 2026-08-19).

Продолжение разбора дефекта, по которому применяли prompt-edits-mirror.json.
После той правки ссылка выдаётся, но без обязательного пояснения и с точкой
вплотную к адресу.

Причина пропавшего пояснения — ошибка в самом тексте блока: тактика говорила
«Как именно проходит авторизация в кабинете, НЕ описывай», а пояснение про
персональный доступ лежало только в ПРИМЕРЕ. Модель прочла прямой запрет и
необязательный пример — и выбрала запрет. Формально она права: фраза про
авторизацию и есть описание авторизации.

Три правки в каждом из трёх блоков:
  * пояснение про персональный кабинет и необходимость авторизации перенесено
    из примера в тактику и помечено обязательным;
  * запрет сужен до механики: не описывать код, СМС и звонок — но сказать, что
    вход потребуется, нужно;
  * добавлено правило оформления: после адреса не ставить точку, запятую,
    скобку или кавычку, только пробел или перенос строки.

Абзац про оформление ссылки ВРЕМЕННЫЙ: прорабатывается выдача ссылки кнопкой
или гиперссылкой вместо голого URL; когда решение появится, абзац уйдёт из всех
трёх блоков.

Блок растёт со 121 до ~211 токенов в каждом из трёх сценариев. Осознанно.

ВАЖНО про сверку. Тексты трёх блоков идентичны побайтово и ДО, и ПОСЛЕ правки,
поэтому сверка и запись идут строго по Block_ID: каждый блок ищется в базе по
своему идентификатору, совпадение содержимого как признак не используется
нигде. Иначе три записи были бы неразличимы между собой.

Сверка как в двух предыдущих пакетах: base_hash пустой, поле `was` сравнивается
с живым Content побайтово.

Писатель — из apply_prompt_edits_v308: одна запись — один PATCH, чтение до и
после, стоп на первом расхождении. Активация не трогается.

Запуск:
  python3 apply_prompt_edits_link.py            # diff, ничего не пишет
  python3 apply_prompt_edits_link.py --apply
"""
import json
import sys
from datetime import date
from pathlib import Path

from apply_prompt_edits_v308 import (
    BLOCK_COLS, IDS, WriteMismatch, fetch_all, load_env, write_verified,
)

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "docs" / "promts" / "prompt-edits-link.json"
SNAPSHOT = ROOT / "flow" / "lib" / "snapshots" / "pre_apply_link.json"
EDIT_TAG = f"[ПРАВКА {date(2026, 8, 19).isoformat()}]"
EXPECTED = ["BLK_CREDIT_CABINET_CHOSEN", "BLK_TRADEIN_CABINET_CHOSEN",
            "BLK_COMBO_CABINET_CHOSEN"]


def note_with(reason, current):
    current = (current or "").strip()
    head = f"{EDIT_TAG} {reason}"
    if head in current:
        return current
    return f"{head}\n{current}".strip()


def main():
    apply_mode = "--apply" in sys.argv
    env = load_env()

    pkg = json.loads(PACKAGE.read_text(encoding="utf-8"))
    ids = [b["Block_ID"] for b in pkg["blocks"]]
    if sorted(ids) != sorted(EXPECTED):
        sys.exit(f"ОСТАНОВ: в пакете блоки {ids}, ТЗ ждёт {EXPECTED}")
    if len(set(ids)) != len(ids):
        sys.exit(f"ОСТАНОВ: в пакете повторяются Block_ID — {ids}")
    if pkg.get("map"):
        sys.exit(f"пакет несёт {len(pkg['map'])} строк карты — ТЗ их не предполагает")

    blocks = fetch_all(env, IDS["Prompt_Blocks"])
    by_block = {b["Block_ID"]: b for b in blocks}
    print(f"живые таблицы: блоков {len(blocks)}")

    # ---- сверка `was` побайтово, СТРОГО по Block_ID -------------------------
    print("\nсверка поля was с живым Content (побайтово, каждый блок по своему "
          "Block_ID):")
    mismatched, applied_already, ok = [], [], []
    row_ids = {}
    for b in pkg["blocks"]:
        bid = b["Block_ID"]
        row = by_block.get(bid)
        if row is None:
            sys.exit(f"ОСТАНОВ: блока {bid} нет в NocoDB")
        row_ids[bid] = row["Id"]
        cur = row.get("Content") or ""
        was, now = b["content"]["was"], b["content"]["now"]
        if cur == was:
            ok.append(bid)
            print(f"  совпало      {bid:28s} Id={row['Id']:<5} {len(was)} -> станет {len(now)}")
        elif cur == now:
            applied_already.append(bid)
            print(f"  уже применён {bid:28s} Id={row['Id']}")
        else:
            mismatched.append(bid)
            print(f"  РАСХОЖДЕНИЕ  {bid:28s} was {len(was)} симв., в базе {len(cur)} симв.")

    if len(set(row_ids.values())) != len(row_ids):
        sys.exit(f"ОСТАНОВ: разные Block_ID указывают на одну строку — {row_ids}")
    print(f"  три записи различны: Id = {', '.join(str(v) for v in row_ids.values())}")

    if mismatched:
        sys.exit(f"\nОСТАНОВ: база разошлась с пакетом по блокам {mismatched} — "
                 f"правки надо пересобирать. Ничего не записано.")
    print(f"\nитог сверки: совпало {len(ok)}, уже применено {len(applied_already)}, "
          f"расхождений 0")

    # ---- план ---------------------------------------------------------------
    patches, delta = {}, 0
    for b in pkg["blocks"]:
        bid = b["Block_ID"]
        row = by_block[bid]
        now = b["content"]["now"]
        fields = {"Content": now, "Tokens_est": len(now) // 4,
                  "Notes": note_with(b["reason"], row.get("Notes"))}
        diff = {k: v for k, v in fields.items() if row.get(k) != v}
        if diff:
            patches[bid] = diff
            delta += len(now) - len(row.get("Content") or "")

    print(f"\nблоков к правке: {len(patches)}")
    for bid in patches:
        now = next(b["content"]["now"] for b in pkg["blocks"] if b["Block_ID"] == bid)
        cur = by_block[bid]
        print(f"  {bid:28s} {len(cur.get('Content') or ''):>4} -> {len(now):<4} симв., "
              f"Tokens_est {cur.get('Tokens_est')} -> {len(now) // 4}  (scen={cur.get('Scenario')})")
    print(f"суммарно добавлено: {delta} симв. (~{delta // 4} ток. на три сценария)")

    if not apply_mode:
        print("\n(diff-режим: ничего не записано; --apply)")
        return
    if not patches:
        print("\nнечего писать — всё уже применено")
        return

    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps({"Prompt_Blocks": blocks}, ensure_ascii=False),
                        encoding="utf-8")
    print(f"\nснапшот до записи: {SNAPSHOT}")

    done = []
    print("\nзапись по одной строке с обратной сверкой:")
    try:
        for bid, fields in patches.items():
            write_verified(env, IDS["Prompt_Blocks"], by_block[bid], fields,
                           BLOCK_COLS, bid, done)
            print(f"  ок  {bid:28s} (Id={row_ids[bid]})")
    except WriteMismatch as e:
        sys.exit(f"\nОСТАНОВ ПО РАСХОЖДЕНИЮ\n  {e}")

    print(f"\nзаписано и сверено обратным чтением: {len(done)} строк, расхождений 0")
    print("Активация не трогалась.")


if __name__ == "__main__":
    main()
