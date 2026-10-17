"""Веб-приложение платформы (aiohttp): /health и модули."""
from __future__ import annotations

import logging

from aiohttp import web

from .config import Settings
from .db import Database
from .health import health_payload
from .mail import Mailer, build_mailer
from .modules import setup_modules

log = logging.getLogger("platform")

SETTINGS_KEY = web.AppKey("settings", Settings)
DB_KEY = web.AppKey("db", Database)
# Почта для модулей: request.app[MAILER_KEY].send(...) (platform_core/mail).
MAILER_KEY = web.AppKey("mailer", Mailer)


async def health(request: web.Request) -> web.Response:
    db_ok = await request.app[DB_KEY].ping()
    status, body = health_payload(request.app[SETTINGS_KEY], db_ok)
    return web.json_response(body, status=status)


async def _close_db(app: web.Application) -> None:
    await app[DB_KEY].close()


async def _close_mailer(app: web.Application) -> None:
    await app[MAILER_KEY].close()


def build_app(settings: Settings) -> web.Application:
    app = web.Application()
    app[SETTINGS_KEY] = settings
    app[DB_KEY] = Database(settings)
    app[MAILER_KEY] = build_mailer(settings.mail)
    app.router.add_get("/health", health)
    app.on_cleanup.append(_close_db)
    app.on_cleanup.append(_close_mailer)
    modules = setup_modules(app)
    log.info("platform env=%s version=%s modules=%s", settings.env, settings.version, ",".join(modules))
    log.info("mail provider=%s", settings.mail.provider)
    return app


def run(settings: Settings) -> None:
    web.run_app(build_app(settings), host="0.0.0.0", port=settings.http_port, print=None)
