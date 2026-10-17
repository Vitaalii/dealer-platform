"""Настройки сервиса платформы — только из переменных окружения.

Проверка ключей при старте (docs/architecture/ARCHITECTURE.md, 7.4): если обязательной
переменной нет, в лог уходит только её имя (без значения), процесс завершается с кодом 1.
Модуль без внешних зависимостей — его тесты идут на стандартной библиотеке.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

REQUIRED_VARS: tuple[str, ...] = (
    "PLATFORM_ENV",
    "PLATFORM_DB_HOST",
    "PLATFORM_DB_NAME",
    "PLATFORM_DB_USER",
    "PLATFORM_DB_PASSWORD",
)
ALLOWED_ENVS: tuple[str, ...] = ("test", "prod")
# Почта (ARCHITECTURE.md, 7.6): сервис отправки сменяемый, выбирается MAIL_PROVIDER.
MAIL_REQUIRED_VARS: tuple[str, ...] = ("MAIL_PROVIDER", "MAIL_FROM")
POSTBOX_REQUIRED_VARS: tuple[str, ...] = ("POSTBOX_KEY_ID", "POSTBOX_SECRET_KEY")
MAIL_PROVIDERS: tuple[str, ...] = ("postbox", "log")
POSTBOX_DEFAULT_ENDPOINT = "https://postbox.cloud.yandex.net"
POSTBOX_DEFAULT_REGION = "ru-central1"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ConfigError(Exception):
    """Ошибка настроек. Текст содержит только имена переменных, не значения."""


@dataclass(frozen=True)
class MailSettings:
    provider: str
    from_address: str
    from_name: str = ""
    postbox_key_id: str = ""
    postbox_secret_key: str = field(default="", repr=False)
    postbox_endpoint: str = POSTBOX_DEFAULT_ENDPOINT
    postbox_region: str = POSTBOX_DEFAULT_REGION
    max_per_second: float = 1.0


@dataclass(frozen=True)
class Settings:
    env: str
    db_host: str
    db_name: str
    db_user: str
    db_password: str = field(repr=False)
    db_port: int = 5432
    http_port: int = 8000
    version: str = "dev"
    mail: MailSettings | None = None


def missing_vars(environ: Mapping[str, str], required: Sequence[str]) -> list[str]:
    """Имена обязательных переменных, которых нет или которые пустые."""
    return [name for name in required if not environ.get(name, "").strip()]


def _int_var(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"invalid {name}: not a number") from None
    if not 0 < value < 65536:
        raise ConfigError(f"invalid {name}: out of range")
    return value


def _rate_var(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"invalid {name}: not a number") from None
    if not 0 < value <= 1000:
        raise ConfigError(f"invalid {name}: out of range")
    return value


def load_mail_settings(environ: Mapping[str, str], env: str) -> MailSettings:
    missing = missing_vars(environ, MAIL_REQUIRED_VARS)
    provider = environ.get("MAIL_PROVIDER", "").strip()
    if provider == "postbox":
        missing += missing_vars(environ, POSTBOX_REQUIRED_VARS)
    if missing:
        raise ConfigError("missing " + ", ".join(missing))
    if provider not in MAIL_PROVIDERS:
        raise ConfigError("invalid MAIL_PROVIDER: expected one of " + "/".join(MAIL_PROVIDERS))
    if provider == "log" and env == "prod":
        raise ConfigError("invalid MAIL_PROVIDER: log is not allowed when PLATFORM_ENV=prod")
    from_address = environ["MAIL_FROM"].strip()
    if not EMAIL_RE.match(from_address):
        raise ConfigError("invalid MAIL_FROM: not an email address")
    return MailSettings(
        provider=provider,
        from_address=from_address,
        from_name=environ.get("MAIL_FROM_NAME", "").strip(),
        postbox_key_id=environ.get("POSTBOX_KEY_ID", "").strip(),
        postbox_secret_key=environ.get("POSTBOX_SECRET_KEY", "").strip(),
        postbox_endpoint=environ.get("POSTBOX_ENDPOINT", "").strip().rstrip("/") or POSTBOX_DEFAULT_ENDPOINT,
        postbox_region=environ.get("POSTBOX_REGION", "").strip() or POSTBOX_DEFAULT_REGION,
        max_per_second=_rate_var(environ, "MAIL_MAX_PER_SECOND", 1.0),
    )


def load_settings(environ: Mapping[str, str]) -> Settings:
    missing = missing_vars(environ, REQUIRED_VARS + MAIL_REQUIRED_VARS)
    if missing:
        raise ConfigError("missing " + ", ".join(missing))
    env = environ["PLATFORM_ENV"].strip()
    if env not in ALLOWED_ENVS:
        raise ConfigError("invalid PLATFORM_ENV: expected one of " + "/".join(ALLOWED_ENVS))
    return Settings(
        env=env,
        db_host=environ["PLATFORM_DB_HOST"].strip(),
        db_name=environ["PLATFORM_DB_NAME"].strip(),
        db_user=environ["PLATFORM_DB_USER"].strip(),
        db_password=environ["PLATFORM_DB_PASSWORD"],
        db_port=_int_var(environ, "PLATFORM_DB_PORT", 5432),
        http_port=_int_var(environ, "PLATFORM_HTTP_PORT", 8000),
        version=environ.get("PLATFORM_VERSION", "").strip() or "dev",
        mail=load_mail_settings(environ, env),
    )


def check_or_exit(
    environ: Mapping[str, str],
    log: Callable[[str], None] = lambda line: print(line, file=sys.stderr, flush=True),
) -> Settings:
    """Проверка ключей при старте: при ошибке — строка CONFIG ERROR в лог и выход с кодом 1."""
    try:
        return load_settings(environ)
    except ConfigError as exc:
        log(f"CONFIG ERROR: {exc}")
        raise SystemExit(1) from None
