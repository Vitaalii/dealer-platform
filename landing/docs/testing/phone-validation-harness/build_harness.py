#!/usr/bin/env python3
"""Собирает тестовый харнесс из НАСТОЯЩЕГО u6-gallery.html.

Стиль и скрипт извлекаются из файла дословно, вокруг них
достраивается минимальный DOM и заглушка формы Tilda. Так проверяется
тот же код, что уйдёт в Tilda, а не его пересказ.
"""
import re, pathlib, sys

# --prod: проверять сборку Production-test из Ver_4_dev (то, что стоит в Tilda),
# а не исходник Ver_3_ong. Блок вставляется целиком, как T123.
PROD = '--prod' in sys.argv

HERE = pathlib.Path(__file__).resolve().parent          # docs/testing/phone-validation-harness
ROOT = HERE.parents[2]                                   # корень проекта
SRC  = ROOT / 'docs' / 'tilda-blocks' / 'Ver_3_ong' / 'u6-gallery.html'
OUT  = HERE / 'harness.html'

if not SRC.exists():
    raise SystemExit('не найден исходник блока: ' + str(SRC))

if PROD:
    SRC = ROOT / 'docs' / 'tilda-blocks' / 'Ver_4_dev' / 'Production-test' / 'u6-gallery.html'
    if not SRC.exists():
        raise SystemExit('нет сборки: ' + str(SRC) + ' — запустите npm run build:prod')
src = SRC.read_text(encoding='utf-8')
style  = re.search(r'<style>(.*?)</style>',   src, re.S).group(1)
script = re.search(r'<script>(.*?)</script>', src, re.S).group(1)

# Разметка calc-секции — тоже из настоящего файла, целиком от
# таб-бара до кнопок навигации: вкладки нужны, иначе calcOffer не
# переключить (переменная живёт внутри IIFE).
lines = src.split('\n')
def line_of(needle, start=0):
    for i in range(start, len(lines)):
        if needle in lines[i]:
            return i
    raise SystemExit('не найдено: ' + needle)

if PROD:
    calc = None
else:
    calc = '\n'.join(lines[line_of('<div class="uc-calc-tabs">'):line_of('</div>', line_of('id="uc-gal-submit"')) + 1])

# Версия для tests.js. Без неё браузер отдаёт закешированный файл, и
# правки в тестах молча не доезжают — уже ловили: прогон показывал
# результаты предыдущей редакции.
TESTS = HERE / 'tests.js'
tests_v = int(TESTS.stat().st_mtime) if TESTS.exists() else 0

if PROD:
    head_style = ''
    tail_script = ''
    body_block = '<div class="t-rec">\n' + src + '\n</div>'
else:
    head_style = '<style>' + style + '</style>'
    tail_script = '<script>' + script + '</script>'
    body_block = ('<div class="t-rec">\n  <div id="uc-gallery-custom">\n    <div class="uc-calc-section">\n'
                  + calc + '\n    </div>\n  </div>\n</div>')

OUT.write_text(f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>U6 harness</title>
<script src="https://static.tildacdn.com/js/jquery-1.10.2.min.js"></script>
<style>
/* Ловушка из CLAUDE.md: Tilda навязывает content-box через
   tilda-grid-3.0.min.css. Без этой строки баги переполнения
   локально не воспроизводятся. */
*, *:before, *:after {{ box-sizing: content-box; }}
body {{ font-family: Arial, sans-serif; margin: 24px; }}
#log {{ white-space: pre; font: 12px/1.5 monospace; margin-top: 20px; }}
.pass {{ color: #0a0; }} .fail {{ color: #d00; font-weight: bold; }}
</style>
{head_style}
</head><body>

<!-- .t-rec обязателен: $(document).ready в U6 ищет его через
     closest() и прячет до прихода uc:step. Без обёртки fallback
     на .parent() прячет весь <body>. -->
{body_block}

<!-- Заглушка формы Tilda: воспроизводит только то, на что опирается
     наш код — .t-submit с классом t-btn_sending, скрытые поля,
     группу чекбокса и события tildaform:*. -->
<div class="uc-tildaform">
  <form class="t-form js-form-proccess" data-success-callback="t678_onSuccess"
        onsubmit="return false;">
    <input type="hidden" name="name"><input type="hidden" name="phone">
    <input type="hidden" name="offer"><input type="hidden" name="messenger">
    <input type="hidden" name="sku-wa"><input type="hidden" name="dealer_id" value="99">
    <div class="t-input-group t-input-group_cb">
      <div class="t-input-block">
        <label class="t-checkbox__control">
          <input type="checkbox" name="Checkbox" class="t-checkbox js-tilda-rule" checked>
          <div class="t-checkbox__indicator"></div>
          <span>Согласие на обработку данных</span>
        </label>
      </div>
    </div>
    <button class="t-submit" type="button">Перейти</button>
  </form>
</div>

<div id="log"></div>

{tail_script}

<script>
/* ── Управляемая заглушка Tilda ──────────────────────────────────

   Сверено с tilda-forms-1.0.min.js (t_forms__initBtnClick,
   window.tildaForm.validate, window.tildaForm.showErrors,
   t_triggerEvent) 2026-09-18. Настоящий порядок при отказе валидации:

       t_addClass(btn,'t-btn_sending'); btn.tildaSendingStatus='1';
       validate() → showErrors()
           └── в самом конце: t_triggerEvent(form,'tildaform:aftererror')
       t_removeClass(btn,'t-btn_sending'); btn.tildaSendingStatus='0';

   То есть событие приходит СИНХРОННО, внутри вызова .click(), и в
   этот момент класс t-btn_sending ещё висит. Прежняя заглушка
   изображала «класса нет, события нет» — Tilda так себя не ведёт
   никогда, и из-за этой выдумки дефект с общим текстом ошибки
   проходил тесты зелёным.

   Отказ валидации у ЭТОЙ формы возможен ровно по одной причине:
   чекбокс согласия — единственное поле с js-tilda-rule, и у него нет
   data-tilda-req. Снятый чекбокс роняет форму в ветку emptyfill
   («форма пустая»), а не в «поле обязательно». Поэтому заглушка
   сама смотрит на чекбокс, а не только на TILDA_MODE.

   TILDA_MODE:
     'ok'        — нормальный цикл: t-btn_sending → aftersuccess
     'error'     — сбой отправки: t-btn_sending → aftererror (асинхронно)
     'blocked'   — принудительный отказ валидации (синхронный aftererror)
     'hang'      — класс поставлен, ответа нет никогда
*/
window.TILDA_MODE  = 'ok';
window.TILDA_DELAY = 60;
window.RELOADED    = false;

$('.uc-tildaform .t-submit').on('click', function () {{
    var btn  = this;
    var form = document.querySelector('.uc-tildaform form');
    var cb   = document.querySelector('.uc-tildaform input[name="Checkbox"]');

    /* Валидация Tilda: единственное js-tilda-rule-поле формы. */
    var refused = (window.TILDA_MODE === 'blocked') || !(cb && cb.checked);

    btn.classList.add('t-btn_sending');
    btn.tildaSendingStatus = '1';

    if (refused) {{
        /* Синхронно, класс ещё висит — как в showErrors(). */
        form.dispatchEvent(new CustomEvent('tildaform:aftererror', {{ bubbles: true }}));
        btn.classList.remove('t-btn_sending');
        btn.tildaSendingStatus = '0';
        return;
    }}

    if (window.TILDA_MODE === 'hang') return;
    setTimeout(function () {{
        btn.classList.remove('t-btn_sending');
        btn.tildaSendingStatus = '0';
        var type = (window.TILDA_MODE === 'error')
            ? 'tildaform:aftererror' : 'tildaform:aftersuccess';
        form.dispatchEvent(new CustomEvent(type, {{ bubbles: true }}));
        if (window.TILDA_MODE === 'ok') window.RELOADED = true;  // вместо location.reload()
    }}, window.TILDA_DELAY);
}});

/* ── Мини-раннер ─────────────────────────────────────────────── */
window.RESULTS = [];
window.T = function (name, cond, extra) {{
    window.RESULTS.push({{ name: name, ok: !!cond, extra: extra || '' }});
    var d = document.createElement('div');
    d.className = cond ? 'pass' : 'fail';
    d.textContent = (cond ? 'ok   ' : 'FAIL ') + name + (extra ? '   ' + extra : '');
    document.getElementById('log').appendChild(d);
}};

window.phoneEl = function () {{ return document.getElementById('uc-calc-phone'); }};

/* Посимвольный ввод через настоящие события. */
window.typeIn = function (el, chars) {{
    el.focus();
    for (var i = 0; i < chars.length; i++) {{
        var s = el.selectionStart, e = el.selectionEnd;
        el.value = el.value.slice(0, s) + chars[i] + el.value.slice(e);
        el.setSelectionRange(s + 1, s + 1);
        $(el).trigger('input');
    }}
}};

window.pasteIn = function (el, text) {{
    el.focus();
    el.value = text;
    el.setSelectionRange(text.length, text.length);
    $(el).trigger('input');
}};

/* Backspace: сначала даём коду сдвинуть каретку (beforeinput /
   keydown), затем выполняем само удаление, затем input. */
window.backspace = function (el) {{
    el.focus();
    var ev;
    if (typeof window.InputEvent === 'function' && 'inputType' in window.InputEvent.prototype) {{
        ev = new InputEvent('beforeinput', {{ bubbles: true, cancelable: true,
                                             inputType: 'deleteContentBackward' }});
    }} else {{
        ev = new KeyboardEvent('keydown', {{ bubbles: true, cancelable: true, key: 'Backspace' }});
    }}
    el.dispatchEvent(ev);
    var s = el.selectionStart, e = el.selectionEnd;
    if (s === e && s > 0) {{ el.value = el.value.slice(0, s - 1) + el.value.slice(s); s -= 1; }}
    else {{ el.value = el.value.slice(0, s) + el.value.slice(e); }}
    el.setSelectionRange(s, s);
    $(el).trigger('input');
}};

window.setPhoneTab = function () {{
    $('[data-calc-tab="phone"]').trigger('click');
}};

/* «В чате» + карточка «На сайте» — путь, на котором открывается
   веб-виджет. Мессенджерные карточки живут только на мобайле, здесь
   нужен именно web. */
window.setChatTab = function () {{
    $('[data-calc-tab="chat"]').trigger('click');
    $('#uc-ms-site').trigger('click');
}};

/* Работа с пунктом согласия — тем же кликом, что и пользователь. */
window.agrItems = function () {{
    return document.querySelectorAll('#uc-calc-agreements .uc-calc-agr-item');
}};
window.setConsent = function (on) {{
    Array.prototype.forEach.call(window.agrItems(), function (it) {{
        if (it.classList.contains('uc-agr-checked') !== !!on) it.click();
    }});
}};

/* Заглушка виджета: без неё tryOpen в ветке «В чате»+web опрашивает
   window.openTDWidget каждые 100 мс бесконечно. */
window.openTDWidget = function (p) {{ window.WIDGET_OPENED = p; }};

/* Входим на шаг 2 тем же путём, что на живой странице. */
window.ucQuiz = window.ucQuiz || {{ step: 2, selectedCar: null,
                                    back: function () {{}}, goTo: function () {{}} }};
window.enterStep = function (n) {{
    window.dispatchEvent(new CustomEvent('uc:step', {{ detail: {{ step: n, scroll: false }} }}));
}};
$(function () {{ window.enterStep(2); window.setPhoneTab(); }});
</script>
<script src="tests.js?v={tests_v}"></script>
<script>
</script>
</body></html>
""", encoding='utf-8')

print('written', OUT, OUT.stat().st_size, 'bytes')
