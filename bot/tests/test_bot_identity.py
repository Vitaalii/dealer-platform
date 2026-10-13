"""U1 (план 2026-09-15-001): bot_identity — единая сборка/разборка составного ID,
санитизация Dialogflow, производный секрет вебхука (KTD-3, KTD-5, R10)."""
import re
import string

import bot_identity


# --- build/parse ---

def test_build_parse_roundtrip_new_bot():
    internal = bot_identity.build_internal_id("max-haval", "12345")
    assert internal == "max-haval:12345"
    assert bot_identity.parse_internal_id(internal) == ("max-haval", "12345")


def test_legacy_keys_give_bare_id():
    for key in ("max-main", "tg-main", "web"):
        assert bot_identity.build_internal_id(key, "777") == "777"
    assert bot_identity.build_internal_id(None, "777") == "777"
    assert bot_identity.build_internal_id("", "777") == "777"


def test_parse_bare_id():
    assert bot_identity.parse_internal_id("224272313") == (None, "224272313")


def test_parse_colon_in_raw_survives_maxsplit():
    # ':' в хвосте не ломает разбор (split c maxsplit=1)
    assert bot_identity.parse_internal_id("max-haval:abc:def") == ("max-haval", "abc:def")


def test_parse_invalid_head_treated_as_bare():
    # голова не проходит формат bot_key — строка целиком считается сырым ID
    assert bot_identity.parse_internal_id("Хост:123") == (None, "Хост:123")
    assert bot_identity.parse_internal_id("Max_Tank:1") == (None, "Max_Tank:1")
    assert bot_identity.parse_internal_id(":123") == (None, ":123")


def test_raw_id_for_send():
    assert bot_identity.raw_id_for_send("max-haval:12345") == "12345"
    assert bot_identity.raw_id_for_send("12345") == "12345"


def test_is_valid_bot_key():
    assert bot_identity.is_valid_bot_key("max-haval")
    assert not bot_identity.is_valid_bot_key("Max_Tank")
    assert not bot_identity.is_valid_bot_key("max tank")
    assert not bot_identity.is_valid_bot_key("бот")
    assert not bot_identity.is_valid_bot_key("")


# --- df_session_id (KTD-5: ':' в ресурсных именах GCP не установлен) ---

def test_df_session_id_no_colon_and_deterministic():
    a = bot_identity.df_session_id("max-haval:12345")
    b = bot_identity.df_session_id("max-haval:12345")
    assert ":" not in a
    assert a == b


def test_df_session_id_length_within_dialogflow_budget():
    # лимит DF ES 36 минус префикс "user_" = 31
    long_id = bot_identity.build_internal_id("very-long-brand-bot-key-name", "9" * 30)
    out = bot_identity.df_session_id(long_id)
    assert len(out) <= 31
    assert ":" not in out
    # разные длинные ID не сворачиваются в одно значение
    other = bot_identity.df_session_id(
        bot_identity.build_internal_id("very-long-brand-bot-key-name", "8" * 30))
    assert out != other


def test_df_session_id_differs_per_bot():
    assert (bot_identity.df_session_id("max-haval:1") !=
            bot_identity.df_session_id("tg-haval:1"))


# --- derive_webhook_secret (KTD-3) ---

def test_secret_is_hex64_deterministic():
    s1 = bot_identity.derive_webhook_secret("token-abc", "max-haval")
    s2 = bot_identity.derive_webhook_secret("token-abc", "max-haval")
    assert s1 == s2
    assert re.fullmatch(r"[0-9a-f]{64}", s1)


def test_secret_charset_fits_both_platforms():
    # Telegram: A-Za-z0-9_- (1-256); MAX: A-Za-z0-9- (5-256). Hex ⊂ обоих.
    s = bot_identity.derive_webhook_secret("t" * 80, "tg-haval")
    allowed = set(string.ascii_lowercase[:6] + string.digits)
    assert set(s) <= allowed
    assert 5 <= len(s) <= 256


def test_secret_differs_per_bot_and_per_token():
    base = bot_identity.derive_webhook_secret("token-abc", "max-haval")
    assert base != bot_identity.derive_webhook_secret("token-abc", "tg-haval")
    assert base != bot_identity.derive_webhook_secret("token-xyz", "max-haval")


def test_secret_does_not_contain_token():
    token = "f9SAMPLEtokenVALUEforTEST0123456789"
    s = bot_identity.derive_webhook_secret(token, "max-haval")
    assert token not in s and token.lower() not in s
