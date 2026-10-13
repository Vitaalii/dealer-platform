---
title: Naive-время при записи в timestamptz интерпретируется по таймзоне машины, не как UTC
date: 2026-08-08
category: database-issues
module: client-cabinet
problem_type: database_issue
component: testing_framework
symptoms:
  - "Сюита зелёная на SQLite, на PostgreSQL — 14 поведенческих падений в тестах OTP/handoff"
  - "Посеянный тестом «только что отправленный» код OTP на PostgreSQL оказывается истёкшим 8 часов назад"
  - "Таймауты/потолки срабатывают не у тех строк: naive-значения уехали на смещение локальной таймзоны"
  - "`TypeError: can't compare offset-naive and offset-aware datetimes` в ассертах тестов"
root_cause: wrong_api
resolution_type: code_fix
severity: high
related_components:
  - testing_framework
  - database
tags: [sqlalchemy, asyncpg, datetime, timezone, timestamptz, postgresql, sqlite, test-parity]
---

# Naive-время при записи в timestamptz интерпретируется по таймзоне машины, не как UTC

Второй случай одного класса с
[naive-datetime-vs-timezone-aware-columns](naive-datetime-vs-timezone-aware-columns.md)
(находка №3 ревью кабинета): там naive ломал **чтение и сравнение**, здесь —
**запись**. Вместе они закрывают обе стороны разрыва SQLite↔PostgreSQL.

## Problem

Тесты сеяли строки в колонки `DateTime(timezone=True)` naive-значениями
(`NOW = datetime.utcnow` в `tests/test_cabinet_otp_flow.py` и ещё четырёх
файлах). Интуиция «naive, но всегда UTC — значит безопасно» оказалась ложной:
на PostgreSQL посеянный `sent_at=NOW()` уезжал на −8 часов, «свежий» OTP
становился давно истёкшим, фолбэк не отправлял SMS (`0 == 1`), потолок
хэндоффа срабатывал у обоих токенов вместо одного. На SQLite naive-значение
проходит хранение нетронутым — сюита была зелёной (372 passed), падения
проявились только на живом PostgreSQL: 17 failed, из них 14 — этот класс.

## Root cause

SQLAlchemy/asyncpg при бинде naive `datetime` в `timestamptz` интерпретирует
его **в локальной таймзоне машины**, а не как UTC. Эмпирическая проба
(машина в Asia/Singapore, +08):

```text
записано naive utcnow: 2026-08-07 16:10:32
прочитано из PG:       2026-08-07 08:10:32+00:00   ← −8 часов
```

То есть даже дисциплина «во всех naive-значениях держим UTC» не спасает:
сдвиг равен смещению таймзоны той машины, где выполняется код. SQLite
разрыва не показывает — там нет ни типа с таймзоной, ни интерпретации.

## Solution

- Во всех тестовых модулях naive-сеяние заменено на aware:
  `datetime.now(timezone.utc)` (алиас `NOW` в otp_flow переведён на aware).
- Боевой код кабинета уже был чист (`_utcnow()`/`_aware()` — конвенция
  handoff №5); аудит остального боевого кода нашёл только display-метки
  (в БД не пишутся).
- Страж `tests/test_payload.py::TestNaiveDatetimeGuard`: запрещает
  `datetime.utcnow` и `datetime.now()` в кабинетных модулях, `database.py`
  и всех `tests/*.py`; в `main.py` легаси запинено точным счётчиком.

## Prevention

- Любое значение, которое едет в колонку `DateTime(timezone=True)`, обязано
  быть aware ещё в Python — и в проде, и в тестах, и в разовых скриптах.
- «Naive, но по договорённости UTC» — не работает: интерпретацию выбирает
  драйвер по таймзоне машины. Договорённость невозможно проверить, aware —
  проверяется типом.
- Двухдиалектный прогон (`TEST_DATABASE_URL` на локальный PostgreSQL,
  `tests/conftest.py`) — единственный способ увидеть этот класс до прода;
  страж ловит рецидивы дёшево, без PostgreSQL.
