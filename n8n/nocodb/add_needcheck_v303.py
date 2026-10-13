#!/usr/bin/env python3
"""Need-check перед закрытием хода (план 2026-07-21-001): черновики v3.03.

Операции над ЖИВЫМИ таблицами (канонический источник — NocoDB):
  1. POST 2 новых интентов черновиками (Active=false, Version v3.03):
     need_check_ok, need_check_not_ok. Тексты ужаты под бюджет R8 плана
     2026-07-17-001 (суммарная добавка к списку интентов ≤ 430 симв.).
  2. PATCH Description у request_phone и propose_visit — ДОПИСЫВАНИЕ разводки
     «не путать с ответом на проверочный вопрос» (идемпотентно, по подстроке;
     правки владельца не затираются).
  3. POST 12 блоков-черновиков (Active=false, Version_Set v3.03) с драфт-
     контентом из origin-документа — финальные тексты дорабатывает владелец
     в UI до активации:
       новые:  BLK_CREDIT_APPROVAL_BRIDGE, BLK_TRADEIN_TESTDRIVE_BRIDGE,
               BLK_TRADEIN_VALUE_DEFENSE, BLK_COMBO_NEEDCHECK_ROUTER,
               BLK_STOCK_NEED_CHECK
       V2:     BLK_CREDIT_PAYMENT_ACCEPTED_V2, ST_TRADEIN_TOOL_NEEDED_V2,
               ST_COMBO_TOOL_NEEDED_V2, ST_STOCK_GREETING_V2,
               CS_CREDIT_V2, CS_TRADEIN_V2, CS_COMBO_V2
  4. POST 10 черновых map-строк (Active=false): 8 need_check_* + greeting_first
     → BLK_STOCK_NEED_CHECK (additional, stock) + data_received_complete →
     BLK_CREDIT_PAYMENT_ACCEPTED_V2 (primary, credit). Для ST_*_TOOL_NEEDED_V2
     map-строки не нужны: после деактивации старых primary-строк их подключает
     дефолт стадии через V2-предпочтение компилятора (U2).
  5. --activate-drafts: ОДНИМ запуском включить интенты+блоки+map-строки v3.03
     И выключить замещаемые старые primary-строки (список печатается) — контур
     не остаётся в частично применённом состоянии.
  6. --rollback: обратная операция — выключить всё v3.03, включить обратно
     замещённые старые строки.

Все правки читают только v3/[TEST]-воркфлоу; прод (AI Agent v2.0, таблица
Prompts) не затрагивается. Без флагов печатает diff и ничего не пишет.
Снапшот живых таблиц пишется в flow/lib/snapshots/v3_tables.json после
любой записи (или по --snapshot-only).

Запуск:
  python3 add_needcheck_v303.py                  # diff + проверка бюджета R8
  python3 add_needcheck_v303.py --snapshot-only  # только свежий снапшот
  python3 add_needcheck_v303.py --apply          # интенты + PATCH описаний
  python3 add_needcheck_v303.py --apply-blocks   # блоки-черновики
  python3 add_needcheck_v303.py --apply-map      # черновые map-строки
  python3 add_needcheck_v303.py --activate-drafts  # включить v3.03, выключить старые
  python3 add_needcheck_v303.py --rollback       # откат активации
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
VERSION = "v3.03"

# Бюджет входа резолвера (R8 плана 2026-07-17-001, замер 2026-07-17)
RESOLVER_PROMPT_CHAR_BUDGET = 8560
RESOLVER_FIXED_CHARS = 2433
BUDGET_HEADROOM = 50  # план 2026-07-21-001, U3: worst-case ≤ 8510

# --- 1. Новые интенты (тексты ужаты под бюджет R8 — см. U3 плана) ---
NEW_INTENTS = [
    {"Intent_ID": "need_check_ok", "Group": "D", "Group_Name": "Закрытие",
     "Description": "Утвердительный ответ на need-check вопрос бота "
                    "(взнос/оценка/платёж/одобрение/тест-драйв)",
     "Example_Phrases": "да, нормально;потяну;устраивает;да, одобрили;уже катался",
     "Scenarios": "credit,tradein,combo,stock", "Stage_Context": "tool_needed,closing",
     "Priority": 13, "Active": False, "Version": VERSION, "Source": "origin",
     "Notes": "Черновик v3.03 (план 2026-07-21-001, need-check). Активация скриптом "
              "--activate-drafts вместе с блоками и map-строками; только [TEST]-контур."},
    {"Intent_ID": "need_check_not_ok", "Group": "D", "Group_Name": "Закрытие",
     "Description": "Короткое «нет» на need-check вопрос бота, без развёрнутого возражения",
     "Example_Phrases": "нет;не подходит;не потяну;ожидал больше;ещё не тестировал",
     "Scenarios": "credit,tradein,combo,stock", "Stage_Context": "tool_needed,closing",
     "Priority": 13, "Active": False, "Version": VERSION, "Source": "origin",
     "Notes": "Черновик v3.03 (план 2026-07-21-001, need-check). Развёрнутые возражения "
              "остаются в objection_price/objection_trust. Активация — --activate-drafts."},
]

# --- 2. PATCH Description: разводка с request_phone/propose_visit (кейс test3) ---
DESC_PATCHES = {
    "request_phone": ". НЕ путать с «да» на need-check вопрос (это need_check_ok)",
    "propose_visit": ". НЕ выбирать как ответ на need-check вопрос",
}
# идемпотентность PATCH Description: сама дописка уже присутствует в живом значении

# --- 3. Блоки-черновики (драфт-контент из origin; финальный текст — владелец) ---


def _blk(block_id, level, scenario, stage, content, notes):
    return {"Block_ID": block_id, "Level": level, "Scenario": scenario, "Stage": stage,
            "Version_Set": VERSION, "Content": content, "Tokens_est": len(content) // 4,
            "Active": False, "Intent_Triggers": "", "Notes": notes}


_ACT_NOTE = ("Черновик v3.03 (план 2026-07-21-001, need-check). Драфт — доработать в UI; "
             "активация скриптом add_needcheck_v303.py --activate-drafts.")

NEW_BLOCKS = [
    _blk("BLK_CREDIT_APPROVAL_BRIDGE", "stage_task", "credit", "all",
         "- МОСТ «ОДОБРЕНИЕ» (взнос комфортен): клиент подтвердил, что сумма взноса ему подходит.\n"
         "Тактика: спроси, получал ли он уже одобрение по кредиту в каком-либо банке. "
         "НЕ проси номер телефона в этом же сообщении.\n"
         "- Если НЕ получал: предложи расчёт у специалиста на выбор. Пример: «Подскажите, вы уже "
         "получали одобрение по кредиту в каком-то банке? [SPLIT] Наши специалисты могут подобрать "
         "программу и сразу отправить заявки в банки — удобнее приехать в салон или чтобы "
         "специалист набрал вас?»\n"
         "- Если уже получал (одобрено в другом банке): без обещаний конкретной выгоды предложи "
         "сравнить условия. Пример: «Отлично, что одобрение уже есть! Наши специалисты часто "
         "подбирают условия интереснее — можно сравнить с вашим вариантом. [SPLIT] Удобнее "
         "подъехать в салон или созвониться со специалистом?»",
         _ACT_NOTE + " Primary @ need_check_ok (credit)."),
    _blk("BLK_TRADEIN_TESTDRIVE_BRIDGE", "stage_task", "tradein", "all",
         "- МОСТ «ТЕСТ-ДРАЙВ» (оценка устроила): клиент подтвердил, что оценка соответствует "
         "ожиданиям.\n"
         "Тактика: спроси, тестировал ли он уже новый автомобиль, который выбирает. "
         "НЕ проси номер телефона в этом же сообщении.\n"
         "- Не тестировал: пригласи совместить тест-драйв нового авто с точной оценкой его машины "
         "одним визитом — два дела за одну поездку. Пример: «Тогда предлагаю совместить: приедете "
         "на тест-драйв, а наш оценщик за это время осмотрит ваш автомобиль и назовёт точную цену. "
         "[SPLIT] В какой день вам было бы удобно?»\n"
         "- Уже тестировал: закрытие с выбором. Пример: «Тогда осталось зафиксировать условия — "
         "удобнее приехать в салон для оформления или чтобы специалист набрал вас?»",
         _ACT_NOTE + " Primary @ need_check_ok (tradein)."),
    _blk("BLK_TRADEIN_VALUE_DEFENSE", "stage_task", "tradein", "all",
         "- РЕЖИМ «ЗАЩИТА ОЦЕНКИ» (оценка ниже ожиданий): клиент разочарован суммой.\n"
         "Тактика: объясни природу цифры, не оправдывайся и не пересчитывай: цифра — средняя по "
         "прошлым сделкам с похожими авто, состояние тех машин неизвестно. Конкретно его "
         "автомобиль в хорошем состоянии может стоить существенно дороже — точную цену даст "
         "только осмотр.\n"
         "Пример: «Понимаю вас. Это средняя цена по прошлым сделкам, состояние тех машин могло "
         "быть любым. [SPLIT] Ваша машина в хорошем состоянии может стоить заметно дороже — "
         "точную цифру назовёт оценщик после осмотра. Приедете уточнить?»\n"
         "- Если последний вопрос бота был про тест-драйв и клиент ответил «ещё не тестировал» — "
         "это не барьер: предложи визит с двумя поводами (тест-драйв + точная оценка).",
         _ACT_NOTE + " Primary @ need_check_not_ok (tradein)."),
    _blk("BLK_COMBO_NEEDCHECK_ROUTER", "stage_task", "combo", "all",
         "- МАРШРУТИЗАТОР NEED-CHECK (combo): определи по истории диалога, какой проверочный "
         "вопрос был задан последним, и работай ТОЛЬКО с первым выявленным барьером — второй "
         "вопрос не задавай, пока барьер не снят. НЕ проси номер телефона в сообщении с "
         "проверочным вопросом.\n"
         "1) Последний вопрос был про ОЦЕНКУ авто:\n"
         "   - Не устроила: объясни природу цифры — средняя по прошлым сделкам, его авто в "
         "хорошем состоянии может стоить дороже, точную цену даст осмотр. При готовности "
         "клиента — визит на оценку.\n"
         "   - Устроила: задай второй проверочный вопрос: «А ежемесячный платёж из расчёта вам "
         "комфортен?»\n"
         "2) Последний вопрос был про ПЛАТЁЖ:\n"
         "   - Не комфортен: предложи субсидированные кредитные программы и больший срок "
         "(трейд-ин уже задействован как взнос). Пример: «У нас есть субсидированные программы, "
         "где платёж выходит ощутимо ниже. [SPLIT] Хотите, специалист подберёт варианты — "
         "удобнее приехать или созвониться?»\n"
         "   - Комфортен: закрытие с выбором: «Удобнее приехать в салон для оформления или "
         "чтобы специалист набрал вас?»",
         _ACT_NOTE + " Primary @ need_check_ok И need_check_not_ok (combo)."),
    _blk("BLK_STOCK_NEED_CHECK", "stage_task", "stock", "all",
         "- NEED-CHECK «НАЛИЧИЕ» (модели нет в наличии / в поставке): НЕ предлагай менеджера и "
         "НЕ проси номер телефона в этом же сообщении. Другие модели и комплектации по своей "
         "инициативе не предлагай.\n"
         "Тактика: сначала задай ОДИН вопрос о готовности ждать. Пример: «Автомобили этой модели "
         "сейчас в поставке, точные сроки уточнит менеджер. [SPLIT] Скажите, готовы ли вы "
         "подождать поставку?»\n"
         "- Готов ждать: предложи переключить диалог на менеджера для уточнения сроков; телефон "
         "проси только после согласия на менеджера (ШАГ 2 правил стадии).\n"
         "- Не готов / отказ: отреагируй эмпатично, не дави.",
         _ACT_NOTE + " Primary @ need_check_not_ok (stock); additional @ greeting_first (stock)."),
    _blk("BLK_CREDIT_PAYMENT_ACCEPTED_V2", "stage_task", "credit", "all",
         "- РЕЖИМ «СБОРЩИК» (Успех):\n"
         "Когда: Клиент идет по сценарию и называет сумму взноса, твоя задача по расчету "
         "окончена.\n"
         "Тактика: зафиксируй взнос и задай вопрос комфортности — НЕ предлагай визит и НЕ проси "
         "номер телефона в этом же сообщении. СТРОГО ЗАПРЕЩЕНО делать новый математический "
         "перерасчет платежа в чате.\n"
         "Пример: «Отлично, зафиксировал! [SPLIT] Скажите, такая сумма первоначального взноса "
         "для вас комфортна?»\n\n"
         "ПРОВЕРКА ДОСТАТОЧНОСТИ (по данным JSON): если названная сумма взноса ОЧЕВИДНО "
         "недостаточна относительно цены автомобиля (например, меньше минимального взноса по "
         "программе или в разы ниже типичного для этой цены) — НЕ фиксируй успех и НЕ задавай "
         "вопрос комфортности. Вместо этого переходи к тактике «АДВОКАТА»: предложи снизить "
         "взнос за счёт Трейд-ин (приоритет, если про авто клиента ещё не говорили) или "
         "субсидированную спецпрограмму через звонок специалиста. Пересчитывать платёж в чате "
         "по-прежнему ЗАПРЕЩЕНО.",
         _ACT_NOTE + " Заменяет BLK_CREDIT_PAYMENT_ACCEPTED (primary @ data_received_complete, "
                     "credit) — старая map-строка выключается при активации."),
    _blk("ST_TRADEIN_TOOL_NEEDED_V2", "stage_task", "tradein", "tool_needed",
         "Все параметры собраны (госномер + пробег) ИЛИ клиент прислал исправленный госномер "
         "после ошибки: немедленно и МОЛЧА вызови инструмент `evaluate_auto`. СТРОГО ЗАПРЕЩЕНО "
         "писать промежуточные фразы («Ожидайте», «Сейчас проведу расчет»).\n"
         "Получив результат, ты ОБЯЗАН написать клиенту текстовый ответ — молчать запрещено. "
         "Ориентируйся СТРОГО на поле status:\n"
         "- SUCCESS: «Посмотрел стоимость сделок по похожим авто. В среднем цена была "
         "[tradein_price_to] руб. [SPLIT] Скажите, такая оценка соответствует вашим ожиданиям?» "
         "ОТПРАВЬ И ЖДИ ОТВЕТА.\n"
         "- INVALID_DATA: скажи, что номер кажется неполным, и попроси корректный госномер. "
         "Получив новый номер — снова вызови инструмент.\n"
         "- IN_PROGRESS / NOT_FOUND / error: отключи правило «Зеркальность», извинись за "
         "техническую задержку баз своими словами и СРАЗУ предложи приехать в салон для живого "
         "осмотра. Закончи вопросом («В какой день вам было бы удобно заехать к нам?»). "
         "ОТПРАВЬ И ЖДИ ОТВЕТА.\n"
         "СТРОГО ЗАПРЕЩЕНО просить номер телефона на этом шаге.",
         _ACT_NOTE + " Заменяет ST_TRADEIN_TOOL_NEEDED через дефолт стадии + V2-предпочтение "
                     "компилятора (U2); старые primary-строки tool-интентов выключаются."),
    _blk("ST_COMBO_TOOL_NEEDED_V2", "stage_task", "combo", "tool_needed",
         "Все параметры собраны (госномер + пробег) ИЛИ клиент прислал исправленный госномер "
         "после ошибки: немедленно и МОЛЧА вызови инструмент `combo_calculator`. СТРОГО "
         "ЗАПРЕЩЕНО писать промежуточные фразы («Ожидайте», «Сейчас проведу расчет»).\n"
         "Получив результат, ты ОБЯЗАН написать клиенту текстовый ответ — молчать запрещено. "
         "Ориентируйся СТРОГО на поле status:\n"
         "- success: «Посмотрел стоимость сделок по похожим авто. В среднем цена была "
         "[down_payment] руб. Если взять эту сумму как первый взнос за новый [Модель], то с "
         "желаемым платежом [monthly_payment] руб. срок кредита выйдет где-то [term] месяцев! "
         "[SPLIT] Скажите, такая оценка вашего авто соответствует ожиданиям?» ОТПРАВЬ И ЖДИ "
         "ОТВЕТА.\n"
         "- impossible: «Посмотрел стоимость сделок по похожим авто. В среднем цена была "
         "[down_payment] руб. Если взять эту сумму как первый взнос за новый [Модель], то "
         "минимальный платеж на максимальный срок ([term] мес.) составит [monthly_payment] руб. "
         "Это минимальный платеж при таком взносе! [SPLIT] Скажите, такая оценка вашего авто "
         "соответствует ожиданиям?» ОТПРАВЬ И ЖДИ ОТВЕТА.\n"
         "- INVALID_DATA: скажи, что номер кажется неполным, и попроси корректный госномер. "
         "Получив новый номер — снова вызови инструмент.\n"
         "- IN_PROGRESS / NOT_FOUND / error: отключи правило «Зеркальность», извинись за "
         "техническую задержку баз своими словами и СРАЗУ предложи приехать в салон для живого "
         "осмотра. Закончи вопросом («В какой день вам было бы удобно заехать к нам?»). "
         "ОТПРАВЬ И ЖДИ ОТВЕТА.\n"
         "СТРОГО ЗАПРЕЩЕНО просить номер телефона на этом шаге.",
         _ACT_NOTE + " Заменяет ST_COMBO_TOOL_NEEDED через дефолт стадии + V2-предпочтение "
                     "компилятора (U2). Порядок need-check: сначала оценка (решение владельца), "
                     "затем платёж — второй вопрос задаёт BLK_COMBO_NEEDCHECK_ROUTER."),
    _blk("ST_STOCK_GREETING_V2", "stage_task", "stock", "greeting",
         "Твое первое сообщение СТРОГО строится по этому алгоритму. ЗАПРЕЩЕНО отклоняться от "
         "него.\n"
         "1. Поздоровайся с клиентом по имени. Его реальное имя передается тебе в блоке "
         "ВХОДЯЩИЕ ДАННЫЕ ИЗ БАЗЫ в поле `user_data.name`. Если там указаны Имя и Фамилия, "
         "СТРОГО ОБЯЗАТЕЛЬНО отбрось фамилию и напиши только Имя. СТРОГО ЗАПРЕЩЕНО писать "
         "шаблон \"[Имя клиента]\" — используй само имя. Представься ассистентом отдела продаж "
         "и сообщи, что получил запрос на подбор автомобиля [Модель].\n"
         "2. Проанализируй складские запасы и озвучь наличие СТРОГО по одному из сценариев. "
         "КРИТИЧНО: СТРОГО ЗАПРЕЩЕНО называть точное количество машин в штуках! Озвучивай "
         "только сам факт наличия и доступные цвета.\n"
         "   * СЦЕНАРИЙ А (Машины в нужной комплектации есть): Если `stock_count` выбранной "
         "комплектации больше 0, скажи, что автомобили в этой комплектации есть в наличии, и "
         "перечисли их цвета из `stock_colors`. В конце задай вопрос: \"Подскажите, какой из "
         "этих вариантов вам больше подходит?\".\n"
         "   * СЦЕНАРИЙ Б (Нужной комплектации нет, но есть другие): Если `stock_count` "
         "выбранной комплектации = 0, но `summary.total_stock` больше 0, честно скажи, что "
         "именно этой комплектации сейчас нет. Сразу же предложи другие доступные комплектации "
         "этой модели и назови их цвета. В конце задай вопрос: \"Подскажите, какой из этих "
         "вариантов вам больше подходит?\".\n"
         "   * СЦЕНАРИЙ В (Машин нет вообще): Если `summary.total_stock` = 0 или массив пуст, "
         "СТРОГО ЗАПРЕЩЕНО говорить, что машин нет в наличии. Скажи: \"Автомобили этой модели "
         "сейчас находятся в поставке, но я, к сожалению, не вижу в своей базе точных сроков "
         "прибытия.\" [SPLIT] Задай ОДИН вопрос о готовности ждать: \"Скажите, готовы ли вы "
         "подождать поставку? Точные сроки уточнит наш менеджер.\" СТРОГО ЗАПРЕЩЕНО в этом "
         "сообщении предлагать менеджера, просить номер телефона и предлагать другие модели по "
         "своей инициативе.\n"
         "3. Вставь тег [SPLIT] перед финальным вопросом (Сценарии А и Б). ОТПРАВЬ ЭТО "
         "СООБЩЕНИЕ И ЖДИ ОТВЕТА. СТРОГО ЗАПРЕЩЕНО просить номер телефона в первом сообщении "
         "при любом сценарии (А, Б и В)!",
         _ACT_NOTE + " Заменяет ST_STOCK_GREETING через дефолт стадии + V2-предпочтение (U2): "
                     "Сценарий В — need-check вместо «менеджер+телефон» одним сообщением."),
    _blk("CS_CREDIT_V2", "general", "credit", "all",
         "Ты действуешь как автономный эксперт: ведёшь диалог сам, без жёстких скриптов, по "
         "правилам:\n"
         "1. ДВИЖЕНИЕ К ЦЕЛИ: каждый ответ продвигает к сделке. Получив сумму взноса (или "
         "отработав возражение) — СНАЧАЛА убедись, что условия клиенту подходят (проверочный "
         "вопрос комфортности взноса), и только после его ответа предлагай следующий этап: "
         "визит в салон или звонок эксперта на выбор. СТРОГО ЗАПРЕЩЕНО просить телефон в том же "
         "сообщении: сначала «продай» встречу/звонок и дождись реакции.\n"
         "2. ТЕЛЕФОН — только после того, как клиент отреагировал на предложение визита или "
         "звонка.\n"
         "3. ЗАПРЕТ РАСЧЁТОВ: у тебя нет доступа к банковским скоринг-системам. СТРОГО "
         "ЗАПРЕЩЕНО выдумывать или пересчитывать платежи при смене взноса/срока — оперируй "
         "только цифрами из JSON; за точными цифрами всегда продавай визит или звонок.\n"
         "4. БЕЗ ГАЛЛЮЦИНАЦИЙ: не обещай 100% одобрение или конкретную ставку без оговорок; не "
         "предлагай несуществующих услуг — выезда кредитного специалиста на дом НЕТ, оформление "
         "ТОЛЬКО в дилерском центре.\n"
         "5. Не предлагай по своей инициативе расчёт других комплектаций или «узнать детали».",
         _ACT_NOTE + " Правка п.1: need-check перед предложением этапа (вместо «СРАЗУ»)."),
    _blk("CS_TRADEIN_V2", "general", "tradein", "all",
         "Ты действуешь автономно, без жёстких скриптов, по правилам:\n"
         "1. ДВИЖЕНИЕ К ЦЕЛИ: озвучив оценку (после ответа инструмента) — СНАЧАЛА спроси, "
         "соответствует ли она ожиданиям клиента (проверочный вопрос), и только после его "
         "ответа предлагай визит в салон или звонок эксперта на выбор. СТРОГО ЗАПРЕЩЕНО просить "
         "телефон в том же сообщении: сначала «продай» встречу/звонок и дождись реакции.\n"
         "2. ТЕЛЕФОН — только после того, как клиент отреагировал на предложение визита или "
         "звонка.\n"
         "3. ЗАПРЕТ САМООЦЕНКИ: у тебя нет глаз и диагностического оборудования. СТРОГО "
         "ЗАПРЕЩЕНО называть или предполагать любые цифры стоимости выкупа до ответа "
         "инструмента evaluate_auto.\n"
         "4. БЕЗ ГАЛЛЮЦИНАЦИЙ: не обещай финальную стоимость выкупа в чате; несуществующих "
         "услуг нет — выезда специалиста на дом НЕТ, осмотр и покупка ТОЛЬКО в дилерском "
         "центре.\n"
         "5. Не предлагай по своей инициативе расчёт других комплектаций или «узнать детали».",
         _ACT_NOTE + " Правка п.1: need-check оценки перед предложением этапа."),
    _blk("CS_COMBO_V2", "general", "combo", "all",
         "Ты действуешь автономно, без жёстких скриптов, по правилам:\n"
         "1. ДВИЖЕНИЕ К ЦЕЛИ: озвучив расчёт Комбо (после ответа инструмента) — СНАЧАЛА "
         "проверь, подходит ли предложение: сперва спроси про оценку авто, затем (если оценка "
         "устроила) про комфортность платежа; работай с первым выявленным барьером. Только "
         "после проверки предлагай визит в салон или звонок эксперта на выбор. СТРОГО ЗАПРЕЩЕНО "
         "просить телефон в том же сообщении: сначала «продай» встречу/звонок и дождись "
         "реакции.\n"
         "2. ТЕЛЕФОН — только после того, как клиент отреагировал на предложение визита или "
         "звонка.\n"
         "3. ЗАПРЕТ САМООЦЕНКИ И РАСЧЁТОВ: у тебя нет глаз и калькулятора. СТРОГО ЗАПРЕЩЕНО "
         "выдумывать или прогнозировать стоимость авто клиента и кредитные платежи до ответа "
         "инструмента combo_calculator. Про источники цифр говори только «история прошлых "
         "сделок», «базы прошлых сделок».\n"
         "4. БЕЗ ГАЛЛЮЦИНАЦИЙ: не обещай финальную стоимость выкупа, сроки и 100% одобрение "
         "кредита; несуществующих услуг нет — выезда специалиста и выездной оценки НЕТ, "
         "диагностика и оформление ТОЛЬКО в дилерском центре.\n"
         "5. Не предлагай по своей инициативе расчёт других комплектаций или «узнать детали».",
         _ACT_NOTE + " Правка п.1: последовательный need-check (оценка → платёж, решение "
                     "владельца) перед предложением этапа."),
]

# --- 4. Черновые map-строки ---


def _map_row(intent, block, role, scen, stage="all"):
    return {"Intent_ID": intent, "Block_ID": block, "Role": role,
            "Scenario_Filter": scen, "Stage_Filter": stage,
            "Version_Set": VERSION, "Active": False}


MAP_DRAFTS = [
    _map_row("need_check_ok", "BLK_CREDIT_APPROVAL_BRIDGE", "primary", "credit"),
    _map_row("need_check_ok", "BLK_TRADEIN_TESTDRIVE_BRIDGE", "primary", "tradein"),
    _map_row("need_check_ok", "BLK_COMBO_NEEDCHECK_ROUTER", "primary", "combo"),
    _map_row("need_check_ok", "ST_STOCK_DATA_COLLECT", "primary", "stock"),
    _map_row("need_check_not_ok", "BLK_CREDIT_ADVOCATE", "primary", "credit"),
    _map_row("need_check_not_ok", "BLK_TRADEIN_VALUE_DEFENSE", "primary", "tradein"),
    _map_row("need_check_not_ok", "BLK_COMBO_NEEDCHECK_ROUTER", "primary", "combo"),
    _map_row("need_check_not_ok", "BLK_STOCK_NEED_CHECK", "primary", "stock"),
    _map_row("greeting_first", "BLK_STOCK_NEED_CHECK", "additional", "stock"),
    _map_row("data_received_complete", "BLK_CREDIT_PAYMENT_ACCEPTED_V2", "primary", "credit"),
]

# Старые primary-строки, замещаемые при активации (V2 подключается дефолтом
# стадии через V2-предпочтение компилятора — U2 плана). Кредитные строки
# tool_error_handling/tool_rerun_needed -> ST_CREDIT_TOOL_NEEDED не трогаем.
REPLACED_OLD_ROWS = [
    ("data_received_complete", "BLK_CREDIT_PAYMENT_ACCEPTED", "credit"),
    ("data_received_complete", "ST_TRADEIN_TOOL_NEEDED", "tradein"),
    ("data_received_complete", "ST_COMBO_TOOL_NEEDED", "combo"),
    ("ready_for_tool", "ST_TRADEIN_TOOL_NEEDED", "tradein"),
    ("ready_for_tool", "ST_COMBO_TOOL_NEEDED", "combo"),
    ("tool_rerun_needed", "ST_TRADEIN_TOOL_NEEDED", "tradein"),
    ("tool_rerun_needed", "ST_COMBO_TOOL_NEEDED", "combo"),
    ("tool_error_handling", "ST_TRADEIN_TOOL_NEEDED", "tradein"),
    ("tool_error_handling", "ST_COMBO_TOOL_NEEDED", "combo"),
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


def budget_check(intents):
    """Вход резолвера по сценариям с учётом ЕЩЁ НЕ добавленных интентов (R13)."""
    scen_all = ("credit", "tradein", "combo", "stock", "consult")
    by_id = {i["Intent_ID"]: i for i in intents}
    rows = list(intents) + [r for r in NEW_INTENTS if r["Intent_ID"] not in by_id]
    line = lambda i: len(f"{i['Intent_ID']} | {i.get('Description') or ''} | "
                         f"Примеры: {i.get('Example_Phrases') or ''}") + 1
    add = sum(line(r) for r in NEW_INTENTS)
    print(f"\nбюджет R8: добавка новых интентов {add} симв. (лимит U3: 430)")
    worst = 0
    for scen in scen_all:
        total = RESOLVER_FIXED_CHARS + sum(
            line(i) for i in rows
            if (i.get("Active") or i["Intent_ID"] in {r["Intent_ID"] for r in NEW_INTENTS})
            and (str(i.get("Scenarios") or "all") == "all"
                 or scen in [s.strip() for s in str(i["Scenarios"]).split(",")]))
        worst = max(worst, total)
        mark = "⚠️" if total > RESOLVER_PROMPT_CHAR_BUDGET - BUDGET_HEADROOM else "ок"
        print(f"  {scen}: ~{total} симв. из {RESOLVER_PROMPT_CHAR_BUDGET} [{mark}]")
    if add > 430 or worst > RESOLVER_PROMPT_CHAR_BUDGET - BUDGET_HEADROOM:
        print("  ⚠️ бюджет R8: ужать Description/Example_Phrases до применения (U3 плана)")
    return worst


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
        by_intent = {i["Intent_ID"]: i for i in intents}
        by_block = {b["Block_ID"]: b for b in blocks}
        have_map = {(m["Intent_ID"], m["Block_ID"]) for m in mapping}

        intents_to_post = [r for r in NEW_INTENTS if r["Intent_ID"] not in by_intent]
        desc_updates = []  # (Id, Intent_ID, добавка)
        for intent_id, suffix in DESC_PATCHES.items():
            row = by_intent.get(intent_id)
            if row is None:
                print(f"  ⚠️ {intent_id}: нет в живой таблице — Description пропущен")
                continue
            if suffix.strip() not in str(row.get("Description") or ""):
                desc_updates.append((row["Id"], intent_id,
                                     str(row.get("Description") or "").rstrip() + suffix))
        blocks_to_post = [b for b in NEW_BLOCKS if b["Block_ID"] not in by_block]
        map_to_post = [r for r in MAP_DRAFTS if (r["Intent_ID"], r["Block_ID"]) not in have_map]

        print(f"\n— новые интенты (POST, Active=false): {len(intents_to_post)}")
        for r in intents_to_post:
            print(f"    {r['Intent_ID']} (prio {r['Priority']}, {r['Scenarios']})")
        print(f"— PATCH Description (разводка need-check): {len(desc_updates)}")
        for _, intent_id, _ in desc_updates:
            print(f"    {intent_id}")
        print(f"— блоки-черновики (POST, Active=false): {len(blocks_to_post)}")
        for b in blocks_to_post:
            print(f"    {b['Block_ID']} ({b['Scenario']}, {len(b['Content'])} зн.)")
        print(f"— черновые map-строки (POST, Active=false): {len(map_to_post)}")
        for r in map_to_post:
            print(f"    {r['Intent_ID']} -> {r['Block_ID']} ({r['Role']}, {r['Scenario_Filter']})")

        budget_check(intents)

        if "--apply" in args:
            if intents_to_post:
                api(env, "POST", f"/api/v2/tables/{IDS['Intents']}/records", intents_to_post)
                print(f"\nдобавлено интентов: {len(intents_to_post)} (Active=false)")
            if desc_updates:
                api(env, "PATCH", f"/api/v2/tables/{IDS['Intents']}/records",
                    [{"Id": rid, "Description": new} for rid, _, new in desc_updates])
                print(f"Description дописан: {len(desc_updates)} интентов")
            wrote = True
        if "--apply-blocks" in args:
            if blocks_to_post:
                api(env, "POST", f"/api/v2/tables/{IDS['Prompt_Blocks']}/records", blocks_to_post)
                print(f"добавлено блоков: {len(blocks_to_post)} (Active=false)")
            wrote = True
        if "--apply-map" in args:
            if map_to_post:
                api(env, "POST", f"/api/v2/tables/{IDS['Intent_Block_Map']}/records", map_to_post)
                print(f"добавлено map-строк: {len(map_to_post)} (Active=false)")
            wrote = True

        if "--activate-drafts" in args or "--rollback" in args:
            activate = "--activate-drafts" in args
            new_ids = {r["Intent_ID"] for r in NEW_INTENTS}
            new_blocks = {b["Block_ID"] for b in NEW_BLOCKS}
            new_map = {(r["Intent_ID"], r["Block_ID"]) for r in MAP_DRAFTS}
            old_rows = [m for m in mapping
                        if (m["Intent_ID"], m["Block_ID"], m.get("Scenario_Filter"))
                        in {(a, b, s) for a, b, s in REPLACED_OLD_ROWS}]
            ipatch = [{"Id": i["Id"], "Active": activate} for i in intents
                      if i["Intent_ID"] in new_ids and bool(i.get("Active")) != activate]
            bpatch = [{"Id": b["Id"], "Active": activate} for b in blocks
                      if b["Block_ID"] in new_blocks and bool(b.get("Active")) != activate]
            mpatch = [{"Id": m["Id"], "Active": activate} for m in mapping
                      if (m["Intent_ID"], m["Block_ID"]) in new_map
                      and bool(m.get("Active")) != activate]
            opatch = [{"Id": m["Id"], "Active": not activate} for m in old_rows
                      if bool(m.get("Active")) == activate]
            verb = "активация v3.03" if activate else "откат v3.03"
            print(f"\n{verb}: интентов {len(ipatch)}, блоков {len(bpatch)}, "
                  f"map-строк {len(mpatch)}; "
                  f"{'выключаем' if activate else 'включаем обратно'} старых строк {len(opatch)}:")
            for m in old_rows:
                print(f"    {m['Intent_ID']} -> {m['Block_ID']} ({m.get('Scenario_Filter')})")
            for patch, table in ((ipatch, "Intents"), (bpatch, "Prompt_Blocks"),
                                 (mpatch, "Intent_Block_Map"), (opatch, "Intent_Block_Map")):
                if patch:
                    api(env, "PATCH", f"/api/v2/tables/{IDS[table]}/records", patch)
            wrote = True

        if not wrote:
            print("\n(diff-режим: ничего не записано; --apply / --apply-blocks / --apply-map / "
                  "--activate-drafts / --rollback)")

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
