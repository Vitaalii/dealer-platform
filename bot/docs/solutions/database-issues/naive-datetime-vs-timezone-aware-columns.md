---
title: Наивное время проходит на SQLite и падает на PostgreSQL
date: 2026-08-04
category: database-issues
module: client-cabinet
problem_type: database_issue
component: database
symptoms:
  - "На боевом PostgreSQL каждый запрос к `/cabinet/api/*` отдавал бы 500"
  - "`TypeError: can't compare offset-naive and offset-aware datetimes` при сравнении времени из БД с «сейчас»"
  - Фоновый воркер падает на каждом тике
  - Весь тестовый набор на SQLite при этом зелёный
root_cause: wrong_api
resolution_type: code_fix
severity: high
related_components:
  - service_object
  - background_job
  - testing_framework
tags: [sqlalchemy, datetime, timezone, postgresql, sqlite, asyncpg, test-parity]
---

# Наивное время проходит на SQLite и падает на PostgreSQL

## Problem

Весь тестовый набор зелёный на SQLite, но на боевом PostgreSQL каждый запрос к API кабинета отдавал бы 500, а фоновый воркер падал бы на каждом тике.

## Symptoms

- `TypeError: can't compare offset-naive and offset-aware datetimes` в любом месте, где значение читается из БД и сравнивается с «сейчас».
- 500 на каждом `/cabinet/api/*`: валидация Bearer (`validate_bearer`), срок жизни токена и кода (`_get_live_token`), окна лимитов.
- Мёртвый фоновый воркер: три дежурства `otp_worker_tick` падают на каждом тике.
- Локально и в CI (SQLite) ничего из этого не воспроизводится — набор полностью зелёный.

## What Didn't Work

Провальных гипотез не было: дефект найден код-ревью ветки `feat/client-cabinet` (персоны correctness и adversarial), а не отладкой. Отладить его локально было невозможно по построению — на SQLite отказ не возникает.

Что не сработало как защита — сам зелёный набор тестов: он не просто не ловил дефект, он активно вводил в заблуждение, потому что покрывал ровно те пути, которые на проде падают. Запись значений в БД тоже выглядела корректной, что дополнительно маскировало проблему: расхождение проявляется только на чтении.

## Solution

1. Один источник «сейчас»: `_utcnow()` (`cabinet_service.py:133`) возвращает `datetime.now(timezone.utc)`, а не `datetime.utcnow()`.
2. Хелпер нормализации `_aware(dt)` (`cabinet_service.py:144`): naive → подставить UTC, aware → пропустить. Применяется к **каждому** значению, прочитанному из БД, перед сравнением или арифметикой.
3. Фикстура `aware_db` (`tests/test_cabinet_auth.py:85`) эмулирует боевой драйвер: подменяет `result_processor` у SQLite-диалекта `DATETIME`, чтобы чтение отдавало timezone-aware значения, как PostgreSQL/asyncpg. Фикстура самопроверяется (assert внутри самой фикстуры: если эмуляция не работает — тест падает с явным сообщением, а не тихо вырождается в обычный SQLite-тест).

## Why This Works

Колонки объявлены как `DateTime(timezone=True)`, а код сравнивал их значения с `datetime.utcnow()` — наивным временем. SQLite не хранит зону и возвращает naive-значения, поэтому сравнение naive-с-naive работает. PostgreSQL через asyncpg возвращает timezone-aware значения, и `aware > naive` немедленно даёт `TypeError`.

Единый aware-источник «сейчас» плюс нормализация всех прочитанных значений делают обе стороны сравнения aware независимо от движка БД, а фикстура переносит боевую семантику драйвера в тесты, где отказ теперь воспроизводим.

## Prevention

- Никогда не использовать `datetime.utcnow()` в коде, работающем с `DateTime(timezone=True)`; единственный источник «сейчас» — `datetime.now(timezone.utc)`.
- Любое значение времени, прочитанное из БД, пропускать через `_aware()` до сравнения или арифметики.
- Если тесты идут на другом движке БД, чем прод, — эмулировать поведение прод-драйвера, и фикстура эмуляции обязана самопроверяться, иначе однажды тихо выродится в обычный SQLite-тест и перестанет что-либо ловить.
- Обобщение: расхождение движка БД в тестах и в проде — не «мелкая деталь окружения»: типы, которые возвращает драйвер, часть контракта. То же касается предела `INTEGER` (PostgreSQL — 2^31, SQLite — 2^63), частичных индексов, семантики `rowcount` и упорядочивания. Каждое такое различие — класс дефектов, невидимый локально.

## Related Issues

- Отчёт код-ревью: `docs/reviews/2026-08-04-client-cabinet-review.md` (находка #3, P1)
- `docs/solutions/integration-issues/cors-allow-headers-missing-custom-header.md` — дефект, невидимый серверному тест-клиенту по построению
- `docs/solutions/logic-errors/injected-callable-hidden-side-effect.md` — дефект, существующий только в боевой проводке
