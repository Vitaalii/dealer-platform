"""U1 (план 2026-09-15-001): bot_registry — загрузка реестра, фолбэк-цепочка R13,
фильтр окружений (Фаза 0.2), снапшот в БД (Д3), неприкосновенность секретного
блока (R18), боевая проводка клиентов, маскирование секретов."""
import pytest
from sqlalchemy import select

import bot_identity
import bot_registry
import database
import log_masking
from max_client import MaxBotClient
from telegram_client import TelegramBotClient


class AlertRecorder:
    """Подтверждающий доставку хук алертов (delivered=True)."""

    def __init__(self, delivered=True):
        self.calls = []
        self.delivered = delivered

    async def __call__(self, text):
        self.calls.append(text)
        return self.delivered


def rows_fetcher(rows):
    async def fetch():
        return rows
    return fetch


def failing_fetcher():
    async def fetch():
        raise RuntimeError("NocoDB down")
    return fetch


def row(bot_key, channel="max", brands="haval", token_env="BOT_TOKEN_X",
        environment="dev", active=True, **extra):
    r = {"Bot_Key": bot_key, "Channel": channel, "Brands": brands,
         "Token_Env": token_env, "Environment": environment, "Active": active}
    r.update(extra)
    return r


@pytest.fixture(autouse=True)
def registry_env(monkeypatch):
    """Чистое состояние реестра и предсказуемый env на каждый тест."""
    bot_registry.reset_for_tests()
    monkeypatch.setenv("BOT_ENV", "dev")
    monkeypatch.setenv("MAX_BOT_TOKEN", "legacy-max-token-0001")
    monkeypatch.setenv("TG_BOT_TOKEN", "legacy-tg-token-0002")
    # U9: новые имена читаются ПЕРВЫМИ — фиксируем те же значения, иначе
    # реальные *_TEST из дев-.env (load_dotenv при импорте) затеняют фикстуру
    monkeypatch.setenv("MAX_BOT_TOKEN_TEST", "legacy-max-token-0001")
    monkeypatch.setenv("TG_BOT_TOKEN_TEST", "legacy-tg-token-0002")
    monkeypatch.setenv("BOT_TOKEN_X", "brand-bot-token-0003")
    yield
    bot_registry.reset_for_tests()


@pytest.mark.asyncio
async def test_missing_env_var_deactivates_and_alerts_once(db):
    alerts = AlertRecorder()
    bot_registry.setup(alert=alerts, fetch_rows=rows_fetcher(
        [row("max-haval", token_env="BOT_TOKEN_ABSENT")]))
    await bot_registry.load_bots_registry()
    entry = bot_registry.get_bot_entry("max-haval")
    assert entry is not None and entry.active is False   # R9
    assert bot_registry.get_bot_client("max-haval") is None
    token_alerts = [a for a in alerts.calls if "BOT_TOKEN_ABSENT" in a]
    assert len(token_alerts) == 1
    # повторная загрузка в кулдауне — алерт не дублируется (дедуп)
    await bot_registry.load_bots_registry()
    token_alerts = [a for a in alerts.calls if "BOT_TOKEN_ABSENT" in a]
    assert len(token_alerts) == 1


@pytest.mark.asyncio
async def test_invalid_bot_key_deactivated_with_alert(db):
    alerts = AlertRecorder()
    bot_registry.setup(alert=alerts, fetch_rows=rows_fetcher(
        [row("Max_Tank"), row("max tank"), row("бот-1")]))
    await bot_registry.load_bots_registry()
    for bad in ("Max_Tank", "max tank", "бот-1"):
        entry = bot_registry.get_bot_entry(bad)
        assert entry is not None and entry.active is False
        assert bot_registry.get_bot_client(bad) is None
    assert sum("недопустимый bot_key" in a for a in alerts.calls) == 3


@pytest.mark.asyncio
async def test_brands_parsed_case_insensitive(db):
    bot_registry.setup(alert=AlertRecorder(), fetch_rows=rows_fetcher(
        [row("max-haval", brands="Geely, Belgee")]))
    await bot_registry.load_bots_registry()
    assert bot_registry.get_bot_entry("max-haval").brands == {"geely", "belgee"}


@pytest.mark.asyncio
async def test_fallback_to_snapshot_when_nocodb_down(db):
    # шаг 1: успешная загрузка пишет снапшот
    bot_registry.setup(alert=AlertRecorder(), fetch_rows=rows_fetcher(
        [row("max-haval")]))
    assert await bot_registry.load_bots_registry() is True
    # шаг 2: NocoDB падает — реестр поднимается из снапшота БД
    bot_registry.reset_for_tests()
    bot_registry.setup(alert=AlertRecorder(), fetch_rows=failing_fetcher())
    assert await bot_registry.load_bots_registry() is False
    entry = bot_registry.get_bot_entry("max-haval")
    assert entry is not None and entry.active and entry.brands == {"haval"}


@pytest.mark.asyncio
async def test_env_minimum_when_no_nocodb_and_no_snapshot(db):
    bot_registry.setup(alert=AlertRecorder(), fetch_rows=failing_fetcher())
    assert await bot_registry.load_bots_registry() is False
    keys = set(bot_registry.CACHED_BOTS)
    # U9: env-минимум — легаси-пара ОКРУЖЕНИЯ (BOT_ENV=dev в фикстуре)
    assert keys == {"max-test", "tg-test"}
    legacy = bot_registry.get_bot_entry("max-test")
    assert legacy.is_legacy and legacy.brands == {"*"}   # dev-боты «все марки»
    # историческое имя max-main работает алиасом (переходный период U9)
    assert bot_registry.get_bot_client("max-main") is not None


@pytest.mark.asyncio
async def test_snapshot_stores_no_tokens_or_secrets(db):
    bot_registry.setup(alert=AlertRecorder(), fetch_rows=rows_fetcher(
        [row("max-haval")]))
    await bot_registry.load_bots_registry()
    async with database.AsyncSessionLocal() as session:
        rows_db = (await session.execute(
            select(database.BotRegistrySnapshot))).scalars().all()
    assert rows_db
    secret = bot_identity.derive_webhook_secret("brand-bot-token-0003", "max-haval")
    for r in rows_db:
        dump = " ".join(str(getattr(r, c.name)) for c in r.__table__.columns)
        assert "brand-bot-token-0003" not in dump
        assert "legacy-max-token-0001" not in dump
        assert secret not in dump
        assert r.token_env  # хранится только ИМЯ переменной


@pytest.mark.asyncio
async def test_interrupted_snapshot_write_leaves_no_partial_state(db, monkeypatch):
    bot_registry.setup(alert=AlertRecorder(), fetch_rows=rows_fetcher(
        [row("max-haval")]))
    await bot_registry.load_bots_registry()

    from sqlalchemy.ext.asyncio import AsyncSession
    orig_commit = AsyncSession.commit
    state = {"boom": True}

    async def maybe_boom(self):
        if state["boom"]:
            raise RuntimeError("commit interrupted")
        return await orig_commit(self)

    # ВАЖНО: без monkeypatch.undo() — он снял бы и патчи фикстуры db
    monkeypatch.setattr(AsyncSession, "commit", maybe_boom)
    bot_registry.setup(fetch_rows=rows_fetcher(
        [row("max-haval"), row("tg-haval", channel="telegram")]))
    await bot_registry.load_bots_registry()  # снапшот падает, реестр в памяти жив
    state["boom"] = False
    async with database.AsyncSessionLocal() as session:
        keys = {r.bot_key for r in (await session.execute(
            select(database.BotRegistrySnapshot))).scalars().all()}
    assert "tg-haval" not in keys  # транзакция откатилась целиком, частичных строк нет


@pytest.mark.asyncio
async def test_environment_filter_prod_vs_dev(db, monkeypatch):
    rows = [row("max-haval", environment="dev"),
            row("max-brand", environment="prod", token_env="BOT_TOKEN_X"),
            row("max-both", environment="all", token_env="BOT_TOKEN_X")]
    bot_registry.setup(alert=AlertRecorder(), fetch_rows=rows_fetcher(rows))
    await bot_registry.load_bots_registry()          # BOT_ENV=dev (фикстура)
    assert "max-haval" in bot_registry.CACHED_BOTS
    assert "max-both" in bot_registry.CACHED_BOTS
    assert "max-brand" not in bot_registry.CACHED_BOTS

    monkeypatch.setenv("BOT_ENV", "prod")
    bot_registry.reset_for_tests()
    bot_registry.setup(alert=AlertRecorder(), fetch_rows=rows_fetcher(rows))
    await bot_registry.load_bots_registry()
    assert "max-brand" in bot_registry.CACHED_BOTS
    assert "max-both" in bot_registry.CACHED_BOTS
    assert "max-haval" not in bot_registry.CACHED_BOTS


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_env", [None, "production", "  ", "DEV-2"])
async def test_bot_env_unset_or_garbage_loads_only_all(db, monkeypatch, bad_env):
    if bad_env is None:
        monkeypatch.delenv("BOT_ENV", raising=False)
    else:
        monkeypatch.setenv("BOT_ENV", bad_env)
    alerts = AlertRecorder()
    bot_registry.setup(alert=alerts, fetch_rows=rows_fetcher(
        [row("max-haval", environment="dev"),
         row("max-both", environment="all", token_env="BOT_TOKEN_X")]))
    await bot_registry.load_bots_registry()
    assert "max-both" in bot_registry.CACHED_BOTS
    assert "max-haval" not in bot_registry.CACHED_BOTS   # только all
    env_alerts = [a for a in alerts.calls if "BOT_ENV" in a]
    assert len(env_alerts) == 1                          # ровно один техалерт


@pytest.mark.asyncio
async def test_resync_does_not_overwrite_secret_block(db):
    bot_registry.setup(alert=AlertRecorder(), fetch_rows=rows_fetcher(
        [row("max-haval")]))
    await bot_registry.load_bots_registry()
    # register_webhooks (U7) подтвердил регистрацию — пишем секретный блок
    async with database.AsyncSessionLocal() as session:
        snap = (await session.execute(
            select(database.BotRegistrySnapshot)
            .where(database.BotRegistrySnapshot.bot_key == "max-haval")
        )).scalar_one()
        snap.secret_intent_hash = "a" * 64
        snap.secret_enforced = True
        snap.confirmed_url = "https://example.test/max_webhook/max-haval"
        await session.commit()
    # повторная синхронизация реестра (смена брендов) блок не перетирает
    bot_registry.setup(fetch_rows=rows_fetcher([row("max-haval", brands="haval,tank")]))
    await bot_registry.load_bots_registry()
    async with database.AsyncSessionLocal() as session:
        snap = (await session.execute(
            select(database.BotRegistrySnapshot)
            .where(database.BotRegistrySnapshot.bot_key == "max-haval")
        )).scalar_one()
    assert snap.secret_intent_hash == "a" * 64
    assert snap.secret_enforced is True
    assert snap.confirmed_url == "https://example.test/max_webhook/max-haval"
    assert snap.brands == "haval,tank"
    # ... и блок читается обратно в entry (источник истины — БД, Фаза 0.3)
    entry = bot_registry.get_bot_entry("max-haval")
    assert entry.secret_enforced is True
    assert entry.confirmed_url.endswith("/max_webhook/max-haval")


@pytest.mark.asyncio
async def test_live_wiring_two_bots_per_channel(db, monkeypatch):
    """Боевая проводка (learning injected-callable-hidden-side-effect):
    реальные классы клиентов, без моков — по два бота на канал, у каждого
    свой токен."""
    monkeypatch.setenv("BOT_TOKEN_TG_HAVAL", "brand-tg-token-0004")
    bot_registry.setup(alert=AlertRecorder(), fetch_rows=rows_fetcher([
        row("max-haval", token_env="BOT_TOKEN_X"),
        row("tg-haval", channel="telegram", token_env="BOT_TOKEN_TG_HAVAL"),
    ]))
    await bot_registry.load_bots_registry()

    max_legacy = bot_registry.get_bot_client("max-main")
    max_brand = bot_registry.get_bot_client("max-haval")
    tg_legacy = bot_registry.get_bot_client("tg-main")
    tg_brand = bot_registry.get_bot_client("tg-haval")

    assert isinstance(max_legacy, MaxBotClient) and isinstance(max_brand, MaxBotClient)
    assert isinstance(tg_legacy, TelegramBotClient) and isinstance(tg_brand, TelegramBotClient)
    assert max_legacy is not max_brand
    assert max_legacy.token == "legacy-max-token-0001"
    assert max_brand.token == "brand-bot-token-0003"
    assert tg_legacy.BASE_URL != tg_brand.BASE_URL       # токен в URL клиента TG
    assert "brand-tg-token-0004" in tg_brand.BASE_URL
    # повторный запрос отдаёт тот же инстанс (кэш клиентов)
    assert bot_registry.get_bot_client("max-haval") is max_brand


@pytest.mark.asyncio
async def test_inactive_row_gives_no_client(db):
    bot_registry.setup(alert=AlertRecorder(), fetch_rows=rows_fetcher(
        [row("max-haval", active=False)]))
    await bot_registry.load_bots_registry()
    entry = bot_registry.get_bot_entry("max-haval")
    assert entry is not None and entry.active is False
    assert bot_registry.get_bot_client("max-haval") is None
    assert bot_registry.get_bot_client("max-unknown") is None


@pytest.mark.asyncio
async def test_webhook_base_survives_snapshot(db):
    """U9a: Webhook_Base читается из Bots (хвостовой '/' срезается) и
    переживает фолбэк на снапшот (R13) — регистрация чинится и без NocoDB."""
    worker = "https://worker.example.dev"
    bot_registry.setup(alert=AlertRecorder(), fetch_rows=rows_fetcher(
        [dict(row("max-haval"), **{"Webhook_Base": worker + "/"})]))
    await bot_registry.load_bots_registry()
    assert bot_registry.get_bot_entry("max-haval").webhook_base == worker
    bot_registry.reset_for_tests()
    bot_registry.setup(alert=AlertRecorder(), fetch_rows=failing_fetcher())
    await bot_registry.load_bots_registry()               # снапшот БД
    assert bot_registry.get_bot_entry("max-haval").webhook_base == worker


@pytest.mark.asyncio
async def test_legacy_row_in_nocodb_overrides_brands(db):
    # U9: легаси-ключ окружения (dev: max-test) — запись NocoDB главнее env-брендов
    bot_registry.setup(alert=AlertRecorder(), fetch_rows=rows_fetcher(
        [row("max-test", brands="geely,belgee,solaris",
             token_env="MAX_BOT_TOKEN", environment="all")]))
    await bot_registry.load_bots_registry()
    entry = bot_registry.get_bot_entry("max-test")
    assert entry.is_legacy is True
    assert entry.brands == {"geely", "belgee", "solaris"}


# --- маскирование (fix 8 ревью: производный секрет и заголовки платформ) ---

def test_masking_covers_derived_secret_kv_and_headers():
    secret = bot_identity.derive_webhook_secret("token-abc", "max-haval")
    masked_kv = log_masking.mask_sensitive(f"secret={secret}")
    assert secret not in masked_kv and secret[:6] in masked_kv

    for header in ("X-Telegram-Bot-Api-Secret-Token", "X-Max-Bot-Api-Secret"):
        masked = log_masking.mask_sensitive(f"{header}: {secret}")
        assert secret not in masked
        assert header in masked  # имя заголовка остаётся для диагностики


def test_bot_entry_token_never_leaks_via_repr():
    from sms_gateway import Secret
    entry = bot_registry.BotEntry(
        bot_key="max-haval", channel="max", brands=frozenset({"haval"}),
        token=Secret("brand-bot-token-0003"), token_env="BOT_TOKEN_X", active=True)
    assert "brand-bot-token-0003" not in repr(entry)
    assert "brand-bot-token-0003" not in str(entry)
