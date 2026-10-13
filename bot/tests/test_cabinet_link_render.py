"""U1 плана 2026-08-19-001: маркер [CABINET_LINK] → гиперссылка кабинета.

Чистые функции cabinet_link_render (R1-R6) + стражи wiring в main.py
(подстановка до [CONTACT_BUTTON]/[SPLIT], нормализация всех четырёх точек
записи chat_history) — по образцу source-проверок test_payload.py.
"""
import pathlib
import re

import cabinet_link_render as clr

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"
WIDGET_JS = pathlib.Path(__file__).resolve().parent.parent / "widget-custom" / "widget.js"

URL = "https://dealer.example/cabinet#t=AbC123_-xYz"


def render(text, url=URL, channel="max"):
    return clr.render_cabinet_link(text, url, channel)


class TestMarkerSubstitution:
    def test_marker_mid_phrase(self):
        out, info = render("Откройте [CABINET_LINK] и подтвердите номер.")
        assert out == (f'Откройте <a href="{URL}">личный кабинет</a> '
                       "и подтвердите номер.")
        assert info["applied"] == 1 and info["marker_found"]
        assert not info["missing_url"] and not info["broken"]

    def test_extended_form_custom_anchor(self):
        out, _ = render("Вот [CABINET_LINK:ваша персональная страница].")
        assert f'<a href="{URL}">ваша персональная страница</a>.' in out

    def test_extended_form_anchor_escaped(self):
        out, _ = render('Вот [CABINET_LINK:<b>кабинет</b> & "спец"].')
        assert "&lt;b&gt;кабинет&lt;/b&gt; &amp; &quot;спец&quot;" in out
        assert "<b>кабинет</b>" not in out  # разметка из анкора не исполняется

    def test_missing_url_degrades_to_phrase(self):
        out, info = render("Откройте [CABINET_LINK], там анкета.", url=None)
        assert out == "Откройте личный кабинет, там анкета."
        assert info["missing_url"] and info["applied"] == 0
        assert "<a" not in out  # ссылки-пустышки нет

    def test_missing_url_extended_keeps_model_anchor(self):
        out, _ = render("Вот [CABINET_LINK:ваша страница].", url=None)
        assert out == "Вот ваша страница."

    def test_at_most_one_pass_no_double_wrap(self):
        out, info = render(f"Ссылка [CABINET_LINK], а ещё сырая {URL} тут.")
        # маркер подставлен, сырое вхождение обёрнуто, URL внутри готового
        # href повторно не оборачивается (один проход по исходному тексту)
        assert out.count("<a href=") == 2
        assert info["applied"] == 2
        assert "<a href=\"<a" not in out


class TestBrokenMarkers:
    def test_empty_anchor(self):
        out, info = render("Откройте [CABINET_LINK:] сейчас.")
        assert "[CABINET_LINK" not in out
        assert URL in out and info["broken"] == 1

    def test_truncated_at_end(self):
        out, info = render("Откройте [CABINET_LINK")
        assert "[CABINET_LINK" not in out
        assert URL in out and info["broken"] == 1

    def test_bracket_inside_anchor(self):
        out, info = render("Вот [CABINET_LINK:мой [кабинет]] тут.")
        assert "[CABINET_LINK" not in out
        assert URL in out and info["broken"] == 1

    def test_broken_without_url_degrades_to_phrase(self):
        out, info = render("Откройте [CABINET_LINK:] сейчас.", url=None)
        assert out == "Откройте личный кабинет сейчас."
        assert info["broken"] == 1 and info["missing_url"]


class TestSplitInteraction:
    def test_marker_in_second_split_part(self):
        out, _ = render("Здравствуйте![SPLIT]Откройте [CABINET_LINK], пожалуйста.")
        parts = out.split("[SPLIT]")
        assert len(parts) == 2
        assert "<a href=" not in parts[0] and "<a href=" in parts[1]

    def test_marker_adjacent_to_split(self):
        out, _ = render("Готово [CABINET_LINK][SPLIT]Жду вас.")
        assert out.split("[SPLIT]")[0].endswith("</a>")
        assert len(out.split("[SPLIT]")) == 2

    def test_truncated_marker_does_not_eat_split(self):
        out, _ = render("Готово [CABINET_LINK[SPLIT]Жду вас.")
        assert "[SPLIT]" in out  # разрез сообщения пережил битый маркер
        assert "[CABINET_LINK" not in out.replace("[SPLIT]", "")

    def test_truncated_marker_does_not_eat_contact_button(self):
        # Ревью 2026-08-19 (#3): кнопка телефона не должна быть съедена
        out, info = render("Готово [CABINET_LINK[CONTACT_BUTTON]")
        assert "[CONTACT_BUTTON]" in out
        assert "[CABINET_LINK" not in out.replace("[CONTACT_BUTTON]", "")
        assert info["broken"] == 1 and URL in out

    def test_valid_marker_adjacent_to_contact_button(self):
        out, _ = render("Готово [CABINET_LINK][CONTACT_BUTTON]")
        assert "</a>[CONTACT_BUTTON]" in out


class TestSafetyNet:
    def test_raw_url_with_adjacent_punctuation(self):
        out, info = render(f"Заполните анкету: {URL}.")
        assert f'<a href="{URL}">личный кабинет</a>.' in out
        assert info["applied"] == 1 and not info["marker_found"]

    def test_distorted_url_untouched(self):
        distorted = URL[:-1] + "Q"
        out, info = render(f"Вот ссылка {distorted} держите.")
        assert out == f"Вот ссылка {distorted} держите."
        assert info["applied"] == 0

    def test_two_occurrences_both_wrapped(self):
        out, info = render(f"Раз {URL} и два {URL} готово.")
        assert out.count("<a href=") == 2 and info["applied"] == 2

    def test_no_url_available_net_silent(self):
        out, info = render(f"Вот {URL} держите.", url=None)
        assert out == f"Вот {URL} держите."  # URL неизвестен боту — не трогаем
        assert info["applied"] == 0


class TestChannels:
    def test_web_html_by_default(self):
        out, _ = render("Откройте [CABINET_LINK].", channel="web")
        assert "<a href=" in out

    def test_web_plain_degradation_switch(self, monkeypatch):
        monkeypatch.setenv("CABINET_LINK_WEB_PLAIN", "1")
        out, info = render("Откройте [CABINET_LINK].", channel="web")
        assert "<a href=" not in out
        assert f"Откройте {URL} ." == out  # голый URL отделён пробелами
        assert info["applied"] == 1

    def test_plain_switch_not_applied_to_other_channels(self, monkeypatch):
        monkeypatch.setenv("CABINET_LINK_WEB_PLAIN", "1")
        out, _ = render("Откройте [CABINET_LINK].", channel="max")
        assert "<a href=" in out


class TestHistoryNormalization:
    def test_marker_becomes_bare_url(self):
        out = clr.normalize_for_history("Откройте [CABINET_LINK], жду.", URL)
        assert out == f"Откройте {URL} , жду."
        assert "[CABINET_LINK" not in out and "<a" not in out

    def test_marker_without_url_becomes_phrase(self):
        out = clr.normalize_for_history("Откройте [CABINET_LINK], жду.", None)
        assert out == "Откройте личный кабинет, жду."

    def test_text_without_marker_unchanged(self):
        text = f"Обычный текст с {URL} внутри."
        assert clr.normalize_for_history(text, URL) == text


class TestDetectors:
    def test_needs_url_on_marker_and_probe(self):
        assert clr.needs_url("тут [CABINET_LINK]")
        assert clr.needs_url(f"сырой {URL}")
        assert not clr.needs_url("обычный текст без ссылок")


class TestWiring:
    """Стражи main.py/widget.js — по образцу source-проверок test_payload.py."""

    def test_substitution_before_contact_button_and_split(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        subst = src.index("cabinet_link_render.render_cabinet_link")
        contact = src.index('if "[CONTACT_BUTTON]" in reply_text')
        split = src.index('if "[SPLIT]" in reply_text')
        assert subst < contact < split

    def test_history_normalization_at_all_four_sites(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        # 4 вызова + 1 определение: обычный ход, LONG RETRY, ветка A, ремайндер
        calls = re.findall(r"await normalize_bot_text_for_history\(", src)
        assert len(calls) == 4, f"точек нормализации {len(calls)}, ожидалось 4"

    def test_widget_restores_restricted_anchor_form(self):
        src = WIDGET_JS.read_text(encoding="utf-8")
        assert "&lt;a href=" in src, "renderText должен восстанавливать <a> (U2)"
        assert 'rel="noopener"' in src and 'target="_blank"' in src


class TestWidget312Port:
    """Перенос виджета на канон прод-3.1.2 (план 2026-09-01-002, U3, B1):
    fetch-аватар с ngrok-заголовком, robot.png, двухфазная шапка."""

    def test_avatar_loaded_via_fetch_with_ngrok_header(self):
        src = WIDGET_JS.read_text(encoding="utf-8")
        assert "'ngrok-skip-browser-warning': '1'" in src, \
            "аватар обязан грузиться fetch'ем с ngrok-заголовком (через <img> ngrok отдаёт заглушку)"
        assert "URL.createObjectURL" in src and "_imgCache" in src, \
            "blob-URL и кэш загрузок — часть канона 3.1.2"

    def test_offline_avatar_is_robot(self):
        src = WIDGET_JS.read_text(encoding="utf-8")
        assert "robot.png" in src
        assert "person-icon.png" not in src, \
            "оффлайн-аватар канона — robot.png (B1); person-icon больше не референсится"

    def test_two_phase_header(self):
        src = WIDGET_JS.read_text(encoding="utf-8")
        assert ">Информационный бот<" in src, "стартовое имя шапки — «Информационный бот»"
        assert "'Ассистент отдела продаж'" in src, "после передачи — «Ассистент отдела продаж»"
        assert "#td-header-status.online{display:flex}" in src, \
            "статус скрыт до онлайна (display:none → .online)"
        assert "Принято! Ваш запрос отправлен специалисту" in src, \
            "триггер переключения — сообщение о передаче специалисту"
