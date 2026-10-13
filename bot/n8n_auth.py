"""Защита каналов бот ↔ n8n (план n8n 2026-10-03, «n8n-webhook-auth-bot-plan»).

Исходящие вызовы n8n (агент, shadow-резолвер, логи сессии) несут заголовок
X-N8N-Secret со значением N8N_WEBHOOK_SECRET; секрет не задан — заголовка нет
(поведение как раньше, на старте одно предупреждение).

Входящий /n8n_callback сверяет X-N8N-Callback-Secret с N8N_CALLBACK_SECRET.
Режим N8N_CALLBACK_AUTH_MODE:
  off    — без проверки и без строки в логе;
  soft   — запрос обрабатывается как раньше, в лог строка
           N8N_CALLBACK_AUTH result=ok|missing|bad deal=<id> ip=<remote>
           (режим по умолчанию: релиз выходит в soft);
  strict — ok обрабатывается, missing/bad → 403 до любых побочных эффектов.
strict без N8N_CALLBACK_SECRET — ошибка старта (validate_config), а не молча
открытый канал. Значения секретов в логи не попадают (log_masking).

Модуль — лист: только stdlib, без main.py и cabinet_service.py.
"""
import hmac
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

SECRET_HEADER = "X-N8N-Secret"
CALLBACK_HEADER = "X-N8N-Callback-Secret"
MODES = ("off", "soft", "strict")
DEFAULT_MODE = "soft"


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def n8n_headers(extra: Optional[dict] = None) -> dict:
    """Заголовки исходящего вызова n8n: X-N8N-Secret, если секрет задан."""
    headers = dict(extra or {})
    secret = _env("N8N_WEBHOOK_SECRET")
    if secret:
        headers[SECRET_HEADER] = secret
    return headers


def callback_auth_mode() -> str:
    mode = _env("N8N_CALLBACK_AUTH_MODE").lower() or DEFAULT_MODE
    return mode if mode in MODES else DEFAULT_MODE


def callback_auth_result(header_value: Optional[str]) -> str:
    """ok | missing | bad — сравнение за постоянное время."""
    if not header_value:
        return "missing"
    secret = _env("N8N_CALLBACK_SECRET")
    if secret and hmac.compare_digest(header_value.encode("utf-8"), secret.encode("utf-8")):
        return "ok"
    return "bad"


def validate_config() -> None:
    """Проверка на старте: strict без секрета — ошибка; нет исходящего секрета
    или неизвестный режим — предупреждение."""
    raw_mode = _env("N8N_CALLBACK_AUTH_MODE").lower()
    if raw_mode and raw_mode not in MODES:
        logger.warning("N8N_CALLBACK_AUTH_MODE=%r не из %s — работаю в %s",
                       raw_mode, MODES, DEFAULT_MODE)
    if callback_auth_mode() == "strict" and not _env("N8N_CALLBACK_SECRET"):
        raise RuntimeError("N8N_CALLBACK_AUTH_MODE=strict без N8N_CALLBACK_SECRET — "
                           "колбэк n8n был бы открыт; задайте секрет или режим soft")
    if not _env("N8N_WEBHOOK_SECRET"):
        logger.warning("N8N_WEBHOOK_SECRET не задан — вызовы n8n идут без заголовка %s",
                       SECRET_HEADER)
