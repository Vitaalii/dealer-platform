#!/usr/bin/env python3
"""Ручная выдача ссылки на кабинет — тем же кодовым путём, что в проде.

Зовёт cabinet_service.prepare_cabinet_context() (включая заморозку снапшота
калькулятора), а не дублирует логику. Для ручного тестирования дев-стенда.

Примеры:
    # засеять тестовые данные и выдать ссылку
    venv/bin/python scripts/issue_cabinet_link.py \
        --user dev-client-1 --deal DEV-1 --dealer 01 \
        --website https://mytest.tilda.ws --seed

    # повторная выдача той же связки (R19: тот же URL, снапшот перезаморожен)
    venv/bin/python scripts/issue_cabinet_link.py \
        --user dev-client-1 --deal DEV-1 --dealer 01 \
        --website https://mytest.tilda.ws

БД — из DATABASE_URL (по умолчанию дев-SQLite бота). Боевая база недостижима:
PostgreSQL с не-localhost хостом отклоняется гардом до единого запроса.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Ключ HMAC обязан СОВПАДАТЬ с ключом процесса бота: тот же .env, тот же
# дев-дефолт cabinet_service (_hmac_key). Собственный ключ скрипта
# («dev-issue-script-hmac-key», до 2026-08-12) ломал идемпотентность выдачи:
# бот и скрипт не могли вывести URL токенов друг друга и взаимно отзывали
# их на каждой выдаче — клиент терял ссылку и подтверждённый номер (TOFU).
from dotenv import load_dotenv  # noqa: E402
load_dotenv()  # тот же источник переменных, что у бота (main.py)

import database                    # noqa: E402  (DATABASE_URL читается при импорте)
import cabinet_service             # noqa: E402
from database import CabinetProfile, User  # noqa: E402
from sqlalchemy import select      # noqa: E402

# ── Гард: только локальная база ──────────────────────────────────────────────
_url = urlsplit(database.DB_URL.replace("+asyncpg", "").replace("+aiosqlite", ""))
if _url.scheme.startswith("postgres") and (_url.hostname or "") not in (
        "localhost", "127.0.0.1", "::1"):
    sys.exit(f"ОТКАЗ: DATABASE_URL указывает на не-локальный PostgreSQL "
             f"({_url.hostname!r}) — скрипт для дев-баз, боевую не трогаем.")

# ── Правдоподобные дев-таблицы NocoDB (ставки/программа/каталог) ─────────────
# Форма строк — как в бою (см. tests/test_cabinet_calc_data.py); «Сonsumption»
# намеренно с кириллической «С» — реальное имя колонки NocoDB.
DEV_CATALOG = [
    # Идентификация позиции — пара (Model id, Complectation id): комплектационная
    # часть («11»…«16») уникальна только ВНУТРИ модели, между моделями повторяется
    {"Id": 7, "Model id": "02", "Complectation id": "11",
     "Complectation": "Comfort",
     "Model title": "LADA Vesta Comfort", "Engine": "1.6 л, 106 л.с.",
     "Transmission_ru": "Механика", "Drivetrain_ru": "Передний",
     "Acceleration": "12.1", "Сonsumption": "6.9",
     "Pics": [{"signedUrl": "https://picsum.photos/seed/vesta1/800/500"},
              {"signedUrl": "https://picsum.photos/seed/vesta2/800/500"}]},
    {"Id": 9, "Model id": "02", "Complectation id": "12",
     "Complectation": "Luxe",
     "Model title": "LADA Vesta Luxe", "Engine": "1.8 л, 122 л.с.",
     "Transmission_ru": "Вариатор", "Drivetrain_ru": "Передний",
     "Acceleration": "11.0", "Сonsumption": "7.2", "Pics": []},
    # Коллизия, как в бою (выгрузка дилера 99: id 11 → 7 строк разных
    # моделей): другая модель (Model id 05) с тем же комплектационным id —
    # поиск позиции обязан идти по паре, иначе стенд покажет чужую карточку
    {"Id": 21, "Model id": "05", "Complectation id": "11",
     "Complectation": "Comfort",
     "Model title": "LADA Iskra Comfort", "Engine": "1.2 л, 90 л.с.",
     "Transmission_ru": "Механика", "Drivetrain_ru": "Передний",
     "Acceleration": "13.5", "Сonsumption": "6.1", "Pics": []},
]
DEV_RATES = [
    {"Rate": "16,5", "Term_From": 12, "Term_To": 36,
     "Downpayment_From": 20, "Downpayment_To": 50},
    {"Rate": 18, "Term_From": 36, "Term_To": 84,
     "Downpayment_From": 30, "Downpayment_To": ""},  # открытая вилка «до 100%»
]
DEV_PROGRAM = [
    {"Model_Pattern": "02, 05", "Program_Name": "Дев-Стандарт",
     "Max_Term_Months": 84, "Min_Down_Payment": 15},
]
DEV_TABLES = {"dev_catalog": DEV_CATALOG, "dev_rates": DEV_RATES,
              "dev_program": DEV_PROGRAM}


def make_dev_cache(payment: int, item_id: str, item_name: str, price: int,
                   model_id: str) -> dict:
    """n8n_cache в форме ответа Car Search: выбранная комплектация + цены +
    эхо qualification_context с model_id (car_search_prepare.js:220) — из
    него заморозка берёт модельную часть пары идентификации.

    Пару (--model-id, --item-id) стоит брать РЕАЛЬНУЮ из каталога дилера в
    NocoDB (колонки «Model id» и «Complectation id»): /cabinet/api/car на
    стенде ищет позицию в живом каталоге, и выдуманная пара даст 404."""
    return {
        "user_request": {"selected_complectation_id": item_id, "payment": payment},
        "qualification_context": {"model_id": model_id},
        "complectations": [
            {"id": item_id, "name": item_name, "full_name": item_name,
             "price": {"MSRP": price + 110_000, "credit_price": price}},
            {"id": f"{item_id}-alt", "name": "Альтернатива",
             "full_name": f"{item_name} (альт.)",
             "price": {"MSRP": price + 310_000, "credit_price": price + 200_000}},
        ],
    }


async def dev_loader(table_id):
    return DEV_TABLES.get(table_id, [])


def wire(dealer_code: str, website: str):
    """Инъекции того же setup(), что в бою; сайд-эффекты — принтером в stdout."""
    dealers = {dealer_code: {
        "dealer_name": f"Дев-дилер {dealer_code}", "code": dealer_code,
        "website": website,
        "db_config": {"catalog_table_id": "dev_catalog",
                      "rates_table_id": "dev_rates",
                      "program_table_id": "dev_program"},
    }}

    async def fake_bitrix(method, params, user_id=None):
        print(f"  [bitrix-заглушка] {method}")
        return {}

    async def printer(*a, **kw):
        print(f"  [сайд-эффект] {a[:2]}")

    cabinet_service.setup(
        bitrix_request=fake_bitrix, enqueue_nodebounce=printer,
        run_conversion=printer, dealer_alert=printer, tech_alert=printer,
        dealers_provider=lambda: dealers,
        nocodb_table_loader=dev_loader, nocodb_base_url=None)


async def seed_user(platform_user_id: str, payment: int,
                    item_id: str, item_name: str, price: int,
                    model_id: str) -> User:
    async with database.AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(
            User.platform_user_id == platform_user_id))).scalar_one_or_none()
        if user is None:
            user = User(platform_user_id=platform_user_id, channel="max",
                        full_name="Дев Клиент")
            session.add(user)
        # Правдоподобный контекст: n8n_cache как после Car Search
        ctx = json.loads(user.context_json) if user.context_json else {}
        ctx["n8n_cache"] = make_dev_cache(payment, item_id, item_name, price,
                                          model_id)
        user.context_json = json.dumps(ctx, ensure_ascii=False)
        await session.commit()
        await session.refresh(user)
        return user


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--user", required=True, help="platform_user_id клиента")
    p.add_argument("--deal", required=True, help="deal_id сделки")
    p.add_argument("--dealer", required=True, help="код дилера (2 символа)")
    p.add_argument("--website", required=True,
                   help="Website_URL дилера — домен страницы Tilda (https://…)")
    p.add_argument("--scenario", default="credit",
                   choices=["credit", "tradein", "combo"])
    p.add_argument("--payment", type=int, default=35000,
                   help="желаемый платёж (target_payment снапшота)")
    p.add_argument("--seed", action="store_true",
                   help="засеять тестовые данные: пользователь + n8n_cache "
                        "с комплектацией и ценой (иначе калькулятор не покажется)")
    p.add_argument("--item-id", default="11",
                   help="Complectation id позиции — КОРОТКИЙ комплектационный id "
                        "(«11»…«16»; уникален только внутри модели). Позицию "
                        "однозначно задаёт ПАРА (--model-id, --item-id) — для "
                        "/cabinet/api/car на стенде бери реальную пару из "
                        "каталога дилера в NocoDB")
    p.add_argument("--model-id", default="02",
                   help="Model id модели (колонка «Model id» каталога) — "
                        "модельная часть пары идентификации позиции")
    p.add_argument("--item-name", default="LADA Vesta Comfort",
                   help="название позиции для снапшота")
    p.add_argument("--price", type=int, default=2_490_000,
                   help="кредитная цена позиции (credit_price)")
    args = p.parse_args()

    if os.getenv("ENVIRONMENT", "").strip().lower() == "production":
        print("ОТКАЗ: скрипт не предназначен для ENVIRONMENT=production.")
        return 2

    print(f"БД: {database.DB_URL}")
    await database.init_db()
    wire(args.dealer, args.website.rstrip("/"))

    if args.seed:
        user = await seed_user(args.user, args.payment,
                               args.item_id, args.item_name, args.price,
                               args.model_id)
        print(f"Засеяно: user id={user.id} ({args.user}), n8n_cache с позицией "
              f"{args.model_id}/{args.item_id} ({args.item_name}, "
              f"{args.price:,} ₽)".replace(",", " "))
        if args.model_id == "02" and args.item_id == "11":
            print("ВНИМАНИЕ: пара по умолчанию — дев-сеанс; /cabinet/api/car "
                  "на стенде ищет в ЖИВОМ каталоге дилера по паре "
                  "(Model id, Complectation id) и даст 404. Передай реальную "
                  "пару --model-id/--item-id из каталога дилера в NocoDB.")
    else:
        async with database.AsyncSessionLocal() as session:
            user = (await session.execute(select(User).where(
                User.platform_user_id == args.user))).scalar_one_or_none()
        if user is None:
            print(f"Пользователь {args.user!r} не найден — запусти с --seed.")
            return 1

    ctx = json.loads(user.context_json) if user.context_json else {}
    n8n_cache = ctx.get("n8n_cache")
    if not args.seed and n8n_cache:
        stored = (n8n_cache.get("complectations") or [{}])[0]
        if (stored.get("id") != args.item_id
                or (stored.get("price") or {}).get("credit_price") != args.price):
            print("ВНИМАНИЕ: без --seed снапшот замораживается из сохранённого "
                  f"n8n_cache (позиция {stored.get('id')!r}, "
                  f"{(stored.get('price') or {}).get('credit_price')} ₽) — "
                  "--item-id/--price применятся только с --seed.")

    # ═══ ПРОД-ПУТЬ: тот же вызов, что в _attach_cabinet_context (main.py) ═══
    # brand_model — из --item-name: фронт показывает в заголовке
    # profile.brand_model ПРИОРИТЕТНЕЕ снапшотного item_name (app.js,
    # currentCarName), захардкоженный литерал перебивал бы --item-name.
    result = await cabinet_service.prepare_cabinet_context(
        user.id, args.dealer, deal_id=args.deal, has_phone=False,
        scenario_type=args.scenario, payment=args.payment,
        brand_model=args.item_name, n8n_cache=n8n_cache)

    url = result.get("cabinet_url")
    if not url:
        print(f"Ссылка НЕ выдана (cabinet_status={result.get('cabinet_status')}) "
              f"— вероятно, у пользователя уже есть телефон в чате.")
        return 1

    # Итог заморозки — из профиля
    async with database.AsyncSessionLocal() as session:
        profile = (await session.execute(select(CabinetProfile).where(
            CabinetProfile.user_id == user.id))).scalar_one_or_none()
    snap = json.loads(profile.calc_snapshot) \
        if profile is not None and profile.calc_snapshot else None

    print("\n" + "=" * 72)
    print(f"URL:     {url}")
    print(f"Статус:  {result.get('cabinet_status')}")
    if snap:
        print(f"Снапшот: item={snap['item_id']} ({snap.get('item_name')}), "
              f"price={snap['price']}, target={snap.get('target_payment')}, "
              f"rates={len(snap.get('rates') or [])}, "
              f"program={bool(snap.get('program'))}, "
              f"frozen_at={profile.calc_frozen_at}")
    else:
        print("Снапшот: НЕТ — калькулятор не покажется "
              "(нет n8n_cache с выбранной комплектацией; запусти с --seed)")
    print("=" * 72)
    return 0


async def _run() -> int:
    try:
        return await main()
    finally:
        # aiosqlite держит не-демонические треды соединений — без dispose
        # процесс не завершается после последнего print
        await database.engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
