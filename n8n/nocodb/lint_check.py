#!/usr/bin/env python3
"""U8: локальный линт таблиц v3.00 (та же логика, что в flow/Tool_ Blocks Lint.json).

Проверки:
  ERRORS   — битые ссылки: Intent_ID/Block_ID в карте, которых нет в таблицах.
  WARNINGS — активная привязка на неактивный блок (каркас не наполнен);
             отсутствует/неактивен дефолтный ST-блок сценария×стадии;
             бюджет собранного промпта интент×сценарий×стадия > BUDGET_TOKENS;
             вход резолвера по сценарию > бюджета R8 (план 2026-07-17-001).

Запуск: python3 lint_check.py  (читает таблицы из NocoDB по .env + table_ids.json)
"""
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUDGET_TOKENS = 2500
# Tokens_est в таблице = len/4; для русского реальный токен ≈ 2.5 символа,
# поэтому реальные токены ≈ Tokens_est × 1.6 (калибровка по exec 2339)
TOKENS_EST_TO_REAL = 1.6
# Вход резолвера (R8, план 2026-07-17-001): ~2700 ток. × 3.17 симв./ток.
# (замер 2026-07-17, n8n exec 2449-2494) = бюджет в символах;
# фикс-часть промпта Build Prompt без списка интентов (шаблон + история
# 8×150 симв. + сообщение 100 симв.) — замерена исполнением jsCode
RESOLVER_PROMPT_CHAR_BUDGET = 8560
# [U5, план 2026-08-14-001] было 2433; из шаблона удалены client_mode и
# client_signal. Дельта замерена исполнением jsCode Build Prompt до и после
# правки на одинаковом входе (история 8x150 симв., сообщение 100 симв.):
# 2376 -> 1936, то есть -440.
# Берём 2433-440, а не измеренные 1936: замер прежней константы делался другой
# обвязкой и дал 2376 против объявленных 2433. Сохраняем её базис и применяем
# измеренную дельту — это завышает фикс-часть на ~57 симв., то есть ошибается
# в сторону соблюдения бюджета, а не его превышения.
RESOLVER_FIXED_CHARS = 1993
RESOLVER_CHARS_PER_TOKEN = 3.17
SCENARIOS = ("credit", "tradein", "combo", "stock", "consult")
STAGES = ("greeting", "data_collect", "tool_needed", "closing")


def load_env():
    env = {}
    for line in (HERE.parent / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def fetch_all(env, table_id):
    rows, offset = [], 0
    while True:
        req = urllib.request.Request(
            f"{env['NOCODB_API_URL'].rstrip('/')}/api/v2/tables/{table_id}/records?limit=200&offset={offset}",
            headers={"xc-token": env["NOCODB_TOKEN"]})
        with urllib.request.urlopen(req, timeout=30) as r:
            batch = json.loads(r.read()).get("list", [])
        rows.extend(batch)
        if len(batch) < 200:
            return rows
        offset += 200


def expand(value, universe):
    v = (value or "all").strip()
    return list(universe) if v == "all" else [s.strip() for s in v.split(",") if s.strip()]


def lint(intents, blocks, mapping):
    errors, warnings = [], []
    intent_ids = {i["Intent_ID"] for i in intents}
    block_by_id = {b["Block_ID"]: b for b in blocks}

    for m in mapping:
        if m["Intent_ID"] not in intent_ids:
            errors.append(f"карта → несуществующий интент {m['Intent_ID']}")
        blk = block_by_id.get(m["Block_ID"])
        if blk is None:
            errors.append(f"карта {m['Intent_ID']} → несуществующий блок {m['Block_ID']}")
        elif m.get("Active") and not blk.get("Active"):
            warnings.append(f"привязка {m['Intent_ID']} → {m['Block_ID']}: блок неактивен (каркас не наполнен)")

    # Зеркалит выбор основного блока в Prompt Compiler:
    #   pick(id) = active(id + "_V2") ? id + "_V2" : id
    #   если и он неактивен — фолбэк на WF_{СЦЕН}_LEGACY
    # Прежняя редакция смотрела только на базовый блок и давала ложные
    # предупреждения на каждый блок, вытесненный своей версией _V2: после
    # архивации семи блоков (план 2026-08-14-001) их стало семь из девяти.
    def active_id(bid):
        b = block_by_id.get(bid)
        return bool(b and b.get("Active"))

    for scen in SCENARIOS:
        for stage in STAGES:
            bid = f"ST_{scen.upper()}_{stage.upper()}"
            if bid not in block_by_id and f"{bid}_V2" not in block_by_id:
                warnings.append(f"нет дефолтного блока {bid}")
                continue
            if active_id(f"{bid}_V2") or active_id(bid):
                continue  # компилятор возьмёт _V2 либо базовый — дыры нет
            legacy = f"WF_{scen.upper()}_LEGACY"
            if active_id(legacy):
                warnings.append(
                    f"дефолтный блок {bid} и его _V2 неактивны — ход поедет на {legacy}")
            else:
                warnings.append(
                    f"дефолтный блок {bid}: неактивны ни он, ни _V2, ни фолбэк {legacy} — "
                    f"основной блок хода будет ПУСТЫМ")

    # бюджет: ядро + CS + D + primary + additional (+tools)
    core = sum(int(b.get("Tokens_est") or 0) for b in blocks
               if b["Block_ID"].startswith("G-") and b.get("Active") and not b.get("Intent_Triggers"))
    tools = int((block_by_id.get("T-01_TOOLS") or {}).get("Tokens_est") or 0)
    over = []
    for intent in intents:
        if not intent.get("Active"):
            continue
        for scen in expand(intent.get("Scenarios"), SCENARIOS):
            # R1 (план 2026-07-17-001): Stage_Context — справочное поле, не фильтр;
            # активный интент может быть предсказан на любой стадии
            for stage in STAGES:
                rows = [m for m in mapping if m["Intent_ID"] == intent["Intent_ID"] and m.get("Active")
                        and expand(m.get("Scenario_Filter"), SCENARIOS).count(scen)
                        and expand(m.get("Stage_Filter"), STAGES).count(stage)]
                primary = next((m["Block_ID"] for m in rows if m["Role"] == "primary"), None) \
                    or f"ST_{scen.upper()}_{stage.upper()}"
                ids = [f"CS_{scen.upper()}", f"D_{scen.upper()}", primary] + \
                      [m["Block_ID"] for m in rows if m["Role"] == "additional"]
                est = core + tools + sum(int((block_by_id.get(i) or {}).get("Tokens_est") or 0) for i in ids)
                total = int(est * TOKENS_EST_TO_REAL)
                if total > BUDGET_TOKENS:
                    over.append((intent["Intent_ID"], scen, stage, total))
    if over:
        worst = sorted(over, key=lambda x: -x[3])[:5]
        warnings.append(f"бюджет >{BUDGET_TOKENS} ток. у {len(over)} комбинаций интент×сценарий×стадия; "
                        f"худшие: " + ", ".join(f"{i}/{s}/{st}={t}" for i, s, st, t in worst))

    # вход резолвера (R8): фикс-часть + строки `Intent_ID | Description | Примеры`
    # всех активных интентов сценария (стадия список не сужает после R1)
    for scen in SCENARIOS:
        rows = [i for i in intents if i.get("Active")
                and scen in expand(i.get("Scenarios"), SCENARIOS)]
        list_chars = sum(
            len(f"{i['Intent_ID']} | {i.get('Description') or ''} | Примеры: {i.get('Example_Phrases') or ''}") + 1
            for i in rows)
        total = RESOLVER_FIXED_CHARS + list_chars
        if total > RESOLVER_PROMPT_CHAR_BUDGET:
            warnings.append(f"вход резолвера {scen}: ~{total} симв. "
                            f"(~{round(total / RESOLVER_CHARS_PER_TOKEN)} ток., {len(rows)} интентов) "
                            f"> бюджета R8 {RESOLVER_PROMPT_CHAR_BUDGET} симв.")
    return errors, warnings


def main():
    env = load_env()
    ids = json.loads((HERE / "table_ids.json").read_text())
    intents = fetch_all(env, ids["Intents"])
    blocks = fetch_all(env, ids["Prompt_Blocks"])
    mapping = fetch_all(env, ids["Intent_Block_Map"])
    print(f"строк: intents={len(intents)} blocks={len(blocks)} map={len(mapping)}")

    errors, warnings = lint(intents, blocks, mapping)
    for e in errors:
        print("ERROR:", e)
    print(f"\nошибок: {len(errors)}; предупреждений: {len(warnings)}")
    for w in warnings[:12]:
        print("warn:", w)
    if len(warnings) > 12:
        print(f"... и ещё {len(warnings) - 12}")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
