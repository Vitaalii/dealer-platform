"""План 2026-08-21-001: чистый лист истории для Александра с момента передачи.

U1 — маркер границы (mark_ai_handoff: двойной append в снапшот и живой
контекст + немедленное сохранение); U2 — срез build_recent_history от
последнего маркера + фильтр строк-кнопок. Тесты — по фактическому
содержимому среза за несколько ходов (урок token-idempotency-verified-by-
table-rows), сквозные сценарии всех путей передачи: MAX-инцидент 3316,
timeout/intercept, Manager-web.
"""
import copy
import json
import pathlib

import pytest
from sqlalchemy import select

import payload_v2
from payload_v2 import build_recent_history, build_session_context, make_handoff_marker
from database import User
import main as main_mod

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"

MARKER = make_handoff_marker("10:00")


def _funnel_history():
    """История MAX-инцидента 3316: воронка кредита до момента передачи."""
    return [
        {"time": "09:58", "role": "system", "text": "Клиент перешел по ссылке: 010001 BMW X5"},
        {"time": "09:58", "role": "user", "type": "text", "text": "ЗДРАВСТВУЙТЕ! МЕНЯ ИНТЕРЕСУЕТ X5"},
        {"time": "09:59", "role": "bot", "text": "Какой первоначальный взнос вы планируете?"},
        {"time": "09:59", "role": "user", "type": "text", "text": "500000"},
        {"time": "09:59", "role": "user", "type": "button", "text": "ПО ТЕЛЕФОНУ"},
        {"time": "10:00", "role": "bot", "text": "Для получения расчета, подтвердите Ваши контакты"},
    ]


class TestSliceFromMarker:
    def test_init_after_marker_is_clean_slate(self):
        # Сценарий инцидента 3316: на INIT срез пуст, висящий вопрос отрезан
        assert build_recent_history(_funnel_history() + [dict(MARKER)]) == []

    def test_turn2_contains_only_ai_dialogue(self):
        history = _funnel_history() + [
            dict(MARKER),
            {"time": "10:00", "role": "bot", "text": "Ваш расчёт готов: ..."},
            {"time": "10:01", "role": "user", "type": "text", "text": "А если взнос больше?"},
        ]
        assert build_recent_history(history) == [
            {"role": "bot", "text": "Ваш расчёт готов: ..."},
            {"role": "client", "text": "А если взнос больше?"},
        ]

    def test_timeout_intercept_path_clean_slate(self):
        history = _funnel_history() + [
            {"time": "10:00", "role": "bot", "text": "Вы также можете получить расчет онлайн"},
            {"time": "10:01", "role": "user", "type": "button", "text": "__SYSTEM_TIMEOUT_HANDOFF__"},
            {"time": "10:01", "role": "bot", "text": "Переводим диалог на специалиста"},
            dict(MARKER),
        ]
        assert build_recent_history(history) == []

    def test_manager_path_finale_cut(self):
        history = _funnel_history() + [
            {"time": "10:00", "role": "bot", "text": "Принято! Ваш запрос отправлен специалисту ⚡"},
            dict(MARKER),
        ]
        assert build_recent_history(history) == []

    def test_two_markers_slice_from_last(self):
        history = _funnel_history() + [
            dict(MARKER),
            {"time": "10:05", "role": "bot", "text": "Первый диалог с ИИ"},
            dict(make_handoff_marker("11:00")),
            {"time": "11:01", "role": "user", "type": "text", "text": "снова здравствуйте"},
        ]
        assert build_recent_history(history) == [
            {"role": "client", "text": "снова здравствуйте"}]

    def test_marker_text_never_leaks(self):
        out = build_recent_history(_funnel_history() + [dict(MARKER),
                                   {"role": "user", "type": "text", "text": "ок"}])
        assert all(payload_v2.AI_HANDOFF_TEXT not in m["text"] for m in out)


class TestButtonFilter:
    def test_button_after_marker_filtered_isolating(self):
        # Изолирующий тест R2: «НИЧЕГО НЕ НУЖНО» не под __SYSTEM-префиксом —
        # без фильтра type==button эта строка прошла бы в срез
        history = [dict(MARKER),
                   {"time": "10:02", "role": "user", "type": "button", "text": "НИЧЕГО НЕ НУЖНО"},
                   {"time": "10:02", "role": "bot", "text": "Понимаю, всего доброго!"}]
        assert build_recent_history(history) == [
            {"role": "bot", "text": "Понимаю, всего доброго!"}]

    def test_no_marker_fallback_keeps_window_but_filters_buttons(self):
        # Легаси-диалог/усечение: отсечки нет (окно как раньше), но кнопки
        # исключены безусловно (R2 действует на любом ходе)
        out = build_recent_history(_funnel_history())
        texts = [m["text"] for m in out]
        assert "ПО ТЕЛЕФОНУ" not in texts
        assert "Для получения расчета, подтвердите Ваши контакты" in texts
        assert {"role": "client", "text": "500000"} in out


class TestStageRegression:
    def test_msg_count_and_stage_unchanged_by_marker(self):
        base = {"scenario_type": "credit", "payment": "500000",
                "chat_history": _funnel_history()}
        with_marker = copy.deepcopy(base)
        with_marker["chat_history"].append(dict(MARKER))
        sc_base = build_session_context(copy.deepcopy(base), "", "base_v1")
        sc_marked = build_session_context(copy.deepcopy(with_marker), "", "base_v1")
        # msg_count считает user-строки ПОЛНОЙ истории — маркер и срез на него
        # не влияют (R4: иначе INIT дал бы greeting вместо tool_needed)
        assert sc_base["msg_count"] == sc_marked["msg_count"] == 3
        assert sc_base["current_stage"] == sc_marked["current_stage"]


class TestMarkAiHandoff:
    @pytest.mark.asyncio
    async def test_double_append_and_persistence(self, db, monkeypatch):
        monkeypatch.setattr(main_mod, "AsyncSessionLocal", db.AsyncSessionLocal)
        ctx = {"start_code": "010001", "channel": "max",
               "chat_history": _funnel_history()}
        async with db.AsyncSessionLocal() as session:
            user = User(platform_user_id="u-handoff", channel="max",
                        full_name="Иван", is_smartbot_active=False,
                        context_json=json.dumps(ctx, ensure_ascii=False))
            session.add(user)
            await session.commit()
        main_mod.USER_CONTEXT.clear()
        main_mod.USER_CONTEXT["u-handoff"] = copy.deepcopy(ctx)
        snapshot = copy.deepcopy(main_mod.USER_CONTEXT["u-handoff"])
        try:
            await main_mod.mark_ai_handoff("u-handoff", snapshot)

            # Маркер в СНАПШОТЕ, который уйдёт в send_handoff_webhook, —
            # и срез INIT-хода по нему пуст
            assert snapshot["chat_history"][-1]["type"] == payload_v2.AI_HANDOFF_TYPE
            assert build_recent_history(snapshot["chat_history"]) == []

            # Маркер в живом контексте
            live = main_mod.USER_CONTEXT["u-handoff"]["chat_history"]
            assert live[-1]["type"] == payload_v2.AI_HANDOFF_TYPE

            # Персистентность БЕЗ штатного сохранения после ответа n8n —
            # маркер переживает сбой INIT (сохранение внутри mark_ai_handoff)
            async with db.AsyncSessionLocal() as session:
                row = (await session.execute(
                    select(User).where(User.platform_user_id == "u-handoff")
                )).scalar_one()
            saved = json.loads(row.context_json)
            assert saved["chat_history"][-1]["type"] == payload_v2.AI_HANDOFF_TYPE
        finally:
            main_mod.USER_CONTEXT.clear()


class TestWiring:
    def test_marker_between_flag_and_webhook(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        flag = src.index("values(is_smartbot_active=True)")
        marker = src.index("await mark_ai_handoff(")
        webhook = src.index("ai_response = await send_handoff_webhook(")
        assert flag < marker < webhook, \
            "маркер должен писаться после включения флага и до INIT-запроса"
