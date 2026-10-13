---
title: "fix: Правдивые логи и сторож ProstoSMS — алерты с парным логом, статусы отправок, БД pre-ping"
type: fix
date: 2026-09-08
status: awaiting-owner-decisions
origin: разведка инцидента «627 без следа в логах» + ревизия ложных записей (Н1-Н9)
---

# fix: Правдивые логи, сторож ProstoSMS, устойчивость БД

## Ответы разведки (сводно; [код]/[live] в отчёте сессии)

- **Н1:** версия «вебхук MAX — триггер» опровергнута; причинность обратная:
  ответ 342 — ветка `sender.is_bot` → `web.Response("OK")` (короче ~22Б) —
  это эхо-вебхук СОБСТВЕННОГО алерт-сообщения бота в группе. К ProstoSMS
  обращаются ровно три места: send_otp (request-code кабинета), get_balance
  в startup_selfcheck (старт контейнера), get_balance в check_balance ←
  sync_worker_duties ← sync_worker (каждые 1800с). Кнопка request_contact
  SMS-провайдера не касается (vCard → текст → воронка).
- **Н2:** обе алерт-точки шлют в чат БЕЗ парной строки в логе:
  sms_gateway.startup_selfcheck:356-372 (tech_alert без logger) и
  cabinet_service.check_balance:601-621 (logger.warning в ветке
  `if not _maybe_config_alert(...)` — при отправленном алерте лог молчит).
- **Н4:** ключ ОДИН — PROSTOSMS_API_KEY (sms_gateway.py: send_otp и
  get_balance используют `self._api_key`); PROSTOSMS_LOGIN/PASSWORD в .env —
  легаси, кодом не читаются. Гипотеза «просрочен один из ключей» снята.
- **«NOT delivered» всегда ложь:** send_alert_to_max_group (main.py:1024-1095)
  завершается `await max_client.send_alert_to_group(...)` БЕЗ return → каждая
  проверка `_ok = await send_alert_to_max_group(...)` получает None → строка
  «Notification card NOT delivered» пишется при КАЖДОЙ доставленной карточке.
- **Н9:** send_smartbot_reply статуса не возвращает; ремайндер логирует
  «Sent to» безусловно (main.py:3428-3429), причём context с reminder_level=1
  и репликой пишется ДО отправки. Что вернул MAX — из кода не установить:
  reliable_send/max_client статус и тело ошибки не логируют (только
  «Возможно 503»).
- **Н8:** database.py:20 `create_async_engine(DB_URL, echo=False)` — без
  pool_pre_ping/pool_recycle; ночной обрыв (~06:00, три дня подряд — окно
  на стороне Dockhost/PG) оставляет мёртвые коннекты в пуле → InterfaceError/
  ConnectionDoesNotExistError при первом использовании. Контекст 5488361
  потерян: Логика Б продолжила закрытие при упавшем сохранении.

---

## Implementation Units

### U1. Алерт = парный лог (Н2)

**Files:** sms_gateway.py (startup_selfcheck), cabinet_service.py
(_maybe_config_alert:205-216, check_balance), main.py (_sms_tech_alert:3959),
tests/test_sms_gateway.py.

**Approach:** единая точка — _sms_tech_alert (и cabinet-аналог) пишет
logger.error("TECH_ALERT %s", text) ПЕРЕД отправкой в чат; грепается одним
словом; содержание и время совпадают с сообщением. Дополнительно
check_balance логирует любой не-ok результат независимо от алерта.

**Test scenarios:** алерт 627 → в caplog строка TECH_ALERT с тем же текстом;
ok-баланс → нет; не-ok без алерта → warning остаётся.

### U2. Отправки возвращают статус; ложные ветки убраны

**Files:** main.py (send_alert_to_max_group — return bool по результату;
send_smartbot_reply — return bool «все куски доставлены»; reliable_send —
лог фактического status и тела ответа MAX), max_client.py (send_message /
send_alert_to_group — статус и тело в лог при не-200), tests.

**Test scenarios:** успешный alert → «card sent», ложного NOT delivered нет;
фейл всех ретраев → NOT delivered с фактическим статусом; send_smartbot_reply
False при FATAL.

### U3. Ремайндер честен (Н9)

**Files:** main.py (reminder-блок :3416-3430), tests.

**Approach:** «Sent to» — только при True от send_smartbot_reply (U2);
reminder_level/история — тоже только после успеха (порядок: отправка →
фиксация), иначе «Retry на следующем тике».

**Test scenarios:** фейл отправки → нет «Sent», reminder_level не растёт,
реплика не дописана; успех → как раньше.

### U4. Сторож ProstoSMS информативен (Н6)

**Files:** sms_gateway.py (build_config_alert_text:347-354, _outbound_ip),
cabinet_service.py (передача контекста вызова), tests/test_sms_gateway.py.

**Approach:** (а) внешний IP: определяется ОДИН раз при старте запросом к
https://api.ipify.org (таймаут 3с, фолбэк «не определён») и кэшируется;
при алерте используется кэш + пере-запрос не чаще раза в 15 мин — дёшево и
без стороннего сервиса на каждый отказ; внутренний IP остаётся отдельной
строкой с пометкой «внутренний»; (б) контекст вызова: какая операция
(get_balance/send_otp), какой получатель (маскированный телефон/селфчек);
(в) маска ключа: первые4…последние4 и длина. Плюс отметка аккаунта A-42189.

**Test scenarios:** текст алерта содержит все три блока; ключ в тексте
только маской; кэш IP не дёргает сеть повторно в течение TTL.

### U5. БД: pre-ping, recycle, ретрай сохранения контекста (Н8)

**Files:** database.py:20 (engine: pool_pre_ping=True, pool_recycle=1800 —
только для не-sqlite), main.py (save_context_to_db — один ретрай с новой
сессией при InterfaceError/ConnectionDoesNotExistError; Логика Б: при
неудачном сохранении контекста НЕ продолжать закрытие — перенос на следующий
тик, данные не теряются), tests/test_init_db_postgres.py или новый.

**Test scenarios:** engine создаётся с pre_ping на PG-URL и без него на
sqlite; save_context ретраится при обрыве (мок первого execute); Логика Б
при сбое сохранения не двигает стадию и не чистит контекст.

### U6. Пины принципа правдивого лога (Н7)

**Files:** tests/test_bot_integration.py (source-пины: send_alert_to_max_group
возвращает результат; «Sent to» под условием; TECH_ALERT существует),
docs/solutions/logic-errors/ — урок «лог результата — только по
подтверждённому результату» (попытка/результат/канал — раздельные строки).

---

## Вне кода (владельцу)

- Внешний IP прод-контейнера: `curl -s https://api.ipify.org` изнутри
  контейнера несколько раз (Н3) + время стартов контейнера в панели Dockhost
  около 05:4x/06:2x UTC (проверка версии «второй алерт — selfcheck после
  рестарта»); совпадает ли IP с 45.92.179.192/28 — сказать по результату.
- Сверка прод-ключа по маске (панель Dockhost env vs ProstoSMS).
- Вопрос Dockhost про ночное окно ~06:00 UTC (обрывы соединений три дня).

## Scope Boundaries

- Ключи не ротируются; n8n/промпты не трогаются; выкат на стопе (в общий
  релиз с d1ec03f…297a6b9 по слову владельца).
- Барьер Р4, пути телефона, воронковые поля — не затрагиваются.
