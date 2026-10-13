#!/usr/bin/env python3
"""Харнесс настроек дилера (план 2026-09-30-001, U2) на СБОРКАХ.

Страницы собираются из того, что реально вставляется в Tilda:
  harness.html          — U7 выше U2 (тестовая сборка), как на странице квиза;
  harness_u7.html       — только U7, как на странице Кабинета (U2 нет);
  harness_blocked.html  — U2 из Production-test/ как есть: тестовая сборка
                          на чужом хосте (127.0.0.1) должна заблокироваться;
  harness_prod.html     — U2 из Production/ как есть: боевая сборка.

harness.html: харнесс открывается с 127.0.0.1, и настоящая тестовая
сборка справедливо заблокировалась бы. Поэтому в ИСХОДНИКЕ U2 список
тестовых хостов расширяется, и он проходит тот же сборщик в режиме test
(tools/build-production.mjs --stdin --mode test). Перед сборкой
харнесса: npm run build:prod.

fetch заменяется заглушкой до загрузки блоков; режим ответа задаётся
параметром ?s=… (см. stubs.js).
"""
import pathlib, subprocess

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SRC  = ROOT / 'docs' / 'tilda-blocks' / 'Ver_4_dev'
PROD, TEST = SRC / 'Production', SRC / 'Production-test'
for d in (PROD, TEST):
    for f in ('u2-unifiedbot-script.html', 'u7-contacts.html'):
        if not (d / f).exists():
            raise SystemExit('нет сборки ' + str(d / f) + ' — запустите npm run build:prod')

def build(text, mode):
    return subprocess.run(['node', str(ROOT / 'tools' / 'build-production.mjs'), '--stdin', '--mode', mode],
                          input=text, capture_output=True, text=True, check=True).stdout

u2_src  = (SRC / 'u2-unifiedbot-script.html').read_text(encoding='utf-8')
u2_test = (TEST / 'u2-unifiedbot-script.html').read_text(encoding='utf-8')
u2_prod = (PROD / 'u2-unifiedbot-script.html').read_text(encoding='utf-8')
u7      = (TEST / 'u7-contacts.html').read_text(encoding='utf-8')

HOSTS = "var UC_TEST_HOSTS = ['demo.autodc.org'];"
if HOSTS not in u2_src:
    raise SystemExit('в исходнике U2 не найдено: ' + HOSTS)
u2_local = build(u2_src.replace(HOSTS, "var UC_TEST_HOSTS = ['demo.autodc.org', '127.0.0.1', 'localhost'];"), 'test')
u2 = u2_test

stubs_v = int((HERE / 'stubs.js').stat().st_mtime)

FORM = """
<div class="uc-tildaform">
  <form id="form2472574461" class="t-form js-form-proccess" onsubmit="return false;">
    <input type="hidden" name="dealer_id" value="99">
    <!-- Значений по умолчанию у этих пяти полей нет (владелец, 2026-10-03):
         их пишет U2 из form_values настроек дилера. -->
    <input type="hidden" name="brand" value=""><input type="hidden" name="dealer" value="">
    <input type="hidden" name="website" value=""><input type="hidden" name="letter_title" value="">
    <input type="hidden" name="form_type" value="">
    <input type="hidden" name="car_number" value=""><input type="hidden" name="mileage" value="">
  </form>
</div>
<div id="uc-skeleton">skeleton</div><div class="uc-cards-block" style="display:none">cards</div>
<script>window.__applyScenarioForm();</script>
"""

def page(title, body_blocks, pre='', form_after=False):
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>{title}</title>
<script src="https://static.tildacdn.com/js/jquery-1.10.2.min.js"></script>
<script src="stubs.js?v={stubs_v}"></script>
{pre}
</head><body>
{'' if form_after else FORM}
{body_blocks}
{FORM if form_after else ''}
<pre id="log"></pre>
</body></html>
"""

(HERE / 'harness.html').write_text(page('U2+U7', '<div class="t-rec">' + u7 + '</div>\n<div class="t-rec">' + u2_local + '</div>'), encoding='utf-8')
(HERE / 'harness_blocked.html').write_text(page('U2 blocked', '<div class="t-rec">' + u7 + '</div>\n<div class="t-rec">' + u2 + '</div>'), encoding='utf-8')
# Форма ниже U2: поля dealer_id при выполнении блока нет — запросы на ready.
(HERE / 'harness_late_form.html').write_text(page('U2 form after', '<div class="t-rec">' + u7 + '</div>\n<div class="t-rec">' + u2_local + '</div>', form_after=True), encoding='utf-8')
(HERE / 'harness_prod.html').write_text(page('U2 prod', '<div class="t-rec">' + u7 + '</div>\n<div class="t-rec">' + u2_prod + '</div>'), encoding='utf-8')
# Страница Кабинета: U2 нет, адрес бота задаёт блок #cabinet-root.
(HERE / 'harness_u7.html').write_text(page('U7 only', '<div class="t-rec">' + u7 + '</div>',
    pre="<script>window.UNIFIEDBOT_URL = 'https://sporophoric-devorah-remonstrative.ngrok-free.dev';</script>"), encoding='utf-8')
print('собрано: harness.html, harness_blocked.html, harness_prod.html, harness_late_form.html, harness_u7.html')
