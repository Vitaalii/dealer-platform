#!/usr/bin/env python3
"""Применение трёх пакетов правок промпта, снятых с симулятора (ТЗ 2026-08-18).

Пакеты: docs/promts/prompt-edits-{credit-final,tradein,combo}.json — все три
сняты с одного среза NocoDB (2026-08-15, отпечаток b3060071568f5803).

Что делает (U1-U6 из ТЗ, редакция 2026-08-18):
  U1  сверяет base_hash каждого из 44 блоков с живым Content; расхождение хотя
      бы в одном — стоп, ничего не пишется (частичное применение запрещено);
  U2  Content = content.now, Tokens_est пересчитывается (len//4 — та же формула,
      что в push_tables.py и add_*.py), reason дописывается в Notes строкой
      [ПРАВКА <дата>] ПЕРЕД прежними заметками (прежние сохраняются: в них
      живут инструкции по активации и роли блоков);
  U3  ОТМЕНЁН. ST_CREDIT_TOOL_NEEDED остаётся активным и получает новый текст:
      стадия tool_needed в кредите задаётся слотом payment из Dialogflow-воронки
      и возникает как раз на диалогах без телефона — целевых для кабинета;
  U4  семь строк Intent_Block_Map -> Active по полю active.now пакета;
  U5  Intent_Triggers = need_check_not_ok двум переиспользованным блокам;
  U6  шести отменённым черновикам ставится пометка в Notes, Active не трогается
      (остаётся false). Из списка активации они вычеркнуты в
      add_cabinet_tactic_v307.py (CANCELLED_DRAFTS).

Чего НЕ делает: активацию (U7). Ни один блок не включается.

=== ПРОТОКОЛ ЗАПИСИ (почин после порчи 2026-08-18) ================================
Батчевый PATCH NocoDB объединяет набор колонок по всей пачке и обнуляет поля,
отсутствующие в конкретной записи: правка {Notes, Active} рядом с правкой
{Content, Notes, Tokens_est} стирает Content у первой записи. Воспроизведено на
одноразовой таблице.

Отсюда три правила, снять которые нельзя:
  1. ОДНА ЗАПИСЬ — ОДИН PATCH. Пачек нет: набор колонок объединять не с чем.
  2. ЧТЕНИЕ ДО И ПОСЛЕ. Ожидаемое состояние = прочитанная строка + намеренные
     поля. После записи строка читается обратно и сверяется ПО ВСЕМ колонкам,
     а не только по изменённым.
  3. СТОП НА ПЕРВОМ РАСХОЖДЕНИИ. Останавливается вся операция; уже записанное
     печатается, откат — из снапшота, снятого до первой записи.

Идемпотентность: повторный --apply печатает нули.

Запуск:
  python3 apply_prompt_edits_v308.py            # diff, ничего не пишет
  python3 apply_prompt_edits_v308.py --apply
"""
import hashlib
import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ENV_PATH = ROOT / ".env"
PKG_DIR = ROOT / "docs" / "promts"
IDS = json.loads((HERE / "table_ids.json").read_text(encoding="utf-8"))
SNAPSHOT = ROOT / "flow" / "lib" / "snapshots" / "pre_apply_v308.json"

PACKAGES = ["prompt-edits-credit-final.json",
            "prompt-edits-tradein.json",
            "prompt-edits-combo.json"]
EDIT_TAG = f"[ПРАВКА {date(2026, 8, 18).isoformat()}]"
SNAPSHOT_FINGERPRINT = "b3060071568f5803"
EXPECTED_BLOCKS, EXPECTED_MAP = 44, 7

# Колонки, по которым идёт обратная сверка. Полный список содержательных полей
# таблицы: сверять только изменённые бессмысленно — порча била как раз по тем,
# которые мы менять не собирались.
BLOCK_COLS = ["Block_ID", "Level", "Scenario", "Stage", "Version_Set",
              "Content", "Tokens_est", "Active", "Intent_Triggers", "Notes"]
MAP_COLS = ["Intent_ID", "Block_ID", "Role", "Scenario_Filter",
            "Stage_Filter", "Version_Set", "Active"]

# U5: два блока сменили назначение — теперь это отработка отказа клиента от
# ОБОИХ вариантов развилки. Формат пакета триггеры не переносит, ставим руками.
# Переименование блоков сознательно НЕ делается: их Block_ID зашит в строки
# карты, в скрипты v3.03/v3.07 и в срез, по которому владелец сверяет результат
# в симуляторе. Неточность имён закрыта пометкой в Notes.
TRIGGER_PATCHES = {
    "BLK_CREDIT_APPROVAL_BRIDGE": "need_check_not_ok",
    "BLK_TRADEIN_TESTDRIVE_BRIDGE": "need_check_not_ok",
}

# U6: шесть отменённых черновиков. Пять несут reason из пакета; BLK_CREDIT_FORK
# в пакет не попал вовсе — его пометка собрана из формулировки ТЗ. Это
# единственная запись, где правится ТОЛЬКО Notes: ровно тот случай, на котором
# старый писатель стирал Content.
CANCELLED_DRAFTS = [
    "BLK_CREDIT_FORK", "BLK_CREDIT_CABINET_FALLBACK",
    "BLK_TRADEIN_FORK", "BLK_TRADEIN_CABINET_FALLBACK",
    "BLK_COMBO_FORK", "BLK_COMBO_CABINET_FALLBACK",
]
CREDIT_FORK_NOTE = ("Черновик отменён: развилка перенесена в "
                    "ST_CREDIT_GREETING_V2 — отдельный блок дублировал бы её на "
                    "том же ходе. Исключить из списка --activate-scenario credit, "
                    "Active оставить false.")


class WriteMismatch(RuntimeError):
    """Обратное чтение не совпало с ожидаемым — операция останавливается."""


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
    with urllib.request.urlopen(req, timeout=60) as resp:
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


def read_one(env, table_id, row_id):
    return api(env, "GET", f"/api/v2/tables/{table_id}/records/{row_id}")


def write_verified(env, table_id, current, intended, cols, label, done):
    """Правило 1+2+3: одна запись — один PATCH, обратное чтение, стоп при сносе.

    `current` — строка, прочитанная перед записью. Ожидаемое состояние строится
    из неё, а не из представлений скрипта о таблице.
    """
    row_id = current["Id"]
    expected = {c: current.get(c) for c in cols}
    expected.update(intended)

    api(env, "PATCH", f"/api/v2/tables/{table_id}/records", [dict(intended, Id=row_id)])

    back = read_one(env, table_id, row_id)
    drift = [c for c in cols if back.get(c) != expected.get(c)]
    if drift:
        raise WriteMismatch(
            f"{label}: обратное чтение разошлось по полям {drift}\n"
            + "\n".join(f"    {c}: ожидали {expected.get(c)!r}, "
                        f"в базе {back.get(c)!r}" for c in drift)
            + f"\n  успели записать до остановки: {len(done)} строк "
              f"({', '.join(done) or '—'})"
            + f"\n  откат: снапшот до записи лежит в {SNAPSHOT}")
    done.append(label)


def h12(text):
    """Контрольная сумма пакета: sha1 текста, первые 12 знаков."""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


def note_with(reason, current):
    """reason дописывается ПЕРЕД прежними заметками, прежние сохраняются."""
    current = (current or "").strip()
    if current.startswith(EDIT_TAG):          # уже применено — не дублируем
        return current
    return f"{EDIT_TAG} {reason}\n{current}".strip()


def main():
    apply_mode = "--apply" in sys.argv
    env = load_env()

    blocks = fetch_all(env, IDS["Prompt_Blocks"])
    mapping = fetch_all(env, IDS["Intent_Block_Map"])
    by_block = {b["Block_ID"]: b for b in blocks}
    print(f"живые таблицы: блоков {len(blocks)}, строк карты {len(mapping)}")

    packages = []
    for name in PACKAGES:
        pkg = json.loads((PKG_DIR / name).read_text(encoding="utf-8"))
        if pkg.get("fingerprint") != SNAPSHOT_FINGERPRINT:
            sys.exit(f"{name}: отпечаток {pkg.get('fingerprint')} != {SNAPSHOT_FINGERPRINT}")
        packages.append((name, pkg))

    n_blocks = sum(len(p["blocks"]) for _, p in packages)
    n_map = sum(len(p["map"]) for _, p in packages)
    if (n_blocks, n_map) != (EXPECTED_BLOCKS, EXPECTED_MAP):
        sys.exit(f"ОСТАНОВ: в пакетах {n_blocks} блоков и {n_map} строк карты, "
                 f"ТЗ ждёт {EXPECTED_BLOCKS} и {EXPECTED_MAP}")

    # ---------------- U1: сверка -------------------------------------------
    mismatched, applied_already, missing = [], [], []
    for name, pkg in packages:
        for b in pkg["blocks"]:
            row = by_block.get(b["Block_ID"])
            if row is None:
                missing.append((name, b["Block_ID"]))
                continue
            cur = row.get("Content") or ""
            content = b.get("content") or {}
            if h12(cur) == b["base_hash"]:
                continue
            if content.get("now") is not None and cur == content["now"]:
                applied_already.append(b["Block_ID"])
                continue
            mismatched.append((name, b["Block_ID"], b["base_hash"], h12(cur)))

    print(f"\nU1 сверка base_hash: блоков в пакетах {n_blocks}, "
          f"расхождений {len(mismatched)}, уже применено {len(applied_already)}, "
          f"нет в базе {len(missing)}")
    for name, bid in missing:
        print(f"  НЕТ В NocoDB: {bid} ({name})")
    for name, bid, want, got in mismatched:
        print(f"  РАСХОЖДЕНИЕ: {bid} ({name}) пакет={want} живое={got}")
    if mismatched or missing:
        sys.exit("\nОСТАНОВ: база менялась после снятия среза — правки надо "
                 "пересобирать. Ничего не записано.")

    # ---------------- U2/U5/U6: блоки ---------------------------------------
    patches, plan = {}, []

    def stage(bid, fields, what):
        row = by_block[bid]
        diff = {k: v for k, v in fields.items() if row.get(k) != v}
        if not diff:
            return
        patches.setdefault(bid, {}).update(diff)
        plan.append((bid, what, sorted(diff)))

    for name, pkg in packages:
        for b in pkg["blocks"]:
            bid = b["Block_ID"]
            fields = {"Notes": note_with(b["reason"], by_block[bid].get("Notes"))}
            content = b.get("content") or {}
            if content.get("now") is not None:
                fields["Content"] = content["now"]
                fields["Tokens_est"] = len(content["now"]) // 4
            active = b.get("active") or {}
            if "now" in active:
                fields["Active"] = bool(active["now"])
            stage(bid, fields, "U2")

    for bid, triggers in TRIGGER_PATCHES.items():                  # U5
        stage(bid, {"Intent_Triggers": triggers}, "U5")

    for bid in CANCELLED_DRAFTS:                                   # U6
        row = by_block[bid]
        if row.get("Active"):
            sys.exit(f"ОСТАНОВ: отменённый черновик {bid} активен — ТЗ ждёт false")
        if bid == "BLK_CREDIT_FORK":
            stage(bid, {"Notes": note_with(CREDIT_FORK_NOTE, row.get("Notes"))}, "U6")

    print(f"\nU2/U5/U6 — блоков к правке: {len(patches)}")
    for bid, what, fields in plan:
        print(f"  {what:4s} {bid:34s} {', '.join(fields)}")

    # ---------------- U4: карта ---------------------------------------------
    map_patches, map_plan = [], []
    for name, pkg in packages:
        for m in pkg["map"]:
            rows = [r for r in mapping
                    if r["Intent_ID"] == m["Intent_ID"]
                    and r["Block_ID"] == m["Block_ID"]
                    and (r.get("Scenario_Filter") or "") == m["Scenario_Filter"]]
            if len(rows) != 1:
                sys.exit(f"ОСТАНОВ: строк карты {len(rows)} для "
                         f"{m['Intent_ID']} -> {m['Block_ID']} ({m['Scenario_Filter']})")
            row = rows[0]
            if (row.get("Role"), row.get("Stage_Filter")) != (m["Role"], m["Stage_Filter"]):
                sys.exit(f"ОСТАНОВ: {m['Intent_ID']} -> {m['Block_ID']}: роль/стадия в базе "
                         f"({row.get('Role')}, {row.get('Stage_Filter')}) != пакет "
                         f"({m['Role']}, {m['Stage_Filter']})")
            want = bool(m["active"]["now"])
            if bool(row.get("Active")) == want:
                continue
            map_patches.append((row, {"Active": want}))
            map_plan.append((m["Intent_ID"], m["Block_ID"], m["Scenario_Filter"], want))

    print(f"\nU4 — строк карты к переключению: {len(map_patches)}")
    for intent, bid, scen, want in map_plan:
        print(f"  {'включить' if want else 'выключить':10s} {intent:22s} -> {bid:30s} ({scen})")

    # ---------------- запись -------------------------------------------------
    if not apply_mode:
        print("\n(diff-режим: ничего не записано; --apply)")
        return

    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(
        {"Prompt_Blocks": blocks, "Intent_Block_Map": mapping,
         "Intents": fetch_all(env, IDS["Intents"])}, ensure_ascii=False), encoding="utf-8")
    print(f"\nснапшот до записи: {SNAPSHOT}")

    done = []
    print(f"\nзапись по одной строке с обратной сверкой ({len(patches)} блоков, "
          f"{len(map_patches)} строк карты):")
    try:
        for bid, fields in patches.items():
            write_verified(env, IDS["Prompt_Blocks"], by_block[bid], fields,
                           BLOCK_COLS, bid, done)
            print(f"  ок  {bid:34s} {', '.join(sorted(fields))}")
        for row, fields in map_patches:
            label = f"{row['Intent_ID']} -> {row['Block_ID']} ({row.get('Scenario_Filter')})"
            write_verified(env, IDS["Intent_Block_Map"], row, fields, MAP_COLS, label, done)
            print(f"  ок  карта: {label} Active={fields['Active']}")
    except WriteMismatch as e:
        sys.exit(f"\nОСТАНОВ ПО РАСХОЖДЕНИЮ\n  {e}")

    print(f"\nзаписано и сверено обратным чтением: {len(done)} строк, расхождений 0")
    print("АКТИВАЦИЯ НЕ ВЫПОЛНЯЛАСЬ (U7): ни один блок не включён.")


if __name__ == "__main__":
    main()
