"""Модули платформы, которые ядро собирает в одно приложение.

Каждый модуль — пакет Python с функцией setup(app): в ней он подключает свои маршруты.
Пакеты лежат в папках монорепозитория (корневой CLAUDE.md, «Зоны агентов»):
leads/leads, dealer-cabinet/dealer_cabinet, billing/billing.
"""
from __future__ import annotations

import importlib
from types import ModuleType

MODULES: tuple[str, ...] = ("leads", "dealer_cabinet", "billing")


def load_modules(names: tuple[str, ...] = MODULES) -> list[ModuleType]:
    loaded = []
    for name in names:
        module = importlib.import_module(name)
        if not callable(getattr(module, "setup", None)):
            raise RuntimeError(f"module {name} has no setup(app)")
        loaded.append(module)
    return loaded


def setup_modules(app: object, names: tuple[str, ...] = MODULES) -> list[str]:
    """Вызывает setup(app) у каждого модуля; возвращает имена подключённых."""
    for module in load_modules(names):
        module.setup(app)
    return list(names)
