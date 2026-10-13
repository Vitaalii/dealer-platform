"""Кликабельная ссылка кабинета: [CABINET_LINK] → <a href> (план 2026-08-19-001, U1).

Чистые функции без I/O — wiring в main.py (send_smartbot_reply и четыре точки
записи chat_history). URL запрашивается у cabinet_service в момент подстановки
и не персистируется в контексте (КТР-5: в БД живёт только отпечаток токена).

Контракт маркера — docs/N8N_HANDOFF_CABINET.md: модель никогда не печатает
адрес, ставит маркер в месте ссылки. Принцип деградации — ссылку не терять:
битый маркер и маркер без URL дают связный текст, сырой URL, напечатанный
моделью мимо маркера, тихо оборачивается страховкой по точному вхождению.
"""
import html
import os
import re

DEFAULT_ANCHOR = "личный кабинет"

# Валидные формы: [CABINET_LINK] и [CABINET_LINK:анкор] — анкор непустой,
# до первой ']', без вложенных скобок.
_VALID_RE = re.compile(r"\[CABINET_LINK(?::([^\[\]]+))?\]")
# Битые остатки ([CABINET_LINK:], оборванный маркер, ']' в анкоре): в общем
# проходе эта альтернатива стоит ПОСЛЕ валидной и срабатывает только там, где
# валидная не совпала. Жадность ограничена строкой (не ест переносы) и
# служебными маркерами ((?!\[SPLIT\]|\[CONTACT_BUTTON\]) — оборванный маркер
# вплотную к ним не должен съесть разрез сообщения или кнопку телефона;
# фикс ревью 2026-08-19, correctness+adversarial); хвост \]\]? съедает
# вторую скобку случая «]» внутри анкора.
_BROKEN = (r"\[CABINET_LINK(?:(?!\[SPLIT\]|\[CONTACT_BUTTON\])[^\]\n])*"
           r"(?:\]\]?|(?=\[SPLIT\]|\[CONTACT_BUTTON\]|\n)|$)")
_MARKER_PROBE_RE = re.compile(r"\[CABINET_LINK")

# Сигнатура ссылки кабинета (…/cabinet#t=<токен>) — дешёвый детектор
# «в тексте может быть сырой URL» без похода в БД за самим URL.
_URL_PROBE = "#t="


def has_marker(text) -> bool:
    return bool(_MARKER_PROBE_RE.search(text or ""))


def needs_url(text) -> bool:
    """Стоит ли ходить за URL: есть маркер или похоже на сырой URL кабинета."""
    text = text or ""
    return has_marker(text) or _URL_PROBE in text


def _web_plain() -> bool:
    """Выключатель деградации web → голый URL (старый кэш widget.js, откат U2).
    Читается при вызове — тесты используют monkeypatch.setenv."""
    return os.getenv("CABINET_LINK_WEB_PLAIN", "").strip().lower() in (
        "1", "true", "yes", "on")


def _anchor_tag(url: str, anchor: str) -> str:
    # Экранирование href — страховка: токен base64url и https-база не содержат
    # спецсимволов по построению (cabinet_service._derive_token_plain)
    return (f'<a href="{html.escape(url, quote=True)}">'
            f"{html.escape(anchor)}</a>")


def _spaced(text: str, start: int, end: int, insert: str) -> str:
    """Голый URL с пробелами-разделителями: пробел добавляется только там,
    где сосед — не пробел/перенос (пунктуация вплотную ломает переход)."""
    before = "" if start == 0 or text[start - 1].isspace() else " "
    after = "" if end >= len(text) or text[end].isspace() else " "
    return before + insert + after


def _render(text: str, url, html_mode: bool):
    info = {"marker_found": False, "broken": 0, "applied": 0, "missing_url": False}
    if not text:
        return text, info

    parts = [_VALID_RE.pattern, f"(?:{_BROKEN})"]
    if url:
        parts.append(re.escape(url))
    pattern = re.compile("|".join(parts))

    def _repl(m):
        token = m.group(0)
        if url and token == url:
            # Страховка: модель напечатала адрес мимо маркера — та же форма;
            # в plain-режиме голый URL уже голый, не трогаем
            if html_mode:
                info["applied"] += 1
                return _anchor_tag(url, DEFAULT_ANCHOR)
            return token
        info["marker_found"] = True
        valid = _VALID_RE.fullmatch(token)
        if valid is None:
            # Битый маркер: анкор из него надёжно не извлекается — вычистить,
            # ссылку доставить голым URL (принцип «ссылку не терять»)
            info["broken"] += 1
            if not url:
                info["missing_url"] = True
                return DEFAULT_ANCHOR
            info["applied"] += 1
            return _spaced(text, m.start(), m.end(), url)
        anchor = (valid.group(1) or DEFAULT_ANCHOR).strip() or DEFAULT_ANCHOR
        if not url:
            info["missing_url"] = True
            return anchor  # связный текст без ссылки-пустышки
        info["applied"] += 1
        if html_mode:
            return _anchor_tag(url, anchor)
        return _spaced(text, m.start(), m.end(), url)

    return pattern.sub(_repl, text), info


def render_cabinet_link(text, url, channel):
    """Подстановка ссылки кабинета в исходящий текст перед [CONTACT_BUTTON]
    и [SPLIT] (порядок — план 2026-08-19-001, U1 Approach).

    Один проход по ИСХОДНОМУ тексту (валидный маркер | битый маркер | точное
    вхождение url): URL внутри только что вставленного href повторно не
    оборачивается. Возвращает (text, info); info: marker_found, broken,
    applied (число подстановок), missing_url (маркер был, URL не дали).
    """
    html_mode = not (channel == "web" and _web_plain())
    return _render(text or "", url, html_mode)


def normalize_for_history(text, url):
    """Текст бота для chat_history/recent_history (R6): маркеры → голый URL
    (n8n и last_bot_message видят фактическое состояние диалога), без URL —
    фраза-анкор. HTML-формы в истории не бывает — это plain-режим рендера."""
    normalized, _ = _render(text or "", url, html_mode=False)
    return normalized
