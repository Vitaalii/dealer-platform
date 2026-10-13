"""U2: SMS-шлюз ProstoSMS — классификатор ошибок, шаблоны, самопроверка (R9, R11, R32, R33)."""
import asyncio
import pathlib
import time

import pytest

import aiohttp

import sms_gateway
from sms_gateway import (
    BalanceResult,
    ProstoSmsGateway,
    Secret,
    SendResult,
    SmsGateway,
    build_otp_text,
    classify_err_code,
    get_sms_gateway,
    needs_config_alert,
    startup_selfcheck,
)

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"


# --- Моки aiohttp -----------------------------------------------------------

class FakeResponse:
    def __init__(self, payload=None, status=200, json_error=False):
        self._payload = payload
        self.status = status
        self._json_error = json_error

    async def json(self, content_type=None):
        if self._json_error:
            raise ValueError("not a json")
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RaisingCtx:
    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Каждый вызов post снимает следующий сценарий: dict-payload,
    FakeResponse или исключение (таймаут/сеть)."""

    def __init__(self, outcomes, calls):
        self._outcomes = outcomes
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, data=None):
        self._calls.append({"url": url, "data": dict(data or {})})
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            return _RaisingCtx(outcome)
        if isinstance(outcome, FakeResponse):
            return outcome
        return FakeResponse(payload=outcome)


def install_session(monkeypatch, outcomes):
    """Подменяет aiohttp.ClientSession; возвращает журнал вызовов post."""
    calls = []
    shared = list(outcomes)

    def factory(*args, **kwargs):
        # Грабли v2.1.8: таймаут обязан быть объектом aiohttp.ClientTimeout
        assert isinstance(kwargs.get("timeout"), aiohttp.ClientTimeout)
        return FakeSession(shared, calls)

    monkeypatch.setattr(sms_gateway.aiohttp, "ClientSession", factory)
    return calls


def ok_response(msg_id="12345", credits="99.50", err_code="0"):
    return {"response": {"msg": {"err_code": err_code, "text": "успех"},
                         "data": {"id": msg_id, "credits": credits, "n_raw_sms": "1"}}}


def err_response(code, text="ошибка"):
    return {"response": {"msg": {"err_code": code, "text": text}}}


def gw():
    return ProstoSmsGateway(api_key="test-key")


class StubGateway(SmsGateway):
    """Шлюз-заглушка для самопроверки: отдаёт заранее заданный BalanceResult."""

    def __init__(self, result=None, exc=None):
        self._result, self._exc = result, exc

    async def send_otp(self, phone, code, channel, external_id):
        raise AssertionError("не должен вызываться")

    async def get_balance(self):
        if self._exc:
            raise self._exc
        return self._result


# --- Happy path -------------------------------------------------------------

class TestSendOk:
    @pytest.mark.asyncio
    async def test_err_code_zero_is_ok(self, monkeypatch):
        calls = install_session(monkeypatch, [ok_response()])
        res = await gw().send_otp("79991234567", "4821", "sms", "ext-1")
        assert res.ok and res.error_class is None
        assert res.provider_msg_id == "12345"
        assert res.err_code == "0"
        data = calls[0]["data"]
        assert data["method"] == "push_msg" and data["format"] == "json"
        assert data["priority"] == "1" and data["external_id"] == "ext-1"
        assert data["key"] == "test-key"
        # sender_name не передаётся: имя определяет провайдер по info4u.pro
        assert "sender_name" not in data
        assert "route" not in data  # SMS — без route

    @pytest.mark.asyncio
    async def test_voice_text_is_code_only_no_sender_name(self, monkeypatch):
        calls = install_session(monkeypatch, [ok_response()])
        await gw().send_otp("79991234567", "4821", "voice", "ext-2")
        data = calls[0]["data"]
        assert data["route"] == "vc"
        # голосовой текст — строго цифры кода, больше ничего
        assert data["text"] == "4821"
        assert "sender_name" not in data

    @pytest.mark.asyncio
    async def test_sms_text_matches_registered_template_verbatim(self, monkeypatch):
        calls = install_session(monkeypatch, [ok_response()])
        await gw().send_otp("79991234567", "4821", "sms", "ext-3")
        text = calls[0]["data"]["text"]
        # Посимвольно = зарегистрированный шаблон «%d — код для входа…»,
        # тире в эталоне — escape U+2014, чтобы подмену символа было видно.
        assert text == "4821 — код для входа в кабинет. Никому не сообщайте."

    def test_templates_helper(self):
        assert build_otp_text("4821", "voice") == "4821"
        sms = build_otp_text("4821", "sms")
        assert sms.startswith("4821 — ") and sms.endswith("сообщайте.")
        # ни дефиса (U+002D), ни короткого тире (U+2013) вместо длинного
        assert "-" not in sms and "–" not in sms
        assert "—" in sms


# --- Классификатор ошибок ---------------------------------------------------

class TestErrorClasses:
    def test_classifier_table(self):
        assert classify_err_code("0") is None
        for code in ("99", "699"):
            assert classify_err_code(code) == "transport", code
        for code in ("3", "602", "623", "624", "625", "626", "627", "628",
                     "631", "645", "671", "607", "608", "620", "621", "632"):
            assert classify_err_code(code) == "service", code
        for code in ("617", "618", "622"):
            assert classify_err_code(code) == "subscriber", code

    @pytest.mark.asyncio
    async def test_623_no_credits_is_service_retry_free(self, monkeypatch):
        install_session(monkeypatch, [err_response("623", "недостаточно средств")])
        res = await gw().send_otp("79991234567", "4821", "sms", "e")
        assert not res.ok and res.error_class == "service" and res.retry_free

    @pytest.mark.asyncio
    async def test_627_is_service_and_config_alert(self, monkeypatch):
        install_session(monkeypatch, [err_response("627")])
        res = await gw().send_otp("79991234567", "4821", "sms", "e")
        assert res.error_class == "service"
        assert needs_config_alert(res.err_code)  # 602/627 — авария конфигурации
        assert needs_config_alert("602") and not needs_config_alert("623")

    @pytest.mark.asyncio
    async def test_618_blacklist_is_subscriber_retry_free(self, monkeypatch):
        install_session(monkeypatch, [err_response("618", "чёрный список")])
        res = await gw().send_otp("79991234567", "4821", "sms", "e")
        assert not res.ok and res.error_class == "subscriber" and res.retry_free

    @pytest.mark.asyncio
    async def test_timeout_is_transport_after_single_retry(self, monkeypatch):
        calls = install_session(monkeypatch, [asyncio.TimeoutError(), asyncio.TimeoutError()])
        res = await gw().send_otp("79991234567", "4821", "voice", "e")
        assert not res.ok and res.error_class == "transport" and res.retry_free
        assert len(calls) == 2  # ровно один ретрай

    @pytest.mark.asyncio
    async def test_timeout_then_success_recovers(self, monkeypatch):
        calls = install_session(monkeypatch, [asyncio.TimeoutError(), ok_response()])
        res = await gw().send_otp("79991234567", "4821", "sms", "e")
        assert res.ok and len(calls) == 2

    @pytest.mark.asyncio
    async def test_http_5xx_is_transport(self, monkeypatch):
        install_session(monkeypatch, [FakeResponse(status=502), FakeResponse(status=502)])
        res = await gw().send_otp("79991234567", "4821", "sms", "e")
        assert not res.ok and res.error_class == "transport"


# --- Оборонительный парсинг -------------------------------------------------

class TestDefensiveParsing:
    @pytest.mark.asyncio
    async def test_numeric_err_code_and_numeric_credits(self, monkeypatch):
        # err_code числом, id и credits числами — всё приводится к строкам
        payload = {"response": {"msg": {"err_code": 0},
                                "data": {"id": 456, "credits": 100}}}
        install_session(monkeypatch, [payload])
        res = await gw().send_otp("79991234567", "4821", "sms", "e")
        assert res.ok and res.provider_msg_id == "456" and res.err_code == "0"

    @pytest.mark.asyncio
    async def test_get_balance_credits_as_string_and_number(self, monkeypatch):
        install_session(monkeypatch, [ok_response(credits="1234.56")])
        bal = await gw().get_balance()
        assert isinstance(bal, BalanceResult) and bal.ok and bal.credits == "1234.56"

        install_session(monkeypatch, [ok_response(credits=500)])
        bal = await gw().get_balance()
        assert bal.ok and bal.credits == "500"

    @pytest.mark.asyncio
    async def test_numeric_error_code_classified(self, monkeypatch):
        install_session(monkeypatch, [err_response(623)])  # код числом
        res = await gw().send_otp("79991234567", "4821", "sms", "e")
        assert res.error_class == "service" and res.err_code == "623"

    @pytest.mark.asyncio
    async def test_malformed_body_does_not_crash(self, monkeypatch):
        install_session(monkeypatch, [{"совсем": "не то"}])
        res = await gw().send_otp("79991234567", "4821", "sms", "e")
        assert not res.ok and res.error_class == "service"  # неизвестное -> service

    @pytest.mark.asyncio
    async def test_non_json_body_is_transport(self, monkeypatch):
        install_session(monkeypatch, [FakeResponse(json_error=True), FakeResponse(json_error=True)])
        res = await gw().send_otp("79991234567", "4821", "sms", "e")
        assert not res.ok and res.error_class == "transport"


# --- AE14: ошибка service не расходует лимит клиента ------------------------

class TestClientLimitNotCharged:
    @pytest.mark.asyncio
    async def test_service_error_is_free_for_client(self, monkeypatch):
        """Covers AE14: недостаток средств -> попытка клиента не засчитана."""
        install_session(monkeypatch, [err_response("623", "недостаточно средств")])
        res = await gw().send_otp("79991234567", "4821", "sms", "e")
        # retry_free — контракт для U4: зачёт лимита только при err_code=0
        assert res.retry_free is True
        assert res.ok is False


# --- Самопроверка при старте (R32/R33) --------------------------------------

class TestStartupSelfcheck:
    @pytest.mark.asyncio
    async def test_gateway_down_does_not_raise(self, monkeypatch):
        """Недоступность ProstoSMS при старте не мешает init_app подняться."""
        alerts = []

        async def tech_alert(text):
            alerts.append(text)

        # и сетевое исключение, и transport-результат — оба безвредны
        await startup_selfcheck(tech_alert, gateway=StubGateway(exc=aiohttp.ClientError("down")))
        await startup_selfcheck(tech_alert, gateway=StubGateway(
            result=BalanceResult(ok=False, error_class="transport", description="таймаут")))
        assert alerts == []  # транспортная недоступность — не авария конфигурации

    @pytest.mark.asyncio
    async def test_627_fires_config_alert_with_ip_and_resolve(self, monkeypatch):
        monkeypatch.setattr(sms_gateway, "_outbound_ip", lambda: "45.92.179.193")
        monkeypatch.setattr(sms_gateway, "_resolve_gateway_host", lambda: "195.133.70.4")
        alerts = []

        async def tech_alert(text):
            alerts.append(text)

        await startup_selfcheck(tech_alert, gateway=StubGateway(
            result=BalanceResult(ok=False, error_class="service", err_code="627")))
        assert len(alerts) == 1
        assert "45.92.179.193" in alerts[0]  # исходящий IP (R33)
        assert "195.133.70.4" in alerts[0]  # резолв ssl.bs00.ru (R33)
        assert "627" in alerts[0]

    @pytest.mark.asyncio
    async def test_broken_tech_alert_is_swallowed(self):
        async def broken_alert(text):
            raise RuntimeError("MAX недоступен")

        await startup_selfcheck(broken_alert, gateway=StubGateway(
            result=BalanceResult(ok=False, error_class="service", err_code="602")))


# --- Маскирующая обёртка секретов -------------------------------------------

class TestSecret:
    def test_repr_and_str_are_masked(self):
        s = Secret("super-secret-key")
        assert "super-secret-key" not in repr(s)
        assert "super-secret-key" not in str(s)
        assert "super-secret-key" not in f"key={s}"  # f-string печатает маску

    def test_reveal_returns_real_value(self):
        assert Secret("super-secret-key").reveal() == "super-secret-key"

    def test_bool(self):
        assert Secret("x") and not Secret("")


# --- Роутер и wiring в main.py ----------------------------------------------

class TestRouterAndWiring:
    def test_router_returns_prostosms_and_caches(self):
        g1 = get_sms_gateway("prostosms")
        assert isinstance(g1, ProstoSmsGateway)
        assert get_sms_gateway("prostosms") is g1  # тот же инстанс
        assert isinstance(get_sms_gateway("неизвестный"), ProstoSmsGateway)  # дефолт

    def test_selfcheck_wired_fire_and_forget(self):
        """Wiring тонкий: create_task в start_background_tasks, запуск не ждёт."""
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "import sms_gateway" in src
        fire = src.index("asyncio.create_task(sms_gateway.startup_selfcheck(")
        start_fn = src.index("async def start_background_tasks")
        init_fn = src.index("async def init_app")
        assert start_fn < fire < init_fn, "самопроверка — fire-and-forget из start_background_tasks"
        assert "await sms_gateway.startup_selfcheck" not in src, "самопроверку нельзя ждать при старте"


class TestDevLogGateway:
    """CABINET_OTP_DEV_LOG: код в stdout/лог вместо отправки — только вне
    production. При ENVIRONMENT=production флаг игнорируется всегда."""

    @pytest.fixture(autouse=True)
    def _clean_registry(self, monkeypatch):
        monkeypatch.setattr(sms_gateway, "_GATEWAYS", {})
        monkeypatch.delenv("CABINET_OTP_DEV_LOG", raising=False)
        monkeypatch.delenv("ENVIRONMENT", raising=False)

    def test_flag_off_returns_prostosms(self):
        assert isinstance(sms_gateway.get_sms_gateway(),
                          sms_gateway.ProstoSmsGateway)

    def test_flag_on_outside_production_returns_dev_gateway(self, monkeypatch):
        monkeypatch.setenv("CABINET_OTP_DEV_LOG", "1")
        assert isinstance(sms_gateway.get_sms_gateway(),
                          sms_gateway.DevLogGateway)

    def test_flag_ignored_in_production(self, monkeypatch):
        """Требование: дев-режим НЕДОСТУПЕН при ENVIRONMENT=production
        ни при каких условиях."""
        monkeypatch.setenv("CABINET_OTP_DEV_LOG", "1")
        monkeypatch.setenv("ENVIRONMENT", "production")
        assert isinstance(sms_gateway.get_sms_gateway(),
                          sms_gateway.ProstoSmsGateway)
        # регистр не отравлен дев-шлюзом
        assert "dev-log" not in sms_gateway._GATEWAYS

    def test_flag_reread_each_call_no_cache_poisoning(self, monkeypatch):
        monkeypatch.setenv("CABINET_OTP_DEV_LOG", "1")
        dev = sms_gateway.get_sms_gateway()
        assert isinstance(dev, sms_gateway.DevLogGateway)
        monkeypatch.delenv("CABINET_OTP_DEV_LOG")
        assert isinstance(sms_gateway.get_sms_gateway(),
                          sms_gateway.ProstoSmsGateway)

    def test_send_prints_code_and_returns_ok(self, monkeypatch, capsys):
        monkeypatch.setenv("CABINET_OTP_DEV_LOG", "1")
        gw = sms_gateway.get_sms_gateway()
        result = asyncio.run(gw.send_otp("+79991234567", "4821", "voice", "ext-1"))
        assert result.ok and result.err_code == "0"
        out = capsys.readouterr().out
        assert "4 8 2 1" in out, "код обязан быть видим в stdout"
        assert "НЕ выполнялась" in out

    def test_balance_ok_without_network(self, monkeypatch):
        monkeypatch.setenv("CABINET_OTP_DEV_LOG", "1")
        gw = sms_gateway.get_sms_gateway()
        result = asyncio.run(gw.get_balance())
        assert result.ok and result.credits == "999999"


# --- План 2026-09-08-002: сторож информативен (U4) + парный лог (U1) --------

class TestConfigAlertText:
    @pytest.mark.asyncio
    async def test_alert_has_explanation_internal_context_and_masked_key(self, monkeypatch):
        """[Поправка U4 2026-09-08] Замер внешнего адреса через сторонний
        сервис убран: у контейнера два исходящих канала, ipify мерил дорогу
        к Cloudflare, не к провайдеру. Алерт честно объясняет это вместо
        показа адреса-с-другой-дороги; сторонние сервисы не дёргаются."""
        monkeypatch.setattr(sms_gateway, "_outbound_ip", lambda: "192.168.0.47")
        monkeypatch.setattr(sms_gateway, "_resolve_gateway_host", lambda: "195.133.70.4")
        fake_key = "Ufno" + "x" * 56 + "1f5d"
        monkeypatch.setenv("PROSTOSMS_API_KEY", fake_key)
        text = await sms_gateway.build_config_alert_text(
            "627", "Некорректный API KEY", context="get_balance (тест)")
        assert "Внешний адрес на пути к ProstoSMS изнутри не определить" in text
        assert "два исходящих канала" in text                    # причина названа
        assert "api.ipify.org" not in text and "103.112.69.233" not in text
        assert "Внутренний IP контейнера: 192.168.0.47" in text  # помечен явно
        assert "Контекст: get_balance (тест)" in text
        assert "Ufno…1f5d (64 симв.)" in text and "A-42189" in text
        assert fake_key not in text                              # ключ только маской

    @pytest.mark.asyncio
    async def test_alert_build_makes_no_http_requests(self, monkeypatch):
        """Ложный адрес хуже отсутствующего: сборка алерта не имеет права
        ходить ни в какие эхо-сервисы."""
        def boom(*a, **kw):
            raise AssertionError("сборка алерта не должна делать HTTP-запросов")
        monkeypatch.setattr(sms_gateway.aiohttp, "ClientSession", boom)
        monkeypatch.setattr(sms_gateway, "_outbound_ip", lambda: "192.168.0.47")
        monkeypatch.setattr(sms_gateway, "_resolve_gateway_host", lambda: "195.133.70.4")
        text = await sms_gateway.build_config_alert_text("602")
        assert "не определить" in text

    @pytest.mark.asyncio
    async def test_short_key_not_leaked(self, monkeypatch):
        monkeypatch.setenv("PROSTOSMS_API_KEY", "shortkey")
        text = await sms_gateway.build_config_alert_text("602")
        assert "не задан или короче 12 символов" in text
        assert "shortkey" not in text


class TestCheckBalanceLogging:
    @pytest.fixture(autouse=True)
    def _fresh_alert_state(self, monkeypatch):
        """[Р11/Р12] Без пауз и с чистым окном дедупа в каждом тесте."""
        import cabinet_service
        monkeypatch.setenv("CABINET_ALERT_RETRY_DELAY", "0")
        cabinet_service.reset_config_alert_dedup()
        yield
        cabinet_service.reset_config_alert_dedup()

    @pytest.mark.asyncio
    async def test_not_ok_logged_even_when_alert_fires(self, monkeypatch, caplog):
        """[U1] Два 627 за 09-08 не оставили в логе ни строки: warning обязан
        писаться и в ветке с отправленным алертом; алерт несёт контекст U4."""
        import logging

        import cabinet_service
        alerts = []

        async def tech(text):
            alerts.append(text)
        monkeypatch.setattr(cabinet_service, "_tech_alert", tech)
        monkeypatch.setattr(cabinet_service, "_gateway", lambda: StubGateway(
            result=BalanceResult(ok=False, error_class="service", err_code="627",
                                 description="Некорректный API KEY")))
        with caplog.at_level(logging.WARNING, logger="cabinet"):
            await cabinet_service.check_balance()
        assert len(alerts) == 1
        assert "Контекст: get_balance (плановая проверка баланса" in alerts[0]
        assert any("проверка баланса не прошла" in r.getMessage()
                   for r in caplog.records), \
            "лог не-ok обязан писаться и при отправленном алерте (U1)"

    @pytest.mark.asyncio
    async def test_ok_balance_no_warning_no_alert(self, monkeypatch, caplog):
        import logging

        import cabinet_service
        alerts = []

        async def tech(text):
            alerts.append(text)
        monkeypatch.setattr(cabinet_service, "_tech_alert", tech)
        monkeypatch.setattr(cabinet_service, "_gateway", lambda: StubGateway(
            result=BalanceResult(ok=True, credits="1000")))
        with caplog.at_level(logging.WARNING, logger="cabinet"):
            await cabinet_service.check_balance()
        assert alerts == []
        assert not any("проверка баланса не прошла" in r.getMessage()
                       for r in caplog.records)


# --- Р11/Р12 поправка владельца 2026-09-08: повтор перед алертом, дедуп ------

class SeqGateway(SmsGateway):
    """get_balance отдаёт результаты по очереди (для ретрая Р11)."""

    def __init__(self, results):
        self._results = list(results)

    async def send_otp(self, phone, code, channel, external_id):
        raise AssertionError("не должен вызываться")

    async def get_balance(self):
        return self._results.pop(0)


def _bal_627():
    return BalanceResult(ok=False, error_class="service", err_code="627",
                         description="Некорректный API KEY")


def _bal_ok():
    return BalanceResult(ok=True, credits="1000")


class TestConfigAlertRetryAndDedup:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        import cabinet_service
        monkeypatch.setenv("CABINET_ALERT_RETRY_DELAY", "0")
        cabinet_service.reset_config_alert_dedup()
        yield
        cabinet_service.reset_config_alert_dedup()

    def _wire(self, monkeypatch, results):
        import cabinet_service
        alerts = []

        async def tech(text):
            alerts.append(text)
        monkeypatch.setattr(cabinet_service, "_tech_alert", tech)
        gw = SeqGateway(results)
        monkeypatch.setattr(cabinet_service, "_gateway", lambda: gw)
        return cabinet_service, alerts

    @pytest.mark.asyncio
    async def test_self_healed_episode_no_alert(self, monkeypatch, caplog):
        """[Р11] 627 → повтор ok: эпизод маршрутизации самоизлечился,
        владельца не будим."""
        import logging
        svc, alerts = self._wire(monkeypatch, [_bal_627(), _bal_ok()])
        with caplog.at_level(logging.INFO, logger="cabinet"):
            await svc.check_balance()
        assert alerts == []
        assert any("самоизлечился" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_persistent_627_alerts_once_with_next_step(self, monkeypatch):
        """[Р11] 627 → повтор тоже 627: один полный алерт со следующим шагом."""
        svc, alerts = self._wire(monkeypatch, [_bal_627(), _bal_627()])
        await svc.check_balance()
        assert len(alerts) == 1
        assert "Следующий шаг: запросить у поддержки ProstoSMS" in alerts[0]
        assert "202.133.219.44" in alerts[0]  # прецедент назван

    @pytest.mark.asyncio
    async def test_repeat_in_window_suppressed_then_escalates(self, monkeypatch, caplog):
        """[Р12] Тик 1 — полный алерт; тик 2 — TECH_ALERT_SUPPRESSED без
        сообщения; тик 3 — один эскалационный алерт «ПОВТОРЯЕТСЯ»."""
        import logging
        svc, alerts = self._wire(monkeypatch, [_bal_627()] * 6)
        with caplog.at_level(logging.WARNING, logger="cabinet"):
            await svc.check_balance()   # тик 1: полный алерт
            await svc.check_balance()   # тик 2: подавлен
            await svc.check_balance()   # тик 3: эскалация
        assert len(alerts) == 2
        assert "ошибка конфигурации" in alerts[0]
        assert "ПОВТОРЯЕТСЯ" in alerts[1] and "3 случаев" in alerts[1]
        assert "Следующий шаг" in alerts[1]
        suppressed = [r for r in caplog.records
                      if "TECH_ALERT_SUPPRESSED" in r.getMessage()]
        assert len(suppressed) == 1, "подавленный повтор оставляет след в логе"

    @pytest.mark.asyncio
    async def test_ok_between_incidents_resets_window(self, monkeypatch):
        """[Р12] Успешная проверка закрывает окно: следующий отказ снова
        получает полный алерт, а не подавление."""
        svc, alerts = self._wire(monkeypatch, [
            _bal_627(), _bal_627(),   # тик 1: инцидент → полный алерт
            _bal_ok(),                # тик 2: канал жив → окно закрыто
            _bal_627(), _bal_627(),   # тик 3: новый инцидент → полный алерт
        ])
        await svc.check_balance()
        await svc.check_balance()
        await svc.check_balance()
        assert len(alerts) == 2
        assert all("ошибка конфигурации" in a for a in alerts)
