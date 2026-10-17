"""Настройки сервиса платформы — только из переменных окружения.

Проверка ключей при старте (docs/architecture/ARCHITECTURE.md, 7.4): если обязательной
переменной нет, в лог уходит только её имя (без значения), процесс завершается с кодом 1.
Модуль без внешних зависимостей — его тесты идут на стандартной библиотеке.
"""
from __future__ import annotations

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


class ConfigError(Exception):
    """Ошибка настроек. Текст содержит только имена переменных, не значения."""


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


def load_settings(environ: Mapping[str, str]) -> Settings:
    missing = missing_vars(environ, REQUIRED_VARS)
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
