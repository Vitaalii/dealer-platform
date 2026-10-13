/* Заглушки харнесса настроек дилера. Режим — параметр ?s=…:
     ok         — настройки 200, каталог/оснащение/ставки отдаются
     s404       — настройки 404 unknown_dealer
     s503       — настройки 503 dealers_unavailable
     nocode     — поле dealer_id пустое
     badtable   — id таблицы каталога не по шаблону
     badvalues  — form_values с нестроковым значением, в cabinet_assets чужие пути
     noassets   — в настройках нет cabinet_assets
     catfail    — настройки 200, каталог 500
     botsstar   — /bot-links: только записи с «*» (как у дев-бота)
     botsnobrand — /bot-links: нет записей для марки и нет «*»
     bots500    — /bot-links отвечает 500
     botsbadlink — /bot-links: ссылка Telegram для марки не t.me
     active1    — tables.catalog.active_field = «Active_1»
     activebad  — tables.catalog.active_field с недопустимыми символами
     pageabs    — cabinet_page_url полным адресом на хосте страницы
     pageforeign — cabinet_page_url полным адресом на чужом хосте
   Все вызовы fetch пишутся в window.FETCHES = [{ url, headers, loading }]:
   loading — запрос ушёл до DOMContentLoaded. EVENTS.cfgLoading — настройки
   пришли до DOMContentLoaded. */
(function () {
    var S = new URLSearchParams(location.search).get('s') || 'ok';
    window.SCENARIO = S;
    window.FETCHES = [];
    window.EVENTS = { ucReady: 0, initCards: null, initGallery: 0 };

    var BOT = 'https://sporophoric-devorah-remonstrative.ngrok-free.dev';
    var SETTINGS = {
        dealer_code: '99',
        dealer: { Dealer_Code: '99', Dealer_name: 'Автомир Самара', Dealer_address: 'Самара, ул. Авроры, д. 150Е',
                  Dealer_location: '53.210155, 50.184859', Dealer_workinghours: 'пн-вс: 09:00-21:00',
                  Dealer_phone: '+7 (999) 555 5555', Dealer_status: 'Официальный дилер HAVAL', Max_Group_ID: 'секрет' },
        tables: { catalog: { table_id: 'mywf0f16ptn25gk' }, features: { table_id: 'mm8wldgk5vn9gwh' },
                  rates: { table_id: 'mfocbgvi50mqm7c' } },
        form_values: { brand: 'Haval', dealer: 'Автомир Самара', website: 'https://demo.autodc.org',
                       letter_title: 'Тестовая заявка', form_type: 'ВЫБОР АВТО' },
        cabinet_assets: ['/widget-custom/cabinet-app/Production/calc_engine.browser.js?v=26b2fb5198',
                         '/widget-custom/cabinet-app/Production/app.js?v=968695878b'],
        cabinet_page_url: '/cabinet_dev?src=web'
    };
    if (S === 'active1') SETTINGS.tables.catalog.active_field = 'Active_1';
    if (S === 'activebad') SETTINGS.tables.catalog.active_field = 'x;drop';
    if (S === 'pageabs') SETTINGS.cabinet_page_url = 'https://' + location.hostname + '/cabinet_dev?src=web';
    if (S === 'pageforeign') SETTINGS.cabinet_page_url = 'https://evil.example/cabinet_dev?src=web';
    if (S === 'noassets') { delete SETTINGS.cabinet_assets; delete SETTINGS.cabinet_page_url; }
    if (S === 'badtable') SETTINGS.tables.catalog.table_id = 'BAD/../x';
    if (S === 'badvalues') { SETTINGS.form_values.brand = 123; SETTINGS.form_values.letter_title = '';
                          SETTINGS.cabinet_assets = ['//evil.example/x.js', 'https://evil.example/y.js', 7, '/ok.js?v=1'];
                          SETTINGS.cabinet_page_url = 'https://evil.example/cabinet_dev'; }

    var CATALOG = { list: [{ Id: 1, Active: true, Active_1: false, Model: 'HAVAL JOLION', 'Model id': '14', 'Complectation id': '11', 'Price new': 2029000 },
                           { Id: 2, Active: false, Active_1: true, Model: 'OFF' },
                           { Id: 3, Active: false, Active_1: true, Model: 'ONLY_ACTIVE_1' }] };
    /* /bot-links боевого бота (2026-10-06) + запись «*», которая не должна
       перебить точную марку */
    var BOT_LINKS = { bots: [
        { channel: 'max',      brands: ['belgee', 'geely'], link: 'https://max.ru/id781424127394_bot' },
        { channel: 'max',      brands: ['haval'],           link: 'https://max.ru/id781424127394_2_bot' },
        { channel: 'telegram', brands: ['*'],               link: 'https://t.me/AnyBrandBot' },
        { channel: 'telegram', brands: ['belgee', 'geely'], link: 'https://t.me/vashassistentbot' },
        { channel: 'telegram', brands: ['haval'],           link: 'https://t.me/havalconsultantbot' }
    ] };
    if (S === 'botsstar') BOT_LINKS = { bots: [
        { channel: 'max',      brands: ['*'], link: 'https://max.ru/id781424127394_1_bot' },
        { channel: 'telegram', brands: ['*'], link: 'https://t.me/TestmesssBot' } ] };
    if (S === 'botsnobrand') BOT_LINKS = { bots: [
        { channel: 'max',      brands: ['geely'], link: 'https://max.ru/id781424127394_bot' },
        { channel: 'telegram', brands: ['geely'], link: 'https://t.me/vashassistentbot' } ] };
    if (S === 'botsbadlink') BOT_LINKS.bots[4].link = 'https://evil.example/havalconsultantbot';
    var FEATURES = { list: [{ Id: 1, 'Feature name': 'Круиз', 'Feature category': 'Комфорт' }] };
    var RATES = { list: [{ Rate: 5, Downpayment_From: 10, Term_From: 12 }] };

    function resp(status, body) {
        return Promise.resolve(new Response(JSON.stringify(body), { status: status, headers: { 'Content-Type': 'application/json' } }));
    }
    window.fetch = function (url, opts) {
        var h = (opts && opts.headers) || {};
        window.FETCHES.push({ url: String(url), headers: h, opts: opts === undefined ? 'none' : Object.keys(opts),
                              loading: document.readyState === 'loading' });
        url = String(url);
        if (url.indexOf(BOT) !== 0) return Promise.reject(new TypeError('unexpected host ' + url));
        if (url.indexOf('/cabinet/api/dealer/') !== -1) {
            if (S === 's404') return resp(404, { error: 'unknown_dealer' });
            if (S === 's503') return resp(503, { error: 'dealers_unavailable' });
            return resp(200, SETTINGS);
        }
        if (url.indexOf('/catalog/mywf0f16ptn25gk') !== -1) return S === 'catfail' ? resp(500, {}) : resp(200, CATALOG);
        if (url.indexOf('/catalog/mm8wldgk5vn9gwh') !== -1) return resp(200, FEATURES);
        if (url.indexOf('/catalog/mfocbgvi50mqm7c') !== -1) return resp(200, RATES);
        if (/\/bot-links$/.test(url)) return S === 'bots500' ? resp(500, {}) : resp(200, BOT_LINKS);
        if (url.indexOf('/widget-custom/widget.js') !== -1) return Promise.resolve(new Response('window.__WIDGET = 1;', { status: 200 }));
        return resp(404, {});
    };

    /* U3/U6 на этой странице нет — их точки входа записывают вызов и
       состояние формы в момент вызова (form_values должны быть уже вписаны). */
    window.initCards = function (data) {
        window.EVENTS.initCards = { n: data.length, brand: document.querySelector('.uc-tildaform input[name="brand"]').value,
                                    models: data.map(function (d) { return d.fields.Model; }) };
    };
    window.initGallery = function () { window.EVENTS.initGallery++; };
    window.addEventListener('uc:ready', function () { window.EVENTS.ucReady++; });
    var cfgP;
    Object.defineProperty(window, '_dealerConfigReady', { configurable: true,
        get: function () { return cfgP; },
        set: function (v) { cfgP = v; v.then(function () { window.EVENTS.cfgLoading = document.readyState === 'loading'; }); } });

    window.__applyScenarioForm = function () {
        if (S === 'nocode') document.querySelector('.uc-tildaform input[name="dealer_id"]').value = '';
    };
})();
