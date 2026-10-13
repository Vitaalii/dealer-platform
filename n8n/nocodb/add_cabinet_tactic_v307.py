#!/usr/bin/env python3
"""Черновики v3.07: диалоговая тактика под личный кабинет.

План: docs/plans/2026-08-14-001-feat-cabinet-dialog-tactic-plan.md (U2, U3, U4)
плюс расчистка из U5.

Вопрос о комфортности условий («устраивает ли взнос», «соответствует ли оценка
ожиданиям») выводится из оборота. Вместо него — развилка «приехать в салон или
оформить в личном кабинете» и три хода её отработки.

Всё, что добавляется, добавляется с Active=false: до активации владельцем
промпт не меняется ни на символ. Активация поэтапная, по одному сценарию:
трейд-ин -> кредит -> комбо (флаг --activate-scenario).

Запуск:
  python3 add_cabinet_tactic_v307.py                      # diff, ничего не пишет
  python3 add_cabinet_tactic_v307.py --apply              # POST черновиков + PATCH описаний
  python3 add_cabinet_tactic_v307.py --cleanup            # расчистка U5 (Stage_Filter, архив)
  python3 add_cabinet_tactic_v307.py --activate-scenario tradein
  python3 add_cabinet_tactic_v307.py --rollback-scenario tradein
  python3 add_cabinet_tactic_v307.py --snapshot-only [--snapshot PATH]

Тексты блоков стадий (ST_CREDIT_GREETING_V2, ST_TRADEIN_TOOL_NEEDED_V2,
ST_COMBO_TOOL_NEEDED_V2) переписаны пакетами правок 2026-08-18: вопрос о
комфортности снят, развилка перенесена внутрь них. Прежнее предупреждение
«активация требует переписанных текстов» убрано 2026-08-19 как выполненное.
"""
import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE.parent / ".env"
IDS = json.loads((HERE / "table_ids.json").read_text(encoding="utf-8"))
SNAPSHOT_DEFAULT = HERE.parent / "flow" / "lib" / "snapshots" / "v3_tables.json"
VERSION = "v3.07"
ARCHIVE_TAG = f"[АРХИВ {date(2026, 8, 14).isoformat()}]"
# Порядок выката (ТЗ 2026-08-19, подтверждён владельцем): КРЕДИТ ПЕРВЫМ.
# Кортеж задаёт только очерёдность и допустимые значения флага. На то, когда
# снимается общий слой, он НЕ влияет — см. common_layer_applied().
SCENARIOS_WITH_CABINET = ("credit", "tradein", "combo")

# Канон обновлён 2026-08-19: «Драфт — текст дорабатывает владелец» убрано,
# тексты написаны пакетами правок 2026-08-18. Тот же принцип, что у
# CABINET_NOTES: канон в коде совпадает с тем, что лежит в живом поле, поэтому
# повторные запуски не возвращают устаревшую формулировку. Здесь этого хватает
# и без notes_with_canon: _NOTE подставляется только в POST новых записей,
# существующие строки скрипт не патчит вовсе.
_NOTE = ("Черновик v3.07 (план 2026-08-14-001, тактика кабинета). Черновик "
         "доработан, текст утверждён (пакет правок 2026-08-18); "
         "активация --activate-scenario <сценарий>.")

# --------------------------------------------------------------------------
# 1. Новые интенты (U2)
# --------------------------------------------------------------------------
# Четвёртый интент «клиент выбрал визит» сознательно НЕ заводится: эту роль
# уже играет request_phone («Клиент согласился на визит или звонок»). Второй
# интент под одно состояние — ровно та ошибка, из-за которой need_check_not_ok
# конкурировал с objection_price.
NEW_INTENTS = [
    {"Intent_ID": "choose_cabinet", "Group": "D", "Group_Name": "Закрытие",
     "Description": "Клиент выбирает личный кабинет вместо визита",
     "Example_Phrases": "в кабинете;сам посмотрю;онлайн удобнее;давайте ссылку;"
                        "второй вариант;на сайте",
     "Scenarios": "credit,tradein,combo", "Stage_Context": "closing",
     "Priority": 16, "Active": False, "Version": VERSION, "Source": "plan-2026-08-14-001",
     "Notes": _NOTE + " Ветвь развилки: выдать ссылку, телефон в чате НЕ просить."},
    {"Intent_ID": "prefer_chat_over_call", "Group": "E", "Group_Name": "Возражения",
     "Description": "Клиент отказывается от звонка и просит писать в чат",
     "Example_Phrases": "не звоните;пишите сюда;давайте в чате;только перепиской",
     "Scenarios": "all", "Stage_Context": "closing",
     "Priority": 11, "Active": False, "Version": VERSION, "Source": "plan-2026-08-14-001",
     "Notes": _NOTE + " Отработка: объяснить, что у специалистов нет доступа к чату, "
                      "плюс предложить кабинет."},
    {"Intent_ID": "cabinet_stuck", "Group": "F", "Group_Name": "Служебные",
     "Description": "Клиент не справился в кабинете или спрашивает про него",
     "Example_Phrases": "не получается в кабинете;код не приходит;не разобрался;"
                        "что там заполнять",
     "Scenarios": "credit,tradein,combo", "Stage_Context": "closing",
     "Priority": 13, "Active": False, "Version": VERSION, "Source": "plan-2026-08-14-001",
     "Notes": _NOTE + " Поддержка после выдачи ссылки."},
]

# Интенты, выводимые из оборота вместе с вопросом о комфортности (U2).
# СПИСОК ПУСТ С 2026-08-19. Гасить интенты при активации больше не нужно ни один.
#
# need_check_not_ok убран, потому что после переиспользования блоков (ТЗ
# 2026-08-18, U5) он — ЕДИНСТВЕННЫЙ вход в BLK_CREDIT_APPROVAL_BRIDGE и
# BLK_TRADEIN_TESTDRIVE_BRIDGE через Intent_Triggers, плюс держит primary-строку
# BLK_COMBO_NEEDCHECK_ROUTER. Он остаётся активным во всех сценариях.
#
# need_check_ok убран, потому что решён иначе: вместо гашения интент СУЖЕН по
# колонке Scenarios до одного стока (скрипт fix_need_check_ok_scope.py). Резолвер
# отбирает кандидатов ровно по этому полю — см. ноду Build Prompt в
# flow/Tool_ Resolver Core.json, — поэтому в кредите, трейд-ине и комбо интент
# просто перестаёт быть кандидатом, а сток продолжает работать как работал.
# Гашение задело бы и сток: там need_check_ok — живой вход в ST_STOCK_DATA_COLLECT.
RETIRE_INTENTS = {}

# PATCH описаний и примеров (U2).
# request_phone получает примеры: он играет вторую половину развилки, а в живой
# таблице у него ноль примеров против шести у choose_cabinet — половина развилки
# распознавалась бы заметно хуже другой.
# propose_visit примеров НЕ получает: это интент состояния, а не реплики клиента
# (как tool_result_received). Клиентские фразы там сделали бы хуже — резолвер
# начал бы возвращать его на реплики, принадлежащие другим интентам.
INTENT_PATCHES = {
    "request_phone": {
        "Description": "Клиент согласился на визит или звонок — запросить телефон. "
                       "НЕ путать с выбором кабинета (это choose_cabinet)",
        "Example_Phrases": "приеду;давайте в салон;лучше подъеду;готов приехать;первый вариант",
    },
    "propose_visit": {
        "Description": "Расчёт озвучен — пора предлагать выбор: визит или личный кабинет",
        "Example_Phrases": "(системный переход: расчёт озвучен)",
    },
}

# --------------------------------------------------------------------------
# 2. Блоки развилки (U3)
# --------------------------------------------------------------------------
# Подключение — только Intent_Triggers, чтобы владелец включал и выключал их без
# правки кода. Scenario у каждого блока свой: поэтапный выкат по сценариям
# требует, чтобы включение трейд-ина не задевало кредит.
#
# Тексты — заглушки с назначением. Пишет владелец продукта.
_STUB = "[ЧЕРНОВИК v3.07 — текст пишет владелец продукта] {}"

# триггер первого хода различается: в кредите развилка стоит в первом сообщении
# (model_id и complectation_id приходят на нулевом ходе), в трейд-ине и комбо —
# после оценки.
FORK_TRIGGERS = {
    "credit": "greeting_first,tool_result_received,data_received_complete",
    "tradein": "tool_result_received,data_received_complete",
    "combo": "tool_result_received,data_received_complete",
}

BLOCK_SPECS = [
    ("FORK", None, "tools",
     "Развилка: честно обозначить природу цифры и предложить выбор — "
     "приехать в салон или оформить самостоятельно в личном кабинете."),
    ("CABINET_CHOSEN", "choose_cabinet", "tools",
     "Клиент выбрал кабинет: выдать ссылку. Телефон в чате НЕ просить вовсе."),
    ("CABINET_FALLBACK", "objection_distance,propose_call", "tools",
     "Клиент отказался от визита: вторым ходом предложить кабинет — он снимает "
     "барьер расстояния."),
    ("CHAT_ONLY", "prefer_chat_over_call", "objections",
     "Объяснить, что у специалистов нет доступа к этому чату, и предложить кабинет."),
    ("CABINET_HELP", "cabinet_stuck", "tools",
     "Поддержка: клиент не справился в кабинете. Активируется по усмотрению "
     "владельца — возможно, достаточно строки внутри CABINET_CHOSEN."),
]


def _blk(block_id, level, scenario, content, triggers, note_extra=""):
    return {"Block_ID": block_id, "Level": level, "Scenario": scenario, "Stage": "all",
            "Version_Set": VERSION, "Content": content, "Tokens_est": len(content) // 4,
            "Active": False, "Intent_Triggers": triggers or "",
            "Notes": (_NOTE + " " + note_extra).strip()}


NEW_BLOCKS = []
for scen in SCENARIOS_WITH_CABINET:
    up = scen.upper()
    for suffix, triggers, level, purpose in BLOCK_SPECS:
        NEW_BLOCKS.append(_blk(
            f"BLK_{up}_{suffix}", level, scen, _STUB.format(purpose),
            FORK_TRIGGERS[scen] if suffix == "FORK" else triggers,
            "Подключается Intent_Triggers, строки карты не нужны."))

# Пробел, вскрытый в части A: в combo нет аналога BLK_TRADEIN_VALUE_DEFENSE —
# защиту оценки там нёс BLK_COMBO_NEEDCHECK_ROUTER, который выводится.
# Подключается картой, а не триггером: нужна роль primary (замещение основного
# блока стадии), а Intent_Triggers умеет только добавлять.
NEW_BLOCKS.append(_blk(
    "BLK_COMBO_VALUE_DEFENSE", "stage_task", "combo",
    _STUB.format("Защита оценки в комбо: цифра — средняя по прошлым сделкам с "
                 "похожими авто, а не оценка конкретной машины. Не пересчитывать."),
    "", "Подключается строкой карты objection_price (нужна роль primary)."))

# Шесть отменённых черновиков (ТЗ 2026-08-18): из активации вычеркнуты, Active
# остаётся false навсегда. Развилка перенесена ВНУТРЬ блоков, которые и так
# работают на нужном ходе (кредит — ST_CREDIT_GREETING_V2, трейд-ин —
# ST_TRADEIN_TOOL_NEEDED_V2, комбо — ST_COMBO_TOOL_NEEDED_V2): отдельный блок
# развилки дублировал бы её на том же ходе. *_CABINET_FALLBACK потеряли смысл
# вовсе — кабинет теперь предлагается сразу, а не вторым ходом.
CANCELLED_DRAFTS = {
    "BLK_CREDIT_FORK", "BLK_CREDIT_CABINET_FALLBACK",
    "BLK_TRADEIN_FORK", "BLK_TRADEIN_CABINET_FALLBACK",
    "BLK_COMBO_FORK", "BLK_COMBO_CABINET_FALLBACK",
}

# Блоки, выводимые вместе с вопросом о комфортности (U2/U4).
#
# СПИСОК ПУСТ С 2026-08-19. Все три блока, стоявшие здесь, отменены как
# «выводимые»: BLK_CREDIT_APPROVAL_BRIDGE, BLK_TRADEIN_TESTDRIVE_BRIDGE и
# BLK_COMBO_NEEDCHECK_ROUTER ПЕРЕИСПОЛЬЗОВАНЫ под обработку отказа клиента и от
# визита, и от кабинета (ТЗ 2026-08-18, U5) — их тексты переписаны, входы
# перевешены на need_check_not_ok. Замены, на которые они здесь ссылались
# (BLK_*_FORK), сами отменены и вычеркнуты из активации: см. CANCELLED_DRAFTS.
# Гасить их при активации значило бы убить три живых блока.
RETIRE_BLOCKS = {}

# Семь архивных блоков (U5): активны, но недостижимы — их вытесняет версия _V2.
# При переименовании или удалении _V2 старый текст молча вернулся бы в промпт.
ARCHIVE_BLOCKS = {
    "CS_CREDIT": "CS_CREDIT_V2",
    "CS_TRADEIN": "CS_TRADEIN_V2",
    "CS_COMBO": "CS_COMBO_V2",
    "ST_TRADEIN_TOOL_NEEDED": "ST_TRADEIN_TOOL_NEEDED_V2",
    "ST_COMBO_TOOL_NEEDED": "ST_COMBO_TOOL_NEEDED_V2",
    "ST_STOCK_GREETING": "ST_STOCK_GREETING_V2",
    "BLK_CREDIT_PAYMENT_ACCEPTED": "BLK_CREDIT_PAYMENT_ACCEPTED_V2",
}

# --------------------------------------------------------------------------
# 2b. Проводка общего блока кабинета (U1/U8)
# --------------------------------------------------------------------------
# G-07_CABINET убран из жёсткого списка компилятора юнитом U1 и с тех пор не
# подключается НИ ОДНИМ из четырёх способов: Intent_Triggers пуст, строк карты
# нет, имя не подходит под шаблон. Без этого патча включение любого этапа не
# положило бы блок в промпт вовсе.
#
# Состав триггеров — широкий, и вот почему.
# У Intent_Triggers нет подстановочного знака: компилятор сверяет интент со
# списком через includes(), поэтому «всегда» выражается только перечислением.
# Значит выбор не между механизмами, а между длиной списка.
# Блок несёт два вида правил (замер по тексту):
#   ~121 ток. — гарды: когда кабинет НЕ упоминать (пустой cabinet_url, телефон
#               уже получен) и как читать cabinet_status. Нужны везде, где
#               cabinet_url виден модели, а он виден в ai_clean_context на
#               КАЖДОМ ходе — проверено на живых исполнениях.
#   ~58 ток.  — инструкция предложения. Нужна на ходах развилки.
# Узкий список (8 интентов развилки) покрыл бы 25% ходов и оставил гарды
# отсутствующими там, где клиент возвращается к цифрам: в shadow-логе
# objection_price и objection_trust дают 9 ходов из 28, и оба вне узкого списка.
#
# Исключены три интента, которые до Prompt Compiler не доходят физически:
# reminder_response (Route уводит в Reminder Chain), final_refusal и ban_trigger
# (Stage Classifier ставит bypass) — и два выводимых need_check_*.
CABINET_TRIGGER_EXCLUDE = {
    "reminder_response", "final_refusal", "ban_trigger",  # не доходят до компилятора
    "need_check_ok", "need_check_not_ok",                 # выводятся этим же планом
}

CABINET_NOTES = (
    "v3.07: подключается ТОЛЬКО через Intent_Triggers — из жёсткого списка "
    "general-блоков Prompt Compiler убран (план 2026-08-14-001, U1). Рубильник "
    "поэтапного выката — колонка Scenario: пока в ней не перечислен сценарий, "
    "блок в его промпт не попадает. Переключать через "
    "add_cabinet_tactic_v307.py --activate-scenario, не руками: скрипт меняет "
    "Active и Scenario одним PATCH, ручное включение при Scenario=all открыло бы "
    "кабинет сразу в stock и consult."
)
# Строка «ПЕРЕД АКТИВАЦИЕЙ удалить правило про доведение подбора до выбора
# комплектации» убрана 2026-08-19: правило удалено пакетом правок 2026-08-18.
# Инструкция стала указанием сделать уже сделанное — рано или поздно её выполнили
# бы второй раз.


def notes_with_canon(current, canon):
    """Канонический текст ДОПИСЫВАЕТСЯ к заметкам, а не заменяет их.

    В Notes живут пометки о применённых правках ([ПРАВКА <дата>] ...), и замена
    поля каноническим текстом стирала бы их при каждом повторном --apply.
    Если канон уже присутствует — поле считается совпадающим и не трогается,
    поэтому шаг идемпотентен при любом числе запусков.
    """
    cur = (current or "").strip()
    if not cur:
        return canon
    if canon in cur:
        return cur
    return f"{cur}\n{canon}"

# --------------------------------------------------------------------------
# 3. Карта (U4)
# --------------------------------------------------------------------------
MAP_DRAFTS = [
    {"Intent_ID": "objection_price", "Block_ID": "BLK_TRADEIN_VALUE_DEFENSE",
     "Role": "primary", "Scenario_Filter": "tradein", "Stage_Filter": "all",
     "Version_Set": VERSION, "Active": False},
    {"Intent_ID": "objection_price", "Block_ID": "BLK_COMBO_VALUE_DEFENSE",
     "Role": "primary", "Scenario_Filter": "combo", "Stage_Filter": "all",
     "Version_Set": VERSION, "Active": False},
]

# Строки карты выводимых интентов.
#
# СПИСОК ПУСТ С 2026-08-19. Гашение было сплошным по Intent_ID, без учёта
# Scenario_Filter, и снимало три ЖИВЫЕ primary-строки чужих сценариев:
# need_check_not_ok -> BLK_COMBO_NEEDCHECK_ROUTER (combo, переиспользован),
# need_check_ok -> ST_STOCK_DATA_COLLECT и need_check_not_ok ->
# BLK_STOCK_NEED_CHECK (сток кабинетом не трогаем вовсе).
# Вся работа по карте уже сделана адресно (ТЗ 2026-08-18, U4): пять строк
# выключены с учётом Scenario_Filter, две включены. Скрипту в Intent_Block_Map
# делать больше нечего — сплошное гашение по интенту может только навредить.
RETIRE_MAP_INTENTS = ()


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


def common_layer_applied(by_intent):
    """Снят ли общий слой — по СОСТОЯНИЮ его собственных строк.

    Общий слой (после чистки 2026-08-19) состоит ровно из одного действия:
    включить три интента v3.07. Ничего не гасит — RETIRE_* пусты.

    Признак «уже снят» берётся из состояния этих трёх интентов, а НЕ из имени
    сценария и НЕ из колонки Scenario у G-07_CABINET. Прежняя проверка
    (`not cur`, где cur — сценарии в G-07_CABINET.Scenario) была косвенной и
    ломалась двумя способами: G-07_CABINET патчится РАНЬШЕ общего слоя, поэтому
    обрыв между двумя запросами навсегда пометил бы слой снятым; и значение
    "all" в той же колонке служит признаком «выключено», что двусмысленно.
    Состояние самих интентов таких дыр не имеет и делает шаг идемпотентным:
    повторный запуск видит их включёнными и не трогает ничего.
    """
    rows = [by_intent[i] for i in (r["Intent_ID"] for r in NEW_INTENTS) if i in by_intent]
    return bool(rows) and all(bool(r.get("Active")) for r in rows)


def budget_check(intents, label):
    """Вход резолвера (R8): фикс-часть + строки `Intent_ID | Description | Примеры`.

    Константы зеркалят nocodb/lint_check.py. Фикс-часть уменьшена на 435 символов
    после удаления client_mode/client_signal из шаблона (U5).
    """
    budget, fixed = 8560, 1993
    scen_list = ("credit", "tradein", "combo", "stock", "consult")
    print(f"\nбюджет входа резолвера ({label}):")
    worst = 0
    for scen in scen_list:
        rows = [i for i in intents if i.get("Active")
                and (str(i.get("Scenarios")) == "all"
                     or scen in [s.strip() for s in str(i.get("Scenarios")).split(",")])]
        chars = sum(len(f"{i['Intent_ID']} | {i.get('Description') or ''} | "
                        f"Примеры: {i.get('Example_Phrases') or ''}") + 1 for i in rows)
        total = fixed + chars
        worst = max(worst, total)
        flag = "ок" if total <= budget else f"ПРЕВЫШЕНИЕ на {total - budget}"
        print(f"  {scen:<9} {len(rows):>2} интентов, {total:>5} симв. из {budget} [{flag}]")
    if worst > budget:
        print("  ⚠️ порог не поднимать — ужимать Example_Phrases новых интентов")
    return worst <= budget


def main():
    env = load_env()
    args = sys.argv[1:]
    snap_path = Path(args[args.index("--snapshot") + 1]) if "--snapshot" in args else SNAPSHOT_DEFAULT

    intents = fetch_all(env, IDS["Intents"])
    blocks = fetch_all(env, IDS["Prompt_Blocks"])
    mapping = fetch_all(env, IDS["Intent_Block_Map"])
    print(f"живые таблицы: интентов {len(intents)}, блоков {len(blocks)}, map-строк {len(mapping)}")

    by_intent = {i["Intent_ID"]: i for i in intents}
    by_block = {b["Block_ID"]: b for b in blocks}
    have_map = {(m["Intent_ID"], m["Block_ID"], m.get("Scenario_Filter")) for m in mapping}

    # Intent_Triggers работают только по живым интентам: несуществующий триггер
    # молча никогда не сработает, поэтому проверяем громко.
    known = set(by_intent) | {i["Intent_ID"] for i in NEW_INTENTS}
    for b in NEW_BLOCKS:
        missing = [t for t in str(b["Intent_Triggers"]).split(",") if t and t not in known]
        if missing:
            print(f"  ⚠️ {b['Block_ID']}: триггеров нет в таблице интентов: {', '.join(missing)}")

    intents_to_post = [r for r in NEW_INTENTS if r["Intent_ID"] not in by_intent]
    blocks_to_post = [b for b in NEW_BLOCKS if b["Block_ID"] not in by_block]
    map_to_post = [r for r in MAP_DRAFTS
                   if (r["Intent_ID"], r["Block_ID"], r["Scenario_Filter"]) not in have_map]
    patches = [(by_intent[k]["Id"], k, v) for k, v in INTENT_PATCHES.items()
               if k in by_intent and any(str(by_intent[k].get(f) or "") != v[f] for f in v)]

    # Проводка G-07_CABINET: триггеры заполняются, пока блок неактивен, поэтому
    # окна «триггеры есть, Scenario ещё all, блок уже включён» не возникает —
    # Active и Scenario меняет только --activate-scenario, одним PATCH.
    # скобки обязательны: в Python `-` связывает крепче `|`, и без них
    # исключения применились бы только к NEW_INTENTS, а не ко всему множеству
    cabinet_triggers = ",".join(sorted(
        ({i["Intent_ID"] for i in intents} | {r["Intent_ID"] for r in NEW_INTENTS})
        - CABINET_TRIGGER_EXCLUDE))
    cab_row = by_block.get("G-07_CABINET")
    cab_patch = None
    if cab_row:
        cab_notes = notes_with_canon(cab_row.get("Notes"), CABINET_NOTES)
        if (str(cab_row.get("Intent_Triggers") or "") != cabinet_triggers
                or str(cab_row.get("Notes") or "") != cab_notes):
            cab_patch = {"Id": cab_row["Id"], "Intent_Triggers": cabinet_triggers,
                         "Notes": cab_notes}

    wrote = False
    if "--snapshot-only" not in args:
        print(f"\n— новые интенты (POST, Active=false): {len(intents_to_post)}")
        for r in intents_to_post:
            print(f"    {r['Intent_ID']} (prio {r['Priority']}, {r['Scenarios']})")
        print(f"— PATCH описаний/примеров: {len(patches)}")
        for _, k, _ in patches:
            print(f"    {k}")
        print(f"— блоки-черновики (POST, Active=false): {len(blocks_to_post)}")
        for b in blocks_to_post:
            print(f"    {b['Block_ID']:<28} scen={b['Scenario']:<8} trig={b['Intent_Triggers'] or '— (карта)'}")
        print(f"— строки карты (POST, Active=false): {len(map_to_post)}")
        for r in map_to_post:
            print(f"    {r['Intent_ID']} -> {r['Block_ID']} ({r['Scenario_Filter']})")
        print(f"— проводка G-07_CABINET: {'ТРЕБУЕТСЯ' if cab_patch else 'уже проведён'}")
        if cab_patch:
            print(f"    Intent_Triggers: {len(cabinet_triggers.split(','))} интентов, "
                  f"{len(cabinet_triggers)} симв.")
            print(f"    Scenario остаётся {str(cab_row.get('Scenario'))!r} — сузит "
                  f"--activate-scenario")

        # Моделируем ЦЕЛЕВОЕ состояние, а не переходное: новые интенты активны,
        # need_check_* выведены, описания и примеры пропатчены. Иначе проверка
        # считает состояние, которого не будет ни одной минуты.
        target = []
        for i in intents:
            if i["Intent_ID"] in RETIRE_INTENTS:
                continue
            target.append(dict(i, **INTENT_PATCHES.get(i["Intent_ID"], {})))
        target += [dict(r, Active=True) for r in NEW_INTENTS]
        budget_check(target, "целевое состояние: черновики активны, need_check выведены")

        if "--apply" in args:
            if intents_to_post:
                api(env, "POST", f"/api/v2/tables/{IDS['Intents']}/records", intents_to_post)
                print(f"\nдобавлено интентов: {len(intents_to_post)}")
            if patches:
                api(env, "PATCH", f"/api/v2/tables/{IDS['Intents']}/records",
                    [dict(v, Id=rid) for rid, _, v in patches])
                print(f"обновлено описаний/примеров: {len(patches)}")
            if blocks_to_post:
                api(env, "POST", f"/api/v2/tables/{IDS['Prompt_Blocks']}/records", blocks_to_post)
                print(f"добавлено блоков: {len(blocks_to_post)}")
            if map_to_post:
                api(env, "POST", f"/api/v2/tables/{IDS['Intent_Block_Map']}/records", map_to_post)
                print(f"добавлено map-строк: {len(map_to_post)}")
            if cab_patch:
                api(env, "PATCH", f"/api/v2/tables/{IDS['Prompt_Blocks']}/records", [cab_patch])
                print("проведён G-07_CABINET: Intent_Triggers + Notes "
                      "(Active и Scenario не тронуты)")
            wrote = True

        if "--cleanup" in args:
            # 1. Stage_Filter снимается только с additional-строк: primary-строка
            #    замещает основной блок стадии и меняла бы поведение во всех пяти
            #    сценариях, а не только там, где её задумывали.
            sf = [m for m in mapping if str(m.get("Stage_Filter")) == "data_collect"
                  and m.get("Role") == "additional"]
            keep = [m for m in mapping if str(m.get("Stage_Filter")) == "data_collect"
                    and m.get("Role") != "additional"]
            print(f"\nрасчистка: Stage_Filter снимается с {len(sf)} строк (additional), "
                  f"остаётся на {len(keep)} (primary)")
            if sf:
                api(env, "PATCH", f"/api/v2/tables/{IDS['Intent_Block_Map']}/records",
                    [{"Id": m["Id"], "Stage_Filter": "all"} for m in sf])
            # 2. Семь архивных блоков
            arch = [(by_block[b], repl) for b, repl in ARCHIVE_BLOCKS.items() if b in by_block]
            print(f"архивируется блоков: {len(arch)}")
            for b, repl in arch:
                print(f"    {b['Block_ID']:<32} -> {repl}")
            if arch:
                api(env, "PATCH", f"/api/v2/tables/{IDS['Prompt_Blocks']}/records",
                    [{"Id": b["Id"], "Active": False,
                      "Notes": f"{ARCHIVE_TAG} заменён на {repl}"} for b, repl in arch])
            wrote = True

        for flag, activate in (("--activate-scenario", True), ("--rollback-scenario", False)):
            if flag not in args:
                continue
            scen = args[args.index(flag) + 1]
            if scen not in SCENARIOS_WITH_CABINET:
                raise SystemExit(f"сценарий {scen!r} не из {SCENARIOS_WITH_CABINET}")
            # Предупреждение «требуются переписанные тексты ST_*» убрано
            # 2026-08-19: тексты переписаны пакетами правок 2026-08-18.
            print(f"\n{'активация' if activate else 'откат'} этапа: {scen}")

            ids = {b["Block_ID"] for b in NEW_BLOCKS
                   if b["Scenario"] == scen and b["Block_ID"] not in CANCELLED_DRAFTS}
            skipped = sorted({b["Block_ID"] for b in NEW_BLOCKS if b["Scenario"] == scen}
                             & CANCELLED_DRAFTS)
            if skipped:
                print(f"  вычеркнуты из активации (черновики отменены): {', '.join(skipped)}")
            bpatch = [{"Id": by_block[i]["Id"], "Active": activate}
                      for i in ids if i in by_block
                      and bool(by_block[i].get("Active")) != activate]
            # рубильник: колонка Scenario общего блока кабинета
            cab = by_block.get("G-07_CABINET")
            cur = [s.strip() for s in str((cab or {}).get("Scenario") or "").split(",")
                   if s.strip() and s.strip() != "all"]
            new = sorted(set(cur) | {scen}) if activate else sorted(set(cur) - {scen})
            mpatch = [{"Id": m["Id"], "Active": activate} for m in mapping
                      if m.get("Version_Set") == VERSION
                      and m.get("Scenario_Filter") == scen
                      and bool(m.get("Active")) != activate]
            # Общий слой развязан с конкретным сценарием: он отрабатывает при
            # ПЕРВОЙ активации любого из трёх, а признак берётся из состояния
            # его собственных интентов (см. common_layer_applied).
            have_common = common_layer_applied(by_intent)
            need_common = activate and not have_common
            drop_common = (not activate) and not new and have_common
            # Патч — всегда диффом, поэтому повторный запуск ничего не перезаписывает.
            ip = [{"Id": by_intent[i]["Id"], "Active": activate}
                  for i in (r["Intent_ID"] for r in NEW_INTENTS)
                  if i in by_intent and bool(by_intent[i].get("Active")) != activate]
            ip += [{"Id": by_intent[i]["Id"], "Active": not activate,
                    "Notes": f"{ARCHIVE_TAG} заменён на {repl}" if activate
                             else (by_intent[i].get("Notes") or "")}
                   for i, repl in RETIRE_INTENTS.items() if i in by_intent]

            if need_common or drop_common:
                print(f"  {'+' if need_common else '-'} общий слой: интенты v3.07 "
                      f"({len(ip)} к правке). Гашений нет: RETIRE_* пусты")
            else:
                print(f"  общий слой уже {'снят' if have_common else 'не нужен'} — "
                      f"пропускается")

            print(f"  блоков: {len(bpatch)}, строк карты: {len(mpatch)}, "
                  f"G-07_CABINET.Scenario: {','.join(new) or '—'}")

            # Порядок записи: сперва общий слой, затем блоки и карта, и только
            # ПОСЛЕДНИМ — рубильник G-07_CABINET. Он же служит видимой отметкой
            # «этап включён», поэтому ставится, когда всё остальное уже легло:
            # обрыв на любом шаге оставляет этап выключенным и повторный запуск
            # доделывает работу, а не считает её сделанной.
            if (need_common or drop_common) and ip:
                api(env, "PATCH", f"/api/v2/tables/{IDS['Intents']}/records", ip)
                rb = [{"Id": by_block[i]["Id"], "Active": not activate}
                      for i in RETIRE_BLOCKS if i in by_block]
                if rb:
                    api(env, "PATCH", f"/api/v2/tables/{IDS['Prompt_Blocks']}/records", rb)
                rm = [{"Id": m["Id"], "Active": not activate} for m in mapping
                      if m["Intent_ID"] in RETIRE_MAP_INTENTS]
                if rm:
                    api(env, "PATCH", f"/api/v2/tables/{IDS['Intent_Block_Map']}/records", rm)
            if bpatch:
                api(env, "PATCH", f"/api/v2/tables/{IDS['Prompt_Blocks']}/records", bpatch)
            if mpatch:
                api(env, "PATCH", f"/api/v2/tables/{IDS['Intent_Block_Map']}/records", mpatch)
            if cab and (bool(cab.get("Active")) != bool(new)
                        or str(cab.get("Scenario") or "") != (",".join(new) if new else "all")):
                api(env, "PATCH", f"/api/v2/tables/{IDS['Prompt_Blocks']}/records",
                    [{"Id": cab["Id"], "Active": bool(new),
                      "Scenario": ",".join(new) if new else "all"}])
            wrote = True

        if not wrote:
            print("\n(diff-режим: ничего не записано; --apply / --cleanup / "
                  "--activate-scenario <s> / --rollback-scenario <s>)")

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
