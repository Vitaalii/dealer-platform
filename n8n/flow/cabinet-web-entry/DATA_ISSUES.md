# Ошибки данных в каталогах NocoDB — отложены

Срез живых таблиц 2026-09-30 (только чтение). Решение владельца 2026-09-30:
**пока не исправляем**, список — чтобы вернуться. Исправлять у источника, в
NocoDB (см. `docs/solutions/design-patterns/fix-data-gaps-at-source-not-in-llm-prompt-guards.md`),
запись в живые таблицы — только по протоколу `apply_*.py`.

Строки таблицы ниже — `Id` строки каталога. Как влияет — по коду нод
`Prepare Data` / `Prepare Combo` и правкам N4/N5 из этой папки.

## 1. Нет `Price old` — цены уходят в 0 и минус

MSRP = 0, `credit_price` = 0 − бонусы. Бот такой снапшот не замораживает
(`credit_price <= 0`), Александр получает нули в данных. Позиции исключены
из подилерных файлов эталона N2.

| Каталог | Id | Model id | Модель | Complectation id | Комплектация |
|---|---|---|---|---|---|
| ALLCAR `mjcsjc2rtbm94yf` | 13 | 12 | TIGGO 4 PRO | 14 | Style |
| ALLCAR | 32 | 12 | TIGGO 4 PRO | 15 | Ultimate |
| CHERY `m3swq2h1g0eikyc` | 24 | 12 | TIGGO 4 PRO | 13 | Фемили |
| CHERY | 8 | 12 | TIGGO 4 PRO | 14 | Стайл |
| CHERY | 16 | 12 | TIGGO 4 PRO | 15 | Ультима |
| CHERY ATK `mjhd30hsi50lvcm` | 65 | 12 | TIGGO 4 PRO | 13 | Фемили |
| CHERY ATK | 49 | 12 | TIGGO 4 PRO | 14 | Стайл |
| CHERY ATK | 57 | 12 | TIGGO 4 PRO | 15 | Ультима |

## 2. Нет `Model id` (и `Complectation id`)

Нода `Get Catalog` при известном `model_id` такие строки не находит;
идентификатор позиции — пара (Model id, Complectation id), без неё позицию не
сопоставить с ботом. Программа N4 не применяется. Из подилерных файлов N2
исключены.

| Каталог | Id | Модель | Комплектация |
|---|---|---|---|
| GEELY NOCO `m2flt4d2r5d9cpa` | 42 | Новый COOLRAY | Спорт |
| GEELY NOCO | 43 | EX5 ГИБРИД | EM-R Про |
| GEELY NOCO | 44 | EX5 ГИБРИД | EM-R Макс |

## 3. `Model_Pattern` программы не покрывает модели каталога

С N4 такие модели считаются без программы: 20 % / 84 мес.

| Программа | Модели каталога вне `Model_Pattern` (Model id) |
|---|---|
| GEELY_Credit_Programs `4201` (`12, 18, 14, 13, 19, 20, 11`) | ATLAS PRO (15), ATLAS (16), OKAVANGO (17), NORDCROSS 001 (51), Новый COOLRAY (21) + строки без Model id из п. 2 |
| BELGEE_Credit_Programs `5101` (`13, 14,15`) | X50 (11), X70 (12), Новый X50 (16) |
| HAVAL_Credit_Programs `0101` | покрыты все |

## 4. Дубли `Complectation id` внутри модели

Car Search берёт первую строку по `Id`, остальные отбрасывает (`ignored_row_ids`
в эталоне N2). Если строки различаются ценой — какая попадёт в расчёт, решает
порядок `Id`, а не данные.

| Каталог | Model id | Модель | Complectation id | Берётся Id | Отбрасывается Id |
|---|---|---|---|---|---|
| ALLCAR | 13 | HAVAL DARGO X | 11 | 1 | 56 |
| ALLCAR | 13 | HAVAL DARGO X | 12 | 9 | 52 |
| ALLCAR | 19 | ARRIZO 8 | 12 | 3 | 14 |
| ALLCAR | 19 | ARRIZO 8 | 14 | 12 | 45 |
| ALLCAR | 19 | GWM POER KINGKONG | 11 | 20 | 37 |
| ALLCAR | 19 | GWM POER KINGKONG | 13 | 2 | 49 |
| ALLCAR | 12 | TIGGO 4 PRO | 14 | 13 | 29 |
| ALLCAR | 12 | HAVAL DARGO | 11 | 21 | 36 |
| ALLCAR | 12 | HAVAL DARGO | 12 | 10 | 23 |
| ALLCAR | 12 | HAVAL DARGO | 13 | 24 | 48 |
| ALLCAR | 11 | HAVAL M6 | 11 | 18 | 54 |
| ALLCAR | 11 | НОВЫЙ TIGGO 4 | 12 | 11 | 43 |
| ALLCAR | 14 | HAVAL JOLION | 12 | 39 | 42 |
| ALLCAR | 14 | HAVAL JOLION | 14 | 35 | 44 |
| ALLCAR | 14 | HAVAL JOLION | 15 | 22 | 55 |
| ALLCAR | 14 | НОВЫЙ TIGGO 7L | 11 | 41 | 47 |
| ALLCAR | 14 | НОВЫЙ TIGGO 7L | 13 | 30 | 46 |
| ALLCAR | 14 | НОВЫЙ TIGGO 7L | 16 | 33 | 51 |
| GEELY NOCO | 18 | MONJARO | 13 | 2 | 20 |
| TANK `moitytlvaj7uclx` | 14 | TANK 500 | 11 | 17 | 25 |

В ALLCAR под одним Model id лежат модели разных брендов (HAVAL DARGO и
TIGGO 4 PRO — оба 12): Model id в этом каталоге не уникален по модели.

## 5. `Price new` ≠ `Price old − Cash in hook` (смысл колонки у брендов разный)

После N5 `Price new` в расчётах не участвует; расхождение важно только для
текстов, где бот подставляет `[Price new]` (`main.py:2180`, «с максимальной
скидкой»). Сильнее всего у **GAC** (сейчас без активных сайтов): N5 поднимает
комбо-цену на 350 000–1 500 000, например GS8 GL — Price old 4 099 000,
Cash in hook 200 000, Price new 2 699 000. У SOLARIS наоборот: Price new =
Price old, а скидка 600 000 стоит в Cash in hook. Полный построчный разбор —
`n2-price-reference/price_reference.json` (`input`, `combo_price.live` / `.n5`).

## 6. HAVAL: сток 2026 модельного года не привязывается к каталогу

Решение владельца 2026-10-05: **не трогаем** ни данные, ни логику — нужен
отдельный разбор хвостов slug. Порядок привязки в Car Search: точный slug →
название (только для строк каталога без slug). Падение привязок у тестовых
HAVAL-дилеров (99, 01) принято — на боевые продажи не влияет.

Точечная сверка 2026-10-05, дилер `avtomir-samara` (каталог `mywf0f16ptn25gk`,
сток `mtsf9afpiqe1yr1`), 209 машин: прежний код привязывал 50, точный slug — 21.

| Что | Машин | Пример |
|---|---|---|
| Хвост slug 2026MY / иной код — основа совпадает с каталогом | 21 | сток `premium-1-5-dct-2wd-143-l-s-2026my`, каталог `premium-1-5-dct-2wd-143-l-s-27398_255870` |
| Slug иного формата (без `-l-s`) — раньше ловился по названию | 8 | `premium-2-0t-262564_33906` (POER), `premium-1-5-dct7-2wd-150-l-s-2026-my` (F7), `premium-2-0-at-4wd-200-ls-2026my` (DARGO) |
| Комплектаций 2026 нет в каталоге | 136 | `optimum-…` (JOLION, M6), `tekhno-…` (F7, DARGO), `komfort-…` |
| Модели `F7x` нет в каталоге (`Model_short`) | 23 | сток `Model = F7x` |

Прежнее сравнение по названию ловило часть 2026MY, но неточно: Premium 4WD
уходил в Premium 2WD (первую одноимённую комплектацию).
