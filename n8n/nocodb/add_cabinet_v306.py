#!/usr/bin/env python3
"""Черновики v3.06: личный кабинет + переиспользование готовой оценки трейд-ина.

План: docs/plans/2026-08-10-001-feat-cabinet-evaluation-data-and-prompt-plan.md (U6).

Два блока, оба Active=false. Владелец дорабатывает тексты в UI NocoDB и включает
их одним заходом (или скриптом --activate-drafts) — до активации промпт не
меняется ни на символ.

  G-07_CABINET     general/all/all — предложение личного кабинета. Подключается
                   списком general-блоков в Prompt Compiler (build_v3_workflow.py),
                   поэтому код уже пропатчен; map-строки не нужны.
  BLK_EVAL_REUSE   tradein+combo — запрет повторного платного вызова оценки.
                   Подключается механизмом Intent_Triggers (как живой G-05_DEFENSE),
                   поэтому ни кода, ни map-строк не требует вовсе.

Запуск:
  python3 add_cabinet_v306.py                    # diff, ничего не пишет
  python3 add_cabinet_v306.py --apply-blocks     # POST черновиков (Active=false)
  python3 add_cabinet_v306.py --activate-drafts  # включить (только [TEST]-контур)
  python3 add_cabinet_v306.py --rollback         # выключить обратно
  python3 add_cabinet_v306.py --snapshot-only    # только пересобрать снапшот
  ... [--snapshot PATH]                          # переопределить путь снапшота
"""
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE.parent / ".env"
IDS = json.loads((HERE / "table_ids.json").read_text(encoding="utf-8"))
SNAPSHOT_DEFAULT = HERE.parent / "flow" / "lib" / "snapshots" / "v3_tables.json"
VERSION = "v3.06"

_NOTE = ("Черновик v3.06 (план 2026-08-10-001, личный кабинет). Драфт — доработать в UI; "
         "активация add_cabinet_v306.py --activate-drafts, только [TEST]-контур.")

# --- G-07_CABINET: обе вставки из docs/CABINET_PROMPT_NOTE.md одним блоком ---
# Одним, а не двумя: тексты связаны условием «когда предлагать», и активация
# половины правила дала бы ссылку до выбора комплектации — ровно то, что чиним.
# Формулировка условия молчания — «пустая ИЛИ отсутствует»: при сбое выдачи токена
# бот не присылает ключи вовсе (main.py:1050-1055), и «если пустая» модель прочтёт
# как неприменимое.
CABINET_CONTENT = (
    "ЛИЧНЫЙ КАБИНЕТ КЛИЕНТА.\n"
    "Тебе доступны переменные qualification_context.cabinet_url (персональная ссылка на "
    "личный кабинет клиента) и qualification_context.cabinet_status "
    "(not_visited / phone_confirmed / form_submitted).\n"
    "- Если cabinet_url пустая или отсутствует — кабинет НЕ упоминай вовсе.\n"
    "- Если телефон клиента уже получен — кабинет НЕ упоминай.\n"
    "- Если клиент не оставил телефон и cabinet_url заполнена — предложи продолжить "
    "оформление в личном кабинете по этой ссылке (подтвердить номер по коду и заполнить "
    "анкету онлайн). Один раз, ненавязчиво, не повторяй в каждом сообщении.\n"
    "- ПРЕЖДЕ чем предлагать кабинет, доведи подбор до выбора КОНКРЕТНОЙ комплектации "
    "(клиент подтвердил, какая машина его интересует). Ссылку предлагай только после "
    "этого — тогда клиент увидит в кабинете кредитный калькулятор по выбранной машине. "
    "Ссылка, выданная раньше, открывает кабинет без калькулятора.\n"
    "- cabinet_status можно использовать в репликах: phone_confirmed — «вижу, вы "
    "подтвердили номер в кабинете»; form_submitted — «анкета получена»."
)

# --- BLK_EVAL_REUSE: экономия платных вызовов auto.ru ---
# Вызов оценки платный, а сабфлоу оценки исполняется безусловно при КАЖДОМ вызове
# Combo Calculator (Execute Workflow до Prepare Combo, кэша по (номер, пробег) нет).
# Ни один активный блок повторный вызов не запрещал, при этом «ЗАПРЕЩЕНО называть
# цифры до ответа инструмента» + безпамятная архитектура агента формально обязывали
# звать оценку заново на каждом возврате к цифрам.
# Правило исполнимо только вместе с правкой Data Selector (U6): ti_valuation и
# ti_evaluated_car выведены в ai_clean_context, иначе модели нечего проверять.
EVAL_REUSE_CONTENT = (
    "ПОВТОРНЫЙ ВЫЗОВ ОЦЕНКИ АВТОМОБИЛЯ — ЗАПРЕЩЁН.\n"
    "Если в JSON уже есть ti_valuation (сумма оценки трейд-ина), оценка по этому "
    "автомобилю УЖЕ выполнена. Бери сумму из JSON и НЕ вызывай инструмент оценки "
    "повторно — ни evaluate_auto, ни комбо-расчёт ради переоценки.\n"
    "Это НЕ противоречит запрету выдумывать стоимость выкупа: цифра из JSON — "
    "результат инструмента, а не твоя догадка.\n"
    "Повторный вызов допустим РОВНО в двух случаях:\n"
    "- клиент назвал ДРУГОЙ госномер, чем в ti_evaluated_car.plate;\n"
    "- клиент назвал ДРУГОЙ пробег, чем в ti_evaluated_car.mileage.\n"
    "Во всех остальных случаях — включая просьбу пересчитать платёж, изменить взнос "
    "или срок, вернуться к обсуждению цифр — используй уже полученную оценку."
)

NEW_BLOCKS = [
    {"Block_ID": "G-07_CABINET", "Level": "general", "Scenario": "all", "Stage": "all",
     "Version_Set": VERSION, "Content": CABINET_CONTENT, "Tokens_est": len(CABINET_CONTENT) // 4,
     "Active": False, "Intent_Triggers": "",
     "Notes": _NOTE + " Подключён списком general-блоков в Prompt Compiler."},
    {"Block_ID": "BLK_EVAL_REUSE", "Level": "tools", "Scenario": "tradein,combo", "Stage": "all",
     "Version_Set": VERSION, "Content": EVAL_REUSE_CONTENT,
     "Tokens_est": len(EVAL_REUSE_CONTENT) // 4, "Active": False,
     "Intent_Triggers": "ready_for_tool,tool_rerun_needed,data_received_complete,"
                        "tool_error_handling,tool_result_received",
     "Notes": _NOTE + " Подключается Intent_Triggers, map-строки не нужны. Требует "
                      "ti_valuation/ti_evaluated_car в ai_clean_context (Data Selector, U6)."},
]


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


def main():
    env = load_env()
    args = sys.argv[1:]
    snap_path = Path(args[args.index("--snapshot") + 1]) if "--snapshot" in args else SNAPSHOT_DEFAULT

    intents = fetch_all(env, IDS["Intents"])
    blocks = fetch_all(env, IDS["Prompt_Blocks"])
    mapping = fetch_all(env, IDS["Intent_Block_Map"])
    print(f"живые таблицы: интентов {len(intents)}, блоков {len(blocks)}, map-строк {len(mapping)}")

    wrote = False
    if "--snapshot-only" not in args:
        by_block = {b["Block_ID"]: b for b in blocks}
        blocks_to_post = [b for b in NEW_BLOCKS if b["Block_ID"] not in by_block]

        # Intent_Triggers работают только по живым интентам: несуществующий триггер
        # молча никогда не сработает, поэтому проверяем громко.
        live_intents = {i["Intent_ID"] for i in intents}
        for b in NEW_BLOCKS:
            missing = [t for t in str(b["Intent_Triggers"]).split(",") if t and t not in live_intents]
            if missing:
                print(f"  ⚠️ {b['Block_ID']}: триггеры отсутствуют в живой таблице интентов: "
                      f"{', '.join(missing)} — блок по ним никогда не подключится")

        print(f"\n— блоки-черновики (POST, Active=false): {len(blocks_to_post)}")
        for b in blocks_to_post:
            print(f"    {b['Block_ID']} ({b['Level']}/{b['Scenario']}, {len(b['Content'])} зн., "
                  f"~{b['Tokens_est']} ток.)")
        for b in NEW_BLOCKS:
            if b["Block_ID"] in by_block:
                live = by_block[b["Block_ID"]]
                same = str(live.get("Content") or "").strip() == b["Content"].strip()
                print(f"    {b['Block_ID']}: уже есть (Active={live.get('Active')}, "
                      f"текст {'совпадает с драфтом' if same else 'правлен владельцем — не трогаем'})")

        if "--apply-blocks" in args:
            if blocks_to_post:
                api(env, "POST", f"/api/v2/tables/{IDS['Prompt_Blocks']}/records", blocks_to_post)
                print(f"\nдобавлено блоков: {len(blocks_to_post)} (Active=false)")
            else:
                print("\nнечего добавлять — блоки уже в таблице")
            wrote = True

        if "--activate-drafts" in args or "--rollback" in args:
            activate = "--activate-drafts" in args
            ids = {b["Block_ID"] for b in NEW_BLOCKS}
            bpatch = [{"Id": b["Id"], "Active": activate} for b in blocks
                      if b["Block_ID"] in ids and bool(b.get("Active")) != activate]
            verb = "активация v3.06" if activate else "откат v3.06"
            print(f"\n{verb}: блоков к переключению {len(bpatch)}")
            for b in blocks:
                if b["Block_ID"] in ids:
                    print(f"    {b['Block_ID']}: {b.get('Active')} -> {activate}")
            if bpatch:
                api(env, "PATCH", f"/api/v2/tables/{IDS['Prompt_Blocks']}/records", bpatch)
            wrote = True

        if not wrote:
            print("\n(diff-режим: ничего не записано; "
                  "--apply-blocks / --activate-drafts / --rollback)")

    if wrote or "--snapshot-only" in args:
        snap = {"intents": fetch_all(env, IDS["Intents"]),
                "blocks": fetch_all(env, IDS["Prompt_Blocks"]),
                "map": fetch_all(env, IDS["Intent_Block_Map"])}
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        print(f"снапшот таблиц: {snap_path} ({len(snap['intents'])} интентов, "
              f"{len(snap['blocks'])} блоков, {len(snap['map'])} строк карты)")


if __name__ == "__main__":
    main()
