"""U1 (план 2026-09-15-001): реестр ботов MAX/Telegram с брендами.

Источник — таблица NocoDB Bots (метаданные, без секретов); токены — в env по
имени из Token_Env (R8). Порядок загрузки (R13): NocoDB → снапшот в таблице
БД bot_registry_snapshot (Д3) → минимальный реестр легаси-ботов из env.

Окружения (Фаза 0.2): NocoDB общий для дев и прода, записи фильтруются по
Environment ∈ {BOT_ENV, all}; BOT_ENV без дефолта — не задан/невалиден →
только записи all + один дедуп-техалерт (правка владельца 2026-09-16).

Секретный блок снапшота (intent-хэш, secret_enforced, confirmed_url) пишет
ТОЛЬКО scripts/register_webhooks.py (U7, R18) — синхронизация реестра его
не перетирает. Маршрутизацию модуль не меняет: U1 только загружает реестр
и держит клиентов; выбор отправителя по боту — U4 (get_sender).

Зависимости инъецируются через setup() (образец — cabinet_service.setup):
alert — async callable(text) -> truthy при подтверждённой доставке;
fetch_rows — async callable() -> list[dict] из NocoDB (в тестах — фейк).
"""
import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

import aiohttp
from sqlalchemy import select

import bot_identity
import database
from max_client import MaxBotClient
from sms_gateway import Secret
from telegram_client import TelegramBotClient

logger = logging.getLogger(__name__)

NOCODB_TABLE_ID_BOTS = "ma0nf1e807qb9e0"
VALID_ENVS = ("prod", "dev")
VALID_CHANNELS = ("max", "telegram")

# Легаси-боты (R7/R15, схема U9 2026-09-22): ключи и переменные — по
# окружению; кортеж имён переменных — с фолбэком на дообменные имена
# (переходный период переименования env). dev-пара — боты «все марки» ('*').
LEGACY_BOTS_BY_ENV = {
    "prod": (
        ("max-geely", "max", ("MAX_BOT_TOKEN_GEELY", "MAX_BOT_TOKEN"),
         frozenset({"geely", "belgee"})),
        ("tg-geely", "telegram", ("TG_BOT_TOKEN_GEELY", "TG_BOT_TOKEN"),
         frozenset({"geely", "belgee"})),
    ),
    "dev": (
        ("max-test", "max", ("MAX_BOT_TOKEN_TEST", "MAX_BOT_TOKEN"),
         frozenset({"*"})),
        ("tg-test", "telegram", ("TG_BOT_TOKEN_TEST", "TG_BOT_TOKEN"),
         frozenset({"*"})),
    ),
}


def legacy_bots_for_env():
    """Пары легаси-ботов текущего окружения; BOT_ENV не задан → dev-пара
    (аварийный минимум: на проде dev-имена переменных отсутствуют, фолбэк
    поднимет ботов на старых именах — живо, и алерт о BOT_ENV уже уходит)."""
    return LEGACY_BOTS_BY_ENV["prod" if current_env() == "prod" else "dev"]


def legacy_key_for_channel(channel: str) -> str:
    """Актуальный легаси-ключ канала в текущем окружении (алиасы URL/ctx)."""
    for bot_key, ch, _envs, _brands in legacy_bots_for_env():
        if ch == channel:
            return bot_key
    return ""


def resolve_alias(bot_key):
    """Историческое имя (max-main/tg-main) → актуальный легаси-ключ окружения.
    Переходная логика U9: старые ключи остаются в ctx активных сессий,
    в users до миграции и в URL старых подписок."""
    channel = bot_identity.HISTORIC_LEGACY_KEYS.get(bot_key)
    if channel:
        return legacy_key_for_channel(channel) or bot_key
    return bot_key


def legacy_env_token(channel: str) -> str:
    """Токен легаси-бота канала из env (с фолбэком имён) — для служебного
    клиента алертов и env-минимума."""
    for _key, ch, env_names, _brands in legacy_bots_for_env():
        if ch == channel:
            for name in env_names:
                value = os.getenv(name, "")
                if value:
                    return value
    return ""

ALERT_COOLDOWN = 15 * 60  # секунд; дедуп как у _alert_dedup в otp_worker


@dataclass
class BotEntry:
    bot_key: str
    channel: str
    brands: frozenset
    token: Secret
    token_env: str
    active: bool
    environment: str = "all"
    is_legacy: bool = False
    # Секретный блок — источник истины в БД (Фаза 0.3), сюда читается из снапшота
    secret_enforced: bool = False
    confirmed_url: str = ""
    # U9: публичная ссылка бота (столбец Link в Bots) — для /bot-links сайта
    link: str = ""
    # U9a: своя база вебхука (столбец Webhook_Base в Bots) — прокси/воркер
    # обхода блокировки TG; пусто — вебхук прямо на PUBLIC_BASE_URL.
    # Заголовки прокси передаёт целиком, поэтому производный секрет (KTD-3)
    # проверяется как обычно; сторож MAX сверяет confirmed_url — фактический
    # (воркерный) URL, отдельной логики не требует
    webhook_base: str = ""

    @property
    def has_token(self) -> bool:
        return bool(self.token)

    def webhook_secret(self) -> str:
        """Производный секрет (KTD-3); пустой токен -> пустая строка."""
        if not self.has_token:
            return ""
        return bot_identity.derive_webhook_secret(self.token.reveal(), self.bot_key)


CACHED_BOTS: Dict[str, BotEntry] = {}
_CLIENTS: Dict[str, object] = {}

_alert_hook = None
_fetch_rows = None
_ALERT_DEDUP: Dict[str, float] = {}
# Источник последней загрузки: nocodb | snapshot | env | none (до первой загрузки).
# KTD-4: "env"/"none" = деградированный реестр — неизвестный bot_key получает 503,
# чтобы платформы ретраили события до восстановления NocoDB.
_source = "none"


def setup(alert=None, fetch_rows=None):
    """Инъекция зависимостей — единственный канал (образец cabinet_service)."""
    global _alert_hook, _fetch_rows
    if alert is not None:
        _alert_hook = alert
    if fetch_rows is not None:
        _fetch_rows = fetch_rows


def reset_for_tests():
    """Полный сброс модульного состояния (только для тестов)."""
    global _alert_hook, _fetch_rows, _source
    _alert_hook = None
    _fetch_rows = None
    _source = "none"
    CACHED_BOTS.clear()
    _CLIENTS.clear()
    _ALERT_DEDUP.clear()


def is_degraded() -> bool:
    """KTD-4: реестр в деградации (env-минимум или ещё не загружен)."""
    return _source in ("env", "none")


def current_env() -> Optional[str]:
    """BOT_ENV без дефолта: prod/dev или None (не задан/невалиден)."""
    value = os.getenv("BOT_ENV", "").strip().lower()
    return value if value in VALID_ENVS else None


def parse_brands(raw) -> frozenset:
    """'Geely, Belgee' -> {'geely','belgee'} (R15, регистронезависимо)."""
    if not raw:
        return frozenset()
    return frozenset(p.strip().lower() for p in str(raw).split(",") if p.strip())


async def _tech_alert(key: str, text: str, cooldown: float = ALERT_COOLDOWN) -> None:
    """Дедуп-техалерт: парная греп-строка TECH_ALERT в лог ДО отправки
    (learning log-result-only-on-confirmed-result); кулдаун фиксируется
    только по подтверждённой доставке — иначе повтор на следующем событии."""
    now = time.time()
    if now - _ALERT_DEDUP.get(key, 0) < cooldown:
        return
    logger.warning("TECH_ALERT [bot_registry:%s] %s", key, text)
    if _alert_hook is None:
        return
    try:
        delivered = await _alert_hook(text)
    except Exception as e:
        logger.error("bot_registry: алерт '%s' не отправлен: %s", key, e)
        return
    if delivered:
        _ALERT_DEDUP[key] = now


async def tech_alert(key: str, text: str, cooldown: float = ALERT_COOLDOWN) -> None:
    """Публичный дедуп-техалерт реестра — общий механизм для U3/U4/U6
    (KTD-7: второй параллельный дедуп не заводим)."""
    await _tech_alert(key, text, cooldown)


async def _default_fetch_rows():
    """Чтение таблицы Bots из NocoDB (паритет с load_data_from_nocodb)."""
    token = os.getenv("NOCODB_TOKEN")
    base = os.getenv("NOCODB_API_URL")
    if not token or not base:
        return None
    url = f"{base}/api/v2/tables/{NOCODB_TABLE_ID_BOTS}/records"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers={"xc-token": token},
                               params={"limit": 100}, ssl=False,
                               timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"NocoDB Bots HTTP {resp.status}")
            data = await resp.json()
    return data.get("list", [])


def _resolve_token(entry_env_name: str) -> Secret:
    return Secret(os.getenv(entry_env_name, "") or "")


async def _entries_from_rows(rows, env: Optional[str]) -> Dict[str, BotEntry]:
    entries: Dict[str, BotEntry] = {}
    allowed_envs = {"all"} | ({env} if env else set())
    for row in rows:
        bot_key = str(row.get("Bot_Key") or "").strip()
        if not bot_key:
            continue
        channel = str(row.get("Channel") or "").strip().lower()
        row_env = (str(row.get("Environment") or "all").strip().lower()) or "all"
        if row_env not in ("all",) + VALID_ENVS:
            logger.warning("bot_registry: запись %s с неизвестным Environment=%r пропущена",
                           bot_key, row_env)
            continue
        if row_env not in allowed_envs:
            continue  # чужое окружение — не наша запись (Фаза 0.2)
        active = bool(row.get("Active"))
        entry = BotEntry(
            bot_key=bot_key,
            channel=channel,
            brands=parse_brands(row.get("Brands")),
            token=_resolve_token(str(row.get("Token_Env") or "")),
            token_env=str(row.get("Token_Env") or ""),
            active=active,
            environment=row_env,
            is_legacy=bot_key in bot_identity.LEGACY_BOT_KEYS,
            link=str(row.get("Link") or "").strip(),
            webhook_base=str(row.get("Webhook_Base") or "").strip().rstrip("/"),
        )
        # U9-переход: легаси-запись окружения с ещё не заведённой новой
        # env-переменной поднимается на фолбэк-токене (старое имя) — боевые
        # боты не молчат ни на одном шаге переименования переменных
        if entry.is_legacy and not entry.has_token and channel in VALID_CHANNELS:
            fallback = legacy_env_token(channel)
            if fallback:
                entry.token = Secret(fallback)
                entry.active = active
        if not bot_identity.is_valid_bot_key(bot_key):
            entry.active = False  # KTD-1: bot_key уходит в session_id -> SQL n8n
            await _tech_alert(
                f"bad-bot-key:{bot_key}",
                f"⚠️ Реестр ботов: недопустимый bot_key {bot_key!r} (только [a-z0-9-]) — запись деактивирована")
        if channel not in VALID_CHANNELS:
            entry.active = False
            await _tech_alert(
                f"bad-channel:{bot_key}",
                f"⚠️ Реестр ботов: у {bot_key} неизвестный Channel {channel!r} — запись деактивирована")
        if entry.active and not entry.has_token:
            entry.active = False  # R9
            await _tech_alert(
                f"token-missing:{bot_key}",
                f"⚠️ Реестр ботов: у {bot_key} не задана env-переменная {entry.token_env or '<пусто>'} — бот неактивен (R9)")
        entries[bot_key] = entry
    return entries


async def _ensure_legacy(entries: Dict[str, BotEntry]) -> None:
    """Легаси-боты окружения присутствуют всегда (R7/R13, схема U9): из
    NocoDB-записи, если она есть (бренды оттуда), иначе — из env; токен —
    первое непустое из имён переменных (новое имя, затем дообменное)."""
    for bot_key, channel, env_names, brands in legacy_bots_for_env():
        if bot_key in entries:
            entries[bot_key].is_legacy = True
            continue
        token_value = ""
        used_name = env_names[0]
        for name in env_names:
            token_value = os.getenv(name, "") or ""
            if token_value:
                used_name = name
                break
        token = Secret(token_value)
        entry = BotEntry(
            bot_key=bot_key, channel=channel, brands=brands,
            token=token, token_env=used_name, active=bool(token),
            environment="prod" if current_env() == "prod" else "dev",
            is_legacy=True,
        )
        if not token:
            await _tech_alert(
                f"token-missing:{bot_key}",
                f"⚠️ Реестр ботов: у легаси-бота {bot_key} не задана ни одна из "
                f"env-переменных {'/'.join(env_names)} — бот неактивен (R9)")
        entries[bot_key] = entry


async def _sync_snapshot(entries: Dict[str, BotEntry]) -> None:
    """Снапшот в БД (Д3): upsert реестровых полей ОДНОЙ транзакцией; секретный
    блок (intent-хэш/secret_enforced/confirmed_url) не трогается — его пишет
    только register_webhooks (R18). Отсутствующие в реестре строки
    деактивируются (не удаляются — секретный блок сохраняется)."""
    now = datetime.now(timezone.utc)
    async with database.AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(database.BotRegistrySnapshot))).scalars().all()
        by_key = {r.bot_key: r for r in rows}
        for entry in entries.values():
            row = by_key.get(entry.bot_key)
            if row is None:
                row = database.BotRegistrySnapshot(bot_key=entry.bot_key)
                session.add(row)
            row.channel = entry.channel
            row.brands = ",".join(sorted(entry.brands))
            row.token_env = entry.token_env
            row.environment = entry.environment
            row.active = entry.active
            row.is_legacy = entry.is_legacy
            row.link = entry.link
            row.webhook_base = entry.webhook_base
            row.updated_at = now
            # секретный блок -> обратно в entry (источник истины — БД, Фаза 0.3)
            entry.secret_enforced = bool(row.secret_enforced)
            entry.confirmed_url = row.confirmed_url or ""
        for bot_key, row in by_key.items():
            if bot_key not in entries and row.active:
                row.active = False
                row.updated_at = now
        await session.commit()


async def _entries_from_snapshot() -> Dict[str, BotEntry]:
    """Реестр из последнего успешного снапшота (NocoDB недоступен, R13).
    Токены всегда живые — из env по сохранённому имени переменной."""
    async with database.AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(database.BotRegistrySnapshot))).scalars().all()
    entries: Dict[str, BotEntry] = {}
    for row in rows:
        token = _resolve_token(row.token_env)
        entries[row.bot_key] = BotEntry(
            bot_key=row.bot_key, channel=row.channel,
            brands=parse_brands(row.brands), token=token,
            token_env=row.token_env,
            active=bool(row.active) and bool(token),
            environment=row.environment or "all",
            is_legacy=bool(row.is_legacy),
            secret_enforced=bool(row.secret_enforced),
            confirmed_url=row.confirmed_url or "",
            link=getattr(row, "link", "") or "",
            webhook_base=(getattr(row, "webhook_base", "") or "").rstrip("/"),
        )
    return entries


def _install(entries: Dict[str, BotEntry]) -> None:
    """Атомарная подмена реестра и сброс клиентов на новые токены."""
    CACHED_BOTS.clear()
    CACHED_BOTS.update(entries)
    _CLIENTS.clear()


async def load_bots_registry() -> bool:
    """Загрузка реестра по цепочке R13. True — из NocoDB, False — фолбэк."""
    global _source
    env = current_env()
    if env is None:
        await _tech_alert(
            "bot-env-missing",
            "⚠️ BOT_ENV не задан (или невалиден) — реестр ботов загружен только с Environment=all")
    rows = None
    fetch = _fetch_rows or _default_fetch_rows
    try:
        rows = await fetch()
    except Exception as e:
        logger.error("bot_registry: NocoDB недоступен (%s: %s) — фолбэк на снапшот",
                     type(e).__name__, e)
    if rows is not None:
        entries = await _entries_from_rows(rows, env)
        await _ensure_legacy(entries)
        try:
            await _sync_snapshot(entries)
        except Exception:
            logger.exception("bot_registry: снапшот не записан — реестр в памяти актуален")
        _install(entries)
        _source = "nocodb"
        logger.info("✅ [bot_registry] Loaded %d bots from NocoDB (env=%s)",
                    len(entries), env or "—")
        return True
    entries = await _entries_from_snapshot()
    if not entries:
        await _ensure_legacy(entries)
        _source = "env"
        logger.warning("bot_registry: ни NocoDB, ни снапшота — env-минимум из %d легаси-ботов",
                       len(entries))
    else:
        await _ensure_legacy(entries)
        _source = "snapshot"
        logger.warning("bot_registry: реестр поднят из снапшота БД (%d ботов)", len(entries))
    _install(entries)
    return False


def get_bot_entry(bot_key: str) -> Optional[BotEntry]:
    return CACHED_BOTS.get(resolve_alias(bot_key))


def find_brand_bot(channel: str, brand: str):
    """(client, bot_key) активного бота канала, обслуживающего бренд
    ('*' — универсал; правило дилер-алертов 2026-09-23). Детерминизм —
    сортировка по bot_key. (None, None) — бота бренда нет: фолбэк на чужой
    бренд ЗАПРЕЩЁН, вызывающий код алертит админам."""
    brand = (brand or "").strip().lower()
    if not brand:
        return None, None
    for entry in sorted(CACHED_BOTS.values(), key=lambda e: e.bot_key):
        if (entry.channel == channel and entry.active and entry.has_token
                and (brand in entry.brands or "*" in entry.brands)):
            return get_bot_client(entry.bot_key), entry.bot_key
    return None, None


def get_bot_client(bot_key: str):
    """Клиент-отправитель бота из реестра; None — бот неизвестен/неактивен/без
    токена (R14: вызывающий код не шлёт и алертит, фолбэка на чужого бота нет).
    Исторические имена max-main/tg-main резолвятся в легаси-ключ окружения."""
    bot_key = resolve_alias(bot_key)
    entry = CACHED_BOTS.get(bot_key)
    if entry is None or not entry.active or not entry.has_token:
        return None
    client = _CLIENTS.get(bot_key)
    if client is None:
        token = entry.token.reveal()
        if entry.channel == "max":
            client = MaxBotClient(token=token)
        elif entry.channel == "telegram":
            client = TelegramBotClient(token=token)
        else:
            return None
        _CLIENTS[bot_key] = client
    return client


async def check_max_subscriptions() -> None:
    """Read-only сторож подписок MAX (KTD-9): сверяет GET /subscriptions с
    подтверждённым URL. Только боты с непустым confirmed_url — до U7 записей
    нет и сторож молчит (Фаза 0.10). Починка — руками через --apply."""
    base = os.getenv("MAX_API_URL", "https://platform-api.max.ru")
    for entry in list(CACHED_BOTS.values()):
        if (entry.channel != "max" or not entry.active
                or not entry.has_token or not entry.confirmed_url):
            continue
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                        f"{base}/subscriptions",
                        headers={"Authorization": entry.token.reveal()},
                        timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    data = await resp.json()
        except Exception as e:
            logger.warning("bot_registry: сторож подписок %s — API недоступен (%s)",
                           entry.bot_key, e)
            continue
        urls = [s.get("url", "") for s in data.get("subscriptions", [])]
        if entry.confirmed_url not in urls:
            await _tech_alert(
                f"subscription-drift:{entry.bot_key}",
                f"🆘 Подписка MAX-бота {entry.bot_key} не совпадает с подтверждённой "
                f"({entry.confirmed_url}) — вероятна автоотписка (8ч). "
                f"Проверьте и почините: register_webhooks.py --check / --apply")


async def registry_worker(interval: int = 600):
    """Фоновое обновление реестра + сторож подписок (образец dealers_watchdog).
    Первый цикл — сразу: старт не блокируется, пустой реестр добирается."""
    logger.info("⏱ Bots Registry Worker started.")
    while True:
        try:
            await load_bots_registry()
            await check_max_subscriptions()
        except asyncio.CancelledError:
            logger.info("🛑 Bots Registry Worker cancelled.")
            break
        except Exception:
            logger.exception("bot_registry: цикл воркера упал — повтор через интервал")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
