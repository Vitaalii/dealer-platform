"""Проверка почты владельцем: python -m platform_core.mail --to <адрес> [--to <адрес> …].

Отправляет одно тестовое письмо всем адресам сразу. Успех — строка `SENT message_id=… recipients=N`,
код 0. Ошибка — `MAIL ERROR: …` (без адресов и ключей), код 1. Настройки — те же, что у сервиса
(проверка ключей при старте, config.py).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from ..config import check_or_exit
from . import MailError, build_mailer

SUBJECT = "Проверка почты платформы дилеров"
TEXT = "Тестовое письмо платформы дилеров (окружение: {env}). Отвечать на него не нужно."


async def _send(mail_settings, to: list[str], env: str) -> int:
    mailer = build_mailer(mail_settings)
    try:
        result = await mailer.send(to, SUBJECT, TEXT.format(env=env))
    except MailError as exc:
        print(f"MAIL ERROR: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        await mailer.close()
    print(f"SENT message_id={result.message_id} recipients={result.recipients}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m platform_core.mail", description=__doc__.splitlines()[0])
    parser.add_argument("--to", action="append", required=True, help="адрес получателя; можно несколько раз")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = check_or_exit(os.environ)
    return asyncio.run(_send(settings.mail, args.to, settings.env))


if __name__ == "__main__":
    raise SystemExit(main())
