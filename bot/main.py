from aiohttp import client_exceptions
import os
import asyncio
import hmac
import logging
import re
import time
import json
import gzip
import copy # [v1.34.4] Atomic Snapshot
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from collections import defaultdict

import aiohttp
from aiohttp import web
from google.cloud import dialogflow_v2 as dialogflow
from dotenv import load_dotenv

from sqlalchemy import or_, select, update
from database import init_db, AsyncSessionLocal, User, PendingSync, get_or_create_user, save_user_phone, is_valid_phone, record_sync_failure
from crm_mapping import (resolve_stage, resolve_callback_status, callback_stage,
                         is_duplicate_callback, register_callback)
import payload_v2
import cabinet_link_render
import shadow_resolver
import n8n_auth
import sms_gateway
import cabinet_service
import cabinet_api
import log_masking
import bot_identity
import bot_registry
from turn_locks import get_turn_lock

# Импорт клиентов каналов
from max_client import MaxBotClient, MAX_ERR_PERMANENT, MAX_ERR_CONTENT, MAX_ERR_TEMPORARY
from base_client import BaseBotClient
from telegram_client import TelegramBotClient
from web_client import WebBotClient

# --- Setup ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
# U9 (R16): фильтр маскирования — на ХЕНДЛЕРЫ корневого логгера, сразу после
# basicConfig; так маскируются и записи дочерних логгеров (aiohttp.access).
log_masking.install_masking()
logger = logging.getLogger(__name__)

def log_session(target, message: str, level: str = "INFO"):
    """
    Умный логгер. target может быть словарем (ctx) ИЛИ ID пользователя (max_user_id).
    Безопасно извлекает контекст, проверяя разные типы ключей (int/str).
    """
    # 1. Вывод в терминал (работает всегда)
    if level == "INFO":
        logger.info(message)
    elif level == "ERROR":
        logger.error(message)
    elif level == "WARNING":
        logger.warning(message)
        
    # 2. Автоматически определяем тип аргумента и извлекаем ctx
    ctx = None
    if isinstance(target, dict):
        ctx = target  # Если передали словарь ctx напрямую
    elif target is not None:
        # Защита от разности типов: проверяем ключ в исходном виде, как строку и как число
        ctx = USER_CONTEXT.get(target)
        if ctx is None:
            ctx = USER_CONTEXT.get(str(target))
        if ctx is None and str(target).isdigit():
            ctx = USER_CONTEXT.get(int(target))
            
    # 3. Сохранение в массив внутри контекста (если он существует)
    if ctx is not None and isinstance(ctx, dict):
        if 'python_logs' not in ctx:
            ctx['python_logs'] = []
            
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Журнал сессии уходит в БД и архив NocoDB мимо фильтра логгера —
        # секреты (адрес вебхука Битрикса, токены) маскируются здесь (2026-10-03)
        ctx['python_logs'].append(f"[{timestamp}] [{level}] {log_masking.mask_secrets(str(message))}")

# --- Config ---
# В мессенджере MAX ID групп и пользователей - это строки (хэши)
SUPPORT_GROUP_ID = os.getenv("MAX_SUPPORT_GROUP_ID", "")
TECH_ADMIN_ID = os.getenv("MAX_TECH_ADMIN_ID", "")

NOCODB_TOKEN = os.getenv("NOCODB_TOKEN")
NOCODB_API_URL = os.getenv("NOCODB_API_URL")
BITRIX_WEBHOOK_URL = os.getenv("BITRIX_WEBHOOK_URL")
NOCODB_TABLE_ID_CARS = os.getenv("NOCODB_TABLE_ID")
NOCODB_TABLE_ID_DEALERS = "muruk10ipof33m5" # [v1.26.1] Updated Dealer Table

# --- Гейт публичного прокси /catalog (решение владельца 2026-09-23) ---
# Прокси отдавал ЛЮБУЮ таблицу NocoDB серверным токеном (архив диалогов,
# Bots, промпты). Статичная тройка сайта (отчёт CE сайта) разрешена всегда —
# сайт не должен зависеть от успешной загрузки реестра дилеров; динамика —
# из db_config дилеров. Вне списка — 404 без подробностей.
CATALOG_STATIC_ALLOWED_TABLES = frozenset({
    "mywf0f16ptn25gk",   # TABLE_MAIN — основной каталог (= env NOCODB_TABLE_ID)
    "mm8wldgk5vn9gwh",   # TABLE_FEATURES — оснащение
    "muruk10ipof33m5",   # TABLE_DEALERS (= NOCODB_TABLE_ID_DEALERS)
})

# Whitelist публичных полей карточки дилера: сайт сверяет имена
# регистронезависимо и без учёта '_'/пробелов — сверка по нормализованному
# имени, наружу поле уходит с исходным именем. Всё прочее (Max_Group_ID,
# Telegram_Group_ID, db_config, промпты, Business_Connection_ID, Dealer_id…)
# вырезается.
DEALER_PUBLIC_FIELDS_NORM = frozenset({
    "dealercode", "dealerlocation", "dealername", "dealerstatus",
    "dealeraddress", "dealerworkinghours", "dealerphone",
})


def _norm_field_name(name) -> str:
    return re.sub(r"[\s_]+", "", str(name)).lower()


def catalog_allowed_tables() -> set:
    """Разрешённые /catalog-таблицы: статичная тройка + env NOCODB_TABLE_ID +
    ВСЕ id из `* table id`-колонок ВСЕХ строк Dealers (план 2026-09-25-001,
    КТД-1: инцидент Geely — оснащение лежало в колонке, которую код не читал).
    ПРАВИЛО ДАННЫХ: колонка Dealers с именем `* table id` = публичная таблица,
    её содержимое доступно в интернете (решение владельца 2026-09-25)."""
    allowed = set(CATALOG_STATIC_ALLOWED_TABLES)
    if NOCODB_TABLE_ID_CARS:
        allowed.add(str(NOCODB_TABLE_ID_CARS))
    for d in CACHED_DEALERS.values():
        allowed |= d.get('catalog_tables') or set()
    return allowed


# Таблицы `* table id`-колонок, виденные предыдущим прочтением Dealers.
# None = справочник ещё не читался (первое прочтение НЕ алертит — иначе шум
# из всех таблиц на каждом старте). Новая таблица в последующих прочтениях →
# техалерт владельцу: allowlist расширился (замена denylist, решение
# 2026-09-25); дедуп — членством в этом наборе.
_CATALOG_TABLES_SEEN = None


def _dealer_table_columns(row: dict) -> dict:
    """{table_id: имя колонки} для всех `* table id`-колонок строки Dealers
    (КТД-1: нормализованное имя оканчивается на 'tableid'; пустые/нестроковые
    значения пропускаются). База (`* base id`) — не таблица, не попадает."""
    return {v.strip(): k for k, v in row.items()
            if _norm_field_name(k).endswith("tableid")
            and isinstance(v, str) and v.strip()}


def _report_new_catalog_tables(catalog_tables_info: dict) -> None:
    """Техалерт о расширении allowlist (решение 2026-09-25, замена denylist):
    таблица, которой не было в предыдущем прочтении Dealers. Первое прочтение
    только инициализирует набор (иначе шум на каждом старте); дедуп —
    членством в _CATALOG_TABLES_SEEN."""
    global _CATALOG_TABLES_SEEN
    current = set(catalog_tables_info)
    if _CATALOG_TABLES_SEEN is not None:
        for tid in sorted(current - _CATALOG_TABLES_SEEN):
            col_name, dealer_code = catalog_tables_info[tid]
            logger.warning("TECH_ALERT [catalog-allow-new] таблица %s "
                           "(колонка %r, дилер %s) добавлена в allowlist",
                           tid, col_name, dealer_code)
            _spawn_bg(bot_registry.tech_alert(
                f"catalog-allow-new:{tid}",
                f"ℹ️ В allowlist /catalog добавлена таблица {tid} "
                f"(колонка «{col_name}», дилер {dealer_code}) — её "
                f"содержимое теперь публично. Если это не планировалось — "
                f"убрать значение из Dealers."))
    _CATALOG_TABLES_SEEN = current


def _dealer_public_row(row: dict) -> dict:
    """Карточка дилера для сайта: только whitelist-поля, исходные имена."""
    return {k: v for k, v in row.items()
            if _norm_field_name(k) in DEALER_PUBLIC_FIELDS_NORM}


def _dealer_column(row: dict, name: str):
    """Значение колонки Dealers по нормализованному имени (регистр, '_' и
    пробелы не важны) — новые колонки владельца (letter_title, form_type)."""
    wanted = _norm_field_name(name)
    for k, v in row.items():
        if _norm_field_name(k) == wanted:
            return v
    return None


# 3.4.1 (решение владельца 2026-10-06): колонка активности каталога — настройка
# дилера. Общий каталог (Geely: дилеры 02 и 09) несёт несколько колонок-флажков;
# дилер видит позиции, где взведена ЕГО колонка. Правило значения — cabinet_service.
CATALOG_ACTIVE_FIELD_DEFAULT = cabinet_service.CATALOG_ACTIVE_FIELD_DEFAULT


def _dealer_catalog_active_field(row: dict):
    """(колонка, неверное_значение | None) из Catalog_Active_Field строки Dealers."""
    return cabinet_service.parse_catalog_active_field(_dealer_column(row, "Catalog_Active_Field"))


def _dealer_landing_settings(row: dict) -> dict:
    """U13a (план 2026-09-30-001): настройки дилера для лендинга —
    GET /cabinet/api/dealer/{code}. WHITELIST: только то, что использует
    лендинг (решение владельца 2026-10-01); новые поля — по его решению."""
    return {
        "card": _dealer_public_row(row),
        "tables": {"catalog": row.get('Sourse table id'),
                   "catalog_active_field": _dealer_catalog_active_field(row)[0],
                   "features": row.get('Features table id'),
                   "rates": row.get('Rates_Table_ID')},
        "form_values": {"brand": row.get('Brand'),
                        "dealer": row.get('Dealer_name'),
                        "website": row.get('Website_URL'),
                        "letter_title": _dealer_column(row, "letter_title"),
                        "form_type": _dealer_column(row, "form_type")},
    }
NOCODB_TABLE_ID_PROMPTS = "mipsjod64tdgpee" # Таблица с блоками промптов
NOCODB_TABLE_ID_ARCHIVE = "mnivr6uyf5ckzru" # [v2.0] Таблица для архива диалогов

PROJECT_ID = os.getenv("DIALOGFLOW_PROJECT_ID")
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")

# Инициализация клиентов каналов. U9: токен легаси-клиента — по схеме
# окружения (новые имена переменных с фолбэком на дообменные, bot_registry)
max_client = MaxBotClient(token=bot_registry.legacy_env_token("max"))
tg_client = TelegramBotClient(token=bot_registry.legacy_env_token("telegram"))
web_client = WebBotClient()


def service_alert_client():
    """U9: служебный MAX-бот техалертов (админам/тех-группам, R5). Явно —
    env ALERT_BOT_KEY (прод: max-geely, дев: max-test); дефолт — легаси-ключ
    окружения; финальный фолбэк — глобальный max_client (env-токен): алерты
    не пропадают ни на одном шаге перехода."""
    key = os.getenv("ALERT_BOT_KEY", "").strip() or bot_registry.legacy_key_for_channel("max")
    client = bot_registry.get_bot_client(key) or max_client
    if bot_registry.current_env() == "dev":
        return _DevAlertClient(client)
    return client


DEV_ALERT_PREFIX = "[DEV] "


class _DevAlertClient:
    """Техалерты дев-стенда (BOT_ENV=dev) — с префиксом «[DEV]» (2026-10-02):
    дев и бой шлют в одну тех-группу, и без пометки алерт дева читался как
    боевой. Остальное — как у обёрнутого клиента."""

    def __init__(self, client):
        self.client = client

    async def send_alert_to_group(self, group_id, text):
        text = str(text or "")
        if not text.startswith(DEV_ALERT_PREFIX):
            text = DEV_ALERT_PREFIX + text
        return await self.client.send_alert_to_group(group_id=group_id, text=text)

    def __getattr__(self, name):
        return getattr(self.client, name)


async def _send_dealer_group_alert(dealer_config, dealer_code, text, deal_id=None) -> bool:
    """Правило дилер-алертов (решение владельца 2026-09-23): сообщение в
    группу дилера уходит ТОЛЬКО через MAX-бота БРЕНДА этого дилера ('*' у
    тестовых «все марки»); фолбэка на бота чужого бренда НЕТ. Бот бренда не
    найден или отправка не прошла → дедуп-техалерт админам (дилер, бренд,
    бот, сделка, причина), дилеру ничего; CRM-путь не зависит (R14).
    Фолбэк-группа поддержки (нет Max_Group_ID) — тех-группа, туда шлёт
    служебный бот."""
    dealer_config = dealer_config or {}
    dealer_group = dealer_config.get('Max_Group_ID')
    brand = str(dealer_config.get('brand') or '').strip().lower()
    if not dealer_group:
        if SUPPORT_GROUP_ID:  # тех-группа поддержки — служебным ботом
            _res = await service_alert_client().send_alert_to_group(
                group_id=SUPPORT_GROUP_ID, text=text)
            return _res is not None
        logger.error(f"❌ ROUTING FATAL: No Max_Group_ID found for dealer {dealer_code}")
        return False
    client, bot_key = bot_registry.find_brand_bot("max", brand)
    if client is None:
        reason = f"нет активного MAX-бота бренда {brand or '(бренд не заполнен)'}"
    else:
        _res = await client.send_alert_to_group(group_id=dealer_group, text=text)
        if _res is not None:
            return True
        reason = f"MAX API отказ (бот {bot_key}, статус в MAX_API_ERROR выше)"
    logger.warning("TECH_ALERT [dealer-alert-fail] дилер %s: %s", dealer_code, reason)
    _spawn_bg(bot_registry.tech_alert(
        f"dealer-alert-fail:{dealer_code}:{bot_key or brand or '-'}",
        f"🛑 Алерт дилеру {dealer_code} не доставлен: {reason}. Бренд {brand or '—'}, "
        f"бот {bot_key or '—'}, сделка {deal_id or '—'} — свяжитесь с дилером вручную."))
    return False

def get_client(channel: str) -> BaseBotClient:
    return {"max": max_client, "telegram": tg_client, "web": web_client}.get(channel, max_client)


def get_sender(user_id, ctx=None, channel=None):
    """U4 мультибота (KTD-6): (client, raw_id) для исходящих — по боту сессии.

    Резолв: ctx['bot_key'] → parse составного platform_user_id (самовосстановление
    для записей с потерянным bot_key) → легаси-маппинг по каналу (прежнее
    поведение, R7). Брендовый бот неизвестен/выключен/без токена → (None, raw):
    вызывающий код НЕ отправляет и не падает, CRM-путь и алерт дилеру идут
    как обычно (R14), фолбэка на чужого бота нет; дедуп-техалерт с дилером и
    сделкой уходит отсюда fire-and-forget."""
    ctx = ctx if ctx is not None else (USER_CONTEXT.get(user_id) or {})
    channel = channel or ctx.get('channel', 'max')
    parsed_key, raw_id = bot_identity.parse_internal_id(user_id)
    if channel == 'web':
        # через get_client: Р6 (web не меняется) + тестовые моки get_client
        return get_client('web'), raw_id
    bot_key = ctx.get('bot_key') or parsed_key
    if bot_key and bot_key not in bot_identity.LEGACY_BOT_KEYS:
        client = bot_registry.get_bot_client(bot_key)
        if client is None:
            dealer_prefix = (ctx.get('start_code') or '')[:2] or '—'
            logger.warning("TECH_ALERT [send-blocked] бот %s недоступен — отправка "
                           "пользователю %s пропущена (R14)", bot_key, user_id)
            _spawn_bg(bot_registry.tech_alert(
                f"send-blocked:{bot_key}",
                f"🛑 Отправка через бота {bot_key} невозможна (неактивен или вне реестра). "
                f"Дилер {dealer_prefix}, сделка {ctx.get('deal_id') or '—'}, клиент {user_id} — "
                f"свяжитесь с клиентом вручную (R14)."))
            return None, raw_id
        return client, raw_id
    return get_client(channel), raw_id


async def _brand_check(ctx, dealer_data, code_found):
    """U6 (R3/R15/R16, KTD-7): сверка «бренд дилера ∈ бренды бота» на активации
    диплинка. Несовпадение НЕ прерывает диалог — WARN + дедуп-техалерт (кулдаун
    1 час на пару дилер+бот); пустой Brand — сверка пропускается + алерт раз в
    сутки (R16); web вне сверки (R6). Дедуп/подтверждение доставки — общий
    механизм bot_registry.tech_alert (KTD-7: второй параллельный не заводим)."""
    ctx = ctx or {}
    if ctx.get('channel') == 'web':
        return
    bot_key = ctx.get('bot_key')
    entry = bot_registry.get_bot_entry(bot_key) if bot_key else None
    if entry is None:
        return  # реестр пуст или бот неизвестен — приём уже отфильтровал (KTD-4)
    if "*" in entry.brands:
        return  # U9: тестовый бот «все марки» — сверка бренда молчит (решение 2026-09-22)
    dealer_code = str(dealer_data.get('code') or code_found[:2])
    dealer_brand = str(dealer_data.get('brand') or '').strip().lower()
    dealer_name = dealer_data.get('dealer_name', '')
    if not dealer_brand:
        logger.warning("TECH_ALERT [brand-empty] у дилера %s не заполнен Brand — сверка пропущена",
                       dealer_code)
        await bot_registry.tech_alert(
            f"brand-empty:{dealer_code}",
            f"⚠️ У дилера {dealer_name} (код {dealer_code}) не заполнен Brand в NocoDB — "
            f"сверка бренда пропускается (R16)",
            cooldown=86400)
        return
    if dealer_brand not in entry.brands:
        logger.warning("TECH_ALERT [brand-mismatch] дилер %s (%s) не входит в бренды бота %s %s",
                       dealer_code, dealer_brand, bot_key, sorted(entry.brands))
        await bot_registry.tech_alert(
            f"brand-mismatch:{dealer_code}:{bot_key}",
            f"⚠️ Несоответствие бренда: дилер {dealer_name} (код {dealer_code}, бренд "
            f"{dealer_brand}), бот {bot_key} (бренды: {', '.join(sorted(entry.brands)) or '—'}), "
            f"сделка {ctx.get('deal_id') or '—'}. Диалог продолжается через бота входа (R3).",
            cooldown=3600)


def _archive_bot_key(ctx, db_user=None, user_id=None) -> str:
    """U5 (R19): Bot_Key для архива NocoDB — заполняется на ВСЕХ путях сборки
    archive_payload (learning ai-fields-only-on-ai-payload-paths). Порядок:
    ctx → users.bot_key → parse составного ID → легаси-ключ по каналу.
    U9: исторические max-main/tg-main резолвятся в ключ окружения."""
    if ctx and ctx.get('bot_key'):
        return bot_registry.resolve_alias(str(ctx['bot_key']))
    if db_user is not None and getattr(db_user, 'bot_key', None):
        return bot_registry.resolve_alias(str(db_user.bot_key))
    if user_id:
        parsed_key, _ = bot_identity.parse_internal_id(user_id)
        if parsed_key:
            return parsed_key
    channel = (ctx or {}).get('channel', 'max')
    if channel == 'web':
        return "web"
    if channel in ("max", "telegram"):
        return bot_registry.legacy_key_for_channel(channel)
    return ""


async def _send_typing_for(ctx, chat_id):
    """Typing-индикатор клиентом бота сессии; бот недоступен — тихо пропустить."""
    _cl, _ = get_sender('', ctx)
    if _cl is not None:
        await _cl.send_typing(chat_id=chat_id)

# --- Globals ---
CACHED_DEALERS: Dict[str, dict] = {} # {biz_id: {website, code, table_id, comm_type, db_config}}
DICT_STOCKS: Dict[str, List[dict]] = {} # {table_id: [cars]}
STOCK_LAST_UPDATED: Dict[str, float] = {} # {table_id: timestamp}

# Глобальный кэш промптов
# Структура: {"base_v01.00": {"credit": "...", "tradein": "...", ...}}
PROMPTS_CACHE: Dict[str, Dict[str, str]] = {}

CACHE_TTL = 600 # 10 minutes

# Битрикс (2026-10-02): таймаут на попытку и паузы перед попытками _request.
# Худший случай одного вызова: 3 × 10 с + 1 с + 3 с ≈ 34 с (раньше — до 900 с).
BITRIX_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=3)
BITRIX_RETRY_DELAYS = (0, 1, 3)
# Смена стадии (план 2026-10-12-001): Битрикс выполняет роботов стадии внутри
# crm.deal.update (штатно ~8 с, «Обогащение» → «Отправлено» + письма дилеру),
# поэтому отдельный таймаут 30 с — только для записи STAGE_ID.
BITRIX_STAGE_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=3)
# Д1: после сбоя записи стадии — паузы перед проверочными чтениями. Таймаут
# клиента не значит, что Битрикс закончил: первое чтение через 10 с, второе
# ещё через 20 с; стадия «было» в обоих — запись считается не применённой.
BITRIX_STAGE_SETTLE_DELAYS = (10, 20)
# Записей стадии на один вызов: первая + один повтор; дальше — pending_sync
BITRIX_STAGE_MAX_WRITES = 2
# Служебный ключ задачи pending_sync: стадия «было» для условной досылки
# (не уходит в Битрикс — реплей снимает его перед отправкой)
STAGE_EXPECT_KEY = "_expect_stage"
# «Обогащение» бот пишет только из стадий до передачи (план 2026-10-12-002,
# KTD-A1, решение В1): из «Отправлено» и дальше — никогда (робот стадии
# разослал бы письма дилеру повторно). LOSE/APOLOGY — вернувшийся клиент,
# живой лид. Код «Отправлено» не нужен: список — откуда можно.
ENRICH_SOURCE_STAGES = frozenset({
    "C9:NEW", "C9:PREPAYMENT_INVOICE", "C9:UC_CKY0IR", "C9:UC_HW8Q2H",
    "C9:UC_MDA6RZ", "C9:LOSE", "C9:APOLOGY",
})
# Замки записи «Обогащения» по сделке и финальной синхронизации по пользователю
# (KTD-A2/A3) — внутри процесса: на бою одна реплика бота (В6). При
# нескольких репликах они перестают защищать — нужен замок в БД.
_DEAL_STAGE_LOCKS: dict = {}
_FINAL_SYNC_LOCKS: dict = {}


def _deal_stage_lock(deal_id) -> asyncio.Lock:
    return _DEAL_STAGE_LOCKS.setdefault(str(deal_id), asyncio.Lock())


def _is_enrich_stage(stage) -> bool:
    """«Обогащение»: стадия передачи из env и карта колбэка n8n (visit/call)."""
    return bool(stage) and stage in (BitrixService.STAGE_QUALIFIED, resolve_stage("visit"))


def enrich_fields_allowed(fields: dict, current_stage: Optional[str], deal_id, where: str) -> dict:
    """Колбэк n8n и аварийный мост (решение В3): поля записи без STAGE_ID, если
    это «Обогащение» не из стадии до передачи. Стадия не прочитана — запись как
    раньше: у этих путей нет ни повтора, ни очереди, без записи заявка не дошла
    бы до дилера. Остальные стадии и поля — без изменений."""
    if (not _is_enrich_stage(fields.get("STAGE_ID")) or current_stage is None
            or current_stage in ENRICH_SOURCE_STAGES):
        if _is_enrich_stage(fields.get("STAGE_ID")) and current_stage is None:
            logger.warning(f"⚠️ [Bitrix Stage] {where} deal={deal_id}: стадия не прочитана — "
                           f"«Обогащение» пишется как раньше")
        return fields
    reason = "не из стадии до передачи"
    logger.warning(f"⏭ [Bitrix Stage] {where} deal={deal_id} {current_stage or '?'} → "
                   f"{fields['STAGE_ID']}: {reason} — стадия не пишется")
    return {k: v for k, v in fields.items() if k != "STAGE_ID"}


# Поиск сделки Tilda при открытии веб-сессии: одна попытка, короткий таймаут —
# неудача не страшна, сделку найдёт конверсия (R13/R36)
BITRIX_LOOKUP_TIMEOUT = aiohttp.ClientTimeout(total=2.5, connect=1.5)
# Прогрев таблиц ставок и программ дилеров: раньше истечения CACHE_TTL
RATES_PROGRAMS_WARM_INTERVAL = 300
USER_CONTEXT = defaultdict(dict)

# --- Глобальные переменные для склейки сообщений Smart-бота (Debounce) ---
USER_MESSAGE_BUFFERS = defaultdict(list)
USER_MESSAGE_TASKS = {}
SMARTBOT_HANDOFF_TASKS = {} # Таймеры для перевода на ИИ при молчании

# [P2 code-review #6] Сильные ссылки на fire-and-forget фоновые задачи —
# без этого asyncio может собрать GC незавершённую задачу (документированный
# анти-паттерн asyncio.create_task). Задача убирает себя сама по завершении.
_BG_TASKS: set = set()

CODE_REGEX = re.compile(r"\b(\d{2}[A-Z0-9]{6})\b", re.IGNORECASE)

# U7a: голый старт (bot_started без payload / TG /start без payload).
# Окно анти-дубля: артефакт клиентов «второй, голый /start после диплинка»
# (комментарий из прод-3.1.2) — внутри окна молчим, серия «Старт» подряд
# остаётся молчанием; окно меряется по снимку активности ДО текущего входа.
BARE_START_DEDUP_WINDOW = 120  # секунд

# Единый фолбэк «нет активной заявки» — используется защитой от «залётных»
# и гейтом голого старта (U7a): текст обязан совпадать.
FALLBACK_NO_CODE_TEXT = (
    "Здравствуйте! 🚗\n\n"
    "Чтобы я мог вас проконсультировать, пожалуйста, выберите интересующий вас автомобиль на сайте и нажмите кнопку связи."
)

CALLBACK_MAPPING = {
    "btn_credit": "В КРЕДИТ", "btn_tradein": "В ТРЕЙД-ИН",
    "btn_combo": "В КРЕДИТ + ТРЕЙД-ИН", "btn_no": "НЕ АКТУАЛЬНО",
    "btn_stock": "АВТО В НАЛИЧИИ", "btn_other": "ДРУГАЯ МОДЕЛЬ",
    "btn_consult": "КОНСУЛЬТАЦИЯ МЕНЕДЖЕРА", "btn_stop": "НИЧЕГО НЕ НУЖНО",
    "btn_tg": "В TELEGRAM", "btn_phone": "ПО ТЕЛЕФОНУ",
    "btn_continue_ai": "__SYSTEM_TIMEOUT_HANDOFF__",
    "btn_stop_ai": "НИЧЕГО НЕ НУЖНО"
}

MANAGER_TEMPLATES = {
    "credit": "Клиент попросил рассчитать для него кредит на покупку <b>{model}</b> с ежемесячным платежом <b>{payment} руб.</b>",
    "tradein": "Клиент попросил оценить его автомобиль (<b>{plate}</b>) для покупки <b>{model}</b> в Трейд-ин.",
    "combo": "Клиент попросил рассчитать для него кредит на покупку <b>{model}</b> с ежемесячным платежом <b>{payment} руб.</b> и оценить его автомобиль (Гос. номер: <b>{plate}</b>)",
    "stock": "Клиент попросил подобрать для него <b>{model}</b> в наличии.",
    "consult": "Клиенту требуется консультация менеджера по вопросу покупки <b>{model}</b>."
}

# =======================
# 1. SERVICES CLASS
# =======================

def format_chat_history(history: list) -> str:
    """Склеивает массив сообщений в стенограмму для таймлайна Битрикс24 (BBCode)"""
    if not history: return ""
    lines = ["[B]💬 История диалога с ботом:[/B]\n"]
    for msg in history:
        t = msg.get('time', '')
        role = msg.get('role', '')
        # Оставляем оригинальный текст (Битрикс сам поймет обычные \n)
        text = msg.get('text', '')
        
        if role == 'system':
            lines.append(f"⚙️ {t} [I]{text}[/I]")
        elif role == 'bot':
            lines.append(f"🤖 {t} [B]Бот:[/B] {text}")
        elif role == 'user':
            if msg.get('type') == 'button':
                lines.append(f"👤 {t} [B]Клиент:[/B] [Нажал кнопку: {text}]")
            else:
                lines.append(f"👤 {t} [B]Клиент:[/B] {text}")
                
    # Склеиваем сообщения двойным переносом для красивых отступов
    return "\n\n".join(lines)


BITRIX_READ_SUFFIXES = ("list", "get", "fields")
_EMPTY_DEAL_REFS = {"", "none", "null", "0", "false"}


def deal_ref(value) -> Optional[str]:
    """ID сделки строкой или None (2026-10-03): пустой ответ Битрикса раньше
    превращался в строку «None» (str(None)) — она уходила в active_deal_id,
    в n8n как deal_id и в Битрикс как ID сделки. Старые строки «None»/«null»
    в БД читаются как отсутствие сделки."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in _EMPTY_DEAL_REFS else text


def is_bitrix_read_method(method: str) -> bool:
    """Метод Битрикса только читает: crm.deal.list, crm.deal.get, crm.deal.fields…"""
    return str(method or "").rsplit(".", 1)[-1].lower() in BITRIX_READ_SUFFIXES


class BitrixService:
    WEBHOOK = os.getenv("BITRIX_WEBHOOK_URL")
    USER_ID = os.getenv("BITRIX_USER_ID", 1)
    
    WEB_DEAL_STAGE_NEW = "C9:NEW"   # сделка бота веб-входа создаётся в «Новая» (R34)
    F_CODE = os.getenv("BITRIX_FIELD_CODE")
    F_PAYMENT = os.getenv("BITRIX_FIELD_PAYMENT")
    F_TRADEIN = os.getenv("BITRIX_FIELD_TRADEIN")
    F_TYPE = os.getenv("BITRIX_FIELD_TYPE")
    F_CHANNEL = os.getenv("BITRIX_FIELD_CHANNEL")
    F_SITE_SOURCE = os.getenv("BITRIX_FIELD_SITE_SOURCE")
    F_CONTACT_USERNAME = os.getenv("BITRIX_FIELD_CONTACT_USERNAME")
    F_TRANSCRIPT = os.getenv("BITRIX_FIELD_TRANSCRIPT") # UF_CRM_1709041341
    
    STAGE_SUCCESS = os.getenv("BITRIX_STAGE_SUCCESS")

    @staticmethod
    def _channel_field(context: dict):
        """Гейт поля «Канал коммуникации (квалиф)» (план 2026-09-02-001, U2).

        В CRM уходит ровно одно значение — «По телефону»; любое другое
        (включая унаследованное «В MAX» из персистентных context_json)
        отсекается: None фильтруется перед отправкой, значение Тильды
        в сделке не затирается. Источник «По телефону» — единый предикат
        телефона в final_sync_wrapper/background_deal_hunt."""
        value = context.get('channel_val')
        return value if value == "По телефону" else None
    STAGE_QUALIFIED = os.getenv("BITRIX_STAGE_QUALIFIED")
    STAGE_NEW_BOT = os.getenv("BITRIX_STAGE_NEW_BOT")
    STAGE_MANUAL = os.getenv("BITRIX_STAGE_MANUAL")
    STAGE_LOSE = os.getenv("BITRIX_STAGE_LOSE", "LOSE")

    @staticmethod
    async def enqueue_failed_request(method: str, params: dict, user_id: str = None, skip_debounce: bool = False):
        """Сохраняет неудачный запрос в локальную БД (Очередь).

        skip_debounce=True (кабинет, R35) — постановка мимо 30-секундного
        дебаунса: не читает и не обновляет его таймстемп, чтобы не влиять
        на чат-путь того же пользователя и метода.

        Только записи (2026-10-02): чтение (*.list, *.get, *.fields) в очередь
        не ставится ни в одном пути — результат реплея никто не читает, а
        застрявшая строка доходила до fatal и техалерта."""
        if is_bitrix_read_method(method):
            logger.warning(f"⏭ [Persistence] Чтение {method} в очередь не ставится (User {user_id})")
            return
        try:
            deb_key = f"{user_id}_{method}"
            now = time.time()
            if not skip_debounce:
                last_time = getattr(BitrixService, f"_debounce_{deb_key}", 0)
                if now - last_time < 30:
                    return
                setattr(BitrixService, f"_debounce_{deb_key}", now)
            
            logger.error(f"🚨 [Persistence] Enqueueing failed request {method} for User {user_id}")
            async with AsyncSessionLocal() as session:
                new_task = PendingSync(
                    method=method,
                    payload=json.dumps(params, ensure_ascii=False),
                    user_id=user_id,
                    status="pending"
                )
                session.add(new_task)
                await session.commit()
        except Exception as e:
            logger.error(f"❌ [Persistence FATAL] Failed to save queue item: {e}")

    @classmethod
    async def _request(cls, method: str, params: dict, user_id: str = None,
                       enqueue_on_failure: bool = True, attempts: int = None,
                       timeout: aiohttp.ClientTimeout = None,
                       queue_expect_stage: str = None) -> dict:
        """enqueue_on_failure=False (кабинет) — исчерпанные ретраи НЕ ставят
        задачу в pending_sync здесь: кабинет ставит её сам недебаунсимым путём
        (R35), и своя постановка _request'а давала бы вторую строку на тот же
        вызов — дубль комментария в таймлайне при реплее. Чат-путь работает
        по умолчанию (True) и своё поведение сохраняет.

        attempts/timeout (план 2026-10-12-001) — одиночная попытка с таймаутом
        смены стадии для set_stage_guarded; queue_expect_stage — стадия «было»
        в задачу очереди (условная досылка)."""
        if not cls.WEBHOOK:
            logger.error("❌ Bitrix Webhook URL is missing!")
            return {}
        
        url = f"{cls.WEBHOOK}{method}"
        if user_id:
            log_session(user_id, f"🔄 [Bitrix] Starting request {method}")
        else:
            logger.info(f"🔄 [Bitrix] Starting request {method} for User {user_id}")
        
        total = attempts or len(BITRIX_RETRY_DELAYS)
        deal_ref_log = params.get("id") if isinstance(params, dict) else None
        for attempt in range(1, total + 1):
            started = time.monotonic()
            try:
                # attempts больше длины таблицы — последняя задержка (P3 ревью v3.4.2)
                delay = BITRIX_RETRY_DELAYS[min(attempt, len(BITRIX_RETRY_DELAYS)) - 1]
                if delay: await asyncio.sleep(delay)
                started = time.monotonic()

                # Таймаут на каждую попытку (2026-10-02): без него aiohttp ждёт
                # до 300 с, и зависший Битрикс держал бы вызов минутами
                http = (aiohttp.ClientSession(timeout=BITRIX_TIMEOUT) if timeout is None
                        else aiohttp.ClientSession(timeout=timeout))
                async with http as session:
                    async with session.post(url, json=params) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            
                            if user_id:
                                log_session(user_id, f"🔍 [Bitrix Response] {method} -> {result.get('result') or 'OK'}")
                            else:
                                logger.info(f"🔍 [Bitrix RAW Response] for {method}: {json.dumps(result, ensure_ascii=False)}")
                            
                            if 'error' not in result:
                                return result
                        raise Exception(f"API Error (Status {resp.status})")

            except Exception as e:
                # Тип и длительность (план 2026-10-12-001, U1): str() таймаута
                # aiohttp пустой — без типа строка не говорила, что случилось
                logger.warning(
                    f"⚠️ [Bitrix Retry] Attempt {attempt}/{total} failed for {method} "
                    f"deal={deal_ref_log or '—'}: {type(e).__name__}: {e} "
                    f"({time.monotonic() - started:.1f} с)")

        logger.error(f"❌ [Bitrix FATAL] Failed {method} after {total} attempts.")
        if enqueue_on_failure:
            queued = params
            if queue_expect_stage:
                queued = {**params, STAGE_EXPECT_KEY: queue_expect_stage}
            await cls.enqueue_failed_request(method, queued, user_id)
        return {}

    @classmethod
    async def get_deal_by_id(cls, deal_id: str, user_id: str = None) -> Optional[dict]:
        if not deal_id: return None
        logger.info(f"🔄 Getting Bitrix deal by ID: {deal_id}")
        res = await cls._request("crm.deal.get", {"id": deal_id}, user_id=user_id)
        result = res.get('result')
        if result:
            logger.info(f"✅ Retrieved Deal {deal_id} via ID lookup")
            return result
        return None

    @classmethod
    async def find_deal_data_by_code(cls, code: str, stage_id: str = None, user_id: str = None) -> Optional[dict]:
        if not code: return None
        filter_params = {cls.F_CODE: code}
        if stage_id:
            filter_params["STAGE_ID"] = stage_id
            logger.info(f"🔎 Searching deal by code: {code} STRICTLY in {stage_id}")
        else:
            logger.info(f"🔎 Searching deal by code: {code} (Any Stage)")

        params = {"filter": filter_params, "select": ["ID", "TITLE", "STAGE_ID", "CONTACT_ID"]}
        res = await cls._request("crm.deal.list", params, user_id=user_id)
        deals = res.get('result', [])
        if deals:
            logger.info(f"✅ Found Bitrix deal: {deals[0]['ID']}")
            return deals[0]
        return None
          
    @classmethod
    async def update_contact(cls, contact_id: str, name: str, phone: str, username: str, user_id: str = None):
        # Веб-вход (план 2026-09-30-001, U10) — правило для ВСЕХ путей:
        # R54: у контакта уже есть другой телефон — контакт не трогаем;
        # R35: «Клиент» не пишется поверх непустого имени контакта.
        current = (await cls._request("crm.contact.get", {"id": contact_id},
                                      user_id=user_id) or {}).get("result") or {}
        if not isinstance(current, dict):
            current = {}
        existing_phones = {_phone_tail(p.get("VALUE")) for p in (current.get("PHONE") or [])
                           if isinstance(p, dict) and _phone_tail(p.get("VALUE"))}
        if phone and existing_phones and _phone_tail(phone) not in existing_phones:
            logger.info(f"👤 [CRM] Контакт {contact_id} с другим телефоном — не перезаписываем (R54)")
            return
        fields = {
            "NAME": name,
            "LAST_NAME": "",
            "WEB": [{"VALUE": username if username else "", "VALUE_TYPE": "WORK"}]
        }
        existing_name = str(current.get("NAME") or "").strip()
        if name == "Клиент" and (existing_name not in ("", "Клиент") or not current):
            # имя неизвестно (сбой чтения) или уже настоящее — не затираем
            fields.pop("NAME"); fields.pop("LAST_NAME")
        if cls.F_CONTACT_USERNAME and username:
            fields[cls.F_CONTACT_USERNAME] = username
        if phone:
            fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "WORK"}]

        await cls._request("crm.contact.update", {"id": contact_id, "fields": fields}, user_id=user_id)

    @classmethod
    async def add_timeline_comment(cls, deal_id: str, comment_text: str, user_id: str = None,
                                   enqueue_on_failure: bool = True):
        """Новый метод: Отправляет HTML-комментарий в таймлайн сделки"""
        if not deal_id or not comment_text: return
        payload = {
            "fields": {
                "ENTITY_ID": deal_id,
                "ENTITY_TYPE": "DEAL",
                "COMMENT": comment_text
            }
        }
        await cls._request("crm.timeline.comment.add", payload, user_id=user_id,
                           enqueue_on_failure=enqueue_on_failure)

    @classmethod
    async def create_deal_with_contact(cls, name: str, phone: str, username: str, context: dict, code: str, user_id: str = None) -> str:
        if context.get(cabinet_service.WEB_CONVERSION_KEY):
            return await cls._create_web_deal(name, phone, context, code, user_id=user_id)
        contact_id = None
        if phone:
            res_c = await cls._request("crm.contact.list", {"filter": {"PHONE": phone}, "select": ["ID"]}, user_id=user_id)
            if res_c.get('result'):
                contact_id = res_c['result'][0]['ID']
                await cls.update_contact(contact_id, name, phone, username, user_id=user_id)
        if not contact_id:
            new_c_fields = {
                "NAME": name, 
                "LAST_NAME": "", 
                "PHONE": [{"VALUE": phone, "VALUE_TYPE": "WORK"}],
                "ASSIGNED_BY_ID": cls.USER_ID,
                "WEB": [{"VALUE": username if username else "", "VALUE_TYPE": "WORK"}]
            }
            if cls.F_CONTACT_USERNAME and username:
                new_c_fields[cls.F_CONTACT_USERNAME] = username
                
            new_c = await cls._request("crm.contact.add", {"fields": new_c_fields}, user_id=user_id)
            contact_id = new_c.get('result')

        car_model = context.get('model') or "Автомобиль"
        deal_fields = {
            "TITLE": f"Заявка из MAX: {car_model}",
            "CONTACT_ID": contact_id,
            "ASSIGNED_BY_ID": cls.USER_ID,
            "STAGE_ID": cls.STAGE_NEW_BOT,
            "COMMENTS": f"История диалога:\nМодель: {context.get('model') or 'Автомобиль'}\nКод: {code}",
            cls.F_CODE: code,
            cls.F_PAYMENT: context.get('payment'),
            cls.F_TRADEIN: context.get('plate'),
            # Кабинетная конверсия несёт готовый «Тип обращения» в снапшоте
            # (cabinet_service.CABINET_TYPE_KEY, план 2026-09-01-001); в
            # чатовых контекстах ключа не бывает — фолбэк сохраняет прежнее
            # поведение некабинетных сделок.
            cls.F_TYPE: context.get('cabinet_uf_type') or cls._map_type(context.get('scenario_type')),
            cls.F_CHANNEL: cls._channel_field(context),
            cls.F_SITE_SOURCE: context.get('site_source')
        }

        deal_fields = {k: v for k, v in deal_fields.items() if v is not None}
        res_d = await cls._request("crm.deal.add", {"fields": deal_fields}, user_id=user_id)
        deal_id = deal_ref((res_d or {}).get('result'))
        if deal_id is None:
            # Битрикс не вернул ID (сбой/таймаут/выключен): сделки нет — None,
            # а не строка «None»; active_deal_id остаётся пустым (2026-10-03)
            logger.error(f"❌ [Bitrix] crm.deal.add не вернул ID сделки для {name} — сделка не создана")
            return None
        logger.info(f"✨ [Bitrix] New Deal Created: {deal_id} for {name}")
        return deal_id

    @classmethod
    async def update_deal_data(cls, deal_id: str, context: dict, stage_id: str = None, user_id: str = None,
                               expect_stage: str = None):
        fields = {
            cls.F_PAYMENT: context.get('payment'),
            cls.F_TRADEIN: context.get('plate'),
            # Кабинетный «Тип обращения» при наличии, иначе прежний _map_type
            # (см. комментарий в create_deal_with_contact)
            cls.F_TYPE: context.get('cabinet_uf_type') or cls._map_type(context.get('scenario_type')),
            cls.F_CHANNEL: cls._channel_field(context),
            cls.F_SITE_SOURCE: context.get('site_source')
        }
        # Веб-заявка в найденную сделку Tilda: пробег рядом с госномером
        # (2026-10-03; чат-контексты блока веб-входа не несут)
        _web = context.get(cabinet_service.WEB_CONVERSION_KEY) or {}
        if isinstance(_web, dict) and _web.get("mileage") not in (None, ""):
            fields[cabinet_service.WEB_MILEAGE_FIELD] = _web["mileage"]
        if stage_id: fields["STAGE_ID"] = stage_id
        
        fields_to_send = {k: v for k, v in fields.items() if v is not None}
        logger.info(f"📤 Updating Deal {deal_id} with fields: {fields_to_send}")
        
        if fields_to_send:
            # expect_stage — «было» в задачу очереди при сбое (план 2026-10-12-001,
            # Р5): досылка не вернёт ушедшую дальше сделку назад
            res = await cls._request("crm.deal.update", {"id": deal_id, "fields": fields_to_send}, user_id=user_id,
                                     queue_expect_stage=expect_stage if stage_id else None)
            return bool(res)
        return True

    @classmethod
    async def _read_stage_once(cls, deal_id: str, user_id: str = None):
        """Проверочное чтение стадии: одна попытка, без очереди. None — чтение не удалось."""
        res = await cls._request("crm.deal.get", {"id": deal_id}, user_id=user_id,
                                 enqueue_on_failure=False, attempts=1)
        deal = (res or {}).get("result")
        if isinstance(deal, dict) and deal.get("STAGE_ID"):
            return str(deal["STAGE_ID"])
        return None

    @classmethod
    async def _enqueue_stage(cls, params: dict, expect_stage: Optional[str], user_id: str = None):
        """Стадия в pending_sync мимо дебаунса; с «было» досылка проверит её сама (U4)."""
        queued = {**params, STAGE_EXPECT_KEY: expect_stage} if expect_stage else params
        await cls.enqueue_failed_request("crm.deal.update", queued, user_id, skip_debounce=True)

    @classmethod
    async def set_stage_guarded(cls, deal_id: str, stage_id: str, expect_stage: Optional[str],
                                user_id: str = None) -> bool:
        """Условная смена стадии (план 2026-10-12-001, вариант А).

        Битрикс выполняет роботов стадии внутри crm.deal.update: таймаут
        клиента не значит, что запись не применилась (53859, 53893 — повтор
        вернул «Отправлено» в «Обогащение», письма дилеру ушли дважды).
        После сбоя попытки — проверочные чтения с паузами (Д1): стадия ушла
        с «было» → запись применилась или сделку сдвинули — повтора нет;
        стадия «было» в обоих чтениях → повтор. Чтение не удалось (Д2) или
        записи кончились — задача в pending_sync с «было», вслепую не пишем.
        «Было» неизвестно — одна попытка, при сбое очередь без ключа.

        «Обогащение» (план 2026-10-12-002, KTD-A2): под замком на сделку —
        свежее чтение стадии; пишется только из стадий до передачи
        (ENRICH_SOURCE_STAGES), прочитанная стадия и есть «было». Чтение не
        удалось — записи нет, задача в очередь (досылка применит то же правило).
        True — стадия поставлена (или уже ушла дальше); False — в очереди."""
        if not _is_enrich_stage(stage_id):
            return await cls._write_stage_guarded(deal_id, stage_id, expect_stage, user_id)
        async with _deal_stage_lock(deal_id):
            current = await cls._read_stage_once(deal_id, user_id=user_id)
            if current is None:
                logger.warning(f"⚠️ [Bitrix Stage] deal={deal_id} {expect_stage or '?'} → {stage_id}: "
                               f"чтение перед записью не удалось — в очередь")
                await cls._enqueue_stage({"id": deal_id, "fields": {"STAGE_ID": stage_id}},
                                         expect_stage, user_id)
                return False
            if current not in ENRICH_SOURCE_STAGES:
                logger.warning(f"⏭ [Bitrix Stage] deal={deal_id} {current} → {stage_id}: "
                               f"не из стадии до передачи — запись пропущена")
                return True
            return await cls._write_stage_guarded(deal_id, stage_id, current, user_id)

    @classmethod
    async def _write_stage_guarded(cls, deal_id: str, stage_id: str, expect_stage: Optional[str],
                                   user_id: str = None) -> bool:
        """Запись стадии с проверочными чтениями после сбоя (v3.4.2). Отмена
        задачи (рестарт бота) после начала записи — задача в очередь с «было»
        (KTD-A4): досылка прочитает стадию и не повторит применившуюся запись."""
        params = {"id": deal_id, "fields": {"STAGE_ID": stage_id}}
        tag = f"deal={deal_id} {expect_stage or '?'} → {stage_id}"
        writes = BITRIX_STAGE_MAX_WRITES if expect_stage else 1
        try:
            for write in range(1, writes + 1):
                res = await cls._request("crm.deal.update", params, user_id=user_id,
                                         enqueue_on_failure=False, attempts=1,
                                         timeout=BITRIX_STAGE_TIMEOUT)
                if res:
                    if write > 1:
                        logger.info(f"🎯 [Bitrix Stage] {tag}: повтор прошёл (запись {write})")
                    return True
                if not expect_stage:
                    break
                current = expect_stage
                for pause in BITRIX_STAGE_SETTLE_DELAYS:
                    await asyncio.sleep(pause)
                    current = await cls._read_stage_once(deal_id, user_id=user_id)
                    if current is None:
                        logger.warning(f"⚠️ [Bitrix Stage] {tag}: проверочное чтение не удалось — "
                                       f"в очередь с «было», без повтора записи")
                        await cls._enqueue_stage(params, expect_stage, user_id)
                        return False
                    if current != expect_stage:
                        break
                if current != expect_stage:
                    logger.warning(f"⏭ [Bitrix Stage] {tag}: после сбоя стадия {current} — "
                                   f"запись применилась или сделку сдвинули, повтор пропущен")
                    return True
                logger.warning(f"🔁 [Bitrix Stage] {tag}: после сбоя стадия всё ещё «было» "
                               f"(запись {write}/{writes})")
        except asyncio.CancelledError:
            logger.warning(f"🛑 [Bitrix Stage] {tag}: запись прервана (остановка) — в очередь с «было»")
            await asyncio.shield(cls._enqueue_stage(params, expect_stage, user_id))
            raise
        logger.error(f"❌ [Bitrix Stage] {tag}: не поставлена — в очередь")
        await cls._enqueue_stage(params, expect_stage, user_id)
        return False

    @classmethod
    async def set_stage_after_fields(cls, deal_id: str, stage_id: str, fields_ok: bool,
                                     user_id: str = None, expect_stage: str = None) -> bool:
        """Смена стадии — ПОСЛЕДНЯЯ запись в сделку (исправление 2026-09-30):
        робот стадии «Обогащение» собирает «Текст письма (квалиф)» из полей
        сделки в момент смены стадии, поэтому все поля пишутся раньше. Поля
        записаны — прямой вызов; поля ушли в очередь (сбой Битрикса) — стадия
        тоже в очередь, после них: мимо 30-секундного дебаунса (иначе та же
        пара «пользователь + crm.deal.update» проглотила бы стадию), реплей
        держит порядок по сделке."""
        if not (deal_id and stage_id):
            return True
        if fields_ok:
            # Условная смена (план 2026-10-12-001): ложный таймаут не повторяет запись
            return await cls.set_stage_guarded(deal_id, stage_id, expect_stage, user_id=user_id)
        params = {"id": deal_id, "fields": {"STAGE_ID": stage_id}}
        await cls._enqueue_stage(params, expect_stage, user_id)
        return False

    @classmethod
    async def _create_web_deal(cls, name: str, phone: str, context: dict, code: str,
                               user_id: str = None) -> str:
        """Веб-вход (план 2026-09-30-001, U10/R34/R61): сделка бота, когда
        сделка Tilda не нашлась: создаётся в воронке 9 в «Новая»; в
        «Обогащение» её переводит кабинет ПОСЛЕ записи полей анкеты
        (cabinet_service._set_handoff_stage — роботы обеих стадий, как у
        сделки Tilda, и письмо дилеру по заполненным полям); заголовок
        «Сделка #ID» (на деве с пометкой TEST). Поля: сначала
        значения бота, которые форма может уточнить (канал, сайт), затем блок
        form лендинга по cabinet_web_deal_map.json, поверх — поля, которые
        кабинет знает сам (машина из каталога, код, «Тип лида 1», «Тип
        обращения», платёж, срок, взнос, стоимость авто, трейд-ин, IP).
        crm.deal.add — без постановки в pending_sync: пустой ответ —
        исключение, передача повторяется следующим тиком (без «Сделка #None»);
        заголовок после создания — с постановкой в pending_sync, чтобы сбой
        не породил вторую сделку."""
        web = context[cabinet_service.WEB_CONVERSION_KEY]
        mark = web.get("test_mark") or ""
        contact_id = None
        if phone:
            res_c = await cls._request("crm.contact.list", {"filter": {"PHONE": phone}, "select": ["ID"]},
                                       user_id=user_id, enqueue_on_failure=False)
            if res_c.get('result'):
                contact_id = res_c['result'][0]['ID']
                await cls.update_contact(contact_id, name, phone, "", user_id=user_id)
        if not contact_id:
            new_c = await cls._request("crm.contact.add", {"fields": {
                "NAME": name, "LAST_NAME": "", "ASSIGNED_BY_ID": cls.USER_ID,
                "PHONE": [{"VALUE": phone, "VALUE_TYPE": "WORK"}] if phone else []}},
                user_id=user_id, enqueue_on_failure=False)
            contact_id = new_c.get('result')
            if not contact_id:
                raise RuntimeError("crm.contact.add веб-сделки: пустой ответ Битрикса")
        soft = {                                   # форма лендинга может уточнить
            cls.F_CHANNEL: cls._channel_field(context),
            cls.F_SITE_SOURCE: context.get('site_source'),
        }
        owned = {                                  # кабинет знает сам — поверх блока
            cls.F_CODE: code,
            cabinet_service.WEB_CHAT_ID_URL_FIELD: code,
            cabinet_service.WEB_LEAD_TYPE_FIELD: web.get("lead_type"),
            cls.F_PAYMENT: context.get('payment'),
            cls.F_TRADEIN: context.get('plate'),
            cls.F_TYPE: context.get('cabinet_uf_type') or cls._map_type(context.get('scenario_type')),
            **(web.get("car_fields") or {}),
            **(web.get("bot_fields") or {}),
        }
        deal_fields = {k: v for k, v in soft.items() if v not in (None, "")}
        deal_fields.update(web.get("form_fields") or {})
        deal_fields.update({k: v for k, v in owned.items() if v not in (None, "")})
        deal_fields.update({
            "TITLE": f"{mark} Сделка".strip(),
            "CATEGORY_ID": 9,
            "STAGE_ID": cls.WEB_DEAL_STAGE_NEW,
            "CONTACT_ID": contact_id,
            "ASSIGNED_BY_ID": cls.USER_ID,
            "COMMENTS": (f"{mark}: " if mark else "") + "Заявка из личного кабинета с сайта.",
        })
        res_d = await cls._request("crm.deal.add", {"fields": deal_fields}, user_id=user_id,
                                   enqueue_on_failure=False)
        deal_id = res_d.get('result')
        if not deal_id:
            raise RuntimeError("crm.deal.add веб-сделки: пустой ответ Битрикса — передача повторится")
        deal_id = str(deal_id)
        title = f"{mark + ' ' if mark else ''}Сделка #{deal_id}"
        await cls._request("crm.deal.update", {"id": deal_id, "fields": {"TITLE": title}},
                           user_id=user_id)
        logger.info(f"✨ [Bitrix] Web-entry deal created: {deal_id} ({title}), fields={sorted(deal_fields)}")
        return deal_id

    @classmethod
    async def final_sync_wrapper(cls, max_user_id: str, context_copy: dict, skip_tg: bool = False,
                                  skip_stage: bool = False):
        """Финальная синхронизация пользователя — по одной (план 2026-10-12-002,
        KTD-A3): вторая (второе сообщение клиента, конверсия кабинета) ждёт
        первую, а не пропускается — её поля могут быть новее. Стадию после
        ожидания отсекает свежее чтение (set_stage_guarded)."""
        lock = _FINAL_SYNC_LOCKS.setdefault(str(max_user_id), asyncio.Lock())
        if lock.locked():
            logger.info(f"⏳ [BG] Final Sync для {max_user_id} ждёт предыдущую")
        async with lock:
            return await cls._final_sync(max_user_id, context_copy, skip_tg=skip_tg,
                                         skip_stage=skip_stage)

    @classmethod
    async def _final_sync(cls, max_user_id: str, context_copy: dict, skip_tg: bool = False,
                          skip_stage: bool = False):
        """Фоновая синхронизация: отправка данных в CRM и уведомление менеджеров MAX."""
        try:
            logger.info(f"🌪 [BG] Starting Final Sync for {max_user_id}")
            
            async with AsyncSessionLocal() as session:
                res = await session.execute(select(User).where(User.platform_user_id == max_user_id))
                u = res.scalar_one_or_none()
                if not u: 
                    logger.error(f"❌ [BG] User {max_user_id} not found")
                    return
                
                u_name = str(u.full_name or "Клиент")
                u_phone = str(u.phone) if is_valid_phone(u.phone) else ""
                u_username = str(u.username or "")

                # Единое правило поля «Канал коммуникации (квалиф)» (план
                # 2026-09-02-001, ОВ-1): критерий один — валидный телефон.
                # Есть — «По телефону»; нет — ключ не уходит, и значение
                # Тильды (web/max/telegram) в сделке остаётся нетронутым.
                # Правило общее для чата и кабинетной конверсии: оба пути
                # идут через этот враппер, телефон берётся из той же строки
                # User, что уходит в контакт CRM (для кабинета это
                # OTP-подтверждённый номер).
                if u_phone:
                    context_copy['channel_val'] = "По телефону"
                else:
                    context_copy.pop('channel_val', None)
                active_deal_id = deal_ref(u.active_deal_id)
                code = context_copy.get('start_code')

                is_refusal = context_copy.get('scenario_type') in ["refusal", "other_model"]
                target_stage = "C9:LOSE" if is_refusal else cls.STAGE_QUALIFIED
                if skip_stage:
                    target_stage = None

            deal_data = None
            if active_deal_id:
                deal_data = await cls.get_deal_by_id(str(active_deal_id), user_id=max_user_id)
            if not deal_data and code:
                deal_data = await cls.find_deal_data_by_code(code, stage_id="C9:NEW", user_id=max_user_id)

            # Флаг для запуска охотника
            created_new_deal_id = None 
            stage_deal_id = None       # найденная сделка: стадия ставится последней
            stage_before = None
            fields_ok = True
            target_deal_id_for_comment = None # ID сделки, куда будем писать лог

            if deal_data:
                original_id = str(deal_data['ID'])
                logger.info(f"✅ [BG] Updating Deal {original_id}")
                if deal_data.get('CONTACT_ID'):
                    await cls.update_contact(deal_data['CONTACT_ID'], u_name, u_phone, u_username, user_id=max_user_id)
                # Стадия — ПОСЛЕДНЕЙ, после всех полей (ниже): робот стадии
                # собирает письмо дилеру из полей сделки (исправление 2026-09-30)
                fields_ok = await cls.update_deal_data(original_id, context_copy, None, user_id=max_user_id)
                stage_deal_id = original_id
                stage_before = deal_data.get('STAGE_ID')   # «было» для условной смены стадии
                
                # --- [ИСПРАВЛЕНО] Сохраняем найденный ID в базу бота ---
                async with AsyncSessionLocal() as session_fix:
                    await session_fix.execute(
                        update(User).where(User.platform_user_id == max_user_id).values(active_deal_id=original_id)
                    )
                    await session_fix.commit()
                target_deal_id_for_comment = original_id
            else:
                logger.info(f"✨ [BG] Creating NEW Deal")
                new_id = deal_ref(await cls.create_deal_with_contact(
                    u_name, u_phone, u_username, context_copy, code, user_id=max_user_id))
                created_new_deal_id = new_id
                target_deal_id_for_comment = new_id

                if new_id:  # нет ID — active_deal_id не трогаем (пустой), охоты нет
                    async with AsyncSessionLocal() as session_final:
                        await session_final.execute(
                            update(User).where(User.platform_user_id == max_user_id).values(active_deal_id=new_id)
                        )
                        await session_final.commit()

            # --- [План 2026-09-08-001, U2] SB-поля трейд-ина из слотов защёлки ---
            # Точка 1 (KTD-12): закрывает пути без ai_payload — таймаут кабинета,
            # воронковые финализации. R34-фильтр обязателен: после сабмита анкеты
            # SB-семьёй владеет кабинет. Лог состава — наблюдаемость (KTD-17).
            if target_deal_id_for_comment:
                _ai_fields = build_ai_tradein_fields(context_copy)
                if _ai_fields:
                    _ai_fields = await cabinet_service.filter_cabinet_owned_fields(
                        _ai_fields, target_deal_id_for_comment)
                if _ai_fields:
                    logger.info("📤 AI_TRADEIN_FIELDS deal=%s source=slots fields=%s",
                                target_deal_id_for_comment, _ai_fields)
                    try:
                        _ai_res = await cls._request("crm.deal.update",
                                                     {"id": target_deal_id_for_comment,
                                                      "fields": _ai_fields},
                                                     user_id=max_user_id)
                        fields_ok = fields_ok and bool(_ai_res)
                    except Exception as _ai_err:
                        fields_ok = False
                        logger.error(f"❌ [BG] Запись AI-полей трейд-ина не прошла: {_ai_err}")

            # --- Смена стадии — после ВСЕХ полей (исправление 2026-09-30) ---
            if stage_deal_id and target_stage:
                await cls.set_stage_after_fields(stage_deal_id, target_stage, fields_ok,
                                                 user_id=max_user_id, expect_stage=stage_before)

            # --- Веб-вход (U10): пометка тестовой сделки дев-стенда ---
            _web = context_copy.get(cabinet_service.WEB_CONVERSION_KEY) or {}
            if _web.get("test_mark") and target_deal_id_for_comment and not created_new_deal_id:
                await cls.add_timeline_comment(
                    target_deal_id_for_comment,
                    f"{_web['test_mark']}: сделка обработана веб-входом дев-стенда.",
                    user_id=max_user_id, enqueue_on_failure=False)

            # --- ОТПРАВКА СТЕНОГРАММЫ В ТАЙМЛАЙН ---
            history = context_copy.get('chat_history', [])
            if history and target_deal_id_for_comment:
                html_comment = format_chat_history(history)
                await cls.add_timeline_comment(target_deal_id_for_comment, html_comment, user_id=max_user_id)
                logger.info(f"✅ [BG] Timeline comment sent to Deal {target_deal_id_for_comment}")

            channel = context_copy.get('channel_val', 'Не указан')
            logger.info(f"🔔 [BG] Preparing notification card for {max_user_id}. Channel: {channel}")
            
            try:
                if not skip_tg:
                    _ok = await send_alert_to_max_group(max_user_id, context_copy)
                    if _ok:
                        logger.info(f"✅ [BG] Notification card sent for {max_user_id}")
                    else:
                        logger.error(f"❌ [BG] Notification card NOT delivered for {max_user_id} (см. Alert error выше)")
                else:
                    logger.info(f"🔇 [BG] MAX notification SKIPPED (Scenario: {context_copy.get('scenario_type')})")
            except Exception as e:
                logger.error(f"❌ [BG] Notification failed for {max_user_id}: {e}")

            logger.info(f"🏁 [BG] Final Sync Complete for {max_user_id}")
            
            # Возвращаем ID новой сделки (или None, если обновили старую)
            return created_new_deal_id

        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            logger.error(f"❌ [BG FATAL] Crash in final_sync_wrapper for {max_user_id}:\n{error_details}")
            if context_copy.get(cabinet_service.WEB_CONVERSION_KEY):
                # Веб-вход (U10): сбой конверсии — наружу, чтобы кабинет не
                # засчитал передачу и повторил её следующим тиком
                raise
            return None

    @classmethod
    async def close_as_duplicate(cls, deal_id: str, original_id: str):
        await cls._request("crm.deal.update", {
            "id": deal_id, 
            "fields": {"STAGE_ID": cls.STAGE_LOSE, "COMMENTS": f"Дубль. Объединено с ID: {original_id}"}
        })

    @classmethod
    async def move_to_manual(cls, deal_id: str):
        await cls._request("crm.deal.update", {
            "id": deal_id, 
            "fields": {"STAGE_ID": cls.STAGE_MANUAL, "COMMENTS": "⚠️ Авто-поиск оригинала не дал результатов."}
        })

    @staticmethod
    def _map_type(scenario: str) -> Optional[str]:
        mapping = {
            "credit": "Расчет кредита", "tradein": "Оценка авто в трейд-ин",
            "combo": "Расчет кредита и Оценка авто", "stock": "Подбор авто в наличии",
            "consult": "Консультация менеджера",
            "refusal": "Отказ",
            "other_model": "Другая модель"
        }
        return mapping.get(scenario)


# =======================
# 2. HELPER FUNCTIONS & AI TRANSPORT
# =======================

async def _web_delayed_typing(user_id: str, delay: float = 2.0):
    """Показывает typing indicator в web-виджете через delay секунд."""
    await asyncio.sleep(delay)
    await web_client.send_typing(user_id)

# --- Ссылка кабинета в исходящем тексте (план 2026-08-19-001, U1) ---
async def _get_cabinet_url_for_platform(platform_user_id: str):
    """(url, err) для подстановки [CABINET_LINK]: read-only запрос в момент
    отправки (КТР-5 — без стэша, живой токен не персистируется). err не None —
    сбой сервиса; url is None при err is None — кабинет не настроен/токена нет."""
    try:
        async with AsyncSessionLocal() as session:
            user = (await session.execute(
                select(User).where(User.platform_user_id == str(platform_user_id))
            )).scalar_one_or_none()
        if user is None:
            return None, None
        return await cabinet_service.get_active_cabinet_url(user.id), None
    except Exception as e:
        logger.exception(f"cabinet-link: URL для подстановки не получен ({platform_user_id})")
        return None, e


async def normalize_bot_text_for_history(platform_user_id: str, bot_text: str) -> str:
    """[U1] Текст бота для chat_history: [CABINET_LINK] → голый URL — n8n
    (recent_history, last_bot_message) видит фактическое состояние, служебный
    маркер не утекает (R6). Вызывается во всех четырёх точках записи истории."""
    if not cabinet_link_render.has_marker(bot_text):
        return bot_text
    url, _err = await _get_cabinet_url_for_platform(platform_user_id)
    return cabinet_link_render.normalize_for_history(bot_text, url)


# --- Согласование сделки диалога при склейке (план 2026-08-21-002, U2) ---
async def sync_dialog_deal_refs(platform_user_id: str, old_ref: str, new_ref: str) -> None:
    """Деал-хант закрыл бот-дубль old_ref в пользу оригинала new_ref:
    active_deal_id и ctx['deal_id'] переводятся на оригинал С ЗЕРКАЛЬНЫМ
    ГАРДОМ (только если текущее значение — закрываемый дубль или пусто).
    Охота длится до 2 часов — поздний финиш не должен перетереть сделку
    свежей повторной квалификации. Вызывается ДО rebind токена, чтобы
    источники «сделки диалога» не расходились с токеном (КТР-6 плана)."""
    async with AsyncSessionLocal() as session:
        # Атомарный зеркальный гард (находка ревью: check-then-update имел
        # TOCTOU-окно против конкурентной повторной квалификации)
        await session.execute(
            update(User).where(
                User.platform_user_id == str(platform_user_id),
                or_(User.active_deal_id.is_(None), User.active_deal_id == "",
                    User.active_deal_id == old_ref))
            .values(active_deal_id=new_ref))
        await session.commit()
    ctx = USER_CONTEXT.get(str(platform_user_id))
    if ctx is not None and str(ctx.get('deal_id') or '') in ('', old_ref):
        ctx['deal_id'] = new_ref
        await save_context_to_db(str(platform_user_id), ctx)


# --- Граница передачи ИИ (план 2026-08-21-001, U1) ---
async def mark_ai_handoff(max_user_id: str, ctx_snapshot: dict) -> None:
    """Маркер границы «дальше — диалог с Александром» в ОБА объекта контекста.

    Снапшот deepcopy снят ДО этой точки, а INIT-payload читает именно его —
    без append в снапшот срез на INIT не нашёл бы границу (кросс-находка
    ревью плана). Живой контекст сохраняется немедленно: штатный save после
    передачи условный (только при успешном ответе n8n) — маркер обязан
    пережить и сбой INIT. Прецедент двойного проброса — deal_id ниже."""
    marker = payload_v2.make_handoff_marker(datetime.now().strftime("%H:%M"))
    ctx_snapshot.setdefault('chat_history', []).append(dict(marker))
    USER_CONTEXT[max_user_id].setdefault('chat_history', []).append(dict(marker))
    await save_context_to_db(max_user_id, USER_CONTEXT[max_user_id])


# --- Уведомления MAX ---
# Пометка «диалог клиента в MAX закрыт» (план 2026-10-12-002, KTD-C2): MAX
# отвечает 403/404 — повторы и напоминания клиенту останавливаются до его
# нового сообщения или старта (process_input снимает пометку).
MAX_SEND_BLOCKED_KEY = "max_send_blocked"


async def send_client_message_detailed(client, uid, text, keyboard=None):
    """Отправка клиенту с причиной отказа: (ответ, ошибка). Клиент без
    подробной отправки или подменённый send_message — прежний путь, отказ
    считается временным."""
    detailed = getattr(client, "send_message_detailed", None)
    if detailed is None or "send_message" in vars(client):
        result = await client.send_message(user_id=uid, text=text, keyboard=keyboard)
        return result, (None if result else {"kind": MAX_ERR_TEMPORARY, "status": None, "code": None})
    return await detailed(uid, text, keyboard=keyboard)


async def mark_max_send_blocked(user_id: str, err: dict):
    """Пометка в контексте клиента (БД — переживает рестарт, и память)."""
    mark = {"since": time.time(), "status": err.get("status"), "code": err.get("code")}
    if user_id in USER_CONTEXT:
        USER_CONTEXT[user_id][MAX_SEND_BLOCKED_KEY] = mark
    try:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(select(User).where(User.platform_user_id == user_id))).scalar_one_or_none()
            if row is not None:
                ctx = json.loads(row.context_json or "{}")
                ctx[MAX_SEND_BLOCKED_KEY] = mark
                row.context_json = json.dumps(ctx, ensure_ascii=False)
                await session.commit()
    except Exception as e:
        logger.error(f"❌ [MAX] Пометка закрытого диалога {user_id} не сохранена: {e}")
    log_session(user_id, f"🚫 [MAX] Диалог клиента закрыт ({err.get('status')} {err.get('code')}) — "
                         f"повторы и напоминания остановлены", "WARNING")


async def send_smartbot_reply(user_id: str, ai_response: dict) -> bool:
    """Отправляет ответ от n8n клиенту с поддержкой тегов [SPLIT], [CONTACT_BUTTON] и RETRY-логикой.

    [U2 план 2026-09-08-002] Возвращает True только когда ВСЕ куски ответа
    подтверждённо доставлены — вызывающие (ремайндер U3) фиксируют «Sent»
    по этому статусу, а не по факту вызова."""
    if not ai_response: return False

    max_user_id = user_id  # локальный алиас для совместимости внутри функции
    channel = USER_CONTEXT.get(user_id, {}).get('channel', 'max')
    if channel == 'max' and user_id not in USER_CONTEXT:
        # channel не известен — пробуем загрузить из БД (например при вызове из handle_n8n_callback)
        ctx_from_db = await load_context_from_db(user_id)
        if ctx_from_db:
            channel = ctx_from_db.get('channel', 'max')
            USER_CONTEXT[user_id] = ctx_from_db
    client, raw_uid = get_sender(user_id, USER_CONTEXT.get(user_id), channel=channel)
    if client is None:
        log_session(max_user_id, "🛑 [U4] Бот сессии недоступен — ответ ИИ клиенту не отправлен (R14)", "ERROR")
        return False
    logger.info(f"send_smartbot_reply: user={user_id}, channel={channel}, bot={USER_CONTEXT.get(user_id, {}).get('bot_key')}")

    # 1. Сохраняем кэш состояния ИИ
    if "json_cache" in ai_response:
        USER_CONTEXT[max_user_id]['n8n_cache'] = ai_response["json_cache"]
        USER_CONTEXT[max_user_id]['n8n_cache_saved_at'] = time.time()  # [U6] возраст кэша для TTL
        # [План 2026-08-14-001, U4] Защёлка «оценка получена» — до сохранения
        # контекста, чтобы пережила рестарт (wiring-only, логика в payload_v2)
        payload_v2.register_evaluation(USER_CONTEXT[max_user_id])
        await save_context_to_db(max_user_id, USER_CONTEXT[max_user_id])
        # [U5 план 2026-08-06] Свежий кэш → перезаморозка снапшота калькулятора
        # тем же ходом (wiring-only, логика и проверка active-токена — в
        # cabinet_service). Фоном: заморозка не задерживает отправку ответа
        # клиенту, её сбой отправку не ломает.
        async def _bg_refresh(uid=max_user_id, cache=ai_response["json_cache"]):
            try:
                await cabinet_service.refresh_calc_snapshot(uid, cache)
            except Exception as _calc_err:
                logger.error(f"❌ [Cabinet U5] refresh_calc_snapshot не выполнен: {_calc_err}")
        # [P2 code-review #6] Держим сильную ссылку, пока задача не завершится —
        # иначе asyncio.create_task() без сохранённой ссылки может быть собран GC.
        _task = asyncio.create_task(_bg_refresh())
        _BG_TASKS.add(_task)
        _task.add_done_callback(_BG_TASKS.discard)
        
    # 2. Извлекаем текст
    reply_text = ai_response.get('text') or ai_response.get('output')
    if not reply_text: return False

    # --- АВАРИЙНЫЙ МОСТ ДЛЯ БИТРИКСА И NOCODB ---
    if "<function_calls>" in reply_text:
        try:
            # 1. Извлекаем внутренности ai_payload='...' с помощью регулярного выражения
            payload_match = re.search(r"ai_payload='(.*?)'", reply_text, flags=re.DOTALL)
            if payload_match:
                raw_json = payload_match.group(1).strip()
                smart_data = json.loads(raw_json)
                
                # 2. Получаем контекст пользователя и ID сделки
                ctx = await load_context_from_db(max_user_id)
                deal_id = ctx.get('deal_id')
                status = smart_data.get("status", "in_progress")
                report_text = smart_data.get("report", "Аварийное завершение диалога")
                
                log_session(max_user_id, f"⚡ Перехвачен аварийный payload для Битрикса. Сделка: {deal_id}, Статус: {status}")
                
                # 3. Сопоставляем стадии Битрикса — единый источник crm_mapping.
                # Барьер Р4 действует и здесь (решение В1): honest visit/call
                # без валидного телефона — в ОЖИДАНИЕ, не на «Обогащение»;
                # мост карточек дилеру не шлёт, меняется только стадия
                async with AsyncSessionLocal() as _bridge_sess:
                    _bu = (await _bridge_sess.execute(
                        select(User).where(User.platform_user_id == str(max_user_id))
                    )).scalar_one_or_none()
                new_stage = callback_stage(status, is_valid_phone(_bu.phone if _bu else None))
                if new_stage != resolve_stage(status):
                    log_session(max_user_id,
                                f"🚧 Барьер Р4 (мост): {status} без валидного телефона — "
                                f"сделка {deal_id} в ОЖИДАНИЕ")

                fields_to_update = {"STAGE_ID": new_stage}
                if status == "visit":
                    fields_to_update["UF_CRM_666D69C9EEE1B"] = "Визит в салон"
                elif status == "call":
                    fields_to_update["UF_CRM_666D69C9EEE1B"] = "Звонок по телефону"
                
                # Наполняем смарт-поля (марка, модель, кредит) на основе вашей глобальной карты
                for json_key, bitrix_id in BITRIX_SMART_FIELDS_MAP.items():
                    val = smart_data.get(json_key)
                    if val:
                        fields_to_update[bitrix_id] = val

                # [Планы 2026-09-07-001 U6, 2026-09-08-001 U2/B-2] Слоты защёлки —
                # фолбэк пустых ключей ai_payload (модель главнее); ctx из БД
                for _code, _val in build_ai_tradein_fields(ctx or {}).items():
                    fields_to_update.setdefault(_code, _val)

                # [U7/R34] «Кабинет перезаписывает»: после сабмита анкеты кабинета
                # по этой сделке пересекающиеся UF-ключи исключаются из чат-пути
                fields_to_update = await cabinet_service.filter_cabinet_owned_fields(fields_to_update, deal_id)

                # 4. Отправляем обновление полей и стадии напрямую в Битрикс24
                bx_url = BITRIX_WEBHOOK_URL
                if bx_url and deal_id:
                    # Таймаут попытки, как у _request (2026-10-02): зависший Битрикс не держит мост минутами
                    async with aiohttp.ClientSession(timeout=BITRIX_TIMEOUT) as bx_session, \
                            _deal_stage_lock(deal_id):
                        # «Обогащение» — только из стадий до передачи: одно чтение (план 2026-10-12-002, В3)
                        if _is_enrich_stage(fields_to_update.get("STAGE_ID")):
                            _stage_now = None
                            try:
                                _gr = await bx_session.post(f"{bx_url}/crm.deal.get", json={"id": deal_id})
                                if _gr.status == 200:
                                    _stage_now = ((await _gr.json()).get("result") or {}).get("STAGE_ID")
                            except Exception as _ge:
                                logger.warning(f"⚠️ [Аварийный мост] чтение стадии сделки {deal_id} не удалось: {_ge}")
                            fields_to_update = enrich_fields_allowed(fields_to_update, _stage_now, deal_id,
                                                                     "Аварийный мост")
                        await bx_session.post(f"{bx_url}/crm.deal.update", json={"id": deal_id, "fields": fields_to_update})
                        log_session(max_user_id, f"✅ [Аварийный мост] Карточка Битрикс {deal_id} успешно обновлена.")
                
                # 5. Принудительно гасим флаг ИИ в базе данных
                async with AsyncSessionLocal() as db_sess:
                    await db_sess.execute(update(User).where(User.platform_user_id == str(max_user_id)).values(is_smartbot_active=False))
                    await db_sess.commit()
                
                # 6. Задаем параметры для отправки финальной стенограммы в NocoDB и Битрикс-таймлайн
                if max_user_id not in USER_CONTEXT:
                    USER_CONTEXT[max_user_id] = {}
                USER_CONTEXT[max_user_id]['pending_archive'] = {
                    "deal_id": deal_id,
                    "status": status,
                    "report_text": report_text
                }
                USER_CONTEXT[max_user_id]['pending_farewell'] = True
                # Мост уже обновил сделку: поздний ретрай Save-to-CRM с той же парой — дубль
                register_callback(USER_CONTEXT[max_user_id], deal_id, status)
                
        except Exception as bridge_err:
            log_session(max_user_id, f"❌ Ошибка работы аварийного моста CRM: {bridge_err}", "ERROR")
        
        # Финал шага 2: Полностью вырезаем технический мусор из текста сообщения
        reply_text = re.sub(r'<function_calls>.*?</function_calls>', '', reply_text, flags=re.DOTALL).strip()

    if not reply_text: return False

    # Добавляем подпись сотрудника строго к очищенному от кодов тексту
    reply_text = f"<b>👨‍💼 Александр:</b>\n{reply_text}"

    # --- [План 2026-08-19-001, U1] Ссылка кабинета: маркер/сырой URL → гиперссылка ---
    # До [CONTACT_BUTTON] и [SPLIT]; URL запрашивается в момент подстановки
    # (КТР-5), сбой не роняет отправку — деградация внутри рендера (R4)
    if cabinet_link_render.needs_url(reply_text):
        _cab_url, _cab_err = await _get_cabinet_url_for_platform(max_user_id)
        reply_text, _cl_info = cabinet_link_render.render_cabinet_link(
            reply_text, _cab_url, channel)
        if _cl_info["missing_url"]:
            _reason = "сбой cabinet_service" if _cab_err else "кабинет не настроен или токена нет"
            log_session(max_user_id,
                        f"⚠️ [CabinetLink] Маркер без URL ({_reason}) — деградация до фразы",
                        "WARNING")
        elif _cl_info["applied"] or _cl_info["broken"]:
            log_session(max_user_id, f"🔗 [CabinetLink] Подстановка: {_cl_info}")

    # --- НОВАЯ ЛОГИКА: ПЕРЕХВАТ КНОПКИ ---
    keyboard = None
    if "[CONTACT_BUTTON]" in reply_text:
        reply_text = reply_text.replace("[CONTACT_BUTTON]", "").strip()
        if client.supports_contact_button():
            keyboard = [[{"type": "request_contact", "text": "ПОДТВЕРДИТЬ"}]]

    # --- PROFILER: START SENDING ---
    send_start = time.time()

    # -----------------------------------------------------
    # ФИКС 1: ВНУТРЕННЯЯ ФУНКЦИЯ ДЛЯ RETRY (ПОВТОРНЫХ ПОПЫТОК)
    # -----------------------------------------------------
    async def reliable_send(uid, text, kb):
        for attempt in range(1, 4): # Пробуем 3 раза
            result, err = await send_client_message_detailed(client, uid, text, kb)
            if result:
                return True
            # Окончательный отказ (диалог закрыт) и ошибка содержимого — без повторов (KTD-C1)
            if err and err.get("kind") == MAX_ERR_PERMANENT:
                await mark_max_send_blocked(max_user_id, err)
                return False
            if err and err.get("kind") == MAX_ERR_CONTENT:
                log_session(max_user_id, "⚠️ MAX отклонил содержимое (400) — без повторов", "WARNING")
                return False
            log_session(max_user_id, f"⚠️ Ошибка API MAX — статус и тело в строке MAX_API_ERROR выше. Повторная попытка {attempt}/3 через 1 сек...", "WARNING")
            await asyncio.sleep(1.0)
        return False
    # -----------------------------------------------------

    # 3. Отправка с логикой [SPLIT]
    all_delivered = True  # [U2] итог: все куски подтверждённо доставлены
    if "[SPLIT]" in reply_text:
        parts = [p.strip() for p in reply_text.split("[SPLIT]") if p.strip()]
        for i, part in enumerate(parts):
            if i > 0:
                delay = min(len(parts[i-1]) * 0.04, 3.5)
                log_session(max_user_id, f"⏱ [PROFILER] Пауза на сплит: {delay:.2f} сек.")
                await asyncio.sleep(delay)
            
            req_ts = time.time()
            # Важно: Кнопку прикрепляем ТОЛЬКО к последнему сообщению в серии сплитов!
            current_kb = keyboard if i == len(parts) - 1 else None
            
            # --- ФИКС 2: ЗАЩИТА ОТ ОБОРВЫШЕЙ ---
            success = await reliable_send(raw_uid, part, current_kb)
            
            if not success:
                log_session(max_user_id, f"❌ [SPLIT FATAL] Сбой отправки куска {i+1} после 3 попыток. Остальные части отменены!", "ERROR")
                all_delivered = False
                break # Прерываем отправку, чтобы клиент не получил кнопку без текста
                
            log_session(max_user_id, f"⏱ [PROFILER] Кусок сообщения доставлен в MAX за {time.time() - req_ts:.2f} сек.")
    else:
        req_ts = time.time()
        success = await reliable_send(raw_uid, reply_text, keyboard)
        if success:
            log_session(max_user_id, f"⏱ [PROFILER] Сообщение доставлено в MAX за {time.time() - req_ts:.2f} сек.")
        else:
            log_session(max_user_id, f"❌ [FATAL] Не удалось отправить целое сообщение в MAX после 3 попыток.", "ERROR")
            all_delivered = False

    log_session(max_user_id, f"🏁 [PROFILER] ПОЛНЫЙ ЦИКЛ ОТВЕТА ЗАВЕРШЕН за {time.time() - send_start:.2f} сек.")

    # --- НОВОЕ: Отправляем системную отбивку строго ПОСЛЕ того, как ИИ договорил ---
    if USER_CONTEXT.get(max_user_id, {}).pop('pending_farewell', False):
        await asyncio.sleep(1) # Небольшая микро-пауза для естественности
        farewell_text = build_farewell_text(
            USER_CONTEXT.get(max_user_id, {}).get('channel', 'max'))
        # Отбивку тоже отправляем с защитой от сбоев
        await reliable_send(raw_uid, farewell_text, None)
        # Кабинетное завершение pending-путём: фиксируем факт отправки
        # прощания (ревью 2026-08-15) — если финализация ниже упадёт, добор
        # повторит ТОЛЬКО финализацию, без второго «покинул чат»
        _cab_pa = USER_CONTEXT.get(max_user_id, {}).get('pending_archive') or {}
        if _cab_pa.get('cabinet_token_id') is not None:
            await cabinet_service.record_session_farewell(
                _cab_pa.get('cabinet_user_pk'), _cab_pa['cabinet_token_id'])
        
        # ДОБАВЛЯЕМ В ИСТОРИЮ (Чтобы системное сообщение тоже попало в стенограмму)
        if 'chat_history' in USER_CONTEXT[max_user_id]:
            USER_CONTEXT[max_user_id]['chat_history'].append({
                "time": datetime.now().strftime("%H:%M"), "role": "system", "text": farewell_text
            })
            await save_context_to_db(max_user_id, USER_CONTEXT[max_user_id])

    return all_delivered

def build_farewell_text(channel: str) -> str:
    """Единый текст «Александр покинул чат» — общий для финализации по
    n8n-callback и завершения по передаче кабинета (план 2026-08-14-001, R5,
    решение владельца: отдельной кабинетной формулировки пока нет). Для web —
    без «перейдите на сайт»: клиент уже на сайте."""
    if channel == 'web':
        return (
            "[Автоматическое уведомление]\n\n"
            "Ассистент Александр покинул чат. Если у Вас появятся новые вопросы, пожалуйста, отправьте повторный запрос. 🌐"
        )
    return (
        "[Автоматическое уведомление]\n\n"
        "Ассистент Александр покинул чат. Если у Вас появятся новые вопросы, пожалуйста, перейдите на сайт и отправьте повторный запрос. 🌐"
    )


async def finalize_transcript_archive(user_id: str, archive_data: dict) -> bool:
    """Выгружает финальную стенограмму в Битрикс и NocoDB ПОСЛЕ отправки последнего сообщения.

    Возвращает успех: True — архив выгружен и стейт зафиксирован; False —
    исключение (повтор возьмёт на себя вызывающий/добор). [План 2026-08-14-001,
    U2] Для завершения по передаче кабинета archive_data несёт
    cabinet_token_id/cabinet_user_pk — тогда ПОСЛЕ успешной финализации здесь же
    пишется событие session_closed (владелец записи — эта обёртка, обе ветки:
    немедленная и pending_*; запись до фактического завершения недопустима —
    рестарт терял бы прощание без возможности добора)."""
    max_user_id = user_id  # алиас для совместимости внутри функции
    deal_id = archive_data.get('deal_id')
    status = archive_data.get('status')
    report_text = archive_data.get('report_text')

    ctx = await load_context_from_db(user_id)
    history = ctx.get('chat_history', [])
    synced_index = ctx.get('bitrix_synced_index', 0)
    
    unsent_history = history[synced_index:]
    formatted_transcript_bitrix = format_chat_history(unsent_history)
    formatted_transcript_full = format_chat_history(history)
    
    bx_url = BITRIX_WEBHOOK_URL
    
    try:
        # Таймаут попытки, как у _request (2026-10-03): зависший Битрикс не держит финализацию
        async with aiohttp.ClientSession(timeout=BITRIX_TIMEOUT) as bx_session:
            # 1. Отправка комментария в Битрикс
            if bx_url and deal_id:
                final_comment = f"[B]🤖 Итоги AI-диалога:[/B]\n{report_text}"
                if formatted_transcript_bitrix:
                    final_comment += f"\n\n--------------------------------------------------\n\n{formatted_transcript_bitrix}"
                
                comment_payload = {"fields": {"ENTITY_ID": deal_id, "ENTITY_TYPE": "DEAL", "COMMENT": final_comment}}
                await bx_session.post(f"{bx_url}/crm.timeline.comment.add", json=comment_payload)
                logger.info(f"✅ Bitrix Deal {deal_id} comment updated with FINAL transcript.")
        
        # 2. Обновление курсора в БД
        ctx['bitrix_synced_index'] = len(history)
        ctx['nocodb_synced_index'] = len(history)
        ctx['is_finalized'] = True

        # Для web-канала — сигнал закрытия чата
        if ctx.get('channel') == 'web':
            await web_client.send_close(user_id)
            
        # 3. Отправка в NocoDB
        start_code = ctx.get('start_code', '')
        dealer_code = start_code[:2] if len(start_code) >= 2 else "00"
        dealer_info = CACHED_DEALERS.get(dealer_code, {})
        prompt_teg = dealer_info.get("Prompt_Teg", "base")
        prompt_ver = dealer_info.get("Prompt_Ver", "v01.00")
        
        archive_payload = {
            "user_id": str(max_user_id),
            "session_id": f"sess_{max_user_id}_{start_code}",
            "Bot_Key": _archive_bot_key(ctx, user_id=max_user_id),
            "model": ctx.get('model', 'Не указана'),
            "Deal_ID": str(deal_id),
            "Dealer_Code": dealer_code,
            "Prompt_Version": f"{prompt_teg}_{prompt_ver}",
            "Scenario": ctx.get('scenario_type', 'unknown'),
            "Final_Status": str(status),
            "AI_Report": str(report_text),
            "Transcript": formatted_transcript_full
        }
        await upsert_dialogue_archive(archive_payload)

        # 4. Очищаем логи ТОЛЬКО ПОСЛЕ отправки и сохраняем финальный стейт
        if 'python_logs' in ctx:
            del ctx['python_logs']
        
        async with AsyncSessionLocal() as db_sess:
            await db_sess.execute(update(User).where(User.platform_user_id == user_id).values(context_json=json.dumps(ctx, ensure_ascii=False)))
            await db_sess.commit()

        # Очищаем контекст из памяти после финальной архивации
        USER_CONTEXT.pop(user_id, None)
        logger.info(f"🧹 [Memory] USER_CONTEXT очищен для {user_id}")

        # [План 2026-08-14-001, U2] Завершение по передаче кабинета: отметка
        # session_closed — строго ПОСЛЕ фактической финализации (гард добора)
        _cab_token_id = archive_data.get('cabinet_token_id')
        if _cab_token_id is not None:
            await cabinet_service.record_session_closed(
                archive_data.get('cabinet_user_pk'), _cab_token_id)
        return True

    except Exception as e:
        logger.error(f"❌ Finalize transcript archive error: {e}")
        return False

async def _cabinet_close_gate(ctx: dict, user_id: int, token_id: int) -> bool:
    """No-op гейт завершения (R4, общий для проверки до лока и под локом):
    True — завершать нечего. Финализация другим путём (n8n-callback) — сессия
    фактически завершена: фиксируем session_closed, иначе добор повторял бы
    no-op вечно."""
    if ctx.get('deal_finalized') or ctx.get('is_finalized'):
        await cabinet_service.record_session_closed(user_id, token_id)
        return True
    return await cabinet_service.has_session_closed(token_id)


async def close_cabinet_session(user_id: int, trigger: str, deal_id, token_id):
    """[План 2026-08-14-001, U2] Завершение сессии диалога с ИИ при ДОВЕДЁННОЙ
    передаче заявки кабинета в CRM (колбэк cabinet_service.on_handoff_completed,
    вызывается fire-and-forget — см. U1).

    Продуктовое требование: клиентом занялся продавец — Александр замолкает.
    STAGE_ID сделки здесь НЕ меняется: конверсию Bitrix кабинетная передача
    уже выполнила сама (_complete_handoff). Идемпотентно (гард session_closed),
    переживает рестарт (добор _recover_unclosed_sessions)."""
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
    if user is None:
        return
    puid = user.platform_user_id

    # Веб-вход (U10/R33): диалога нет — ни прощания в мессенджер, ни «Итогов
    # AI-диалога», ни архива стенограммы; только отметка завершения сессии
    if puid.startswith(cabinet_service.WEB_PLATFORM_PREFIX):
        await cabinet_service.record_session_closed(user_id, token_id)
        return

    ctx = USER_CONTEXT.get(puid) or await load_context_from_db(puid) or {}

    if await _cabinet_close_gate(ctx, user_id, token_id):
        return

    archive_data = {
        'deal_id': deal_id or ctx.get('deal_id'),
        'status': 'cabinet_handoff',
        'report_text': f"Заявка личного кабинета передана менеджеру (триггер: {trigger}). "
                       "Диалог с ассистентом завершён автоматически.",
        # владелец записи session_closed — finalize_transcript_archive
        'cabinet_token_id': token_id,
        'cabinet_user_pk': user_id,
    }

    # Развилка «ход в полёте» — ДО захвата лока (по факту занятости), иначе
    # честное ожидание turn_lock (ход держит его до минут: n8n 120с + LONG
    # RETRY) съело бы смысл fire-and-forget, а захват после ожидания попал бы
    # в уже завершённый ход, чьи pending_* никто больше не потребит
    if puid in USER_MESSAGE_TASKS or get_turn_lock(puid).locked():
        async with AsyncSessionLocal() as session:
            await session.execute(update(User).where(User.id == user_id)
                                  .values(is_smartbot_active=False))
            await session.commit()
        mem = USER_CONTEXT.setdefault(puid, ctx)
        mem['pending_farewell'] = True
        mem['pending_archive'] = archive_data
        await save_context_to_db(puid, mem)
        log_session(puid, f"🔚 [Cabinet] Передача {trigger}: ход в полёте — "
                          "завершение сессии после него")
        return

    async with get_turn_lock(puid):
        # Перечитка гейта под локом: пока ждали захват (мгновенный при
        # свободном локе, но гонка возможна), ход мог финализировать сессию
        ctx = USER_CONTEXT.get(puid) or await load_context_from_db(puid) or {}
        if await _cabinet_close_gate(ctx, user_id, token_id):
            return
        async with AsyncSessionLocal() as session:
            await session.execute(update(User).where(User.id == user_id)
                                  .values(is_smartbot_active=False))
            await session.commit()

        # Идемпотентный гард прощания (ревью 2026-08-15): session_closed
        # пишется только после УСПЕШНОЙ финализации, и без этого гарда каждый
        # повтор добора при падающей финализации слал бы клиенту новое
        # «покинул чат». Повтор — только финализация, без второго прощания.
        if await cabinet_service.record_session_farewell(user_id, token_id):
            farewell = build_farewell_text(ctx.get('channel', 'max'))
            _cl, _raw = get_sender(puid, ctx)
            if _cl is not None:
                await _cl.send_message(user_id=_raw, text=farewell)
            ctx.setdefault('chat_history', []).append({
                "time": datetime.now().strftime("%H:%M"), "role": "system",
                "text": farewell})
            USER_CONTEXT[puid] = ctx
            await save_context_to_db(puid, ctx)

        # Финализация (web: send_close внутри) + session_closed при успехе;
        # False — добор повторит завершение следующим тиком
        ok = await finalize_transcript_archive(puid, archive_data)
        log_session(puid, f"🔚 [Cabinet] Сессия завершена по передаче "
                          f"({trigger}), финализация: {'ok' if ok else 'повтор добором'}")


def _phone_tail(value) -> str:
    """Последние 10 цифр телефона — для сравнения номеров контакта."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else ""


async def send_alert_to_max_group(max_user_id: str, context_data: dict) -> bool:
    """Отправляет карточку лида в группу дилера в MAX.

    [U2 план 2026-09-08-002] Возвращает фактический результат доставки:
    раньше функция ничего не возвращала, и проверка `_ok` в final_sync_wrapper
    писала «NOT delivered» на КАЖДОЙ доставленной карточке."""
    start_code = context_data.get('start_code', '')
    prefix = start_code[:2] if len(start_code) >= 2 else ""
    
    dealer_config = {}
    for _, d_info in CACHED_DEALERS.items():
        if d_info.get('code') == prefix:
            dealer_config = d_info
            break
            
    channel_val = context_data.get('channel_val', 'Не указан')
    comm_type = dealer_config.get("comm_type", "Chat")
    
    action_call = "☎️ <b>Пожалуйста, свяжитесь с клиентом по телефону</b>"
    _web = context_data.get(cabinet_service.WEB_CONVERSION_KEY) or {}
    if _web:
        # Веб-вход (U10/R34): дилер — из сессии, источник — ЛК с сайта, без MAX
        dealer_config = CACHED_DEALERS.get(_web.get("dealer_code"), dealer_config)
        prefix = _web.get("dealer_code") or prefix
        channel_val = "личный кабинет с сайта"
    elif comm_type == "Chat" and channel_val != "По телефону":
         action_call = "👇 <b>Пожалуйста, свяжитесь с клиентом в MAX</b>"

    template = MANAGER_TEMPLATES.get(context_data.get('scenario_type', 'consult'), MANAGER_TEMPLATES['consult'])
    car_model_base = context_data.get('model') or "Автомобиль"
    text_scenario = template.replace("{model}", car_model_base).replace("{payment}", context_data.get('payment', '---')).replace("{plate}", context_data.get('plate', '---'))
    model_full = f"{car_model_base} {context_data.get('model_title', '')}".strip()

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.platform_user_id == max_user_id))
        user = result.scalar_one_or_none()
        u_name = user.full_name if user else "Клиент"
        u_phone = user.phone if (user and is_valid_phone(user.phone)) else "Не указан"
        u_username = user.username if user else ""

    username_line = f"🔗 @{u_username}\n" if u_username else ""

    final_text = (
        f"{text_scenario}\n\n"
        f"👤 <b>{u_name}</b>\n"
        f"{username_line}"
        f"🚗 <b>{model_full}</b>\n"
        f"📱 Способ связи: <b>{channel_val}</b>\n"
        f"📞 <b>{u_phone}</b>\n\n"
        f"{action_call}"
    )

    # U9: карточка дилеру — ботом БРЕНДА дилера (правило 2026-09-23)
    return await _send_dealer_group_alert(dealer_config, prefix, final_text,
                                          deal_id=context_data.get('deal_id'))


async def send_dealer_alert(dealer_code: str, text: str):
    """Алерт дилеру всегда идёт через MAX, независимо от канала клиента.
    U9: через MAX-бота бренда дилера (правило 2026-09-23)."""
    dealer_config = CACHED_DEALERS.get(dealer_code, {})
    await _send_dealer_group_alert(dealer_config, dealer_code, text)


# --- Интеграция n8n AI Agent ---
# План 2026-09-07-001 (Р3/Д1): сумма оценки трейд-ина из ИИ-диалога.
# НЕ ключ BITRIX_SMART_FIELDS_MAP: в ai_payload суммы нет по контракту finalize
# — значение берётся из собственного слота бота ctx['evaluation_value']
# (ti_valuation инструмента auto.ru, payload_v2.register_evaluation).
# Раздельно с кабинетным UF_CRM_1785857524 («Кабинет: Оценка трейд-ина»,
# путь анкеты) — решение владельца Д1: два поля, два источника события,
# при прохождении обоих путей обе ячейки получают одно и то же число.
BITRIX_FIELD_TRADEIN_VALUATION = "UF_CRM_1788758195"  # «SB Трейдин Оценка autoru»

# Слот защёлки → ключ ai_payload (та же семья SB-полей, что в карте ниже)
# Пробега нет (2026-10-03): слот заполнялся из ответа оценки — это пробег
# Auto.ru, не клиента; старые контексты со слотом mileage его не передают
_TRADEIN_SLOT_TO_KEY = (("make", "tradein_make"), ("model", "tradein_model"),
                        ("year", "tradein_year"), ("plate", "tradein_plate"))


def build_ai_tradein_fields(ctx: dict) -> dict:
    """План 2026-09-08-001 (U2, KTD-12 точка 1): SB-поля трейд-ина и сумма
    оценки из персистентных слотов защёлки (payload_v2.register_evaluation) —
    для путей завершения, где ai_payload не существует: таймаут кабинета,
    Логика Б, фолбэк n8n-сбоя, воронковые финализации; на колбэке/мосту —
    фолбэк для пустых ключей ai_payload (B-2, значения модели главнее).
    Слоты пусты → ключи не появляются вовсе. Вызывающий обязан пропустить
    результат через filter_cabinet_owned_fields (R34, KTD-15)."""
    fields = {}
    car = (ctx or {}).get("evaluation_car") or {}
    for slot_key, json_key in _TRADEIN_SLOT_TO_KEY:
        val = car.get(slot_key)
        if val not in (None, ""):
            fields[BITRIX_SMART_FIELDS_MAP[json_key]] = str(val)
    eval_val = (ctx or {}).get("evaluation_value")
    if eval_val:
        fields[BITRIX_FIELD_TRADEIN_VALUATION] = str(eval_val)
    return fields

BITRIX_SMART_FIELDS_MAP = {
    "sb_brand": "UF_CRM_1770632572",          
    "sb_model": "UF_CRM_1770632588",          
    "sb_complectation": "UF_CRM_1770632890",  
    "sb_mod": "UF_CRM_1770632964",            
    "sb_color": "UF_CRM_1770633094",          
    "credit_initial": "UF_CRM_1770634607",    
    "credit_term": "UF_CRM_1770634728",       
    "credit_payment": "UF_CRM_1770634992",    
    "tradein_make": "UF_CRM_1770635063",      
    "tradein_model": "UF_CRM_1770635116",     
    "tradein_year": "UF_CRM_1770635208",      
    "tradein_mileage": "UF_CRM_1770635257",   
    "tradein_plate": "UF_CRM_1770635310",     
    "buy_term": "UF_CRM_1770635420",          
    "buy_method": "UF_CRM_1770635471",        
    "alt_car": "UF_CRM_1770635635",           
    "city": "UF_CRM_1770635728",              
    "special_req": "UF_CRM_1770635804",       
    "report": "UF_CRM_1770636065",            
    "dialog": "UF_CRM_1770636186",            
    "next_step": "UF_CRM_1770636277",         
    "next_step_time": "UF_CRM_1770636537"     
}

def _callback_client_ip(request):
    """IP отправителя колбэка для лога: X-Forwarded-For от доверенного прокси
    (CABINET_TRUSTED_PROXIES) — та же логика, что у лимитов веб-входа."""
    if not hasattr(request, "headers") or not hasattr(request, "remote"):
        return getattr(request, "remote", None)
    try:
        return cabinet_api.client_ip(request)
    except Exception:
        return getattr(request, "remote", None)


async def handle_n8n_callback(request):
    """Принимает результаты диалога от агента n8n.

    Проверка отправителя (2026-10-03, n8n_auth): ДО любых побочных эффектов.
    strict — без верного X-N8N-Callback-Secret 403 без записи в Битрикс, смены
    is_smartbot_active и алертов; soft — обработка как раньше + строка в логе."""
    mode = n8n_auth.callback_auth_mode()
    if mode != "off":
        auth_result = n8n_auth.callback_auth_result(
            (getattr(request, "headers", None) or {}).get(n8n_auth.CALLBACK_HEADER))
        try:
            _peek = await request.json()
            _deal_for_log = _peek.get("deal_id") if isinstance(_peek, dict) else None
        except Exception:
            _deal_for_log = None
        logger.info("N8N_CALLBACK_AUTH result=%s mode=%s deal=%s ip=%s",
                    auth_result, mode, _deal_for_log, _callback_client_ip(request))
        if mode == "strict" and auth_result != "ok":
            return web.json_response({"status": "forbidden"}, status=403)
    try:
        data = await request.json()
        max_user_id = str(data.get("user_id"))
        deal_id = data.get("deal_id")
        
        # Извлекаем и парсим сгенерированный ИИ JSON (ai_payload)
        raw_payload = data.get("ai_payload", "{}")
        try:
            smart_data = json.loads(raw_payload)
        except json.JSONDecodeError:
            smart_data = {}
            log_session(max_user_id, f"❌ AI прислал невалидный JSON в ai_payload: {raw_payload}", "ERROR")
            
        # --- ФИКС: Умный поиск статуса и отчета (из корня ИЛИ из ai_payload) ---
        raw_status = data.get("status") or smart_data.get("status")
        report_text = data.get("report") or smart_data.get("report", "Нет отчета")

        # Телефон читается из БД один раз в начале обработчика (план
        # 2026-09-02-002): он решает подстановку статуса (Р3), стадию (Р4)
        # и отправку карточки дилеру.
        async with AsyncSessionLocal() as _phone_sess:
            _pu = (await _phone_sess.execute(
                select(User).where(User.platform_user_id == max_user_id)
            )).scalar_one_or_none()
        has_phone = is_valid_phone(_pu.phone if _pu else None)

        # Р1-Р3: нормализация статуса — чистая функция crm_mapping; подстановка
        # нераспознанного возможна только при телефоне и логируется с исходником
        status, substituted, recognized = resolve_callback_status(
            raw_status, smart_data, has_phone)
        if substituted:
            log_session(max_user_id,
                        f"⚠️ Статус n8n не распознан: {raw_status!r} → подставлен "
                        f"'{status}' (deal {deal_id})", "WARNING")
        elif not recognized:
            log_session(max_user_id,
                        f"⚠️ Статус n8n не распознан: {raw_status!r}; телефона нет — "
                        f"без подстановки, сделка {deal_id} уйдёт в ОЖИДАНИЕ (Р3)",
                        "WARNING")

        log_session(max_user_id, f"📥 N8N Callback received for Deal {deal_id}, Status (Fixed): {status}")

        # Идемпотентность: ретраи Save-to-CRM (5x/3s) и поздние дубли после
        # аварийного моста не должны повторно двигать сделку и слать алерты
        ctx_mem = USER_CONTEXT.setdefault(max_user_id, {})
        if is_duplicate_callback(ctx_mem, deal_id, status):
            log_session(max_user_id, f"↩️ Дубль n8n callback (deal {deal_id}, status {status}) — пропущен.")
            return web.json_response({"status": "duplicate_ignored"})
        register_callback(ctx_mem, deal_id, status)

        # Барьеры Р3/Р4 (план 2026-09-02-002): стадия с учётом телефона; при
        # срабатывании меняется ТОЛЬКО стадия — данные пишутся полностью (В2)
        new_stage = callback_stage(status, has_phone)
        if status in ("visit", "call") and not has_phone:
            log_session(max_user_id,
                        f"🚧 Барьер Р4: {status} без валидного телефона — сделка "
                        f"{deal_id} в ОЖИДАНИЕ, карточка дилеру не отправляется")

        # 4. Отключение ИИ: колбэк Save-to-CRM — событие завершения диалога.
        # Раньше условие по списку статусов было рудиментом (после подстановки
        # выполнялось всегда); безусловное выполнение сохраняет то же поведение
        # и для сырых (Р3) и working (Р2) статусов — стенограмма и архив
        # уходят как раньше.
        # [U4] персистентный слот для Tool Guard v3.0: сделка финализирована
        ctx_mem['deal_finalized'] = True
        async with AsyncSessionLocal() as session:
            await session.execute(update(User).where(User.platform_user_id == max_user_id).values(is_smartbot_active=False))
            await session.commit()

        if max_user_id not in USER_CONTEXT:
            USER_CONTEXT[max_user_id] = {}
        # --- НОВОЕ: Ставим флаг в память, чтобы отбивка ушла СТРОГО после ответа ИИ ---
        USER_CONTEXT[max_user_id]['pending_farewell'] = True

        # --- ФИКС ТРАНСКРИПТА: Ставим флаг на финальную выгрузку стенограммы ---
        USER_CONTEXT[max_user_id]['pending_archive'] = {
            "deal_id": deal_id,
            "status": status,
            "report_text": report_text
        }

        log_session(max_user_id, f"🛑 SmartBot отключен для пользователя {max_user_id} (статус: {status})")

        # [U4] Реестр дедупа и deal_finalized должны пережить рестарт Python (R28, слоты в Postgres)
        await save_context_to_db(max_user_id, ctx_mem)

        fields_to_update = {"STAGE_ID": new_stage}

        # --- НОВОЕ: Обновляем кастомное поле "Тип обращения" ---
        if status == "visit":
            fields_to_update["UF_CRM_666D69C9EEE1B"] = "Визит в салон"
        elif status == "call":
            fields_to_update["UF_CRM_666D69C9EEE1B"] = "Звонок по телефону"

        for json_key, bitrix_id in BITRIX_SMART_FIELDS_MAP.items():
            val = smart_data.get(json_key)
            if val: fields_to_update[bitrix_id] = val

        # [Планы 2026-09-07-001 U6 и 2026-09-08-001 U2/B-2] Слоты защёлки —
        # фолбэк для пустых ключей ai_payload (значения модели ГЛАВНЕЕ:
        # setdefault не перетирает уже записанное из smart_data). Сумма оценки
        # в ai_payload отсутствует по контракту — всегда из слота. Слоты
        # персистентны (context_json): при пустой памяти после рестарта — из
        # строки User, уже прочитанной выше для телефонного гейта.
        _slots_ctx = ctx_mem
        if not (_slots_ctx.get('evaluation_value') or _slots_ctx.get('evaluation_car')) \
                and _pu is not None and _pu.context_json:
            try:
                _slots_ctx = json.loads(_pu.context_json)
            except (ValueError, TypeError):
                _slots_ctx = ctx_mem
        for _code, _val in build_ai_tradein_fields(_slots_ctx).items():
            fields_to_update.setdefault(_code, _val)

        # [U7/R34] «Кабинет перезаписывает»: после сабмита анкеты кабинета
        # по этой сделке пересекающиеся UF-ключи исключаются из чат-пути
        fields_to_update = await cabinet_service.filter_cabinet_owned_fields(fields_to_update, deal_id)

        bx_url = BITRIX_WEBHOOK_URL
        
        # --- ФОНОВАЯ ЗАДАЧА ДЛЯ CRM И АЛЕРТОВ ---
        async def background_crm_update():
            if bx_url and deal_id:
                try:
                    # Таймаут попытки, как у _request (2026-10-02)
                    async with aiohttp.ClientSession(timeout=BITRIX_TIMEOUT) as bx_session, \
                            _deal_stage_lock(deal_id):
                        # --- ШАГ 1 (ИСПРАВЛЕНО): СНАЧАЛА Ищем CONTACT_ID и обновляем телефон ---
                        current_stage = None
                        deal_res = await bx_session.post(f"{bx_url}/crm.deal.get", json={"id": deal_id})
                        if deal_res.status == 200:
                            deal_json = await deal_res.json()
                            contact_id = deal_json.get("result", {}).get("CONTACT_ID")
                            current_stage = (deal_json.get("result") or {}).get("STAGE_ID")
                            
                            async with AsyncSessionLocal() as db_sess:
                                res = await db_sess.execute(select(User).where(User.platform_user_id == max_user_id))
                                usr = res.scalar_one_or_none()
                                if contact_id and usr and is_valid_phone(usr.phone):
                                    # Обновляем контакт ДО смены стадии сделки
                                    await BitrixService.update_contact(str(contact_id), usr.full_name or "Клиент", usr.phone, usr.username or "", user_id=max_user_id)

                        # --- ШАГ 2: ОБНОВЛЯЕМ СДЕЛКУ И МЕНЯЕМ СТАДИЮ ---
                        # «Обогащение» — только из стадий до передачи (план 2026-10-12-002, В3)
                        _cb_fields = enrich_fields_allowed(fields_to_update, current_stage, deal_id, "Callback")
                        await bx_session.post(f"{bx_url}/crm.deal.update", json={"id": deal_id, "fields": _cb_fields})
                        log_session(max_user_id, f"✅ Bitrix Deal {deal_id} status updated in BACKGROUND.")
                        
                        # Шаги 3 и 4 (Стенограмма) перенесены в finalize_transcript_archive для ожидания ответа ИИ!
                except Exception as bg_e:
                    log_session(max_user_id, f"❌ Фоновая отправка в Bitrix упала: {bg_e}", "ERROR")

            # Уведомление менеджеров тоже отправляем в фоне. Барьер Р4: без
            # валидного телефона карточка дилеру не уходит — сделка в
            # ОЖИДАНИИ, ею занимается саппорт (решение владельца, В1/В2)
            if status in ["visit", "call"] and has_phone:
                try:
                    # 1. Достаем актуальные данные пользователя из БД
                    async with AsyncSessionLocal() as db_sess:
                        res = await db_sess.execute(select(User).where(User.platform_user_id == max_user_id))
                        usr = res.scalar_one_or_none()
                    
                    u_name = usr.full_name if usr else "Клиент"
                    u_phone = usr.phone if (usr and is_valid_phone(usr.phone)) else "Не указан"
                    u_username = usr.username if usr and usr.username else ""
                    username_line = f"🔗 @{u_username}\n" if u_username else ""
                    
                    # 2. Собираем модель авто из данных ИИ (или ставим заглушку)
                    model_info = f"{smart_data.get('sb_brand', '')} {smart_data.get('sb_model', '')} {smart_data.get('sb_complectation', '')}".strip()
                    if not model_info: model_info = "Автомобиль"
                    
                    # Определяем тип контакта для менеджера на основе статуса от n8n
                    next_contact = "Визит в салон" if status == "visit" else "Звонок по телефону"

                    # 3. Формируем красивую карточку
                    alert_text = (
                        f"📝 <b>Резюме от ассистента:</b>\n"
                        f"{report_text}\n\n"
                        f"👤 <b>{u_name}</b>\n"
                        f"{username_line}"
                        f"🚗 <b>{model_info}</b>\n"
                        f"📅 Следующий контакт: <b>{next_contact}</b>\n"
                        f"📞 <b>{u_phone}</b>\n"
                        f"🔗 ID Сделки: {deal_id}\n\n"
                        f"👇 <b>Пожалуйста, свяжитесь с клиентом по телефону</b>"
                    )
                    
                    # Динамически определяем группу конкретного дилера
                    user_context = await load_context_from_db(max_user_id)
                    start_code = user_context.get('start_code', '')
                    prefix = start_code[:2] if len(start_code) >= 2 else "00"
                    
                    dealer_config = CACHED_DEALERS.get(prefix, {})
                    # U9: карточка дилеру — ботом бренда (правило 2026-09-23)
                    if not await _send_dealer_group_alert(dealer_config, prefix,
                                                          alert_text, deal_id=deal_id):
                        log_session(max_user_id, f"❌ [N8N Callback] Алерт дилеру {prefix} не доставлен (детали в TECH_ALERT).", "ERROR")
                except Exception as alert_err:
                    log_session(max_user_id, f"❌ Ошибка отправки алерта: {alert_err}", "ERROR")

        # Запускаем обновление CRM и отправку алертов в фоне, не дожидаясь результата!
        asyncio.create_task(background_crm_update())

        # Моментально отвечаем n8n, чтобы он сразу отправил сообщение клиенту
        return web.json_response({"status": "ok"})
        
    except Exception as e:
        log_session(max_user_id, f"💥 Callback Error: {e}", "ERROR")
        return web.json_response({"status": "error", "reason": str(e)}, status=500)

async def update_deal_stage_direct(deal_id: str, stage_id: str):
    if not deal_id: return
    webhook_url = BITRIX_WEBHOOK_URL 
    if not webhook_url: return

    url = f"{webhook_url}/crm.deal.update"
    payload = {"id": deal_id, "fields": {"STAGE_ID": stage_id}}
    try:
        # Таймаут попытки, как у _request (2026-10-03)
        async with aiohttp.ClientSession(timeout=BITRIX_TIMEOUT) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    logger.info(f"🤖 [Smartbot] Silent stage update: Deal {deal_id} -> {stage_id}")
    except Exception as e:
        logger.error(f"💥 [Smartbot] Connection error: {e}")

CABINET_BUILD_PREFIX = '/widget-custom/cabinet-app/Production/'
CABINET_BUILD_CACHE_CONTROL = 'public, max-age=31536000, immutable'
# Вендорские файлы лендинга (план 2026-10-11-001): Swiper и Tabler — версия в
# пути, файлы не меняются → годовой кэш без условия на ?v=. Шрифтам тип
# ставится явно: aiohttp держит свою таблицу типов, .woff2/.woff/.ttf в ней
# нет, и в образе (без /etc/mime.types) ушло бы application/octet-stream.
VENDOR_PREFIX = '/widget-custom/vendor/'
VENDOR_FONT_TYPES = {'.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf'}
_CABINET_BUILD_MANIFEST = {"mtime": None, "versions": {}}


def _cabinet_build_versions() -> dict:
    """Версии файлов сборки кабинета из Production/manifest.json (перечитка
    по mtime — пересборка без перезапуска бота подхватывается сразу)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'widget-custom', 'cabinet-app', 'Production', 'manifest.json')
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _CABINET_BUILD_MANIFEST["mtime"] != mtime:
        try:
            with open(path, encoding='utf-8') as fh:
                files = (json.load(fh) or {}).get('files') or {}
            _CABINET_BUILD_MANIFEST["versions"] = {
                name: str(info.get('version') or '') for name, info in files.items()}
        except (OSError, ValueError):
            _CABINET_BUILD_MANIFEST["versions"] = {}
        _CABINET_BUILD_MANIFEST["mtime"] = mtime
    return _CABINET_BUILD_MANIFEST["versions"]


def _cabinet_build_version_matches(path: str, v) -> bool:
    if not v or not path.startswith(CABINET_BUILD_PREFIX):
        return False
    name = path[len(CABINET_BUILD_PREFIX):]
    expected = _cabinet_build_versions().get(name)
    return bool(expected) and v == expected


async def widget_static_utf8_charset(request, response):
    """/widget-custom/: явный charset=utf-8 в Content-Type — без него кириллица
    в JS/HTML при прямом открытии файла показывается кракозябрами (add_static
    отдаёт mimetype без charset). На текстовые типы только; бинарные не трогаем.

    Cache-Control: no-cache на *.js — инцидент 2026-09-03: скрипты подключаются
    тегом <script> без ?v=, без указаний по кэшированию браузер держал widget.js,
    скачанный до выката (эвристика по Last-Modified). no-cache = хранить можно,
    но перед использованием ревалидировать: неизменный файл — 304 без тела
    (валидаторы ETag/Last-Modified отдаёт add_static), изменённый — свежее тело.
    Картинки (аватары) не трогаем — им кэш полезен."""
    if not request.path.startswith('/widget-custom/'):
        return
    if request.path.startswith(VENDOR_PREFIX):
        # CORS — '*' из cors_middleware: лендинг грузит Swiper с integrity
        # (crossorigin), шрифты всегда идут CORS-запросом
        if response.status in (200, 206, 304):
            response.headers['Cache-Control'] = CABINET_BUILD_CACHE_CONTROL
        font_type = VENDOR_FONT_TYPES.get(os.path.splitext(request.path)[1])
        if font_type and response.status in (200, 206):
            response.content_type = font_type
    elif request.path.endswith('.js'):
        # Ускорение A3 (2026-10-03): сборка кабинета с версией в адресе —
        # долгий кэш; только при ?v=, совпадающем с манифестом сборки
        if _cabinet_build_version_matches(request.path, request.query.get('v')):
            response.headers['Cache-Control'] = CABINET_BUILD_CACHE_CONTROL
        else:
            response.headers['Cache-Control'] = 'no-cache'
    ct = response.content_type or ""
    if ct.startswith('text/') or ct in ('application/javascript', 'application/json'):
        response.charset = 'utf-8'


async def _attach_cabinet_context(payload: dict, user_db_data: User, dealer_code: str,
                                  deal_id, scenario, user_context: dict, label: str):
    """[U7] Кабинет: ссылка и статус в qualification_context (wiring-only,
    логика — cabinet_service.prepare_cabinet_context; выдача только клиенту
    без телефона в чате, существующий токен возвращает тот же URL — R19).
    Общий блок обоих payload-путей (chat и handoff); label — для лога."""
    try:
        _cab = await cabinet_service.prepare_cabinet_context(
            user_db_data.id, dealer_code,
            deal_id=deal_id,
            # B2 (план 2026-09-01-002): тот же предикат, что у воронки бота —
            # клиент с невалидным номером не должен выпадать из ОБЕИХ воронок
            # (бот считает «телефона нет» → кабинетная ссылка обязана выдаваться)
            has_phone=is_valid_phone(user_db_data.phone),
            scenario_type=scenario,
            payment=user_context.get('payment'),
            brand_model=user_context.get('model'),
            # [U5 план 2026-08-06] кэш Car Search — источник снапшота калькулятора
            n8n_cache=user_context.get('n8n_cache'))
        payload["qualification_context"]["cabinet_url"] = _cab["cabinet_url"]
        payload["qualification_context"]["cabinet_status"] = _cab["cabinet_status"]
    except Exception as _cab_err:
        logger.error(f"❌ [Cabinet U7] cabinet-контекст ({label}) не добавлен: {_cab_err}")


async def send_chat_message_to_n8n(max_user_id: str, text: str, user_name: str, user_context: dict):
    url = os.getenv("N8N_WEBHOOK_URL") 
    if not url: return None

    start_code = user_context.get('start_code', '')
    dealer_code = start_code[:2] if len(start_code) >= 2 else "00"
    dealer_info = CACHED_DEALERS.get(dealer_code, {})
    db_config = dealer_info.get("db_config", {}) 

    # 1. Формируем текст промпта на лету
    prompt_teg = dealer_info.get("Prompt_Teg", "base")
    prompt_ver = dealer_info.get("Prompt_Ver", "v01.00")
    prompt_key = f"{prompt_teg}_{prompt_ver}"
    scenario = user_context.get('scenario_type', 'unknown')
    
    raw_system_prompt = PROMPTS_CACHE.get(prompt_key, {}).get(scenario, "Вы — ИИ-ассистент автосалона.")
    
    system_prompt_ready = raw_system_prompt \
        .replace("[dealer_name]", dealer_info.get("dealer_name", "")) \
        .replace("[dealer_address]", dealer_info.get("dealer_address", "")) \
        .replace("[dealer_hours]", dealer_info.get("dealer_hours", ""))

    payload = {
        "action": "chat",
        "user_id": max_user_id,
        "session_id": f"sess_{max_user_id}_{user_context.get('start_code', 'default')}",
        "message": text,
        "user_data": {"name": user_name},
        "qualification_context": {
            "request_type": scenario,
            "payment": user_context.get('payment', 0),
            "model": user_context.get('model', ''),
            "model_id": user_context.get("model_id"),         # ДОБАВИТЬ ЭТО
            "complectation_id": user_context.get("complectation_id"), # ДОБАВИТЬ ЭТО
            # 3.4.1: из карточки дилера (как в send_handoff_webhook) — в продолжении
            # диалога ключа dealer_id в контексте нет, n8n искал сток по пустому Dealer_id
            "dealer_id": dealer_info.get("dealer_id") or user_context.get("dealer_id"),
            "system_prompt": system_prompt_ready,
            "cached_json": user_context.get('n8n_cache', None),
            "deal_id": deal_ref(user_context.get('deal_id')),
            "last_bot_message": user_context.get('last_bot_message', '') # <--- ДОБАВЛЕНО ДЛЯ AI-РЕМАЙНДЕРА
        },
        "data_routing": db_config
    }

    # [Carryover] Данные оценки auto.ru переживают пересборку json_cache —
    # шлём на каждом ходе (контракт docs/N8N_HANDOFF_CABINET.md; wiring-only,
    # логика в payload_v2). Нет данных → ключ отсутствует вовсе.
    _carry = payload_v2.build_evaluation_carryover(user_context)
    if _carry is not None:
        payload["qualification_context"]["evaluation_carryover"] = _carry

    # [U7] Кабинет: ссылка и статус в qualification_context — общий блок
    # _attach_cabinet_context (wiring-only)
    try:
        async with AsyncSessionLocal() as _cab_sess:
            _cab_user = (await _cab_sess.execute(
                select(User).where(User.platform_user_id == str(max_user_id)))).scalar_one_or_none()
    except Exception as _cab_err:
        logger.error(f"❌ [Cabinet U7] cabinet-контекст (chat) не добавлен: {_cab_err}")
        _cab_user = None
    if _cab_user is not None:
        await _attach_cabinet_context(
            payload, _cab_user, dealer_code,
            deal_id=user_context.get('deal_id') or _cab_user.active_deal_id,
            scenario=scenario, user_context=user_context, label="chat")

    # [U4] Payload v2: recent_history + session_context (слоты, флаги, эвристическая
    # стадия, channel, version_set, поля дилера для v3.0). Старый воркфлоу эти
    # поля игнорирует (R30).
    prev_stage = user_context.get('current_stage')
    payload_v2.extend_payload(payload, user_context, text, prompt_key, dealer_info)
    new_stage = payload["session_context"]["current_stage"]
    user_context['current_stage'] = new_stage

    # [U6] Свежесть каталога (R17): TTL 10 мин или переход на стадию расчёта/финализации
    # → пустой cached_json заставит n8n перезапросить Car Search (механизм первого хода).
    if payload_v2.should_refresh_cache(user_context, new_stage, prev_stage):
        selected = payload_v2.extract_selected_complectation(user_context.get('n8n_cache'))
        if selected and not payload["qualification_context"].get("complectation_id"):
            payload["qualification_context"]["complectation_id"] = selected
        payload["qualification_context"]["cached_json"] = None
        log_session(max_user_id, "♻️ [U6] cached_json сброшен (TTL/переход стадии) — n8n перезапросит каталог")

    # [U9] Shadow-резолвер: fire-and-forget, включается переменной N8N_RESOLVER_SHADOW_URL,
    # основной ход не ждёт и не зависит от результата (R21)
    shadow_resolver.fire_shadow(payload)

    # Настраиваем таймаут: 120 секунд (с учетом тяжелых расчетов ИИ)
    timeout = aiohttp.ClientTimeout(total=120)
    max_retries = 2

    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=n8n_auth.n8n_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, list) and len(data) > 0:
                            return data[0]
                        return data
                    else:
                        log_session(max_user_id, f"⚠️ Ошибка n8n (статус {resp.status}). Попытка {attempt + 1} из {max_retries}", "WARNING")
        except asyncio.TimeoutError:
            log_session(max_user_id, f"⏳ Таймаут n8n (120 сек). Попытка {attempt + 1} из {max_retries}", "WARNING")
        except Exception as e:
            log_session(max_user_id, f"💥 AI Chat error: {e}. Попытка {attempt + 1} из {max_retries}", "ERROR")

        # Пауза перед повторной попыткой (если это не последний раз)
        if attempt < max_retries - 1:
            await asyncio.sleep(3)

    return None # Возвращаем None, если все попытки провалились

async def send_handoff_webhook(max_user_id: str, user_context: dict, user_db_data: User):
    url = os.getenv("N8N_WEBHOOK_URL")
    if not url: return None

    start_code = user_context.get('start_code', '')
    dealer_code = start_code[:2] if len(start_code) >= 2 else "00"
    model_id = start_code[2:4] if len(start_code) >= 4 else "00"
    complectation_id = start_code[4:6] if len(start_code) >= 6 else "00"

    dealer_info = CACHED_DEALERS.get(dealer_code, {})
    db_config = dealer_info.get("db_config", {}) 

    # 1. Формируем текст промпта на лету
    prompt_teg = dealer_info.get("Prompt_Teg", "base")
    prompt_ver = dealer_info.get("Prompt_Ver", "v01.00")
    prompt_key = f"{prompt_teg}_{prompt_ver}"
    scenario = user_context.get('scenario_type', 'unknown')
    
    raw_system_prompt = PROMPTS_CACHE.get(prompt_key, {}).get(scenario, "Вы — ИИ-ассистент автосалона.")
    
    system_prompt_ready = raw_system_prompt \
        .replace("[dealer_name]", dealer_info.get("dealer_name", "")) \
        .replace("[dealer_address]", dealer_info.get("dealer_address", "")) \
        .replace("[dealer_hours]", dealer_info.get("dealer_hours", ""))

    # [План 2026-09-08-001, U5/KTD-16] Канонизация воронкового слота номера
    # перед хендофом: «T582XP 33 rus» → «Т582ХР33» — Александр не стартует
    # с мусорного номера. Не привёлся к канону → исходный ввод сохраняется
    # (fail-open, полный путь закрывает инструмент оценки на стороне n8n).
    _plate_canon = payload_v2.normalize_plate_input(user_context.get('plate'))
    if _plate_canon and _plate_canon != user_context.get('plate'):
        log_session(max_user_id, f"🔤 [Plate] Канонизация слота воронки: "
                                 f"{user_context.get('plate')!r} → {_plate_canon!r}")
        user_context['plate'] = _plate_canon

    payload = {
        "user_id": max_user_id,
        "session_id": f"sess_{max_user_id}_{user_context.get('start_code', 'default')}",
        "message": "INIT_AI_HANDOFF",
        "user_data": {
            "name": user_db_data.full_name,
            "username": user_db_data.username,
            "phone": user_db_data.phone if is_valid_phone(user_db_data.phone) else None
        },
        "qualification_context": {
            "dealer_code": dealer_code,
            "dealer_id": dealer_info.get("dealer_id"), 
            "model_id": model_id,             
            "complectation_id": complectation_id, 
            "request_type": scenario,
            "system_prompt": system_prompt_ready,   # <--- ДОБАВЛЕНО (Готовый промпт)
            "payment": user_context.get('payment'),
            "plate": user_context.get('plate'),
            "price": user_context.get('price'),
            "deal_id": deal_ref(user_db_data.active_deal_id)
        },
        "data_routing": db_config
    }

    # [Carryover] Данные оценки auto.ru — и на INIT-ходе (контракт
    # docs/N8N_HANDOFF_CABINET.md; wiring-only, логика в payload_v2)
    _carry = payload_v2.build_evaluation_carryover(user_context)
    if _carry is not None:
        payload["qualification_context"]["evaluation_carryover"] = _carry

    # [U7] Первый AI handoff (и ветка A): выдача ссылки кабинета клиенту без
    # телефона + статус в qualification_context — общий блок _attach_cabinet_context
    await _attach_cabinet_context(
        payload, user_db_data, dealer_code,
        deal_id=user_db_data.active_deal_id,
        scenario=scenario, user_context=user_context, label="handoff")

    # [U4] Payload v2 и на INIT-ходе: телефон из воронки попадает в слоты сразу
    if user_db_data.phone:
        user_context['phone_collected'] = True
    payload_v2.extend_payload(payload, user_context, "", prompt_key, dealer_info)
    user_context['current_stage'] = payload["session_context"]["current_stage"]

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=n8n_auth.n8n_headers()) as resp:
                if resp.status == 200:
                    log_session(max_user_id, f"🤖 AI Handoff successful for {max_user_id}")
                    data = await resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        return data[0]
                    return data
                return None
    except Exception as e:
        log_session(max_user_id, f"💥 AI Handoff connection error: {e}", "ERROR")
        return None


# --- Базы Данных (NocoDB) ---
async def load_data_from_nocodb():
    """Загружает настройки дилеров из NocoDB (включая конфигурацию баз данных для Smart-bot)"""
    if not NOCODB_TOKEN: return
    headers = {"xc-token": NOCODB_TOKEN}
    try:
        url_d = f"{NOCODB_API_URL}/api/v2/tables/{NOCODB_TABLE_ID_DEALERS}/records"
        async with aiohttp.ClientSession() as session:
            async with session.get(url_d, headers=headers, params={"limit": 100}, ssl=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    global CACHED_DEALERS, _CATALOG_TABLES_SEEN
                    CACHED_DEALERS.clear()
                    # КТД-1 (план 2026-09-25-001): id всех `* table id`-колонок
                    # каждой строки → allowlist /catalog; {tid: (колонка, дилер)}
                    # для техалерта о расширении доступа
                    catalog_tables_info = {}
                    for row in data.get("list", []):
                        d_code = str(row.get('Dealer_Code', '')).strip()
                        if d_code:
                            comm_type = row.get("Communication_Type_MAX", "Chat")
                            if not comm_type or str(comm_type).strip() == "":
                                comm_type = "Chat"
                            comm_type_td = row.get("Communication_Type_TD", comm_type)
                            if not comm_type_td or str(comm_type_td).strip() == "":
                                comm_type_td = comm_type
                            comm_type_tg = row.get("Communication_Type", comm_type)
                            if not comm_type_tg or str(comm_type_tg).strip() == "":
                                comm_type_tg = comm_type

                            # КТД-1: все `* table id`-колонки строки —
                            # публичные каталожные таблицы (allowlist)
                            _tbl_cols = _dealer_table_columns(row)
                            dealer_catalog_tables = set(_tbl_cols)
                            for _tid, _col in _tbl_cols.items():
                                catalog_tables_info.setdefault(_tid, (_col, d_code))

                            catalog_base = row.get('Sourse base id')
                            catalog_table = row.get('Sourse table id')
                            stock_base = row.get('Stock_Base_ID') or catalog_base
                            stock_table = row.get('Stock_Table_ID') or catalog_table
                            program_table = row.get('Program_Table_ID')
                            rates_table = row.get('Rates_Table_ID')
                            catalog_active_field, bad_active_field = _dealer_catalog_active_field(row)
                            if bad_active_field is not None:
                                logger.warning("TECH_ALERT [catalog-active-field] дилер %s: "
                                               "Catalog_Active_Field=%r — взято %s",
                                               d_code, bad_active_field, CATALOG_ACTIVE_FIELD_DEFAULT)
                                await bot_registry.tech_alert(
                                    f"catalog-active-field:{d_code}:{bad_active_field}",
                                    f"⚠️ Дилер {row.get('Dealer_name') or d_code} (код {d_code}): неверная "
                                    f"настройка Catalog_Active_Field = «{bad_active_field}» в Dealers "
                                    f"(допустимо Active или Active_<число>) — используется "
                                    f"«{CATALOG_ACTIVE_FIELD_DEFAULT}»",
                                    cooldown=86400)

                            # --- НОВОЕ: Парсинг картинки ремайндера ---
                            raw_img = row.get("Reminder_Image", [])
                            reminder_url = ""
                            if isinstance(raw_img, list) and len(raw_img) > 0:
                                img_obj = raw_img[0]
                                url_path = img_obj.get('signedUrl') or img_obj.get('url') or img_obj.get('path', '')
                                if url_path:
                                    reminder_url = url_path if url_path.startswith('http') else f"{NOCODB_API_URL}/{url_path.lstrip('/')}"
                            elif isinstance(raw_img, str) and raw_img.startswith('http'):
                                reminder_url = raw_img
                            
                            # --- НОВОЕ: Часовой пояс дилера ---
                            utc_offset = 3 # По умолчанию Московское время
                            try:
                                if row.get("UTC_Offset") is not None:
                                    utc_offset = float(row.get("UTC_Offset"))
                            except ValueError:
                                pass
                            # ------------------------------------------

                            CACHED_DEALERS[d_code] = {
                                "dealer_name": row.get('Dealer_name') or row.get('Name', 'Автосалон'),
                                "UTC_Offset": utc_offset,
                                "reminder_image_url": reminder_url,
                                "dealer_address": row.get('Dealer_address', 'Адрес уточняйте у менеджера'),
                                "dealer_hours": row.get('Dealer_workinghours', '09:00 - 21:00'),
                                "website": row.get('Website_URL', ''),
                                # [Кабинет, п.3 2026-08-31] Тексты согласия для
                                # поп-апов экрана авторизации (простой текст, не HTML)
                                "policy_text": row.get('Policy'),
                                "agreement_text": row.get('Agreement'),
                                "code": d_code,
                                # U6 мультибота (R15): нормализованный ключ бренда —
                                # сверка по strip+lower, не по отображаемой строке
                                "brand": str(row.get('Brand') or '').strip().lower(),
                                "table_id": catalog_table,
                                "catalog_tables": dealer_catalog_tables,
                                "Max_Group_ID": row.get('Max_Group_ID'),
                                "comm_type": comm_type,
                                "comm_type_td": comm_type_td,
                                "comm_type_tg": comm_type_tg,
                                "dealer_id": row.get('Dealer_id'),
                                # U13a: публичные настройки для лендинга (whitelist)
                                "landing": _dealer_landing_settings(row),
                                "Prompt_Teg": row.get('Prompt_Teg', 'base'),     # <--- ДОБАВЛЕНО
                                "Prompt_Ver": row.get('Prompt_Ver', 'v01.00'),   # <--- ДОБАВЛЕНО
                                "db_config": {
                                    "catalog_base_id": catalog_base,
                                    "catalog_table_id": catalog_table,
                                    "stock_base_id": stock_base,
                                    "stock_table_id": stock_table,
                                    "program_table_id": program_table,
                                    "rates_table_id": rates_table,
                                    # n8n-воркфлоу (Car Search / Combo, нода Get Rates) читают
                                    # исторический ключ credit_table_id — шлём оба имени
                                    "credit_table_id": rates_table,
                                    # 3.4.1: колонка активности каталога дилера → n8n
                                    # (data_routing.catalog_active_field — корень тела запроса к n8n)
                                    "catalog_active_field": catalog_active_field
                                }
                            }
                    _report_new_catalog_tables(catalog_tables_info)
                    logger.info(f"✅ [MAX v2.0] Loaded {len(CACHED_DEALERS)} dealers (with Smartbot Config).")
    except Exception as e:
        logger.error(f"❌ NocoDB load error: {e}")


async def dealers_refresh_worker(interval: int = 600):
    """U1 (план 2026-09-25-001, R5): периодическая перечитка справочника
    дилеров — новый дилер/новая таблица попадают в allowlist без рестарта.
    Ошибка перечитки кэш НЕ трогает (load_data_from_nocodb очищает кэш только
    при HTTP 200, R6); образец цикла — bot_registry.registry_worker."""
    logger.info("⏱ Dealers Refresh Worker started.")
    while True:
        try:
            await asyncio.sleep(interval)
            await load_data_from_nocodb()
        except asyncio.CancelledError:
            logger.info("🛑 Dealers Refresh Worker cancelled.")
            break
        except Exception:
            logger.exception("dealers_refresh: перечитка упала — кэш прежний, повтор через интервал")

async def warm_rates_programs_once() -> int:
    """Прогрев (2026-10-02): таблицы ставок и программ всех дилеров читаются
    заранее, а не первым запросом «открыть сессию» / снапшота."""
    tables = set()
    for cfg in list(CACHED_DEALERS.values()):
        db_cfg = (cfg or {}).get("db_config") or {}
        for key in ("rates_table_id", "program_table_id"):
            if db_cfg.get(key):
                tables.add(db_cfg[key])
    await asyncio.gather(*(get_cars_from_nocodb_dynamic(t, force=True) for t in tables),
                         return_exceptions=True)
    return len(tables)


async def rates_programs_warm_worker(interval: int = RATES_PROGRAMS_WARM_INTERVAL):
    """Прогрев ставок и программ при старте (как только загружен справочник
    дилеров) и затем каждые interval с — раньше истечения кэша CACHE_TTL."""
    logger.info("⏱ Rates/Programs Warm Worker started.")
    while True:
        try:
            if CACHED_DEALERS:
                n = await warm_rates_programs_once()
                logger.info("🔥 Прогрев ставок и программ: таблиц %s", n)
                await asyncio.sleep(interval)
            else:
                await asyncio.sleep(5)   # справочник дилеров ещё не загружен
        except asyncio.CancelledError:
            logger.info("🛑 Rates/Programs Warm Worker cancelled.")
            break
        except Exception:
            logger.exception("rates_programs_warm: прогрев упал — повтор через интервал")
            await asyncio.sleep(interval)


async def load_prompts_from_nocodb():
    """Скачивает блоки промптов из БД, оборачивает в теги и склеивает по сценариям."""
    if not NOCODB_TOKEN: return
    headers = {"xc-token": NOCODB_TOKEN}
    url = f"{NOCODB_API_URL}/api/v2/tables/{NOCODB_TABLE_ID_PROMPTS}/records"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params={"limit": 500}, ssl=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    records = data.get("list", [])
                    
                    # Группируем блоки: raw_blocks["base_v01.00"]["credit"] = [(id, "текст"), ...]
                    raw_blocks = defaultdict(lambda: defaultdict(list))
                    
                    for row in records:
                        if not row.get("Active"): continue # Пропускаем выключенные
                        
                        p_teg = str(row.get("Teg", "base")).strip()
                        p_ver = str(row.get("Version", "v01.00")).strip()
                        p_key = f"{p_teg}_{p_ver}"
                        
                        p_type = str(row.get("Type", "")).strip().lower()
                        p_block = str(row.get("Block", "")).strip()
                        p_content = str(row.get("Content", "")).strip()
                        p_id = str(row.get("id_block", "99")).strip()
                        
                        if not p_type or not p_block or not p_content: continue
                        
                        # Оборачиваем в теги автоматически
                        formatted_block = f"<{p_block}>\n{p_content}\n</{p_block}>"
                        raw_blocks[p_key][p_type].append((p_id, formatted_block))
                    
                    # Склеиваем финальные промпты
                    global PROMPTS_CACHE
                    PROMPTS_CACHE.clear()
                    
                    scenarios = ["credit", "tradein", "combo", "consult", "stock"]
                    
                    for p_key, types_dict in raw_blocks.items():
                        PROMPTS_CACHE[p_key] = {}
                        
                        # Собираем General блоки и сортируем по id_block
                        general_sorted = sorted(types_dict.get("general", []), key=lambda x: x[0])
                        general_text = "\n\n".join([item[1] for item in general_sorted])
                        
                        for scenario in scenarios:
                            scenario_sorted = sorted(types_dict.get(scenario, []), key=lambda x: x[0])
                            scenario_text = "\n\n".join([item[1] for item in scenario_sorted])
                            
                            # Итоговый промпт: General + Scenario
                            PROMPTS_CACHE[p_key][scenario] = f"{general_text}\n\n{scenario_text}".strip()
                            
                    logger.info(f"✅ [MAX v2.0] Prompts Cache Assembled. Profiles: {list(PROMPTS_CACHE.keys())}")
    except Exception as e:
        logger.error(f"❌ Prompts load error: {e}")

# Веб-вход (план 2026-09-30-001, U3): точечный запрос строки каталога по паре
# (Model id, Complectation id) — не зависит от лимита полной выгрузки таблицы
# (limit 100 в get_cars_from_nocodb_dynamic). Кэш на CACHE_TTL по паре.
CATALOG_ROW_CACHE: dict = {}   # (table_id, model_id, compl_id) -> (ts, row|None)


async def get_catalog_row_by_pair(table_id: str, model_id: str, compl_id: str):
    """Строка каталога NocoDB по паре или None (строки нет). Сетевой сбой или
    HTTP ≠ 200 — исключение: вызывающий отличает «нет строки» от «каталог
    недоступен» (cabinet_unavailable). Числовые пары передаются как есть —
    колонки «Model id» / «Complectation id» строковые."""
    if not table_id:
        return None
    key = (table_id, str(model_id), str(compl_id))
    cached = CATALOG_ROW_CACHE.get(key)
    now = time.time()
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]
    headers = {"xc-token": NOCODB_TOKEN}
    url = f"{NOCODB_API_URL}/api/v2/tables/{table_id}/records"
    where = f"(Model id,eq,{model_id})~and(Complectation id,eq,{compl_id})"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get(url, headers=headers,
                               params={"where": where, "limit": 1}, ssl=False) as resp:
            if resp.status != 200:
                raise RuntimeError(f"NocoDB HTTP {resp.status} (table {table_id})")
            data = await resp.json()
    rows = [r for r in (data.get("list") or []) if isinstance(r, dict)]
    # Внутри модели Complectation id бывает повторён (каталоги Geely, ALLCAR):
    # как n8n Car Search — первая строка в порядке NocoDB (поправка 2026-09-30)
    row = rows[0] if rows else None
    if row is not None:
        for field in ('Pics', 'Title_pic'):
            imgs = row.get(field)
            if isinstance(imgs, list):
                for img in imgs:
                    if isinstance(img, dict):
                        img.pop('thumbnails', None)
        row.pop('ncRecordHash', None)
    CATALOG_ROW_CACHE[key] = (now, row)
    return row


async def get_cars_from_nocodb_dynamic(table_id: str, force: bool = False) -> List[dict]:
    """Таблица NocoDB с кэшем CACHE_TTL; force — перечитать (прогрев ставок
    и программ по таймеру), при сбое остаётся прежняя копия."""
    if not table_id: return []
    now = time.time()
    if table_id in DICT_STOCKS and not force:
        last_update = STOCK_LAST_UPDATED.get(table_id, 0)
        if now - last_update < CACHE_TTL:
            return DICT_STOCKS[table_id]

    headers = {"xc-token": NOCODB_TOKEN}
    url = f"{NOCODB_API_URL}/api/v2/tables/{table_id}/records"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params={"limit": 100}, ssl=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    cars = data.get("list", [])
                    for car in cars:
                        for field in ('Pics', 'Title_pic'):
                            imgs = car.get(field)
                            if isinstance(imgs, list):
                                for img in imgs:
                                    img.pop('thumbnails', None)
                        car.pop('ncRecordHash', None)
                    DICT_STOCKS[table_id] = cars
                    STOCK_LAST_UPDATED[table_id] = now
                    logger.info(f"🔄 Stock refreshed for table {table_id}: {len(cars)} cars")
                    return cars
                else:
                    return DICT_STOCKS.get(table_id, []) 
    except Exception as e:
        return DICT_STOCKS.get(table_id, [])


# --- NLU & UI Helpers ---
def get_website_by_lead_code(lead_code: str) -> Optional[str]:
    if not lead_code or len(lead_code) < 2: return None
    dealer_prefix = lead_code[:2]
    for biz_id, data in CACHED_DEALERS.items():
        if data.get('code') == dealer_prefix:
            return data.get('website')
    return None

async def inject_prices_and_capture_model(max_user_id: str, user_text: str, bot_text: str, user_context_dict: dict) -> str:
    """Умный поиск авто (Prod-версия: CAPS LOCK + схлопывание одинаковых цен)."""
    found_car = None
    model_id = user_context_dict.get(max_user_id, {}).get('model_id')
    start_code = user_context_dict.get(max_user_id, {}).get('start_code') or ""
    
    target_table_id = NOCODB_TABLE_ID_CARS
    if len(start_code) >= 2:
        prefix = start_code[:2]
        for _, d_data in CACHED_DEALERS.items():
            if d_data.get('code') == prefix:
                target_table_id = d_data.get('table_id') or target_table_id
                break

    stock_list = await get_cars_from_nocodb_dynamic(target_table_id)
    
    complectation_id = user_context_dict.get(max_user_id, {}).get('complectation_id')
    if stock_list and model_id:
        for car in stock_list:
            car_id = str(car.get('ID', ''))
            if len(car_id) >= 4 and car_id[2:4] == str(model_id):
                # Если есть complectation_id — ищем точное совпадение
                if complectation_id and len(car_id) >= 6:
                    if car_id[4:6] == str(complectation_id):
                        found_car = car
                        break
                else:
                    # Если complectation_id нет — берём первую подходящую модель
                    if not found_car:
                        found_car = car
    elif stock_list:
        # Объединяем текущий ввод и сохраненный текст из диплинка для надежного поиска
        saved_model_text = USER_CONTEXT.get(max_user_id, {}).get('raw_model_text', '') \
                           or user_context_dict.get(max_user_id, {}).get('raw_model_text', '')
        search_text = f"{user_text} {saved_model_text}".upper()
        
        for car in stock_list:
            car_model_utm = str(car.get('Model utm', '') or '').upper()
            car_model = car.get('Model', '').upper()
            if car_model in search_text or (car_model_utm and car_model_utm in search_text):
                if car.get('Model title', '').upper() in search_text:
                    found_car = car; break
                if not found_car: found_car = car
                
    if found_car:
        model_name = found_car.get('Model', 'Автомобиль')
        user_context_dict[max_user_id]['model'] = model_name
        user_context_dict[max_user_id]['model_title'] = found_car.get('Model title', '')
        try:
            val_old = int(float(str(found_car.get('Price old', 0)).replace(' ','')))
            val_new = int(float(str(found_car.get('Price new', 0)).replace(' ','')))
            # --- НОВОЕ: Извлекаем прямую скидку из NOCODB ---
            val_cash = int(float(str(found_car.get('Cash in hook', 0)).replace(' ','')))
            
            p_old = '{0:,}'.format(val_old).replace(',', ' ')
            p_new = '{0:,}'.format(val_new).replace(',', ' ')
            p_cash = '{0:,}'.format(val_cash).replace(',', ' ')
        except:
            p_old, p_new, p_cash = str(found_car.get('Price old')), str(found_car.get('Price new')), "0"
            val_old, val_new = 0, 0
        
        # Схлопывание цены, если скидки нет
        if val_old == val_new and val_old > 0:
            bot_text = re.sub(
                r'\n*\s*Без скидки - \[Price old\] руб\.\n*\s*С максимальной скидкой - \[Price new\] руб\.',
                f' - {p_old} руб.',
                bot_text
            )
        
        # --- ДОБАВЛЕНА ЗАМЕНА ПЛЕЙСХОЛДЕРА [Cash Bonus] ---
        return bot_text.replace("[Price old]", p_old) \
                       .replace("[Price new]", p_new) \
                       .replace("[Cash Bonus]", p_cash) \
                       .replace("[Model]", model_name) \
                       .replace("[Model title]", found_car.get('Model title', ''))
    
    return bot_text.replace("[Price old]", "по запросу").replace("[Price new]", "по запросу").replace("[Model]", "Выбранный автомобиль").replace("[Model title]", "")

def inject_website(bot_text: str, lead_code: str = None) -> str:
    placeholders = ["[website]", "[СЮДА_ВСТАВЬТЕ_ВАШ_САЙТ]"]
    if any(p in bot_text for p in placeholders):
        link = get_website_by_lead_code(lead_code) if lead_code else None
        if not link: link = "https://      .ru"
        for p in placeholders:
            bot_text = bot_text.replace(p, link)
    return bot_text

async def reset_dialogflow_context(session_id):
    contexts_client = dialogflow.ContextsClient()
    session_path = f"projects/{PROJECT_ID}/locations/global/agent/sessions/{session_id}"
    try:
        contexts_client.delete_all_contexts(request={"parent": session_path})
    except Exception as e:
        logger.warning(f"Failed to reset DF contexts: {e}")

async def get_dialogflow_response(session_id, text, language_code="ru"):
    session_client = dialogflow.SessionsClient()
    session = f"projects/{PROJECT_ID}/locations/global/agent/sessions/{session_id}"
    try:
        uid = session_id.replace("user_", "")
        log_session(uid, f"🧠 [DF] Запрос: '{text}'")
        response = session_client.detect_intent(request={"session": session, "query_input": dialogflow.QueryInput(text=dialogflow.TextInput(text=text, language_code=language_code))})
        log_session(uid, f"🧠 [DF] Распознан Интент: '{response.query_result.intent.display_name}' (Уверенность: {response.query_result.intent_detection_confidence:.2f})")
        return response.query_result
    except Exception as e:
        logger.error(f"Dialogflow Error: {e}")
        return None

def get_keyboard_by_intent(intent_name: str, dealer_code: str = None) -> Optional[list]:
    """Возвращает клавиатуру в чистом JSON-формате для MAX API."""
    kb = []
    
    comm_type = "Chat"
    if dealer_code and dealer_code in CACHED_DEALERS:
        comm_type = CACHED_DEALERS[dealer_code].get("comm_type", "Chat")

    if intent_name == "00_Start_Lead_Request":
        kb = [[{"text": "В КРЕДИТ", "callback_data": "btn_credit"}],
              [{"text": "В ТРЕЙД-ИН", "callback_data": "btn_tradein"}],
              [{"text": "В КРЕДИТ + ТРЕЙД-ИН", "callback_data": "btn_combo"}],
              [{"text": "НЕ АКТУАЛЬНО", "callback_data": "btn_no"}]]
    elif intent_name in ["08_Scenario4_NotActual", "09_Refusal_NoNeed"]:
        kb = [[{"text": "АВТО В НАЛИЧИИ", "callback_data": "btn_stock"}],
              [{"text": "ДРУГАЯ МОДЕЛЬ", "callback_data": "btn_other"}],
              [{"text": "КОНСУЛЬТАЦИЯ МЕНЕДЖЕРА", "callback_data": "btn_consult"}],
              [{"text": "НИЧЕГО НЕ НУЖНО", "callback_data": "btn_stop"}]]
    elif intent_name in ["03_Payment_Input", "05_Plate_Input", "08_Plate_Complex_Input", "90_Stock_Request", "91_Consultation_Request"]:
        if comm_type == "Call":
            return None 
            
        kb = [[{"text": "В МЕССЕНДЖЕРЕ", "callback_data": "btn_tg"}],
              [{"text": "ПО ТЕЛЕФОНУ", "callback_data": "btn_phone"}]]

    elif intent_name in ["10_Contact_Phone", "12_Phone_Input"]:
        # Когда бот просит телефон, выводим специальную системную кнопку
        kb = [[{"type": "request_contact", "text": "ПОДТВЕРДИТЬ"}]]
              
    return kb if kb else None


# =======================
# 3. DATABASE WRAPPERS (Local Context)
# =======================
def _is_connection_drop(e: Exception) -> bool:
    """[U5 план 2026-09-08-002] Обрыв соединения БД (ночное окно PG ~06:00 UTC):
    sqlalchemy.exc.InterfaceError либо asyncpg ConnectionDoesNotExistError —
    матчим по имени, чтобы не тянуть asyncpg в SQLite-окружение."""
    return "InterfaceError" in type(e).__name__ or "ConnectionDoesNotExist" in repr(e)


async def save_context_to_db(user_id: str, ctx: dict) -> bool:
    """Сохраняет контекст; True — запись зафиксирована. [U5] Один ретрай
    свежей сессией при обрыве соединения: pre-ping чинит пул, но соединение,
    выданное до обрыва, всё равно умирает на первом использовании."""
    ctx_to_save = ctx.copy()
    if len(ctx_to_save.get('chat_history', [])) > 100:
        ctx_to_save['chat_history'] = ctx_to_save['chat_history'][-100:]
    payload = json.dumps(ctx_to_save, ensure_ascii=False)
    for attempt in (1, 2):
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(User).where(User.platform_user_id == user_id)
                    .values(context_json=payload)
                )
                await session.commit()
            return True
        except Exception as e:
            logger.error(f"❌ Context Save Error for {user_id} (попытка {attempt}/2): {e}")
            if not (_is_connection_drop(e) and attempt == 1):
                return False
    return False

async def load_context_from_db(user_id: str) -> dict:
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.platform_user_id == user_id))
            user = result.scalar_one_or_none()
            if user and user.context_json:
                return json.loads(user.context_json)
        return {}
    except Exception as e:
        logger.error(f"❌ Context Load Error for {user_id}: {e}")
        return {}

def normalize_payment(text: str) -> str:
    """Превращает человеческий ввод денег ('50т', '1.5 млн', '50k') в чистые цифры."""
    text = text.lower().replace(' ', '').replace(',', '.')
    
    # 1. Паттерны миллионов (например, 1.5млн, 2миллиона)
    match_m = re.search(r'(\d+(?:\.\d+)?)(?:млн|миллион)', text)
    if match_m:
        return str(int(float(match_m.group(1)) * 1000000))
        
    # 2. Паттерны тысяч (например, 50т, 50тыс, 50k, 50к)
    match_k = re.search(r'(\d+(?:\.\d+)?)(?:т|тыс|k|к)', text)
    if match_k:
        return str(int(float(match_k.group(1)) * 1000))
        
    # 3. Фолбэк: просто вытаскиваем все цифры
    digits = re.sub(r'\D', '', text)
    if digits:
        return digits
        
    return text

async def fetch_n8n_logs_for_archive(session_id: str) -> str:
    """Делает HTTP-запрос к n8n для получения логов. Возвращает форматированную JSON-строку."""
    # Очищаем префикс 'sess_', так как в вашем SQL запросе n8n используется '%sess_{{ $json.query.session_id }}%'
    clean_session_id = session_id.replace("sess_", "") if session_id else ""
    # Адрес — из env (2026-10-03): воркфлоу логов защищается заголовком и
    # может переехать; пусто — логи не запрашиваются вовсе
    url = os.getenv("N8N_SESSION_LOGS_URL", "").strip()
    if not url or not clean_session_id:
        return ""

    timeout_config = aiohttp.ClientTimeout(total=10) # Увеличили до 10 секунд
    try:
        async with aiohttp.ClientSession(timeout=timeout_config) as http_sess:
            async with http_sess.get(url, params={"session_id": clean_session_id},
                                     headers=n8n_auth.n8n_headers()) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logs = data.get("logs", data) if isinstance(data, dict) else data
                    if logs:
                        return json.dumps(logs, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"⚠️ n8n не ответил за 15 сек для сессии {session_id}: {e}")
    return ""

async def upsert_dialogue_archive(archive_payload: dict):
    """Ищет диалог в NocoDB по session_id. Если находит - обновляет (PATCH), if нет - создает (POST)."""
    if not NOCODB_TOKEN or not NOCODB_API_URL: return
    
    headers = {"xc-token": NOCODB_TOKEN, "Content-Type": "application/json"}
    base_url = f"{NOCODB_API_URL}/api/v2/tables/{NOCODB_TABLE_ID_ARCHIVE}/records"
    session_id = archive_payload.get("session_id")
    
    # 1. Автоматический подшив логов n8n. Пусто — ключ "logs" не кладётся:
    # PATCH существующей записи иначе затирал ранее сохранённые логи n8n
    # (2026-10-03); "no_n8n_data" — только новой записи (ниже, при POST)
    archive_payload.pop("logs", None)
    if session_id:
        try:
            fetched_logs = await fetch_n8n_logs_for_archive(session_id)
            if fetched_logs:
                archive_payload["logs"] = fetched_logs
        except Exception as e:
            # Перехватываем ошибку отсутствия данных/таймаута и продолжаем работу
            logger.warning(f"Логи n8n не найдены для сессии {session_id} (скорее всего, не дошла до n8n): {e}")
            
    # 2. Автоматический подшив логов Python из контекста сессии
    max_user_id = archive_payload.get("user_id")
    if max_user_id:
        ctx = USER_CONTEXT.get(str(max_user_id)) or USER_CONTEXT.get(max_user_id)
        if ctx:
            python_logs_array = ctx.get('python_logs', [])
            if python_logs_array:
                archive_payload["python_logs"] = "\n".join(python_logs_array)
    
    try:
        async with aiohttp.ClientSession() as session:
            # 1. Ищем существующую запись
            search_url = f"{base_url}?where=(session_id,eq,{session_id})"
            async with session.get(search_url, headers=headers, ssl=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    records = data.get("list", [])
                    if records:
                        # 2. ЗАПИСЬ НАЙДЕНА -> Обновляем (PATCH)
                        record_id = records[0]["Id"]
                        archive_payload["Id"] = record_id
                        await session.patch(base_url, headers=headers, json=archive_payload, ssl=False)
                        logger.info(f"🔄 NocoDB Archive UPDATED for {session_id}")
                        return
            
            # 3. ЗАПИСИ НЕТ -> Создаем новую (POST)
            archive_payload.setdefault("logs", "no_n8n_data")
            await session.post(base_url, headers=headers, json=archive_payload, ssl=False)
            logger.info(f"💾 NocoDB Archive CREATED for {session_id}")
    except Exception as e:
        logger.error(f"❌ NocoDB Upsert Error: {e}")

# =======================
# 4. CORE ROUTING LOGIC
# =======================
async def check_and_link_deal(db_user: User, code: str):
    """Сразу после старта ищет сделку с сайта, чтобы привязать к ней клиента."""
    if not code: return
    logger.info(f"🔎 Force searching for NEW deal with code {code} only in C9:NEW")
    
    deal_data = await BitrixService.find_deal_data_by_code(code, stage_id="C9:NEW", user_id=db_user.platform_user_id)
    if deal_data and deal_data.get('STAGE_ID') == 'C9:NEW':
        deal_id = str(deal_data['ID'])
        contact_id = deal_data.get('CONTACT_ID')
        logger.info(f"🔗 Linking user {db_user.platform_user_id} to Deal {deal_id}")
        
        async with AsyncSessionLocal() as session:
            await session.execute(update(User).where(User.platform_user_id == db_user.platform_user_id).values(active_deal_id=deal_id))
            await session.commit()
        
        db_user.active_deal_id = deal_id
        USER_CONTEXT[db_user.platform_user_id]['deal_id'] = deal_id
        
        if contact_id:
            await BitrixService.update_contact(
                contact_id,
                db_user.full_name or "Клиент",
                db_user.phone if is_valid_phone(db_user.phone) else "",
                db_user.username or "",
                user_id=db_user.platform_user_id
            )
        
        site_url = get_website_by_lead_code(code)
        deal_updates = {}
        if site_url: deal_updates['site_source'] = site_url
            
        await BitrixService.update_deal_data(deal_id, deal_updates, BitrixService.STAGE_SUCCESS,
                                             user_id=db_user.platform_user_id, expect_stage="C9:NEW")

async def background_deal_hunt(bot_deal_id: str, code: str, context: dict, max_user_id: str, full_name: str, phone: str, username: str):
    """Фоновая охота за дублями (ждет, пока сайт создаст сделку в Bitrix24)."""
    logger.info(f"🕵️‍♂️ Started Deal Hunt for Code {code}")
    delays = [900] * 8 # 8 попыток x 15 минут = 2 часа ожидания
    for attempt, wait_time in enumerate(delays):
        logger.info(f"⏳ Hunt {code}: sleeping {wait_time}s")
        await asyncio.sleep(wait_time)
        
        deal_data = await BitrixService.find_deal_data_by_code(code, stage_id="C9:NEW", user_id=max_user_id)
        
        if deal_data:
            original_id = str(deal_data['ID'])
            current_stage = deal_data.get('STAGE_ID')
            
            if current_stage != "C9:NEW":
                continue 
            
            logger.info(f"🎯 Hunt Success! Original deal {original_id} found in NEW stage.")
            
            # Читаем актуальное состояние сессии из БД на момент находки
            # [U7] ДО переноса контакта: телефон мог подтвердиться в кабинете
            # после старта охоты — подтверждённый номер входит в перенос склейки
            async with AsyncSessionLocal() as hunt_session:
                hunt_res = await hunt_session.execute(
                    select(User).where(User.platform_user_id == max_user_id))
                hunt_user = hunt_res.scalar_one_or_none()

            fresh_phone = (hunt_user.phone if hunt_user and hunt_user.phone else phone) or ""
            # Перенос 3.1.2 поверх кабинетного fresh_phone: невалидный номер
            # в контакт не уходит (адаптация ханка `phone or ""`)
            fresh_phone = fresh_phone if is_valid_phone(fresh_phone) else ""

            # То же единое правило канала, что в final_sync_wrapper: охота
            # переносит поля на сделку Тильды, и без телефона её значение
            # источника (web/max/telegram) обязано пережить перенос.
            if fresh_phone:
                context['channel_val'] = "По телефону"
            else:
                context.pop('channel_val', None)

            contact_id = deal_data.get('CONTACT_ID')
            if contact_id:
                await BitrixService.update_contact(
                    contact_id,
                    full_name or "Клиент",
                    fresh_phone,
                    username or "",
                    user_id=max_user_id
                )

            if code:
                site_url = get_website_by_lead_code(code)
                if site_url:
                    context['site_source'] = site_url

            live_ctx = USER_CONTEXT.get(max_user_id) or {}
            is_finalized = live_ctx.get('is_finalized', False)
            has_phone = is_valid_phone(hunt_user.phone if hunt_user else phone)
            is_smartbot_active = getattr(hunt_user, 'is_smartbot_active', False)

            if is_finalized and not has_phone:
                target_stage = "C9:LOSE"
            elif has_phone:
                target_stage = BitrixService.STAGE_QUALIFIED
            elif is_smartbot_active:
                target_stage = "C9:UC_CKY0IR"
            else:
                target_stage = BitrixService.STAGE_SUCCESS

            logger.info(f"🕵️ [HUNT] State: is_finalized={is_finalized}, "
                        f"has_phone={has_phone}, "
                        f"is_smartbot_active={is_smartbot_active} "
                        f"→ target_stage={target_stage}")

            # Стадия — ПОСЛЕДНЕЙ: сначала поля и переотправка анкеты кабинета
            # (rebind ниже), потом смена стадии (исправление 2026-09-30)
            hunt_fields_ok = await BitrixService.update_deal_data(original_id, context, None, user_id=max_user_id)
            await BitrixService.close_as_duplicate(bot_deal_id, original_id)

            # [План 2026-08-21-002, U2] Согласование «сделки диалога» с токеном
            # ДО rebind — зеркальный гард внутри хелпера
            try:
                await sync_dialog_deal_refs(max_user_id, str(bot_deal_id), str(original_id))
            except Exception as _sync_err:
                logger.error(f"❌ [Cabinet U2] согласование active_deal_id при склейке: {_sync_err}")

            # [U7] Склейка кабинета: токен сохраняется (меняется только deal_id),
            # анкеты сливаются, pending_sync переписывается; при уже прошедшей
            # передаче — переотправка результата на основную сделку (wiring-only)
            try:
                await cabinet_service.rebind_cabinet_on_merge(bot_deal_id, original_id, max_user_id)
            except Exception as _cab_err:
                hunt_fields_ok = False
                logger.error(f"❌ [Cabinet U7] rebind_cabinet_on_merge не выполнен: {_cab_err}")
            await BitrixService.set_stage_after_fields(original_id, target_stage, hunt_fields_ok,
                                                       user_id=max_user_id,
                                                       expect_stage=current_stage)
            return
            
    await BitrixService.move_to_manual(bot_deal_id)

async def process_input(user_id: str, user_text: str, user_name: str, user_username: str, input_type: str = "text", channel: str = "max", bot_key: str = None):
    """Единое ядро маршрутизации бота."""
    max_user_id = user_id  # алиас для совместимости внутри функции
    # U3 мультибота: бот сессии — всегда в ctx и в users (KTD-5/R12); вызовы
    # без bot_key (легаси-пути, web) получают ключ по каналу; U9 —
    # окружение-зависимый легаси-ключ + резолв исторических имён
    if bot_key is None:
        bot_key = ("web" if channel == "web"
                   else bot_registry.legacy_key_for_channel(channel))
    else:
        bot_key = bot_registry.resolve_alias(bot_key)
    db_user = await get_or_create_user(user_id, user_name, user_username, channel=channel,
                                       bot_key=bot_key,
                                       raw_platform_id=bot_identity.raw_id_for_send(user_id))

    if user_id not in USER_CONTEXT:
        loaded = await load_context_from_db(user_id)
        USER_CONTEXT[user_id] = loaded if loaded is not None else {}
    USER_CONTEXT[user_id]['channel'] = channel  # всегда перезаписываем channel из параметра
    USER_CONTEXT[user_id]['bot_key'] = bot_key

    ctx = USER_CONTEXT[user_id]
    
    # --- ОТМЕНА ТАЙМЕРА И МГНОВЕННЫЙ ПЕРЕХВАТ ВОЗРАЖЕНИЙ ---
    timer_cancelled = False
    if max_user_id in SMARTBOT_HANDOFF_TASKS:
        # Если это системный вызов от самого таймера, мы НЕ отменяем его (иначе убьем процесс)
        if user_text != "__SYSTEM_TIMEOUT_HANDOFF__":
            SMARTBOT_HANDOFF_TASKS[max_user_id].cancel()
            timer_cancelled = True
        
        del SMARTBOT_HANDOFF_TASKS[max_user_id] # Просто удаляем из памяти

    # Исключаем системные команды кнопок из текстового перехвата
    if timer_cancelled and input_type != "deeplink" and user_text not in ["__SYSTEM_TIMEOUT_HANDOFF__", "НИЧЕГО НЕ НУЖНО"]:
        
        # 1. Очищаем от мусора
        digits_only = re.sub(r'\D', '', user_text or "")
        
        # 2. Проверяем длину (10 или 11 цифр - стандарт)
        is_length_ok = 10 <= len(digits_only) <= 11
        
        # 3. Проверяем, что номер начинается на 7, 8 или 9
        starts_correctly = False
        if is_length_ok:
            first_digit = digits_only[0]
            if len(digits_only) == 11 and first_digit in ['7', '8']:
                starts_correctly = True
            elif len(digits_only) == 10 and first_digit == '9':
                starts_correctly = True

        # 4. Проверка на "чистоту": букв должно быть меньше 5
        letters_only = re.sub(r'[^a-zA-Zа-яА-ЯёЁ]', '', user_text or "")
        is_pure_number = len(letters_only) < 5

        looks_like_phone = is_length_ok and starts_correctly and is_pure_number
        
        # Если клиент написал текст вместо номера (возражение/вопрос)
        if not looks_like_phone:
            log_session(ctx, f"🛑 [INTERCEPT] Client wrote text instead of phone. Triggering immediate AI Handoff.")
            sys_msg = "Переводим диалог на специалиста"

            _cl, _raw = get_sender(user_id, ctx)
            if _cl is not None:
                await _cl.send_message(user_id=_raw, text=sys_msg)

            ctx['chat_history'].append({"time": datetime.now().strftime("%H:%M"), "role": "bot", "text": sys_msg})
            await save_context_to_db(user_id, ctx)

            await asyncio.sleep(1.5)

            try:
                chat_id = ctx.get("chat_id")
                try:
                    target_id = int(chat_id) if chat_id else int(user_id)
                except (ValueError, TypeError):
                    target_id = None
                logger.info(f"✍️ [UI] Включаем тайпинг для ID {target_id}")
                if target_id is not None:
                    asyncio.create_task(_send_typing_for(ctx, target_id))
            except Exception as e:
                logger.error(f"❌ Ошибка старта тайпинга: {e}")

            # 3. Ждем оставшиеся 3.5 секунд (в сумме получаются те же 5 сек задержки)
            await asyncio.sleep(3.5)
            
            # Подменяем текст, чтобы пропустить Dialogflow и сразу запустить Handoff
            user_text = "__SYSTEM_TIMEOUT_HANDOFF__"

    # ==========================================
    # --- ТЕХНИЧЕСКИЙ АНТИСПАМ (RATE LIMITING) ---
    # ==========================================
    now_ts = time.time()
    spam_data = ctx.setdefault('spam_control', {'count': 0, 'first_msg_time': now_ts, 'is_blocked': False})
    
    # Если клиент уже в глухом бане — проверяем время разбана
    if spam_data['is_blocked']:
        # Разбан через 15 минут (900 секунд)
        if now_ts - spam_data['first_msg_time'] > 900:
            spam_data['is_blocked'] = False
            spam_data['count'] = 0
            spam_data['first_msg_time'] = now_ts
        else:
            return # Молча игнорируем сообщение и не пускаем дальше по коду

    # Считаем сообщения в окне 60 секунд
    if now_ts - spam_data['first_msg_time'] < 60:
        spam_data['count'] += 1
        # Если написал больше 10 сообщений за минуту — БАН
        if spam_data['count'] > 10:
            log_session(ctx, f"🛑 ANTI-SPAM TRIGGERED for user {max_user_id}. Blocking for 15 mins.", "WARNING")
            spam_data['is_blocked'] = True
            spam_data['first_msg_time'] = now_ts
            _cl, _raw = get_sender(user_id, ctx)
            if _cl is not None:
                await _cl.send_message(user_id=_raw, text="⚠️ Вы отправляете слишком много сообщений. Пожалуйста, подождите 15 минут.")
            
            # Принудительно отключаем ИИ
            async with AsyncSessionLocal() as session:
                await session.execute(update(User).where(User.platform_user_id == max_user_id).values(is_smartbot_active=False))
                await session.commit()
            return
    else:
        # Окно (60 сек) прошло штатно — сбрасываем счетчик
        spam_data['count'] = 1
        spam_data['first_msg_time'] = now_ts
        
    # ==========================================
    
    # --- ЛОГИРОВАНИЕ: Сохраняем сообщение клиента ---
    history = ctx.setdefault('chat_history', [])
    current_time = datetime.now().strftime("%H:%M")
    
    # U7a: снимок активности ДО обновления таймеров — окно анти-дубля голого
    # старта не должно продлеваться самим обновлением этого же входа
    _prev_interaction = ctx.get('last_interaction_time') or 0

    # [НОВОЕ] Обновляем таймер ИМЕННО КЛИЕНТСКОГО сообщения и сбрасываем уровень ремайндеров
    ctx['client_last_message_time'] = time.time()
    ctx['reminder_level'] = 0
    # Клиент снова пишет — пометка закрытого диалога, кэш и счётчик первого
    # напоминания снимаются (план 2026-10-12-002, KTD-C2/C3)
    if ctx.pop(MAX_SEND_BLOCKED_KEY, None) is not None:
        log_session(ctx, "✅ [MAX] Клиент снова пишет — пометка закрытого диалога снята")
    ctx.pop('reminder_1_pending', None)
    ctx.pop('reminder_1_failures', None)
    
    ctx['last_interaction_time'] = time.time() # Таймер для отлова "брошенных" диалогов
    
    # Не пишем техническую команду диплинка как фразу клиента
    if user_text and not user_text.startswith("start "):
        history.append({"time": current_time, "role": "user", "type": input_type, "text": user_text})
        # СТАЛО: Сохраняем фразу/кнопку для отчета (если это не системная команда)
        if not user_text.startswith("__SYSTEM"):
            ctx['last_client_event'] = user_text
            
    session_id = f"user_{max_user_id}"

    # 1. Перехват Smartbot (AI Агент)
    if getattr(db_user, 'is_smartbot_active', False):
        if CODE_REGEX.search(user_text or ""):
            pass # Если ввели новый код - идем на сброс сессии
        else:
            # Складываем текущую фразу в буфер
            USER_MESSAGE_BUFFERS[max_user_id].append(user_text)

            # Если таймер уже тикает — отменяем его (клиент еще пишет мысль)
            if max_user_id in USER_MESSAGE_TASKS:
                USER_MESSAGE_TASKS[max_user_id].cancel()

            # Запускаем новый таймер ожидания (3 секунды тишины + 15 секунд тайпинг)
            async def delayed_sender():
                try:
                    await asyncio.sleep(3.0)
                    
                    # Включаем индикатор "Александр печатает..."
                    chat_id = ctx.get("chat_id")
                    if chat_id:
                        try:
                            _typing_id = int(chat_id)
                        except (ValueError, TypeError):
                            _typing_id = None
                        if _typing_id is not None:
                            asyncio.create_task(_send_typing_for(ctx, _typing_id))
                        
                    await asyncio.sleep(10.0)
                except asyncio.CancelledError:
                    # Если клиент прислал новое сообщение, таймер прерывается
                    raise
                
                # [U5] Дебаунс закончен — снимаем себя с реестра: новое сообщение
                # больше не отменит ход посреди запроса к n8n, а ляжет в буфер
                # и уйдёт следующим ходом (R29).
                if USER_MESSAGE_TASKS.get(max_user_id) is asyncio.current_task():
                    USER_MESSAGE_TASKS.pop(max_user_id, None)

                # [U5] Сериализация: один round-trip к n8n на пользователя за раз
                async with get_turn_lock(max_user_id):
                    # [План 2026-08-14-001, U3] Перепроверка ПОД локом свежим
                    # чтением: сессия могла завершиться (передача кабинета,
                    # финализация), пока таймер ждал/лок был занят — уже
                    # созданный ход не должен уносить буфер в n8n (R2)
                    async with AsyncSessionLocal() as _gs:
                        _alive = (await _gs.execute(
                            select(User.is_smartbot_active)
                            .where(User.platform_user_id == max_user_id)
                        )).scalar_one_or_none()
                    if not _alive or USER_CONTEXT.get(max_user_id, {}).get('is_finalized'):
                        USER_MESSAGE_BUFFERS.pop(max_user_id, None)
                        # Ревью 2026-08-15: этот ход был «потребителем» pending_*
                        # (кабинетное завершение взяло pending-ветку по нашей
                        # таске) — не потребить их здесь значит задержать
                        # прощание до добора и оставить протухшие флаги в
                        # финализированном context_json (ложное прощание при
                        # повторном входе R7). Потребляем перед выходом.
                        _mem = USER_CONTEXT.get(max_user_id, {})
                        _pa = _mem.pop('pending_archive', None)
                        if _mem.pop('pending_farewell', False):
                            _tok = (_pa or {}).get('cabinet_token_id')
                            _upk = (_pa or {}).get('cabinet_user_pk')
                            if _tok is None or await cabinet_service.record_session_farewell(_upk, _tok):
                                _fw = build_farewell_text(_mem.get('channel', 'max'))
                                _cl, _raw = get_sender(max_user_id, _mem)
                                if _cl is not None:
                                    await _cl.send_message(user_id=_raw, text=_fw)
                        if _pa:
                            await finalize_transcript_archive(max_user_id, _pa)
                        log_session(ctx, "⛔ [Session Closed] Ход отменён: сессия завершена до отправки в n8n")
                        return

                    # Склеиваем всё, что клиент напечатал за это время
                    combined_text = " ".join(USER_MESSAGE_BUFFERS.pop(max_user_id, []))
                    if not combined_text.strip(): return

                    # --- РЕФЛЕКС: Перехват телефона прямо в процессе диалога с ИИ ---
                    # План 2026-09-07-001 (Р1): извлекатель паритета живёт в payload_v2
                    # (тот же, что даёт флаг в session_context); решение о валидности
                    # принимает is_valid_phone. Лог извлечённого — наблюдаемость для
                    # разбора ложных срабатываний (риск Р-1, откат — revert коммита).
                    # session_id — для лога промахов белого списка (Д4, PHONE_PATTERN_MISS)
                    _sess_id = f"sess_{max_user_id}_{USER_CONTEXT[max_user_id].get('start_code', 'default')}"
                    _phone_found = payload_v2.extract_phone(combined_text, session_id=_sess_id)
                    if _phone_found:
                        log_session(max_user_id, f"📞 [Рефлекс] Извлечён телефон из текста: {_phone_found!r}")
                        # Р10: слот — только при фактической записи; отказ стража
                        # (OTP-номер не перезаписывается) слот не защёлкивает
                        if await save_user_phone(max_user_id, _phone_found, user_name, user_username, channel=channel):
                            USER_CONTEXT[max_user_id]['phone_collected'] = True

                    # --- ФИКС: Сохраняем БД (само сообщение уже было добавлено в массив в начале функции) ---
                    await save_context_to_db(max_user_id, USER_CONTEXT[max_user_id])

                    # --- PROFILER: N8N CALL ---
                    n8n_start = time.time()
                    log_session(ctx, f"⏱ [PROFILER] Отправляем в n8n (Ждали буфер: 10 сек)...")
                
                    # Отправляем склеенный текст в n8n (ТЕПЕРЬ ТОЛЬКО ОДИН РАЗ!)
                    ai_resp = await send_chat_message_to_n8n(max_user_id, combined_text, user_name, USER_CONTEXT.get(max_user_id, {}))
                
                    log_session(ctx, f"⏱ [PROFILER] Ответ от n8n получен за {time.time() - n8n_start:.2f} сек.")
                    # --------------------------
                
                    if ai_resp:
                        # УСПЕХ: Сохраняем ответ ИИ в стенограмму
                        # [U1] нормализация: [CABINET_LINK] → URL (R6)
                        bot_text = await normalize_bot_text_for_history(
                            max_user_id,
                            (ai_resp.get('text') or ai_resp.get('output') or "").replace('[SPLIT]', '\n\n'))
                        USER_CONTEXT[max_user_id]['chat_history'].append({
                            "time": datetime.now().strftime("%H:%M"), "role": "bot", "text": bot_text
                        })
                        await save_context_to_db(max_user_id, USER_CONTEXT[max_user_id])

                        # Отправляем ответ клиенту через сплиттер
                        await send_smartbot_reply(max_user_id, ai_resp)
                    else:
                        # --- ФОЛБЭК: Первичный сбой ИИ (Таймаут 90 сек) ---
                        log_session(ctx, f"⚠️ AI Agent не ответил для {max_user_id} за 90 сек. Проверяем наличие телефона...", "WARNING")

                        async with AsyncSessionLocal() as session:
                            res = await session.execute(select(User).where(User.platform_user_id == max_user_id))
                            usr = res.scalar_one_or_none()
                            has_phone = is_valid_phone(usr.phone if usr else None)

                        _fb_client, _fb_raw = get_sender(user_id, USER_CONTEXT.get(user_id))
                        if has_phone:
                            # СЦЕНАРИЙ А: ТЕЛЕФОН УЖЕ ЕСТЬ В БАЗЕ
                            async with AsyncSessionLocal() as session:
                                await session.execute(update(User).where(User.platform_user_id == max_user_id).values(is_smartbot_active=False))
                                await session.commit()

                            fallback_msg = "Прошу прощения, у меня небольшие технические неполадки с компьютером. Я уже передал вашу заявку менеджеру отдела продаж, он скоро свяжется с вами по телефону!"
                            if _fb_client is not None:
                                await _fb_client.send_message(user_id=_fb_raw, text=fallback_msg)

                            deal_id_str = USER_CONTEXT[max_user_id].get('deal_id')
                            # Ревью 2026-08-15: при кабинетной передаче в полёте
                            # (pending_archive) STAGE двигает конверсия кабинета —
                            # фолбэк не должен перетирать её своим статусом
                            if deal_id_str and not USER_CONTEXT[max_user_id].get('pending_archive'):
                                await update_deal_stage_direct(deal_id_str, "C9:UC_HW8Q2H")
                                # [План 2026-09-08-001, U3/B-1] только запись
                                # SB-полей из слотов, стадии не трогаются
                                _ai_fields = await cabinet_service.filter_cabinet_owned_fields(
                                    build_ai_tradein_fields(USER_CONTEXT[max_user_id]), deal_id_str)
                                if _ai_fields:
                                    logger.info("📤 AI_TRADEIN_FIELDS deal=%s source=slots(fallback) fields=%s",
                                                deal_id_str, _ai_fields)
                                    await BitrixService._request(
                                        "crm.deal.update",
                                        {"id": deal_id_str, "fields": _ai_fields},
                                        user_id=max_user_id)
                        else:
                            # СЦЕНАРИЙ Б: ТЕЛЕФОНА НЕТ (LONG RETRY 3 МИНУТЫ)
                            try:
                                # 1. Удерживаем клиента в чате
                                if _fb_client is not None:
                                    await _fb_client.send_message(user_id=_fb_raw, text="Прошу прощения, у меня небольшие технические неполадки с компьютером. Я у Вам вернусь буквально через пару минут ⏳")
                            
                                # 2. Ждем 3 минуты (180 секунд)
                                log_session(ctx, f"⏳ LONG RETRY: Ждем 180 сек для {max_user_id} перед повторной отправкой.")
                                await asyncio.sleep(180)
                            
                                # 3. Вторая (и последняя) попытка отправить запрос
                                log_session(ctx, f"🔄 LONG RETRY: Повторная попытка отправки для {max_user_id}.")
                                retry_resp = await send_chat_message_to_n8n(max_user_id, combined_text, user_name, USER_CONTEXT.get(max_user_id, {}))
                            
                                if retry_resp:
                                    # Успех! Сервер "отвис"
                                    # [U1] нормализация: [CABINET_LINK] → URL (R6)
                                    bot_text = await normalize_bot_text_for_history(
                                        max_user_id,
                                        (retry_resp.get('text') or retry_resp.get('output') or "").replace('[SPLIT]', '\n\n'))
                                    USER_CONTEXT[max_user_id]['chat_history'].append({
                                        "time": datetime.now().strftime("%H:%M"), "role": "bot", "text": bot_text
                                    })
                                    await save_context_to_db(max_user_id, USER_CONTEXT[max_user_id])
                                    await send_smartbot_reply(max_user_id, retry_resp)
                                else:
                                    # Окончательный провал. Просим телефон для ЗВОНКА.
                                    async with AsyncSessionLocal() as session:
                                        await session.execute(update(User).where(User.platform_user_id == max_user_id).values(is_smartbot_active=False))
                                        await session.commit()

                                    final_fallback = "К сожалению, не удается быстро решить проблему. Могли бы вы оставить номер телефона, чтобы коллеги смогли связаться с вами"
                                    if _fb_client is not None:
                                        await _fb_client.send_message(user_id=_fb_raw, text=final_fallback)
                                
                                    deal_id_str = USER_CONTEXT[max_user_id].get('deal_id')
                                    # См. комментарий в сценарии А: конверсию кабинета не перетираем
                                    if deal_id_str and not USER_CONTEXT[max_user_id].get('pending_archive'):
                                        await update_deal_stage_direct(deal_id_str, "C9:UC_HW8Q2H")
                                    
                            except asyncio.CancelledError:
                                # Если клиент написал еще одно сообщение во время 3-минутного ожидания
                                log_session(ctx, f"🛑 LONG RETRY прерван: клиент {max_user_id} прислал новое сообщение.")
                                raise

                    # [План 2026-08-14-001, U2] Симметрия флагов: фолбэк-ветки
                    # (ai_resp=None) шлют собственный финальный текст и минуют
                    # send_smartbot_reply — зависший pending_farewell снимаем без
                    # второго прощания; финализацию ведёт pending_archive ниже
                    if not ai_resp:
                        USER_CONTEXT.get(max_user_id, {}).pop('pending_farewell', None)

                    # --- NEW: Выгружаем полную стенограмму, если была команда на финализацию ---
                    pending_archive = USER_CONTEXT.get(max_user_id, {}).pop('pending_archive', None)
                    if pending_archive:
                        asyncio.create_task(finalize_transcript_archive(max_user_id, pending_archive))


            USER_MESSAGE_TASKS[max_user_id] = asyncio.create_task(delayed_sender())
            return

    # --- U7a: единый гейт голого старта (MAX bot_started и TG /start без payload) ---
    # Правило для бота-получателя (R4), решение ТОЛЬКО по ctx, без Dialogflow:
    # окно анти-дубля → молчание; контекст пуст или заявка закрыта
    # (deal_finalized/is_finalized — прецедент _cabinet_close_gate) → фолбэк;
    # незакрытая заявка → дальше штатный конвейер (DF повторит карточку).
    # Активный ИИ обработан веткой выше (в n8n, как раньше). Сделки, алерты
    # и архив здесь не создаются.
    if input_type == "deeplink" and (user_text or "").strip().lower() == "start":
        if time.time() - _prev_interaction < BARE_START_DEDUP_WINDOW:
            log_session(ctx, "⏸ [U7a] Голый старт в анти-дубль окне — молчание")
            return
        if (not ctx.get('start_code') or ctx.get('deal_finalized')
                or ctx.get('is_finalized')):
            log_session(ctx, "👋 [U7a] Голый старт: контекст пуст или заявка закрыта — фолбэк")
            _cl, _raw = get_sender(user_id, ctx)
            if _cl is not None:
                await _cl.send_message(user_id=_raw, text=FALLBACK_NO_CODE_TEXT)
            await save_context_to_db(user_id, ctx)
            return
        log_session(ctx, "🔁 [U7a] Голый старт: незакрытая заявка — повтор карточки конвейером")

    # 2. Запуск воронки по коду (Deep Link)
    match = CODE_REGEX.search(user_text or "")
    if match:
        code_found = match.group(1)
        prefix = code_found[:2]
        
        dealer_data = next((d for d in CACHED_DEALERS.values() if d.get('code') == prefix), None)
        if not dealer_data:
            _cl, _raw = get_sender(user_id, ctx)
            if _cl is not None:
                await _cl.send_message(user_id=_raw, text="Ошибка: Дилер не найден или код не активен.")
            return
        if dealer_data:
            USER_CONTEXT[max_user_id]['dealer_id'] = dealer_data.get('dealer_id') or dealer_data.get('id')
            # U6 (KTD-7): сверка бренда — fire-and-forget, диалог не ждёт и не прерывается
            _spawn_bg(_brand_check(USER_CONTEXT[max_user_id], dealer_data, code_found))

        log_session(ctx, f"🚨 New Code Activated: {code_found}")
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(User).where(User.platform_user_id == max_user_id)
                .values(is_smartbot_active=False, context_json='{}', active_deal_id=None)
            )
            await session.commit()
        
        db_user.is_smartbot_active = False
        db_user.active_deal_id = None
        USER_CONTEXT[max_user_id] = {
            'start_code': code_found,
            'chat_history': [],
            'last_interaction_time': time.time(),
            'channel': channel,  # сохраняем канал при сбросе контекста
            'bot_key': bot_key,  # U3: бот сессии переживает пересоздание контекста
        }
        
        # --- ЛОГИРОВАНИЕ: Системное сообщение о старте ---
        model_tail = user_text.replace("start", "").replace(code_found, "").strip()
        USER_CONTEXT[max_user_id]['chat_history'].append({
            "time": datetime.now().strftime("%H:%M"), 
            "role": "system", 
            "text": f"Клиент перешел по ссылке: {code_found} {model_tail}"
        })
        
        ctx = USER_CONTEXT[max_user_id] # Обновляем ссылку на контекст
        
        model_id_extracted = code_found[2:4]  # ВЕРНО — 2 символа
        complectation_id_extracted = code_found[4:6]
        if model_id_extracted:
            USER_CONTEXT[max_user_id]['model_id'] = model_id_extracted
        if complectation_id_extracted:
            USER_CONTEXT[max_user_id]['complectation_id'] = complectation_id_extracted

        # Извлекаем название модели из диплинка (всё, что идет после кода)
        model_tail = user_text.replace(code_found, "").strip()
        if model_tail:
            USER_CONTEXT[max_user_id]['raw_model_text'] = model_tail

        await save_context_to_db(max_user_id, USER_CONTEXT[max_user_id])
        await reset_dialogflow_context(session_id)
        
        # Сразу пытаемся привязаться к сделке Битрикс
        asyncio.create_task(check_and_link_deal(db_user, code_found))

    # Защита от "залетных" пользователей без диплинка
    if not USER_CONTEXT[user_id].get('start_code'):
        fallback_text = FALLBACK_NO_CODE_TEXT  # U7a: единый текст с гейтом голого старта
        _cl, _raw = get_sender(user_id, ctx)
        if _cl is not None:
            await _cl.send_message(user_id=_raw, text=fallback_text)
        return

    # 3. Dialogflow
    ctx = USER_CONTEXT[max_user_id]
    clean_user_text = (user_text or "").strip().upper()
    
    if clean_user_text == "__SYSTEM_TIMEOUT_HANDOFF__":
        intent = "system_timeout"
        bot_text = "" # Ничего не отправляем от DF
    elif clean_user_text == "НИЧЕГО НЕ НУЖНО":
        # Перехватываем отказ до Dialogflow, чтобы обойти жесткий контекст телефона
        intent = "93_Refusal_Final"
        bot_text = "Диалог успешно завершен.\n\nЕсли у Вас появятся вопросы, Вы всегда можете запустить повторный расчет параметров на нашем сайте.\nВсего доброго!"
    else:
        if match:
            # Очищаем модель от технических слов и формируем идеальную фразу для Dialogflow, как это было в Telegram
            code_found = match.group(1)
            clean_model = user_text.replace(code_found, "").replace("start", "").strip()
            text_for_df = f"ЗДРАВСТВУЙТЕ! МЕНЯ ИНТЕРЕСУЕТ {clean_model}"
        else:
            text_for_df = user_text
        
        df_result = await get_dialogflow_response(session_id, text_for_df)
        if not df_result: return

        intent = df_result.intent.display_name
        bot_text = df_result.fulfillment_text

        # Подменяем текст запроса контакта в зависимости от сценария
        if intent == "10_Contact_Phone":
            scenario_type = ctx.get('scenario_type', '')
            if scenario_type == 'stock':
                bot_text = "Для получения информации, подтвердите Ваши контакты"
            elif scenario_type == 'consult':
                bot_text = "Для получения консультации, подтвердите Ваши контакты"
            else:
                bot_text = "Для получения расчета, подтвердите Ваши контакты"   
    
    scenario_map = {
        "02_Scenario1_Credit": "credit", "04_Scenario2_TradeIn": "tradein",
        "06_Scenario3_Both": "combo", "90_Stock_Request": "stock",
        "91_Consultation_Request": "consult"
    }
    if intent in scenario_map:
        ctx['scenario_type'] = scenario_map[intent]
        await save_context_to_db(max_user_id, ctx)
    
    if intent in ["03_Payment_Input", "07_Payment_Complex_Input"]: 
        ctx['payment'] = normalize_payment(user_text)
        await save_context_to_db(max_user_id, ctx)
    elif intent == "05_Plate_Input": 
        ctx['plate'] = user_text
        await save_context_to_db(max_user_id, ctx)
    elif intent == "08_Plate_Complex_Input": 
        ctx['plate'] = user_text
        if not ctx.get('payment'):
            clean_val = re.sub(r'\s+', '', user_text)
            if clean_val.isdigit() and len(clean_val) >= 4:
                ctx['payment'] = user_text
        await save_context_to_db(max_user_id, ctx)

    # 4. УМНЫЙ ПЕРЕХВАТ ТЕЛЕФОНА
    # Запоминаем, был ли номер в базе ДО текущего сообщения
    already_had_phone = is_valid_phone(db_user.phone)
    has_valid_phone = already_had_phone

    # Единый предикат (перенос 3.1.2): длина <=20, букв <5, 11 цифр с 7/8 либо
    # 10 цифр с 9. Старый _starts_ok пропускал 10 цифр с '7'/'8' и 11 цифр
    # с '9' — оба варианта невалидны для РФ и давали менеджеру недозвонимый номер.
    looks_like_phone = is_valid_phone(user_text)

    # Сценарий А: Клиент ввел или прислал НОВЫЙ номер прямо сейчас.
    # Возврат save_user_phone проверяется: запись могли отклонить и валидность
    # (последний рубеж), и кабинетный страж (R8/AE6) — тогда «Принято!» не отвечаем.
    if looks_like_phone and not already_had_phone:
        if await save_user_phone(max_user_id, user_text, user_name, user_username, channel=channel):
            db_user.phone = user_text
            has_valid_phone = True
            intent = "10_Contact_Phone"
            bot_text = "Принято! В ближайшее время мои коллеги свяжутся с вами по этому номеру."

    # Сценарий Б: Бот дошел до шага связи, а номер УЖЕ есть в базе
    elif (intent in ["10_Contact_Phone", "btn_phone_intent"]) and already_had_phone:
        intent = "10_Contact_Phone"
        bot_text = "В ближайшее время мои коллеги свяжутся с вами по номеру, который был указан ранее."

    # Сценарий В: DF вернул 12_Phone_Input, но номер уже был в базе
    elif intent == "12_Phone_Input" and already_had_phone:
        has_valid_phone = True

    # 5. Цены и Сайт
    if "[Price" in bot_text or "[Model]" in bot_text:
        bot_text = await inject_prices_and_capture_model(max_user_id, user_text, bot_text, USER_CONTEXT)
    bot_text = inject_website(bot_text, ctx.get('start_code'))

    # 6. Финализация и Маршрутизация
    is_final_step = False
    clean_text = (user_text or "").strip().upper()
    
    if clean_text == "__SYSTEM_TIMEOUT_HANDOFF__":
        is_final_step = True
    elif intent == "09_Refusal_NoNeed" and "НЕ НАДО ЗВОНИТЬ/ПИСАТЬ" in clean_text:
        is_final_step = True
        ctx['scenario_type'] = "refusal"
    elif intent == "93_Refusal_Final" or clean_text in ["НИЧЕГО НЕ НУЖНО"]:
        is_final_step = True
        ctx['scenario_type'] = "refusal"
    elif clean_text in ["ДРУГАЯ МОДЕЛЬ"]:
        is_final_step = True
        ctx['scenario_type'] = "other_model"
    elif intent in ["10_Contact_Phone", "12_Phone_Input"] and has_valid_phone:
        is_final_step = True
        ctx['channel_val'] = "По телефону"
    elif intent == "11_Contact_Telegram":
        is_final_step = True

    # 7. Отправка ответа клиенту в MAX (с фиксом для Call и Smartbot дилеров)
    if bot_text:
        dealer_prefix = ctx.get('start_code')[:2] if ctx.get('start_code') else None
        
        comm_type = "Chat"
        if dealer_prefix and dealer_prefix in CACHED_DEALERS:
            _channel = ctx.get('channel', 'max')
            if _channel == 'web':
                comm_type = CACHED_DEALERS[dealer_prefix].get("comm_type_td", "Chat")
            elif _channel == 'telegram':
                comm_type = CACHED_DEALERS[dealer_prefix].get("comm_type_tg", "Chat")
            else:
                comm_type = CACHED_DEALERS[dealer_prefix].get("comm_type", "Chat")

        # Универсальная обрезка и авто-переход к запросу номера (включая новый режим Manager)
        if comm_type in ["Call", "Smartbot", "Manager"] and intent in ["03_Payment_Input", "05_Plate_Input", "08_Plate_Complex_Input", "90_Stock_Request", "91_Consultation_Request"]:
            cut_phrases = [
                "Какой способ связи",
                "Скажите, как Вам будет удобнее",
                "Скажите, какой способ связи"
            ]
            
            clean_bot_text = bot_text
            for phrase in cut_phrases:
                if phrase in clean_bot_text:
                    clean_bot_text = clean_bot_text.split(phrase)[0].strip()
            
            # Жестко форсируем текст для режима Manager под все типы интентов
            if comm_type == "Manager":
                bot_text = "Принято! Ваш запрос отправлен специалисту 👨‍💼"
                is_final_step = True
            else:
                # Стандартное поведение для Call и Smartbot сценариев
                _cl, _raw = get_sender(user_id, ctx)
                if _cl is not None:
                    await _cl.send_message(user_id=_raw, text=clean_bot_text)
                ctx['chat_history'].append({"time": datetime.now().strftime("%H:%M"), "role": "bot", "text": clean_bot_text})
                await save_context_to_db(user_id, ctx)
                log_session(ctx, f"🔄 [MAX v2.0] Triggering 'ПО ТЕЛЕФОНУ' for {comm_type}-dealer (Intent: {intent})")
                return await process_input(user_id, "ПО ТЕЛЕФОНУ", user_name, user_username, input_type="button", channel=channel, bot_key=bot_key)

        # Стандартная отправка
        kb = get_keyboard_by_intent(intent, dealer_prefix)
        if comm_type == "Manager" and is_final_step:
            kb = None

        # Убираем кнопку контакта, если телефон уже получен
        if has_valid_phone and intent in ["10_Contact_Phone", "12_Phone_Input"]:
            kb = None

        # Если канал не поддерживает request_contact — убираем кнопку
        _channel_for_send = ctx.get('channel', 'max')
        _send_client, _send_raw = get_sender(user_id, ctx, channel=_channel_for_send)
        if _send_client is None:
            log_session(ctx, f"🛑 [U4] Бот сессии недоступен — сообщение пользователю {max_user_id} не отправлено (R14)", "ERROR")
            return
        if kb and not _send_client.supports_contact_button():
            if any(btn.get("type") == "request_contact" for row in kb for btn in row):
                kb = None

        # Подмена текста "Другая модель" для веб-канала
        if ctx.get('scenario_type') == 'other_model' and ctx.get('channel') == 'web':
            bot_text = (
                "Без проблем 👍\n\n"
                "Для выбора другого автомобиля Вам необходимо:\n"
                "- Закрыть текущий чат\n"
                "- Выбрать интересующий авто\n"
                "- На Шаге 3 нажать кнопку \"Перейти\""
            )

        res = await _send_client.send_message(user_id=_send_raw, text=bot_text, keyboard=kb)
        if res:
            # --- ЛОГИРОВАНИЕ: Сохраняем ответ бота (Полный) ---
            ctx['chat_history'].append({"time": datetime.now().strftime("%H:%M"), "role": "bot", "text": bot_text})
            await save_context_to_db(max_user_id, ctx)
            
            log_session(ctx, f"🚀 Бот успешно ответил пользователю {max_user_id}")

            # Для web-канала: показываем typing через 2 сек после финального сообщения
            if is_final_step and ctx.get('channel') == 'web':
                asyncio.create_task(_web_delayed_typing(max_user_id, delay=2.0))

            # === ТАЙМЕР ПЕРЕХВАТА ДЛЯ SMARTBOT (30 сек) ===
            if comm_type == "Smartbot" and intent in ["10_Contact_Phone", "12_Phone_Input"] and not has_valid_phone:
                if max_user_id in SMARTBOT_HANDOFF_TASKS:
                    SMARTBOT_HANDOFF_TASKS[max_user_id].cancel()
                    
                async def smartbot_timeout_handoff():
                    try:
                        await asyncio.sleep(30) # Таймер снижен до 30 секунд
                        
                        # Проверяем, не появился ли номер за это время
                        async with AsyncSessionLocal() as session:
                            res_db = await session.execute(select(User).where(User.platform_user_id == max_user_id))
                            usr = res_db.scalar_one_or_none()
                            if is_valid_phone(usr.phone if usr else None):
                                return # Телефон получен, таймер не нужен
                                
                        log_session(ctx, f"⏳ [TIMEOUT] 30s expired for {max_user_id}. Showing continuation buttons.")
                        
                        scenario_type = ctx.get('scenario_type', '')
                        if scenario_type == 'stock':
                            sys_msg = "Вы также можете получить информацию онлайн"
                        elif scenario_type == 'consult':
                            sys_msg = "Вы также можете получить консультацию онлайн"
                        else:
                            sys_msg = "Вы также можете получить расчет онлайн"
                        kb = [
                            [{"text": "ПРОДОЛЖИТЬ", "callback_data": "btn_continue_ai"}],
                            [{"text": "ОТМЕНА", "callback_data": "btn_stop_ai"}]
                        ]
                        
                        _cl, _raw = get_sender(user_id, ctx)
                        if _cl is not None:
                            await _cl.send_message(user_id=_raw, text=sys_msg, keyboard=kb)

                        ctx_current = await load_context_from_db(user_id)
                        ctx_current['chat_history'].append({"time": datetime.now().strftime("%H:%M"), "role": "bot", "text": sys_msg})
                        await save_context_to_db(max_user_id, ctx_current)
                        
                        # Мы больше не запускаем ИИ принудительно, а ждем нажатия кнопки клиентом
                        
                    except asyncio.CancelledError:
                        log_session(ctx, f"🛑 Smartbot handoff timer cancelled for {max_user_id}.")

                SMARTBOT_HANDOFF_TASKS[max_user_id] = asyncio.create_task(smartbot_timeout_handoff())
            # ==========================================

        else:
            log_session(ctx, f"⚠️ Бот НЕ смог отправить ответ пользователю {max_user_id}. Проверь логи max_client.", "ERROR")

    # 8. CRM и AI Handoff
    if is_final_step:
        log_session(ctx, f"🌪 Starting TRANSACTIONAL CRM sync for {max_user_id}")
        ctx_snapshot = copy.deepcopy(ctx)
        
        should_skip_tg = ctx_snapshot.get('scenario_type') in ["refusal", "other_model"]
        prefix = ctx_snapshot.get('start_code', '')[:2]
        dealer_config = next((d for d in CACHED_DEALERS.values() if d.get('code') == prefix), {})
        _channel = ctx.get('channel', 'max')
        if _channel == 'web':
            comm_type = dealer_config.get("comm_type_td", "Chat")
        elif _channel == 'telegram':
            comm_type = dealer_config.get("comm_type_tg", "Chat")
        else:
            comm_type = dealer_config.get("comm_type", "Chat")

        # =================================================================
        # ВЕТКА A: Smartbot (AI AGENT ACTIVATION)
        # =================================================================
        # Активируем ИИ для Smartbot (при условии отсутствия телефона) ИЛИ мгновенно для режима Manager
        if not should_skip_tg and (comm_type == "Manager" or (comm_type == "Smartbot" and not has_valid_phone)):
            log_session(ctx, f"🤖 Activating SmartBot for {max_user_id}")

            # --- [КРИТИЧЕСКИЙ ФИКС] Гарантируем наличие Deal ID перед передачей в AI ---
            if not deal_ref(db_user.active_deal_id):
                log_session(ctx, f"⚠️ Deal not linked for {max_user_id}. Running sync before handoff...", "WARNING")
                # Создаем сделку в фоне (skip_tg=True, чтобы не слать алерт раньше времени)
                new_deal_id_a = await BitrixService.final_sync_wrapper(max_user_id, ctx_snapshot, skip_tg=True, skip_stage=True)

                # СРАЗУ обновляем локальный объект пользователя, чтобы увидеть ID новой сделки
                async with AsyncSessionLocal() as session:
                    res = await session.execute(select(User).where(User.platform_user_id == max_user_id))
                    db_user = res.scalar_one_or_none()

                # Запускаем охоту ТОЛЬКО если бот только что создал новую сделку
                if new_deal_id_a and ctx_snapshot.get('start_code'):
                    asyncio.create_task(background_deal_hunt(
                        str(new_deal_id_a),
                        ctx_snapshot.get('start_code'),
                        ctx_snapshot,
                        max_user_id,
                        db_user.full_name,
                        db_user.phone or "",
                        db_user.username or ""
                    ))

            # Включаем флаг активности AI в базе данных
            async with AsyncSessionLocal() as session:
                await session.execute(update(User).where(User.platform_user_id == max_user_id).values(is_smartbot_active=True))
                await session.commit()

            # [План 2026-08-21-001, U1] Граница передачи: с этого места история
            # для n8n начинается с чистого листа (срез в build_recent_history)
            await mark_ai_handoff(max_user_id, ctx_snapshot)

            # Если сделка теперь есть (найдена или создана ботом выше)
            if deal_ref(db_user.active_deal_id):
                deal_id_str = deal_ref(db_user.active_deal_id)
                # Переводим в техническую стадию "AI Обработка"
                await update_deal_stage_direct(deal_id_str, "C9:UC_CKY0IR")
                
                # Принудительно пробрасываем ID в контекст, чтобы n8n его увидел
                ctx_snapshot['deal_id'] = deal_id_str
                USER_CONTEXT[max_user_id]['deal_id'] = deal_id_str

            # Включаем индикатор "печатает" ПРЯМО ПЕРЕД запросом в n8n
            try:
                chat_id = ctx.get("chat_id")
                try:
                    target_id = int(chat_id) if chat_id else int(user_id)
                except (ValueError, TypeError):
                    target_id = None
                logger.info(f"✍️ [UI] Включаем тайпинг ПЕРЕД N8N для ID {target_id}")
                if target_id is not None:
                    asyncio.create_task(_send_typing_for(ctx, target_id))
            except Exception as e:
                logger.error(f"❌ Ошибка старта тайпинга: {e}")

            # Передаем управление в n8n (Handoff)
            ai_response = await send_handoff_webhook(max_user_id, ctx_snapshot, db_user)
            
            # --- ФИКС: Сохраняем самое первое сообщение ИИ в историю ---
            if ai_response:
                # [U1] нормализация: [CABINET_LINK] → URL (R6)
                bot_text = await normalize_bot_text_for_history(
                    max_user_id,
                    (ai_response.get('text') or ai_response.get('output') or "").replace('[SPLIT]', '\n\n'))
                USER_CONTEXT[max_user_id]['chat_history'].append({
                    "time": datetime.now().strftime("%H:%M"), "role": "bot", "text": bot_text
                })
                await save_context_to_db(max_user_id, USER_CONTEXT[max_user_id])
            
            # Отправляем ответ AI пользователю (сплиттер + задержки)
            await send_smartbot_reply(max_user_id, ai_response)
            
            # --- NEW: Проверяем финализацию сразу после старта ---
            pending_archive = USER_CONTEXT.get(max_user_id, {}).pop('pending_archive', None)
            if pending_archive:
                asyncio.create_task(finalize_transcript_archive(max_user_id, pending_archive))
            
            log_session(ctx, f"🤖 SmartBot Activation Complete for {max_user_id}")
            return # ПРЕРЫВАЕМ стандартную воронку

        # =================================================================
        # ВЕТКА B: Стандартная обработка (Call/Алерт + Охота)
        # =================================================================
        try:
            # Сначала синхронизируем данные (создаем/обновляем сделку)
            new_deal_id = await BitrixService.final_sync_wrapper(max_user_id, ctx_snapshot, skip_tg=should_skip_tg)
            
            # Запускаем фоновую охоту ТОЛЬКО если бот создал НОВУЮ сделку (оригинал с сайта еще не прилетел)
            if new_deal_id and ctx_snapshot.get('start_code'):
                asyncio.create_task(background_deal_hunt(
                    str(new_deal_id),
                    ctx_snapshot.get('start_code'),
                    ctx_snapshot,
                    max_user_id,
                    db_user.full_name,
                    db_user.phone,
                    db_user.username
                ))

            # [План 2026-08-21-002, U4] Кабинет следует за новой заявкой и на
            # ветке B: у вернувшегося клиента с телефоном INIT/prepare не
            # выполняются — синхронизация сделки кабинета здесь (гарды и
            # флейвор «новая заявка» — cabinet_service.sync_cabinet_deal)
            try:
                _b_deal = new_deal_id
                if not _b_deal:
                    async with AsyncSessionLocal() as _bs:
                        _bu = (await _bs.execute(select(User).where(
                            User.platform_user_id == max_user_id))).scalar_one_or_none()
                    _b_deal = _bu.active_deal_id if _bu else None
                if _b_deal:
                    _b_prefix = (ctx_snapshot.get('start_code', '') or '')[:2]
                    await cabinet_service.sync_cabinet_deal(
                        max_user_id, _b_prefix, str(_b_deal),
                        payment=ctx_snapshot.get('payment'))
            except Exception as _cab_err:
                logger.error(f"❌ [Cabinet U4] sync_cabinet_deal на ветке B: {_cab_err}")
        except Exception as e:
            log_session(ctx, f"❌ Critical Sync Error: {e}", "ERROR")

        # --- АРХИВАЦИЯ DIALOGFLOW-ДИАЛОГА В NOCODB ---
        try:
            start_code = ctx_snapshot.get('start_code', '')
            dealer_code = start_code[:2] if len(start_code) >= 2 else "00"
            dealer_info = CACHED_DEALERS.get(dealer_code, {})
            resolved_deal_id = ctx_snapshot.get('deal_id') or str(new_deal_id or '')
            scenario_type = ctx_snapshot.get('scenario_type', '')
            channel_val = ctx_snapshot.get('channel_val')
            if channel_val:
                final_status = channel_val
            elif scenario_type == "refusal":
                final_status = "refuse"
            elif scenario_type == "other_model":
                final_status = "other_model"
            else:
                final_status = "DF_Finalized"

            archive_payload = {
                "user_id": str(max_user_id),
                "session_id": f"sess_{max_user_id}_{start_code}",
                "Bot_Key": _archive_bot_key(ctx_snapshot, user_id=max_user_id),
                "model": ctx_snapshot.get('model', 'Не указана'),
                "Deal_ID": resolved_deal_id,
                "Dealer_Code": dealer_code,
                "Prompt_Version": f"{dealer_info.get('Prompt_Teg', 'base')}_{dealer_info.get('Prompt_Ver', 'v01.00')}",
                "Scenario": ctx_snapshot.get('scenario_type', 'unknown'),
                "Final_Status": final_status,
                "AI_Report": "Диалог завершён через Dialogflow (без ИИ-агента)",
                "Transcript": format_chat_history(ctx_snapshot.get('chat_history', []))
            }
            asyncio.create_task(upsert_dialogue_archive(archive_payload))

            ctx['is_finalized'] = True

            # Для web-канала — сигнал закрытия чата
            if ctx.get('channel') == 'web':
                await web_client.send_close(max_user_id)

            await save_context_to_db(max_user_id, ctx)

            # Очищаем контекст из памяти после финальной архивации
            USER_CONTEXT.pop(max_user_id, None)
            logger.info(f"🧹 [Memory] USER_CONTEXT очищен для {max_user_id}")

        except Exception as e:
            log_session(ctx, f"❌ [Archive] Ошибка архивации Dialogflow-диалога: {e}", "ERROR")

# --- GLOBAL MIDDLEWARE ---

# U4: префикс API кабинета — CORS для него echo-whitelist, не '*'
CABINET_API_PREFIX = '/cabinet/api/'


def _cabinet_cors(request: web.Request) -> dict:
    """U4: CORS-заголовки кабинета (echo Origin из whitelist дилеров, Vary: Origin);
    чужой origin — пустой dict. Логика — в cabinet_service (wiring-only здесь)."""
    return cabinet_service.cabinet_cors_headers(request.headers.get("Origin"))


@web.middleware
async def error_middleware(request, handler):
    """Глобальный перехватчик любых необработанных исключений сервера aiohttp."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise  # Стандартные HTTP-ответы (404, 403, 200 от static) пропускаем как есть
    except Exception as e:
        import traceback
        logger.error(f"🚨 [GLOBAL FATAL] Непредвиденная ошибка в {request.path}: {e}", exc_info=True)
        
        # Отправляем алерт ИСКЛЮЧИТЕЛЬНО в техническую группу
        if TECH_ADMIN_ID:
            try:
                tb_string = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                short_tb = tb_string[-500:] if len(tb_string) > 500 else tb_string
                alert_text = (
                    f"🚨 <b>КРИТИЧЕСКИЙ СБОЙ БОТА MAX (Global)</b> 🚨\n\n"
                    f"<b>Путь:</b> {request.path}\n"
                    f"<b>Ошибка:</b> {type(e).__name__}\n"
                    f"<b>Текст:</b> {str(e)}\n\n"
                    f"<code>{short_tb}</code>"
                )
                # U9 (AE9, R16): алерт идёт мимо logging — маскируем текст явно
                alert_text = log_masking.mask_sensitive(alert_text)
                await service_alert_client().send_alert_to_group(group_id=TECH_ADMIN_ID, text=alert_text)
            except Exception as alert_err:
                logger.error(f"Не удалось отправить алерт об ошибке в MAX: {alert_err}")

        # Отвечаем серверу MAX статусом 500, чтобы он попробовал прислать хук позже
        response = web.json_response({"success": False, "error": "Internal Server Error"}, status=500)
        if request.path.startswith(CABINET_API_PREFIX):
            # U4: 500-путь для /cabinet/api/* тоже несёт echo-CORS (не '*'),
            # иначе браузер скроет тело ошибки от фронта кабинета
            for key, value in _cabinet_cors(request).items():
                response.headers[key] = value
        return response

COMPRESS_MIN_BYTES = 1024
_COMPRESSIBLE_TYPES = ("application/json", "text/html", "text/plain", "text/css",
                       "application/javascript", "text/javascript")


@web.middleware
async def compression_middleware(request: web.Request, handler):
    """Ускорение A1 (2026-10-03): сжатие gzip/deflate (br — если в окружении есть
    brotli) текстовых ответов бота больше 1 КБ, если клиент его принимает.
    Только web.Response с готовым телом: FileResponse (/widget-custom) отдаёт
    заранее сжатые .br/.gz сборки сам, потоковые ответы (SSE) не трогаются."""
    response = await handler(request)
    try:
        if (type(response) is web.Response
                and "gzip" in request.headers.get("Accept-Encoding", "").lower()
                and not response.headers.get("Content-Encoding")
                and (response.content_type or "") in _COMPRESSIBLE_TYPES
                and response.body is not None
                and len(response.body) >= COMPRESS_MIN_BYTES):
            response.enable_compression()
    except Exception:
        logger.exception("compression_middleware: сжатие пропущено")
    return response


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Добавляет CORS-заголовки ко всем ответам (нужно для web-виджета).
    U4: для /cabinet/api/* — echo Origin из whitelist дилеров вместо '*';
    остальные пути сохраняют прежнее поведение '*'."""
    if request.method == "OPTIONS":
        # U4: ветка кабинета ДО wildcard-возврата — чужой origin остаётся без CORS
        if request.path.startswith(CABINET_API_PREFIX):
            return web.Response(status=200, headers=_cabinet_cors(request))
        return web.Response(
            status=200,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization, ngrok-skip-browser-warning",
                "Access-Control-Max-Age": "86400",
            }
        )
    try:
        response = await handler(request)
    except web.HTTPException as ex:
        response = ex
    if request.path.startswith(CABINET_API_PREFIX):
        # U4: и основной путь, и HTTPException (429/410/422 и т.д.) — echo, не '*'
        for key, value in _cabinet_cors(request).items():
            response.headers[key] = value
        return response
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, ngrok-skip-browser-warning"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

async def handle_catalog(request: web.Request):
    """Проксирует таблицу NocoDB для Tilda. Токен не покидает сервер.

    Гейт: только allowlist-таблицы (сайт+дилеры); карточки дилеров режутся до
    публичных полей. Формат ответа и отсутствие требований к заголовкам —
    неизменны (страница Кабинета делает голый fetch)."""
    table_id = request.match_info.get('dealer_code', '').strip()
    if not table_id:
        return web.json_response({'error': 'no table_id'}, status=400)
    if table_id not in catalog_allowed_tables():
        return web.json_response({'error': 'not found'}, status=404)
    cars = await get_cars_from_nocodb_dynamic(table_id)
    if table_id == NOCODB_TABLE_ID_DEALERS:
        cars = [_dealer_public_row(r) for r in cars]
    body = json.dumps({'list': cars}, ensure_ascii=False).encode('utf-8')
    if 'gzip' in request.headers.get('Accept-Encoding', ''):
        body = gzip.compress(body, compresslevel=6)
        return web.Response(
            body=body,
            content_type='application/json',
            headers={
                'Content-Encoding': 'gzip',
                'Access-Control-Allow-Origin': '*',
            }
        )
    return web.json_response({'list': cars})


async def handle_catalog_colors(request: web.Request):
    """Проксирует таблицу цветов из NocoDB для Tilda."""
    table_id = request.query.get('table_id', '').strip()
    if not table_id:
        return web.json_response({'error': 'no table_id'}, status=400)

    # Единый allowlist с /catalog (гейт 2026-09-23); 404 без подробностей
    if table_id not in catalog_allowed_tables():
        return web.json_response({'error': 'not found'}, status=404)

    cars = await get_cars_from_nocodb_dynamic(table_id)
    if table_id == NOCODB_TABLE_ID_DEALERS:
        cars = [_dealer_public_row(r) for r in cars]
    return web.json_response({'list': cars})


async def handle_bot_links(request: web.Request):
    """U9: публичный справочник ссылок ботов для лендингов (решение владельца
    2026-09-23). Отдаёт ТОЛЬКО brand/channel/link активных ботов с заполненной
    ссылкой; Token_Env, Environment, секретный блок наружу не выходят."""
    bots = []
    for entry in sorted(bot_registry.CACHED_BOTS.values(), key=lambda e: e.bot_key):
        if not entry.active or not entry.link:
            continue
        bots.append({"channel": entry.channel,
                     "brands": sorted(entry.brands),
                     "link": entry.link})
    return web.json_response({"bots": bots})


async def handle_cors_preflight(request: web.Request):
    """Обработчик OPTIONS preflight-запросов от браузера."""
    return web.Response(
        status=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, ngrok-skip-browser-warning",
            "Access-Control-Max-Age": "86400",
        }
    )

# =======================
# 5. MAX WEBHOOK HANDLERS & SERVER
# =======================

# --- U3 мультибота (план 2026-09-15-001): резолв бота по адресу вебхука ---

_WEBHOOK_SECRET_HEADERS = {"max": "X-Max-Bot-Api-Secret",
                           "telegram": "X-Telegram-Bot-Api-Secret-Token"}


def _webhook_bot_resolve(request, channel: str):
    """(bot_key, deny_response | None) — KTD-4, до парсинга тела запроса.

    Дроп «не нашего» события выполняется ДО кода, способного кинуть исключение
    (иначе error_middleware ответил бы 500 и платформа ретраила бы чужое).
    Неизвестный bot_key: 200-дроп при полном реестре (MAX не должен копить
    ретраи и отписывать вебхук), 503 при деградированном (события переживут
    восстановление NocoDB). Легаси-ключи проходят всегда (R7 — голые адреса
    работают и без реестра). Неверный/отсутствующий секрет при взведённом
    secret_enforced → 200-дроп с различимой причиной алерта. Событие бота с
    Active=✗ НЕ дропается (Д1) — блокируется только отправка (KTD-6)."""
    legacy_key = bot_registry.legacy_key_for_channel(channel)
    # resolve_alias: исторические max-main/tg-main (старые подписки, U9-переход)
    bot_key = bot_registry.resolve_alias(request.match_info.get("bot_key") or legacy_key)
    entry = bot_registry.get_bot_entry(bot_key)
    if entry is None:
        if bot_key == legacy_key:
            return bot_key, None  # R7: легаси живёт и с пустым реестром
        if bot_registry.is_degraded():
            return bot_key, web.json_response({"status": "registry degraded"}, status=503)
        logger.warning("TECH_ALERT [webhook] неизвестный bot_key %r (%s) — событие отброшено",
                       bot_key, channel)
        _spawn_bg(bot_registry.tech_alert(
            f"unknown-bot:{bot_key}",
            f"⚠️ Вебхук {channel}: событие на неизвестный bot_key {bot_key!r} — отброшено (KTD-4)"))
        return bot_key, web.json_response({"status": "unknown bot"}, status=200)
    if entry.secret_enforced:
        provided = request.headers.get(_WEBHOOK_SECRET_HEADERS[channel], "")
        if not provided or not hmac.compare_digest(provided, entry.webhook_secret()):
            reason = "отсутствует" if not provided else "неверный"
            logger.warning("TECH_ALERT [webhook] %s секрет для %s — событие отброшено",
                           reason, bot_key)
            _spawn_bg(bot_registry.tech_alert(
                f"bad-secret:{bot_key}",
                f"⚠️ Вебхук {channel}: {reason} секрет для известного бота {bot_key} — "
                f"почти наверняка рассинхронизация регистрации, проверьте register_webhooks (--check)"))
            return bot_key, web.json_response({"status": "bad secret"}, status=200)
    return bot_key, None


def _spawn_bg(coro):
    """Фоновая корутина с сильной ссылкой (анти-паттерн GC, см. _BG_TASKS)."""
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


async def handle_max_webhook(request):
    bot_key, deny = _webhook_bot_resolve(request, "max")
    if deny is not None:
        return deny
    try:
        # --- PROFILER: START ---
        req_start_time = time.time()
        # -----------------------

        obj = await request.json()
        print(f"[{req_start_time:.2f}] 📥 ПОЛУЧЕН ВЕБХУК: {obj.get('update_type')}")
        print(f"📥 ПОЛУЧЕН ВЕБХУК: {obj}")

        # --- ФИЛЬТР УСТАРЕВШИХ ВЕБХУКОВ ---
        webhook_ts = obj.get("timestamp", 0) / 1000  # MAX присылает в миллисекундах
        if webhook_ts and (time.time() - webhook_ts) > 60:  # старше 60 секунд
            logger.warning(f"⚠️ [WEBHOOK] Устаревший вебхук проигнорирован (опоздал на {int(time.time() - webhook_ts)} сек). Type: {obj.get('update_type')}")
            return web.json_response({"success": True})

        update_type = obj.get("update_type")
        
        max_user_id = ""
        text = ""
        user_name = "Клиент"
        user_username = ""
        
        # 1. Обработка обычного текстового сообщения
        if update_type == "message_created":
            msg_obj = obj.get("message", {})
            sender = msg_obj.get("sender", {})

            # --- ИГНОРИРУЕМ СООБЩЕНИЯ ОТ ДРУГИХ БОТОВ ---
            if sender.get("is_bot") is True:
                return web.Response(text="OK")
            
            max_user_id = str(sender.get("user_id", ""))
            if max_user_id:  # U3: внутренний ID (составной для новых ботов, KTD-5)
                max_user_id = bot_identity.build_internal_id(bot_key, max_user_id)
            user_name = sender.get("name", sender.get("first_name", "Клиент"))
            user_username = sender.get("username", "")

            body = msg_obj.get("body", {})
            text = body.get("text", "")

            # --- ПЕРЕХВАТ КОНТАКТА ИЗ КНОПКИ ---
            attachments = body.get("attachments", [])
            if not text and attachments:
                for att in attachments:
                    if att.get("type") == "contact":
                        vcf_info = att.get("payload", {}).get("vcf_info", "")
                        # Вытаскиваем номер из строки формата vCard (например, TEL;TYPE=cell:79095556391)
                        phone_match = re.search(r'TEL[^:]*:(\+?\d+)', vcf_info)
                        if phone_match:
                            text = phone_match.group(1)
                            print(f"📱 ПОЛУЧЕН КОНТАКТ ИЗ КНОПКИ: {text}")
                        break

        # 2. Обработка перехода по диплинку (bot_started)
        elif update_type == "bot_started":
            user_obj = obj.get("user", {})
            max_user_id = str(user_obj.get("user_id", ""))
            if max_user_id:
                max_user_id = bot_identity.build_internal_id(bot_key, max_user_id)
            user_name = user_obj.get("name", "Клиент")
            user_username = user_obj.get("username", "")
            
            raw_payload = obj.get("payload", "")
            text = "start " + raw_payload.replace("_", " ")

        # 3. Обработка клика по кнопке (message_callback)
        elif update_type == "message_callback":
            callback_obj = obj.get("callback", {})
            message_obj = obj.get("message", {}) # Достаем объект самого сообщения
            
            user_obj = callback_obj.get("user", {})
            max_user_id = str(user_obj.get("user_id", ""))
            if max_user_id:
                max_user_id = bot_identity.build_internal_id(bot_key, max_user_id)
            user_name = user_obj.get("name", user_obj.get("first_name", "Клиент"))
            user_username = user_obj.get("username", "")
            
            # Достаем payload кнопки
            callback_data = callback_obj.get("payload", "")
            
            # Достаем ID и текст сообщения, чтобы стереть на нем кнопки
            body_obj = message_obj.get("body", {})
            message_id = body_obj.get("mid", "")
            original_text = body_obj.get("text", "")
            
            # --- ЗАЩИТА ОТ МУЛЬТИ-КЛИКОВ ---
            if max_user_id not in USER_CONTEXT:
                USER_CONTEXT[max_user_id] = await load_context_from_db(max_user_id)
                
            processed_mids = USER_CONTEXT[max_user_id].setdefault('processed_mids', [])
            if message_id and message_id in processed_mids:
                print(f"🛑 ДУБЛЬ КЛИКА: Сообщение {message_id} уже обработано. Игнорируем.")
                return web.json_response({"success": True})
                
            if message_id:
                processed_mids.append(message_id)
                # Храним только последние 10 ID, чтобы не забивать память
                USER_CONTEXT[max_user_id]['processed_mids'] = processed_mids[-10:]
                await save_context_to_db(max_user_id, USER_CONTEXT[max_user_id])
            # -------------------------------
            
            if message_id and original_text:
                # Запускаем скрытие кнопок в фоне, чтобы не тормозить ответ бота
                asyncio.create_task((bot_registry.get_bot_client(bot_key) or max_client)
                                    .remove_inline_keyboard(message_id, original_text))
            
            # Переводим payload в русский текст
            text = CALLBACK_MAPPING.get(callback_data, "")
            log_session(max_user_id, f"🔘 НАЖАТА КНОПКА: '{callback_data}' -> Симулируем текст: '{text}'")

        # 4. Единый вызов ядра логики бота
        if max_user_id and text: 
            # Извлекаем и сохраняем chat_id для статуса "печатает"
            chat_id = obj.get("chat_id") or obj.get("message", {}).get("recipient", {}).get("chat_id")
            if chat_id:
                if max_user_id not in USER_CONTEXT:
                    USER_CONTEXT[max_user_id] = await load_context_from_db(max_user_id)
                USER_CONTEXT[max_user_id]['chat_id'] = chat_id

            log_session(max_user_id, f"😎 СОБЫТИЕ: {update_type} | Ввод: {text}")

            print(f"🛠 Отправляем задачу process_input в фон для ID: {max_user_id}")
            try:
                if update_type == "message_callback":
                    input_type = "button"
                elif update_type == "bot_started":
                    input_type = "deeplink"
                else:
                    input_type = "text"

                asyncio.create_task(process_input(max_user_id, text, user_name, user_username, input_type=input_type, channel="max", bot_key=bot_key))
                print("✅ Задача process_input отправлена в фон (Ушли от таймаута MAX)")
            except Exception as e:
                print(f"❌ ОШИБКА ЗАПУСКА process_input: {e}")
            
        return web.json_response({"success": True})

    except Exception as e:
        # Пробрасываем ошибку наверх, чтобы её поймал наш error_middleware и отправил алерт
        raise


async def _worker_resolve_sender(session, u, ctx, now_ts):
    """U5 (R14): резолв бота сессии в воркерах с ПРОВЕРЯЕМЫМ маркером.

    Маркер bot_send_blocked не терминален: резолв выполняется каждый цикл,
    и когда бот снова активен (Active=✓ в реестре), маркер снимается и
    рассылка возобновляется. Маркер нужен для наблюдаемости и однократной
    записи контекста; дедуп-техалерт уходит из get_sender."""
    _cl, _raw = get_sender(u.platform_user_id, ctx)
    if _cl is None:
        if not ctx.get('bot_send_blocked'):
            ctx['bot_send_blocked'] = {"bot_key": ctx.get('bot_key'), "since": now_ts}
            await session.execute(update(User).where(User.id == u.id)
                .values(context_json=json.dumps(ctx, ensure_ascii=False)))
            if u.platform_user_id in USER_CONTEXT:
                USER_CONTEXT[u.platform_user_id] = ctx
        return None, _raw
    if ctx.pop('bot_send_blocked', None) is not None:
        log_session(u.platform_user_id,
                    "✅ [U5] Бот сессии снова активен — рассылка возобновлена")
        await session.execute(update(User).where(User.id == u.id)
            .values(context_json=json.dumps(ctx, ensure_ascii=False)))
        if u.platform_user_id in USER_CONTEXT:
            USER_CONTEXT[u.platform_user_id] = ctx
    return _cl, _raw


# Первое напоминание (план 2026-10-12-002, KTD-C3/C4): не больше
# REMINDER_1_MAX_FAILURES неудачных тиков; текст n8n генерируется один раз и
# переиспользуется при повторе; текст со служебной разметкой не отправляется.
REMINDER_1_MAX_FAILURES = 3
REMINDER_TEXT_MAX_LEN = 600


def reminder_markup_reason(text: str) -> Optional[str]:
    """Причина не отправлять текст напоминания (служебная разметка модели n8n,
    12.10: «**Если клиенту выдана ссылка…**», «---»), иначе None."""
    if "**" in text:
        return "**"
    lines = text.splitlines()
    if any(re.fullmatch(r"\s*-{3,}\s*", line) for line in lines):
        return "---"
    if any(re.match(r"\s*#{1,6}\s", line) for line in lines):
        return "#-заголовок"
    if len(text) > REMINDER_TEXT_MAX_LEN:
        return f"длина {len(text)} > {REMINDER_TEXT_MAX_LEN}"
    return None


async def _store_reminder_1_state(platform_user_id: str, tick_client_time, updates: dict,
                                  remove: tuple = ()) -> bool:
    """Неудачный тик первого напоминания (v3.4.4, P2.1 ревью v3.4.3): точечно
    пишет только свои ключи поверх свежего контекста, своей короткой
    транзакцией — строка не держится до конца прохода, ключи, записанные за
    время тика (реплика клиента, кэш n8n, пометка MAX), не теряются. Клиент
    написал за время тика — ничего не пишет: его сообщение уже сбросило
    состояние напоминания (сверяется и БД, и память: до дебаунс-сохранения
    сообщение клиента есть только в USER_CONTEXT). True — записано."""
    mem = USER_CONTEXT.get(platform_user_id)
    if mem is not None and mem.get('client_last_message_time') != tick_client_time:
        return False
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(User).where(User.platform_user_id == platform_user_id)
                               .with_for_update())).scalar_one_or_none()
        if row is None:
            return False
        live = json.loads(row.context_json or "{}")
        if live.get('client_last_message_time') != tick_client_time:
            return False
        live.update(updates)
        for key in remove:
            live.pop(key, None)
        row.context_json = json.dumps(live, ensure_ascii=False)
        await s.commit()
    if mem is not None:
        mem.update(updates)
        for key in remove:
            mem.pop(key, None)
    return True


async def reminder_worker():
    """Фоновый процесс рассылки ремайндеров 'уснувшим' клиентам."""
    logger.info("⏱ Reminder Worker started.")
    
    while True:
        try:
            await asyncio.sleep(60) # Проверяем каждую минуту
            now_ts = time.time()
            
            async with AsyncSessionLocal() as session:
                # Ищем ТОЛЬКО тех, у кого ИИ сейчас активен (сделка не финализирована)
                res = await session.execute(select(User).where(User.is_smartbot_active == True))
                active_users = res.scalars().all()
                
                for u in active_users:
                    if not u.context_json: continue
                    ctx = json.loads(u.context_json)
                    # Диалог клиента в MAX закрыт (403/404) — напоминания не идут
                    # и n8n не вызывается до его нового сообщения (KTD-C2)
                    if ctx.get(MAX_SEND_BLOCKED_KEY):
                        continue

                    # Пропускаем каналы без поддержки ремайндеров (например, Tiledesk).
                    # U4/U5 (KTD-6, R14): клиент — по боту сессии, резолв КАЖДЫЙ
                    # цикл через _worker_resolve_sender (проверяемый маркер).
                    _r_cl, _r_raw = await _worker_resolve_sender(session, u, ctx, now_ts)
                    if _r_cl is None or not _r_cl.supports_reminders():
                        continue

                    # [План 2026-08-14-001, U5] Кабинетное окно: номер подтверждён,
                    # передача ещё не произошла — клиент заполняет анкету, «актуален
                    # ли вопрос?» здесь шум. Сбой предиката не роняет воркер.
                    try:
                        if await cabinet_service.reminders_suppressed(u.id):
                            continue
                    except Exception:
                        logger.exception("reminder_worker: предикат кабинетного окна упал — ремайндер идёт как раньше")

                    start_code = ctx.get('start_code', '')
                    dealer_code = start_code[:2] if len(start_code) >= 2 else "00"
                    dealer_info = CACHED_DEALERS.get(dealer_code, {})
                    
                    # --- ТИХИЕ ЧАСЫ (с 21:00 до 09:00 по местному времени дилера) ---
                    utc_offset = dealer_info.get("UTC_Offset", 3)
                    local_time = datetime.utcnow() + timedelta(hours=utc_offset)
                    if 21 <= local_time.hour or local_time.hour < 9:
                        continue # Спит. Не трогаем до утра.
                    
                    # Считаем время от последнего сообщения КЛИЕНТА
                    last_client_time = ctx.get('client_last_message_time', ctx.get('last_interaction_time', now_ts))
                    rem_level = ctx.get('reminder_level', 0)
                    time_passed = now_ts - last_client_time
                    
                    # --- ПРЕДОХРАНИТЕЛЬ ОТ СТАРЫХ БАЗ ---
                    # Если клиент молчит больше 48 часов (172800 сек), игнорируем его.
                    # Его тихо закроет sync_worker.
                    if time_passed > 172800:
                        continue
                    # ------------------------------------
                    
                    # 1й РЕМАЙНДЕР (AI Рерайт): Прошло > 30 минут (1800 сек)
                    if time_passed > 1800 and rem_level == 0:
                        # Ищем последнюю фразу бота для AI-рерайта
                        last_bot_msg = ""
                        for msg in reversed(ctx.get('chat_history', [])):
                            if msg.get('role') == 'bot':
                                last_bot_msg = msg.get('text', '')
                                break
                        
                        ctx['last_bot_message'] = last_bot_msg

                        # Повтор после неудачи — текст из кэша, без нового вызова n8n (KTD-C3)
                        _pending = ctx.get('reminder_1_pending') or {}
                        _failures = int(ctx.get('reminder_1_failures') or 0)
                        if _pending.get('text'):
                            ai_resp = {"text": _pending['text']}
                            log_session(u.platform_user_id,
                                        f"♻️ [Reminder 1 - AI] Текст из кэша (попытка {_failures + 1}/{REMINDER_1_MAX_FAILURES})")
                        else:
                            ai_resp = await send_chat_message_to_n8n(
                                u.platform_user_id, "__SYSTEM_REMINDER__", u.full_name or "Клиент", ctx
                            )
                        _alive_r = True

                        # [План 2026-08-14-001, U3] Ремайндер-ход идёт без turn_lock
                        # и невидим развилке «ход в полёте» — перепроверяем свежим
                        # чтением: сессия завершилась, пока ответ летел → НЕ
                        # доставляем и НЕ перезаписываем context_json (иначе
                        # устаревшая копия затёрла бы is_finalized, сломав гейт R4)
                        if ai_resp:
                            async with AsyncSessionLocal() as _rs:
                                _alive = (await _rs.execute(
                                    select(User.is_smartbot_active).where(User.id == u.id)
                                )).scalar_one_or_none()
                            if not _alive:
                                log_session(u.platform_user_id,
                                            "⛔ [Reminder] Ответ ремайндера не доставлен: сессия завершена во время полёта")
                                ai_resp = None
                                _alive_r = False

                        _raw_r = ((ai_resp or {}).get('text') or (ai_resp or {}).get('output') or "")
                        _markup = reminder_markup_reason(_raw_r.replace('[SPLIT]', '\n\n')) if _raw_r else None
                        if _markup:
                            log_session(u.platform_user_id,
                                        f"🧹 [Reminder 1 - AI] Текст со служебной разметкой ({_markup}) — не отправлен",
                                        "WARNING")

                        delivered = False
                        if ai_resp and not _markup:
                            # [U1] нормализация: [CABINET_LINK] → URL (R6)
                            bot_text = await normalize_bot_text_for_history(
                                u.platform_user_id,
                                (ai_resp.get('text') or ai_resp.get('output') or "").replace('[SPLIT]', '\n\n'))

                            # [U3 план 2026-09-08-002] Порядок: отправка → фиксация.
                            # Раньше reminder_level=1 и реплика писались ДО отправки,
                            # а «Sent to» логировался безусловно — при FATAL от MAX
                            # клиент ничего не получал, но история и уровень росли.
                            # Записи прохода (успехи раньше, снятие bot_send_blocked) — до
                            # отправки: пометка MAX пишется своим соединением (ревью v3.4.4)
                            await session.commit()
                            delivered = await send_smartbot_reply(u.platform_user_id, ai_resp)
                            if delivered:
                                ctx.pop('reminder_1_pending', None)
                                ctx.pop('reminder_1_failures', None)
                                ctx['reminder_level'] = 1
                                ctx['reminder_1_time'] = now_ts # Запоминаем точную метку времени отправки 1-го ремайндера
                                ctx['chat_history'].append({"time": datetime.now().strftime("%H:%M"), "role": "bot", "text": bot_text})
                                await session.execute(update(User).where(User.id == u.id).values(context_json=json.dumps(ctx, ensure_ascii=False)))
                                if u.platform_user_id in USER_CONTEXT: USER_CONTEXT[u.platform_user_id] = ctx
                                log_session(u.platform_user_id, f"🔔 [Reminder 1 - AI] Sent to {u.platform_user_id}")

                        if _alive_r and not delivered and not _raw_r:
                            # Нет ответа n8n (пусто, таймаут, ошибка) — в потолок не идёт
                            # (v3.4.4, P2.2): общий сбой n8n не глушит напоминания всем
                            log_session(u.platform_user_id,
                                        "⚠️ [Reminder 1 - AI] n8n не ответил — повтор на следующем тике",
                                        "WARNING")
                        elif _alive_r and not delivered:
                            # Неудача доставки в MAX или разметка: счётчик и кэш текста;
                            # на REMINDER_1_MAX_FAILURES — напоминание заглушено (KTD-C3)
                            _failures += 1
                            _tick_client_time = ctx.get('client_last_message_time')
                            # Записи прохода (успехи раньше, снятие bot_send_blocked этого же
                            # клиента) — зафиксировать до точечной записи своим соединением:
                            # иначе она ждёт незакоммиченную строку (PG — вечно, SQLite — locked)
                            await session.commit()
                            if _failures >= REMINDER_1_MAX_FAILURES:
                                _stored = await _store_reminder_1_state(
                                    u.platform_user_id, _tick_client_time, {'reminder_level': 2},
                                    remove=('reminder_1_pending', 'reminder_1_failures'))
                                _msg = f"🔕 [Reminder 1 - AI] {_failures} неудачи — напоминание заглушено"
                            else:
                                _upd = {'reminder_1_failures': _failures}
                                _rm = ()
                                if _raw_r and not _markup:
                                    _upd['reminder_1_pending'] = {"text": _raw_r, "at": now_ts}
                                else:
                                    _rm = ('reminder_1_pending',)
                                _stored = await _store_reminder_1_state(
                                    u.platform_user_id, _tick_client_time, _upd, remove=_rm)
                                _msg = (f"⚠️ [Reminder 1 - AI] Доставка не подтверждена — retry на следующем тике "
                                        f"({_failures}/{REMINDER_1_MAX_FAILURES})")
                            if not _stored:
                                _msg += " — клиент написал за время тика, состояние напоминания не пишется"
                            log_session(u.platform_user_id, _msg, "WARNING")
                            
                    # 2й РЕМАЙНДЕР: По расписанию на основе времени отправки 1-го ремайндера
                    elif rem_level == 1:
                        rem1_time = ctx.get('reminder_1_time')
                        if rem1_time:
                            # Переводим таймстамп 1-го ремайндера в datetime с учетом часового пояса дилера
                            rem1_dt = datetime.utcfromtimestamp(rem1_time) + timedelta(hours=utc_offset)
                            
                            if 6 <= rem1_dt.hour < 18:
                                # ТИП 1 (Вечер): 1-й ремайндер ушел днем (с 6:00 до 18:00). Отправляем в 20:00 этого же дня
                                target_dt = rem1_dt.replace(hour=20, minute=0, second=0, microsecond=0)
                                target_text_msg = "👨‍💼 Александр:\nРешил написать вам вечером, надеюсь вы освободились. Подскажите, актуален ли еще вопрос покупки авто?"
                            else:
                                # ТИП 2 (Утро): 1-й ремайндер ушел вечером или ночью (с 18:00 до 6:00). Отправляем в 9:00 утра
                                if rem1_dt.hour >= 18:
                                    # Если ушел вечером текущего дня — отправляем на следующее утро в 9:00
                                    target_dt = (rem1_dt + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
                                else:
                                    # Если ушел глубокой ночью (0:00 - 5:59) — отправляем этим же утром в 9:00
                                    target_dt = rem1_dt.replace(hour=9, minute=0, second=0, microsecond=0)
                                target_text_msg = "👨‍💼 Александр:\nЗдравствуйте! Решил написать вам утром, надеюсь у вас есть минутка. Подскажите, актуален ли еще вопрос покупки авто?"
                            
                            # Условие отправки: текущее локальное время подошло к целевому
                            if local_time >= target_dt:
                                # [Ревью 2026-08-15] Гард завершения и для 2-го
                                # ремайндера: снапшот active_users взят в начале
                                # прохода, сессия могла завершиться (передача
                                # кабинета/финализация) — не пишем и не шлём
                                async with AsyncSessionLocal() as _r2s:
                                    _alive2 = (await _r2s.execute(
                                        select(User.is_smartbot_active).where(User.id == u.id)
                                    )).scalar_one_or_none()
                                if not _alive2:
                                    continue
                                custom_image = dealer_info.get("reminder_image_url")
                                
                                if custom_image:
                                    attachments = [{"type": "image", "payload": {"url": custom_image}}]
                                    text_msg = "" 
                                    history_log = "[Отправлено напоминание с картинкой]"
                                else:
                                    attachments = None
                                    text_msg = target_text_msg
                                    history_log = f"[Отправлено текстовое напоминание: {text_msg}]"
                                
                                success = await _r_cl.send_message(user_id=_r_raw, text=text_msg, attachments=attachments)
                                
                                # Принудительно гасим дальнейшие проверки для этой сессии
                                ctx['reminder_level'] = 2
                                
                                if success:
                                    ctx['chat_history'].append({"time": datetime.now().strftime("%H:%M"), "role": "bot", "text": history_log})
                                    log_session(u.platform_user_id, f"🔔 [Reminder 2 - Scheduled via Rem1] Sent to {u.platform_user_id}")
                                else:
                                    log_session(u.platform_user_id, f"⚠️ [Reminder 2] MAX API rejected message for {u.platform_user_id}. Muted forever.", "WARNING")
                                    
                                await session.execute(update(User).where(User.id == u.id).values(context_json=json.dumps(ctx, ensure_ascii=False)))
                                if u.platform_user_id in USER_CONTEXT: USER_CONTEXT[u.platform_user_id] = ctx

                await session.commit()
                
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"❌ [Reminder Worker] Error: {e}")
            await asyncio.sleep(60)

async def replay_pending_sync(session):
    """Один цикл реплея очереди pending_sync (R35).

    Успешный реплей удаляет строку. Любой неуспех — исключение или
    «молчаливый» (не-200 или тело с ошибкой) — учитывается через
    record_sync_failure; на пороге PENDING_SYNC_MAX_ATTEMPTS строка
    переводится в fatal с однократным техалертом. Строки fatal реплей
    не трогает: выборка берёт только status='pending'.

    Битрикс выключен (пустой BITRIX_WEBHOOK_URL, дев-стенд) — проход
    пропускается целиком: без попыток, отчёта и алертов (2026-10-02).
    Иначе каждая строка сжигала попытку о пустой адрес и уходила в fatal."""
    if not BitrixService.WEBHOOK:
        logger.info("⏸ [Sync Worker] Битрикс выключен (пустой BITRIX_WEBHOOK_URL) — очередь не реплеится.")
        return
    # Порядок постановки (id): смена стадии встаёт в очередь после полей своей
    # сделки и не должна их обогнать (исправление 2026-09-30)
    result = await session.execute(select(PendingSync).where(PendingSync.status == "pending")
                                   .order_by(PendingSync.id))
    tasks = result.scalars().all()
    count = len(tasks)

    if count == 0:
        logger.info("✅ [Sync Worker] Queue is empty. Silence maintained.")
        return

    unique_leads = set()
    for t in tasks:
        try:
            p = json.loads(t.payload)
            code = p.get('UF_CRM_1731438093')
            if not code:
                fields = p.get('fields', {})
                if isinstance(fields, dict):
                    code = fields.get('UF_CRM_1731438093')

            if not code:
                comments = p.get('COMMENTS', '')
                match = re.search(r"КОД ЗАЯВКИ:\s*([a-zA-Z0-9а-яА-Я]+)", comments)
                if match:
                    code = match.group(1)

            if code: unique_leads.add(code)
        except: pass

    leads_count = len(unique_leads)
    target_group_id = str(TECH_ADMIN_ID) if TECH_ADMIN_ID else str(SUPPORT_GROUP_ID)

    if target_group_id and leads_count > 0:
        logger.warning(f"⚠️ [Sync Worker] Found {count} tasks ({leads_count} unique leads). Sending Alert.")
        report_text = (f"📊 <b>Ежечасный отчет CRM:</b>\n"
                       f"В очереди на досылку: <b>{leads_count}</b> уникальных заявок с сайта.\n"
                       f"(Всего технических операций: {count})\n"
                       f"Статус: Авто-досылка продолжается.")
        try:
            await service_alert_client().send_alert_to_group(group_id=target_group_id, text=report_text)
        except Exception as e:
            logger.error(f"Failed to send hourly report: {e}")
    else:
        logger.info(f"✅ [Sync Worker] {count} tasks in queue, 0 unique leads. Staying silent.")

    blocked_deals = set()   # сделки, чьи поля в этом проходе не дошли
    for task in tasks:
        became_fatal = False
        try:
            _peek = json.loads(task.payload)
        except (TypeError, ValueError):
            _peek = {}
        _deal_ref = str(_peek.get("id") or "") if isinstance(_peek, dict) else ""
        _fields = _peek.get("fields") if isinstance(_peek, dict) else None
        if (_deal_ref in blocked_deals and isinstance(_fields, dict)
                and "STAGE_ID" in _fields):
            # Стадия не обгоняет поля своей сделки: ждёт следующего прохода
            logger.info(f"⏸ [Sync Worker] Task {task.id}: стадия сделки {_deal_ref} "
                        f"ждёт недошедших полей.")
            continue
        stage_lock = None
        try:
            params = json.loads(task.payload)
            url = f"{BitrixService.WEBHOOK}{task.method}"
            # Условная досылка стадии (план 2026-10-12-001, U4): задача несёт
            # «было» — сначала чтение; сделка ушла с «было» → стадию не пишем
            # (остальные поля задачи уходят), без полей — задача снимается.
            # «Обогащение» (план 2026-10-12-002, U3) — чтение всегда, с ключом
            # и без, под замком сделки: не из стадии до передачи → не досылается.
            expect = params.pop(STAGE_EXPECT_KEY, None) if isinstance(params, dict) else None
            enrich = isinstance(params, dict) and _is_enrich_stage((params.get("fields") or {}).get("STAGE_ID"))
            if enrich:
                stage_lock = _deal_stage_lock(_deal_ref)
                await stage_lock.acquire()
            if expect or enrich:
                current = await BitrixService._read_stage_once(str(params.get("id") or ""))
                if current is None:
                    raise RuntimeError("проверочное чтение стадии не удалось")
                if (expect and current != expect) or (enrich and current not in ENRICH_SOURCE_STAGES):
                    rest = {k: v for k, v in (params.get("fields") or {}).items() if k != "STAGE_ID"}
                    why = (f"«было» {expect}" if expect and current != expect
                           else "не из стадии до передачи")
                    logger.warning(f"⏭ [Sync Worker] Task {task.id}: сделка {_deal_ref} на стадии "
                                   f"{current}, {why} — стадия не досылается"
                                   + ("" if rest else ", задача снята"))
                    if not rest:
                        await session.delete(task)
                        await session.commit()
                        continue
                    params = {**params, "fields": rest}
            # Таймаут попытки, как у _request (2026-10-02): зависшая задача —
            # исключение и учёт неуспеха, проход идёт дальше; смена стадии —
            # таймаут стадии (роботы выполняются внутри запроса), с ключом и без (P2.4)
            has_stage = isinstance(params, dict) and "STAGE_ID" in (params.get("fields") or {})
            http = (aiohttp.ClientSession(timeout=BITRIX_TIMEOUT) if not has_stage
                    else aiohttp.ClientSession(timeout=BITRIX_STAGE_TIMEOUT))
            async with http as sess:
                async with sess.post(url, json=params) as resp:
                    if resp.status == 200:
                        res_json = await resp.json()
                        if 'error' not in res_json:
                            await session.delete(task)
                            await session.commit()
                            logger.info(f"✅ [Sync Worker] Task {task.id} recovered.")
                            continue
                    # Молчаливый неуспех: не-200 или тело с ошибкой (R35)
                    if _deal_ref:
                        blocked_deals.add(_deal_ref)
                    became_fatal = record_sync_failure(task)
                    await session.commit()
                    logger.warning(f"⚠️ [Sync Worker] Task {task.id} silent failure (HTTP {resp.status}), attempt {task.attempts}.")
        except Exception as ex:
            if _deal_ref:
                blocked_deals.add(_deal_ref)
            became_fatal = record_sync_failure(task)
            try:
                await session.commit()
            except Exception as commit_ex:
                logger.error(f"❌ [Sync Worker] Failed to persist attempts for task {task.id}: {commit_ex}")
            logger.warning(f"⚠️ [Sync Worker] Task {task.id} retry failed: {ex} (attempt {task.attempts}).")
        finally:
            if stage_lock is not None:
                stage_lock.release()

        if became_fatal:
            # Однократный техалерт: record_sync_failure возвращает True только
            # в момент перехода pending → fatal, дальше строка в выборку не попадает.
            logger.error(f"💀 [Sync Worker] Task {task.id} ({task.method}) достиг потолка попыток → fatal.")
            fatal_group_id = str(TECH_ADMIN_ID) if TECH_ADMIN_ID else str(SUPPORT_GROUP_ID)
            if fatal_group_id:
                alert_text = (f"💀 <b>CRM-очередь: задача переведена в fatal</b>\n"
                              f"ID: {task.id}, метод: <code>{task.method}</code>, user: {task.user_id}\n"
                              f"Попыток: {task.attempts}. Авто-досылка остановлена, нужен ручной реплей.")
                try:
                    await service_alert_client().send_alert_to_group(group_id=fatal_group_id, text=alert_text)
                except Exception as e:
                    logger.error(f"Failed to send fatal alert for task {task.id}: {e}")


async def sync_worker():
    """Smart Monitoring Background Worker (MAX Edition)."""
    logger.info("🛠 Sync Worker started.")
    while True:
        try:
            await asyncio.sleep(1800) # Проверяем брошенные диалоги каждые 30 минут
            
            async with AsyncSessionLocal() as session:
                # --- 1. ОХОТА ЗА БРОШЕННЫМИ ДИАЛОГАМИ ---
                try:
                    result_users = await session.execute(select(User).where(User.active_deal_id != None))
                    all_users = result_users.scalars().all()
                    now_ts = time.time()
                    
                    for u in all_users:
                        if not u.context_json: continue
                        ctx = json.loads(u.context_json)
                        history = ctx.get('chat_history', [])
                        if not history: continue
                        
                        # Отсчет времени ведем от последней активности
                        last_time = ctx.get('last_interaction_time', 0)
                        time_passed = now_ts - last_time
                        
                        start_code = ctx.get('start_code', '')
                        dealer_code = start_code[:2] if len(start_code) >= 2 else "00"
                        dealer_info = CACHED_DEALERS.get(dealer_code, {})
                        prompt_teg = dealer_info.get("Prompt_Teg", "base")
                        prompt_ver = dealer_info.get("Prompt_Ver", "v01.00")
                        deal_id = u.active_deal_id
                        
                        # --- ЛОГИКА А: Регулярный бекап в NocoDB (Каждые 30 минут) ---
                        noco_synced = ctx.get('nocodb_synced_index', 0)
                        is_finalized = ctx.get('is_finalized', False) # <--- ФИКС
                        
                        # Если диалог уже закрыт ИИ (есть флаг is_finalized), игнорируем его
                        if len(history) > noco_synced and time_passed > 1800 and not is_finalized:
                            
                            # СТАЛО: Определяем зону ответственности и последнее действие
                            is_ai = getattr(u, 'is_smartbot_active', False)
                            last_action = ctx.get('last_client_event', 'Нет данных')
                            
                            archive_payload = {
                                "user_id": str(u.platform_user_id),
                                "session_id": f"sess_{u.platform_user_id}_{start_code}",
                                "Bot_Key": _archive_bot_key(ctx, db_user=u, user_id=u.platform_user_id),
                                "model": ctx.get('model', 'Не указана'),
                                "Deal_ID": str(deal_id) if deal_id else "",
                                "Dealer_Code": dealer_code,
                                "Prompt_Version": f"{prompt_teg}_{prompt_ver}",
                                "Scenario": ctx.get('scenario_type', 'unknown'),
                                "Final_Status": "Alexander" if is_ai else "Python",
                                "AI_Report": f"Пауза > 30м. Последнее действие: {last_action}",
                                "Transcript": format_chat_history(history)
                            }
                            await upsert_dialogue_archive(archive_payload)

                            ctx['nocodb_synced_index'] = len(history)
                            # Чистим логи после отправки в архив — они уже сохранены в NocoDB
                            if 'python_logs' in ctx:
                                del ctx['python_logs']
                            await session.execute(update(User).where(User.id == u.id).values(context_json=json.dumps(ctx, ensure_ascii=False)))
                            
                        # --- ЛОГИКА Б: "СМЕРТЬ" ДИАЛОГА ЧЕРЕЗ 48 ЧАСОВ (172800 сек) ---
                        if getattr(u, 'is_smartbot_active', False) and time_passed > 172800:
                            # 1. Отправляем прощальное сообщение (только если канал поддерживает)
                            _farewell_channel = ctx.get('channel', 'max')
                            if _farewell_channel == 'web':
                                farewell_text = "[Автоматическое уведомление]\n\nАссистент Александр покинул чат. Если у Вас появятся новые вопросы, пожалуйста, отправьте повторный запрос. 🌐"
                            else:
                                farewell_text = "[Автоматическое уведомление]\n\nАссистент Александр покинул чат. Если у Вас появятся новые вопросы, пожалуйста, перейдите на сайт и отправьте повторный запрос. 🌐"
                            _fw_cl, _fw_raw = await _worker_resolve_sender(session, u, ctx, now_ts)
                            if _fw_cl is not None and _fw_cl.supports_reminders():
                                await _fw_cl.send_message(user_id=_fw_raw, text=farewell_text)
                            history.append({"time": datetime.now().strftime("%H:%M"), "role": "system", "text": farewell_text})
                            
                            # 2. Выгружаем стенограмму в Битрикс (Только неотправленный остаток)
                            bx_synced = ctx.get('bitrix_synced_index', 0)
                            unsent_history = history[bx_synced:]
                            if unsent_history and deal_id:
                                formatted_unsent = format_chat_history(unsent_history)
                                url = f"{BitrixService.WEBHOOK}crm.timeline.comment.add"
                                payload = {"fields": {"ENTITY_ID": deal_id, "ENTITY_TYPE": "DEAL", "COMMENT": f"[B]🤖 Итоги AI-диалога (Таймаут 48ч):[/B]\n\n{formatted_unsent}"}}
                                # Таймаут попытки, как у _request (2026-10-03)
                                async with aiohttp.ClientSession(timeout=BITRIX_TIMEOUT) as session_bx:
                                    await session_bx.post(url, json=payload)
                            
                            # 3. Финальный статус в NocoDB
                            last_action = ctx.get('last_client_event', 'Нет данных')
                            
                            archive_payload = {
                                "user_id": str(u.platform_user_id),
                                "session_id": f"sess_{u.platform_user_id}_{start_code}",
                                "Bot_Key": _archive_bot_key(ctx, db_user=u, user_id=u.platform_user_id),
                                "model": ctx.get('model', 'Не указана'),
                                "Deal_ID": str(deal_id) if deal_id else "",
                                "Dealer_Code": dealer_code,
                                "Prompt_Version": f"{prompt_teg}_{prompt_ver}",
                                "Scenario": ctx.get('scenario_type', 'unknown'),
                                "Final_Status": "Alexander", # Здесь всегда ИИ, т.к. Логика Б срабатывает только для ИИ (is_smartbot_active)
                                "AI_Report": f"Таймаут 48ч. Последнее действие: {last_action}",
                                "Transcript": format_chat_history(history)
                            }
                            await upsert_dialogue_archive(archive_payload)
                            
                            # Очищаем массив логов ТОЛЬКО после финальной отправки архива
                            if 'python_logs' in ctx:
                                del ctx['python_logs']

                            # 4. [U5 план 2026-09-08-002] Сначала фиксируем контекст
                            # и отключение ИИ с коммитом ЗДЕСЬ ЖЕ: обрыв БД на этом
                            # шаге переносит закрытие на следующий тик — стадия не
                            # двигается, контекст не теряется (прецедент 5488361:
                            # закрытие продолжилось при упавшем сохранении).
                            ctx['bitrix_synced_index'] = len(history)
                            ctx['nocodb_synced_index'] = len(history)
                            # U7a: TTL-закрытие — полноценная финализация для
                            # гейтов «заявка закрыта» (голый старт, close-gate
                            # кабинета). Читатели is_finalized проверены:
                            # побочных действий установка не вызывает.
                            ctx['is_finalized'] = True
                            try:
                                await session.execute(update(User).where(User.id == u.id).values(
                                    is_smartbot_active=False,
                                    context_json=json.dumps(ctx, ensure_ascii=False)
                                ))
                                await session.commit()
                            except Exception as save_err:
                                logger.error(f"❌ [Логика Б] Контекст {u.platform_user_id} не сохранён — "
                                             f"закрытие перенесено на следующий тик: {save_err}")
                                await session.rollback()
                                continue

                            # 5. Закрываем сделку в Битриксе (Стадия Проиграно) —
                            # после фиксации стейта; LOSE идемпотентен, сбой здесь
                            # данных не теряет и виден в логе
                            if deal_id:
                                # [План 2026-09-08-001, U3/B-1] SB-поля из слотов
                                # защёлки — данные 48ч-сделки полезны при разборе
                                # LOSE; R34 — владение кабинета после сабмита
                                payload_fields = {"STAGE_ID": BitrixService.STAGE_LOSE, "COMMENTS": "Закрыто автоматически: таймаут 48 часов."}
                                _ai_fields = await cabinet_service.filter_cabinet_owned_fields(
                                    build_ai_tradein_fields(ctx), deal_id)
                                if _ai_fields:
                                    logger.info("📤 AI_TRADEIN_FIELDS deal=%s source=slots(48h) fields=%s",
                                                deal_id, _ai_fields)
                                    payload_fields.update(_ai_fields)
                                url_update = f"{BitrixService.WEBHOOK}crm.deal.update"
                                payload_update = {"id": deal_id, "fields": payload_fields}
                                try:
                                    async with aiohttp.ClientSession(timeout=BITRIX_TIMEOUT) as session_bx:
                                        await session_bx.post(url_update, json=payload_update)
                                except Exception as bx_err:
                                    logger.error(f"❌ [Логика Б] Сделка {deal_id} не закрыта (LOSE) "
                                                 f"после отключения ИИ — закрыть руками: {bx_err}")
                            log_session(u.platform_user_id, f"💀 [Sync Worker] Диалог {u.platform_user_id} убит по таймауту 48 часов.")

                    await session.commit()
                except Exception as e:
                    logger.error(f"❌ [Sync Worker] Ошибка сборки брошенных диалогов: {e}")

                # --- 2. ОБРАБОТКА ОЧЕРЕДИ СИНХРОНИЗАЦИИ ---
                # Потолок попыток и fatal-строки — R35, см. replay_pending_sync.
                # Свой try/except, как у соседних шагов: сбой реплея (БД/сеть)
                # не должен уносить с собой обязанности кабинета — экспирацию
                # токенов и проверку баланса — на следующие полчаса.
                try:
                    await replay_pending_sync(session)
                except Exception as e:
                    logger.error(f"❌ [Sync Worker] Реплей очереди синхронизации: {e}")

            # --- 3. КАБИНЕТ (U3, wiring-only): экспирация токенов + баланс (R32/R33) ---
            try:
                await cabinet_service.sync_worker_duties()
            except Exception as e:
                logger.error(f"❌ [Sync Worker] Обязанности кабинета: {e}")

        except asyncio.CancelledError:
            logger.info("🛑 Sync Worker cancelled.")
            break
        except Exception as e:
            logger.error(f"❌ [Sync Worker FATAL] Error: {e}")
            
            # Отправляем алерт ИСКЛЮЧИТЕЛЬНО в техническую группу
            if TECH_ADMIN_ID:
                try:
                    alert_text = (
                        f"🆘 <b>[MAX Sync Worker FATAL] Фоновый процесс упал!</b>\n\n"
                        f"Ошибка: <code>{e}</code>\n"
                        f"<i>Воркер приостановлен на 60 секунд для защиты от спама логами.</i>"
                    )
                    await service_alert_client().send_alert_to_group(group_id=TECH_ADMIN_ID, text=alert_text)
                except Exception as alert_err:
                    logger.error(f"Не удалось отправить алерт воркера в MAX: {alert_err}")
            
            await asyncio.sleep(60)

async def handle_tg_webhook(request: web.Request):
    bot_key, deny = _webhook_bot_resolve(request, "telegram")
    if deny is not None:
        return deny
    # U4 (KTD-6): служебные вызовы TG API — клиентом бота-получателя;
    # фолбэк на глобальный tg_client сохраняет легаси-поведение (R7)
    _tg_api = bot_registry.get_bot_client(bot_key) or tg_client
    try:
        obj = await request.json()

        user_id = ""
        raw_user_id = ""  # U3: «чистый» TG-ID для chat_id/удалений (KTD-5)
        user_name = "Клиент"
        user_username = ""
        text = ""
        input_type = "text"
        message_id_for_removal = None
        original_text_for_removal = None

        if "callback_query" in obj:
            cq = obj["callback_query"]
            from_obj = cq.get("from", {})
            raw_user_id = str(from_obj.get("id", ""))
            user_id = bot_identity.build_internal_id(bot_key, raw_user_id) if raw_user_id else ""
            user_name = from_obj.get("first_name", "Клиент")
            user_username = from_obj.get("username", "")
            callback_data = cq.get("data", "")
            text = CALLBACK_MAPPING.get(callback_data, callback_data)
            input_type = "button"

            msg = cq.get("message", {})
            chat_id_for_removal = str(msg.get("chat", {}).get("id", ""))
            msg_id_for_removal = str(msg.get("message_id", ""))
            if chat_id_for_removal and msg_id_for_removal:
                message_id_for_removal = f"{chat_id_for_removal}:{msg_id_for_removal}"
                original_text_for_removal = msg.get("text", "")

            asyncio.create_task(_tg_api.answer_callback(obj["callback_query"]["id"]))

        elif "message" in obj:
            msg = obj["message"]
            from_obj = msg.get("from", {})

            if from_obj.get("is_bot"):
                return web.json_response({"ok": True})

            raw_user_id = str(from_obj.get("id", ""))
            user_id = bot_identity.build_internal_id(bot_key, raw_user_id) if raw_user_id else ""
            user_name = from_obj.get("first_name", "Клиент")
            user_username = from_obj.get("username", "")

            if "contact" in msg:
                contact = msg["contact"]
                phone = contact.get("phone_number", "")
                if phone and user_id:
                    # U2/U3 мультибота: контакт-кнопка обрабатывается ДО process_input —
                    # пользователь-первоход получает полную идентичность бота (KTD-5)
                    await save_user_phone(user_id, phone, user_name, user_username,
                                          channel="telegram", bot_key=bot_key,
                                          raw_platform_id=raw_user_id)
                    text = phone
                    input_type = "text"
                else:
                    return web.json_response({"ok": True})

            elif "text" in msg:
                raw_text = msg["text"]
                if raw_text.startswith("/start"):
                    parts = raw_text.split(" ", 1)
                    if len(parts) > 1 and parts[1]:
                        payload = parts[1].replace("_", " ")
                        text = "start " + payload
                        input_type = "deeplink"
                    else:
                        # U7a: голый /start больше не игнорируется молча —
                        # уходит в единый гейт process_input как «start »
                        # (паритет с MAX bot_started без payload). Прежний
                        # анти-дубль «второй /start после диплинка» живёт в
                        # гейте (BARE_START_DEDUP_WINDOW). Сообщение /start
                        # по-прежнему удаляем из чата — UX «не путать
                        # пользователя» (комментарий прод-3.1.2 сохранён).
                        bare_start_user_id = str(from_obj.get("id", ""))
                        bare_start_msg_id = msg.get("message_id")
                        if bare_start_user_id and bare_start_msg_id:
                            asyncio.create_task(_tg_api.delete_message(bare_start_user_id, bare_start_msg_id))
                        text = "start "
                        input_type = "deeplink"
                else:
                    text = raw_text
                    input_type = "text"
            else:
                return web.json_response({"ok": True})
        else:
            return web.json_response({"ok": True})

        if not user_id or not text:
            return web.json_response({"ok": True})

        if user_id not in USER_CONTEXT:
            USER_CONTEXT[user_id] = await load_context_from_db(user_id)
        # U3: chat_id — из СЫРОГО TG-ID; int(составного ID) ронял бы обработчик
        # 500-кой на каждом событии нового бота (блокер выката, категория А)
        USER_CONTEXT[user_id]['chat_id'] = int(raw_user_id)
        USER_CONTEXT[user_id]['channel'] = 'telegram'

        if message_id_for_removal and original_text_for_removal:
            asyncio.create_task(_tg_api.remove_inline_keyboard(message_id_for_removal, original_text_for_removal))

        asyncio.create_task(process_input(user_id, text, user_name, user_username, input_type=input_type, channel="telegram", bot_key=bot_key))
        return web.json_response({"ok": True})

    except Exception:
        raise



async def handle_chat_message(request: web.Request):
    """Принимает сообщение от web-виджета и передаёт в process_input."""
    try:
        obj = await request.json()
        session_id = obj.get("session_id", "")
        text = obj.get("text", "").strip()
        # Маппинг callback payload → текст (как в MAX/TG каналах)
        text = CALLBACK_MAPPING.get(text, text)
        payload = obj.get("payload", "")
        user_name = obj.get("user_name", "Клиент")
        logger.info(f"📨 [chat_message] session_id={session_id} payload={payload!r} text={text!r}")

        if not session_id:
            return web.json_response({"ok": False, "error": "no session_id"}, status=400)

        if session_id not in USER_CONTEXT:
            USER_CONTEXT[session_id] = await load_context_from_db(session_id)
        USER_CONTEXT[session_id]['channel'] = 'web'
        USER_CONTEXT[session_id]['chat_id'] = session_id

        if payload:
            text = "start " + payload.replace("_", " ")
            input_type = "deeplink"
        else:
            input_type = "text"
            if not text:
                return web.json_response({"ok": False, "error": "no text"}, status=400)

        asyncio.create_task(process_input(
            user_id=session_id,
            user_text=text,
            user_name=user_name,
            user_username="",
            input_type=input_type,
            channel="web"
        ))
        return web.json_response({"ok": True})

    except Exception:
        raise


async def handle_chat_stream(request: web.Request):
    """SSE endpoint: держит соединение открытым и пушит сообщения из очереди."""
    session_id = request.match_info.get("session_id", "")
    if not session_id:
        return web.Response(status=400)

    response = web.StreamResponse()
    response.headers['Content-Type'] = 'text/event-stream'
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    # CORS-заголовки ДОЛЖНЫ быть до prepare() — после уже поздно
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, ngrok-skip-browser-warning'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    await response.prepare(request)

    q = web_client.get_or_create_queue(session_id)
    logger.info(f"📡 [SSE] session={session_id} connected")

    try:
        while True:
            try:
                # Ждём сообщение из очереди, таймаут 30 сек
                msg = await asyncio.wait_for(q.get(), timeout=30.0)
                data = json.dumps(msg, ensure_ascii=False)
                await response.write(f"data: {data}\n\n".encode('utf-8'))
                logger.info(f"📡 [SSE] session={session_id} sent: {msg}")
            except asyncio.TimeoutError:
                # Keepalive ping каждые 30 сек чтобы соединение не закрылось
                await response.write(b": ping\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        logger.info(f"📡 [SSE] session={session_id} disconnected")
    finally:
        return response


async def _bot_registry_alert(text: str) -> bool:
    """Хук техалертов реестра ботов (U1 план 2026-09-15-001): парную строку
    TECH_ALERT пишет сам bot_registry ДО вызова; здесь — только отправка через
    служебный MAX-бот (R5). Возвращает подтверждение доставки — дедуп реестра
    фиксируется только по True (learning log-result-only-on-confirmed-result)."""
    if not TECH_ADMIN_ID:
        return False
    return bool(await service_alert_client().send_alert_to_group(group_id=TECH_ADMIN_ID, text=text))


async def _sms_tech_alert(text: str):
    """Техалерт самопроверки SMS-канала (U2, R32/R33) — существующий механизм.

    [U1 план 2026-09-08-002] Парная строка в логе ДО отправки в чат: алерт,
    видимый только в мессенджере, дважды уводил диагностику в сторону
    (два 627 за 09-08 без единого следа в системе). Грепается TECH_ALERT."""
    logger.error("TECH_ALERT %s", text)
    if TECH_ADMIN_ID:
        await service_alert_client().send_alert_to_group(group_id=TECH_ADMIN_ID, text=text)


# --- U3: wiring кабинета (вся логика — в cabinet_service, здесь только проводка) ---

async def _cabinet_run_conversion(user_id: int):
    """U10: запуск существующей конверсии для передачи заявки из кабинета.

    Снапшот контекста строится ИЗ БД (cabinet_service.build_conversion_snapshot:
    User.context_json + cabinet_profile), НЕ из USER_CONTEXT: кабинет живёт
    дольше сессии диалога, и к моменту таймаут-передачи контекст в памяти
    может быть уже вычищен. main.py — тонкая проводка."""
    platform_uid, context_snapshot = await cabinet_service.build_conversion_snapshot(user_id)
    if platform_uid is None:
        logger.error(f"❌ [Cabinet U10] run_conversion: user id={user_id} не найден")
        return None
    # Стадию «Обогащение» ставит кабинет ПОСЛЕ полей анкеты
    # (cabinet_service._set_handoff_stage, исправление 2026-09-30)
    return await BitrixService.final_sync_wrapper(platform_uid, context_snapshot, skip_stage=True)


async def _cabinet_bitrix_request(method: str, params: dict, user_id: str = None) -> dict:
    """U3: вызов Bitrix для кабинета — БЕЗ собственной постановки в pending_sync.

    BitrixService._request после трёх неудач сам ставил задачу в очередь
    (дебаунсимым путём) и возвращал {}; кабинет видел неуспех и ставил ВТОРУЮ
    строку недебаунсимым путём (R35). replay_pending_sync реплеит обе:
    crm.deal.update идемпотентен, а crm.timeline.comment.add создавал ДВА
    комментария в сделке. Постановка остаётся ровно одна — та, что требует
    R35 (недебаунсимая, из cabinet_service._bitrix_call_or_enqueue)."""
    return await BitrixService._request(method, params, user_id=user_id,
                                        enqueue_on_failure=False)


async def _cabinet_find_deal_by_code(code: str):
    """Веб-вход U5: сделка Tilda по коду в «Новая» — только чтение. Одна
    попытка с таймаутом BITRIX_LOOKUP_TIMEOUT, без повторов и без очереди
    (2026-10-02): «открыть сессию» не ждёт Битрикс; сбой — исключение,
    вызывающий (cabinet_service._find_tilda_deal) считает сделку не найденной,
    её найдёт конверсия. Битрикс выключен (пустой BITRIX_WEBHOOK_URL) — None."""
    if not BitrixService.WEBHOOK:
        return None
    params = {"filter": {BitrixService.F_CODE: code, "STAGE_ID": "C9:NEW"},
              "select": ["ID", "STAGE_ID", BitrixService.F_CODE]}
    async with aiohttp.ClientSession(timeout=BITRIX_LOOKUP_TIMEOUT) as session:
        async with session.post(f"{BitrixService.WEBHOOK}crm.deal.list", json=params) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Bitrix crm.deal.list HTTP {resp.status}")
            data = await resp.json()
    if not isinstance(data, dict) or "error" in data:
        raise RuntimeError(f"Bitrix crm.deal.list: {data.get('error') if isinstance(data, dict) else data}")
    deals = data.get("result") or []
    return deals[0] if deals else None


def setup_cabinet_service():
    """Инъекция зависимостей в cabinet_service — единственный канал (план U3)."""
    cabinet_service.setup(
        # Вызов Bitrix без своей постановки в очередь — единственная постановка
        # идёт недебаунсимым путём ниже (R35, без дублей в pending_sync)
        bitrix_request=_cabinet_bitrix_request,
        # Недебаунсимая постановка в очередь (R35): кабинет ставит мимо 30-секундного окна
        enqueue_nodebounce=lambda method, params, user_id=None: BitrixService.enqueue_failed_request(
            method, params, user_id=user_id, skip_debounce=True),
        # U10: существующий путь конверсии; снапшот контекста — из БД, не из USER_CONTEXT
        run_conversion=_cabinet_run_conversion,
        # Стадия передачи — последней записью, после полей анкеты (2026-09-30)
        handoff_stage=BitrixService.STAGE_QUALIFIED,
        # План 2026-10-12-001: условная смена стадии передачи
        set_stage=BitrixService.set_stage_guarded,
        dealer_alert=send_dealer_alert,
        tech_alert=_sms_tech_alert,
        dealers_provider=lambda: CACHED_DEALERS,
        # U5 (план 2026-08-06): загрузчик таблиц NocoDB (TTL-кэш 600 с внутри)
        # и база для абсолютных URL фото; токен NocoDB в cabinet_service не уходит
        nocodb_table_loader=get_cars_from_nocodb_dynamic,
        nocodb_base_url=NOCODB_API_URL,
        # План 2026-08-14-001 (U1/U2): завершение сессии диалога при
        # доведённой передаче заявки кабинета в CRM
        on_handoff_completed=close_cabinet_session,
        # Веб-вход (план 2026-09-30-001, U3): точечный запрос строки каталога
        catalog_row_loader=get_catalog_row_by_pair,
        # Веб-вход (план 2026-09-30-001, U5): сделка Tilda по коду — только чтение
        find_deal_by_code=_cabinet_find_deal_by_code,
        # 2026-10-02: пустой BITRIX_WEBHOOK_URL — подбор зависших передач не запускается
        bitrix_enabled=lambda: bool(BitrixService.WEBHOOK),
    )


async def handle_sms_status_callback(request: web.Request):
    """GET/POST /sms/status-callback — тонкая обёртка, логика в cabinet_service (R31)."""
    return await cabinet_service.handle_status_callback(request)


async def otp_worker():
    """U3: короткоинтервальный цикл OTP-оркестрации. Тело — cabinet_service.otp_worker_tick();
    здесь только CancelledError, catch-all с бэкоффом и дедупом техалертов."""
    logger.info("⏱ OTP Worker started.")
    interval = int(os.getenv("CABINET_OTP_WORKER_INTERVAL", "30"))
    delay = interval
    _alert_dedup = {}  # текст ошибки -> ts последнего алерта (не спамить чаще раза в 15 мин)
    while True:
        try:
            await asyncio.sleep(delay)
            await cabinet_service.otp_worker_tick()
            delay = interval  # успешный проход сбрасывает бэкофф
        except asyncio.CancelledError:
            logger.info("🛑 OTP Worker cancelled.")
            break
        except Exception as e:
            logger.error(f"❌ [OTP Worker] Ошибка прохода: {e}")
            delay = min(max(delay * 2, interval), 300)  # бэкофф до 5 минут
            err_key = f"{type(e).__name__}: {e}"
            now_ts = time.time()
            if TECH_ADMIN_ID and now_ts - _alert_dedup.get(err_key, 0) > 900:
                _alert_dedup[err_key] = now_ts
                try:
                    await _sms_tech_alert(
                        f"🆘 <b>[OTP Worker] Ошибка цикла кабинета:</b>\n<code>{err_key}</code>")
                except Exception as alert_err:
                    logger.error(f"Не удалось отправить алерт OTP Worker: {alert_err}")


async def start_background_tasks(app):
    """Привязываем фоновые задачи к жизненному циклу aiohttp"""
    app['sync_worker'] = asyncio.create_task(sync_worker())
    app['reminder_worker'] = asyncio.create_task(reminder_worker())
    # U2: стартовая самопроверка ProstoSMS — fire-and-forget, запуск бота не блокирует
    app['sms_selfcheck'] = asyncio.create_task(sms_gateway.startup_selfcheck(_sms_tech_alert))
    # U3: короткоинтервальный воркер OTP-оркестрации кабинета
    app['otp_worker'] = asyncio.create_task(otp_worker())
    # U4/R38: пустой справочник дилеров — техалерт и повторные попытки загрузки
    # (обесточенный кабинет не должен деградировать тихо)
    app['dealers_watchdog'] = asyncio.create_task(
        cabinet_service.ensure_dealers_loaded(load_data_from_nocodb))
    # U1 (план 2026-09-25-001, R5): периодическая перечитка дилеров — новые
    # таблицы попадают в allowlist /catalog без рестарта (≤10 мин)
    app['dealers_refresh_worker'] = asyncio.create_task(dealers_refresh_worker())
    # Веб-вход (2026-10-02): прогрев таблиц ставок и программ — не по первому запросу
    app['rates_programs_warm_worker'] = asyncio.create_task(rates_programs_warm_worker())
    # U1 мультибота: периодическое обновление реестра ботов + сторож подписок MAX
    # (боты только с подтверждённой регистрацией — до U7 сторож молчит, Фаза 0.10)
    app['bots_registry_worker'] = asyncio.create_task(bot_registry.registry_worker())
    # Веб-вход U7 (R68): подменный ответ оценки разрешён только на дев-стенде —
    # флаг в другом окружении игнорируется, оператору техалерт
    import valuation_client
    if valuation_client.fake_flag_misconfigured():
        logger.error("CABINET_EVAL_FAKE задан вне дев-окружения — подмена оценки выключена")
        app['eval_fake_alert'] = asyncio.create_task(_sms_tech_alert(
            "⚠️ CABINET_EVAL_FAKE задан вне дев-окружения (BOT_ENV≠dev или "
            "ENVIRONMENT=production) — подменный ответ оценки выключен."))

async def init_app():
    # U4: проверка секретов кабинета ДО любой инициализации —
    # ENVIRONMENT=production без CABINET_HMAC_KEY валит старт с внятной ошибкой
    cabinet_service.check_secrets()
    # Каналы бот ↔ n8n: strict без секрета колбэка — ошибка старта
    n8n_auth.validate_config()
    await init_db()
    await load_data_from_nocodb()
    await load_prompts_from_nocodb()

    # U1 мультибота (план 2026-09-15-001): реестр ботов. Загрузка не блокирует
    # старт — при недоступной NocoDB работает цепочка R13 (снапшот БД → env-минимум),
    # добор — в bots_registry_worker. Маршрутизацию U1 не меняет.
    bot_registry.setup(alert=_bot_registry_alert)
    try:
        await bot_registry.load_bots_registry()
    except Exception:
        logger.exception("bot_registry: стартовая загрузка не удалась — доберёт воркер")

    logger.info("🚀 UNIFIED BOT STARTED (MAX EDITION v2.0)")
    USER_CONTEXT.clear()

    # U3: инъекция зависимостей кабинета — до регистрации роутов и воркеров
    setup_cabinet_service()

    # Подключаем наш глобальный щит от падений
    app = web.Application(middlewares=[error_middleware, cors_middleware,
                                       compression_middleware])
    app.router.add_post('/max_webhook', handle_max_webhook)
    app.router.add_post('/tg_webhook', handle_tg_webhook)
    # U3 мультибота (KTD-4): URL на бота; голые адреса выше — легаси-алиасы (R7)
    app.router.add_post('/max_webhook/{bot_key}', handle_max_webhook)
    app.router.add_post('/tg_webhook/{bot_key}', handle_tg_webhook)
    app.router.add_post('/chat_message', handle_chat_message)
    app.router.add_route('OPTIONS', '/chat_message', handle_cors_preflight)
    app.router.add_get('/catalog/{dealer_code}', handle_catalog)
    app.router.add_get('/catalog-colors', handle_catalog_colors)
    app.router.add_route('OPTIONS', '/catalog/{dealer_code}', handle_cors_preflight)
    app.router.add_route('OPTIONS', '/catalog-colors', handle_cors_preflight)
    # U9: ссылки активных ботов для сайта (только brand/channel/link)
    app.router.add_get('/bot-links', handle_bot_links)
    app.router.add_route('OPTIONS', '/bot-links', handle_cors_preflight)
    app.router.add_route('OPTIONS', '/chat_stream/{session_id}', handle_cors_preflight)
    app.router.add_get('/chat_stream/{session_id}', handle_chat_stream)
    app.router.add_post('/n8n_callback', handle_n8n_callback)
    # U3: приём push-статусов ProstoSMS — публичный, GET и POST, вне CORS-whitelist
    app.router.add_get('/sms/status-callback', handle_sms_status_callback)
    app.router.add_post('/sms/status-callback', handle_sms_status_callback)
    # U4: auth-API кабинета /cabinet/api/* — тонкие хендлеры, логика в cabinet_service
    cabinet_api.register_routes(app)
    _widget_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'widget-custom')
    app.router.add_static('/widget-custom/', path=_widget_dir, name='widget_static')
    app.on_response_prepare.append(widget_static_utf8_charset)
    
    # Регистрируем запуск воркера при старте сервера
    app.on_startup.append(start_background_tasks)
    
    return app

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    # Передаем саму корутину init_app(), сервер web.run_app сам создаст цикл событий
    web.run_app(init_app(), host='0.0.0.0', port=port)