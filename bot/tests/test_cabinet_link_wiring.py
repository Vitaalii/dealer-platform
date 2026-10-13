"""Ревью 2026-08-19 плана 2026-08-19-001: проводка ссылки кабинета живьём.

Интеграционные проверки поверх общих фикстур db/deps (conftest.py) — по
находкам ревью #1/#2/#6: read-only геттер get_active_cabinet_url, реальный
проход send_smartbot_reply с маркером через фейковый клиент, нормализация
истории. Дополняют чистые тесты test_cabinet_link_render.py.
"""
import json

import pytest
from sqlalchemy import update

import cabinet_service
from database import CabinetToken, User
import main as main_mod


class _FakeClient:
    """Фейковый канальный клиент: пишет отправки."""

    def __init__(self):
        self.sent = []

    async def send_message(self, user_id=None, text=None, keyboard=None,
                           attachments=None, **kw):
        self.sent.append({"user_id": user_id, "text": text, "keyboard": keyboard})
        return {"ok": True}

    async def send_typing(self, chat_id=None, **kw):
        return True

    def supports_contact_button(self):
        return True


async def _mk_user(db, suffix, channel="max"):
    ctx = {"start_code": "010001", "channel": channel,
           "chat_history": [{"role": "user", "text": "привет", "time": "10:00"}]}
    async with db.AsyncSessionLocal() as session:
        user = User(platform_user_id=f"u-{suffix}", channel=channel,
                    full_name="Иван", is_smartbot_active=True,
                    context_json=json.dumps(ctx, ensure_ascii=False))
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user, ctx


# =======================
# get_active_cabinet_url (#1)
# =======================

class TestGetActiveCabinetUrl:
    @pytest.mark.asyncio
    async def test_returns_same_url_as_issue(self, db, deps):
        user, _ = await _mk_user(db, "g1")
        url = await cabinet_service.issue_cabinet_token(user.id, "01", None)
        assert url and "#t=" in url
        assert await cabinet_service.get_active_cabinet_url(user.id) == url

    @pytest.mark.asyncio
    async def test_none_without_active_token(self, db, deps):
        user, _ = await _mk_user(db, "g2")
        assert await cabinet_service.get_active_cabinet_url(user.id) is None

    @pytest.mark.asyncio
    async def test_none_after_hmac_key_rotation(self, db, deps, monkeypatch):
        # Инцидент 2026-08-12: расхождение ключей — токен жив, URL не выводится
        user, _ = await _mk_user(db, "g3")
        assert await cabinet_service.issue_cabinet_token(user.id, "01", None)
        monkeypatch.setenv("CABINET_HMAC_KEY", "rotated-key")
        assert await cabinet_service.get_active_cabinet_url(user.id) is None

    @pytest.mark.asyncio
    async def test_none_when_dealer_site_not_https(self, db, deps, dealers):
        user, _ = await _mk_user(db, "g4")
        assert await cabinet_service.issue_cabinet_token(user.id, "01", None)
        original = dealers["01"]["website"]
        dealers["01"]["website"] = "http://dealer-one.ru/"
        try:
            assert await cabinet_service.get_active_cabinet_url(user.id) is None
        finally:
            dealers["01"]["website"] = original

    @pytest.mark.asyncio
    async def test_none_for_legacy_row_without_nonce(self, db, deps):
        user, _ = await _mk_user(db, "g5")
        assert await cabinet_service.issue_cabinet_token(user.id, "01", None)
        async with db.AsyncSessionLocal() as session:
            await session.execute(update(CabinetToken)
                                  .where(CabinetToken.user_id == user.id)
                                  .values(issue_nonce=None))
            await session.commit()
        assert await cabinet_service.get_active_cabinet_url(user.id) is None


# =======================
# send_smartbot_reply с маркером (#2)
# =======================

@pytest.fixture
def link_env(db, monkeypatch):
    """Перепривязка main.py к тестовой БД + фейковый клиент (образец — bot_env
    в test_cabinet_session_close.py)."""
    client = _FakeClient()
    monkeypatch.setattr(main_mod, "AsyncSessionLocal", db.AsyncSessionLocal)
    monkeypatch.setattr(main_mod, "get_client", lambda ch=None: client)
    main_mod.USER_CONTEXT.clear()
    yield client
    main_mod.USER_CONTEXT.clear()


class TestSendSmartbotReplyMarker:
    @pytest.mark.asyncio
    async def test_marker_substituted_into_anchor(self, db, deps, link_env):
        user, ctx = await _mk_user(db, "s1")
        url = await cabinet_service.issue_cabinet_token(user.id, "01", None)
        main_mod.USER_CONTEXT[user.platform_user_id] = dict(ctx)
        await main_mod.send_smartbot_reply(
            user.platform_user_id, {"text": "Откройте [CABINET_LINK], пожалуйста."})
        assert link_env.sent, "сообщение должно быть отправлено"
        text = link_env.sent[-1]["text"]
        assert f'<a href="{url}">личный кабинет</a>' in text
        assert "[CABINET_LINK" not in text

    @pytest.mark.asyncio
    async def test_degrades_to_phrase_on_service_error(self, db, deps, link_env,
                                                       monkeypatch):
        user, ctx = await _mk_user(db, "s2")
        await cabinet_service.issue_cabinet_token(user.id, "01", None)

        async def boom(user_id):
            raise RuntimeError("cabinet down")

        monkeypatch.setattr(main_mod.cabinet_service,
                            "get_active_cabinet_url", boom)
        main_mod.USER_CONTEXT[user.platform_user_id] = dict(ctx)
        await main_mod.send_smartbot_reply(
            user.platform_user_id, {"text": "Откройте [CABINET_LINK], пожалуйста."})
        text = link_env.sent[-1]["text"]
        assert "<a href=" not in text and "[CABINET_LINK" not in text
        assert "личный кабинет" in text  # связный текст, ссылка не «пустышка»


# =======================
# normalize_bot_text_for_history (#6)
# =======================

class TestNormalizeBotTextForHistory:
    @pytest.mark.asyncio
    async def test_marker_becomes_bare_url(self, db, deps, monkeypatch):
        user, _ = await _mk_user(db, "h1")
        url = await cabinet_service.issue_cabinet_token(user.id, "01", None)
        monkeypatch.setattr(main_mod, "AsyncSessionLocal", db.AsyncSessionLocal)
        out = await main_mod.normalize_bot_text_for_history(
            user.platform_user_id, "Вот [CABINET_LINK], жду.")
        assert url in out and "<a" not in out and "[CABINET_LINK" not in out

    @pytest.mark.asyncio
    async def test_no_marker_no_db_roundtrip(self, monkeypatch):
        calls = []

        async def spy(user_id):
            calls.append(user_id)
            return None

        monkeypatch.setattr(main_mod.cabinet_service,
                            "get_active_cabinet_url", spy)
        text = "Обычный ответ без маркера."
        assert await main_mod.normalize_bot_text_for_history("u-x", text) == text
        assert calls == []

    @pytest.mark.asyncio
    async def test_unknown_user_degrades_without_exception(self, db, deps,
                                                           monkeypatch):
        monkeypatch.setattr(main_mod, "AsyncSessionLocal", db.AsyncSessionLocal)
        out = await main_mod.normalize_bot_text_for_history(
            "no-such-user", "Вот [CABINET_LINK].")
        assert out == "Вот личный кабинет."


# =======================
# B2 (план 2026-09-01-002, U4): гейт выдачи — единый предикат телефона
# =======================

class TestHasPhoneGateUsesUnifiedPredicate:
    """Гейт выдачи ссылки кабинета использует is_valid_phone, а не bool():
    клиент с мусорным номером не должен выпадать из обеих воронок сразу."""

    @pytest.mark.asyncio
    async def test_invalid_phone_still_gets_cabinet_link(self, db, deps):
        user, ctx = await _mk_user(db, "gate-garbage")
        async with db.AsyncSessionLocal() as session:
            await session.execute(update(User).where(User.id == user.id)
                                  .values(phone="созвонимся завтра"))
            await session.commit()
            user = await session.get(User, user.id)
        payload = {"qualification_context": {}}
        await main_mod._attach_cabinet_context(
            payload, user, "01", deal_id="D-1", scenario="credit",
            user_context=ctx, label="test")
        assert payload["qualification_context"].get("cabinet_url"), \
            "мусорный номер = «телефона нет» для воронки → ссылка обязана выдаваться"

    @pytest.mark.asyncio
    async def test_valid_phone_issues_no_new_link(self, db, deps):
        user, ctx = await _mk_user(db, "gate-valid")
        async with db.AsyncSessionLocal() as session:
            await session.execute(update(User).where(User.id == user.id)
                                  .values(phone="+79001234567"))
            await session.commit()
            user = await session.get(User, user.id)
        payload = {"qualification_context": {}}
        await main_mod._attach_cabinet_context(
            payload, user, "01", deal_id="D-1", scenario="credit",
            user_context=ctx, label="test")
        assert payload["qualification_context"].get("cabinet_url") is None, \
            "валидный номер без активного токена — нового токена нет (как раньше)"
