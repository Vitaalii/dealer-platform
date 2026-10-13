---
title: Кастомный заголовок ломает CORS, а тест-клиент aiohttp этого не видит
date: 2026-08-04
category: integration-issues
module: client-cabinet
problem_type: integration_issue
component: rails_controller
symptoms:
  - Кросс-доменный опрос статуса доставки кода в браузере не работает вовсе, при этом ошибок на сервере нет
  - "Браузер блокирует `GET /cabinet/api/auth/send-status` после preflight `OPTIONS`"
  - "Все интеграционные тесты CORS зелёные — расхождение видно только в реальном браузере"
root_cause: config_error
resolution_type: code_fix
severity: critical
related_components:
  - service_object
  - testing_framework
tags: [cors, preflight, aiohttp, custom-header, frontend-contract, integration-testing]
---

# Кастомный заголовок ломает CORS, а тест-клиент aiohttp этого не видит

## Problem

Кросс-доменный опрос статуса доставки кода (`GET /cabinet/api/auth/send-status`) не работал бы в браузере вообще — в боевой схеме (Tilda дилера → домен бота) цикл опроса не запустился бы ни разу, хотя все интеграционные тесты CORS были зелёными.

## Symptoms

- Фронт на домене дилера не получает ответ от `/cabinet/api/auth/send-status`; запрос блокируется браузером до отправки.
- На сервере нет ни ошибки, ни записи о неуспехе — основной запрос просто не доходит.
- Тестовый набор при этом полностью зелёный, включая тесты CORS.

## What Didn't Work

Провальных гипотез отладки не было: дефект найден код-ревью ветки `feat/client-cabinet` (персоны api-contract и security), а не воспроизведением. Отладка его и не нашла бы — локально кабинет открывается с того же origin, preflight не возникает, а серверный тест-клиент выполняет запрос напрямую.

Что действительно не сработало — существовавший «страховочный» тест: он проверял, что в ответе присутствуют `Access-Control-Allow-Origin` (echo Origin) и `Vary: Origin`, и что в `Access-Control-Allow-Headers` есть `Authorization` и `Content-Type`. Все эти утверждения выполнялись и создавали ложное ощущение покрытия, потому что ни одно из них не сверялось с тем, что реально отправляет фронт.

## Solution

1. Заголовок добавлен в whitelist — `cabinet_service.cabinet_cors_headers` (`cabinet_service.py:1408`):

   ```python
   "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Cabinet-Token",
   ```

2. Добавлен тест, сверяющий контракт с фронтом, а не сам с собой — `test_cors_preflight_allows_x_cabinet_token` (`tests/test_cabinet_auth.py`): `OPTIONS /cabinet/api/auth/send-status` с `Origin`, `Access-Control-Request-Method: GET` и `Access-Control-Request-Headers: x-cabinet-token`, затем регистронезависимая проверка, что заголовок присутствует в `Access-Control-Allow-Headers` (вместе с `authorization` и `content-type`).

## Why This Works

Фронт слал кастомный заголовок `X-Cabinet-Token`, а `Access-Control-Allow-Headers` перечислял только `Authorization, Content-Type`. Кастомный заголовок делает запрос «непростым» (non-simple): браузер отправляет preflight `OPTIONS`, не находит заголовок в whitelist и блокирует основной запрос. Добавление заголовка в whitelist закрывает preflight, а тест фиксирует список как контракт.

**Почему тесты молчали:** `aiohttp.test_utils` не эмулирует браузерный preflight — он просто выполняет запрос. Тесты проверяли echo Origin и `Vary`, но никогда не сверяли список разрешённых заголовков с тем, что реально отправляет фронт.

## Prevention

- Список `Access-Control-Allow-Headers` — это контракт с фронтом, и он должен проверяться против фронта, а не сам по себе. Тест: `OPTIONS` с `Access-Control-Request-Headers: <каждый кастомный заголовок фронта>` → заголовок присутствует в ответе (сравнение регистронезависимое).
- При добавлении любого кастомного заголовка на фронте одновременно обновлять whitelist и тест preflight; источник заголовков — `cabinet_service.cabinet_cors_headers`, единственный потребитель — `widget-custom/cabinet-app/app.js`.
- Обобщение: любой контракт, который в проде проверяет браузер, а не сервер (preflight, CSP, SameSite, mixed content), не проверяется серверным тест-клиентом по построению. Такие вещи ловятся только сверкой двух сторон контракта в одном тесте либо ручной проверкой в реальном браузере.

## Related Issues

- Отчёт код-ревью: `docs/reviews/2026-08-04-client-cabinet-review.md` (находка #1, P0)
- `docs/solutions/database-issues/naive-datetime-vs-timezone-aware-columns.md` — другой класс дефектов, невидимый для локального тестового окружения
- `docs/solutions/logic-errors/injected-callable-hidden-side-effect.md` — дефект, существующий только в боевой проводке
