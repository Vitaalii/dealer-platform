"""U1 (план 2026-09-15-001): единая сборка/разборка составного внутреннего ID.

Идентичность клиента = (бот, ID мессенджера) — R4/KTD-5. Внутренний ID для
новых ботов: "{bot_key}:{raw_id}"; легаси-боты (max-main, tg-main) и web
сохраняют голые ID — существующие записи БД не переименовываются, миграция
данных нулевая. ВСЯ сборка/разборка — только через этот модуль (R10):
никакой код не должен склеивать или резать ID по разделителю сам.

Модуль строгого листа: только stdlib, не импортирует ни main.py, ни
database.py — используется и приложением, и скриптами, и тестами.
"""
import hashlib
import hmac
import re

SEPARATOR = ":"

# Ключи с ГОЛЫМИ внутренними ID (KTD-5, R11): web, исторические max-main/
# tg-main и их переименования U9 (решение владельца 2026-09-22: max-geely/
# tg-geely на проде, max-test/tg-test на деве). Пользователи этих ботов
# живут в БД с голыми ID со времён однобота — составной ID для них создал
# бы вторую строку того же человека. Составные ID — только у ботов,
# добавленных ПОСЛЕ мультибота (max-haval/tg-haval и будущие бренды).
LEGACY_BOT_KEYS = frozenset({
    "web", "max-main", "tg-main",
    "max-geely", "tg-geely", "max-test", "tg-test",
})

# Исторические имена легаси-ключей (до U9) → канал; используются резолвером
# алиасов bot_registry на переходный период (ctx/users/URL со старым ключом).
HISTORIC_LEGACY_KEYS = {"max-main": "max", "tg-main": "telegram"}

# Формат bot_key — строго [a-z0-9-] (KTD-1): ключ входит в session_id,
# который n8n подставляет в SQL склейкой строк без экранирования.
BOT_KEY_RE = re.compile(r"^[a-z0-9-]+$")

# Dialogflow ES ограничивает session id 36 байтами; main.py собирает его как
# f"user_{id}" — на долю ID остаётся 31 символ.
_DF_MAX_LEN = 31


def is_valid_bot_key(bot_key) -> bool:
    """Строгая проверка формата ключа бота (KTD-1)."""
    return bool(bot_key) and bool(BOT_KEY_RE.match(str(bot_key)))


def build_internal_id(bot_key, raw_id) -> str:
    """Внутренний ID пользователя: составной для новых ботов, голый для легаси.

    Пустой/легаси bot_key -> голый raw_id (обратная совместимость, R7)."""
    raw = str(raw_id)
    if not bot_key or bot_key in LEGACY_BOT_KEYS:
        return raw
    return f"{bot_key}{SEPARATOR}{raw}"


def parse_internal_id(internal_id):
    """(bot_key | None, raw_id). Голый ID -> (None, id).

    Составным считается только ID с валидным bot_key до разделителя и
    непустым хвостом — произвольная строка с ':' не разрезается ложно.
    split c maxsplit=1: ':' внутри raw-части не ломает разбор."""
    s = str(internal_id)
    if SEPARATOR in s:
        head, tail = s.split(SEPARATOR, 1)
        if tail and BOT_KEY_RE.match(head):
            return head, tail
    return None, s


def raw_id_for_send(internal_id) -> str:
    """«Чистый» ID мессенджера для исходящих вызовов API (KTD-6)."""
    return parse_internal_id(internal_id)[1]


def df_session_id(internal_id) -> str:
    """Санитизированный ID для ресурсных имён Dialogflow (KTD-5).

    ':' в GCP-путях — служебный символ (поведение не установлено), поэтому
    все символы вне [A-Za-z0-9_-] заменяются на '-'. Длина ограничена 31
    символом (лимит DF ES 36 минус префикс "user_"): длинный ID детерминированно
    сворачивается в head + хэш-хвост, коллизии практически исключены."""
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "-", str(internal_id))
    if len(sanitized) <= _DF_MAX_LEN:
        return sanitized
    digest = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()[:10]
    return f"{sanitized[:_DF_MAX_LEN - 11]}-{digest}"


def derive_webhook_secret(token: str, bot_key: str) -> str:
    """Производный секрет вебхука (KTD-3, R8): HMAC-SHA256 от токена, hex.

    Необратим, отдельных секретов не заводим. Hex-алфавит [0-9a-f] проходит
    ограничения и Telegram (A-Za-z0-9_-, 1-256), и MAX (A-Za-z0-9-, 5-256)."""
    return hmac.new(
        str(token).encode("utf-8"),
        f"webhook-secret{SEPARATOR}{bot_key}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
