#!/usr/bin/env python3
"""Чистка Notes от инструкций про уже сделанное (ТЗ 2026-08-19).

Заметки блоков накопили указания владельцу, которые давно выполнены пакетами
правок. Читают их те же люди, что и активируют этапы, поэтому «сделай уже
сделанное» рано или поздно выполняется второй раз. Чистим ДО активации: после
включения сюда никто не вернётся.

Правится ТОЛЬКО поле Notes. Content, Active, Intent_Triggers, Scenario, строки
карты и интенты не трогаются — обратная сверка идёт по всем колонкам таблицы,
поэтому любое постороннее изменение остановит работу.

Три чистки:
  1. G-07_CABINET — снимается «ПЕРЕД АКТИВАЦИЕЙ владельцу удалить правило про
     доведение подбора до выбора комплектации…»: правило удалено пакетом
     2026-08-18. Пометка [ПРАВКА 2026-08-18] и канонический текст новой
     редакции сохраняются дословно.
  2. Шестнадцать черновиков v3.07 — «Драфт — текст дорабатывает владелец»
     заменяется на «Черновик доработан, текст утверждён». У шести отменённых
     (*_FORK, *_CABINET_FALLBACK) дополнительно снимается «активация
     --activate-scenario <сценарий>»: они вычеркнуты из активации, она к ним не
     придёт никогда. Пометки об отмене сохраняются.
  3. Двенадцать блоков v3.03 — снимается «Драфт — доработать в UI; активация
     скриптом add_needcheck_v303.py --activate-drafts»: все двенадцать активны.
     Скрипт чужой, но поле общее.

Замена — точечная, по известной подстроке. Если ожидаемой подстроки в поле нет,
работа останавливается: значит заметку правили и вслепую резать её нельзя.

Писатель — из apply_prompt_edits_v308: одна запись — один PATCH, чтение до и
после, стоп на первом расхождении.

Запуск:
  python3 cleanup_stale_notes.py            # diff, ничего не пишет
  python3 cleanup_stale_notes.py --apply
"""
import json
import sys
from pathlib import Path

from apply_prompt_edits_v308 import (
    BLOCK_COLS, IDS, WriteMismatch, fetch_all, load_env, write_verified,
)

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "flow" / "lib" / "snapshots" / "pre_cleanup_notes.json"

# --- 1. G-07_CABINET -------------------------------------------------------
CABINET_STALE = (
    " ПЕРЕД АКТИВАЦИЕЙ владельцу удалить из текста правило про доведение подбора "
    "до выбора комплектации: комплектация приходит с лендинга на нулевом ходе "
    "(model_id + complectation_id), правило ошибочно и стоит ~75 токенов.")

# --- 2. черновики v3.07 ----------------------------------------------------
V307_STALE = "Драфт — текст дорабатывает владелец; активация --activate-scenario <сценарий>."
V307_FRESH = ("Черновик доработан, текст утверждён (пакет правок 2026-08-18); "
              "активация --activate-scenario <сценарий>.")
# у отменённых — без упоминания активации
V307_FRESH_CANCELLED = "Черновик доработан, текст утверждён (пакет правок 2026-08-18)."
CANCELLED_DRAFTS = {
    "BLK_CREDIT_FORK", "BLK_CREDIT_CABINET_FALLBACK",
    "BLK_TRADEIN_FORK", "BLK_TRADEIN_CABINET_FALLBACK",
    "BLK_COMBO_FORK", "BLK_COMBO_CABINET_FALLBACK",
}

# --- 3. черновики v3.03 ----------------------------------------------------
V303_STALE = ("Драфт — доработать в UI; активация скриптом "
              "add_needcheck_v303.py --activate-drafts. ")

# --- 4. BLK_EVAL_REUSE, канон v3.06 ----------------------------------------
# Снимается только «доработать в UI»: текст блока переписан пакетом трейд-ина
# 2026-08-18. Указание на активацию остаётся — блок действительно ещё
# Active=false, и инструкция верна.
V306_STALE = "Драфт — доработать в UI; активация add_cabinet_v306.py"
V306_FRESH = "Активация add_cabinet_v306.py"

EXPECTED = {"G-07_CABINET": 1, "v3.07": 16, "v3.03": 12, "v3.06": 1}


def plan_notes(blocks):
    """Возвращает {Block_ID: новые Notes} — только там, где есть что менять."""
    out, counts = {}, {"G-07_CABINET": 0, "v3.07": 0, "v3.03": 0, "v3.06": 0}
    for b in blocks:
        bid, notes = b["Block_ID"], (b.get("Notes") or "")
        new = notes
        if bid == "G-07_CABINET" and CABINET_STALE in new:
            new = new.replace(CABINET_STALE, "")
            counts["G-07_CABINET"] += 1
        if V307_STALE in new:
            new = new.replace(
                V307_STALE,
                V307_FRESH_CANCELLED if bid in CANCELLED_DRAFTS else V307_FRESH)
            counts["v3.07"] += 1
        if V303_STALE in new:
            new = new.replace(V303_STALE, "")
            counts["v3.03"] += 1
        if V306_STALE in new:
            new = new.replace(V306_STALE, V306_FRESH)
            counts["v3.06"] += 1
        new = new.strip()
        if new != notes:
            out[bid] = new
    return out, counts


def main():
    apply_mode = "--apply" in sys.argv
    env = load_env()

    blocks = fetch_all(env, IDS["Prompt_Blocks"])
    by_block = {b["Block_ID"]: b for b in blocks}
    print(f"живые таблицы: блоков {len(blocks)}")

    patches, counts = plan_notes(blocks)
    print("\nнайдено к чистке:")
    for k, want in EXPECTED.items():
        got = counts[k]
        mark = "ок" if got == want else f"ОЖИДАЛОСЬ {want}"
        print(f"  {k:14s} {got:>3} [{mark}]")
    already = {k: EXPECTED[k] - counts[k] for k in EXPECTED if counts[k] != EXPECTED[k]}
    if already and not all(counts[k] == 0 for k in already):
        sys.exit(f"ОСТАНОВ: частичное совпадение по {already} — заметки правили, "
                 f"вслепую резать нельзя")
    if already:
        print(f"  (нулевые группы {list(already)} — вероятно, уже вычищены)")

    print(f"\nблоков к правке: {len(patches)}")
    for bid, new in patches.items():
        old = by_block[bid].get("Notes") or ""
        print(f"  {bid:32s} Notes {len(old):>4} -> {len(new):>4} симв.")

    if not apply_mode:
        print("\n(diff-режим: ничего не записано; --apply)")
        return
    if not patches:
        print("\nнечего писать — всё уже вычищено")
        return

    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps({"Prompt_Blocks": blocks}, ensure_ascii=False),
                        encoding="utf-8")
    print(f"\nснапшот до записи: {SNAPSHOT}")

    done = []
    print("\nзапись по одной строке с обратной сверкой:")
    try:
        for bid, new in patches.items():
            write_verified(env, IDS["Prompt_Blocks"], by_block[bid],
                           {"Notes": new}, BLOCK_COLS, bid, done)
            print(f"  ок  {bid}")
    except WriteMismatch as e:
        sys.exit(f"\nОСТАНОВ ПО РАСХОЖДЕНИЮ\n  {e}")

    print(f"\nзаписано и сверено обратным чтением: {len(done)} строк, расхождений 0")
    print("менялось только поле Notes — обратная сверка идёт по всем колонкам.")


if __name__ == "__main__":
    main()
