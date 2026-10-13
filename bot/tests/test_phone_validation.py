"""Перенос прод-3.1.2 (план 2026-09-01-002, U1): единый предикат телефона
бота и порядок отказов save_user_phone. Таблица примеров — из живого прогона
обоих валидаторов при разведке (риск А)."""
import pathlib

import pytest
from sqlalchemy import select

import database
from database import User
import cabinet_service

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"


VALID = [
    "+79001234567", "89001234567", "9001234567",
    "8 (900) 123-45-67", "79991234567",
    "84951234567", "74951234567",   # городские 11-значные — бот принимает
]
INVALID = [
    "4951234567",                   # 10 цифр не с девятки
    "+375291234567", "+380501234567",  # международные
    "phone79001234567",             # >=5 букв вокруг валидных цифр
    "тел 8-900-123-45-67 срочно позвонить",  # длиннее 20 символов
    "созвонимся завтра", "", None,
]


@pytest.mark.parametrize("value", VALID)
def test_is_valid_phone_accepts(value):
    assert database.is_valid_phone(value) is True


@pytest.mark.parametrize("value", INVALID)
def test_is_valid_phone_rejects(value):
    assert database.is_valid_phone(value) is False


def test_normalize_phone_canon_but_storage_unchanged():
    """normalize_phone — только для проверки; канон не пишется в БД."""
    assert database.normalize_phone("8 (900) 123-45-67") == "+79001234567"
    assert database.normalize_phone("9001234567") == "+79001234567"
    assert database.normalize_phone("84951234567") == "+74951234567"


def test_cabinet_canonical_passes_bot_predicate():
    """Кабинет пишет канонический +79XXXXXXXXX (cabinet_service.normalize_phone)
    — бот обязан видеть такой номер как валидный (риск А закрыт)."""
    canon = cabinet_service.normalize_phone("8-999-123-45-67")
    assert canon == "+79991234567"
    assert database.is_valid_phone(canon) is True


def test_bot_predicate_wider_than_cabinet_on_city_numbers():
    """Зафиксированное расхождение: городские 11-значные бот принимает,
    кабинетный шлюзовой валидатор — нет (мобильные only, ProstoSMS)."""
    assert database.is_valid_phone("84951234567") is True
    assert cabinet_service.normalize_phone("84951234567") is None


async def _get_user(db, uid):
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(
            select(User).where(User.platform_user_id == uid)
        )).scalar_one_or_none()


@pytest.mark.asyncio
async def test_save_rejects_invalid_first_guard_not_called(db, monkeypatch, caplog):
    """Причина 1 (валидность) — ПЕРВАЯ: страж не вызывается, пользователь
    не создаётся, в логе различимая пометка."""
    guard_calls = []

    async def guard(uid, phone):
        guard_calls.append(phone)
        return True

    monkeypatch.setattr(database, "PHONE_WRITE_GUARD", guard)
    with caplog.at_level("WARNING"):
        ok = await database.save_user_phone("u-invalid", "4951234567")
    assert ok is False
    assert guard_calls == [], "при невалидном номере страж не должен вызываться"
    assert await _get_user(db, "u-invalid") is None, "запись/создание не выполнены"
    assert any("невалидный номер" in r.message for r in caplog.records), \
        "причина отказа «невалидность» должна быть различима в логе"


@pytest.mark.asyncio
async def test_save_guard_is_second_distinct_reason(db, monkeypatch, caplog):
    """Причина 2 (страж кабинета): валидный номер, страж отклонил — False
    БЕЗ пометки «невалидный номер» (различимость причин)."""
    async def deny(uid, phone):
        return False

    monkeypatch.setattr(database, "PHONE_WRITE_GUARD", deny)
    with caplog.at_level("WARNING"):
        ok = await database.save_user_phone("u-guarded", "+79001234567")
    assert ok is False
    user = await _get_user(db, "u-guarded")
    assert user is not None and user.phone is None, \
        "пользователь создан, но номер стражем не записан"
    assert not any("невалидный номер" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_save_valid_written_as_sent(db, monkeypatch):
    """Успех: значение пишется КАК ПРИСЛАНО, без канонизации."""
    monkeypatch.setattr(database, "PHONE_WRITE_GUARD", None)
    ok = await database.save_user_phone("u-ok", "8 (900) 123-45-67")
    assert ok is True
    user = await _get_user(db, "u-ok")
    assert user.phone == "8 (900) 123-45-67"


class TestPathsBCFrozen:
    """План 2026-09-07-001 (KTD-6): пути B (таймер-перехват) и C (стадия
    воронки) в правке паритета НЕ трогаются. Характеризационные пины пишутся
    ДО правки пути A и обязаны остаться зелёными после неё."""

    def test_path_b_inline_predicate_unchanged(self):
        """Путь B: инлайн-предикат таймер-перехвата остаётся digit-strip
        (10-11 цифр, старт 7/8/9, букв <5) — ровно один экземпляр."""
        src = MAIN_PY.read_text(encoding="utf-8")
        assert src.count(r"digits_only = re.sub(r'\D', '', user_text or " + '""' + ")") == 1
        assert "is_length_ok = 10 <= len(digits_only) <= 11" in src
        assert 'if len(digits_only) == 11 and first_digit in [\'7\', \'8\']' in src

    def test_path_c_uses_is_valid_phone_on_whole_text(self):
        """Путь C: стадия запроса телефона решает единым предикатом по всему
        тексту — ровно один вызов looks_like_phone = is_valid_phone(user_text)."""
        src = MAIN_PY.read_text(encoding="utf-8")
        assert src.count("looks_like_phone = is_valid_phone(user_text)") == 1
