"""Рендер сообщений виджета: allowlist renderText + кэш-дисциплина отдачи.

Урок инцидента 2026-09-03 (ссылка кабинета видна разметкой на проде):
образы multy-* собираются из ЭТОГО репозитория (Dockerfile: COPY . .), и
правило <a> в образ уехало штатно — симптом давал КЭШ БРАУЗЕРА. widget.js
подключается тегом <script> без ?v=, а add_static не слал указаний по
кэшированию — клиент держал копию, скачанную до выката (эвристическое
кэширование по Last-Modified). Отсюда два стража ниже: поведение renderText
(node-набор) и Cache-Control: no-cache на отдаче /widget-custom/*.js
с фактической проверкой ревалидации (304 / свежее тело).
"""
import asyncio
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WIDGET_JS = ROOT / "widget-custom" / "widget.js"


def _extract_function(src: str, name: str) -> str:
    """Текст функции по балансу скобок — тот же приём, что в node-наборе."""
    start = src.index(f"function {name}(")
    depth = 0
    for i in range(src.index("{", start), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"скобки функции {name} не сошлись")


def test_widget_render_node_suite():
    """Поведенческие проверки renderText — голый node, без npm (R21:
    отсутствие node — красный тест, защита не снимается молча)."""
    if shutil.which("node") is None:
        pytest.fail("node не найден — проверки renderText не могут быть сняты молча")
    proc = subprocess.run(
        ["node", "tests/js/widget_render.test.mjs"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"widget_render.test.mjs упал (код {proc.returncode})\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")


class TestSourcePins:
    def test_quotes_escaped_before_tag_restore(self):
        """Кавычки экранируются первым шагом: сырая кавычка из текста модели
        не должна уметь закрыть атрибут src/href (вырыв из атрибута = XSS)."""
        fn = _extract_function(WIDGET_JS.read_text(encoding="utf-8"), "renderText")
        assert "&quot;" in fn and "&#39;" in fn, \
            "экранирование обеих кавычек обязано жить в renderText"

    def test_anchor_rule_matches_escaped_quotes(self):
        """Анкор-правило матчит форму ПОСЛЕ экранирования кавычек."""
        fn = _extract_function(WIDGET_JS.read_text(encoding="utf-8"), "renderText")
        assert "a href=&quot;(https:" in fn, \
            "правило <a> обязано матчить экранированную форму href=&quot;...&quot;"

    def test_user_bubble_stays_text_content(self):
        """Сообщения пользователя — только textContent, никакого innerHTML."""
        fn = _extract_function(WIDGET_JS.read_text(encoding="utf-8"),
                               "appendUserBubble")
        assert "bubble.textContent = text" in fn
        assert "innerHTML" not in fn


class TestWidgetCacheDiscipline:
    """Cache-Control: no-cache на /widget-custom/*.js — браузер обязан
    ревалидировать копию перед использованием (при неизменном файле это 304
    без тела). Урок инцидента 2026-09-03: без указаний по кэшированию клиент
    держал widget.js, скачанный до выката. Приложение собирается по образцу
    test_cabinet_frontend.TestStaticCharset (реальный хук из main)."""

    @staticmethod
    def _make_app(static_dir):
        from aiohttp import web
        import main as main_mod
        app = web.Application()
        app.router.add_static('/widget-custom/', path=str(static_dir),
                              name='widget_static')
        app.on_response_prepare.append(main_mod.widget_static_utf8_charset)
        return app

    def test_widget_js_and_cabinet_js_served_with_no_cache(self):
        """Заголовок присутствует на widget.js И на скриптах кабинета —
        cabinet-app/ лежит под тем же префиксом и попадает под тот же хук
        (у них своя дисциплина ?v=, лишняя ревалидация не вредна).
        Charset-поведение хука не регрессирует."""
        from aiohttp.test_utils import TestClient, TestServer

        async def run():
            client = TestClient(TestServer(self._make_app(ROOT / 'widget-custom')))
            await client.start_server()
            try:
                for path in ('/widget-custom/widget.js',
                             '/widget-custom/cabinet-app/app.js'):
                    resp = await client.get(path)
                    assert resp.status == 200, path
                    assert resp.headers.get('Cache-Control') == 'no-cache', \
                        f"{path}: Cache-Control={resp.headers.get('Cache-Control')}"
                    assert 'charset=utf-8' in resp.headers.get('Content-Type', '').lower()
            finally:
                await client.close()

        asyncio.run(run())

    def test_revalidation_304_when_unchanged_and_fresh_body_after_change(self):
        """Фактическая ревалидация: файл не изменился → 304 с пустым телом;
        изменился → 200 со свежим телом. Файл — во временной папке, боевой
        widget-custom не трогается; mtime после правки сдвигается явно
        (os.utime), чтобы не зависеть от гранулярности часов ФС."""
        from aiohttp.test_utils import TestClient, TestServer

        def bump(path, text, mtime):
            path.write_text(text, encoding="utf-8")
            os.utime(path, (mtime, mtime))

        async def run(tmp_dir):
            js = tmp_dir / "widget.js"
            bump(js, "var WIDGET_REV = 'old';\n", 1_700_000_000)
            client = TestClient(TestServer(self._make_app(tmp_dir)))
            await client.start_server()
            try:
                first = await client.get('/widget-custom/widget.js')
                assert first.status == 200
                assert "old" in await first.text()
                validators = {}
                if first.headers.get('ETag'):
                    validators['If-None-Match'] = first.headers['ETag']
                if first.headers.get('Last-Modified'):
                    validators['If-Modified-Since'] = first.headers['Last-Modified']
                assert validators, "отдача без валидаторов — ревалидация невозможна"

                unchanged = await client.get('/widget-custom/widget.js',
                                             headers=validators)
                assert unchanged.status == 304, \
                    f"неизменный файл обязан давать 304, пришло {unchanged.status}"
                assert (await unchanged.read()) == b"", "тело 304 обязано быть пустым"

                bump(js, "var WIDGET_REV = 'new';\n", 1_700_000_060)
                changed = await client.get('/widget-custom/widget.js',
                                           headers=validators)
                assert changed.status == 200, \
                    f"изменённый файл обязан давать 200, пришло {changed.status}"
                assert "new" in await changed.text(), "тело обязано быть свежим"
            finally:
                await client.close()

        import tempfile
        with tempfile.TemporaryDirectory() as td:
            asyncio.run(run(pathlib.Path(td)))
