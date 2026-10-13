#!/usr/bin/env python3
"""U8: создание таблиц Intents / Prompt_Blocks / Intent_Block_Map в NocoDB
и загрузка данных из nocodb/*.csv.

Читает .env в корне рабочей папки (NOCODB_API_URL, NOCODB_TOKEN, NOCODB_BASE_ID,
NOCODB_TABLE_PROMPTS). Таблицу промптов старого бота НЕ трогает ни при каких
условиях (жёсткая проверка по ID). Идемпотентность: существующая таблица не
пересоздаётся; --replace-data очищает и перезаливает строки ТОЛЬКО трёх
наших таблиц.

Запуск: python3 push_tables.py [--replace-data]

⚠️ С 2026-07-13 канонический источник данных — ЖИВЫЕ таблицы NocoDB (владелец
правит контент и каркасы прямо в UI). CSV в этой папке — стартовый снапшот.
--replace-data ЗАТРЁТ правки владельца — использовать только при полном
пересоздании с его явного согласия.
"""
import csv
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE.parent / ".env"

TABLES = {
    "Intents": ("intents.csv", [
        ("Intent_ID", "SingleLineText"), ("Group", "SingleLineText"),
        ("Group_Name", "SingleLineText"), ("Description", "LongText"),
        ("Example_Phrases", "LongText"), ("Scenarios", "SingleLineText"),
        ("Stage_Context", "SingleLineText"), ("Priority", "Number"),
        ("Active", "Checkbox"), ("Version", "SingleLineText"),
        ("Source", "SingleLineText"), ("Notes", "LongText"),
    ]),
    "Prompt_Blocks": ("prompt_blocks.csv", [
        ("Block_ID", "SingleLineText"), ("Level", "SingleLineText"),
        ("Scenario", "SingleLineText"), ("Stage", "SingleLineText"),
        ("Version_Set", "SingleLineText"), ("Content", "LongText"),
        ("Tokens_est", "Number"), ("Active", "Checkbox"),
        ("Intent_Triggers", "LongText"), ("Notes", "LongText"),
    ]),
    "Intent_Block_Map": ("intent_block_map.csv", [
        ("Intent_ID", "SingleLineText"), ("Block_ID", "SingleLineText"),
        ("Role", "SingleLineText"), ("Scenario_Filter", "SingleLineText"),
        ("Stage_Filter", "SingleLineText"), ("Version_Set", "SingleLineText"),
        ("Active", "Checkbox"),
    ]),
}
NUMBER_COLS = {"Priority", "Tokens_est"}
BOOL_COLS = {"Active"}


def load_env():
    if not ENV_PATH.exists():
        sys.exit(f"Нет файла {ENV_PATH} — создайте его (NOCODB_API_URL/TOKEN/BASE_ID/TABLE_PROMPTS)")
    env = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    missing = [k for k in ("NOCODB_API_URL", "NOCODB_TOKEN", "NOCODB_BASE_ID", "NOCODB_TABLE_PROMPTS")
               if not env.get(k)]
    if missing:
        sys.exit(f"В .env не хватает: {missing}")
    return env


def api(env, method, path, body=None):
    req = urllib.request.Request(
        env["NOCODB_API_URL"].rstrip("/") + path, method=method,
        headers={"xc-token": env["NOCODB_TOKEN"], "Content-Type": "application/json"},
        data=json.dumps(body).encode("utf-8") if body is not None else None)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def convert(row):
    out = {}
    for k, v in row.items():
        if k in NUMBER_COLS:
            out[k] = int(v) if v not in ("", None) else None
        elif k in BOOL_COLS:
            out[k] = v.lower() == "true"
        else:
            out[k] = v
    return out


def main():
    replace = "--replace-data" in sys.argv
    env = load_env()
    prompts_tid = env["NOCODB_TABLE_PROMPTS"]

    existing = {t["title"]: t["id"]
                for t in api(env, "GET", f"/api/v2/meta/bases/{env['NOCODB_BASE_ID']}/tables")["list"]}
    print("таблиц в базе:", len(existing))

    created_ids = {}
    for title, (csv_name, columns) in TABLES.items():
        if title in existing:
            tid = existing[title]
            assert tid != prompts_tid, "защита: совпадение с таблицей промптов старого бота!"
            print(f"{title}: уже существует ({tid})")
            if not replace:
                created_ids[title] = tid
                continue
            # очистка ТОЛЬКО нашей таблицы
            while True:
                recs = api(env, "GET", f"/api/v2/tables/{tid}/records?limit=200").get("list", [])
                if not recs:
                    break
                api(env, "DELETE", f"/api/v2/tables/{tid}/records",
                    [{"Id": r["Id"]} for r in recs])
            print(f"{title}: строки очищены (--replace-data)")
        else:
            resp = api(env, "POST", f"/api/v2/meta/bases/{env['NOCODB_BASE_ID']}/tables", {
                "table_name": title, "title": title,
                # PK обязателен: без него NocoDB UI не даёт редактировать строки
                # («Update not allowed for table which doesn't have primary key»)
                "columns": [{"column_name": "Id", "title": "Id", "uidt": "ID", "pk": True}] +
                           [{"column_name": c, "title": c, "uidt": t} for c, t in columns],
            })
            tid = resp["id"]
            print(f"{title}: создана ({tid})")
        created_ids[title] = tid

        rows = [convert(r) for r in csv.DictReader(open(HERE / csv_name, encoding="utf-8"))]
        for i in range(0, len(rows), 50):
            api(env, "POST", f"/api/v2/tables/{tid}/records", rows[i:i + 50])
        print(f"{title}: загружено {len(rows)} строк")

    ids_path = HERE / "table_ids.json"
    ids_path.write_text(json.dumps(created_ids, indent=1), encoding="utf-8")
    print(f"\nID таблиц сохранены в {ids_path.name}: {created_ids}")
    print("Таблица промптов старого бота не тронута:", prompts_tid)


if __name__ == "__main__":
    main()
