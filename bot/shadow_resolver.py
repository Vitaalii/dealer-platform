"""Shadow-вызов резолвера интентов (U9, R21).

Каждый ход дублируется fire-and-forget запросом в отдельный n8n-воркфлоу
«AI Resolver (shadow)», который классифицирует ход всеми интентами и пишет
лог в NocoDB. На основной путь не влияет: не ждём ответа, любые ошибки
глотаются, при отсутствии N8N_RESOLVER_SHADOW_URL — полностью выключен.
"""
import asyncio
import logging
import os

import aiohttp

import n8n_auth

logger = logging.getLogger(__name__)

SHADOW_URL_ENV = "N8N_RESOLVER_SHADOW_URL"
_SHADOW_TIMEOUT = aiohttp.ClientTimeout(total=8)
# было бы утечкой бюджета слать в shadow огромные поля основного payload
SHADOW_FIELDS = ("session_id", "user_id", "message", "recent_history", "session_context")


def build_shadow_payload(payload: dict) -> dict:
    """Компактный срез основного payload: без system_prompt и cached_json."""
    return {k: payload.get(k) for k in SHADOW_FIELDS}


def fire_shadow(payload: dict, url: str | None = None) -> bool:
    """Планирует отправку в фоне; True — если shadow включён и задача создана."""
    url = url or os.getenv(SHADOW_URL_ENV)
    if not url:
        return False
    asyncio.create_task(_send(url, build_shadow_payload(payload)))
    return True


async def _send(url: str, body: dict) -> None:
    try:
        async with aiohttp.ClientSession(timeout=_SHADOW_TIMEOUT) as session:
            await session.post(url, json=body, headers=n8n_auth.n8n_headers())
    except Exception as e:  # shadow никогда не влияет на прод
        logger.debug("shadow resolver недоступен: %s", e)
