# Предложение: эндпоинт «настройки дилера по коду» для лендинга

Дата: 2026-09-30. Статус: предложение, реализация — после решения владельца.
Решение владельца: на сайте задаётся только код дилера (скрытое поле формы `dealer_id`),
всё остальное лендинг получает у бота из таблицы Dealers.

## 1. Что бот отдаёт лендингу сейчас

| Эндпоинт | Что отдаёт | Ограничение | CORS |
|---|---|---|---|
| `GET /catalog/{table_id}` (`main.py`, `handle_catalog`) | строки таблицы NocoDB как есть (`{"list": [...]}`) | `table_id` должен быть в `catalog_allowed_tables()`, иначе 404 без подробностей | `*` |
| `GET /catalog-colors?table_id=…` (`handle_catalog_colors`) | то же для таблицы цветов | тот же allowlist | `*` |
| `GET /bot-links` (`handle_bot_links`) | ссылки активных ботов: `channel`, `brands`, `link` | только эти поля | `*` |
| `/cabinet/api/*` | кабинет и «открыть сессию» | токен или контракт | echo `Origin` из whitelist `Website_URL` дилеров |

**Allowlist таблиц** — `catalog_allowed_tables()`, `main.py:~120`. В него входят:
- статичная тройка сайта `CATALOG_STATIC_ALLOWED_TABLES`, `main.py:98–102`: `mywf0f16ptn25gk` (основной каталог), `mm8wldgk5vn9gwh` (оснащение), `muruk10ipof33m5` (Dealers);
- env `NOCODB_TABLE_ID`;
- id из ВСЕХ колонок Dealers, чьё имя после нормализации оканчивается на `tableid` (`_dealer_table_columns`).

Правило данных (решение 2026-09-25): колонка Dealers с именем `* table id` — это публичная таблица. Поэтому `Sourse table id`, `Features table id`, `Color table id`, `Stock_Table_ID`, `Program_Table_ID` и `Rates_Table_ID` уже читаются через `/catalog`, если известен их id. Базы (`* base id`) таблицами не считаются и в allowlist не попадают. Новая таблица в allowlist вызывает техалерт `catalog-allow-new`.

**Карточка дилера.** `/catalog/muruk10ipof33m5` (Dealers) отдаётся через `_dealer_public_row`, `main.py:172–175`. Наружу уходят только поля из whitelist `DEALER_PUBLIC_FIELDS_NORM`, `main.py:109–112`: `Dealer_Code`, `Dealer_location`, `Dealer_name`, `Dealer_status`, `Dealer_address`, `Dealer_workinghours`, `Dealer_phone`. Сверка имён — без учёта регистра, `_` и пробелов.

Всё остальное вырезается, в том числе:
- `Website_URL` и `Brand`;
- id таблиц (`Sourse table id`, `Features table id`, `Color table id`, `Stock_*`, `Program_Table_ID`, `Rates_Table_ID`).

Поэтому лендинг держит id таблиц константами в коде (`TABLE_MAIN`, `TABLE_FEATURES`, `TABLE_DEALERS`) и не может узнать `Rates_Table_ID` — ровно то, о чём пишет агент лендинга.

Колонки Dealers на 2026-09-30:
- **Публичные сейчас:** `Dealer_Code`, `Dealer_name`, `Dealer_address`, `Dealer_location`, `Dealer_workinghours`, `Dealer_phone`, `Dealer_status`.
- **Публичные по смыслу, но вырезаются:** `Website_URL`, `Brand`, `* table id` (6 шт.), `Policy`, `Agreement` — тексты согласия, которые кабинет уже показывает клиенту.
- **Внутренние (наружу нельзя):** `Telegram_Group_ID`, `Max_Group_ID`, `Business_Connection_ID`, `Communication_Type*`, `UTC_Offset`, `Dealer_id`, `Name`, `Prompt_Ver`, `Prompt_Teg`, `Reminder_Image`, служебные `Id`/`CreatedAt`/`UpdatedAt`.

## 2. Контракт `GET /cabinet/api/dealer/{dealer_code}`

Путь под `/cabinet/api/` — CORS такой же, как у кабинета: echo `Origin` из whitelist `Website_URL` дилеров, чужой Origin без CORS-заголовков (`_cabinet_cors`).

**Вход:** `dealer_code` — 2 цифры (`^\d{2}$`).

**Ответ 200:**
```json
{
  "dealer_code": "99",
  "brand": "Haval",
  "website_url": "https://demo.autodc.org",
  "dealer": {
    "name": "Автомир Самара",
    "address": "Самара, ул. Авроры, д. 150Е",
    "location": "53.21…, 50.18…",
    "working_hours": "пн-вс: 09:00-21:00",
    "phone": "+7 (999) 555 5555",
    "status": "Официальный дилер HAVAL"
  },
  "tables": {
    "catalog":  {"base_id": "p96x…", "table_id": "mywf0f16ptn25gk"},
    "features": {"table_id": "mm8wldgk5vn9gwh"},
    "colors":   {"table_id": "mwyu1qq92hm96o9"},
    "stock":    {"base_id": "p96x…", "table_id": "mtsf9afpiqe1yr1"},
    "programs": {"table_id": "my7rg0l7j6varzn"},
    "rates":    {"table_id": "mfocbgvi50mqm7c"}
  },
  "documents": {"policy": "…", "agreement": "…"}
}
```

Правила ответа:
- Пустых значений нет: колонка не заполнена — ключ не отдаётся.
- Остатки: `stock` при пустых `Stock_*` падает на каталог — как `db_config` бота.
- Базы: `base_id` отдаётся по решению владельца. Лендингу он не нужен — чтение идёт через `/catalog/{table_id}` бота, — но и не секретен без токена NocoDB. Можно не отдавать.
- Тексты согласия: `documents` — по желанию владельца, лендинг может показывать те же тексты согласия, что кабинет. Ограничение: длинные тексты раздувают ответ, если лендинг их не использует.

**Ошибки:**

| HTTP | `error` | Когда |
|---|---|---|
| 422 | `invalid_input` | код не из двух цифр |
| 404 | `unknown_dealer` | кода нет в Dealers (в том числе дилер неактивен или не загружен) |
| 503 | `dealers_unavailable` | справочник дилеров ещё не загружен (старт бота, сбой NocoDB) |

**Кэш:**
- ответ строится из кэша дилеров бота (`CACHED_DEALERS`, перечитывается из NocoDB по расписанию), без запроса к NocoDB на каждый вызов;
- заголовок `Cache-Control: public, max-age=300`.
Изменения в Dealers доходят до сайта не позже перечитывания кэша + 5 минут.

**Наружу не уходит никогда** (whitelist, а не denylist — новая колонка Dealers по умолчанию закрыта):
- группы и подключения мессенджеров: `Max_Group_ID`, `Telegram_Group_ID`, `Business_Connection_ID`;
- режимы коммуникации `Communication_Type*`;
- промпты и служебные поля: `Prompt_*`, `Dealer_id`, `Name`, `UTC_Offset`, `Reminder_Image`;
- токены, секреты и env, `db_config` целиком, служебные поля NocoDB.

**Что меняется для `/catalog`:** ничего. Allowlist таблиц остаётся прежним. `TABLE_DEALERS` в статичной тройке остаётся для совместимости со старыми страницами, пока лендинги не перейдут на новый эндпоинт.

## 3. Объём и место в плане

Объём — небольшой, один шаг U, около 0,5 дня с тестами:
- `main.py`: в `CACHED_DEALERS` добавить публичный набор — `Brand` как в таблице, `Features table id`, `Color table id`, `Dealer_location`, `Dealer_phone`, `Dealer_status`; остальное уже есть в кэше;
- `cabinet_service.py`: `dealer_settings(code)` — сборка ответа по whitelist;
- `cabinet_api.py`: маршрут `GET /cabinet/api/dealer/{dealer_code}`;
- тесты `tests/test_dealer_settings.py`:
  - формат кода, неизвестный дилер, справочник не загружен;
  - в ответе нет внутренних полей, в том числе новой колонки Dealers;
  - запасные значения stock;
  - CORS: свой и чужой Origin;
  - `Cache-Control`.

**Место в плане:** отдельный шаг блока E до интеграции с лендингом, например U13a «Настройки дилера для лендинга». Шаг не зависит от блоков C и D, его можно сделать в любой момент до передачи лендингу. В бриф лендинга (`docs/handoff/landing-brief.md`) добавить контракт и работу L8: константы `DEALER_CODE`/`TABLE_*` заменить на код из скрытого поля `dealer_id` и ответ эндпоинта.
