#!/usr/bin/env python
"""U7 (план 2026-09-15-001, KTD-9): идемпотентная регистрация вебхуков ботов
реестра — замена activate_webhook.py.

Принципы:
- Токены не принимаются аргументами и не печатаются (env — единственный
  источник, learning token-idempotency: у скрипта нет собственных дефолтов —
  конфиг читается тем же bot_registry, что и приложение).
- Идемпотентность: URL — по фактическому состоянию (getWebhookInfo /
  GET /subscriptions), секрет — по intent-состоянию (снаружи ненаблюдаем):
  в снапшот-таблице хранится sha256 производного секрета; изменяющий вызов
  выполняется всегда, когда ожидаемый секрет не подтверждён.
- Порядок MAX: сначала POST новой подписки, затем DELETE лишних — частичный
  отказ не оставляет бота без подписок; любой отказ — ненулевой exit-код и
  строка «состояние бота X неконсистентно, повторите --apply».
- Включение проверки секрета — ОТДЕЛЬНЫЙ явный шаг --enforce после живой
  проверки (двухшаговый порядок R18); скрипт — единственный писатель
  секретных колонок снапшота.
- --apply требует --confirm-env=<хост PUBLIC_BASE_URL> (защита от запуска
  не в том окружении, Фаза 0.8); окружение печатается всегда.

Запуск владельцем по одной команде (однострочно, без heredoc):
  python scripts/register_webhooks.py --check
  python scripts/register_webhooks.py --apply --confirm-env=<хост>
  python scripts/register_webhooks.py --enforce <bot_key>
  python scripts/register_webhooks.py --deregister <bot_key> --confirm-env=<хост>
"""
import argparse
import asyncio
import hashlib
import os
import pathlib
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Локальный дев: env из .env репозитория (как в main.py); в контейнере — no-op.
# ДО импорта database: DATABASE_URL читается при импорте модуля.
from dotenv import load_dotenv
load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")

import aiohttp
from sqlalchemy import select

import bot_identity
import bot_registry
import database

# Паритет с исторической подпиской (activate_webhook.py:21)
UPDATE_TYPES = ["message_created", "bot_started", "message_callback"]

WEBHOOK_PATH = {"max": "max_webhook", "telegram": "tg_webhook"}


def intent_hash(secret: str) -> str:
    """Необратимый отпечаток производного секрета для снапшот-таблицы (R8)."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def expected_url(base_url: str, entry) -> str:
    """Целевой URL вебхука бота. U9a: у бота может быть своя база
    (Webhook_Base в Bots — прокси/Cloudflare Worker обхода блокировки TG,
    сохраняющий путь и заголовки); пусто — PUBLIC_BASE_URL."""
    base = (getattr(entry, "webhook_base", "") or "").strip() or base_url
    return f"{base.rstrip('/')}/{WEBHOOK_PATH[entry.channel]}/{entry.bot_key}"


class RealHttp:
    """Тонкий транспорт (в тестах подменяется скриптуемым фейком)."""

    async def request(self, method: str, url: str, headers=None, json_body=None):
        async with aiohttp.ClientSession() as session:
            async with session.request(
                    method, url, headers=headers, json=json_body,
                    timeout=aiohttp.ClientTimeout(total=25)) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    data = {}
                return resp.status, data


# --- снапшот-таблица: блок подтверждённой регистрации (пишет только этот скрипт) ---

async def read_confirmation(bot_key: str):
    async with database.AsyncSessionLocal() as session:
        row = (await session.execute(
            select(database.BotRegistrySnapshot)
            .where(database.BotRegistrySnapshot.bot_key == bot_key)
        )).scalar_one_or_none()
    if row is None:
        return {"intent_hash": None, "confirmed_url": "", "enforced": False}
    return {"intent_hash": row.secret_intent_hash,
            "confirmed_url": row.confirmed_url or "",
            "enforced": bool(row.secret_enforced)}


async def write_confirmation(bot_key: str, secret: str, url: str):
    async with database.AsyncSessionLocal() as session:
        row = (await session.execute(
            select(database.BotRegistrySnapshot)
            .where(database.BotRegistrySnapshot.bot_key == bot_key)
        )).scalar_one_or_none()
        if row is None:
            row = database.BotRegistrySnapshot(
                bot_key=bot_key, channel="", brands="", token_env="")
            session.add(row)
        row.secret_intent_hash = intent_hash(secret)
        row.confirmed_url = url
        row.confirmed_at = datetime.now(timezone.utc)
        await session.commit()


async def clear_confirmation(bot_key: str) -> None:
    """U9 --deregister: очистка секретного блока снапшота — после снятия
    подписок подтверждённой регистрации больше нет, проверка выключается."""
    async with database.AsyncSessionLocal() as session:
        row = (await session.execute(
            select(database.BotRegistrySnapshot)
            .where(database.BotRegistrySnapshot.bot_key == bot_key)
        )).scalar_one_or_none()
        if row is not None:
            row.secret_intent_hash = None
            row.confirmed_url = ""
            row.confirmed_at = None
            row.secret_enforced = False
            await session.commit()


async def set_enforced(bot_key: str, value: bool) -> str:
    """--enforce/--unenforce: гейт R18. Включение — только при подтверждённой
    регистрации, отпечаток которой воспроизводим ТЕКУЩИМ токеном."""
    entry = bot_registry.get_bot_entry(bot_key)
    if entry is None:
        return f"ОТКАЗ: бот {bot_key} не найден в реестре"
    conf = await read_confirmation(bot_key)
    if value:
        if not conf["confirmed_url"] or not conf["intent_hash"]:
            return (f"ОТКАЗ: у {bot_key} нет подтверждённой регистрации — "
                    f"сначала --apply, затем живая проверка (R18)")
        if not entry.has_token or intent_hash(entry.webhook_secret()) != conf["intent_hash"]:
            return (f"ОТКАЗ: подтверждённый секрет {bot_key} не совпадает с производным от "
                    f"текущего токена — повторите --apply (смена токена?)")
    async with database.AsyncSessionLocal() as session:
        row = (await session.execute(
            select(database.BotRegistrySnapshot)
            .where(database.BotRegistrySnapshot.bot_key == bot_key)
        )).scalar_one_or_none()
        if row is None:
            return f"ОТКАЗ: нет строки снапшота для {bot_key}"
        row.secret_enforced = value
        await session.commit()
    return (f"OK: проверка секрета {bot_key} "
            f"{'ВКЛЮЧЕНА' if value else 'выключена'}")


# --- фактическое состояние платформ ---

async def fetch_tg_url(http, token: str) -> str:
    status, data = await http.request(
        "GET", f"https://api.telegram.org/bot{token}/getWebhookInfo")
    if status != 200 or not data.get("ok"):
        raise RuntimeError(f"getWebhookInfo HTTP {status}")
    return (data.get("result") or {}).get("url", "") or ""


async def fetch_max_urls(http, token: str) -> list:
    base = os.getenv("MAX_API_URL", "https://platform-api.max.ru")
    status, data = await http.request(
        "GET", f"{base}/subscriptions", headers={"Authorization": token})
    if status != 200:
        raise RuntimeError(f"GET /subscriptions HTTP {status}")
    return [s.get("url", "") for s in data.get("subscriptions", [])]


# --- план и применение по одному боту ---

async def inspect_bot(http, entry, base_url: str) -> dict:
    """Состояние бота: фактический URL(ы), подтверждённость секрета, действия."""
    want_url = expected_url(base_url, entry)
    conf = await read_confirmation(entry.bot_key)
    secret_confirmed = (entry.has_token and conf["intent_hash"] is not None
                        and conf["intent_hash"] == intent_hash(entry.webhook_secret())
                        and conf["confirmed_url"] == want_url)
    state = {"bot_key": entry.bot_key, "channel": entry.channel,
             "want_url": want_url, "secret_confirmed": secret_confirmed,
             "enforced": conf["enforced"], "actions": [], "error": None}
    try:
        if entry.channel == "telegram":
            actual = await fetch_tg_url(http, entry.token.reveal())
            state["actual"] = actual or "(не зарегистрирован)"
            if actual != want_url or not secret_confirmed:
                state["actions"].append("setWebhook (URL+секрет)")
        else:
            urls = await fetch_max_urls(http, entry.token.reveal())
            state["actual"] = ", ".join(urls) or "(подписок нет)"
            if want_url not in urls or not secret_confirmed:
                state["actions"].append("POST новой подписки (URL+секрет)")
            stale = [u for u in urls if u != want_url]
            if stale:
                state["actions"].append(f"DELETE лишних подписок: {len(stale)}")
    except Exception as e:
        state["error"] = f"{type(e).__name__}: {e}"
        state["actual"] = "(недоступно)"
    return state


async def apply_bot(http, entry, base_url: str, secret_confirmed: bool) -> bool:
    """Применить состояние. True — бот консистентен, False — повторите --apply."""
    want_url = expected_url(base_url, entry)
    secret = entry.webhook_secret()
    token = entry.token.reveal()
    if entry.channel == "telegram":
        status, data = await http.request(
            "POST", f"https://api.telegram.org/bot{token}/setWebhook",
            json_body={"url": want_url, "secret_token": secret})
        if status != 200 or not data.get("ok"):
            print(f"  ❌ {entry.bot_key}: setWebhook HTTP {status} — "
                  f"состояние бота {entry.bot_key} неконсистентно, повторите --apply")
            return False
        await write_confirmation(entry.bot_key, secret, want_url)
        print(f"  ✅ {entry.bot_key}: setWebhook применён, секрет подтверждён")
        return True
    # MAX. Базовый порядок — POST новой подписки, затем DELETE лишних
    # (частичный отказ не оставляет бота без подписок). Исключение: ожидаемый
    # URL уже подписан, но секрет не подтверждён — повторный POST задвоил бы
    # подписку на тот же URL (дубли событий, POST накапливает — И2), а DELETE
    # по URL снял бы обе; поэтому здесь сначала DELETE этого URL, затем POST
    # (окно — секунды, отказ чинится повторным --apply и виден по exit-коду).
    base = os.getenv("MAX_API_URL", "https://platform-api.max.ru")
    urls = await fetch_max_urls(http, token)
    if want_url in urls and not secret_confirmed:
        status, _ = await http.request(
            "DELETE", f"{base}/subscriptions?url={want_url}",
            headers={"Authorization": token})
        if status != 200:
            print(f"  ❌ {entry.bot_key}: DELETE перед пере-подпиской HTTP {status} — "
                  f"состояние бота {entry.bot_key} неконсистентно, повторите --apply")
            return False
        urls = [u for u in urls if u != want_url]
    if want_url not in urls:
        status, _ = await http.request(
            "POST", f"{base}/subscriptions", headers={"Authorization": token},
            json_body={"url": want_url, "secret": secret, "update_types": UPDATE_TYPES})
        if status != 200:
            print(f"  ❌ {entry.bot_key}: POST /subscriptions HTTP {status} — "
                  f"состояние бота {entry.bot_key} неконсистентно, повторите --apply")
            return False
        await write_confirmation(entry.bot_key, secret, want_url)
    consistent = True
    for stale in [u for u in urls if u and u != want_url]:
        status, _ = await http.request(
            "DELETE", f"{base}/subscriptions?url={stale}",
            headers={"Authorization": token})
        if status != 200:
            print(f"  ⚠️ {entry.bot_key}: DELETE {stale!r} HTTP {status} — "
                  f"состояние бота {entry.bot_key} неконсистентно, повторите --apply")
            consistent = False
    if consistent:
        print(f"  ✅ {entry.bot_key}: подписка применена, секрет подтверждён")
    return consistent


async def deregister_bot(http, entry) -> bool:
    """U9: полное снятие вебхуков бота. TG — deleteWebhook; MAX — DELETE ВСЕХ
    подписок токена (у токена нет «частичного» выключения по нашему URL —
    снимается всё, о чём скрипт честно пишет). После успеха секретный блок
    снапшота очищается."""
    token = entry.token.reveal()
    if entry.channel == "telegram":
        status, data = await http.request(
            "POST", f"https://api.telegram.org/bot{token}/deleteWebhook")
        if status != 200 or not data.get("ok"):
            print(f"  ❌ {entry.bot_key}: deleteWebhook HTTP {status} — подписка НЕ снята")
            return False
    else:
        base = os.getenv("MAX_API_URL", "https://platform-api.max.ru")
        urls = [u for u in await fetch_max_urls(http, token) if u]
        ok = True
        for u in urls:
            status, _ = await http.request(
                "DELETE", f"{base}/subscriptions?url={u}",
                headers={"Authorization": token})
            if status != 200:
                print(f"  ❌ {entry.bot_key}: DELETE {u!r} HTTP {status} — "
                      f"подписка осталась, повторите --deregister")
                ok = False
        if not urls:
            print(f"  • {entry.bot_key}: активных подписок нет")
        if not ok:
            return False
    await clear_confirmation(entry.bot_key)
    print(f"  ✅ {entry.bot_key}: вебхуки сняты, секретный блок снапшота очищен")
    return True


def print_environment(base_url: str, bots) -> None:
    print("=== Окружение ===")
    print(f"BOT_ENV:         {os.getenv('BOT_ENV', '(не задан)')}")
    print(f"PUBLIC_BASE_URL: {base_url or '(НЕ ЗАДАН — обязателен)'}")
    print(f"MAX_API_URL:     {os.getenv('MAX_API_URL', 'https://platform-api.max.ru')}")
    db_url = os.getenv("DATABASE_URL", "sqlite (локальный файл)")
    print(f"БД:              {db_url.split('@')[-1] if '@' in db_url else db_url}")
    print(f"Боты реестра:    {', '.join(sorted(b.bot_key for b in bots)) or '—'}")


async def run(args, http=None) -> int:
    http = http or RealHttp()
    base_url = os.getenv("PUBLIC_BASE_URL", "").strip()
    await bot_registry.load_bots_registry()
    bots = [e for e in bot_registry.CACHED_BOTS.values()
            if e.channel in WEBHOOK_PATH]
    if args.bot:
        bots = [e for e in bots if e.bot_key == args.bot]
        if not bots:
            print(f"ОТКАЗ: бот {args.bot} не найден в реестре")
            return 1
    print_environment(base_url, bots)

    if args.enforce or args.unenforce:
        key = args.enforce or args.unenforce
        msg = await set_enforced(key, bool(args.enforce))
        print(msg)
        return 0 if msg.startswith("OK") else 1

    if not base_url:
        print("ОТКАЗ: PUBLIC_BASE_URL не задан")
        return 1

    if args.deregister:
        resolved = bot_registry.resolve_alias(args.deregister)
        if resolved != args.deregister:
            print(f"ОТКАЗ: {args.deregister} — историческое имя бота {resolved} "
                  f"(тот же токен). Старые URL снимаются перерегистрацией "
                  f"(--apply); полное отключение — --deregister {resolved}")
            return 1
        entry = bot_registry.get_bot_entry(resolved)
        if entry is None or entry.channel not in WEBHOOK_PATH:
            print(f"ОТКАЗ: бот {args.deregister} не найден в реестре")
            return 1
        if not entry.has_token:
            print(f"ОТКАЗ: env-переменная {entry.token_env} не задана")
            return 1
        want_host = urlsplit(base_url).hostname or ""
        if (args.confirm_env or "") != want_host:
            print(f"ОТКАЗ: --deregister требует --confirm-env={want_host} "
                  f"(подтверждение окружения, Фаза 0.8)")
            return 1
        return 0 if await deregister_bot(http, entry) else 1

    if args.apply:
        want_host = urlsplit(base_url).hostname or ""
        if (args.confirm_env or "") != want_host:
            print(f"ОТКАЗ: --apply требует --confirm-env={want_host} "
                  f"(подтверждение окружения, Фаза 0.8)")
            return 1

    exit_code = 0
    print("=== Состояние ботов ===")
    for entry in sorted(bots, key=lambda e: e.bot_key):
        if not entry.has_token:
            print(f"  ⏭ {entry.bot_key}: env-переменная {entry.token_env} не задана — пропущен")
            continue
        state = await inspect_bot(http, entry, base_url)
        if state["error"]:
            print(f"  ❌ {entry.bot_key}: API недоступен ({state['error']})")
            exit_code = 1
            continue
        need = "; ".join(state["actions"]) or "расхождений нет"
        base_note = f" (база: {entry.webhook_base})" if getattr(entry, "webhook_base", "") else ""
        print(f"  • {entry.bot_key} [{entry.channel}]{base_note} факт: {state['actual']}")
        print(f"    ожидание: {state['want_url']}")
        print(f"    секрет: {'подтверждён' if state['secret_confirmed'] else 'НЕ подтверждён'}; "
              f"проверка: {'ВКЛЮЧЕНА' if state['enforced'] else 'выключена'}; "
              f"действия: {need}")
        if args.apply and state["actions"]:
            if not await apply_bot(http, entry, base_url, state["secret_confirmed"]):
                exit_code = 1
    if args.apply and exit_code == 0:
        print("Готово: расхождений не осталось (проверьте повторным --check).")
    return exit_code


def build_parser():
    p = argparse.ArgumentParser(description="Регистрация вебхуков ботов реестра (U7)")
    p.add_argument("--check", action="store_true",
                   help="только показать состояние (режим по умолчанию; флаг — "
                        "для явности в инструкциях)")
    p.add_argument("--apply", action="store_true",
                   help="применить изменения (по умолчанию — только показать)")
    p.add_argument("--confirm-env", default="",
                   help="хост PUBLIC_BASE_URL — обязательное подтверждение при --apply")
    p.add_argument("--bot", default="", help="ограничиться одним bot_key")
    p.add_argument("--enforce", default="",
                   help="включить проверку секрета для bot_key (после живой проверки)")
    p.add_argument("--unenforce", default="",
                   help="выключить проверку секрета для bot_key (откат)")
    p.add_argument("--deregister", default="",
                   help="снять ВСЕ вебхуки бота (bot_key) и очистить секретный "
                        "блок; требует --confirm-env")
    return p


async def _cli(argv=None) -> int:
    """CLI-обёртка: гарантированное закрытие пула БД при любом исходе.

    Дефект дев-прогона 2026-09-19: без dispose() процесс печатал «OK» и
    зависал навсегда — aiosqlite держит non-daemon поток на соединение, и
    интерпретатор ждал его в threading._shutdown (выход только по Ctrl+C).
    На PostgreSQL потоков нет, но закрытие пула обязательно и там."""
    try:
        return await run(build_parser().parse_args(argv))
    finally:
        await database.engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(_cli()))
