# Карта миграции промпт-блоков: Prompts.csv → таблицы v3.00

Источник: рабочая таблица Prompts (37 блоков, `base_v01.03`) — **read-only**,
старый бот продолжает читать её без изменений. Новые таблицы (`Intents`,
`Prompt_Blocks`, `Intent_Block_Map`, Version_Set `v3.00`) живут в той же базе
NocoDB и наполняются генератором `nocodb/build_tables_data.py` →
`nocodb/*.csv` → `nocodb/push_tables.py`.

## Модель сборки промпта (Prompt Compiler v3.0)

Каждый ход: **ядро** (G-блоки) + **CS_{SCEN}** (стратегия сценария) +
**D_{SCEN}** (источник данных) + **primary stage-task** (по стадии, либо
переопределение из Intent_Block_Map) + **additional-блоки по интенту**
(возражения, REQUIRED_DATA, DEFENSE) + **T-01_TOOLS** (только когда инструменты
открыты Stage Classifier'ом). `final_refusal`/`ban_trigger` уходят в Bypass —
промпт не собирается.

**V2-предпочтение (v3.03):** для name-selected блоков (`CS_{SCEN}` и дефолтный
`ST_{SCEN}_{STAGE}`) компилятор берёт `<ID>_V2`, как только владелец его
активировал (`pick()` в Prompt Compiler, по образцу `G-02_VOICE_V2`);
map-managed блоки катятся черновыми map-строками. `D_{SCEN}` не оборачивается —
V2 для D-блоков нет.

## Перенос 1:1 (32 блока — контент скопирован, готовы к работе)

| Prompts.csv (Type/Block) | Новый Block_ID | Level | Когда в промпте |
|---|---|---|---|
| General/ROLE | G-01_ROLE | general | всегда |
| General/VOICE_AND_TONE | G-02_VOICE | voice | всегда ⚠️ 3365 симв. — кандидат на сжатие |
| General/GOAL | G-03_GOAL | general | всегда |
| General/SPLIT_FORMATTING | G-04_FORMAT | general | всегда |
| General/DEFENSE_MODE | G-05_DEFENSE | general | только интент off_topic |
| General/CRITICAL_CHECKLIST | G-06_CHECKLIST | general | всегда, последним |
| General/TOOLS | T-01_TOOLS | tools | только когда инструменты открыты |
| {Scen}/DATA_SOURCE | D_{SCEN} | data_source | всегда в сценарии |
| {Scen}/FIRST_MESSAGE_LOGIC | ST_{SCEN}_GREETING | stage_task | стадия greeting |
| {Scen}/REQUIRED_DATA | ST_{SCEN}_REQUIRED_DATA | stage_task | additional на data_collect |
| {Scen}/CORE_STRATEGY | CS_{SCEN} | general | всегда в сценарии ⚠️ 1.9–3.4k — кандидат на сжатие |
| {Scen}/GLOBAL_OBJECTIONS | OBJ_{SCEN} | objections | только интенты objection_*/pause — главный источник экономии |

## Нарезка WORKFLOW (5 блоков — контентная работа владельца)

Старые `WORKFLOW` (credit 5354, tradein 7921, combo 8639, stock 2133, consult
1302 симв.) описывают все этапы сразу. В таблице уже созданы:

- `WF_{SCEN}_LEGACY` — полный старый текст, `Active=false`, референс;
- каркасы `ST_{SCEN}_DATA_COLLECT`, `ST_{SCEN}_TOOL_NEEDED`, `ST_{SCEN}_CLOSING`
  — `Active=false`, поле Content с TODO-заглушкой.

Правила нарезки: в каждый ST-блок попадают только инструкции его этапа;
пересечения (повторы «запроси телефон» в трёх местах) остаются только в
CLOSING; ссылки «см. выше» разворачиваются в самодостаточный текст; всё, что
дублирует REQUIRED_DATA/OBJ/TOOLS, — удаляется. После наполнения: `Active=true`
на ST-блоке. Пока ST-блок неактивен, компилятор падает на дефолт — этого
состояния в проде быть не должно (линт ловит).

Для consult и stock WORKFLOW короткие (1.3k/2.1k) — допустимо положить целиком
в ST_{SCEN}_DATA_COLLECT и активировать сразу.

### Тонкая нарезка credit (каркас создан 2026-07-13)

Режимы ШАГА 2 и варианты ШАГА 3 старого WORKFLOW ложатся на существующие
интенты — новые интенты не вводились. Каркасы (Active=false) и привязки уже
в таблицах:

| Блок | Что перенести из WF_CREDIT_LEGACY | Привязка |
|---|---|---|
| ST_CREDIT_DATA_COLLECT | только ШАГ 1 (цель — взнос) | дефолт стадии |
| BLK_CREDIT_PAYMENT_ACCEPTED | ШАГ 2 «СБОРЩИК» | primary @ data_received_complete |
| BLK_CREDIT_REFERENCE | ШАГ 2 «СПРАВОЧНИК» | additional @ answer_question_in_json, price_hierarchy_question |
| BLK_CREDIT_ADVOCATE | ШАГ 2 «АДВОКАТ» (трейд-ин/спецпрограмма) | primary @ objection_price |
| BLK_CREDIT_SECRETARY | ШАГ 2 «СЕКРЕТАРЬ» | additional @ answer_question_no_json |
| BLK_CREDIT_VISIT_DECLINED | ШАГ 3 визит-А (отказ → дистанционный расчёт) | additional @ objection_distance |
| BLK_CREDIT_APPOINTMENT | ШАГ 3 визит-В (дата → адрес + телефон) | additional @ schedule_appointment |

Ответы про оценку авто (ШАГ 3 А/Б) — интенты collect_plate*/objection_no_car,
их Scenarios расширены на credit. «Узнавание клиента» — не промпт, а
персистентный флаг phone_collected (Stage Classifier). Визит-Б/звонок-Б —
ST_CREDIT_CLOSING. Старый primary data_received_complete→ST_CREDIT_TOOL_NEEDED
(credit) деактивирован: пересчёт в чате запрещён протоколом.

⚠️ С 2026-07-13 канонический источник — живые таблицы NocoDB; CSV в nocodb/ —
исторический снапшот, `--replace-data` без согласия владельца запрещён.

## Токен-бюджет: честная арифметика

⚠️ Калибровка (по замеру exec 2339, 2026-07-11): для русского текста реальный
коэффициент — **~2.5 символа/токен**, а не 4, как считалось изначально
(19 675 символов промпта = 8 138 токенов по счётчику n8n). Все цифры ниже
пересчитаны; колонка Tokens_est в NocoDB хранит len/4 — реальные токены
≈ Tokens_est × 1.6.

Оценка типового хода data_collect без возражений (нарезка WORKFLOW ≈ 1/3):

| Сценарий | Ход сейчас (статика) | Ход v3.00 при переносе 1:1 | Цель R10 |
|---|---|---|---|
| credit | ~9950 ток. | **~4950 ток.** | ≤2500 |
| tradein | ~10790 | **~5190** | ≤2500 |
| combo | ~11270 | **~5570** | ≤2500 |
| stock | ~7250 | ~3780 | ≤2500 |
| consult | ~6900 | ~3920 | ≤2500 |

Структурная декомпозиция даёт −50% (подтверждено замером: старый ход ≥9k,
greeting-ход v3 после фикса стадии ≈ 4.7k), но до цели ≤2.5k не дотягивает
ни одна ветка. Добор — контентное сжатие владельцем двух блоков: `G-02_VOICE`
(3365 симв.: примеры тона можно сократить вдвое) и `CS_{SCEN}` (3.2–3.4k:
стратегия частично дублирует WORKFLOW — при нарезке дубли убрать). Линт
предупреждает по бюджету на каждую пару интент×стадия; включение shadow (U9)
и конвейера (U10) от этого не блокируется — цель проверяется на воротах D.

## Intent_Block_Map (108 строк) — только не-дефолтные привязки

Дефолт компилятора: primary = `ST_{SCEN}_{текущая стадия}`. В карте:
объединённые по сценариям правила — возражения → additional `OBJ_{SCEN}`;
сбор данных → additional `ST_{SCEN}_REQUIRED_DATA`; интенты закрытия → primary
`ST_{SCEN}_CLOSING`; интенты инструментов → primary `ST_{SCEN}_TOOL_NEEDED`
(credit/tradein/combo); `off_topic` → additional `G-05_DEFENSE`.

`cross_scenario_question` в v3.00 не имеет отдельного D-блока (спека предлагает
`D_CROSS_*`) — интент распознаётся и логируется, срез данных остаётся базовым;
D_CROSS вводится после shadow-статистики по частоте. Отложено сознательно.

## Статус

- [x] `nocodb/intents.csv` — 32 интента (spec v1.0), Source=origin
- [x] `nocodb/prompt_blocks.csv` — 52 блока (32 активных, 15 каркасов, 5 legacy)
- [x] `nocodb/intent_block_map.csv` — 108 привязок
- [ ] Создание таблиц + загрузка через `nocodb/push_tables.py` (нужен `.env`)
- [ ] Линт-воркфлоу `flow/Tool_ Blocks Lint.json` (нужны ID созданных таблиц)
- [ ] Нарезка 5 WORKFLOW-блоков — владелец контента (полностью в NocoDB UI)
- [ ] Сжатие G-02_VOICE и CS_* до бюджета ≤2.5k — владелец контента
