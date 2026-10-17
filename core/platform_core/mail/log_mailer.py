"""Сервис-заглушка log: письмо не отправляет, в лог — только тема и число получателей.

Только для теста и разработки: при PLATFORM_ENV=prod настройка MAIL_PROVIDER=log запрещена (config.py).
"""
from __future__ import annotations

import logging
import uuid

from ..config import MailSettings
from . import Mailer, SendResult, normalize_recipients

log = logging.getLogger("platform.mail")


class LogMailer(Mailer):
    name = "log"

    def __init__(self, settings: MailSettings) -> None:
        self.settings = settings

    async def send(self, to: list[str], subject: str, text: str, html: str | None = None) -> SendResult:
        recipients = normalize_recipients(to)
        message_id = f"log-{uuid.uuid4()}"
        log.info("mail not sent (provider=log) subject=%r recipients=%d", subject, len(recipients))
        return SendResult(message_id=message_id, recipients=len(recipients))
