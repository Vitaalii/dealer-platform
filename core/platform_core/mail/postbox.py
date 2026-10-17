"""Postbox (Yandex Cloud): SendEmail в API, совместимом с AWS SES v2.

Запрос: POST {POSTBOX_ENDPOINT}/v2/email/outbound-emails, ответ 200 {"MessageId": …},
ошибка — JSON {"Code": …, "message": …}
(https://yandex.cloud/ru/docs/postbox/aws-compatible-api/api-ref/send-email).
Повторы: 429, 5xx, обрыв сети, тайм-аут — до MAX_ATTEMPTS попыток с растущей паузой.
Остальные ответы — MailPermanentError с кодом. Поле message ответа не сохраняем: в нём бывают адреса.
Скорость: не чаще MAIL_MAX_PER_SECOND запросов в секунду на процесс (квота — 1 письмо в секунду,
https://yandex.cloud/ru/docs/postbox/concepts/limits).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from email.utils import formataddr
from typing import Awaitable, Callable

from ..config import MailSettings
from . import Mailer, MailPermanentError, MailTemporaryError, SendResult, normalize_recipients
from .sigv4 import sign

log = logging.getLogger("platform.mail")

SEND_PATH = "/v2/email/outbound-emails"
SERVICE = "ses"
TIMEOUT_SECONDS = 10.0
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 1.0  # пауза перед повтором: 1, 2, 4 … с
JITTER_SECONDS = 0.5

# transport(url, headers, body) -> (HTTP-код, тело ответа). Сетевая ошибка — исключение TransportError.
Transport = Callable[[str, dict[str, str], bytes], Awaitable[tuple[int, bytes]]]


class TransportError(Exception):
    """Обрыв сети или тайм-аут: запрос можно повторить."""


class AiohttpTransport:
    """HTTP через aiohttp (закреплён в core/requirements.txt). Сессия создаётся при первой отправке."""

    def __init__(self, timeout: float = TIMEOUT_SECONDS) -> None:
        self.timeout = timeout
        self._session = None

    async def __call__(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        import aiohttp

        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
        try:
            async with self._session.post(url, data=body, headers=headers) as response:
                return response.status, await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise TransportError(type(exc).__name__) from None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


def _error_code(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "unknown"
    code = data.get("Code") if isinstance(data, dict) else None
    # Код — короткое имя вида MessageRejected; всё остальное не берём в текст ошибки.
    if isinstance(code, str) and code.replace(":", "").replace(" ", "").isalnum() and len(code) <= 80:
        return code
    return "unknown"


class PostboxMailer(Mailer):
    name = "postbox"

    def __init__(
        self,
        settings: MailSettings,
        transport: Transport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = lambda: random.uniform(0, JITTER_SECONDS),
    ) -> None:
        self.settings = settings
        self.transport = transport or AiohttpTransport()
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        self._interval = 1.0 / settings.max_per_second
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def sender(self) -> str:
        if self.settings.from_name:
            return formataddr((self.settings.from_name, self.settings.from_address), charset="utf-8")
        return self.settings.from_address

    def build_body(self, to: list[str], subject: str, text: str, html: str | None) -> bytes:
        content = {"Text": {"Data": text, "Charset": "UTF-8"}}
        if html is not None:
            content["Html"] = {"Data": html, "Charset": "UTF-8"}
        payload = {
            "FromEmailAddress": self.sender,
            "Destination": {"ToAddresses": to},
            "Content": {"Simple": {"Subject": {"Data": subject, "Charset": "UTF-8"}, "Body": content}},
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    async def _wait_rate(self) -> None:
        async with self._lock:
            now = self._clock()
            if now < self._next_at:
                await self._sleep(self._next_at - now)
                now = self._next_at
            self._next_at = now + self._interval

    async def _post(self, body: bytes) -> tuple[int, bytes]:
        await self._wait_rate()
        url = self.settings.postbox_endpoint + SEND_PATH
        headers = sign(
            "POST",
            url,
            {"Content-Type": "application/json"},
            body,
            key_id=self.settings.postbox_key_id,
            secret_key=self.settings.postbox_secret_key,
            region=self.settings.postbox_region,
            service=SERVICE,
        )
        return await self.transport(url, headers, body)

    async def send(self, to: list[str], subject: str, text: str, html: str | None = None) -> SendResult:
        recipients = normalize_recipients(to)
        body = self.build_body(recipients, subject, text, html)
        reason = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                await self._sleep(BACKOFF_SECONDS * 2 ** (attempt - 2) + self._jitter())
            try:
                status, response = await self._post(body)
            except TransportError as exc:
                reason = f"network {exc}"
            else:
                if 200 <= status < 300:
                    message_id = _message_id(response)
                    log.info("mail sent provider=postbox message_id=%s recipients=%d", message_id, len(recipients))
                    return SendResult(message_id=message_id, recipients=len(recipients))
                code = _error_code(response)
                if status != 429 and status < 500:
                    log.warning("mail rejected provider=postbox status=%d code=%s", status, code)
                    raise MailPermanentError(f"postbox status={status} code={code}")
                reason = f"status={status} code={code}"
            log.warning("mail attempt %d/%d failed provider=postbox %s", attempt, MAX_ATTEMPTS, reason)
        raise MailTemporaryError(f"postbox attempts={MAX_ATTEMPTS} last {reason}")

    async def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if close is not None:
            await close()


def _message_id(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "unknown"
    message_id = data.get("MessageId") if isinstance(data, dict) else None
    return message_id if isinstance(message_id, str) and message_id else "unknown"
