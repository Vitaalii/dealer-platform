"""U6: TTL и событийный рефреш cached_json (R17)."""
import pathlib

from payload_v2 import (
    CACHE_TTL_SECONDS,
    extract_selected_complectation,
    should_refresh_cache,
)

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"
NOW = 1_000_000.0


def ctx(cache={"a": 1}, saved_ago=None):
    c = {"n8n_cache": cache}
    if saved_ago is not None:
        c["n8n_cache_saved_at"] = NOW - saved_ago
    return c


class TestShouldRefresh:
    def test_no_cache_nothing_to_refresh(self):
        assert not should_refresh_cache({"n8n_cache": None}, "closing", "data_collect", now=NOW)
        assert not should_refresh_cache({}, "closing", "data_collect", now=NOW)

    def test_fresh_cache_same_stage_kept(self):
        assert not should_refresh_cache(ctx(saved_ago=60), "data_collect", "data_collect", now=NOW)

    def test_ttl_expired(self):
        assert should_refresh_cache(ctx(saved_ago=CACHE_TTL_SECONDS + 1), "data_collect", "data_collect", now=NOW)

    def test_legacy_cache_without_timestamp_treated_stale(self):
        assert should_refresh_cache(ctx(saved_ago=None), "data_collect", "data_collect", now=NOW)

    def test_transition_to_tool_needed_refreshes(self):
        assert should_refresh_cache(ctx(saved_ago=60), "tool_needed", "data_collect", now=NOW)

    def test_transition_to_closing_refreshes(self):
        assert should_refresh_cache(ctx(saved_ago=60), "closing", "tool_needed", now=NOW)

    def test_staying_in_closing_does_not_refresh_every_turn(self):
        assert not should_refresh_cache(ctx(saved_ago=60), "closing", "closing", now=NOW)

    def test_transition_to_data_collect_does_not_refresh(self):
        assert not should_refresh_cache(ctx(saved_ago=60), "data_collect", "greeting", now=NOW)


class TestExtractComplectation:
    def test_from_cache_root(self):
        assert extract_selected_complectation({"complectation_id": 12}) == "12"

    def test_from_user_request(self):
        assert extract_selected_complectation(
            {"user_request": {"selected_complectation_id": "11"}}) == "11"

    def test_any_complectation_00_is_none(self):
        assert extract_selected_complectation({"complectation_id": "00"}) is None

    def test_missing_or_non_dict(self):
        assert extract_selected_complectation(None) is None
        assert extract_selected_complectation("строка") is None
        assert extract_selected_complectation({}) is None


class TestMainWiring:
    def test_cache_save_records_timestamp(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        save_pos = src.index("USER_CONTEXT[max_user_id]['n8n_cache'] = ai_response")
        assert "n8n_cache_saved_at" in src[save_pos:save_pos + 300], \
            "рядом с сохранением кэша нет записи возраста"

    def test_prev_stage_captured_before_extend(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        prev = src.index("prev_stage = user_context.get('current_stage')")
        extend = src.index("payload_v2.extend_payload(payload, user_context, text, prompt_key, dealer_info)")
        refresh = src.index("payload_v2.should_refresh_cache(user_context, new_stage, prev_stage)")
        assert prev < extend < refresh, "порядок: prev_stage -> extend_payload -> should_refresh_cache"
