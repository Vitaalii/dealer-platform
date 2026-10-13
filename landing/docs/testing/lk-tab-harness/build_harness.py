#!/usr/bin/env python3
"""Собирает харнесс вкладки «В личном кабинете» из СБОРКИ Production-test.

Проверяется ровно то, что стоит на странице: блок
docs/tilda-blocks/Ver_4_dev/Production-test/u6-gallery.html (тестовая страница) вставляется целиком
(стиль, разметка, скрипт), как Tilda вставляет T123. Вокруг — заглушка
формы Tilda с полями живой формы и заглушки U2/U3 (window.ucQuiz,
window._ratesReady, «открыть сессию»). Перед сборкой харнесса соберите
Production: npm run build:prod.
"""
import pathlib

HERE = pathlib.Path(__file__).resolve().parent          # docs/testing/lk-tab-harness
ROOT = HERE.parents[2]                                   # корень проекта
SRC  = ROOT / 'docs' / 'tilda-blocks' / 'Ver_4_dev' / 'Production-test' / 'u6-gallery.html'
OUT  = HERE / 'harness.html'

if not SRC.exists():
    raise SystemExit('нет сборки блока: ' + str(SRC) + ' — запустите npm run build:prod')

block = SRC.read_text(encoding='utf-8')

TESTS = HERE / 'tests.js'
tests_v = int(TESTS.stat().st_mtime) if TESTS.exists() else 0

OUT.write_text(f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>U6 LK harness</title>
<script src="https://static.tildacdn.com/js/jquery-1.10.2.min.js"></script>
<style>
/* Ловушка из CLAUDE.md: Tilda навязывает content-box через
   tilda-grid-3.0.min.css. Без этой строки переполнения не видно. */
*, *:before, *:after {{ box-sizing: content-box; }}
body {{ font-family: Arial, sans-serif; margin: 24px; }}
#log {{ white-space: pre; font: 12px/1.5 monospace; margin-top: 20px; }}
.pass {{ color: #0a0; }} .fail {{ color: #d00; font-weight: bold; }}
</style>
<script>
/* Заглушка U2/U3: шаг квиза, выбранная машина, ставки. */
window.ucQuiz = {{ step: 2, selectedCar: null, back: function () {{}} }};
window.skuId = '99141111AB'; window.globalModelUtm = 'HAVALJOLION';
window.waSend = false; window.waMessenger = ''; window.ucLkSending = false;
/* Заглушка U2: блок «ТЕСТ / БОЙ» и адрес бота */
/* ?site=prod — бот не через ngrok (как боевая сборка): прогрев качает
   скрипты кабинета, настройки отдают cabinet_page_url. Путь кабинета
   остаётся /cabinet_dev: под него ответы заглушки бота. */
window.SITE_PROD = /[?&]site=prod/.test(location.search);
window.UC_SITE = {{ mode: window.SITE_PROD ? 'prod' : 'test', cabinetPath: '/cabinet_dev', isNgrok: !window.SITE_PROD, blocked: false }};
window.UNIFIEDBOT_URL = 'https://bot.test';
/* Заглушка U2: настройки дилера с cabinet_assets (L9) — пришли сразу */
window.CABINET_ASSETS = ['/widget-custom/cabinet-app/Production/calc_engine.browser.js?v=26b2fb5198',
                         '/widget-custom/cabinet-app/Production/calc_model.js?v=d7828a95bc',
                         '/widget-custom/cabinet-app/Production/app.js?v=968695878b'];
window.CABINET_PAGE_URL = window.SITE_PROD ? '/cabinet_dev?src=web&v=2' : null;
window._dealerConfigReady = Promise.resolve({{ cabinetAssets: window.CABINET_ASSETS, cabinetPageUrl: window.CABINET_PAGE_URL }});
/* Шов перехода: location.assign в браузере не подменяется */
window.NAVIGATED = [];
window.__ucLkNavigate = function (u) {{ window.NAVIGATED.push(u); }};

/* «Открыть сессию»: FETCH_MODE = ok | 422 | 404 | 429 | 403 | 503 | network |
   hang | badurl | http. Все вызовы — в window.OPEN_CALLS. */
window.FETCH_MODE = 'ok';
window.OPEN_CALLS = [];
window.fetch = function (url, opts) {{
    url = String(url);
    if (url.indexOf('/cabinet/api/web/open') === -1) return Promise.reject(new TypeError('unexpected ' + url));
    var body = null; try {{ body = JSON.parse(opts.body); }} catch (e) {{}}
    window.OPEN_CALLS.push({{ url: url, headers: opts.headers || {{}}, body: body }});
    var m = window.FETCH_MODE;
    function json(status, obj) {{ return Promise.resolve(new Response(JSON.stringify(obj), {{ status: status }})); }}
    if (m === 'network') return Promise.reject(new TypeError('Failed to fetch'));
    if (m === 'hang') return new Promise(function (res, rej) {{
        if (opts.signal) opts.signal.addEventListener('abort', function () {{ rej(new DOMException('aborted', 'AbortError')); }});
    }});
    if (m === 'ok')     return json(200, {{ cabinet_url: 'https://127.0.0.1/cabinet_dev#t=TOKEN' }});
    if (m === 'badurl') return json(200, {{ cabinet_url: 'https://evil.example/cabinet_dev#t=TOKEN' }});
    if (m === 'http')   return json(200, {{ cabinet_url: 'http://127.0.0.1/cabinet_dev#t=TOKEN' }});
    var codes = {{ '422': 'invalid_input', '404': 'unknown_position', '429': 'rate_limited', '403': 'forbidden_origin', '503': 'cabinet_unavailable' }};
    return json(Number(m), {{ error: codes[m] || 'x' }});
}};
/* Swiper галереи не нужен: шаг 2 перерисовывает галерею, заглушка
   глотает вызовы. */
window.Swiper = function () {{
    return {{ on: function () {{}}, destroy: function () {{}}, update: function () {{}},
             slideTo: function () {{}}, removeAllSlides: function () {{}},
             params: {{}}, activeIndex: 0 }};
}};
</script>
</head><body>

<!-- Блок U6 целиком, как на странице Tilda; .t-rec обязателен:
     U6 прячет и показывает свой .t-rec по uc:step. -->
<div class="t-rec">
{block}
</div>

<!-- Заглушка формы Tilda: поля живой формы page154296216 (2026-09-29). -->
<div class="uc-tildaform">
  <form id="form2472574461" class="t-form js-form-proccess" data-success-callback="t678_onSuccess"
        onsubmit="return false;">
    <input type="hidden" name="formservices[]" value="x">
    <input type="hidden" name="modification" value="1.5T"><input type="hidden" name="complectation" value="Tech+">
    <input type="hidden" name="offer"><input type="hidden" name="messenger">
    <input type="hidden" name="calculation"><input type="hidden" name="screen_type">
    <input type="hidden" name="sku-wa"><input type="hidden" name="phone"><input type="hidden" name="name">
    <input type="hidden" name="SKU" value="SKU1"><input type="hidden" name="Model" value="HAVAL JOLION">
    <input type="hidden" name="Link" value="/price?x"><input type="hidden" name="brand" value="HAVAL">
    <input type="hidden" name="dealer" value="Тест"><input type="hidden" name="dealer_id" value="99">
    <input type="hidden" name="Price_ac" value="2849000"><input type="hidden" name="Price_ol" value="2949000">
    <input type="hidden" name="letter_title" value="Тестовая заявка"><input type="hidden" name="website" value="demosite.ru">
    <input type="hidden" name="form_type" value="ВЫБОР АВТО">
    <input type="hidden" name="car_number"><input type="hidden" name="mileage">
    <div class="t-input-group t-input-group_cb">
      <label class="t-checkbox__control">
        <input type="checkbox" name="Checkbox" class="t-checkbox js-tilda-rule" checked>
        <div class="t-checkbox__indicator"></div>
        <span>Согласие на обработку данных</span>
      </label>
    </div>
    <button class="t-submit" type="button">Перейти</button>
  </form>
</div>

<div id="log"></div>


<script>
/* Заглушка отправки Tilda. Каждый клик по .t-submit пишет значения
   полей в window.SUBMITS. TILDA_MODE:
     'none'  — только запись (сценарии U3–U6);
     'ok'    — t-btn_sending → aftersuccess → затем, как настоящая Tilda,
               окно успеха и очистка полей (после события);
     'error' — t-btn_sending → aftererror;
     'hang'  — t-btn_sending и тишина. */
window.SUBMITS = [];
window.TILDA_MODE = 'none';
window.TILDA_DELAY = 30;
$('.uc-tildaform .t-submit').on('click', function () {{
    var btn = this, form = document.querySelector('.uc-tildaform form');
    var snap = {{}};
    $(form).find('input[name]').each(function () {{
        if (!this.disabled) snap[this.name] = this.value;
    }});
    window.SUBMITS.push(snap);
    if (window.TILDA_MODE === 'none') return;
    btn.classList.add('t-btn_sending');
    if (window.TILDA_MODE === 'hang') return;
    setTimeout(function () {{
        btn.classList.remove('t-btn_sending');
        var ok = window.TILDA_MODE === 'ok';
        form.dispatchEvent(new CustomEvent(ok ? 'tildaform:aftersuccess' : 'tildaform:aftererror', {{ bubbles: true }}));
        if (ok) setTimeout(function () {{
            document.body.classList.add('t-body_success-popup-showed');
            $(form).find('input[type="hidden"]').val('');
            $(form).find('input[type="checkbox"]').prop('checked', false);
        }}, 40);
    }}, window.TILDA_DELAY);
}});

window.RESULTS = [];
window.T = function (name, cond, extra) {{
    window.RESULTS.push({{ name: name, ok: !!cond, extra: extra || '' }});
    var d = document.createElement('div');
    d.className = cond ? 'pass' : 'fail';
    d.textContent = (cond ? 'ok   ' : 'FAIL ') + name + (extra ? '   ' + extra : '');
    document.getElementById('log').appendChild(d);
}};
</script>
<script src="tests.js?v={tests_v}"></script>
</body></html>
""", encoding='utf-8')
print('собрано:', OUT)
