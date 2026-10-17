"""Отправка писем (ARCHITECTURE.md, 7.6): общий модуль для заявок, биллинга и кабинета.

Сервис отправки сменяемый: каждый — класс с методом send(), выбор — MAIL_PROVIDER (build_mailer).
Несколько получателей уходят одним письмом. Ошибки — MailPermanentError (повтор не поможет)
и MailTemporaryError (попытки кончились). Адреса, текст письма и ключи в лог и в текст ошибок не попадают.
Пакет не импортирует aiohttp при загрузке — тесты идут на стандартной библиотеке.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..config import EMAIL_RE, MailSettings

# Квота Postbox — не больше 200 получателей в одном письме
# (https://yandex.cloud/ru/docs/postbox/concepts/limits).
MAX_RECIPIENTS = 200


class MailError(Exception):
    """Ошибка отправки. Текст — только код и число, без адресов и текста письма."""


class MailPermanentError(MailError):
    """Повтор не поможет: неверные адреса, отказ сервиса, домен не подтверждён."""


class MailTemporaryError(MailError):
    """Временная ошибка (квота, сбой сервиса, сеть), все попытки исчерпаны."""


@dataclass(frozen=True)
class SendResult:
    message_id: str
    recipients: int


class Mailer:
    """Интерфейс сервиса отправки."""

    name = "base"

    async def send(self, to: list[str], subject: str, text: str, html: str | None = None) -> SendResult:
        raise NotImplementedError

    async def close(self) -> None:
        return None


def normalize_recipients(to: Iterable[str]) -> list[str]:
    """Пробелы, регистр, дубли; проверка вида x@y.z и числа получателей."""
    result: list[str] = []
    for index, raw in enumerate(to, start=1):
        address = str(raw).strip().lower()
        if not EMAIL_RE.match(address):
            raise MailPermanentError(f"invalid recipient #{index}")
        if address not in result:
            result.append(address)
    if not result:
        raise MailPermanentError("no recipients")
    if len(result) > MAX_RECIPIENTS:
        raise MailPermanentError(f"too many recipients: {len(result)} > {MAX_RECIPIENTS}")
    return result


def build_mailer(settings: MailSettings, **kwargs) -> Mailer:
    """Сервис отправки по настройке MAIL_PROVIDER. kwargs — для тестов (transport, sleep, clock)."""
    if settings.provider == "postbox":
        from .postbox import PostboxMailer

        return PostboxMailer(settings, **kwargs)
    if settings.provider == "log":
        from .log_mailer import LogMailer

        return LogMailer(settings)
    raise ValueError(f"unknown mail provider: {settings.provider}")
