"""U9: изоляция логов и маскирование (R16, R4; AE9).

- mask_sensitive: телефоны РФ в любых форматах, OTP-код в code-контексте,
  токены в query/json/Bearer; легитимные числа (год, deal_id, суммы) живут.
- MaskingFilter/install_masking: фильтр на ХЕНДЛЕРАХ корневого логгера —
  записи дочерних логгеров (aiohttp.access) тоже маскируются; идемпотентность.
- AE9: искусственная ошибка хендлера /cabinet/api/* с телефоном и кодом в
  теле → алерт Global Error Middleware несёт маску телефона и НЕ несёт код.
- Source-grep: wiring в main.py (install_masking после basicConfig,
  mask_sensitive на тексте алерта), кабинет не вызывает log_session.

repr()/str() Secret покрыты в U2 (tests/test_sms_gateway.py) — не дублируются.
"""
import io
import logging
import pathlib

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import log_masking
from log_masking import MaskingFilter, install_masking, mask_sensitive

import main as main_mod

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAIN_PY = ROOT / "main.py"

PHONE = "79161234567"
PHONE_MASK = "+7•••••••567"


# =======================
# Happy: mask_sensitive — телефоны
# =======================

@pytest.mark.parametrize("raw", [
    "+79161234567",
    "79161234567",
    "89161234567",
    "+7 916 123-45-67",
    "8 (916) 123-45-67",
    "+7-916-123-45-67",
])
def test_mask_phone_formats(raw):
    out = mask_sensitive(f"звонок на {raw} завершён")
    assert PHONE_MASK in out
    # ни одного 4+ значного куска исходного номера в открытом виде
    assert "1234567" not in out
    assert raw not in out


def test_mask_phone_in_query_string():
    line = f"GET /sms/status-callback?phone={PHONE}&external_id=cab-42&state=deliver"
    out = mask_sensitive(line)
    assert PHONE not in out
    assert PHONE_MASK in out
    assert "external_id=cab-42" in out  # не-секреты не тронуты


# =======================
# Happy: mask_sensitive — код в code-контексте
# =======================

@pytest.mark.parametrize("raw,gone", [
    ("code=4821", "4821"),
    ('{"code": 4821}', "4821"),
    ('{"code": "4821"}', "4821"),
    ("code: 4821", "4821"),
])
def test_mask_code_contexts(raw, gone):
    out = mask_sensitive(f"verify body {raw} end")
    assert gone not in out  # AE9: код отсутствует полностью
    assert "••••" in out


# =======================
# Happy: mask_sensitive — токены
# =======================

def test_mask_token_query_t():
    out = mask_sensitive("init t=AbCdEf123456XyZ ok")
    assert "AbCdEf123456XyZ" not in out
    assert "t=AbCdEf…" in out


@pytest.mark.parametrize("raw,masked", [
    ("token=secrettokenvalue", "token=secret…"),
    ('"access_token": "aaaabbbbccccdddd"', "aaaabb…"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload", "Bearer eyJhbG…"),
])
def test_mask_token_contexts(raw, masked):
    out = mask_sensitive(f"req {raw} done")
    assert masked in out
    assert "tokenvalue" not in out
    assert "ccccdddd" not in out
    assert "IUzI1NiJ9" not in out


# =======================
# Edge: легитимные значения НЕ маскируются
# =======================

@pytest.mark.parametrize("text", [
    "выпуск 2024 года",
    "deal_id 12345 обновлён",
    "deal_id=12345 обновлён",
    "сумма 500000 руб",
    "пробег 1234 км",          # 4 цифры вне code-контекста
    "state=1234",               # не code-ключ
    "id 12345678901",           # 11 цифр, но не 7/8+9xx
    "start=123456789012",       # 't=' внутри слова — не токен-контекст
])
def test_legit_numbers_survive(text):
    assert mask_sensitive(text) == text


def test_non_string_passthrough():
    assert mask_sensitive(None) is None
    assert mask_sensitive(12345) == 12345
    assert mask_sensitive("") == ""


# =======================
# MaskingFilter: args учтены (склейка в msg, args=None)
# =======================

def test_filter_formats_args_then_masks():
    record = logging.LogRecord(
        "cabinet", logging.INFO, __file__, 1,
        "otp для %s, code=%s", (PHONE, "4821"), None,
    )
    assert MaskingFilter().filter(record) is True
    assert record.args is None
    assert PHONE not in record.msg
    assert PHONE_MASK in record.msg
    assert "4821" not in record.msg
    assert record.getMessage() == record.msg  # запись остаётся форматируемой


# =======================
# install_masking: хендлеры корневого, дочерние логгеры, идемпотентность
# =======================

@pytest.fixture
def fake_root():
    """Изолированный 'корневой' логгер с StringIO-хендлером (глобальный root
    не трогаем, чтобы не цеплять хендлеры pytest)."""
    lg = logging.getLogger("u9_fake_root")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    sio = io.StringIO()
    handler = logging.StreamHandler(sio)
    lg.addHandler(handler)
    yield lg, handler, sio
    lg.removeHandler(handler)


def test_access_log_line_masked_via_root_handler(fake_root):
    """Integration: строка формата aiohttp.access для GET-колбэка, испущенная
    ДОЧЕРНИМ логгером, маскируется фильтром на хендлере корневого — именно
    поэтому фильтр вешается на хендлеры, а не на сам логгер."""
    lg, _handler, sio = fake_root
    install_masking(lg)
    access = logging.getLogger("u9_fake_root.aiohttp.access")
    access.info(
        '%s [%s] "%s %s HTTP/1.1" %s %s',
        "203.0.113.7", "03/Aug/2026:12:00:00 +0300", "GET",
        f"/sms/status-callback?phone={PHONE}&external_id=cab-42&state=deliver",
        "200", "123",
    )
    out = sio.getvalue()
    assert PHONE not in out
    assert PHONE_MASK in out
    assert "/sms/status-callback" in out
    assert "external_id=cab-42" in out


def test_exception_traceback_masked(fake_root):
    """R16: logger.exception() внутри except — исключение несёт телефон и код,
    в вывод хендлера они попасть не должны (обёрнутые ошибки БД/Bitrix/хуков)."""
    lg, _handler, sio = fake_root
    install_masking(lg)
    child = logging.getLogger("u9_fake_root.cabinet")
    try:
        raise RuntimeError(f"upstream failed: phone={PHONE}, code=4821")
    except RuntimeError:
        child.exception("не удалось обработать заявку")
    out = sio.getvalue()
    assert "Traceback" in out            # traceback реально попал в вывод
    assert PHONE not in out
    assert PHONE_MASK in out
    assert "4821" not in out
    assert "••••" in out


def test_exception_traceback_masked_repeated_emit_idempotent(fake_root):
    """exc_text кешируется на записи: повторный проход фильтра не портит
    уже маскированный traceback и не возвращает открытые значения."""
    lg, handler, sio = fake_root
    install_masking(lg)
    try:
        raise RuntimeError(f"upstream failed: phone={PHONE}, code=4821")
    except RuntimeError as exc:
        record = logging.LogRecord(
            "u9_fake_root.cabinet", logging.ERROR, __file__, 1,
            "сбой", None, (type(exc), exc, exc.__traceback__),
        )
    for _ in range(2):
        assert MaskingFilter().filter(record) is True
    handler.handle(record)
    out = sio.getvalue()
    assert PHONE not in out
    assert PHONE_MASK in out
    assert "4821" not in out


def test_stack_info_masked():
    record = logging.LogRecord(
        "cabinet", logging.ERROR, __file__, 1, "сбой", None, None,
    )
    record.stack_info = f'Stack (most recent call last):\n  send(phone="{PHONE}", code=4821)'
    assert MaskingFilter().filter(record) is True
    assert PHONE not in record.stack_info
    assert PHONE_MASK in record.stack_info
    assert "4821" not in record.stack_info


def test_filter_survives_broken_exc_info():
    """Устойчивость: битый exc_info не роняет логирование."""
    record = logging.LogRecord(
        "cabinet", logging.ERROR, __file__, 1, "сбой", None, None,
    )
    record.exc_info = ("не", "кортеж-тройка")  # не (type, value, tb)
    assert MaskingFilter().filter(record) is True


def test_install_masking_idempotent(fake_root):
    lg, handler, _sio = fake_root
    install_masking(lg)
    install_masking(lg)
    masks = [f for f in handler.filters if isinstance(f, MaskingFilter)]
    assert len(masks) == 1


# =======================
# AE9: алерт Global Error Middleware (covers R16)
# =======================

@pytest_asyncio.fixture
async def alert_client(monkeypatch):
    """Боевые middleware из main.py + boom-хендлер /cabinet/api/*;
    алерт-функция перехвачена моком."""
    alerts = []

    async def fake_alert(group_id, text):
        alerts.append(text)

    monkeypatch.setattr(main_mod, "TECH_ADMIN_ID", "tech-group")
    monkeypatch.setattr(main_mod.max_client, "send_alert_to_group", fake_alert)

    async def boom(request):
        body = await request.text()
        raise RuntimeError(f"неожиданный сбой, payload: {body}")

    app = web.Application(middlewares=[main_mod.error_middleware,
                                       main_mod.cors_middleware])
    app.router.add_post("/cabinet/api/_boom", boom)
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client, alerts
    await client.close()


@pytest.mark.asyncio
async def test_ae9_alert_masks_phone_and_drops_code(alert_client):
    client, alerts = alert_client
    resp = await client.post(
        "/cabinet/api/_boom",
        json={"phone": PHONE, "code": "4821"},
    )
    assert resp.status == 500
    assert len(alerts) == 1
    text = alerts[0]
    assert PHONE not in text          # телефон только маской
    assert PHONE_MASK in text
    assert "4821" not in text         # код отсутствует полностью
    assert "КРИТИЧЕСКИЙ СБОЙ" in text


# =======================
# Source-grep: wiring и запреты
# =======================

def test_install_masking_wired_after_basicconfig():
    src = MAIN_PY.read_text(encoding="utf-8")
    i_cfg = src.index("logging.basicConfig(")
    i_install = src.index("log_masking.install_masking()")
    assert i_cfg < i_install, "install_masking должен вызываться ПОСЛЕ basicConfig"


def test_error_middleware_masks_alert_text():
    src = MAIN_PY.read_text(encoding="utf-8")
    mw = src.split("async def error_middleware", 1)[1].split("@web.middleware", 1)[0]
    assert "mask_sensitive" in mw
    assert mw.index("mask_sensitive") < mw.index("send_alert_to_group"), \
        "текст алерта маскируется ДО отправки"


def test_cabinet_code_does_not_use_log_session():
    for name in ("cabinet_service.py", "cabinet_api.py"):
        assert "log_session" not in (ROOT / name).read_text(encoding="utf-8"), \
            f"{name} не должен вызывать log_session (R16)"
