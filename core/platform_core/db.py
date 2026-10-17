"""Подключение к PostgreSQL (asyncpg). Пул создаётся лениво: если база ещё не поднялась,
сервис стартует, а /health отвечает 503, пока подключение не появится."""
from __future__ import annotations

import asyncio
import logging

import asyncpg

from .config import Settings

log = logging.getLogger("platform.db")


class Database:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

    async def _ensure_pool(self) -> asyncpg.Pool:
        async with self._lock:
            if self._pool is None:
                s = self._settings
                self._pool = await asyncpg.create_pool(
                    host=s.db_host,
                    port=s.db_port,
                    database=s.db_name,
                    user=s.db_user,
                    password=s.db_password,
                    min_size=1,
                    max_size=5,
                    timeout=5,
                )
            return self._pool

    async def ping(self) -> bool:
        try:
            pool = await self._ensure_pool()
            async with pool.acquire(timeout=3) as conn:
                return await conn.fetchval("SELECT 1", timeout=3) == 1
        except Exception as exc:  # причина — только тип ошибки, без адресов и ключей
            log.warning("db ping failed: %s", type(exc).__name__)
            return False

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
