# MAX_API_REFERENCE.MD

Данный документ описывает структуру REST API клиента `MaxBotClient` (на базе `aiohttp`) и спецификацию JSON-объектов для интеграции с корпоративным мессенджером MAX.

## 1. Базовая конфигурация
* **Base URL:** `https://platform-api.max.ru`
* **Auth:** Bearer Token (передается в Header `Authorization`).
* **Content-Type:** `application/json`

## 2. Методы Клиента (`max_client.py`)

### `send_message`
Отправка текстового сообщения с поддержкой HTML-форматирования и inline-кнопок.
* **Endpoint:** `POST /messages?user_id={user_id}` (ID передается как Query-параметр <int64>)
* **Payload Example:**
```json
{
  "text": "Текст сообщения",
  "format": "html",
  "attachments": [
    {
      "type": "image",
      "payload": {"url": "https://example.com/image.jpg"}
    },
    {
      "type": "inline_keyboard",
      "payload": {
        "buttons": [[{"type": "callback", "text": "КНОПКА", "payload": "btn"}]]
      }
    }
  ]
}
```

```json
// Шаблон кнопки сбора телефона (интент 10_Contact_Phone)
{
  "type": "inline_keyboard",
  "payload": {
    "buttons": [[{"type": "request_contact", "text": "ПОДТВЕРДИТЬ"}]]
  }
}
```

```json
// Шаблон кнопок таймаута удержания (30 секунд ожидания контакта)
// Тексты кнопок статичны; текст сообщения над кнопками динамический (зависит от сценария)
{
  "type": "inline_keyboard",
  "payload": {
    "buttons": [
      [{"type": "callback", "text": "ПРОДОЛЖИТЬ", "payload": "btn_continue_ai"}],
      [{"type": "callback", "text": "ОТМЕНА", "payload": "btn_stop_ai"}]
    ]
  }
}
```

### `send_alert_to_group`
Отправка сервисного уведомления (карточки лида) в групповой чат сотрудников дилера.
* **Endpoint:** `POST /messages?chat_id={group_id}`
* **Payload Example:**
```json
{
  "text": "🔔 **НОВАЯ ЗАЯВКА**\n\n👤 Иван Иванов\n🚗 Haval Jolion\n📞 +79991234567",
  "format": "html"
}
```

## 3. Обработка Webhook-ов от MAX
Бот слушает входящие события на эндпоинте `/max_webhook`. Подписка оформляется на события: `message_created`, `bot_started`, `message_callback`.

### Фильтр устаревших событий
Перед обработкой каждого вебхука проверяется поле `timestamp`. Если событие опоздало более чем на 60 секунд — оно игнорируется. Это защищает от ложных срабатываний при повторной доставке зависших событий (например, `bot_started` от предыдущей сессии).

```python
webhook_ts = obj.get("timestamp", 0) / 1000  # MAX присылает в миллисекундах
if webhook_ts and (time.time() - webhook_ts) > 60:
    # игнорируем
```

### Классификация типа ввода (`input_type`)
Каждое событие получает тип перед передачей в ядро маршрутизации:

| `update_type` | `input_type` | Описание |
| :--- | :--- | :--- |
| `message_created` | `"text"` | Обычное текстовое сообщение |
| `message_callback` | `"button"` | Нажатие inline-кнопки |
| `bot_started` | `"deeplink"` | Переход по стартовой ссылке |

Тип `"deeplink"` исключает событие из перехватчика возражений, предотвращая ошибочный запуск ИИ при повторном переходе клиента по ссылке с другой моделью.

### Событие 1: Обычное сообщение (`message_created`)
```json
{
  "update_type": "message_created",
  "timestamp": 1773424441673,
  "message": {
    "sender": {"user_id": 123754089, "name": "Виталий", "is_bot": false},
    "body": {"text": "Тест"}
  }
}
```
> Сообщения от ботов (`"is_bot": true`) игнорируются.

### Событие 2: Deep Link (`bot_started`)
Срабатывает при переходе по ссылке `https://max.ru/bot?start=991911GH_GWM_POER`.
```json
{
  "update_type": "bot_started",
  "user": {"user_id": 123754089, "name": "Виталий"},
  "payload": "991911GH_GWM_POER_KINGKONG"
}
```

### Событие 3: Клик по кнопке (`message_callback`)
```json
{
  "update_type": "message_callback",
  "user": {"user_id": 123754089, "name": "Виталий"},
  "payload": "btn_credit"
}
```

## 4. CALLBACK_MAPPING — таблица маппинга кнопок

| `callback_data` | Транслируется в текст | Назначение |
| :--- | :--- | :--- |
| `btn_credit` | `"В КРЕДИТ"` | Сценарий кредита |
| `btn_tradein` | `"В ТРЕЙД-ИН"` | Сценарий трейд-ин |
| `btn_combo` | `"В КРЕДИТ + ТРЕЙД-ИН"` | Комбо-сценарий |
| `btn_stock` | `"АВТО В НАЛИЧИИ"` | Сценарий стока |
| `btn_consult` | `"КОНСУЛЬТАЦИЯ МЕНЕДЖЕРА"` | Консультация |
| `btn_no` | `"НЕ АКТУАЛЬНО"` | Отказ на старте |
| `btn_stop` | `"НИЧЕГО НЕ НУЖНО"` | Финальный отказ (DF-воронка) |
| `btn_other` | `"ДРУГАЯ МОДЕЛЬ"` | Другая модель |
| `btn_phone` | `"ПО ТЕЛЕФОНУ"` | Выбор канала: телефон |
| `btn_tg` | `"В МЕССЕНДЖЕРЕ"` | Выбор канала: мессенджер |
| `btn_continue_ai` | `"__SYSTEM_TIMEOUT_HANDOFF__"` | Кнопка таймаута: продолжить с ИИ |
| `btn_stop_ai` | `"НИЧЕГО НЕ НУЖНО"` | Кнопка таймаута: отмена (внутренний маппинг) |

---

## 5. Многоканальная архитектура (Multi-Channel)

С версии 3.1.0 бот поддерживает три канала. Алерты дилерам всегда идут через MAX независимо от канала клиента.

### Каналы и точки входа

| Канал | Хэндлер | Эндпоинт | Клиент |
| :--- | :--- | :--- | :--- |
| MAX | `handle_max_webhook` | `POST /max_webhook` | `MaxBotClient` |
| Telegram | `handle_tg_webhook` | `POST /tg_webhook` | `TelegramBotClient` |
| Web | `handle_chat_message` | `POST /chat_message` | `WebBotClient` |

### Возможности каналов

| Возможность | MAX | Telegram | Web |
| :--- | :---: | :---: | :---: |
| `supports_contact_button()` | ✅ | ✅ | ❌ |
| `supports_reminders()` | ✅ | ✅ | ❌ |
| `supports_keyboard_removal()` | ✅ | ✅ | ❌ |

### Роутер каналов

```python
def get_client(channel: str) -> BaseBotClient:
    return {"max": max_client, "telegram": tg_client, "web": web_client}.get(channel, max_client)
```

Алерты дилерам и технические алерты (`error_middleware`, `sync_worker`) всегда вызывают `max_client` напрямую, минуя роутер.

### Web Widget API

**Входящее сообщение:**
- Endpoint: `POST /chat_message`
- Payload:
```json
{
  "session_id": "1780910478426",
  "message": "Текст от клиента",
  "user_name": "Клиент"
}
```

**SSE-поток:**
- Endpoint: `GET /chat_stream/{session_id}`
- События:
```
data: {"type": "text", "text": "Ответ бота", "buttons": [...]}
data: {"type": "typing_on"}
data: {"type": "chat_close"}
: ping
```

**Каталог-прокси:**
- `GET /catalog/{dealer_code}` — список авто из NocoDB
- `GET /catalog-colors?table_id=...` — цвета из NocoDB
- Оба эндпоинта добавляют CORS-заголовки и не передают NocoDB-токен клиенту.
