"""U7 (план 2026-08-27-001): боевая проводка «сервис — реальный шлюз».

Урок docs/solutions/logic-errors/injected-callable-hidden-side-effect.md:
дефекты стыка невидимы на фейках принципиально — нужен хотя бы один прогон
с реальным ProstoSmsGateway. Шлюз направляется на недоступный адрес
(127.0.0.1:9, discard): отправка не выполняется и денег не стоит, но весь
путь request_code → шлюз → transport-ошибка → retry_free проходит боевыми
классами. Проверяется контракт стыка: transport-ошибка возвращается
РЕЗУЛЬТАТОМ (SendResult), а не исключением — иначе запись зависла бы в
'sending' и заблокировала повтор до конца TTL.
"""
import aiohttp
import pytest
from sqlalchemy import select

import cabinet_service
from database import CabinetOtp
from sms_gateway import ProstoSmsGateway
from tests.test_cabinet_auth import _issue, _mk_user, CONSENT, PHONE


@pytest.fixture
def real_gateway_unreachable():
    """Реальный ProstoSmsGateway на недоступный адрес с коротким таймаутом."""
    gw = ProstoSmsGateway(api_key="wiring-test-key",
                          url="http://127.0.0.1:9/",
                          timeout=aiohttp.ClientTimeout(total=0.4))
    cabinet_service.set_gateway(gw)
    yield gw
    # deps-фикстура вернёт свой фейк сама; на выходе — явный сброс роутера
    cabinet_service.set_gateway(None)


@pytest.mark.asyncio
async def test_real_gateway_transport_failure_is_result_not_exception(
        db, deps, real_gateway_unreachable):
    """SMS через реальный шлюз в никуда: retry_free, одна запись failed_send,
    SMS-лимиты не съедены, второй отправки никто не делает."""
    user = await _mk_user(db, "wire")
    _, token = await _issue(user.id)

    data = await cabinet_service.request_code(
        token, phone=PHONE, channel="sms", consent=CONSENT)
    assert data["status"] == "retry_free"

    async with db.AsyncSessionLocal() as session:
        rows = (await session.execute(select(CabinetOtp))).scalars().all()
    # Путь отправки один: ровно одна запись, застрявших 'sending' нет
    assert len(rows) == 1
    assert rows[0].status == "failed_send"
    assert rows[0].sent_at is None

    # Неуспешная SMS бесплатна: немедленный повтор не упирается ни в кулдаун,
    # ни в суточные окна (шлюз снова недоступен — снова retry_free, не 429)
    data2 = await cabinet_service.request_code(token, phone=PHONE, channel="sms")
    assert data2["status"] == "retry_free"


@pytest.mark.asyncio
async def test_real_gateway_voice_failure_still_occupies_voice_window(
        db, deps, real_gateway_unreachable):
    """Голос через реальный шлюз в никуда: запрос не принят, но голосовое окно
    номера занято (O1 — непринятый запрос взводит триггер подмены провайдера)."""
    user = await _mk_user(db, "wire-v")
    _, token = await _issue(user.id, deal="D-2")

    data = await cabinet_service.request_code(
        token, phone=PHONE, channel="voice", consent=CONSENT)
    assert data["status"] == "retry_free"

    with pytest.raises(cabinet_service.ApiError) as e:
        await cabinet_service.request_code(token, phone=PHONE, channel="voice")
    assert e.value.error == "rate_limited"
    assert e.value.extra["limit_source"] == "phone"
