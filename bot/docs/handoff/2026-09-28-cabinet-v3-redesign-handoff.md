---
title: "Кабинет: дизайн v3 — состояние перед выкладкой (U8 отложен)"
date: 2026-09-28
module: client-cabinet
status: реализация закончена, выкладка отложена решением владельца
---

# Кабинет, дизайн v3 — состояние на 2026-09-28

## Что за задача

Фронт личного кабинета (`widget-custom/cabinet-app/app.js`) переведён на внешний
вид макета v3: карточка-товар авто в шапке с подзаголовком сценария, серые
плашки-фразы вместо `h2`+lead, оценка «~ N ₽» внутри карточки «Ваш автомобиль»,
строка «Первоначальный взнос» в комбо. Функциональность заморожена; поведенческое
изменение одно (карточка авто кликабельна после отправки — пересмотр правила Ж4),
правка бэка — поле `price_new` в ответе `GET /cabinet/api/car` плюс сборка `name`
как «Model · Complectation».

- План (с решениями владельца и статусом): `docs/plans/2026-09-26-001-feat-cabinet-v3-redesign-plan.md`
- Макет: `docs/design/lk-source-v3.html`

## Состояние

**Ветка** `feat/cabinet-v3-redesign`, отведена от `d2e7489` (HEAD
`feat/multibot-brand-routing`). Коммиты от точки ответвления:

| Хэш | Суть |
|---|---|
| `959a6d1` | U0 — ветка, план, макет v3 в git |
| `757d58e` | U1 — подзаголовок сценария, серые плашки-фразы, удаление старых `h2`/lead |
| `0207ed0` | U2 — оценка «~ N ₽» с пояснением внутри карточки «Ваш автомобиль» |
| `de12ecc` | U3 — реордер комбо и строка взноса «зачёт + доплата» (KTD1) |
| `8508abd` | U9 — бэк: `price_new` в ответе `GET /cabinet/api/car` |
| `2dc118c` | U4 — карточка-товар авто в шапке (миниатюра, цена, `aria-label`) |
| `2dea867` | U5 — Ж4-инверсия: карточка кликабельна после отправки, попап вынесен из `#cab-content` (KTD2) |
| `38c4501` | фикс: имя авто в `/car` — «Model · Complectation» (В10) |
| `b78103f` | U6 — комментарии `app.js` переведены на макет v3 |
| `fec29fc` | U6 добит до строгого критерия R14; В10 и расхождение `?v=` внесены в план |

**Тесты:** `954 passed, 4 skipped` (полный `python3 -m pytest -q`, перепроверено
2026-09-28). На этой машине `python` в PATH нет — звать `python3` или
`venv/bin/python`.

**Прод не тронут.** Версия на бою — **3.3.3** (образ `vitaln/unified-bot:multy-v3.3.3`).
Живой `app.js` байт-в-байт равен версии из `feat/multibot-brand-routing` и
отличается от `feat/cabinet-v3-redesign` — то есть новый дизайн наружу не уехал.
Проверка (одна команда):

```
curl -sS -o /tmp/app_live.js https://obut-5oaj-5i2u.gw-1a.dockhost.net/widget-custom/cabinet-app/app.js && git show feat/multibot-brand-routing:widget-custom/cabinet-app/app.js > /tmp/app_br.js && cmp /tmp/app_live.js /tmp/app_br.js && echo "прод = brand-routing, дизайн v3 не выложен"
```

**U8 (выкладка) отложен решением владельца и не начат.** Образ `multy-v3.4.0` не
собирался и не пушился.

## Решения владельца В1–В10

Кратко; формулировки и обоснования — в разделе «Решения владельца» плана.

- **В1** цена карточки — розничная `Price new` из каталога NocoDB через новое поле `price_new`; `credit_price` не показывать.
- **В2** неизвестный/пустой сценарий — запасной вариант на сбой: подзаголовок «Анкета», состав блоков как был, отдельного оформления нет.
- **В3** оценка трейд-ина — обе точки вывода «~ N ₽» читают `me.calc.ti_valuation`, тот же источник, что у калькулятора; «оценки нет» — редкий сбойный случай.
- **В4** трейд-ин без `found_info` — остаётся как есть, нового оформления нет.
- **В5** комбо в деградации R18 — прежний состав, меняется только разметка строки.
- **В6** ключ `FORM_LABELS.combo_extra_down` удалён, страж переписан на новую разметку.
- **В7** кликабельность карточки появляется вместе с данными `/car`.
- **В8** переход в submitted при открытом попапе — попап закрывается, фокус возвращается на карточку, без переоткрытия.
- **В9** `no_credit_needed` — строка «зачёт + доплата» остаётся видимой, вместо платежа/срока прежняя строка «Кредит не требуется — зачёт покрывает стоимость».
- **В10** `name` в ответе `/car` — «Model · Complectation»; одна заполненная часть — без точки, обе пустые — фолбэк `Model title` → `item_name`. Причина: `Model title` каталога — комплектационный титул без модели («Tech+ 1.5T 150 л.с. AT 4WD»).

## Как тестировать

### Страница

Тестирование — только на Tilda-странице **`/cabinet_dev`**. Боевая `/cabinet` не
трогается. Путь `/cabinet` захардкожен в `cabinet_service.py:861`
(`f"https://{netloc}/cabinet"`) — параметризовать его договорились **не**: владелец
правит выданную ссылку руками, меняя `/cabinet` на `/cabinet_dev`.

### Дев-стенд (команды по одной за шаг)

Шаг 1 — бот (порт из `.env`, `PORT=8080`):

```
CABINET_OTP_DEV_LOG=1 venv/bin/python main.py
```

Шаг 2 — туннель, в отдельном окне (статичный домен уже прописан в `PUBLIC_BASE_URL`):

```
ngrok http 8080 --domain=sporophoric-devorah-remonstrative.ngrok-free.dev
```

Проверка, что стенд поднялся: `pgrep -fl "main.py"`, `pgrep -fl ngrok`,
`lsof -nP -iTCP:8080 -sTCP:LISTEN`.

### Выдача тестовых ссылок

`scripts/issue_cabinet_link.py` зовёт тот же `prepare_cabinet_context()`, что и
бой (включая заморозку снапшота). БД берётся из `DATABASE_URL`; в `.env` её нет,
поэтому используется дев-SQLite `data/max_database_v2.db`. Прод-PostgreSQL
недостижим — гард отклоняет не-localhost хост до первого запроса.

Три сценария (`--seed` засевает пользователя и `n8n_cache`, без него калькулятор
не покажется):

```
venv/bin/python scripts/issue_cabinet_link.py --user dev-credit-1 --deal DEV-C1 --dealer 99 --website https://demo.autodc.org --scenario credit --seed
```

```
venv/bin/python scripts/issue_cabinet_link.py --user dev-tradein-1 --deal DEV-T1 --dealer 99 --website https://demo.autodc.org --scenario tradein --seed
```

```
venv/bin/python scripts/issue_cabinet_link.py --user dev-combo-1 --deal DEV-K1 --dealer 99 --website https://demo.autodc.org --scenario combo --seed
```

Полезные флаги: `--model-id` / `--item-id` — пара идентификации позиции каталога
(для живого `/cabinet/api/car` брать реальную пару из каталога дилера в NocoDB),
`--item-name`, `--price` (это `credit_price`, цена расчёта), `--payment`
(`target_payment`). Повторный запуск той же связки без `--seed` выдаёт **тот же
URL** и перезамораживает снапшот.

### Подстановка `ti_valuation` / `found_info` в дев-БД

Снапшот лежит в `cabinet_profile.calc_snapshot` (TEXT с JSON), связь с клиентом —
`cabinet_profile.user_id` → `users.id`. Ключи снапшота: `schema_version`,
`item_id`, `model_id`, `item_name`, `price`, `target_payment`, `rates`, `program`,
`found_info`, `ti_valuation`.

Посмотреть, что есть сейчас:

```
sqlite3 data/max_database_v2.db "SELECT id, user_id, dealer_code, scenario_type, json_extract(calc_snapshot,'\$.price') AS price, json_extract(calc_snapshot,'\$.ti_valuation') AS ti FROM cabinet_profile WHERE calc_snapshot IS NOT NULL ORDER BY calc_frozen_at DESC LIMIT 10;"
```

Подставить оценку (пример — профиль `id=13`; перед правкой сделать копию файла БД):

```
cp data/max_database_v2.db data/max_database_v2.db.bak-$(date +%Y%m%d-%H%M%S)
```

```
sqlite3 data/max_database_v2.db "UPDATE cabinet_profile SET calc_snapshot = json_set(calc_snapshot,'\$.ti_valuation', 2890000) WHERE id = 13;"
```

Откат — вернуть `null` (или восстановить файл из копии выше):

```
sqlite3 data/max_database_v2.db "UPDATE cabinet_profile SET calc_snapshot = json_set(calc_snapshot,'\$.ti_valuation', json('null')) WHERE id = 13;"
```

`found_info` (карточка «Ваш автомобиль» вместо редактируемых полей `tradein_*`)
правится тем же `json_set` по `$.found_info`; снять — `json_remove`:

```
sqlite3 data/max_database_v2.db "UPDATE cabinet_profile SET calc_snapshot = json_remove(calc_snapshot,'\$.found_info') WHERE id = 13;"
```

> **Не перевыпускать ссылку после подстановки.** Повторный запуск
> `issue_cabinet_link.py` для той же связки заново замораживает снапшот из
> `n8n_cache` и затирает подставленные `ti_valuation`/`found_info`. Порядок такой:
> сначала выдать ссылку, потом править БД, потом открывать страницу (F5 достаточно).

Текущее состояние дев-БД на момент фиксации: профили `id=12` (user 15) и `id=13`
(user 16), дилер 99, сценарий `combo`, `ti_valuation = 2 890 000`, `price = 2 050 000`,
`found_info` — MAZDA CX_5 2018 (данные Auto.ru). Остальные девять снапшотов —
`credit` с `ti_valuation: null`.

### Флип «Кредит не требуется — зачёт покрывает стоимость»

Значения доплаты, которыми владелец проверял флип: **1 600 000** — туда,
**100 000** — обратно. Набирать в поле «+ доплата» строки взноса и следить, что
при пересборке блока фокус и значение не теряются (дебаунс 600 мс не «глотает»
коммит).

Внимание: порог флипа — это пара (`price`, `ti_valuation`) конкретного снапшота,
поэтому значения выше привязаны к тому состоянию БД, на котором шла приёмка. В
текущем дев-состоянии `ti_valuation` (2 890 000) уже больше `price` (2 050 000),
то есть «Кредит не требуется» будет включён и при нулевой доплате — чтобы
воспроизвести флип, сначала занизить `ti_valuation` (например, до 400 000) тем же
`json_set`, а затем подбирать доплату. Пересчитать порог по своим числам, а не
брать 1 600 000 вслепую.

## Что осталось

### a) U8 — выкладка (по отдельной отмашке владельца, одна команда за шаг)

Образец порядка — `docs/handoff/2026-09-23-multibot-prod-release-runbook.md`.

1. Сборка образа с НОВЫМ тегом (старые теги не перезаписывать — это точки отката):
   `docker build --platform linux/amd64 -t vitaln/unified-bot:multy-v3.4.0 .`
2. Проверить состав образа — что внутри новый фронт и нет слепков прод-версий:
   `docker run --rm vitaln/unified-bot:multy-v3.4.0 sh -c "grep -c cab-phrase widget-custom/cabinet-app/app.js; ls scripts/ | grep -c '_old\|_new'"`
   — ожидание: первое число больше нуля, второе `0`.
3. `docker push vitaln/unified-bot:multy-v3.4.0`
4. **Dockhost (делает владелец):** сменить образ приложения на `multy-v3.4.0` → рестарт.
5. `curl`-сверка живого фронта с рабочей папкой (это же — критерий отката):
   `curl -sS -o /tmp/app_live.js https://obut-5oaj-5i2u.gw-1a.dockhost.net/widget-custom/cabinet-app/app.js && cmp /tmp/app_live.js widget-custom/cabinet-app/app.js && echo OK`
6. **Tilda (делает владелец):** на боевой странице `/cabinet` заменить `app.js?v=6`
   на `app.js?v=7`; `calc_model.js?v=1` и `calc_engine.browser.js?v=2026-08-06.1`
   не трогать — файлы не менялись.
7. В `docs/CABINET_TILDA_SETUP.md` поставить `app.js?v=7`. Сейчас там `?v=2` —
   расхождение досталось до этой задачи (живая страница была на `?v=6`), поэтому
   док равняем на фактическое значение страниц, а не инкрементируем от 2. Страж
   `TestU10TildaThreeScripts` целое число `app.js` не проверяет — сверять глазами.
8. Живая приёмка на `/cabinet` — только после смены `?v=` и обновления страницы.
9. Откат при любом расхождении: в Dockhost вернуть образ **`multy-v3.3.3`** и на
   Tilda вернуть **`?v=6`**.

### b) Перед сборкой обязательно проверить

- активна ветка `feat/cabinet-v3-redesign` (`git branch --show-current`);
- `git status` чистый по tracked-файлам (untracked `scripts/*` — норма, их не коммитим);
- untracked `scripts/*` не попадают в образ. Уже обеспечено: `.dockerignore`
  исключает все шесть файлов по именам (`scripts/main_old.py`, `main_new.py`,
  `database_old.py`, `database_new.py`, `widget_old.js`, `widget_new.js`) — при
  сборке перепроверить шагом 2 выше, потому что `Dockerfile` делает `COPY . .`;
- запущен Docker Desktop (на момент фиксации демон не работал:
  `Cannot connect to the Docker daemon` — проверять `docker info` до сборки).

### c) После приёмки на бою — слияние ветки

Пока `feat/cabinet-v3-redesign` не слита, любая следующая сборка из другой ветки
(например, из `feat/multibot-brand-routing`) вернёт на бой старый дизайн кабинета.
После успешной живой приёмки составить план слияния и выполнить его **только по
подтверждению владельца**. До слияния помнить: точка сборки прод-образов — эта
ветка, не `main` и не `feat/multibot-brand-routing`.

### d) Если с момента фиксации прошло время

Перед выкладкой проверить, что база не уехала:

- прод всё ещё 3.3.3 — сверкой живого `app.js` с `feat/multibot-brand-routing`
  (команда в разделе «Состояние»); тег образа в Dockhost смотрит владелец;
- `feat/multibot-brand-routing` не ушла вперёд: `git log --oneline -1 feat/multibot-brand-routing`
  должен дать **`d2e7489`**.

Если хоть одно не так — **остановиться и доложить владельцу**, не собирать образ:
ветка отведена от `d2e7489`, и новые коммиты в `feat/multibot-brand-routing` в
образ v3.4.0 не попадут.

## Отдельные задачи, не в этом релизе

- **Подчёркивание в модели авто.** В карточке «Ваш автомобиль» модель из
  `found_info` выводится с подчёркиванием — «CX_5» вместо «CX-5». Это данные
  Auto.ru как есть (`found_info.model` в снапшоте), поведение было и до этой
  задачи. Решения по нормализации нет.
- **Синяя рамка фокуса на карточке авто** после закрытия попапа. Возврат фокуса на
  карточку сделан осознанно (В8, доступность), но визуально рамка заметна.
  Решение владельцем не принято — трогать не надо.

## Договорённости с владельцем

- Сначала разбор и план, потом реализация — не начинать правки, пока план не согласован.
- Не выдумывать: чего нет в коде или данных — спрашивать, а не достраивать догадкой.
- Терминальные команды — по одной за шаг.
- Действия в Dockhost и на Tilda владелец делает сам; агент только готовит команды и проверяет результат.
- Прод-БД — **только чтение**. Прод-окружение Dockhost и боевую страницу `/cabinet` не трогать.
