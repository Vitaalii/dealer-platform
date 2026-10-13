"""U2: SMS-шлюз ProstoSMS (R9, R11, R32, R33).

Транспорт без состояния: шлюз не трогает БД, возвращает `SendResult` с единым
классом ошибки — единственным источником классификации для breaker'а (U3).
Паттерн — ABC + флаги (`base_client.py`), роутер — по образцу
`get_client` (`main.py`). Здесь же живёт базовый маскирующий тип секретов
`Secret` — U9 импортирует его отсюда, не создавая цикла.

Адрес — только https://ssl.bs00.ru (незашифрованный api.sms-prosto.ru
недопустим для кодов). Таймауты — строго `aiohttp.ClientTimeout`
(см. docs/CHANGELOG.md v2.1.8: числовой timeout — грабли).
"""
import asyncio
import logging
import os
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

PROSTOSMS_HOST = "ssl.bs00.ru"
PROSTOSMS_URL = f"https://{PROSTOSMS_HOST}/"
PROSTOSMS_KNOWN_IP = "195.133.70.4"  # единственная A-запись на момент проектирования

OTP_TTL_MINUTES = 5

# Голосовой канал: робот провайдера говорит «проверочный шифр», затем цифры кода
# (живая проверка v3.4.2, 12.10.2026); бот передаёт только код.
# Текст = только код: при молчаливой подмене звонка на SMS (повторный запрос
# звонка в 5-минутном окне провайдера) сообщение уложится в шаблон «%d».
VOICE_TEXT_TEMPLATE = "{code}"
# ТЕКСТ ЗАРЕГИСТРИРОВАН У ПРОВАЙДЕРА КАК ШАБЛОН — посимвольно, включая
# длинное тире (U+2014, ниже записано escape-последовательностью, чтобы
# подмена символа была видна в диффе), пробелы и точку. Менять ТОЛЬКО
# вместе с перерегистрацией шаблона в ProstoSMS, иначе отправка будет отбита.
SMS_TEXT_TEMPLATE = "{code} \u2014 код для входа в кабинет. Никому не сообщайте."

_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15)

# --- Единый классификатор ошибок push_msg/get_profile (таблица плана U2) ---
# transport: сеть, таймаут, HTTP 5xx + коды провайдера 99/699
TRANSPORT_CODES = frozenset({"99", "699"})
# service: авария на нашей стороне/у провайдера — попытку клиенту не засчитывать
SERVICE_CODES = frozenset({
    "3", "602", "623", "624", "625", "626", "627", "628", "631", "645", "671",
    # проблемы с именем отправителя
    "607", "608", "620", "621", "632",
})
# subscriber: проблема номера получателя — breaker НЕ считает
SUBSCRIBER_CODES = frozenset({"617", "618", "622"})
# 602/627 не различают «неверный ключ» и «запрос с незаявленного адреса» —
# алерт обязан содержать исходящий IP и резолв ssl.bs00.ru (R33)
CONFIG_ALERT_CODES = frozenset({"602", "627"})


class Secret:
    """Базовый маскирующий тип секретов (импортируется U9).

    `repr()`/`str()`/f-string дают маску — случайный лог или traceback
    печатает `***`, не значение. Реальное значение — только через `reveal()`.
    """
    __slots__ = ("_value",)
    _MASK = "***"

    def __init__(self, value):
        self._value = value

    def reveal(self):
        """Единственный способ получить реальное значение."""
        return self._value

    def __repr__(self) -> str:
        return f"{type(self).__name__}('{self._MASK}')"

    def __str__(self) -> str:
        return self._MASK

    def __bool__(self) -> bool:
        return bool(self._value)


def classify_err_code(err_code) -> Optional[str]:
    """Класс ошибки по err_code провайдера; None — успех (err_code=0).

    Оборонительно: err_code может прийти числом или строкой.
    Неизвестный код считаем `service` — breaker его учитывает, попытка
    клиенту не засчитывается.
    """
    code = str(err_code).strip()
    if code == "0":
        return None
    if code in TRANSPORT_CODES:
        return "transport"
    if code in SUBSCRIBER_CODES:
        return "subscriber"
    return "service"


def needs_config_alert(err_code) -> bool:
    """602/627 — авария конфигурации: техалерт с IP и резолвом (R32/R33)."""
    return str(err_code).strip() in CONFIG_ALERT_CODES


@dataclass
class SendResult:
    """Результат отправки. error_class — единственный источник классификации.

    retry_free=True — попытка НЕ расходует лимит клиента (R11: зачёт только
    при err_code=0, т.е. любой неуспех бесплатен для клиента).
    """
    ok: bool
    error_class: Optional[str] = None  # 'transport' | 'service' | 'subscriber' | None
    retry_free: bool = False
    provider_msg_id: Optional[str] = None
    err_code: Optional[str] = None
    description: str = ""


@dataclass
class BalanceResult:
    """Результат get_profile: баланс + те же err_code/класс для самопроверки."""
    ok: bool
    credits: Optional[str] = None  # всегда строка — провайдер шлёт и числом, и строкой
    error_class: Optional[str] = None
    err_code: Optional[str] = None
    description: str = ""


class SmsGateway(ABC):
    """Абстракция шлюза доставки кодов (по образцу BaseBotClient)."""

    @abstractmethod
    async def send_otp(self, phone: str, code: str, channel: str, external_id: str) -> SendResult:
        """channel: 'voice' — голосовой робот, 'sms' — обычное сообщение."""

    @abstractmethod
    async def get_balance(self) -> BalanceResult:
        pass

    def supports_voice(self) -> bool:
        """Поддерживает ли провайдер голосовую доставку кода."""
        return False


def build_otp_text(code: str, channel: str) -> str:
    if channel == "voice":
        return VOICE_TEXT_TEMPLATE.format(code=code)
    return SMS_TEXT_TEMPLATE.format(code=code)


def _dig(payload, *path):
    """Оборонительный проход по вложенным dict: любой сбой -> None."""
    cur = payload
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _as_str(value) -> Optional[str]:
    """Значения ответа приводятся к строкам; None остаётся None."""
    if value is None:
        return None
    return str(value)


class ProstoSmsGateway(SmsGateway):
    """Клиент ProstoSMS: push_msg / get_profile, POST format=json.

    Только https://ssl.bs00.ru. priority=1 закреплён документацией за OTP.
    route=vc — голосовой робот. sender_name не передаётся: имя отправителя
    определяет провайдер по зарегистрированному info4u.pro (решение владельца,
    план 2026-08-27-001). Один ретрай при таймауте/сети/5xx/99/699,
    затем класс transport.
    """

    def __init__(self, api_key=None,
                 url: str = PROSTOSMS_URL, timeout: aiohttp.ClientTimeout = _DEFAULT_TIMEOUT):
        if api_key is None:
            api_key = os.getenv("PROSTOSMS_API_KEY", "")
        self._api_key = api_key if isinstance(api_key, Secret) else Secret(api_key)
        self._url = url
        self._timeout = timeout

    def supports_voice(self) -> bool:
        return True

    async def send_otp(self, phone: str, code: str, channel: str, external_id: str) -> SendResult:
        params = {
            "method": "push_msg",
            "format": "json",
            "key": self._api_key.reveal(),
            "phone": phone,
            "text": build_otp_text(code, channel),
            "priority": "1",  # закреплён документацией за кодами авторизации
            "external_id": external_id,
        }
        if channel == "voice":
            params["route"] = "vc"

        outcome = await self._request(params)
        if isinstance(outcome, SendResult):  # transport-неудача после ретрая
            return outcome

        err_code = _as_str(_dig(outcome, "response", "msg", "err_code"))
        error_class = classify_err_code(err_code if err_code is not None else "")
        description = _as_str(_dig(outcome, "response", "msg", "text")) or ""
        if error_class is None:
            return SendResult(
                ok=True,
                provider_msg_id=_as_str(_dig(outcome, "response", "data", "id")),
                err_code=err_code,
                description=description,
            )
        return SendResult(
            ok=False,
            error_class=error_class,
            retry_free=True,  # R11: зачёт попытки только при err_code=0
            err_code=err_code,
            description=description,
        )

    async def get_balance(self) -> BalanceResult:
        params = {"method": "get_profile", "format": "json", "key": self._api_key.reveal()}
        outcome = await self._request(params)
        if isinstance(outcome, SendResult):
            return BalanceResult(ok=False, error_class="transport",
                                 err_code=outcome.err_code, description=outcome.description)

        err_code = _as_str(_dig(outcome, "response", "msg", "err_code"))
        error_class = classify_err_code(err_code if err_code is not None else "")
        description = _as_str(_dig(outcome, "response", "msg", "text")) or ""
        # credits может прийти и числом, и строкой — приводим к строке
        credits = _as_str(_dig(outcome, "response", "data", "credits"))
        if error_class is None:
            return BalanceResult(ok=True, credits=credits, err_code=err_code, description=description)
        return BalanceResult(ok=False, error_class=error_class, err_code=err_code, description=description)

    async def _request(self, params: dict):
        """POST с одним ретраем transport-неудач.

        Возвращает распарсенный JSON (dict) либо готовый transport-SendResult.
        Ключ API в логи не попадает.
        """
        last_desc, last_code = "нет ответа", None
        for attempt in (0, 1):
            try:
                async with aiohttp.ClientSession(timeout=self._timeout) as session:
                    async with session.post(self._url, data=params) as resp:
                        if resp.status >= 500:
                            last_desc, last_code = f"HTTP {resp.status}", None
                            continue  # transport: один ретрай
                        try:
                            body = await resp.json(content_type=None)
                        except Exception:
                            last_desc, last_code = "неразбираемый ответ провайдера", None
                            continue
                        provider_code = _as_str(_dig(body, "response", "msg", "err_code"))
                        if provider_code in TRANSPORT_CODES and attempt == 0:
                            last_desc, last_code = f"код провайдера {provider_code}", provider_code
                            continue  # 99/699: один ретрай
                        return body
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
                last_desc, last_code = f"{type(e).__name__}: {e}", None
                logger.warning("ProstoSMS: попытка %d не удалась: %s", attempt + 1, last_desc)
        return SendResult(ok=False, error_class="transport", retry_free=True,
                          err_code=last_code, description=last_desc)


# --- Роутер по образцу get_client (main.py) ---
_GATEWAYS: dict = {}


class DevLogGateway(SmsGateway):
    """Дев-режим: код пишется в stdout/лог вместо отправки провайдеру.

    Для локального стенда, где исходящий IP не входит в подсеть, заявленную
    ProstoSMS (сервис отвечал бы 602/627). Выбирается флагом окружения
    CABINET_OTP_DEV_LOG=1 и НЕДОСТУПЕН при ENVIRONMENT=production ни при
    каких условиях — см. get_sms_gateway().
    """

    async def send_otp(self, phone: str, code: str, channel: str, external_id: str) -> SendResult:
        spaced = " ".join(code)  # пробелы: фильтр маскирования ловит слитные 4 цифры
        banner = (f"[DEV-OTP] канал={channel} телефон={phone} → "
                  f"К О Д : {spaced}  (отправка провайдеру НЕ выполнялась)")
        print(banner, flush=True)  # stdout — мимо логирующего конвейера с маскированием
        logger.info("[DEV-OTP] выдан код для %s (%s), external_id=%s — "
                    "значение в stdout", phone, channel, external_id)
        return SendResult(ok=True, provider_msg_id=f"dev-{external_id}",
                          err_code="0", description="dev-log, без отправки")

    async def get_balance(self) -> BalanceResult:
        return BalanceResult(ok=True, credits="999999",
                             description="dev-log, баланс фиктивный")


def _dev_otp_log_enabled() -> bool:
    """Флаг дев-режима; при ENVIRONMENT=production игнорируется всегда."""
    if os.getenv("ENVIRONMENT", "").strip().lower() == "production":
        return False
    return os.getenv("CABINET_OTP_DEV_LOG", "").strip().lower() in ("1", "true", "yes")


def get_sms_gateway(provider: Optional[str] = None) -> SmsGateway:
    """Выбор провайдера конфигом; MVP — единственная реализация ProstoSMS.

    CABINET_OTP_DEV_LOG=1 (вне production) подменяет шлюз на DevLogGateway —
    флаг перечитывается на каждый вызов, кэш прод-шлюза не подменяется."""
    if _dev_otp_log_enabled():
        if "dev-log" not in _GATEWAYS:
            _GATEWAYS["dev-log"] = DevLogGateway()
        return _GATEWAYS["dev-log"]
    provider = (provider or os.getenv("SMS_GATEWAY_PROVIDER", "prostosms")).lower()
    if provider not in _GATEWAYS:
        _GATEWAYS[provider] = ProstoSmsGateway()  # неизвестный провайдер -> дефолт
    return _GATEWAYS[provider]


# --- Стартовая самопроверка канала (R32/R33), wiring в main.py — тонкий ---

def _resolve_gateway_host() -> str:
    try:
        _, _, addrs = socket.gethostbyname_ex(PROSTOSMS_HOST)
        return ", ".join(addrs) if addrs else "пусто"
    except OSError as e:
        return f"резолв не удался: {e}"


def _outbound_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((PROSTOSMS_KNOWN_IP, 443))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError as e:
        return f"не определён: {e}"


# --- [U4 план 2026-09-08-002 + поправка 2026-09-08] Внешний адрес ----------
# Замер через api.ipify.org ВРАЛ: у контейнера Dockhost ДВА исходящих канала
# (РФ 45.92.179.192/28 и зарубежный), выбор — по направлению трафика; ipify
# за Cloudflare → всегда зарубежная дорога (103.112.69.233), а к российскому
# ssl.bs00.ru трафик идёт другой дорогой (замер владельца: ifconfig.me →
# 45.92.179.194). Адрес на пути к ПРОВАЙДЕРУ изнутри контейнера не знает
# никто, кроме самого получателя; метода «покажи адрес вызывающего» у API
# ProstoSMS нет (push_msg/get_msg_report/get_profile/get_prices/wait_call).
# Ложный адрес хуже отсутствующего — алерт честно говорит «не определить».

EXTERNAL_IP_EXPLANATION = (
    "Внешний адрес на пути к ProstoSMS изнутри не определить: у контейнера "
    "два исходящих канала (РФ 45.92.179.192/28 и зарубежный), выбор — по "
    "направлению трафика; адрес видит только получатель "
    "(ответ Dockhost 2026-09-08)."
)

# [Р13 поправка владельца 2026-09-08] Алерт обязан называть следующий шаг,
# а не только объяснять отсутствие адреса: путь от алерта до диагноза занял
# полдня и три ложные версии. Виновным оказался 202.133.219.44 — адрес,
# которого не знали ни мы, ни хостинг; показали его только логи провайдера.
NEXT_STEP_HINT = (
    "Следующий шаг: запросить у поддержки ProstoSMS (аккаунт A-42189) "
    "исходные IP запросов по ключу за период отказа — только их логи "
    "показывают фактический адрес (прецедент 2026-09-08: виновным был "
    "202.133.219.44, не известный ни нам, ни хостингу)."
)


def masked_api_key() -> str:
    """Маска ключа для алертов: первые4…последние4 и длина (U4). Прод читает
    ключ из PROSTOSMS_API_KEY (ProstoSmsGateway.__init__), env — источник."""
    key = os.getenv("PROSTOSMS_API_KEY", "")
    if len(key) < 12:
        return "не задан или короче 12 символов"
    return f"{key[:4]}…{key[-4:]} ({len(key)} симв.)"


async def build_config_alert_text(err_code, description: str = "", context: str = "") -> str:
    """Текст техалерта по 602/627 (R33 + U4 план 2026-09-08-002, поправка
    владельца 2026-09-08): контекст вызова (операция/получатель), честное
    объяснение про внешний адрес вместо ложного замера, маска ключа;
    внутренний IP помечен явно, резолв хоста — отдельной строкой."""
    internal_ip, resolved = await asyncio.to_thread(
        lambda: (_outbound_ip(), _resolve_gateway_host()))
    context_line = f"Контекст: {context}\n" if context else ""
    return (
        f"🚨 ProstoSMS: ошибка конфигурации (код {err_code}). {description}\n"
        f"Код не различает «неверный ключ» и «запрос с незаявленного адреса».\n"
        f"{context_line}"
        f"{EXTERNAL_IP_EXPLANATION}\n"
        f"{NEXT_STEP_HINT}\n"
        f"Внутренний IP контейнера: {internal_ip}\n"
        f"Резолв {PROSTOSMS_HOST}: {resolved}\n"
        f"Ключ: {masked_api_key()}, аккаунт A-42189"
    )


async def startup_selfcheck(tech_alert, gateway: Optional[SmsGateway] = None) -> None:
    """Самопроверка get_balance при старте. НИКОГДА не мешает запуску бота:
    любые ошибки глотаются, вызывается fire-and-forget задачей из main.py."""
    try:
        gw = gateway or get_sms_gateway()
        result = await gw.get_balance()
        if result.ok:
            logger.info("ProstoSMS: самопроверка ok, баланс %s", result.credits)
        elif needs_config_alert(result.err_code):
            text = await build_config_alert_text(
                result.err_code, result.description,
                context="get_balance (самопроверка при старте)")
            await tech_alert(text)
        else:
            logger.warning("ProstoSMS: самопроверка не прошла (%s: %s)",
                           result.err_code, result.description)
    except Exception as e:  # самопроверка никогда не роняет старт
        logger.warning("ProstoSMS: самопроверка недоступна: %s", e)
