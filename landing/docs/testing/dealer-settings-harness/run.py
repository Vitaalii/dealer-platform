#!/usr/bin/env python3
"""Прогон сценариев харнесса настроек дилера в headless Chromium.

    python3 build_harness.py && python3 run.py

Последняя строка — «итого: N ok, M fail». Нужен python-пакет playwright.
"""
import sys, threading, functools, http.server, socketserver, pathlib
from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
H = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(HERE))
http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
class Srv(socketserver.TCPServer): allow_reuse_address = True
srv = Srv(('127.0.0.1', 0), lambda *a, **k: H(*a, **k)); port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

STATE = """async () => {
  await new Promise(r => setTimeout(r, 400));
  const f = n => (document.querySelector('.uc-tildaform input[name="' + n + '"]') || {}).value;
  const val = async p => { try { return p ? await p : 'NO_PROMISE'; } catch (e) { return 'REJECTED'; } };
  const main = await Promise.race([window._mainReady ? window._mainReady.then(() => 'resolved') : 'NO_PROMISE',
                                   new Promise(r => setTimeout(() => r('pending'), 300))]);
  return { fetches: window.FETCHES.map(x => ({ url: x.url.replace(/^https:\\/\\/[^/]+/, ''), ngrok: !!(x.headers && x.headers['ngrok-skip-browser-warning']),
                                              hdrs: Object.keys(x.headers || {}).map(k => k.toLowerCase()), opts: x.opts, loading: x.loading })),
           form: { brand: f('brand'), dealer: f('dealer'), website: f('website'), letter_title: f('letter_title'), form_type: f('form_type') },
           dealer: await val(window._dealerReady), assets: await val(window._dealerConfigReady && window._dealerConfigReady.then(c => c && c.cabinetAssets)), page: await val(window._dealerConfigReady && window._dealerConfigReady.then(c => c && c.cabinetPageUrl)), activeField: await val(window._dealerConfigReady && window._dealerConfigReady.then(c => c && c.activeField)), bots: await val(window._botLinksReady), ucBots: window.UC_BOTS || null, features: await val(window._featuresReady), rates: await val(window._ratesReady),
           mainReady: main, events: window.EVENTS, all: (window.allComplectations || []).length,
           skeletonHidden: getComputedStyle(document.getElementById('uc-skeleton')).display === 'none',
           unifiedbot: window.UNIFIEDBOT_URL,
           site: window.UC_SITE ? { mode: window.UC_SITE.mode, cabinetPath: window.UC_SITE.cabinetPath, blocked: window.UC_SITE.blocked,
                                    keys: Object.keys(window.UC_SITE) } : null,
           btnInline: document.documentElement.style.getPropertyValue('--uc-btn-next-bg').trim(),
           btnComputed: getComputedStyle(document.documentElement).getPropertyValue('--uc-btn-next-bg').trim(),
           sites: typeof UC_SITES === 'object' ? Object.keys(UC_SITES) : null };
}"""

results = []
def t(name, ok, extra=''):
    ok = bool(ok); results.append(ok); print(('ok   ' if ok else 'FAIL ') + name + (('  | ' + str(extra)) if extra and not ok else ''))

def urls(st, part): return [x for x in st['fetches'] if part in x['url']]

with sync_playwright() as p:
    br = p.chromium.launch()
    def load(page, s):
        pg = br.new_page(); errs = []; cons = []
        pg.on('pageerror', lambda e: errs.append(str(e)))
        pg.on('console', lambda m: cons.append(m.text) if m.type == 'error' else None)
        pg.goto(f'http://127.0.0.1:{port}/{page}?s={s}')
        st = pg.evaluate(STATE); st['errors'] = errs; st['console'] = cons; pg.close(); return st

    print('== U2 + U7, настройки пришли ==')
    st = load('harness.html', 'ok')
    t('один запрос настроек /cabinet/api/dealer/99', len(urls(st, '/cabinet/api/dealer/99')) == 1, st['fetches'])
    t('запрос настроек с заголовком ngrok', all(x['ngrok'] for x in urls(st, '/cabinet/api/dealer/')))
    t('каталог, оснащение, ставки — по id из настроек',
      len(urls(st, '/catalog/mywf0f16ptn25gk')) == 1 and len(urls(st, '/catalog/mm8wldgk5vn9gwh')) == 1 and len(urls(st, '/catalog/mfocbgvi50mqm7c')) == 1, st['fetches'])
    t('таблица Dealers больше не читается', not urls(st, '/catalog/muruk10ipof33m5'))
    t('U7 своего запроса не делает (настройки — ровно один раз)', len(urls(st, '/cabinet/api/dealer/')) == 1)
    t('form_values вписаны в скрытые поля', st['form'] == {'brand': 'Haval', 'dealer': 'Автомир Самара', 'website': 'https://demo.autodc.org', 'letter_title': 'Тестовая заявка', 'form_type': 'ВЫБОР АВТО'}, st['form'])
    t('form_values вписаны до initCards', st['events']['initCards'] and st['events']['initCards']['brand'] == 'Haval', st['events'])
    t('initCards получил только активные машины', st['events']['initCards'] and st['events']['initCards']['n'] == 1 and st['all'] == 1, st['events'])
    t('_dealerReady — карточка из 7 публичных полей', isinstance(st['dealer'], dict) and st['dealer'].get('Dealer_Code') == '99' and len(st['dealer']) == 7 and 'Max_Group_ID' not in st['dealer'], st['dealer'])
    t('_featuresReady — строки оснащения', isinstance(st['features'], list) and len(st['features']) == 1)
    t('_ratesReady — строки ставок', isinstance(st['rates'], list) and st['rates'][0].get('Rate') == 5, st['rates'])
    t('_mainReady разрешён', st['mainReady'] == 'resolved')
    t('cabinet_page_url — дословно', st['page'] == '/cabinet_dev?src=web', st['page'])
    t('cabinet_assets — дословно, с ?v= (для прогрева L9)', st['assets'] == ['/widget-custom/cabinet-app/Production/calc_engine.browser.js?v=26b2fb5198', '/widget-custom/cabinet-app/Production/app.js?v=968695878b'], st['assets'])
    t('нет ошибок на странице', not st['errors'], st['errors'])

    for s, why in (('s404', 'настройки 404'), ('s503', 'настройки 503'), ('nocode', 'нет кода дилера'),
                   ('badtable', 'id каталога не по шаблону'), ('catfail', 'каталог 500')):
        print('== ' + why + ' ==')
        st = load('harness.html', s)
        t(why + ': скелетон убран, uc:ready отправлен, _mainReady разрешён',
          st['skeletonHidden'] and st['events']['ucReady'] >= 1 and st['mainReady'] == 'resolved', st)
        t(why + ': initCards не вызывался', st['events']['initCards'] is None)
        if s != 'catfail':
            t(why + ': поля формы не заполнены (значений по умолчанию в редакторе нет)', st['form']['brand'] == '' and st['form']['dealer'] == '', st['form'])
            t(why + ': оснащение, ставки, дилер — null', st['features'] is None and st['rates'] is None and st['dealer'] is None,
              (st['features'], st['rates'], st['dealer']))
            t(why + ': к /catalog не обращались', not urls(st, '/catalog/'), st['fetches'])
        else:
            t(why + ': поля формы из настроек уже вписаны', st['form']['brand'] == 'Haval')
        if s == 'nocode':
            t(why + ': запроса настроек нет', not urls(st, '/cabinet/api/dealer/'), st['fetches'])
        t(why + ': нет необработанных ошибок', not st['errors'], st['errors'])

    print('== form_values с мусором ==')
    st = load('harness.html', 'badvalues')
    t('нестроковое brand не вписано', st['form']['brand'] == '', st['form'])
    t('пустое letter_title не вписано', st['form']['letter_title'] == '', st['form'])
    t('остальные поля вписаны', st['form']['dealer'] == 'Автомир Самара', st['form'])
    t('cabinet_assets: чужие адреса и не строки отброшены', st['assets'] == ['/ok.js?v=1'], st['assets'])
    t('cabinet_page_url на чужой домен отброшен', st['page'] is None, st['page'])
    st = load('harness.html', 'noassets')
    t('нет cabinet_assets и cabinet_page_url — пустой список и null, каталог работает', st['assets'] == [] and st['page'] is None and st['mainReady'] == 'resolved' and not st['errors'], (st['assets'], st['errors']))

    print('== защита «ТЕСТ / БОЙ»: тестовая сборка Production-test/ на чужом хосте ==')
    st = load('harness_blocked.html', 'ok')
    t('ни одного запроса к боту', not st['fetches'], st['fetches'])
    t('UNIFIEDBOT_URL пуст', st['unifiedbot'] == '')
    t('поведение «каталог недоступен»', st['skeletonHidden'] and st['events']['ucReady'] >= 1 and st['mainReady'] == 'resolved')

    print('== флаг ветки ЛК в mySuccessFunction ==')
    pg = br.new_page(); pg.goto(f'http://127.0.0.1:{port}/harness.html?s=ok'); pg.wait_for_timeout(300)
    r = pg.evaluate("""() => {
        var delays = [], real = window.setTimeout;
        window.setTimeout = function (fn, ms) { delays.push(ms); return 0; };   // не исполняем: там location.reload
        var f = document.querySelector('.uc-tildaform form');
        window.ucLkSending = true;  window.waSend = false; mySuccessFunction(f); var withFlag = delays.slice();
        delays.length = 0;
        window.ucLkSending = false; mySuccessFunction(f); var noFlag = delays.slice();
        window.setTimeout = real;
        return { withFlag: withFlag, noFlag: noFlag };
    }""")
    pg.close()
    t('флаг ЛК: mySuccessFunction не планирует перезагрузку', r['withFlag'] == [], r)
    t('без флага: перезагрузка через 1 с, как раньше', 1000 in r['noFlag'], r)

    print('== боты Telegram / MAX по марке (/bot-links) ==')
    st = load('harness.html', 'ok')
    t('один запрос /bot-links с заголовком ngrok', len(urls(st, '/bot-links')) == 1 and all(x['ngrok'] for x in urls(st, '/bot-links')), st['fetches'])
    t('марка «Haval» (регистр не важен) → havalconsultantbot / id781424127394_2_bot; точная марка важнее «*»',
      st['bots'] == {'telegram': 'havalconsultantbot', 'max': 'id781424127394_2_bot'} and st['ucBots'] == st['bots'], st['bots'])
    t('ошибок в консоли нет', not st['console'], st['console'])
    st = load('harness.html', 'botsstar')
    t('только записи «*» (дев-бот) → TestmesssBot / id781424127394_1_bot', st['bots'] == {'telegram': 'TestmesssBot', 'max': 'id781424127394_1_bot'}, st['bots'])
    for s, why in (('botsnobrand', 'нет записи для марки'), ('bots500', '/bot-links 500')):
        st = load('harness.html', s)
        t(why + ' → оба null, ошибка в консоль, каталог работает',
          st['bots'] == {'telegram': None, 'max': None} and any('⛔' in c for c in st['console']) and st['mainReady'] == 'resolved', (st['bots'], st['console']))
    st = load('harness.html', 'botsbadlink')
    t('ссылка Telegram не t.me → telegram null, MAX есть', st['bots'] == {'telegram': None, 'max': 'id781424127394_2_bot'}, st['bots'])

    print('== переход в мессенджер (mySuccessFunction) ==')
    def success(s, messenger, immediate):
        pg = br.new_page(); got = []; cons = []
        pg.on('console', lambda m: cons.append(m.text) if m.type == 'error' else None)
        for pat in ('https://max.ru/**', 'https://web.max.ru/**', 'https://t.me/**'):
            pg.route(pat, lambda route: (got.append(route.request.url), route.abort()))
        pg.goto(f'http://127.0.0.1:{port}/harness.html?s={s}'); pg.wait_for_function('window.UC_BOTS !== undefined')
        r = pg.evaluate("""([messenger, immediate]) => {
            var delays = [], real = window.setTimeout;
            window.setTimeout = function (fn, ms) { delays.push(ms); if (immediate && ms === 1500) { try { fn(); } catch (e) {} } return 0; };
            window.skuId = '99141111AB'; window.globalModelUtm = 'HAVALJOLION';
            window.ucLkSending = false; window.waSend = true; window.waMessenger = messenger; window.isMaxOverlayActive = false;
            mySuccessFunction(document.querySelector('.uc-tildaform form'));
            window.setTimeout = real;
            return { delays: delays, overlay: !!window.isMaxOverlayActive };
        }""", [messenger, immediate])
        pg.wait_for_timeout(300); pg.close()
        return r, got, cons
    r, got, cons = success('ok', 'MAX', False)
    # ucMsgMobile по умолчанию false — компьютер; телефон — в разделе «адрес MAX» ниже
    t('MAX (компьютер): https://web.max.ru/<бот марки>?start=<код>_<utm> (encodeURIComponent)',
      got == ['https://web.max.ru/id781424127394_2_bot?start=99141111AB_HAVALJOLION'] and r['overlay'], (got, r))
    r, got, cons = success('ok', 'Telegram', True)
    t('Telegram: tg://resolve, через 1,5 с — https://t.me/<бот марки>?start=<код>_<utm>',
      got == ['https://t.me/havalconsultantbot?start=99141111AB_HAVALJOLION'] and 1500 in r['delays'], (got, r))
    r, got, cons = success('botsnobrand', 'Telegram', True)
    t('нет ссылки на бота: перехода нет, ошибка в консоль, перезагрузка через 1 с',
      got == [] and 1000 in r['delays'] and any('Нет ссылки на бота Telegram' in c for c in cons), (got, r, cons))

    print('== колонка активности каталога (tables.catalog.active_field) ==')
    st = load('harness.html', 'ok')
    t('нет поля → «Active»', st['activeField'] == 'Active' and st['events']['initCards']['models'] == ['HAVAL JOLION'], (st['activeField'], st['events']))
    st = load('harness.html', 'active1')
    t('«Active_1» → каталог по Active_1', st['activeField'] == 'Active_1' and st['events']['initCards']['models'] == ['OFF', 'ONLY_ACTIVE_1'], (st['activeField'], st['events']))
    st = load('harness.html', 'activebad')
    t('недопустимое имя → «Active»', st['activeField'] == 'Active' and st['events']['initCards']['models'] == ['HAVAL JOLION'], st['activeField'])

    print('== cabinet_page_url ==')
    st = load('harness.html', 'pageabs')
    t('полный адрес https на хосте страницы — принят', st['page'] == 'https://127.0.0.1/cabinet_dev?src=web', st['page'])
    st = load('harness.html', 'pageforeign')
    t('полный адрес на чужом хосте — отброшен', st['page'] is None, st['page'])

    print('== цвет «ДАЛЕЕ →» не в U2 ==')
    st = load('harness.html', 'ok')
    t('U2 не пишет цвет в <html> и не объявляет его (без U1 переменной нет)', st['btnInline'] == '' and st['btnComputed'] == '', (st['btnInline'], st['btnComputed']))

    print('== MODE ==')
    t("тестовая сборка: в коде только набор test", st['sites'] == ['test'], st['sites'])
    t("MODE 'test': путь /cabinet_dev; в наборе нет ботов и цвета",
      st['site']['cabinetPath'] == '/cabinet_dev' and not ({'tgBot', 'maxBot', 'btnBg', 'btnBgHover'} & set(st['site']['keys'])), st['site'])
    st = load('harness_prod.html', 'ok')
    t("боевая сборка Production/: в коде только набор prod — тестовых значений нет", st['sites'] == ['prod'], st['sites'])
    t("MODE 'prod': боевой бот, /cabinet; в наборе нет ботов и цвета",
      st['unifiedbot'] == 'https://obut-5oaj-5i2u.gw-1a.dockhost.net' and st['site']['cabinetPath'] == '/cabinet'
      and not ({'tgBot', 'maxBot', 'btnBg', 'btnBgHover'} & set(st['site']['keys'])) and st['btnInline'] == '', st['site'])
    t("MODE 'prod': запросы идут на боевой бот без заголовка ngrok",
      # запросы самого widget.js (аватар) не в счёт: он шлёт заголовок ngrok всегда
      [x for x in st['fetches'] if not x['url'].startswith('/widget-custom/')]
      and not any(x['ngrok'] for x in st['fetches'] if not x['url'].startswith('/widget-custom/')), st['fetches'])
    print('== ранние запросы и GET без Content-Type (план 2026-10-08-005) ==')
    bot = lambda st: [x for x in st['fetches'] if not x['url'].startswith('/widget-custom/')]
    for page, label in (('harness_prod.html', 'боевая сборка'), ('harness.html', 'тестовая сборка')):
        st = load(page, 'ok')
        cfg = urls(st, '/cabinet/api/dealer/')
        t(label + ': запросы настроек и /bot-links уходят до DOMContentLoaded (форма выше U2)',
          cfg and cfg[0]['loading'] and all(x['loading'] for x in urls(st, '/bot-links')), st['fetches'])
        t(label + ': ни у одного GET к боту нет Content-Type', bot(st) and not any('content-type' in x['hdrs'] for x in bot(st)), bot(st))
    st = load('harness.html', 'ok')
    # заглушка отвечает только дев-боту — данные проверяются на тестовой сборке
    t('настройки пришли до DOMContentLoaded — поля формы вписаны до initCards, карточки построены',
      st['events'].get('cfgLoading') is True and st['events']['initCards'] and st['events']['initCards']['brand'] == 'Haval'
      and st['mainReady'] == 'resolved' and not st['errors'], st['events'])
    st = load('harness_prod.html', 'ok')
    t('боевая сборка: GET к боту без заголовков вовсе (простой запрос CORS — без OPTIONS)',
      all(x['opts'] == 'none' for x in bot(st)), bot(st))
    st = load('harness.html', 'ok')
    t('тестовая сборка: заголовок ngrok — единственный', all(x['hdrs'] == ['ngrok-skip-browser-warning'] for x in bot(st)), bot(st))
    st = load('harness_late_form.html', 'ok')
    cfg = urls(st, '/cabinet/api/dealer/99')
    t('форма ниже U2: настройки запрашиваются после DOMContentLoaded, один раз, страница работает',
      len(cfg) == 1 and not cfg[0]['loading'] and st['events']['initCards'] and st['events']['initCards']['brand'] == 'Haval'
      and st['events'].get('cfgLoading') is False and not st['errors'], (st['fetches'], st['events'], st['errors']))

    print('== адрес MAX: телефон — max.ru, компьютер — web.max.ru (план 2026-10-08-001) ==')
    BOT, PAYLOAD = 'id781424127394_bot', '991211PW_HAVAL_DARGO'
    def max_jump(page, mobile):
        """mySuccessFunction в ветке MAX: куда уходит переход и куда ведёт
        запасная кнопка «Открыть в MAX». Переход отменяется событием
        navigate (Navigation API) — страница остаётся, через 1,5 с
        появляется кнопка (её показ и проверяет, что адрес тот же)."""
        pg = br.new_page()
        pg.goto(f'http://127.0.0.1:{port}/{page}?s=ok'); pg.wait_for_timeout(400)
        pg.evaluate("""m => { window.NAV = [];
            navigation.addEventListener('navigate', function (e) { window.NAV.push(e.destination.url); e.preventDefault(); });
            window.UC_BOTS = { telegram: 'tgbot', max: 'id781424127394_bot' };
            skuId = '991211PW'; globalModelUtm = 'HAVAL_DARGO'; waSend = true; waMessenger = 'MAX';
            ucLkSending = false; ucMsgMobile = m;
            mySuccessFunction(document.querySelector('.uc-tildaform form') || document.querySelector('.uc-tildaform')); }""", mobile)
        pg.wait_for_timeout(2000)
        nav = pg.evaluate("window.NAV")
        btn = pg.evaluate("(document.getElementById('max-open-btn') || {}).href || null")
        texts.append(pg.evaluate("""() => { const b = document.getElementById('max-open-btn');
            return b ? [b.textContent.trim(), b.nextElementSibling ? b.nextElementSibling.textContent.trim() : null] : null; }"""))
        helper = pg.evaluate("m => ucMaxUrl('%s', '%s', m)" % (BOT, PAYLOAD), mobile)
        pg.close(); return nav, btn, helper
    texts = []
    WEB_TXT = ['🌐 Открыть MAX в браузере', 'Для перехода в MAX нажмите на кнопку']
    APP_TXT = ['📱 Открыть в MAX', 'Для перехода в приложение MAX нажмите на кнопку']
    for page, label in (('harness.html', 'тестовая сборка'), ('harness_prod.html', 'боевая сборка')):
        nav, btn, helper = max_jump(page, False)
        t(label + ': web.max.ru — запасное окно «🌐 Открыть MAX в браузере» / «Для перехода в MAX нажмите на кнопку» (план 2026-10-08-004)',
          texts[-1] == WEB_TXT, texts[-1])
        want = 'https://web.max.ru/%s?start=%s' % (BOT, PAYLOAD)
        t(label + ': компьютер (ширина > 480) — переход на web.max.ru', nav[:1] == [want] and helper == want, (nav, helper))
        t(label + ': компьютер — «Открыть в MAX» ведёт туда же', btn == want, btn)
        nav, btn, helper = max_jump(page, True)
        t(label + ': max.ru — запасное окно без изменений «📱 Открыть в MAX» / «…в приложение MAX…»', texts[-1] == APP_TXT, texts[-1])
        want = 'https://max.ru/%s?start=%s' % (BOT, PAYLOAD)
        t(label + ': телефон (ширина ≤ 480) — переход на max.ru, как прежде', nav[:1] == [want] and helper == want, (nav, helper))
        t(label + ': телефон — «Открыть в MAX» ведёт туда же', btn == want, btn)
    pg = br.new_page(); pg.goto(f'http://127.0.0.1:{port}/harness.html?s=ok'); pg.wait_for_timeout(300)
    enc = pg.evaluate("[ucMaxUrl('b', 'A Б_&?', false), ucMaxUrl('b', 'A Б_&?', true)]"); pg.close()
    t('start-параметр кодируется одинаково на обоих адресах',
      enc[0].split('?', 1)[1] == enc[1].split('?', 1)[1] == 'start=A%20%D0%91_%26%3F', enc)

    print('== связка U6 → U2: «Где открыть MAX» на телефоне (план 2026-10-08-003) ==')
    # Боевой U2 (harness_prod) + боевой U6 из Production/, вставленный в ту же
    # страницу. Вариант выбирается кликом в U6, «Далее» — тоже в U6 (формы
    # Tilda в харнессе нет, запроса нет); затем mySuccessFunction, как после
    # успеха формы. Переход отменяется через Navigation API.
    U6P = (HERE.parents[2] / 'docs' / 'tilda-blocks' / 'Ver_4_dev' / 'Production' / 'u6-gallery.html').read_text(encoding='utf-8')
    # Чекбокс согласия и кнопка Tilda — как в форме на странице (U6 строит
    # из чекбокса свои галочки); кнопка ничего не отправляет.
    INJECT = """html => { var f = document.querySelector('.uc-tildaform form');
      f.insertAdjacentHTML('beforeend', '<div class="t-input-group t-input-group_cb"><label class="t-checkbox__control">' +
        '<input type="checkbox" name="Checkbox" class="t-checkbox js-tilda-rule" checked><div class="t-checkbox__indicator"></div>' +
        '<span>Согласие</span></label></div><button class="t-submit" type="button">Перейти</button>');
      var d = document.createElement('div'); d.innerHTML = html; document.body.appendChild(d);
      d.querySelectorAll('script').forEach(function (s) { var n = document.createElement('script'); n.textContent = s.textContent; document.body.appendChild(n); }); }"""
    def u6_u2(width, choose):
        pg = br.new_page(viewport={'width': width, 'height': 800}); errs = []
        pg.on('pageerror', lambda e: errs.append(str(e)))
        pg.goto(f'http://127.0.0.1:{port}/harness_prod.html?s=ok'); pg.wait_for_timeout(400)
        pg.evaluate(INJECT, U6P); pg.wait_for_timeout(200)
        r = pg.evaluate("""choose => {
            window.ucQuiz.selectedCar = { id: 1, fields: { 'Model': 'HAVAL DARGO', 'Complectation': 'Tech+', 'Model id': '12',
                'Complectation id': '11', 'Price new': 2029000, 'Pics': [] } };
            window.ucQuiz.step = 2; window.dispatchEvent(new CustomEvent('uc:step', { detail: { step: 2, scroll: false } }));
            var shown = $('#uc-calc-maxmode').hasClass('uc-calc-maxmode--show');
            $('#uc-ms-max').trigger('click');
            if (choose) $('#uc-calc-maxmode .uc-calc-maxmode__btn[data-max-mode="' + choose + '"]').trigger('click');
            var active = $('#uc-calc-maxmode .uc-calc-maxmode__btn--active').data('max-mode');
            $('#uc-calc-agreements .uc-calc-agr-item').addClass('uc-agr-checked');
            ucMsgMobile = 'не выставлен';
            $('#uc-gal-submit').trigger('click');
            window.NAV = [];
            navigation.addEventListener('navigate', function (e) { window.NAV.push(e.destination.url); e.preventDefault(); });
            window.UC_BOTS = { telegram: 'tgbot', max: '%s' };
            skuId = '991211PW'; globalModelUtm = 'HAVAL_DARGO'; isMaxOverlayActive = false;
            mySuccessFunction(document.querySelector('.uc-tildaform form') || document.querySelector('.uc-tildaform'));
            return { shown: shown, active: active, mobile: ucMsgMobile, messenger: waMessenger,
                     block: $('#uc-calc-maxmode').hasClass('uc-calc-maxmode--show') }; }""" % BOT, choose)
        pg.wait_for_timeout(2000)
        r['nav'] = pg.evaluate("window.NAV"); r['btn'] = pg.evaluate("(document.getElementById('max-open-btn') || {}).href || null")
        r['btnText'] = pg.evaluate("(document.getElementById('max-open-btn') || {}).textContent || null")
        r['errs'] = errs; pg.close(); return r
    APP = 'https://max.ru/%s?start=%s' % (BOT, PAYLOAD); WEB = 'https://web.max.ru/%s?start=%s' % (BOT, PAYLOAD)
    r = u6_u2(390, None)
    t('390 px, по умолчанию: блок виден, «Приложение» → переход и «Открыть в MAX» на max.ru',
      r['shown'] and r['active'] == 'app' and r['mobile'] is True and r['messenger'] == 'MAX'
      and r['nav'][:1] == [APP] and r['btn'] == APP and r['btnText'] == APP_TXT[0] and not r['errs'], r)
    r = u6_u2(390, 'web')
    t('390 px, «WEB-версия» → переход и «Открыть в MAX» на web.max.ru',
      r['active'] == 'web' and r['mobile'] is False and r['nav'][:1] == [WEB] and r['btn'] == WEB and r['btnText'] == WEB_TXT[0] and not r['errs'], r)
    t('start-параметр у «Приложения» и «WEB-версии» одинаковый', APP.split('?')[1] == WEB.split('?')[1] == 'start=' + PAYLOAD)
    r = u6_u2(1280, None)
    t('1280 px: блока нет, MAX → web.max.ru, как прежде',
      not r['shown'] and not r['block'] and r['mobile'] is False and r['nav'][:1] == [WEB] and r['btn'] == WEB and not r['errs'], r)

    print('== поп-апы Tilda при возврате во вкладку (живая haval-demo-new, U2 заменён сборкой) ==')
    # Настоящие T390 и tilda-popup-1.0 со страницы; U2 подменяется сборкой.
    # До 2026-10-08 возврат во вкладку удалял .t-popup, и следующий клик по
    # #popup:soglasie падал на focus() у null, блокируя прокрутку.
    import re as _re, subprocess as _sp, time as _time
    LIVE = 'https://demo.autodc.org/haval-demo-new'
    BUILDS = HERE.parents[2] / 'docs' / 'tilda-blocks' / 'Ver_4_dev'
    live_html = _sp.run(['curl', '-sSL', '--compressed', f'{LIVE}?cb={int(_time.time())}'], capture_output=True).stdout.decode('utf-8', 'replace')
    def with_u2(build):
        i = live_html.index('<div id="rec3912976101"'); j = live_html.index('<div id="rec', i + 10)
        seg = live_html[i:j]; a = seg.index('<!-- nominify begin -->')
        b = seg.rindex('<!-- nominify end -->') + len('<!-- nominify end -->')
        return live_html[:i] + seg[:a] + build + ' <!-- nominify end -->' + seg[b:] + live_html[j:]
    RETURN = """() => {   // уход и возврат во вкладку
        Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
        document.dispatchEvent(new Event('visibilitychange'));
        Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
        Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
        document.dispatchEvent(new Event('visibilitychange')); }"""
    POPUP = """() => { const p = document.querySelector('.t-popup[data-tooltip-hook="#popup:soglasie"]');
        return { n: document.querySelectorAll('.t-popup').length, shown: !!(p && p.classList.contains('t-popup_show')),
                 display: p ? getComputedStyle(p).display : null, opacity: p ? getComputedStyle(p).opacity : null,
                 lock: document.body.classList.contains('t-body_popupshowed') || document.body.classList.contains('t390__body_popupshowed'),
                 success: document.querySelectorAll('.t-form__successbox, .t-form-success-popup, .t706__cartwin').length }; }"""
    CLICK = "() => document.querySelector('a[href=\"#popup:soglasie\"]').click()"
    for folder, label in (('Production', 'боевая сборка'), ('Production-test', 'тестовая сборка')):
        html_u2 = with_u2((BUILDS / folder / 'u2-unifiedbot-script.html').read_text(encoding='utf-8'))
        pg = br.new_page(viewport={'width': 1280, 'height': 800}); perr = []
        pg.on('pageerror', lambda e: perr.append(str(e)[:120]))
        pg.route(lambda u: u.split('?')[0] == LIVE, lambda route: route.fulfill(status=200, content_type='text/html; charset=utf-8', body=html_u2))
        pg.goto(LIVE, wait_until='load', timeout=60000); pg.wait_for_timeout(2500)
        n0 = pg.evaluate(POPUP)['n']
        pg.evaluate(CLICK); pg.wait_for_timeout(600); open1 = pg.evaluate(POPUP)
        pg.evaluate("() => { const d = document.createElement('div'); d.className = 't-form__successbox'; document.body.appendChild(d); }")
        pg.evaluate(RETURN); pg.wait_for_timeout(300); back = pg.evaluate(POPUP)
        t(label + ': до возврата поп-апы на месте и «Согласие» открывается', n0 >= 2 and open1['shown'] and open1['opacity'] == '1', (n0, open1))
        t(label + ': после возврата поп-апы в DOM, показанный закрыт', back['n'] == n0 and not back['shown'] and back['display'] == 'none', back)
        t(label + ': после возврата классы блокировки с body сняты', not back['lock'], back)
        t(label + ': после возврата окно успеха удалено', back['success'] == 0, back)
        perr.clear(); pg.evaluate(CLICK); pg.wait_for_timeout(600); again = pg.evaluate(POPUP)
        t(label + ': клик по #popup:soglasie после возврата открывает поп-ап без ошибки',
          again['shown'] and again['opacity'] == '1' and again['display'] == 'block' and not perr, (again, perr))
        pg.evaluate("() => document.querySelector('.t-popup_show .t-popup__close').click()"); pg.wait_for_timeout(300)
        # оверлей MAX: то же закрытие, поп-ап остаётся
        pg.evaluate(CLICK); pg.wait_for_timeout(500)
        pg.evaluate("() => showMaxOverlay('https://web.max.ru/x?start=y')"); pg.wait_for_timeout(300); mx = pg.evaluate(POPUP)
        t(label + ': showMaxOverlay закрывает поп-ап, не удаляя его', mx['n'] == n0 and not mx['shown'] and not mx['lock'], mx)
        pg.close()

    print('== страница Кабинета: U7 без U2 ==')
    st = load('harness_u7.html', 'ok')
    t('U7 запрашивает настройки по dealer_id', len(urls(st, '/cabinet/api/dealer/99')) == 1, st['fetches'])
    t('U7 шлёт заголовок ngrok дев-боту', all(x['ngrok'] for x in urls(st, '/cabinet/api/dealer/')))
    t('U7 не читает таблицу Dealers', not urls(st, '/catalog/'))
    t('_dealerReady — карточка дилера', isinstance(st['dealer'], dict) and st['dealer'].get('Dealer_location') == '53.210155, 50.184859', st['dealer'])
    st = load('harness_u7.html', 's404')
    t('U7: настройки 404 → _dealerReady = null', st['dealer'] is None, st['dealer'])
    br.close()

srv.shutdown()
ok = sum(results); fail = len(results) - ok
print('итого: %d ok, %d fail' % (ok, fail))
sys.exit(1 if fail else 0)
