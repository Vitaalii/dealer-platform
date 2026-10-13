#!/usr/bin/env python3
"""Пакет docs/promts/prompt-edits-prinyal.json — снятие «Принял» (ТЗ 2026-08-19).

Десять блоков диктовали «Принял» дословно как финальный текст, а новая редакция
G-02_VOICE_V2 п.7 это слово запрещает: промпт противоречил сам себе. Три блока
из десяти (T_FINALIZE, BLK_STOCK_PAUSE, BLK_CONSULT_*) относятся к стоку и
консультации — сценариям, которые кабинетом не трогаем; правка там нужна только
ради снятия противоречия с п.7.

Правится ТОЛЬКО реплика клиенту. Механика finalize_deal — статус, next_step,
next_step_time, формат даты — не меняется нигде, и это проверяется отдельно:
  * до записи  — сверкой пакета: текст без закавыченных реплик должен совпасть
                 у was и now побайтово, а набор механических токенов — не
                 измениться;
  * после записи — той же сверкой уже по прочитанному из базы значению.

Писатель — из apply_prompt_edits_v308: одна запись — один PATCH, чтение до и
после, стоп на первом расхождении. Своего протокола записи здесь нет.

Запуск:
  python3 apply_prompt_edits_prinyal.py            # diff, ничего не пишет
  python3 apply_prompt_edits_prinyal.py --apply
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

from apply_prompt_edits_v308 import (
    BLOCK_COLS, IDS, SNAPSHOT_FINGERPRINT, WriteMismatch,
    api, fetch_all, h12, load_env, write_verified,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PACKAGE = ROOT / "docs" / "promts" / "prompt-edits-prinyal.json"
SNAPSHOT = ROOT / "flow" / "lib" / "snapshots" / "pre_apply_prinyal.json"
EDIT_TAG = f"[ПРАВКА {date(2026, 8, 19).isoformat()}]"
EXPECTED_BLOCKS = 10

# Реплика клиенту живёт в «ёлочках». Всё, что снаружи, — механика хода.
QUOTED = re.compile(r"«[^»]*»")
# Токены механики finalize_deal: имя тула, статусы, поля и формат даты.
MECHANICS = re.compile(
    r"finalize_deal|in_progress|refuse|\"call\"|\"visit\"|next_step_time|next_step"
    r"|DD\.MM\.YYYY|\+2 дня|Условие [АБ]")


def mechanics_of(text):
    """Механика хода = текст без реплик + перечень механических токенов."""
    return QUOTED.sub("«…»", text or ""), MECHANICS.findall(text or "")


def check_mechanics(bid, before, after, where):
    """Реплика могла измениться, механика — нет. Иначе стоп."""
    b_skel, b_tok = mechanics_of(before)
    a_skel, a_tok = mechanics_of(after)
    if b_skel != a_skel:
        sys.exit(f"ОСТАНОВ ({where}): {bid} — правка вышла за пределы реплики.\n"
                 f"  было:  {b_skel[:300]}\n  стало: {a_skel[:300]}")
    if b_tok != a_tok:
        sys.exit(f"ОСТАНОВ ({where}): {bid} — изменился набор механических токенов.\n"
                 f"  было:  {b_tok}\n  стало: {a_tok}")


def note_with(reason, current):
    current = (current or "").strip()
    if current.startswith(EDIT_TAG):
        return current
    return f"{EDIT_TAG} {reason}\n{current}".strip()


def main():
    apply_mode = "--apply" in sys.argv
    env = load_env()

    pkg = json.loads(PACKAGE.read_text(encoding="utf-8"))
    if pkg.get("fingerprint") != SNAPSHOT_FINGERPRINT:
        sys.exit(f"отпечаток {pkg.get('fingerprint')} != {SNAPSHOT_FINGERPRINT}")
    if len(pkg["blocks"]) != EXPECTED_BLOCKS:
        sys.exit(f"в пакете {len(pkg['blocks'])} блоков, ТЗ ждёт {EXPECTED_BLOCKS}")
    if pkg.get("map"):
        sys.exit(f"пакет неожиданно несёт {len(pkg['map'])} строк карты — ТЗ их не предполагает")

    blocks = fetch_all(env, IDS["Prompt_Blocks"])
    by_block = {b["Block_ID"]: b for b in blocks}
    print(f"живые таблицы: блоков {len(blocks)}")

    # ---- сверка base_hash + сверка механики ПО ПАКЕТУ ----------------------
    mismatched, applied_already = [], []
    for b in pkg["blocks"]:
        bid = b["Block_ID"]
        row = by_block.get(bid)
        if row is None:
            sys.exit(f"ОСТАНОВ: блока {bid} нет в NocoDB")
        check_mechanics(bid, b["content"]["was"], b["content"]["now"], "сверка пакета")
        cur = row.get("Content") or ""
        if h12(cur) == b["base_hash"]:
            continue
        if cur == b["content"]["now"]:
            applied_already.append(bid)
            continue
        mismatched.append((bid, b["base_hash"], h12(cur)))

    print(f"\nсверка base_hash: блоков {len(pkg['blocks'])}, расхождений {len(mismatched)}, "
          f"уже применено {len(applied_already)}")
    for bid, want, got in mismatched:
        print(f"  РАСХОЖДЕНИЕ: {bid} пакет={want} живое={got}")
    if mismatched:
        sys.exit("\nОСТАНОВ: база менялась после снятия среза — правки надо "
                 "пересобирать. Ничего не записано.")
    print("механика finalize_deal в пакете: у всех 10 блоков вне реплики "
          "текст и токены совпадают")

    # ---- план --------------------------------------------------------------
    patches = {}
    for b in pkg["blocks"]:
        bid = b["Block_ID"]
        row = by_block[bid]
        fields = {"Content": b["content"]["now"],
                  "Tokens_est": len(b["content"]["now"]) // 4,
                  "Notes": note_with(b["reason"], row.get("Notes"))}
        diff = {k: v for k, v in fields.items() if row.get(k) != v}
        if diff:
            patches[bid] = diff

    print(f"\nблоков к правке: {len(patches)}")
    for bid, f in patches.items():
        row = by_block[bid]
        print(f"  {bid:30s} {', '.join(sorted(f))}  "
              f"({len(row.get('Content') or '')} -> {len(f.get('Content', row.get('Content')) or '')} симв.)")

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
            back = api(env, "GET", f"/api/v2/tables/{IDS['Prompt_Blocks']}/records/"
                                   f"{by_block[bid]['Id']}")
            # отдельная проверка ТЗ: механика хода не тронута — сверяем уже
            # прочитанное из базы против исходного текста среза
            check_mechanics(bid, by_block[bid].get("Content"), back.get("Content"),
                            "обратное чтение")
            print(f"  ок  {bid:30s} реплика правлена, механика finalize_deal совпала")
    except WriteMismatch as e:
        sys.exit(f"\nОСТАНОВ ПО РАСХОЖДЕНИЮ\n  {e}")

    print(f"\nзаписано и сверено обратным чтением: {len(done)} строк, расхождений 0")
    print("механика finalize_deal сверена отдельно по каждой записи: расхождений 0")
    print("АКТИВАЦИЯ НЕ ВЫПОЛНЯЛАСЬ: ни один блок не включён.")


if __name__ == "__main__":
    main()
