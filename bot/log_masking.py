"""U9: маскирование секретов кабинета в логах и алертах (R16, R4, AE9).

Строгий лист: модуль импортируется из main.py и тестов, сам НЕ импортирует
ни main.py, ни cabinet_service.py — только stdlib. Базовый маскирующий тип
`Secret` живёт в sms_gateway.py (U2) и здесь не дублируется: Secret прячет
значение у источника, этот модуль — последний рубеж для строк, уже попавших
в логирующий конвейер (record дочерних логгеров, aiohttp.access, алерты).

Правила маскирования (`mask_sensitive`):
- телефоны РФ в любом начертании (+79..., 79..., 89..., с пробелами,
  дефисами, скобками) → `+7•••••••NNN` — последние 3 цифры остаются для
  корреляции с событиями, R16 это допускает (маска, не открытый номер);
- 4-значные OTP-коды ТОЛЬКО в контексте ключа `code` (`code=1234`,
  `"code": 1234`) → `••••` полностью (AE9: код в алерте отсутствует);
  произвольные 4 цифры (год, пробег) не трогаются;
- значения токенов в query/json/заголовках (`t=`, `token=`,
  `access_token=`, `Authorization: Bearer ...`) → первые 6 символов + `…`;
- поля анкеты глобально НЕ маскируются: легитимные маршруты (UF-поля,
  комментарий с черновиком) идут не через логи (R16-исключение).

Фильтр вешается на ХЕНДЛЕРЫ корневого логгера (`install_masking`), а не на
сам логгер: фильтры логгера не применяются к записям, всплывшим от дочерних
логгеров, поэтому иначе `aiohttp.access` (GET-колбэк с phone и external_id
в строке запроса) остался бы немаскированным.
"""
import logging
import re
import traceback

__all__ = ["mask_sensitive", "mask_secrets", "MaskingFilter", "install_masking",
           "format_masked_phone"]

# Телефон РФ: +7/7/8, затем 10 цифр (первая — 9, мобильные), между цифрами
# допустимы пробелы/дефисы/скобки/точки. Границы (?<!\d)/(?!\d) не дают
# откусить кусок более длинного числа (deal_id, суммы).
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+\s*7|8|7)[\s\-().]*9(?:[\s\-().]*\d){9}(?!\d)"
)

# 4-значный код строго в контексте ключа code (query/json/kv).
_CODE_RE = re.compile(
    r"(?i)([\"']?code[\"']?\s*[=:]\s*[\"']?)\d{4}(?!\d)"
)

# Значение токена в kv/json-контексте; Bearer — отдельно (заголовок).
# U1 мультибота: ключи secret/webhook_secret — производный секрет вебхука
# (KTD-3) не должен доживать до вывода даже при случайном логировании.
_TOKEN_KV_RE = re.compile(
    r"(?i)\b(t|token|access_token|cabinet_token|secret|webhook_secret)"
    r"([\"']?\s*[=:]\s*[\"']?)"
    r"([A-Za-z0-9._~+/\-]{8,})"
)
_BEARER_RE = re.compile(r"(?i)\b(Bearer)\s+([A-Za-z0-9._~+/\-]{8,})")

# U1 мультибота: платформенные заголовки секрета вебхука — Telegram шлёт
# X-Telegram-Bot-Api-Secret-Token, MAX — X-Max-Bot-Api-Secret; ни один не
# попадал под _TOKEN_KV_RE (имя ключа не из списка).
_SECRET_HEADER_RE = re.compile(
    r"(?i)\b(X-(?:Max|Telegram)-Bot-Api-Secret(?:-Token)?|X-Cabinet-Eval-Secret|xc-token"
    r"|X-N8N-Secret|X-N8N-Callback-Secret"
    r"|x-authorization)"
    r"([\"']?\s*[=:]\s*[\"']?)"
    r"([A-Za-z0-9._~+/\-]{8,})"
)


# 2026-10-03: входящий вебхук Битрикса — секрет в пути адреса
# (…/rest/<user_id>/<токен>/метод). Попадал в лог текстом исключения aiohttp
# («Connection timeout to host https://…/rest/1/<токен>/crm.contact.get»).
_BITRIX_WEBHOOK_RE = re.compile(r"(?i)(/rest/\d+/)([A-Za-z0-9_\-]{6,})(?=/|\b)")


def format_masked_phone(digits: str) -> str:
    """+7•••••••NNN — единый формат телефонной маски (R16): последние 3 цифры
    остаются для корреляции. Используется и cabinet_service.mask_phone."""
    return "+7•••••••" + digits[-3:]


def _mask_phone(m: "re.Match") -> str:
    return format_masked_phone(re.sub(r"\D", "", m.group(0)))


def _mask_token_kv(m: "re.Match") -> str:
    return f"{m.group(1)}{m.group(2)}{m.group(3)[:6]}…"


def _mask_bearer(m: "re.Match") -> str:
    return f"{m.group(1)} {m.group(2)[:6]}…"


def mask_sensitive(text):
    """Маскирует телефоны/коды/токены в произвольной строке.

    Не-строки возвращаются как есть (оборонительно — фильтр может получить
    что угодно). Порядок: токены → коды → телефоны, чтобы телефонная маска
    не разрезала значение токена до его маскирования.
    """
    if not isinstance(text, str) or not text:
        return text
    text = mask_secrets(text)
    text = _CODE_RE.sub(r"\1••••", text)
    text = _PHONE_RE.sub(_mask_phone, text)
    return text


def mask_secrets(text):
    """Только секреты: адрес вебхука Битрикса, токены и секретные заголовки.
    Телефоны и коды не трогает — для журналов, где телефон нужен по делу
    (python_logs сессии в архиве диалога)."""
    if not isinstance(text, str) or not text:
        return text
    text = _BITRIX_WEBHOOK_RE.sub(r"\1•••", text)
    text = _SECRET_HEADER_RE.sub(_mask_token_kv, text)
    text = _TOKEN_KV_RE.sub(_mask_token_kv, text)
    text = _BEARER_RE.sub(_mask_bearer, text)
    return text


class MaskingFilter(logging.Filter):
    """Применяет mask_sensitive к УЖЕ отформатированному сообщению записи,
    а также к traceback (exc_info/exc_text) и stack_info.

    record.args учтены: getMessage() выполняет %-подстановку, результат
    склеивается в record.msg, args обнуляются — иначе маска, наложенная на
    шаблон, не увидела бы секрет, пришедший аргументом.

    Traceback: logger.exception() внутри except несёт текст исключения, где
    может оказаться телефон или code=NNNN (обёрнутые ошибки БД/Bitrix/хуков,
    R16). Formatter кеширует отрендеренный traceback в record.exc_text и
    ПЕРЕИСПОЛЬЗУЕТ уже заполненное значение — поэтому фильтр рендерит
    traceback сам, маскирует и кладёт в exc_text: до вывода дойдёт маска,
    а не оригинал.

    Любая ошибка внутри фильтра не должна ронять логирование — filter при
    любом исходе возвращает True.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = mask_sensitive(record.getMessage())
            record.args = None
        except Exception:
            pass  # битые args — не роняем логирование, пропускаем как есть
        try:
            self._mask_traceback(record)
        except Exception:
            pass
        return True

    @staticmethod
    def _mask_traceback(record: logging.LogRecord) -> None:
        exc_text = getattr(record, "exc_text", None)
        if not exc_text and record.exc_info:
            exc_info = record.exc_info
            if isinstance(exc_info, tuple) and len(exc_info) == 3:
                exc_text = "".join(traceback.format_exception(*exc_info)).rstrip("\n")
        if exc_text:
            # идемпотентно: повторный проход по уже маскированному тексту
            # ничего не меняет (маска не совпадает с исходными шаблонами)
            record.exc_text = mask_sensitive(exc_text)
        stack_info = getattr(record, "stack_info", None)
        if stack_info:
            record.stack_info = mask_sensitive(stack_info)


def install_masking(root_logger=None) -> None:
    """Вешает MaskingFilter на ВСЕ хендлеры корневого логгера.

    Идемпотентна: хендлер, уже несущий MaskingFilter, не получает второй.
    """
    root = root_logger if root_logger is not None else logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, MaskingFilter) for f in handler.filters):
            handler.addFilter(MaskingFilter())
