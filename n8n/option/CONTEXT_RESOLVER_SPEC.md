# Context Resolver — Полная спецификация
## UnifiedBot v3.0
### Ревизия: финальная (исправлен порядок нод, добавлено масштабируемое хранение)

---

## Роль в конвейере

```
Webhook → Edit Fields
        → Context Resolver    ← ПЕРВЫМ: читает смысл сообщения
        → Stage Classifier    ← теперь знает intent, точнее управляет доступом
        → Data Selector       ← знает intent + stage → точный срез данных
        → Prompt Compiler     ← все три сигнала → точечный промпт
        → [Bypass]
        → LLM Agent
        → Tool Guard
        → Respond to Webhook
```

**Почему Context Resolver стоит первым — не третьим.**

Stage Classifier и Data Selector принимают решения, которые зависят от смысла сообщения клиента:

- **Data Selector:** нужны ли `cross_scenario` данные стока? Зависит от `dialog_intent`, а не от `current_stage`.
- **Stage Classifier:** открывать ли инструменты? `data_received_complete` открывает `tool_needed` даже если Python ещё не обновил `collected_data`. `final_refusal` немедленно уходит в Bypass без запуска остальных нод.

Если Resolver стоит после них — они уже приняли решения вслепую. Resolver должен давать сигнал до, а не после.

---

## Вход: что видит Context Resolver

Три источника. Все обязательны.

### 1. Последние 6–8 сообщений диалога

Только текст, без технических полей и JSON каталога.

```json
"recent_history": [
  { "role": "bot",    "text": "На какую сумму взноса вы ориентируетесь?" },
  { "role": "client", "text": "А какой там двигатель?" },
  { "role": "bot",    "text": "Двигатель 2.0 турбо, 197 л.с. Так на какой взнос рассчитываете?" },
  { "role": "client", "text": "ну тысяч 200 наверное, а можно меньше?" }
]
```

### 2. Текущее сообщение клиента (отдельно)

```json
"current_message": "ну тысяч 200 наверное, а можно меньше?"
```

### 3. Контекст сессии (минимальный)

```json
"session_context": {
  "request_type":    "credit",
  "current_stage":   "data_collect",
  "has_phone":       false,
  "has_plate":       false,
  "has_mileage":     false,
  "has_down":        false,
  "phone_ask_count": 0,
  "msg_count":       4,
  "appointment_date": null
}
```

---

## Системный промпт Context Resolver

Промпт **не хардкодит список интентов** — он загружает их динамически из NocoDB-кэша при старте воркфлоу. Это ключевое отличие от предыдущей версии: новый интент в таблице = Resolver автоматически начинает его распознавать без правки кода.

```
Ты — классификатор состояния диалога в автосалоне.
Проанализируй последние сообщения и текущее сообщение клиента.
Определи точный момент диалога и верни СТРОГО JSON, без пояснений.

Сценарий: [request_type]
Текущая стадия системы: [current_stage]
Данные собраны: взнос=[has_down], госномер=[has_plate], пробег=[has_mileage]
Телефон запрашивался: [phone_ask_count] раз

[INTENT_LIST]
--- НАЧАЛО СПИСКА ДОПУСТИМЫХ ИНТЕНТОВ ---
[динамически загруженный список из NocoDB — см. раздел "Хранение интентов"]
Формат каждой строки:
  INTENT_ID | Описание | Примеры фраз
--- КОНЕЦ СПИСКА ---
[/INTENT_LIST]

Последние сообщения:
[recent_history]

Текущее сообщение клиента: "[current_message]"

Верни ТОЛЬКО JSON:
{
  "dialog_intent":      "<значение строго из списка выше>",
  "client_mode":        "<значение строго из списка client_mode>",
  "client_signal":      "<значение строго из списка client_signal>",
  "key_data_extracted": { <данные если распознаны, иначе {}> },
  "reasoning":          "<одно предложение: почему именно этот интент>"
}
```

---

## Выход: три измерения + извлечённые данные

### `dialog_intent` — что делать агенту прямо сейчас
Главное поле. Определяет выбор STAGE_TASK блока и влияет на Data Selector.

### `client_mode` — какой режим из матрицы реакций активен
Прямое отображение на РЕЖИМЫ из промптов (СБОРЩИК, СПРАВОЧНИК, АДВОКАТ, СЕКРЕТАРЬ и т.д.).

### `client_signal` — эмоциональный вектор
Двигается к сделке или от неё. Помогает агенту выбрать тон.

### `key_data_extracted` — данные из текста клиента
Resolver парсит данные попутно с классификацией. Python обновляет `collected_data` из этого объекта — не нужен отдельный парсинг.

```json
"key_data_extracted": {
  "phone":            "+79991234567",
  "plate":            "А123АА77",
  "mileage":          85000,
  "down_payment":     300000,
  "appointment_date": "30.05.2026",
  "appointment_type": "visit",
  "competitor_name":  "Авито"
}
```

---

## Полная матрица `dialog_intent` (v1.0 — 32 значения)

Стартовый набор. Пополняется через NocoDB без правки кода.

### Группа A: СБОР ДАННЫХ

| `dialog_intent` | Когда | STAGE_TASK |
|---|---|---|
| `collect_down_payment` | Взнос не назван, клиент пишет что угодно | ST_CREDIT_DATA_COLLECT |
| `collect_plate` | Госномер не собран (tradein/combo) | ST_TRADEIN_DATA_COLLECT |
| `collect_mileage` | Госномер есть, пробег не собран | ST_TRADEIN_DATA_COLLECT |
| `collect_plate_and_mileage` | Ни того ни другого нет | ST_TRADEIN_DATA_COLLECT |
| `data_received_complete` | Клиент передал последний нужный параметр | ST_*_TOOL_NEEDED |

### Группа B: ВОПРОСЫ И СПРАВОЧНИК

| `dialog_intent` | Когда | STAGE_TASK |
|---|---|---|
| `answer_question_in_json` | Вопрос по цене/характеристикам, данные ЕСТЬ в JSON | ST_*_DATA_COLLECT (СПРАВОЧНИК) |
| `answer_question_no_json` | Вопрос, данных нет в JSON | ST_*_DATA_COLLECT (СЕКРЕТАРЬ) |
| `cross_scenario_question` | Вопрос из «чужого» сценария (сток в кредитном) | ST_*_DATA_COLLECT + cross_scenario |
| `price_hierarchy_question` | «Сколько стоит» / «какая цена» | ST_*_DATA_COLLECT (СПРАВОЧНИК) |
| `competitor_comparison` | «На Авито дешевле», «у другого дилера» | ST_*_CLOSING (ЦЕНОВОЕ СРАВНЕНИЕ) |

### Группа C: ИНСТРУМЕНТЫ

| `dialog_intent` | Когда | STAGE_TASK |
|---|---|---|
| `ready_for_tool` | Все параметры собраны, инструмент не вызывался | ST_*_TOOL_NEEDED |
| `tool_result_received` | Инструмент вернул результат | ST_*_CLOSING |
| `tool_error_handling` | Инструмент вернул ошибку / IN_PROGRESS | ST_*_CLOSING_TOOL_FAILED |
| `tool_rerun_needed` | Клиент прислал исправленный госномер | ST_*_TOOL_NEEDED |

### Группа D: ЗАКРЫТИЕ

| `dialog_intent` | Когда | STAGE_TASK |
|---|---|---|
| `propose_visit` | После расчёта — предложить визит | ST_*_CLOSING |
| `propose_call` | Клиент отказался от визита | ST_*_CLOSING |
| `request_phone` | Клиент согласился на визит или звонок | ST_*_CLOSING |
| `finalize_agreed` | Телефон дан ИЛИ известный клиент согласился | → finalize_deal |
| `schedule_appointment` | Клиент называет конкретную дату | ST_*_CLOSING + appointment |

### Группа E: ВОЗРАЖЕНИЯ

| `dialog_intent` | Когда | STAGE_TASK |
|---|---|---|
| `objection_no_phone` | Отказывается от телефона (первый раз) | OBJ_UNIVERSAL |
| `objection_no_phone_repeat` | Второй раз уклоняется | OBJ_UNIVERSAL + ask_count=2 |
| `objection_distance` | «Далеко», «нет времени» | ST_*_CLOSING (ЛОГИСТИЧЕСКИЙ ТУПИК) |
| `objection_price` | «Дорого», «нет взноса» | ST_*_DATA_COLLECT (АДВОКАТ) |
| `objection_trust` | «Откуда такие данные» | ST_*_CLOSING (СКЕПСИС) |
| `objection_data_privacy` | «Зачем госномер», «боюсь баз» | ST_*_DATA_COLLECT (АДВОКАТ) |
| `objection_no_car` | «Нет своей машины» (tradein/combo) | ST_*_DATA_COLLECT (разворот) |
| `pause_requested` | «Подумаю», «напишу позже» | OBJ_UNIVERSAL (пауза) |
| `final_refusal` | Стоп-слова категорические | → Bypass, finalize(refuse) |

### Группа F: СИСТЕМНЫЕ

| `dialog_intent` | Когда | STAGE_TASK |
|---|---|---|
| `greeting_first` | msg_count=0 | ST_*_GREETING + VE-01 |
| `reminder_response` | Клиент ответил на ремайндер | ST_*_DATA_COLLECT / CLOSING |
| `off_topic` | Полностью вне темы | DEFENSE_MODE |
| `ban_trigger` | Мат, агрессия | → Bypass, finalize(ban) |

---

## Полная матрица `client_mode` (10 значений)

| `client_mode` | Из промпта | Описание |
|---|---|---|
| `collector` | СБОРЩИК | Идёт по сценарию, даёт данные |
| `reference` | СПРАВОЧНИК | Задаёт вопросы, данные есть в JSON |
| `advocate_tradein` | АДВОКАТ → Трейдин | Финансовый барьер, авто есть |
| `advocate_special` | АДВОКАТ → Спецпрограмма | Финансовый барьер, авто нет |
| `secretary` | СЕКРЕТАРЬ | Вопрос вне JSON |
| `logistics_block` | ЛОГИСТИЧЕСКИЙ ТУПИК | Далеко / нет времени |
| `skeptic` | СКЕПСИС ПО ОЦЕНКЕ | Сомневается в данных |
| `price_compare` | ЦЕНОВОЕ СРАВНЕНИЕ | Сравнивает с конкурентами |
| `direct_agree` | ПРЯМОЕ СОГЛАСИЕ | Согласен на визит или звонок |
| `resistance` | Барьер данных | Уходит от госномера |

## Полная матрица `client_signal` (9 значений)

| `client_signal` | Описание | Пример |
|---|---|---|
| `moving_forward` | Двигается к сделке | «окей, взнос 300к» |
| `hesitating` | Отвечает, но сомневается | «ну может 200, не знаю» |
| `asking_more` | Уточняет перед решением | «а какой процент?» |
| `stalling` | Уклоняется, не отказывает | «посмотрим», «не знаю» |
| `resisting` | Активное противодействие | «зачем вам мои данные» |
| `scheduling` | Называет дату | «приеду в пятницу» |
| `refusing_softly` | Мягкий отказ | «не готов пока» |
| `refusing_hard` | Жёсткий отказ, стоп-слова | «не пишите мне» |
| `neutral` | Нейтрально | «понятно», «ладно» |

---

## Масштабируемое хранение интентов и промпт-блоков в NocoDB

### Принцип: всё хранится в трёх таблицах, код не меняется

```
Таблица 1: Intents          ← список всех dialog_intent
Таблица 2: Prompt_Blocks    ← все блоки промптов (G-*, ST-*, OBJ-*, T-*, R-*)
Таблица 3: Intent_Block_Map ← связь интент → блоки промптов (many-to-many)
```

При добавлении нового интента из практики: добавить строку в Intents + строки в Intent_Block_Map. Код нод не трогается.

---

### Таблица 1: `Intents`

Полный реестр всех dialog_intent. Resolver загружает эту таблицу при старте и подставляет в свой промпт.

| Поле | Тип | Описание | Пример |
|---|---|---|---|
| `Intent_ID` | string, PK | Уникальный идентификатор | `objection_price` |
| `Group` | enum | A/B/C/D/E/F | `E` |
| `Group_Name` | string | Человекочитаемое название группы | `Возражения` |
| `Description` | text | Когда срабатывает — для Resolver'а | «Клиент говорит "дорого", называет взнос ниже расчётного» |
| `Example_Phrases` | text | Примеры фраз клиента (через `;`) | «дорого;нет таких денег;200к это много» |
| `Scenarios` | multi-enum | credit/tradein/combo/stock/consult/all | `credit,combo` |
| `Stage_Context` | multi-enum | На каком стейдже возможен | `data_collect,closing` |
| `Priority` | int | При конфликте — выше приоритет побеждает | `10` |
| `Active` | bool | Включён ли интент | `true` |
| `Version` | string | Версия набора | `v01.00` |
| `Source` | enum | origin/practice/test | `practice` |
| `Notes` | text | Комментарий для администратора | «Добавлен после кейса 2026-06-01» |

**Поле `Source`** — важное для качества: `origin` = базовые 32 интента, `practice` = добавлены из реальных кейсов, `test` = экспериментальные (ещё не в проде).

**Поле `Priority`** — нужно когда одно сообщение клиента можно отнести к нескольким интентам. Например «дорого, и вообще не хочу давать номер» — конфликт между `objection_price` (E, приоритет 10) и `objection_no_phone` (E, приоритет 8). Побеждает `objection_price` как более ранний в воронке.

---

### Таблица 2: `Prompt_Blocks`

Без изменений относительно PROMPT_STRUCTURE_V3.md, но добавляется одно ключевое поле:

| Поле | Тип | Описание |
|---|---|---|
| `Block_ID` | string, PK | G-01, ST_CREDIT_GREETING и т.д. |
| `Level` | enum | general/data_source/stage_task/objections/tools/voice/reminder |
| `Scenario` | enum | all/credit/tradein/combo/stock/consult |
| `Stage` | enum | all/greeting/data_collect/tool_needed/closing/objection/reminder |
| `Version_Set` | string | Имя набора версий: «base», «v01.00» |
| `Content` | text | Текст блока с переменными |
| `Tokens_est` | int | Оценка размера |
| `Active` | bool | Включён |
| `**Intent_Triggers**` | text | **НОВОЕ:** список Intent_ID через запятую, которые активируют этот блок как дополнительный |
| `Notes` | text | Комментарии |

**Поле `Intent_Triggers`** — для блоков, которые подключаются не по стейджу, а по интенту. Пример: блок `OBJECTION_COMPETITOR_RESPONSE` подключается когда `dialog_intent=competitor_comparison`, независимо от стейджа. Значение: `competitor_comparison`. Prompt Compiler при компиляции проверяет все блоки с `Intent_Triggers` и добавляет совпавшие.

---

### Таблица 3: `Intent_Block_Map`

Связь many-to-many между интентом и блоками промптов. Определяет: этот интент → эти блоки STAGE_TASK + эти дополнительные блоки.

| Поле | Тип | Описание | Пример |
|---|---|---|---|
| `Map_ID` | int, PK | Автоинкремент | `42` |
| `Intent_ID` | string, FK → Intents | Интент | `objection_price` |
| `Block_ID` | string, FK → Prompt_Blocks | Блок промпта | `ST_CREDIT_DATA_COLLECT` |
| `Role` | enum | primary/additional/override/suppress | `primary` |
| `Scenario_Filter` | enum | all/credit/tradein/combo/stock/consult | `credit` |
| `Stage_Filter` | enum | all/data_collect/closing/... | `all` |
| `Version_Set` | string | Версия набора | `v01.00` |
| `Active` | bool | Включено | `true` |

**Поле `Role`:**
- `primary` — этот блок STAGE_TASK заменяет дефолтный (выбранный по current_stage)
- `additional` — этот блок добавляется к промпту поверх основного
- `override` — этот блок заменяет другой конкретный блок (указывается в Notes)
- `suppress` — этот блок исключается из промпта при данном интенте

**Пример маппинга для `objection_price` в credit:**
```
Intent_ID             | Block_ID                      | Role      | Scenario_Filter
objection_price       | ST_CREDIT_DATA_COLLECT        | primary   | credit
objection_price       | OBJ_ADVOCATE_TRADEIN          | additional| credit
objection_price       | ST_COMBO_DATA_COLLECT         | primary   | combo
```

**Пример маппинга для `cross_scenario_question` в credit:**
```
Intent_ID               | Block_ID                  | Role      | Scenario_Filter
cross_scenario_question | ST_CREDIT_DATA_COLLECT    | primary   | credit
cross_scenario_question | D_CROSS_SCENARIO_CREDIT   | additional| credit
```
D_CROSS_SCENARIO_CREDIT — это дополнительный DATA_SOURCE блок с cross_scenario данными, который подключается только при этом интенте.

---

### Как Prompt Compiler читает маппинг

```javascript
// При старте воркфлоу — загрузить кэши из NocoDB
INTENTS_CACHE      = await loadTable('Intents',         { Active: true, Version: activeVersion });
BLOCKS_CACHE       = await loadTable('Prompt_Blocks',   { Active: true, Version_Set: activeVersionSet });
INTENT_MAP_CACHE   = await loadTable('Intent_Block_Map',{ Active: true, Version_Set: activeVersionSet });

// В Prompt Compiler — при каждом запросе
function getBlocksForIntent(dialogIntent, requestType, currentStage) {
  // 1. Найти все маппинги для этого интента
  const maps = INTENT_MAP_CACHE.filter(m =>
    m.Intent_ID === dialogIntent &&
    (m.Scenario_Filter === 'all' || m.Scenario_Filter === requestType) &&
    (m.Stage_Filter === 'all' || m.Stage_Filter === currentStage)
  );

  const primaryBlock    = maps.find(m => m.Role === 'primary')?.Block_ID;
  const additionalBlocks = maps.filter(m => m.Role === 'additional').map(m => m.Block_ID);
  const suppressedBlocks = maps.filter(m => m.Role === 'suppress').map(m => m.Block_ID);

  return { primaryBlock, additionalBlocks, suppressedBlocks };
}

function compilePrompt(dialogIntent, currentStage, requestType, toolAccess, stageMeta) {
  const parts = [];

  // General блоки (всегда)
  ['G-01','G-02','G-03','G-04','G-05'].forEach(id => parts.push(block(id)));

  // DATA_SOURCE (по стейджу и сценарию)
  if (['data_collect','tool_needed','closing'].includes(currentStage)) {
    parts.push(block(`D_${requestType.toUpperCase()}`));
  }

  // STAGE_TASK: берём primary из Intent_Block_Map или дефолтный по стейджу
  const { primaryBlock, additionalBlocks, suppressedBlocks } =
    getBlocksForIntent(dialogIntent, requestType, currentStage);

  const stageTaskId = primaryBlock
    || (stageMeta.tool_failed
        ? `ST_${requestType.toUpperCase()}_${currentStage.toUpperCase()}_TOOL_FAILED`
        : `ST_${requestType.toUpperCase()}_${currentStage.toUpperCase()}`);

  if (stageTaskId && !suppressedBlocks.includes(stageTaskId)) {
    parts.push(block(stageTaskId));
  }

  // Дополнительные блоки из маппинга (additional)
  additionalBlocks
    .filter(id => !suppressedBlocks.includes(id))
    .forEach(id => parts.push(block(id)));

  // Voice examples (только первое сообщение)
  if (stageMeta.msg_count === 0) parts.push(block('VE-01'));

  // TOOLS (только при наличии доступа)
  if (toolAccess.length > 0) {
    toolAccess.forEach(t => parts.push(block(`T_${t.toUpperCase()}`)));
    parts.push(block('T-05'));
  }

  // CHECKLIST (всегда последним)
  parts.push(block('G-06'));

  return parts.filter(Boolean).join('\n\n');
}
```

---

### Как Data Selector читает маппинг

Data Selector тоже смотрит на `dialog_intent` — для решения, какой срез данных нужен:

```javascript
// Специальные срезы данных по интенту
const intentDataRules = {
  'cross_scenario_question': (base, comps) => ({
    ...base,
    cross_scenario: crossScenarioData(comps)  // добавить данные стока и specs
  }),
  'answer_question_no_json': (base) => ({
    ...base,
    // Каталог не нужен — СЕКРЕТАРЬ не смотрит на данные
  }),
  'tool_result_received': (base, ctx) => ({
    ...base,
    tool_result: ctx.last_tool_result  // добавить результат инструмента
  }),
  'competitor_comparison': (base) => ({
    ...base,
    // Только базовые цены для сравнения
  }),
};

// Применяем правило если есть, иначе дефолтный срез по currentStage
const baseContext = stageDataMap[currentStage]();
const aiContext = intentDataRules[dialogIntent]
  ? intentDataRules[dialogIntent](baseContext, rawComps, ctx)
  : baseContext;
```

**Важно:** `intentDataRules` тоже должны переехать в NocoDB в следующей итерации. На v3.0 — хардкод в ноде, но структура готова к миграции.

---

### Как Context Resolver загружает список интентов динамически

```javascript
// При старте воркфлоу (нода "Load Caches")
const intentsForPrompt = INTENTS_CACHE
  .filter(i => i.Active && i.Version === activeVersion)
  .sort((a, b) => b.Priority - a.Priority)
  .map(i => `${i.Intent_ID} | ${i.Description} | Примеры: ${i.Example_Phrases}`)
  .join('\n');

// Этот текст подставляется в [INTENT_LIST] секцию промпта Resolver'а
RESOLVER_INTENT_LIST = intentsForPrompt;
```

Таким образом: добавил строку в таблицу `Intents` → при следующем рестарте воркфлоу Resolver начинает распознавать новый интент. Нет правки промпта вручную, нет деплоя.

---

## Процедура добавления нового интента из практики

Когда в реальных диалогах обнаруживается поведение, которое текущие интенты не покрывают:

**Шаг 1 — Зафиксировать кейс.**
Сохранить реальный диалог в `Notes` нового интента. Это источник правды.

**Шаг 2 — Добавить строку в таблицу `Intents`.**
```
Intent_ID:       objection_already_visited       ← новый ID
Group:           E
Description:     Клиент говорит что уже был в салоне и ничего не понравилось
Example_Phrases: «я уже был;уже приезжал;не понравилось у вас»
Scenarios:       all
Source:          practice
Active:          true  ← сразу активируем или сначала test
```

**Шаг 3 — Решить: нужен новый промпт-блок или существующий подходит?**

- Если существующий подходит → добавить строку в `Intent_Block_Map`:
  ```
  Intent_ID: objection_already_visited
  Block_ID:  OBJ_UNIVERSAL  ← существующий блок
  Role:      primary
  ```
- Если нужна специальная тактика → создать новый блок в `Prompt_Blocks`:
  ```
  Block_ID:        ST_ALL_OBJECTION_ALREADY_VISITED
  Level:           stage_task
  Scenario:        all
  Stage:           all
  Intent_Triggers: objection_already_visited
  Content:         [текст блока с конкретной тактикой]
  ```
  И добавить в `Intent_Block_Map`:
  ```
  Intent_ID: objection_already_visited
  Block_ID:  ST_ALL_OBJECTION_ALREADY_VISITED
  Role:      primary
  ```

**Шаг 4 — Рестарт воркфлоу.**
Кэши перезагружаются. Новый интент активен.

**Шаг 5 — Мониторинг.**
В поле `reasoning` в ответах Resolver'а отслеживать, правильно ли он распознаёт новый интент. При ошибках — уточнить `Description` и `Example_Phrases` в таблице.

---

## Примеры полных ответов Context Resolver

### Пример 1: Клиент называет сомнительный взнос
```
Текущее: «ну тысяч 200 наверное, а можно меньше?»
Сценарий: credit, стадия: data_collect, взнос не собран
```
```json
{
  "dialog_intent":       "objection_price",
  "client_mode":         "advocate_tradein",
  "client_signal":       "hesitating",
  "key_data_extracted":  { "down_payment": 200000 },
  "reasoning":           "Клиент называет сумму, но сомневается — финансовый барьер, нужен АДВОКАТ"
}
```
→ Stage Classifier: `tool_needed` закрыт (взнос есть, но сомнение)
→ Data Selector: базовый срез credit
→ Prompt Compiler: `objection_price` → ST_CREDIT_DATA_COLLECT + OBJ_ADVOCATE_TRADEIN

---

### Пример 2: Вопрос из чужого сценария
```
Текущее: «а в синем есть в наличии?»
Сценарий: credit, стадия: data_collect, взнос = 300к (уже собран)
```
```json
{
  "dialog_intent":       "cross_scenario_question",
  "client_mode":         "reference",
  "client_signal":       "asking_more",
  "key_data_extracted":  {},
  "reasoning":           "Взнос назван, клиент уточняет наличие — ответить по cross_scenario"
}
```
→ Stage Classifier: `tool_needed` НЕ открывается (credit не требует инструмента)
→ Data Selector: добавляет `cross_scenario` к базовому срезу
→ Prompt Compiler: ST_CREDIT_DATA_COLLECT + D_CROSS_SCENARIO_CREDIT

---

### Пример 3: Клиент передаёт последний параметр
```
Текущее: «пробег примерно 85 тысяч»
Сценарий: tradein, plate=А123АА77, mileage=null (в Python)
```
```json
{
  "dialog_intent":       "data_received_complete",
  "client_mode":         "collector",
  "client_signal":       "moving_forward",
  "key_data_extracted":  { "mileage": 85000 },
  "reasoning":           "Пробег передан — оба параметра собраны, вызвать инструмент"
}
```
→ Stage Classifier: видит `data_received_complete` → открывает `evaluate_auto`
  (даже если Python ещё не обновил collected_data.mileage)
→ Data Selector: срез `tool_needed` с одной комплектацией
→ Prompt Compiler: ST_TRADEIN_TOOL_NEEDED

---

### Пример 4: Нелинейный возврат назад
```
Текущее: «подождите, а сколько без кредита будет стоить?»
Сценарий: credit, стадия: closing (по Python)
```
```json
{
  "dialog_intent":       "price_hierarchy_question",
  "client_mode":         "reference",
  "client_signal":       "asking_more",
  "key_data_extracted":  {},
  "reasoning":           "Клиент вернулся к вопросу о цене — не готов к закрытию"
}
```
→ Stage Classifier: `closing` подтверждён, `finalize_deal` открыт
→ Data Selector: срез с ценами (price_hierarchy_question требует base_price и MSRP)
→ Prompt Compiler: `price_hierarchy_question` → ST_CREDIT_DATA_COLLECT (СПРАВОЧНИК)
  вместо ST_CREDIT_CLOSING — несмотря на то что Python говорит closing

---

### Пример 5: Клиент называет дату
```
Текущее: «приеду в пятницу, 30-го»
Сценарий: любой, стадия: closing
```
```json
{
  "dialog_intent":       "schedule_appointment",
  "client_mode":         "direct_agree",
  "client_signal":       "scheduling",
  "key_data_extracted":  { "appointment_date": "30.05.2026", "appointment_type": "visit" },
  "reasoning":           "Клиент называет дату визита — зафиксировать и вызвать finalize(in_progress)"
}
```
→ Python: обновляет `appointment.date = "30.05.2026"` из key_data_extracted
→ Prompt Compiler: ST_*_CLOSING + инструкция finalize с appointment_date

---

### Пример 6: Уклонение от телефона третий раз
```
Текущее: «я подумаю, позже»
phone_ask_count: 2
```
```json
{
  "dialog_intent":       "pause_requested",
  "client_mode":         "stalling",
  "client_signal":       "refusing_softly",
  "key_data_extracted":  {},
  "reasoning":           "Телефон запрашивался дважды, снова уклоняется — пауза обязательна"
}
```
→ Prompt Compiler: OBJ_UNIVERSAL с флагом `phone_ask_count=2` → предложить паузу

---

## Влияние dialog_intent на Stage Classifier

После того как Resolver встаёт первым — Stage Classifier получает `dialog_intent` и использует его для более точных решений:

```javascript
// Расширенный Stage Classifier — теперь знает dialog_intent
const dialogIntent = input.dialog_intent; // от Context Resolver

// data_received_complete → открыть инструменты даже если Python ещё не обновил collected_data
if (dialogIntent === 'data_received_complete') {
  resolvedStage = 'tool_needed';  // переопределить стейдж Python
}

// final_refusal или ban_trigger → немедленный Bypass, не запускать Data Selector
if (['final_refusal', 'ban_trigger'].includes(dialogIntent)) {
  resolvedStage = dialogIntent === 'ban_trigger' ? 'ban' : 'objection_final';
}

// schedule_appointment → открыть finalize_deal (даже если stage ещё data_collect)
if (dialogIntent === 'schedule_appointment') {
  toolAccess.push('finalize_deal');
}
```

---

## Стоимость ноды

| Параметр | Значение |
|---|---|
| Модель | Gemini 2.5 Flash Lite |
| Токены на вход | ~350–450 (история + контекст + динамический список интентов) |
| Токены на выход | ~80–120 (JSON) |
| Итого за запрос | ~550 токенов |
| Vs. основной агент | ~15% от стоимости |
| Задержка | ~300–500 мс |

**Рост числа интентов → рост токенов на вход.** При 32 интентах список занимает ~200 токенов. При 100 интентах — ~600 токенов. Это управляемый рост: даже при 200 интентах общая стоимость ноды не превысит 25% от основного агента. При необходимости — фильтровать список по `Scenario` и `Stage_Context` перед подстановкой.

---

## Что НЕ делает Context Resolver

- Не видит JSON каталога — только текст диалога
- Не принимает решений об инструментах — это Stage Classifier
- Не генерирует ответ клиенту — только классифицирует
- Не хранит состояние — stateless, каждый вызов независим
- Не является агентом — Basic LLM Chain, нет tools, нет памяти
- Не правится при добавлении нового интента — только таблица NocoDB
