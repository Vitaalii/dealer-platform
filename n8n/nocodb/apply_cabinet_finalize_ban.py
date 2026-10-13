#!/usr/bin/env python3
"""Запрет вызова finalize_deal на ходе выдачи ссылки на кабинет.

Основание: исполнение #4084 (2026-09-08, сделка 51499) и шесть прогонов
воспроизведения на [TEST]-контуре 2026-09-09 (#4237, #4240, #4242, #4244,
#4247, #4249). На интенте choose_cabinet модель в двух прогонах из шести
вызывала finalize_deal со статусом in_progress, обрывая диалог сразу после
выдачи ссылки — при том, что сама же заканчивала сообщение вопросом
«Получилось зайти?» и ждала ответа.

Входы шести исполнений сверены побайтово и идентичны (одинаковый
system_prompt_compiled, одинаковые messages, promptTokens 5152 во всех
шести). Причина расхождения 2/6 НЕ УСТАНОВЛЕНА. Эта правка — не устранение
причины, а запрет на видном месте ровно на том ходе, где сбой наблюдался.

Почему текст такой (решения, не украшения):
  * отдельная строка со своей меткой, а не растворение в правиле тактики:
    модель нарушает не текстовое требование, а ДЕЙСТВИЕ — G-07_CABINET уже
    велит продолжать диалог, и модель это выполняет;
  * инструмент назван по имени и запрещён поимённо по всем пяти статусам:
    оба наблюдавшихся вызова были со статусом in_progress, выбранным по
    остаточному принципу, и формулировка «ни с каким статусом» оставила бы
    его в серой зоне;
  * прямо сказано, чем выдача ссылки НЕ является: оба вызова несли next_step
    «Визит в кабинет для ознакомления» и «Визит в личный кабинет»;
  * проверка перед отправкой воспроизводит единственный приём этого блока,
    который модель соблюдает стопроцентно: маркер [CABINET_LINK] стоял во
    всех шести прогонах, включая оба сбойных, и он введён именно через
    «Перед отправкой проверь».

Три блока несут побайтово одинаковый Content и подключаются только по
Intent_Triggers=choose_cabinet — то есть ровно на сбойном ходе и ни на одном
другом. Ложного применения к законным финализациям на прочих интентах быть
не может по построению.

Протокол записи — конвенция репозитория (см. apply_prompt_edits_v308.py):
  1. ОДНА ЗАПИСЬ — ОДИН PATCH. Батчевый PATCH объединяет набор колонок по
     пачке и обнуляет отсутствующие поля.
  2. ЧТЕНИЕ ДО И ПОСЛЕ, сверка по ВСЕМ колонкам.
  3. СТОП НА ПЕРВОМ РАСХОЖДЕНИИ, откат из снапшота.
Идемпотентность: повторный --apply печатает нули.

Запуск:
  python3 apply_cabinet_finalize_ban.py            # diff, ничего не пишет
  python3 apply_cabinet_finalize_ban.py --apply
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
SNAPSHOT = ROOT / "flow" / "lib" / "snapshots" / "pre_apply_cabinet_ban.json"

EDIT_TAG = f"[ПРАВКА {date(2026, 9, 9).isoformat()}]"
REASON = ("запрет вызова finalize_deal на ходе выдачи ссылки (инцидент #4084, "
          "воспроизведение 2 из 6 на [TEST] 2026-09-09)")

TARGETS = ["BLK_CREDIT_CABINET_CHOSEN",
           "BLK_TRADEIN_CABINET_CHOSEN",
           "BLK_COMBO_CABINET_CHOSEN"]

# sha256[:12] Content до правки — одинаков у всех трёх (снято 2026-09-09).
# Расхождение означает, что блок правил кто-то ещё: пишем поверх невиданного
# текста — стоп.
BASE_SHA = "4d5cd1f65fa2"

BLOCK_COLS = ["Block_ID", "Level", "Scenario", "Stage", "Version_Set",
              "Content", "Tokens_est", "Active", "Intent_Triggers", "Notes"]

# Утверждено владельцем 2026-09-09 без изменений. Дописывается В КОНЕЦ блока,
# существующий текст не меняется ни на символ.
BAN_TEXT = (
    "ИНСТРУМЕНТЫ НА ЭТОМ ХОДЕ: инструмент finalize_deal ты НЕ вызываешь — ни со "
    "статусом in_progress, ни call, ни visit, ни refuse, ни ban. Выдача ссылки не "
    "является ни визитом, ни звонком, ни паузой, ни завершением диалога: клиент ещё "
    "не в кабинете, ты сам заканчиваешь сообщение вопросом «Получилось зайти?» и "
    "ждёшь на него ответа. Заявка уйдёт в работу позже — когда клиент подтвердит "
    "номер при авторизации в кабинете либо ответит, что войти не получилось.\n"
    "Перед отправкой проверь ещё раз: этот ход — только текст. Если ты собрался "
    "вызвать инструмент, отмени вызов и отправь одно сообщение."
)


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


def main():
    apply_mode = "--apply" in sys.argv
    env = load_env()

    blocks = fetch_all(env, IDS["Prompt_Blocks"])
    by_block = {b["Block_ID"]: b for b in blocks}
    print(f"живые таблицы: блоков {len(blocks)}")

    missing = [b for b in TARGETS if b not in by_block]
    if missing:
        sys.exit(f"ОСТАНОВ: в живой таблице нет блоков {missing}")

    todo, already = [], []
    for bid in TARGETS:
        content = by_block[bid]["Content"] or ""
        if BAN_TEXT in content:
            already.append(bid)
            continue
        got = sha12(content)
        if got != BASE_SHA:
            sys.exit(f"ОСТАНОВ: {bid} — Content не тот, поверх которого правка "
                     f"задумывалась (sha {got}, ждали {BASE_SHA}). Блок правил "
                     f"кто-то ещё: перечитайте текст и обновите BASE_SHA.")
        todo.append(bid)

    print(f"к записи: {len(todo)} ({', '.join(todo) or '—'})")
    print(f"уже применено: {len(already)} ({', '.join(already) or '—'})")
    if not todo:
        print("нечего делать")
        return

    for bid in todo:
        cur = by_block[bid]["Content"]
        print(f"\n--- {bid}: {len(cur)} -> {len(cur.rstrip() + chr(10) + BAN_TEXT)} символов")
        print(f"    sha до: {sha12(cur)}  после: {sha12(cur.rstrip() + chr(10) + BAN_TEXT)}")

    if not apply_mode:
        print("\nрежим diff: ничего не записано. Для записи — флаг --apply")
        return

    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(
        {b["Block_ID"]: b for b in blocks if b["Block_ID"] in TARGETS},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nснапшот до записи: {SNAPSHOT}")

    done = []
    for bid in todo:
        cur = by_block[bid]
        new_content = cur["Content"].rstrip() + "\n" + BAN_TEXT
        fields = {"Content": new_content,
                  "Tokens_est": len(new_content) // 4,
                  "Notes": note_with(cur.get("Notes"))}
        write_verified(env, IDS["Prompt_Blocks"], cur, fields, BLOCK_COLS, bid, done)
        print(f"записан {bid}: sha {sha12(new_content)}, Tokens_est {fields['Tokens_est']}")

    print(f"\nготово: записано {len(done)} блоков")


if __name__ == "__main__":
    main()
