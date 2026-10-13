#!/usr/bin/env python3
"""Пакет docs/promts/prompt-edits-dedup.json — снятие дублей (ТЗ 2026-08-19).

Одни и те же правила лежали в нескольких блоках, попадающих в ОДИН
компилированный промпт: в кредите развилка описывалась в пяти блоках, ветка при
пустом cabinet_url — в четырёх, правила про телефон — в пяти. Каждое правило
остаётся ровно в одном месте; ожидаемая экономия ~330 токенов.

Особенность пакета: base_hash ПУСТОЙ. Суммы считались от среза 2026-08-15, а эти
пять блоков уже изменены нашими правками 2026-08-18 — старые суммы к ним
неприменимы, а алгоритм пересчёта живёт в симуляторе владельца. Поэтому сверка
идёт по полю `was`: оно сравнивается с живым Content ПОБАЙТОВО. Это строже
хеша — сверяется сам текст, а не его отпечаток. Не совпало хотя бы у одного
блока — стоп, ничего не пишется.

Писатель — из apply_prompt_edits_v308: одна запись — один PATCH, чтение до и
после, стоп на первом расхождении.

Запуск:
  python3 apply_prompt_edits_dedup.py            # diff, ничего не пишет
  python3 apply_prompt_edits_dedup.py --apply
"""
import json
import sys
from datetime import date
from pathlib import Path

from apply_prompt_edits_v308 import (
    BLOCK_COLS, IDS, WriteMismatch, fetch_all, load_env, write_verified,
)

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "docs" / "promts" / "prompt-edits-dedup.json"
SNAPSHOT = ROOT / "flow" / "lib" / "snapshots" / "pre_apply_dedup.json"
EDIT_TAG = f"[ПРАВКА {date(2026, 8, 19).isoformat()}]"
EXPECTED_BLOCKS = 5


def note_with(reason, current):
    current = (current or "").strip()
    if current.startswith(EDIT_TAG):
        return current
    return f"{EDIT_TAG} {reason}\n{current}".strip()


def main():
    apply_mode = "--apply" in sys.argv
    env = load_env()

    pkg = json.loads(PACKAGE.read_text(encoding="utf-8"))
    if len(pkg["blocks"]) != EXPECTED_BLOCKS:
        sys.exit(f"в пакете {len(pkg['blocks'])} блоков, ТЗ ждёт {EXPECTED_BLOCKS}")
    if pkg.get("map"):
        sys.exit(f"пакет несёт {len(pkg['map'])} строк карты — ТЗ их не предполагает")

    blocks = fetch_all(env, IDS["Prompt_Blocks"])
    by_block = {b["Block_ID"]: b for b in blocks}
    print(f"живые таблицы: блоков {len(blocks)}")

    # ---- сверка `was` побайтово --------------------------------------------
    mismatched, applied_already, ok = [], [], []
    print("\nсверка поля was с живым Content (побайтово):")
    for b in pkg["blocks"]:
        bid = b["Block_ID"]
        row = by_block.get(bid)
        if row is None:
            sys.exit(f"ОСТАНОВ: блока {bid} нет в NocoDB")
        cur = row.get("Content") or ""
        was, now = b["content"]["was"], b["content"]["now"]
        if cur == was:
            ok.append(bid)
            print(f"  совпало      {bid:24s} {len(was):>5} симв. -> станет {len(now)}")
        elif cur == now:
            applied_already.append(bid)
            print(f"  уже применён {bid:24s}")
        else:
            mismatched.append((bid, len(was), len(cur)))
            print(f"  РАСХОЖДЕНИЕ  {bid:24s} was {len(was)} симв., в базе {len(cur)} симв.")

    if mismatched:
        print()
        for bid, lw, lc in mismatched:
            print(f"  {bid}: текст в базе не равен полю was пакета")
        sys.exit("\nОСТАНОВ: база разошлась с пакетом — правки надо пересобирать. "
                 "Ничего не записано.")
    print(f"\nитог сверки: совпало {len(ok)}, уже применено {len(applied_already)}, "
          f"расхождений 0")

    # ---- план ---------------------------------------------------------------
    patches, saved = {}, 0
    for b in pkg["blocks"]:
        bid = b["Block_ID"]
        row = by_block[bid]
        now = b["content"]["now"]
        fields = {"Content": now, "Tokens_est": len(now) // 4,
                  "Notes": note_with(b["reason"], row.get("Notes"))}
        diff = {k: v for k, v in fields.items() if row.get(k) != v}
        if diff:
            patches[bid] = diff
            saved += len(row.get("Content") or "") - len(now)

    print(f"\nблоков к правке: {len(patches)}")
    for bid in patches:
        row, now = by_block[bid], next(
            b["content"]["now"] for b in pkg["blocks"] if b["Block_ID"] == bid)
        print(f"  {bid:24s} {len(row.get('Content') or ''):>5} -> {len(now):<5} симв.")
    print(f"суммарно снято: {saved} симв. (~{saved // 4} ток. по формуле Tokens_est)")

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
            print(f"  ок  {bid}")
    except WriteMismatch as e:
        sys.exit(f"\nОСТАНОВ ПО РАСХОЖДЕНИЮ\n  {e}")

    print(f"\nзаписано и сверено обратным чтением: {len(done)} строк, расхождений 0")
    print("АКТИВАЦИЯ НЕ ВЫПОЛНЯЛАСЬ этим скриптом.")


if __name__ == "__main__":
    main()
