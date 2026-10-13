"""Извлекатель телефона (планы 2026-09-07-001 и -002): extract_phone.

История критерия: PHONE_RE (список разделителей) терял «8/980/555-28-17»
(инцидент 51365) → паритет с is_valid_phone по цифро-группам (план -001) →
ловил склейки денежных пар «800 000, 30 000» (находка n8n-стороны) →
действующий критерий Р9/KTD-8 (план -002): извлекаются формы ЗАПИСИ телефона
(белый список разрядностей PHONE_GROUP_PATTERNS), ошибаться — в сторону
строгости. Наборы — из живых прогонов всех механизмов, включая архив
реальных реплик (632 диалога). Урок: docs/solutions/logic-errors/
digit-window-parity-money-pairs.md.
"""
import pathlib

import pytest

from payload_v2 import extract_phone, detect_flags

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"
PAYLOAD_V2_PY = pathlib.Path(__file__).resolve().parent.parent / "payload_v2.py"

# (вход, ожидаемое извлечение)
POSITIVE = [
    # формы разделителей — включая инцидент 51365
    ("8/980/555-28-17", "8/980/555-28-17"),
    ("8 980 555-28-17", "8 980 555-28-17"),
    ("8(980)555-28-17", "8(980)555-28-17"),
    ("+7 980 555 28 17", "+7 980 555 28 17"),
    ("89805552817", "89805552817"),
    ("8.980.555.28.17", "8.980.555.28.17"),
    ("8-980-555-28-17", "8-980-555-28-17"),
    # номер в контексте
    ("мой номер 8/980/555-28-17 спасибо", "8/980/555-28-17"),
    ("запишите89805552817позвоните", "89805552817"),
    ("тел:8-980-555-28-17.", "8-980-555-28-17"),
    # Д3: число и номер подряд, без букв между (окна по цифро-группам)
    ("148000 89805552817", "89805552817"),
    ("89805552817 148000", "89805552817"),
    ("26000 8 980 555 28 17", "8 980 555 28 17"),
    ("М736ОТ37 148000 89805552817", "89805552817"),
    ("8 980 555 28 17 148000", "8 980 555 28 17"),
    ("8-980-555-28-17 2219990", "8-980-555-28-17"),
]

NEGATIVE = [
    "М736ОТ37 148 000",            # госномер + пробег (реплика инцидента)
    "148000", "25000", "26000",
    "2 219 990", "1 820 000",
    "XTA219990Y2755454",           # VIN
    "2014",                        # год выпуска
    "от 850000 до 2010000",
    "850000-2010000",              # диапазон через дефис — старый ловил обрезок
    "пробег 148 000, бюджет 2 219 990",
    "148 000 2 219 990",           # две цены с разрядными пробелами подряд
    "08.09.2026 в 14:30",          # дата и время
    "заказ №8-980-734",            # неполный номер
    "ВИН X9L21230080123456",
    "карта 2202 2061 2345 6789",
    "СНИЛС 123-456-789 01",
    "", None,
]


@pytest.mark.parametrize("text,expected", POSITIVE)
def test_extracts_valid_numbers_as_sent(text, expected):
    assert extract_phone(text) == expected


@pytest.mark.parametrize("text", NEGATIVE)
def test_ignores_non_phone_digits(text):
    assert extract_phone(text) is None


def test_old_false_positive_price_range_stays_clean():
    """Регресс снятого ложного срабатывания: старый PHONE_RE извлекал из
    диапазона цен обрезок «850000-2010» — новый обязан молчать."""
    assert extract_phone("хочу от 850000-2010000") is None


def test_d3_full_number_instead_of_truncated():
    """Регресс дефекта старого механизма на классе Д3: PHONE_RE на
    «148000 89805552817» извлекал ОБРЕЗОК « 8980734596» (10 цифр без
    последней) — новый обязан извлечь полный номер."""
    got = extract_phone("148000 89805552817")
    assert got == "89805552817", got


def test_glued_digits_boundary_documented():
    """KTD-7, граница паритета: номер, слитый с другой цифрой БЕЗ разделителя,
    не извлекается (одна неделимая группа длиннее 11 цифр). Фиксируем тестом,
    чтобы изменение границы было осознанным."""
    assert extract_phone("14800089807345967") is None


class TestFlagParity:
    """Флаг has_valid_phone_in_msg питается тем же извлекателем (п.1.3)."""

    def test_flag_true_on_incident_form(self):
        assert detect_flags("8/980/555-28-17")["has_valid_phone_in_msg"] is True

    def test_flag_false_on_price(self):
        assert detect_flags("2 219 990")["has_valid_phone_in_msg"] is False

    def test_flag_true_on_d3_class(self):
        assert detect_flags("148000 89805552817")["has_valid_phone_in_msg"] is True


class TestSingleSource:
    """Единственный источник извлечения и флага — extract_phone (п.1.3)."""

    def test_phone_re_removed_from_repo(self):
        assert "PHONE_RE" not in PAYLOAD_V2_PY.read_text(encoding="utf-8").replace(
            "_PHONE_CANDIDATE_RE", "").replace("_DIGIT_GROUP_RE", ""), \
            "PHONE_RE не должен существовать — второго правила быть не должно"
        assert "PHONE_RE" not in MAIN_PY.read_text(encoding="utf-8")

    def test_path_a_wired_to_extract_phone(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        assert src.count("payload_v2.extract_phone(combined_text, session_id=") == 1, \
            "рефлекс ИИ-диалога обязан извлекать через payload_v2.extract_phone " \
            "с session_id для лога промахов (Д4)"

    def test_path_a_latch_only_on_successful_save(self):
        """Р10: слот phone_collected ставится только при True от save_user_phone —
        отказ стража не защёлкивает «финализируй» до конца диалога."""
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "if await save_user_phone(max_user_id, _phone_found" in src, \
            "возврат save_user_phone в рефлексе обязан проверяться (Р10)"


class TestMoneyPairsClass:
    """План 2026-09-07-002 (Р9/KTD-8/9): класс «склейка двух чисел в валидный
    номер» — находка n8n-стороны. Денежные пары предметной области (взнос+платёж,
    пробег+цена) суммируются в 11 цифр с ведущей 7/8 и проходили digit-strip.
    Замер на живом архиве: 2 таких ложных на 632 диалога (госномер+пробег).
    Закрыто белым списком разрядностей + запятая/« - » не разделители."""

    SECOND_SIDE = [
        "Бюджет 800 000, 30 000 в месяц",
        "700 000, 40 000",
        "рассрочка 700000, 40000 в мес",
        "85 000, 750 000",
        "S50, 800 000, 30 000",
        "750 000 - 20 000",
    ]
    SAME_LOGIC = [
        "800000 30000",                 # без запятой — ловит слой разрядностей
        "800 000 30 000",               # разрядные пробелы, паттерн 3-3-2-3
        "700 000-40 000",               # дефис без пробелов
        "взнос 800000 платеж 30000",    # буквы между — чисто и раньше
        "85000 750000",
        "750000 20 000",
        "Е259РХ37 120000",              # живое ложное из архива (fallback-класс)
        "Н864СМ37, 220000",             # живое ложное из архива
    ]

    @pytest.mark.parametrize("text", SECOND_SIDE + SAME_LOGIC)
    def test_money_pairs_not_extracted(self, text):
        assert extract_phone(text) is None, text

    @pytest.mark.parametrize("text", SECOND_SIDE + SAME_LOGIC)
    def test_flag_stays_false_on_money_pairs(self, text):
        assert detect_flags(text)["has_valid_phone_in_msg"] is False, text


class TestPhonePatternsWhitelist:
    """KTD-8/11: формы записи телефона из белого списка ловятся; «+» сохраняется."""

    def test_plus_preserved(self):
        assert extract_phone("+7 980 555 28 17") == "+7 980 555 28 17"
        assert extract_phone("мой +79805552817") == "+79805552817"

    def test_ten_digit_mobile_without_code(self):
        assert extract_phone("9805552817") == "9805552817"

    def test_code_separated_from_monolith(self):
        assert extract_phone("8 9805552817") == "8 9805552817"

    def test_grouped_ten_digits(self):
        """Живая форма из архива: 902 555-49-28 (паттерн 3-3-2-2 без кода)."""
        assert extract_phone("902 555-49-28") == "902 555-49-28"

    def test_whitelist_is_named_frozen_constant(self):
        """Д7: список — именованная константа модуля, пополнение только с тестом."""
        import payload_v2
        assert isinstance(payload_v2.PHONE_GROUP_PATTERNS, frozenset)
        assert (1, 3, 3, 2, 2) in payload_v2.PHONE_GROUP_PATTERNS
        src = PAYLOAD_V2_PY.read_text(encoding="utf-8")
        assert "ПОПОЛНЕНИЕ — ТОЛЬКО" in src and "2026-09-07-002" in src


class TestPatternMissLogging:
    """Д4: промах белого списка (цифры телефонные, разрядность — нет) виден
    в логе меткой PHONE_PATTERN_MISS; без session_id (флаговый путь) — тишина."""

    def test_miss_logged_with_session_and_candidate(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="payload_v2"):
            got = extract_phone("800 000 30 000", session_id="sess_test_01")
        assert got is None
        recs = [r for r in caplog.records if "PHONE_PATTERN_MISS" in r.getMessage()]
        assert recs, "промах обязан логироваться меткой PHONE_PATTERN_MISS"
        msg = recs[0].getMessage()
        assert "sess_test_01" in msg and "800 000 30 000" in msg

    def test_no_log_without_session_id(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="payload_v2"):
            extract_phone("800 000 30 000")
        assert not any("PHONE_PATTERN_MISS" in r.getMessage() for r in caplog.records)

    def test_no_log_on_phone_form(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="payload_v2"):
            got = extract_phone("8 980 555 28 17", session_id="sess_test_02")
        assert got == "8 980 555 28 17"
        assert not any("PHONE_PATTERN_MISS" in r.getMessage() for r in caplog.records)
