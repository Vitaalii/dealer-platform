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
- Почта (`platform_core/mail/`, `ARCHITECTURE.md` 7.6): обязательные `MAIL_PROVIDER` (`postbox` или `log`;
  `log` только пишет в лог и запрещён при `PLATFORM_ENV=prod`) и `MAIL_FROM`; при `postbox` — ещё
  `POSTBOX_KEY_ID`, `POSTBOX_SECRET_KEY`. Необязательные: `MAIL_FROM_NAME`, `MAIL_MAX_PER_SECOND` (1),
  `POSTBOX_ENDPOINT` (`https://postbox.cloud.yandex.net`), `POSTBOX_REGION` (`ru-central1`). Модули берут
  отправителя из `app[MAILER_KEY]` (`platform_core/app.py`): `await mailer.send(to, subject, text, html=None)`;
  несколько адресов — одно письмо, до 200 получателей. Ошибки: `MailPermanentError` (не повторять),
  `MailTemporaryError` (3 попытки при 429, 5xx, сбое сети — кончились). Проверка владельцем:
  `python -m platform_core.mail --to <адрес> [--to <адрес>]` (`infra/README.md`, раздел 14).
  Новый сервис отправки — класс с тем же `send()` и значение `MAIL_PROVIDER` в `build_mailer`.
- `/health`: `200 {"status":"ok","db":"ok","env":…,"version":…}` или `503`, если база не отвечает.
- Модуль подключается функцией `setup(app)` в своём пакете (`platform_core/modules.py`).

Тесты (стандартная библиотека, без aiohttp и базы), из корня репозитория:

```
python3 -m unittest discover -s core/tests -t core
```
