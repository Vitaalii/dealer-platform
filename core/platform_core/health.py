"""Ответ /health. Чистая функция: тестируется без aiohttp и без базы."""
from __future__ import annotations

from .config import Settings


def health_payload(settings: Settings, db_ok: bool) -> tuple[int, dict[str, str]]:
    """200 и status=ok, если база отвечает; иначе 503. Ни адресов, ни ключей в ответе нет."""
    body = {
        "status": "ok" if db_ok else "error",
        "db": "ok" if db_ok else "error",
        "env": settings.env,
        "version": settings.version,
    }
    return (200 if db_ok else 503), body
