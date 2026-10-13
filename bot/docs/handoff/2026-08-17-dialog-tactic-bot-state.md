# Хендофф: диалоговая тактика бота — состояние на 2026-08-17

Работа по плану `docs/plans/2026-08-14-001-feat-cabinet-dialog-tactic-bot-plan.md`
(там — требования R1-R15, KTD, установленные факты Ф1-Ф6 со срезом 2026-08-14).
Остатки код-ревью: `docs/residual-review-findings/feat-cabinet-dialog-tactic.md`.
Этот документ + те два файла = достаточный контекст для продолжения.
Номера строк ниже — срез HEAD `264237d`.

## 1. Ветка и коммиты

`feat/cabinet-calculator`, шесть коммитов этой работы (база — `a2c1683`):

- `5d7c2ff` — U1: колбэк `on_handoff_completed(user_id, trigger, deal_id,
  token_id)` в `cabinet_service.setup()`; хелперы session_closed; фоновый
  добор незакрытых сессий в `otp_worker_tick`.
- `2fb748a` — U2/U3/U5: `close_cabinet_session` в main.py (обе ветки
  доставки), гарды delayed_sender/reminder_worker, подавление ремайндеров
  в окне анкеты; `finalize_transcript_archive` возвращает bool.
- `7225954` — U4: защёлка «оценка получена» → closing, за выключенным
  env-флагом `STAGE_EVAL_RULE`.
- `1e19060` — U6: docs/N8N_HANDOFF_CABINET.md §4 (сигналы для приёмки n8n),
  CABINET_CONTEXT.md.
- `fc11ad5` — фиксы 9-персонного код-ревью (4 P1 закрыты: идемпотентное
  прощание, потолок добора, единственный fire колбэка, потребление pending_*
  гардом; P2: STAGE-защита фолбэков, гард 2-го ремайндера, stale-plate).
- `264237d` — durable-сток остатков ревью.

Откат всей работы: `git revert --no-edit a2c1683..HEAD` (HEAD = `264237d`).

## 2. Тесты

- SQLite: **448 passed, 4 skipped**; PostgreSQL (`TEST_DATABASE_URL`,
  контейнер `unifiedbot-pg16-test`): **452 passed**. JS: golden 13 кейсов,
  calc model 231 ассерт.
- Многоходовые сценарии по фактическим строкам таблиц
  (`tests/test_cabinet_session_close.py`, 16 тестов): рестарт после
  `pending_*` → добор доводит ровно один раз; падение финализации → одно
  прощание, повтор доводит только архив; порядок «ответ Александра →
  прощание» реальным `send_smartbot_reply`; n8n-путь финализации не пишет
  `session_closed`; потолок добора + однократный техалерт; expired-токен
  добором не теряется. Плюс `tests/test_cabinet_handoff.py` (хвост, 7
  тестов колбэка/добора) и `tests/test_payload.py` (17 тестов защёлки).

## 3. Механика завершения сессии (реализовано)

- **Точка подвески**: успешный `_complete_handoff`
  (`cabinet_service.py:2361`) — колбэк стреляет ТОЛЬКО при первичной записи
  события `handoff_done`; fire-and-forget с in-flight-дедупом по user_id
  (`_fire_handoff_completed`, `cabinet_service.py:2222`).
- **Три триггера передачи** сходятся в `attempt_handoff`
  (`cabinet_service.py:2157`): submit анкеты; таймаут бездействия 10 мин;
  потолок 60 мин от верификации (`_check_handoff_timeouts`,
  `cabinet_service.py:504`, triggers "timeout"/"max_age").
- **Признак передачи** — `CabinetToken.handoff_at` + событие `handoff_done`;
  `cabinet_status` его НЕ отражает (draft-передача остаётся
  `phone_confirmed`) — осознанно, контракт не менялся по просьбе n8n.
- **Обёртка завершения** — `close_cabinet_session` (`main.py:816`):
  гейт `_cabinet_close_gate` (`main.py:805`; финализация другим путём тоже
  фиксирует `session_closed`, чтобы добор не крутился), развилка «ход в
  полёте» по `USER_MESSAGE_TASKS`/`lock.locked()` ДО захвата; немедленная
  ветка — под `turn_lock`; pending-ветка — существующий паттерн
  `pending_farewell`/`pending_archive`. STAGE_ID сделки не трогается.
- **Гарды идемпотентности**: `session_farewell_sent`
  (`cabinet_service.py:2254`) — прощание ровно один раз, пишется до
  finalize в немедленной ветке и после отправки в pending-потреблении;
  `session_closed` (`cabinet_service.py:2264`) — только ПОСЛЕ успешной
  финализации, владелец записи — `finalize_transcript_archive`
  (`main.py:714`, по `cabinet_token_id` из archive_data). Оба — через
  `_record_event_once` с пустым meta.
- **Добор** — `_recover_unclosed_sessions` (`cabinet_service.py:2305`):
  grace 600 c от СОБЫТИЯ `handoff_done`, БЕЗ фильтра по статусу токена,
  потолок 24 ч (`cabinet_service.py:2128`) с однократным техалертом.
- **Гарды путей к n8n**: delayed_sender перепроверяет живость под локом и
  потребляет зависшие `pending_*` перед выходом (`main.py:2148` окрестность);
  reminder_worker гасит доставку и перезапись context_json после завершения
  (`main.py:3090`), гард и у 2-го ремайндера (`main.py:3133-3136`); фолбэки
  не двигают STAGE при кабинетной передаче в полёте (`main.py:2208, 2243`).
- **Подавление ремайндеров** в окне «bound без handoff» —
  `reminders_suppressed` (`cabinet_service.py:2275`), вызов `main.py:3036`.
- Текст прощания — общий, `build_farewell_text` (`main.py:698`); web —
  вариант без «перейдите на сайт» + `chat_close` через finalize.

## 4. Защёлка оценки (U4) — код готов, ФЛАГ ВЫКЛЮЧЕН

- Флаг `STAGE_EVAL_RULE` (дефолт ВЫКЛ; `payload_v2.py:51`). С выключенным
  флагом прод-поведение не меняется ВООБЩЕ (включая мутацию слота plate —
  фикс ревью).
- Механика: `register_evaluation` (`payload_v2.py:65`, вызов в точке
  сохранения json_cache) ставит `evaluation_received`/`evaluation_plate`
  при валидном `ti_valuation > 0`; правило в `compute_stage_heuristic`
  (`payload_v2.py:173`; приоритет: phone_collected → greeting → защёлка →
  слоты; только tradein/combo); снятие — по госномеру из ТЕКСТА сообщения
  в `_maybe_unlatch_evaluation` (`payload_v2.py:95`, нормализация
  гомоглифов `normalize_plate`, `payload_v2.py:57`); маркер
  `evaluation_stale_plate` не даёт эху carryover ре-защёлкнуть снятый номер.
- 17 тестов (`tests/test_payload.py`, классы TestEvaluationStageLatch +
  TestEvaluationLatchReviewFixes) зелёные.
- **Условие включения** (в плане, не риск): ответ стороны n8n на
  обострённый Г1 — «что именно у них выставляет tool_needed после успешной
  оценки и перестанет ли перебивать closing от бота». Включение — совместный
  шаг, деплой кода с выключенным флагом безопасен.

## 5. Контракты n8n — установленные факты

- **Тройка «дилер, модель, комплектация» на нулевом ходе**: источник —
  парсинг start_code диплинка прямо в `send_handoff_webhook`
  (`main.py:1310` — `model_id = start_code[2:4]`, там же dealer_code[:2],
  complectation_id[4:6]; в payload — `main.py:1341`). На chat-ходах — те же
  значения из user_context (`main.py:1225`), записанные при входе по
  диплинку. Эхо qualification_context внутри n8n_cache — ДРУГОЙ механизм
  (model_id для снапшота калькулятора кабинета), на нулевом ходе кэша нет.
- **cabinet_url / cabinet_status** — в обеих точках сборки payload через
  `_attach_cabinet_context` (`main.py:1187`); при сбое выдачи ключей нет
  вовсе. Проверено n8n-стороной на живых исполнениях (их постановка
  2026-08-14) — этой работой не тронуто, регрессионные тесты зелёные.
- **Ремайндер-ход** `__SYSTEM_REMINDER__` (`main.py:3075`) несёт
  last_bot_message + кабинетные поля тем же chat-payload'ом. Изменения:
  в окне «bound без handoff» ремайндеров теперь нет (R6); гарантия доставки
  ответа «в полёте» НЕ распространяется на ремайндер-ход — оговорено в
  `docs/N8N_HANDOFF_CABINET.md` §4.

## 6. Ф5 — главное наблюдение для следующей сессии

**Слот `mileage` не пишется нигде в Python-коде** — только читается
(`payload_v2.py:168-169`). Обязательные слоты tradein/combo включают
mileage (`payload_v2.py:33`) → бот-эвристика для этих сценариев
**НИКОГДА не возвращает tool_needed**. Наблюдаемое n8n-стороной
«tool_needed на каждом ходе после оценки» рождается на ИХ стороне
(гипотеза: переопределение стадии по инструментальным интентам их
резолвера — проверяемо только у них). Следствия: (а) защёлка U4 бота
необходима, но НЕ достаточна — без их правки их сторона перебьёт closing;
(б) их формулировки о стадиях, опирающиеся на «бот выставил tool_needed»,
не подтверждаются кодом. Владелец передал Ф5 стороне n8n; Г1 остаётся
за ними.

## 7. Принятые ограничения (из остатков ревью, с тестами-фиксациями)

- PLATE_RE на склеенном probe снимает защёлку ложным матчем («вариант 123
  вс 77» → Т123ВС77) — flag-gated; зафиксировано
  `test_plate_re_false_positive_on_glued_text_documented`.
- Гард delayed_sender исполняется тестами не реальным замыканием
  (source-грep `test_u3_guards_pinned_in_source` + воспроизведение
  потребления `test_pending_consumed_by_guard_branch`); порядок
  «ответ → прощание» — реальным `send_smartbot_reply`
  (`test_answer_delivered_before_farewell_via_real_send_smartbot_reply`).
- `reminders_suppressed` требует status='active': истёкший в окне токен
  снимает подавление (worst case — лишний ремайндер).
- Фолбэк-ветка при `pending_farewell` шлёт свой финальный текст вместо
  канонического прощания (симметричный pop, одно сообщение клиенту).
- `_record_event_once` — check-then-insert без unique constraint
  (однопроцессность бота; pre-existing).
- Полный список — в `docs/residual-review-findings/feat-cabinet-dialog-tactic.md`.

## 8. Незакрыто

- **Ручная приёмка на дев-стенде — за владельцем** (у агента нет доступа к
  боту/ngrok/Tilda): сценарий А — верификация → молчание 10 мин → прощание,
  пара `handoff_done`+`session_closed` в cabinet_events, архив
  `cabinet_handoff`, повторных реплик Александра нет; Б — то же с явным
  submit; В — сообщение в секунду передачи → ответ строго до прощания,
  исполнений в n8n после — ноль. Сценарий Г — только после включения флага.
- **Продуктовый вопрос владельцу**: текст прощания MAX/Telegram («перейдите
  на сайт и отправьте повторный запрос») для клиента, только что отправившего
  заявку, — приглашение ко второй заявке; пересматривается с остальными
  текстами.
- **Структурные выносы** (отложенный refactor): close_cabinet_session и
  гарды — в отдельный модуль; кабинетная ветка finalize — в обёртку;
  `_drain_hook_tasks` — в conftest.
- Институциональный долг: гомоглиф-урок не оформлен записью в
  `docs/solutions/`.
