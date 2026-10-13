#!/usr/bin/env python3
"""Де-брендинг активных промпт-блоков: убрать марку/модель и мессенджер.

План: docs/plans/2026-09-16-001-fix-debrand-prompt-blocks-plan.md (U1, U2).

Таблица Prompt_Blocks одна на всех дилеров и все бренды, Prompt Compiler читает
её на каждом ходе. Четыре активных блока несут конкретику, которая верна в
лучшем случае для одного бренда из планируемых пяти-восьми:

  * три REFERENCE-блока — пример фразы «Haval Dargo в комплектации Elite»;
  * G-02_VOICE_V2 — «длинные тексты в Telegram не читают».

Марка и модель заменяются на домовой стандарт «[Модель] в комплектации
[Комплектация]». Это плейсхолдеры КЛАССА B: их не подставляет ни Prompt
Compiler, ни Data Selector, ни бот — заполняет сама модель из блока
<DATA_FROM_DATABASE>. В правимой фразе такой плейсхолдер уже стоит
([credit_price] / [base_price]), поэтому механизм не меняется; замер на живых
исполнениях 2026-09-16 показал, что модель класс B заполняет исправно
(исполнение 4395: "model": "НОВЫЙ HAVAL F7" -> «HAVAL F7 в комплектации
Premium», утечек шаблонов 0 на 18 ходах).

Мессенджер заменяется нейтральным «в переписке» — без подстановки: правило в
этом пункте про объём ответа, конкретика канала в нём не работает, а
плейсхолдер канала потребовал бы правки воркфлоу и нового поля в payload бота.

Смысл и структура правил не меняются: правится только конкретика.

Протокол записи — конвенция репозитория (см. apply_cabinet_finalize_ban.py):
  1. ОДНА ЗАПИСЬ — ОДИН PATCH. Батчевый PATCH объединяет набор колонок по пачке
     и обнуляет отсутствующие поля.
  2. ЧТЕНИЕ ДО И ПОСЛЕ, сверка по ВСЕМ колонкам.
  3. СТОП НА ПЕРВОМ РАСХОЖДЕНИИ, откат из снапшота.
Идемпотентность: повторный --apply печатает нули.

Три гарда на каждую запись:
  * BASE_SHA — sha256[:12] Content до правки. Расхождение значит, что блок
    правил кто-то ещё: пишем поверх невиданного текста — стоп.
  * ровно одно вхождение искомого фрагмента — иначе замена затронула бы не то,
    что задумано.
  * обратное чтение по всем колонкам после PATCH.

Запуск:
  python3 apply_debrand_blocks.py                      # diff, ничего не пишет
  python3 apply_debrand_blocks.py --apply
  python3 apply_debrand_blocks.py --rollback           # вернуть всё из снапшота
  python3 apply_debrand_blocks.py --rollback --only G-02_VOICE_V2
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
SNAPSHOT = ROOT / "flow" / "lib" / "snapshots" / "pre_apply_debrand.json"

EDIT_TAG = f"[ПРАВКА {date(2026, 9, 16).isoformat()}]"
REASON = ("де-брендинг: марка/модель -> [Модель]/[Комплектация], мессенджер -> "
          "нейтральная формулировка (общая таблица на 5-8 брендов и два канала)")

BLOCK_COLS = ["Block_ID", "Level", "Scenario", "Stage", "Version_Set",
              "Content", "Tokens_est", "Active", "Intent_Triggers", "Notes"]

MODEL_OLD = "Haval Dargo в комплектации Elite"
MODEL_NEW = "[Модель] в комплектации [Комплектация]"
VOICE_OLD = "длинные тексты в Telegram не читают"
VOICE_NEW = "в переписке длинные тексты не читают"

# sha256[:12] Content до правки — снято с живой таблицы 2026-09-16.
# BLK_TRADEIN_REFERENCE и BLK_COMBO_REFERENCE несут побайтово одинаковый
# Content, поэтому у них общий BASE_SHA.
TARGETS = [
    ("BLK_CREDIT_REFERENCE",  MODEL_OLD, MODEL_NEW, "a634147e9730"),
    ("BLK_TRADEIN_REFERENCE", MODEL_OLD, MODEL_NEW, "2192621719ce"),
    ("BLK_COMBO_REFERENCE",   MODEL_OLD, MODEL_NEW, "2192621719ce"),
    ("G-02_VOICE_V2",         VOICE_OLD, VOICE_NEW, "df0fa3bcf287"),
]


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
            + f"\n  откат: python3 apply_debrand_blocks.py --rollback")
    done.append(label)


def sha12(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def tokens_est(text):
    """Конвенция таблицы: round(len(Content)/4). Снято с живых блоков —
    медиана len/Tokens_est по 93 активным блокам 4.008."""
    return round(len(text) / 4)


def note_with(current):
    current = (current or "").strip()
    if current.startswith(EDIT_TAG):
        return current
    return f"{EDIT_TAG} {REASON}\n{current}".strip()


def plan_edits(by_block):
    """Возвращает (todo, already). Стоп-условия выбрасывают SystemExit."""
    todo, already = [], []
    for bid, old, new, base_sha in TARGETS:
        row = by_block.get(bid)
        if row is None:
            sys.exit(f"ОСТАНОВ: в живой таблице нет блока {bid}")
        content = row.get("Content") or ""

        if new in content and old not in content:
            already.append(bid)
            continue

        got = sha12(content)
        if got != base_sha:
            sys.exit(
                f"ОСТАНОВ: {bid} — Content не тот, поверх которого правка "
                f"задумывалась (sha {got}, ждали {base_sha}). Блок правил кто-то "
                f"ещё: перечитайте текст и обновите BASE_SHA в TARGETS.")

        hits = content.count(old)
        if hits != 1:
            sys.exit(
                f"ОСТАНОВ: {bid} — искомый фрагмент встречается {hits} раз, "
                f"ожидалось ровно 1. Замена затронула бы не то, что задумано.")

        todo.append((bid, row, content, content.replace(old, new)))
    return todo, already


def do_rollback(env, only):
    if not SNAPSHOT.exists():
        sys.exit(f"ОСТАНОВ: снапшота нет — {SNAPSHOT}")
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    ids = [only] if only else list(snap)
    missing = [b for b in ids if b not in snap]
    if missing:
        sys.exit(f"ОСТАНОВ: в снапшоте нет блоков {missing}")

    done = []
    for bid in ids:
        saved = snap[bid]
        cur = read_one(env, IDS["Prompt_Blocks"], saved["Id"])
        if (cur.get("Content") or "") == (saved.get("Content") or ""):
            print(f"  {bid}: уже в состоянии снапшота — пропуск")
            continue
        fields = {"Content": saved.get("Content"),
                  "Tokens_est": saved.get("Tokens_est"),
                  "Notes": saved.get("Notes")}
        write_verified(env, IDS["Prompt_Blocks"], cur, fields, BLOCK_COLS, bid, done)
        print(f"  {bid}: возвращён, sha {sha12(saved.get('Content'))}, "
              f"Tokens_est {saved.get('Tokens_est')}")
    print(f"откат: возвращено {len(done)} блоков")


def main():
    args = sys.argv[1:]
    apply_mode = "--apply" in args
    rollback = "--rollback" in args
    only = args[args.index("--only") + 1] if "--only" in args else None
    env = load_env()

    if rollback:
        print(f"откат из снапшота {SNAPSHOT.relative_to(ROOT)}"
              + (f", только блок {only}" if only else ", все блоки"))
        do_rollback(env, only)
        return

    blocks = fetch_all(env, IDS["Prompt_Blocks"])
    by_block = {b["Block_ID"]: b for b in blocks}
    print(f"живые таблицы: блоков {len(blocks)}")

    todo, already = plan_edits(by_block)
    print(f"к записи: {len(todo)} ({', '.join(b for b, *_ in todo) or '—'})")
    print(f"уже применено: {len(already)} ({', '.join(already) or '—'})")
    if not todo:
        print("нечего делать")
        return

    total_delta = 0
    for bid, row, cur, new in todo:
        total_delta += len(new) - len(cur)
        old_line = next(l for l in cur.split("\n")
                        if (MODEL_OLD in l or VOICE_OLD in l))
        new_line = next(l for l in new.split("\n")
                        if (MODEL_NEW in l or VOICE_NEW in l))
        print(f"\n--- {bid}")
        print(f"    Content {len(cur)} -> {len(new)} симв. ({len(new) - len(cur):+d}), "
              f"Tokens_est {row.get('Tokens_est')} -> {tokens_est(new)}")
        print(f"    sha {sha12(cur)} -> {sha12(new)}")
        print(f"    БЫЛО:  {old_line.strip()}")
        print(f"    СТАЛО: {new_line.strip()}")
    print(f"\nсуммарно Content: {total_delta:+d} символов")

    if not apply_mode:
        print("\nрежим diff: ничего не записано. Для записи — флаг --apply")
        return

    # Снапшот пишется РОВНО ОДИН РАЗ, при первом --apply.
    #
    # Почему не «перезаписать текущим состоянием»: повторный --apply (после
    # частичного отката, после доводки одного блока) видит в базе уже
    # ПРАВЛЕНЫЙ текст остальных блоков. Перезапись подменила бы состояние «до»
    # состоянием «после» — путь отката молча исчез бы ровно в тот момент, когда
    # он нужнее всего. Ошибка поймана на живой правке 2026-09-16: второй --apply
    # после проверки отката испортил три записи из четырёх.
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    if SNAPSHOT.exists():
        print(f"\nснапшот уже есть, НЕ перезаписываю: {SNAPSHOT.relative_to(ROOT)}")
        saved = set(json.loads(SNAPSHOT.read_text(encoding="utf-8")))
        gap = [bid for bid, *_ in todo if bid not in saved]
        if gap:
            sys.exit(f"ОСТАНОВ: в снапшоте нет блоков {gap}, а писать их "
                     f"собираемся — откатывать будет нечем. Уберите "
                     f"{SNAPSHOT.name} только вместе с откатом уже применённого.")
    else:
        SNAPSHOT.write_text(json.dumps(
            {b["Block_ID"]: b for b in blocks
             if b["Block_ID"] in {t[0] for t in TARGETS}},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nснапшот до записи: {SNAPSHOT.relative_to(ROOT)}")

    done = []
    for bid, row, cur, new in todo:
        fields = {"Content": new,
                  "Tokens_est": tokens_est(new),
                  "Notes": note_with(row.get("Notes"))}
        write_verified(env, IDS["Prompt_Blocks"], row, fields, BLOCK_COLS, bid, done)
        print(f"записан {bid}: sha {sha12(new)}, Tokens_est {fields['Tokens_est']}")

    print(f"\nготово: записано {len(done)} блоков")


if __name__ == "__main__":
    main()
