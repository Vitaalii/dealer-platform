# core/

Ядро сервиса платформы (пакет `platform_core`): запуск веб-сервера (aiohttp), настройки только из окружения,
проверка ключей при старте (`docs/architecture/ARCHITECTURE.md`, 7.4), подключение к PostgreSQL (asyncpg),
`GET /health`, сборка модулей `leads`, `dealer_cabinet`, `billing` в одно приложение. Зона Разработчика,
конкретные папки — в «Границах» задачи. Правила — корневой `CLAUDE.md`.

- Запуск: `python -m platform_core`. Образ собирает `infra/platform/Dockerfile`, запускает `infra/compose.yaml`.
- Обязательные переменные: `PLATFORM_ENV` (`test`/`prod`), `PLATFORM_DB_HOST`, `PLATFORM_DB_NAME`,
  `PLATFORM_DB_USER`, `PLATFORM_DB_PASSWORD`. Необязательные: `PLATFORM_DB_PORT` (5432), `PLATFORM_HTTP_PORT`
  (8000), `PLATFORM_VERSION` (`dev`). Нет обязательной — в лог `CONFIG ERROR: missing ИМЯ` (без значения),
  выход с кодом 1.
- `/health`: `200 {"status":"ok","db":"ok","env":…,"version":…}` или `503`, если база не отвечает.
- Модуль подключается функцией `setup(app)` в своём пакете (`platform_core/modules.py`).

Тесты (стандартная библиотека, без aiohttp и базы), из корня репозитория:

```
python3 -m unittest discover -s core/tests -t core
```
