#!/usr/bin/env python3
"""Пакет docs/promts/prompt-edits-mirror.json — снятие конфликта правил (ТЗ 2026-08-19).

Дефект, вскрытый тестированием (исполнение n8n 3218): Александр задал развилку,
клиент ответил односложно «оонлайн». Резолвер отработал верно (choose_cabinet),
BLK_CREDIT_CABINET_CHOSEN и G-07_CABINET в промпт попали, cabinet_url была
заполнена и дошла до модели — но вместо ссылки модель переспросила.

Причина — конфликт правил ВНУТРИ промпта, а не сбой обвязки: G-02_VOICE_V2 п.1
(зеркальность) требует на короткую реплику отвечать одним предложением, а
G-06_CHECKLIST п.2 повторяет то же требование ПОСЛЕДНИМ, что читает модель.
Выдача ссылки в такую форму не влезает, и модель выбросила именно ссылку.

Пять правок:
  G-02_VOICE_V2          п.1 ослаблен: зеркальность остаётся правилом о манере и
                         перестаёт отменять обязательное содержание ответа;
  G-06_CHECKLIST         п.2 ослаблен: проверка на избыточность, а не на длину;
  ST_CREDIT_TOOL_NEEDED  возвращена ветка «выбор уже сделан, веди по нему, не
  ST_CREDIT_DATA_COLLECT переспрашивай» — при дедупликации срезана ошибочно;
  G-07_CABINET           порог выдачи ссылки смягчён: косвенный ответ («онлайн»,
                         «сам посмотрю», «второй вариант», «на сайте») считается
                         выбором кабинета.

Объём растёт на ~107 токенов: часть экономии от дедупликации возвращается
осознанно.

ВНИМАНИЕ: G-02_VOICE_V2 и G-06_CHECKLIST имеют Scenario=all — ослабление
ограничения на длину ответа действует во всех пяти сценариях, включая сток и
консультацию. Решение владельца, принято осознанно.

Сверка как в пакете дедупликации: base_hash пустой (суммы среза 2026-08-15 к
этим блокам неприменимы — они уже правлены), поэтому поле `was` сравнивается с
живым Content ПОБАЙТОВО. Это строже хеша: сверяется текст, а не отпечаток.

Писатель — из apply_prompt_edits_v308: одна запись — один PATCH, чтение до и
после, стоп на первом расхождении. Активация не трогается: она уже выполнена
2026-08-19 и остаётся как есть.

Запуск:
  python3 apply_prompt_edits_mirror.py            # diff, ничего не пишет
  python3 apply_prompt_edits_mirror.py --apply
"""
import json
import sys
from datetime import date
from pathlib import Path

from apply_prompt_edits_v308 import (
    BLOCK_COLS, IDS, WriteMismatch, fetch_all, load_env, write_verified,
)

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "docs" / "promts" / "prompt-edits-mirror.json"
SNAPSHOT = ROOT / "flow" / "lib" / "snapshots" / "pre_apply_mirror.json"
EDIT_TAG = f"[ПРАВКА {date(2026, 8, 19).isoformat()}]"
EXPECTED_BLOCKS = 5
# Общие блоки: правка действует во всех пяти сценариях — печатаем отдельно,
# чтобы это не потерялось в общем списке.
SHARED_BLOCKS = {"G-02_VOICE_V2", "G-06_CHECKLIST"}


def note_with(reason, current):
    """Причина дописывается ПЕРЕД прежними заметками, прежние сохраняются."""
    current = (current or "").strip()
    head = f"{EDIT_TAG} {reason}"
    if head in current:                      # уже применено — не дублируем
        return current
    return f"{head}\n{current}".strip()


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
        shared = "  [общий для всех 5 сценариев]" if bid in SHARED_BLOCKS else ""
        if cur == was:
            ok.append(bid)
            print(f"  совпало      {bid:24s} {len(was):>5} -> станет {len(now)}{shared}")
        elif cur == now:
            applied_already.append(bid)
            print(f"  уже применён {bid:24s}{shared}")
        else:
            mismatched.append(bid)
            print(f"  РАСХОЖДЕНИЕ  {bid:24s} was {len(was)} симв., в базе {len(cur)} симв.")

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
        cur = len(by_block[bid].get("Content") or "")
        print(f"  {bid:24s} {cur:>5} -> {len(now):<5} симв."
              f"{'   ОБЩИЙ (все 5 сценариев)' if bid in SHARED_BLOCKS else ''}")
    print(f"суммарно добавлено: {delta} симв. (~{delta // 4} ток. по формуле Tokens_est)")

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
    print("Активация не трогалась: выполнена 2026-08-19, остаётся как есть.")


if __name__ == "__main__":
    main()
