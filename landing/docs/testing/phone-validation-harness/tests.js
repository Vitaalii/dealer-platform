/* Сценарии U1-U3 из плана
   docs/plans/2026-09-14-001-feat-phone-validation-submit-indicator-plan.md

   Запуск:  await runAll()         — всё, кроме сторожа (быстро)
            await runAll(true)     — со сторожем (+21 с)

   Поле должно быть в реальном фокусе, иначе проверки каретки
   пропускаются: ucPhoneApply восстанавливает каретку только когда
   document.activeElement === input.
*/
window.runAll = async function (withWatchdog) {
    var out = [], pass = 0, fail = 0, skip = 0;
    /* Результат каждой проверки сразу уходит в #log и в window.LAST.
       Прогон во вкладке без фокуса растягивается на минуты, и без
       этого невозможно отличить «медленно идёт» от «повисло». */
    function t(name, ok, extra) {
        if (ok) pass++; else fail++;
        out.push((ok ? 'ok   ' : 'FAIL ') + name + (extra ? '  | ' + extra : ''));
        window.LAST = name;
        if (window.T) window.T(name, ok, extra);
    }
    function note(s) { out.push(s); window.LAST = s; }
    function skipped(name, why) { skip++; out.push('skip ' + name + '  | ' + why); }
    var wait = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

    var btn = function () { return document.getElementById('uc-gal-submit'); };
    var err = function () { return document.getElementById('uc-calc-error'); };
    var sending  = function () { return btn().classList.contains('uc-quiz-btn-next--sending'); };
    var errShown = function () { return getComputedStyle(err()).display !== 'none'; };
    var el = function () { return document.getElementById('uc-calc-phone'); };

    function setPhone(v) {
        var e = el(); e.value = ''; e.focus(); e.setSelectionRange(0, 0); typeIn(e, v);
    }
    function typeSuffix(v) { var e = el(); e.focus(); typeIn(e, v); }
    function fill(name, phone) {
        document.getElementById('uc-calc-name').value = name;
        setPhone(phone);
    }
    async function reset() {
        window.RELOADED = false; window.WIDGET_OPENED = null;
        /* Ветка «В чате»+web выставляет флаг и никогда не сбрасывает
           его обратно — на живой странице это известный долг, между
           тестами он обязан быть чистым. */
        window.isMaxOverlayActive = false;
        /* Режим 'hang' оставляет t-btn_sending на заглушке навсегда.
           На живой странице так не бывает: Tilda снимает класс в
           каждой ветке — успех, ошибка, непройденная валидация, —
           поэтому чистим его руками, иначе следующий тест увидит
           чужой «запрос в полёте». */
        var st = document.querySelector('.uc-tildaform .t-submit');
        if (st) st.classList.remove('t-btn_sending');
        enterStep(1); enterStep(2); setPhoneTab();
        setConsent(true);   /* галочка — общее состояние между тестами */
        await wait(10);
    }

    var focused = (document.activeElement === el());

    /* ── U1: маска ───────────────────────────────────────────── */
    note('== U1 маска ==');
    await reset();
    [['9123456789', '+7 (912) 345-67-89', 'ввод 10 цифр'],
     ['9й1q2',      '+7 (912',            'цифры сквозь мусор'],
     ['йq-(  *',    '',                   'буквы и спецсимволы отброшены'],
     ['912345678901234', '+7 (912) 345-67-89', 'лишние цифры игнорируются']
    ].forEach(function (c) { setPhone(c[0]); t(c[2], el().value === c[1], el().value); });

    [['8 (912) 345-67-89', 'вставка 8 (912)…'],
     ['+79123456789',      'вставка +7912…'],
     ['9123456789',        'вставка без кода'],
     ['+7 912 345 67 89 доб. 12', 'вставка с добавочным']
    ].forEach(function (c) {
        pasteIn(el(), c[0]);
        t(c[1], el().value === '+7 (912) 345-67-89', el().value);
    });

    setPhone('9123456789');
    var guard = 0;
    while (el().value !== '' && guard++ < 40) backspace(el());
    t('backspace очищает поле', el().value === '' && guard <= 20, 'нажатий=' + guard);

    setPhone('912345');
    el().setSelectionRange(9, 9);
    backspace(el());
    t('backspace у разделителя съедает цифру', el().value === '+7 (913) 45', el().value);

    if (focused) {
        setPhone('9123456789');
        t('каретка в конце после набора', el().selectionStart === 18, 'pos=' + el().selectionStart);
        el().setSelectionRange(5, 5); typeIn(el(), '0');
        t('вставка в середину: значение', el().value === '+7 (901) 234-56-78', el().value);
        t('вставка в середину: каретка', el().selectionStart === 6, 'pos=' + el().selectionStart);
        setPhone('912');
        t('каретка перед ") "', el().selectionStart === 7, 'pos=' + el().selectionStart);
    } else {
        skipped('проверки каретки', 'поле не в фокусе — кликните в него и перезапустите');
    }

    /* ── U2: валидация ───────────────────────────────────────── */
    note('== U2 валидация ==');
    /* 4-й элемент: true — вставить целиком, иначе набрать посимвольно.
       Для 812 важна именно вставка: при посимвольном наборе ведущая
       8 съедается как междугородний префикс (см. ucPhoneParse), и
       остаётся 9 цифр — это ожидаемая неоднозначность, а не дефект. */
    var V = [
        ['9123456789', null,                              'валидный мобильный'],
        ['9123334567', null,                              '3 одинаковые подряд — норма'],
        ['9000001234', null,                              '5 нулей подряд — порог V4 не достигнут'],
        ['912345',     'Введите номер полностью',         'V1 неполный'],
        ['4951234567', 'Укажите мобильный номер',         'V2 стационарный 495'],
        ['8125553535', 'Укажите мобильный номер',         'V2 стационарный 812 (вставка)', true],
        ['88125553535','Укажите мобильный номер',         'V2 стационарный 812 (набор с 8)'],
        ['8125553535', 'Введите номер полностью',         'V2 812 набором без префикса → 9 цифр'],
        ['9999999999', 'Проверьте правильность номера',   'V3 все одинаковые'],
        ['9001111111', 'Проверьте правильность номера',   'V4 серия единиц'],
        ['9990000000', 'Проверьте правильность номера',   'V4 серия нулей'],
        ['9876543210', 'Проверьте правильность номера',   'V5 убывающая']
    ];
    for (var i = 0; i < V.length; i++) {
        await reset();
        document.getElementById('uc-calc-name').value = 'Иван';
        if (V[i][3]) {
            pasteIn(el(), V[i][0]);
        } else {
            setPhone(V[i][0]);
        }
        btn().click();
        await wait(20);
        if (V[i][1] === null) {
            t(V[i][2] + ' → проходит', sending() || window.RELOADED, 'sending=' + sending());
        } else {
            t(V[i][2] + ' → блокирует',
              !sending() && errShown() && err().textContent.indexOf(V[i][1]) === 0,
              err().textContent);
        }
    }
    await reset();
    fill('', '9123456789'); btn().click(); await wait(50);
    t('пустое имя блокирует', !sending() && /заполните имя/.test(err().textContent), err().textContent);
    /* Ошибку снимает ввод именно в подсвеченное поле: существующий
       обработчик прячет #uc-calc-error, только когда ни на одном поле
       не осталось .uc-calc-input--error. Здесь подсвечено имя. */
    document.getElementById('uc-calc-name').value = 'И';
    $('#uc-calc-name').trigger('input');
    t('ввод в подсвеченное поле снимает ошибку', !errShown());

    await reset();
    fill('Иван', '912345'); btn().click(); await wait(50);
    t('неполный телефон подсвечен', errShown());
    typeSuffix('6');
    t('ввод в телефон снимает ошибку', !errShown());

    /* ── U3: состояние отправки ──────────────────────────────── */
    note('== U3 состояние отправки ==');
    await reset(); window.TILDA_MODE = 'ok'; fill('Иван', '9123456789');
    btn().click();
    t('включилось', sending());
    t('надпись ОТПРАВКА…', btn().textContent === 'ОТПРАВКА…', JSON.stringify(btn().textContent));
    t('кнопка заблокирована', btn().disabled);
    await wait(200);
    t('успех: состояние НЕ снято (KTD-7)', sending());
    t('успех: заявка ушла', window.RELOADED);

    await reset(); window.TILDA_MODE = 'hang'; fill('Иван', '9123456789');
    var fired = 0, stub = document.querySelector('.uc-tildaform .t-submit');
    var spy = function () { fired++; };
    stub.addEventListener('click', spy);
    for (var k = 0; k < 5; k++) btn().click();
    await wait(40);
    stub.removeEventListener('click', spy);
    t('пять кликов → один сабмит', fired === 1, 'сабмитов=' + fired);

    /* Отказ валидации на стороне Tilda при ОТМЕЧЕННОМ согласии.
       После проверки согласия в U6 штатным путём это недостижимо —
       остаётся как модель будущего второго js-tilda-rule-поля.
       Сообщение тут общее и это правильно: причина нам неизвестна. */
    await reset(); window.TILDA_MODE = 'blocked'; fill('Иван', '9123456789');
    btn().click();
    await wait(250);   /* 40 мс мало: во вкладке без фокуса Chrome душит таймеры */
    t('blocked: состояние снято', !sending() && !btn().disabled);
    t('blocked: надпись восстановлена', btn().textContent === 'ОТПРАВИТЬ', JSON.stringify(btn().textContent));
    t('blocked: показан общий текст сбоя',
      errShown() && /Не удалось отправить/.test(err().textContent), err().textContent);

    await reset(); window.TILDA_MODE = 'error'; fill('Иван', '9123456789');
    btn().click(); await wait(200);
    t('aftererror: снято', !sending() && !btn().disabled);
    t('aftererror: ошибка показана', errShown(), err().textContent);

    await reset(); window.TILDA_MODE = 'hang'; fill('Иван', '9123456789');
    btn().click(); await wait(30);
    enterStep(1);
    t('уход со шага снял состояние', !sending() && !btn().disabled);
    enterStep(2); setPhoneTab(); await wait(10);
    t('возврат: кнопка в норме', !btn().disabled && btn().textContent === 'ОТПРАВИТЬ',
      JSON.stringify(btn().textContent));

    await reset(); window.TILDA_MODE = 'hang'; fill('Иван', '9123456789');
    btn().click(); await wait(30);
    $('[data-calc-tab="chat"]').trigger('click');
    t('переключение вкладки не затёрло надпись', btn().textContent === 'ОТПРАВКА…',
      JSON.stringify(btn().textContent));

    /* ── R8: «В чате» не затронута ───────────────────────────── */
    note('== R8 «В чате» ==');
    await reset(); window.TILDA_MODE = 'ok'; window.TILDA_DELAY = 400;
    $('[data-calc-tab="chat"]').trigger('click');
    btn().click();
    t('«В чате»: состояние не включается', !sending() && !btn().disabled);
    t('«В чате»: надпись прежняя', btn().textContent === 'ДАЛЕЕ →', JSON.stringify(btn().textContent));
    await wait(250);
    t('«В чате»: виджет открылся ДО ответа Tilda',
      !!window.WIDGET_OPENED && !window.RELOADED,
      'widget=' + window.WIDGET_OPENED + ' reloaded=' + window.RELOADED);
    await wait(300);
    t('«В чате»: отправка прошла', window.RELOADED);
    window.TILDA_DELAY = 60;

    /* ── Согласие: единый запрет на обоих путях ──────────────────
       План docs/plans/2026-09-18-001-fix-consent-gate-both-paths-plan.md */
    note('== согласие ==');

    var agrErr = function () {
        return !!document.querySelector('#uc-calc-agreements .uc-calc-agr-item--error');
    };
    /* Шпион на настоящем сабмите Tilda: главная проверка — что до
       него дело вообще не доходит. */
    var stubBtn = document.querySelector('.uc-tildaform .t-submit');
    var submits = 0, countSubmit = function () { submits++; };
    var watchSubmits = function () { submits = 0; stubBtn.addEventListener('click', countSubmit); };
    var stopWatching = function () { stubBtn.removeEventListener('click', countSubmit); };

    /* «По телефону», галочка снята */
    await reset(); window.TILDA_MODE = 'ok'; fill('Иван', '9123456789');
    setConsent(false); watchSubmits();
    btn().click(); await wait(250);
    stopWatching();
    t('телефон: названа причина',
      errShown() && /согласие/.test(err().textContent), err().textContent);
    t('телефон: пункт согласия подсвечен', agrErr());
    t('телефон: до сабмита Tilda не дошло', submits === 0, 'сабмитов=' + submits);
    t('телефон: заявка не ушла', !window.RELOADED);
    t('телефон: кнопка не в отправке', !sending() && !btn().disabled);

    /* «В чате» + web — тот самый симптом: виджет открывался всегда */
    await reset(); window.TILDA_MODE = 'ok';
    setChatTab(); setConsent(false); watchSubmits();
    btn().click(); await wait(400);
    stopWatching();
    t('чат: виджет НЕ открылся', window.WIDGET_OPENED === null || !window.WIDGET_OPENED,
      'widget=' + window.WIDGET_OPENED);
    t('чат: названа та же причина',
      errShown() && /согласие/.test(err().textContent), err().textContent);
    t('чат: до сабмита Tilda не дошло', submits === 0, 'сабмитов=' + submits);
    t('чат: isMaxOverlayActive не выставлен', !window.isMaxOverlayActive);

    /* Снятие ошибки кликом по пункту согласия */
    t('клик по согласию снял подсветку',
      (function () { setConsent(true); return !agrErr() && !errShown(); })());

    /* Повторный клик после исправления — путь открывается */
    window.TILDA_DELAY = 400;
    btn().click(); await wait(250);
    t('чат: после отметки виджет открылся', !!window.WIDGET_OPENED,
      'widget=' + window.WIDGET_OPENED);
    await wait(400);
    t('чат: после отметки заявка ушла', window.RELOADED);
    window.TILDA_DELAY = 60;

    /* Сообщение об имени важнее: поля стоят выше галочки */
    await reset(); setConsent(false);
    document.getElementById('uc-calc-name').value = '';
    setPhone('9123456789');
    btn().click(); await wait(50);
    t('пустое имя важнее согласия',
      /заполните имя/.test(err().textContent), err().textContent);
    t('при ошибке имени согласие не подсвечено', !agrErr());

    /* Ошибка имени и согласия вместе: отметка галочки не должна
       гасить сообщение, которое всё ещё относится к имени. Имя тут
       подсвечено настоящей проверкой из клика выше, а не руками. */
    setConsent(true);
    t('подсвеченное имя удерживает сообщение', errShown(), err().textContent);

    /* Формы нет вовсе — отказ, а не молчаливая отправка.

       reset() обязателен перед каждым случаем: после удачной
       отправки _submitting сознательно НЕ снимается (KTD-7), и
       следующий клик вышел бы по предохранителю в первой строке
       обработчика — проверка «прошла» бы, ничего не проверив. Уход
       со шага внутри reset() состояние снимает. */
    await reset(); fill('Иван', '9123456789');
    var $cbGroup = $('.uc-tildaform .t-input-group_cb').detach();
    $('#uc-calc-agreements').empty();
    watchSubmits();
    btn().click();
    stopWatching();
    t('нет формы: отправки нет', submits === 0 && !window.RELOADED, 'сабмитов=' + submits);
    t('нет формы: сказано про загрузку',
      errShown() && /загружается/.test(err().textContent), err().textContent);

    /* Форма вернулась — следующий клик перерисовывает и пропускает.
       renderCalcAgreements живёт внутри IIFE блока и снаружи
       недоступна, поэтому перерисовку запускаем штатным путём:
       её зовёт сама ветка 'empty'. */
    $('.uc-tildaform form').prepend($cbGroup);
    await reset(); fill('Иван', '9123456789');
    btn().click(); await wait(250);
    t('форма вернулась: перерисовался и пропустил', window.RELOADED,
      err().textContent + ' | пунктов=' + agrItems().length);

    /* ── сторож ──────────────────────────────────────────────── */
    if (withWatchdog) {
        note('== сторож (20 с) ==');
        await reset(); window.TILDA_MODE = 'hang'; fill('Иван', '9123456789');
        btn().click();
        t('сторож: висим в отправке', sending());
        await wait(21000);
        t('сторож: состояние снято', !sending() && !btn().disabled);
        t('сторож: ошибка показана', errShown(), err().textContent);
    } else {
        skipped('сторож 20 с', 'запустите runAll(true)');
    }

    window.TILDA_MODE = 'ok';
    await reset();
    out.push('');
    out.push('итого: ' + pass + ' ok, ' + fail + ' fail, ' + skip + ' skip');
    return out.join('\n');
};
