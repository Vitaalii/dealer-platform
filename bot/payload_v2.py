"""Payload v2: слоты, детерминированные флаги и история для n8n (U4, R5/R14/R15/R30).

Python — единственный источник диалогового состояния. На каждый ход в payload
уезжают `recent_history` (последние сообщения без технических) и `session_context`
(слоты + флаги + эвристическая стадия). Старый воркфлоу эти поля игнорирует;
конвейер v3.0 строит на них Stage Classifier и Prompt Compiler.

Детерминированные триггеры (телефон, госномер, стоп-слова) вычисляются здесь,
на стороне Python, — n8n только читает флаги (единый источник регэкспов).
До фазы D стадия — эвристика; из ответа n8n она начнёт обновляться только
на v3-сессиях.
"""
import logging
import os
import re
import time

logger = logging.getLogger(__name__)

# ── Извлечение телефона: формы ЗАПИСИ номера (план 2026-09-07-002, Р9/KTD-8) ─
# Инцидент 51365: «8/980/555-28-17» валиден по is_valid_phone, но старый
# регэксп со списком разделителей его не извлекал. Первое лечение (паритет
# «всё, что digit-strip признаёт номером») ловило склейки чисел предметной
# области: «800 000, 30 000» → 11 цифр с ведущей 8 → «+70000030000». Замер
# на живом архиве: 2 таких ложных на 632 диалога (госномер+пробег).
#
# Действующий критерий (Р9): извлекаются формы ЗАПИСИ телефона. Последствия
# асимметричны — пропущенный номер виден (сделка в ОЖИДАНИИ), ложно пойманный
# невидим (недозвонимый номер у дилера); ошибаться в сторону строгости.
# Три слоя:
#  1) кандидат — цифры и телефонные разделители; запятая разделителем НЕ
#     является, « - » (дефис в пробелах — диапазон) режет кандидата (KTD-9);
#  2) окно внутри кандидата — ЦЕЛЫЕ цифро-группы, паттерн длин которых входит
#     в PHONE_GROUP_PATTERNS (KTD-8); валидность окна решает is_valid_phone;
#  3) сырого fallback «всё сообщение целиком» нет (KTD-10): путь A сознательно
#     СТРОЖЕ пути C (Д6 — в воронке всё сообщение и есть ответ на прямой
#     вопрос о телефоне, контекст другой).
_PHONE_CANDIDATE_RE = re.compile(r'[+\d][\d\s\-\(\)\./]*\d')
_RANGE_CUT_RE = re.compile(r'\s-\s')
_DIGIT_GROUP_RE = re.compile(r'\d+')

# Д7 (план 2026-09-07-002): закрытый формат телефонной записи — белый список
# разрядностей (длины цифро-групп окна). Монолит, «8 9805552817», «8 980 5552817»,
# «8 980 555 2817», «8 980 555 28 17», «8980 …» и те же формы без кода страны.
# Денежные пары сюда не попадают (3-3-2-3, 6-5, 2-3-3-3 …). ПОПОЛНЕНИЕ — ТОЛЬКО
# ВМЕСТЕ С ТЕСТОМ в tests/test_phone_extract.py; промахи видны в логе по метке
# PHONE_PATTERN_MISS (Д4).
PHONE_GROUP_PATTERNS = frozenset({
    (11,), (1, 10), (1, 3, 7), (1, 3, 3, 4), (1, 3, 3, 2, 2),
    (4, 7), (4, 3, 4), (4, 3, 2, 2),
    (10,), (3, 7), (3, 3, 4), (3, 3, 2, 2),
})


def extract_phone(text, session_id=None):
    """Телефон из текста клиента «как прислан» (подстрока) или None.

    Валидность окна решает database.is_valid_phone; форму записи — белый
    список PHONE_GROUP_PATTERNS. Ведущий «+» кандидата сохраняется (KTD-11).

    session_id — только для наблюдаемости Д4: кандидат с телефонной суммой
    цифр, но нетелефонной разрядностью логируется меткой PHONE_PATTERN_MISS
    (данные для пополнения списка приходят сами). Без session_id (например,
    вычисление флага для n8n) промахи не логируются — иначе каждая находка
    дублировалась бы на каждом ходе."""
    from database import is_valid_phone  # локальный импорт: без циклов на старте
    misses = []
    for raw in _PHONE_CANDIDATE_RE.finditer(text or ""):
        for cand in _RANGE_CUT_RE.split(raw.group(0)):
            groups = [(g.start(), g.end()) for g in _DIGIT_GROUP_RE.finditer(cand)]
            candidate_missed = False
            for i in range(len(groups)):
                for j in range(i, len(groups)):
                    lens = tuple(e - s for s, e in groups[i:j + 1])
                    if sum(lens) > 11:
                        break
                    if sum(lens) < 10:
                        continue
                    # KTD-11: «+» прямо перед первой группой окна — часть номера
                    start = groups[i][0]
                    if start > 0 and cand[start - 1] == '+':
                        start -= 1
                    window = cand[start:groups[j][1]]
                    if not is_valid_phone(window):
                        continue
                    if lens in PHONE_GROUP_PATTERNS:
                        return window.strip()
                    candidate_missed = True
            if candidate_missed:
                misses.append(cand.strip())
    if session_id and misses:
        for cand in misses:
            logger.warning("PHONE_PATTERN_MISS session=%s candidate=%r "
                           "(цифры телефонные, разрядность вне белого списка)",
                           session_id, cand)
    return None

# Госномер РФ — как в валидации TOOL_ AUTO.RU EVALUATION (кириллица + латиница)
PLATE_RE = re.compile(r'[АВЕКМНОРСТУХABEKMHOPCTYX]\d{3}[АВЕКМНОРСТУХABEKMHOPCTYX]{2}\d{2,3}')

# Категорические стоп-слова: только флаг для Stage Classifier, решение — за конвейером
STOP_WORDS = (
    "не пишите", "не пиши мне", "отстань", "отвяжись", "отписаться", "отпишись",
    "хватит писать", "прекратите", "удалите мой номер", "удали мой номер",
    "я вас заблокирую", "не звоните",
)

RECENT_HISTORY_LIMIT = 8

# ── Граница передачи ИИ (план 2026-08-21-001) ───────────────────────────────
# Маркер-строка в chat_history: роль system не проходит фильтр ролей
# recent_history (в n8n не утекает), type — машинный токен среза (КТР-2:
# идентичность не на отображаемом тексте), текст свободен для правок и
# виден в стенограммах (format_chat_history рендерит system-строки).
AI_HANDOFF_TYPE = "ai_handoff"
AI_HANDOFF_TEXT = "— Диалог передан ИИ-ассистенту —"


def make_handoff_marker(time_str: str) -> dict:
    """Строка-граница «дальше — диалог с Александром» для chat_history."""
    return {"time": time_str, "role": "system", "type": AI_HANDOFF_TYPE,
            "text": AI_HANDOFF_TEXT}

# Слоты, обязательные для вызова инструмента, по сценариям
_REQUIRED_SLOTS = {
    "credit": ("payment",),
    "tradein": ("plate", "mileage"),
    "combo": ("payment", "plate", "mileage"),
}


# ── Защёлка «оценка получена» (план 2026-08-14-001, U4) ─────────────────────
# Правило стадии — ЗА ВЫКЛЮЧЕННЫМ флагом: включение только синхронно с
# готовностью стадийной половины n8n (условие включения в плане). Причина —
# Ф5 плана: бот-эвристика для tradein/combo tool_needed не выдаёт никогда
# (слот mileage мёртв), источник повторных вызовов оценки — на стороне n8n.

# Латиница → кириллица для букв госномера (гомоглифы; в репо был урок
# про кириллическую «С» — сравнение номеров обязано их не различать)
_PLATE_HOMOGLYPHS = str.maketrans("ABEKMHOPCTYX", "АВЕКМНОРСТУХ")


def stage_eval_rule_enabled() -> bool:
    """env-флаг правила стадии (дефолт ВЫКЛ); читается при вызове —
    тесты используют monkeypatch.setenv."""
    return os.getenv("STAGE_EVAL_RULE", "").strip().lower() in ("1", "true", "yes", "on")


def normalize_plate(raw) -> str:
    """Каноническая форма госномера для сравнения: без пробелов/дефисов,
    верхний регистр, латинские гомоглифы приведены к кириллице."""
    if not raw:
        return ""
    return re.sub(r"[\s\-]", "", str(raw)).upper().translate(_PLATE_HOMOGLYPHS)


# ── Канонизация ВОРОНКОВОГО ввода номера (план 2026-09-08-001, U5/KTD-16) ───
# Кейс А (Радар): «T582XP 33 rus» — латиница, регион через пробел, хвост rus —
# ушёл в qualification_context как есть, инструмент оценки отверг, клиент
# потерян после двух слепых переспросов. Наша зона — слот воронки перед
# хендофом; диалоговые вводы идут моделью прямо в инструмент (n8n-сторона).
_PLATE_COUNTRY_TAIL_RE = re.compile(r'(RUS|РУС|RU)$')
_PLATE_CANON_RE = re.compile(
    r'([АВЕКМНОРСТУХ])(\d{3})([АВЕКМНОРСТУХ]{2})(\d{2,3})')
# Коды регионов РФ: 2-значные 01-99; 3-значные только с ведущей 1/7/9
# (правило ГИБДД; ловит класс «пробег принят за регион» — «К647МУ211»).
# Пополнение/правка правила — только вместе с тестом.


def normalize_plate_input(raw):
    """Канон госномера из воронкового ввода или None (не привёлся).

    strip/upper/гомоглифы (normalize_plate) → отрезание хвоста RUS/РУС/RU →
    структура буква+3цифры+2буквы+регион → регион по правилу. None означает
    «оставить исходный ввод как есть» (fail-open: инструмент оценки отвергнет
    сам, бот исходную строку клиента не теряет)."""
    canon = normalize_plate(raw)
    if not canon:
        return None
    canon = _PLATE_COUNTRY_TAIL_RE.sub("", canon)
    m = _PLATE_CANON_RE.fullmatch(canon)
    if m is None:
        return None
    region = m.group(4)
    if region == "00" or (len(region) == 3 and region[0] not in "179"):
        return None
    return canon


def register_evaluation(ctx: dict) -> None:
    """Выставление защёлки при сохранении свежего json_cache (main.py,
    wiring-only): валидная оценка (ti_valuation — положительное число) →
    персистентные слоты evaluation_received/evaluation_plate. Защёлка,
    а не чтение кэша на каждом ходе (R9): переживает пересборку json_cache
    и не зависит от того, вливает ли версия воркфлоу evaluation_carryover
    обратно (без осцилляции стадий). Мусор/объект-ошибка защёлку не ставит;
    отсутствие оценки в новом кэше её НЕ снимает — снятие только по новому
    госномеру (см. build_session_context)."""
    cache = ctx.get("n8n_cache")
    if not isinstance(cache, dict):
        return
    valuation = cache.get("ti_valuation")
    if isinstance(valuation, bool) or not isinstance(valuation, (int, float)) \
            or valuation <= 0:
        return
    found = cache.get("found_info") if isinstance(cache.get("found_info"), dict) else {}
    plate = normalize_plate(found.get("plate")) or None
    # Ревью 2026-08-15 (adversarial): после снятия защёлки новым номером
    # эхо evaluation_carryover ещё несёт СТАРУЮ оценку в свежих json_cache —
    # без этой проверки следующий же ход ре-защёлкивал бы снятый номер (R10
    # побеждён). Снятый номер помечается stale; новая оценка (другой номер
    # или свежая по снятому) ставится штатно, когда n8n её реально сделает.
    if plate and plate == ctx.get("evaluation_stale_plate"):
        return
    ctx.pop("evaluation_stale_plate", None)
    ctx["evaluation_received"] = True
    ctx["evaluation_plate"] = plate
    # План 2026-09-07-001 (U5): сумма оценки — персистентным слотом по образцу
    # защёлки: n8n_cache пересобирается (TTL/стадии), значение в нём не вечно.
    # Источник механический (ti_valuation инструмента auto.ru), не модельный.
    ctx["evaluation_value"] = valuation
    # План 2026-09-08-001 (U1, KTD-13): данные машины — тем же персистентным
    # слотом; found_info кэша не вечен, а пути завершения без ai_payload
    # (таймаут кабинета, Логика Б, фолбэк) читают именно слоты защёлки.
    # Ключи кладутся как есть, отсутствующие не выдумываются.
    # Пробег из ответа оценки не берётся (решение владельца 2026-10-03):
    # Auto.ru отдаёт свой пробег, а не пробег клиента
    car = {}
    for src_key, dst_key in (("mark", "make"), ("model", "model"),
                             ("year", "year"), ("plate", "plate")):
        if found.get(src_key) not in (None, ""):
            car[dst_key] = found[src_key]
    if car:
        ctx["evaluation_car"] = car


def _maybe_unlatch_evaluation(ctx: dict, message_text: str) -> None:
    """Снятие защёлки — по госномеру из ТЕКСТА текущего сообщения (R10).

    Слот plate в снятии не участвует: после передачи ИИ он не обновляется
    никогда (Ф5 плана — пишется только Dialogflow-воронкой), и завязка на
    него запирала бы клиента со старой оценкой. Новый номер, нормализованно
    отличный от оценённого (или любой номер при защёлке без привязки), →
    защёлка снята, слот plate обновлён извлечённым значением — повторная
    оценка не блокируется. Смена пробега защёлку не снимает — принятое
    ограничение плана.

    Весь механизм (включая мутацию слота plate) — за тем же env-флагом, что
    и правило стадии (ревью 2026-08-15): с выключенным флагом прод-поведение
    не меняется вовсе, слот plate обновляется только Dialogflow-воронкой."""
    if not stage_eval_rule_enabled():
        return
    if not ctx.get("evaluation_received"):
        return
    probe = re.sub(r"[\s\-]", "", message_text or "").upper()
    match = PLATE_RE.search(probe)
    if not match:
        return
    plate = normalize_plate(match.group(0))
    known = ctx.get("evaluation_plate")
    if known and plate == known:
        return  # тот же автомобиль — оценка действительна
    ctx.pop("evaluation_received", None)
    ctx.pop("evaluation_plate", None)
    ctx.pop("evaluation_value", None)  # оценка снятого номера недействительна
    ctx.pop("evaluation_car", None)    # и данные снятой машины тоже
    # Маркер против ре-защёлки эхом carryover (см. register_evaluation)
    if known:
        ctx["evaluation_stale_plate"] = known
    ctx["plate"] = plate


def detect_flags(message_text: str) -> dict:
    """Детерминированные флаги по тексту текущего сообщения клиента."""
    text = message_text or ""
    plate_probe = re.sub(r"[\s\-]", "", text).upper()
    lowered = text.lower()
    return {
        "has_valid_phone_in_msg": extract_phone(text) is not None,
        "has_valid_plate": bool(PLATE_RE.search(plate_probe)),
        "stop_word_detected": any(sw in lowered for sw in STOP_WORDS),
    }


def build_recent_history(chat_history: list, limit: int = RECENT_HISTORY_LIMIT) -> list:
    """История для n8n: срез от ПОСЛЕДНЕЙ границы передачи, без служебных строк.

    План 2026-08-21-001: Александр стартует с чистого листа — транскрипт
    воронки квалификации в производную не попадает (на INIT срез пуст),
    полная история живёт в архиве chat_history. Дополнительно исключаются
    строки-кнопки (type=button): это служебная механика запуска сценариев,
    в том числе нажатия, сделанные самим ботом (main.py, авто-«ПО ТЕЛЕФОНУ»),
    — модель не должна ссылаться на «выбор», которого клиент не делал.
    Маркера нет (легаси-диалог до выката, усечение истории при сохранении) →
    текущее окно без отсечки — безопасный fallback (R6), не исключение."""
    history = list(chat_history or [])
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("type") == AI_HANDOFF_TYPE:
            history = history[i + 1:]
            break
    cleaned = []
    for msg in history:
        if msg.get("type") == "button":
            continue
        text = (msg.get("text") or "").strip()
        role = msg.get("role")
        if not text or role not in ("user", "bot"):
            continue
        if text.startswith("__SYSTEM") or text.startswith("INIT_AI_HANDOFF"):
            continue
        cleaned.append({"role": "client" if role == "user" else "bot", "text": text})
    return cleaned[-limit:]


def _collected_data(ctx: dict) -> dict:
    data = {
        "payment": ctx.get("payment", 0),
        "model": ctx.get("model", ""),
        "model_id": ctx.get("model_id"),
        "complectation_id": ctx.get("complectation_id"),
        "plate": ctx.get("plate", ""),
        "phone_collected": bool(ctx.get("phone_collected")),
        "deal_id": ctx.get("deal_id"),
        # План 2026-08-14-001, U4: защёлка приходит в чистую стадийную
        # функцию через collected — мутаций ctx внутри эвристики нет
        "evaluation_received": bool(ctx.get("evaluation_received")),
    }
    if ctx.get("mileage"):
        data["mileage"] = ctx.get("mileage")
    return data


def compute_stage_heuristic(request_type: str, collected: dict, msg_count: int) -> str:
    """Эвристическая стадия до фазы D (v3-сессии начнут получать её из ответа n8n).

    Словарь стадий — из CONTEXT_RESOLVER_SPEC: greeting / data_collect /
    tool_needed / closing. Порядок приоритетов (план 2026-08-14-001):
    phone_collected → greeting → защёлка оценки (за env-флагом) → слоты.
    """
    if collected.get("phone_collected"):
        return "closing"
    if msg_count == 0:
        return "greeting"
    # U4 (за ВЫКЛЮЧЕННЫМ флагом STAGE_EVAL_RULE): оценка получена →
    # closing, повторный tool_needed не выставляется (R8)
    if (stage_eval_rule_enabled() and request_type in ("tradein", "combo")
            and collected.get("evaluation_received")):
        return "closing"
    required = _REQUIRED_SLOTS.get(request_type, ())
    if required and all(collected.get(slot) for slot in required):
        return "tool_needed"
    return "data_collect"


def build_session_context(ctx: dict, message_text: str, prompt_key: str,
                          dealer_info: dict = None) -> dict:
    """session_context для payload: слоты + счётчики + флаги + стадия.

    Единственное место с ctx — здесь же снятие защёлки оценки по новому
    госномеру в тексте (ДО вычисления стадии); сама стадийная функция
    остаётся чистой.

    dealer_info — карточка дилера (CACHED_DEALERS): v3.0 собирает системный
    промпт сам из блоков NocoDB с плейсхолдерами [dealer_name]/[dealer_address]/
    [dealer_hours], поэтому значения уезжают отдельными полями на КАЖДОМ ходе.
    Значения — как есть, без умолчаний: null означает «данных в справочнике
    нет», политику пустых значений реализует сторона n8n."""
    _maybe_unlatch_evaluation(ctx, message_text)
    chat_history = ctx.get("chat_history") or []
    msg_count = sum(1 for m in chat_history if m.get("role") == "user")
    collected = _collected_data(ctx)
    request_type = ctx.get("scenario_type", "unknown")
    info = dealer_info or {}
    return {
        "request_type": request_type,
        "collected_data": collected,
        "phone_ask_count": ctx.get("phone_ask_count", 0),
        "msg_count": msg_count,
        "appointment": ctx.get("appointment"),
        "current_stage": compute_stage_heuristic(request_type, collected, msg_count),
        "flags": detect_flags(message_text),
        "channel": ctx.get("channel", "max"),
        "version_set": prompt_key,
        "deal_finalized": bool(ctx.get("deal_finalized")),
        "dealer_name": info.get("dealer_name"),
        "dealer_address": info.get("dealer_address"),
        "dealer_hours": info.get("dealer_hours"),
    }


# --- U6: свежесть каталожного кэша (R17) ---

CACHE_TTL_SECONDS = 600  # симметрично CACHE_TTL каталога DICT_STOCKS
REFRESH_STAGES = ("tool_needed", "closing")  # моменты, где цена решает


def should_refresh_cache(ctx: dict, new_stage: str, prev_stage, now=None,
                         ttl_seconds: int = CACHE_TTL_SECONDS) -> bool:
    """True, если cached_json пора сбросить (n8n перезапросит каталог).

    Триггеры: возраст кэша больше TTL (или возраст неизвестен — legacy-кэш),
    либо ПЕРЕХОД на стадию расчёта/финализации. Переход, а не пребывание:
    иначе каждый ход в closing перезапрашивал бы каталог.
    """
    if not ctx.get("n8n_cache"):
        return False
    now = time.time() if now is None else now
    saved_at = ctx.get("n8n_cache_saved_at")
    if saved_at is None or (now - saved_at) > ttl_seconds:
        return True
    return new_stage in REFRESH_STAGES and new_stage != prev_stage


def extract_selected_complectation(cache):
    """Выбранная комплектация из кэша — не должна потеряться при сбросе.

    Старый воркфлоу ищет её в qualification_context.complectation_id первым
    приоритетом, поэтому перенос туда сохраняет выбор без правки n8n.
    """
    if not isinstance(cache, dict):
        return None
    comp = cache.get("complectation_id") \
        or (cache.get("user_request") or {}).get("selected_complectation_id")
    comp = str(comp) if comp not in (None, "") else ""
    return comp if comp and comp != "00" else None


# --- Перенос данных оценки auto.ru (контракт docs/N8N_HANDOFF_CABINET.md) ---

def build_evaluation_carryover(ctx: dict):
    """qualification_context.evaluation_carryover — страховка от пересборки
    json_cache: found_info/ti_valuation доезжают до n8n и на rebuild-ходе,
    когда cached_json уходит пустым (иначе источник переноса пуст именно
    тогда, когда нужен). Шлётся на каждом ходе. request{plate, mileage} —
    задел для сверки n8n при кэшировании платной оценки.

    Нет данных оценки → None: ключ в payload НЕ появляется вовсе (не пустой
    объект) — приёмная сторона различает отсутствие и пустоту."""
    cache = ctx.get("n8n_cache")
    if not isinstance(cache, dict):
        return None
    found = cache.get("found_info")
    found = found if isinstance(found, dict) and found else None
    ti = cache.get("ti_valuation")
    if found is None and ti is None:
        return None
    return {
        "found_info": found,
        "ti_valuation": ti,
        "request": {
            "plate": found.get("plate") if found else None,
            "mileage": found.get("mileage") if found else None,
        },
    }


def extend_payload(payload: dict, ctx: dict, message_text: str, prompt_key: str,
                   dealer_info: dict = None) -> dict:
    """Добавляет v2-поля на верхний уровень payload, не трогая существующие.

    Обратная совместимость (R30): существующие ключи payload и содержимое
    qualification_context не изменяются — старый воркфлоу новые поля игнорирует.
    """
    payload["recent_history"] = build_recent_history(ctx.get("chat_history"))
    payload["session_context"] = build_session_context(ctx, message_text, prompt_key,
                                                       dealer_info)
    return payload
