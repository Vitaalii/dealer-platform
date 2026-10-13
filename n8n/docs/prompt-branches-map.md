# Карта диалоговых веток Александра

Разведочный документ. Снят с фактических данных 2026-08-13, ничего не менялось.

**Источники (всё остальное — производное от них):**

| Что | Откуда снято | Объём |
|---|---|---|
| Блоки промпта | живая таблица NocoDB `Prompt_Blocks` | 118 строк (93 активных) |
| Интенты | живая таблица `Intents` | 38 строк (все активные) |
| Карта интент→блок | живая таблица `Intent_Block_Map` | 191 строка, из них 140 активных |
| Логика стадий | нода `Stage Classifier` в `flow/AI Agent v3.0.json` | — |
| Логика сборки | нода `Prompt Compiler` там же | — |
| Чтение таблиц | ноды `Get Blocks` / `Get Map` (NocoDB, `returnAll`) | — |

**Расхождения с документацией — истиной считаются данные:**

| Документация утверждает | Факт |
|---|---|
| `nocodb/prompt_blocks.csv` — 52 блока | в живой таблице 118; CSV — снапшот от июля, для анализа непригоден |
| `docs/prompt-blocks-mapping.md` описывает нарезку legacy-блоков | два блока (`ST_STOCK_TOOL_NEEDED`, `ST_CONSULT_TOOL_NEEDED`) до сих пор содержат текст-заглушку «[TODO владелец контента: вырезать из WF_*_LEGACY…]» и выключены |
| Колонки `Level` и `Stage` у блока управляют подключением | **компилятор их не читает вообще.** Подключение определяется только `Block_ID`, `Active`, `Intent_Triggers` и `Scenario`. `Level`/`Stage` — справочные поля для человека |

---

## 0. Как читать документ

Клиент приходит в один из **пяти сценариев** (`credit`, `tradein`, `combo`, `stock`, `consult`) — сценарий задаёт бот, n8n его не выбирает. Внутри сценария клиент находится на одной из **четырёх стадий** (`greeting`, `data_collect`, `tool_needed`, `closing`). На каждом ходе отдельная LLM-модель («резолвер») определяет **интент** — что клиент сделал этим сообщением.

Из тройки «сценарий + стадия + интент» собирается системный промпт: несколько текстовых блоков склеиваются в один текст. Блоки лежат в таблице NocoDB и правятся владельцем контента без участия кода.

Пометка **✗ мёртвый** означает: блок помечен активным, но при текущей логике сборки не попадает в промпт ни при одном сочетании сценария, стадии и интента (проверено полным перебором).

---

## 1. Инвентарь блоков

93 блока активны, 25 выключены. Из активных **84 достижимы, 9 мёртвых**.

### 1.1. Общие блоки (`Scenario = all`)

| Block_ID | Level | Stage | Version | Active | Tokens | Intent_Triggers | Что делает |
|---|---|---|---|---|---|---|---|
| `G-01_ROLE` | general | all | v3.00 | ✔ | 121 | — | Роль Александра, паспортные данные салона (адрес, график) |
| `G-02_VOICE` | voice | all | v3.00 | ✘ | 841 | — | Старые правила тона; вытеснен `_V2` |
| `G-02_VOICE_V2` | voice | all | v3.01 | ✔ | 508 | — | Тон и форма: лимит 2–3 предложения, зеркальность, запреты |
| `G-03_GOAL` | general | all | v3.00 | ✔ | 46 | — | Финальная цель: продать визит либо получить телефон |
| `G-04_FORMAT` | general | all | v3.00 | ✔ | 82 | — | Тег `[SPLIT]` для разбивки ответа на два сообщения |
| `G-05_DEFENSE` | general | all | v3.00 | ✔ | 209 | `off_topic` | Защита от вопросов не по теме и промпт-инъекций; при агрессии — `finalize_deal` со статусом `ban` |
| `G-06_CHECKLIST` | general | all | v3.00 | ✔ | 195 | — | Само-проверка перед отправкой (имя, телефон, формат) |
| `G-07_CABINET` | general | all | v3.06 | ✘ | 257 | — | Личный кабинет: когда предлагать ссылку (черновик, не активирован) |
| `T-01_TOOLS` | tools | all | v3.00 | ✘ | 757 | — | Старое описание `finalize_deal`; вытеснен `T_FINALIZE` |
| `T_FINALIZE` | tools | closing | v3.01 | ✔ | 757 | — | Правила вызова `finalize_deal` (когда и с каким статусом) |
| `BYPASS_REFUSE` | general | all | v3.00 | ✔ | 27 | — | ✗ Не в промпте. Готовый текст прощания при отказе клиента — читается нодой `Bypass Build` |
| `BYPASS_BAN` | general | all | v3.00 | ✔ | 7 | — | ✗ Не в промпте. Готовый текст при бан-триггере — читается нодой `Bypass Build` |
| `BLK_EVAL_REUSE` | tools | all | v3.06 | ✘ | 174 | 5 tool-интентов | Запрет повторного платного вызова оценки (черновик, `Scenario = tradein,combo`) |

### 1.2. Сценарий `credit` (25 блоков)

| Block_ID | Level | Stage | Ver | Active | Tok | Что делает |
|---|---|---|---|---|---|---|
| `D_CREDIT` | data_source | all | v3.00 | ✔ | 337 | Описание переменных JSON, запрет выдумывать цены и считать самому |
| `CS_CREDIT` | general | all | v3.00 | ✔ | 243 | ✗ **мёртвый** — вытеснен `_V2`. Правила поведения: после взноса СРАЗУ визит |
| `CS_CREDIT_V2` | general | all | v3.03 | ✔ | 272 | Правила поведения: расчёт сделан на старте, сначала вопрос комфортности |
| `CS_CREDIT_LEGACY` | general | all | v3.00 | ✘ | 809 | Монолитные правила до нарезки v3.00 |
| `PRICES_CREDIT` | general | all | v3.00 | ✔ | 57 | Иерархия цен: base_price → credit_price |
| `ST_CREDIT_GREETING` | stage_task | greeting | v3.00 | ✘ | 485 | Старый шаблон первого сообщения |
| `ST_CREDIT_GREETING_V2` | stage_task | greeting | v3.04 | ✔ | 671 | Первое сообщение: правила подстановки имени, расчёт, вопрос комфортности |
| `ST_CREDIT_DATA_COLLECT` | stage_task | data_collect | v3.00 | ✔ | 29 | Взнос отдельно не собирать; цель — ответ о комфортности |
| `ST_CREDIT_REQUIRED_DATA` | stage_task | data_collect | v3.00 | ✔ | 211 | Что нужно собрать за диалог; финальный артефакт — телефон |
| `ST_CREDIT_TOOL_NEEDED` | stage_task | tool_needed | v3.00 | ✔ | 112 | Запрет перерасчёта в чате после расчёта на старте |
| `ST_CREDIT_CLOSING` | stage_task | closing | v3.00 | ✔ | 164 | Шаблоны закрытия: согласие на визит, дата, звонок, `finalize_deal` |
| `BLK_CREDIT_PAYMENT_ACCEPTED` | stage_task | all | v3.00 | ✔ | 245 | ✗ **мёртвый** — режим «Сборщик»: взнос назван → СРАЗУ визит |
| `BLK_CREDIT_PAYMENT_ACCEPTED_V2` | stage_task | all | v3.03 | ✔ | 231 | Режим «Сборщик» (фолбэк): клиент сам назвал сумму → вопрос комфортности |
| `BLK_CREDIT_APPROVAL_BRIDGE` | stage_task | all | v3.03 | ✔ | 197 | Мост «Одобрение»: взнос комфортен → спросить про одобрение банка |
| `BLK_CREDIT_ADVOCATE` | stage_task | all | v3.00 | ✔ | 0 | Режим «Адвокат»: финансовый барьер → трейд-ин или спецпрограмма |
| `BLK_CREDIT_REFERENCE` | stage_task | all | v3.00 | ✔ | 161 | Режим «Справочник»: вопросы, ответ на которые есть в JSON |
| `BLK_CREDIT_SECRETARY` | stage_task | all | v3.00 | ✔ | 0 | Режим «Секретарь»: данных нет в JSON → повод продать визит |
| `BLK_CREDIT_TRADEIN_COLLECT` | stage_task | all | v3.00 | ✔ | 0 | Переход из кредита в оценку авто: запросить госномер и пробег |
| `BLK_CREDIT_APPOINTMENT` | stage_task | all | v3.00 | ✔ | 0 | Клиент назвал дату визита → адрес, график, просьба о телефоне |
| `BLK_CREDIT_VISIT_DECLINED` | stage_task | all | v3.00 | ✔ | 0 | Отказ от визита → дистанционный звонок эксперта |
| `BLK_CREDIT_NO_PHONE` | objections | all | v3.00 | ✔ | 269 | Первый отказ дать телефон |
| `BLK_CREDIT_NO_PHONE_REPEAT` | objections | all | v3.00 | ✔ | 161 | Повторный отказ → снять давление, предложить паузу |
| `BLK_CREDIT_PAUSE` | objections | all | v3.00 | ✔ | 117 | Клиент просит паузу |
| `OBJ_CREDIT` | objections | all | v3.00 | ✘ | 783 | Монолит возражений до нарезки на `BLK_*` |
| `WF_CREDIT_LEGACY` | stage_task | all | v3.00 | ✘ | 1351 | Пошаговый алгоритм до перехода на стадии |

### 1.3. Сценарий `tradein` (26 блоков)

| Block_ID | Level | Stage | Ver | Active | Tok | Что делает |
|---|---|---|---|---|---|---|
| `D_TRADEIN` | data_source | all | v3.00 | ✔ | 335 | Переменные JSON, запрет выдумывать стоимость выкупа |
| `CS_TRADEIN` | general | all | v3.00 | ✔ | 200 | ✗ **мёртвый** — вытеснен `_V2`. После оценки СРАЗУ визит |
| `CS_TRADEIN_V2` | general | all | v3.03 | ✔ | 226 | После оценки СНАЧАЛА вопрос «соответствует ожиданиям?» |
| `CS_TRADEIN_LEGACY` | general | all | v3.00 | ✘ | 794 | Монолитные правила |
| `PRICES_TRADEIN` | general | all | v3.00 | ✔ | 58 | Иерархия цен: base_price → tradein_price |
| `ST_TRADEIN_GREETING` | stage_task | greeting | v3.00 | ✘ | 419 | Старый шаблон первого сообщения |
| `ST_TRADEIN_GREETING_V2` | stage_task | greeting | v3.04 | ✔ | 604 | Первое сообщение; ветвление по наличию госномера в JSON |
| `ST_TRADEIN_DATA_COLLECT` | stage_task | data_collect | v3.00 | ✔ | 143 | Сбор госномера и пробега, переход к вызову инструмента |
| `ST_TRADEIN_REQUIRED_DATA` | stage_task | data_collect | v3.00 | ✔ | 166 | Те же два параметра + финальный артефакт «телефон» |
| `ST_TRADEIN_TOOL_NEEDED` | stage_task | tool_needed | v3.00 | ✔ | 261 | ✗ **мёртвый** — вытеснен `_V2`. Вызов `evaluate_auto`, шаблон ответа без need-check |
| `ST_TRADEIN_TOOL_NEEDED_V2` | stage_task | tool_needed | v3.03 | ✔ | 245 | Вызов `evaluate_auto` + шаблон с вопросом «соответствует ожиданиям?» |
| `ST_TRADEIN_CLOSING` | stage_task | closing | v3.00 | ✔ | 164 | Шаблоны закрытия и `finalize_deal` |
| `BLK_TRADEIN_TESTDRIVE_BRIDGE` | stage_task | all | v3.03 | ✔ | 169 | Мост «Тест-драйв»: оценка устроила → спросить про тест-драйв |
| `BLK_TRADEIN_VALUE_DEFENSE` | stage_task | all | v3.03 | ✔ | 182 | Защита оценки: цифра — средняя по прошлым сделкам |
| `BLK_TRADEIN_ADVOCATE` | stage_task | all | v3.00 | ✔ | 152 | Клиент уходит от госномера («зачем вам номер», «боюсь спама») |
| `BLK_TRADEIN_SKEPTIC` | stage_task | all | v3.00 | ✔ | 143 | Сомнения в цифрах → легализовать технологию |
| `BLK_TRADEIN_REFERENCE` | stage_task | all | v3.00 | ✔ | 156 | Вопросы про новое авто, ответ есть в JSON |
| `BLK_TRADEIN_SECRETARY` | stage_task | all | v3.00 | ✔ | 114 | Данных нет в JSON |
| `BLK_TRADEIN_PRICE_COMPARE` | stage_task | all | v3.00 | ✔ | 170 | Сравнение с другими дилерами/маркетплейсами |
| `BLK_TRADEIN_APPOINTMENT` | stage_task | all | v3.00 | ✔ | 68 | Названа дата визита |
| `BLK_TRADEIN_VISIT_DECLINED` | stage_task | all | v3.00 | ✔ | 121 | Логистический тупик: далеко/нет времени |
| `BLK_TRADEIN_NO_PHONE` | objections | all | v3.00 | ✔ | 295 | Первый отказ дать телефон |
| `BLK_TRADEIN_NO_PHONE_REPEAT` | objections | all | v3.00 | ✔ | 148 | Повторный отказ |
| `BLK_TRADEIN_PAUSE` | objections | all | v3.00 | ✔ | 106 | Просьба о паузе |
| `OBJ_TRADEIN` | objections | all | v3.00 | ✘ | 796 | Монолит возражений |
| `WF_TRADEIN_LEGACY` | stage_task | all | v3.00 | ✘ | 1991 | Пошаговый алгоритм оценки |

### 1.4. Сценарий `combo` (25 блоков)

Структура зеркалит `tradein`; отличия отмечены.

| Block_ID | Level | Stage | Ver | Active | Tok | Что делает |
|---|---|---|---|---|---|---|
| `D_COMBO` | data_source | all | v3.00 | ✔ | 449 | Переменные JSON обеих половин сделки |
| `CS_COMBO` | general | all | v3.00 | ✔ | 236 | ✗ **мёртвый** — вытеснен `_V2` |
| `CS_COMBO_V2` | general | all | v3.03 | ✔ | 284 | Двухступенчатая проверка: сначала оценка, потом платёж |
| `CS_COMBO_LEGACY` | general | all | v3.00 | ✘ | 845 | Монолитные правила |
| `PRICES_COMBO` | general | all | v3.00 | ✔ | 79 | Иерархия цен при совмещении программ |
| `ST_COMBO_GREETING` | stage_task | greeting | v3.00 | ✘ | 370 | Старый шаблон |
| `ST_COMBO_GREETING_V2` | stage_task | greeting | v3.04 | ✔ | 556 | Первое сообщение |
| `ST_COMBO_DATA_COLLECT` | stage_task | data_collect | v3.00 | ✔ | 143 | Сбор госномера и пробега |
| `ST_COMBO_REQUIRED_DATA` | stage_task | data_collect | v3.00 | ✔ | 179 | Параметры + финальный артефакт |
| `ST_COMBO_TOOL_NEEDED` | stage_task | tool_needed | v3.00 | ✔ | 403 | ✗ **мёртвый** — вытеснен `_V2` |
| `ST_COMBO_TOOL_NEEDED_V2` | stage_task | tool_needed | v3.03 | ✔ | 377 | Вызов `combo_calculator` + шаблон с проверочным вопросом |
| `ST_COMBO_CLOSING` | stage_task | closing | v3.00 | ✔ | 164 | Шаблоны закрытия |
| `BLK_COMBO_NEEDCHECK_ROUTER` | stage_task | all | v3.03 | ✔ | 254 | **Уникален для combo.** Определяет, какой из двух барьеров разбирать первым |
| `BLK_COMBO_ADVOCATE` | stage_task | all | v3.00 | ✔ | 152 | Барьер госномера |
| `BLK_COMBO_SKEPTIC` | stage_task | all | v3.00 | ✔ | 143 | Сомнения в цифрах |
| `BLK_COMBO_REFERENCE` | stage_task | all | v3.00 | ✔ | 156 | Справочные вопросы |
| `BLK_COMBO_SECRETARY` | stage_task | all | v3.00 | ✔ | 114 | Данных нет в JSON |
| `BLK_COMBO_PRICE_COMPARE` | stage_task | all | v3.00 | ✔ | 170 | Ценовое сравнение |
| `BLK_COMBO_APPOINTMENT` | stage_task | all | v3.00 | ✔ | 68 | Названа дата визита |
| `BLK_COMBO_VISIT_DECLINED` | stage_task | all | v3.00 | ✔ | 121 | Логистический тупик |
| `BLK_COMBO_NO_PHONE` | objections | all | v3.00 | ✔ | 285 | Первый отказ |
| `BLK_COMBO_NO_PHONE_REPEAT` | objections | all | v3.00 | ✔ | 148 | Повторный отказ |
| `BLK_COMBO_PAUSE` | objections | all | v3.00 | ✔ | 106 | Пауза |
| `OBJ_COMBO` | objections | all | v3.00 | ✘ | 785 | Монолит возражений |
| `WF_COMBO_LEGACY` | stage_task | all | v3.00 | ✘ | 2170 | Пошаговый алгоритм |

### 1.5. Сценарий `stock` (15 блоков)

| Block_ID | Level | Stage | Ver | Active | Tok | Что делает |
|---|---|---|---|---|---|---|
| `D_STOCK` | data_source | all | v3.00 | ✔ | 267 | Переменные JSON склада |
| `CS_STOCK` | general | all | v3.00 | ✔ | 176 | Компетенция: наличие, цвета, цена за наличные |
| `CS_STOCK_LEGACY` | general | all | v3.00 | ✘ | 486 | Монолитные правила |
| `ST_STOCK_GREETING` | stage_task | greeting | v3.00 | ✔ | 489 | ✗ **мёртвый** — вытеснен `_V2`. При отсутствии авто сразу переключает на менеджера |
| `ST_STOCK_GREETING_V2` | stage_task | greeting | v3.03 | ✔ | 690 | То же + вопрос о готовности ждать поставку вместо менеджера |
| `ST_STOCK_DATA_COLLECT` | stage_task | data_collect | v3.00 | ✔ | 209 | Отработка выбора, переход к тест-драйву |
| `ST_STOCK_REQUIRED_DATA` | stage_task | data_collect | v3.00 | ✔ | 143 | Квалификация + финальный артефакт |
| `ST_STOCK_TOOL_NEEDED` | stage_task | tool_needed | v3.00 | ✘ | 29 | **Заглушка** «[TODO владелец контента…]», не заполнена |
| `ST_STOCK_CLOSING` | stage_task | closing | v3.00 | ✔ | 183 | Шаблоны закрытия |
| `BLK_STOCK_NEED_CHECK` | stage_task | all | v3.03 | ✔ | 145 | Модели нет в наличии → вопрос о готовности ждать |
| `BLK_STOCK_NO_PHONE` | objections | all | v3.00 | ✔ | 233 | Первый отказ |
| `BLK_STOCK_NO_PHONE_REPEAT` | objections | all | v3.00 | ✔ | 69 | Второй отказ → `finalize_deal` со статусом refuse |
| `BLK_STOCK_PAUSE` | objections | all | v3.00 | ✔ | 106 | Пауза |
| `OBJ_STOCK` | objections | all | v3.00 | ✘ | 359 | Монолит возражений |
| `WF_STOCK_LEGACY` | stage_task | all | v3.00 | ✘ | 533 | Пошаговый алгоритм |

### 1.6. Сценарий `consult` (14 блоков)

| Block_ID | Level | Stage | Ver | Active | Tok | Что делает |
|---|---|---|---|---|---|---|
| `D_CONSULT` | data_source | all | v3.00 | ✔ | 212 | Переменные JSON |
| `CS_CONSULT` | general | all | v3.00 | ✔ | 251 | Компетенция: справочник по одной модели |
| `CS_CONSULT_LEGACY` | general | all | v3.00 | ✘ | 665 | Монолитные правила |
| `ST_CONSULT_GREETING` | stage_task | greeting | v3.00 | ✘ | 215 | Старый шаблон |
| `ST_CONSULT_GREETING_V2` | stage_task | greeting | v3.04 | ✔ | 400 | Первое сообщение |
| `ST_CONSULT_DATA_COLLECT` | stage_task | data_collect | v3.00 | ✔ | 221 | Вопросы в зоне компетенции и вне её |
| `ST_CONSULT_REQUIRED_DATA` | stage_task | data_collect | v3.00 | ✔ | 178 | Квалификация интереса |
| `ST_CONSULT_TOOL_NEEDED` | stage_task | tool_needed | v3.00 | ✘ | 29 | **Заглушка** «[TODO владелец контента…]» |
| `ST_CONSULT_CLOSING` | stage_task | closing | v3.00 | ✔ | 99 | Шаблоны закрытия |
| `BLK_CONSULT_NO_PHONE` | objections | all | v3.00 | ✔ | 209 | Первый отказ |
| `BLK_CONSULT_NO_PHONE_REPEAT` | objections | all | v3.00 | ✔ | 84 | Второй отказ → `finalize_deal` |
| `BLK_CONSULT_PAUSE` | objections | all | v3.00 | ✔ | 106 | Пауза |
| `OBJ_CONSULT` | objections | all | v3.00 | ✘ | 465 | Монолит возражений |
| `WF_CONSULT_LEGACY` | stage_task | all | v3.00 | ✘ | 325 | Пошаговый алгоритм |

**Замечание по `consult`:** у сценария нет ни одного инструмента и ни одного `BLK_*`-режима — только четыре стадийных блока и три блока возражений. Это самый бедный сценарий.

---

## 2. Машина стадий

### 2.1. Главное, что нужно понять

**Стадию считает не n8n, а бот.** Нода `Stage Classifier` берёт готовое значение `session_context.current_stage`, пришедшее в вебхуке, и только **переопределяет** его по интенту. Как бот вычисляет `current_stage` — по этим данным **не установлено** (код бота вне репозитория).

Дефолт при отсутствии значения — `data_collect`.

### 2.2. Правила переопределения (нода `Stage Classifier`, порядок сверху вниз)

| № | Условие | Результат |
|---|---|---|
| 1 | `verdict` резолвера не `ok`/`preset` | интент пустой; стадия остаётся от бота |
| 2 | `flags.stop_word_detected` **или** интент `final_refusal` | `bypass = refuse` — ход уходит мимо промпта целиком |
| 3 | интент `ban_trigger` | `bypass = ban` — то же |
| 4 | телефон в сообщении **или** телефон собран и сделка не финализирована | `finalize_required = true`, интент **принудительно** меняется на `finalize_agreed` |
| 5 | интент ∈ `data_received_complete`, `ready_for_tool`, `tool_rerun_needed`, `tool_error_handling` | стадия → `tool_needed` |
| 6 | интент ∈ `tool_result_received`, `propose_visit`, `propose_call`, `request_phone`, `finalize_agreed`, `schedule_appointment`, `competitor_comparison`, `polite_close_satisfied` | стадия → `closing` |
| 7 | `msg_count == 0` и не `finalize_required` | стадия → `greeting` |
| 8 | интент `greeting_first` и не `finalize_required` | стадия → `greeting` |

Дополнительно вычисляется `tools_open = стадия ∈ {tool_needed, closing}` или `finalize_required`.

### 2.3. Порядок стадий по сценариям

Порядок одинаков во всех пяти сценариях — он не задан таблицей, а вытекает из правил выше:

```
greeting  →  data_collect  →  tool_needed  →  closing
   ↑             ↑                              │
   └─ msg_count=0 / greeting_first              │
                 └──────────── возврат назад невозможен по правилам 5–8,
                               но стадия может «откатиться», если бот
                               прислал прежнее current_stage
```

| Сценарий | greeting | data_collect | tool_needed | closing | Инструмент стадии tool_needed |
|---|---|---|---|---|---|
| `credit` | ✔ `_V2` | ✔ | ✔ | ✔ | нет (расчёт сделан ботом на старте) |
| `tradein` | ✔ `_V2` | ✔ | ✔ | ✔ | `evaluate_auto` |
| `combo` | ✔ `_V2` | ✔ | ✔ | ✔ | `combo_calculator` |
| `stock` | ✔ `_V2` | ✔ | **заглушка выключена** | ✔ | нет |
| `consult` | ✔ `_V2` | ✔ | **заглушка выключена** | ✔ | нет |

**Следствие для `stock` и `consult`:** если клиент попадёт на стадию `tool_needed` (интенты `data_received_complete` и `tool_rerun_needed` разрешены в этих сценариях по таблице интентов), профильного блока не найдётся. Компилятор уйдёт в фолбэк `WF_STOCK_LEGACY` / `WF_CONSULT_LEGACY`, а они **выключены** — значит основной блок хода окажется пустым, и промпт составится из одних общих правил. Проверено перебором: `stock/tool_needed` и `consult/tool_needed` собираются из 7 блоков вместо 8.

---

## 3. Сборка промпта

### 3.1. Три ноды

| Нода | Что делает |
|---|---|
| `Get Blocks` | читает таблицу `Prompt_Blocks` **целиком**, без фильтров (`returnAll`) |
| `Get Map` | читает `Intent_Block_Map` целиком |
| `Prompt Compiler` | Code-нода: фильтрует прочитанное и склеивает тексты в один системный промпт |

Фильтрация целиком на стороне JS — база отдаёт всё, что есть.

### 3.2. Порядок склейки (точный, из кода ноды)

| № | Что добавляется | Условие |
|---|---|---|
| 1 | `G-01_ROLE` | Active |
| 2 | `G-02_VOICE_V2`, иначе `G-02_VOICE` | Active |
| 3 | `G-03_GOAL` | Active |
| 4 | `G-04_FORMAT` | Active |
| 5 | `G-07_CABINET` | Active (сейчас выключен) |
| 6 | `CS_{СЦЕНАРИЙ}` — с предпочтением `_V2` | Active |
| 7 | `D_{СЦЕНАРИЙ}` — **без** предпочтения `_V2` | Active |
| 8 | **основной блок хода** (см. 3.3) | всегда один |
| 9 | все `additional`-блоки из карты для этого интента | фильтры по сценарию и стадии |
| 10 | любые блоки, у которых `Intent_Triggers` содержит текущий интент и `Scenario` совпадает | дубли текста отсекаются |
| 11 | `T_FINALIZE`, иначе `T-01_TOOLS` | `T_FINALIZE` — только на `closing` либо при `finalize_required`; `T-01_TOOLS` — при `tools_open` (сейчас выключен) |
| 12 | `G-06_CHECKLIST` | Active |
| 13 | блок `<DATA_FROM_DATABASE>` с JSON контекста | всегда |
| 14 | напоминание «ТЫ ОБЯЗАН ВЫЗВАТЬ FINALIZE_DEAL» | при `finalize_required` |

Разделитель между блоками — пустая строка.

### 3.3. Как выбирается основной блок хода

1. Строка карты с `Role = primary` для этого интента, прошедшая фильтры сценария и стадии.
2. Если такой нет — блок по имени `ST_{СЦЕНАРИЙ}_{СТАДИЯ}` с предпочтением `_V2`.
3. Если и он неактивен — `WF_{СЦЕНАРИЙ}_LEGACY` (все пять выключены, то есть фактически пусто).

### 3.4. Блоки, которые НЕ подключаются автоматически

Это ответ на прямой вопрос. Подключения «по уровню» (`Level`) компилятор **не делает вообще** — колонка `Level` в коде не используется. Ни один блок не попадает в промпт «потому что он general» или «потому что он objections».

Существует ровно **четыре** способа попасть в промпт:

| Способ | Кто так подключается | Что будет с новым блоком |
|---|---|---|
| **A. Жёсткий список в коде** | `G-01_ROLE`, `G-02_VOICE(_V2)`, `G-03_GOAL`, `G-04_FORMAT`, `G-07_CABINET`, `G-06_CHECKLIST`, `T_FINALIZE`, `T-01_TOOLS` | **не попадёт никогда** без правки `nocodb/build_v3_workflow.py` и переимпорта воркфлоу |
| **B. Имя по шаблону** | `CS_{СЦЕН}`, `D_{СЦЕН}`, `ST_{СЦЕН}_{СТАДИЯ}`, `WF_{СЦЕН}_LEGACY` | попадёт, только если Block_ID точно совпал с шаблоном |
| **C. Строка в `Intent_Block_Map`** | все `BLK_*`, `PRICES_*`, стадийные блоки как `primary` | попадёт при активной строке карты |
| **D. `Intent_Triggers` у самого блока** | `G-05_DEFENSE`, `BLK_EVAL_REUSE`, пять `OBJ_*` | попадёт без правок кода — самый дешёвый способ |

**Случай `G-07_CABINET`.** Это блок способа A: он был добавлен в жёсткий список правкой кода. Если бы его завели только в таблице, он не попал бы в промпт ни при каких настройках `Level`, `Scenario` и `Stage` — потому что этих колонок компилятор не читает.

**Ограничение механизма `_V2`.** Предпочтение черновика применяется только к пунктам 6 и 8 списка выше (`CS_{СЦЕН}` и дефолтный `ST_{СЦЕН}_{СТАДИЯ}`). Оно **не** работает для `D_{СЦЕН}`, для блоков из жёсткого списка и для блоков, выбранных картой. Суффикс распознаётся ровно один — `_V2`; `_V3` не существует.

### 3.5. Фактический объём промпта

Оценка по `Tokens_est`, без `<DATA_FROM_DATABASE>` (он добавляет от 500 до 3000 токенов в зависимости от каталога).

| Сценарий | greeting | data_collect | tool_needed | closing |
|---|---|---|---|---|
| `credit` | 2232 (8 блоков) | 1590 (8) | 1673 (8) | 2482 (9) |
| `tradein` | 2117 (8) | 1656 (8) | 1758 (8) | 2434 (9) |
| `combo` | 2241 (8) | 1828 (8) | 2062 (8) | 2606 (9) |
| `stock` | 2085 (8) | 1604 (8) | **1395 (7)** | 2335 (9) |
| `consult` | 1815 (8) | 1636 (8) | **1415 (7)** | 2271 (9) |

Худшая точка каждого сценария — `closing` + `objection_no_phone`: от 2480 (`consult`) до 2891 (`combo`) токенов, 10 блоков.

Пример состава — `tradein` / `tool_needed` / `ready_for_tool`:

| Блок | Токенов |
|---|---|
| `G-01_ROLE` | 121 |
| `G-02_VOICE_V2` | 508 |
| `G-03_GOAL` | 46 |
| `G-04_FORMAT` | 82 |
| `CS_TRADEIN_V2` | 226 |
| `D_TRADEIN` | 335 |
| `ST_TRADEIN_TOOL_NEEDED_V2` | 245 |
| `G-06_CHECKLIST` | 195 |
| **Итого** | **1758** |

Самый тяжёлый одиночный блок в любом промпте — `G-02_VOICE_V2` (508 токенов, 29% этого примера).

---

## 4. Интенты

### 4.1. Таблица интентов

Все 38 активны. `карт` — сколько активных строк карты ссылается на интент.

| Intent_ID | Гр. | Prio | Сценарии | Стадии | карт |
|---|---|---|---|---|---|
| `data_received_complete` | A | 20 | credit, tradein, combo | data_collect | 1 |
| `collect_down_payment` | A | 5 | credit, combo | data_collect | 5 |
| `collect_plate` | A | 5 | tradein, combo, credit | data_collect | 6 |
| `collect_mileage` | A | 5 | tradein, combo, credit | data_collect | 6 |
| `collect_plate_and_mileage` | A | 4 | tradein, combo, credit | data_collect | 6 |
| `competitor_comparison` | B | 10 | all | all | 10 |
| `price_hierarchy_question` | B | 9 | all | all | 6 |
| `answer_question_in_json` | B | 8 | all | all | 6 |
| `payment_method_change` | B | 8 | all | all | 3 |
| `cross_scenario_question` | B | 7 | all | data_collect, closing | **0** |
| `answer_question_no_json` | B | 6 | all | all | 3 |
| `ready_for_tool` | C | 18 | tradein, combo | data_collect, tool_needed | **0** |
| `tool_rerun_needed` | C | 16 | tradein, combo, credit | tool_needed, closing | 1 |
| `tool_result_received` | C | 15 | credit, tradein, combo | tool_needed | 8 |
| `tool_error_handling` | C | 15 | tradein, combo | tool_needed | 1 |
| `finalize_agreed` | D | 25 | all | closing | 5 |
| `schedule_appointment` | D | 14 | all | closing | 8 |
| `need_check_ok` | D | 13 | credit, tradein, combo, stock | tool_needed, closing | 4 |
| `need_check_not_ok` | D | 13 | credit, tradein, combo, stock | tool_needed, closing | 4 |
| `request_phone` | D | 12 | all | closing | 5 |
| `propose_call` | D | 9 | all | closing | 5 |
| `propose_visit` | D | 8 | all | closing | 5 |
| `polite_close_satisfied` | D | 7 | all | closing | 5 |
| `final_refusal` | E | 30 | all | all | **0** |
| `objection_price` | E | 10 | all | all | 1 |
| `objection_no_phone_repeat` | E | 9 | all | closing | 5 |
| `objection_data_privacy` | E | 9 | tradein, combo, credit | data_collect | 3 |
| `objection_no_car` | E | 9 | tradein, combo, credit | data_collect | 1 |
| `data_already_provided` | E | 9 | all | data_collect | 5 |
| `objection_no_phone` | E | 8 | all | closing | 5 |
| `objection_distance` | E | 8 | all | all | 3 |
| `objection_trust` | E | 8 | all | all | 2 |
| `pause_requested` | E | 7 | all | all | 5 |
| `greeting_first` | F | 30 | all | greeting | 1 |
| `ban_trigger` | F | 30 | all | all | **0** |
| `reminder_response` | F | 13 | all | all | **0** |
| `request_human_handoff` | F | 12 | all | all | 5 |
| `off_topic` | F | 6 | all | all | 1 |

### 4.2. Карта интент → блоки (только активные строки, 140 из 191)

| Интент | Role | Блоки (сценарий) |
|---|---|---|
| `answer_question_in_json` | additional | `BLK_{CREDIT,TRADEIN,COMBO}_REFERENCE`, `PRICES_{CREDIT,TRADEIN,COMBO}` |
| `answer_question_no_json` | additional | `BLK_{CREDIT,TRADEIN,COMBO}_SECRETARY` |
| `collect_down_payment` | additional | `ST_{все 5}_REQUIRED_DATA` |
| `collect_mileage` | additional | `ST_{все 5}_REQUIRED_DATA` + `BLK_CREDIT_TRADEIN_COLLECT` (credit) |
| `collect_plate` | additional | то же |
| `collect_plate_and_mileage` | additional | то же |
| `competitor_comparison` | primary + additional | `ST_{все 5}_CLOSING` + `BLK_{TRADEIN,COMBO}_PRICE_COMPARE` + `PRICES_*` |
| `data_already_provided` | primary | `ST_{все 5}_DATA_COLLECT` |
| `data_received_complete` | primary | **только** `BLK_CREDIT_PAYMENT_ACCEPTED_V2` (credit) |
| `finalize_agreed` | primary | `ST_{все 5}_CLOSING` |
| `greeting_first` | additional | `BLK_STOCK_NEED_CHECK` (stock) |
| `need_check_ok` | primary | `BLK_CREDIT_APPROVAL_BRIDGE`, `BLK_TRADEIN_TESTDRIVE_BRIDGE`, `BLK_COMBO_NEEDCHECK_ROUTER`, `ST_STOCK_DATA_COLLECT` |
| `need_check_not_ok` | primary | `BLK_CREDIT_ADVOCATE`, `BLK_TRADEIN_VALUE_DEFENSE`, `BLK_COMBO_NEEDCHECK_ROUTER`, `BLK_STOCK_NEED_CHECK` |
| `objection_data_privacy` | primary + additional | `BLK_{TRADEIN,COMBO}_ADVOCATE` + `BLK_TRADEIN_ADVOCATE` **в сценарии credit** |
| `objection_distance` | additional | `BLK_{CREDIT,TRADEIN,COMBO}_VISIT_DECLINED` |
| `objection_no_car` | additional | `BLK_CREDIT_ADVOCATE` (credit) |
| `objection_no_phone` | additional | `BLK_{все 5}_NO_PHONE` |
| `objection_no_phone_repeat` | additional | `BLK_{все 5}_NO_PHONE_REPEAT` |
| `objection_price` | primary | `BLK_CREDIT_ADVOCATE` (credit) |
| `objection_trust` | additional | `BLK_{TRADEIN,COMBO}_SKEPTIC` |
| `off_topic` | additional | `G-05_DEFENSE` |
| `pause_requested` | additional | `BLK_{все 5}_PAUSE` |
| `payment_method_change` | additional | `BLK_{CREDIT,TRADEIN,COMBO}_REFERENCE` |
| `polite_close_satisfied` | primary | `ST_{все 5}_CLOSING` |
| `price_hierarchy_question` | additional | `BLK_{...}_REFERENCE` + `PRICES_*` |
| `propose_call` / `propose_visit` | primary | `ST_{все 5}_CLOSING` |
| `request_human_handoff` | primary | `ST_{все 5}_CLOSING` |
| `request_phone` | primary | `ST_{все 5}_CLOSING` |
| `schedule_appointment` | primary + additional | `ST_{все 5}_CLOSING` + `BLK_{CREDIT,TRADEIN,COMBO}_APPOINTMENT` |
| `tool_error_handling` | primary | **только** `ST_CREDIT_TOOL_NEEDED` (credit) |
| `tool_rerun_needed` | primary | **только** `ST_CREDIT_TOOL_NEEDED` (credit) |
| `tool_result_received` | primary + additional | `ST_{все 5}_CLOSING` + `PRICES_*` |

### 4.3. Интенты без единой строки карты

| Интент | Что подключается вместо | Комментарий |
|---|---|---|
| `ready_for_tool` | дефолт стадии `ST_{СЦЕН}_TOOL_NEEDED_V2` | работает — ключевой рабочий интент трейд-ина и комбо держится на дефолте |
| `cross_scenario_question` | только дефолт стадии | профильного блока нет вовсе |
| `final_refusal` | — | до компилятора не доходит: уходит в `Bypass` |
| `ban_trigger` | — | то же |
| `reminder_response` | — | ход-напоминание идёт другой веткой (`Route → Reminder Chain`), промпт не собирается |

### 4.4. Блоки, на которые не ссылается ни одна активная строка карты и у которых нет триггеров

32 блока. Из них:

- **подключаются по имени и работают** — `G-01_ROLE`, `G-03_GOAL`, `G-04_FORMAT`, `G-06_CHECKLIST`, `G-02_VOICE_V2`, `T_FINALIZE`, все `D_{СЦЕН}`, все `CS_{СЦЕН}_V2`, все `ST_*_GREETING_V2`, `ST_{TRADEIN,COMBO}_TOOL_NEEDED_V2`, `CS_STOCK`, `CS_CONSULT`;
- **мёртвые** (9 штук, перечислены в 6.1);
- **читаются вне компилятора** — `BYPASS_REFUSE`, `BYPASS_BAN` (нода `Bypass Build`).

---

## 5. Вызовы инструментов

### 5.1. Какие инструменты подключены к агенту

| Инструмент | Имя ноды в воркфлоу | Имя в логах агента | Что делает |
|---|---|---|---|
| Финализация | `finalize_deal` | `finalize_deal` | сохраняет контакт в CRM, завершает сделку |
| Оценка авто | `evaluate_auto` | `evaluate_auto` | оценка трейд-ина по госномеру и пробегу |
| Комбо-расчёт | `Combo Calculator` | `Combo_Calculator` | оценка + подбор кредита одним вызовом |

**Расхождение имён.** Блоки `ST_COMBO_TOOL_NEEDED(_V2)` и `CS_COMBO(_V2)` называют инструмент `combo_calculator` (строчными, через подчёркивание), тогда как агент видит его как `Combo_Calculator`. Модель фактически вызывает инструмент верно — значит сопоставление работает, — но текст промпта и реальное имя не совпадают. У `evaluate_auto` имена совпадают точно.

### 5.2. Из какой стадии что вызывается

| Инструмент | Стадия | Чем открыт |
|---|---|---|
| `evaluate_auto` | `tool_needed` (tradein) | `ST_TRADEIN_TOOL_NEEDED_V2` — «немедленно и МОЛЧА вызови» |
| `Combo Calculator` | `tool_needed` (combo) | `ST_COMBO_TOOL_NEEDED_V2` — то же |
| `finalize_deal` | `closing` либо `finalize_required` | `T_FINALIZE` |

Формально инструменты доступны агенту на **любом** ходе — они привязаны к ноде агента, а не к стадии. Стадия управляет только тем, есть ли в промпте блок, предписывающий вызов.

### 5.3. Где предписано и где запрещено

**`evaluate_auto`**

| Блок | Active | Что говорит |
|---|---|---|
| `ST_TRADEIN_TOOL_NEEDED_V2` | ✔ | «Все параметры собраны (госномер + пробег)… немедленно и МОЛЧА вызови инструмент `evaluate_auto`» |
| `CS_TRADEIN_V2` | ✔ | «СТРОГО ЗАПРЕЩЕНО называть или предполагать любые цифры стоимости выкупа до ответа инструмента evaluate_auto» |
| `ST_TRADEIN_TOOL_NEEDED` | ✔ ✗мёртвый | тот же текст вызова, но шаблон ответа без проверочного вопроса |
| `CS_TRADEIN` | ✔ ✗мёртвый | тот же запрет |
| `BLK_EVAL_REUSE` | ✘ | «Если в JSON уже есть ti_valuation… НЕ вызывай инструмент оценки повторно» |
| `WF_TRADEIN_LEGACY`, `CS_TRADEIN_LEGACY` | ✘ | пошаговые версии тех же правил |

**`Combo Calculator`**

| Блок | Active | Что говорит |
|---|---|---|
| `ST_COMBO_TOOL_NEEDED_V2` | ✔ | «немедленно и МОЛЧА вызови инструмент `combo_calculator`» |
| `CS_COMBO_V2` | ✔ | «СТРОГО ЗАПРЕЩЕНО выдумывать или прогнозировать стоимость авто клиента и кредитные платежи до ответа инструмента combo_calculator» |
| `ST_COMBO_TOOL_NEEDED`, `CS_COMBO` | ✔ ✗мёртвые | те же формулировки |
| `BLK_EVAL_REUSE` | ✘ | запрет повторного вызова, включая «комбо-расчёт ради переоценки» |

**`finalize_deal`** — предписан в `T_FINALIZE` (✔), `G-05_DEFENSE` (✔, при агрессии со статусом `ban`), `G-06_CHECKLIST` (✔, при наличии телефона в базе), `BLK_STOCK_NO_PHONE_REPEAT` и `BLK_CONSULT_NO_PHONE_REPEAT` (✔, статус `refuse`). Выключенные носители тех же правил — `T-01_TOOLS`, все пять `OBJ_*`.

### 5.4. Противоречат ли места вызова друг другу

Между **активными и достижимыми** блоками прямых противоречий по вызову инструментов **нет**: правило вызова живёт ровно в одном блоке на сценарий (`ST_*_TOOL_NEEDED_V2`), запрет называть цифры до вызова — ровно в одном (`CS_*_V2`).

Противоречие возникает не между блоками, а между блоком и состоянием данных — см. конфликт К1 ниже.

Отдельно: правил, **ограничивающих количество** вызовов, среди активных блоков нет ни одного. Единственный такой блок (`BLK_EVAL_REUSE`) выключен.

---

## 6. Конфликты и дубли

### 6.1. Мёртвый груз: активные, но недостижимые блоки

Девять блоков помечены активными, редактируются владельцем и учитываются глазами при работе с таблицей — но в промпт не попадают никогда.

| Блок | Токенов | Почему мёртв |
|---|---|---|
| `CS_CREDIT` | 243 | вытеснен `CS_CREDIT_V2` механизмом `_V2` |
| `CS_TRADEIN` | 200 | вытеснен `CS_TRADEIN_V2` |
| `CS_COMBO` | 236 | вытеснен `CS_COMBO_V2` |
| `ST_TRADEIN_TOOL_NEEDED` | 261 | вытеснен `ST_TRADEIN_TOOL_NEEDED_V2` |
| `ST_COMBO_TOOL_NEEDED` | 403 | вытеснен `ST_COMBO_TOOL_NEEDED_V2` |
| `ST_STOCK_GREETING` | 489 | вытеснен `ST_STOCK_GREETING_V2` |
| `BLK_CREDIT_PAYMENT_ACCEPTED` | 245 | карта ссылается только на `_V2`-версию |
| `BYPASS_REFUSE` | 27 | читается нодой `Bypass Build`, не компилятором — это норма |
| `BYPASS_BAN` | 7 | то же |

Итого около **2100 токенов** контента, который выглядит живым, но не работает. Опасность не в объёме, а в том, что правка такого блока не даёт эффекта — и это никак не сигнализируется.

### 6.2. Живые конфликты

Пары, где **оба блока попадают в один и тот же промпт** и говорят разное.

---

**К1. «JSON — единственный источник правды» против «цифры только после инструмента»**

- `D_TRADEIN` (✔, в каждом промпте tradein): «JSON-массив — твой ЕДИНСТВЕННЫЙ источник правды. СТРОГО ЗАПРЕЩЕНО выдумывать стоимость выкупа автомобилей клиента.»
- `CS_TRADEIN_V2` (✔, в каждом промпте tradein): «СТРОГО ЗАПРЕЩЕНО называть или предполагать любые цифры стоимости выкупа **до ответа инструмента evaluate_auto**.»
- `D_COMBO` (✔) и `CS_COMBO_V2` (✔) — та же пара для комбо.

Пока сумма оценки не попадала в JSON, оба правила указывали в одну сторону: цифру взять неоткуда, кроме как из свежего вызова. С добавлением `ti_valuation` в контекст модели (правка U6 плана 2026-08-10-001) состояние изменилось: сумма **есть** в JSON, но `CS_*_V2` по-прежнему требует «ответа инструмента». Формально модель обязана вызвать платный инструмент заново, даже имея цифру перед глазами. Блок, разрешающий переиспользование (`BLK_EVAL_REUSE`), заведён, но выключен.

Это единственный конфликт в документе, где цена ошибки — деньги за каждый лишний вызов.

---

**К2. Цель «каждое сообщение ведёт к визиту» против обязательного проверочного вопроса**

- `G-03_GOAL` (✔, в **каждом** промпте всех пяти сценариев): «Твоя финальная цель: ПРОДАТЬ ВИЗИТ… **Каждое твое сообщение** должно мягко вести клиента к этому.»
- `CS_TRADEIN_V2` (✔): «озвучив оценку — **СНАЧАЛА спроси**, соответствует ли она ожиданиям клиента (проверочный вопрос), и **только после его ответа** предлагай визит.»
- `CS_COMBO_V2` (✔): «сперва спроси про оценку авто, затем… про комфортность платежа; работай с первым выявленным барьером. **Только после проверки** предлагай визит.»
- `CS_CREDIT_V2` (✔): «СНАЧАЛА убедись, что условия клиенту подходят… и только после его ответа предлагай следующий этап.»

`G-03_GOAL` — древний блок v3.00, не переписанный при введении need-check в v3.03. Он требует вести к визиту в каждом сообщении; три `CS_*_V2` вставляют обязательный ход-паузу. Модель получает обе инструкции одновременно, во всех промптах.

---

**К3. Два блока сбора данных в одном промпте**

На ходе `data_collect` с интентами `collect_plate`, `collect_mileage`, `collect_plate_and_mileage`, `collect_down_payment` в промпт попадают **оба**:

- основной блок хода `ST_TRADEIN_DATA_COLLECT` (✔): «Твоя приоритетная цель — подтвердить госномер… и узнать пробег… Если появились ОБА параметра — немедленно переходи к вызову инструмента.»
- дополнительный `ST_TRADEIN_REQUIRED_DATA` (✔): «Тебе нужно ОБЯЗАТЕЛЬНО выяснить два параметра: 1. Госномер… 2. Примерный пробег… **ФИНАЛЬНЫЙ АРТЕФАКТ: Действительный номер телефона клиента**.»

Первый описывает цель хода, второй — цель диалога, и они называют разные «обязательные» и «финальные» артефакты. То же во всех пяти сценариях: пары `ST_{СЦЕН}_DATA_COLLECT` + `ST_{СЦЕН}_REQUIRED_DATA` всегда идут вместе, при этом `REQUIRED_DATA` подключён к **пяти** интентам сразу.

---

**К4. Двойной вопрос о поставке в `stock` на первом ходе**

На ходе `greeting_first` в сценарии `stock` в промпт попадают:

- `ST_STOCK_GREETING_V2` (✔, основной): «Автомобили этой модели сейчас находятся в поставке… [SPLIT] Задай ОДИН вопрос о готовности ждать: "Скажите, готовы ли вы подождать поставку?"»
- `BLK_STOCK_NEED_CHECK` (✔, additional через карту `greeting_first → stock`): «NEED-CHECK "НАЛИЧИЕ" (модели нет в наличии / в поставке): НЕ предлагай менеджера и НЕ проси номер телефона в этом же сообщении.»

Оба описывают одну и ту же развилку. Дубль не противоречивый, но инструкция задаётся дважды разными словами в одном промпте.

---

**К5. Блок чужого сценария в карте**

Строка карты: `objection_data_privacy` → `BLK_TRADEIN_ADVOCATE`, роль `additional`, **`Scenario_Filter = credit`**.

То есть в кредитном диалоге при возражении о приватности данных в промпт добавляется блок из сценария трейд-ина. Возможно, это осознанно — кредитный диалог может свернуть в оценку авто через `BLK_CREDIT_TRADEIN_COLLECT`. Но по данным **не установлено**, намеренно это или опечатка: единственная такая строка во всей карте, все остальные ссылаются на блоки своего сценария.

---

**К6. Стадия `tool_needed` без содержания в `stock` и `consult`**

Интенты `data_received_complete` (stock не входит) и `tool_rerun_needed` (входят credit, tradein, combo) переводят на `tool_needed`. Для `stock` и `consult` профильный блок — заглушка «[TODO владелец контента…]», выключена; фолбэк `WF_{STOCK,CONSULT}_LEGACY` тоже выключен.

Результат: промпт на такой ход собирается из 7 блоков вместо 8, без основного блока хода вовсе. Насколько часто клиент туда попадает — по данным **не установлено** (нужен срез логов резолвера).

---

### 6.3. Легаси-блоки

Все двенадцать legacy-блоков **выключены**, ни один в промпт не попадает.

| Блок | Токенов | Что содержит |
|---|---|---|
| `WF_TRADEIN_LEGACY` | 1991 | пошаговый алгоритм оценки: сбор → вызов `evaluate_auto` → ответ → визит |
| `WF_COMBO_LEGACY` | 2170 | то же для комбо-расчёта |
| `WF_CREDIT_LEGACY` | 1351 | пошаговый алгоритм кредита, начиная со сбора взноса |
| `WF_STOCK_LEGACY` | 533 | алгоритм отработки выбора со склада |
| `WF_CONSULT_LEGACY` | 325 | алгоритм консультации |
| `CS_{CREDIT,TRADEIN,COMBO,STOCK,CONSULT}_LEGACY` | 486–845 | монолитные правила поведения до нарезки |
| `OBJ_{CREDIT,TRADEIN,COMBO,STOCK,CONSULT}` | 359–796 | монолиты возражений до нарезки на `BLK_*` |

Суммарно **13 157 токенов** выключенного контента — примерно вчетверо больше самого тяжёлого живого промпта (2891).

Три из них остаются **страховочным фолбэком в коде**: если дефолтный `ST_{СЦЕН}_{СТАДИЯ}` окажется неактивен, компилятор возьмёт `WF_{СЦЕН}_LEGACY`. Поскольку все пять выключены, страховка не сработает — вместо неё будет пустой основной блок (это и наблюдается в К6).

Отдельно от legacy: две заглушки `ST_STOCK_TOOL_NEEDED` и `ST_CONSULT_TOOL_NEEDED` содержат вместо текста задачу владельцу контента, поставленную при нарезке v3.00 и не закрытую до сих пор.

### 6.4. Дубли структуры

Не конфликты, но повторы, которые придётся учитывать при переработке.

| Что дублируется | Сколько копий | Различаются ли |
|---|---|---|
| Блок «первый отказ дать телефон» | 5 (`BLK_{СЦЕН}_NO_PHONE`) | да, объёмом (69–295 ток.) и тактикой |
| Блок «повторный отказ» | 5 | да: `stock`/`consult` сразу зовут `finalize_deal`, остальные предлагают паузу |
| Блок «просьба о паузе» | 5 | почти нет: 106 ток. в четырёх из пяти, текст совпадает дословно |
| Блок «закрытие хода» | 5 (`ST_{СЦЕН}_CLOSING`) | слабо: 164 ток. в трёх из пяти |
| Блок «справочник» / «секретарь» | по 3 (`credit`, `tradein`, `combo`) | слабо |
| Блок «ценовое сравнение» | 2 (`tradein`, `combo`) | нет, 170 ток. оба |
| Блок «скепсис по оценке» | 2 | нет, 143 ток. оба |
| Блок «логистический тупик» | 2 + 1 (`credit` под другим именем) | да, credit-версия отличается |
| Иерархия цен | 3 (`PRICES_*`) | да, по названиям целевых цен |

`ST_{СЦЕН}_CLOSING` подключён как `primary` к **девяти** интентам сразу (`competitor_comparison`, `finalize_agreed`, `polite_close_satisfied`, `propose_call`, `propose_visit`, `request_human_handoff`, `request_phone`, `schedule_appointment`, `tool_result_received`) — то есть вся стадия закрытия во всех сценариях управляется одним текстом на сценарий, независимо от того, что именно сделал клиент.

---

## 7. Что осталось неустановленным

| Вопрос | Почему не установлено |
|---|---|
| Как бот вычисляет `session_context.current_stage` | код бота вне этого репозитория; n8n принимает значение как данность |
| Намеренна ли строка карты `objection_data_privacy → BLK_TRADEIN_ADVOCATE` в сценарии `credit` | единственная кросс-сценарная строка, комментария в данных нет |
| Как часто клиенты попадают на `tool_needed` в `stock`/`consult` | нужен срез логов резолвера; ретенция исполнений n8n ~2.5 суток |
| Почему `Tokens_est` равен нулю у пяти блоков `credit` (`BLK_CREDIT_ADVOCATE`, `_SECRETARY`, `_TRADEIN_COLLECT`, `_APPOINTMENT`, `_VISIT_DECLINED`) | поле заполняется вручную; содержимое у блоков непустое. Оценки объёма промпта в разделе 3.5 на эту величину занижены |
| Совпадает ли `Combo_Calculator` из логов с `combo_calculator` из промпта на уровне сопоставления моделью | вызовы проходят, но механизм сопоставления не наблюдаем |
