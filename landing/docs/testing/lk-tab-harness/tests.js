/* Сценарии вкладки «В личном кабинете» из плана
   docs/plans/2026-09-30-001-feat-lk-tab-cabinet-web-entry-plan.md
   (U3 каркас, U5 ввод, U6 неактивная «Далее»).

   Запуск:  await runAll()   — итог последней строкой: «итого: N ok, M fail»
*/
window.runAll = async function () {
    var pass = 0, fail = 0, out = [];
    function t(name, ok, extra) {
        if (ok) pass++; else fail++;
        out.push((ok ? 'ok   ' : 'FAIL ') + name + (extra !== undefined ? '  | ' + extra : ''));
        window.LAST = name;
        window.T(name, ok, extra);
    }
    function note(s) { out.push(s); }
    var wait = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
    var $ = window.jQuery;

    /* ── заглушки и действия ─────────────────────────────────────── */
    var PIC = { url: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=' };
    function car(price, extra) {
        var f = { 'Model': 'HAVAL JOLION', 'Complectation': 'Tech+', 'Model id': '14',
                  'Complectation id': '11', 'Price new': price, 'Pics': [PIC] };
        for (var k in (extra || {})) f[k] = extra[k];
        return { id: 1, fields: f };
    }
    function enterStep(n, c) {
        if (c !== undefined) window.ucQuiz.selectedCar = c;
        window.ucQuiz.step = n;
        window.dispatchEvent(new CustomEvent('uc:step', { detail: { step: n, scroll: false } }));
    }
    /* Вход на шаг 2 открывает «В чате» (план 2026-10-08-001); сценарии
       вкладки ЛК входят и открывают её явно. */
    function enterLk(c) { enterStep(2, c); $('.uc-calc-method-btn[data-calc-tab="lk"]').trigger('click'); }
    function setRates(rows) { window._ratesReady = Promise.resolve(rows); }
    function noRates() { window._ratesReady = undefined; }

    var q = function (s) { return document.querySelector(s); };
    var btn = function () { return q('#uc-gal-submit'); };
    var visible = function (s) { var e = q(s); return !!e && getComputedStyle(e).display !== 'none'; };
    function click(s) { $(s).trigger('click'); }
    function tab(name) { click('.uc-calc-method-btn[data-calc-tab="' + name + '"]'); }
    function scenario(s) { click('.uc-calc-buy[data-scenario="' + s + '"]'); }

    function typeIn(el, chars) {
        el.focus();
        for (var i = 0; i < chars.length; i++) {
            var target = document.activeElement === el || !el ? el : document.activeElement;
            var s = el.selectionStart, e = el.selectionEnd;
            el.value = el.value.slice(0, s) + chars[i] + el.value.slice(e);
            el.setSelectionRange(s + 1, s + 1);
            el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: chars[i] }));
        }
    }
    function setVal(el, v) {
        el.focus();
        el.value = v;
        el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertFromPaste' }));
    }
    function blurOut() {
        /* Уход фокуса за пределы номера — на нейтральную кнопку */
        q('#uc-gal-back').focus();
        $(document.activeElement).trigger('focusin');
    }
    var main = function () { return q('#uc-calc-lk-plate-main'); };
    var reg  = function () { return q('#uc-calc-lk-plate-reg'); };
    var mil  = function () { return q('#uc-calc-lk-mileage'); };
    var sum  = function () { return q('#uc-calc-lk-sum'); };
    var range = function () { return q('#uc-calc-lk-range'); };
    function clearPlate() {
        main().value = ''; main().setAttribute('data-uc-len', '0'); reg().value = '';
        $(main()).trigger('input'); blurOut();
    }
    function setMileage(v) { mil().setAttribute('data-uc-prev', ''); setVal(mil(), v); }

    /* Независимый расчёт границ — для сверки с кодом блока */
    function pmt(loan, r, n) { var m = r / 12; return m <= 0.00001 ? loan / n : loan * m / (1 - Math.pow(1 + m, -n)); }
    /* минимум: взнос 90 %, 36 мес, ставка rMin; максимум: взнос 15 %,
       36 мес, ставка rMax (getSmartRate для 15 % / 36 мес) */
    function bounds(price, rMin, rMax) {
        if (rMax === undefined) rMax = rMin;
        var mn = Math.ceil(pmt(price * 0.1, rMin, 36) / 5000) * 5000;
        var mx = Math.floor(pmt(price * 0.85, rMax, 36) / 5000) * 5000;
        return { min: mn, max: Math.max(mx, mn) };
    }
    var fmt = function (n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ' '); };

    await wait(50);

    /* ── L9: прогрев кабинета ──────────────────────────────────── */
    note('== L9 прогрев ==');
    function warmLinks() {
        return Array.prototype.map.call(document.querySelectorAll('head link[rel=preconnect], head link[rel=prefetch]'),
            function (l) { return { rel: l.rel, href: l.getAttribute('href'), as: l.getAttribute('as'), co: l.getAttribute('crossorigin') }; });
    }
    async function waitLinks() {
        for (var i = 0; i < 80 && !warmLinks().length; i++) await wait(50);
        return warmLinks();
    }
    await wait(300);
    t('до шага 2 прогрева нет (настройки уже пришли)', warmLinks().length === 0);
    noRates();
    enterStep(2, car(2029000));
    t('прогрев идёт без клика по ЛК: при входе открыта «В чате»', $('.uc-calc-method-btn--active').data('calc-tab') === 'chat');
    var wl = await waitLinks();
    var pre = wl.filter(function (l) { return l.rel === 'preconnect'; });
    var pf  = wl.filter(function (l) { return l.rel === 'prefetch'; });
    t('вход на шаг 2 (вкладка «В чате») — два preconnect к боту: с crossorigin и без',
      pre.length === 2 && pre.every(function (l) { return l.href === 'https://bot.test'; }) &&
      pre.filter(function (l) { return l.co === 'anonymous'; }).length === 1, JSON.stringify(pre));
    if (window.SITE_PROD) {
        t('без ngrok: prefetch трёх файлов кабинета — дословно, с ?v=, as=script',
          window.CABINET_ASSETS.every(function (p) {
              return pf.some(function (l) { return l.href === 'https://bot.test' + p && l.as === 'script' && l.co === null; });
          }), JSON.stringify(pf));
        t('prefetch HTML кабинета — cabinet_page_url из настроек, as=document',
          pf.some(function (l) { return l.href === window.CABINET_PAGE_URL && l.as === 'document'; }) && pf.length === 4, JSON.stringify(pf));
    } else {
        t('через ngrok: скрипты кабинета не prefetch-ятся', !pf.some(function (l) { return l.as === 'script'; }), JSON.stringify(pf));
        t('prefetch HTML кабинета — без cabinet_page_url: путь из ТЕСТ / БОЙ + ?src=web, as=document',
          pf.length === 1 && pf[0].href === '/cabinet_dev?src=web' && pf[0].as === 'document', JSON.stringify(pf));
    }
    var warmCount = wl.length;
    enterStep(1); enterStep(2, car(2029000)); tab('chat'); tab('lk'); await wait(300);
    t('прогрев один раз за загрузку: повторный вход и вкладки ссылок не добавляют', warmLinks().length === warmCount, warmLinks().length);
    enterStep(1);

    /* ── U3: каркас вкладки ─────────────────────────────────────── */
    note('== U3 каркас ==');
    noRates();
    t('порядок вкладок: «В чате» | «В личном кабинете» | «По телефону»',
      $('.uc-calc-method-btn').map(function () { return $(this).data('calc-tab'); }).get().join(',') === 'chat,lk,phone');
    enterStep(2, car(2029000));
    await wait(20);
    t('вход на шаг 2 — активна «В чате»', $('.uc-calc-method-btn--active').data('calc-tab') === 'chat');
    t('видна панель чата, ЛК и телефон скрыты',
      visible('#uc-calc-chat-section') && !visible('#uc-calc-lk-section') && !visible('#uc-calc-phone-section'));
    t('кнопка — «ДАЛЕЕ →»', btn().textContent === 'ДАЛЕЕ →', btn().textContent);
    t('карточки мессенджеров по ширине: «На сайте» на компьютере, MAX на телефоне',
      window.innerWidth > 480 ? (visible('#uc-ms-site') && q('#uc-ms-site').classList.contains('uc-messenger-card--active'))
                              : (!visible('#uc-ms-site') && q('#uc-ms-max').classList.contains('uc-messenger-card--active')));
    tab('lk');
    t('клик по ЛК — видна панель ЛК, чат и телефон скрыты, «ДАЛЕЕ →»',
      visible('#uc-calc-lk-section') && !visible('#uc-calc-chat-section') && !visible('#uc-calc-phone-section') &&
      btn().textContent === 'ДАЛЕЕ →');
    t('name/phone формы отключены на ЛК',
      q('.uc-tildaform input[name="name"]').disabled && q('.uc-tildaform input[name="phone"]').disabled);

    tab('phone');
    t('«По телефону» — кнопка «ОТПРАВИТЬ»', btn().textContent === 'ОТПРАВИТЬ', btn().textContent);
    t('«По телефону» — видна панель, поля формы включены',
      visible('#uc-calc-phone-section') && !visible('#uc-calc-lk-section') &&
      !q('.uc-tildaform input[name="phone"]').disabled);
    tab('chat');
    t('«В чате» — «ДАЛЕЕ →», видна панель чата',
      btn().textContent === 'ДАЛЕЕ →' && visible('#uc-calc-chat-section') && !visible('#uc-calc-lk-section'));

    tab('lk');
    enterStep(1); enterStep(2, car(2029000)); await wait(20);
    t('выход на шаг 1 и возврат — снова «В чате»', $('.uc-calc-method-btn--active').data('calc-tab') === 'chat');
    tab('lk');

    /* Переполнение под content-box: ни один элемент панели не выходит
       за правый край панели. */
    function overflowFree() {
        var box = q('#uc-calc-lk-section').getBoundingClientRect();
        var bad = [];
        q('#uc-calc-lk-section').querySelectorAll('input, button, .uc-calc-plate, .uc-calc-field, .uc-calc-pane')
            .forEach(function (e) {
                var r = e.getBoundingClientRect();
                if (r.width && r.right > box.right + 0.5) bad.push((e.id || e.className) + ' +' + Math.round(r.right - box.right));
            });
        return bad;
    }
    var bar = q('#uc-calc-tabbar'), tabsEl = q('.uc-calc-tabs');
    var phoneBtn = q('[data-calc-tab="phone"]');
    var fits = bar.scrollWidth <= bar.clientWidth + 1;
    t('таб-бар: все вкладки помещаются или бар прокручивается (' + window.innerWidth + ')',
      fits || getComputedStyle(bar).overflowX === 'auto', getComputedStyle(bar).overflowX);
    t('затухание: высота = высоте таб-бара',
      fits || getComputedStyle(tabsEl, '::after').height === bar.offsetHeight + 'px',
      getComputedStyle(tabsEl, '::after').height + ' / ' + bar.offsetHeight);
    t('затухание скрыто, когда всё помещается', !fits || tabsEl.classList.contains('is-fit'));
    if (!fits) {
        tab('phone'); await wait(5);
        var pr = phoneBtn.getBoundingClientRect(), br = bar.getBoundingClientRect();
        t('выбранная вкладка «По телефону» прокручена в зону видимости', pr.right <= br.right + 1, Math.round(pr.right) + ' / ' + Math.round(br.right));
        enterStep(1); enterStep(2, car(2029000)); await wait(10);
        var cr = q('[data-calc-tab="chat"]').getBoundingClientRect();
        t('повторный вход: вкладка «В чате» снова видна', cr.left >= bar.getBoundingClientRect().left - 1 && bar.scrollLeft === 0,
          Math.round(cr.left) + ' / scrollLeft ' + bar.scrollLeft);
        tab('lk'); await wait(5);
        var lr = q('[data-calc-tab="lk"]').getBoundingClientRect(), br2 = bar.getBoundingClientRect();
        t('клик по ЛК: вкладка ЛК в зоне видимости', lr.left >= br2.left - 1 && lr.right <= br2.right + 1,
          Math.round(lr.left) + '…' + Math.round(lr.right) + ' / ' + Math.round(br2.left) + '…' + Math.round(br2.right));
    }
    scenario('combo'); await wait(10);
    var bad = overflowFree();
    t('панель ЛК без переполнения (ширина ' + window.innerWidth + ')', bad.length === 0, bad.join(', '));
    var nav = q('.uc-calc-nav').getBoundingClientRect(), b = btn().getBoundingClientRect();
    t('«Далее» в пределах навигации', b.right <= nav.right + 0.5, Math.round(b.right) + ' / ' + Math.round(nav.right));
    scenario('credit');

    /* Таб-бар: прокрутка только по горизонтали (план 2026-10-08-001, U3) */
    var bcs = getComputedStyle(bar);
    t('таб-бар: overflow-x auto, overflow-y hidden', bcs.overflowX === 'auto' && bcs.overflowY === 'hidden',
      bcs.overflowX + ' / ' + bcs.overflowY);

    /* «Где открыть MAX» (план 2026-10-08-003): на телефоне (<= 480) блок
       под карточками при выбранном MAX, «Приложение» по умолчанию; «Далее»
       пишет ucMsgMobile = телефон && «Приложение» (true → max.ru в U2). */
    note('== «Где открыть MAX» ==');
    var PHONE = window.innerWidth <= 480;
    var mode = function () { return $('#uc-calc-maxmode .uc-calc-maxmode__btn--active').data('max-mode'); };
    var modeAria = function () { return $('#uc-calc-maxmode [aria-checked="true"]').map(function () { return $(this).data('max-mode'); }).get().join(','); };
    function maxMode(m) { click('#uc-calc-maxmode .uc-calc-maxmode__btn[data-max-mode="' + m + '"]'); }
    async function next() {
        $('#uc-calc-agreements .uc-calc-agr-item').addClass('uc-agr-checked');
        window.ucMsgMobile = 'не выставлен';
        click('#uc-gal-submit'); await wait(20);
    }
    enterStep(1); enterStep(2, car(2029000)); await wait(10);
    if (PHONE) {
        t('телефон, первый вход: MAX выбран, блок виден, «Приложение» (aria-checked)',
          q('#uc-ms-max').classList.contains('uc-messenger-card--active') && visible('#uc-calc-maxmode') &&
          mode() === 'app' && modeAria() === 'app', mode() + ' / ' + modeAria());
        t('блок: radiogroup с подписью, кнопки role=radio, высота ≥ 44 px',
          q('#uc-calc-maxmode [role=radiogroup]').getAttribute('aria-labelledby') === 'uc-calc-maxmode-title' &&
          $('#uc-calc-maxmode button[role=radio]').length === 2 &&
          $('#uc-calc-maxmode button[role=radio]').toArray().every(function (b) { return b.getBoundingClientRect().height >= 44; }),
          $('#uc-calc-maxmode button').toArray().map(function (b) { return Math.round(b.getBoundingClientRect().height); }).join(','));
        var panel = q('#uc-calc-chat-section');
        /* Страница харнесса сама шире 390 (нет стилей Tilda) — горизонтальная
           прокрутка страницы проверяется на живой странице (скриншоты) */
        var pr = panel.getBoundingClientRect(), br = q('#uc-calc-maxmode').getBoundingClientRect();
        t('блок не расширяет панель: помещается в её ширину',
          panel.scrollWidth <= panel.clientWidth && br.left >= pr.left && br.right <= pr.right,
          panel.scrollWidth + '/' + panel.clientWidth + ', блок ' + Math.round(br.left) + '…' + Math.round(br.right) + ' в ' + Math.round(pr.left) + '…' + Math.round(pr.right));
        await next();
        t('«Приложение» → «Далее»: ucMsgMobile = true (max.ru), MAX',
          window.ucMsgMobile === true && window.waMessenger === 'MAX', String(window.ucMsgMobile));
        maxMode('web');
        t('клик «WEB-версия» — активна она, aria-checked', mode() === 'web' && modeAria() === 'web', modeAria());
        await next();
        t('«WEB-версия» → «Далее»: ucMsgMobile = false (web.max.ru), MAX',
          window.ucMsgMobile === false && window.waMessenger === 'MAX', String(window.ucMsgMobile));
        click('#uc-ms-tg');
        t('Telegram — блока нет', !visible('#uc-calc-maxmode'));
        click('#uc-ms-max');
        t('MAX → Telegram → MAX: вернулся вариант до Telegram (WEB-версия)', visible('#uc-calc-maxmode') && mode() === 'web', mode());
        $(window).trigger('resize'); await wait(300);
        t('resize без смены ширины (адресная строка): вариант не сброшен', visible('#uc-calc-maxmode') && mode() === 'web', mode());
        tab('lk'); tab('chat');
        t('ЛК → «В чате»: вариант не сброшен', visible('#uc-calc-maxmode') && mode() === 'web', mode());
        enterStep(1); enterStep(2, car(2029000)); await wait(10);
        t('повторный вход на шаг 2: MAX и WEB-версия сохранены',
          q('#uc-ms-max').classList.contains('uc-messenger-card--active') && visible('#uc-calc-maxmode') && mode() === 'web', mode());
        click('#uc-ms-tg'); enterStep(1); enterStep(2, car(2029000)); await wait(10);
        t('повторный вход после Telegram: Telegram сохранён, блока нет',
          q('#uc-ms-tg').classList.contains('uc-messenger-card--active') && !visible('#uc-calc-maxmode'));
        click('#uc-ms-max');
        /* Поворот через порог 480: подменяем $(window).width() — так же,
           как его читает U6 */
        var origW = $.fn.width;
        $.fn.width = function () { return (this[0] === window && !arguments.length && window.__fakeW) ? window.__fakeW : origW.apply(this, arguments); };
        window.__fakeW = 800; $(window).trigger('resize'); await wait(300);
        t('поворот (> 480): блока нет, «На сайте» видна и выбрана',
          !visible('#uc-calc-maxmode') && visible('#uc-ms-site') && q('#uc-ms-site').classList.contains('uc-messenger-card--active'));
        click('#uc-ms-max');
        t('поворот (> 480), клик MAX: блока нет', !visible('#uc-calc-maxmode'));
        await next();
        t('поворот (> 480), MAX → «Далее»: ucMsgMobile = false (web.max.ru)', window.ucMsgMobile === false, String(window.ucMsgMobile));
        window.__fakeW = 390; $(window).trigger('resize'); await wait(300);
        t('обратно (≤ 480): MAX выбран, блок виден, вариант прежний (WEB-версия)',
          q('#uc-ms-max').classList.contains('uc-messenger-card--active') && visible('#uc-calc-maxmode') && mode() === 'web', mode());
        window.__fakeW = 0; $.fn.width = origW;
        maxMode('app');
        await next();
        t('снова «Приложение» → ucMsgMobile = true', window.ucMsgMobile === true, String(window.ucMsgMobile));
    } else {
        t('компьютер: блока нет при «На сайте»', !visible('#uc-calc-maxmode'));
        click('#uc-ms-max');
        t('компьютер: блока нет и при MAX', !visible('#uc-calc-maxmode'));
        await next();
        t('компьютер, MAX → «Далее»: ucMsgMobile = false (web.max.ru)',
          window.ucMsgMobile === false && window.waMessenger === 'MAX', String(window.ucMsgMobile));
        click('#uc-ms-tg');
        t('компьютер: блока нет при Telegram', !visible('#uc-calc-maxmode'));
    }
    tab('lk');

    /* ── U5: шкала ──────────────────────────────────────────────── */
    note('== U5 шкала ==');
    noRates();
    enterStep(1); enterLk(car(2029000)); await wait(20);
    t('2 029 000 при 16 % → шкала 10 000…60 000',
      range().min === '10000' && range().max === '60000', range().min + '…' + range().max);
    t('платёж по умолчанию 30 000', range().value === '30000' && sum().value === '30 000', sum().value);
    t('плитка «Платеж от 10 000 ₽»', /Платеж от 10 000 ₽/.test(q('#uc-calc-lk-buy-min').textContent),
      q('#uc-calc-lk-buy-min').textContent);
    t('подписи краёв шкалы', q('#uc-calc-lk-min').textContent === '10 000 ₽' &&
      q('#uc-calc-lk-max').textContent === '60 000 ₽');
    t('aria-valuetext у ползунка', range().getAttribute('aria-valuetext') === '30 000 рублей в месяц',
      range().getAttribute('aria-valuetext'));

    var bigB = bounds(11000000, 0.16);
    enterStep(1); window.__lkReset = true;
    /* Новый вход с другой машиной: прошлый платёж 30 000 прижимается */
    enterLk(car(11000000)); await wait(20);
    t('минимум выше 30 000 → платёж = минимум (' + bigB.min + ')',
      Number(range().value) === bigB.min && sum().value === fmt(bigB.min), sum().value);

    var tinyB = bounds(150000, 0.16);
    enterStep(1); enterLk(car(150000)); await wait(20);
    t('максимум ниже 30 000 → платёж = максимум (' + tinyB.max + ')',
      Number(range().value) === tinyB.max, range().value);
    t('150 000: после округления максимум ≤ минимума — шкала из одной точки 5 000',
      tinyB.min === 5000 && tinyB.max === 5000 && range().min === '5000' && range().max === '5000' && sum().value === '5 000',
      range().min + '…' + range().max);
    t('шкала из одной точки: ползунок и подписи краёв спрятаны, поле видно',
      !visible('#uc-calc-lk-scale') && q('#uc-calc-lk-range').getClientRects().length === 0 &&
      q('#uc-calc-lk-min').getClientRects().length === 0 && q('#uc-calc-lk-max').getClientRects().length === 0 &&
      q('#uc-calc-lk-sum').getClientRects().length > 0);
    setVal(sum(), '20000'); $(sum()).trigger('change'); blurOut();
    t('шкала из одной точки: ввод в поле прижимается к минимуму', sum().value === '5 000', sum().value);
    t('шкала из одной точки: «Далее» активна', btn().getAttribute('aria-disabled') !== 'true' && btn().textContent === 'ДАЛЕЕ →');
    enterStep(1); enterLk(car(300000)); await wait(20);
    t('300 000 при 16 %: 5 000…5 000 — тоже одна точка', range().max === '5000' && !visible('#uc-calc-lk-scale'), range().min + '…' + range().max);
    enterStep(1); enterLk(car(350000)); await wait(20);
    t('350 000 при 16 %: 5 000…10 000 — ползунок снова виден', range().max === '10000' && visible('#uc-calc-lk-scale'), range().min + '…' + range().max);

    enterStep(1); enterLk(car(2029000)); await wait(20);
    function typeSum(v) { setVal(sum(), v); $(sum()).trigger('change'); blurOut(); }
    typeSum('32400');
    t('ручной ввод 32 400 → 30 000', sum().value === '30 000' && range().value === '30000', sum().value);
    typeSum('7000');
    t('7 000 при минимуме 10 000 → 10 000', sum().value === '10 000', sum().value);
    typeSum('999999');
    t('сверх максимума → максимум', sum().value === '60 000', sum().value);
    setVal(sum(), '4a5b6'); t('во время ввода — только цифры с разделителем', sum().value === '456', sum().value);
    typeSum('');
    t('пустое поле при уходе — прежний платёж', sum().value === '60 000', sum().value);
    range().value = '40000'; range().dispatchEvent(new Event('input', { bubbles: true }));
    t('ползунок → поле суммы', sum().value === '40 000', sum().value);
    t('заливка дорожки выставлена',
      /%$/.test(range().style.getPropertyValue('--uc-calc-fill')), range().style.getPropertyValue('--uc-calc-fill'));

    /* Ставки. Минимум — строка с минимальным Downpayment_From, при
       равенстве — с меньшим Term_From. Максимум — getSmartRate(36 мес,
       30 %): строка, где срок и взнос в диапазонах (Downpayment_To
       пусто — 100); иначе среди строк по сроку — с меньшим
       Downpayment_From; иначе 16 %. */
    setRates([
        { Rate: 20, Downpayment_From: 30, Term_From: 12, Term_To: 36 },
        { Rate: 10, Downpayment_From: 10, Term_From: 24, Term_To: 84 },
        { Rate: 12, Downpayment_From: 10, Term_From: 12, Term_To: 24 },
        { Rate: 'нет', Downpayment_From: 0, Term_From: 12 }
    ]);
    enterStep(1); enterLk(car(2029000)); await wait(30);
    var rb = bounds(2029000, 0.12, 0.10);
    t('минимум по 12 % (мин. взнос, мин. срок; нечисловой Rate пропущен), максимум по 10 % (15 % / 36 мес попадает в строку с пустым Downpayment_To)',
      range().min === String(rb.min) && range().max === String(rb.max), range().min + '…' + range().max + ' / ' + rb.min + '…' + rb.max);

    setRates([
        { Rate: 9,  Term_From: 36, Term_To: 36, Downpayment_From: 40, Downpayment_To: 50 },
        { Rate: 11, Term_From: 36, Term_To: 36, Downpayment_From: 35, Downpayment_To: 39 },
        { Rate: 4,  Term_From: 12, Term_To: 12, Downpayment_From: 10, Downpayment_To: 19 }
    ]);
    enterStep(1); enterLk(car(2029000)); await wait(30);
    var nb = bounds(2029000, 0.04, 0.11);
    t('нет строки для 15 % → среди строк на 36 мес — с меньшим Downpayment_From (11 %)',
      range().max === String(nb.max) && range().min === String(nb.min), range().min + '…' + range().max + ' / ' + nb.min + '…' + nb.max);

    setRates([{ Rate: 5, Term_From: 12, Term_To: 24, Downpayment_From: 10 }]);
    enterStep(1); enterLk(car(2029000)); await wait(30);
    var fb16 = bounds(2029000, 0.05, 0.16);
    t('нет строк на 36 мес → максимум по 16 %, минимум по 5 %',
      range().max === String(fb16.max) && range().min === String(fb16.min), range().min + '…' + range().max);

    /* Таблица ставок дилера 99 (выдержка из NocoDB, 2026-10-04) */
    var DEALER99 = [
        { Rate: 5,    Term_From: 12, Term_To: 12, Downpayment_From: 10, Downpayment_To: 19 },
        { Rate: 13.5, Term_From: 36, Term_To: 36, Downpayment_From: 10, Downpayment_To: 19 },
        { Rate: 9,    Term_From: 24, Term_To: 24, Downpayment_From: 30, Downpayment_To: 30 },
        { Rate: 12,   Term_From: 36, Term_To: 36, Downpayment_From: 30, Downpayment_To: 30 },
        { Rate: 12,   Term_From: 36, Term_To: 36, Downpayment_From: 20, Downpayment_To: 29 },
        { Rate: 0.1,  Term_From: 12, Term_To: 36, Downpayment_From: 70, Downpayment_To: 80 }
    ];
    setRates(DEALER99);
    enterStep(1); enterLk(car(2029000)); await wait(30);
    t('ставки дилера 99, 2 029 000 → 10 000…55 000 (5 % / 13,5 %)',
      range().min === '10000' && range().max === '55000', range().min + '…' + range().max);
    enterStep(1); enterLk(car(3749000)); await wait(30);
    t('ставки дилера 99, 3 749 000 → 15 000…105 000', range().min === '15000' && range().max === '105000', range().min + '…' + range().max);
    t('плитка «В КРЕДИТ» — по минимуму: «Платеж от 15 000 ₽»', /Платеж от 15\s000\s₽/.test(q('#uc-calc-lk-buy-min').textContent));
    setRates(null);
    enterStep(1); enterLk(car(3749000)); await wait(30);
    t('16 %, 3 749 000 → 15 000…110 000', range().min === '15000' && range().max === '110000', range().min + '…' + range().max);

    setRates([{ Rate: 0, Downpayment_From: 0, Term_From: 12, Term_To: 84 }]);
    enterStep(1); enterLk(car(2029000)); await wait(30);
    var zb = bounds(2029000, 0, 0);
    t('ставка 0 % принимается', range().max === String(zb.max), range().max + ' / ' + zb.max);

    setRates(null);
    enterStep(1); enterLk(car(2029000)); await wait(30);
    t('_ratesReady → null — 16 %', range().max === '60000', range().max);

    var resolveRates;
    window._ratesReady = new Promise(function (r) { resolveRates = r; });
    enterStep(1); enterLk(car(2029000)); await wait(20);
    var before = range().max;
    resolveRates([{ Rate: 5, Downpayment_From: 0, Term_From: 12, Term_To: 84 }]);
    await wait(30);
    var lb = bounds(2029000, 0.05, 0.05);
    t('поздние ставки пересчитывают шкалу', before === '60000' && range().max === String(lb.max),
      before + ' → ' + range().max);
    noRates();

    /* Позиция без цены: только трейд-ин */
    enterStep(1); enterLk(car(null)); await wait(20);
    t('без «Price new» — кредит и комбо скрыты, выбран трейд-ин',
      !visible('.uc-calc-buy[data-scenario="credit"]') && !visible('.uc-calc-buy[data-scenario="combo"]') &&
      $('.uc-calc-buy--active').data('scenario') === 'tradein' && !visible('#uc-calc-lk-credit') && visible('#uc-calc-lk-car'));
    enterStep(1); enterLk(car(2029000)); await wait(20);
    t('цена снова есть — плитки кредита вернулись', visible('.uc-calc-buy[data-scenario="credit"]'));
    t('…и вернулся выбор клиента (кредит), а не навязанный трейд-ин',
      $('.uc-calc-buy--active').data('scenario') === 'credit', $('.uc-calc-buy--active').data('scenario'));

    /* ── U5: госномер и пробег ──────────────────────────────────── */
    note('== U5 госномер и пробег ==');
    scenario('tradein'); await wait(5);
    t('трейд-ин: блок авто виден, блок кредита скрыт', visible('#uc-calc-lk-car') && !visible('#uc-calc-lk-credit'));
    scenario('combo'); await wait(5);
    t('комбо: видны оба блока', visible('#uc-calc-lk-car') && visible('#uc-calc-lk-credit'));
    t('aria-checked у выбранной плитки', q('.uc-calc-buy[data-scenario="combo"]').getAttribute('aria-checked') === 'true');

    clearPlate();
    typeIn(main(), 'a123bc77');
    t('ввод a123bc77 → «А 123 ВС» + «77»', main().value === 'А 123 ВС' && reg().value === '77',
      main().value + ' / ' + reg().value);
    t('после основной части фокус в регионе', document.activeElement === reg());

    clearPlate();
    typeIn(main(), 'БДZ1');
    t('Б, Д, Z и цифра на месте буквы не вводятся', main().value === '', main().value);
    typeIn(main(), 'к1х');
    t('буква на месте цифры не вводится, строчные → прописные', main().value === 'К 1', main().value);

    clearPlate();
    typeIn(main(), 'А1');
    var mm = main(); mm.setSelectionRange(1, 1);
    mm.value = 'АЖ 1'; mm.setSelectionRange(2, 2);
    mm.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: 'Ж' }));
    t('отброшенный символ в середине не сдвигает каретку', main().value === 'А 1' && main().selectionStart === 1,
      main().value + ' caret ' + main().selectionStart);

    clearPlate();
    setVal(main(), 'А123ВС'); reg().value = ''; reg().focus();
    var kd = new KeyboardEvent('keydown', { key: 'Backspace', bubbles: true, cancelable: true });
    reg().dispatchEvent(kd);
    t('Backspace в пустом регионе — фокус в основную часть, символ не стёрт',
      document.activeElement === main() && kd.defaultPrevented && main().value === 'А 123 ВС', main().value);

    clearPlate();
    setVal(main(), 'а 123 вс 777');
    t('вставка «а 123 вс 777» целиком', main().value === 'А 123 ВС' && reg().value === '777',
      main().value + ' / ' + reg().value);

    clearPlate();
    setVal(main(), 'А12'); blurOut();
    t('уход из поля с «А12» — ошибка', !q('#uc-calc-lk-plate-err').hidden &&
      main().getAttribute('aria-invalid') === 'true' && q('#uc-calc-lk-plate').classList.contains('uc-calc-plate--err'));
    setVal(main(), 'А123ВС'); setVal(reg(), '77');
    t('номер дописан — ошибка снята во время ввода', q('#uc-calc-lk-plate-err').hidden);
    clearPlate();
    t('уход из пустого номера — без ошибки', q('#uc-calc-lk-plate-err').hidden);

    function plateOk(m, r) { clearPlate(); setVal(main(), m); setVal(reg(), r); blurOut();
        return q('#uc-calc-lk-plate-err').hidden; }
    t('регион 00 — ошибка', !plateOk('А123ВС', '00'));
    t('регион 250 — ошибка', !plateOk('А123ВС', '250'));
    t('регион 177 — верно', plateOk('А123ВС', '177'));
    setVal(reg(), '7a8'); t('регион: только цифры', reg().value === '78', reg().value);

    setMileage('1000000');
    t('пробег 1 000 000 принят', mil().value === '1 000 000', mil().value);
    var m = mil(); m.focus(); m.setSelectionRange(m.value.length, m.value.length); typeIn(m, '0');
    t('цифра сверх 1 000 000 не вводится', mil().value === '1 000 000', mil().value);
    setMileage('ab12c');
    t('пробег: нецифровое отбрасывается', mil().value === '12', mil().value);
    setMileage('0');
    t('пробег 0 принят', mil().value === '0', mil().value);

    /* ── U6: неактивная «Далее» ─────────────────────────────────── */
    note('== U6 неактивная «Далее» ==');
    scenario('credit'); await wait(5);
    t('кредит — кнопка активна', !btn().hasAttribute('aria-disabled'));

    scenario('tradein');
    plateOk('А123ВС', '77'); setMileage('86000');
    t('трейд-ин с номером и пробегом — активна', !btn().hasAttribute('aria-disabled'));
    setMileage('');
    t('пробег пуст — aria-disabled', btn().getAttribute('aria-disabled') === 'true' &&
      btn().getAttribute('aria-describedby') === 'uc-calc-tip');
    t('подсказка про пробег', q('#uc-calc-tip').textContent === 'Укажите пробег, чтобы продолжить',
      q('#uc-calc-tip').textContent);
    t('нетронутый пустой пробег не красный', !mil().classList.contains('uc-calc-input--error'));

    clearPlate();
    t('пустой номер и пустой пробег — «Внесите гос. номер…» (первое поле на экране)',
      q('#uc-calc-tip').textContent === 'Внесите гос. номер, чтобы продолжить', q('#uc-calc-tip').textContent);
    setVal(main(), 'А12');
    t('неполный номер — «Исправьте гос. номер…»',
      q('#uc-calc-tip').textContent === 'Исправьте гос. номер, чтобы продолжить', q('#uc-calc-tip').textContent);
    clearPlate(); setVal(reg(), '77');
    t('только регион — номер считается введённым, «Исправьте…»',
      q('#uc-calc-tip').textContent === 'Исправьте гос. номер, чтобы продолжить', q('#uc-calc-tip').textContent);
    plateOk('А123ВС', '00');
    t('полный номер с неверным регионом — «Исправьте…»',
      q('#uc-calc-tip').textContent === 'Исправьте гос. номер, чтобы продолжить', q('#uc-calc-tip').textContent);
    setVal(main(), 'А123ВС'); setVal(reg(), '77');
    t('номер исправлен — подсказка про пробег', q('#uc-calc-tip').textContent === 'Укажите пробег, чтобы продолжить');

    var n1 = window.SUBMITS.length;
    click('#uc-gal-submit');
    t('нажатие по неактивной — подсказка открыта, отправки нет',
      q('#uc-calc-tip').classList.contains('uc-calc-tip--open') && window.SUBMITS.length === n1);
    click('.uc-calc-pane');
    t('нажатие вне кнопки закрывает подсказку', !q('#uc-calc-tip').classList.contains('uc-calc-tip--open'));
    click('#uc-gal-submit');
    window.dispatchEvent(new Event('scroll'));
    t('прокрутка закрывает подсказку', !q('#uc-calc-tip').classList.contains('uc-calc-tip--open'));

    setMileage('50000');
    t('пробег введён — кнопка активна, подсказка скрыта',
      !btn().hasAttribute('aria-disabled') && !q('#uc-calc-tipw').classList.contains('is-inactive'));

    /* Согласие: активная кнопка без галочки — ошибка, отправки нет */
    $('#uc-calc-agreements .uc-calc-agr-item').removeClass('uc-agr-checked');
    var n2 = window.SUBMITS.length;
    click('#uc-gal-submit');
    t('без согласия — ошибка согласия, отправки нет',
      visible('#uc-calc-error') && /согласие/i.test(q('#uc-calc-error').textContent) && window.SUBMITS.length === n2,
      q('#uc-calc-error').textContent);
    $('#uc-calc-agreements .uc-calc-agr-item').addClass('uc-agr-checked');

    setMileage('');
    tab('chat');
    t('переход на «В чате» снимает aria-disabled и подсказку',
      !btn().hasAttribute('aria-disabled') && !q('#uc-calc-tipw').classList.contains('is-inactive'));
    tab('lk');
    t('возврат на ЛК — неактивность вернулась (трейд-ин, пробег пуст)', btn().getAttribute('aria-disabled') === 'true');

    /* ── U7–U8: отправка из вкладки ЛК ──────────────────────────── */
    note('== U7 отправка ==');
    var form = q('.uc-tildaform form');
    var fval = function (n) { return (form.querySelector('[name="' + n + '"]') || {}).value; };
    var lastOpen = function () { return window.OPEN_CALLS[window.OPEN_CALLS.length - 1]; };
    var lastSubmit = function () { return window.SUBMITS[window.SUBMITS.length - 1] || {}; };
    var failShown = function () { return !q('#uc-calc-lk-fail').hidden; };
    async function freshLk(c) {
        window.TILDA_MODE = 'ok'; window.FETCH_MODE = 'ok';
        /* 6 000 000 при 16 % — шкала 25 000…175 000: в неё входят все
           платежи сценариев отправки (30 000…130 000) */
        enterStep(1); enterLk(c || car(6000000)); await wait(20);
        tab('lk'); scenario('credit');
        $('#uc-calc-agreements .uc-calc-agr-item').addClass('uc-agr-checked');
        /* платёж сохраняется между входами на шаг — задаём явно */
        setPay(30000);
    }
    function setPay(v) { setVal(sum(), String(v)); $(sum()).trigger('change'); blurOut(); }

    await freshLk();
    var s0 = window.SUBMITS.length, o0 = window.OPEN_CALLS.length;
    click('#uc-gal-submit');
    t('ожидание: кнопка заблокирована, спиннер, aria-busy',
      btn().disabled && btn().classList.contains('uc-quiz-btn-next--lk-busy') && btn().getAttribute('aria-busy') === 'true');
    t('ожидание: вкладки, панель, согласие и «Назад» недоступны',
      q('.uc-calc-tabbar').hasAttribute('inert') && q('#uc-calc-lk-section').hasAttribute('inert') &&
      q('#uc-calc-agreements').hasAttribute('inert') && q('#uc-gal-back').hasAttribute('inert'));
    t('флаг ветки ЛК для U2 поднят', window.ucLkSending === true);
    var sub = lastSubmit();
    t('форма Tilda ушла один раз', window.SUBMITS.length === s0 + 1);
    t('L4b: «открыть сессию» ушёл сразу, вместе с формой', window.OPEN_CALLS.length === o0 + 1);
    var navBefore = window.NAVIGATED.length;
    await wait(10);
    t('L4b: бот ответил, форма ещё в полёте — перехода нет', window.NAVIGATED.length === navBefore && btn().disabled);
    t('offer = «ЛК с сайта», form_type = «В кредит»', sub.offer === 'ЛК с сайта' && sub.form_type === 'В кредит', sub.offer + ' / ' + sub.form_type);
    t('sku-wa — код 99+14+11 и 4 буквы', /^991411[A-Z]{4}$/.test(sub['sku-wa']), sub['sku-wa']);
    t('messenger пуст', sub.messenger === '');
    await wait(150);
    var oc = lastOpen() || { body: {}, headers: {} };
    t('после успеха формы — переход, «открыть сессию» не повторялся', window.OPEN_CALLS.length === o0 + 1 && window.NAVIGATED.length === navBefore + 1);
    t('тело: дилер, код из формы, nonce, пара позиции',
      oc.body.dealer_code === '99' && oc.body.lead_code === sub['sku-wa'] && /^[A-Za-z0-9_-]{22,128}$/.test(oc.body.nonce) &&
      oc.body.model_id === '14' && oc.body.complectation_id === '11', JSON.stringify(oc.body).slice(0, 200));
    t('тело: кредит 30 000, без номера и пробега',
      oc.body.scenario === 'credit' && oc.body.desired_payment === 30000 && !('plate' in oc.body) && !('mileage' in oc.body));
    var fb = oc.body.form || {};
    t('блок form: FORMID и поля формы как ушли',
      fb.FORMID === 'form2472574461' && fb.Model === 'HAVAL JOLION' && fb.dealer_id === '99' && fb.offer === 'ЛК с сайта' &&
      fb.form_type === 'В кредит' && fb['sku-wa'] === sub['sku-wa'] && fb.Checkbox, JSON.stringify(fb).slice(0, 300));
    t('блок form: нет name, phone, formservices, пустых', !('name' in fb) && !('phone' in fb) && !('formservices[]' in fb) && !('calculation' in fb));
    t('кредит: CAR_NUMBER и mileage в форме пусты и в блок form не попали',
      sub.car_number === '' && sub.mileage === '' && !('CAR_NUMBER' in fb) && !('mileage' in fb), JSON.stringify({c: sub.car_number, m: sub.mileage}));
    t('значения из form_values ушли в форму: brand, dealer, website, letter_title',
      sub.brand && sub.dealer && sub.website && sub.letter_title, JSON.stringify({b: sub.brand, d: sub.dealer, w: sub.website, l: sub.letter_title}));
    t('блок form: только строки', Object.keys(fb).every(function (k) { return typeof fb[k] === 'string'; }));
    t('заголовок ngrok — только для бота через ngrok', (oc.headers['ngrok-skip-browser-warning'] === '1') === !window.SITE_PROD);
    t('переход по cabinet_url', window.NAVIGATED[window.NAVIGATED.length - 1] === 'https://127.0.0.1/cabinet_dev#t=TOKEN');
    await wait(150);
    t('после очистки Tilda скрытые поля возвращены', fval('Model') === 'HAVAL JOLION' && fval('dealer_id') === '99' && fval('Price_ac') === '2849000',
      fval('Model') + ' / ' + fval('dealer_id'));
    t('блокировка прокрутки Tilda снята', !document.body.classList.contains('t-body_success-popup-showed'));
    t('после перехода ожидание не снимается', btn().disabled);

    var firstCode = oc.body.lead_code, firstNonce = oc.body.nonce;
    window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
    t('«Назад» (pageshow persisted) снимает ожидание',
      !btn().disabled && !q('.uc-calc-tabbar').hasAttribute('inert') && btn().textContent === 'ДАЛЕЕ →' && document.activeElement === btn());
    var s1 = window.SUBMITS.length;
    click('#uc-gal-submit'); await wait(120);
    t('повторное «Далее» без изменений: форма не уходит, тот же код и nonce',
      window.SUBMITS.length === s1 && lastOpen().body.lead_code === firstCode && lastOpen().body.nonce === firstNonce);
    window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));

    var spec = document.createElement('input'); spec.type = 'hidden'; spec.name = 'tildaspec-cookie'; spec.value = 'a=b'; form.appendChild(spec);
    setPay(50000);
    click('#uc-gal-submit'); await wait(200);
    t('изменён платёж: новая форма, новый код и nonce',
      window.SUBMITS.length === s1 + 1 && lastOpen().body.lead_code !== firstCode && lastOpen().body.nonce !== firstNonce && lastOpen().body.desired_payment === 50000);
    t('служебные tildaspec-* в блок form не попадают', !('tildaspec-cookie' in lastOpen().body.form));
    spec.remove();
    window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));

    /* комбо и трейд-ин */
    await freshLk(); scenario('combo'); plateOk('А123ВС', '77'); setMileage('86000');
    click('#uc-gal-submit'); await wait(200);
    t('комбо: form_type «Комбо», платёж, номер, пробег целым',
      lastSubmit().form_type === 'Комбо' && lastOpen().body.scenario === 'combo' && lastOpen().body.desired_payment === 30000 &&
      lastOpen().body.plate === 'А123ВС77' && lastOpen().body.mileage === 86000, JSON.stringify(lastOpen().body).slice(0, 160));
    t('комбо: CAR_NUMBER = номер, mileage = пробег (число) — в форме Tilda и в блоке form',
      lastSubmit().car_number === 'А123ВС77' && lastSubmit().mileage === '86000' &&
      lastOpen().body.form.car_number === 'А123ВС77' && lastOpen().body.form.mileage === '86000',
      JSON.stringify({c: lastSubmit().car_number, m: lastSubmit().mileage}));
    window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
    scenario('tradein'); await wait(5);
    click('#uc-gal-submit'); await wait(200);
    t('трейд-ин: CAR_NUMBER и mileage заполнены', lastSubmit().car_number === 'А123ВС77' && lastSubmit().mileage === '86000');
    t('трейд-ин: form_type «В трейдин», без платежа',
      lastSubmit().form_type === 'В трейдин' && lastOpen().body.scenario === 'tradein' && !('desired_payment' in lastOpen().body));
    window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));

    note('== U8 сбой ==');
    await freshLk(); setPay(45000);
    window.FETCH_MODE = '503';
    var s2 = window.SUBMITS.length;
    click('#uc-gal-submit'); await wait(200);
    t('бот 503 — плашка сбоя, кнопка «↻ ПОВТОРИТЬ», всё разблокировано',
      failShown() && btn().textContent === '↻ ПОВТОРИТЬ' && !btn().disabled && !q('.uc-calc-tabbar').hasAttribute('inert'), btn().textContent);
    t('фокус на тексте ошибки', document.activeElement === q('#uc-calc-lk-fail-text'));
    var failCode = lastOpen().body.lead_code;
    window.FETCH_MODE = 'ok';
    click('#uc-gal-submit'); await wait(150);
    t('«ПОВТОРИТЬ» после успешной формы: только бот, тот же код',
      window.SUBMITS.length === s2 + 1 && lastOpen().body.lead_code === failCode && window.NAVIGATED[window.NAVIGATED.length - 1].indexOf('/cabinet_dev') !== -1);
    window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));

    var codes = ['422', '404', '429', '403', 'network', 'badurl', 'http'];
    for (var ci = 0; ci < codes.length; ci++) {
        await freshLk(); setPay(60000 + ci * 5000);
        window.FETCH_MODE = codes[ci];
        var nav0 = window.NAVIGATED.length;
        click('#uc-gal-submit'); await wait(200);
        t('ответ «' + codes[ci] + '» — плашка, перехода нет', failShown() && window.NAVIGATED.length === nav0);
    }

    await freshLk(); setPay(100000);
    window.TILDA_MODE = 'error';
    var o3 = window.OPEN_CALLS.length, n3 = window.NAVIGATED.length;
    click('#uc-gal-submit'); await wait(150);
    t('L4b: форма Tilda с ошибкой, бот ответил — переход, флаг ЛК снят',
      !failShown() && window.OPEN_CALLS.length === o3 + 1 && window.NAVIGATED.length === n3 + 1 && window.ucLkSending === false);
    window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));

    await freshLk(); setPay(102000);
    window.TILDA_MODE = 'error'; window.FETCH_MODE = '503';
    var s3 = window.SUBMITS.length;
    click('#uc-gal-submit'); await wait(150);
    t('L4b: форма с ошибкой и бот 503 — плашка', failShown() && window.SUBMITS.length === s3 + 1);
    window.TILDA_MODE = 'ok'; window.FETCH_MODE = 'ok';
    click('#uc-gal-submit'); await wait(150);
    t('L4b: «ПОВТОРИТЬ» после ошибки формы — форма уходит снова, тот же код, переход',
      window.SUBMITS.length === s3 + 2 && lastSubmit()['sku-wa'] === window.SUBMITS[s3]['sku-wa'] &&
      window.NAVIGATED[window.NAVIGATED.length - 1].indexOf('/cabinet_dev') !== -1);
    window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));

    await freshLk(); setPay(103000);
    window.TILDA_MODE = 'error'; window.FETCH_MODE = '503';
    click('#uc-gal-submit'); await wait(150);

    click('.uc-calc-alt[data-calc-alt="chat"]');
    t('кнопка «В чате» в плашке — вкладка «В чате», плашка скрыта',
      $('.uc-calc-method-btn--active').data('calc-tab') === 'chat' && !failShown());

    await freshLk(); setPay(105000); window.FETCH_MODE = '503';
    click('#uc-gal-submit'); await wait(200);
    setPay(110000);
    t('изменили платёж после сбоя — плашка ушла, «ДАЛЕЕ →»', !failShown() && btn().textContent === 'ДАЛЕЕ →');

    /* После ЛК (форма ушла, бот упал) — «По телефону»: поля машины на
       месте, offer и form_type свои, флаг ЛК снят. */
    await freshLk(); setPay(115000); window.FETCH_MODE = '503';
    click('#uc-gal-submit'); await wait(250);
    window.TILDA_MODE = 'none';
    click('.uc-calc-alt[data-calc-alt="phone"]');
    q('#uc-calc-name').value = 'Иван';
    var ph = q('#uc-calc-phone'); ph.value = ''; ph.focus(); typeIn(ph, '9123456789');
    click('#uc-gal-submit');
    var ps = lastSubmit();
    t('«По телефону» после ЛК: Model и dealer_id в форме, offer и form_type свои, флаг снят',
      ps.Model === 'HAVAL JOLION' && ps.dealer_id === '99' && ps.offer === 'По телефону' && ps.form_type === 'ВЫБОР АВТО' && window.ucLkSending === false &&
      ps.brand && ps.dealer && ps.website && ps.letter_title,
      JSON.stringify({ m: ps.Model, d: ps.dealer_id, o: ps.offer, f: ps.form_type, flag: window.ucLkSending }));
    window.ucSetSendingReset = true;
    enterStep(1);

    /* Комбо из ЛК (форма ушла, бот упал) → «В чате»: номер и пробег из
       слепка ЛК в заявку чата не уходят, поля настроек на месте. */
    await freshLk(); scenario('combo'); plateOk('А123ВС77'.slice(0, 6), '77'); setMileage('86000');
    window.FETCH_MODE = '503';
    click('#uc-gal-submit'); await wait(250);
    t('комбо ЛК ушло в форму с номером', lastSubmit().car_number === 'А123ВС77');
    window.TILDA_MODE = 'none';
    click('.uc-calc-alt[data-calc-alt="chat"]');
    click('#uc-gal-submit');
    var cs = lastSubmit();
    t('«В чате» после комбо ЛК: CAR_NUMBER и mileage пусты, form_type «ВЫБОР АВТО», поля настроек заполнены',
      cs.offer === 'В чате' && cs.car_number === '' && cs.mileage === '' && cs.form_type === 'ВЫБОР АВТО' &&
      cs.brand && cs.dealer && cs.website && cs.letter_title,
      JSON.stringify({o: cs.offer, c: cs.car_number, m: cs.mileage, f: cs.form_type, b: cs.brand}));
    window.isMaxOverlayActive = false;
    enterStep(1);

    await freshLk(car(6000000, { 'Model id': '7' }));
    var s4 = window.SUBMITS.length;
    click('#uc-gal-submit'); await wait(50);
    t('код не по формату (Model id из одной цифры) — плашка, формы нет', failShown() && window.SUBMITS.length === s4);

    note('== U8 дедлайн 15 с (ждём ~32 с) ==');
    await freshLk(); setPay(120000);
    window.TILDA_MODE = 'hang';
    var o5 = window.OPEN_CALLS.length, n5 = window.NAVIGATED.length, t0 = Date.now();
    click('#uc-gal-submit'); await wait(14500);
    t('форма молчит 14,5 с, бот ответил — ждём форму, перехода нет',
      !failShown() && btn().disabled && window.OPEN_CALLS.length === o5 + 1 && window.NAVIGATED.length === n5);
    await wait(800);
    t('форма молчит 15 с, адрес есть — переход без формы', !failShown() && window.NAVIGATED.length === n5 + 1, (Date.now() - t0) + ' мс');
    window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
    setPay(125000);
    var s6 = window.SUBMITS.length;
    click('#uc-gal-submit'); await wait(50);
    t('новая попытка, пока Tilda ещё отправляет — плашка без новой отправки', failShown() && window.SUBMITS.length === s6);
    form.dispatchEvent(new CustomEvent('tildaform:aftersuccess', { bubbles: true }));
    q('.uc-tildaform .t-submit').classList.remove('t-btn_sending');
    window.TILDA_MODE = 'ok';
    click('#uc-gal-submit'); await wait(250);
    t('поздний успех старой попытки не засчитан новой: её форма уходит', window.SUBMITS.length === s6 + 1 && lastSubmit().form_type === 'В кредит');
    window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));

    await freshLk(); setPay(130000);
    window.FETCH_MODE = 'hang';
    click('#uc-gal-submit'); await wait(15400);
    t('бот молчит 15 с от нажатия — запрос прерван, плашка', failShown() && !btn().disabled);

    var summary = 'итого: ' + pass + ' ok, ' + fail + ' fail';
    out.push(summary);
    window.T(summary, fail === 0);
    window.SUMMARY = { pass: pass, fail: fail, out: out };
    return out.join('\n');
};
