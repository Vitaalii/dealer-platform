"""Сериализация ходов диалога per-user (U5, R29).

Один round-trip к n8n на пользователя за раз: ход, начавший обработку,
не отменяется новым сообщением клиента — новое сообщение остаётся в
USER_MESSAGE_BUFFERS и уходит следующим ходом после освобождения лока.
Локи разных пользователей независимы.
"""
import asyncio
from collections import defaultdict

_TURN_LOCKS = defaultdict(asyncio.Lock)


def get_turn_lock(user_id: str) -> asyncio.Lock:
    return _TURN_LOCKS[user_id]
