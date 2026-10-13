#!/usr/bin/env python3
"""v3.04: обработка отсутствующих данных во ВСЕХ greeting-блоках.

Баг: бот здоровался «Здравствуйте, Клиент!» и писал «заявку на покупку
[Модель] [Комплектация]» с литеральными шаблонами, когда данные не прилетели
от Python. Причина в greeting-блоках: (1) прямой приказ «поздоровайся по имени»
перебивал правило G-02_VOICE об отсутствующем имени; (2) [Модель]/[Комплектация]
предписаны безусловно, без обработки пустых данных.

Лечение — единый ПРЕАМБУЛ-блок правил, дописываемый в НАЧАЛО каждого
greeting-блока (объявляет приоритет над нижеследующими фразами):
  • ИМЯ: пусто / "Клиент" / с цифрами → «Здравствуйте!» без имени;
  • ЗНАЧЕНИЯ В СКОБКАХ: подставлять только из ВХОДЯЩИХ ДАННЫХ, при отсутствии —
    не выводить шаблон в скобках и имя переменной, а переформулировать фразу.

Тексты берутся из ЖИВОЙ таблицы (канонический источник с 2026-07-13), не из CSV.
Идемпотентно: блок с уже дописанным преамбулом пропускается.

Выкатка по конвенции plan-prompt-block-changes-from-live-nocodb:
  • credit/tradein/combo/consult — свободный слот `_V2`: черновик Active=false,
    компилятор предпочтёт его через pick() (build_v3_workflow.py) после того,
    как владелец включит V2 и выключит базовый блок;
  • stock — слот `ST_STOCK_GREETING_V2` уже занят АКТИВНЫМ блоком владельца
    (v3.03): преамбул дописывается прямо в него (PATCH). Это ЕДИНСТВЕННОЕ
    изменение с немедленным эффектом; контент владельца сохраняется 1:1 после
    преамбула, откат — снять преамбул. Только [TEST]-контур (прод не читает v3).

Запуск:
  python3 add_greeting_missingdata_v304.py            # dry-run (только план)
  python3 add_greeting_missingdata_v304.py --apply     # применить POST/PATCH
"""
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE.parent / ".env"
IDS = json.loads((HERE / "table_ids.json").read_text(encoding="utf-8"))

PREAMBLE_MARKER = "ПРАВИЛА ПОДСТАНОВКИ"
PREAMBLE = (
    "ПРАВИЛА ПОДСТАНОВКИ (действуют для ВСЕХ фраз ниже и имеют приоритет над ними):\n"
    "• ИМЯ: бери из `user_data.name`. Если оно пустое, отсутствует, равно "
    '"Клиент", содержит цифры/спецсимволы или вызывает сомнения — поздоровайся '
    'БЕЗ имени: просто "Здравствуйте!". Если есть Имя и Фамилия — оставь только '
    'Имя. СТРОГО ЗАПРЕЩЕНО выводить шаблон "[Имя клиента]".\n'
    "• ЗНАЧЕНИЯ В КВАДРАТНЫХ СКОБКАХ ([Модель], [Комплектация], [Сумма] и любые "
    "другие): подставляй ТОЛЬКО из блока ВХОДЯЩИЕ ДАННЫЕ ИЗ БАЗЫ. Если значения "
    "там нет (поле пустое или отсутствует) — НИКОГДА не выводи сам шаблон в "
    "скобках и не пиши имя переменной (model, complectation и т.п.); переформулируй "
    'фразу без него (нет модели/комплектации → «получил вашу заявку на покупку '
    'автомобиля»).\n\n'
)

# base greeting-блоки со свободным слотом _V2 → выкатываем инертным черновиком
DRAFT_BASES = [
    "ST_CREDIT_GREETING", "ST_TRADEIN_GREETING",
    "ST_COMBO_GREETING", "ST_CONSULT_GREETING",
]
# greeting-блок, чей _V2 уже активен у владельца → дописываем прямо (немедленно)
ACTIVE_PATCH_ID = "ST_STOCK_GREETING_V2"


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


def with_preamble(content):
    return PREAMBLE + content.strip()


def main():
    apply = "--apply" in sys.argv
    env = load_env()
    blocks = fetch_all(env, IDS["Prompt_Blocks"])
    by_id = {b["Block_ID"]: b for b in blocks}
    print(f"живых блоков: {len(blocks)} | режим: {'APPLY' if apply else 'dry-run'}\n")

    posts, patches = [], []  # patches: (row_ref_for_print, body)

    # --- 1. черновики _V2 для блоков со свободным слотом ---
    for base in DRAFT_BASES:
        src = by_id.get(base)
        if src is None:
            sys.exit(f"в живой таблице нет {base} — таблица дрейфнула, пересоберите список")
        v2id = base + "_V2"
        content = with_preamble(src["Content"])
        row = {
            "Block_ID": v2id, "Level": src.get("Level", "stage_task"),
            "Scenario": src.get("Scenario"), "Stage": "greeting",
            "Version_Set": "v3.04", "Content": content,
            "Tokens_est": round(len(content) / 4), "Active": False,
            "Intent_Triggers": "",
            "Notes": ("Черновик v3.04: greeting + ПРЕАМБУЛ правил (имя без «Клиент», "
                      "не выводить шаблоны [Модель]/[Комплектация] и имена переменных "
                      f"при отсутствии данных). Активация: включить {v2id}, выключить "
                      f"{base} — компилятор сам предпочтёт V2 через pick()."),
        }
        existing = by_id.get(v2id)
        if existing is None:
            posts.append(row)
            print(f"  POST  {v2id}: новый черновик, {len(content)} зн. (Active=false)")
        elif PREAMBLE_MARKER in (existing.get("Content") or ""):
            print(f"  skip  {v2id}: преамбул уже дописан")
        elif not existing.get("Active"):
            patches.append((v2id, {"Id": existing["Id"], "Content": content,
                                   "Tokens_est": round(len(content) / 4)}))
            print(f"  PATCH {v2id}: инертный черновик → унифицирую преамбулом, {len(content)} зн.")
        else:
            print(f"  ⚠️  {v2id}: АКТИВЕН — пропускаю (не трогаю активный блок владельца)")

    # --- 2. активный stock _V2: дописать преамбул напрямую (немедленный эффект) ---
    stock = by_id.get(ACTIVE_PATCH_ID)
    if stock is None:
        print(f"  ⚠️  {ACTIVE_PATCH_ID}: нет в живой таблице — пропускаю")
    elif PREAMBLE_MARKER in (stock.get("Content") or ""):
        print(f"  skip  {ACTIVE_PATCH_ID}: преамбул уже дописан")
    else:
        content = with_preamble(stock["Content"])
        patches.append((ACTIVE_PATCH_ID, {"Id": stock["Id"], "Content": content,
                                          "Tokens_est": round(len(content) / 4)}))
        print(f"  PATCH {ACTIVE_PATCH_ID}: АКТИВЕН — преамбул дописан НЕМЕДЛЕННО, "
              f"{len(content)} зн. (контент владельца сохранён 1:1)")

    if not apply:
        print(f"\ndry-run: POST {len(posts)}, PATCH {len(patches)}. Повторить с --apply.")
        return

    if posts:
        api(env, "POST", f"/api/v2/tables/{IDS['Prompt_Blocks']}/records", posts)
        print(f"\nдобавлено черновиков: {len(posts)} (все Active=false)")
    if patches:
        api(env, "PATCH", f"/api/v2/tables/{IDS['Prompt_Blocks']}/records",
            [body for _, body in patches])
        print(f"обновлено блоков (PATCH): {len(patches)}")


if __name__ == "__main__":
    main()
