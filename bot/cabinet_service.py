"""U3: приём статусов доставки и OTP-оркестрация кабинета (R26 сигнал A, R29–R32, R36).

Строгий лист: НЕ импортирует main.py. Все зависимости от main.py приходят
через `setup(...)` — единственный канал связи; тесты подменяют их тем же
`setup()`. Шлюз отправки берётся из sms_gateway.get_sms_gateway(), для тестов
подменяется `set_gateway()`.

Содержимое:
- колбэк статусов ProstoSMS (GET/POST, без аутентификации, строгая пара
  external_id + id_sms, phone из запроса не используется — R31/R36).
  Статус — запись-факт: пишется в delivery_state и лог, на поведение не
  влияет (статусы провайдера недостоверны — решение владельца 2026-08-27,
  автофолбэк голос→SMS снят);
- `otp_worker_tick()` — импортируемое тело короткоинтервального цикла;
- circuit breaker c half-open (transport+service считаются, subscriber — нет);
- обязанности sync_worker: экспирация токенов и проверка баланса (R32/R33).
"""
import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import math
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

from aiohttp import web
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

import database
import log_masking
import sms_gateway
from database import (
    CabinetEvent,
    CabinetForm,
    CabinetOtp,
    CabinetProfile,
    CabinetSession,
    CabinetToken,
    Consent,
    PendingSync,
    User,
)

logger = logging.getLogger("cabinet")

# =======================
# Константы U3
# =======================

# Значимые state колбэка (origin-дока: реальные значения снять первой тестовой
# отправкой — Open Questions плана; парсер оборонительный, прочие значения
# игнорируются с логом).
DELIVERED_STATES = frozenset({"1"})            # доставлено
UNDELIVERED_STATES = frozenset({"2", "16", "34"})  # не доставлено
SIGNIFICANT_STATES = DELIVERED_STATES | UNDELIVERED_STATES

BREAKER_WINDOW_SECONDS = 600       # окно подсчёта отказов breaker'а: «5 за 10 мин»
SIGNAL_A_DELAY_SECONDS = 15 * 60   # R26: code_requested без verified 15 минут


def _esc(value) -> str:
    """Экранирование значения клиента для HTML-алерта.

    Все алерты уходят через send_alert_to_group с format="html": имя клиента,
    модель или сделка с символами < > & иначе ломают разметку сообщения
    (обрезка/искажение текста у дилера). Применяется к КАЖДОЙ подстановке
    пользовательского значения в шаблоны алертов."""
    return html.escape("" if value is None else str(value))


SIGNAL_A_TEMPLATE = (
    "⚠️ Личный кабинет: {name} ({channel}), сделка {deal_id} ({model}) — "
    "запросил код, но не подтвердил номер. Заявка в CRM не передана. "
    "Продолжите в чате вручную."
)

# =======================
# Инъекция зависимостей из main.py (единственный канал)
# =======================

_bitrix_request = None      # обёртка над BitrixService._request (потребители — U4/U5);
                            # контракт: САМА в pending_sync не ставит — единственная
                            # постановка идёт через _enqueue_nodebounce (R35)
_enqueue_nodebounce = None  # enqueue_failed_request(..., skip_debounce=True) (R35)
_run_conversion = None      # BitrixService.final_sync_wrapper — потребитель U10
_dealer_alert = None        # send_dealer_alert(dealer_code, text)
_tech_alert = None          # алерт в TECH_ADMIN_ID
_dealers_provider = None    # lambda: CACHED_DEALERS
# U5 (план 2026-08-06, R13/R15): загрузчик таблиц NocoDB — async callable
# table_id -> list[dict] (main.get_cars_from_nocodb_dynamic, TTL-кэш 600 с
# внутри). Токен NocoDB в cabinet_service НЕ передаётся — только загрузчик.
_nocodb_table_loader = None
# Базовый URL NocoDB для нормализации относительных путей вложений (main.py:
# NOCODB_API_URL); наружу уходят только абсолютные URL.
_nocodb_base_url = None

_gateway_override: Optional[sms_gateway.SmsGateway] = None
_catalog_row_loader = None   # веб-вход U3: точечный запрос строки каталога по паре
_find_deal_by_code = None    # веб-вход U5: сделка Tilda по коду в «Новая» (только чтение)
_handoff_stage = None        # стадия передачи («Обогащение») — ставится ПОСЛЕ полей анкеты
_bitrix_enabled = None       # () -> bool: Битрикс включён (непустой BITRIX_WEBHOOK_URL)
# Условная смена стадии (план 2026-10-12-001): BitrixService.set_stage_guarded
# (deal_id, stage_id, expect_stage, user_id=) -> bool; сама ставит в очередь
_set_stage = None
STAGE_EXPECT_KEY = "_expect_stage"   # ключ «было» в задаче pending_sync (main.STAGE_EXPECT_KEY)

# Колбэк «передача заявки доведена» (план 2026-08-14-001, U1): main.py завершает
# этим хуком сессию диалога с ИИ. Вызов — fire-and-forget (_fire_handoff_completed):
# _complete_handoff await-ится из HTTP-хендлера submit и последовательных циклов
# otp_worker, а завершение сессии может ждать turn_lock хода до минут.
_on_handoff_completed = None
_HOOK_TASKS: set = set()  # сильные ссылки на fire-and-forget задачи (иначе GC)
# In-flight дедуп по пользователю (ревью 2026-08-15, adversarial/reliability):
# пока задача завершения жива, вторую для того же user_id не плодим — иначе
# добор на каждом тике копил бы задачи, ждущие один и тот же turn_lock
_HOOK_INFLIGHT: dict = {}


def setup(bitrix_request, enqueue_nodebounce, run_conversion,
          dealer_alert, tech_alert, dealers_provider,
          nocodb_table_loader=None, nocodb_base_url=None,
          on_handoff_completed=None, catalog_row_loader=None,
          find_deal_by_code=None, handoff_stage=None, bitrix_enabled=None,
          set_stage=None) -> None:
    """Единственный канал получения зависимостей из main.py.

    Тесты подменяют зависимости этим же setup(). run_conversion — обёртка
    main.py над существующей конверсией; потребитель — attempt_handoff (U10).
    nocodb_table_loader / nocodb_base_url — U5 (данные калькулятора): загрузчик
    таблиц дилера (ставки/программа/каталог) и база для абсолютных URL фото.
    on_handoff_completed — асинхронный колбэк (user_id, trigger, deal_id,
    token_id) о ДОВЕДЁННОЙ передаче заявки (план 2026-08-14-001, U1); None —
    поведение до правки (ничего не вызывается).
    handoff_stage — стадия сделки после передачи заявки («Обогащение»,
    BITRIX_STAGE_QUALIFIED): конверсия идёт без смены стадии, стадию ставит
    _complete_handoff последним шагом — после записи полей анкеты, чтобы робот
    стадии собрал письмо дилеру по заполненным полям. None — стадию не трогать.
    catalog_row_loader — веб-вход (план 2026-09-30-001, U3): точечный запрос
    строки каталога по паре (table_id, model_id, complectation_id) → строка
    или None; сетевой сбой / HTTP ≠ 200 — исключение. None — фолбэк на
    полный загрузчик таблицы (nocodb_table_loader) и _find_catalog_row.
    bitrix_enabled — () -> bool, Битрикс включён; False — подбор зависших
    захватов не запускается (дев-стенд с пустым BITRIX_WEBHOOK_URL). None —
    считать включённым.
    set_stage — условная смена стадии (план 2026-10-12-001): повтор записи
    только если сделка всё ещё на стадии «было». None — прежний вызов.
    """
    global _set_stage
    global _bitrix_request, _enqueue_nodebounce, _run_conversion, _catalog_row_loader
    global _find_deal_by_code, _handoff_stage, _bitrix_enabled
    global _dealer_alert, _tech_alert, _dealers_provider
    global _nocodb_table_loader, _nocodb_base_url, _on_handoff_completed
    _bitrix_request = bitrix_request
    _enqueue_nodebounce = enqueue_nodebounce
    _run_conversion = run_conversion
    _dealer_alert = dealer_alert
    _tech_alert = tech_alert
    _dealers_provider = dealers_provider
    _nocodb_table_loader = nocodb_table_loader
    _nocodb_base_url = nocodb_base_url
    _on_handoff_completed = on_handoff_completed
    _catalog_row_loader = catalog_row_loader
    _find_deal_by_code = find_deal_by_code
    _handoff_stage = handoff_stage
    _bitrix_enabled = bitrix_enabled
    _set_stage = set_stage
    # U7 (R8/AE6): страж записи телефона из чата — покрывает ВСЕ чат-пути
    # save_user_phone (vCard, рефлекс телефона, воронка) одной точкой.
    database.PHONE_WRITE_GUARD = chat_phone_guard


def set_gateway(gateway: Optional[sms_gateway.SmsGateway]) -> None:
    """Подмена шлюза для тестов; None — вернуть роутер get_sms_gateway()."""
    global _gateway_override
    _gateway_override = gateway


def _gateway() -> sms_gateway.SmsGateway:
    return _gateway_override or sms_gateway.get_sms_gateway()


def _utcnow() -> datetime:
    """Текущее время UTC — timezone-aware.

    Все временные колонки кабинета объявлены DateTime(timezone=True), а бой —
    PostgreSQL/asyncpg: оттуда значения приходят aware. Наивный utcnow() ронял
    бы TypeError'ом каждое сравнение (validate_bearer, _get_live_token,
    otp_worker_tick), то есть весь кабинет. Внутреннее время — всегда aware UTC;
    значения, прочитанные из БД, нормализуются _aware()."""
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Нормализация прочитанного из БД времени к aware UTC.

    PostgreSQL (timezone=True) отдаёт aware, SQLite — наивное значение в UTC.
    Применяется к КАЖДОМУ значению из БД перед сравнением/арифметикой с
    _utcnow(); уже aware-значение возвращается как есть."""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


def _retry_after(oldest: datetime, window: timedelta, now: datetime) -> int:
    """Честный остаток окна лимита: секунды до выхода старейшей учтённой
    записи из окна (минимум 1 — фронт форматирует, ноль читался бы как сбой)."""
    return max(1, int((oldest + window - now).total_seconds()))


# --- Конфигурация (env читается при вызове — тесты используют monkeypatch.setenv) ---

def _breaker_threshold() -> int:
    return int(os.getenv("CABINET_BREAKER_THRESHOLD", "5"))


def _breaker_cooldown_seconds() -> float:
    return float(os.getenv("CABINET_BREAKER_COOLDOWN", "5")) * 60  # env в минутах


def _balance_threshold() -> float:
    return float(os.getenv("CABINET_BALANCE_THRESHOLD", "500"))


# [Р12 поправка владельца 2026-09-08] Дедупликация конфиг-алертов: адресами
# и маршрутами мы не управляем, эпизоды 602/627 будут повторяться — владельцу
# не нужна лента одинаковых сообщений. Первый случай — полный алерт; повторы
# в окне — строка TECH_ALERT_SUPPRESSED в лог; на N-м подряд — один
# эскалационный алерт «повторяется». Успешный get_balance закрывает окно.
ALERT_DEDUP_WINDOW_SECONDS = 6 * 3600
ALERT_ESCALATE_AFTER = 3

_config_alert_dedup: dict = {"window_start_monotonic": None,
                             "window_start_wall": None,
                             "count": 0, "escalated": False}


def reset_config_alert_dedup() -> None:
    """Инцидент закончился (баланс снова ok) — следующий отказ получит
    полный алерт заново. Также используется тестами."""
    _config_alert_dedup.update(window_start_monotonic=None,
                               window_start_wall=None,
                               count=0, escalated=False)


def _alert_retry_delay() -> float:
    """[Р11] Пауза перед повторной проверкой при 602/627, секунды (env)."""
    return float(os.getenv("CABINET_ALERT_RETRY_DELAY", "90"))


async def _maybe_config_alert(result, require_service: bool = True,
                              context: str = "") -> bool:
    """Общий блок «602/627 → техалерт конфигурации канала» (R33):
    needs_config_alert по err_code → _tech_alert с объяснением про внешний
    адрес, следующим шагом, маской ключа и контекстом вызова (U4/Р13).
    require_service=True (точки отправки) дополнительно требует
    error_class == 'service'. Дедупликация — Р12 (см. выше).
    True — алерт (полный или эскалационный) отправлен."""
    if require_service and result.error_class != "service":
        return False
    if not (sms_gateway.needs_config_alert(result.err_code) and _tech_alert):
        return False

    st = _config_alert_dedup
    now_m = time.monotonic()
    window_stale = (st["window_start_monotonic"] is None
                    or now_m - st["window_start_monotonic"] > ALERT_DEDUP_WINDOW_SECONDS)
    if window_stale:
        st.update(window_start_monotonic=now_m,
                  window_start_wall=datetime.now(timezone.utc).strftime("%H:%M UTC"),
                  count=1, escalated=False)
        text = await sms_gateway.build_config_alert_text(
            result.err_code, result.description, context=context)
        await _tech_alert(text)
        return True

    st["count"] += 1
    if st["count"] >= ALERT_ESCALATE_AFTER and not st["escalated"]:
        st["escalated"] = True
        await _tech_alert(
            f"🚨 ProstoSMS: код {result.err_code} ПОВТОРЯЕТСЯ — "
            f"{st['count']} случаев с {st['window_start_wall']}.\n"
            f"Контекст последнего: {context or 'не указан'}.\n"
            f"Похоже на эпизод маршрутизации (двухканальный выход Dockhost).\n"
            f"{sms_gateway.NEXT_STEP_HINT}"
        )
        return True

    logger.warning(
        "TECH_ALERT_SUPPRESSED %s повторился (%d-й случай с %s, окно %dч) — "
        "алерт не дублируется", result.err_code, st["count"],
        st["window_start_wall"], ALERT_DEDUP_WINDOW_SECONDS // 3600)
    return False


# =======================
# HMAC кодов
# =======================
# Код в БД не хранится (только HMAC). Обязательность ключа в проде
# (ENVIRONMENT=production) — check_secrets: смена ключа инвалидирует
# неистёкшие коды и активные токены.

def _hmac_key() -> bytes:
    return os.getenv("CABINET_HMAC_KEY", "dev-insecure-cabinet-key").encode("utf-8")


def compute_code_hmac(code: str) -> str:
    return hmac.new(_hmac_key(), code.encode("utf-8"), hashlib.sha256).hexdigest()


# =======================
# Circuit breaker (состояние в процессе; reset для тестов)
# =======================
# Считает только transport+service; subscriber исключён сознательно — иначе
# один клиент пятью запросами на плохие номера остановил бы отправку всем.
# Half-open вместо сброса по тишине: после охлаждения ровно одна пробная
# отправка — успех закрывает, неудача возвращает паузу.

_breaker = {"failures": [], "opened_at": None, "probe_inflight": False}


def reset_breaker() -> None:
    """Полный сброс состояния breaker'а (для тестов)."""
    _breaker["failures"] = []
    _breaker["opened_at"] = None
    _breaker["probe_inflight"] = False


def _breaker_prune(now: float) -> None:
    window_start = now - BREAKER_WINDOW_SECONDS
    _breaker["failures"] = [ts for ts in _breaker["failures"] if ts >= window_start]


def breaker_state(now: Optional[float] = None) -> str:
    """'closed' | 'open' | 'half_open' — наблюдаемость и send-status U4."""
    if _breaker["opened_at"] is None:
        return "closed"
    now = now if now is not None else time.time()
    if now - _breaker["opened_at"] < _breaker_cooldown_seconds():
        return "open"
    return "half_open"


def breaker_allows(now: Optional[float] = None) -> bool:
    """Разрешена ли отправка. В half-open выдаёт ровно одну пробную отправку."""
    now = now if now is not None else time.time()
    state = breaker_state(now)
    if state == "closed":
        return True
    if state == "open":
        return False
    # half-open: ровно одна пробная отправка
    if _breaker["probe_inflight"]:
        return False
    _breaker["probe_inflight"] = True
    return True


def _breaker_release_probe() -> None:
    """Пробная отправка не состоялась (захват не выигран и т.п.) — вернуть слот."""
    _breaker["probe_inflight"] = False


def breaker_record_success() -> None:
    """Успешная отправка: закрывает breaker из half-open; в closed — no-op
    (окно отказов скользящее, успехи его не чистят)."""
    if _breaker["opened_at"] is not None or _breaker["probe_inflight"]:
        reset_breaker()


def breaker_record_failure(error_class: Optional[str], now: Optional[float] = None) -> None:
    """Учёт отказа. subscriber НЕ считает (план: класс исключён сознательно)."""
    if error_class == "subscriber":
        return
    now = now if now is not None else time.time()
    if _breaker["opened_at"] is not None:
        # Неудача пробной отправки (или отказ в open) — новая пауза
        _breaker["opened_at"] = now
        _breaker["probe_inflight"] = False
        return
    _breaker["failures"].append(now)
    _breaker_prune(now)
    if len(_breaker["failures"]) >= _breaker_threshold():
        _breaker["opened_at"] = now
        _breaker["probe_inflight"] = False
        logger.warning("cabinet: circuit breaker открыт (%d отказов за %d с)",
                       len(_breaker["failures"]), BREAKER_WINDOW_SECONDS)


# =======================
# Колбэк статусов ProstoSMS (R31, R36)
# =======================

async def process_status_callback(external_id, id_sms, state) -> str:
    """Обработка одного статуса. Сопоставление СТРОГО по паре external_id+id_sms;
    phone из запроса не используется (R36). Неизвестные external_id — 200 без
    изменений. Статус — запись-факт (delivery_state + лог), на поведение не
    влияет: статусы провайдера недостоверны (измерено дважды, решение O3/O6)."""
    if not external_id:
        return "no_external_id"
    state_str = str(state).strip() if state is not None else ""

    async with database.AsyncSessionLocal() as session:
        otp = (await session.execute(
            select(CabinetOtp).where(CabinetOtp.external_id == external_id)
        )).scalars().first()

        if otp is None:
            logger.info("cabinet: колбэк с неизвестным external_id — игнор")
            return "unknown_external_id"

        expected_msg_id = otp.voice_msg_id if otp.channel == "voice" else otp.sms_msg_id

        if id_sms is None or expected_msg_id is None or str(id_sms).strip() != str(expected_msg_id):
            logger.warning("cabinet: колбэк OTP %s (%s): id_sms не совпал с сохранённым — игнор",
                           otp.id, otp.channel)
            return "msg_id_mismatch"

        if state_str not in SIGNIFICANT_STATES:
            logger.info("cabinet: колбэк OTP %s (%s): незначимый state=%s — игнор",
                        otp.id, otp.channel, state_str)
            return "ignored_state"

        otp.delivery_state = state_str
        await session.commit()
        logger.info("cabinet: колбэк OTP %s (%s): state=%s записан (поведение не меняется)",
                    otp.id, otp.channel, state_str)

    return "recorded"


async def handle_status_callback(request: web.Request) -> web.Response:
    """GET и POST /sms/status-callback — публичный, без аутентификации.
    Всегда 200: провайдеру не на что реагировать, неизвестное игнорируется."""
    params = dict(request.query)
    if request.method == "POST":
        try:
            if request.content_type and "json" in (request.content_type or ""):
                body = await request.json()
                if isinstance(body, dict):
                    params.update(body)
            else:
                params.update(dict(await request.post()))
        except Exception:
            pass  # оборонительный разбор: битое тело — работаем по query
    try:
        await process_status_callback(
            external_id=params.get("external_id"),
            id_sms=params.get("id_sms") or params.get("id"),
            state=params.get("state"),
        )
    except Exception:
        logger.exception("cabinet: ошибка обработки статус-колбэка")
    return web.Response(text="ok")


# =======================
# otp_worker: импортируемое тело одного прохода
# =======================

async def _finalized_elsewhere(user_id: int) -> bool:
    """Гейт таймаут-передачи (блокер С4 дев-прогона 2026-09-22): сессия
    диалога уже финализирована НЕ-кабинетным путём — Manager/DF-путь или
    n8n (признаки is_finalized/deal_finalized, прецедент _cabinet_close_gate
    в main.py). Таймаут-передача такой сессии дублировала бы менеджерский
    алерт и слала бы дилеру «Личный кабинет…», хотя клиент кабинет не
    выбирал (вечный rebind-токен со старых сессий)."""
    async with database.AsyncSessionLocal() as session:
        ctx_json = (await session.execute(
            select(database.User.context_json)
            .where(database.User.id == user_id))).scalar_one_or_none()
    try:
        ctx = json.loads(ctx_json) if ctx_json else {}
    except (TypeError, ValueError):
        ctx = {}
    return bool(ctx.get("deal_finalized") or ctx.get("is_finalized"))


async def _retire_finalized_elsewhere(user_id: int, trigger: str) -> None:
    """Токен сессии, закрытой другим путём: отзыв (прецедент rebind-ветки —
    revoked освобождает частичный уникальный индекс, новая сделка выдаст
    новый токен) + session_closed, чтобы кандидат не выбирался каждым тиком.
    Без конверсии и без алертов — заявка уже у менеджера штатным путём."""
    async with database.AsyncSessionLocal() as session:
        token = (await session.execute(
            select(CabinetToken).where(
                CabinetToken.user_id == user_id,
                CabinetToken.status == "active",
                CabinetToken.handoff_at.is_(None)))).scalars().first()
        if token is None:
            return
        token.status = "revoked"
        await session.commit()
        token_id = token.id
    await record_session_closed(user_id, token_id)
    logger.info("cabinet: таймаут-передача (%s) пропущена для user=%s — сессия "
                "финализирована не-кабинетным путём, токен отозван",
                trigger, user_id)


async def _check_handoff_timeouts(now: datetime) -> int:
    """U10: передача заявки по таймаутам (триггеры 2 и 3, R40).

    Кандидаты — активные токены с bound_at NOT NULL (R39) и handoff_at IS NULL.
    Триггер 3 (абсолютный потолок): now − bound_at > CABINET_HANDOFF_MAX_AGE —
    независимо от активности (забытая вкладка с пингами не держит телефон у нас).
    Триггер 2 (бездействие): now − max(last_seen_at, bound_at) >
    CABINET_HANDOFF_TIMEOUT — отсчёт от верификации, если сигналов жизни не было.
    Захват и вся работа — в attempt_handoff(); возвращает число конверсий."""
    timeout = timedelta(seconds=_handoff_timeout_seconds())
    max_age = timedelta(seconds=_handoff_max_age_seconds())
    async with database.AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(CabinetToken.user_id, CabinetToken.bound_at, CabinetToken.last_seen_at)
            .where(
                CabinetToken.status == "active",
                CabinetToken.bound_at.is_not(None),
                CabinetToken.handoff_at.is_(None),
            )
        )).all()

    converted = 0
    for user_id, bound_at, last_seen_at in rows:
        bound_at, last_seen_at = _aware(bound_at), _aware(last_seen_at)
        if bound_at is None:
            continue
        if now - bound_at > max_age:
            trigger = "max_age"                                   # триггер 3
        elif now - max(last_seen_at or bound_at, bound_at) > timeout:
            trigger = "timeout"                                   # триггер 2
        else:
            continue
        # Блокер С4 (2026-09-22): сессия финализирована не-кабинетным путём —
        # передача не нужна, токен отзываем без конверсии и алертов
        if await _finalized_elsewhere(user_id):
            await _retire_finalized_elsewhere(user_id, trigger)
            continue
        if await attempt_handoff(user_id, trigger, now=now) == "converted":
            converted += 1
    return converted


async def _recover_stuck_handoffs(now: datetime) -> int:
    """U10: подбор ЗАВИСШИХ захватов передачи (рестарт между захватом и фактом).

    Захват handoff_at коммитится отдельной транзакцией, а конверсия и отправка
    результата идут после неё: падение/рестарт в этом окне оставлял токен с
    проставленным handoff_at, по которому все триггеры фильтруют
    (handoff_at IS NULL) — верифицированный клиент не попадал в CRM и никто
    об этом не узнавал. Намерение и факт разведены событиями: 'handoff'
    пишется в момент захвата, 'handoff_done' — только после доведения
    (_complete_handoff). Кандидаты: активные токены с handoff_at старше
    HANDOFF_RECOVERY_DELAY_SECONDS без события 'handoff_done'.

    Повтор безопасен: конверсия (final_sync_wrapper) штатно повторяема,
    crm.deal.update идемпотентен, а комментарии и сигнал B закрыты
    гард-событиями _record_event_once. Техалерт «захвачено, но не завершено»
    уходит однократно на токен — чтобы факт рестарта в этом окне был виден.

    Битрикс выключен (пустой BITRIX_WEBHOOK_URL, дев-стенд) — подбор не
    запускается вовсе (2026-10-02): конверсия гарантированно падала бы
    каждые 30 с, а накопленные повторы ушли бы в портал при его включении."""
    if _bitrix_enabled is not None and not _bitrix_enabled():
        return 0
    cutoff = now - timedelta(seconds=HANDOFF_RECOVERY_DELAY_SECONDS)
    oldest = now - timedelta(seconds=HANDOFF_RECOVERY_MAX_AGE_SECONDS)
    async with database.AsyncSessionLocal() as session:
        done = (select(CabinetEvent.id)
                .where(CabinetEvent.token_id == CabinetToken.id,
                       CabinetEvent.event_type == "handoff_done")
                .exists())
        rows = (await session.execute(
            select(CabinetToken.id, CabinetToken.user_id, CabinetToken.handoff_at)
            .where(
                CabinetToken.status == "active",
                CabinetToken.handoff_at.is_not(None),
                CabinetToken.handoff_at <= cutoff,
                ~done,
            )
        )).all()

    recovered = 0
    for token_id, user_id, handoff_at in rows:
        too_old = (_aware(handoff_at) or now) < oldest
        logger.warning("cabinet: захват передачи без отметки завершения "
                       "(token=%s, user=%s, повтор=%s)", token_id, user_id, not too_old)
        if await _record_event_once(user_id, token_id, "handoff_stuck_alert") and _tech_alert:
            await _tech_alert(
                f"⚠️ Личный кабинет: передача заявки захвачена, но не завершена "
                f"(user_id={user_id}, token_id={token_id}). "
                + ("Возраст захвата больше суток — доведите вручную."
                   if too_old else "Воркер доводит её повторно.")
            )
        if too_old:
            continue
        if await _complete_handoff(user_id, "recovery"):
            recovered += 1
    return recovered


async def _expire_otp_codes(now: datetime) -> int:
    """(а) Экспирация кодов: sending/sent старше expires_at → expired."""
    async with database.AsyncSessionLocal() as session:
        res = await session.execute(
            update(CabinetOtp)
            .where(
                CabinetOtp.status.in_(("sending", "sent")),
                CabinetOtp.expires_at.is_not(None),
                CabinetOtp.expires_at <= now,
            )
            .values(status="expired")
        )
        await session.commit()
        return res.rowcount or 0


async def _send_signal_a_alerts(now: datetime) -> int:
    """(б) Сигнал A (R26): code_requested без bound_at через 15 минут →
    dealer_alert, однократно (пометка событием signal_a_sent); отменяется
    верификацией (bound_at)."""
    threshold = now - timedelta(seconds=SIGNAL_A_DELAY_SECONDS)
    sent = 0
    async with database.AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(CabinetToken, func.min(CabinetOtp.sent_at))
            .join(CabinetOtp, CabinetOtp.token_id == CabinetToken.id)
            .where(CabinetToken.status == "active",  # истёкшие/отозванные не сканируются вечно
                   CabinetToken.bound_at.is_(None))
            .group_by(CabinetToken.id)
        )).all()

        for token, first_sent_at in rows:
            first_sent_at = _aware(first_sent_at)
            if first_sent_at is None or first_sent_at > threshold:
                continue
            already = (await session.execute(
                select(CabinetEvent.id).where(
                    CabinetEvent.token_id == token.id,
                    CabinetEvent.event_type == "signal_a_sent",
                ).limit(1)
            )).scalar_one_or_none()
            if already is not None:
                continue

            user = await session.get(User, token.user_id)
            profile = (await session.execute(
                select(CabinetProfile).where(CabinetProfile.user_id == token.user_id)
            )).scalar_one_or_none()
            text = SIGNAL_A_TEMPLATE.format(
                name=_esc(user.full_name if user and user.full_name else "Клиент"),
                channel=_esc(user.channel if user and user.channel else "max"),
                deal_id=_esc(token.deal_id or "—"),
                model=_esc(profile.brand_model if profile and profile.brand_model else "—"),
            )
            if _dealer_alert:
                await _dealer_alert(token.dealer_code, text)
            session.add(CabinetEvent(
                user_id=token.user_id, token_id=token.id, event_type="signal_a_sent",
            ))
            sent += 1
        await session.commit()
    return sent


async def otp_worker_tick(now: Optional[datetime] = None) -> dict:
    """Один проход otp_worker; цикл-обёртка живёт в main.py (wiring-only).

    Обязанности изолированы друг от друга: каждая идёт в собственном
    try/except и её падение не отменяет остальные. Раньше исключение ранней
    обязанности (незащищённая сессия БД, сбой _dealer_alert/_tech_alert)
    прерывало тик ДО передачи заявок, а обёртка main.py лишь повторяла тот же
    падающий проход с бэкоффом — передача вставала до ручного вмешательства.
    Упавшие обязанности перечислены в stats["failed"], их значение — None."""
    now = _aware(now) or _utcnow()   # наивный now от вызывающего — к aware UTC
    stats = {"failed": []}

    async def _duty(name: str, coro) -> None:
        try:
            stats[name] = await coro
        except Exception:
            logger.exception("cabinet: обязанность otp_worker '%s' упала — "
                             "остальные обязанности прохода выполняются", name)
            stats[name] = None
            stats["failed"].append(name)

    await _duty("expired_codes", _expire_otp_codes(now))          # (а)
    await _duty("signal_a", _send_signal_a_alerts(now))           # (б)
    try:
        _breaker_prune(time.time())                               # (в) учёт breaker'а
    except Exception:
        logger.exception("cabinet: прунинг breaker'а упал — проход продолжается")
        stats["failed"].append("breaker_prune")
    await _duty("handoffs", _check_handoff_timeouts(now))         # (г) U10: триггеры 2/3
    await _duty("recovered_handoffs", _recover_stuck_handoffs(now))  # (д) U10: зависшие захваты
    await _duty("unclosed_sessions", _recover_unclosed_sessions(now))  # (е) план 2026-08-14-001: добор завершения сессий
    await _duty("stale_valuations", _expire_stale_valuations(now))  # (ж) веб-вход U7: зависшие оценки → 8e
    await _duty("web_entry_cleanup", _cleanup_web_entry_data(now))  # (з) веб-вход R71: хранение данных
    return stats


# =======================
# Обязанности sync_worker (медленная каденция)
# =======================

async def expire_tokens(now: Optional[datetime] = None) -> int:
    """Экспирация токенов — уборка, не механизм контроля доступа (R37)."""
    now = _aware(now) or _utcnow()   # наивный now от вызывающего — к aware UTC
    async with database.AsyncSessionLocal() as session:
        res = await session.execute(
            update(CabinetToken)
            .where(
                CabinetToken.status == "active",
                CabinetToken.expires_at.is_not(None),
                CabinetToken.expires_at <= now,
            )
            .values(status="expired")
        )
        await session.commit()
        return res.rowcount or 0


def _parse_credits(credits: Optional[str]) -> Optional[float]:
    if credits is None:
        return None
    try:
        return float(str(credits).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


async def check_balance() -> None:
    """Проверка баланса и самопроверка канала (R32); 602/627 → алерт
    с исходящим IP и резолвом (R33), порог баланса — CABINET_BALANCE_THRESHOLD."""
    try:
        result = await _gateway().get_balance()
    except Exception as e:
        logger.warning("cabinet: get_balance недоступен: %s", e)
        return

    if result.ok:
        reset_config_alert_dedup()  # [Р12] канал жив — окно дедупа закрыто
        credits = _parse_credits(result.credits)
        threshold = _balance_threshold()
        if credits is not None and credits < threshold and _tech_alert:
            await _tech_alert(
                f"⚠️ Личный кабинет: баланс ProstoSMS {result.credits} ₽ — "
                f"ниже порога {threshold:g} ₽. Пополните счёт, иначе коды не уйдут."
            )
        return

    # [U1 план 2026-09-08-002] Лог любого не-ok — ВСЕГДА, независимо от
    # алерта: раньше при отправленном алерте лог молчал, и два 627 за 09-08
    # не оставили в системе ни строки
    logger.warning("cabinet: проверка баланса не прошла (%s: %s)",
                   result.err_code, result.description)

    # [Р11 поправка владельца 2026-09-08] 602/627 бывают эпизодом маршрутизации
    # (двухканальный выход Dockhost) — один повтор через паузу перед алертом:
    # самоизлечившийся эпизод не будит владельца
    if sms_gateway.needs_config_alert(result.err_code):
        delay = _alert_retry_delay()
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            retry_result = await _gateway().get_balance()
        except Exception as e:
            logger.warning("cabinet: повторная проверка баланса недоступна: %s", e)
            retry_result = result
        if retry_result.ok:
            reset_config_alert_dedup()
            logger.info("cabinet: %s самоизлечился — повторная проверка ok "
                        "(похоже на эпизод маршрутизации)", result.err_code)
            return
        result = retry_result
        logger.warning("cabinet: повтор проверки тоже не прошёл (%s: %s)",
                       result.err_code, result.description)

    await _maybe_config_alert(
        result, require_service=False,
        context="get_balance (плановая проверка баланса, sync_worker)")


async def sync_worker_duties(now: Optional[datetime] = None) -> dict:
    """Вызывается из sync_worker (main.py, wiring-only): экспирация токенов
    и проверка баланса."""
    now = _aware(now) or _utcnow()   # наивный now от вызывающего — к aware UTC
    expired = await expire_tokens(now)
    await check_balance()
    return {"tokens_expired": expired}


# =======================================================================
# U4: Auth API — токены, коды, сессии, лимиты (R1–R6, R13, R19–R22, R27,
# R36–R38). Бизнес-логика здесь; тонкие aiohttp-хендлеры — в cabinet_api.py.
# =======================================================================

TOKEN_TTL_UNBOUND = timedelta(hours=72)      # TTL до привязки
TOKEN_TTL_BOUND = timedelta(days=30)         # скользящий TTL после привязки
OTP_TTL = timedelta(minutes=sms_gateway.OTP_TTL_MINUTES)  # TTL кода — 5 минут
REQUEST_CODE_COOLDOWN = 30                   # с, кулдаун ручных запросов на токен (R6: пауза 30 с)
REQUEST_CODE_HOURLY_LIMIT = 5                # принятых отправок в час на токен
VOICE_PHONE_WINDOW = timedelta(minutes=10)   # R4: один запрос звонка на номер за окно (решение O1)
# Веб-вход (план 2026-09-30-001, U6/R55): не больше 2 звонков за 10 минут и
# на номер, и на сессию (токен). Повтор — не раньше 45 с после предыдущего
# запроса (решение владельца 2026-10-11, план 2026-10-11-001): не все звонки
# доходят, ждать 5 минут клиенты не готовы. Повтор внутри 5 минут ProstoSMS
# молча отправляет SMS вместо звонка — это принято сознательно.
WEB_VOICE_MAX_CALLS = 2
WEB_VOICE_RETRY_AFTER = timedelta(seconds=45)
SMS_LINK_DAILY_LIMIT = 2                     # R5: принятых SMS на ссылку за сутки (решение O2)
SMS_PHONE_DAILY_LIMIT = 5                    # R5: принятых SMS на номер за сутки
VERIFY_MAX_FAILED = 5                        # неверных вводов в окне → блок
VERIFY_WINDOW = timedelta(minutes=30)        # окно счётчика неверных вводов (R6)
VERIFY_LOCK = timedelta(minutes=30)          # длительность блокировки
SESSION_SLIDING = timedelta(minutes=40)      # скользящее окно сессии
SESSION_ABSOLUTE = timedelta(hours=12)       # абсолютный потолок сессии
TOUCH_THRESHOLD = 30                         # с: продление/last_seen пишутся только при сдвиге больше порога
SEND_STATUS_PER_MINUTE = 60                  # опрос send-status: 60/мин на токен


class ApiError(Exception):
    """Ошибка контракта auth-API: HTTP-статус + машинный код + доп. поля ответа.
    cabinet_api.py транслирует её в json_response — единственный формат ошибок."""

    def __init__(self, status: int, error: str, **extra):
        super().__init__(error)
        self.status = status
        self.error = error
        self.extra = extra


def check_secrets() -> None:
    """При ENVIRONMENT=production отсутствие CABINET_HMAC_KEY — фатальная ошибка
    старта (план, Key Technical Decisions); вне прода действует dev-дефолт."""
    if os.getenv("ENVIRONMENT", "").strip().lower() == "production" and not os.getenv("CABINET_HMAC_KEY"):
        raise RuntimeError(
            "CABINET_HMAC_KEY не задан при ENVIRONMENT=production: без ключа HMAC "
            "кодов кабинета каждый рестарт молча инвалидировал бы все коды. "
            "Задайте CABINET_HMAC_KEY в окружении или уберите прод-флаг."
        )


# --- Телефон: нормализация и маска (R21) ---

def normalize_phone(raw) -> Optional[str]:
    """E.164, только мобильные РФ (+79xx). Казахстанские +77xx и любой иной
    формат → None (invalid_phone до вызова шлюза, код 617 не должен возникать)."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    if len(digits) != 10 or not digits.startswith("9"):
        return None
    return "+7" + digits


def mask_phone(phone: Optional[str]) -> Optional[str]:
    """+7•••••••123 — телефон в любых ответах только маской (единый формат
    с последним рубежом логов — log_masking.format_masked_phone)."""
    if not phone:
        return None
    return log_masking.format_masked_phone(re.sub(r"\D", "", str(phone)))


# --- Токен ссылки: хеш, детерминированный вывод, выдача (R19, R4) ---

def _hash_token(token_plain: str) -> str:
    return hashlib.sha256((token_plain or "").encode("utf-8")).hexdigest()


def _derive_token_plain(user_id, dealer_code, deal_id, nonce: str) -> str:
    """Детерминированный вывод токена: HMAC(CABINET_HMAC_KEY, связка+nonce).
    Plaintext в БД не хранится (R4/R16) — «тот же URL» (R19) восстанавливается
    из nonce строки токена и env-ключа. 32 байта → 43 url-safe символа."""
    msg = f"cabinet-token:{user_id}:{dealer_code}:{deal_id or ''}:{nonce}".encode("utf-8")
    digest = hmac.new(_hmac_key(), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _dealer_https_netloc(website) -> Optional[str]:
    """Валидация Website_URL дилера: только https с netloc. Регистр netloc
    сохраняется — потребители решают сами (base_url не лужит lower,
    CORS-whitelist приводит к нижнему регистру)."""
    if not website:
        return None
    parts = urlsplit(str(website).strip())
    if parts.scheme != "https" or not parts.netloc:
        return None
    return parts.netloc


def _cabinet_base_url(website) -> Optional[str]:
    """Website_URL дилера → https://host/<путь кабинета>. Разбор как URL, не
    склейка строк (в поле бывают слеши, путь, query); только https. Путь —
    из env CABINET_PAGE_PATH (дев — /cabinet_dev, R16), по умолчанию /cabinet."""
    netloc = _dealer_https_netloc(website)
    return None if netloc is None else f"https://{netloc}{cabinet_page_path()}"


WEB_URL_MARKER = "src=web"


def _cabinet_url(base_url: str, plain: str, entry_source: Optional[str]) -> str:
    """Адрес кабинета: токен во фрагменте (#t=...). Веб-вход (U8, R50) несёт
    признак ?src=web ДО фрагмента — фронт узнаёт веб-сессию и при мёртвом
    токене (новая вкладка, истёкшая сессия), не нарушая R5: ответ сервера
    на такой токен остаётся неотличимым link_expired."""
    marker = f"?{WEB_URL_MARKER}" if entry_source == "web" else ""
    return f"{base_url}{marker}#t={plain}"


async def issue_cabinet_token(user_id: int, dealer_code: str, deal_id=None,
                              entry_source: str = "chat",
                              unbound_ttl: Optional[timedelta] = None) -> Optional[str]:
    """Выдача ссылки кабинета (R19): активный токен той же связки
    «клиент+дилер+сделка» → тот же URL; другая сделка или дилер → старый
    revoked, новый токен. Отзыв — ТОЛЬКО по этим исключениям (плюс
    перепривязка склейкой в rebind-путях): если URL той же связки
    невоспроизводим текущим CABINET_HMAC_KEY, токен сохраняется, а ссылка
    на этот ход не выдаётся (None + техалерт) — ранее отправленная ссылка
    продолжает работать. Токен во фрагменте (#t=...) — не попадает в логи."""
    now = _utcnow()
    dealers = _dealers_provider() if _dealers_provider else {}
    base_url = _cabinet_base_url((dealers or {}).get(dealer_code, {}).get("website"))
    if base_url is None:
        logger.warning("cabinet: у дилера %s нет валидного https Website_URL — ссылка не выдана",
                       dealer_code)
        # U7: тихая деградация недопустима — техалерт о неверной конфигурации дилера
        if _tech_alert:
            await _tech_alert(
                f"⚠️ Личный кабинет: у дилера {dealer_code} нет валидного https "
                f"Website_URL — ссылка кабинета не выдана (user_id={user_id})."
            )
        return None
    deal_id = str(deal_id) if deal_id is not None else None

    for _ in range(2):  # одна повторная попытка на гонку частичного уникального индекса
        async with database.AsyncSessionLocal() as session:
            active = None
            # Дружелюбная предпроверка (database.ensure_single_active): active-токен
            # уже есть → читаем его строку для правил R19 (тот же URL / revoke)
            if not await database.ensure_single_active(session, user_id):
                active = (await session.execute(
                    select(CabinetToken).where(
                        CabinetToken.user_id == user_id, CabinetToken.status == "active")
                )).scalars().first()
            if active is not None:
                same_bundle = (active.dealer_code == dealer_code
                               and (active.deal_id or None) == deal_id)
                if same_bundle and active.issue_nonce:
                    # Вывод — по сделке МОМЕНТА ВЫДАЧИ (issue_deal_id): deal_id
                    # токена мог быть дозаполнен (U7) или перепривязан склейкой,
                    # но URL обязан остаться прежним. NULL — старая строка,
                    # выводится по deal_id как раньше.
                    issue_deal = (active.issue_deal_id
                                  if active.issue_deal_id is not None else deal_id)
                    plain = _derive_token_plain(user_id, dealer_code, issue_deal,
                                                active.issue_nonce)
                    # Сверка с сохранённым хешем: выведенный из ТЕКУЩЕГО ключа
                    # токен обязан совпасть с token_hash БД, иначе URL был бы
                    # мёртвой ссылкой (вечный link_expired).
                    if _hash_token(plain) == active.token_hash:
                        return _cabinet_url(base_url, plain, active.entry_source)
                if same_bundle:
                    # Токен той же связки жив, но URL невоспроизводим текущим
                    # CABINET_HMAC_KEY (ротация ключа или расхождение ключей
                    # между процессами — инцидент дев-стенда 2026-08-12: скрипт
                    # выдачи со СВОИМ дев-ключом и бот взаимно отзывали токены
                    # друг друга на каждой выдаче). НЕ отзываем: отзыв уносит
                    # bound_phone (TOFU — главная ценность кабинета) и убивает
                    # ссылку из ранних сообщений клиента, ХОТЯ ОНА РАБОТАЕТ —
                    # /auth/init сверяет sha256 предъявленного токена, ключ
                    # HMAC в проверке не участвует. Отзыв — только по
                    # исключениям R19: другая сделка, другой дилер,
                    # перепривязка склейкой. Здесь: токен остаётся, ссылки на
                    # этот ход нет (None), оператору — техалерт.
                    logger.warning(
                        "cabinet: токен user=%s той же связки не выводится из "
                        "текущего CABINET_HMAC_KEY (ротация ключа или разные "
                        "ключи у процессов) — токен сохранён, ссылка на этот "
                        "ход не выдана", user_id)
                    if _tech_alert:
                        await _tech_alert(
                            f"⚠️ Личный кабинет: активный токен user_id={user_id} "
                            f"не выводится из текущего CABINET_HMAC_KEY (ротация "
                            f"ключа или расхождение ключей между процессами). "
                            f"Токен и подтверждённый номер сохранены, новая "
                            f"ссылка не выдаётся — ранее отправленная ссылка "
                            f"продолжает работать."
                        )
                    return None
                active.status = "revoked"  # другая сделка или другой дилер

            nonce = uuid.uuid4().hex
            plain = _derive_token_plain(user_id, dealer_code, deal_id, nonce)
            session.add(CabinetToken(
                user_id=user_id, dealer_code=dealer_code, deal_id=deal_id,
                token_hash=_hash_token(plain), issue_nonce=nonce,
                issue_deal_id=deal_id or "",  # сделка вывода: "" ≡ NULL в _derive_token_plain
                status="active", expires_at=now + (unbound_ttl or TOKEN_TTL_UNBOUND),
                entry_source=entry_source,
            ))
            try:
                await session.commit()
                return _cabinet_url(base_url, plain, entry_source)
            except IntegrityError:
                await session.rollback()  # гонка выдачи — перечитываем active
                continue
    return None


async def get_active_cabinet_url(user_id: int) -> Optional[str]:
    """[План 2026-08-19-001, U1] Read-only URL активного токена — тот же, что
    выдавался клиенту (R19), для подстановки [CABINET_LINK] в момент отправки.

    Ни выдачи, ни записей: КТР-5 плана — URL не стэшится и не персистируется
    (в БД только отпечаток), а запрашивается на каждую отправку с маркером.
    Вывод зеркалит ветку same_bundle issue_cabinet_token: derive из issue_nonce
    по сделке момента выдачи + сверка с token_hash (невоспроизводимый текущим
    CABINET_HMAC_KEY URL не выдаётся — ротация ключа, см. issue_cabinet_token).
    None — активного токена нет, сайт дилера не https, старая строка без
    nonce или несовпадение хеша."""
    async with database.AsyncSessionLocal() as session:
        token = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == user_id,
                                       CabinetToken.status == "active")
        )).scalars().first()
    if token is None or not token.issue_nonce:
        return None
    dealers = _dealers_provider() if _dealers_provider else {}
    base_url = _cabinet_base_url((dealers or {}).get(token.dealer_code, {}).get("website"))
    if base_url is None:
        return None
    issue_deal = (token.issue_deal_id
                  if token.issue_deal_id is not None else token.deal_id)
    plain = _derive_token_plain(user_id, token.dealer_code, issue_deal,
                                token.issue_nonce)
    if _hash_token(plain) != token.token_hash:
        # Ревью 2026-08-19 (learnings, инцидент 2026-08-12): несовпадение хеша =
        # ротация/расхождение CABINET_HMAC_KEY, а не «кабинет не настроен» —
        # warning, чтобы оператор отличил в логах. Без техалерта: та же
        # мисматч-ветка issue_cabinet_token алертит на каждой сборке payload.
        logger.warning(
            "cabinet: URL для подстановки user=%s не выводится из текущего "
            "CABINET_HMAC_KEY (ротация/расхождение ключей) — деградация ссылки",
            user_id)
        return None
    return _cabinet_url(base_url, plain, token.entry_source)


async def _get_live_token(session, token_plain: str, now: datetime) -> CabinetToken:
    """R5: несуществующий, отозванный и истёкший токен неразличимы — один
    идентичный ответ link_expired, одна проверка хеша, БЕЗ записи в БД."""
    row = (await session.execute(
        select(CabinetToken).where(CabinetToken.token_hash == _hash_token(token_plain))
    )).scalar_one_or_none()
    expires_at = _aware(row.expires_at) if row is not None else None
    if (row is None or row.status != "active"
            or (expires_at is not None and expires_at <= now)):
        raise ApiError(410, "link_expired")
    return row


# --- Блокировка неверных вводов (R6): счётчик на токене за окно ---

def _locked_until(token: CabinetToken, now: datetime) -> Optional[datetime]:
    window_start = _aware(token.attempts_window_start)
    if ((token.failed_attempts or 0) >= VERIFY_MAX_FAILED
            and window_start is not None):
        until = window_start + VERIFY_LOCK
        if now < until:
            return until
    return None


def _raise_if_locked(token: CabinetToken, now: datetime) -> None:
    until = _locked_until(token, now)
    if until is not None:
        raise ApiError(423, "locked", retry_after=max(1, int((until - now).total_seconds())))


def _register_failed_attempt(token: CabinetToken, now: datetime) -> None:
    ws = _aware(token.attempts_window_start)
    if ws is None or now - ws > VERIFY_WINDOW:
        token.attempts_window_start = now
        token.failed_attempts = 1
    else:
        token.failed_attempts = (token.failed_attempts or 0) + 1
    if token.failed_attempts >= VERIFY_MAX_FAILED:
        token.attempts_window_start = now  # точка отсчёта 30-минутной блокировки


# --- Профиль в ответах auth (минимум; полный состав — U5) ---

def _profile_payload(profile: Optional[CabinetProfile], token: CabinetToken) -> Optional[dict]:
    if profile is None:
        return {"deal_id": token.deal_id, "brand_model": None,
                "scenario_type": None, "qualification_payment": None}
    payload = {
        "deal_id": profile.deal_id or token.deal_id,
        "brand_model": profile.brand_model,
        "scenario_type": profile.scenario_type,
        "qualification_payment": profile.qualification_payment,
    }
    if token.entry_source == "web":
        # Веб-вход (U8/U9): госномер и пробег с лендинга — предзаполнение
        # полей 8a/8c/8e; адрес возврата — кнопка 8f (истёкшая сессия)
        payload.update(web_plate=profile.web_plate, web_mileage=profile.web_mileage,
                       return_url=profile.return_url)
    return payload


def _dealer_consent_text(info: dict, key: str) -> Optional[str]:
    """Текст юридического документа дилера (Policy/Agreement из NocoDB) для
    поп-апов экрана авторизации. Пустое/пробельное/отсутствующее значение →
    None: фронт рендерит фразу обычным текстом без ссылки (решение В3,
    2026-08-31); поля проверяются независимо друг от друга."""
    value = info.get(key)
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _dealer_payload(dealer_code: str, profile: Optional[CabinetProfile]) -> dict:
    """Ускорение A6 (2026-10-03): тексты Policy/Agreement (≈44 КБ) в auth/init
    не отдаются — только признаки наличия; текст — GET
    /cabinet/api/dealer/{code}/legal/{kind} по открытию поп-апа."""
    dealers = _dealers_provider() if _dealers_provider else {}
    info = (dealers or {}).get(dealer_code, {})
    return {"name": info.get("dealer_name") or "Автосалон",
            "code": dealer_code,
            "brand_model": profile.brand_model if profile else None,
            "policy_available": _dealer_consent_text(info, "policy_text") is not None,
            "agreement_available": _dealer_consent_text(info, "agreement_text") is not None}


LEGAL_KINDS = {"policy": "policy_text", "agreement": "agreement_text"}


def dealer_legal_text(dealer_code: str, kind: str) -> dict:
    """GET /cabinet/api/dealer/{code}/legal/{policy|agreement}: текст документа
    дилера для поп-апа согласия. 422 — неверный код/вид, 503 — справочник не
    загружен, 404 — дилер неизвестен или текста нет (В3)."""
    code = str(dealer_code or "").strip()
    if not re.fullmatch(r"\d{2}", code) or kind not in LEGAL_KINDS:
        raise ApiError(422, "invalid_input", fields={"legal": "invalid"})
    dealers = _dealers_provider() if _dealers_provider else {}
    if not dealers:
        raise ApiError(503, "dealers_unavailable")
    info = dealers.get(code)
    if not isinstance(info, dict):
        raise ApiError(404, "unknown_dealer")
    text = _dealer_consent_text(info, LEGAL_KINDS[kind])
    if text is None:
        raise ApiError(404, "not_found")
    return {"dealer_code": code, "kind": kind, "text": text}


# --- POST /cabinet/api/auth/init ---

async def auth_init(token_plain: str) -> dict:
    now = _utcnow()
    async with database.AsyncSessionLocal() as session:
        token = await _get_live_token(session, token_plain, now)
        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == token.user_id)
        )).scalar_one_or_none()
        dealer = _dealer_payload(token.dealer_code, profile)
        # Веб-вход (U6/R24): источник сессии, адрес возврата (8f) и живая
        # отправка кода — экран звонка восстанавливается после F5 у всех
        extra = {"entry_source": token.entry_source or "chat",
                 "call": await _web_call_state(session, token, now)}
        if token.entry_source == "web":
            extra["return_url"] = profile.return_url if profile is not None else None

        if token.bound_phone:
            return {"state": "bound", "masked_phone": mask_phone(token.bound_phone),
                    "dealer": dealer, **extra}

        payload = {"state": "need_phone", "dealer": dealer, **extra}
        user = await session.get(User, token.user_id)
        known = normalize_phone(user.phone) if user and user.phone else None
        if known:  # R27: известный из чата номер — маска + use_known
            payload["known_phone_masked"] = mask_phone(known)
            payload["use_known"] = True
        return payload


# --- POST /cabinet/api/auth/request-code ---

def _generate_code() -> str:
    return f"{secrets.randbelow(10000):04d}"


def _dealer_daily_cap() -> int:
    return int(os.getenv("CABINET_DEALER_DAILY_CAP", "100"))


# Дедуп техалерта об исчерпанном суточном капе дилера (образец — _alert_dedup
# в otp_worker main.py): dealer_code -> ts последнего алерта. Без него КАЖДЫЙ
# запрос кода после исчерпания капа шлёт алерт, и любой держатель ссылки
# заливает админа. 429 клиенту отдаётся всегда, дедуплицируется только алерт.
_dealer_cap_alerts: dict = {}
DEALER_CAP_ALERT_WINDOW = 3600.0  # с: не чаще раза в час на дилера


def reset_dealer_cap_alerts() -> None:
    """Для тестов."""
    _dealer_cap_alerts.clear()


def _dealer_cap_alert_due(dealer_code: str, now_ts: float) -> bool:
    """True — алерт по этому дилеру пора слать (и окно сдвигается)."""
    if now_ts - _dealer_cap_alerts.get(dealer_code, 0.0) <= DEALER_CAP_ALERT_WINDOW:
        return False
    _dealer_cap_alerts[dealer_code] = now_ts
    # Уборка: дилеры, чьё окно давно закрыто, из словаря убираются
    for code in [c for c, ts in _dealer_cap_alerts.items()
                 if c != dealer_code and now_ts - ts > DEALER_CAP_ALERT_WINDOW]:
        del _dealer_cap_alerts[code]
    return True


async def _enforce_send_limits(session, token: CabinetToken, phone: str,
                               channel: str, now: datetime) -> None:
    """R11 + R4/R5: SMS-лимиты и кап дилера считают только принятые отправки
    (sent_at IS NOT NULL); голосовой лимит — ВСЕ запросы звонка на номер,
    включая непринятые (решение O1: по ответу поддержки провайдера непринятый
    запрос тоже взводит 5-минутный триггер подмены звонка на SMS).
    Порядок: кулдаун → канальные лимиты → 5/час на токен → кап дилера; первый
    сработавший определяет ответ. Каждый 429, кроме кулдауна, несёт честный
    retry_after (остаток окна от старейшей учтённой записи) и limit_source
    (phone | link | dealer) — фронт различает формулировки."""
    day_window = timedelta(hours=24)
    hour_ago = now - timedelta(hours=1)
    # Кулдаун и 5/час смотрят одно и то же окно (30 с ⊂ 1 ч) — одна выборка
    # на оба: свежее принятое старше часа кулдауну уже не мешает.
    sent_hour = list((await session.execute(
        select(CabinetOtp.sent_at).where(
            CabinetOtp.token_id == token.id, CabinetOtp.sent_at > hour_ago)
    )).scalars())
    if sent_hour:
        elapsed = (now - _aware(max(sent_hour))).total_seconds()
        if elapsed < REQUEST_CODE_COOLDOWN:
            raise ApiError(429, "rate_limited",
                           retry_after=max(1, int(REQUEST_CODE_COOLDOWN - elapsed)))

    if channel == "voice" and token.entry_source == "web":
        await _enforce_web_voice_window(session, token, phone, now)
    elif channel == "voice":
        # R4: created-колонки нет — момент запроса восстанавливается как
        # expires_at − OTP_TTL (expires_at ставится при создании записи).
        horizon = now - VOICE_PHONE_WINDOW + OTP_TTL
        voice_expirations = list((await session.execute(
            select(CabinetOtp.expires_at).where(
                CabinetOtp.phone == phone, CabinetOtp.channel == "voice",
                CabinetOtp.expires_at > horizon)
        )).scalars())
        if voice_expirations:
            oldest_created = _aware(min(voice_expirations)) - OTP_TTL
            raise ApiError(429, "rate_limited",
                           retry_after=_retry_after(oldest_created, VOICE_PHONE_WINDOW, now),
                           limit_source="phone")
    else:
        day_ago = now - day_window
        link_sms = list((await session.execute(
            select(CabinetOtp.sent_at).where(
                CabinetOtp.token_id == token.id, CabinetOtp.channel == "sms",
                CabinetOtp.sent_at > day_ago)
        )).scalars())
        if len(link_sms) >= SMS_LINK_DAILY_LIMIT:
            raise ApiError(429, "rate_limited",
                           retry_after=_retry_after(_aware(min(link_sms)), day_window, now),
                           limit_source="link")
        phone_sms = list((await session.execute(
            select(CabinetOtp.sent_at).where(
                CabinetOtp.phone == phone, CabinetOtp.channel == "sms",
                CabinetOtp.sent_at > day_ago)
        )).scalars())
        if len(phone_sms) >= SMS_PHONE_DAILY_LIMIT:
            raise ApiError(429, "rate_limited",
                           retry_after=_retry_after(_aware(min(phone_sms)), day_window, now),
                           limit_source="phone")

    if len(sent_hour) >= REQUEST_CODE_HOURLY_LIMIT:
        raise ApiError(429, "rate_limited",
                       retry_after=_retry_after(_aware(min(sent_hour)), timedelta(hours=1), now),
                       limit_source="link")

    day_ago = now - day_window
    dealer_count, dealer_oldest = (await session.execute(
        select(func.count(CabinetOtp.id), func.min(CabinetOtp.sent_at))
        .join(CabinetToken, CabinetToken.id == CabinetOtp.token_id)
        .where(CabinetToken.dealer_code == token.dealer_code, CabinetOtp.sent_at > day_ago)
    )).one()
    if dealer_count >= _dealer_daily_cap():
        if _tech_alert and _dealer_cap_alert_due(token.dealer_code, time.time()):
            await _tech_alert(
                f"⚠️ Личный кабинет: суточный кап отправок дилера {token.dealer_code} "
                f"({_dealer_daily_cap()}) исчерпан — отправки остановлены до спада окна."
            )
        # dealer_oldest пуст при капе 0 (kill-switch конфигом) — окно целиком
        retry = (_retry_after(_aware(dealer_oldest), day_window, now)
                 if dealer_oldest is not None else int(day_window.total_seconds()))
        raise ApiError(429, "rate_limited", retry_after=retry, limit_source="dealer")


async def _web_voice_window(session, token: CabinetToken, phone: str,
                            now: datetime) -> dict:
    """R55: окно звонков веб-сессии. Считаются ВСЕ запросы звонка (как R4)
    на этот номер ИЛИ этот токен за 10 минут: смена номера не обходит лимит
    сессии. Момент запроса — expires_at − OTP_TTL (created-колонки нет).

    wait — секунд до момента, когда следующий звонок будет разрешён (0 —
    уже можно): максимум из «последний запрос + окно повтора» и, если в окне
    уже WEB_VOICE_MAX_CALLS запросов, «выход старейшего учтённого из окна».
    Один расчёт и для 429, и для времени на экране (план 2026-10-11-001, В4):
    обещанное клиенту время совпадает с моментом, когда 429 уходит."""
    horizon = now - VOICE_PHONE_WINDOW + OTP_TTL
    rows = list((await session.execute(
        select(CabinetOtp.expires_at, CabinetOtp.token_id).where(
            CabinetOtp.channel == "voice", CabinetOtp.expires_at > horizon,
            (CabinetOtp.phone == phone) | (CabinetOtp.token_id == token.id))
    )).all())
    state = {"used": len(rows), "wait": 0, "limit_source": None}
    if not rows:
        return state
    created = sorted(_aware(r.expires_at) - OTP_TTL for r in rows)
    recall_left = (created[-1] + WEB_VOICE_RETRY_AFTER - now).total_seconds()
    if recall_left > 0:
        state["wait"] = max(1, int(recall_left))
        state["limit_source"] = "phone"
    if len(rows) >= WEB_VOICE_MAX_CALLS:
        by_token = sum(1 for r in rows if r.token_id == token.id)
        window_wait = _retry_after(created[-WEB_VOICE_MAX_CALLS], VOICE_PHONE_WINDOW, now)
        if window_wait >= state["wait"]:
            state["wait"] = window_wait
            state["limit_source"] = "link" if by_token >= WEB_VOICE_MAX_CALLS else "phone"
    return state


async def _enforce_web_voice_window(session, token: CabinetToken, phone: str,
                                    now: datetime) -> None:
    window = await _web_voice_window(session, token, phone, now)
    if window["wait"] > 0:
        raise ApiError(429, "rate_limited", retry_after=window["wait"],
                       limit_source=window["limit_source"])


async def _web_call_state(session, token: CabinetToken, now: datetime) -> Optional[dict]:
    """Живая отправка для восстановления экрана звонка после F5 (R24):
    последняя запись OTP токена в sending/sent с живым кодом — маска номера,
    канал, момент следующего звонка и остаток звонков (веб-сессия)."""
    web = token.entry_source == "web"
    # Веб-вход (8h-2б): экран звонка живёт и после истечения кода — в пределах
    # окна звонков (10 мин): ячейки, подсказка, повторный звонок
    statuses = ("sending", "sent", "expired") if web else ("sending", "sent")
    otp = (await session.execute(
        select(CabinetOtp).where(CabinetOtp.token_id == token.id,
                                 CabinetOtp.status.in_(statuses))
        .order_by(CabinetOtp.id.desc()).limit(1)
    )).scalars().first()
    if otp is None or otp.expires_at is None:
        return None
    created = _aware(otp.expires_at) - OTP_TTL
    code_expired = _aware(otp.expires_at) <= now
    if code_expired and (not web or created + VOICE_PHONE_WINDOW <= now):
        return None
    state = {"masked_phone": mask_phone(otp.phone), "channel": otp.channel,
             "code_expires_at": _aware(otp.expires_at).isoformat()}
    if web:
        state["code_expired"] = code_expired
        state.update(_web_call_fields(await _web_voice_window(session, token, otp.phone, now), now))
    else:
        state["next_call_at"] = (created + timedelta(seconds=REQUEST_CODE_COOLDOWN)).isoformat()
    return state


def _web_call_fields(window: dict, now: datetime) -> dict:
    """Поля звонка веб-сессии для фронта: остаток звонков в окне, момент
    следующего разрешённого звонка (всегда, и при calls_left = 0 — В4) и
    признак повтора recall (в окне уже ≥ 2 запросов — тексты «звонком или в
    SMS», В1/В3)."""
    return {"calls_left": max(0, WEB_VOICE_MAX_CALLS - window["used"]),
            "next_call_at": (now + timedelta(seconds=window["wait"])).isoformat(),
            "recall": window["used"] >= 2}


async def request_code(token_plain: str, phone: Optional[str] = None,
                       channel: str = "voice", consent: Optional[dict] = None,
                       use_known: bool = False, ip: Optional[str] = None,
                       user_agent: Optional[str] = None) -> dict:
    """Порядок проверок: токен → блок → номер (нормализация/TOFU) → согласие
    (R13, INSERT только при отсутствии) → лимиты (R11) → breaker → создание
    CabinetOtp в 'sending' ДО отправки (частичный индекс закрывает дубль)."""
    now = _utcnow()
    channel = channel if channel in ("voice", "sms") else "voice"

    async with database.AsyncSessionLocal() as session:
        token = await _get_live_token(session, token_plain, now)
        _raise_if_locked(token, now)  # в блоке отклоняется тем же locked (AE2)
        if token.entry_source == "web" and channel == "sms":
            # R23: в веб-сессии только звонок — до лимитов и до отправки
            raise ApiError(422, "channel_unavailable")

        # --- Номер: нормализация и TOFU (R2, R21; AE1) ---
        if token.bound_phone:
            if phone is not None:
                norm = normalize_phone(phone)
                if norm is None:
                    raise ApiError(422, "invalid_phone")
                if norm != token.bound_phone:
                    raise ApiError(409, "phone_mismatch")
            target_phone = token.bound_phone
        else:
            if use_known:
                user = await session.get(User, token.user_id)
                target_phone = normalize_phone(user.phone) if user and user.phone else None
            elif token.entry_source == "web" and (phone is None or not str(phone).strip()):
                # Повтор после F5: фронту доступна только маска, номер не
                # приходит — берём номер последнего запроса кода этой сессии
                # (решение владельца 2026-10-12); нет запросов — 422 ниже
                target_phone = (await session.execute(
                    select(CabinetOtp.phone).where(CabinetOtp.token_id == token.id)
                    .order_by(CabinetOtp.id.desc()).limit(1)
                )).scalar_one_or_none()
            else:
                target_phone = normalize_phone(phone)
            if target_phone is None:
                raise ApiError(422, "invalid_phone")

        # --- Согласие до первой отправки (R13): INSERT только при отсутствии ---
        has_consent = (await session.execute(
            select(Consent.id).where(Consent.token_id == token.id).limit(1)
        )).scalar_one_or_none() is not None
        if not has_consent:
            if not (isinstance(consent, dict) and consent.get("accepted")):
                raise ApiError(422, "consent_required")
            version = str(consent.get("version") or "v1")
            text_hash = str(consent.get("text_hash") or
                            hashlib.sha256(version.encode("utf-8")).hexdigest())
            session.add(Consent(user_id=token.user_id, token_id=token.id,
                                consent_version=version, consent_text_hash=text_hash,
                                ip=ip, user_agent=user_agent))
            await session.commit()  # согласие — факт, откату лимитами не подлежит

        # --- Лимиты (R11, R4/R5) и пауза breaker'а ---
        await _enforce_send_limits(session, token, target_phone, channel, now)
        if not breaker_allows():
            return {"status": "retry_free"}  # пауза breaker: попытка не засчитана

        # --- Ручной повтор: прежняя sent/sending → superseded (R22/R30, AE8/AE13) ---
        await session.execute(
            update(CabinetOtp)
            .where(CabinetOtp.token_id == token.id,
                   CabinetOtp.status.in_(("sending", "sent")))
            .values(status="superseded")
        )

        code = _generate_code()
        otp = CabinetOtp(
            token_id=token.id, phone=target_phone,
            code_hmac=compute_code_hmac(code), external_id=str(uuid.uuid4()),
            channel=channel, status="sending", expires_at=now + OTP_TTL,
        )
        session.add(otp)
        try:
            await session.commit()  # строка в 'sending' ДО отправки
        except IntegrityError as e:
            await session.rollback()
            _breaker_release_probe()
            # 429 — только настоящая гонка (дубль закрыл частичный индекс).
            # Прочие IntegrityError — дефект схемы (напр., легаси-колонка
            # NOT NULL): маскировать их под rate_limited значит хоронить
            # поломку каждой отправки под вечным «попробуйте позже».
            if "uq_cabinet_otp_inflight_token" not in str(e.orig):
                raise
            raise ApiError(429, "rate_limited", retry_after=REQUEST_CODE_COOLDOWN)

        result = await _gateway().send_otp(target_phone, code, channel, otp.external_id)

        if result.ok:  # err_code=0 — единственный зачёт попытки (R11)
            breaker_record_success()
            otp.status = "sent"
            otp.sent_at = now
            if channel == "voice":
                otp.voice_msg_id = result.provider_msg_id
            else:
                otp.sms_msg_id = result.provider_msg_id
            session.add(CabinetEvent(user_id=token.user_id, token_id=token.id,
                                     event_type="code_sent", ip=ip))
            await session.commit()
            if token.entry_source == "web":
                # U8/R55 + В4: retry_after — секунд до следующего разрешённого
                # звонка (45 с; при calls_left = 0 — до выхода из окна 10 мин);
                # recall — повтор: тексты «звонком или в SMS»
                window = await _web_voice_window(session, token, target_phone, now)
                fields = _web_call_fields(window, now)
                return {"status": "ok", "retry_after": window["wait"],
                        "calls_left": fields["calls_left"], "recall": fields["recall"]}
            return {"status": "ok", "retry_after": REQUEST_CODE_COOLDOWN}

        breaker_record_failure(result.error_class)
        otp.status = "failed_send"
        session.add(CabinetEvent(
            user_id=token.user_id, token_id=token.id, event_type="send_failed",
            meta_json=json.dumps({"error_class": result.error_class, "retry_free": True,
                                  "err_code": result.err_code}),
        ))
        await session.commit()

    await _maybe_config_alert(
        result, context=f"send_otp (канал {channel}) → {mask_phone(target_phone)}")
    return {"status": "retry_free"}  # неуспех бесплатен для клиента (R11)


# --- GET /cabinet/api/auth/send-status (лимит 60/мин на токен, в памяти) ---

_send_status_hits: dict = {}  # token_hash -> [ts, ...] за последнюю минуту


def reset_send_status_limiter() -> None:
    """Для тестов."""
    _send_status_hits.clear()


def _send_status_rate_ok(token_hash: str, now_ts: float) -> bool:
    hits = [ts for ts in _send_status_hits.get(token_hash, []) if now_ts - ts < 60]
    hits.append(now_ts)
    _send_status_hits[token_hash] = hits
    # Уборка: ключи, чей список таймстампов опустел (все хиты старше минуты),
    # удаляются — словарь не растёт бесконечно по отработавшим токенам.
    stale = [k for k, v in _send_status_hits.items()
             if k != token_hash and (not v or now_ts - v[-1] >= 60)]
    for k in stale:
        del _send_status_hits[k]
    return len(hits) <= SEND_STATUS_PER_MINUTE


async def send_status(token_plain: str) -> dict:
    now = _utcnow()
    async with database.AsyncSessionLocal() as session:
        token = await _get_live_token(session, token_plain, now)
        if not _send_status_rate_ok(token.token_hash, time.time()):
            raise ApiError(429, "rate_limited", retry_after=60)
        otp = (await session.execute(
            select(CabinetOtp).where(CabinetOtp.token_id == token.id)
            .order_by(CabinetOtp.id.desc()).limit(1)
        )).scalars().first()

    if otp is None:
        return {"state": "retry_free" if breaker_state() != "closed" else "none"}
    if otp.status == "sending":
        state = "sending"
    elif otp.status == "sent":
        # delivery_state НЕ влияет: статусы провайдера недостоверны (решение O3),
        # клиент действует сам — после отсчёта доступна ссылка на SMS.
        state = "sent"
    elif otp.status == "failed_send":
        state = "retry_free"  # конвенция U3: failed_send → наружу retry_free
    elif otp.status in ("verified", "superseded"):
        state = "sent"
    else:
        state = "expired"
    return {"state": state}


# --- POST /cabinet/api/auth/verify ---

async def verify_code(token_plain: str, code, ip: Optional[str] = None,
                      user_agent: Optional[str] = None) -> dict:
    now = _utcnow()
    async with database.AsyncSessionLocal() as session:
        token = await _get_live_token(session, token_plain, now)
        _raise_if_locked(token, now)

        otp = (await session.execute(
            select(CabinetOtp).where(
                CabinetOtp.token_id == token.id, CabinetOtp.status == "sent")
            .order_by(CabinetOtp.id.desc()).limit(1)
        )).scalars().first()
        provided_hmac = compute_code_hmac(str(code or "").strip())

        otp_expires_at = _aware(otp.expires_at) if otp is not None else None
        if (otp is not None and otp_expires_at is not None and otp_expires_at > now
                and hmac.compare_digest(provided_hmac, otp.code_hmac)):
            # --- Успех: TOFU-привязка, продление токена, сессия (R2, R37) ---
            otp.status = "verified"
            if token.bound_at is None:
                token.bound_at = now
                token.bound_phone = otp.phone
            token.last_login_at = now
            token.expires_at = now + TOKEN_TTL_BOUND  # 30 дн скользящих после привязки
            token.failed_attempts = 0
            token.attempts_window_start = None

            bearer = secrets.token_urlsafe(32)
            session.add(CabinetSession(
                token_id=token.id, user_id=token.user_id,
                session_hash=hashlib.sha256(bearer.encode("utf-8")).hexdigest(),
                created_at=now, expires_at=now + SESSION_SLIDING,
                ip=ip, user_agent=user_agent,
            ))
            session.add(CabinetEvent(user_id=token.user_id, token_id=token.id,
                                     event_type="verified", ip=ip))
            profile = (await session.execute(
                select(CabinetProfile).where(CabinetProfile.user_id == token.user_id)
            )).scalar_one_or_none()
            await session.commit()

            # U7 (R7/R8): пост-верификационный хук — телефон в User.phone
            # (источник «кабинет»), алерт дилеру, расхождение с номером из
            # чата. Сбой хука не роняет верификацию.
            try:
                await _post_verify_hook(token.user_id, token.id, token.deal_id,
                                        token.dealer_code, otp.phone)
            except Exception:
                logger.exception("cabinet: пост-верификационный хук упал (user=%s)",
                                 token.user_id)
            # Веб-вход U7 (R26): оценка трейд-ина — только после авторизации
            try:
                await start_valuation_after_verify(token.id)
            except Exception:
                logger.exception("cabinet: запуск оценки после входа упал (user=%s)",
                                 token.user_id)

            return {"access_token": bearer,
                    "expires_in": int(SESSION_SLIDING.total_seconds()),
                    "profile": _profile_payload(profile, token)}

        # --- Вежливость к мёртвым кодам (R8, решение O8): отпечаток совпал с
        # погашенной/использованной записью → code_superseded, с истёкшей →
        # code_expired; счётчик попыток не тратится, сессия не создаётся.
        # Оракул принят владельцем: мёртвый код ценности не имеет. Блокировку
        # это не обходит — _raise_if_locked уже отработал выше. Окно поиска —
        # сутки (expires_at = создание + TTL, created-колонки у записи нет).
        dead_rows = (await session.execute(
            select(CabinetOtp).where(
                CabinetOtp.token_id == token.id,
                CabinetOtp.status.in_(("superseded", "verified", "expired", "sent")),
                CabinetOtp.expires_at > now - timedelta(days=1),
            ).order_by(CabinetOtp.id.desc())
        )).scalars().all()
        for dead in dead_rows:
            if dead.status == "sent":
                dead_expires = _aware(dead.expires_at)
                if dead_expires is None or dead_expires > now:
                    continue  # живой код уже сверен выше — он не мёртвый
            if hmac.compare_digest(provided_hmac, dead.code_hmac):
                if dead.status in ("superseded", "verified"):
                    raise ApiError(400, "code_superseded")
                raise ApiError(400, "code_expired")

        # --- Неуспех: несовпадение ни с одной записью — invalid_code;
        # попытка инкрементирует счётчик (R6) ---
        _register_failed_attempt(token, now)
        session.add(CabinetEvent(user_id=token.user_id, token_id=token.id,
                                 event_type="verify_failed", ip=ip))
        locked_now = token.failed_attempts >= VERIFY_MAX_FAILED
        attempts_left = max(0, VERIFY_MAX_FAILED - token.failed_attempts)
        await session.commit()
        if locked_now:
            raise ApiError(423, "locked", retry_after=int(VERIFY_LOCK.total_seconds()))
        raise ApiError(400, "invalid_code", attempts_left=attempts_left)


# --- Валидация Bearer (общая для /me и всех auth-вызовов кабинета; R37) ---

async def validate_bearer(authorization: Optional[str],
                          now: Optional[datetime] = None) -> dict:
    """Сессия жива по обоим окнам (скользящее 40 мин, абсолют 12 ч); токен —
    active И expires_at > now ПО ВРЕМЕНИ (R37: воркер — уборка, не контроль).
    Успех продлевает скользящее окно и обновляет last_seen_at токена —
    единый сигнал жизни для триггеров передачи заявки (U10)."""
    now = _aware(now) or _utcnow()   # наивный now от вызывающего — к aware UTC
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiError(401, "unauthorized")
    bearer = authorization[len("Bearer "):].strip()
    if not bearer:
        raise ApiError(401, "unauthorized")
    session_hash = hashlib.sha256(bearer.encode("utf-8")).hexdigest()

    async with database.AsyncSessionLocal() as session:
        sess = (await session.execute(
            select(CabinetSession).where(CabinetSession.session_hash == session_hash)
        )).scalar_one_or_none()
        if sess is None:
            raise ApiError(401, "unauthorized")
        created = _aware(sess.created_at) or now
        sess_expires_at = _aware(sess.expires_at)
        if sess_expires_at is None or now >= sess_expires_at:
            raise ApiError(401, "unauthorized")
        if now >= created + SESSION_ABSOLUTE:
            raise ApiError(401, "unauthorized")

        token = await session.get(CabinetToken, sess.token_id)
        if token is None or token.status != "active":
            raise ApiError(401, "unauthorized")
        token_expires_at = _aware(token.expires_at)
        if token_expires_at is not None and token_expires_at <= now:
            raise ApiError(401, "unauthorized")  # R37: по времени, не по статусу

        # Продление окна и сигнал жизни пишутся только при сдвиге больше
        # TOUCH_THRESHOLD с — частые пинги /me не порождают поток UPDATE'ов.
        new_expires = min(now + SESSION_SLIDING, created + SESSION_ABSOLUTE)
        last_seen_at = _aware(token.last_seen_at)
        touched = False
        if abs((new_expires - sess_expires_at).total_seconds()) > TOUCH_THRESHOLD:
            sess.expires_at = new_expires
            touched = True
        if (last_seen_at is None
                or abs((now - last_seen_at).total_seconds()) > TOUCH_THRESHOLD):
            token.last_seen_at = now
            touched = True
        if touched:
            await session.commit()
        return {"user_id": token.user_id, "token_id": token.id, "session_id": sess.id}


# GET /cabinet/api/me реализован в U5 (get_me ниже) — тонкого алиаса me()
# больше нет: cabinet_api зовёт get_me напрямую, других потребителей не было.


# --- CORS-whitelist для /cabinet/api/* (origins из Website_URL дилеров) ---

def _dealer_origins() -> set:
    origins = set()
    dealers = _dealers_provider() if _dealers_provider else {}
    for info in (dealers or {}).values():
        netloc = _dealer_https_netloc(info.get("website"))
        if netloc:
            origins.add(f"https://{netloc.lower()}")
    return origins


# --- GET /cabinet/api/dealer/{dealer_code} (U13a): настройки дилера для лендинга ---

DEALER_SETTINGS_CACHE_CONTROL = "public, max-age=300"


def _non_empty(value) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


# 3.4.1: колонка активности каталога — настройка дилера Dealers.Catalog_Active_Field
# (решение владельца 2026-10-06): только «Active» или «Active_<число>», пусто — «Active»
CATALOG_ACTIVE_FIELD_DEFAULT = "Active"
_CATALOG_ACTIVE_FIELD_RE = re.compile(r"Active(_\d+)?")


def parse_catalog_active_field(raw):
    """(колонка, неверное_значение | None): пусто — умолчание; не «Active» /
    «Active_<число>» — умолчание + исходное значение (для техалерта)."""
    value = str(raw).strip() if raw is not None else ""
    if not value:
        return CATALOG_ACTIVE_FIELD_DEFAULT, None
    if _CATALOG_ACTIVE_FIELD_RE.fullmatch(value):
        return value, None
    return CATALOG_ACTIVE_FIELD_DEFAULT, value


def dealer_settings(dealer_code: str) -> dict:
    """Ответ из кэша дилеров бота (без NocoDB): карточка (7 публичных полей
    /catalog), id таблиц catalog/features/rates, значения скрытых полей формы
    Tilda. Пустое значение — ключ не отдаётся. Ошибки: 422 invalid_input,
    503 dealers_unavailable (справочник не загружен), 404 unknown_dealer."""
    code = str(dealer_code or "").strip()
    if not re.fullmatch(r"\d{2}", code):
        raise ApiError(422, "invalid_input", fields={"dealer_code": "invalid"})
    dealers = _dealers_provider() if _dealers_provider else {}
    if not dealers:
        raise ApiError(503, "dealers_unavailable")
    info = dealers.get(code)
    landing = (info or {}).get("landing") if isinstance(info, dict) else None
    if not isinstance(landing, dict):
        raise ApiError(404, "unknown_dealer")
    card = {k: v for k, v in (landing.get("card") or {}).items() if _non_empty(v)}
    tables = {name: {"table_id": str(tid).strip()}
              for name, tid in (landing.get("tables") or {}).items()
              if name in ("catalog", "features", "rates") and _non_empty(tid)}
    if "catalog" in tables:
        # 3.4.1: колонка активности каталога дилера (Catalog_Active_Field, по
        # умолчанию «Active») — лендинг показывает позиции, где она взведена
        tables["catalog"]["active_field"] = (
            str(landing.get("tables", {}).get("catalog_active_field") or "").strip()
            or CATALOG_ACTIVE_FIELD_DEFAULT)
    form_values = {k: v for k, v in (landing.get("form_values") or {}).items()
                   if k in ("brand", "dealer", "website", "letter_title", "form_type")
                   and _non_empty(v)}
    out = {"dealer_code": code, "dealer": card, "tables": tables, "form_values": form_values}
    assets = cabinet_build_assets()
    if assets:
        out["cabinet_assets"] = assets
    # Ускорение A4: адрес страницы кабинета ровно как начало cabinet_url из
    # web/open (до #t=) — браузер переиспользует prefetch только при точном совпадении
    base_url = _cabinet_base_url(info.get("website"))
    if base_url:
        out["cabinet_page_url"] = f"{base_url}?{WEB_URL_MARKER}"
    return out


CABINET_BUILD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "widget-custom", "cabinet-app", "Production")


def cabinet_build_assets() -> list:
    """Ускорение A4: пути файлов сборки кабинета с версией (тот же ?v=, что в
    строках <script> страниц /cabinet и /cabinet_dev) — лендинг делает по ним
    prefetch на шаге 2. Порядок — порядок подключения. Сборки нет — []."""
    try:
        with open(os.path.join(CABINET_BUILD_DIR, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh) or {}
    except (OSError, ValueError):
        return []
    files = manifest.get("files") or {}
    return [f"/widget-custom/cabinet-app/Production/{name}?v={files[name]['version']}"
            for name in manifest.get("order") or [] if files.get(name, {}).get("version")]


def cabinet_cors_headers(origin: Optional[str]) -> dict:
    """Echo Origin из whitelist дилеров + Vary: Origin; чужой origin —
    пустой dict (ответ БЕЗ CORS-заголовков). Используется main.py в обеих
    middleware-ветках, включая 500-путь error_middleware.

    X-Cabinet-Token обязателен в Allow-Headers: опрос GET
    /cabinet/api/auth/send-status шлёт токен кастомным заголовком (не в
    query — чтобы не попасть в access-лог), а кастомный заголовок делает
    запрос непростым и включает браузерный preflight. Без него кросс-доменный
    опрос статуса доставки с сайта дилера не работает вовсе."""
    if not origin:
        return {}
    if str(origin).strip().rstrip("/").lower() not in _dealer_origins():
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Vary": "Origin",
        # ngrok-skip-browser-warning — обход interstitial дев-туннеля ngrok
        # (app.js шлёт его всегда; вне ngrok безвреден). Урок CORS: каждый
        # заголовок фронта обязан быть в whitelist, иначе preflight его срежет.
        "Access-Control-Allow-Headers":
            "Authorization, Content-Type, X-Cabinet-Token, ngrok-skip-browser-warning",
        "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
        "Access-Control-Max-Age": "86400",
    }


# --- R38: пустой справочник дилеров — техалерт и повторные попытки ---

async def ensure_dealers_loaded(reload_dealers, interval: float = 60.0,
                                max_attempts: Optional[int] = None) -> bool:
    """Пустой CACHED_DEALERS при старте обесточивает кабинет (CORS-whitelist
    и выдача ссылок) — тихая деградация недопустима (R38): техалерт один раз,
    затем повторные загрузки до успеха. Вызывается фоновой задачей из main.py."""
    dealers = _dealers_provider() if _dealers_provider else None
    if dealers:
        return True
    logger.error("cabinet: справочник дилеров пуст при старте — кабинет обесточен (R38)")
    if _tech_alert:
        await _tech_alert(
            "🚨 Личный кабинет: справочник дилеров пуст при старте — CORS-whitelist "
            "и выдача ссылок кабинета не работают. Повторяю загрузку из NocoDB (R38)."
        )
    attempt = 0
    while max_attempts is None or attempt < max_attempts:
        attempt += 1
        await asyncio.sleep(interval)
        try:
            await reload_dealers()
        except Exception as e:
            logger.warning("cabinet: повторная загрузка дилеров не удалась: %s", e)
            continue
        if _dealers_provider and _dealers_provider():
            logger.info("cabinet: справочник дилеров загружен с попытки %d", attempt)
            return True
    return False


# =======================================================================
# U5: Анкета — черновики, сабмит, UF-маппинг (R24, R25, R41; R34 —
# потребитель карты в U7). Черновик автосохраняется ТОЛЬКО в локальную БД;
# в Bitrix данные уходят один раз — по явному «Отправить» (submitted_at).
# Read-only наступает только от submitted_at (Key Technical Decisions).
# =======================================================================

FORM_INT_FIELDS = frozenset({
    "credit_initial", "credit_term", "credit_payment",
    "tradein_year", "tradein_mileage", "combo_extra_down",
})
FORM_STR_FIELDS = frozenset({
    "marital_status", "education", "employment",
    "tradein_make", "tradein_model", "tradein_condition",
    "owners_by_pts", "pts_type", "keys_count",
    "client_comment", "tradein_plate",
    "tradein_steering",   # веб-вход U9: плитки «Руль» (8c/8e)
})
# Порядок — для детерминированных payload'ов и комментария таймлайна.
# Состав — по дизайну (план 2026-08-06, R1): выведенные поля (income,
# work_experience, family_composition, tradein_damage, tradein_engine,
# tradein_vin, tradein_owners) удалены из констант, колонки в БД остаются.
FORM_FIELDS = (
    "credit_initial", "credit_term", "credit_payment",
    "marital_status", "education", "employment",
    "tradein_make", "tradein_model", "tradein_year", "tradein_mileage",
    "tradein_condition", "owners_by_pts", "pts_type", "keys_count",
    "client_comment", "tradein_plate", "combo_extra_down",
    "tradein_steering",
)

# ---------------------------------------------------------------
# Канонические словари плиток (R2): значения — ДОСЛОВНО из макета
# docs/design/lk-source.html; клиент видит их на плитках, и ровно эти
# строки уходят в UF (не коды и не ID). app.js рендерит те же строки —
# единство пинится стражем (U8).
# ---------------------------------------------------------------

TILE_MARITAL_STATUS = (
    "Холост/не замужем", "Женат/замужем", "В разводе",
    "Вдовец/вдова", "Гражданский брак",
)
TILE_EDUCATION = (
    "Среднее", "Среднее специальное", "Незаконченное высшее",
    "Высшее", "2 и более высших", "Учёная степень",
)
TILE_EMPLOYMENT = (
    "Работа по найму", "Служба", "Декретный отпуск", "Работающий пенсионер",
    "Пенсионер", "Частичная занятость", "ИП",
)
TILE_TRADEIN_CONDITION = (
    "Отличное", "Хорошее", "Удовлетворительное", "Требует ремонта", "Не на ходу",
)
TILE_OWNERS_BY_PTS = ("1", "2", "3", "4 и более")
TILE_PTS_TYPE = ("Оригинал", "Дубликат", "Электронный ПТС")
TILE_KEYS_COUNT = ("1", "2", "Более 2")
# Веб-вход U9 (8c/8e): руль вводит клиент, когда оценка не дала данных авто;
# те же строки, что STEERING_CANON для found_info
TILE_TRADEIN_STEERING = ("Левый", "Правый")

# Поле анкеты → допустимые значения словаря; не из словаря → 422 not_allowed
# (пустая строка/None допустимы — черновик).
FORM_TILE_FIELDS = {
    "marital_status": frozenset(TILE_MARITAL_STATUS),
    "education": frozenset(TILE_EDUCATION),
    "employment": frozenset(TILE_EMPLOYMENT),
    "tradein_condition": frozenset(TILE_TRADEIN_CONDITION),
    "owners_by_pts": frozenset(TILE_OWNERS_BY_PTS),
    "pts_type": frozenset(TILE_PTS_TYPE),
    "keys_count": frozenset(TILE_KEYS_COUNT),
    "tradein_steering": frozenset(TILE_TRADEIN_STEERING),
}

FREE_TEXT_MAX = 500   # лимит свободного текста анкеты (origin-дока, «Обработка ввода»)
# Диапазон целых полей анкеты. Верхняя граница — ниже потолка INTEGER
# PostgreSQL (2 147 483 647), в который упирается запись: без неё 10**30 из
# автосохранения давал необработанный OverflowError на commit → 500 и техалерт
# на КАЖДОЕ нажатие клавиши (фронт сохраняет по дебаунсу). 2 млрд с запасом
# перекрывают взнос/доход в рублях, срок, пробег, год и число владельцев.
FORM_INT_MIN = 0
FORM_INT_MAX = 2_000_000_000
# По-полевые границы поверх общих: год выпуска трейд-ина не бывает 1899
# (план U1: tradein_year=1899 → out_of_range)
FORM_INT_RANGES = {"tradein_year": (1900, FORM_INT_MAX)}
COMMENT_MAX = 500     # лимит комментария таймлайна с несопоставленными полями

# --- Профильные UF-значения (R3): пишутся из замороженного снапшота
# calc_snapshot профиля, НЕ из формы; значения нет / снапшота нет — код
# не пишется, отправка не падает. Значения — строками.
PROFILE_UF_MAP = {
    "steering": "UF_CRM_1785857507",      # found_info.steering_wheel снапшота
    "ti_valuation": "UF_CRM_1785857524",  # ключ ti_valuation снапшота
}

# auto.ru отдаёт steering_wheel кодом (LEFT/RIGHT); в UF уходит каноническая
# формулировка, которую клиент видит в блоке «Ваш автомобиль» макета
# («Руль: Левый»). Уже каноническое значение проходит как есть; иные
# непустые строки пропускаются без изменений (данные не теряются).
STEERING_CANON = {"LEFT": "Левый", "RIGHT": "Правый"}

# Поля формы, чьи значения уходят в UF строкой (план U1: «число → строкой»)
FORM_UF_STR_VALUES = frozenset({"combo_extra_down"})
# Кредитные поля анкеты — в чистом трейд-ине в сделку не пишутся (2026-10-03)
CREDIT_FORM_FIELDS = frozenset({"credit_initial", "credit_term", "credit_payment",
                                "combo_extra_down"})

# Значение карты с этим префиксом — плейсхолдер до заведения UF-поля на портале:
# поле считается несопоставленным и уходит в комментарий (cabinet_uf_map.json).
UF_PLACEHOLDER_PREFIX = "UF_CRM_CABINET_"
UF_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cabinet_uf_map.json")


_uf_map_cache = {"path": None, "mtime": None, "map": None}


def load_uf_map() -> dict:
    """Карта «поле анкеты → UF-код сделки» (R24). Разобранный JSON кешируется
    в модульной переменной с инвалидацией по mtime — карта по-прежнему
    расширяется без изменения кода и без рестарта, но парсится только при
    изменении файла. Служебные ключи (_comment) отбрасываются; битый/
    отсутствующий файл не роняет сабмит — все поля уйдут комментарием
    таймлайна (фолбэк, не кешируется)."""
    try:
        mtime = os.path.getmtime(UF_MAP_FILE)
        if _uf_map_cache["path"] == UF_MAP_FILE and _uf_map_cache["mtime"] == mtime:
            return _uf_map_cache["map"]
        with open(UF_MAP_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.error("cabinet: cabinet_uf_map.json не прочитан (%s) — "
                     "все поля анкеты уйдут комментарием таймлайна", e)
        return {}
    if not isinstance(raw, dict):
        logger.error("cabinet: cabinet_uf_map.json не dict — карта проигнорирована")
        return {}
    parsed = {k: v for k, v in raw.items() if not k.startswith("_")}
    _uf_map_cache.update(path=UF_MAP_FILE, mtime=mtime, map=parsed)
    return parsed


def _coerce_form_fields(fields) -> tuple:
    """Мягкая валидация типов: числа приводимы ('36' → 36, 36.0 → 36), None и
    пустая строка очищают поле, мусор → ошибка ПО ПОЛЮ (422 validation).
    Неизвестный ключ → unknown_field: молчаливая потеря данных хуже отказа.
    Свободный текст обрезается до FREE_TEXT_MAX. Целые проверяются на диапазон
    FORM_INT_MIN..FORM_INT_MAX с по-полевыми границами FORM_INT_RANGES
    (out_of_range) — иначе значение вне INTEGER роняет commit OverflowError'ом
    мимо ApiError: 500 и техалерт на каждое автосохранение. Словарные поля
    плиток (FORM_TILE_FIELDS) принимают ТОЛЬКО канонические строки макета —
    не из словаря → not_allowed (R2); None/пустая строка допустимы (черновик).
    Возвращает (clean, errors)."""
    clean, errors = {}, {}
    if not isinstance(fields, dict):
        return {}, {"_body": "object_required"}
    for key, value in fields.items():
        if key in FORM_INT_FIELDS:
            if value is None or (isinstance(value, str) and not value.strip()):
                clean[key] = None
                continue
            if isinstance(value, bool):
                errors[key] = "not_a_number"
                continue
            if isinstance(value, int):
                number = value
            elif isinstance(value, float) and value.is_integer():
                number = int(value)
            else:
                try:
                    number = int(str(value).strip())
                except (ValueError, TypeError, OverflowError):
                    errors[key] = "not_a_number"
                    continue
            lo, hi = FORM_INT_RANGES.get(key, (FORM_INT_MIN, FORM_INT_MAX))
            if not (lo <= number <= hi):
                errors[key] = "out_of_range"
            else:
                clean[key] = number
        elif key in FORM_STR_FIELDS:
            if value is None or (isinstance(value, str) and not value.strip()):
                clean[key] = None
            elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
                text_value = str(value).strip()[:FREE_TEXT_MAX]
                allowed = FORM_TILE_FIELDS.get(key)
                if allowed is not None and text_value not in allowed:
                    errors[key] = "not_allowed"
                else:
                    clean[key] = text_value
            else:
                errors[key] = "not_a_string"
        else:
            errors[key] = "unknown_field"
    return clean, errors


async def _load_form(session, user_id: int, deal_id: str) -> Optional[CabinetForm]:
    return (await session.execute(
        select(CabinetForm).where(CabinetForm.user_id == user_id,
                                  CabinetForm.deal_id == deal_id)
    )).scalar_one_or_none()


async def _get_or_create_form(session, user_id: int, deal_id: str, **fields):
    """Get-or-create строки анкеты. IntegrityError уникальности (user_id,
    deal_id) — гонка параллельного создания, лечится повторным чтением.
    Возвращает (form | None, created)."""
    form = CabinetForm(user_id=user_id, deal_id=deal_id, **fields)
    session.add(form)
    try:
        await session.commit()
        return form, True
    except IntegrityError:
        await session.rollback()
        return await _load_form(session, user_id, deal_id), False


def _form_payload(form: Optional[CabinetForm]) -> dict:
    return {f: (getattr(form, f) if form is not None else None) for f in FORM_FIELDS}


def _parse_steps(profile: Optional[CabinetProfile]) -> dict:
    try:
        data = json.loads(profile.steps_status) if profile and profile.steps_status else {}
    except (ValueError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


# --- GET /cabinet/api/me (расширение заглушки U4) ---

async def get_me(authorization: Optional[str]) -> dict:
    """Профиль + черновик анкеты + статусы шагов + признак submitted.
    Сам вызов — сигнал жизни: validate_bearer продлевает скользящее окно
    сессии и обновляет last_seen_at токена (/me — фоновый пинг U8).

    R25: qualification_payment из CabinetProfile предзаполняет ПУСТОЕ поле
    платежа черновика (однократно, до сабмита); сабмит значение в профиле
    не перезаписывает — оно остаётся для аналитики."""
    auth = await validate_bearer(authorization)
    async with database.AsyncSessionLocal() as session:
        token = await session.get(CabinetToken, auth["token_id"])
        user = await session.get(User, auth["user_id"])
        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == auth["user_id"])
        )).scalar_one_or_none()
        deal_id = token.deal_id or ""
        form = await _load_form(session, auth["user_id"], deal_id)

        qp = profile.qualification_payment if profile is not None else None
        if qp is not None:
            if form is None:
                # гонка параллельного /me — _get_or_create_form перечитывает
                form, _ = await _get_or_create_form(session, auth["user_id"],
                                                    deal_id, credit_payment=qp)
            elif form.submitted_at is None and form.credit_payment is None:
                form.credit_payment = qp
                await session.commit()

        submitted = bool(form is not None and form.submitted_at is not None)
        steps = _parse_steps(profile)
        steps["phone_confirmed"] = token.bound_at is not None
        steps["form_submitted"] = submitted

        profile_payload = _profile_payload(profile, token)
        profile_payload["name"] = user.full_name if user else None

        return {
            "state": "ok",
            "profile": profile_payload,
            "masked_phone": mask_phone(token.bound_phone),
            "form": _form_payload(form),
            "submitted": submitted,
            # Наружу всегда aware-ISO (с зоной) — одинаково на PostgreSQL и SQLite
            "submitted_at": _aware(form.submitted_at).isoformat() if submitted else None,
            "steps_status": steps,
            # U5 (R14): замороженный снапшот калькулятора + frozen_at ISO —
            # только после верификации (весь /me за Bearer); нет снапшота → null
            "calc": _calc_payload(profile),
            # Веб-вход (план 2026-09-30-001, U3): источник сессии и явный
            # статус оценки трейд-ина (0 и null в ti_valuation неразличимы)
            "entry_source": token.entry_source or "chat",
            "valuation": await _valuation_payload(session, profile),
        }


async def _valuation_payload(session, profile) -> dict:
    """Блок valuation для /me: {status, reason}. Оценки нет — status «none»."""
    valuation_id = getattr(profile, "valuation_id", None) if profile is not None else None
    if not valuation_id:
        return {"status": "none", "reason": None}
    row = await session.get(database.CabinetValuation, valuation_id)
    if row is None:
        return {"status": "none", "reason": None}
    return {"status": row.status, "reason": row.reason}


# --- PUT /cabinet/api/form (R24: черновик — только локальная БД) ---

async def save_form_draft(authorization: Optional[str], fields) -> dict:
    """Частичное обновление черновика: применяются ТОЛЬКО переданные ключи;
    Bitrix не вызывается. После submitted_at → 409 already_submitted
    (read-only наступает только от submitted_at). Отсутствующая строка
    создаётся под (user_id, deal_id) токена; IntegrityError уникальности —
    гонка параллельного сохранения, лечится повторным чтением."""
    auth = await validate_bearer(authorization)
    clean, errors = _coerce_form_fields(fields)
    if errors:
        raise ApiError(422, "validation", fields=errors)

    async with database.AsyncSessionLocal() as session:
        token = await session.get(CabinetToken, auth["token_id"])
        deal_id = token.deal_id or ""
        form = await _load_form(session, auth["user_id"], deal_id)
        if form is None:
            form, created = await _get_or_create_form(session, auth["user_id"],
                                                      deal_id, **clean)
            if created:
                return {"status": "draft_saved"}
            if form is None:
                raise ApiError(409, "conflict")
        if form.submitted_at is not None:
            raise ApiError(409, "already_submitted")
        for key, value in clean.items():
            setattr(form, key, value)
        await session.commit()
    return {"status": "draft_saved"}


# --- POST /cabinet/api/form/submit ---

async def _verify_calc_check(user_id: int, calc_check: dict) -> None:
    """Сверка блока calc={item_id, price} сабмита с ТЕКУЩИМ снапшотом профиля
    (U5): item_id ИЛИ price отличаются → 409 conflict (фронт показывает попап
    «Повторить» и перечитывает /me). Сверка по содержанию, не по frozen_at:
    перезаморозка идёт на каждом ходе с активным токеном, и проверка по
    времени давала бы 409 любому клиенту, параллельно переписывающемуся с
    агентом. Перезаморозка теми же данными 409 не даёт."""
    async with database.AsyncSessionLocal() as session:
        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == user_id)
        )).scalar_one_or_none()
    snap = _parse_calc_snapshot(profile)
    snap_item = str(snap["item_id"]) if snap and snap.get("item_id") is not None else None
    snap_price = _calc_num(snap.get("price")) if snap else None
    req_item = calc_check.get("item_id")
    req_item = str(req_item) if req_item is not None else None
    req_price = _calc_num(calc_check.get("price"))
    if snap is None or req_item != snap_item or req_price != snap_price:
        raise ApiError(409, "conflict")


async def submit_form(authorization: Optional[str], fields) -> dict:
    """Однократная отправка анкеты. Финальные значения — ИЗ ТЕЛА запроса
    (поверх черновика, тело выигрывает); валидация «минимум одно заполненное
    поле любого блока» → иначе 422. Однократность — атомарный
    UPDATE ... WHERE submitted_at IS NULL: двойной сабмит даёт одного
    победителя, второй получает already_submitted. После успеха —
    on_form_submitted (триггер 1 передачи заявки, U10): конверсия + push UF.

    U5 (план 2026-08-06): опциональный блок сверки снапшота calc={item_id,
    price}, по которому считал браузер; блока нет (старый фронт) → проверка
    пропускается; расхождение с текущим снапшотом → 409 conflict ДО любых
    эффектов (защита от «химеры»). Сверка по содержанию, не по времени.
    calc присутствует, но не object (например, строка/список/null-не-в-JSON) →
    422 validation (fields={"calc": "object_required"}) — malformed payload
    не должен молча проходить как «блока нет»."""
    auth = await validate_bearer(authorization)
    calc_check = None
    if isinstance(fields, dict) and "calc" in fields:
        fields = dict(fields)
        raw_calc = fields.pop("calc")
        if not isinstance(raw_calc, dict):
            raise ApiError(422, "validation", fields={"calc": "object_required"})
        calc_check = raw_calc
    clean, errors = _coerce_form_fields(fields)
    if errors:
        raise ApiError(422, "validation", fields=errors)
    if calc_check is not None:
        await _verify_calc_check(auth["user_id"], calc_check)
    now = _utcnow()

    async with database.AsyncSessionLocal() as session:
        token = await session.get(CabinetToken, auth["token_id"])
        deal_id = token.deal_id or ""
        form = await _load_form(session, auth["user_id"], deal_id)
        if form is None:
            form, _ = await _get_or_create_form(session, auth["user_id"], deal_id)
            if form is None:
                raise ApiError(409, "conflict")
        if form.submitted_at is not None:
            raise ApiError(409, "already_submitted")

        for key, value in clean.items():
            setattr(form, key, value)  # финальные значения — из тела
        if not any(getattr(form, f) is not None for f in FORM_FIELDS):
            await session.rollback()
            raise ApiError(422, "validation", reason="empty_form")

        captured = await session.execute(
            update(CabinetForm)
            .where(CabinetForm.id == form.id, CabinetForm.submitted_at.is_(None))
            .values(submitted_at=now)
        )
        if captured.rowcount != 1:
            await session.rollback()  # гонка двойного сабмита — победитель один
            raise ApiError(409, "already_submitted")

        # steps_status.form_submitted в профиль НЕ персистится — get_me всегда
        # пересчитывает его из submitted_at, других читателей нет (мёртвая
        # запись убрана); R25: qualification_payment сабмитом не трогается.
        session.add(CabinetEvent(user_id=auth["user_id"], token_id=token.id,
                                 event_type="form_submitted"))
        await session.commit()

    # U10, триггер 1: конверсия и push UF — внутри attempt_handoff (конверсия
    # идёт ПЕРВОЙ: стадия/статус/телефон, затем поля анкеты по карте).
    await on_form_submitted(auth["user_id"], deal_id)
    return {"status": "submitted"}


# --- Отправка анкеты в Bitrix (переиспользуется U10 при переотправке) ---

def _escape_bbcode(value) -> str:
    """[ и ] → HTML-сущности: текст клиента не может открыть/закрыть BBCode-тег
    в комментарии таймлайна (origin-дока, «Обработка ввода»)."""
    return str(value).replace("[", "&#91;").replace("]", "&#93;")


def _build_field_comment(header: str, pairs, footer: Optional[str] = None) -> str:
    """Комментарий таймлайна «заголовок + поле: значение»: BBCode-экранирование,
    лимит COMMENT_MAX — общий механизм U5 (несопоставленные поля) и U10
    (черновик). footer (маркер позиции расчёта, U1/R19) дописывается ДО общей
    обрезки — лимит один на весь комментарий."""
    lines = [header]
    for field, value in pairs:
        lines.append(f"{field}: {_escape_bbcode(value)}")
    if footer:
        lines.append(footer)
    comment = "\n".join(lines)
    if len(comment) > COMMENT_MAX:
        comment = comment[:COMMENT_MAX - 1] + "…"
    return comment


def _build_unmapped_comment(unmapped, marker: Optional[str] = None) -> str:
    return _build_field_comment("Анкета кабинета — поля без UF-сопоставления:",
                                unmapped, footer=marker)


def _parse_calc_snapshot(profile: Optional[CabinetProfile]) -> Optional[dict]:
    """JSON снапшота калькулятора из cabinet_profile.calc_snapshot (Text).
    Битый/пустой снапшот → None: отправка анкеты не падает (R3)."""
    if profile is None or not profile.calc_snapshot:
        return None
    try:
        data = json.loads(profile.calc_snapshot)
    except (ValueError, TypeError):
        logger.warning("cabinet: calc_snapshot профиля user=%s не парсится — "
                       "профильные UF пропущены", profile.user_id)
        return None
    if not isinstance(data, dict):
        return None
    # Гейт версии схемы (остаток #10 закрыт: schema_version теперь читается).
    # Более новая версия, чем знает этот код (даунгрейд после отката деплоя),
    # не отбрасывается — известные ключи стабильны, но факт фиксируется.
    ver = data.get("schema_version")
    if isinstance(ver, int) and ver > CALC_SNAPSHOT_SCHEMA_VERSION:
        logger.warning("cabinet: calc_snapshot user=%s схемы v%s новее "
                       "поддерживаемой v%s — читаются известные ключи",
                       profile.user_id, ver, CALC_SNAPSHOT_SCHEMA_VERSION)
    return data


def _canonical_steering(raw) -> Optional[str]:
    """LEFT/RIGHT auto.ru → каноническая строка макета «Левый»/«Правый» (R2);
    уже каноническое или иное непустое значение — как есть (строкой)."""
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    return STEERING_CANON.get(value.upper(), value)


def _profile_uf_and_marker(profile: Optional[CabinetProfile]) -> tuple:
    """(uf-словарь профильных значений, строка-маркер позиции расчёта | None).

    R3: steering / ti_valuation — из замороженного снапшота, строками;
    значения нет — код не пишется. Маркер (item_id + frozen_at снапшота) —
    строка в комментарий таймлайна при сабмите; UF-поле под него не заводится."""
    snap = _parse_calc_snapshot(profile)
    if not snap:
        return {}, None
    uf = {}
    found = snap.get("found_info")
    steering = _canonical_steering(
        found.get("steering_wheel") if isinstance(found, dict) else None)
    if steering:
        uf[PROFILE_UF_MAP["steering"]] = steering
    ti = snap.get("ti_valuation")
    if ti is not None and str(ti).strip():
        uf[PROFILE_UF_MAP["ti_valuation"]] = str(ti).strip()

    marker = None
    item_id = snap.get("item_id")
    if item_id is not None and str(item_id).strip():
        frozen = _aware(profile.calc_frozen_at)
        frozen_str = frozen.isoformat() if frozen else str(snap.get("frozen_at") or "—")
        marker = (f"Расчёт кабинета: item_id={_escape_bbcode(item_id)}, "
                  f"frozen_at={_escape_bbcode(frozen_str)}")
    return uf, marker


async def _bitrix_call_or_enqueue(method: str, params: dict, platform_uid,
                                  err_log: str) -> bool:
    """Вызов Bitrix с фолбэком в pending_sync НЕДЕБАУНСИМЫМ путём (R35):
    чат-путь того же пользователя мог ставить задачу секунду назад, и дебаунс
    проглотил бы единственную постановку. True — вызов прошёл (конвенция
    _request: сбой → {}).

    Постановка здесь — ЕДИНСТВЕННАЯ: инъецированный _bitrix_request обязан
    возвращать неуспех, не ставя задачу сам (main._cabinet_bitrix_request).
    Иначе на один сбой приходились бы две строки очереди, и реплей
    crm.timeline.comment.add дублировал бы комментарий в сделке."""
    ok = False
    try:
        result = await _bitrix_request(method, params, user_id=platform_uid) \
            if _bitrix_request else {}
        ok = bool(result) and "error" not in result
    except Exception:
        logger.exception(err_log)
    if not ok and _enqueue_nodebounce:
        await _enqueue_nodebounce(method, params, user_id=platform_uid)
    return ok


def _is_web_profile(profile) -> bool:
    return profile is not None and bool(profile.lead_code or profile.web_form)


def _web_credit_deal_fields(profile, form, snap) -> dict:
    """«Кредит - платеж / срок / 1ый взнос / Стоимость авто» — итог кабинета
    после отправки анкеты веб-сессии (те же поля, что в сделке бота, R61).
    Коды — по таблице интеграции Tilda (cabinet_web_deal_map.json)."""
    if not _is_web_profile(profile) or form is None or form.submitted_at is None:
        return {}
    if profile.scenario_type not in ("credit", "combo"):
        return {}
    mapping = load_web_deal_map()
    if not mapping:
        return {}
    snap = snap or {}
    price = snap.get("combo_price") if profile.scenario_type == "combo" else snap.get("price")
    _, bot = web_deal_form_fields(mapping, None, {
        "MONTHLY_PAYMENT": form.credit_payment,
        "CREDIT_DURATION": form.credit_term,
        "DOWN_PAYMENT_RUB": form.credit_initial,
        "CREDIT_SUMM": _calc_num(price),
    })
    return bot


async def push_form_to_bitrix(user_id: int, deal_id) -> dict:
    """Читает анкету (user_id, deal_id) из БД, мапит поля по cabinet_uf_map.json
    в UF-словарь (crm.deal.update); несопоставленные ключи уходят текстом в
    комментарий таймлайна (crm.timeline.comment.add, BBCode-экранирование,
    лимит COMMENT_MAX). Сбой Bitrix (пустой результат/исключение) → постановка
    в pending_sync НЕДЕБАУНСИМЫМ путём (_enqueue_nodebounce, R35): чат-путь
    того же пользователя мог ставить задачу секунду назад, и дебаунс проглотил
    бы единственную постановку. Отдельная функция — её же вызовет U10 при
    переотправке результата на основную сделку."""
    deal_id = str(deal_id) if deal_id else ""
    async with database.AsyncSessionLocal() as session:
        form = await _load_form(session, user_id, deal_id)
        user = await session.get(User, user_id)
        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == user_id)
        )).scalar_one_or_none()

    outcome = {"uf": {}, "unmapped": [], "enqueued": []}
    if form is None:
        logger.warning("cabinet: push_form_to_bitrix — анкеты нет (user=%s, deal=%s)",
                       user_id, deal_id)
        return outcome
    if not deal_id:
        logger.warning("cabinet: push_form_to_bitrix — у токена нет deal_id, "
                       "отправлять некуда (user=%s)", user_id)
        return outcome

    uf_map = load_uf_map()
    uf_fields, unmapped = {}, []
    # Чистый трейд-ин: кредитных полей анкеты в сделке нет (решение 2026-10-03) —
    # фронт держит в черновике расчёт и для трейд-ина, но кредита клиент не просил
    tradein_only = profile is not None and profile.scenario_type == "tradein"
    for field in FORM_FIELDS:
        value = getattr(form, field)
        if value is None:
            continue
        if tradein_only and field in CREDIT_FORM_FIELDS:
            continue
        if field in FORM_UF_STR_VALUES:
            value = str(value)  # план U1: «число → строкой» (combo_extra_down)
        code = uf_map.get(field)
        if isinstance(code, str) and code and not code.startswith(UF_PLACEHOLDER_PREFIX):
            uf_fields[code] = value
        else:
            unmapped.append((field, value))

    # [План 2026-09-08-001, U7 / KTD-12 точка 2] Снапшот-фолбэк трейд-ин-полей:
    # при живом found_info фронт эти поля НЕ рендерит (read-only «Ваш
    # автомобиль»), в форме они None и без фолбэка не уходят вовсе (сделки
    # 51503/51511). Форма ГЛАВНЕЕ (П3): non-None значение клиента не
    # перетирается; пусто у обоих источников → ключ не появляется.
    # 2026-10-03: госномер и пробег, которых нет в анкете (в 8a/8b/8d они
    # только в карточке), — из сессии (лендинг); пробег из ответа оценки не
    # берётся никогда: Auto.ru возвращает свой, а не пробег клиента.
    snap = _parse_calc_snapshot(profile)
    found = (snap or {}).get("found_info")
    if not isinstance(found, dict):
        found = {}
    session_values = {"tradein_plate": getattr(profile, "web_plate", None),
                      "tradein_mileage": getattr(profile, "web_mileage", None)}
    snapshot_fill = {}
    for form_field, found_key in (("tradein_make", "mark"),
                                  ("tradein_model", "model"),
                                  ("tradein_year", "year"),
                                  ("tradein_mileage", None),
                                  ("tradein_plate", "plate")):
        if getattr(form, form_field) is not None:
            continue  # форма главнее
        val = session_values.get(form_field)
        if val in (None, "") and found_key:
            val = found.get(found_key)
        code = uf_map.get(form_field)
        if val in (None, "") or not isinstance(code, str) or not code \
                or code.startswith(UF_PLACEHOLDER_PREFIX):
            continue
        snapshot_fill[code] = str(val)
    uf_fields.update(snapshot_fill)
    # 2026-10-03: после отправки анкеты — итог кабинета в «Кредит - …» сделки
    # веб-входа (Tilda или бота); до отправки там желаемый платёж с лендинга
    credit_fill = _web_credit_deal_fields(profile, form, snap)
    uf_fields.update(credit_fill)
    outcome["uf"] = uf_fields
    outcome["unmapped"] = [f for f, _ in unmapped]

    # R3: профильные значения (steering / ti_valuation) — ПОСЛЕ полей формы;
    # маркер позиции расчёта — в тот же комментарий, что несёт несопоставленные
    profile_uf, calc_marker = _profile_uf_and_marker(profile)
    uf_fields.update(profile_uf)

    # KTD-17 (наблюдаемость): состав отправки одной грепаемой строкой с
    # признаком источника каждой группы — невидимость состава этого писателя
    # уже дважды стоила ложных выводов (колбэк 51365, сабмит 51503).
    logger.info("CABINET_UF_PUSH deal=%s form=%s snapshot_fill=%s profile=%s credit=%s",
                deal_id,
                {k: v for k, v in uf_fields.items()
                 if k not in snapshot_fill and k not in profile_uf and k not in credit_fill},
                snapshot_fill, profile_uf, credit_fill)

    calls = []
    if uf_fields:
        calls.append(("crm.deal.update", {"id": deal_id, "fields": uf_fields}))
    if unmapped or calc_marker:
        comment = _build_unmapped_comment(unmapped, calc_marker) if unmapped \
            else calc_marker
        if len(comment) > COMMENT_MAX:
            comment = comment[:COMMENT_MAX - 1] + "…"
        calls.append(("crm.timeline.comment.add", {"fields": {
            "ENTITY_ID": deal_id, "ENTITY_TYPE": "DEAL",
            "COMMENT": comment,
        }}))

    platform_uid = user.platform_user_id if user else None
    for method, params in calls:
        ok = await _bitrix_call_or_enqueue(
            method, params, platform_uid,
            f"cabinet: Bitrix {method} упал при отправке анкеты (deal={deal_id})")
        if not ok and _enqueue_nodebounce:
            outcome["enqueued"].append(method)
    return outcome


async def on_form_submitted(user_id: int, deal_id) -> str:
    """U10, триггер 1 (R40): явная отправка анкеты — submitted_at уже выставлен
    submit_form. Выигранный захват → конверсия + push UF; проигранный (таймаут
    успел раньше) → ветка поздней отправки внутри attempt_handoff: только
    дозапись UF и комментарий об уточнении, второй конверсии нет.

    2026-10-03 (ускорение B): клиент получает ответ сразу после захвата
    handoff_at — доведение передачи в Битрикс идёт в фоне
    (HANDOFF_IN_BACKGROUND)."""
    return await attempt_handoff(user_id, trigger="submit",
                                 background=HANDOFF_IN_BACKGROUND)


# =======================================================================
# U10: Передача заявки в Bitrix из кабинета (R39, R40, R41, R26 сигнал B).
# Передача — новый ТРИГГЕР к существующей конверсии, не новый механизм:
# _run_conversion (инъекция из main.py) внутри вызывает final_sync_wrapper
# со снапшотом контекста ИЗ БД (cabinet_profile + User) — стадия «Обогащение»,
# статус по квалификации, телефон в контакт, квалификация и стенограмма.
# Новых стадий и значений статуса кабинет не вводит.
# =======================================================================

SIGNAL_B_TEMPLATE = (
    "ℹ️ Личный кабинет: {name}, сделка {deal_id} — номер подтверждён, "
    "заявка передана в CRM, анкета не заполнена. {draft_note}"
)
SIGNAL_B_DRAFT_NOTE = "Черновик в комментарии сделки."
SIGNAL_B_NO_DATA_NOTE = "Данных анкеты нет."


# Возраст незавершённого захвата, после которого воркер доводит передачу
# повторно (_recover_stuck_handoffs). Заведомо больше длительности штатной
# конверсии и меньше времени, за которое дилер успевает заметить пропажу.
HANDOFF_RECOVERY_DELAY_SECONDS = 600
# Потолок возраста захвата для АВТОМАТИЧЕСКОГО повтора: заявка суточной
# давности доводится руками — повторная конверсия по протухшему контексту
# опаснее уведомления. Такие захваты дают техалерт и в повтор не идут.
HANDOFF_RECOVERY_MAX_AGE_SECONDS = 24 * 3600
# Grace добора незакрытых сессий (план 2026-08-14-001, U1): не раньше, чем
# у фоновой финализации хода был шанс дописать session_closed штатно.
SESSION_CLOSE_RECOVERY_DELAY_SECONDS = 600
# Потолок автоповторов добора (ревью 2026-08-15): хронически незакрываемая
# сессия не должна генерировать попытки вечно — старше суток от факта
# доведения: однократный техалерт и ручное вмешательство (образец
# HANDOFF_RECOVERY_MAX_AGE_SECONDS)
SESSION_CLOSE_RECOVERY_MAX_AGE_SECONDS = 24 * 3600


def _handoff_timeout_seconds() -> int:
    return int(os.getenv("CABINET_HANDOFF_TIMEOUT", "600"))


def _handoff_max_age_seconds() -> int:
    return int(os.getenv("CABINET_HANDOFF_MAX_AGE", "3600"))


def _build_draft_comment(values) -> str:
    """R41: незавершённая анкета уходит ТОЛЬКО текстом в таймлайн — значения
    не подтверждены клиентом и не должны попадать в UF-поля расчёта.
    Тот же механизм, что комментарий U5 (_build_field_comment)."""
    return _build_field_comment("Черновик анкеты кабинета (клиент не отправил анкету):",
                                values)


async def _send_timeline_comment(deal_id: str, comment: str, platform_uid) -> None:
    """crm.timeline.comment.add с фолбэком в pending_sync недебаунсимым путём
    (R35) — тот же паттерн, что push_form_to_bitrix (_bitrix_call_or_enqueue)."""
    params = {"fields": {"ENTITY_ID": deal_id, "ENTITY_TYPE": "DEAL",
                         "COMMENT": comment}}
    await _bitrix_call_or_enqueue(
        "crm.timeline.comment.add", params, platform_uid,
        f"cabinet: Bitrix comment.add упал при передаче (deal={deal_id})")


async def _update_cabinet_type_after_late_submit(user_id: int, deal_id) -> None:
    """O1 (план 2026-09-01-001): поздний сабмит после таймаут-передачи
    дозаписывает «Тип обращения» на «Анкета {сценарий}» — иначе карточка
    противоречит себе (UF анкеты дописаны, а поле утверждает, что анкеты
    нет) и искажается отчётность по доходимости. Сценарий — из профиля (O3);
    вне словаря поле не трогается: конверсия здесь не повторяется, и прежнего
    фолбэка _map_type у этой ветки нет. Транспорт — недебаунсимый фолбэк в
    pending_sync (R35), как у остальных отправок кабинета."""
    if not deal_id:
        return
    field_code = os.getenv("BITRIX_FIELD_TYPE")
    if not field_code:
        return
    async with database.AsyncSessionLocal() as session:
        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == user_id)
        )).scalar_one_or_none()
        user = await session.get(User, user_id)
    value = _cabinet_type_value(
        profile.scenario_type if profile is not None else None, submitted=True)
    if value is None:
        return
    await _bitrix_call_or_enqueue(
        "crm.deal.update",
        {"id": str(deal_id), "fields": {field_code: value}},
        user.platform_user_id if user else None,
        f"cabinet: Bitrix deal.update упал при дозаписи типа обращения (deal={deal_id})")


# Ускорение B (решение владельца 2026-10-03): после отправки анкеты доведение
# передачи (_complete_handoff) — фоновой задачей. Захват handoff_at и событие
# 'handoff' пишутся ДО ответа клиенту, поэтому перезапуск бота во время фоновой
# передачи доводит подбор зависших захватов (_recover_stuck_handoffs).
HANDOFF_IN_BACKGROUND = True
_HANDOFF_TASKS: set = set()   # сильные ссылки на фоновые передачи


async def _complete_handoff_logged(user_id: int, trigger: str) -> None:
    try:
        await _complete_handoff(user_id, trigger)
    except Exception:
        logger.exception("cabinet: фоновая передача заявки упала (user=%s, trigger=%s) — "
                         "доведёт подбор зависших захватов", user_id, trigger)


def _spawn_handoff(user_id: int, trigger: str) -> None:
    task = asyncio.create_task(_complete_handoff_logged(user_id, trigger))
    _HANDOFF_TASKS.add(task)
    task.add_done_callback(_HANDOFF_TASKS.discard)


async def drain_background_handoffs() -> None:
    """Дождаться фоновых передач (тесты; штатная остановка не обязана)."""
    while _HANDOFF_TASKS:
        await asyncio.gather(*list(_HANDOFF_TASKS), return_exceptions=True)


async def attempt_handoff(user_id: int, trigger: str,
                          now: Optional[datetime] = None,
                          background: bool = False) -> str:
    """Единая точка передачи заявки — ровно одна конверсия при любом сочетании
    триггеров (R40). trigger: 'submit' | 'timeout' | 'max_age'.

    Захват атомарный: UPDATE cabinet_tokens SET handoff_at=now WHERE
    user_id=? AND handoff_at IS NULL AND bound_at IS NOT NULL (+ активный
    токен) — rowcount решает гонку кнопки и таймаута. bound_at IS NOT NULL
    в самом захвате — R39: неверифицированный клиент не передаётся вовсе.

    Захват (намерение) и доведение (факт) разведены: 'handoff' пишется
    в момент захвата, 'handoff_done' — только после _complete_handoff.
    Рестарт между ними подбирается _recover_stuck_handoffs.

    Возвращает исход: 'converted' (захват выигран, передача доведена или
    оставлена воркеру) | 'not_verified' (R39 / активного токена нет) |
    'already_handed' (захват занят, таймаут-триггер — no-op) | 'late_submit'
    (захват занят, но trigger='submit': поздняя отправка — только дозапись UF
    + комментарий об уточнении, без второй конверсии и без дубля
    черновик-комментария)."""
    now = _aware(now) or _utcnow()   # наивный now от вызывающего — к aware UTC
    async with database.AsyncSessionLocal() as session:
        captured = await session.execute(
            update(CabinetToken)
            .where(
                CabinetToken.user_id == user_id,
                CabinetToken.status == "active",
                CabinetToken.bound_at.is_not(None),
                CabinetToken.handoff_at.is_(None),
            )
            .values(handoff_at=now)
        )
        await session.commit()
        won = (captured.rowcount or 0) == 1

        token = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == user_id,
                                       CabinetToken.status == "active")
        )).scalars().first()
        if token is None or token.bound_at is None:
            return "not_verified"  # R39: без верификации — ни по одному триггеру
        deal_id = token.deal_id or ""
        token_id = token.id

        if won:
            session.add(CabinetEvent(
                user_id=user_id, token_id=token_id, event_type="handoff",
                meta_json=json.dumps({"trigger": trigger}),
            ))
            await session.commit()

    if not won:
        if trigger == "submit":
            # Поздняя отправка: конверсия уже была по таймауту — стадия
            # повторно не трогается. Дозапись UF по карте + комментарий с
            # несопоставленным (push шлёт лишь новое — черновик-комментарий
            # не дублируется) + дозапись «Типа обращения» на «Анкета …»
            # (решение O1, план 2026-09-01-001).
            # Синхронно и при background: у поздней дозаписи нет подбора после
            # перезапуска, фон потерял бы её молча
            await push_form_to_bitrix(user_id, deal_id)
            await _update_cabinet_type_after_late_submit(user_id, deal_id)
            return "late_submit"
        return "already_handed"

    if background:
        _spawn_handoff(user_id, trigger)
        return "converted"
    await _complete_handoff(user_id, trigger)
    return "converted"


def _fire_handoff_completed(user_id: int, trigger: str, deal_id, token_id) -> None:
    """Fire-and-forget вызов колбэка «передача доведена» (план 2026-08-14-001).

    НЕ await: _complete_handoff await-ится из HTTP-хендлера submit_form и из
    последовательных циклов otp_worker — ожидание завершения сессии (turn_lock
    хода держится до минут) подвешивало бы ответ сабмита клиенту и передачи
    остальных пользователей. Сильная ссылка — в _HOOK_TASKS (create_task без
    ссылки может быть собран GC). Сбой колбэка не роняет передачу: сам колбэк
    оборачивает себя try/except, а незакрытую сессию добирает
    _recover_unclosed_sessions по паре событий handoff_done/session_closed."""
    if _on_handoff_completed is None:
        return
    inflight = _HOOK_INFLIGHT.get(user_id)
    if inflight is not None and not inflight.done():
        return  # завершение этого пользователя уже в работе — не дублируем

    async def _run():
        try:
            await _on_handoff_completed(user_id, trigger, deal_id, token_id)
        except Exception:
            logger.exception("cabinet: колбэк завершения сессии упал "
                             "(user=%s, trigger=%s) — добор повторит",
                             user_id, trigger)

    task = asyncio.create_task(_run())
    _HOOK_TASKS.add(task)
    _HOOK_INFLIGHT[user_id] = task
    task.add_done_callback(_HOOK_TASKS.discard)
    task.add_done_callback(lambda t, uid=user_id: (
        _HOOK_INFLIGHT.pop(uid, None) if _HOOK_INFLIGHT.get(uid) is t else None))


async def record_session_farewell(user_id, token_id: int) -> bool:
    """Идемпотентный гард отправки прощания (ревью 2026-08-15, P1 трёх
    ревьюеров): прощание уходит клиенту ДО финализации архива, а session_closed
    пишется только ПОСЛЕ успешной финализации — без этого гарда каждый повтор
    добора при хронически падающей финализации слал бы клиенту новое
    «Александр покинул чат». True — прощание ещё не отправлялось, можно слать;
    False — уже уходило, повтор ограничивается финализацией."""
    return await _record_event_once(user_id, token_id, "session_farewell_sent")


async def record_session_closed(user_id: int, token_id: int) -> bool:
    """Идемпотентная отметка «сессия диалога завершена по передаче» (U1).

    Пишется обёрткой завершения main.py СТРОГО ПОСЛЕ фактического завершения
    (прощание + финализация) — запись раньше при рестарте теряла бы in-memory
    pending_* без возможности добора. Meta ПУСТОЙ намеренно: _record_event_once
    дедуплицирует по token+type+meta, и триггер в meta пропускал бы второе
    завершение при повторе другим путём (timeout против recovery/добора)."""
    return await _record_event_once(user_id, token_id, "session_closed")


async def reminders_suppressed(user_pk: int) -> bool:
    """Кабинетное окно «номер подтверждён, передача не произошла» (план
    2026-08-14-001, U5): клиент заполняет анкету — ремайндеры бота «актуален
    ли вопрос?» в этом окне шум. После передачи подавление не нужно: сессию
    завершает on_handoff_completed, и reminder_worker сам отфильтрует по
    is_smartbot_active. Потребитель — reminder_worker (wiring-only)."""
    async with database.AsyncSessionLocal() as session:
        row = (await session.execute(
            select(CabinetToken.id).where(
                CabinetToken.user_id == user_pk,
                CabinetToken.status == "active",
                CabinetToken.bound_at.is_not(None),
                CabinetToken.handoff_at.is_(None),
            ).limit(1)
        )).scalar_one_or_none()
    return row is not None


async def has_session_closed(token_id: int) -> bool:
    """Есть ли отметка завершения сессии по токену — no-op гейт обёртки U2."""
    async with database.AsyncSessionLocal() as session:
        row = (await session.execute(
            select(CabinetEvent.id).where(
                CabinetEvent.token_id == token_id,
                CabinetEvent.event_type == "session_closed",
            ).limit(1)
        )).scalar_one_or_none()
    return row is not None


async def _recover_unclosed_sessions(now: datetime) -> int:
    """Добор незакрытых сессий (план 2026-08-14-001, U1): передача доведена
    ('handoff_done'), а завершение сессии не состоялось ('session_closed' нет) —
    рестарт/сбой между ними, включая потерю in-memory pending_*.

    Grace-задержка — по образцу HANDOFF_RECOVERY_DELAY_SECONDS, отсчёт от
    СОБЫТИЯ 'handoff_done' (не от handoff_at): окно фоновой финализации хода
    (прощание ушло, finalize ещё пишет Bitrix/NocoDB) — не сигнал к повтору,
    а recovery-путь, доведший передачу только что, не должен получать второй
    выстрел добора в том же тике. НАМЕРЕННО без фильтра по статусу токена:
    истёкший или отозванный после передачи токен не должен молча терять
    незакрытую сессию. Повтор безопасен: колбэк идемпотентен по гарду
    session_closed."""
    if _on_handoff_completed is None:
        return 0
    cutoff = now - timedelta(seconds=SESSION_CLOSE_RECOVERY_DELAY_SECONDS)
    async with database.AsyncSessionLocal() as session:
        # Алиас обязателен: внешний запрос уже JOIN-ит CabinetEvent, и
        # exists-подзапрос по той же таблице авто-коррелировался бы в пустой FROM
        closed_ev = aliased(CabinetEvent)
        closed = (select(closed_ev.id)
                  .where(closed_ev.token_id == CabinetToken.id,
                         closed_ev.event_type == "session_closed")
                  .exists())
        rows = (await session.execute(
            select(CabinetToken.id, CabinetToken.user_id, CabinetToken.deal_id,
                   CabinetEvent.created_at)
            .join(CabinetEvent, CabinetEvent.token_id == CabinetToken.id)
            .where(
                CabinetToken.handoff_at.is_not(None),
                CabinetEvent.event_type == "handoff_done",
                CabinetEvent.created_at <= cutoff,
                ~closed,
            )
        )).all()
    oldest = now - timedelta(seconds=SESSION_CLOSE_RECOVERY_MAX_AGE_SECONDS)
    fired = 0
    for token_id, user_id, deal_id, done_at in rows:
        if (_aware(done_at) or now) < oldest:
            # Потолок автоповторов: суточная незакрываемая сессия — ручное
            # вмешательство; алерт однократен (гард-событие)
            if await _record_event_once(user_id, token_id,
                                        "session_close_stuck_alert") and _tech_alert:
                await _tech_alert(
                    f"⚠️ Личный кабинет: сессия диалога не закрывается после "
                    f"передачи заявки больше суток (user_id={user_id}, "
                    f"token_id={token_id}) — завершите вручную."
                )
            continue
        logger.warning("cabinet: сессия не завершена после передачи "
                       "(token=%s, user=%s) — повтор завершения", token_id, user_id)
        _fire_handoff_completed(user_id, "close_recovery", deal_id, token_id)
        fired += 1
    return fired


async def _set_handoff_stage(user_id: int, token_id, deal_id, platform_uid,
                             fields_ok: bool) -> None:
    """Смена стадии передачи — ПОСЛЕДНЯЯ запись в сделку (исправление
    2026-09-30: робот «Обогащения» собирает «Текст письма (квалиф)» из полей
    сделки в момент смены стадии; при прежнем порядке «стадия → поля анкеты»
    письмо дилеру уходило с пустыми цифрами, сделка 52869). Поля записаны —
    прямой вызов (сбой → очередь); поля ушли в очередь — стадия тоже в
    очередь, ПОСЛЕ них (реплей держит порядок по сделке). Однократно на
    сделку: гард-событие, повтор подбора зависших захватов стадию не шлёт."""
    if not (_handoff_stage and deal_id):
        return
    if not await _record_event_once(user_id, token_id, "handoff_stage", {"deal_id": deal_id}):
        return
    params = {"id": deal_id, "fields": {"STAGE_ID": _handoff_stage}}
    if _set_stage:
        # План 2026-10-12-001: стадия «было» — одним чтением перед записью;
        # ложный таймаут записи не повторяет её (роботы «Обогащения» шлют
        # письма дилеру внутри запроса). Чтение не удалось — «было» неизвестно:
        # одна попытка, при сбое очередь без ключа (прежний путь)
        expect = await _read_deal_stage(deal_id, platform_uid)
        if fields_ok:
            await _set_stage(deal_id, _handoff_stage, expect, user_id=platform_uid)
        elif _enqueue_nodebounce:
            queued = {**params, STAGE_EXPECT_KEY: expect} if expect else params
            await _enqueue_nodebounce("crm.deal.update", queued, user_id=platform_uid)
        return
    if fields_ok:
        await _bitrix_call_or_enqueue(
            "crm.deal.update", params, platform_uid,
            f"cabinet: Bitrix не принял смену стадии передачи (deal={deal_id})")
    elif _enqueue_nodebounce:
        await _enqueue_nodebounce("crm.deal.update", params, user_id=platform_uid)


async def _read_deal_stage(deal_id, platform_uid) -> Optional[str]:
    """Текущая стадия сделки (crm.deal.get через _bitrix_request); None — не прочитана."""
    if not _bitrix_request:
        return None
    try:
        res = await _bitrix_request("crm.deal.get", {"id": deal_id}, user_id=platform_uid)
    except Exception:
        logger.exception("cabinet: чтение стадии сделки %s перед сменой не удалось", deal_id)
        return None
    deal = (res or {}).get("result")
    if isinstance(deal, dict) and deal.get("STAGE_ID"):
        return str(deal["STAGE_ID"])
    return None


async def _complete_handoff(user_id: int, trigger: str) -> bool:
    """Доведение передачи ПОСЛЕ выигранного захвата: конверсия (без смены
    стадии) → отправка результата в сделку → смена стадии на «Обогащение»
    (_set_handoff_stage, последней) → отметка факта 'handoff_done'.

    Вызывается из attempt_handoff и из подбора зависших захватов
    (_recover_stuck_handoffs), поэтому каждый внешний эффект идемпотентен:
    push UF, комментарий черновика и сигнал B закрыты гард-событиями
    _record_event_once с меткой сделки. Возвращает True — передача доведена
    (записано 'handoff_done'); False — не доведена, подберёт следующий тик.

    Сделка перечитывается ПОСЛЕ конверсии: ссылку кабинета выдают и без
    сделки (deal_id=None), а сделку заводит как раз конверсия — иначе анкета
    верифицированного клиента навсегда оставалась бы в локальной БД."""
    if _run_conversion:
        try:
            await _run_conversion(user_id)
        except Exception:
            logger.exception("cabinet: конверсия передачи заявки упала "
                             "(user=%s, trigger=%s) — факт не отмечен, "
                             "передачу довёдет подбор зависших захватов",
                             user_id, trigger)
            return False

    # Сделка могла появиться только что (конверсия) или прийти из чата —
    # дозаполняем токен/профиль/анкету и работаем уже с ней.
    async with database.AsyncSessionLocal() as session:
        token = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == user_id,
                                       CabinetToken.status == "active")
        )).scalars().first()
        if token is None:
            return False
        token_id, dealer_code = token.id, token.dealer_code
        deal_id = token.deal_id or ""
        user = await session.get(User, user_id)
        fresh_deal = str(user.active_deal_id) if user and user.active_deal_id else ""

    if not deal_id and fresh_deal:
        # resend=False: результат отправит эта же функция ниже — иначе
        # crm.deal.update ушёл бы дважды.
        await adopt_deal_id(user_id, fresh_deal, resend=False)
        deal_id = fresh_deal

    async with database.AsyncSessionLocal() as session:
        form = await _load_form(session, user_id, deal_id)
        form_submitted = form is not None and form.submitted_at is not None
        draft_values = [(f, getattr(form, f)) for f in FORM_FIELDS
                        if form is not None and getattr(form, f) is not None]
        user = await session.get(User, user_id)
    has_draft = (not form_submitted) and bool(draft_values)
    client_name = user.full_name if user and user.full_name else "Клиент"
    platform_uid = user.platform_user_id if user else None

    if trigger == "submit" or form_submitted:
        # Триггер 1: полная анкета — UF по карте + комментарий несопоставленных.
        # Сигнал B не уходит: заявка ушла штатным путём (R26). Гард с меткой
        # сделки: повтор подбора не дублирует комментарий несопоставленных,
        # но появившаяся ПОЗЖЕ сделка (другая метка) отправку получает.
        fields_ok = True
        if deal_id and await _record_event_once(user_id, token_id,
                                                "handoff_form_push",
                                                {"deal_id": deal_id}):
            pushed = await push_form_to_bitrix(user_id, deal_id)
            fields_ok = not pushed.get("enqueued")
        await _set_handoff_stage(user_id, token_id, deal_id, platform_uid, fields_ok)
        # Колбэк — только при ПЕРВИЧНОЙ записи факта (ревью 2026-08-15):
        # повторные доведения (recovery/late_submit) не стреляют вторым
        # завершением — незакрытую сессию подберёт _recover_unclosed_sessions
        if await _record_event_once(user_id, token_id, "handoff_done"):
            _fire_handoff_completed(user_id, trigger, deal_id, token_id)
        await _web_valuation_after_handoff(user_id)
        return True

    # Триггеры 2/3: незавершённая анкета — ТОЛЬКО комментарий в таймлайн
    # (R41), UF не пишутся, submitted_at не выставляется (фильтр R34 не
    # включается); плюс сигнал B дилеру (R26).
    # Идемпотентность черновик-комментария: «комментарий с черновиком — ровно
    # один» (Key Technical Decisions) — гард-событие _record_event_once ДО
    # отправки закрывает и гонку двойного комментария, и повтор подбора.
    if has_draft and deal_id and await _record_event_once(
            user_id, token_id, "handoff_draft_comment", {"deal_id": deal_id}):
        await _send_timeline_comment(deal_id, _build_draft_comment(draft_values),
                                     platform_uid)

    await _set_handoff_stage(user_id, token_id, deal_id, platform_uid, True)

    if _dealer_alert and await _record_event_once(user_id, token_id, "handoff_signal_b"):
        await _dealer_alert(dealer_code, SIGNAL_B_TEMPLATE.format(
            name=_esc(client_name), deal_id=_esc(deal_id or "—"),
            draft_note=SIGNAL_B_DRAFT_NOTE if has_draft else SIGNAL_B_NO_DATA_NOTE,
        ))
    # Колбэк — только при первичной записи факта (см. комментарий в ветке submit)
    if await _record_event_once(user_id, token_id, "handoff_done"):
        _fire_handoff_completed(user_id, trigger, deal_id, token_id)
    await _web_valuation_after_handoff(user_id)
    return True


async def _web_valuation_after_handoff(user_id: int) -> None:
    """U12: оценка, завершившаяся до передачи, — в сделку сразу после неё
    (неуспех — строка R58; успех без анкеты — UF). Сбой не роняет передачу."""
    try:
        await write_valuation_to_deal(user_id)
    except Exception:
        logger.exception("cabinet: запись оценки после передачи user=%s упала", user_id)


# Подписи сценариев для «Типа обращения» (BITRIX_FIELD_TYPE) кабинетной
# конверсии — решение владельца O2 (2026-09-01, план 2026-09-01-001): единый
# принцип для всех сценариев, итог — «Анкета {подпись}» / «Авторизация
# {подпись}». Сценарий вне словаря / пустой — решения владелец не давал:
# значение НЕ формируется, поле заполняет прежний _map_type (фолбэк).
CABINET_TYPE_SCENARIO_LABELS = {
    "credit": "кредит", "tradein": "трейд-ин", "combo": "комбо",
    "stock": "наличие", "consult": "консультация", "refusal": "отказ",
    "other_model": "другая модель",
}
# Ключ снапшота конверсии с готовым значением поля. Живёт ТОЛЬКО в памяти
# снапшота (build_conversion_snapshot ничего не персистит): чатовые контексты
# и context_json его не содержат — на этом стоит гарантия «некабинетные
# сделки не затронуты» (фолбэк в BitrixService, main.py).
CABINET_TYPE_KEY = "cabinet_uf_type"


def _cabinet_type_value(scenario_type: Optional[str], submitted: bool) -> Optional[str]:
    """Готовое значение «Типа обращения» кабинетной конверсии или None.

    Исход — по факту submitted_at в момент вызова (решение владельца: НЕ по
    триггеру передачи — триггер до формирования поля не доезжает, а recovery
    подменяет его на "recovery"). Сценарий вне словаря → None."""
    label = CABINET_TYPE_SCENARIO_LABELS.get(scenario_type or "")
    if label is None:
        return None
    return ("Анкета " if submitted else "Авторизация ") + label


async def build_conversion_snapshot(user_pk: int) -> tuple:
    """U10: снапшот контекста конверсии ИЗ БД — User.context_json, дополненный
    полями cabinet_profile (scenario_type, qualification_payment) без
    перетирания. НЕ из USER_CONTEXT: кабинет живёт дольше сессии диалога,
    и к моменту таймаут-передачи контекст в памяти может быть уже вычищен.
    Возвращает (platform_user_id, snapshot); (None, None) — пользователь
    не найден. Потребитель — обёртка run_conversion в main.py (wiring-only).

    Дополнительно (план 2026-09-01-001): снапшот несёт готовый «Тип
    обращения» (CABINET_TYPE_KEY). Сценарий — СТРОГО из profile.scenario_type
    (решение O3: подпись называет анкету, под которую выдана ссылка;
    переквалификация в чате не влияет — приоритет context_json в
    scenario_type самого снапшота при этом сохраняется). Исход — по
    submitted_at анкеты активного токена, адрес формы тот же, что у
    push_form_to_bitrix: (user_id, deal_id токена)."""
    async with database.AsyncSessionLocal() as session:
        user = await session.get(User, user_pk)
        if user is None:
            return None, None
        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == user_pk)
        )).scalar_one_or_none()
        token = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == user_pk,
                                       CabinetToken.status == "active")
        )).scalars().first()
        form = None
        if token is not None:
            form = await _load_form(session, user_pk, token.deal_id or "")
    try:
        snapshot = json.loads(user.context_json or '{}')
    except (ValueError, TypeError):
        snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    if profile is not None:
        # cabinet_profile дополняет пробелы кэша квалификации, не перетирая его
        if profile.scenario_type and not snapshot.get('scenario_type'):
            snapshot['scenario_type'] = profile.scenario_type
        if profile.qualification_payment is not None and not snapshot.get('payment'):
            snapshot['payment'] = profile.qualification_payment
    # «Тип обращения»: без активного токена значение не формируется (передача
    # без токена невозможна — attempt_handoff вернул бы not_verified; ветка
    # только страхует прямые вызовы) — работает прежний фолбэк _map_type.
    if token is not None:
        type_value = _cabinet_type_value(
            profile.scenario_type if profile is not None else None,
            submitted=form is not None and form.submitted_at is not None)
        if type_value is not None:
            snapshot[CABINET_TYPE_KEY] = type_value
        # Веб-вход (U10): диалога нет — ключи конверсии из профиля и каталога
        if token.entry_source == "web" and profile is not None:
            snapshot.update(await _web_conversion_extras(profile, token))
    return user.platform_user_id, snapshot


# =======================================================================
# U7: Интеграция с ботом (R7, R8/AE6, R10, R25, R34) — выдача ссылки при
# AI handoff, статусы кабинета в qualification_context, телефон в CRM после
# верификации, фильтр «кабинет перезаписывает», склейка deal hunt.
# main.py здесь wiring-only: вся логика — в этих функциях.
# =======================================================================

PHONE_CONFIRMED_ALERT_TEMPLATE = (
    "✅ Личный кабинет: {name}, сделка {deal_id} — телефон подтверждён: {masked}. "
    "Источник: личный кабинет."
)
# Веб-вход (план 2026-09-30-001, U6/R70): имя «Клиент», номер сделки — только
# если сделка уже известна, источник — «личный кабинет с сайта»
PHONE_CONFIRMED_WEB_ALERT_TEMPLATE = (
    "✅ Личный кабинет: {name}, сделка {deal_id} — телефон подтверждён: {masked}. "
    "Источник: личный кабинет с сайта."
)
PHONE_CONFIRMED_WEB_NO_DEAL_ALERT_TEMPLATE = (
    "✅ Личный кабинет: {name} — телефон подтверждён: {masked}. "
    "Источник: личный кабинет с сайта."
)
VERIFY_MISMATCH_ALERT_TEMPLATE = (
    "⚠️ Личный кабинет: {name}, сделка {deal_id} — расхождение номеров: "
    "в кабинете подтверждён {masked_new}, ранее в чате указан {masked_old}. "
    "Номер в карточке не перезаписан."
)
VERIFY_MISMATCH_COMMENT_TEMPLATE = (
    "Личный кабинет: клиент подтвердил по OTP номер {masked_new}, "
    "в чате ранее указан другой номер ({masked_old}). "
    "Номер из чата сохранён без изменений."
)
CHAT_MISMATCH_ALERT_TEMPLATE = (
    "⚠️ Личный кабинет: {name}, сделка {deal_id} — клиент прислал в чате номер "
    "{masked_new}, отличный от подтверждённого в кабинете {masked_old}. "
    "OTP-подтверждённый номер не перезаписан (R8)."
)
CHAT_MISMATCH_COMMENT_TEMPLATE = (
    "Личный кабинет: клиент прислал в чате номер {masked_new}, отличный от "
    "OTP-подтверждённого {masked_old}. Подтверждённый номер сохранён без изменений."
)


async def _record_event_once(user_id, token_id, event_type: str,
                             meta: Optional[dict] = None) -> bool:
    """Идемпотентная пометка фактом-событием: True — событие записано впервые
    (для пары token_id + event_type + идентичный meta). Паттерн реестра
    идемпотентности (crm_mapping) на постоянном хранилище CabinetEvent."""
    meta_dump = json.dumps(meta or {}, ensure_ascii=False, sort_keys=True)
    async with database.AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(CabinetEvent.meta_json).where(
                CabinetEvent.token_id == token_id,
                CabinetEvent.event_type == event_type,
            )
        )).scalars().all()
        for stored in rows:
            try:
                same = json.dumps(json.loads(stored or "{}"), ensure_ascii=False,
                                  sort_keys=True) == meta_dump
            except (ValueError, TypeError):
                same = False
            if same:
                return False
        session.add(CabinetEvent(user_id=user_id, token_id=token_id,
                                 event_type=event_type, meta_json=meta_dump))
        await session.commit()
        return True


# --- Пост-верификация (R7/R8; хук из verify_code, зависимости — из setup) ---

async def _phone_confirmed_text(token_id, name, deal_ref, verified_phone) -> str:
    """Текст алерта «телефон подтверждён» (R70): веб-вход — отдельный вариант."""
    async with database.AsyncSessionLocal() as session:
        token = await session.get(CabinetToken, token_id)
    masked = _esc(mask_phone(verified_phone))
    if token is not None and token.entry_source == "web":
        if deal_ref:
            return PHONE_CONFIRMED_WEB_ALERT_TEMPLATE.format(
                name="Клиент", deal_id=_esc(deal_ref), masked=masked)
        return PHONE_CONFIRMED_WEB_NO_DEAL_ALERT_TEMPLATE.format(name="Клиент", masked=masked)
    return PHONE_CONFIRMED_ALERT_TEMPLATE.format(
        name=_esc(name), deal_id=_esc(deal_ref or "—"), masked=masked)


async def _post_verify_hook(user_id: int, token_id: int, deal_id,
                            dealer_code: str, verified_phone: str) -> None:
    """После успешной верификации: save_user_phone() с пометкой источника
    «кабинет», алерт дилеру о подтверждении (идемпотентно по сделке —
    CabinetEvent); если в User.phone уже лежит ДРУГОЙ номер из чата —
    User.phone НЕ перезаписывается, в сделку уходит комментарий + алерт (AE6)."""
    async with database.AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
    if user is None:
        return
    platform_uid = user.platform_user_id
    name = user.full_name or "Клиент"
    chat_phone = normalize_phone(user.phone) if user.phone else None
    mismatch = chat_phone is not None and chat_phone != verified_phone
    deal_ref = str(deal_id) if deal_id else ""

    if not mismatch:
        # Существующий хелпер записи; источник «кабинет» фиксируется событием.
        # Возврат проверяется защитно (перенос 3.1.2): канонический +79xx
        # всегда проходит предикат бота, False здесь — сигнал расхождения
        # валидаторов, молчать о нём нельзя.
        saved = await database.save_user_phone(platform_uid, verified_phone,
                                               user.full_name or "Клиент",
                                               user.username or "")
        if not saved:
            logger.error("cabinet: OTP-подтверждённый номер НЕ записан "
                         "save_user_phone (user=%s) — проверьте согласованность "
                         "валидаторов", user_id)
        await _record_event_once(user_id, token_id, "cabinet_phone_saved",
                                 {"deal_id": deal_ref, "source": "cabinet"})

    # Алерт дилеру о подтверждении номера — однократно по сделке (R7)
    if await _record_event_once(user_id, token_id, "phone_confirmed_alert",
                                {"deal_id": deal_ref}):
        if _dealer_alert:
            await _dealer_alert(dealer_code, await _phone_confirmed_text(
                token_id, name, deal_ref, verified_phone))

    if mismatch and await _record_event_once(user_id, token_id, "phone_mismatch",
                                             {"deal_id": deal_ref, "side": "verify"}):
        if deal_ref:
            await _send_timeline_comment(
                deal_ref,
                VERIFY_MISMATCH_COMMENT_TEMPLATE.format(
                    masked_new=mask_phone(verified_phone),
                    masked_old=mask_phone(chat_phone)),
                platform_uid)
        if _dealer_alert:
            await _dealer_alert(dealer_code, VERIFY_MISMATCH_ALERT_TEMPLATE.format(
                name=_esc(name), deal_id=_esc(deal_ref or "—"),
                masked_new=_esc(mask_phone(verified_phone)),
                masked_old=_esc(mask_phone(chat_phone))))


# --- Страж записи телефона из чата (AE6/R8; ставится в database из setup) ---

async def chat_phone_guard(platform_user_id, phone) -> bool:
    """False — запись в User.phone пропускается: у клиента OTP-подтверждённый
    номер, и новый номер из чата с ним расходится (AE6). Расхождение
    фиксируется комментарием в сделку и алертом дилеру, идемпотентно по
    сделке. Любой сбой стража разрешает запись (fail-open: кабинет не должен
    ломать штатную воронку)."""
    try:
        async with database.AsyncSessionLocal() as session:
            user = (await session.execute(
                select(User).where(User.platform_user_id == str(platform_user_id))
            )).scalar_one_or_none()
            if user is None:
                return True
            token = (await session.execute(
                select(CabinetToken).where(
                    CabinetToken.user_id == user.id,
                    CabinetToken.status == "active",
                    CabinetToken.bound_at.is_not(None),
                    CabinetToken.bound_phone.is_not(None),
                )
            )).scalars().first()
        if token is None:
            return True
        incoming = normalize_phone(phone)
        if incoming is not None and incoming == token.bound_phone:
            return True  # тот же номер — запись безвредна
        current = normalize_phone(user.phone) if user.phone else None
        if current is not None and current != token.bound_phone:
            # В User.phone и так номер из чата (verify-расхождение) — R8 его не охраняет
            return True

        deal_ref = str(token.deal_id) if token.deal_id else ""
        if await _record_event_once(user.id, token.id, "phone_mismatch",
                                    {"deal_id": deal_ref, "side": "chat"}):
            masked_new = mask_phone(incoming) or "не распознан"
            masked_old = mask_phone(token.bound_phone)
            if deal_ref:
                await _send_timeline_comment(
                    deal_ref,
                    CHAT_MISMATCH_COMMENT_TEMPLATE.format(
                        masked_new=masked_new, masked_old=masked_old),
                    str(platform_user_id))
            if _dealer_alert:
                await _dealer_alert(token.dealer_code, CHAT_MISMATCH_ALERT_TEMPLATE.format(
                    name=_esc(user.full_name or "Клиент"), deal_id=_esc(deal_ref or "—"),
                    masked_new=_esc(masked_new), masked_old=_esc(masked_old)))
        logger.info("cabinet: R8 — номер из чата отличается от OTP-подтверждённого, "
                    "User.phone не перезаписан (user=%s)", user.id)
        return False
    except Exception:
        logger.exception("cabinet: страж телефона упал — запись разрешена (fail-open)")
        return True


# --- Выдача ссылки при AI handoff и статус кабинета для n8n ---

async def get_cabinet_status(user_id: int) -> str:
    """not_visited / phone_confirmed / form_submitted — вычисляется из БД
    для qualification_context (обе точки сборки в main.py)."""
    async with database.AsyncSessionLocal() as session:
        token = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == user_id,
                                       CabinetToken.status == "active")
        )).scalars().first()
        if token is None or token.bound_at is None:
            return "not_visited"
        form = await _load_form(session, user_id, token.deal_id or "")
        if form is not None and form.submitted_at is not None:
            return "form_submitted"
        return "phone_confirmed"


async def upsert_cabinet_profile(user_id: int, dealer_code: Optional[str] = None,
                                 deal_id=None, scenario_type: Optional[str] = None,
                                 payment=None, brand_model: Optional[str] = None) -> None:
    """Заполнение CabinetProfile из контекста квалификации при выдаче ссылки.
    qualification_payment — первичная копия (R25: дальше не перезаписывается);
    остальные поля дополняются, пустое значением не перетирается."""
    try:
        payment_int = int(payment) if payment not in (None, "", 0, "0") else None
    except (ValueError, TypeError):
        payment_int = None
    deal_ref = str(deal_id) if deal_id else None

    async with database.AsyncSessionLocal() as session:
        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == user_id)
        )).scalar_one_or_none()
        changed = profile is None
        if profile is None:
            profile = CabinetProfile(user_id=user_id)
            session.add(profile)
        if dealer_code and profile.dealer_code != dealer_code:
            profile.dealer_code = dealer_code
            changed = True
        if deal_ref and profile.deal_id != deal_ref:
            profile.deal_id = deal_ref
            changed = True
        if scenario_type and profile.scenario_type != scenario_type:
            profile.scenario_type = scenario_type
            changed = True
        if brand_model and profile.brand_model != brand_model:
            profile.brand_model = brand_model
            changed = True
        if payment_int is not None and profile.qualification_payment is None:
            profile.qualification_payment = payment_int  # R25: копия один раз
            changed = True
        if not changed:
            return  # значения не изменились — без commit
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()  # гонка создания профиля — следующая попытка дозаполнит


# =======================================================================
# U5 (план 2026-08-06): данные расчёта — заморозка снапшота калькулятора,
# NocoDB-ставки/программа, карточка авто (R13–R15, R18, R20).
# =======================================================================

# v2: + model_id (модельная часть пары идентификации позиции каталога).
# Снапшоты v1 живут до перезаморозки (каждый ход с активным токеном) —
# _find_catalog_row деградирует к поиску по одной комплектационной части.
# v3 (план 2026-09-30-001, U3): + combo_price (цена комбо, R17).
CALC_SNAPSHOT_SCHEMA_VERSION = 3

# Проекция полей при заморозке (KTD «Ставки/программа … с проекцией полей»):
# служебные колонки таблиц дилера в браузер не уезжают.
CALC_RATES_FIELDS = ("Rate", "Term_From", "Term_To",
                     "Downpayment_From", "Downpayment_To")
CALC_PROGRAM_FIELDS = ("Program_Name", "Max_Term_Months", "Min_Down_Payment")

# Веб-вход (план 2026-09-30-001, U2/R21): единственная колонка минимального
# взноса в таблицах программ — Min_Down_Payment (проценты). Нет колонки, пусто
# или у дилера нет таблицы программ — умолчание 20 % (как в n8n) и техалерт
# не чаще раза в сутки на дилера и причину.
MIN_DOWN_PAYMENT_DEFAULT_PCT = 20
MIN_DOWN_ALERT_INTERVAL = timedelta(hours=24)
_min_down_alert_sent: dict = {}   # (dealer_code, reason) -> datetime последнего алерта


def round10k(value) -> int:
    """Округление до 10 000 «половина вверх» — как Math.round(v / 1e4) * 1e4
    в JS (n8n Car Search). Для неотрицательных цен и скидок."""
    return int(math.floor(float(value) / 10000 + 0.5)) * 10000


def compute_prices(row: dict) -> Optional[dict]:
    """Цены позиции каталога (R17, решение владельца 2026-09-30):

        credit_price = round10k(Price old) − round10k(Cash in hook) − round10k(Credit bonus)
        combo_price  = credit_price − round10k(Trade-in bonus)

    «Price new» — цена со всеми скидками — в расчётах не участвует.
    Пустая или нечисловая скидка — 0. Нечисловая или непозитивная «Price old»
    либо неположительный результат — позиция недоступна для расчёта (None)."""
    if not isinstance(row, dict):
        return None
    base = _calc_num(row.get("Price old"))
    if base is None or base <= 0:
        return None

    def _discount(field):
        num = _calc_num(row.get(field))
        return round10k(num) if num is not None and num > 0 else 0

    credit = round10k(base) - _discount("Cash in hook") - _discount("Credit bonus")
    combo = credit - _discount("Trade-in bonus")
    if credit <= 0 or combo <= 0:
        return None
    return {"credit_price": credit, "combo_price": combo}


def reset_min_down_alerts() -> None:
    """Сброс дедупа техалертов минимального взноса (тесты)."""
    _min_down_alert_sent.clear()


INACTIVE_POSITION_ALERT_INTERVAL = timedelta(hours=24)
_inactive_position_alert_sent: dict = {}


def reset_inactive_position_alerts() -> None:
    """Сброс дедупа техалертов о неактивной позиции (тесты)."""
    _inactive_position_alert_sent.clear()


def dealer_catalog_active_field(dealer_code) -> str:
    """Колонка активности каталога дилера (db_config.catalog_active_field, 3.4.1)."""
    dealers = _dealers_provider() if _dealers_provider else {}
    db_config = ((dealers or {}).get(str(dealer_code)) or {}).get("db_config") or {}
    return str(db_config.get("catalog_active_field") or "").strip() or CATALOG_ACTIVE_FIELD_DEFAULT


def _flag_on(value) -> bool:
    """Флажок NocoDB: True / 1 / «true» / «1» — взведён; остальное — нет."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return str(value or "").strip().lower() in ("true", "1")


async def _alert_inactive_position(dealer_code, row: dict, model_id, compl_id) -> None:
    """web/open позиции, не активной у дилера по его колонке, — техалерт раз в
    сутки на дилера+позицию; сессия открывается (вариант б). Колонки нет в
    строке каталога — отдельная причина (неверная настройка дилера)."""
    field = dealer_catalog_active_field(dealer_code)
    if field in row and _flag_on(row.get(field)):
        return
    key = (str(dealer_code), str(model_id), str(compl_id))
    now = _utcnow()
    last = _inactive_position_alert_sent.get(key)
    if last is not None and now - last < INACTIVE_POSITION_ALERT_INTERVAL:
        return
    _inactive_position_alert_sent[key] = now
    reason = (f"в каталоге нет колонки «{field}»" if field not in row
              else f"позиция не отмечена в колонке «{field}»")
    logger.warning("cabinet: web/open неактивной позиции дилер=%s model_id=%s compl_id=%s — %s",
                   dealer_code, model_id, compl_id, reason)
    if _tech_alert:
        try:
            await _tech_alert(
                f"⚠️ Вход с сайта: дилер {dealer_code}, позиция {catalog_item_name(row) or '—'} "
                f"(Model id {model_id}, Complectation id {compl_id}) — {reason}. Кабинет открыт; "
                f"проверьте лендинг дилера (устаревшая сборка?) и Catalog_Active_Field.")
        except Exception as e:  # алерт не должен ронять открытие сессии
            logger.warning("cabinet: техалерт о неактивной позиции не отправлен: %s", e)


async def _alert_min_down_default(dealer_code, reason: str) -> None:
    """Техалерт об умолчании минимального взноса — раз в сутки на дилера+причину."""
    key = (str(dealer_code), reason)
    now = _utcnow()
    last = _min_down_alert_sent.get(key)
    if last is not None and now - last < MIN_DOWN_ALERT_INTERVAL:
        return
    _min_down_alert_sent[key] = now
    if _tech_alert:
        try:
            await _tech_alert(
                f"⚠️ Кабинет: дилер {dealer_code} — {reason}; минимальный взнос "
                f"взят по умолчанию {MIN_DOWN_PAYMENT_DEFAULT_PCT} %.")
        except Exception as e:  # алерт не должен ронять заморозку снапшота
            logger.warning("cabinet: техалерт о минимальном взносе не отправлен: %s", e)


def _calc_num(value):
    """Число из значения NocoDB: строка с запятой → точка («16,5» → 16.5),
    пробелы/неразрывные пробелы выбрасываются; непарсимое → None.
    Целые значения возвращаются int (чистый JSON без 84.0)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if value is None:
        return None
    s = str(value).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def _normalize_rates(rows) -> list:
    """Матрица ставок для снапшота: только именованные поля CALC_RATES_FIELDS,
    все значения — числами; строка с нечисловым/пустым значением выбрасывается
    ЦЕЛИКОМ; сортировка по (Term_From, Downpayment_From). Пустая матрица —
    штатно: движок дофолбэчит 16%.

    Исключение — Downpayment_To: пустое/None читается как «открытая вилка
    сверху» и превращается в число 100, а не роняет строку — это ДОСЛОВНО
    поведение движка (calc_engine.browser.js getSmartRate: undefined/null/""
    → 100). Остальные четыре поля такого фолбэка не имеют: нечисловое или
    пустое значение по-прежнему выбрасывает строку целиком."""
    rates = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        projected = {}
        for field in CALC_RATES_FIELDS:
            raw = row.get(field)
            if field == "Downpayment_To" and (raw is None or (isinstance(raw, str) and raw.strip() == "")):
                num = 100
            else:
                num = _calc_num(raw)
            if num is None:
                projected = None
                break
            projected[field] = num
        if projected is not None:
            rates.append(projected)
    rates.sort(key=lambda r: (r["Term_From"], r["Downpayment_From"]))
    return rates


def _catalog_complectation_id(row: dict) -> str:
    """Комплектационный id строки каталога — дословно car_search_prepare.js:86:
    `Complectation id` || `Complectation_id` || `Id` (строкой).

    ВНИМАНИЕ: это ТОЛЬКО комплектационная часть («16»), она НЕ уникальна по
    таблице каталога — уникальна лишь внутри одной модели. n8n сравнивает её
    в массиве, уже отфильтрованном моделью запроса (нода Get Catalog), бот
    ищет по всему каталогу — поэтому голое сравнение по этой функции даёт
    молчаливое чужое совпадение; используйте _find_catalog_row."""
    return str(row.get("Complectation id") or row.get("Complectation_id")
               or row.get("Id") or "")


def _catalog_row_model_id(row: dict) -> str:
    """Модельная часть идентификатора строки каталога — колонка `Model id`
    NocoDB (защитно: и вариант с подчёркиванием, по образцу комплектационной
    колонки)."""
    return str(row.get("Model id") or row.get("Model_id") or "")


def _cache_model_id(cache) -> Optional[str]:
    """model_id позиции из n8n_cache: эхо qualification_context, которое
    Car Search возвращает внутри ответа (car_search_prepare.js:220), а бот
    сохраняет как json_cache целиком («полный кэш, не срез»). Это значение
    темпорально согласовано с complectations[] того же ответа — в отличие от
    сессионного user_context, который на момент заморозки мог не обновляться
    с входа по диплинку. «00» — «не задана», как у complectation_id."""
    if not isinstance(cache, dict):
        return None
    qc = cache.get("qualification_context")
    if isinstance(qc, str):  # в пайплайне встречается и JSON-строкой
        try:
            qc = json.loads(qc)
        except (ValueError, TypeError):
            return None
    if not isinstance(qc, dict):
        return None
    value = qc.get("model_id")
    if value in (None, "", "00"):
        return None
    return str(value)


def _find_catalog_row(catalog: list, item_id, model_id) -> Optional[dict]:
    """Однозначный поиск строки каталога по идентификатору позиции снапшота.

    Идентичность позиции в системе — пара (Model id, Complectation id):
    комплектационная часть уникальна только внутри модели (выгрузка дилера
    99: id 11 — 7 строк разных моделей), пара уникальна по таблице. Поиск
    намеренно НЕ опирается на отображаемые строки (Model title и т.п.) —
    их переименовывают в NocoDB независимо от снапшота.

    model_id есть (схема снапшота v2) → строгий поиск по паре: ровно одно
    совпадение — оно, иначе None. model_id нет (снапшот, замороженный до v2)
    → деградация к поиску по одной комплектационной части: единственное
    совпадение — оно, коллизия → None. В обоих режимах неоднозначность даёт
    честный «не найден», чужая модель не показывается никогда."""
    matches = [r for r in catalog
               if isinstance(r, dict)
               and _catalog_complectation_id(r) == str(item_id)]
    if model_id not in (None, ""):
        matches = [r for r in matches
                   if _catalog_row_model_id(r) == str(model_id)]
        # Дубли пары внутри модели (каталоги Geely, ALLCAR): как n8n Car
        # Search — первая строка в порядке NocoDB (поправка 2026-09-30)
        return matches[0] if matches else None
    return matches[0] if len(matches) == 1 else None


async def _load_dealer_table(dealer_code, cfg_key: str) -> Optional[list]:
    """Строки таблицы NocoDB дилера по ключу db_config (rates_table_id /
    program_table_id / catalog_table_id) через инъецированный загрузчик.
    Нет table_id / загрузчика — штатно (таблица у дилера не настроена):
    пустой список. Исключение загрузчика (транзиентный сбой NocoDB) — None:
    это НЕ то же самое, что «данных нет», и вызывающий код (_freeze_calc_snapshot)
    решает сам, можно ли доверять пустому результату при непустом существующем
    снапшоте."""
    dealers = _dealers_provider() if _dealers_provider else {}
    db_config = ((dealers or {}).get(dealer_code) or {}).get("db_config") or {}
    table_id = db_config.get(cfg_key)
    if not table_id or _nocodb_table_loader is None:
        return []
    try:
        rows = await _nocodb_table_loader(table_id)
    except Exception as e:
        logger.warning("cabinet: загрузка таблицы %s (%s) дилера %s не удалась: %s",
                       cfg_key, table_id, dealer_code, e)
        return None
    return rows if isinstance(rows, list) else []


_LOAD_FAILED = object()  # сентинел транзиентного сбоя загрузки таблицы NocoDB
                         # (не путать с None = «строки нет» / «нет матча»)


async def _match_program(item_id: str, dealer_code, model_id=None):
    """Выбор программы дилера (подтверждено владельцем 2026-09-30): Model_Pattern —
    СПИСОК Model id через запятую; берётся ПЕРВАЯ строка программы, в списке
    которой есть Model id позиции (точное совпадение элемента). Прежнее
    сравнение каталожного Id по подстроке давало ошибки (HAVAL M6 без
    программы, BELGEE X50 — с чужой); то же исправление делает n8n.
    Нет строки → None: движок применит дефолты 84/20 сам. Проекция — только
    CALC_PROGRAM_FIELDS (+ дубль ключа взноса для движка).

    Model id — из снапшота (v2+); у старых снапшотов без model_id он берётся
    из строки каталога (_find_catalog_row), неоднозначность → None.

    Возвращает _LOAD_FAILED, если каталог или таблица программ не загрузились
    из-за транзиентного сбоя NocoDB (см. _load_dealer_table) — это отличается
    от штатного None («строки действительно нет»), и вызывающий код
    (_freeze_calc_snapshot) не должен путать одно с другим."""
    needle = str(model_id).strip() if model_id not in (None, "") else ""
    if not needle:
        catalog = await _load_dealer_table(dealer_code, "catalog_table_id")
        if catalog is None:
            return _LOAD_FAILED
        row = _find_catalog_row(catalog, item_id, None)
        if row is None:
            return None
        needle = _catalog_row_model_id(row).strip()
        if not needle:
            return None
    if not _dealer_has_table(dealer_code, "program_table_id"):
        await _alert_min_down_default(dealer_code, "нет таблицы программ")
        return None
    programs = await _load_dealer_table(dealer_code, "program_table_id")
    if programs is None:
        return _LOAD_FAILED
    for prog in programs:
        if isinstance(prog, dict) and needle in _model_pattern_ids(prog.get("Model_Pattern")):
            name = prog.get("Program_Name")
            min_down = _calc_num(prog.get("Min_Down_Payment"))
            if min_down is None:
                await _alert_min_down_default(
                    dealer_code, "в таблице программ нет Min_Down_Payment или значение пустое")
                min_down = MIN_DOWN_PAYMENT_DEFAULT_PCT
            return {
                "Program_Name": str(name) if name not in (None, "") else None,
                "Max_Term_Months": _calc_num(prog.get("Max_Term_Months")),
                "Min_Down_Payment": min_down,
                # Общий с n8n движок (calc_engine.browser.js) читает прежний
                # ключ — дублируем значение, чтобы движок не менялся (U2)
                "Min_Down_Payment_Pct": min_down,
            }
    return None


def _model_pattern_ids(pattern) -> set:
    """Model_Pattern программы → множество Model id («12, 18, 14» → {"12","18","14"});
    разделители — запятая, точка с запятой, «|» и пробелы."""
    return {part for part in re.split(r"[,;|\s]+", str(pattern or "")) if part}


def _dealer_has_table(dealer_code, cfg_key: str) -> bool:
    """Настроена ли у дилера таблица db_config[cfg_key] (без загрузки)."""
    dealers = _dealers_provider() if _dealers_provider else {}
    db_config = ((dealers or {}).get(dealer_code) or {}).get("db_config") or {}
    return bool(db_config.get(cfg_key))


def _https_proto_urls(obj):
    """Протокол-относительные URL (`//avatars.mds.yandex.net/...`) в данных
    found_info → `https:` + как есть, рекурсивно (план U5: нормализация на
    сервере). Прочие значения не меняются."""
    if isinstance(obj, str):
        return "https:" + obj if obj.startswith("//") else obj
    if isinstance(obj, list):
        return [_https_proto_urls(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _https_proto_urls(v) for k, v in obj.items()}
    return obj


def _select_cache_position(cache, existing_item_id):
    """Выбор позиции расчёта из n8n_cache (KTD «Позиция расчёта»):
    selected_complectation_id → комплектация из complectations[]; нет selected
    («00» = «Любая» — тоже нет) и снапшот ПУСТ и комплектация ровно одна → она;
    иначе существующий снапшот не трогается. Угадывание по названию запрещено.

    Возвращает (комплектация | None, причина пропуска | None)."""
    if not isinstance(cache, dict):
        return None, "кэш отсутствует или не dict"
    comps = [c for c in (cache.get("complectations") or []) if isinstance(c, dict)]
    if not comps:
        return None, "в кэше нет комплектаций"
    selected = (cache.get("user_request") or {}).get("selected_complectation_id")
    selected = str(selected) if selected not in (None, "") else ""
    if selected and selected != "00":
        for comp in comps:
            if str(comp.get("id")) == selected:
                return comp, None
        return None, f"selected {selected} не найден в комплектациях кэша"
    if existing_item_id is not None:
        return None, "снапшот существует, явного selected нет — не трогаем"
    if len(comps) == 1:
        return comps[0], None
    return None, "несколько комплектаций без selected"


class CatalogUnavailable(Exception):
    """Каталог дилера недоступен (сетевой сбой NocoDB / HTTP ≠ 200) —
    отличается от «строки нет» (None). Веб-вход U3/U5: → cabinet_unavailable."""


async def load_catalog_row(dealer_code, model_id, item_id) -> Optional[dict]:
    """Строка каталога дилера по паре (Model id, Complectation id) — веб-вход
    U3 (KTD «Один точечный запрос строки каталога по паре»).

    Точечный загрузчик (catalog_row_loader) ищет фильтром NocoDB и не зависит
    от лимита полной выгрузки таблицы; без него — фолбэк на полную таблицу и
    _find_catalog_row. Нет таблицы каталога у дилера / пустой model_id —
    None. Сбой загрузки — CatalogUnavailable."""
    if model_id in (None, "") or item_id in (None, ""):
        return None
    if not _dealer_has_table(dealer_code, "catalog_table_id"):
        return None
    if _catalog_row_loader is not None:
        dealers = _dealers_provider() if _dealers_provider else {}
        table_id = (((dealers or {}).get(dealer_code) or {}).get("db_config") or {}).get(
            "catalog_table_id")
        try:
            row = await _catalog_row_loader(table_id, str(model_id), str(item_id))
        except Exception as e:
            logger.warning("cabinet: точечный запрос каталога дилера %s (%s/%s) "
                           "не удался: %s", dealer_code, model_id, item_id, e)
            raise CatalogUnavailable(str(e)) from e
        return row if isinstance(row, dict) else None
    catalog = await _load_dealer_table(dealer_code, "catalog_table_id")
    if catalog is None:
        raise CatalogUnavailable("catalog load failed")
    return _find_catalog_row(catalog, item_id, model_id)


def catalog_item_name(row: dict) -> Optional[str]:
    """Название позиции «Model · Complectation» (как карточка /car, макет v3)."""
    parts = [str(p).strip() for p in (row.get("Model"), row.get("Complectation"))
             if p is not None and str(p).strip()]
    return " · ".join(parts) if parts else (row.get("Model title") or None)


_price_fallback_alerted: dict = {}   # (user_id, item_id) -> datetime техалерта


async def _alert_price_fallback(user_id, dealer_code, item_id) -> None:
    """Путь Александра: строки каталога по паре нет — цена из n8n_cache и
    техалерт однократно в сутки на пользователя и позицию (U3)."""
    key = (user_id, str(item_id))
    now = _utcnow()
    last = _price_fallback_alerted.get(key)
    if last is not None and now - last < MIN_DOWN_ALERT_INTERVAL:
        return
    _price_fallback_alerted[key] = now
    if _tech_alert:
        try:
            await _tech_alert(
                f"⚠️ Кабинет: позиция {item_id} дилера {dealer_code} не найдена в "
                f"каталоге по паре или без «Price old» — цена взята из ответа n8n "
                f"(user_id={user_id}).")
        except Exception as e:
            logger.warning("cabinet: техалерт о цене не отправлен: %s", e)


def reset_price_fallback_alerts() -> None:
    """Сброс дедупа техалертов цены (тесты)."""
    _price_fallback_alerted.clear()


async def _collect_rates_program(user_id, dealer_code, item_id, model_id, existing):
    """Ставки и программа для снапшота. None — заморозку пропустить
    (транзиентный сбой NocoDB при непустых существующих rates/program)."""
    raw_rates = await _load_dealer_table(dealer_code, "rates_table_id")
    if raw_rates is None:
        if existing and existing.get("rates"):
            logger.warning("cabinet: заморозка снапшота user=%s пропущена: "
                           "сбой загрузки таблицы ставок дилера %s — "
                           "существующий снапшот (rates) сохранён",
                           user_id, dealer_code)
            return None
        rates = []  # существующего нет/пуст — штатная деградация, как раньше
    else:
        rates = _normalize_rates(raw_rates)

    program_result = await _match_program(item_id, dealer_code, model_id=model_id)
    if program_result is _LOAD_FAILED:
        if existing and existing.get("program"):
            logger.warning("cabinet: заморозка снапшота user=%s пропущена: "
                           "сбой загрузки каталога/программ дилера %s — "
                           "существующий снапшот (program) сохранён",
                           user_id, dealer_code)
            return None
        program = None
    else:
        program = program_result
    return rates, program


async def _store_snapshot(user_id: int, dealer_code, snapshot: dict) -> bool:
    """Запись снапшота в cabinet_profile; неизменный JSON БД не трогает."""
    new_json = json.dumps(snapshot, ensure_ascii=False)
    async with database.AsyncSessionLocal() as session:
        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == user_id)
        )).scalar_one_or_none()
        if profile is not None and profile.calc_snapshot == new_json:
            # Содержимое не изменилось (тот же item, цена, ставки) — БД не
            # трогаем: перезаморозка идёт на каждом ходе с активным токеном,
            # и без short-circuit каждый ход писал бы вхолостую.
            return True
        if profile is None:
            # Оборонительно: заморозку зовут после upsert_cabinet_profile /
            # при active-токене, но гонка создания профиля возможна
            profile = CabinetProfile(user_id=user_id, dealer_code=dealer_code)
            session.add(profile)
        profile.calc_snapshot = new_json
        profile.calc_frozen_at = _utcnow()
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()  # гонка создания профиля — заморозит следующий ход
            return False
    logger.info("cabinet: снапшот калькулятора user=%s заморожен: item_id=%s price=%s "
                "combo_price=%s (rates=%d, program=%s)", user_id, snapshot.get("item_id"),
                snapshot.get("price"), snapshot.get("combo_price"),
                len(snapshot.get("rates") or []), bool(snapshot.get("program")))
    return True


async def _existing_snapshot(user_id: int) -> Optional[dict]:
    async with database.AsyncSessionLocal() as session:
        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == user_id)
        )).scalar_one_or_none()
    return _parse_calc_snapshot(profile)


def _target_payment(payment):
    value = _calc_num(payment)
    return int(value) if value else None  # int|null


async def _freeze_calc_snapshot(user_id: int, n8n_cache, dealer_code,
                                payment) -> bool:
    """Заморозка снапшота калькулятора в cabinet_profile (R14, KTD «Снапшот
    обновляется…») — путь клиента Александра: позиция из n8n_cache.
    Неудачная заморозка (нет кэша, позиция не определена, цена невалидна,
    ТРАНЗИЕНТНЫЙ СБОЙ NocoDB при непустых существующих rates/program)
    НИКОГДА не затирает существующий снапшот — замена только успешной
    заморозкой, пропуск логируется. True — снапшот заморожен.

    Цены (план 2026-09-30-001, U3/R19): credit_price и combo_price считает
    бот по строке каталога той же пары (compute_prices); строки нет —
    credit_price из кэша n8n, combo_price null и техалерт однократно.

    Отдельно от «сбоя» — штатная пустота: если NocoDB реально ответила пустым
    списком (дилер без ставок/без программы), заморозка проходит как обычно,
    rates=[] / program=None замораживаются — это не деградация, а факт."""
    existing = await _existing_snapshot(user_id)
    existing_item = existing.get("item_id") if existing else None

    comp, skip_reason = _select_cache_position(n8n_cache, existing_item)
    if comp is None:
        logger.info("cabinet: заморозка снапшота user=%s пропущена: %s",
                    user_id, skip_reason)
        return False
    item_id = str(comp.get("id"))
    item_name = comp.get("full_name") or comp.get("name")
    # Модельная часть идентификатора — из того же кэша, что комплектации
    # (эхо qualification_context; None у старых/неполных кэшей — деградация
    # поиска описана в _find_catalog_row)
    model_id = _cache_model_id(n8n_cache)

    prices = None
    try:
        row = await load_catalog_row(dealer_code, model_id, item_id)
    except CatalogUnavailable:
        row = None
    else:
        prices = compute_prices(row) if row is not None else None
        if prices is None:
            # Нет строки или нет цены в каталоге (пустой/нулевой «Price old») —
            # цена n8n и техалерт однократно (поправка 2026-09-30)
            await _alert_price_fallback(user_id, dealer_code, item_id)
    if prices:
        price, combo_price = prices["credit_price"], prices["combo_price"]
    else:
        price = _calc_num((comp.get("price") or {}).get("credit_price"))
        combo_price = None
        if price is None or price <= 0:
            logger.warning("cabinet: заморозка снапшота user=%s пропущена: "
                           "credit_price невалиден (%r) — существующий снапшот сохранён",
                           user_id, (comp.get("price") or {}).get("credit_price"))
            return False

    collected = await _collect_rates_program(user_id, dealer_code, item_id,
                                             model_id, existing)
    if collected is None:
        return False
    rates, program = collected

    # found_info / ti_valuation: n8n v3-пайплайн кладёт их в json_cache,
    # когда оценка Auto.ru реально была (combo_prepare.js → Final Response;
    # диагностика В3 плана 2026-09-26-001) — копируются в снапшот как есть,
    # без оценки ключей в кэше нет → null. URL фото found_info
    # нормализуются до https: на сервере.
    found_info = n8n_cache.get("found_info")
    found_info = _https_proto_urls(found_info) if isinstance(found_info, dict) else None
    ti_valuation = n8n_cache.get("ti_valuation")

    snapshot = {
        "schema_version": CALC_SNAPSHOT_SCHEMA_VERSION,
        "item_id": item_id,
        "model_id": model_id,  # v2: модельная часть пары идентификации
        "item_name": item_name,
        "price": price,
        "combo_price": combo_price,  # v3: цена комбо (R17)
        "target_payment": _target_payment(payment),
        "rates": rates,
        "program": program,
        "found_info": found_info,
        "ti_valuation": ti_valuation,
    }
    return await _store_snapshot(user_id, dealer_code, snapshot)


async def freeze_web_snapshot(user_id: int, dealer_code, row: dict, payment) -> bool:
    """Веб-вход (план 2026-09-30-001, U3/R18): снапшот из строки каталога,
    без n8n_cache. Цены — compute_prices; оценка дописывается позже
    (merge_valuation_into_snapshot). False — позиция недоступна для расчёта
    или сбой NocoDB (вызывающий отвечает cabinet_unavailable)."""
    prices = compute_prices(row)
    if not prices:
        return False
    item_id = _catalog_complectation_id(row)
    model_id = _catalog_row_model_id(row) or None
    existing = await _existing_snapshot(user_id)
    collected = await _collect_rates_program(user_id, dealer_code, item_id,
                                             model_id, existing)
    if collected is None:
        return False
    rates, program = collected
    snapshot = {
        "schema_version": CALC_SNAPSHOT_SCHEMA_VERSION,
        "item_id": item_id,
        "model_id": model_id,
        "item_name": catalog_item_name(row),
        "price": prices["credit_price"],
        "combo_price": prices["combo_price"],
        "target_payment": _target_payment(payment),
        "rates": rates,
        "program": program,
        "found_info": None,
        "ti_valuation": None,
    }
    return await _store_snapshot(user_id, dealer_code, snapshot)


async def merge_valuation_into_snapshot(user_id: int, found_info, ti_valuation) -> bool:
    """Результат оценки → снапшот (U3/U7): меняются ТОЛЬКО found_info и
    ti_valuation; item_id и price не трогаются (сверка _verify_calc_check при
    сабмите). Снапшота нет — False."""
    async with database.AsyncSessionLocal() as session:
        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == user_id)
        )).scalar_one_or_none()
        snap = _parse_calc_snapshot(profile)
        if not snap:
            return False
        snap["found_info"] = (_https_proto_urls(found_info)
                              if isinstance(found_info, dict) else None)
        value = _calc_num(ti_valuation)
        snap["ti_valuation"] = int(value) if value else None
        profile.calc_snapshot = json.dumps(snap, ensure_ascii=False)
        await session.commit()
    return True


async def refresh_calc_snapshot(platform_user_id, json_cache) -> bool:
    """Вторая точка заморозки (KTD): момент сохранения свежего json_cache в
    main.py — выбор комплектации попадает в снапшот тем же ходом, без лага.
    Только при наличии active-токена у пользователя; сбой/пропуск не ломает
    отправку ответа клиенту (main.py зовёт в try/except, wiring-only).
    target_payment — из user_request.payment кэша."""
    uid = await _resolve_user_pk(platform_user_id)
    if uid is None:
        return False
    async with database.AsyncSessionLocal() as session:
        token = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == uid,
                                       CabinetToken.status == "active")
        )).scalars().first()
    if token is None:
        return False  # кабинета нет — замораживать не для кого
    if token.entry_source == "web":
        return False  # снапшот веб-сессии собирается из каталога (U3), чат его не трогает
    payment = None
    if isinstance(json_cache, dict):
        payment = (json_cache.get("user_request") or {}).get("payment")
    return await _freeze_calc_snapshot(uid, json_cache, token.dealer_code, payment)


# Allow-list ключей calc наружу (остаток #10 закрыт: спред снапшота заменён
# проекцией). Ровно то, что читает фронт (calc_model.js initState + app.js:
# found_info/ti_valuation/item_name/item_id/price); служебные ключи снапшота
# (schema_version, model_id — идентификация для серверного поиска по каталогу)
# клиенту не утекают.
CALC_PAYLOAD_KEYS = ("item_id", "item_name", "price", "combo_price", "target_payment",
                     "rates", "program", "found_info", "ti_valuation")


def _calc_payload(profile: Optional[CabinetProfile]) -> Optional[dict]:
    """Ключ calc для /cabinet/api/me: проекция снапшота по CALC_PAYLOAD_KEYS
    + frozen_at ISO (aware); снапшота нет → None."""
    snap = _parse_calc_snapshot(profile)
    if not snap:
        return None
    payload = {k: snap.get(k) for k in CALC_PAYLOAD_KEYS}
    frozen = _aware(profile.calc_frozen_at)
    payload["frozen_at"] = frozen.isoformat() if frozen else None
    return payload


def _absolute_media_url(url) -> Optional[str]:
    """Нормализация URL вложения NocoDB по образцу main.py:1256–1258:
    протокол-относительный `//…` → `https:` + как есть; не начинается с http →
    префикс базового URL NocoDB. Относительный путь без базы наружу не уходит."""
    if not url:
        return None
    u = str(url).strip()
    if not u:
        return None
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http"):
        return u
    base = str(_nocodb_base_url or "").rstrip("/")
    if not base:
        return None
    return f"{base}/{u.lstrip('/')}"


async def get_car_card(authorization: Optional[str]) -> dict:
    """GET /cabinet/api/car (R15): живая карточка авто по item_id снапшота
    поверх каталожного кэша дилера. Bearer; снапшота нет → 404 no_snapshot;
    item пропал из каталога → 404 not_found; сбой загрузки NocoDB → 503
    catalog_unavailable (находка #16: «позиция пропала» и «сервис недоступен»
    различимы). URL фото — только абсолютные (thumbnails уже вырезаны
    загрузчиком); токен NocoDB не покидает сервер."""
    auth = await validate_bearer(authorization)
    async with database.AsyncSessionLocal() as session:
        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == auth["user_id"])
        )).scalar_one_or_none()
    snap = _parse_calc_snapshot(profile)
    item_id = snap.get("item_id") if snap else None
    if item_id in (None, ""):
        raise ApiError(404, "no_snapshot")
    item_id = str(item_id)

    # None = сбой загрузки (сеть/NocoDB), НЕ «пустой каталог»: 503, чтобы
    # транзиентная недоступность не выглядела как «машина пропала» (#16)
    catalog = await _load_dealer_table(profile.dealer_code, "catalog_table_id")
    if catalog is None:
        raise ApiError(503, "catalog_unavailable")
    row = _find_catalog_row(catalog, item_id, snap.get("model_id"))
    if row is None:
        raise ApiError(404, "not_found")

    # Поля ТТХ — как их читает car_search_prepare.js (145–151);
    # «Сonsumption» — имя колонки NocoDB с КИРИЛЛИЧЕСКОЙ «С».
    # clearance — по паттерну лендинга (u6-gallery.html:947): оба написания
    # колонки; значение в каталоге уже с размерностью («170 мм») — как и у
    # остальных полей, ничего не форматируем.
    specs = {
        "engine": row.get("Engine"),
        "transmission": row.get("Transmission_ru") or row.get("Transmission"),
        "drive": row.get("Drivetrain_ru") or row.get("Drivetrain"),
        "acceleration": row.get("Acceleration"),
        "consumption": row.get("Сonsumption"),
        "clearance": row.get("clearance") or row.get("Clearance"),
    }
    # В карусель идут ТОЛЬКО Pics — как в галерее лендинга
    # (u6-gallery.html:775: car.fields['Pics'], Title_pic не участвует).
    # Цепочка значений — объединение эталона (url || signedPath || path,
    # u6-gallery.html:781) и n8n extractUrl (signedUrl || url || path).
    raw = row.get("Pics")
    pics = raw if isinstance(raw, list) else ([raw] if raw else [])
    images = []
    for pic in pics:
        if isinstance(pic, dict):
            candidate = (pic.get("signedUrl") or pic.get("url")
                         or pic.get("signedPath") or pic.get("path"))
        else:
            candidate = pic
        absolute = _absolute_media_url(candidate)
        if absolute and absolute not in images:
            images.append(absolute)
    # Название — «Model · Complectation» (макет v3: «HAVAL M6 · Family»);
    # заполнена одна часть → она без точки; обе пустые → прежний фолбэк
    # (Model title → item_name снапшота). «Model title» — комплектационный
    # титул без модели («Tech+ 1.5T 150 л.с. AT 4WD»), заголовком не годится.
    name_parts = [str(part).strip() for part in
                  (row.get("Model"), row.get("Complectation"))
                  if part is not None and str(part).strip()]
    name = (" · ".join(name_parts) if name_parts
            else row.get("Model title") or snap.get("item_name"))
    # price_new (R16 плана 2026-09-26-001, KTD8): розничная цена карточки —
    # колонка каталога «Price new» того же живого ряда; пусто/невалидно → null.
    # Это цена ВИТРИНЫ (строка «от N ₽» в шапке кабинета), НЕ цена расчёта:
    # калькулятор продолжает считать по me.calc.price (credit_price снапшота).
    price_new = _calc_num(row.get("Price new"))
    return {"name": name, "specs": specs, "images": images, "price_new": price_new}


async def prepare_cabinet_context(user_id: int, dealer_code: str, deal_id=None,
                                  has_phone: bool = False,
                                  scenario_type: Optional[str] = None,
                                  payment=None,
                                  brand_model: Optional[str] = None,
                                  n8n_cache=None) -> dict:
    """U7: {"cabinet_url", "cabinet_status"} для qualification_context.

    Выдача НОВОГО токена — только клиенту без телефона в чате (план U7:
    если телефон уже есть, штатная конверсия отработала — ссылка не нужна).
    Существующий active-токен возвращает тот же URL (R19) независимо от
    телефона — после верификации в кабинете User.phone заполняется, и ссылка
    не должна пропадать из контекста. Повторная выдача идёт связкой АКТИВНОГО
    токена (склейка deal hunt меняет deal_id токена — авторитетен токен,
    а не устаревший ctx), кроме смены дилера — тогда штатный revoke+reissue.

    Исключение из «авторитетен токен»: ПУСТАЯ сделка токена. Ссылку выдают
    и до появления сделки, и такой токен без дозаполнения навсегда уносил бы
    анкету в никуда — если сделка уже появилась в контексте, она дописывается
    в токен, профиль и анкету (adopt_deal_id), URL при этом не меняется."""
    async with database.AsyncSessionLocal() as session:
        active = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == user_id,
                                       CabinetToken.status == "active")
        )).scalars().first()
        if active is None and has_phone:
            # Ни выдачи, ни записей в этой ветке: активного токена нет →
            # статус заведомо not_visited — вычислен в той же сессии,
            # без повторного чтения токена в get_cabinet_status.
            return {"cabinet_url": None, "cabinet_status": "not_visited"}

    if active is not None and not (active.deal_id or "") and deal_id:
        await adopt_deal_id(user_id, deal_id)
        async with database.AsyncSessionLocal() as session:
            active = (await session.execute(
                select(CabinetToken).where(CabinetToken.user_id == user_id,
                                           CabinetToken.status == "active")
            )).scalars().first()

    url = None
    freeze_dealer = dealer_code
    if active is not None:
        if dealer_code and dealer_code != active.dealer_code:
            # Другой дилер — Key Technical Decisions: revoke + новый токен.
            # Профиль апсертится НОВЫМ dealer_code ДО заморозки — иначе
            # profile.dealer_code (старый) и freeze_dealer (новый) расходятся,
            # и get_car_card/заморозка читают ставки/каталог не того дилера.
            await upsert_cabinet_profile(user_id, dealer_code=dealer_code,
                                         deal_id=active.deal_id, scenario_type=scenario_type,
                                         payment=payment, brand_model=brand_model)
            url = await issue_cabinet_token(user_id, dealer_code, deal_id)
        else:
            # [План 2026-08-21-002, U1] Новая заявка того же дилера: токен
            # следует за сделкой диалога (флейвор «новая заявка» — гарды и
            # сбросы в sync_cabinet_deal). После перепривязки active
            # ПЕРЕЧИТЫВАЕТСЯ (по образцу adopt-ветки выше) — без этого
            # same_bundle сравнил бы устаревшую сделку с обновлённым токеном
            # и ОТОЗВАЛ его, выродив перепривязку в отвергнутый revoke-путь.
            if (deal_id and (active.deal_id or "")
                    and str(deal_id) != (active.deal_id or "")):
                if await sync_cabinet_deal(user_id, active.dealer_code, deal_id,
                                           payment=payment):
                    async with database.AsyncSessionLocal() as session:
                        active = (await session.execute(
                            select(CabinetToken).where(
                                CabinetToken.user_id == user_id,
                                CabinetToken.status == "active")
                        )).scalars().first()
            if active is None:
                # Токен исчез в окно перечита (гашение/экспирация) — не
                # возрождать устаревшую связку выдачей по старому объекту
                logger.warning("cabinet: active-токен user=%s исчез после "
                               "перепривязки — ход без ссылки", user_id)
            else:
                await upsert_cabinet_profile(user_id, dealer_code=active.dealer_code,
                                             deal_id=active.deal_id, scenario_type=scenario_type,
                                             payment=payment, brand_model=brand_model)
                freeze_dealer = active.dealer_code
                url = await issue_cabinet_token(user_id, active.dealer_code, active.deal_id)
    elif not has_phone:
        await upsert_cabinet_profile(user_id, dealer_code=dealer_code, deal_id=deal_id,
                                     scenario_type=scenario_type, payment=payment,
                                     brand_model=brand_model)
        url = await issue_cabinet_token(user_id, dealer_code, deal_id)

    if url is not None:
        # U5 (R14): заморозка снапшота калькулятора при каждой выдаче ссылки;
        # сбой заморозки не ломает выдачу — существующий снапшот сохраняется
        try:
            await _freeze_calc_snapshot(user_id, n8n_cache, freeze_dealer, payment)
        except Exception:
            logger.exception("cabinet: заморозка снапшота user=%s при выдаче "
                             "ссылки не удалась — снапшот не изменён", user_id)

    return {"cabinet_url": url, "cabinet_status": await get_cabinet_status(user_id)}


# --- Фильтр R34 «кабинет перезаписывает» (обе точки карты в main.py) ---

def _cabinet_owned_codes() -> set:
    """Заполненные (не плейсхолдерные) UF-коды карты cabinet_uf_map.json."""
    return {code for code in load_uf_map().values()
            if isinstance(code, str) and code and not code.startswith(UF_PLACEHOLDER_PREFIX)}


async def filter_cabinet_owned_fields(fields: dict, deal_id) -> dict:
    """R34: если по ОБНОВЛЯЕМОЙ сделке есть отправленная анкета кабинета
    (cabinet_form.submitted_at) — из fields исключаются UF-коды, пересекающиеся
    с заполненными кодами cabinet_uf_map.json; иначе fields возвращается как
    есть. Проверка по сделке, не по пользователю; передача по таймауту
    submitted_at не выставляет и фильтр не включает. Вызывается в обеих
    точках применения BITRIX_SMART_FIELDS_MAP (main.py, wiring-only)."""
    if not fields or not deal_id:
        return fields
    async with database.AsyncSessionLocal() as session:
        submitted = (await session.execute(
            select(CabinetForm.id).where(
                CabinetForm.deal_id == str(deal_id),
                CabinetForm.submitted_at.is_not(None),
            ).limit(1)
        )).scalar_one_or_none()
    if submitted is None:
        return fields
    owned = _cabinet_owned_codes()
    filtered = {k: v for k, v in fields.items() if k not in owned}
    dropped = sorted(set(fields) - set(filtered))
    if dropped:
        logger.info("cabinet: R34 — сделка %s: анкета кабинета отправлена, "
                    "из чат-обновления исключены ключи %s", deal_id, dropped)
    return filtered


# --- Склейка deal hunt: перепривязка токена, слияние анкет, переотправка ---

_DEAL_REF_KEYS = frozenset({"id", "ID", "ENTITY_ID", "deal_id", "DEAL_ID"})


def _rewrite_deal_refs(obj, old_deal_id: str, new_deal_id: str) -> bool:
    """Рекурсивная перепись ссылок на сделку в params pending_sync
    (crm.deal.update: {"id": ...}; crm.timeline.comment.add:
    {"fields": {"ENTITY_ID": ...}}). Возвращает признак изменения."""
    changed = False
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in _DEAL_REF_KEYS and str(value) == old_deal_id:
                obj[key] = new_deal_id
                changed = True
            elif isinstance(value, (dict, list)):
                changed = _rewrite_deal_refs(value, old_deal_id, new_deal_id) or changed
    elif isinstance(obj, list):
        for item in obj:
            changed = _rewrite_deal_refs(item, old_deal_id, new_deal_id) or changed
    return changed


async def _resolve_user_pk(user_id) -> Optional[int]:
    """users.id из int-PK или platform_user_id (main.py оперирует platform-ID)."""
    if isinstance(user_id, int):
        return user_id
    async with database.AsyncSessionLocal() as session:
        pk = (await session.execute(
            select(User.id).where(User.platform_user_id == str(user_id))
        )).scalar_one_or_none()
    if pk is not None:
        return pk
    try:
        return int(user_id)
    except (ValueError, TypeError):
        return None


def _form_merge_key(form: CabinetForm) -> tuple:
    """Правило слияния: побеждает строка с submitted_at, при равенстве —
    более поздний updated_at."""
    upd = _aware(form.updated_at)
    return (1 if form.submitted_at is not None else 0,
            upd.timestamp() if upd is not None else 0.0)


async def _merge_form_into_deal(session, uid: int, old_ref: str,
                                new_ref: str) -> Optional[str]:
    """Перенос строки анкеты (user_id, old_ref) на new_ref в ОТКРЫТОЙ сессии.

    Общий механизм двух путей смены сделки: склейки deal hunt
    (rebind_cabinet_on_merge) и дозаполнения сделки в токен, выданный до её
    появления (adopt_deal_id). Коллизия уникальности (user_id, new_ref)
    решается правилом слияния (_form_merge_key); проигравшая строка помечается
    вытесненной суррогатным deal_id, поэтому IntegrityError посреди операции
    не возникает. Возвращает 'rebound' | 'merged_old_won' | 'merged_new_won'
    либо None (переносить нечего). Commit — на вызывающей стороне."""
    old_form = await _load_form(session, uid, old_ref)
    if old_form is None:
        return None
    new_form = await _load_form(session, uid, new_ref)
    if new_form is None:
        old_form.deal_id = new_ref
        return "rebound"
    winner_is_old = _form_merge_key(old_form) > _form_merge_key(new_form)
    loser = new_form if winner_is_old else old_form
    loser.deal_id = f"displaced:{uuid.uuid4().hex[:8]}:{loser.deal_id}"
    await session.flush()  # освободить (user_id, new_ref) до перепривязки
    if winner_is_old:
        old_form.deal_id = new_ref
    return "merged_old_won" if winner_is_old else "merged_new_won"


async def _resend_handoff_result(uid: int, token_id: int, deal_ref: str,
                                 reason: str) -> Optional[str]:
    """Переотправка результата УЖЕ ПРОШЕДШЕЙ передачи на сделку deal_ref:
    UF отправленной анкеты (push_form_to_bitrix) либо комментарий черновика.
    Повторная конверсия НЕ запускается. Идемпотентность — гард-событие
    с меткой сделки; reason разделяет поводы ('merge' — склейка, 'adopt' —
    дозаполнение сделки). Возвращает 'form' | 'draft' | None."""
    async with database.AsyncSessionLocal() as session:
        winner = await _load_form(session, uid, deal_ref)
        user = await session.get(User, uid)
    platform_uid = user.platform_user_id if user else None

    if winner is not None and winner.submitted_at is not None:
        if await _record_event_once(uid, token_id, f"{reason}_resend_form",
                                    {"deal_id": deal_ref}):
            await push_form_to_bitrix(uid, deal_ref)
            return "form"
        return None
    draft_values = [(f, getattr(winner, f)) for f in FORM_FIELDS
                    if winner is not None and getattr(winner, f) is not None]
    if draft_values and await _record_event_once(uid, token_id, f"{reason}_resend_draft",
                                                 {"deal_id": deal_ref}):
        await _send_timeline_comment(deal_ref, _build_draft_comment(draft_values),
                                     platform_uid)
        return "draft"
    return None


async def adopt_deal_id(user_id, deal_id, resend: bool = True) -> dict:
    """U7: дозаполнение сделки в токен, выданный ДО её появления (deal_id=None).

    Ссылку кабинета выдают и без сделки; всё, что пишет клиент, ложится под
    пустой deal_id (анкета, статусы), а push_form_to_bitrix без сделки только
    логировал предупреждение — анкета верифицированного клиента оставалась
    в локальной БД навсегда. Как только сделка появилась (конверсия завела её
    или пришла из чата), она дописывается в токен, профиль и строку анкеты —
    правилом слияния _merge_form_into_deal, как при склейке deal hunt.

    Токен СОХРАНЯЕТСЯ и URL остаётся прежним: сделка вывода фиксируется в
    issue_deal_id, поэтому «тот же URL» (R19) по-прежнему воспроизводится.
    Если передача заявки по токену уже прошла — результат переотправляется на
    появившуюся сделку (_resend_handoff_result), повторной конверсии нет.
    resend=False — вызов изнутри самой передачи (_complete_handoff): она
    отправит результат сама, второй отправки не нужно."""
    outcome = {"token_updated": False, "form": None, "resent": None}
    new_ref = str(deal_id) if deal_id else ""
    if not new_ref:
        return outcome
    uid = await _resolve_user_pk(user_id)
    if uid is None:
        return outcome

    handoff_done, token_id = False, None
    async with database.AsyncSessionLocal() as session:
        token = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == uid,
                                       CabinetToken.status == "active")
        )).scalars().first()
        if token is None or (token.deal_id or ""):
            return outcome  # сделки нет или она уже проставлена — авторитетен токен
        # Сделка вывода токена фиксируется ДО подмены deal_id (R19: тот же URL)
        if token.issue_deal_id is None:
            token.issue_deal_id = token.deal_id or ""
        token.deal_id = new_ref
        token_id, handoff_done = token.id, token.handoff_at is not None
        outcome["token_updated"] = True

        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == uid)
        )).scalar_one_or_none()
        if profile is not None and not (profile.deal_id or ""):
            profile.deal_id = new_ref

        outcome["form"] = await _merge_form_into_deal(session, uid, "", new_ref)
        await session.commit()

    logger.info("cabinet: сделка %s дозаписана в токен (user=%s, анкета=%s)",
                new_ref, uid, outcome["form"])
    if resend and handoff_done and token_id is not None:
        outcome["resent"] = await _resend_handoff_result(uid, token_id, new_ref, "adopt")
    return outcome


async def _copy_form_as_draft(session, uid: int, old_ref: str,
                              new_ref: str) -> Optional[str]:
    """Флейвор «новая заявка» (план 2026-08-21-002): данные анкеты переезжают
    ЧЕРНОВИКОМ — submitted_at сброшен, анкета по новой заявке редактируема и
    отправляема, фильтр R34 переносом не включается. Строка старой заявки
    остаётся в БД историей (архив CRM-заявки). Существующую строку новой
    сделки не перетираем. Commit — на вызывающей стороне."""
    old_form = await _load_form(session, uid, old_ref)
    if old_form is None:
        return None
    if await _load_form(session, uid, new_ref) is not None:
        return None  # у новой заявки уже есть своя анкета — авторитетна она
    draft = CabinetForm(user_id=uid, deal_id=new_ref)
    for field in FORM_FIELDS:
        setattr(draft, field, getattr(old_form, field))
    session.add(draft)
    return "copied"


# Окно гарда от отката: устаревший ctx живёт не дольше цикла охоты (~2 ч);
# старше окна кандидат легитимен (менеджер вернул сделку в работу — вечный
# гард приковывал бы кабинет к прошлой сделке навсегда, находка ревью)
REBOUND_GUARD_WINDOW = timedelta(hours=3)


async def _rebound_from_deals(token_id: int) -> set:
    """Сделки, С КОТОРЫХ токен уводили НЕДАВНО (события deal_rebound моложе
    REBOUND_GUARD_WINDOW) — гард от отката (КТР-3 плана 2026-08-21-002):
    устаревший ctx конкурентного хода не должен перепривязать токен назад
    на только что покинутую сделку."""
    cutoff = _utcnow() - REBOUND_GUARD_WINDOW
    async with database.AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(CabinetEvent.meta_json).where(
                CabinetEvent.token_id == token_id,
                CabinetEvent.event_type == "deal_rebound",
                CabinetEvent.created_at >= cutoff)
        )).scalars().all()
    out = set()
    for meta in rows:
        try:
            out.add(str(json.loads(meta or "{}").get("from") or ""))
        except (ValueError, TypeError):
            continue
    out.discard("")
    return out


async def _rebind_token_to_deal(uid: int, old_ref: str, new_ref: str,
                                flavor: str, payment=None) -> dict:
    """Общий хелпер смены сделки токена — два флейвора (план 2026-08-21-002,
    КТР-2; урок formula-parity: одна механика — явные области применения).

    'merge' — склейка ТОЙ ЖЕ заявки (deal hunt): перенос как есть — слияние
    анкеты с сохранением submitted_at, перепись pending_sync, переотправка
    результата при прошедшей передаче. Поведение бит-в-бит прежнего
    rebind_cabinet_on_merge (характеризация: tests/test_bot_integration.py).

    'new_deal' — НОВАЯ заявка при той же идентичности (повторная
    квалификация): токен/URL/bound_phone сохраняются, но жизненный цикл
    заявки сбрасывается — submitted_at (анкета переезжает черновиком),
    handoff_at (таймаут-каналы передачи оживают; часы bound_at
    перезапускаются, иначе триггер max_age выстрелил бы мгновенно по
    старой верификации), снапшот калькулятора и qualification_payment
    (:= платёж новой квалификации либо NULL — «первичная по заявке»).
    Перепись pending_sync и переотправка НЕ выполняются: старая сделка
    живёт своей жизнью, её хвосты — при ней."""
    outcome = {"token_rebound": False, "form": None,
               "pending_rewritten": 0, "resent": None}
    handoff_done = False
    token_id = None
    async with database.AsyncSessionLocal() as session:
        token = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == uid,
                                       CabinetToken.status == "active")
        )).scalars().first()
        if token is not None:
            token_id = token.id
            handoff_done = token.handoff_at is not None
            if (token.deal_id or "") == old_ref:
                # Сделка вывода фиксируется ДО подмены deal_id: иначе «тот же
                # URL» (R19) перестал бы выводиться и склейка отзывала бы
                # токен, выбрасывая клиента из открытого кабинета.
                if token.issue_deal_id is None:
                    token.issue_deal_id = token.deal_id or ""
                token.deal_id = new_ref  # токен сохраняется, меняется только deal_id
                outcome["token_rebound"] = True
                if flavor == "new_deal":
                    token.handoff_at = None
                    if token.bound_at is not None:
                        # Часы новой заявки: и таймаут-передачи (bound_at),
                        # и жизнь токена (expires_at — симметрично verify_code,
                        # находка ревью: иначе кабинет новой заявки умирал бы
                        # по остатку окна старой привязки)
                        token.bound_at = _utcnow()
                        token.expires_at = _utcnow() + TOKEN_TTL_BOUND
                    # Реестр однократных «на токен» событий — жизненный цикл
                    # на заявку (находка ревью, P1): пометка старой сделкой
                    # освобождает гарды с пустым meta (handoff_done, сигнал B,
                    # завершение сессии, stuck-алерты) для полного второго
                    # цикла передачи. deal_rebound не трогаем — источник
                    # гарда от отката.
                    lifecycle = ("handoff", "handoff_done", "handoff_signal_b",
                                 "handoff_stuck_alert", "session_closed",
                                 "session_farewell_sent",
                                 "session_close_stuck_alert")
                    ev_rows = (await session.execute(
                        select(CabinetEvent).where(
                            CabinetEvent.token_id == token.id,
                            CabinetEvent.event_type.in_(lifecycle))
                    )).scalars().all()
                    for ev in ev_rows:
                        try:
                            ev_meta = json.loads(ev.meta_json or "{}")
                        except (ValueError, TypeError):
                            ev_meta = {}
                        if "archived_deal" not in ev_meta:
                            ev_meta["archived_deal"] = old_ref
                            ev.meta_json = json.dumps(ev_meta, ensure_ascii=False)

        if flavor == "new_deal" and not outcome["token_rebound"]:
            # Токен не совпал со старой сделкой (конкурентная смена) —
            # сбрасывать профиль/снапшот по чужому состоянию нельзя
            await session.rollback()
            return outcome

        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == uid)
        )).scalar_one_or_none()
        if profile is not None:
            if flavor == "merge":
                if (profile.deal_id or "") == old_ref:
                    profile.deal_id = new_ref
            else:
                profile.deal_id = new_ref
                profile.calc_snapshot = None
                profile.calc_frozen_at = None
                try:
                    profile.qualification_payment = (
                        int(payment) if payment not in (None, "", 0, "0") else None)
                except (ValueError, TypeError):
                    profile.qualification_payment = None

        if flavor == "merge":
            outcome["form"] = await _merge_form_into_deal(session, uid, old_ref, new_ref)
            # pending_sync: params содержат старый deal_id — переписываем на основную
            rows = (await session.execute(
                select(PendingSync).where(PendingSync.status.in_(("pending", "fatal")))
            )).scalars().all()
            for row in rows:
                try:
                    params = json.loads(row.payload)
                except (ValueError, TypeError):
                    continue
                if _rewrite_deal_refs(params, old_ref, new_ref):
                    row.payload = json.dumps(params, ensure_ascii=False)
                    outcome["pending_rewritten"] += 1
        else:
            outcome["form"] = await _copy_form_as_draft(session, uid, old_ref, new_ref)
        try:
            await session.commit()
        except IntegrityError:
            # Конкурентная смена анкеты/сделки (фоновая склейка против хода) —
            # откат целиком, самоизлечение следующим ходом (находка ревью)
            await session.rollback()
            logger.warning("cabinet: перепривязка user=%s %s→%s не зафиксирована "
                           "(конкурентная операция) — повтор следующим ходом",
                           uid, old_ref, new_ref)
            return {"token_rebound": False, "form": None,
                    "pending_rewritten": 0, "resent": None}

    if outcome["token_rebound"] and token_id is not None:
        # Маркер «с какой сделки увели» — источник гарда от отката
        await _record_event_once(uid, token_id, "deal_rebound",
                                 {"from": old_ref, "to": new_ref, "flavor": flavor})

    if flavor != "merge" or not handoff_done or token_id is None:
        return outcome

    # Передача по старой сделке уже прошла — переотправка результата на основную
    # (иначе значения остались бы на удалённой сделке, а фильтр R34 запретил бы
    # чат-пути записать их на основную). Конверсия повторно НЕ запускается.
    outcome["resent"] = await _resend_handoff_result(uid, token_id, new_ref, "merge")
    return outcome


async def rebind_cabinet_on_merge(old_deal_id, new_deal_id, user_id) -> dict:
    """U7: завершение deal hunt — временная сделка склеена с основной.

    Склейка — техоперация (Key Technical Decisions): токен СОХРАНЯЕТСЯ,
    обновляется только deal_id. Коллизия уникальности cabinet_form
    (user_id, new_deal_id) решается правилом слияния (_form_merge_key);
    проигравшая строка помечается вытесненной суррогатным deal_id —
    IntegrityError посреди склейки не возникает. Строки pending_sync со
    старым deal_id переписываются на основную сделку. Если по старой сделке
    уже прошла передача заявки (handoff_at) — результат переотправляется на
    основную сделку; повторная конверсия НЕ запускается. Механика — в
    _rebind_token_to_deal (флейвор 'merge', план 2026-08-21-002)."""
    old_ref, new_ref = str(old_deal_id), str(new_deal_id)
    if not old_ref or not new_ref or old_ref == new_ref:
        return {"token_rebound": False, "form": None,
                "pending_rewritten": 0, "resent": None}
    uid = await _resolve_user_pk(user_id)
    if uid is None:
        return {"token_rebound": False, "form": None,
                "pending_rewritten": 0, "resent": None}
    return await _rebind_token_to_deal(uid, old_ref, new_ref, flavor="merge")


async def sync_cabinet_deal(user_id, dealer_code, deal_id, payment=None) -> bool:
    """[План 2026-08-21-002, U1/U4] Кабинет следует за новой заявкой.

    Вход триггера обеих точек: prepare_cabinet_context (payload-пути) и
    ветка B main.py (вернувшийся клиент с телефоном — INIT не выполняется).
    Гарды: нет активного токена / другой дилер (ветка revoke+reissue в
    prepare) / та же сделка → False; пустая сделка токена → штатное
    дозаполнение adopt_deal_id; кандидат из прошлых сделок токена
    (deal_rebound) → откат устаревшего ctx, пропуск. Возвращает True при
    перепривязке — вызывающий в prepare обязан перечитать active
    (иначе same_bundle с устаревшей сделкой отозвал бы токен)."""
    new_ref = str(deal_id) if deal_id else ""
    if not new_ref:
        return False
    uid = await _resolve_user_pk(user_id)
    if uid is None:
        return False
    async with database.AsyncSessionLocal() as session:
        token = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == uid,
                                       CabinetToken.status == "active")
        )).scalars().first()
    if token is None:
        return False
    if not dealer_code or token.dealer_code != dealer_code:
        # Пустой дилер (нет start_code) — fail-closed: кросс-дилерская
        # перепривязка без revoke запрещена; смена дилера — отдельная
        # ветка prepare (revoke+reissue)
        return False
    old_ref = token.deal_id or ""
    if not old_ref:
        # Токен без сделки — штатное дозаполнение (adopt), не перепривязка
        await adopt_deal_id(uid, new_ref)
        return True
    if old_ref == new_ref:
        return False
    if new_ref in await _rebound_from_deals(token.id):
        logger.info("cabinet: кандидат %s — прошлая сделка токена user=%s "
                    "(устаревший ctx конкурентного хода) — перепривязка пропущена",
                    new_ref, uid)
        return False
    outcome = await _rebind_token_to_deal(uid, old_ref, new_ref,
                                          flavor="new_deal", payment=payment)
    if outcome["token_rebound"]:
        logger.info("cabinet: токен user=%s перепривязан на новую заявку %s "
                    "(была %s) — жизненный цикл заявки сброшен", uid, new_ref, old_ref)
    return outcome["token_rebound"]


# =======================================================================
# Веб-вход (план 2026-09-30-001, U5): «открыть сессию» с лендинга без диалога
# =======================================================================

WEB_UNBOUND_TTL = timedelta(minutes=40)          # R63: веб-токен до привязки номера
WEB_OPEN_IP_HOURLY_LIMIT = 5                     # R42
WEB_BAD_NONCE_IP_HOURLY_LIMIT = 10               # R64: повторы с чужим nonce
WEB_DEALER_DAILY_CAP_DEFAULT = 100               # R43 (env CABINET_WEB_DEALER_DAILY_CAP); 0 — выключатель веб-входа
WEB_LIMIT_ALERT_INTERVAL = timedelta(hours=24)   # R44: техалерт не чаще раза в сутки
WEB_MILEAGE_MAX = 1_000_000                      # R15
WEB_SCENARIOS = ("credit", "tradein", "combo")
WEB_PLATFORM_PREFIX = "webcab:"
_LEAD_CODE_RE = re.compile(r"^\d{6}[A-Z]{4}$")   # дилер(2)+модель(2)+комплектация(2)+4 буквы (R52)
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{22,128}$")  # ≥128 бит в base64url/hex
_web_limit_alert_sent: dict = {}                 # (kind, dealer) -> datetime


def reset_web_limit_alerts() -> None:
    """Сброс дедупа техалертов лимитов веб-входа (тесты)."""
    _web_limit_alert_sent.clear()


def _web_ip_hourly_limit() -> int:
    """Лимит открытий веб-сессии с одного IP в час (R42): по умолчанию 5;
    env CABINET_WEB_IP_HOURLY_LIMIT — для дев-стенда (проверка лендинга);
    на бою переменная не задаётся. Неверное значение — умолчание."""
    try:
        value = int(os.getenv("CABINET_WEB_IP_HOURLY_LIMIT", str(WEB_OPEN_IP_HOURLY_LIMIT)))
    except ValueError:
        return WEB_OPEN_IP_HOURLY_LIMIT
    return value if value > 0 else WEB_OPEN_IP_HOURLY_LIMIT


def _dev_web_open_fault() -> bool:
    """Дев-стенд: искусственный сбой «открыть сессию» (503) для проверки
    экрана ошибки лендинга (L5) — пока существует файл из env
    CABINET_DEV_WEB_OPEN_FAULT_FILE. Только BOT_ENV=dev и ENVIRONMENT≠production
    (как подменный ответ оценки); на бою игнорируется."""
    path = (os.getenv("CABINET_DEV_WEB_OPEN_FAULT_FILE") or "").strip()
    if not path:
        return False
    if os.getenv("ENVIRONMENT", "").strip().lower() == "production":
        return False
    if os.getenv("BOT_ENV", "").strip().lower() != "dev":
        return False
    return os.path.exists(path)


def _web_dealer_daily_cap() -> int:
    try:
        return int(os.getenv("CABINET_WEB_DEALER_DAILY_CAP",
                             str(WEB_DEALER_DAILY_CAP_DEFAULT)))
    except ValueError:
        return WEB_DEALER_DAILY_CAP_DEFAULT


def cabinet_page_path() -> str:
    """Путь страницы кабинета на сайте дилера (R16): дев-бот — /cabinet_dev
    (env CABINET_PAGE_PATH), по умолчанию /cabinet."""
    path = (os.getenv("CABINET_PAGE_PATH") or "/cabinet").strip() or "/cabinet"
    return path if path.startswith("/") else "/" + path


def _parse_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _validate_web_open(body: dict) -> dict:
    """Разбор и проверка полей контракта «открыть сессию» (Contracts плана).
    Ошибки — ApiError(422 invalid_input с fields | invalid_plate)."""
    errors = {}
    dealer_code = str(body.get("dealer_code") or "").strip()
    lead_code = str(body.get("lead_code") or "").strip().upper()
    nonce = str(body.get("nonce") or "").strip()
    model_id = str(body.get("model_id") or "").strip()
    compl_id = str(body.get("complectation_id") or "").strip()
    scenario = str(body.get("scenario") or "").strip()
    if not re.fullmatch(r"\d{2}", dealer_code):
        errors["dealer_code"] = "invalid"
    if not _LEAD_CODE_RE.fullmatch(lead_code):
        errors["lead_code"] = "invalid"
    elif dealer_code and not lead_code.startswith(dealer_code):
        errors["lead_code"] = "dealer_mismatch"          # R53
    if not _NONCE_RE.fullmatch(nonce):
        errors["nonce"] = "invalid"
    if not model_id:
        errors["model_id"] = "required"
    if not compl_id:
        errors["complectation_id"] = "required"
    if scenario not in WEB_SCENARIOS:
        errors["scenario"] = "invalid"

    payment = mileage = plate = None
    if scenario in ("credit", "combo"):
        payment = _parse_int(body.get("desired_payment"))
        if payment is None or payment <= 0:
            errors["desired_payment"] = "invalid"
    if scenario in ("tradein", "combo"):
        mileage = _parse_int(body.get("mileage"))
        if mileage is None or not (0 <= mileage <= WEB_MILEAGE_MAX):
            errors["mileage"] = "invalid"
    form, form_error = _validate_web_form(body.get("form"))
    if form_error:
        errors["form"] = form_error
    if errors:
        raise ApiError(422, "invalid_input", fields=errors)
    if scenario in ("tradein", "combo"):
        from payload_v2 import normalize_plate_input  # строгий канон R14
        plate = normalize_plate_input(body.get("plate"))
        if plate is None:
            raise ApiError(422, "invalid_plate")
    return {"dealer_code": dealer_code, "lead_code": lead_code, "nonce": nonce,
            "model_id": model_id, "compl_id": compl_id, "scenario": scenario,
            "payment": payment, "mileage": mileage, "plate": plate,
            "form": form, "link": _form_value(form, "LINK")}


# Блок form (R62): поля формы Tilda как есть + UTM + cookies — только типы и
# размер, без проверки по смыслу
WEB_FORM_MAX_KEYS = 80
WEB_FORM_MAX_KEY_LEN = 64
WEB_FORM_MAX_VALUE_LEN = 2000
WEB_FORM_MAX_BYTES = 16 * 1024


def _form_value(form: dict, name: str):
    """Значение переменной формы Tilda без учёта регистра имени (в форме
    лендинга «Link», в таблице интеграции «LINK»)."""
    wanted = name.lower()
    for key, value in (form or {}).items():
        if key.lower() == wanted:
            return value
    return None


def _validate_web_form(form) -> tuple:
    """(блок, ошибка): объект «строка → строка» в пределах размеров, иначе
    ошибка поля form для invalid_input."""
    if form is None:
        return {}, "required"
    if not isinstance(form, dict) or len(form) > WEB_FORM_MAX_KEYS:
        return {}, "invalid"
    for key, value in form.items():
        if not (isinstance(key, str) and 0 < len(key) <= WEB_FORM_MAX_KEY_LEN) \
                or not isinstance(value, str) or len(value) > WEB_FORM_MAX_VALUE_LEN:
            return {}, "invalid"
    if len(json.dumps(form, ensure_ascii=False).encode("utf-8")) > WEB_FORM_MAX_BYTES:
        return {}, "too_large"
    return form, None


def _safe_link(link, website) -> Optional[str]:
    """«Link» из запроса (запасной для «Вход в ЛК»): относительный путь или
    https-адрес на домене Website_URL дилера; иначе None (не пишется)."""
    if not isinstance(link, str):
        return None
    link = link.strip()
    if not link or len(link) > 1000:
        return None
    if link.startswith("/") and not link.startswith("//"):
        return link
    parts = urlsplit(link)
    dealer_host = (_dealer_https_netloc(website) or "").lower()
    if parts.scheme == "https" and dealer_host and parts.netloc.lower() == dealer_host:
        return link
    return None


def _return_url(referer, origin, website) -> Optional[str]:
    """Страница лендинга для 8f (R69): Referer/Origin из whitelist Website_URL,
    без фрагмента; иначе корень Website_URL."""
    allowed = _dealer_origins()
    for candidate in (referer, origin):
        if not candidate:
            continue
        parts = urlsplit(str(candidate))
        if parts.scheme == "https" and f"https://{parts.netloc.lower()}" in allowed:
            return parts._replace(fragment="", path=parts.path or "/").geturl()
    netloc = _dealer_https_netloc(website)
    return f"https://{netloc}/" if netloc else None


async def _web_limit_alert(kind: str, dealer_code, text: str) -> None:
    key = (kind, str(dealer_code))
    now = _utcnow()
    last = _web_limit_alert_sent.get(key)
    if last is not None and now - last < WEB_LIMIT_ALERT_INTERVAL:
        return
    _web_limit_alert_sent[key] = now
    if _tech_alert:
        try:
            await _tech_alert(text)
        except Exception as e:
            logger.warning("cabinet: техалерт лимита веб-входа не отправлен: %s", e)


async def _count_web_opens(session, since, kind="open", ip=None, dealer_code=None) -> int:
    q = select(func.count()).select_from(database.CabinetWebOpen).where(
        database.CabinetWebOpen.kind == kind, database.CabinetWebOpen.created_at >= since)
    if ip is not None:
        q = q.where(database.CabinetWebOpen.ip == ip)
    if dealer_code is not None:
        q = q.where(database.CabinetWebOpen.dealer_code == dealer_code)
    return int((await session.execute(q)).scalar() or 0)


async def _record_web_open(ip, dealer_code, kind="open") -> None:
    async with database.AsyncSessionLocal() as session:
        session.add(database.CabinetWebOpen(ip=ip, dealer_code=dealer_code, kind=kind))
        await session.commit()


def _nonce_hash(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


async def _find_web_repeat(dealer_code: str, lead_code: str):
    """Профиль и ЖИВОЙ веб-токен ранее открытой сессии с тем же кодом.

    Решение владельца 2026-10-02: истёкший токен (неподтверждённый — 40 мин)
    повтором не считается — «открыть сессию» открывает НОВУЮ сессию (новый
    токен, тот же код и сделка, новый звонок, расход лимита), иначе цикл
    8f → «Вернуться на сайт» → «Далее» → 8f. Истечение проверяется по
    expires_at, а не по статусу: поведение не зависит от того, успела ли
    фоновая уборка (expire_tokens) перевести токен в expired."""
    now = _utcnow()
    async with database.AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(CabinetProfile, CabinetToken)
            .join(CabinetToken, CabinetToken.user_id == CabinetProfile.user_id)
            .where(CabinetProfile.lead_code == lead_code,
                   CabinetProfile.dealer_code == dealer_code,
                   CabinetToken.status == "active",
                   CabinetToken.entry_source == "web",
                   (CabinetToken.expires_at.is_(None)) | (CabinetToken.expires_at > now))
            .order_by(CabinetToken.id.desc())
        )).all()
    return rows[0] if rows else (None, None)


async def _find_tilda_deal(lead_code: str) -> Optional[str]:
    """Сделка Tilda по коду в «Новая» — только чтение (R13). Сбой — None
    (сессия открывается без сделки, конверсия ищет ещё раз)."""
    if _find_deal_by_code is None:
        return None
    try:
        deal = await _find_deal_by_code(lead_code)
    except Exception as e:
        logger.warning("cabinet: поиск сделки по коду %s не удался: %s", lead_code, e)
        return None
    deal_id = (deal or {}).get("ID") if isinstance(deal, dict) else None
    return str(deal_id) if deal_id else None


# Сериализация «открыть сессию» по (дилер, код заявки) — идемпотентность
# повтора (решение владельца 2026-10-02): параллельный запрос с тем же кодом
# ждёт первый и получает его сессию, а не создаёт вторую. Бот — один процесс
# aiohttp; запись открытия (профиль с кодом и хешем nonce) создаётся ДО
# медленных шагов.
_WEB_OPEN_LOCKS: dict = {}


class _WebOpenLock:
    def __init__(self, key):
        self.key = key

    async def __aenter__(self):
        entry = _WEB_OPEN_LOCKS.setdefault(self.key, [asyncio.Lock(), 0])
        entry[1] += 1
        self.entry = entry
        await entry[0].acquire()

    async def __aexit__(self, *exc):
        self.entry[0].release()
        self.entry[1] -= 1
        if self.entry[1] <= 0 and _WEB_OPEN_LOCKS.get(self.key) is self.entry:
            del _WEB_OPEN_LOCKS[self.key]
        return False


async def _prefetch_rates_program(dealer_code) -> None:
    """Параллельный прогрев таблиц ставок и программ дилера перед заморозкой
    снапшота (кэш загрузчика main; сбой — молча, freeze решит сам)."""
    await asyncio.gather(_load_dealer_table(dealer_code, "rates_table_id"),
                         _load_dealer_table(dealer_code, "program_table_id"),
                         return_exceptions=True)


async def _release_web_reservation(user_id: int) -> None:
    """Неудачное открытие: снять код с профиля-резерва — повтор откроет заново."""
    try:
        async with database.AsyncSessionLocal() as session:
            await session.execute(update(CabinetProfile)
                                  .where(CabinetProfile.user_id == user_id)
                                  .values(lead_code=None, lead_nonce_hash=None))
            await session.commit()
    except Exception:
        logger.exception("cabinet: снятие резерва веб-сессии user=%s не удалось", user_id)


async def open_web_session(body, ip: Optional[str] = None,
                           origin: Optional[str] = None,
                           referer: Optional[str] = None) -> dict:
    """POST /cabinet/api/web/open (U5): открыть веб-сессию и вернуть полный
    адрес кабинета. Порядок: проверка полей → [блокировка по коду] повтор по
    коду+nonce (R64) → лимиты (R42–R43) → резерв: пользователь и профиль с
    кодом и хешем nonce → ПАРАЛЛЕЛЬНО строка каталога, поиск сделки Tilda
    (одна попытка, таймаут — R13/R36), ставки и программа → снапшот → токен
    (TTL 40 мин) → запись открытия."""
    if _dev_web_open_fault():
        logger.warning("cabinet: дев-сбой «открыть сессию» включён (файл-флаг) — 503")
        raise ApiError(503, "cabinet_unavailable")
    fields = _validate_web_open(body if isinstance(body, dict) else {})
    dealer_code = fields["dealer_code"]
    dealers = _dealers_provider() if _dealers_provider else {}
    dealer = (dealers or {}).get(dealer_code)
    if not dealer:
        raise ApiError(404, "unknown_dealer")
    website = dealer.get("website")
    if _dealer_https_netloc(website) is None:
        raise ApiError(503, "cabinet_unavailable")

    # Выключатель (R43): кап 0 — веб-вход остановлен для всех дилеров (как
    # OTP-кап), включая повторы; лендинг показывает ошибку L5
    cap = _web_dealer_daily_cap()
    if cap <= 0:
        await _web_limit_alert("switch", "all",
                               "⚠️ Веб-вход в кабинет выключен: CABINET_WEB_DEALER_DAILY_CAP=0 — "
                               "сессии с лендинга не открываются.")
        raise ApiError(429, "rate_limited", limit_source="dealer")

    async with _WebOpenLock((dealer_code, fields["lead_code"])):
        return await _open_web_session_locked(fields, dealer_code, website, cap,
                                              ip, origin, referer)


async def _open_web_session_locked(fields, dealer_code, website, cap, ip, origin, referer):
    now = _utcnow()
    # Повтор (R64): тот же код + тот же nonce → тот же адрес, до и после
    # привязки номера; другой nonce → отказ и учёт в отдельном лимите по IP
    profile, token = await _find_web_repeat(dealer_code, fields["lead_code"])
    if profile is not None:
        if hmac.compare_digest(profile.lead_nonce_hash or "", _nonce_hash(fields["nonce"])):
            url = await get_active_cabinet_url(profile.user_id)
            if url:
                return {"cabinet_url": url}
        async with database.AsyncSessionLocal() as session:
            bad = await _count_web_opens(session, now - timedelta(hours=1),
                                         kind="bad_nonce", ip=ip)
        await _record_web_open(ip, dealer_code, kind="bad_nonce")
        if bad + 1 > WEB_BAD_NONCE_IP_HOURLY_LIMIT:
            raise ApiError(429, "rate_limited", limit_source="ip")
        raise ApiError(422, "invalid_input", fields={"nonce": "mismatch"})

    # Лимиты (R42–R43): IP за час, дилер за сутки
    async with database.AsyncSessionLocal() as session:
        ip_limit = _web_ip_hourly_limit()
        if ip and await _count_web_opens(session, now - timedelta(hours=1), ip=ip) \
                >= ip_limit:
            await _web_limit_alert("ip", dealer_code,
                                   f"⚠️ Веб-вход в кабинет: лимит {ip_limit} "
                                   f"открытий в час с одного IP (дилер {dealer_code}).")
            raise ApiError(429, "rate_limited", limit_source="ip")
        if await _count_web_opens(session, now - timedelta(hours=24),
                                  dealer_code=dealer_code) >= cap:
            await _web_limit_alert("dealer", dealer_code,
                                   f"⚠️ Веб-вход в кабинет: дилер {dealer_code} исчерпал "
                                   f"суточный лимит {cap} открытий сессий.")
            raise ApiError(429, "rate_limited", limit_source="dealer")

    # Резерв ДО медленных шагов: пользователь и профиль с кодом и хешем nonce
    # Веб-пользователь (R12): «Клиент», канал web
    puid = f"{WEB_PLATFORM_PREFIX}{uuid.uuid4().hex}"
    async with database.AsyncSessionLocal() as session:
        user = User(platform_user_id=puid, channel="web", bot_key="web",
                    raw_platform_id=puid, full_name="Клиент")
        session.add(user)
        await session.commit()
        await session.refresh(user)
    user_id = user.id
    await upsert_cabinet_profile(user_id, dealer_code=dealer_code,
                                 scenario_type=fields["scenario"], payment=fields["payment"])
    async with database.AsyncSessionLocal() as session:
        prof = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == user_id)
        )).scalar_one()
        prof.lead_code = fields["lead_code"]
        prof.lead_nonce_hash = _nonce_hash(fields["nonce"])
        prof.return_url = _return_url(referer, origin, website)
        prof.web_plate = fields["plate"]
        prof.web_mileage = fields["mileage"]
        prof.link_fallback = _safe_link(fields["link"], website)
        prof.web_form = json.dumps({"fields": fields["form"], "client_ip": ip},
                                   ensure_ascii=False)
        await session.commit()

    try:
        # Независимые медленные шаги — параллельно
        row_res, deal_id, _ = await asyncio.gather(
            load_catalog_row(dealer_code, fields["model_id"], fields["compl_id"]),
            _find_tilda_deal(fields["lead_code"]),
            _prefetch_rates_program(dealer_code),
            return_exceptions=True)
        if isinstance(row_res, CatalogUnavailable):
            raise ApiError(503, "cabinet_unavailable")
        if isinstance(row_res, BaseException):
            raise row_res
        row = row_res
        if isinstance(deal_id, BaseException):
            deal_id = None   # сделку найдёт конверсия (R13/R36)
        if row is None or compute_prices(row) is None:
            # Нет строки или нет цены (пустой/нулевой «Price old», поправка
            # 2026-09-30: TIGGO 4 PRO у ALLCAR и CHERY) — позиция недоступна
            raise ApiError(404, "unknown_position")

        # 3.4.1 (решение владельца 2026-10-06, вариант б): позиция, не активная
        # у дилера по его колонке, — сессия открывается, техалерт с дедупом
        await _alert_inactive_position(dealer_code, row, fields["model_id"], fields["compl_id"])
        await upsert_cabinet_profile(user_id, deal_id=deal_id, brand_model=catalog_item_name(row))
        if not await freeze_web_snapshot(user_id, dealer_code, row, fields["payment"]):
            raise ApiError(503, "cabinet_unavailable")
        url = await issue_cabinet_token(user_id, dealer_code, deal_id,
                                        entry_source="web", unbound_ttl=WEB_UNBOUND_TTL)
        if not url:
            raise ApiError(503, "cabinet_unavailable")
    except BaseException:
        await _release_web_reservation(user_id)
        raise
    await _record_web_open(ip, dealer_code)
    logger.info("cabinet: веб-сессия открыта user=%s дилер=%s сценарий=%s сделка=%s",
                user_id, dealer_code, fields["scenario"], deal_id or "—")
    return {"cabinet_url": url}


# =======================================================================
# Веб-вход (план 2026-09-30-001, U7): оценка трейд-ина через вебхук n8n
# =======================================================================

VALUATION_CACHE_TTL = timedelta(hours=24)            # R56
VALUATION_STALE_AFTER = timedelta(seconds=90)        # таймаут вызова 60 с + запас
WEB_OPENS_RETENTION = timedelta(hours=48)            # R71
VALUATION_ANONYMIZE_AFTER = timedelta(days=30)       # R71
VALUATION_SCENARIOS = ("tradein", "combo")
_valuation_client = None                             # async (plate, mileage, request_id) → ValuationResult


def set_valuation_client(client) -> None:
    """Подмена клиента вебхука оценки (тесты); None — valuation_client.evaluate."""
    global _valuation_client
    _valuation_client = client


def _valuation_call():
    if _valuation_client is not None:
        return _valuation_client
    import valuation_client
    return valuation_client.evaluate


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _HOOK_TASKS.add(task)
    task.add_done_callback(_HOOK_TASKS.discard)


async def _link_valuation(user_id: int, valuation_id: int) -> None:
    async with database.AsyncSessionLocal() as session:
        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == user_id)
        )).scalar_one_or_none()
        if profile is not None:
            profile.valuation_id = valuation_id
            await session.commit()


async def _apply_valuation_to_snapshot(user_id: int, row) -> None:
    """Результат оценки → снапшот сессии (только found_info/ti_valuation)."""
    if row.status not in ("success", "found_no_price"):
        return
    found_info = json.loads(row.found_info) if row.found_info else None
    await merge_valuation_into_snapshot(user_id, found_info, row.price_to)


async def start_valuation_after_verify(token_id: int) -> Optional[str]:
    """R26/R27/R56: после успешной авторизации веб-сессии трейд-ина или
    комбо — прошлый результат за 24 ч (тот же дилер и госномер), общая
    оценка в ожидании или новый фоновый вызов вебхука. Возвращает источник:
    'cache' | 'shared' | 'started' | None (оценка не нужна)."""
    async with database.AsyncSessionLocal() as session:
        token = await session.get(CabinetToken, token_id)
        if token is None or token.entry_source != "web":
            return None
        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == token.user_id)
        )).scalar_one_or_none()
        if (profile is None or profile.scenario_type not in VALUATION_SCENARIOS
                or not profile.web_plate or profile.valuation_id):
            return None
        user_id, dealer_code = token.user_id, token.dealer_code
        plate, mileage = profile.web_plate, profile.web_mileage
        now = _utcnow()
        cached = (await session.execute(
            select(database.CabinetValuation).where(
                database.CabinetValuation.dealer_code == dealer_code,
                database.CabinetValuation.plate == plate,
                database.CabinetValuation.status != "pending",
                database.CabinetValuation.our_failure.is_(False),
                database.CabinetValuation.finished_at > now - VALUATION_CACHE_TTL)
            .order_by(database.CabinetValuation.finished_at.desc()).limit(1)
        )).scalars().first()
    if cached is not None:
        await _link_valuation(user_id, cached.id)
        await _apply_valuation_to_snapshot(user_id, cached)
        logger.info("cabinet: оценка user=%s взята из кэша 24 ч (valuation=%s)",
                    user_id, cached.id)
        return "cache"

    request_id = str(uuid.uuid4())
    async with database.AsyncSessionLocal() as session:
        row = database.CabinetValuation(dealer_code=dealer_code, plate=plate,
                                        status="pending", request_id=request_id,
                                        token_id=token_id)
        session.add(row)
        try:
            await session.commit()
            valuation_id, source = row.id, "started"
        except IntegrityError:
            await session.rollback()   # уже идёт оценка этого номера у дилера
            pending = (await session.execute(
                select(database.CabinetValuation).where(
                    database.CabinetValuation.dealer_code == dealer_code,
                    database.CabinetValuation.plate == plate,
                    database.CabinetValuation.status == "pending")
            )).scalars().first()
            if pending is None:
                return None
            valuation_id, source = pending.id, "shared"
    await _link_valuation(user_id, valuation_id)
    if source == "started":
        _spawn(_run_valuation(valuation_id, plate, mileage, request_id))
    logger.info("cabinet: оценка user=%s: %s (valuation=%s)", user_id, source, valuation_id)
    return source


async def _run_valuation(valuation_id: int, plate: str, mileage, request_id: str) -> None:
    """Один вызов вебхука и раздача результата всем ожидающим сессиям."""
    try:
        result = await _valuation_call()(plate, mileage, request_id)
    except Exception as e:
        logger.exception("cabinet: вызов оценки valuation=%s упал", valuation_id)
        import valuation_client
        result = valuation_client.ValuationResult(
            "failed", f"сбой вызова оценки: {type(e).__name__}", True)
    await _finish_valuation(valuation_id, result)


async def _finish_valuation(valuation_id: int, result) -> None:
    async with database.AsyncSessionLocal() as session:
        row = await session.get(database.CabinetValuation, valuation_id)
        if row is None or row.status != "pending":
            return   # уже закрыта (например, воркером по таймауту)
        row.status = result.status
        row.reason = result.reason
        row.our_failure = bool(result.our_failure)
        row.found_info = (json.dumps(_https_proto_urls(result.found_info), ensure_ascii=False)
                          if result.found_info else None)
        row.price_from = result.price_from
        row.price_to = result.price_to
        row.finished_at = _utcnow()
        await session.commit()
        user_ids = list((await session.execute(
            select(CabinetProfile.user_id).where(CabinetProfile.valuation_id == valuation_id)
        )).scalars())
    for user_id in user_ids:
        try:
            await _apply_valuation_to_snapshot(user_id, row)
        except Exception:
            logger.exception("cabinet: слияние оценки в снапшот user=%s упало", user_id)
        try:
            await write_valuation_to_deal(user_id)   # U12: сессия уже передана
        except Exception:
            logger.exception("cabinet: запись оценки в сделку user=%s упала", user_id)
    logger.info("cabinet: оценка valuation=%s завершена: %s (%s), сессий %d",
                valuation_id, result.status, result.reason or "—", len(user_ids))


async def _expire_stale_valuations(now: datetime) -> int:
    """Обязанность воркера: оценка в ожидании дольше таймаута (рестарт бота,
    потерянная задача) → failed «таймаут», без нового вызова (R65)."""
    async with database.AsyncSessionLocal() as session:
        ids = list((await session.execute(
            select(database.CabinetValuation.id).where(
                database.CabinetValuation.status == "pending",
                database.CabinetValuation.started_at < now - VALUATION_STALE_AFTER)
        )).scalars())
    import valuation_client
    for valuation_id in ids:
        await _finish_valuation(valuation_id, valuation_client.ValuationResult(
            "failed", "оценка не завершилась (таймаут или рестарт)", True))
    return len(ids)


async def _cleanup_web_entry_data(now: datetime) -> dict:
    """R71: открытия старше 48 ч удаляются; госномер и данные авто в
    оценках старше 30 дней обезличиваются; блок form лендинга (UTM, cookies,
    IP клиента) стирается у веб-сессий, открытых больше 30 дней назад."""
    async with database.AsyncSessionLocal() as session:
        opens = await session.execute(
            database.CabinetWebOpen.__table__.delete().where(
                database.CabinetWebOpen.created_at < now - WEB_OPENS_RETENTION))
        anon = await session.execute(
            update(database.CabinetValuation)
            .where(database.CabinetValuation.anonymized_at.is_(None),
                   database.CabinetValuation.started_at < now - VALUATION_ANONYMIZE_AFTER)
            .values(plate=None, found_info=None, anonymized_at=now))
        old_web_users = select(CabinetToken.user_id).where(
            CabinetToken.entry_source == "web",
            CabinetToken.created_at < now - VALUATION_ANONYMIZE_AFTER)
        forms = await session.execute(
            update(CabinetProfile)
            .where(CabinetProfile.web_form.is_not(None),
                   CabinetProfile.user_id.in_(old_web_users))
            .values(web_form=None))
        await session.commit()
    return {"opens_deleted": opens.rowcount or 0, "valuations_anonymized": anon.rowcount or 0,
            "web_forms_cleared": forms.rowcount or 0}


# =======================================================================
# Веб-вход (план 2026-09-30-001, U10/U12): конверсия веб-сессии и оценка в сделке
# =======================================================================

WEB_CONVERSION_KEY = "web_entry"          # ключ снапшота конверсии веб-сессии
WEB_LEAD_TYPE_FIELD = "UF_CRM_65D5BD2A777E5"   # «Тип лида 1» (offer формы Tilda)
WEB_LEAD_TYPE_VALUE = "ЛК с сайта"
WEB_CHAT_ID_URL_FIELD = "UF_CRM_I2CRM_CHAT"    # «Чат ID» (url) — код, как у Tilda
WEB_MILEAGE_FIELD = "UF_CRM_65B0A4607EC41"     # «Trade-in - пробег» (MILEAGE формы Tilda)
# Поля о машине fallback-сделки: те же UF, что пишет интеграция Tilda (R61);
# источник — строка каталога по паре (решение владельца 2026-09-30)
WEB_DEAL_CAR_FIELDS = (
    ("UF_CRM_65B0A4605FA7C", "Model"),               # Модель
    ("UF_CRM_65B0A4606E9CA", "Model Modification"),  # Модификация
    ("UF_CRM_65B0A46077BCD", "Complectation"),       # Комплектация 1
    ("UF_CRM_67E4E4691A18E", "Price old"),           # Цена_прайс
    ("UF_CRM_67E4E4692881A", "Price new"),           # Цена_скидка
    ("UF_CRM_1708662321", "Link"),                   # Вход в ЛК
    ("UF_CRM_66068DB231026", "ID"),                  # Код комплектации (не подтверждено)
)
WEB_DEAL_PRICE_MONEY_FIELD = "UF_CRM_1745996090"     # Цена_прайс_ (деньги) — «N|RUB»


WEB_DEAL_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "cabinet_web_deal_map.json")
_web_deal_map_alerted = {"done": False}


def load_web_deal_map() -> Optional[dict]:
    """Таблица соответствий интеграции Tilda (R61) или None, если файл битый
    или отсутствует: сделка бота тогда создаётся без полей блока, техалерт."""
    try:
        with open(WEB_DEAL_MAP_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw.get("fields"), dict):
            raise ValueError("нет раздела fields")
        return raw
    except Exception as e:
        logger.error("cabinet: cabinet_web_deal_map.json не прочитан (%s) — "
                     "сделка бота без полей формы Tilda", e)
        return None


async def _alert_web_deal_map() -> None:
    if _web_deal_map_alerted["done"] or not _tech_alert:
        return
    _web_deal_map_alerted["done"] = True
    try:
        await _tech_alert("⚠️ Веб-вход: cabinet_web_deal_map.json не прочитан — "
                          "сделки бота создаются без полей формы Tilda (UTM, cookies, «Форма»).")
    except Exception as e:
        logger.warning("cabinet: техалерт о карте полей сделки не отправлен: %s", e)


def _mapped(table: dict, name: str):
    """Код поля по имени переменной Tilda (без учёта регистра)."""
    wanted = name.lower()
    for key, code in (table or {}).items():
        if key.lower() == wanted:
            return code
    return None


def web_deal_form_fields(mapping: Optional[dict], web_form: Optional[str],
                         bot_values: dict) -> tuple:
    """R61: (поля из блока form, поля-значения бота) по таблице соответствий.
    Блок — только переменные таблицы, кроме bot_owned; cookies — строковые
    поля с «;» на конце, целое поле — цифрами без «;». Значения бота — по
    именам переменных Tilda (срок, взнос, стоимость авто, пробег) и IP
    клиента. Без таблицы — ({}, {})."""
    if not mapping:
        return {}, {}
    try:
        stored = json.loads(web_form) if web_form else {}
    except (TypeError, ValueError):
        stored = {}
    form = stored.get("fields") if isinstance(stored.get("fields"), dict) else {}
    owned = {str(n).lower() for n in mapping.get("bot_owned") or []}
    table = mapping.get("fields") or {}
    block = {}
    for key, value in form.items():
        if not isinstance(value, str) or not value.strip() or key.lower() in owned:
            continue
        code = _mapped(table, key)
        if isinstance(code, str) and code:
            block[code] = value.strip()
        cookie_code = _mapped(mapping.get("cookie_string_fields"), key)
        if isinstance(cookie_code, str) and cookie_code:
            raw = value.strip()
            block[cookie_code] = raw if raw.endswith(";") else raw + ";"
        int_code = _mapped(mapping.get("cookie_integer_fields"), key)
        digits = value.strip().rstrip(";").strip()
        if isinstance(int_code, str) and int_code and digits.isdigit():
            block[int_code] = digits
    bot = {}
    for name, value in (bot_values or {}).items():
        code = _mapped(table, name)
        if isinstance(code, str) and code and value not in (None, ""):
            bot[code] = str(value)
    ip_code = mapping.get("client_ip_field")
    client_ip = stored.get("client_ip")
    if isinstance(ip_code, str) and ip_code and client_ip:
        bot[ip_code] = str(client_ip)
    return block, bot


def deal_test_mark() -> str:
    """Пометка тестовых сделок дев-стенда (env CABINET_DEAL_TEST_MARK, «TEST»);
    на бою переменная не задаётся — пусто."""
    return (os.getenv("CABINET_DEAL_TEST_MARK") or "").strip()


def web_deal_car_fields(row: dict, link_fallback: Optional[str]) -> dict:
    """UF полей о машине для fallback-сделки из строки каталога (R61)."""
    fields = {}
    for code, column in WEB_DEAL_CAR_FIELDS:
        value = (row or {}).get(column)
        if column == "Link" and value in (None, ""):
            value = link_fallback
        if value in (None, ""):
            continue
        num = _calc_num(value) if column.startswith("Price") else None
        fields[code] = str(num if num is not None else value)
    price_old = _calc_num((row or {}).get("Price old"))
    if price_old:
        fields[WEB_DEAL_PRICE_MONEY_FIELD] = f"{price_old}|RUB"
    return fields


async def _web_conversion_extras(profile, token) -> dict:
    """Ключи снапшота конверсии веб-сессии: то, что читает final_sync_wrapper
    и create_deal_with_contact. Сбой каталога — исключение (передача не
    засчитывается и повторяется следующим тиком)."""
    snap = _parse_calc_snapshot(profile) or {}
    row = await load_catalog_row(token.dealer_code, snap.get("model_id"), snap.get("item_id"))
    dealers = _dealers_provider() if _dealers_provider else {}
    website = ((dealers or {}).get(token.dealer_code) or {}).get("website")
    netloc = _dealer_https_netloc(website)
    extras = {
        "start_code": profile.lead_code or "",
        "model": (row or {}).get("Model") or snap.get("item_name") or "Автомобиль",
        "model_title": (row or {}).get("Complectation") or "",
        "scenario_type": profile.scenario_type,
        "site_source": f"https://{netloc}" if netloc else None,
        WEB_CONVERSION_KEY: {
            "dealer_code": token.dealer_code,
            "car_fields": web_deal_car_fields(row or {}, profile.link_fallback),
            "lead_type": WEB_LEAD_TYPE_VALUE,
            "test_mark": deal_test_mark(),
        },
    }
    if profile.qualification_payment:
        extras["payment"] = str(profile.qualification_payment)
    async with database.AsyncSessionLocal() as session:
        form = (await session.execute(
            select(CabinetForm).where(CabinetForm.user_id == profile.user_id)
            .order_by(CabinetForm.id.desc()).limit(1)
        )).scalar_one_or_none()
    # Госномер и пробег: значение кабинета (клиент поправил) — поверх лендинга
    # (решение владельца 2026-10-03); пишутся и в найденную сделку Tilda
    form_plate = str(getattr(form, "tradein_plate", None) or "").strip()
    plate = form_plate or profile.web_plate
    mileage = getattr(form, "tradein_mileage", None)
    if mileage is None:
        mileage = profile.web_mileage
    if plate:
        extras["plate"] = plate
    if mileage is not None:
        extras[WEB_CONVERSION_KEY]["mileage"] = str(mileage)
    mapping = load_web_deal_map()
    if mapping is None:
        await _alert_web_deal_map()
    price = None                      # «Кредит - Стоимость авто» — только для кредита и комбо
    if profile.scenario_type in ("credit", "combo"):
        price = snap.get("combo_price") if profile.scenario_type == "combo" else snap.get("price")
    credit_form = form if profile.scenario_type != "tradein" else None  # чистый трейд-ин — без кредита
    block, bot = web_deal_form_fields(mapping, profile.web_form, {
        "CREDIT_DURATION": getattr(credit_form, "credit_term", None),
        "DOWN_PAYMENT_RUB": getattr(credit_form, "credit_initial", None),
        "CREDIT_SUMM": _calc_num(price),
        "MILEAGE": mileage,
    })
    extras[WEB_CONVERSION_KEY]["form_fields"] = block
    extras[WEB_CONVERSION_KEY]["bot_fields"] = bot
    return extras


async def _valuation_for_user(user_id: int):
    async with database.AsyncSessionLocal() as session:
        profile = (await session.execute(
            select(CabinetProfile).where(CabinetProfile.user_id == user_id)
        )).scalar_one_or_none()
        if profile is None or not profile.valuation_id:
            return profile, None
        row = await session.get(database.CabinetValuation, profile.valuation_id)
    return profile, row


def _valuation_uf_fields(profile, row) -> dict:
    """UF оценки и данных авто: сумма (ti_valuation), руль, марка/модель/год
    из found_info — по тем же кодам, что анкета (карта cabinet_uf_map)."""
    uf_map = load_uf_map()
    fields = {}
    if row.price_to:
        fields[PROFILE_UF_MAP["ti_valuation"]] = str(row.price_to)
    found = json.loads(row.found_info) if row.found_info else {}
    for form_field, key in (("tradein_make", "mark"), ("tradein_model", "model"),
                            ("tradein_year", "year")):
        code = uf_map.get(form_field)
        if found.get(key) not in (None, "") and isinstance(code, str) and code:
            fields[code] = str(found[key])
    steering = found.get("steering_wheel")
    if steering:
        fields[PROFILE_UF_MAP["steering"]] = STEERING_CANON.get(str(steering), str(steering))
    return fields


async def write_valuation_to_deal(user_id: int) -> Optional[str]:
    """R57/R58/R67: оценка веб-сессии в сделку после передачи — однократно
    на токен, сделку и оценку. Успех: UF оценки и данных авто (+ комментарий,
    если оценка пришла после передачи; в комбо с отправленной раньше анкетой —
    пометка «расчёт в анкете — без зачёта»). Неуспех: строка «Оценка не
    получена: <причина>». Возвращает 'uf' | 'comment' | None."""
    async with database.AsyncSessionLocal() as session:
        token = (await session.execute(
            select(CabinetToken).where(CabinetToken.user_id == user_id,
                                       CabinetToken.status == "active")
        )).scalars().first()
        if token is None or token.entry_source != "web" or not token.deal_id:
            return None
        done = (await session.execute(
            select(CabinetEvent.created_at).where(
                CabinetEvent.token_id == token.id, CabinetEvent.event_type == "handoff_done")
        )).scalars().first()
        user = await session.get(User, user_id)
        form = await _load_form(session, user_id, token.deal_id)
    if done is None:
        return None   # до передачи оценка уходит вместе с анкетой (push_form_to_bitrix)
    profile, row = await _valuation_for_user(user_id)
    if row is None or row.status == "pending":
        return None
    deal_id, platform_uid = token.deal_id, (user.platform_user_id if user else None)
    if not await _record_event_once(user_id, token.id, "valuation_to_deal",
                                    {"deal_id": deal_id, "valuation": row.id}):
        return None
    submitted_at = _aware(form.submitted_at) if form is not None and form.submitted_at else None
    late = _aware(row.finished_at) > _aware(done) if row.finished_at else False
    if row.status in ("success", "found_no_price"):
        if submitted_at and not late:
            return None   # уже ушла с анкетой
        fields = _valuation_uf_fields(profile, row)
        if fields:
            await _bitrix_call_or_enqueue(
                "crm.deal.update", {"id": deal_id, "fields": fields}, platform_uid,
                f"cabinet: Bitrix crm.deal.update оценки упал (deal={deal_id})")
        if late and row.price_to:
            comment = f"Оценка трейд-ина получена после передачи заявки: {row.price_to} ₽."
            if profile.scenario_type == "combo" and submitted_at and submitted_at < _aware(row.finished_at):
                comment += " Расчёт в анкете — без зачёта."
            await _send_timeline_comment(deal_id, comment, platform_uid)
        return "uf"
    await _send_timeline_comment(deal_id, f"Оценка не получена: {row.reason or 'причина неизвестна'}",
                                 platform_uid)
    return "comment"
