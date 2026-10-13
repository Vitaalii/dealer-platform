#!/usr/bin/env python3
"""U8: генератор данных трёх таблиц NocoDB (Intents / Prompt_Blocks / Intent_Block_Map).

Читает ТОЛЬКО read-only Prompts.csv (рабочая таблица бота не трогается) и пишет
nocodb/intents.csv, nocodb/prompt_blocks.csv, nocodb/intent_block_map.csv —
для загрузки через API (push_tables.py) или ручного импорта.

Version_Set новых наборов: v3.00 (не пересекается с base_v01.xx старого бота).
"""
import csv
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROMPTS_CSV = HERE.parent / "Prompts.csv"
VERSION = "v3.00"
SCENARIOS = ("credit", "tradein", "combo", "stock", "consult")

# ---------------------------------------------------------------------------
# 1. INTENTS — 32 интента из option/CONTEXT_RESOLVER_SPEC.md (v1.0)
# поля: id, group, desc, examples, scenarios, stages, priority
# ---------------------------------------------------------------------------
INTENTS = [
    # Группа A: СБОР ДАННЫХ
    ("collect_down_payment", "A", "Взнос/платёж не назван; клиент пишет что угодно, кроме нужных данных",
     "пока думаю;а какие условия;ну не знаю", "credit,combo", "data_collect", 5),
    ("collect_plate", "A", "Госномер не собран (для оценки трейд-ин); клиент уклоняется или пишет другое",
     "зачем номер;у меня киа рио;машина 2018 года", "tradein,combo", "data_collect", 5),
    ("collect_mileage", "A", "Госномер есть, пробег не собран",
     "номер А123АА77;а пробег зачем", "tradein,combo", "data_collect", 5),
    ("collect_plate_and_mileage", "A", "Ни госномера, ни пробега ещё нет",
     "хочу сдать свою машину;оцените авто", "tradein,combo", "data_collect", 4),
    ("data_received_complete", "A", "Клиент передал последний недостающий параметр — данные собраны полностью",
     "пробег 85 тысяч;взнос 300000;А123АА77 пробег 60к", "all", "data_collect", 20),
    # Группа B: ВОПРОСЫ И СПРАВОЧНИК
    ("answer_question_in_json", "B", "Вопрос по цене/характеристикам, ответ ЕСТЬ в данных каталога",
     "какой двигатель;сколько лошадей;какая коробка", "all", "all", 8),
    ("answer_question_no_json", "B", "Вопрос, ответа на который нет в данных каталога",
     "есть ли парковка у салона;когда привезут новую модель", "all", "all", 6),
    ("cross_scenario_question", "B", "Вопрос из чужого сценария (например про наличие/цвет в кредитной ветке)",
     "а в синем есть;есть в наличии;когда можно посмотреть", "all", "data_collect,closing", 7),
    ("price_hierarchy_question", "B", "Вопрос о цене: сколько стоит, какая скидка, почему такая цена",
     "сколько стоит;а без кредита какая цена;какая скидка", "all", "all", 9),
    ("competitor_comparison", "B", "Сравнение с конкурентами или другими площадками",
     "на авито дешевле;у другого дилера выгоднее", "all", "all", 10),
    # Группа C: ИНСТРУМЕНТЫ
    ("ready_for_tool", "C", "Все параметры собраны, инструмент ещё не вызывался",
     "да всё верно;посчитайте", "credit,tradein,combo", "data_collect,tool_needed", 18),
    ("tool_result_received", "C", "Инструмент вернул результат — нужно озвучить расчёт/оценку",
     "(системный переход после расчёта)", "credit,tradein,combo", "tool_needed", 15),
    ("tool_error_handling", "C", "Инструмент вернул ошибку или IN_PROGRESS",
     "(системный переход при ошибке инструмента)", "tradein,combo", "tool_needed", 15),
    ("tool_rerun_needed", "C", "Клиент прислал исправленные данные (новый госномер/пробег) после ошибки",
     "ой, номер неправильный, вот верный;А321ВС99", "tradein,combo,credit", "tool_needed,closing", 16),
    # Группа D: ЗАКРЫТИЕ
    ("propose_visit", "D", "Расчёт озвучен — пора предлагать визит в салон",
     "интересно;неплохо;а что дальше", "all", "closing", 8),
    ("propose_call", "D", "Клиент отказался от визита — предложить звонок",
     "приехать не смогу;далеко ехать", "all", "closing", 9),
    ("request_phone", "D", "Клиент согласился на визит или звонок — запросить телефон",
     "давайте;хорошо, приеду;согласен", "all", "closing", 12),
    ("finalize_agreed", "D", "Телефон получен или известный клиент подтвердил договорённость — финализировать сделку",
     "+79991234567;да, жду звонка", "all", "closing", 25),
    ("schedule_appointment", "D", "Клиент называет конкретную дату/время визита",
     "приеду в пятницу;завтра после обеда;в субботу в 12", "all", "closing", 14),
    # Группа E: ВОЗРАЖЕНИЯ
    ("objection_no_phone", "E", "Первый отказ давать телефон",
     "не хочу оставлять номер;зачем вам мой телефон", "all", "closing", 8),
    ("objection_no_phone_repeat", "E", "Повторное уклонение от телефона (phone_ask_count>=1)",
     "я же сказал, без номера;потом оставлю", "all", "closing", 9),
    ("objection_distance", "E", "Логистический барьер: далеко, нет времени ехать",
     "вы далеко;нет времени ехать;я из другого города", "all", "all", 8),
    ("objection_price", "E", "Финансовый барьер: дорого, взнос ниже расчётного",
     "дорого;нет таких денег;200к это много", "all", "all", 10),
    ("objection_trust", "E", "Сомнение в данных/оценке: откуда цифры",
     "откуда такая оценка;не верю этим цифрам", "all", "all", 8),
    ("objection_data_privacy", "E", "Барьер данных: зачем госномер, боюсь утечки",
     "зачем вам госномер;не дам данные;боюсь баз", "tradein,combo,credit", "data_collect", 9),
    ("objection_no_car", "E", "У клиента нет своей машины (в tradein/combo)",
     "у меня нет машины;первый автомобиль", "tradein,combo", "data_collect", 9),
    ("pause_requested", "E", "Клиент просит паузу: подумаю, напишу позже",
     "подумаю;напишу позже;мне надо посоветоваться", "all", "all", 7),
    ("final_refusal", "E", "Категорический отказ, стоп-слова — завершить диалог",
     "не пишите мне;отстаньте;удалите мой номер", "all", "all", 30),
    # Группа F: СИСТЕМНЫЕ
    ("greeting_first", "F", "Первое сообщение диалога (msg_count=0)",
     "(системный: старт диалога)", "all", "greeting", 30),
    ("reminder_response", "F", "Клиент ответил на напоминание после паузы",
     "да, актуально;ещё интересуюсь", "all", "all", 13),
    ("off_topic", "F", "Сообщение полностью вне темы покупки авто",
     "как дела;расскажи анекдот;что думаешь про погоду", "all", "all", 6),
    ("ban_trigger", "F", "Мат, агрессия, явный троллинг — завершить с баном",
     "(оскорбления и мат)", "all", "all", 30),
]

# v3.02 (план 2026-07-17-001): 4 интента из реальных кластеров логов — заведены
# скриптом add_intents_v2.py, активированы 2026-07-21 (схема целиком на
# [TEST]-контуре, прод не затронут); Stage_Context — справочное поле, не фильтр
INTENTS_V302 = [
    ("request_human_handoff", "F", "Клиент просит передать диалог живому менеджеру/сотруднику",
     "очень хорошо переключите на менеджера;переключите на менеджера", "all", "all", 12),
    ("payment_method_change", "B", "Клиент хочет другой способ оплаты: наличные, без кредита, рассрочка",
     "а можно за наличные купить?;за наличку;без кредита;оплата сразу;"
     "два миллиона деньгами остальное рассрочка", "all", "all", 8),
    ("data_already_provided", "E", "Клиент считает, что уже передал запрашиваемые данные ранее в диалоге",
     "почитайте диалог выше. Там все уже есть;все данные я вроде предоставил;"
     "зачем это делать второй раз?;расчет уже есть", "all", "data_collect", 9),
    ("polite_close_satisfied", "D", "Клиент вежливо завершает диалог: удовлетворён ответом, отказа нет",
     "спасибо, я удовлетворён вашим ответом;спасибо, пока это все, что мне нужно;"
     "я все уже поняла;да я вроде все уточнил", "all", "closing", 7),
]

# v3.03 (план 2026-07-21-001, need-check): интенты ответов на проверочный
# вопрос — заведены скриптом add_needcheck_v303.py черновиками; активация
# --activate-drafts вместе с блоками и map-строками, только [TEST]-контур
INTENTS_V303 = [
    ("need_check_ok", "D", "Утвердительный ответ на need-check вопрос бота "
     "(взнос/оценка/платёж/одобрение/тест-драйв)",
     "да, нормально;потяну;устраивает;да, одобрили;уже катался",
     "credit,tradein,combo,stock", "tool_needed,closing", 13),
    ("need_check_not_ok", "D", "Короткое «нет» на need-check вопрос бота, без развёрнутого возражения",
     "нет;не подходит;не потяну;ожидал больше;ещё не тестировал",
     "credit,tradein,combo,stock", "tool_needed,closing", 13),
]

# ---------------------------------------------------------------------------
# 2. PROMPT_BLOCKS — перенос 1:1 из Prompts.csv + каркасы для нарезки WORKFLOW
# ---------------------------------------------------------------------------
GENERAL_MAP = {  # Block из Prompts.csv -> (Block_ID, Level, Intent_Triggers)
    "ROLE": ("G-01_ROLE", "general", ""),
    "VOICE_AND_TONE": ("G-02_VOICE", "voice", ""),
    "GOAL": ("G-03_GOAL", "general", ""),
    "SPLIT_FORMATTING": ("G-04_FORMAT", "general", ""),
    "DEFENSE_MODE": ("G-05_DEFENSE", "general", "off_topic"),
    "CRITICAL_CHECKLIST": ("G-06_CHECKLIST", "general", ""),
    "TOOLS": ("T-01_TOOLS", "tools", ""),
}
SCEN_TITLE = {"credit": "Credit", "tradein": "TradeIn", "combo": "Combo",
              "stock": "Stock", "consult": "Consult"}
WORKFLOW_STAGES = ("DATA_COLLECT", "TOOL_NEEDED", "CLOSING")


def load_prompts():
    return list(csv.DictReader(open(PROMPTS_CSV, encoding="utf-8")))


def build_blocks(rows):
    blocks = []

    def add(block_id, level, scenario, stage, content, active=True, triggers="", notes=""):
        blocks.append({
            "Block_ID": block_id, "Level": level, "Scenario": scenario, "Stage": stage,
            "Version_Set": VERSION, "Content": content, "Tokens_est": len(content) // 4,
            "Active": "true" if active else "false", "Intent_Triggers": triggers, "Notes": notes,
        })

    by_key = {(r["Type"], r["Block"]): r for r in rows}

    for src_name, (bid, level, triggers) in GENERAL_MAP.items():
        r = by_key[("General", src_name)]
        add(bid, level, "all", "all", r["Content"], triggers=triggers,
            notes=f"Перенос 1:1 из Prompts.csv id_block={r['id_block']} ({src_name})")

    for scen in SCENARIOS:
        t = SCEN_TITLE[scen]
        up = scen.upper()
        add(f"D_{up}", "data_source", scen, "all", by_key[(t, "DATA_SOURCE")]["Content"],
            notes="Перенос 1:1 (DATA_SOURCE)")
        add(f"ST_{up}_GREETING", "stage_task", scen, "greeting",
            by_key[(t, "FIRST_MESSAGE_LOGIC")]["Content"], notes="Перенос 1:1 (FIRST_MESSAGE_LOGIC)")
        add(f"ST_{up}_REQUIRED_DATA", "stage_task", scen, "data_collect",
            by_key[(t, "REQUIRED_DATA")]["Content"], notes="Перенос 1:1 (REQUIRED_DATA); additional к data_collect")
        add(f"CS_{up}", "general", scen, "all", by_key[(t, "CORE_STRATEGY")]["Content"],
            notes="Перенос 1:1 (CORE_STRATEGY); всегда в промпте сценария")
        add(f"OBJ_{up}", "objections", scen, "all", by_key[(t, "GLOBAL_OBJECTIONS")]["Content"],
            triggers=("objection_no_phone,objection_no_phone_repeat,objection_distance,"
                      "objection_price,objection_trust,objection_data_privacy,"
                      "objection_no_car,pause_requested"),
            notes="Перенос 1:1 (GLOBAL_OBJECTIONS); подключается только на возражениях — главный источник экономии")
        # WORKFLOW: каркасы для ручной нарезки владельцем + legacy-референс
        wf = by_key[(t, "WORKFLOW")]["Content"]
        for stage in WORKFLOW_STAGES:
            add(f"ST_{up}_{stage}", "stage_task", scen, stage.lower(),
                f"[TODO владелец контента: вырезать из WF_{up}_LEGACY часть про этап "
                f"{stage.lower()} — см. docs/prompt-blocks-mapping.md]",
                active=False, notes="Каркас: наполняется нарезкой WORKFLOW")
        add(f"WF_{up}_LEGACY", "stage_task", scen, "all", wf, active=False,
            notes="Полный старый WORKFLOW — референс для нарезки, в промпт не входит")

    return blocks


def build_map():
    """Intent_Block_Map: только НЕ-дефолтные привязки.

    Дефолт компилятора: primary = ST_{SCEN}_{stage} по текущей стадии.
    Здесь: переопределения primary по интенту + additional-блоки.
    """
    rows = []

    def add(intent, block, role, scen_filter="all", stage_filter="all",
            active=True, version=VERSION):
        rows.append({"Intent_ID": intent, "Block_ID": block, "Role": role,
                     "Scenario_Filter": scen_filter, "Stage_Filter": stage_filter,
                     "Version_Set": version, "Active": "true" if active else "false"})

    tool_scens = ("credit", "tradein", "combo")
    for scen in SCENARIOS:
        up = scen.upper()
        # возражения: OBJ-блок сценария добавляется поверх основного
        for obj_intent in ("objection_no_phone", "objection_no_phone_repeat", "objection_distance",
                           "objection_price", "objection_trust", "objection_data_privacy",
                           "objection_no_car", "pause_requested"):
            add(obj_intent, f"OBJ_{up}", "additional", scen)
        # REQUIRED_DATA добавляется на сборе данных
        for col_intent in ("collect_down_payment", "collect_plate", "collect_mileage",
                           "collect_plate_and_mileage"):
            add(col_intent, f"ST_{up}_REQUIRED_DATA", "additional", scen, "data_collect")
        # закрытие: primary CLOSING независимо от стадии Python
        for cl_intent in ("tool_result_received", "propose_visit", "propose_call", "request_phone",
                          "finalize_agreed", "schedule_appointment", "competitor_comparison"):
            add(cl_intent, f"ST_{up}_CLOSING", "primary", scen)
    for scen in tool_scens:
        up = scen.upper()
        for tool_intent in ("data_received_complete", "ready_for_tool", "tool_rerun_needed"):
            add(tool_intent, f"ST_{up}_TOOL_NEEDED", "primary", scen)
        add("tool_error_handling", f"ST_{up}_TOOL_NEEDED", "primary", scen)
    # off_topic: защитный режим поверх основного
    add("off_topic", "G-05_DEFENSE", "additional")
    # v3.02 (план 2026-07-17-001): привязки новых интентов — зеркало живых строк
    # add_intents_v2.py --apply-map, активированы 2026-07-21 ([TEST]-контур);
    # BLK_*_REFERENCE существуют только в живой таблице (созданы владельцем)
    for scen in SCENARIOS:
        up = scen.upper()
        add("request_human_handoff", f"ST_{up}_CLOSING", "primary", scen,
            version="v3.02")
        add("polite_close_satisfied", f"ST_{up}_CLOSING", "primary", scen,
            version="v3.02")
        add("data_already_provided", f"ST_{up}_DATA_COLLECT", "primary", scen,
            "data_collect", version="v3.02")
    for scen in tool_scens:
        add("payment_method_change", f"BLK_{scen.upper()}_REFERENCE", "additional", scen,
            version="v3.02")
    return rows


def main():
    rows = load_prompts()
    assert len(rows) == 37, f"ожидалось 37 блоков, найдено {len(rows)}"

    group_names = {"A": "Сбор данных", "B": "Вопросы и справочник", "C": "Инструменты",
                   "D": "Закрытие", "E": "Возражения", "F": "Системные"}

    def intent_row(i, active, version, source, notes):
        return {"Intent_ID": i[0], "Group": i[1], "Group_Name": group_names[i[1]],
                "Description": i[2], "Example_Phrases": i[3], "Scenarios": i[4],
                "Stage_Context": i[5], "Priority": i[6],
                "Active": "true" if active else "false",
                "Version": version, "Source": source, "Notes": notes}

    intents = [intent_row(i, True, VERSION, "origin",
                          "Стартовый набор из CONTEXT_RESOLVER_SPEC v1.0") for i in INTENTS] \
        + [intent_row(i, True, "v3.02", "logs",
                      "v3.02 (план 2026-07-17-001); активирован 2026-07-21 для "
                      "[TEST]-контура, прод не затронут") for i in INTENTS_V302] \
        + [intent_row(i, False, "v3.03", "origin",
                      "v3.03 (план 2026-07-21-001, need-check); черновик — активация "
                      "add_needcheck_v303.py --activate-drafts, только [TEST]-контур")
           for i in INTENTS_V303]

    blocks = build_blocks(rows)
    mapping = build_map()

    for name, data in (("intents.csv", intents), ("prompt_blocks.csv", blocks),
                       ("intent_block_map.csv", mapping)):
        path = HERE / name
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(data[0].keys()))
            w.writeheader()
            w.writerows(data)
        print(f"{name}: {len(data)} строк")

    active_blocks = [b for b in blocks if b["Active"] == "true"]
    todo_blocks = [b for b in blocks if b["Active"] == "false" and not b["Block_ID"].startswith("WF_")]
    print(f"\nблоков активных (перенос 1:1): {len(active_blocks)}; каркасов под нарезку: {len(todo_blocks)}; legacy-референсов: 5")


if __name__ == "__main__":
    main()
