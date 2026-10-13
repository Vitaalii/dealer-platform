"""U9: shadow-вызов резолвера — fire-and-forget без влияния на прод (R21, R23)."""
import asyncio
import pathlib

import pytest

import shadow_resolver
from shadow_resolver import build_shadow_payload, fire_shadow

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"

FULL_PAYLOAD = {
    "action": "chat",
    "user_id": "u1",
    "session_id": "sess_u1_x",
    "message": "хочу кредит",
    "user_data": {"name": "Иван"},
    "qualification_context": {"system_prompt": "ОГРОМНЫЙ ПРОМПТ", "cached_json": {"big": True}},
    "data_routing": {"catalog_table_id": "t"},
    "recent_history": [{"role": "client", "text": "привет"}],
    "session_context": {"request_type": "credit", "current_stage": "data_collect"},
}


class TestShadowPayload:
    def test_takes_only_compact_fields(self):
        sp = build_shadow_payload(FULL_PAYLOAD)
        assert set(sp) == {"session_id", "user_id", "message", "recent_history", "session_context"}

    def test_never_leaks_prompt_or_cache(self):
        sp = str(build_shadow_payload(FULL_PAYLOAD))
        assert "system_prompt" not in sp and "ОГРОМНЫЙ ПРОМПТ" not in sp
        assert "cached_json" not in sp


class TestFireShadow:
    @pytest.mark.asyncio
    async def test_disabled_without_url(self, monkeypatch):
        monkeypatch.delenv(shadow_resolver.SHADOW_URL_ENV, raising=False)
        assert fire_shadow(FULL_PAYLOAD) is False

    @pytest.mark.asyncio
    async def test_network_errors_are_swallowed(self):
        # заведомо недоступный адрес: задача создаётся, исключение не всплывает
        assert fire_shadow(FULL_PAYLOAD, url="http://127.0.0.1:1/nope") is True
        await asyncio.sleep(0.05)  # даём задаче упасть и проглотить ошибку

    @pytest.mark.asyncio
    async def test_fire_does_not_block(self):
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        fire_shadow(FULL_PAYLOAD, url="http://127.0.0.1:1/nope")
        assert loop.time() - t0 < 0.05, "fire_shadow не должен ждать сеть"
        await asyncio.sleep(0.05)


class TestMainWiring:
    def test_shadow_fired_from_chat_sender(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "import shadow_resolver" in src
        fire = src.index("shadow_resolver.fire_shadow(payload)")
        send_fn = src.index("async def send_chat_message_to_n8n")
        next_fn = src.index("async def send_handoff_webhook")
        assert send_fn < fire < next_fn, "fire_shadow должен вызываться из send_chat_message_to_n8n"
