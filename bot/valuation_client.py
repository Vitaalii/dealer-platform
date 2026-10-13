"""Веб-вход (план 2026-09-30-001, U7): клиент вебхука оценки трейд-ина n8n.

Одна попытка, таймаут 60 с, секрет в заголовке X-Cabinet-Eval-Secret.
URL и секрет — env по окружению бота:
    BOT_ENV=dev  → N8N_CABINET_EVAL_URL_TEST / N8N_CABINET_EVAL_SECRET_TEST
    BOT_ENV=prod → N8N_CABINET_EVAL_URL_PROD / N8N_CABINET_EVAL_SECRET_PROD

Результат — ValuationResult со статусом кэша:
    success          SUCCESS и tradein_price_to > 0 (found_info может не быть)
    found_no_price   SUCCESS с found_info без цены (кадр 8d)
    not_found        NOT_FOUND / INVALID_DATA (кадр 8c)
    failed           IN_PROGRESS / NO_REGION / пустой SUCCESS (ответ Auto.ru,
                     кэшируется) и любые сбои на нашей стороне: 400, 401/403,
                     502 и другие 5xx, таймаут, сеть, битый ответ, нет
                     конфигурации (our_failure=True — в кэш не идёт, R65).

Подменный ответ для дев-стенда (R68): CABINET_EVAL_FAKE=1 действует ТОЛЬКО
при BOT_ENV=dev и ENVIRONMENT≠production — проверка на каждом вызове;
в любом другом окружении флаг игнорируется (техалерт на старте —
fake_flag_misconfigured). Статус по тестовому госномеру — FAKE_PLATES.
"""
import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

logger = logging.getLogger("cabinet")

TIMEOUT_SECONDS = 60
SECRET_HEADER = "X-Cabinet-Eval-Secret"

# Тестовые госномера подменного ответа → (HTTP-статус, тело ответа)
FAKE_FOUND_INFO = {"mark": "Kia", "model": "Rio", "year": 2019,
                   "steering_wheel": "LEFT", "photo": "//img.example.com/rio.jpg"}
FAKE_PLATES = {
    "А001АА77": (200, {"status": "SUCCESS", "tradein_price_from": 900000,
                       "tradein_price_to": 1000000, "found_info": FAKE_FOUND_INFO}),
    "А002АА77": (200, {"status": "SUCCESS", "tradein_price_from": 700000,
                       "tradein_price_to": 800000}),
    "А003АА77": (200, {"status": "SUCCESS", "found_info": FAKE_FOUND_INFO}),
    "А004АА77": (200, {"status": "NOT_FOUND"}),
    "А005АА77": (200, {"status": "INVALID_DATA"}),
    "А006АА77": (200, {"status": "IN_PROGRESS"}),
    "А007АА77": (200, {"status": "NO_REGION"}),
    "А008АА77": (502, {"status": "ERROR"}),
    "А009АА77": (400, {"status": "BAD_REQUEST"}),
    "А010АА77": (403, {"error": "forbidden"}),
}
FAKE_DEFAULT = FAKE_PLATES["А001АА77"]


@dataclass
class ValuationResult:
    status: str                       # success / found_no_price / not_found / failed
    reason: Optional[str] = None      # причина неуспеха (для 8e и строки R58)
    our_failure: bool = False         # сбой на нашей стороне — не кэшировать
    found_info: Optional[dict] = None
    price_from: Optional[int] = None
    price_to: Optional[int] = None
    raw_status: Optional[str] = field(default=None)


def _bot_env() -> str:
    return os.getenv("BOT_ENV", "").strip().lower()


def fake_enabled() -> bool:
    """Подменный ответ разрешён только на дев-стенде (закрыт по умолчанию)."""
    if os.getenv("CABINET_EVAL_FAKE", "").strip().lower() not in ("1", "true", "yes"):
        return False
    if os.getenv("ENVIRONMENT", "").strip().lower() == "production":
        return False
    return _bot_env() == "dev"


def fake_flag_misconfigured() -> bool:
    """Флаг подмены задан, но окружение не дев — для стартового техалерта."""
    flag = os.getenv("CABINET_EVAL_FAKE", "").strip().lower() in ("1", "true", "yes")
    return flag and not fake_enabled()


def _config():
    suffix = {"dev": "TEST", "prod": "PROD"}.get(_bot_env())
    if suffix is None:
        return None, None
    return (os.getenv(f"N8N_CABINET_EVAL_URL_{suffix}", "").strip() or None,
            os.getenv(f"N8N_CABINET_EVAL_SECRET_{suffix}", "").strip() or None)


def _int_or_none(value) -> Optional[int]:
    try:
        num = int(float(str(value).replace(" ", "")))
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def parse_response(http_status: int, body) -> ValuationResult:
    """HTTP-ответ вебхука → ValuationResult (таблица «статус → кадр»)."""
    if http_status in (401, 403):
        return ValuationResult("failed", "отказ по секрету вебхука", True)
    if http_status == 400:
        return ValuationResult("failed", "вебхук отклонил запрос (BAD_REQUEST)", True)
    if http_status >= 500:
        return ValuationResult("failed", f"сбой вебхука n8n (HTTP {http_status})", True)
    if http_status != 200 or not isinstance(body, dict):
        return ValuationResult("failed", f"неожиданный ответ вебхука (HTTP {http_status})", True)
    status = str(body.get("status") or "").upper()
    found_info = body.get("found_info") if isinstance(body.get("found_info"), dict) else None
    price_to = _int_or_none(body.get("tradein_price_to"))
    price_from = _int_or_none(body.get("tradein_price_from"))
    if status == "SUCCESS":
        if price_to:
            return ValuationResult("success", None, False, found_info, price_from, price_to, status)
        if found_info:
            return ValuationResult("found_no_price", "нет оценки по похожим сделкам",
                                   False, found_info, None, None, status)
        return ValuationResult("failed", "пустой ответ оценки", False, raw_status=status)
    if status in ("NOT_FOUND", "INVALID_DATA"):
        return ValuationResult("not_found", "автомобиль не найден по госномеру",
                               False, raw_status=status)
    if status == "IN_PROGRESS":
        return ValuationResult("failed", "отчёт Auto.ru ещё формируется", False, raw_status=status)
    if status == "NO_REGION":
        return ValuationResult("failed", "регион не поддерживается", False, raw_status=status)
    return ValuationResult("failed", f"неизвестный статус оценки {status or '—'}", True,
                           raw_status=status or None)


async def evaluate(plate: str, mileage: Optional[int], request_id: str) -> ValuationResult:
    """Один вызов вебхука оценки (или подменный ответ на дев-стенде)."""
    if fake_enabled():
        delay = float(os.getenv("CABINET_EVAL_FAKE_DELAY", "5") or 0)
        if delay > 0:
            await asyncio.sleep(delay)
        http_status, body = FAKE_PLATES.get(plate, FAKE_DEFAULT)
        logger.info("cabinet: подменный ответ оценки для %s → HTTP %s", request_id, http_status)
        return parse_response(http_status, dict(body))
    url, secret = _config()
    if not url or not secret:
        return ValuationResult("failed", "вебхук оценки не настроен (env)", True)
    payload = {"plate": plate, "mileage": mileage, "request_id": request_id}
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)) as session:
            async with session.post(url, json=payload,
                                    headers={SECRET_HEADER: secret}) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = None
                return parse_response(resp.status, body)
    except asyncio.TimeoutError:
        return ValuationResult("failed", f"таймаут вебхука ({TIMEOUT_SECONDS} с)", True)
    except aiohttp.ClientError as e:
        return ValuationResult("failed", f"сетевая ошибка вебхука: {type(e).__name__}", True)
