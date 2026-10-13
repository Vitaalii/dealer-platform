"""Единый маппинг стадий Битрикс24 и идемпотентность n8n-callback.

Единственный источник соответствия статусов ИИ стадиям воронки: раньше карта
была скопирована в аварийном мосте и в handle_n8n_callback и требовала ручной
синхронизации. Реестр обработанных callback'ов защищает от дублей смены стадии
при ретраях Save-to-CRM (5x/3s) и при срабатывании аварийного моста.
"""

BITRIX_STAGE_MAP = {
    "visit": "C9:UC_0SOJJA",
    "call": "C9:UC_0SOJJA",
    "working": "C9:UC_HW8Q2H",
    "refuse": "C9:LOSE",
    "ban": "C9:APOLOGY",
    "in_progress": "C9:UC_MDA6RZ",  # стадия "Нет номера"
}

DEFAULT_STAGE = BITRIX_STAGE_MAP["working"]

# «ОЖИДАНИЕ» — приёмник телефонных барьеров (план 2026-09-02-002): стадия
# ручной обработки саппорта, карточка дилеру при уходе сюда не отправляется.
WAITING_STAGE = BITRIX_STAGE_MAP["working"]

_PROCESSED_KEY = "processed_callbacks"


def resolve_stage(status) -> str:
    return BITRIX_STAGE_MAP.get(status, DEFAULT_STAGE)


def resolve_callback_status(raw_status, smart_data, has_phone):
    """Нормализация статуса n8n-колбэка (план 2026-09-02-002, Р1-Р3).

    Белый список — ключи BITRIX_STAGE_MAP (единый источник: working
    распознаётся, Р2). Нераспознанный статус подменяется (эвристика
    next_step → visit, иначе call) ТОЛЬКО при валидном телефоне; без
    телефона и подстановка, и эвристика не выполняются (Р3) — статус
    остаётся сырым, стадию решает callback_stage, реестр дедупа получает
    сырую пару (решение В4).

    Возвращает (итоговый статус, была_подстановка, статус_распознан)."""
    if isinstance(raw_status, str) and raw_status in BITRIX_STAGE_MAP:
        return raw_status, False, True
    if not has_phone:
        return raw_status, False, False
    next_step = str((smart_data or {}).get("next_step", "")).lower()
    if "визит" in next_step or "салон" in next_step or "приедет" in next_step:
        return "visit", True, False
    return "call", True, False  # по умолчанию считаем, что это звонок


def callback_stage(status, has_phone) -> str:
    """Стадия сделки с телефонными барьерами (Р3/Р4) — общая для колбэка
    и аварийного моста.

    Барьер переопределяет ТОЛЬКО стадию: visit/call без валидного телефона —
    в ОЖИДАНИЕ вместо «Обогащения» (Р4); нераспознанный статус без телефона
    попадает туда же через DEFAULT_STAGE (Р3). Данные сделки, стенограмма
    и архив барьером не затрагиваются."""
    if status in ("visit", "call") and not has_phone:
        return WAITING_STAGE
    return resolve_stage(status)


def _key(deal_id, status) -> str:
    return f"{deal_id}:{status}"


def is_duplicate_callback(ctx: dict, deal_id, status) -> bool:
    """True, если callback с этой парой (deal_id, status) уже обработан в сессии."""
    if not deal_id:
        return False
    return _key(deal_id, status) in ctx.get(_PROCESSED_KEY, [])


def register_callback(ctx: dict, deal_id, status) -> None:
    """Помечает пару (deal_id, status) обработанной.

    Реестр — list, а не set: ctx уезжает в БД через json-сериализацию.
    """
    if not deal_id:
        return
    registry = ctx.setdefault(_PROCESSED_KEY, [])
    key = _key(deal_id, status)
    if key not in registry:
        registry.append(key)
