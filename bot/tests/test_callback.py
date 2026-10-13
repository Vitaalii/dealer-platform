"""U3: единый stage_map Битрикса и идемпотентность n8n-callback (R20, R28)."""
import pathlib

from crm_mapping import (
    DEFAULT_STAGE,
    resolve_stage,
    is_duplicate_callback,
    register_callback,
)

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"


class TestStageMap:
    def test_known_statuses(self):
        assert resolve_stage("visit") == "C9:UC_0SOJJA"
        assert resolve_stage("call") == "C9:UC_0SOJJA"
        assert resolve_stage("working") == "C9:UC_HW8Q2H"
        assert resolve_stage("refuse") == "C9:LOSE"
        assert resolve_stage("ban") == "C9:APOLOGY"
        assert resolve_stage("in_progress") == "C9:UC_MDA6RZ"

    def test_unknown_status_falls_back_to_default(self):
        assert resolve_stage("garbage") == DEFAULT_STAGE
        assert resolve_stage(None) == DEFAULT_STAGE
        assert DEFAULT_STAGE == "C9:UC_HW8Q2H"

    def test_main_py_has_no_inline_stage_map(self):
        """Обе точки main.py обязаны читать crm_mapping, а не держать копии.

        Проверяем на уровне исходника: инлайн-словарей status->stage больше нет
        (маркер обеих старых копий — ключ "in_progress" рядом с C9-стадией),
        а стадия в обеих точках берётся из crm_mapping.callback_stage
        (обёртка resolve_stage с телефонными барьерами, план 2026-09-02-002).
        Точечные литералы стадий квалификации (C9:NEW и т.п.) — другая
        подсистема, U3 их не трогает.
        """
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "stage_map = {" not in src, "инлайн-копия stage_map в main.py"
        assert '"in_progress": "C9:' not in src, "инлайн-маппинг статуса на стадию в main.py"
        assert src.count("callback_stage(") >= 2, "мост и callback должны звать crm_mapping.callback_stage"


class TestCallbackIdempotency:
    def test_first_callback_is_not_duplicate(self):
        ctx = {}
        assert not is_duplicate_callback(ctx, 555, "call")

    def test_repeated_callback_is_duplicate(self):
        ctx = {}
        register_callback(ctx, 555, "call")
        assert is_duplicate_callback(ctx, 555, "call")

    def test_same_deal_other_status_is_processed(self):
        ctx = {}
        register_callback(ctx, 555, "call")
        assert not is_duplicate_callback(ctx, 555, "refuse")

    def test_other_deal_same_status_is_processed(self):
        ctx = {}
        register_callback(ctx, 555, "call")
        assert not is_duplicate_callback(ctx, 777, "call")

    def test_missing_deal_id_never_dedupes(self):
        ctx = {}
        register_callback(ctx, None, "call")
        assert not is_duplicate_callback(ctx, None, "call")

    def test_registry_is_json_serializable(self):
        """USER_CONTEXT сохраняется в БД как JSON — реестр не должен это ломать."""
        import json

        ctx = {}
        register_callback(ctx, 555, "call")
        register_callback(ctx, 555, "call")  # повторная регистрация не дублирует запись
        restored = json.loads(json.dumps(ctx))
        assert is_duplicate_callback(restored, 555, "call")
        assert restored["processed_callbacks"] == ["555:call"]
