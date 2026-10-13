#!/usr/bin/env python3
"""U8: поведение Александра при ошибке госномера — различимые ветки и выход из цикла.

Вторая половина плана 2026-09-08-001. Первая (U5-U7, нормализация госномера и
различимые статусы инструмента) выкачена в бой 2026-09-08 12:41; без этой
правки она бесполезна: инструмент научился называть причину, а блоки
по-прежнему отвечали на любую причину одной фразой.

Инцидент, который закрывается: клиент прислал «T582XP 33 rus», инструмент
отверг, Александр дважды подряд повторил одну и ту же фразу «номер кажется
неполным», клиент ушёл по таймауту. Выхода из цикла в блоках не было вовсе.

Что пишется:
  ST_TRADEIN_TOOL_NEEDED_V2, ST_COMBO_TOOL_NEEDED_V2 — строка про INVALID_DATA
      заменяется тремя: ветка NO_REGION (называет разобранную часть номера и
      просит код региона), переписанная ветка INVALID_DATA (пересказ причины
      из message, запрет дословного повтора прошлой формулировки) и правило
      выхода после ВТОРОЙ неудачи подряд — предложение осмотра вместо
      третьего запроса номера;
  D_TRADEIN, D_COMBO — словарь статусов инструмента: дописываются NO_REGION и
      INVALID_DATA, добавляется строка про plate_seen. INVALID_DATA в словарях
      отсутствовал и до этой правки, хотя инструмент его возвращал.

Тексты утверждены владельцем 2026-09-09. Ветка NO_REGION утверждена ранее и
подтверждена живым диалогом (клиент прислал «х318те», Александр спросил именно
код региона, клиент дописал «750», оценка отработала).

Счёт «две неудачи подряд» модель ведёт по recent_history — отдельного счётчика
в session_context нет намеренно (решение владельца: не заводить, пока
необходимость не доказана; это зона второго агента).

ОТЛОЖЕНО, НЕ ЗАБЫТО: разведение сообщений внутри INVALID_DATA. Сегодня четыре
разных входа (цифры вместо номера, VIN, номер склеенный с пробегом, марка с
годом) дают модели ОДИН И ТОТ ЖЕ message и ни одного plate_seen — различать их
она может только по тексту клиента. Причина отсрочки: не смешивать две правки
в одном замере, и сейчас идёт наблюдение за правкой блоков кабинетной тактики
(apply_cabinet_finalize_ban.py) — лишний импорт в бой добавил бы в него
переменную. Вернуться, когда по живым диалогам станет видно, путается ли
модель на общей фразе.

Протокол записи — конвенция репозитория (см. apply_prompt_edits_v308.py):
  1. ОДНА ЗАПИСЬ — ОДИН PATCH.
  2. ЧТЕНИЕ ДО И ПОСЛЕ, сверка по ВСЕМ колонкам.
  3. СТОП НА ПЕРВОМ РАСХОЖДЕНИИ, откат из снапшота.
Идемпотентность: повторный --apply печатает нули.

Запуск:
  python3 apply_plate_error_branches.py            # diff, ничего не пишет
  python3 apply_plate_error_branches.py --apply
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
IDS = json.loads((HERE / "table_ids.json").read_text(encoding="utf-8"))
SNAPSHOT = ROOT / "flow" / "lib" / "snapshots" / "pre_apply_plate_errors.json"

EDIT_TAG = f"[ПРАВКА {date(2026, 9, 9).isoformat()}]"
REASON = ("U8: различимые ветки ошибки госномера (NO_REGION / INVALID_DATA) "
          "и выход после второй неудачи вместо третьего запроса номера")

BLOCK_COLS = ["Block_ID", "Level", "Scenario", "Stage", "Version_Set",
              "Content", "Tokens_est", "Active", "Intent_Triggers", "Notes"]

# sha256[:12] Content до правки, снято 2026-09-09. Расхождение = блок правил
# кто-то ещё, пишем поверх невиданного текста — стоп.
BASE_SHA = {
    "ST_TRADEIN_TOOL_NEEDED_V2": "ce562b9ee397",
    "ST_COMBO_TOOL_NEEDED_V2":   "296adf7f6ee5",
    "D_TRADEIN":                 "39d6cff13b9c",
    "D_COMBO":                   "f39c61f1ed62",
}

# --- ветки ошибки в двух ST-блоках -------------------------------------------
OLD_INVALID = ("- INVALID_DATA: скажи, что номер кажется неполным, и попроси "
               "корректный госномер. Получив новый номер — снова вызови инструмент.")

NEW_BRANCHES = (
    "- NO_REGION: инструмент разобрал номер, но кода региона в нём нет. Назови "
    "клиенту то, что видно в `plate_seen`, и попроси дописать код региона: «Вижу "
    "О427ВН — допишите, пожалуйста, код региона, две-три цифры в конце». Получив "
    "номер — снова вызови инструмент.\n"
    "- INVALID_DATA: перескажи причину из `message` своими словами и попроси "
    "написать госномер целиком; если в `plate_seen` что-то есть — назови это. "
    "Дословно свою прошлую формулировку НЕ повторяй: если ты уже спрашивал про "
    "номер в этом диалоге, спроси иначе.\n"
    "- ПОСЛЕ ВТОРОЙ НЕУДАЧИ ПОДРЯД инструмент больше НЕ вызывай. Считай по истории "
    "диалога: два твоих обращения за номером, между которыми не было успешной "
    "оценки. Отключи правило «Зеркальность», скажи, что по номеру не выходит, и "
    "предложи живой осмотр в салоне или звонок оценщика. Закончи вопросом («В какой "
    "день вам было бы удобно заехать?»). ОТПРАВЬ И ЖДИ ОТВЕТА."
)

# --- словари статусов в двух D-блоках ----------------------------------------
OLD_MESSAGE_LINE = "- `message` — Системное сообщение (приходит при ошибках и задержках баз)."
NEW_MESSAGE_LINE = (OLD_MESSAGE_LINE + "\n"
                    "- `plate_seen` — разобранная часть госномера; приходит при "
                    "`NO_REGION` и при неверном коде региона.")

STATUS_LINES = {
    "D_TRADEIN": (
        "- `status` — Статус оценки (`SUCCESS`, `IN_PROGRESS`, `NOT_FOUND`, `ERROR`).",
        "- `status` — Статус оценки (`SUCCESS`, `NO_REGION`, `INVALID_DATA`, "
        "`IN_PROGRESS`, `NOT_FOUND`, `ERROR`)."),
    "D_COMBO": (
        "- `status` — Статус расчета (`success`, `impossible`, `IN_PROGRESS`, "
        "`error`, `NOT_FOUND`).",
        "- `status` — Статус расчета (`success`, `impossible`, `NO_REGION`, "
        "`INVALID_DATA`, `IN_PROGRESS`, `error`, `NOT_FOUND`)."),
}

TARGETS = ["ST_TRADEIN_TOOL_NEEDED_V2", "ST_COMBO_TOOL_NEEDED_V2",
           "D_TRADEIN", "D_COMBO"]


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
    """Правило 1+2+3: одна запись — один PATCH, обратное чтение, стоп при сносе."""
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


def sha12(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def note_with(current):
    current = (current or "").strip()
    if current.startswith(EDIT_TAG):
        return current
    return f"{EDIT_TAG} {REASON}\n{current}".strip()


def transform(bid, content):
    """Новый Content блока. Возвращает None, если правка уже применена."""
    if bid.startswith("ST_"):
        if NEW_BRANCHES.split("\n")[0] in content:
            return None
        if content.count(OLD_INVALID) != 1:
            sys.exit(f"ОСТАНОВ: {bid} — строка INVALID_DATA встречается "
                     f"{content.count(OLD_INVALID)} раз, ждали ровно одну")
        return content.replace(OLD_INVALID, NEW_BRANCHES)

    old_status, new_status = STATUS_LINES[bid]
    if "`plate_seen`" in content:
        return None
    for needle in (old_status, OLD_MESSAGE_LINE):
        if content.count(needle) != 1:
            sys.exit(f"ОСТАНОВ: {bid} — строка {needle[:40]!r} встречается "
                     f"{content.count(needle)} раз, ждали ровно одну")
    return content.replace(old_status, new_status).replace(OLD_MESSAGE_LINE, NEW_MESSAGE_LINE)


def main():
    apply_mode = "--apply" in sys.argv
    env = load_env()

    blocks = fetch_all(env, IDS["Prompt_Blocks"])
    by_block = {b["Block_ID"]: b for b in blocks}
    print(f"живые таблицы: блоков {len(blocks)}")

    missing = [b for b in TARGETS if b not in by_block]
    if missing:
        sys.exit(f"ОСТАНОВ: в живой таблице нет блоков {missing}")

    plan, already = [], []
    for bid in TARGETS:
        content = by_block[bid]["Content"] or ""
        new = transform(bid, content)
        if new is None:
            already.append(bid)
            continue
        got = sha12(content)
        if got != BASE_SHA[bid]:
            sys.exit(f"ОСТАНОВ: {bid} — Content не тот, поверх которого правка "
                     f"задумывалась (sha {got}, ждали {BASE_SHA[bid]}). Блок правил "
                     f"кто-то ещё: перечитайте текст и обновите BASE_SHA.")
        plan.append((bid, content, new))

    print(f"к записи: {len(plan)} ({', '.join(b for b, _, _ in plan) or '—'})")
    print(f"уже применено: {len(already)} ({', '.join(already) or '—'})")
    if not plan:
        print("нечего делать")
        return

    for bid, old, new in plan:
        print(f"\n--- {bid}: {len(old)} -> {len(new)} символов")
        print(f"    sha до: {sha12(old)}  после: {sha12(new)}")

    if not apply_mode:
        print("\nрежим diff: ничего не записано. Для записи — флаг --apply")
        return

    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(
        {b["Block_ID"]: b for b in blocks if b["Block_ID"] in TARGETS},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nснапшот до записи: {SNAPSHOT}")

    done = []
    for bid, _, new in plan:
        cur = by_block[bid]
        fields = {"Content": new,
                  "Tokens_est": len(new) // 4,
                  "Notes": note_with(cur.get("Notes"))}
        write_verified(env, IDS["Prompt_Blocks"], cur, fields, BLOCK_COLS, bid, done)
        print(f"записан {bid}: sha {sha12(new)}, Tokens_est {fields['Tokens_est']}")

    print(f"\nготово: записано {len(done)} блоков")


if __name__ == "__main__":
    main()
