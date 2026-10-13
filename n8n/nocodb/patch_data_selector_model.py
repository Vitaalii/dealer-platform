#!/usr/bin/env python3
# СТАТУС: ПРИМЕНЕНО И ПЕРЕНЕСЕНО В ГЕНЕРАТОР (2026-08-30).
# Содержимое этих узлов теперь печатает сам nocodb/build_v3_workflow.py,
# поэтому запускать скрипт повторно НЕ НУЖНО — в diff-режиме он честно
# показывает «нечего менять». Оставлен как след двух правок и как
# описание того, чем они проверялись; удалять не надо, но и применять
# больше незачем: единственный источник истины — генератор.
"""Data Selector: имя модели на INIT-ходе (ТЗ 2026-08-21).

Дефект приёмки: в приветствии вместо модели и комплектации — «автомобиля Elite».
Правило подстановки в ST_CREDIT_GREETING_V2 отработало верно: имени модели в
контексте не было, и фраза переформулирована без него. Пустым оно оказалось не
случайно.

Замер по логам (все сохранённые ходы 19 и 21 августа): на ходе INIT_AI_HANDOFF
`model` пуст ВСЕГДА, со второго хода — всегда заполнен. Причина видна на входе
ноды:

    exec 3325 (INIT):
      item.model                        = null
      item.qualification_context.model  = null        <- читаем только это
      item.fresh_cache.model            = "HAVAL JOLION"
      qc.cached_json.model              = "HAVAL JOLION"

Бот шлёт имя модели в `qualification_context.model` начиная со ВТОРОГО хода; на
ходе передачи в payload только числовой `model_id`. Но внутри n8n имя уже есть:
`Call Car Search` его вернул, `Attach Cache` положил и в `fresh_cache`, и в
`qualification_context.cached_json`. Нода туда просто не заглядывала — при том
что соседние комплектации запасную цепочку имеют:

    if (cached.complectations) { rawComps = cached.complectations; }
    else if (data.complectations) { rawComps = data.complectations; }

Правка добавляет ту же цепочку для `model` во ВСЕХ ПЯТИ ветках.

Порядок источников: сначала прежние (`data.model`, `qc.model` — то, что шлёт
бот), затем `cached_json.model` (по образцу комплектаций), затем
`fresh_cache.model` как сеть для холодного старта. По логам на INIT последние
два заполнены и равны, на поздних ходах `fresh_cache` бывает null, а
`cached_json` заполнен — поэтому нужны оба.

Подтверждение структуры по логам:
    credit  — 13 исполнений, tradein — 5, combo — 4: форма одинакова везде.
    stock и consult — исполнений в ретенции (~2.5 суток) НЕТ, структура
    подтверждена только по коду: обе ветки читают тот же `qc.cached_json` и уже
    используют его для комплектаций. Первый же живой диалог в этих сценариях
    стоит проверить отдельно.

Правится канонический flow/AI Agent v3.0.json; [TEST]-копия перегенерируется
через build_test_contour.py. Скрипт идемпотентен.

Запуск:
  python3 patch_data_selector_model.py           # показать, что изменится
  python3 patch_data_selector_model.py --apply
"""

raise SystemExit(
    "СКРИПТ ОБЕЗВРЕЖЕН — ЗАПУСКАТЬ НЕЛЬЗЯ.\n"
    "\n"
    "Правка Data Selector применена 2026-08-21 и перенесена в генератор 2026-08-30\n"
    "(коммит a1bd6e7). С тех пор код узла собирает генератор, а не этот файл.\n"
    "Скрипт остался заряженным: он перезаписывает flow/AI Agent v3.0.json телом\n"
    "узла, вшитым ниже, и молча откатывает всё, что вошло в генератор позже.\n"
    "\n"
    "Отдельная опасность именно этого узла: в генераторе Data Selector не константа,\n"
    "а результат вывода из трим-функций боевого v2 (SCENARIO_TRIM_NODES) плюс точечная\n"
    "замена строки выбора модели (MODEL_FALLBACK / apply_model_fallback), и функция\n"
    "падает, если форма кода в v2 изменилась. Вшитая сюда копия этой защиты не несёт —\n"
    "она заморозила состояние на 2026-08-21 и паритет срезов данных с v2 не проверяет.\n"
    "\n"
    "ГДЕ ПРАВИТЬ НА САМОМ ДЕЛЕ: nocodb/build_v3_workflow.py, функция\n"
    "build_data_selector_js() и таблица MODEL_FALLBACK. После правки — перегенерация:\n"
    "    python3 nocodb/build_v3_workflow.py && python3 nocodb/build_test_contour.py\n"
    "\n"
    "Файл сохранён ради докстроки выше: в ней разобран замер по логам, показавший,\n"
    "что на ходе INIT_AI_HANDOFF имя модели приходит пустым всегда, и почему запасная\n"
    "цепочка берётся из fresh_cache. Удалять и переименовывать нельзя — на этот путь\n"
    "ссылается docs/handoff/2026-08-30-combo-tool-name-and-generator.md.\n"
    "\n"
    "Снять запрет — только осознанно и с ревизией вшитого тела узла на актуальность."
)

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FLOW = HERE.parent / "flow" / "AI Agent v3.0.json"

FRESH = '(data.fresh_cache || {}).model'
FRESH_INPUT = '(input.fresh_cache || {}).model'

# (ветка, было, стало) — по одной точной замене на ветку, каждая обязана
# встретиться в тексте ровно один раз.
REPLACEMENTS = [
    ("trim_credit",
     '"model": data.model || qc.model || "",',
     f'"model": data.model || qc.model || cached.model || {FRESH} || "",'),
    ("trim_consult",
     '"model": input.model || qc.model || "",',
     f'"model": input.model || qc.model || cached.model || {FRESH_INPUT} || "",'),
    # у trim_stock нет локальной привязки cached — читаем тем же способом,
    # каким она сама читает комплектации, инлайном
    ("trim_stock",
     '"model": data.model || (data.qualification_context ? data.qualification_context.model : ""),',
     '"model": data.model'
     ' || (data.qualification_context ? data.qualification_context.model : "")'
     ' || ((data.qualification_context || {}).cached_json || {}).model'
     f' || {FRESH} || "",'),
    ("trim_tradein",
     '"model": data.model || qc.model || "",',
     f'"model": data.model || qc.model || cached.model || {FRESH} || "",'),
    ("trim_combo",
     '"model": data.model || qc.model || "",',
     f'"model": data.model || qc.model || cached.model || {FRESH} || "",'),
]


def main():
    apply_mode = "--apply" in sys.argv
    wf = json.loads(FLOW.read_text(encoding="utf-8"))
    node = next((n for n in wf["nodes"] if n["name"] == "Data Selector"), None)
    if node is None:
        sys.exit("ОСТАНОВ: в воркфлоу нет ноды Data Selector")
    code = node["parameters"]["jsCode"]

    done, todo = [], []
    for branch, old, new in REPLACEMENTS:
        if new in code:
            done.append(branch)
            continue
        n = code.count(old)
        if n == 0:
            sys.exit(f"ОСТАНОВ: в {branch} не найдена строка для замены:\n  {old}")
        # trim_credit / trim_tradein / trim_combo делят одинаковую строку —
        # заменяем их одной операцией replace по всем вхождениям, но считать
        # обязаны честно: сколько осталось, столько и заменится.
        todo.append((branch, old, new, n))

    for b in done:
        print(f"  {b}: правка уже на месте")
    if not todo:
        print("\nнечего менять")
        return

    print(f"\nветок к правке: {len(todo)}")
    for branch, old, _, n in todo:
        print(f"  {branch:14s} вхождений искомой строки в ноде: {n}")

    if not apply_mode:
        print("\n(diff-режим: ничего не записано; --apply)")
        return

    for _, old, new, _ in todo:
        code = code.replace(old, new)
    # контроль: ни одной необработанной строки прежней формы не осталось
    leftovers = [b for b, old, _, _ in todo if old in code]
    if leftovers:
        sys.exit(f"ОСТАНОВ: после замены остались прежние строки в {leftovers} — "
                 f"файл не записан")
    node["parameters"]["jsCode"] = code
    FLOW.write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nзаписан {FLOW}")
    print("Дальше: python3 build_test_contour.py — перегенерировать [TEST]-копию.")


if __name__ == "__main__":
    main()
