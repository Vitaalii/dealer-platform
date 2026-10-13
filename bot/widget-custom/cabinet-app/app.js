/**
 * Cabinet App (U8) — клиентский личный кабинет.
 *
 * Самодостаточный IIFE без сборки и внешних зависимостей (по образцу widget.js):
 *   - конфиг через window.UNIFIEDBOT_URL, монтирование в #cabinet-root;
 *   - стили инжектируются строкой, meta referrer/robots — в head;
 *   - токен ссылки читается из фрагмента URL (#t=...) ДО инициализации,
 *     копируется в sessionStorage, фрагмент затирается history.replaceState;
 *   - одностраничная смена экранов: скелетон загрузки → объединённый экран
 *     авторизации (телефон + согласие + равноправный выбор канала, R28, макет m1) →
 *     ввод кода (4 ячейки, макет m2) → кабинет (анкета);
 *   - вёрстка/стили auth-контура — из docs/design/lk-source.html (U6; в v3
 *     авторизация не менялась — кадры 1a/2a); анкета с 2026-09-26 — по
 *     docs/design/lk-source-v3.html (план 2026-09-26-001, кадры 7a/4b/4c,
 *     моб. 3–7); классы макета скоупятся под #cabinet-root (виджет живёт
 *     на чужой странице);
 *   - 401 в кабинете → инлайн re-OTP с сохранением локального состояния формы;
 *   - автосохранение анкеты (PUT /form, дебаунс 1.5 с) с видимым состоянием;
 *   - фоновый пинг GET /me раз в 5 минут (сигнал жизни, продление сессии).
 */
(function () {
  'use strict';

  // ── 0. ТОКЕН ИЗ ФРАГМЕНТА URL — до инициализации чего-либо ─────────────────
  var TOKEN_KEY = 'cabinet_token';
  var BEARER_KEY = 'cabinet_bearer';
  // Веб-вход (план 2026-09-30-001, U8): признак ?src=web в адресе кабинета —
  // сессия открыта с лендинга. Живёт в sessionStorage вкладки; адрес возврата
  // для 8f — в localStorage (переживает закрытие вкладки, R50).
  var WEB_KEY = 'cabinet_web';
  var RETURN_KEY = 'cabinet_web_return';

  (function captureToken() {
    var webUrl = /(?:^|[?&])src=web(?:&|$)/.test((window.location.search || '').replace(/^\?/, ''));
    if (webUrl) {
      try { sessionStorage.setItem(WEB_KEY, '1'); } catch (e) { /* private mode */ }
    }
    var hash = window.location.hash || '';
    var m = hash.match(/[#&]t=([^&]+)/);
    if (m && m[1]) {
      try {
        var fresh = decodeURIComponent(m[1]);
        // Другая ссылка в той же вкладке — прежний вход относится к чужой
        // сессии: без сброса кабинет показал бы прошлую анкету
        if (sessionStorage.getItem(TOKEN_KEY) !== fresh) {
          sessionStorage.removeItem(BEARER_KEY);
          if (!webUrl) sessionStorage.removeItem(WEB_KEY); // ссылка из чата — не веб-вход
        }
        sessionStorage.setItem(TOKEN_KEY, fresh);
      } catch (e) { /* private mode */ }
      try {
        history.replaceState(null, document.title,
          window.location.pathname + window.location.search);
      } catch (e) { /* older browsers */ }
    }
  })();

  // ── 1. META: referrer / robots ──────────────────────────────────────────────
  function injectMeta(name, content) {
    var meta = document.querySelector('meta[name="' + name + '"]');
    if (!meta) {
      meta = document.createElement('meta');
      meta.setAttribute('name', name);
      document.head.appendChild(meta);
    }
    meta.setAttribute('content', content);
  }
  injectMeta('referrer', 'no-referrer');
  injectMeta('robots', 'noindex, nofollow');

  // ── 2. КОНФИГ И КОНСТАНТЫ ───────────────────────────────────────────────────
  var API_BASE = (window.UNIFIEDBOT_URL || '').replace(/\/+$/, '');

  var SEND_STATUS_POLL_MS = 4000;   // опрос send-status: раз в 3–5 с
  var PING_INTERVAL_MS = 300000;    // фоновый пинг /me: 5 минут
  var CODE_TTL_MS = 300000;         // TTL кода — стоп опроса через 5 минут
  var RESEND_COOLDOWN_S = 30;       // кулдаун повторной отправки (фолбэк, если бэк не прислал retry_after)
  var AUTOSAVE_DEBOUNCE_MS = 1500;  // дебаунс автосохранения анкеты
  // Веб-вход (U8/U9)
  var WEB_RECALL_S = 45;             // повторный звонок — не раньше 45 с (план 2026-10-11-001; фолбэк без retry_after)
  var VALUATION_POLL_MS = 4000;      // опрос /me, пока оценка идёт: раз в 3–5 с
  var VALUATION_POLL_CAP_MS = 120000; // потолок опроса ~2 минуты → 8e на клиенте
  var VALUATION_POLL_MAX_FAILS = 3;  // три неудачных опроса подряд → 8e на клиенте

  var CONSENT_VERSION = 'v1';
  // Формулировка макета (docs/design/lk-source.html, группы #auth/#mobile m1,
  // check__text; в v3 авторизация не менялась — кадры 1a/2a); юридический
  // текст будет поставлен отдельно (внешний блокер).
  // Куски для сборки подписи с кликабельными фразами (п.3, 2026-08-31); полный
  // текст = PREFIX + POLICY_PHRASE + MIDDLE + AGREEMENT_PHRASE:
  // «Я ознакомлен(а) с Политикой конфиденциальности и даю Согласие на
  // предоставление и обработку персональных данных». Кликабельная зона
  // согласия — вся фраза целиком (решение В4).
  var CONSENT_PREFIX = 'Я ознакомлен(а) с ';
  var CONSENT_POLICY_PHRASE = 'Политикой конфиденциальности';
  var CONSENT_MIDDLE = ' и даю ';
  var CONSENT_AGREEMENT_PHRASE = 'Согласие на предоставление и обработку персональных данных';

  // ── 3. СТИЛИ (инжект строкой; авторизация — docs/design/lk-source.html,
  // анкета — docs/design/lk-source-v3.html; mobile-first).
  // Классы макета переносятся как есть, но скоупятся под #cabinet-root:
  // виджет живёт на чужой (Tilda) странице и не должен задевать её стили.
  // Переменные :root макета (--field, --accent и т.д.) — на #cabinet-root.
  var css = [
    '#cabinet-root{--accent:#1a1a1a;--text:#111111;--muted:#888888;--chip:#f5f5f5;--line:#efefef;',
    '--border:#e6e6e6;--field:#fafafa;--error:#b3261e;--error-soft:#f0c9c5;--warn:#a97c1f;',
    '--ok:#2e6b4f;--ok-bg:#eef5f0;--blocked:#d5decf;--r-photo:12px;--r-thumb:6px;--r-chip:8px;--r-btn:6px;',
    '--container:1000px;',
    'box-sizing:border-box;font-family:Arial,Helvetica,sans-serif;color:var(--text);line-height:1.5;',
    'padding:16px 0;min-height:60vh;-webkit-text-size-adjust:100%}',
    '#cabinet-root *,#cabinet-root *::before,#cabinet-root *::after{box-sizing:border-box;font-family:inherit}',
    // hidden не должен проигрывать классам с display (например .link--tap)
    '#cabinet-root [hidden]{display:none!important}',

    '.cab-screen{display:flex;flex-direction:column;gap:16px;padding:8px 0}',

    // Ширина живёт на уровне экрана, не корня (макет: #auth — .card 420px по
    // центру, #credit — .container 1000px по центру; сам корень полноширинный).
    // Мобильный гаттер 16px тоже несут .card/.container (media ниже), у корня
    // горизонтального паддинга нет — иначе на телефоне он удваивался бы.
    '#cabinet-root .card{width:420px;margin:0 auto}',
    '#cabinet-root .container{width:100%;max-width:var(--container);margin:0 auto}',

    /* Типографика макета */
    '#cabinet-root .h1{font-size:26px;font-weight:700;letter-spacing:.02em;margin:0}',
    '#cabinet-root .sub{font-size:15px;color:var(--muted);margin:0}',
    '#cabinet-root .text{font-size:14px;line-height:1.55}',
    '#cabinet-root .strong{font-weight:500}',
    '#cabinet-root .lead{font-size:14px;color:var(--muted);line-height:1.55;margin:0;text-wrap:pretty}',
    '#cabinet-root .note{font-size:12px;color:var(--muted);line-height:1.55}',
    '#cabinet-root .note--md{font-size:13px}',
    '#cabinet-root .note--error{color:var(--error)}',
    '#cabinet-root .note--warn{color:var(--warn)}',
    '#cabinet-root .group-title{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}',
    '#cabinet-root .divider{height:.5px;background:var(--line);border:0;margin:28px 0;width:100%}',
    '#cabinet-root .divider--tight{margin:16px 0}',
    '#cabinet-root .divider--md{margin:22px 0}',
    // Поток авторизации (все три экрана — телефон/канал/код, обёртка .card;
    // решение владельца В1, 2026-08-31): разделитель прижат к заголовку.
    // Анкета (.container) остаётся на базовых 22px.
    '#cabinet-root .card .divider--md{margin:2px 0}',

    /* Поля */
    '#cabinet-root .label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}',
    '#cabinet-root .input{width:100%;padding:13px 14px;border:1px solid var(--border);',
    'border-radius:var(--r-chip);font-size:15px;outline:none;background:#fff;-webkit-appearance:none}',
    '#cabinet-root .input--lg{height:52px;padding:0 14px;font-size:17px}',
    '#cabinet-root .input--error{border-color:var(--error)}',
    '#cabinet-root .input::placeholder{color:#bbb}',

    /* Чекбокс согласия: нативный input — клавиатура и скринридеры из коробки */
    '#cabinet-root .check{display:flex;align-items:flex-start;gap:12px;padding:12px 0;cursor:pointer}',
    '#cabinet-root .check input{width:24px;height:24px;flex-shrink:0;margin:0;accent-color:var(--accent)}',
    '#cabinet-root .check__text{font-size:13px;line-height:1.55;color:var(--muted)}',
    '#cabinet-root .check--error .check__text{color:var(--error)}',

    /* Плитки выбора (канал получения кода, use_known, анкета) — историч. макет
       1a/1b/1c (десктоп: сетка 2/4 колонки) и m1/#mobile (телефон: колонка);
       в v3 плитки те же — кадры 4b/6 — единая разметка на оба размера,
       переключение в media (max-width:600px) ниже. */
    '#cabinet-root .tiles{display:grid;gap:8px}',
    '#cabinet-root .tiles--2{grid-template-columns:repeat(2,1fr)}',
    '#cabinet-root .tiles--4{grid-template-columns:repeat(4,1fr)}',
    '#cabinet-root .tile{display:block;width:100%;text-align:left;border:1px solid var(--border);',
    'border-radius:var(--r-chip);padding:13px 14px;font-size:14px;cursor:pointer;background:#fff;text-wrap:pretty}',
    '#cabinet-root .tile--tap{min-height:52px;padding:15px 16px;font-size:15px}',
    '#cabinet-root .tile--rich{padding:14px 16px}',
    '#cabinet-root .tile--on{border-color:var(--accent)}',
    '#cabinet-root .tile:disabled{opacity:.5;cursor:not-allowed}',
    '#cabinet-root .tile__title{display:block;font-size:15px;font-weight:500}',
    '#cabinet-root .tile__hint{display:block;font-size:13px;color:var(--muted);margin-top:3px}',

    /* Кнопки и ссылки */
    // ,#cab-popup ... — кнопки/ссылка попапов ошибок, монтируемых в body (U4)
    '#cabinet-root .btn,#cab-popup .btn{display:inline-flex;align-items:center;justify-content:center;padding:14px 40px;',
    'border:0;border-radius:var(--r-btn);background:var(--accent);color:#fff;font-size:13px;font-weight:500;',
    'letter-spacing:.08em;text-transform:uppercase;cursor:pointer}',
    '#cabinet-root .btn:hover,#cab-popup .btn:hover{background:#333}',
    '#cabinet-root .btn--tap{width:100%;height:52px;padding:0}',
    '#cabinet-root .btn--block{width:100%;padding:14px 0}',
    '#cabinet-root .btn--sm,#cab-popup .btn--sm{padding:13px 32px}',
    '#cabinet-root .btn--disabled,#cabinet-root .btn:disabled{background:var(--chip);color:#bbb;cursor:not-allowed}',
    '#cabinet-root .btn--disabled:hover,#cabinet-root .btn:disabled:hover{background:var(--chip)}',
    '#cabinet-root .link,#cab-popup .link{font-size:13px;color:var(--muted);text-decoration:underline;',
    'text-underline-offset:3px;background:none;border:0;padding:0;cursor:pointer;text-align:left}',
    '#cabinet-root .link--tap{font-size:14px;padding:14px 0;display:inline-block;min-height:44px}',
    '#cabinet-root .link--disabled{pointer-events:none;opacity:.5}',

    /* Ввод кода: 4 отдельные ячейки (макет m2) */
    '#cabinet-root .code{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}',
    '#cabinet-root .code__cell{width:100%;height:72px;text-align:center;font-size:26px;',
    'border:1px solid var(--border);border-radius:var(--r-chip);outline:none;background:#fff;',
    'font-variant-numeric:tabular-nums;-webkit-appearance:none}',
    '#cabinet-root .code__cell:focus,#cabinet-root .code__cell--active{border-color:var(--text)}',
    '#cabinet-root .code__cell--error{border-color:var(--error);color:var(--error)}',
    '#cabinet-root .code__cell--locked{background:var(--chip);border-color:var(--line)}',
    '#cabinet-root .code__cell:disabled{opacity:.6}',
    // Внутренний контейнер экрана авторизации/кода: своя вертикальная раскладка
    // поверх .cab-screen (в макете это последовательность элементов с mt-*,
    // здесь — один flex-контейнер с равномерным gap).
    '#cabinet-root .auth-body{display:flex;flex-direction:column;gap:14px}',

    /* Скелетон загрузки (макет 2a-8) */
    '#cabinet-root .skeleton{display:flex;flex-direction:column;gap:14px}',
    '#cabinet-root .sk{background:var(--chip);border-radius:var(--r-chip)}',
    '#cabinet-root .sk--text{height:14px;border-radius:4px}',
    '#cabinet-root .sk--title{height:26px;width:240px;max-width:70%}',
    '#cabinet-root .sk--field{height:46px}',
    '#cabinet-root .sk--tile{height:34px}',
    '#cabinet-root .sk--btn{height:46px;border-radius:var(--r-btn)}',
    '#cabinet-root .sk--w160{width:160px}',
    '#cabinet-root .sk--w260{width:260px}',

    /* Попап — компонент lk-source.html:1540–1550 (в v3 тот же, :315-343).
       U4: попапы ошибок (#cab-popup — конфликт 409 и сетевая ошибка сабмита)
       монтируются в document.body (переживают setScreen) и НЕ находятся под
       #cabinet-root — каждое правило достаёт их параллельным селектором по id,
       без дублирования деклараций. Попап авто (#cab-car-popup) живёт внутри
       root и параллельного селектора не требует. */
    // Scope-переменные и шрифт для узла вне root: значения — КОПИЯ корневых
    // (см. объявление на #cabinet-root выше); менять синхронно в обоих местах.
    // На html/body страницы Tilda переменные не выносим — риск конфликта имён.
    '#cab-popup{--accent:#1a1a1a;--text:#111111;--muted:#888888;--chip:#f5f5f5;',
    '--border:#e6e6e6;--r-chip:8px;--r-btn:6px;',
    'font-family:Arial,Helvetica,sans-serif;color:var(--text);line-height:1.5;box-sizing:border-box}',
    '#cab-popup *,#cab-popup *::before,#cab-popup *::after{box-sizing:border-box;font-family:inherit}',
    '#cabinet-root .popup,#cab-popup.popup{display:none;position:fixed;inset:0;z-index:100}',
    '#cabinet-root .popup--visible,#cab-popup.popup--visible{display:block}',
    '#cabinet-root .popup__overlay,#cab-popup .popup__overlay{position:absolute;inset:0;background:rgba(17,17,17,.18)}',
    '#cabinet-root .popup__dialog,#cab-popup .popup__dialog{position:relative;width:400px;max-width:calc(100% - 32px);margin:0 auto;',
    'top:50%;transform:translateY(-50%);background:#fff;border-radius:var(--r-chip);padding:28px}',
    '#cabinet-root .popup__title,#cab-popup .popup__title{font-size:20px;font-weight:700;letter-spacing:.01em}',
    '#cabinet-root .popup__text,#cab-popup .popup__text{font-size:14px;color:var(--muted);line-height:1.6;margin-top:10px;text-wrap:pretty}',
    '#cabinet-root .popup__actions,#cab-popup .popup__actions{display:flex;align-items:center;gap:20px;margin-top:22px}',

    /* Текстовый поп-ап согласия (п.3, 2026-08-31): mount в document.body —
       переживает setScreen; стили на id (узел вне #cabinet-root, урок U4).
       Диалог скроллится: тексты Policy/Agreement дилера — до ~18 тыс. знаков;
       текст вставляется ТОЛЬКО textContent (В2), переносы — pre-wrap. */
    '#cab-text-popup{--accent:#1a1a1a;--text:#111111;--muted:#888888;--chip:#f5f5f5;',
    '--border:#e6e6e6;--r-chip:8px;--r-btn:6px;',
    'font-family:Arial,Helvetica,sans-serif;color:var(--text);line-height:1.5;box-sizing:border-box}',
    '#cab-text-popup *,#cab-text-popup *::before,#cab-text-popup *::after{box-sizing:border-box;font-family:inherit}',
    '#cab-text-popup.popup{display:none;position:fixed;inset:0;z-index:100}',
    '#cab-text-popup.popup--visible{display:block}',
    '#cab-text-popup .popup__overlay{position:absolute;inset:0;background:rgba(17,17,17,.18)}',
    '#cab-text-popup .popup__dialog{position:relative;width:720px;max-width:calc(100% - 32px);margin:0 auto;',
    'top:50%;transform:translateY(-50%);background:#fff;border-radius:var(--r-chip);padding:24px 28px;',
    'display:flex;flex-direction:column;max-height:calc(100vh - 64px)}',
    '#cab-text-popup .popup__head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}',
    '#cab-text-popup .popup__title{font-size:20px;font-weight:700;letter-spacing:.01em}',
    '#cab-text-popup .popup__close{flex-shrink:0;width:44px;height:44px;margin:-8px -12px 0 0;border:none;',
    'background:none;font-size:18px;color:var(--muted);cursor:pointer;border-radius:8px}',
    '#cab-text-popup .popup__close:hover{background:var(--chip);color:#111}',
    '#cab-text-popup .popup__body{overflow-y:auto;margin-top:12px;font-size:14px;line-height:1.6;',
    'white-space:pre-wrap;overflow-wrap:anywhere;-webkit-overflow-scrolling:touch}',
    // Мобильное правило диалога — в ХВОСТЕ общего медиа-блока 600px ниже:
    // страж-тест читает первые 2500 символов от ПЕРВОГО @media в файле,
    // отдельный блок здесь сдвинул бы ему точку отсчёта.

    /* Постоянно видимое напоминание о выбранном способе на экране кода */
    '.cab-reminder{border-radius:var(--r-chip);padding:14px 16px;font-size:15px;font-weight:500}',
    '.cab-reminder-voice{background:#fff7e0;border:1px solid #e6c96a;color:#6b5310}',
    '.cab-reminder-sms{background:#eef3fb;border:1px solid #b9cdec;color:#2b4a7a}',

    /* Кабинет: статус автосохранения — видим всегда, компактной строкой
       (историч. lk-source.html: #form-states, фрейм 16, класс .status «✓
       Черновик сохранён»; в v3 кадра нет, поведение сохранено), не
       полноразмерной плитой в потоке формы. */
    '.cab-savebar{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted)}',
    '.cab-savebar.cab-save-saving{color:var(--warn)}',
    '.cab-savebar.cab-save-saved{color:var(--ok)}',
    '.cab-savebar.cab-save-failed{color:var(--error)}',
    '.cab-savebar button{min-height:28px;padding:2px 10px;font-size:12px;border-radius:6px;',
    'border:1px solid var(--error);background:#fff;color:var(--error);cursor:pointer;margin-left:2px}',

    '.cab-form{display:flex;flex-direction:column;gap:14px}',
    '.cab-field{display:flex;flex-direction:column}',

    /* ── U8: сценарная анкета, карточка авто, «Ваш автомобиль»
       (макет v3: docs/design/lk-source-v3.html, кадры 7a/4b/4c;
       исторически — lk-source.html, перенос 2026-09-26) ── */
    '#cabinet-root .h2{font-size:18px;font-weight:500}',
    '#cabinet-root .cab-group{display:flex;flex-direction:column;gap:12px}',

    '#cabinet-root .textarea{width:100%;padding:13px 14px;border:1px solid var(--border);',
    'border-radius:var(--r-chip);font-size:14px;line-height:1.5;outline:none;resize:vertical;',
    'background:#fff;-webkit-appearance:none}',
    '#cabinet-root .textarea::placeholder{color:#bbb}',
    '#cabinet-root .textarea--error{border-color:var(--error)}',

    /* Плитки-радио: нативный input скрыт (visually-hidden), клавиатура/скринридер из коробки */
    '#cabinet-root .tile-radio{position:absolute;opacity:0;width:1px;height:1px;overflow:hidden;',
    'clip:rect(0,0,0,0);white-space:nowrap;border:0;padding:0;margin:-1px}',
    '#cabinet-root label.tile{position:relative;display:flex;flex-direction:column}',
    '#cabinet-root .tile--error{border-color:var(--error-soft)}',
    '#cabinet-root .tile--ro{cursor:not-allowed;color:#aaa;border-color:var(--line)}',
    '#cabinet-root .tile--ro-on{cursor:not-allowed;color:var(--text);background:var(--field);',
    'border-color:#bbb}',
    '#cabinet-root .tile__desc{font-size:13px;color:var(--muted);line-height:1.5;margin-top:4px;',
    'text-wrap:pretty}',

    /* Чипы ТТХ шапки авто */
    '#cabinet-root .chips{display:flex;gap:8px;flex-wrap:wrap}',
    '#cabinet-root .chips--scroll{flex-wrap:nowrap;white-space:nowrap;overflow-x:auto;',
    'margin:0 -16px;padding:0 16px 4px}',
    '#cabinet-root .chip{background:var(--chip);border-radius:var(--r-chip);padding:8px 14px;',
    'flex:0 0 auto}',
    '#cabinet-root .chip__value{font-size:16px;font-weight:500;margin-bottom:2px}',
    '#cabinet-root .chip__label{font-size:12px;color:var(--muted)}',

    /* Фотогалерея авто (шапка). Числа десктопа — эталон галереи лендинга
       (docs/design/u6-gallery.html:16-88), по десктопу он совпадает с макетом
       (историч. lk-source.html:193-213; в v3 содержимое попапа авто не
       менялось — план 2026-09-26-001). Реализация своя, нативная — Swiper/jQuery
       в кабинет не тянем (проектное решение). Мобильные размеры — в media. */
    '#cabinet-root .photo{position:relative;aspect-ratio:3/2;overflow:hidden;background:#f5f5f5}',
    '#cabinet-root .photo--desktop{width:80%;margin:14px auto 0;border-radius:var(--r-photo)}',
    '#cabinet-root .cab-car-photo-img{width:100%;height:100%;object-fit:contain;display:block}',
    // padding:0 — стрелки теперь нативные <button> (P2-2), сбрасываем UA-паддинг
    '#cabinet-root .photo__nav{position:absolute;top:50%;transform:translateY(-50%);width:32px;',
    'height:40px;padding:0;border-radius:var(--r-chip);border:.5px solid rgba(255,255,255,.6);',
    'background:rgba(255,255,255,.15);color:#fff;display:flex;align-items:center;justify-content:center;',
    'font-size:16px;cursor:pointer;user-select:none;z-index:2}',
    '#cabinet-root .photo__nav--prev{left:12px}',
    '#cabinet-root .photo__nav--next{right:12px}',
    '#cabinet-root .thumbs{display:grid;grid-template-columns:repeat(6,1fr);gap:6px;',
    'width:80%;margin:6px auto 0}',
    // padding:0 — превью теперь нативные <button> (P2-2)
    '#cabinet-root .thumb{aspect-ratio:3/2;border-radius:var(--r-thumb);padding:0;',
    'border:.5px solid #e0e0e0;overflow:hidden;cursor:pointer;background:#f5f5f5}',
    '#cabinet-root .thumb img{width:100%;height:100%;object-fit:cover;display:block}',
    '#cabinet-root .thumb--active{border:1.5px solid var(--accent)}',

    /* Блок «Ваш автомобиль» (read-only, из found_info снапшота).
       Десктоп: flex-строка с переносом — фото слева, факты справа, обе подписи
       зоной на всю ширину внизу (order + flex-basis:100%; приёмка U3b).
       Фото 356×200 (16:9): нативные рендеры auto.ru — 1050×650 (~1.615, замер
       2026-08-27 по 8 машинам), 16:9 режет ~9% по вертикали против ~17% по
       горизонтали у 4:3. Мобильные значения возвращаются в конце media-блока. */
    // column-gap (не gap): вертикальные зазоры зоны подписей несут их паддинги
    '#cabinet-root .own{display:flex;flex-wrap:wrap;column-gap:28px;align-items:flex-start;padding:20px;',
    'background:var(--field);border-radius:var(--r-chip);overflow:hidden}',
    '#cabinet-root .own__photo{flex-shrink:0;width:356px;height:200px;border-radius:var(--r-chip);overflow:hidden;',
    'background:repeating-linear-gradient(135deg,#f2f2f2 0 8px,#ececec 8px 16px)}',
    '#cabinet-root .own__photo img{width:100%;height:100%;object-fit:cover;display:block}',
    // Зона подписей: сначала про фото, следом про источник данных. Без nowrap —
    // на узких вьюпортах строки переносятся штатно.
    '#cabinet-root .own__caption{order:3;flex-basis:100%;min-width:0;padding:16px 0 0}',
    '#cabinet-root .own__source{order:4;flex-basis:100%;min-width:0;padding:6px 0 0}',
    '#cabinet-root .own__empty{width:100%;height:100%;display:flex;flex-direction:column;',
    'align-items:center;justify-content:center;gap:10px}',
    '#cabinet-root .own__empty-icon{width:56px;height:30px;border-radius:6px 6px 3px 3px;',
    'border:1.5px solid #c4c4c4}',
    '#cabinet-root .own__empty-text{font-size:12px;color:var(--muted)}',
    '#cabinet-root .own__grid{display:grid;grid-template-columns:repeat(2,minmax(0,180px));',
    'gap:20px 40px;padding-top:4px}',
    '#cabinet-root .fact__label{font-size:12px;color:var(--muted);margin-bottom:4px}',
    '#cabinet-root .fact__value{font-size:16px;font-weight:500}',
    /* Оценка в низу карточки «Ваш автомобиль» (v3 .s52 десктоп; .s147 — media) */
    '#cabinet-root .own__valuation{order:5;flex-basis:100%;min-width:0;margin-top:16px;',
    'padding-top:16px;border-top:.5px solid var(--border);display:flex;align-items:baseline;',
    'flex-wrap:wrap;gap:6px 16px}',
    '#cabinet-root .own__valuation-sum{font-size:22px;font-weight:700;color:#111;white-space:nowrap}',
    '#cabinet-root .readout{background:var(--field);border-radius:var(--r-chip);padding:14px 16px}',

    /* Шапка раздела (макет v3 .s10/.s11/.s12/.s13): стек «Личный кабинет» +
       подзаголовок сценария слева, карточка авто справа, выравнивание по верху */
    '#cabinet-root .page-head{display:flex;align-items:flex-start;justify-content:space-between;gap:24px}',
    '#cabinet-root .cab-head-stack{display:flex;flex-direction:column;gap:6px}',
    '#cabinet-root .page-title{font-size:26px;font-weight:700;letter-spacing:.05em;color:#111}',
    '#cabinet-root .page-subtitle{font-size:15px;color:var(--muted)}',
    '#cabinet-root .page-head #cab-car-head{min-width:0}',

    /* Серая плашка-фраза (макет v3 .s35; мобильные размеры — в media-блоке) */
    '#cabinet-root .cab-phrase{background:var(--field);border-radius:var(--r-chip);',
    'padding:20px 24px;margin-bottom:24px;font-size:17px;font-weight:500;line-height:1.45;text-wrap:pretty}',

    /* Строка «Первоначальный взнос» комбо (v3 .s64/.s65/.s67-.s70) и внутренний
       узел кредит-блока (KTD1): флекс-колонка как .cab-group — билдеры больше
       не несут собственную cab-group-обёртку */
    '#cabinet-root #cab-credit-block{display:flex;flex-direction:column;gap:12px}',
    '#cabinet-root .cab-down-row{display:flex;flex-direction:column;gap:6px}',
    '#cabinet-root .cab-down-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}',
    '#cabinet-root .cab-down-readout{height:52px;box-sizing:border-box;background:var(--field);',
    'border-radius:var(--r-chip);padding:0 14px;display:flex;align-items:center;',
    'justify-content:space-between;gap:12px}',
    '#cabinet-root .cab-down-sum{font-size:17px;font-weight:500;white-space:nowrap}',
    '#cabinet-root .money__prefix{padding-left:14px;font-size:13px;color:var(--muted);white-space:nowrap}',
    '#cabinet-root .cab-down-extra{text-align:right}',

    /* Карточка выбранного авто — «карточка товара» (макет v3 .s14-.s21):
       миниатюра фото, имя, «от <цена>», шеврон. max-width и эллипсис имени —
       P2-3: длинные названия в одну строку с многоточием, без JS-обрезки. */
    '#cabinet-root .car-pick{display:flex;align-items:center;gap:14px;border:1px solid var(--border);',
    'border-radius:var(--r-chip);padding:8px 14px 8px 8px;background:#fff;cursor:pointer;text-align:left;',
    'font-family:inherit;color:inherit;max-width:400px}',
    '#cabinet-root .car-pick:hover{border-color:#bbb}',
    // Некликабельные состояния (загрузка/нет данных): без ховера и руки
    '#cabinet-root .car-pick--static{cursor:default}',
    '#cabinet-root .car-pick--static:hover{border-color:var(--border)}',
    '#cabinet-root .car-pick--static:focus{outline:none}',
    '#cabinet-root .car-pick__thumb{flex-shrink:0;width:96px;height:64px;border-radius:6px;',
    'overflow:hidden;background:var(--chip);display:block}',
    '#cabinet-root .car-pick__thumb img{width:100%;height:100%;object-fit:cover;display:block}',
    '#cabinet-root .car-pick__body{flex:1;min-width:0;display:flex;flex-direction:column;gap:4px}',
    '#cabinet-root .car-pick__name{display:block;font-size:16px;font-weight:500;color:#111;',
    'white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '#cabinet-root .car-pick__price{display:flex;align-items:baseline;gap:5px;white-space:nowrap}',
    '#cabinet-root .car-pick__price-from{font-size:13px;color:var(--muted)}',
    '#cabinet-root .car-pick__price-sum{font-size:18px;font-weight:700;color:#111}',
    '#cabinet-root .car-pick__chevron{font-size:22px;color:var(--muted);margin-left:10px}',

    /* Широкий попап «Данные об автомобиле» (макет 2.0:326-337; в v3 без
       изменений — #popup-car, lk-source-v3.html:1315-1348). Граббер скрыт
       на десктопе, шторка и лента чипов — в мобильном блоке media ниже. */
    '#cabinet-root .h3{font-size:20px;font-weight:700;letter-spacing:.01em}',
    '#cabinet-root .popup--wide .popup__dialog{width:720px;max-height:calc(100vh - 96px);overflow:auto;padding:32px}',
    '#cabinet-root .popup__head{display:flex;align-items:flex-start;justify-content:space-between;gap:24px}',
    '#cabinet-root .popup__subtitle{font-size:16px;font-weight:500;margin-top:10px}',
    '#cabinet-root .popup__close{flex-shrink:0;width:44px;height:44px;margin:-8px -8px 0 0;border:none;background:none;',
    'border-radius:var(--r-chip);display:flex;align-items:center;justify-content:center;',
    'font-size:18px;color:var(--muted);cursor:pointer}',
    '#cabinet-root .popup__close:hover{background:var(--chip);color:#111}',
    '#cabinet-root .popup__grabber{display:none;width:44px;height:4px;border-radius:2px;background:#e0e0e0;margin:0 auto 18px}',

    /* Баннер успеха + read-only анкета после отправки */
    '#cabinet-root .banner{border-radius:var(--r-chip);padding:14px 16px;font-size:14px}',
    '#cabinet-root .banner--ok{background:var(--ok-bg);color:var(--ok)}',
    '#cabinet-root .readonly{opacity:.55}',

    /* ── U9: блок «Условия кредита» на calc_model.js (историч. m4/2b макета
       2.0; в v3 — кадры 7a и моб. 4, раскладка слайдеров та же) ── */
    '#cabinet-root .label--lg{font-size:15px;color:var(--text);margin-bottom:6px}',
    '#cabinet-root .money{display:flex;align-items:center;border:1px solid var(--border);',
    'border-radius:var(--r-chip);overflow:hidden;background:#fff}',
    '#cabinet-root .money--lg{height:52px}',
    '#cabinet-root .money--warn{border-color:var(--warn)}',
    '#cabinet-root .money__input{flex:1;min-width:0;border:0;outline:none;padding:13px 14px;',
    'font-size:16px;background:transparent;-webkit-appearance:none;color:var(--text)}',
    '#cabinet-root .money--lg .money__input{height:100%;padding:0 14px;font-size:17px}',
    '#cabinet-root .money__input:disabled{opacity:1;background:var(--field)}',
    '#cabinet-root .money__input.input--error{color:var(--error)}',
    '#cabinet-root .money__suffix{padding:0 14px;font-size:13px;color:var(--muted)}',
    '#cabinet-root .slider{-webkit-appearance:none;appearance:none;width:100%;height:2px;',
    'border-radius:2px;background:transparent;margin:14px 0 4px;cursor:pointer}',
    '#cabinet-root .slider::-webkit-slider-runnable-track{height:2px;border-radius:2px;',
    'background:var(--slider-bg, var(--border))}',
    '#cabinet-root .slider::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;',
    'margin-top:-7px;border-radius:50%;background:var(--accent);cursor:pointer}',
    '#cabinet-root .slider::-moz-range-track{height:2px;border-radius:2px;',
    'background:var(--slider-bg, var(--border))}',
    '#cabinet-root .slider::-moz-range-thumb{width:16px;height:16px;border:0;border-radius:50%;',
    'background:var(--accent);cursor:pointer}',
    '#cabinet-root .slider:disabled{cursor:not-allowed}',
    '#cabinet-root .slider:disabled::-webkit-slider-thumb{background:var(--muted)}',
    '#cabinet-root .slider:disabled::-moz-range-thumb{background:var(--muted)}',
    '#cabinet-root .slider.input--error::-webkit-slider-thumb{background:var(--error)}',
    '#cabinet-root .slider.input--error::-moz-range-thumb{background:var(--error)}',
    '#cabinet-root .slider-head{display:flex;justify-content:space-between;align-items:baseline}',
    '#cabinet-root .slider-head__value{font-size:16px;font-weight:500}',
    '#cabinet-root .bounds{display:flex;justify-content:space-between;font-size:12px;color:var(--muted)}',
    // Срок/взнос рядом (историч. макет 1b #credit .duo; в v3 — кадр 7a,
    // раскладка та же) — десктоп-дефолт; мобильный вариант (одна колонка) —
    // только в media ниже.
    '#cabinet-root .duo{display:grid;grid-template-columns:1fr 1fr;gap:40px}',
    // Заголовок ползунка внутри slider-head — виден на десктопе (макет:
    // «Срок кредита» слева, значение справа), на мобильном скрыт — там
    // заголовок несёт отдельный крупный .label над блоком (макет m4).
    '#cabinet-root .cab-slider-title{display:inline}',
    // Границы (min/max) внутри slider-head — мобильный вариант (по бокам от
    // значения, макет m4 slider-head--tap); на десктопе скрыты — там границы
    // отдельной строкой .bounds ПОД ползунком (историч. макет 1b; в v3 — 7a).
    '#cabinet-root .cab-bound-inline{display:none}',
    // Крупный .label над ползунком — только мобильный вариант (макет m4);
    // на десктопе заголовок уже показан внутри slider-head (.cab-slider-title).
    '#cabinet-root .cab-slider-label-mobile{display:none}',

    // ── Веб-вход (план 2026-09-30-001, U8/U9; макет lk-source-v4.html 8a–8h) ──
    '#cabinet-root .cab-sr{position:absolute;width:1px;height:1px;margin:-1px;overflow:hidden;',
    'clip:rect(0 0 0 0);white-space:nowrap;border:0}',
    '#cabinet-root a.btn{text-decoration:none;color:#fff}',
    '#cabinet-root .btn--ghost{background:#fff;color:#111;border:1px solid var(--line,#e0e0e0)}',
    '#cabinet-root a.btn--ghost{color:#111}',
    '#cabinet-root .btn--ghost:hover{background:var(--chip)}',
    '#cabinet-root .cab-actions{display:flex;flex-direction:column;gap:12px;margin-top:8px}',
    '#cabinet-root [tabindex="-1"]:focus{outline:none}',
    // Скелетоны 8a (.w38–.w41): ширины по полю
    '#cabinet-root .own__sk{display:block;height:16px;margin-top:3px;border-radius:4px;background:#ececec}',
    // Нижняя зона карточки (.w43): статус оценки 8a / 8d
    '#cabinet-root .own__status{order:5;flex-basis:100%;min-width:0;margin-top:16px;padding-top:16px;',
    'border-top:.5px solid #e6e6e6;display:flex;flex-direction:column;gap:10px}',
    '#cabinet-root .own__status--text{gap:4px}',
    // 8a: часы + «Оцениваем автомобиль» + «около 1 мин» справа (.w44–.w48)
    '#cabinet-root .cab-wait-row{display:flex;align-items:center;gap:10px}',
    '#cabinet-root .cab-wait-title{font-size:16px;font-weight:500;color:#111}',
    '#cabinet-root .cab-wait-eta{font-size:13px;color:#888;margin-left:auto}',
    '#cabinet-root .cab-clock{position:relative;flex-shrink:0;display:inline-block;width:18px;height:18px;',
    'border:1.5px solid #111;border-radius:50%;box-sizing:border-box}',
    '#cabinet-root .cab-clock i{position:absolute;background:#111}',
    '#cabinet-root .cab-clock i:first-child{left:6.75px;top:2.5px;width:1.5px;height:5.5px}',
    '#cabinet-root .cab-clock i:last-child{left:6.75px;top:6.5px;width:4.5px;height:1.5px}',
    '#cabinet-root .cab-clock--muted{border-color:#888}',
    '#cabinet-root .cab-clock--muted i{background:#888}',
    // Неопределённый прогресс (.w49/.w50 — «бегущая полоса»)
    '#cabinet-root .cab-progress{position:relative;height:2px;background:#e6e6e6;border-radius:2px;overflow:hidden}',
    '#cabinet-root .cab-progress span{position:absolute;top:0;bottom:0;width:45%;background:var(--accent,#1a1a1a);',
    'animation:cabRun 1.6s ease-in-out infinite}',
    '@keyframes cabRun{0%{left:-45%}100%{left:100%}}',
    // 8d / менеджер: заголовок 18/700 (.w119)
    '#cabinet-root .cab-status-title{font-size:18px;font-weight:700;color:#111}',
    // 8c/8e: сообщение (.w110/.w111)
    '#cabinet-root .cab-msg{margin-top:12px;background:#f5f5f5;border-radius:8px;padding:14px 16px;',
    'font-size:14px;line-height:1.55;color:#111;text-wrap:pretty}',
    // 8c/8e: сетка полей авто 3 колонки (.w112), поля (.w53/.w113/.w116/.w117)
    '#cabinet-root .cab-car-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px 20px;margin-top:16px}',
    '#cabinet-root .cab-car-label{display:block;font-size:12px;color:#888;margin-bottom:6px}',
    '#cabinet-root .cab-car-input{width:100%;box-sizing:border-box;height:52px;padding:0 14px;border:1px solid #e6e6e6;',
    'border-radius:8px;font-size:17px;color:#111;outline:none;background:#fff;font-family:inherit}',
    '#cabinet-root .cab-car-input:disabled{background:var(--field,#fafafa)}',
    '#cabinet-root .cab-car-unit{display:flex;align-items:center;height:52px;border:1px solid #e6e6e6;border-radius:8px;',
    'overflow:hidden;background:#fff}',
    '#cabinet-root .cab-car-unit input{flex:1;min-width:0;border:none;outline:none;height:100%;padding:0 14px;',
    'font-size:17px;color:#111;background:transparent;font-family:inherit}',
    '#cabinet-root .cab-car-unit span{padding:0 14px;font-size:13px;color:#888}',
    // Руль — два сегмента (.w68/.w114/.w115)
    '#cabinet-root .cab-seg{display:grid;grid-template-columns:1fr 1fr;gap:8px}',
    '#cabinet-root .cab-seg .tile{height:52px;min-height:52px;box-sizing:border-box;display:flex;flex-direction:row;',
    'align-items:center;justify-content:flex-start;',
    'padding:0 16px;font-size:15px;border-radius:8px}',
    '#cabinet-root .cab-seg .tile__title{margin:0;line-height:1.2;font-size:15px;font-weight:400}',
    // «Оценку проведёт менеджер» (.w118, гэп .w22)
    '#cabinet-root .cab-manager{margin-top:20px;padding-top:16px;border-top:.5px solid #e6e6e6;',
    'display:flex;flex-direction:column;gap:4px}',
    // Строка зачёта: статус «оцениваем… / после осмотра / не получен» (.w70/.w120)
    '#cabinet-root .cab-down-status{display:flex;align-items:center;gap:8px;font-size:15px;color:#888;white-space:nowrap}',
    // Тексты, разные для десктопа и 390 (макет)
    '#cabinet-root .cab-only-mob{display:none}',
    // 8b: момент появления оценки (.w101/.w104/.w105)
    '#cabinet-root .own__valuation[data-flash]{background:#eef5f0;border-radius:8px;padding:12px 14px;',
    'margin:12px -14px 0;border-top:0;flex-wrap:wrap;gap:6px 16px;animation:cabFlash 3s ease-out 1 forwards}',
    '#cabinet-root .cab-down-sum[data-flash]{background:#eef5f0;border-radius:6px;padding:4px 8px;margin-right:-8px;',
    'animation:cabFlash 3s ease-out 1 forwards}',
    // SUCCESS без found_info: сумма и пояснение как в 8b, затем поля
    '#cabinet-root .cab-val-box{display:flex;align-items:baseline;flex-wrap:wrap;gap:6px 16px;margin-top:12px;',
    'background:#fafafa;border-radius:8px;padding:16px 20px}',
    // 8f/8g (.w125–.w137)
    '#cabinet-root .cab-limit-title{font-size:20px;font-weight:700;letter-spacing:.01em}',
    '#cabinet-root .cab-limit-text{font-size:14px;line-height:1.55;text-wrap:pretty}',
    '#cabinet-root .cab-limit-box{background:#f5f5f5;border-radius:8px;padding:14px 16px;display:flex;',
    'align-items:center;gap:12px}',
    '#cabinet-root .cab-limit-box__col{display:flex;flex-direction:column;gap:2px}',
    '#cabinet-root .cab-limit-time{font-size:16px;font-weight:500;color:#111}',
    '#cabinet-root .cab-link-muted{font-size:14px;color:#888;text-decoration:underline;text-underline-offset:3px;',
    'padding:14px 0;display:inline-block}',
    '#cabinet-root .cab-link-strong{font-size:14px;color:#111;text-decoration:underline;text-underline-offset:3px;',
    'padding:8px 0;display:inline-block}',
    '@keyframes cabFlash{0%,70%{background:#eef5f0}100%{background:transparent}}',
    '#cabinet-root .cab-ready{color:#2e6b4f;font-size:13px;animation:cabFade 3s ease-out 1 forwards}',
    '@keyframes cabFade{0%,80%{opacity:1}100%{opacity:0}}',
    '#cabinet-root .cab-fade-in{animation:cabFadeIn .2s ease-out 1;display:flex;flex-direction:column;gap:12px}',
    '#cabinet-root .cab-fade-in .cab-msg{margin-bottom:0}',
    '@keyframes cabFadeIn{from{opacity:0}to{opacity:1}}',

    // ── Мобильный вьюпорт ────────────────────────────────────────────────────
    // Десктопные правила выше — умолчание. Брейкпоинт и содержимое блока — из
    // макета (историч. lk-source.html:312–330; v3: lk-source-v3.html:348–363,
    // тот же брейкпоинт 600px — KTD7 плана 2026-09-26-001), один в один,
    // исключены только правила самой галереи (.gallery*, .screen--*).
    '@media (max-width:600px){',
      // .card{width:100%} — макет; паддинг 16px — гаттер экрана (в макете его
      // даёт рамка .screen--mobile, у нас корень паддинга не имеет)
      '#cabinet-root .card{width:100%;padding:0 16px}',
      '#cabinet-root .container{padding:0 16px}',
      '#cabinet-root .duo{grid-template-columns:1fr;gap:24px}',
      '#cabinet-root .tiles--2,#cabinet-root .tiles--4{grid-template-columns:1fr}',
      // .photo--desktop на телефоне — полный bleed (канон мобильного — макет,
      // группа #mobile: .photo--bleed edge-to-edge, кнопки 44×44; лендинг
      // с его 24×36 при 640px здесь не ориентир)
      '#cabinet-root .photo--desktop{width:calc(100% + 32px);margin:14px -16px 0;border-radius:0}',
      '#cabinet-root .photo__nav{width:44px;height:44px;font-size:18px}',
      '#cabinet-root .photo__nav--prev{left:8px}',
      '#cabinet-root .photo__nav--next{right:8px}',
      '#cabinet-root .thumbs{width:100%}',
      '#cabinet-root .own{flex-direction:column}',
      '#cabinet-root .own__photo{width:100%;height:auto;aspect-ratio:3/2}',
      '#cabinet-root .own__grid{grid-template-columns:1fr 1fr}',
      '#cabinet-root .facts{grid-template-columns:1fr}',
      // Мобильные переключатели приложения (--tap-режим слайдера) — не из
      // media-блока макета: макет показывает их отдельной разметкой в #mobile,
      // у нас одна разметка и переключение видимости
      '#cabinet-root .cab-slider-title{display:none}',
      '#cabinet-root .cab-bound-inline{display:inline}',
      '#cabinet-root .cab-slider-label-mobile{display:block}',
      '#cabinet-root .slider--tap{height:44px;margin:0}',
      '#cabinet-root .slider-head--tap{margin-top:8px}',
      '#cabinet-root .slider-head--tap .slider-head__value{font-size:18px}',
      // Новые правила (2.0, v3) — ТОЛЬКО в конец блока: страж-тест читает
      // первые 2500 символов от начала медиазапроса (см. план 2026-08-26-001).
      // Шапка раздела: заголовок отдельной строкой 24px (макет, группа 3a)
      '#cabinet-root .page-head{display:block}',
      '#cabinet-root .page-title{font-size:24px}',
      // Кнопка авто — строкой на всю ширину, с шевроном (макет, группа 3a)
      '#cabinet-root .car-pick{width:100%;max-width:none;padding:14px 16px;min-height:44px;margin-top:20px}',
      '#cabinet-root .car-pick__chevron{display:inline}',
      // Попап «Данные об автомобиле» — шторка снизу (2.0:335-337; v3 кадр 3а — без изменений)
      '#cabinet-root .popup--wide .popup__dialog{position:absolute;left:0;right:0;bottom:0;top:auto;',
      'width:auto;max-width:none;transform:none;margin:0;max-height:88%;overflow:auto;',
      'border-radius:16px 16px 0 0;padding:20px 20px 28px}',
      '#cabinet-root .popup__grabber{display:block}',
      // Чипы внутри попапа — горизонтальная лента (мобильный эталон, группа 3a)
      '#cabinet-root .popup .chips{flex-wrap:nowrap;white-space:nowrap;overflow-x:auto;padding-bottom:4px}',
      // «Ваш автомобиль»: мобильная геометрия — фото на всю ширину 3:2 без
      // скругления, сетка с прежними паддингом и межрядьем, без column-gap
      // и паддинга карточки. Колонку и aspect-ratio дают правила выше в этом
      // же блоке (десктопные 356×200 не протекают). Порядок элементов — как
      // на десктопе: order/flex-basis зоны подписей наследуются (решение
      // владельца 2026-08-27, U3c: фото → факты → подпись фото → источник);
      // здесь только мобильные отступы зоны — зазор от сетки даёт её нижний
      // паддинг 16px, между строками 6px, снизу 16px (край карточки).
      // align-items:stretch: в колонке flex-start ужимал бы сетку по контенту
      '#cabinet-root .own{gap:0;padding:0;align-items:stretch}',
      '#cabinet-root .own__photo{border-radius:0}',
      '#cabinet-root .own__caption{padding:0 16px}',
      '#cabinet-root .own__grid{gap:14px 16px;padding:16px}',
      '#cabinet-root .own__source{padding:6px 16px 16px}',
      // Текстовый поп-ап согласия (п.3): на мобильной ширине — почти
      // вплотную к краям, паддинг ужат, высота под вьюпорт (текст скроллится)
      '#cab-text-popup .popup__dialog{width:auto;margin:0 8px;max-height:calc(100vh - 24px);padding:20px 16px}',
      // Плашка-фраза: мобильные размеры (v3 .s138) — только в хвост блока
      '#cabinet-root .cab-phrase{padding:16px 18px;margin-bottom:20px;font-size:16px}',
      // Оценка в карточке «Ваш автомобиль»: мобильная геометрия (v3 .s147) —
      // столбец, боковые отступы 16px как у сетки фактов
      '#cabinet-root .own__valuation{margin:0 16px;padding:14px 0 16px;',
      'flex-direction:column;align-items:flex-start;gap:4px}',
      // Строка взноса комбо: зачёт и доплата друг под другом (v3 6b, .s139)
      '#cabinet-root .cab-down-grid{grid-template-columns:1fr}',
      // Карточка-товар авто: мобильная миниатюра и паддинг (v3 .s103/.s104);
      // каскад: побеждает более раннее мобильное правило .car-pick выше —
      // паддинг возвращаем к v3 здесь, в хвосте
      '#cabinet-root .car-pick{padding:8px 14px 8px 8px}',
      '#cabinet-root .car-pick__thumb{width:84px;height:56px}',
      // Веб-вход 8a–8e на 390 (макет v4): одна колонка полей (.w121),
      // зона статуса с боковыми отступами (.w93), «менеджер» (.w122)
      '#cabinet-root .cab-car-grid{grid-template-columns:1fr}',
      '#cabinet-root .own__status{margin:0 16px;padding:14px 0 16px}',
      '#cabinet-root .cab-manager{padding-top:14px}',
      '#cabinet-root .own__valuation[data-flash]{margin:0 0 16px;flex-direction:column;align-items:stretch;gap:4px}',
      '#cabinet-root .cab-only-desk{display:none}',
      '#cabinet-root .cab-only-mob{display:inline}',
      // Ползунок на 390: границы — только в строке над ползунком
      // (slider-head--tap, min · значение · max); нижняя строка .bounds —
      // десктопная, на мобильном давала двойные «12 мес / 84 мес»
      '#cabinet-root .bounds{display:none}',
    '}',
  ].join('');

  var styleEl = document.createElement('style');
  styleEl.textContent = css;
  document.head.appendChild(styleEl);

  // ── 4. УТИЛИТЫ ──────────────────────────────────────────────────────────────
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function onlyDigits(s) { return String(s || '').replace(/\D+/g, ''); }

  // ── U9: утилиты блока «Условия кредита» (calc_model.js) ─────────────────────
  // Число из произвольного пользовательского ввода; '' / нецифровое → null
  // (calc_model.touch тоже трактует null как «не трогать» — согласовано).
  function calcParseNum(v) {
    var n = parseInt(onlyDigits(String(v == null ? '' : v)), 10);
    return isNaN(n) ? null : n;
  }

  // Фоновый градиент трека ползунка под blocked-зоны (доли 0..1 вдоль шкалы,
  // KTD «Максимум шкалы взноса…»): жёсткая граница цветов — каждый стоп
  // задаётся дважды на одном проценте, без интерполяции между зонами.
  function sliderTrackGradient(zones) {
    if (!zones || !zones.length) return 'var(--border)';
    var sorted = zones.slice().sort(function (a, b) { return a.from - b.from; });
    var stops = [], pos = 0;
    for (var i = 0; i < sorted.length; i++) {
      var fromPct = Math.max(0, Math.min(100, sorted[i].from * 100));
      var toPct = Math.max(0, Math.min(100, sorted[i].to * 100));
      if (fromPct > pos) stops.push('var(--border) ' + pos + '%', 'var(--border) ' + fromPct + '%');
      stops.push('var(--blocked) ' + fromPct + '%', 'var(--blocked) ' + toPct + '%');
      pos = toPct;
    }
    if (pos < 100) stops.push('var(--border) ' + pos + '%', 'var(--border) 100%');
    return 'linear-gradient(to right, ' + stops.join(', ') + ')';
  }

  // Коммит числового поля «по blur/Enter/деб-аунс» (R11, U9 approach): ввод
  // подчищается от нецифр сразу, полноценный коммит (в модель) — с задержкой,
  // чтобы не пересчитывать при каждой нажатой цифре.
  function wireDebouncedNumberInput(el, onCommit) {
    var t = null;
    function commitNow() {
      if (t) { clearTimeout(t); t = null; }
      // Блок мог быть пересобран (outerHTML при no_credit_needed) или экран
      // сменился: отсоединённый инпут не должен коммитить стейл-значение.
      if (!el.isConnected) return;
      onCommit(el.value);
    }
    el.addEventListener('input', function () {
      el.value = onlyDigits(el.value);
      if (t) clearTimeout(t);
      t = setTimeout(commitNow, 600);
    });
    el.addEventListener('blur', commitNow);
    el.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); commitNow(); el.blur(); }
    });
  }

  // Маска +7: 10 значащих цифр → "+7 912 345-67-89"
  function phoneDigits(raw) {
    var d = onlyDigits(raw);
    if (d.charAt(0) === '7' || d.charAt(0) === '8') d = d.slice(1);
    return d.slice(0, 10);
  }
  function formatPhone(d) {
    var out = '+7';
    if (d.length > 0) out += ' ' + d.slice(0, 3);
    if (d.length > 3) out += ' ' + d.slice(3, 6);
    if (d.length > 6) out += '-' + d.slice(6, 8);
    if (d.length > 8) out += '-' + d.slice(8, 10);
    return out;
  }

  function fmtSeconds(total) {
    total = Math.max(0, Math.round(total));
    if (total < 60) return total + ' с';
    var m = Math.floor(total / 60), s = total % 60;
    return m + ' мин ' + (s < 10 ? '0' : '') + s + ' с';
  }

  // Человеческое ожидание для лимитов: retry_after может быть суточным —
  // буквальные секунды («через 84600 с») читаются как ошибка.
  function fmtWaitPhrase(total) {
    total = Math.max(0, Math.round(total));
    if (total >= 72000) return 'завтра';                                  // >20 ч
    if (total >= 3600) return 'через ' + Math.ceil(total / 3600) + ' ч';
    if (total >= 60) return 'через ' + Math.ceil(total / 60) + ' мин';
    return 'через ' + total + ' с';
  }

  // Текст 429 rate_limited: limit_source="phone" — ограничение привязано к
  // номеру телефона (свежая ссылка его не обнуляет — иначе новая ссылка
  // выглядит сломанной), остальные источники — общая формулировка.
  function rateLimitText(retryAfter, limitSource) {
    var base = limitSource === 'phone'
      ? 'Лимит отправок кода для этого номера телефона исчерпан. ' +
        'Ограничение привязано к номеру и действует независимо от ссылки.'
      : 'Код запрашивали слишком часто.';
    return base + ' Повторите попытку ' + fmtWaitPhrase(retryAfter) + '.';
  }

  // «0:47» — формат строки таймера повтора из макета m2
  function fmtClock(total) {
    total = Math.max(0, Math.round(total));
    var m = Math.floor(total / 60), s = total % 60;
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  function pluralRu(n, one, few, many) {
    var n10 = n % 10, n100 = n % 100;
    if (n10 === 1 && n100 !== 11) return one;
    if (n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14)) return few;
    return many;
  }

  // «Неверный код. Осталось N попыток» (макет 2a-2), attempts_left — от бэка
  function attemptsText(n) {
    n = n == null ? 0 : n;
    if (n === 1) return 'Неверный код. Осталась 1 попытка';
    return 'Неверный код. Осталось ' + n + ' ' + pluralRu(n, 'попытка', 'попытки', 'попыток');
  }

  // Сетевой сбой (fetch reject / нет ответа): у ошибки нет HTTP-статуса
  function isNetworkError(err) {
    return err.status == null;
  }

  function storageGet(key) {
    try { return sessionStorage.getItem(key); } catch (e) { return null; }
  }
  function storageSet(key, value) {
    try {
      if (value == null) sessionStorage.removeItem(key);
      else sessionStorage.setItem(key, value);
    } catch (e) { /* private mode: живём на памяти */ }
  }
  function localGet(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }
  function localSet(key, value) {
    try {
      if (value == null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch (e) { /* private mode */ }
  }

  // ── 5. API ──────────────────────────────────────────────────────────────────
  function api(method, path, body, headers) {
    // ngrok-skip-browser-warning: дев-туннель ngrok перехватывает браузерные
    // GET interstitial-страницей без CORS-заголовков; заголовок отключает это.
    // В проде (не-ngrok домен) безвреден; включён в whitelist CORS бэка.
    var opts = { method: method, headers: {
      'Content-Type': 'application/json',
      'ngrok-skip-browser-warning': '1'
    } };
    if (headers) for (var k in headers) opts.headers[k] = headers[k];
    if (body != null) opts.body = JSON.stringify(body);
    return fetch(API_BASE + path, opts).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (res.ok) return data;
        var err = new Error(data.error || ('http_' + res.status));
        err.status = res.status;
        err.code = data.error || null;
        err.data = data;
        throw err;
      });
    });
  }

  function apiAuthed(method, path, body) {
    return api(method, path, body, { 'Authorization': 'Bearer ' + (state.bearer || '') });
  }

  // ── 6. СОСТОЯНИЕ ────────────────────────────────────────────────────────────
  var state = {
    token: storageGet(TOKEN_KEY),      // токен ссылки (opaque)
    bearer: storageGet(BEARER_KEY),    // access_token сессии (память + sessionStorage)
    initData: null,                    // ответ auth/init
    phoneDigits: '',                   // введённые 10 цифр
    useKnown: false,                   // «использовать известный номер»
    consentAccepted: false,
    channel: 'voice',                  // выбранный канал (voice предвыбран визуально)
    voiceUsed: false,                  // был ЛЮБОЙ запрос звонка (даже не принятый):
                                        // провайдер молча подменяет повторный звонок
                                        // на SMS в 5-минутном окне — «Получить код
                                        // звонком» одноразова в пределах страницы
    targetMasked: '',                  // маска номера, на который шлём код
    targetFull: null,                  // полный номер СВЕЖЕ введённого (есть у фронта);
                                        // null для bound/use_known — там только маска с сервера
    reauth: false,                     // инлайн re-OTP поверх кабинета
    pendingSubmit: false,              // сабмит, прерванный 401

    me: null,                          // ответ /me
    formValues: {},                    // локальное состояние анкеты (переживает re-OTP)
    formDirty: {},                     // ключи с несохранёнными правками
    submitted: false,

    car: undefined,                    // U8: карточка /cabinet/api/car — undefined
                                        // до первого запроса, null — данных нет (404/сбой)

    web: storageGet(WEB_KEY) === '1',  // веб-вход (U8): сессия открыта с лендинга
    returnUrl: localGet(RETURN_KEY),   // страница лендинга для «Вернуться на сайт» (8f/8g)
    callsLeft: null,                   // веб-вход: остаток звонков в окне (request-code/auth-init)
    nextCallAt: null,                  // веб-вход: момент, с которого доступен повторный звонок (мс)
    recall: false,                     // веб-вход: запрос — повтор (тексты «звонком или в SMS», В1/В3)
    recallReady: false,                // веб-вход: таймер повторного звонка истёк (2б)
    valuation: null,                   // U9: {status, reason} оценки из /me
    valClientFailed: false,            // U9: опрос сдался (потолок/3 сбоя) — 8e на клиенте
    valFlashUntil: 0,                  // U9: подсветка «✓ Отчет готов» (8b) до этого момента
    paymentTouched: false,             // U9: клиент сам двигал платёж/срок — при приходе оценки
                                        // сохраняем его платёж, иначе целимся в платёж с лендинга

    calcState: undefined,              // U9: состояние calc_model.js (initState/touch);
                                        // undefined — не инициализировано, null — деградация
                                        // (R18: calc:null/no_calc/engine_mismatch/скрипты не загружены)
  };

  var timers = { poll: null, cooldown: null, lock: null, ping: null, save: null, ttl: null };

  // Синхронные замки против гонок двойного тапа / параллельных запросов:
  //   requestCodeInFlight — не даём отправить два OTP (второй supersede'ит первый);
  //   saveInFlight/saveQueued — строгая сериализация PUT /form.
  var requestCodeInFlight = false;
  var saveInFlight = false;
  var saveQueued = false;
  // verifyInFlight — на время POST /auth/verify ячейки/кнопка остаются
  // заблокированными; параллельный тик поллинга send-status не должен их
  // ре-энейблить (гонка код-ревью P3).
  var verifyInFlight = false;

  function clearScreenTimers() {
    if (timers.poll) { clearInterval(timers.poll); timers.poll = null; }
    if (timers.cooldown) { clearInterval(timers.cooldown); timers.cooldown = null; }
    if (timers.lock) { clearInterval(timers.lock); timers.lock = null; }
    if (timers.ttl) { clearTimeout(timers.ttl); timers.ttl = null; }
    // Отложенный автосейв не должен переживать смену экрана:
    // иначе saveDraft стреляет на чужом экране (например, после 401 с bearer=null)
    if (timers.save) { clearTimeout(timers.save); timers.save = null; }
  }

  var root = null;
  var screenSeq = 0; // растёт на каждой смене экрана — метка актуальности для ответов сети

  function setScreen(html) {
    // Полный ререндер: узел попапа авто гибнет вместе с экраном — снять
    // Esc-подписку и флаг открытости здесь, единой воронкой для всех смен
    // экрана (кабинет, реавторизация, фатальные состояния).
    closeCarPopup(false);
    clearScreenTimers();
    screenSeq += 1;
    root.innerHTML = html;
  }
  function $(sel) { return root.querySelector(sel); }

  // ── 7. ТЕКСТЫ ОШИБОК API (каждая — своим текстом) ───────────────────────────
  function errorText(err) {
    switch (err.code) {
      case 'link_expired':
        return 'Срок действия ссылки истёк. Попросите ассистента прислать актуальную ссылку на личный кабинет.';
      case 'phone_mismatch':
        return 'Этот номер не совпадает с номером, привязанным к вашей ссылке. ' +
          'Введите номер, который вы подтверждали раньше.';
      case 'consent_required':
        return 'Чтобы получить код, отметьте согласие на обработку персональных данных.';
      case 'invalid_phone':
        return 'Не удалось распознать номер. Проверьте, что он российский и введён полностью.';
      case 'rate_limited':
        return rateLimitText((err.data && err.data.retry_after) || 60,
          err.data && err.data.limit_source);
      case 'invalid_code':
        return attemptsText(err.data && err.data.attempts_left);
      case 'code_superseded':
        return 'Этот код больше не действует — введите код из последнего сообщения.';
      case 'code_expired':
        return 'Код устарел — запросите новый код.';
      case 'locked':
        return 'Ввод кода временно заблокирован. Попробуйте через ' +
          fmtSeconds(err.data && err.data.retry_after || 1800) + '.';
      case 'unauthorized':
        return 'Сессия истекла — подтвердите номер ещё раз.';
      case 'already_submitted':
        return 'Анкета уже отправлена.';
      default:
        return 'Не получилось связаться с сервером. Проверьте интернет и попробуйте ещё раз.';
    }
  }

  function errorHtml(err) {
    return '<div class="note note--md note--error">' + esc(errorText(err)) + '</div>';
  }

  // ── 8. ЭКРАНЫ ───────────────────────────────────────────────────────────────

  // Токен ещё не привязан к номеру — экран m1 (телефон+согласие+канал); после
  // привязки (bound) номер сменить нельзя — тогда используется showChannelScreen.
  function isBound() {
    return !!(state.initData && state.initData.state === 'bound');
  }

  // Скелетон загрузки (макет 2a-8) — вместо спиннера.
  function showLoading() {
    setScreen(
      '<div class="cab-screen card">' +
        '<div class="skeleton">' +
          '<div class="sk sk--title"></div>' +
          '<div class="sk sk--text sk--w160"></div>' +
          '<hr class="divider" style="margin:6px 0">' +
          '<div class="sk sk--field"></div>' +
          '<div class="sk sk--tile"></div>' +
          '<div class="sk sk--btn" style="margin-top:8px"></div>' +
        '</div>' +
      '</div>'
    );
  }

  // Общий каркас информационного экрана (h1 + divider + текст) — макет 2a-1/2a-4.
  function showAuthErrorScreen(title, message, extraNote) {
    setScreen(
      '<div class="cab-screen card">' +
        '<h1 class="h1">' + esc(title) + '</h1>' +
        '<hr class="divider divider--tight">' +
        '<div class="lead">' + esc(message) + '</div>' +
        (extraNote ? '<div class="note">' + esc(extraNote) + '</div>' : '') +
      '</div>'
    );
  }

  function showFatal(message) {
    // Веб-вход (R50): упоминаний чата и ассистента нет
    showAuthErrorScreen('Личный кабинет', message, isWeb() ? null :
      'Вернитесь в чат с ассистентом — там можно запросить новую ссылку.');
  }

  // 2a-1: ссылка недействительна — текст макета дословно. Веб-вход — 8f.
  function showLinkExpired() {
    if (isWeb()) { showSessionEnded(); return; }
    showAuthErrorScreen('Ссылка недействительна',
      'Срок действия ссылки истёк. Попросите ассистента прислать актуальную ссылку на личный кабинет.');
  }

  // ── Веб-вход (план 2026-09-30-001, U8): признак сессии и адрес возврата ──
  function isWeb() { return !!state.web; }

  // entry_source/return_url из auth/init или /me: признак — в sessionStorage
  // вкладки, адрес возврата — в localStorage (8f после закрытия вкладки).
  function rememberWeb(entrySource, returnUrl) {
    if (entrySource === 'web') {
      state.web = true;
      storageSet(WEB_KEY, '1');
    }
    if (state.web && typeof returnUrl === 'string' && returnUrl) {
      state.returnUrl = returnUrl;
      localSet(RETURN_KEY, returnUrl);
    }
  }

  // Кнопка «Вернуться на сайт»: сохранённая страница лендинга — только https
  // на хосте этой же страницы (кабинет живёт на сайте дилера, R69); иначе —
  // корень сайта.
  function safeReturnUrl() {
    var fallback = window.location.origin + '/';
    var url = state.returnUrl;
    if (!url) return fallback;
    try {
      var u = new URL(url, window.location.href);
      var bare = function (h) { return String(h || '').replace(/^www\./, '').toLowerCase(); };
      if (u.protocol === 'https:' && bare(u.hostname) === bare(window.location.hostname)) return u.href;
    } catch (e) { /* битый адрес — корень */ }
    return fallback;
  }

  // Фокус на заголовок нового экрана (8f, 8g, звонок после F5) — скринридер
  // объявляет смену экрана.
  function focusTitle() {
    var t = $('#cab-screen-title');
    if (!t) return;
    try { t.focus({ preventScroll: true }); } catch (e) { t.focus(); }
  }

  // 8f: сессия завершена — веб-вход без действующего входа (R50).
  function showSessionEnded() {
    stopValuationPolling();
    stopPing();
    setScreen(
      '<div class="cab-screen card">' +
        '<h1 class="h1" id="cab-screen-title" tabindex="-1">Сессия завершена</h1>' +
        '<hr class="divider divider--tight">' +
        '<div class="lead">Вкладка была закрыта или истёк срок входа. Вернитесь на сайт дилера ' +
          'и оформите заявку заново — это займёт пару минут.</div>' +
        '<div class="lead">Если вы уже отправили заявку, она у нас — менеджер свяжется с вами.</div>' +
        '<a class="btn btn--tap" id="cab-return" href="' + esc(safeReturnUrl()) + '">Вернуться на сайт</a>' +
      '</div>'
    );
    focusTitle();
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  // 8g: лимит звонков исчерпан — точное время + обратный отсчёт; «Получить код»
  // неактивна до конца срока, потом — ввод номера (1a/8h-1).
  function showTooManyAttempts(retryAfter) {
    var until = Date.now() + Math.max(1, Math.round(retryAfter || 60)) * 1000;
    var at = new Date(until);
    var phone = state.targetFull || state.targetMasked || '';
    setScreen(
      '<div class="cab-screen card">' +
        '<h1 class="h1">Личный кабинет</h1>' +
        '<div class="sub">Авторизация</div>' +
        '<hr class="divider divider--md">' +
        '<div class="cab-limit-title" id="cab-screen-title" role="heading" aria-level="2" tabindex="-1">' +
          'Слишком много попыток</div>' +
        '<div class="cab-limit-text">Лимит звонков с кодом для номера ' +
          (phone ? '<span class="strong">' + esc(phone) + '</span> ' : '') + 'исчерпан.</div>' +
        '<div class="cab-limit-box">' +
          '<span class="cab-clock" aria-hidden="true"><i></i><i></i></span>' +
          '<div class="cab-limit-box__col">' +
            '<span class="cab-limit-time">Повторить можно в ' +
              pad2(at.getHours()) + ':' + pad2(at.getMinutes()) + '</span>' +
            '<span class="note note--md" id="cab-limit-left"></span>' +
          '</div>' +
        '</div>' +
        '<div class="cab-sr" id="cab-live" aria-live="polite"></div>' +
        '<button type="button" class="btn btn--tap" id="cab-limit-get-code" disabled>Получить код</button>' +
        '<div><a class="cab-link-muted" id="cab-return" href="' + esc(safeReturnUrl()) + '">Вернуться на сайт</a></div>' +
      '</div>'
    );
    var leftEl = $('#cab-limit-left');
    var btn = $('#cab-limit-get-code');
    btn.addEventListener('click', function () { if (!btn.disabled) showAuthScreen(); });
    function tick() {
      var left = Math.round((until - Date.now()) / 1000);
      if (left <= 0) {
        clearInterval(timers.lock); timers.lock = null;
        leftEl.textContent = '';
        btn.disabled = false;
        var live = $('#cab-live');
        if (live) live.textContent = 'Можно снова запросить код.';
        return;
      }
      leftEl.textContent = 'через ' + fmtSeconds(left);
    }
    if (timers.lock) clearInterval(timers.lock);
    tick();
    timers.lock = setInterval(tick, 1000);
    focusTitle();
  }

  // 2a-4: лимит запросов кода — отдельный экран для ПЕРВИЧНОЙ отправки.
  // С открытого экрана ввода кода сюда не уходим (renderRateLimitInline):
  // полноэкранная замена уничтожила бы поле ввода при ещё живом коде.
  function showRateLimitScreen(retryAfter, limitSource) {
    showAuthErrorScreen('Слишком много запросов', rateLimitText(retryAfter, limitSource));
  }

  // Плитки выбора канала — общая разметка для объединённого экрана (m1) и
  // экрана повторного входа (showChannelScreen); тексты дословно из макета.
  function channelTileHtml(id, title, hint, selected) {
    return '<button type="button" class="tile tile--rich tile--tap' + (selected ? ' tile--on' : '') + '" ' +
        'id="' + id + '" aria-pressed="' + (selected ? 'true' : 'false') + '">' +
        '<span class="tile__title">' + esc(title) + '</span>' +
        '<span class="tile__hint">' + esc(hint) + '</span>' +
      '</button>';
  }

  function channelTilesHtml(selectedChannel) {
    // Десктоп (макет 1a #auth): tiles--2, два столбца; мобильный (макет m1
    // #mobile) — один столбец, переключение через media (max-width:600px).
    return '<div class="tiles tiles--2" role="group" aria-label="Способ получения кода">' +
        channelTileHtml('cab-ch-voice', 'Звонок с кодом', 'Продиктует робот', selectedChannel === 'voice') +
        channelTileHtml('cab-ch-sms', 'SMS', 'Придёт сообщением', selectedChannel === 'sms') +
      '</div>';
  }

  // ── Согласие: кликабельные фразы (п.3, 2026-08-31) ──
  // auth/init несёт только признаки наличия текстов (policy_available /
  // agreement_available, колонки Policy/Agreement справочника дилеров).
  // Фраза становится ссылкой ТОЛЬКО при наличии текста (В3, фразы
  // независимы); сам текст запрашивается по клику — ускорение A6 (2026-10-03):
  // 44 КБ документов больше не грузятся до экрана входа.
  var legalCache = {};
  function consentDocAvailable(kind) {
    var dealer = (state.initData && state.initData.dealer) || {};
    return dealer[kind + '_available'] === true && !!dealer.code;
  }
  function loadConsentDoc(kind) {
    if (legalCache[kind]) return Promise.resolve(legalCache[kind]);
    var code = ((state.initData && state.initData.dealer) || {}).code;
    return api('GET', '/cabinet/api/dealer/' + encodeURIComponent(code) + '/legal/' + kind)
      .then(function (data) {
        var text = data && typeof data.text === 'string' ? data.text : null;
        if (text) legalCache[kind] = text;
        return text;
      });
  }
  function consentPhraseHtml(id, docKey, phrase) {
    if (!consentDocAvailable(docKey)) return esc(phrase);
    return '<a class="link" href="#" id="' + id + '">' + esc(phrase) + '</a>';
  }
  function consentLabelHtml() {
    return esc(CONSENT_PREFIX) +
      consentPhraseHtml('cab-consent-policy', 'policy', CONSENT_POLICY_PHRASE) +
      esc(CONSENT_MIDDLE) +
      consentPhraseHtml('cab-consent-agreement', 'agreement', CONSENT_AGREEMENT_PHRASE);
  }
  // Клик по фразе не должен переключать чекбокс согласия (label активирует
  // input по клику на потомке): preventDefault гасит и переход по href, и
  // label-активацию; stopPropagation — страховка от делегированных слушателей.
  function wireConsentLinks() {
    var pairs = [
      ['cab-consent-policy', 'policy', 'Политика конфиденциальности'],
      ['cab-consent-agreement', 'agreement', CONSENT_AGREEMENT_PHRASE]
    ];
    for (var i = 0; i < pairs.length; i++) (function (id, docKey, title) {
      var link = document.getElementById(id);
      if (!link) return; // текст у дилера не задан — фраза не ссылка (В3)
      link.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        loadConsentDoc(docKey).then(function (text) {
          if (text) showTextPopup(title, text, link);
        }).catch(function () { /* сеть: фраза остаётся, клик можно повторить */ });
      });
    })(pairs[i][0], pairs[i][1], pairs[i][2]);
  }

  // Экран m1/1a: телефон + согласие + равноправный выбор канала (R28) на одном
  // экране, единая кнопка «Получить код» (макет). Только для небound-токена —
  // для bound используется showChannelScreen (KTD «повторный вход через канал»).
  function showAuthScreen() {
    var d = state.initData || {};
    var knownMasked = d.known_phone_masked;

    var html =
      '<div class="cab-screen card">' +
        '<h1 class="h1">Личный кабинет</h1>' +
        '<div class="sub">Авторизация</div>' +
        '<hr class="divider divider--md">' +
        '<div id="cab-auth-error"></div>' +
        (knownMasked && d.use_known
          ? '<button type="button" class="btn btn--tap" id="cab-use-known">' +
              'Использовать номер ' + esc(knownMasked) + '</button>' +
            '<a class="link link--tap" id="cab-use-known-revert" hidden href="#">Ввести другой номер</a>'
          : '') +
        '<div>' +
          '<label class="label" for="cab-phone">Номер телефона</label>' +
          '<input class="input input--lg" id="cab-phone" type="tel" inputmode="tel" ' +
            'autocomplete="tel" placeholder="+7 (___) ___-__-__" value="' +
            esc(state.phoneDigits ? formatPhone(state.phoneDigits) : '+7 ') + '">' +
        '</div>' +
        '<label class="check check--tap" for="cab-consent-cb">' +
          '<input type="checkbox" id="cab-consent-cb">' +
          '<span class="check__text">' + consentLabelHtml() + '</span>' +
        '</label>' +
        (isWeb()
          // 8h-1: только звонок — без выбора способа и плитки SMS (R23)
          ? '<div class="note">Код подтверждения продиктует робот во входящем звонке.</div>'
          : '<div class="group-title">Способ получения кода</div>' + channelTilesHtml(state.channel)) +
        '<button type="button" class="btn btn--tap" id="cab-get-code" disabled>Получить код</button>' +
      '</div>';

    setScreen(html);
    wireConsentLinks();

    var errBox = $('#cab-auth-error');
    var consentCb = $('#cab-consent-cb');
    var phoneInput = $('#cab-phone');
    var getCodeBtn = $('#cab-get-code');
    var useKnownBtn = $('#cab-use-known');
    var useKnownRevert = $('#cab-use-known-revert');
    var voiceTile = $('#cab-ch-voice');
    var smsTile = $('#cab-ch-sms');

    consentCb.checked = state.consentAccepted;
    state.useKnown = false; // экран открыт заново — предыдущий выбор use_known не переносится молча
    if (isWeb()) state.channel = 'voice'; // R23: в веб-сессии только звонок

    function syncButtons() {
      state.consentAccepted = !!consentCb.checked;
      var phoneOk = state.useKnown || state.phoneDigits.length === 10;
      getCodeBtn.disabled = !(state.consentAccepted && phoneOk);
    }

    function selectChannel(channel) {
      if (!voiceTile || !smsTile) return; // веб-вход: плиток нет
      state.channel = channel;
      voiceTile.classList.toggle('tile--on', channel === 'voice');
      voiceTile.setAttribute('aria-pressed', channel === 'voice' ? 'true' : 'false');
      smsTile.classList.toggle('tile--on', channel === 'sms');
      smsTile.setAttribute('aria-pressed', channel === 'sms' ? 'true' : 'false');
    }

    consentCb.addEventListener('change', syncButtons);
    if (voiceTile) voiceTile.addEventListener('click', function () { selectChannel('voice'); });
    if (smsTile) smsTile.addEventListener('click', function () { selectChannel('sms'); });

    phoneInput.addEventListener('input', function () {
      state.phoneDigits = phoneDigits(phoneInput.value);
      phoneInput.value = formatPhone(state.phoneDigits);
      syncButtons();
    });

    if (useKnownBtn) {
      useKnownBtn.addEventListener('click', function () {
        state.useKnown = true;
        phoneInput.value = knownMasked;
        phoneInput.disabled = true;
        useKnownBtn.hidden = true;
        useKnownRevert.hidden = false;
        syncButtons();
      });
      useKnownRevert.addEventListener('click', function (e) {
        e.preventDefault();
        state.useKnown = false;
        phoneInput.disabled = false;
        phoneInput.value = formatPhone(state.phoneDigits);
        useKnownBtn.hidden = false;
        useKnownRevert.hidden = true;
        syncButtons();
      });
    }

    getCodeBtn.addEventListener('click', function () {
      if (state.useKnown) {
        state.targetMasked = knownMasked;
        state.targetFull = null; // известный номер — фронту доступна только маска
      } else {
        state.targetMasked = formatPhone(state.phoneDigits).replace(/\d(?=\d{2}\D*\d{2}$)/g, '*');
        state.targetFull = formatPhone(state.phoneDigits); // свежевведённый — полный номер есть у фронта
      }
      if (errBox) errBox.innerHTML = '';
      requestCode(state.channel, '#cab-auth-error');
    });

    syncButtons();
  }

  // Экран повторного входа/re-OTP для bound-токена: равноправный выбор канала
  // (R28), код НЕ отправляется автоматически — только по тапу на плитку.
  // Ссылки смены номера здесь нет: TOFU запрещает смену уже привязанного номера.
  function showChannelScreen(extraNote) {
    var html =
      '<div class="cab-screen card">' +
        '<h1 class="h1">Как отправить код?</h1>' +
        '<div class="sub">Авторизация</div>' +
        '<hr class="divider divider--md">' +
        '<div class="text">Код придёт на номер ' +
          '<span class="strong">' + esc(state.targetMasked || 'вашего телефона') + '</span></div>' +
        (extraNote ? '<div class="note">' + esc(extraNote) + '</div>' : '') +
        '<div id="cab-channel-error"></div>' +
        '<div class="group-title">Способ получения кода</div>' +
        channelTilesHtml('voice') +
      '</div>';

    setScreen(html);

    $('#cab-ch-voice').addEventListener('click', function () { requestCode('voice', '#cab-channel-error'); });
    $('#cab-ch-sms').addEventListener('click', function () { requestCode('sms', '#cab-channel-error'); });
  }

  // Кнопки, инициирующие запрос кода: блокируются синхронно на клике
  // (до ответа сети), разблокируются в хвосте .then/.catch.
  var REQUEST_CODE_BUTTONS = ['#cab-ch-voice', '#cab-ch-sms', '#cab-get-code', '#cab-resend', '#cab-switch-channel'];

  function setRequestCodeButtonsDisabled(disabled) {
    for (var i = 0; i < REQUEST_CODE_BUTTONS.length; i++) {
      var btn = $(REQUEST_CODE_BUTTONS[i]);
      if (!btn) continue;
      btn.disabled = disabled;
      if (btn.tagName === 'A') btn.classList.toggle('link--disabled', disabled);
    }
  }

  function unlockRequestCode() {
    requestCodeInFlight = false;
    setRequestCodeButtonsDisabled(false);
  }

  // Гард актуальности ответа request-code: состояние ушло вперёд, если пользователь
  // уже авторизовался (verify прошёл), сменился экран или менялась переавторизация.
  // Без него устаревший ответ выкинул бы авторизованного назад на экран ввода кода.
  function requestCodeOutcomeStale(seqAtRequest, reauthAtRequest) {
    return !!state.bearer || screenSeq !== seqAtRequest || state.reauth !== reauthAtRequest;
  }

  // Запрос кода: request-code, затем экран ввода кода.
  function requestCode(channel, errorSel, fromCodeScreen) {
    // Синхронный замок: двойной тап не должен слать два OTP —
    // второй код supersede'ит первый, и клиент вводит устаревший.
    if (requestCodeInFlight) return;
    requestCodeInFlight = true;
    setRequestCodeButtonsDisabled(true);

    // Метки актуальности: ответ, пришедший после ухода состояния вперёд, не рендерим
    var seqAtRequest = screenSeq;
    var reauthAtRequest = state.reauth;

    // Прежний канал: при отбое нашим же лимитом (инлайн-429) живая отправка —
    // всё ещё старая, и полинг обязан рендерить её канал, не запрошенный.
    var prevChannel = state.channel;
    state.channel = channel;
    // O5: любой запрос звонка — даже не принятый провайдером — взводит его
    // 5-минутный триггер подмены звонка на SMS, поэтому флаг ставится по
    // факту запроса, а не по успеху отправки.
    if (channel === 'voice') state.voiceUsed = true;
    var body = { token: state.token, channel: channel };
    if (!isBound()) {
      if (state.useKnown) body.use_known = true;
      else if (state.phoneDigits.length === 10) body.phone = '+7' + state.phoneDigits;
    }
    if (state.consentAccepted) {
      body.consent = { version: CONSENT_VERSION, accepted: true };
    }

    var errBox = errorSel ? $(errorSel) : null;
    if (errBox) errBox.innerHTML = '';

    api('POST', '/cabinet/api/auth/request-code', body).then(function (data) {
      unlockRequestCode();
      if (requestCodeOutcomeStale(seqAtRequest, reauthAtRequest)) return;
      if (data.status === 'retry_free') {
        // Попытка не засчитана — повтор доступен сразу
        showCodeScreen({ initialState: 'retry_free', cooldown: 0 });
        return;
      }
      var cooldown = data.retry_after || (isWeb() ? WEB_RECALL_S : RESEND_COOLDOWN_S);
      if (isWeb()) {
        // retry_after — секунд до следующего разрешённого звонка (45 с; при
        // calls_left = 0 — до выхода из окна 10 мин, В4); recall — повтор
        state.callsLeft = data.calls_left != null ? data.calls_left : null;
        state.recall = !!data.recall;
        state.nextCallAt = Date.now() + cooldown * 1000;
      }
      showCodeScreen({ initialState: 'sending', cooldown: cooldown });
    }).catch(function (err) {
      unlockRequestCode();
      if (requestCodeOutcomeStale(seqAtRequest, reauthAtRequest)) return;
      if (err.code === 'link_expired') { showLinkExpired(); return; }
      // 2a-4: лимит при первичной отправке — отдельный экран; с открытого
      // экрана ввода кода — инлайн (ранее полученный код ещё жив, полноэкранная
      // замена уничтожила бы поле ввода).
      if (err.code === 'rate_limited' && isWeb()) {
        // 8g: лимит звонков веб-сессии (R50/R55) — отдельный экран с отсчётом
        showTooManyAttempts((err.data && err.data.retry_after) || 600);
        return;
      }
      if (err.code === 'rate_limited') {
        if (fromCodeScreen) {
          // Запрос не прошёл — экран продолжает жить старой отправкой:
          // канал возвращаем (иначе полинг обещает звонок, которого не было),
          // отсчёт перезапускаем, чтобы ссылки повтора не остались кликабельными.
          // voiceUsed НЕ откатываем — решение O5, флаг по факту запроса.
          state.channel = prevChannel;
          renderRateLimitInline(err);
          startResendCooldown((err.data && err.data.retry_after) || RESEND_COOLDOWN_S);
        } else {
          showRateLimitScreen((err.data && err.data.retry_after) || 3600,
            err.data && err.data.limit_source);
        }
        return;
      }
      var msg = errorHtml(err);
      if (fromCodeScreen) {
        var codeErr = $('#cab-code-error');
        if (codeErr) codeErr.innerHTML = msg;
      } else if (errBox) {
        errBox.innerHTML = msg; // phone_mismatch и прочие — инлайн на экране телефона
      } else {
        showFatal(errorText(err));
      }
    });
  }

  // ── КОД: 4 ОТДЕЛЬНЫЕ ЯЧЕЙКИ (макет m2) ───────────────────────────────────────
  var CODE_CELL_IDS = ['cab-code-0', 'cab-code-1', 'cab-code-2', 'cab-code-3'];

  function getCodeCells() {
    var cells = [];
    for (var i = 0; i < CODE_CELL_IDS.length; i++) {
      var c = $('#' + CODE_CELL_IDS[i]);
      if (c) cells.push(c);
    }
    return cells;
  }
  function getCodeCellsValue() {
    return getCodeCells().map(function (c) { return c.value; }).join('');
  }
  // Сброс ячеек к чистому вводу — после смены канала и когда пользователь
  // начинает вводить код заново после ошибки/блокировки.
  function clearCodeCells() {
    var cells = getCodeCells();
    for (var i = 0; i < cells.length; i++) {
      cells[i].value = '';
      cells[i].disabled = false;
      cells[i].className = 'code__cell' + (i === 0 ? ' code__cell--active' : '');
    }
    if (cells[0]) cells[0].focus();
    var confirmBtn = $('#cab-confirm');
    if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.classList.remove('btn--disabled'); }
  }
  // 2a-2: неверный код — красные ячейки; значения очищаются, фокус на первую.
  function markCodeCellsError() {
    var cells = getCodeCells();
    for (var i = 0; i < cells.length; i++) {
      cells[i].value = '';
      cells[i].disabled = false;
      cells[i].className = 'code__cell code__cell--error';
    }
    if (cells[0]) cells[0].focus();
    var confirmBtn = $('#cab-confirm');
    if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.classList.remove('btn--disabled'); }
  }
  // 2a-3: блокировка — серые/заблокированные ячейки, ввод недоступен.
  function markCodeCellsLocked() {
    var cells = getCodeCells();
    for (var i = 0; i < cells.length; i++) {
      cells[i].value = '';
      cells[i].disabled = true;
      cells[i].className = 'code__cell code__cell--locked';
    }
    var confirmBtn = $('#cab-confirm');
    if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.classList.add('btn--disabled'); }
  }

  function codeCellsHtml() {
    var out = '<div class="code" id="cab-code-cells">';
    for (var i = 0; i < CODE_CELL_IDS.length; i++) {
      out += '<input class="code__cell' + (i === 0 ? ' code__cell--active' : '') + '" ' +
        'id="' + CODE_CELL_IDS[i] + '" type="text" inputmode="numeric" pattern="[0-9]*" maxlength="1" ' +
        'autocomplete="' + (i === 0 ? 'one-time-code' : 'off') + '">';
    }
    out += '</div>';
    return out;
  }

  // Общий хелпер распределения автофилла: цифры (paste в любую ячейку, либо
  // iOS one-time-code autofill, который может подставить весь код в ОДНУ
  // ячейку вместо посимвольного набора) раскладываются по ячейкам начиная с
  // startIdx; фокус — на следующую пустую (или последнюю), автосабмит на 4.
  function distributeCodeDigits(cells, confirmBtn, startIdx, digits) {
    for (var i = 0; i < digits.length && (startIdx + i) < cells.length; i++) {
      cells[startIdx + i].value = digits.charAt(i);
    }
    var focusIdx = -1;
    for (var j = 0; j < cells.length; j++) {
      if (!cells[j].value) { focusIdx = j; break; }
    }
    if (focusIdx === -1) focusIdx = cells.length - 1;
    cells[focusIdx].focus();
    var full = getCodeCellsValue();
    if (confirmBtn) confirmBtn.disabled = full.length !== 4;
    if (full.length === 4) verifyCode(full); // автосабмит на 4-й цифре
  }

  // Ввод с мобильной клавиатуры (по цифре на ячейку, автопереход) и вставка
  // всего кода из SMS (paste в любую ячейку) — автосабмит при заполнении всех 4.
  function wireCodeCells() {
    var cells = getCodeCells();
    var confirmBtn = $('#cab-confirm');

    cells.forEach(function (cell, idx) {
      cell.addEventListener('input', function () {
        cell.classList.remove('code__cell--error');
        var digits = onlyDigits(cell.value);
        if (digits.length > 1) {
          // iOS autofill целиком в одну ячейку — распределяем той же логикой, что paste
          distributeCodeDigits(cells, confirmBtn, idx, digits);
          return;
        }
        cell.value = digits;
        if (digits && idx < cells.length - 1) cells[idx + 1].focus();
        var full = getCodeCellsValue();
        if (confirmBtn) confirmBtn.disabled = full.length !== 4;
        if (full.length === 4) verifyCode(full); // автосабмит на 4-й цифре
      });
      cell.addEventListener('keydown', function (e) {
        if (e.key === 'Backspace' && !cell.value && idx > 0) cells[idx - 1].focus();
      });
      cell.addEventListener('paste', function (e) {
        var clip = e.clipboardData || window.clipboardData;
        var digits = onlyDigits(clip ? clip.getData('text') : '').slice(0, 4);
        if (!digits) return;
        e.preventDefault();
        distributeCodeDigits(cells, confirmBtn, 0, digits);
      });
    });
    if (cells[0]) cells[0].focus();
  }

  // «Изменить номер» (только до привязки номера) — общая разметка/навешивание
  // для ЛЮБОГО под-состояния экрана кода (ввод, ошибка отправки): код-ревью
  // нашёл, что ссылка рендерилась только в renderCodeInputArea, а любой
  // переход в renderSendFailureArea (retry_free/expired, в т.ч. с первого
  // тика send-status-поллинга) перезаписывал #cab-auth-body целиком и терял
  // её без возврата — пользователь с ошибочно введённым номером не мог его
  // поправить. Хелпер общий — потерянный вызов не должен повториться при
  // следующей переверстке под-состояний.
  function changePhoneLinkHtml() {
    return isBound() ? '' : '<a class="link link--tap" id="cab-change-phone" href="#">Изменить номер</a>';
  }
  function wireChangePhoneLink() {
    var changeLink = $('#cab-change-phone');
    if (changeLink) {
      changeLink.addEventListener('click', function (e) {
        e.preventDefault();
        // Провайдерский триггер подмены живёт на НОМЕРЕ — для другого номера
        // звонок снова доступен; вернувшегося на тот же номер держит бэкенд-лимит.
        state.voiceUsed = false;
        state.recall = false;
        showAuthScreen();
      });
    }
  }

  // Активная область ввода: ячейки + строка таймера + «Подтвердить» +
  // «Изменить номер» (только до привязки номера).
  function renderCodeInputArea() {
    var body = $('#cab-auth-body');
    if (!body) return;
    body.innerHTML =
      codeCellsHtml() +
      '<div class="note note--md" id="cab-timer"></div>' +
      '<button type="button" class="btn btn--tap" id="cab-confirm" disabled>Подтвердить</button>' +
      changePhoneLinkHtml();
    wireCodeCells();
    $('#cab-confirm').addEventListener('click', function () {
      var v = getCodeCellsValue();
      if (v.length === 4) verifyCode(v);
    });
    wireChangePhoneLink();
  }

  // 2a-5/2a-4-семейство «ошибка отправки»: error-стиль. В голосовой ветке
  // кнопки «Повторить» нет ни при retry_free, ни при expired — повторный
  // запрос звонка провайдер молча подменяет на SMS (O5/R12), поэтому только
  // переход на SMS. В SMS-ветке повтор допустим; «Получить код звонком»
  // одноразова (voiceUsed).
  function renderSendFailureArea(kind) {
    var body = $('#cab-auth-body');
    if (!body) return;
    if (isWeb()) { renderWebSendFailureArea(kind); return; }
    // #cab-code-error — сосед #cab-auth-body, перезапись body его не чистит:
    // без явной очистки инлайн-баннер лимита остался бы висеть над свежим
    // сообщением об ошибке отправки из следующего тика полинга.
    var codeErr = $('#cab-code-error');
    if (codeErr) codeErr.innerHTML = '';
    var target = esc(state.targetFull || state.targetMasked || 'ваш номер');
    var msg1, msg2;
    if (kind === 'retry_free') {
      msg1 = 'Не удалось отправить код на ' + target + '.';
      msg2 = 'Попытка не засчитана — можно повторить сразу.';
    } else if (kind === 'expired') {
      msg1 = 'Код устарел. Запросите новый.';
      msg2 = '';
    } else { // прочие сбои отправки
      msg1 = 'Не удалось отправить код на ' + target + '.';
      msg2 = 'Выберите другой способ получения кода.';
    }
    var isVoice = state.channel === 'voice';
    var actionsHtml = isVoice
      ? '<button type="button" class="btn btn--block" id="cab-switch-channel">Получить код в SMS</button>'
      : '<button type="button" class="btn btn--block" id="cab-resend">Повторить</button>' +
        (state.voiceUsed ? '' :
          '<div><a class="link" id="cab-switch-channel" href="#">Получить код звонком</a></div>');
    body.innerHTML =
      '<div class="text note--error">' + msg1 + '</div>' +
      (msg2 ? '<div class="lead">' + msg2 + '</div>' : '') +
      actionsHtml +
      (isBound() ? '' : '<div style="margin-top:14px">' + changePhoneLinkHtml() + '</div>');
    var statusEl = $('#cab-send-status');
    if (statusEl) statusEl.textContent = '';
    var resendBtn = $('#cab-resend');
    if (resendBtn) resendBtn.addEventListener('click', function () {
      requestCode(state.channel, null, true);
    });
    var switchEl = $('#cab-switch-channel');
    if (switchEl) switchEl.addEventListener('click', function (e) {
      e.preventDefault();
      requestCode(state.channel === 'voice' ? 'sms' : 'voice', null, true);
    });
    wireChangePhoneLink();
  }

  // Веб-вход (U8, R23/R55): SMS нет. Сбой звонка или устаревший код — сообщение
  // и повторный звонок по окну 45 с (таймер или «Позвонить ещё раз»).
  // Неудачная отправка тоже попадает в окно звонков сервера — таймер
  // консервативно отсчитывается от неё (не обещаем звонок, который отобьёт 429).
  // 8h-2б (макет): код истёк — ячейки остаются, статус «Не дозвонились? …»,
  // повторный звонок — по таймеру окна 45 с («Позвонить ещё раз»)
  function showWebNoAnswer() {
    state.recallReady = true;
    var statusEl = $('#cab-send-status');
    if (statusEl) statusEl.textContent = webNoAnswerText();
    if (timers.poll) { clearInterval(timers.poll); timers.poll = null; }
  }

  // Подсказка «код не пришёл»: до повтора — про звонок; после повтора код
  // может прийти и в SMS (каскад провайдера, план 2026-10-11-001, В1/В3)
  function webNoAnswerText() {
    return state.recall
      ? 'Код не пришёл? Проверьте SMS и что телефон не в режиме «Не беспокоить».'
      : 'Не дозвонились? Проверьте, что телефон рядом и не в режиме «Не беспокоить».';
  }

  function renderWebSendFailureArea(kind) {
    if (kind === 'expired') { showWebNoAnswer(); return; }
    var body = $('#cab-auth-body');
    var codeErr = $('#cab-code-error');
    if (codeErr) codeErr.innerHTML = '';
    var target = esc(state.targetFull || state.targetMasked || 'ваш номер');
    var msg = kind === 'expired'
      ? 'Код устарел. Запросите новый звонок.'
      : 'Не удалось позвонить на ' + target + '.';
    if (kind !== 'expired' && !(state.nextCallAt && state.nextCallAt > Date.now())) {
      state.nextCallAt = Date.now() + WEB_RECALL_S * 1000;
    }
    body.innerHTML =
      '<div class="text note--error">' + msg + '</div>' +
      '<div class="note note--md" id="cab-timer"></div>' +
      changePhoneLinkHtml();
    var statusEl = $('#cab-send-status');
    if (statusEl) statusEl.textContent = '';
    var left = state.nextCallAt ? Math.round((state.nextCallAt - Date.now()) / 1000) : 0;
    if (left > 0) startResendCooldown(left);
    else renderResendReady($('#cab-timer'));
    wireChangePhoneLink();
  }

  // 429 с открытого экрана ввода кода: инлайн-предупреждение вместо
  // полноэкранного showRateLimitScreen — ранее полученный код ещё жив
  // (лимит отбивает запрос ДО гашения прежнего кода), поле ввода сохраняется.
  function renderRateLimitInline(err) {
    var errBox = $('#cab-code-error');
    var text = rateLimitText((err.data && err.data.retry_after) || 3600,
      err.data && err.data.limit_source);
    if (errBox) {
      errBox.innerHTML = '<div class="note note--md note--error">' + esc(text) + '</div>';
    } else {
      showRateLimitScreen((err.data && err.data.retry_after) || 3600,
        err.data && err.data.limit_source);
    }
  }

  // Экран ввода кода. Выбранный способ напоминается ПОСТОЯННО видимым блоком:
  // для голоса предупреждение о входящем звонке видно всё время ожидания (R28).
  function showCodeScreen(opts) {
    opts = opts || {};
    var bound = isBound();
    var isVoice = state.channel === 'voice';
    var displayTarget = (!bound && state.targetFull) ? state.targetFull : (state.targetMasked || 'вашего телефона');
    // Веб-вход после повтора: провайдер может прислать код в SMS (В1/В3)
    var webRecall = isWeb() && state.recall;

    var leadText = webRecall
      ? 'Отправляем новый код на номер <span class="strong">' + esc(displayTarget) + '</span>'
      : isVoice
        ? 'Звоним на номер <span class="strong">' + esc(displayTarget) + '</span>'
        : 'Отправляем код на <span class="strong">' + esc(displayTarget) + '</span>';

    var reminderHtml = webRecall
      ? '<div class="cab-reminder cab-reminder-voice" id="cab-reminder">' +
          'Новый код придёт звонком или в SMS. Если позвонят — ' +
          'робот продиктует цифры кода.' +
        '</div>'
      : isVoice
      ? '<div class="cab-reminder cab-reminder-voice" id="cab-reminder">' +
          'Поступит входящий звонок с незнакомого номера. Ответьте на звонок — ' +
          'робот продиктует цифры кода.' +
        '</div>'
      : '<div class="cab-reminder cab-reminder-sms" id="cab-reminder">' +
          'Код придёт SMS-сообщением на номер ' + esc(displayTarget) + '.' +
        '</div>';

    state.recallReady = false;
    var html =
      '<div class="cab-screen card">' +
        '<h1 class="h1" id="cab-screen-title" tabindex="-1">Личный кабинет</h1>' +
        '<div class="sub">Авторизация</div>' +
        '<hr class="divider divider--md">' +
        '<div class="cab-sr" id="cab-live" aria-live="polite"></div>' +
        (state.reauth
          ? '<div class="note" id="cab-reauth-note">Сессия истекла — подтвердите номер ещё раз. ' +
              'Данные анкеты не потеряны.</div>'
          : '') +
        reminderHtml +
        '<div class="text" id="cab-lead">' + leadText + '</div>' +
        '<div class="note note--md" id="cab-send-status" aria-live="polite"></div>' +
        '<div id="cab-code-error"></div>' +
        '<div class="auth-body" id="cab-auth-body"></div>' +
      '</div>';

    setScreen(html);

    if (opts.initialState === 'retry_free') {
      renderSendFailureArea('retry_free');
    } else {
      renderCodeInputArea();
      if (opts.cooldown > 0) startResendCooldown(opts.cooldown);
      renderSendState(opts.initialState || 'sending');
      startSendStatusPolling();
    }
  }

  // Строка таймера «Отправить повторно через 0:NN» (макет m2) вместо надписи
  // на кнопке; по истечении — ссылки повтора/смены канала на этом же месте.
  function startResendCooldown(seconds) {
    var timerEl = $('#cab-timer');
    if (!timerEl) return;
    if (timers.cooldown) clearInterval(timers.cooldown);
    var left = Math.round(seconds);
    var web = isWeb();
    if (web && state.callsLeft === 0 && left > 0) {
      renderNextCallTime(timerEl, left);
      return;
    }
    // Веб-вход (8h-2, R55): «Позвонить повторно через …»; скринридеру —
    // только границы таймера (aria-live polite), не каждый тик.
    if (web) announce('Позвонить повторно можно через ' + fmtClock(left) + '.');

    function tick() {
      if (left <= 0) {
        clearInterval(timers.cooldown); timers.cooldown = null;
        renderResendReady(timerEl);
        return;
      }
      timerEl.innerHTML = '';
      timerEl.textContent = (web ? 'Позвонить повторно через ' : 'Отправить повторно через ') + fmtClock(left);
      left -= 1;
    }
    tick();
    timers.cooldown = setInterval(tick, 1000);
  }

  // В4 (план 2026-10-11-001): звонков в окне не осталось — вместо ссылки,
  // которая вела бы на 8g, время следующего звонка с сервера, без
  // посекундного отсчёта. Сторожевой таймер сверяется с целевым моментом
  // (фоновая вкладка тормозит интервалы) и в срок показывает «Позвонить ещё раз».
  function renderNextCallTime(timerEl, seconds) {
    var until = Date.now() + Math.round(seconds) * 1000;
    var at = new Date(until);
    var text = 'Позвонить повторно можно в ' + pad2(at.getHours()) + ':' + pad2(at.getMinutes());
    timerEl.innerHTML = '';
    timerEl.textContent = text;
    announce(text + '.');
    timers.cooldown = setInterval(function () {
      if (Date.now() < until) return;
      clearInterval(timers.cooldown); timers.cooldown = null;
      renderResendReady(timerEl);
    }, 1000);
  }

  function announce(text) {
    var live = $('#cab-live');
    if (!live) return;
    live.textContent = '';
    setTimeout(function () { if (live.isConnected) live.textContent = text; }, 50);
  }

  function renderResendReady(timerEl) {
    if (!timerEl) return;
    if (isWeb()) {
      // 8h-2б: таймер истёк — подсказка и повторный звонок
      state.recallReady = true;
      var statusEl = $('#cab-send-status');
      if (statusEl) statusEl.textContent = webNoAnswerText();
      timerEl.innerHTML = '<a class="cab-link-strong" id="cab-resend" href="#">Позвонить ещё раз</a>';
      announce('Можно позвонить ещё раз.');
      $('#cab-resend').addEventListener('click', function (e) {
        e.preventDefault();
        requestCode('voice', null, true);
      });
      return;
    }
    // Голосовая ветка: повторного звонка не существует (O5/R12) — после
    // отсчёта только переход на SMS. SMS-ветка: повтор SMS + одноразовая
    // «Получить код звонком» (гаснет по voiceUsed).
    if (state.channel === 'voice') {
      timerEl.innerHTML = '<a class="link" id="cab-switch-channel" href="#">Получить код в SMS</a>';
    } else {
      timerEl.innerHTML =
        '<a class="link" id="cab-resend" href="#">Отправить SMS ещё раз</a>' +
        (state.voiceUsed ? '' :
          ' · <a class="link" id="cab-switch-channel" href="#">Получить код звонком</a>');
    }
    var resendEl = $('#cab-resend');
    if (resendEl) resendEl.addEventListener('click', function (e) {
      e.preventDefault();
      requestCode(state.channel, null, true);
    });
    var switchEl = $('#cab-switch-channel');
    if (switchEl) switchEl.addEventListener('click', function (e) {
      e.preventDefault();
      requestCode(state.channel === 'voice' ? 'sms' : 'voice', null, true);
    });
  }

  // Опрос GET auth/send-status раз в 3–5 с (X-Cabinet-Token);
  // стоп на финальном состоянии или по TTL кода (5 минут).
  function startSendStatusPolling() {
    if (timers.poll) clearInterval(timers.poll);
    if (timers.ttl) clearTimeout(timers.ttl);

    timers.ttl = setTimeout(function () {
      if (timers.poll) { clearInterval(timers.poll); timers.poll = null; }
    }, CODE_TTL_MS);

    timers.poll = setInterval(function () {
      api('GET', '/cabinet/api/auth/send-status', null,
          { 'X-Cabinet-Token': state.token || '' })
        .then(function (data) {
          // Verify уже летит (код введён/автосабмит) — не ре-энейблим ячейки
          // результатами поллинга (renderSendFailureArea сбрасывает disabled);
          // следующий тик решит, когда verify завершится.
          if (verifyInFlight) return;
          if (data.state === 'retry_free' || data.state === 'expired') {
            clearInterval(timers.poll); timers.poll = null; // финальное состояние
            renderSendFailureArea(data.state);
          } else {
            renderSendState(data.state);
          }
        })
        .catch(function (err) {
          if (err.code === 'link_expired') {
            clearInterval(timers.poll); timers.poll = null;
            showLinkExpired();
          }
          // rate_limited/сеть: молча ждём следующего тика
        });
    }, SEND_STATUS_POLL_MS);
  }

  // send-status 'none' (техсостояние до первой отправки) — no-op по умолчанию.
  function renderSendState(sendState) {
    var statusEl = $('#cab-send-status');
    if (!statusEl) return;
    if (isWeb() && state.recallReady) return; // 2б: подсказка «Не дозвонились?» остаётся
    switch (sendState) {
      case 'sending':
        statusEl.textContent = 'Отправляем код…';
        break;
      case 'sent':
        statusEl.textContent = isWeb() && state.recall
          ? 'Код отправлен — ожидайте звонок или SMS.'
          : state.channel === 'voice'
            ? 'Звонок выполняется — ожидайте входящий вызов.'
            : 'SMS отправлено.';
        break;
      default:
        break;
    }
  }

  // Проверка кода
  function verifyCode(code) {
    if (verifyInFlight) return; // второй запуск (двойной автосабмит) не должен лететь параллельно
    verifyInFlight = true;
    var errBox = $('#cab-code-error');
    var confirmBtn = $('#cab-confirm');
    getCodeCells().forEach(function (c) { c.disabled = true; });
    if (confirmBtn) confirmBtn.disabled = true;
    if (errBox) errBox.innerHTML = '';

    api('POST', '/cabinet/api/auth/verify', { token: state.token, code: code })
      .then(function (data) {
        verifyInFlight = false;
        state.bearer = data.access_token;
        storageSet(BEARER_KEY, state.bearer);
        if (timers.poll) { clearInterval(timers.poll); timers.poll = null; }
        if (state.reauth) resumeAfterReauth();
        else loadCabinet();
      })
      .catch(function (err) {
        verifyInFlight = false;
        if (err.code === 'link_expired') { showLinkExpired(); return; }
        if (err.code === 'code_superseded' || err.code === 'code_expired') {
          // Мёртвый, но верно введённый код: бэкенд попытку НЕ тратит —
          // ячейки к чистому вводу, счётчик попыток не упоминаем.
          clearCodeCells();
          if (errBox) errBox.innerHTML = errorHtml(err);
          return;
        }
        if (err.code === 'invalid_code') {
          // 2a-2: красные ячейки + «Осталось N попыток» (attempts_left)
          markCodeCellsError();
          if (errBox) errBox.innerHTML = '<div class="note note--md note--error">' +
            esc(attemptsText(err.data && err.data.attempts_left)) + '</div>';
          return;
        }
        if (err.code === 'locked') {
          startLockCountdown((err.data && err.data.retry_after) || 1800);
          return;
        }
        getCodeCells().forEach(function (c) { c.disabled = false; });
        if (confirmBtn) confirmBtn.disabled = getCodeCellsValue().length !== 4;
        if (errBox) errBox.innerHTML = errorHtml(err);
      });
  }

  // 2a-3: блокировка — .code__cell--locked/.btn--disabled из макета, живой
  // отсчёт оставлен (богаче макета — решение ревизии).
  function startLockCountdown(seconds) {
    markCodeCellsLocked();
    var errBox = $('#cab-code-error');
    var left = seconds;
    if (timers.lock) clearInterval(timers.lock);

    function tick() {
      if (left <= 0) {
        clearInterval(timers.lock); timers.lock = null;
        clearCodeCells();
        if (errBox) errBox.innerHTML = '';
        return;
      }
      if (errBox) {
        errBox.innerHTML = '<div class="lead note--error">Слишком много неверных попыток. ' +
          'Запросите новый код через ' + esc(fmtSeconds(left)) + '.</div>';
      }
      left -= 1;
    }
    tick();
    timers.lock = setInterval(tick, 1000);
  }

  // ── ПОПАП: тексты согласия (п.3, 2026-08-31) — Policy/Agreement дилера ──
  // Mount в document.body по тем же причинам, что у #cab-popup (переживает
  // setScreen); стили — на id #cab-text-popup. Текст вставляется ТОЛЬКО
  // textContent (В2: разметка из NocoDB не должна исполняться), переносы
  // сохраняет white-space:pre-wrap. Закрытие: крестик, оверлей, Esc.
  // Esc-подписка самоудаляется, если узел мёртв (образец попапа авто).
  var textPopupEscHandler = null;
  var textPopupOpener = null; // возврат фокуса при закрытии (образец P2-1)

  function closeTextPopup() {
    if (textPopupEscHandler) {
      document.removeEventListener('keydown', textPopupEscHandler);
      textPopupEscHandler = null;
    }
    var el = document.getElementById('cab-text-popup');
    if (el && el.parentNode) el.parentNode.removeChild(el);
    if (textPopupOpener && textPopupOpener.isConnected && textPopupOpener.focus) {
      textPopupOpener.focus();
    }
    textPopupOpener = null;
  }

  function showTextPopup(title, text, opener) {
    closeTextPopup();
    textPopupOpener = opener || null;
    var div = document.createElement('div');
    div.id = 'cab-text-popup';
    div.className = 'popup popup--visible';
    div.setAttribute('role', 'dialog');
    div.setAttribute('aria-modal', 'true');
    div.innerHTML =
      '<div class="popup__overlay" id="cab-text-popup-overlay"></div>' +
      '<div class="popup__dialog">' +
        '<div class="popup__head">' +
          '<div class="popup__title">' + esc(title) + '</div>' +
          '<button class="popup__close" type="button" id="cab-text-popup-close" aria-label="Закрыть">&#10005;</button>' +
        '</div>' +
        '<div class="popup__body" id="cab-text-popup-body"></div>' +
      '</div>';
    // В2: только текстовый узел — HTML из полей NocoDB не исполняется
    div.querySelector('#cab-text-popup-body').textContent = text;
    document.body.appendChild(div);
    div.querySelector('#cab-text-popup-close').addEventListener('click', closeTextPopup);
    div.querySelector('#cab-text-popup-overlay').addEventListener('click', closeTextPopup);
    textPopupEscHandler = function (e) {
      if (e.key !== 'Escape') return;
      var p = document.getElementById('cab-text-popup');
      if (!p || !p.isConnected) { // узел мёртв — самоудаление подписки
        document.removeEventListener('keydown', textPopupEscHandler);
        textPopupEscHandler = null;
        return;
      }
      closeTextPopup();
    };
    document.addEventListener('keydown', textPopupEscHandler);
    div.querySelector('#cab-text-popup-close').focus();
  }

  // ── ПОПАП: 409 conflict (гонка формы) — компонент lk-source.html:1504–1514
  // (историческое: кадра в v3 нет, компонент и поведение сохранены) ──
  // МОНТИРУЕТСЯ В document.body ОСОЗНАННО: попап не зависит от текущего экрана
  // (root.innerHTML заменяется целиком setScreen) и должен пережить его смену.
  // Перенос внутрь #cabinet-root убьёт попап при первой же перерисовке.
  // Стили узел получает по id: CSS-правила .popup/.btn/.link несут параллельный
  // селектор #cab-popup (U4) — под #cabinet-root узел не находится.
  function closeConflictPopup() {
    var el = document.getElementById('cab-popup');
    if (el && el.parentNode) el.parentNode.removeChild(el);
  }
  function showConflictPopup(onRetry) {
    closeConflictPopup();
    var div = document.createElement('div');
    div.id = 'cab-popup';
    div.className = 'popup popup--visible';
    div.setAttribute('role', 'dialog');
    div.setAttribute('aria-modal', 'true');
    div.innerHTML =
      '<div class="popup__overlay"></div>' +
      '<div class="popup__dialog">' +
        '<div class="popup__title">Не удалось сохранить</div>' +
        '<div class="popup__text">Данные заявки изменились в другом окне. Попробуйте отправить ещё раз.</div>' +
        '<div class="popup__actions">' +
          '<button type="button" class="btn btn--sm" id="cab-popup-retry">Повторить</button>' +
          '<a class="link" id="cab-popup-close" href="#">Закрыть</a>' +
        '</div>' +
      '</div>';
    document.body.appendChild(div);
    div.querySelector('#cab-popup-retry').addEventListener('click', function () {
      closeConflictPopup();
      onRetry();
    });
    div.querySelector('#cab-popup-close').addEventListener('click', function (e) {
      e.preventDefault();
      closeConflictPopup();
    });
  }

  // ── 9. КАБИНЕТ (после verify): сценарная анкета, карточка авто, автосейв (U8) ──
  //
  // Состав анкеты — по сценарию profile.scenario_type (credit/tradein/combo);
  // неизвестный/пустой сценарий — полный набор блоков (фолбэк). Блок «условия
  // кредита» (ползунки) — НЕ этот юнит (U9): credit_initial/credit_term/
  // credit_payment здесь рендерятся ОБЫЧНЫМИ числовыми полями в renderCreditBlock —
  // U9 заменит содержимое этой функции моделью calc_model.js; combo_extra_down —
  // тоже обычное поле. Канонические строки плиток — ДОСЛОВНО из
  // cabinet_service.FORM_TILE_FIELDS (страж единства — tests/test_cabinet_frontend.py).

  // Подписи для «плоских» полей (текст/число). Плитки (TILE_GROUPS), комментарий
  // (textarea) и условия кредита (renderCreditBlock) рендерятся отдельно.
  var FORM_LABELS = {
    credit_initial: 'Первоначальный взнос, ₽',
    credit_term: 'Срок кредита, месяцев',
    credit_payment: 'Желаемый ежемесячный платёж, ₽',
    tradein_make: 'Марка',
    tradein_model: 'Модель',
    tradein_year: 'Год выпуска',
    tradein_mileage: 'Пробег, км',
    tradein_plate: 'Гос. номер',
    // combo_extra_down подписи не имеет: поле рендерится собственной разметкой
    // comboDownRowHtml («+ доплата … ₽»), не через fieldInputHtml (В6, v3)
  };
  // Состав — ровно FORM_INT_FIELDS сервера (cabinet_service.py).
  var FORM_INT_FIELDS = {
    credit_initial: 1, credit_term: 1, credit_payment: 1,
    tradein_year: 1, tradein_mileage: 1, combo_extra_down: 1,
  };

  // U9: поля, которыми при живом калькуляторе владеет CalcModel —
  // единственный путь записи mirrorCalcStateToFormValues (см. листенер #cab-form)
  var CALC_MANAGED_KEYS = {
    credit_initial: 1, credit_term: 1, credit_payment: 1, combo_extra_down: 1,
  };

  // ---------------------------------------------------------------
  // Канонические словари плиток — ДОСЛОВНО из cabinet_service.FORM_TILE_FIELDS
  // (U1). Единство пинится стражем tests/test_cabinet_frontend.py, читающим
  // оба файла.
  // ---------------------------------------------------------------
  var TILE_MARITAL_STATUS = [
    'Холост/не замужем', 'Женат/замужем', 'В разводе', 'Вдовец/вдова', 'Гражданский брак',
  ];
  var TILE_EDUCATION = [
    'Среднее', 'Среднее специальное', 'Незаконченное высшее',
    'Высшее', '2 и более высших', 'Учёная степень',
  ];
  var TILE_EMPLOYMENT = [
    'Работа по найму', 'Служба', 'Декретный отпуск', 'Работающий пенсионер',
    'Пенсионер', 'Частичная занятость', 'ИП',
  ];
  var TILE_TRADEIN_CONDITION = [
    'Отличное', 'Хорошее', 'Удовлетворительное', 'Требует ремонта', 'Не на ходу',
  ];
  var TILE_OWNERS_BY_PTS = ['1', '2', '3', '4 и более'];
  var TILE_PTS_TYPE = ['Оригинал', 'Дубликат', 'Электронный ПТС'];
  var TILE_KEYS_COUNT = ['1', '2', 'Более 2'];
  // Веб-вход U9 (8c/8e): руль вводит клиент, когда оценка не дала данных авто
  var TILE_TRADEIN_STEERING = ['Левый', 'Правый'];

  // Пояснения плиток тех. состояния — вёрстка макета (историч.
  // lk-source.html:868–886; в v3 те же плитки — кадры 4b/6);
  // не участвуют в валидации, только украшают карточку.
  var TRADEIN_CONDITION_DESC = {
    'Отличное': 'Без повреждений, все узлы и агрегаты исправны, кузовной ремонт не производился',
    'Хорошее': 'Технически исправен, возможны мелкие дефекты ЛКП и окраска отдельных элементов',
    'Удовлетворительное': 'Заметные следы эксплуатации, был кузовной ремонт, требуется мелкое обслуживание',
    'Требует ремонта': 'Есть неисправности узлов или повреждения кузова, передвигается своим ходом',
    'Не на ходу': 'Не заводится или не может двигаться самостоятельно',
  };

  var TILE_GROUPS = {
    marital_status: { title: 'Семейное положение', options: TILE_MARITAL_STATUS },
    education: { title: 'Образование', options: TILE_EDUCATION },
    employment: { title: 'Профессиональная деятельность', options: TILE_EMPLOYMENT },
    tradein_condition: { title: 'Общее техническое состояние', options: TILE_TRADEIN_CONDITION,
      desc: TRADEIN_CONDITION_DESC },
    owners_by_pts: { title: 'Количество владельцев по ПТС', options: TILE_OWNERS_BY_PTS },
    pts_type: { title: 'Оригинал ПТС', options: TILE_PTS_TYPE },
    keys_count: { title: 'Количество ключей', options: TILE_KEYS_COUNT },
    tradein_steering: { title: 'Руль', options: TILE_TRADEIN_STEERING },
  };
  var TILE_PERSONAL_KEYS = ['marital_status', 'education', 'employment'];
  // П.6 (решение владельца 2026-08-31): в сценарии «комбо» персональные плитки
  // (семейное положение/образование/занятость) скрыты от пользователя; вернуть —
  // поставить false. Код блоков не удаляется. В «кредите» плитки остаются.
  // Значения, заполненные в черновике до скрытия, не чистятся (В5) — уйдут в CRM.
  var HIDE_PERSONAL_TILES_IN_COMBO = true;
  var TILE_CAR_KEYS = ['tradein_condition', 'owners_by_pts', 'pts_type', 'keys_count'];

  // Тексты успеха по сценарию (R9/R10) — ДОСЛОВНО, отдельная константа на
  // сценарий: страж-тест сверяет их построчно.
  var SUCCESS_TEXT_CREDIT =
    'Специалисты дилерского центра вернутся к вам в ближайшее время с информацией по кредиту.';
  var SUCCESS_TEXT_TRADEIN =
    'Специалисты дилерского центра вернутся к вам в ближайшее время с предварительными условиями трейд-ина.';
  var SUCCESS_TEXT_COMBO =
    'Специалисты дилерского центра вернутся к вам в ближайшее время с предварительными условиями ' +
    'трейд-ина и информацией по кредиту.';
  var SUCCESS_TEXT_DEFAULT =
    'Специалисты дилерского центра свяжутся с вами в ближайшее время.';

  function successText(scenario) {
    if (scenario === 'credit') return SUCCESS_TEXT_CREDIT;
    if (scenario === 'tradein') return SUCCESS_TEXT_TRADEIN;
    if (scenario === 'combo') return SUCCESS_TEXT_COMBO;
    return SUCCESS_TEXT_DEFAULT;
  }

  // Обязательная подпись фото модели (R9/U8): каталожный рендер, не фото
  // машины клиента — без подписи клиент может решить, что оценили не ту машину.
  var OWN_PHOTO_CAPTION = 'Фото модели. Изображение носит справочный характер';

  var RU_MONTHS_GEN = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
  // «2 августа в 14:32» — русские месяцы, ЛОКАЛЬНОЕ время клиента (R10).
  function formatSubmittedAt(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    var mm = d.getMinutes();
    return d.getDate() + ' ' + RU_MONTHS_GEN[d.getMonth()] + ' в ' +
      d.getHours() + ':' + (mm < 10 ? '0' : '') + mm;
  }

  function formatThousands(v) {
    var n = parseInt(v, 10);
    if (isNaN(n)) return String(v == null ? '' : v);
    return n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
  }
  function formatMileage(v) { return formatThousands(v) + ' км'; }
  function formatMoney(v) { return formatThousands(v) + ' ₽'; }

  var STEERING_LABELS = { LEFT: 'Левый', RIGHT: 'Правый' };
  function steeringLabel(raw) {
    var key = String(raw == null ? '' : raw).toUpperCase();
    return STEERING_LABELS[key] || String(raw);
  }

  // ── Карточка авто: GET /cabinet/api/car (Bearer), скелетон на время запроса ──
  var CAR_SPEC_FIELDS = [
    ['engine', 'Двигатель'], ['transmission', 'Коробка'], ['drive', 'Привод'],
    ['acceleration', 'Разгон'], ['consumption', 'Расход'],
    // Шестой чип по порядку лендинга (u6-gallery.html:947); значение приходит
    // уже с размерностью («170 мм»), пустое — чип не рендерится (пропуск ниже).
    ['clearance', 'Клиренс'],
  ];
  var carPhotoIndex = 0;

  function currentCarName() {
    var me = state.me || {};
    var profile = me.profile || {};
    var calc = me.calc || null;
    return profile.brand_model || (calc && calc.item_name) || '';
  }

  // ── Шапка авто (макет v3): карточка-товар .car-pick + попап «Данные об
  // автомобиле» ──
  //
  // carState: undefined (карточка грузится) | null (данных нет) |
  // {name, specs, images, price_new}. Скелетона нет (решение Ж3, 2026-08-26):
  // имя известно сразу из /me, карточка видна с первого кадра; цена, фото и
  // кликабельность появляются только с данными /car.
  //
  // Ж4 пересмотрено владельцем (план 2026-09-26-001, кадр 7): после отправки
  // карточка ОСТАЁТСЯ кликабельной — попап работает в read-only.
  // Некликабельные состояния (Ж5) визуально едины (див без шеврона):
  // undefined, null, объект без единого чипа и фото.
  // Пустое имя при отсутствии данных — блока нет вовсе (Ж5).
  function carSpecChipsHtml(specs) {
    var chipsHtml = '';
    for (var i = 0; i < CAR_SPEC_FIELDS.length; i++) {
      var field = CAR_SPEC_FIELDS[i][0], label = CAR_SPEC_FIELDS[i][1];
      var val = specs[field];
      if (val == null || val === '') continue;
      chipsHtml += '<div class="chip"><div class="chip__value">' + esc(val) + '</div>' +
        '<div class="chip__label">' + esc(label) + '</div></div>';
    }
    return chipsHtml;
  }

  // Узел попапа рендерится в слоте #cab-car-popup-slot — СИБЛИНГ #cab-content
  // внутри .cab-screen (KTD2 плана 2026-09-26-001): submitted-затемнение
  // #cab-content.readonly (opacity:.55) не красит попап и не запирает его в
  // своём stacking context. Демонтаж прежний — setScreen сносит root целиком.
  // patchCarHead владеет обоими узлами (кнопкой и слотом); патч единственный
  // за жизнь экрана (undefined → объект/null), а до прихода данных кнопка
  // некликабельна (В7) — открытый попап не может попасть под патч by construction.
  function carPopupHtml(name, chipsHtml, images) {
    var photoHtml = '';
    if (images.length) {
      photoHtml =
        '<div class="photo" id="cab-car-photo" style="border-radius:12px">' +
          '<img class="cab-car-photo-img" id="cab-car-photo-img" src="' + esc(images[0]) + '" alt="">' +
          (images.length > 1
            // Нативные button (P2-2): Tab/Enter из коробки, без своих key-хендлеров
            ? '<button type="button" class="photo__nav photo__nav--prev" id="cab-car-prev" aria-label="Предыдущее фото">&lsaquo;</button>' +
              '<button type="button" class="photo__nav photo__nav--next" id="cab-car-next" aria-label="Следующее фото">&rsaquo;</button>'
            : '') +
        '</div>';
      if (images.length > 1) {
        var thumbsHtml = '';
        for (var t = 0; t < images.length; t++) {
          thumbsHtml += '<button type="button" class="thumb' + (t === 0 ? ' thumb--active' : '') +
            '" data-idx="' + t + '" aria-label="Фото ' + (t + 1) + '">' +
            '<img src="' + esc(images[t]) + '" alt=""></button>';
        }
        photoHtml += '<div class="thumbs" id="cab-car-thumbs" style="width:100%">' + thumbsHtml + '</div>';
      }
    }
    return '<div class="popup popup--wide" id="cab-car-popup" role="dialog" aria-modal="true" ' +
        'aria-label="Данные об автомобиле">' +
        '<div class="popup__overlay" id="cab-car-popup-overlay"></div>' +
        '<div class="popup__dialog">' +
          '<div class="popup__grabber"></div>' +
          '<div class="popup__head">' +
            '<div>' +
              '<div class="h3">Данные об автомобиле</div>' +
              // Ж5: при пустом имени строки подзаголовка нет вовсе (не пустой div)
              (name ? '<div class="popup__subtitle">' + esc(name) + '</div>' : '') +
            '</div>' +
            '<button class="popup__close" type="button" id="cab-car-popup-close" aria-label="Закрыть">&#10005;</button>' +
          '</div>' +
          (chipsHtml ? '<div class="chips" style="margin-top:12px">' + chipsHtml + '</div>' : '') +
          ((chipsHtml && photoHtml) ? '<hr class="divider" style="margin:22px 0">' : '') +
          photoHtml +
        '</div>' +
      '</div>';
  }

  function carHeadHtml(carState, carName) {
    var specs = (carState && carState.specs) || {};
    var images = (carState && carState.images) || [];
    var chipsHtml = carState ? carSpecChipsHtml(specs) : '';
    var hasContent = !!(chipsHtml || images.length); // Ж5: есть что показать
    var name = (carState && carState.name) || carName || '';
    // Цена карточки — price_new из /car (R16/KTD3 плана 2026-09-26-001):
    // розничная витринная цена, НЕ цена расчёта. Нет числа больше нуля —
    // строки цены нет вовсе (до ответа /car, при 404, при пустой колонке).
    var priceNew = (carState && typeof carState.price_new === 'number' &&
      carState.price_new > 0) ? carState.price_new : null;
    var interactive = hasContent; // Ж5; Ж4 пересмотрено — submitted кликабелен

    // Карточка-товар (v3 .s14/.s103): миниатюра images[0] или серый
    // плейсхолдер, имя, опциональная строка «от <цена>»
    var thumbHtml = images.length
      ? '<span class="car-pick__thumb"><img src="' + esc(images[0]) + '" alt=""></span>'
      : '<span class="car-pick__thumb car-pick__thumb--empty"></span>';
    var priceHtml = priceNew != null
      ? '<span class="car-pick__price"><span class="car-pick__price-from">от</span>' +
        '<span class="car-pick__price-sum">' + esc(formatMoney(priceNew)) + '</span></span>'
      : '';

    if (!interactive) {
      if (!name) return ''; // Ж5: ни имени, ни данных — блока нет
      // tabindex="-1": программный возврат фокуса при закрытии попапа
      // перерендером экрана (В8); таб-стопом див не становится.
      return '<div class="car-pick car-pick--static" id="cab-car-pick" tabindex="-1">' +
          thumbHtml +
          '<span class="car-pick__body">' +
            '<span class="car-pick__name">' + esc(name) + '</span>' + priceHtml +
          '</span>' +
        '</div>';
    }

    // Ж5: данные есть, а имени нет — нейтральная подпись вместо пустой строки
    var shownName = name || 'Выбранный автомобиль';
    // aria-label — замена удалённого текстового хинта для скринридера
    // (P2 ревью плана 2026-09-26-001): видимой разметки не меняет
    var ariaLabel = shownName +
      (priceNew != null ? ', от ' + formatMoney(priceNew) : '') +
      ' — смотреть данные об автомобиле';
    return '<button type="button" class="car-pick" id="cab-car-pick" ' +
        'aria-label="' + esc(ariaLabel) + '">' +
        thumbHtml +
        '<span class="car-pick__body">' +
          '<span class="car-pick__name">' + esc(shownName) + '</span>' + priceHtml +
        '</span>' +
        '<span class="car-pick__chevron">&rsaquo;</span>' +
      '</button>';
  }

  // Разметка попапа для слота #cab-car-popup-slot (KTD2): рендерится только
  // когда есть содержимое (чипы или фото) — в Ж5-состояниях слот пуст,
  // некликабельная карточка попап и не откроет.
  function carPopupSlotHtml(carState, carName) {
    var specs = (carState && carState.specs) || {};
    var images = (carState && carState.images) || [];
    var chipsHtml = carState ? carSpecChipsHtml(specs) : '';
    if (!chipsHtml && !images.length) return '';
    var name = (carState && carState.name) || carName || '';
    return carPopupHtml(name, chipsHtml, images);
  }

  // ── Открытие/закрытие попапа авто ──
  // Esc-подписка на document живёт ТОЛЬКО пока попап открыт; setScreen
  // document-подписки не чистит, поэтому хендлер самоудаляется, если узел
  // попапа уже мёртв (экран сменился при открытом попапе).
  var carPopupOpen = false;
  var carPopupWasOpenBeforeReload = false; // Ж4: снапшот на входе в loadCabinet
  var carPopupEscHandler = null;

  function openCarPopup() {
    var popup = $('#cab-car-popup');
    if (!popup) return;
    popup.classList.add('popup--visible');
    carPopupOpen = true;
    if (!carPopupEscHandler) {
      carPopupEscHandler = function (e) {
        if (e.key !== 'Escape') return;
        var p = document.getElementById('cab-car-popup');
        if (!p || !p.isConnected) { // узел мёртв — самоудаление подписки
          detachCarPopupEsc();
          carPopupOpen = false;
          return;
        }
        closeCarPopup(true);
      };
      document.addEventListener('keydown', carPopupEscHandler);
    }
    var closeBtn = $('#cab-car-popup-close'); // P2-1: фокус на крестик
    if (closeBtn) closeBtn.focus();
  }

  function detachCarPopupEsc() {
    if (carPopupEscHandler) {
      document.removeEventListener('keydown', carPopupEscHandler);
      carPopupEscHandler = null;
    }
  }

  // returnFocus — возврат фокуса на кнопку (P2-1) при закрытии пользователем;
  // при закрытии сменой экрана фокусом занимается showCabinetScreen (Ж4).
  function closeCarPopup(returnFocus) {
    detachCarPopupEsc();
    carPopupOpen = false;
    var popup = document.getElementById('cab-car-popup');
    if (popup) popup.classList.remove('popup--visible');
    if (returnFocus) {
      var pick = document.getElementById('cab-car-pick');
      if (pick && pick.focus) pick.focus();
    }
  }

  // Кнопка, попап и листание фото — без сети, чистый DOM. Клик-листенеры
  // живут на узлах #cab-car-head и пересоздаются вместе с ними (patchCarHead),
  // дублей нет; Esc — отдельно, только на время открытого попапа.
  function wireCarHeadEvents() {
    carPhotoIndex = 0;
    var pick = $('#cab-car-pick');
    var popup = $('#cab-car-popup');
    if (pick && popup) pick.addEventListener('click', openCarPopup);
    if (popup) {
      var closeBtn = $('#cab-car-popup-close');
      var overlay = $('#cab-car-popup-overlay');
      if (closeBtn) closeBtn.addEventListener('click', function () { closeCarPopup(true); });
      if (overlay) overlay.addEventListener('click', function () { closeCarPopup(true); });
    }

    var car = state.car;
    if (!car || !car.images || car.images.length < 2) return;
    var images = car.images;
    var prevBtn = $('#cab-car-prev'), nextBtn = $('#cab-car-next');
    var thumbs = $('#cab-car-thumbs');

    function show(idx) {
      carPhotoIndex = (idx % images.length + images.length) % images.length;
      var img = $('#cab-car-photo-img');
      if (img) img.src = images[carPhotoIndex];
      if (thumbs) {
        var items = thumbs.querySelectorAll('.thumb');
        for (var i = 0; i < items.length; i++) {
          items[i].classList.toggle('thumb--active', i === carPhotoIndex);
        }
      }
    }
    if (prevBtn) prevBtn.addEventListener('click', function () { show(carPhotoIndex - 1); });
    if (nextBtn) nextBtn.addEventListener('click', function () { show(carPhotoIndex + 1); });
    if (thumbs) {
      thumbs.addEventListener('click', function (e) {
        var el = e.target;
        while (el && el !== thumbs && (!el.classList || !el.classList.contains('thumb'))) {
          el = el.parentNode;
        }
        if (!el || el === thumbs) return;
        var idx = parseInt(el.getAttribute('data-idx'), 10);
        if (!isNaN(idx)) show(idx);
      });
    }
  }

  function patchCarHead() {
    var head = $('#cab-car-head');
    if (!head) return; // экран сменился, пока летел запрос
    head.innerHTML = carHeadHtml(state.car, currentCarName());
    // Слот попапа — сиблинг #cab-content (KTD2): патчится вместе с кнопкой
    var slot = $('#cab-car-popup-slot');
    if (slot) slot.innerHTML = carPopupSlotHtml(state.car, currentCarName());
    wireCarHeadEvents();
  }

  // Живая карточка авто по item_id снапшота; данных нет (404/сбой) → шапка без
  // чипов/фото, только название (профиль.brand_model или calc.item_name).
  function fetchCarCard() {
    // Гард поколения (образец requestCodeOutcomeStale): ответ, пришедший после
    // смены экрана (реавторизация/повторный логин) или разлогина, не применяем —
    // иначе он может патчить чужую отрисовку #cab-car-head тем же id.
    var seqAtRequest = screenSeq;
    function stale() { return screenSeq !== seqAtRequest || !state.bearer; }
    apiAuthed('GET', '/cabinet/api/car').then(function (data) {
      if (stale()) return;
      state.car = data;
      patchCarHead();
    }).catch(function () {
      if (stale()) return;
      state.car = null;
      patchCarHead();
    });
  }

  // ── Блок «Ваш автомобиль»: read-only из calc.found_info, либо (found_info
  // нет — блокер n8n) редактируемые tradein_* поля (текущее поведение) ──
  var FOUND_FACTS = [
    ['mark', 'Марка', null],
    ['model', 'Модель', null],
    ['year', 'Год', null],
    ['steering_wheel', 'Руль', steeringLabel],
    ['mileage', 'Пробег', formatMileage],
    ['plate', 'Гос. номер', null],
  ];

  // Пробег в карточке «Ваш автомобиль» — только введённый клиентом: с
  // лендинга или в анкете; пробег из ответа оценки не показывается (2026-10-03)
  function withClientMileage(found, values) {
    var merged = {};
    for (var fk in found) merged[fk] = found[fk];
    var prof = webProfile();
    var own = values && values.tradein_mileage != null && values.tradein_mileage !== ''
      ? values.tradein_mileage : prof.web_mileage;
    if (own == null || own === '') delete merged.mileage;
    else merged.mileage = own;
    return merged;
  }

  function renderOwnBlock(found, tiValuation, statusHtml) {
    var facts = '';
    for (var i = 0; i < FOUND_FACTS.length; i++) {
      var key = FOUND_FACTS[i][0], label = FOUND_FACTS[i][1], fmt = FOUND_FACTS[i][2];
      var raw = found[key];
      if (raw == null || raw === '') continue;
      var text = fmt ? fmt(raw) : String(raw);
      facts += '<div><div class="fact__label">' + esc(label) + '</div>' +
        '<div class="fact__value">' + esc(text) + '</div></div>';
    }
    var photoUrl = (found.photos && typeof found.photos === 'object') ? found.photos.main : null;
    var photoHtml = photoUrl
      ? '<div class="own__photo"><img src="' + esc(photoUrl) + '" alt=""></div>' +
        '<div class="note own__caption">' + esc(OWN_PHOTO_CAPTION) + '</div>'
      : '<div class="own__photo"><div class="own__empty">' +
          '<div class="own__empty-icon"></div><span class="own__empty-text">Фото недоступно</span>' +
        '</div></div>';
    // Обе подписи — ВНУТРИ серой карточки (приёмка U3b, 2026-08-27). Порядок
    // DOM: фото → подпись фото → факты → строка об источнике. На мобильном
    // (flex-column) это и видимый порядок — подпись фото остаётся между фото
    // и сеткой, как раньше; на десктопе order уводит обе подписи в зону на
    // всю ширину под фото и фактами (flex-wrap + flex-basis:100%).
    // Нет фото → нет и подписи фото: в зоне остаётся одна строка об источнике.
    // Оценка «~ N ₽» + пояснение — низ карточки над border-top (v3 .s52/.s147,
    // R5/R8 плана 2026-09-26-001). Источник — me.calc.ti_valuation, тот же,
    // что у калькулятора (В3); нет оценки (редкий сбойный случай) — строки нет.
    var flash = tiValuation != null && Date.now() < state.valFlashUntil;
    var valuationHtml = statusHtml ? statusHtml : tiValuation != null
      ? '<div class="own__valuation"' + (flash ? ' data-flash="1"' : '') + '>' +
          '<span class="own__valuation-sum">~ ' + esc(formatMoney(tiValuation)) + '</span>' +
          (flash ? '<span class="cab-ready">✓ Отчет готов</span>' : '') +
          '<span class="note">Расчёт по сделкам с похожими автомобилями. Финальная оценка — после осмотра.</span>' +
        '</div>'
      : '';
    return '<div class="own">' + photoHtml +
      '<div class="own__grid">' + facts + '</div>' +
      '<div class="note own__source">Данные получены на основе предоставленного номера автомобиля.</div>' +
      valuationHtml +
      '</div>';
  }

  // ── Веб-вход U9: «Ваш автомобиль» по статусу оценки (кадры 8a–8e) ──
  // Статус «none» (клиент Александра, оценки веб-входа нет) — прежний вид.
  function valuationStatus() {
    var st = (state.valuation && state.valuation.status) || 'none';
    if (st === 'pending' && state.valClientFailed) return 'failed'; // 8e на клиенте
    return st;
  }

  function webProfile() { return (state.me && state.me.profile) || {}; }

  // Госномер и пробег с лендинга предзаполняют пустые поля 8c/8e (R31) —
  // тихо, без автосейва: уходят с сабмитом вместе с остальными значениями.
  function prefillWebCarFields(values) {
    var prof = webProfile();
    if ((values.tradein_plate == null || values.tradein_plate === '') && prof.web_plate) {
      values.tradein_plate = prof.web_plate;
    }
    if ((values.tradein_mileage == null || values.tradein_mileage === '') && prof.web_mileage != null) {
      values.tradein_mileage = String(prof.web_mileage);
    }
    // 8c/8e (макет v4): руль по умолчанию «Левый»
    if (values.tradein_steering == null || values.tradein_steering === '') {
      values.tradein_steering = TILE_TRADEIN_STEERING[0];
    }
  }

  // Госномер в формате макета: «А123ВС 154» — пробел перед регионом
  function formatPlate(raw) {
    var v = String(raw == null ? '' : raw).replace(/\s+/g, '').toUpperCase();
    var m = v.match(/^([^\d]\d{3}[^\d]{2})(\d{2,3})$/);
    return m ? m[1] + ' ' + m[2] : String(raw == null ? '' : raw);
  }

  function factHtml(label, valueHtml) {
    return '<div><div class="fact__label">' + esc(label) + '</div>' +
      '<div class="fact__value">' + valueHtml + '</div></div>';
  }

  function managerNoteHtml() {
    // «Оценку проведёт менеджер» (макет 8c/8e, .w118): разделитель сверху
    return '<div class="cab-manager">' +
        '<div class="cab-status-title">Оценку проведёт менеджер</div>' +
        '<div class="note">После отправки заявки менеджер свяжется с вами, уточнит детали ' +
          'и договорится об осмотре.</div>' +
      '</div>';
  }

  // Поле авто 8c/8e (макет .w53/.w113): подпись 12 px, поле 52 px / 17 px
  function carInputCellHtml(key, label, placeholder, value, disabled, extraAttrs) {
    return '<div>' +
        '<label class="cab-car-label" for="cab-f-' + key + '">' + esc(label) + '</label>' +
        '<input class="cab-car-input" id="cab-f-' + key + '" data-key="' + key + '" type="text" ' +
          'placeholder="' + esc(placeholder) + '" value="' + esc(value == null ? '' : value) + '"' +
          (extraAttrs || '') + (disabled ? ' disabled' : '') + '>' +
      '</div>';
  }

  // Руль — два сегмента (макет .w68/.w114/.w115). Разметка плиток (label.tile +
  // radio) — общий листенер 'change' анкеты переключает tile--on.
  function steeringCellHtml(values, disabled) {
    var selected = values.tradein_steering;
    var opts = '';
    for (var i = 0; i < TILE_TRADEIN_STEERING.length; i++) {
      var opt = TILE_TRADEIN_STEERING[i];
      var on = selected === opt;
      var cls = disabled ? (on ? ' tile--ro-on' : ' tile--ro') : (on ? ' tile--on' : '');
      opts += '<label class="tile' + cls + '">' +
          '<input class="tile-radio" type="radio" name="tradein_steering" value="' + esc(opt) + '"' +
            (on ? ' checked' : '') + (disabled ? ' disabled' : '') + '>' +
          '<span class="tile__title">' + esc(opt) + '</span>' +
        '</label>';
    }
    return '<div id="cab-group-tradein_steering">' +
        '<div class="cab-car-label">Руль</div>' +
        '<div class="cab-seg" role="radiogroup" aria-label="Руль">' + opts + '</div>' +
      '</div>';
  }

  // Сетка полей авто: десктоп — 3 колонки (Марка / Модель / Год выпуска;
  // Руль / Пробег / Гос. номер), 390 — одна (макет .w112/.w121).
  function editableCarFieldsHtml(values, disabled, withMileagePlate) {
    var html = '<div class="cab-car-grid">' +
      carInputCellHtml('tradein_make', 'Марка', 'Например, NISSAN', values.tradein_make, disabled) +
      carInputCellHtml('tradein_model', 'Модель', 'Например, ALMERA', values.tradein_model, disabled) +
      carInputCellHtml('tradein_year', 'Год выпуска', 'ГГГГ', values.tradein_year, disabled,
        ' inputmode="numeric" maxlength="4"') +
      steeringCellHtml(values, disabled);
    if (withMileagePlate) {
      var mileage = values.tradein_mileage;
      html += '<div>' +
          '<label class="cab-car-label" for="cab-f-tradein_mileage">Пробег</label>' +
          '<div class="cab-car-unit">' +
            '<input id="cab-f-tradein_mileage" data-key="tradein_mileage" type="text" inputmode="numeric" ' +
              'value="' + esc(mileage == null || mileage === '' ? '' : formatThousands(mileage)) + '"' +
              (disabled ? ' disabled' : '') + '>' +
            '<span>км</span>' +
          '</div>' +
        '</div>' +
        carInputCellHtml('tradein_plate', 'Гос. номер', '', formatPlate(values.tradein_plate), disabled);
    }
    return html + '</div>';
  }

  function skeletonHtml(width) {
    return '<span class="own__sk" style="width:' + width + 'px" aria-hidden="true"></span>';
  }

  function webOwnBlockHtml(status, found, values, disabled, tiValuation) {
    var prof = webProfile();
    var plate = formatPlate(prof.web_plate || values.tradein_plate || '');
    if (status === 'pending') {
      // 8a: скелетоны марки/модели/года/руля, пробег и госномер с сайта,
      // часы + «около 1 мин» справа, бегущая полоса, пояснение (макет .w43)
      return '<div class="own">' +
          '<div class="own__photo"><div class="own__empty"><div class="own__empty-icon"></div>' +
            '<span class="own__empty-text">Фото появится после оценки</span></div></div>' +
          '<div class="own__grid">' +
            factHtml('Марка', skeletonHtml(90)) + factHtml('Модель', skeletonHtml(110)) +
            factHtml('Год', skeletonHtml(50)) + factHtml('Руль', skeletonHtml(70)) +
            (prof.web_mileage != null ? factHtml('Пробег', esc(formatMileage(prof.web_mileage))) : '') +
            (plate ? factHtml('Гос. номер', esc(plate)) : '') +
          '</div>' +
          '<div class="note own__source">Госномер и пробег — из заявки на сайте. ' +
            'Остальные данные подтянем по госномеру.</div>' +
          '<div class="own__status">' +
            '<div class="cab-wait-row"><span class="cab-clock" aria-hidden="true"><i></i><i></i></span>' +
              '<span class="cab-wait-title">Оцениваем автомобиль</span>' +
              '<span class="cab-wait-eta">около 1 мин</span></div>' +
            '<div class="cab-progress" aria-hidden="true"><span></span></div>' +
            '<div class="note">Подбираем похожие сделки — обычно это занимает не более 1 минуты. ' +
              'Пока можно заполнять анкету ниже: всё сохранится.</div>' +
          '</div>' +
        '</div>';
    }
    if (found) {
      // 8b/8d: госномер — из заявки, если оценка его не вернула; пробег —
      // только клиента (2026-10-03: Auto.ru отдаёт свой пробег, не наш)
      found = withClientMileage(found, values);
      found.plate = formatPlate(found.plate || plate);
    }
    if (status === 'success' && found) {
      return renderOwnBlock(found, tiValuation); // 8b
    }
    if (status === 'found_no_price' && found) {
      // 8d: карточка и «Оценка — после осмотра» (макет .w43/.w119/.w51)
      return renderOwnBlock(found, null,
        '<div class="own__status own__status--text">' +
          '<div class="cab-status-title">Оценка — после осмотра</div>' +
          '<div class="note">По этой модели пока мало похожих сделок, чтобы рассчитать сумму заранее. ' +
            'Менеджер оценит автомобиль при осмотре.</div>' +
        '</div>');
    }
    prefillWebCarFields(values);
    if (status === 'success') {
      // SUCCESS без found_info (R30): сумма и пояснение как в 8b + поля как в 8c
      return valuationSumHtml(tiValuation) + editableCarFieldsHtml(values, disabled, true);
    }
    // 8c (не найден) / 8e (ошибка, таймаут): сообщение, поля, «менеджер»
    var msg = status === 'not_found'
      ? 'Не нашли автомобиль по госномеру ' + plate + '. Укажите данные сами — оценку проведёт ' +
        'менеджер дилерского центра.'
      : 'Не удалось получить оценку по госномеру ' + plate + '. Укажите данные сами — оценку ' +
        'проведёт менеджер дилерского центра.';
    return '<div class="cab-msg" id="cab-own-msg">' + esc(msg) + '</div>' +
      editableCarFieldsHtml(values, disabled, true) +
      managerNoteHtml();
  }

  // Сумма оценки «~ N ₽» + пояснение (как в 8b) — для SUCCESS без found_info;
  // в момент прихода — подсветка и «✓ Отчет готов» 3 с.
  function valuationSumHtml(tiValuation) {
    if (tiValuation == null) return '';
    var flash = Date.now() < state.valFlashUntil;
    return '<div class="cab-val-box">' +
        '<span class="own__valuation-sum">~ ' + esc(formatMoney(tiValuation)) + '</span>' +
        (flash ? '<span class="cab-ready">✓ Отчет готов</span>' : '') +
        '<span class="note" style="flex-basis:100%">Расчёт по сделкам с похожими автомобилями. ' +
          'Финальная оценка — после осмотра.</span>' +
      '</div>';
  }

  function ownOrTradeinFieldsHtml(found, values, disabled, tiValuation) {
    var html = '<div class="cab-group">' + '<div class="group-title">Ваш автомобиль</div>';
    var vst = valuationStatus();
    if (vst !== 'none') {
      return html + '<div class="cab-fade-in">' +
        webOwnBlockHtml(vst, found, values, disabled, tiValuation) + '</div></div>';
    }
    if (found) {
      html += renderOwnBlock(withClientMileage(found, values), tiValuation);
    } else {
      html += fieldInputHtml('tradein_make', values.tradein_make, disabled) +
        fieldInputHtml('tradein_model', values.tradein_model, disabled) +
        fieldInputHtml('tradein_year', values.tradein_year, disabled) +
        fieldInputHtml('tradein_mileage', values.tradein_mileage, disabled) +
        fieldInputHtml('tradein_plate', values.tradein_plate, disabled);
    }
    return html + '</div>';
  }

  function commentBlockHtml(value, disabled) {
    return '<div class="cab-group">' +
        '<div>' +
          '<label class="label" for="cab-f-client_comment">Комментарий</label>' +
          '<textarea class="textarea" id="cab-f-client_comment" data-key="client_comment" rows="3" ' +
            'placeholder="Если данные указаны неверно или есть что добавить об автомобиле — напишите здесь"' +
            (disabled ? ' disabled' : '') + '>' + esc(value || '') + '</textarea>' +
        '</div>' +
      '</div>';
  }

  // ============================================================
  // U9: Условия кредита — живой блок на calc_model.js (R11, R16–R19).
  //
  // state.calcState — текущее состояние модели (initState/touch); undefined —
  // ещё не вычислено, null — деградация (R18): calc:null, initState вернул
  // no_calc/engine_mismatch, либо скрипты движка/модели не подключены тегами
  // (typeof-проверка в ensureCalcState). В деградации renderCreditBlock ведёт
  // себя как исходная (U8) реализация — три обычных числовых поля.
  // ============================================================

  // Границы шкал под подписи .bounds: в статусе impossible обе шкалы
  // схлопнуты в единственную достижимую точку (историч. кадр 2b-10 макета 2.0 —
  // «84 мес / 84 мес», а не глобальные границы 36/84; в v3 кадра для этого
  // состояния нет, поведение сохранено) — R16 «ползунки в единственной точке».
  function creditBoundsLabels(cs, axis) {
    if (cs.status === 'impossible') {
      var pinned = axis === 'term' ? cs.term : Math.round(cs.down);
      var label = axis === 'term' ? (pinned + ' мес') : formatMoney(pinned);
      return [label, label];
    }
    var b = cs.bounds[axis];
    return axis === 'term' ? [b.min + ' мес', b.max + ' мес'] : [formatMoney(b.min), formatMoney(b.max)];
  }

  // Поле-ползунок (срок/взнос) — ОДНА разметка на десктоп и мобильный (историч.
  // макет 1b #credit и m4 #mobile расходятся только видимостью частей через CSS;
  // в v3 — кадр 7a и моб. 4, раскладка слайдеров та же,
  // см. .cab-slider-title/.cab-bound-inline/.cab-slider-label-mobile/.bounds
  // выше и их переопределение в @media(max-width:600px)):
  //   десктоп  — slider-head: заголовок слева, значение справа; bounds
  //              (min/max) отдельной строкой ПОД ползунком;
  //   мобильный — крупный .label над блоком; slider-head--tap: min слева,
  //              значение по центру, max справа (bounds НАД ползунком).
  // Значение (slider-head__value) — общий элемент для обоих вариантов.
  function sliderFieldHtml(idBase, dataKey, marginTop, label, valueText,
      boundMin, boundMax, minAttr, maxAttr, stepAttr, curAttr, disabled, styleBg) {
    return '<div style="margin-top:' + marginTop + '">' +
        '<div class="label label--lg cab-slider-label-mobile" style="margin-bottom:0">' + esc(label) + '</div>' +
        '<div class="slider-head slider-head--tap">' +
          '<span class="text cab-slider-title">' + esc(label) + '</span>' +
          '<span class="note cab-bound-inline" id="' + idBase + '-bound-min-m">' + esc(boundMin) + '</span>' +
          '<span class="slider-head__value" id="' + idBase + '-value">' + esc(valueText) + '</span>' +
          '<span class="note cab-bound-inline" id="' + idBase + '-bound-max-m">' + esc(boundMax) + '</span>' +
        '</div>' +
        '<input class="slider slider--tap" type="range" id="cab-f-' + esc(dataKey) + '" data-key="' + esc(dataKey) + '" ' +
          'min="' + minAttr + '" max="' + maxAttr + '" step="' + stepAttr + '" value="' + curAttr + '"' +
          (styleBg ? ' style="--slider-bg:' + styleBg + '"' : '') + (disabled ? ' disabled' : '') + '>' +
        '<div class="bounds">' +
          '<span id="' + idBase + '-bound-min">' + esc(boundMin) + '</span>' +
          '<span id="' + idBase + '-bound-max">' + esc(boundMax) + '</span>' +
        '</div>' +
      '</div>';
  }

  // Кредит-режим (m4 макета): платёж — money__input, срок и взнос — нативные
  // input[type=range] с blocked-градиентом трека (--slider-bg, CSS custom
  // property — единственный способ докрасить ::-webkit/-moz-range track per-
  // инстансно, инлайн-style на самом <input> track-псевдоэлементы не видят).
  // Срок и взнос — рядом (.duo) на десктопе (историч. макет 1b #credit; в v3 —
  // кадр 7a, раскладка та же); мобильный —
  // друг под другом (media max-width:600px схлопывает .duo в одну колонку).
  function creditBlockHtml(cs, disabled) {
    var warn = cs.status === 'impossible';
    var termLabels = creditBoundsLabels(cs, 'term');
    var downLabels = creditBoundsLabels(cs, 'down');
    var minNoteHtml = warn
      ? esc('Минимально возможный платеж — ' + formatMoney(Math.round(cs.minPayment)) + '/мес.')
      : '';
    // KTD1: билдер возвращает ВНУТРЕННИЙ узел (без обёртки и заголовка) —
    // обёртка #cab-credit-wrap с group-title строится в buildFormBlocksHtml
    return '<div id="cab-credit-block" data-calc-mode="credit">' +
        '<div>' +
          '<div class="label label--lg">Желаемый ежемесячный платёж</div>' +
          '<div class="money money--lg' + (warn ? ' money--warn' : '') + '" id="cab-credit-payment-wrap">' +
            '<input class="money__input" id="cab-f-credit_payment" data-key="credit_payment" ' +
              'inputmode="numeric" value="' + esc(formatThousands(Math.round(cs.payment))) + '"' +
              (disabled ? ' disabled' : '') + '>' +
            '<span class="money__suffix">₽/мес</span>' +
          '</div>' +
          '<div class="note note--warn" id="cab-credit-min-note" style="margin-top:6px"' +
            (warn ? '' : ' hidden') + '>' + minNoteHtml + '</div>' +
        '</div>' +
        '<div class="duo" style="margin-top:28px">' +
          sliderFieldHtml('cab-credit-term', 'credit_term', 0, 'Срок кредита', cs.term + ' мес',
            termLabels[0], termLabels[1], cs.bounds.term.min, cs.bounds.term.max, cs.bounds.term.step,
            cs.term, disabled, sliderTrackGradient(cs.blocked.term)) +
          sliderFieldHtml('cab-credit-down', 'credit_initial', 0, 'Первоначальный взнос',
            formatMoney(Math.round(cs.down)), downLabels[0], downLabels[1], cs.bounds.down.min,
            cs.bounds.down.max, cs.bounds.down.step, Math.round(cs.down), disabled,
            sliderTrackGradient(cs.blocked.down)) +
        '</div>' +
      '</div>';
  }

  // Комбо-режим (R17): взнос = зачёт + доплата, ползунка взноса нет — фиксирован
  // (строка взноса comboDownRowHtml в обёртке #cab-credit-wrap). Свободен
  // только срок; платёж следует за ним. no_credit_needed — пояснение вместо
  // платежа/срока (зачёт покрывает стоимость целиком); строка взноса при этом
  // остаётся видимой (В9) — путь уменьшения доплаты обратно.
  // KTD1: оба варианта возвращают ВНУТРЕННИЙ узел без обёртки и заголовка.
  function comboCreditBlockHtml(cs, disabled) {
    if (cs.status === 'no_credit_needed') {
      return '<div id="cab-credit-block" data-calc-mode="combo">' +
          '<div class="note" id="cab-credit-no-credit-note">' +
            'Кредит не требуется — зачёт покрывает стоимость</div>' +
        '</div>';
    }
    return '<div id="cab-credit-block" data-calc-mode="combo">' +
        '<div>' +
          '<div class="label label--lg">Желаемый ежемесячный платёж</div>' +
          '<div class="money money--lg" id="cab-credit-payment-wrap">' +
            '<input class="money__input" id="cab-f-credit_payment" data-key="credit_payment" ' +
              'inputmode="numeric" value="' + esc(formatThousands(Math.round(cs.payment))) + '"' +
              (disabled ? ' disabled' : '') + '>' +
            '<span class="money__suffix">₽/мес</span>' +
          '</div>' +
        '</div>' +
        sliderFieldHtml('cab-credit-term', 'credit_term', 26, 'Срок кредита', cs.term + ' мес',
          cs.bounds.term.min + ' мес', cs.bounds.term.max + ' мес', cs.bounds.term.min, cs.bounds.term.max,
          cs.bounds.term.step, cs.term, disabled, null) +
      '</div>';
  }

  // Деградация (R18): calc отсутствует/невалиден, initState вернул no_calc/
  // engine_mismatch, либо calc_engine.browser.js/calc_model.js не подключены
  // тегами (скрипт может отсутствовать целиком) — три обычных числовых поля,
  // как было до U9; блока ползунков нет.
  function degradedCreditBlockHtml(values, disabled) {
    // KTD1: внутренний узел; вертикальную раскладку полей даёт CSS-правило
    // #cab-credit-block (flex-колонка, как .cab-group)
    return '<div id="cab-credit-block" data-calc-mode="degraded">' +
        fieldInputHtml('credit_payment', values.credit_payment, disabled) +
        fieldInputHtml('credit_term', values.credit_term, disabled) +
        fieldInputHtml('credit_initial', values.credit_initial, disabled) +
      '</div>';
  }

  function renderCreditBlock(values, disabled) {
    var cs = state.calcState;
    if (!cs) return degradedCreditBlockHtml(values, disabled);
    return cs.mode === 'combo' ? comboCreditBlockHtml(cs, disabled) : creditBlockHtml(cs, disabled);
  }

  // Инициализация/восстановление состояния модели из calc-снапшота /me (U7).
  // typeof-проверки — скрипты движка/модели могут не быть подключены тегами
  // (нет <script> на Tilda-странице): обращение к CalcModel напрямую тогда
  // бросило бы ReferenceError вместо управляемой деградации.
  // U9 (план 2026-09-30-001): комбо — всегда один взнос и цена combo_price
  // (R20/R41, оба входа); платёж с лендинга прижимается к границам без
  // «невозможно» (R22, только веб-вход).
  function calcOpts() {
    var profile = (state.me && state.me.profile) || {};
    return { combo: profile.scenario_type === 'combo', clampTarget: isWeb() };
  }

  function ensureCalcState(calc) {
    if (typeof CalcModel === 'undefined' || typeof CalcModel.initState !== 'function') {
      return null;
    }
    var result;
    try {
      result = CalcModel.initState(calc || null, calcOpts());
    } catch (e) {
      console.error('calc_model: initState бросил исключение — деградация в ручной ввод', e);
      return null;
    }
    if (!result) return null;
    if (result.status === 'engine_mismatch') {
      console.error('calc_model: несовпадение версии движка (ожидали ' + result.expected +
        ', загружена ' + result.actual + ') — блок условий кредита деградирует в ручной ввод');
      return null;
    }
    if (result.status === 'no_calc') return null;
    return result;
  }

  // Read-only после отправки (R10): восстанавливаем состояние модели из уже
  // отправленных значений формы, чтобы ползунки визуально стояли там же, где
  // их зафиксировал клиент, а не сбрасывались в исходную точку снапшота.
  // Приближённо (порядок touch может дать не пиксель-в-пиксель то же самое),
  // но детерминировано и без сети.
  function restoreCalcStateFromForm(calc, values) {
    var base = ensureCalcState(calc);
    if (!base) return null;
    try {
      if (base.mode === 'combo') {
        var extra = calcParseNum(values.combo_extra_down) || 0;
        var afterExtra = CalcModel.touch(base, 'extraDown', extra) || base;
        var comboTerm = calcParseNum(values.credit_term);
        if (comboTerm != null && afterExtra.status !== 'no_credit_needed') {
          return CalcModel.touch(afterExtra, 'term', comboTerm) || afterExtra;
        }
        return afterExtra;
      }
      var termVal = calcParseNum(values.credit_term);
      var withTerm = termVal != null ? (CalcModel.touch(base, 'term', termVal) || base) : base;
      var paymentVal = calcParseNum(values.credit_payment);
      if (paymentVal != null && isWeb() && typeof CalcModel.minPayment === 'function') {
        // R22: платёж с лендинга (предзаполнен из профиля) ниже минимума —
        // прижимаем к минимуму, статус «невозможно» не показываем
        var minPayment = CalcModel.minPayment(withTerm);
        if (minPayment != null && paymentVal < minPayment) paymentVal = minPayment;
      }
      return paymentVal != null ? (CalcModel.touch(withTerm, 'payment', paymentVal) || withTerm) : withTerm;
    } catch (e) {
      console.error('calc_model: восстановление read-only состояния не удалось — покажем базовое', e);
      return base;
    }
  }

  // Пересчитывает state.calcState при каждой загрузке /me: submitted — держим
  // уже вычисленное в памяти состояние (переживает re-OTP без потери позиции
  // ползунков), либо восстанавливаем приближённо из отправленных значений
  // (перезагрузка страницы после отправки); черновик — свежая инициализация
  // из calc-снапшота (initState читает target_payment снапшота сам).
  // Есть ли переживший reload/re-OTP черновик тройки калькулятора в formValues
  // (PUT /form уже сохранил его на сервере, но локальный calcState обнулился).
  function hasCalcDraftValues() {
    for (var k in CALC_MANAGED_KEYS) {
      if (state.formValues[k] != null && state.formValues[k] !== '') return true;
    }
    return false;
  }

  function updateCalcState(me) {
    if (state.submitted) {
      if (state.calcState === undefined) {
        state.calcState = restoreCalcStateFromForm(me.calc || null, state.formValues);
      }
      return;
    }
    // Черновик (не submitted): при наличии сохранённых значений — восстановление
    // тем же путём, что для submitted (restoreCalcStateFromForm), но контролы
    // остаются интерактивными (блокировка — только от state.submitted). Так
    // показанная тройка не расходится с той, что реально уйдёт в submitForm.
    if (hasCalcDraftValues()) {
      var restored = restoreCalcStateFromForm(me.calc || null, state.formValues);
      if (restored) {
        state.calcState = restored;
        // Приведение при открытии (шаг срока 12 мес, решение 2026-10-01):
        // срок/взнос/платёж черновика, сдвинутые моделью, записываются в
        // черновик — экран и сабмит ссылаются на одну тройку; showCabinetScreen
        // досохраняет грязные ключи
        var before = [state.formValues.credit_term, state.formValues.credit_initial,
                      state.formValues.credit_payment, state.formValues.combo_extra_down].join('|');
        applyCalcStateToFormValues(restored);
        var after = [state.formValues.credit_term, state.formValues.credit_initial,
                     state.formValues.credit_payment, state.formValues.combo_extra_down].join('|');
        if (before !== after) {
          for (var ck in CALC_MANAGED_KEYS) state.formDirty[ck] = true;
        }
        return;
      }
    }
    state.calcState = ensureCalcState(me.calc || null);
    // Восстановление недоступно (нет черновика/деградация) — свежая
    // инициализация из снапшота; зеркалим её в formValues БЕЗ markDirty, чтобы
    // экран и submit ссылались на одну и ту же тройку без ложного autosave.
    if (state.calcState) applyCalcStateToFormValues(state.calcState);
  }

  // Запись тройки модели в formValues — общая часть mirrorCalcStateToFormValues
  // (dirty+save) и тихой синхронизации при инициализации (без markDirty, см.
  // updateCalcState): сама запись значений не должна дублироваться в двух местах.
  function applyCalcStateToFormValues(cs) {
    state.formValues.credit_term = String(cs.term);
    state.formValues.credit_initial = String(Math.round(cs.down));
    if (cs.mode === 'combo') {
      state.formValues.combo_extra_down = String(cs.extraDown || 0);
    }
    state.formValues.credit_payment = cs.status === 'no_credit_needed'
      ? '0' : String(Math.round(cs.payment));
  }

  // Зеркалирование тройки модели в dirty-поля черновика (R19, п.5 задания):
  // существующий автосейв подхватывает их как любое другое поле формы.
  function mirrorCalcStateToFormValues(cs) {
    applyCalcStateToFormValues(cs);
    markDirtyAndSave('credit_term');
    markDirtyAndSave('credit_initial');
    if (cs.mode === 'combo') markDirtyAndSave('combo_extra_down');
    markDirtyAndSave('credit_payment');
  }

  // ── U9: обработчики ползунков/полей условий кредита — БЕЗ СЕТИ (R11) ──
  // Расчёт при движении ползунка/вводе платежа идёт полностью локально через
  // CalcModel.touch — до маркера конца ни один обработчик не должен вызывать
  // fetch/api/apiAuthed (тесты пинуют это грепом по границам блока).

  // Обновляет ОБА набора подписей границ ползунка — десктопные (.bounds под
  // ползунком) и мобильные inline-дубли (внутри slider-head--tap) — см.
  // sliderFieldHtml выше: один и тот же idBase, два элемента на каждую границу.
  function setBoundText(idBase, minText, maxText) {
    var pairs = [
      [idBase + '-bound-min', minText], [idBase + '-bound-max', maxText],
      [idBase + '-bound-min-m', minText], [idBase + '-bound-max-m', maxText],
    ];
    for (var i = 0; i < pairs.length; i++) {
      var el = $('#' + pairs[i][0]);
      if (el) el.textContent = pairs[i][1];
    }
  }

  function patchCreditBlockDom(cs) {
    var wrap = $('#cab-credit-payment-wrap');
    var paymentEl = $('#cab-f-credit_payment');
    var minNote = $('#cab-credit-min-note');
    var termVal = $('#cab-credit-term-value');
    var termEl = $('#cab-f-credit_term');
    var downVal = $('#cab-credit-down-value');
    var downEl = $('#cab-f-credit_initial');
    var warn = cs.status === 'impossible';

    if (wrap) wrap.classList.toggle('money--warn', warn);
    if (paymentEl) paymentEl.value = formatThousands(Math.round(cs.payment));
    if (minNote) {
      minNote.hidden = !warn;
      minNote.textContent = warn
        ? 'Минимально возможный платеж — ' + formatMoney(Math.round(cs.minPayment)) + '/мес.'
        : '';
    }
    var termLabels = creditBoundsLabels(cs, 'term');
    if (termVal) termVal.textContent = cs.term + ' мес';
    setBoundText('cab-credit-term', termLabels[0], termLabels[1]);
    if (termEl) { termEl.value = cs.term; termEl.style.setProperty('--slider-bg', sliderTrackGradient(cs.blocked.term)); }

    var downLabels = creditBoundsLabels(cs, 'down');
    if (downVal) downVal.textContent = formatMoney(Math.round(cs.down));
    setBoundText('cab-credit-down', downLabels[0], downLabels[1]);
    if (downEl) { downEl.value = Math.round(cs.down); downEl.style.setProperty('--slider-bg', sliderTrackGradient(cs.blocked.down)); }
  }

  function patchComboBlockDom(cs) {
    var paymentEl = $('#cab-f-credit_payment');
    var termVal = $('#cab-credit-term-value');
    var termEl = $('#cab-f-credit_term');
    if (paymentEl) paymentEl.value = formatThousands(Math.round(cs.payment));
    if (termVal) termVal.textContent = cs.term + ' мес';
    if (termEl) termEl.value = cs.term;
  }

  // Единая точка входа прикосновения: touch → зеркалирование в черновик →
  // патч DOM. Комбо-режим может переключить no_credit_needed ⇄ ok — тогда
  // структура #cab-credit-block меняется (появляется/пропадает срок/платёж) —
  // единственный случай, когда мы пересоздаём разметку блока и перевешиваем
  // payment/term (extraDown живёт вне #cab-credit-block и не трогается).
  function applyCalcTouch(field, raw) {
    var v = calcParseNum(raw);
    if (v === null || !state.calcState) return;
    if (field === 'payment' || field === 'term') state.paymentTouched = true;
    var next;
    try {
      next = CalcModel.touch(state.calcState, field, v);
    } catch (e) {
      console.error('calc_model: touch бросил исключение — блок условий кредита деградирует', e);
      next = null;
    }
    if (!next || next.status === 'engine_mismatch' || next.status === 'no_calc') {
      console.error('calc_model: touch вернул недопустимый статус в рантайме (' +
        (next && next.status) + ') — блок условий кредита деградирует');
      state.calcState = null;
      showCabinetScreen();
      return;
    }
    var wasNoCreditNeeded = state.calcState.status === 'no_credit_needed';
    state.calcState = next;
    mirrorCalcStateToFormValues(next);
    if (next.mode === 'combo' && wasNoCreditNeeded !== (next.status === 'no_credit_needed')) {
      var block = $('#cab-credit-block');
      if (block) block.outerHTML = comboCreditBlockHtml(next, false);
      wireCreditControls();
      return;
    }
    if (next.mode === 'combo') patchComboBlockDom(next); else patchCreditBlockDom(next);
  }

  // Вешает обработчики на payment/срок/взнос — вызывается и при первичном
  // монтировании кабинета, и после пересоздания разметки блока (переключение
  // no_credit_needed в комбо).
  function wireCreditControls() {
    if (!state.calcState || state.submitted) return;
    var paymentEl = $('#cab-f-credit_payment');
    var termEl = $('#cab-f-credit_term');
    var downEl = $('#cab-f-credit_initial'); // только кредит-режим (в комбо взнос фиксирован)
    if (paymentEl) wireDebouncedNumberInput(paymentEl, function (raw) { applyCalcTouch('payment', raw); });
    if (termEl) termEl.addEventListener('input', function () { applyCalcTouch('term', termEl.value); });
    if (downEl) downEl.addEventListener('input', function () { applyCalcTouch('down', downEl.value); });
  }

  // Доплата (комбо) вешается один раз здесь, а не в wireCreditControls:
  // её элемент живёт ВНЕ пересобираемого #cab-credit-block — в строке взноса
  // comboDownRowHtml внутри стабильной обёртки #cab-credit-wrap (KTD1) — и не
  // пересоздаётся при переключении no_credit_needed — повторное навешивание
  // в wireCreditControls задвоило бы обработчик.
  function wireCreditBlock() {
    if (!state.calcState || state.submitted) return;
    if (state.calcState.mode === 'combo') {
      var extraDownEl = $('#cab-f-combo_extra_down');
      if (extraDownEl) {
        wireDebouncedNumberInput(extraDownEl, function (raw) { applyCalcTouch('extraDown', raw); });
      }
    }
    wireCreditControls();
  }
  // ── КОНЕЦ БЛОКА БЕЗ СЕТИ (U9) ──

  // Строка «Первоначальный взнос» комбо (v3 .s64/.s65, R7 плана 2026-09-26-001):
  // read-only зачёт (ti_valuation снапшота) + поле «+ доплата». Живёт ВНЕ
  // пересобираемого #cab-credit-block, внутри обёртки #cab-credit-wrap (KTD1) —
  // обработчик touch('extraDown') вешается один раз в wireCreditBlock.
  // Формат суммы — «~ N ₽»; без оценки — «—» (В3). Инпут обязан нести
  // data-key="combo_extra_down" (автосейв делегированным листенером #cab-form
  // при mode!=='combo'), inputmode="numeric" и класс money__input (подсветка
  // 422 существующим правилом .money__input.input--error).
  // Веб-вход U9 (8a–8e): строка зачёта есть во всех состояниях, со статусом.
  var COMBO_DOWN_NOTE_DEFAULT =
    'Взнос складывается из оценки вашего автомобиля и доплаты — её можно не указывать.';
  // [статус, пояснение десктоп, пояснение 390] — дословно из макета v4
  var COMBO_DOWN_BY_STATUS = {
    pending: ['оцениваем…',
      'Сумма зачёта появится после оценки. Доплату, платёж и срок можно указать уже сейчас.',
      'Сумма зачёта появится после оценки. Доплату можно указать уже сейчас.'],
    not_found: ['после осмотра',
      'Сумму зачёта назовёт менеджер после осмотра. Кредит рассчитаем на сумму доплаты — её можно не указывать.',
      'Сумму зачёта назовёт менеджер после осмотра. Кредит рассчитаем на сумму доплаты.'],
    found_no_price: ['после осмотра',
      'Сумма зачёта станет известна после осмотра. Кредит рассчитаем на сумму доплаты — её можно не указывать.',
      'Сумма зачёта станет известна после осмотра.'],
    failed: ['не получен',
      'Можно отправить заявку без зачёта — менеджер уточнит сумму. Кредит рассчитаем на сумму доплаты.',
      'Можно отправить заявку без зачёта — менеджер уточнит сумму.'],
  };

  function comboDownRowHtml(tiValuation, extraDownValue, disabled) {
    var amountText = tiValuation != null ? '~ ' + formatMoney(tiValuation) : '—';
    var noteHtml = esc(COMBO_DOWN_NOTE_DEFAULT);
    var vst = valuationStatus();
    var byStatus = COMBO_DOWN_BY_STATUS[vst];
    var statusHtml = null;
    if (byStatus && !(vst === 'failed' && tiValuation)) {
      statusHtml = '<span class="cab-down-status" id="cab-down-sum">' +
        (vst === 'pending' ? '<span class="cab-clock cab-clock--muted" aria-hidden="true"><i></i><i></i></span>' : '') +
        esc(byStatus[0]) + '</span>';
      noteHtml = '<span class="cab-only-desk">' + esc(byStatus[1]) + '</span>' +
        '<span class="cab-only-mob">' + esc(byStatus[2]) + '</span>';
    }
    var flash = vst === 'success' && tiValuation != null && Date.now() < state.valFlashUntil;
    var val = extraDownValue == null ? '' : String(extraDownValue);
    return '<div class="cab-down-row">' +
        '<div class="label label--lg" style="margin-bottom:0">Первоначальный взнос</div>' +
        '<div class="cab-down-grid">' +
          '<div class="cab-down-readout">' +
            '<span class="note note--md">Зачёт вашего автомобиля</span>' +
            (statusHtml || ('<span class="cab-down-sum" id="cab-down-sum"' + (flash ? ' data-flash="1"' : '') + '>' +
              esc(amountText) + '</span>')) +
          '</div>' +
          '<div class="money money--lg">' +
            '<span class="money__prefix">+ доплата</span>' +
            '<input class="money__input cab-down-extra" id="cab-f-combo_extra_down" ' +
              'data-key="combo_extra_down" inputmode="numeric" placeholder="0" ' +
              'value="' + esc(val) + '"' + (disabled ? ' disabled' : '') + '>' +
            '<span class="money__suffix">₽</span>' +
          '</div>' +
        '</div>' +
        '<div class="note" id="cab-down-note">' + noteHtml + '</div>' +
      '</div>';
  }

  function tileGroupHtml(key, values, disabled) {
    var group = TILE_GROUPS[key];
    var selected = values[key];
    var optionsHtml = '';
    for (var i = 0; i < group.options.length; i++) {
      var opt = group.options[i];
      var checked = selected === opt;
      var stateClass = disabled ? (checked ? ' tile--ro-on' : ' tile--ro') : (checked ? ' tile--on' : '');
      var descHtml = (group.desc && group.desc[opt])
        ? '<span class="tile__desc">' + esc(group.desc[opt]) + '</span>' : '';
      optionsHtml += '<label class="tile tile--tap' + stateClass + '">' +
          '<input class="tile-radio" type="radio" name="' + esc(key) + '" value="' + esc(opt) + '"' +
            (checked ? ' checked' : '') + (disabled ? ' disabled' : '') + '>' +
          '<span class="tile__title">' + esc(opt) + '</span>' + descHtml +
        '</label>';
    }
    // Десктоп (историч. макет 1b/1c; в v3 плитки те же — кадры 4b/6): группы с
    // описанием (tradein_condition) — tiles--2 (карточки шире, под текст
    // описания), остальные — tiles--4; мобильный (историч. m4/m5/m6; в v3 —
    // моб. 5/6) — один столбец, переключение через media (max-width:600px).
    var gridClass = group.desc ? 'tiles tiles--2' : 'tiles tiles--4';
    return '<div class="cab-group" id="cab-group-' + esc(key) + '">' +
        '<div class="group-title">' + esc(group.title) + '</div>' +
        '<div class="' + gridClass + '" role="radiogroup" aria-label="' + esc(group.title) + '">' +
          optionsHtml +
        '</div>' +
        '<div class="note note--error" id="cab-group-' + esc(key) + '-err" hidden>Выберите вариант</div>' +
      '</div>';
  }

  function fieldInputHtml(key, value, disabled) {
    var isInt = !!FORM_INT_FIELDS[key];
    var label = FORM_LABELS[key] || key;
    var val = value == null ? '' : String(value);
    return '<div class="cab-field">' +
        '<label class="label" for="cab-f-' + esc(key) + '">' + esc(label) + '</label>' +
        '<input class="input" id="cab-f-' + esc(key) + '" data-key="' + esc(key) + '" ' +
          'type="text" ' + (isInt ? 'inputmode="numeric" ' : '') +
          'value="' + esc(val) + '"' + (disabled ? ' disabled' : '') + '>' +
      '</div>';
  }

  // Подзаголовок сценария в шапке (макет v3: кадры 7a/4b/4c, моб. 3/6/6b).
  // Неизвестный/пустой сценарий — «Анкета»: запасной вариант на сбой (В2,
  // план 2026-09-26-001), в рабочем процессе не возникает.
  function scenarioSubtitle(scenario) {
    if (scenario === 'credit') return 'Покупка в кредит';
    if (scenario === 'tradein') return 'Покупка в трейд-ин';
    if (scenario === 'combo') return 'Покупка в кредит и трейд-ин';
    return 'Анкета';
  }

  // Серые плашки-фразы (макет v3, тексты дословно — сверяются посимвольно
  // страж-тестами). В PHRASE_TRADEIN между «рассчитаем» и «условия» —
  // НЕРАЗРЫВНЫЙ пробел (U+00A0, &nbsp; макета), «приема» — без «ё», как в v3.
  var PHRASE_CREDIT = 'Расскажите немного о себе, и мы подберем кредит на подходящих условиях.';
  var PHRASE_TRADEIN = 'Расскажите о своём автомобиле, и мы рассчитаем условия его приема в зачёт.';
  var PHRASE_COMBO = 'Расскажите о своём автомобиле, и мы рассчитаем трейд-ин и кредит на новую машину.';

  function phraseHtml(text) {
    return '<div class="cab-phrase">' + esc(text) + '</div>';
  }

  var KNOWN_SCENARIOS = { credit: 1, tradein: 1, combo: 1 };

  // Состав блоков по сценарию (R9, порядок — макет v3): credit — условия
  // кредита·плашка·личные плитки (7a); tradein — «Ваш автомобиль»·комментарий·
  // плашка·плитки авто (4b); combo — «Ваш автомобиль»·комментарий·условия
  // кредита(+строка взноса)·плашка·плитки авто, личные скрыты (4c).
  // Неизвестный/пустой сценарий — полный набор в прежнем порядке (В2,
  // запасной вариант). Обёртка «Условия кредита» (KTD1) строится здесь —
  // единая точка для всех сценариев с кредит-блоком; строка взноса — только
  // при showComboReadout.
  function buildFormBlocksHtml(scenario, values, calc, disabled) {
    var found = (calc && calc.found_info && typeof calc.found_info === 'object' &&
      Object.keys(calc.found_info).length > 0) ? calc.found_info : null;
    var tiValuation = calc ? calc.ti_valuation : null;
    var isKnown = !!KNOWN_SCENARIOS[scenario];

    var showCredit = !isKnown || scenario === 'credit' || scenario === 'combo';
    var showTradein = !isKnown || scenario === 'tradein' || scenario === 'combo';
    var showPersonal = !isKnown || scenario === 'credit' ||
      (scenario === 'combo' && !HIDE_PERSONAL_TILES_IN_COMBO);
    var showComboReadout = scenario === 'combo' || (!isKnown && tiValuation != null);

    // Обёртка «Условия кредита» (KTD1): заголовок + [строка взноса] + внутренний
    // пересобираемый #cab-credit-block. Строка взноса и заголовок стабильны —
    // outerHTML-флип no_credit_needed ⇄ ok их не трогает.
    var creditWrapHtml = showCredit
      ? '<div class="cab-group" id="cab-credit-wrap">' +
          '<div class="group-title">Условия кредита</div>' +
          (showComboReadout ? comboDownRowHtml(tiValuation, values.combo_extra_down, disabled) : '') +
          renderCreditBlock(values, disabled) +
        '</div>' +
        '<hr class="divider divider--md">'
      : '';

    var tradeinTilesHtml = '';
    if (showTradein) {
      for (var i = 0; i < TILE_CAR_KEYS.length; i++) {
        tradeinTilesHtml += tileGroupHtml(TILE_CAR_KEYS[i], values, disabled);
      }
    }

    var html = '';

    if (scenario === 'combo') {
      // v3 4c/6b: «Ваш автомобиль» → «Комментарий» → «Условия кредита» →
      // плашка → плитки авто; личные плитки скрыты (HIDE_PERSONAL_TILES_IN_COMBO)
      html += ownOrTradeinFieldsHtml(found, values, disabled, tiValuation);
      html += commentBlockHtml(values.client_comment, disabled);
      html += '<hr class="divider divider--md">';
      html += creditWrapHtml;
      html += phraseHtml(PHRASE_COMBO);
      html += tradeinTilesHtml;
      html += '<hr class="divider divider--md">';
    } else {
      // credit (7a) и неизвестный сценарий (В2 — прежний порядок: кредит,
      // затем трейд-ин-блоки, затем личные плитки)
      html += creditWrapHtml;
      if (showTradein) {
        html += ownOrTradeinFieldsHtml(found, values, disabled, tiValuation);
        html += commentBlockHtml(values.client_comment, disabled);
        html += '<hr class="divider divider--md">';
        // Плашка-фраза перед «Общее техническое состояние» (v3: 4b, моб. 6)
        html += phraseHtml(PHRASE_TRADEIN);
        html += tradeinTilesHtml;
        html += '<hr class="divider divider--md">';
      }
    }

    if (showPersonal) {
      // Плашка-фраза перед группами личных плиток (v3: 7a, моб. 5)
      html += phraseHtml(PHRASE_CREDIT);
      for (var j = 0; j < TILE_PERSONAL_KEYS.length; j++) {
        html += tileGroupHtml(TILE_PERSONAL_KEYS[j], values, disabled);
      }
    }
    return html;
  }

  function bannerOkHtml(submittedAtIso, scenario) {
    var dateStr = formatSubmittedAt(submittedAtIso);
    var title = 'Заявка отправлена' + (dateStr ? ' ' + dateStr : '');
    return '<div class="banner banner--ok">' + esc(title) + ' — ' + esc(successText(scenario)) + '</div>';
  }

  // ── Подсветка ошибок валидации (422 fields{}/empty_form) ──
  function markGroupError(key) {
    var group = $('#cab-group-' + key);
    if (!group) return;
    var labels = group.querySelectorAll('.tile');
    for (var i = 0; i < labels.length; i++) labels[i].classList.add('tile--error');
    var err = $('#cab-group-' + key + '-err');
    if (err) err.hidden = false;
  }
  function markFieldError(key) {
    var input = $('#cab-f-' + key);
    if (input) input.classList.add(input.tagName === 'TEXTAREA' ? 'textarea--error' : 'input--error');
  }
  function showFieldErrors(fields) {
    for (var key in fields) {
      if (TILE_GROUPS[key]) markGroupError(key); else markFieldError(key);
    }
  }
  function highlightAllGroups() {
    for (var key in TILE_GROUPS) markGroupError(key);
  }

  // Общая обработка ответа /me: локальные несохранённые правки (после re-OTP)
  // важнее серверного черновика; затем отрисовка кабинета и фоновый пинг.
  function applyMeResponse(me) {
    // Смена позиции (перепривязка сделки, 409-retry): карточка авто устарела.
    // Прежний item_id читаем ДО перезаписи state.me — после неё сравнивать
    // было бы не с чем. Гонки двух in-flight /car нет: applyMeResponse всегда
    // идёт через setScreen (showCabinetScreen ниже) → screenSeq растёт →
    // stale() в fetchCarCard отбросит устаревший ответ.
    var prevCalc = (state.me && state.me.calc) || null;
    var nextCalc = (me && me.calc) || null;
    if (prevCalc && (!nextCalc || prevCalc.item_id !== nextCalc.item_id)) {
      state.car = undefined; // showCabinetScreen перезапустит fetchCarCard
    }
    state.me = me;
    state.submitted = !!me.submitted;
    rememberWeb(me.entry_source, me.profile && me.profile.return_url);
    state.valuation = me.valuation || null;
    var merged = {};
    var form = me.form || {};
    for (var k in form) merged[k] = form[k];
    for (var d in state.formDirty) {
      if (state.formDirty[d]) merged[d] = state.formValues[d];
    }
    state.formValues = merged;
    updateCalcState(me); // U9: состояние calc_model.js из calc-снапшота (или деградация R18)
    showCabinetScreen();
    startPing();
    maybeStartValuationPolling();
  }

  // ── U9: опрос /me, пока оценка идёт (8a) и после сабмита до терминального
  // статуса (R28). Раз в VALUATION_POLL_MS; немедленный запрос при возврате на
  // вкладку (visibilitychange); потолок ~2 минуты или три неудачи подряд →
  // 8e на клиенте (сабмит доступен). Одиночный сбой — молча, следующий тик.
  // Таймер живёт вне timers: перерисовка кабинета (setScreen) его не гасит.
  var valPoll = { timer: null, startedAt: 0, fails: 0, inFlight: false };

  function valuationPending() {
    return !!(state.valuation && state.valuation.status === 'pending') && !state.valClientFailed;
  }

  function maybeStartValuationPolling() {
    if (!valuationPending()) { stopValuationPolling(); return; }
    if (valPoll.timer) return;
    valPoll.startedAt = Date.now();
    valPoll.fails = 0;
    valPoll.timer = setInterval(valuationPollTick, VALUATION_POLL_MS);
  }

  function stopValuationPolling() {
    if (valPoll.timer) { clearInterval(valPoll.timer); valPoll.timer = null; }
  }

  function valuationGiveUp() {
    stopValuationPolling();
    state.valClientFailed = true;
    rerenderCabinetKeepingPlace();
    announceCabinet('Оценку получить не удалось. Укажите данные автомобиля сами — оценку проведёт менеджер.');
  }

  function valuationPollTick() {
    if (valPoll.inFlight || state.reauth || !state.bearer) return;
    if (Date.now() - valPoll.startedAt > VALUATION_POLL_CAP_MS) { valuationGiveUp(); return; }
    valPoll.inFlight = true;
    apiAuthed('GET', '/cabinet/api/me').then(function (me) {
      valPoll.inFlight = false;
      valPoll.fails = 0;
      applyValuationPoll(me);
    }).catch(function (err) {
      valPoll.inFlight = false;
      if (err.status === 401) { stopValuationPolling(); startReauth(); return; }
      valPoll.fails += 1;
      if (valPoll.fails >= VALUATION_POLL_MAX_FAILS) valuationGiveUp();
    });
  }

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible' && valPoll.timer) valuationPollTick();
  });

  // Ответ опроса применяется ТОЛЬКО при смене статуса/оценки: локальные
  // значения анкеты (в т.ч. правки полей 8c/8e) не трогаются — state.formValues
  // остаётся источником правды; меняются calc/valuation и пересчёт условий.
  function applyValuationPoll(me) {
    var prevStatus = state.valuation ? state.valuation.status : null;
    var nextStatus = me.valuation ? me.valuation.status : null;
    var prevTi = state.me && state.me.calc ? state.me.calc.ti_valuation : null;
    var nextTi = me.calc ? me.calc.ti_valuation : null;
    if (prevStatus === nextStatus && prevTi === nextTi) {
      if (!valuationPending()) stopValuationPolling();
      return;
    }
    var prevCs = state.calcState;
    state.me.calc = me.calc;
    state.me.valuation = me.valuation;
    state.valuation = me.valuation || null;
    if (!state.submitted) recalcAfterValuation(prevCs);
    if (nextStatus === 'success' && nextTi != null) state.valFlashUntil = Date.now() + 3000;
    rerenderCabinetKeepingPlace();
    announceCabinet(valuationAnnouncement(nextStatus, nextTi));
    if (!valuationPending()) stopValuationPolling();
  }

  // Пересчёт условий по новой оценке: доплата и платёж клиента сохраняются,
  // платёж прижимается к новым границам (R30, AE7); смена объявляется.
  function recalcAfterValuation(prevCs) {
    var next = ensureCalcState(state.me.calc || null);
    if (!next) { state.calcState = next; return; }
    // Цель платежа: тронутый клиентом платёж — его; нетронутый — желаемый
    // с лендинга (до оценки он мог быть прижат к минимуму без зачёта)
    var target = prevCs && prevCs.payment;
    var landing = calcParseNum(state.me.calc && state.me.calc.target_payment);
    if (!state.paymentTouched && landing) target = landing;
    try {
      if (prevCs && next.mode === 'combo') {
        next = CalcModel.touch(next, 'extraDown', prevCs.extraDown || 0) || next;
        if (next.status !== 'no_credit_needed' && target) {
          next = CalcModel.touch(next, 'payment', target) || next;
        }
      } else if (prevCs && next.mode === 'credit' && target) {
        next = CalcModel.touch(next, 'payment', target) || next;
      }
    } catch (e) {
      console.error('calc_model: пересчёт по оценке не удался — базовое состояние', e);
    }
    state.calcState = next;
    mirrorCalcStateToFormValues(next);
  }

  function valuationAnnouncement(status, ti) {
    if (status === 'success' && ti != null) {
      return 'Оценка готова: около ' + formatMoney(ti) + '. Условия кредита пересчитаны с зачётом.';
    }
    if (status === 'success') return 'Оценка готова. Проверьте данные автомобиля ниже.';
    if (status === 'found_no_price') return 'Автомобиль найден. Оценку проведёт менеджер при осмотре.';
    if (status === 'not_found') return 'Автомобиль по госномеру не найден. Укажите данные сами.';
    if (status === 'failed') return 'Оценку получить не удалось. Укажите данные автомобиля сами.';
    return '';
  }

  function announceCabinet(text) {
    if (!text) return;
    var live = $('#cab-val-live');
    if (!live) return;
    live.textContent = '';
    setTimeout(function () { if (live.isConnected) live.textContent = text; }, 50);
  }

  // Перерисовка кабинета без прыжков (8b): фокус, выделение в поле и
  // прокрутка сохраняются; отложенный автосейв досохраняется сразу —
  // setScreen его гасит.
  function rerenderCabinetKeepingPlace() {
    if (timers.save) { clearTimeout(timers.save); timers.save = null; saveDraft(); }
    var active = document.activeElement;
    var activeId = active && root.contains(active) ? active.id : null;
    var selStart = null, selEnd = null;
    try { selStart = active.selectionStart; selEnd = active.selectionEnd; } catch (e) { /* не текст */ }
    var scrollY = window.pageYOffset || document.documentElement.scrollTop || 0;
    showCabinetScreen();
    if (activeId) {
      var el = document.getElementById(activeId);
      if (el && !el.disabled) {
        try { el.focus({ preventScroll: true }); } catch (e) { el.focus(); }
        try { if (selStart != null) el.setSelectionRange(selStart, selEnd); } catch (e) { /* не текст */ }
      }
    }
    window.scrollTo(0, scrollY);
  }

  function loadCabinet() {
    // Ж4: переход в submitted при живом экране всегда идёт через loadCabinet
    // (успех submit, already_submitted из автосейва/сабмита — чужая вкладка).
    // Снапшот открытости попапа — до showLoading, который его закроет.
    carPopupWasOpenBeforeReload = carPopupOpen;
    showLoading();
    apiAuthed('GET', '/cabinet/api/me').then(applyMeResponse).catch(function (err) {
      if (err.status === 401) { startReauth(); return; }
      showFatal(errorText(err));
    });
  }

  function showCabinetScreen() {
    var me = state.me || {};
    var profile = me.profile || {};
    var calc = me.calc || null;
    var scenario = profile.scenario_type;
    var submitted = state.submitted;

    // Шапка раздела: заголовок слева, кнопка выбранного авто справа.
    // Правая часть — контейнер #cab-car-head: его содержимое (кнопка + попап)
    // патчит patchCarHead по ответу /cabinet/api/car.
    var headHtml =
      '<div class="page-head">' +
        '<div class="cab-head-stack">' +
          '<div class="page-title">Личный кабинет</div>' +
          '<div class="page-subtitle">' + esc(scenarioSubtitle(scenario)) + '</div>' +
        '</div>' +
        '<div id="cab-car-head">' + carHeadHtml(state.car, currentCarName()) + '</div>' +
      '</div>';
    var bodyHtml = buildFormBlocksHtml(scenario, state.formValues, calc, submitted);

    var bannerHtml = submitted ? bannerOkHtml(me.submitted_at, scenario) : '';
    var saveBarHtml = submitted ? '' :
      '<div class="cab-savebar" id="cab-savebar">' + esc(savebarIdleText()) + '</div>';
    var emptyNoteHtml = submitted ? '' :
      '<div class="note note--error" id="cab-empty-form-note" hidden>' +
        'Анкета пуста — заполните хотя бы один пункт, чтобы отправить.</div>';
    var submitBtnHtml = submitted ? '' :
      '<button type="button" class="btn btn--block" id="cab-submit">Отправить</button>';

    var html =
      '<div class="cab-screen container">' +
        bannerHtml +
        '<div' + (submitted ? ' class="readonly"' : '') + ' id="cab-content">' +
          // Шапка — ВНУТРИ #cab-content: после отправки бледнеет вместе с
          // анкетой (кадр 7 макета v3), но карточка остаётся кликабельной
          // (пересмотр Ж4, план 2026-09-26-001). Попап поэтому живёт в слоте
          // НИЖЕ, сиблингом #cab-content (KTD2) — иначе opacity:.55 красила
          // бы его и запирала в своём stacking context.
          headHtml +
          '<hr class="divider divider--md">' +
          '<div class="cab-sr" id="cab-val-live" aria-live="polite"></div>' +
          '<form class="cab-form" id="cab-form" autocomplete="off">' + bodyHtml + '</form>' +
        '</div>' +
        '<div id="cab-car-popup-slot">' + carPopupSlotHtml(state.car, currentCarName()) + '</div>' +
        saveBarHtml +
        '<div id="cab-form-error"></div>' +
        emptyNoteHtml +
        submitBtnHtml +
      '</div>';

    setScreen(html);
    wireCarHeadEvents();
    wireCreditBlock(); // U9: ползунки/платёж — no-op в деградации/read-only
    if (state.car === undefined) fetchCarCard();

    // Попап был открыт в момент перезагрузки /me (например, already_submitted
    // из другой вкладки — автосейвом или сабмитом): его закрыл снос DOM в
    // setScreen; возвращаем фокус на карточку в любом состоянии экрана (В8,
    // унификация после пересмотра Ж4 — переоткрытия попапа нет).
    if (carPopupWasOpenBeforeReload) {
      carPopupWasOpenBeforeReload = false;
      var pickAfterReload = $('#cab-car-pick');
      if (pickAfterReload && pickAfterReload.focus) pickAfterReload.focus();
    }

    if (submitted) return;

    var formEl = $('#cab-form');
    formEl.addEventListener('submit', function (e) { e.preventDefault(); });
    formEl.addEventListener('input', function (e) {
      var input = e.target;
      if (input.type === 'radio') return; // радио-плитки — через 'change' ниже
      var key = input.getAttribute('data-key');
      if (!key) return;
      // U9: при живом калькуляторе поля кредитного блока пишутся ТОЛЬКО через
      // CalcModel → mirrorCalcStateToFormValues — единственный путь записи;
      // делегированный листенер их пропускает (в деградации calcState нет —
      // обычные инпуты работают как раньше).
      // combo_extra_down управляется CalcModel ТОЛЬКО в комбо-режиме (ti_valuation
      // есть); при scenario=combo, но mode!=='combo' (например ti_valuation null)
      // поле не имеет ни одного другого писателя — работает как обычный инпут.
      if (state.calcState && CALC_MANAGED_KEYS[key] &&
          (key !== 'combo_extra_down' || state.calcState.mode === 'combo')) return;
      var raw = input.value;
      if (FORM_INT_FIELDS[key]) raw = onlyDigits(raw);
      // 8c/8e (макет): пробег с разделителем тысяч, «км» — суффикс поля
      input.value = key === 'tradein_mileage' && raw ? formatThousands(raw) : raw;
      input.classList.remove('input--error', 'textarea--error');
      state.formValues[key] = raw;
      markDirtyAndSave(key);
    });
    formEl.addEventListener('change', function (e) {
      var input = e.target;
      if (!(input.tagName === 'INPUT' && input.type === 'radio')) return;
      var key = input.name;
      state.formValues[key] = input.value;
      var group = $('#cab-group-' + key);
      if (group) {
        var labels = group.querySelectorAll('.tile');
        for (var i = 0; i < labels.length; i++) {
          var radio = labels[i].querySelector('input[type="radio"]');
          labels[i].classList.toggle('tile--on', !!(radio && radio.checked));
          labels[i].classList.remove('tile--error');
        }
        var err = $('#cab-group-' + key + '-err');
        if (err) err.hidden = true;
      }
      markDirtyAndSave(key);
    });

    $('#cab-submit').addEventListener('click', submitForm);
    setSaveState(hasDirty() ? 'saving' : 'idle');
    if (hasDirty()) { // правки, пережившие re-OTP — досохраняем сразу
      if (timers.save) clearTimeout(timers.save);
      timers.save = setTimeout(saveDraft, 100);
    }
  }

  // 8a (макет): пока оценка идёт — «… · Отправить можно и сейчас — оценку
  // добавим к заявке, когда она будет готова»
  function savebarIdleText() {
    return valuationStatus() === 'pending'
      ? 'Все изменения сохраняются автоматически · Отправить можно и сейчас — оценку добавим ' +
        'к заявке, когда она будет готова'
      : 'Все изменения сохраняются автоматически';
  }

  function markDirtyAndSave(key) {
    state.formDirty[key] = true;
    setSaveState('saving');
    var emptyNote = $('#cab-empty-form-note');
    if (emptyNote) emptyNote.hidden = true;
    if (timers.save) clearTimeout(timers.save);
    timers.save = setTimeout(saveDraft, AUTOSAVE_DEBOUNCE_MS); // дебаунс ~1.5 с
  }

  function hasDirty() {
    for (var k in state.formDirty) if (state.formDirty[k]) return true;
    return false;
  }

  function coerceValue(key, raw) {
    if (raw == null || String(raw).trim() === '') return null;
    if (FORM_INT_FIELDS[key]) return calcParseNum(raw);
    return String(raw);
  }

  // ВИДИМОЕ СОСТОЯНИЕ автосохранения — всегда на экране:
  // «сохраняется…» / «сохранено» / «не сохранено — повторить»
  function setSaveState(mode, message) {
    var bar = $('#cab-savebar');
    if (!bar) return;
    bar.className = 'cab-savebar';
    if (mode === 'saving') {
      bar.classList.add('cab-save-saving');
      bar.textContent = 'Сохраняется…';
    } else if (mode === 'saved') {
      bar.classList.add('cab-save-saved');
      bar.textContent = '✓ Сохранено'; // историч.: lk-source.html #form-states, фрейм 16 (в v3 кадра нет, поведение сохранено)
    } else if (mode === 'failed') {
      bar.classList.add('cab-save-failed');
      bar.textContent = '';
      var span = document.createElement('span');
      span.textContent = message || 'Не сохранено';
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = 'Повторить';
      btn.addEventListener('click', function () { setSaveState('saving'); saveDraft(); });
      bar.appendChild(span);
      bar.appendChild(btn);
    } else {
      bar.textContent = savebarIdleText();
    }
  }

  function saveDraft() {
    if (timers.save) { clearTimeout(timers.save); timers.save = null; }
    // Строгая сериализация: два PUT /form не должны лететь параллельно —
    // иначе они применятся не по порядку, а статус покажет «Сохранено»
    // для значения, которое на сервере не последнее.
    if (saveInFlight) {
      saveQueued = true;
      setSaveState('saving'); // отложенная запись есть — состояние остаётся «сохраняется»
      return;
    }
    var payload = {};
    var sentKeys = [];
    for (var k in state.formDirty) {
      if (state.formDirty[k]) {
        payload[k] = coerceValue(k, state.formValues[k]);
        sentKeys.push(k);
      }
    }
    if (!sentKeys.length) { setSaveState('saved'); return; }

    saveInFlight = true;
    apiAuthed('PUT', '/cabinet/api/form', payload).then(function () {
      saveInFlight = false;
      for (var i = 0; i < sentKeys.length; i++) {
        // ключ мог измениться, пока летел запрос — тогда остаётся dirty
        if (coerceValue(sentKeys[i], state.formValues[sentKeys[i]]) === payload[sentKeys[i]]) {
          delete state.formDirty[sentKeys[i]];
        }
      }
      if (saveQueued) { // правки пришли во время запроса — пишем их следующим PUT
        saveQueued = false;
        setSaveState('saving');
        saveDraft();
        return;
      }
      setSaveState(hasDirty() ? 'saving' : 'saved');
      if (hasDirty() && !timers.save) timers.save = setTimeout(saveDraft, AUTOSAVE_DEBOUNCE_MS);
    }).catch(function (err) {
      saveInFlight = false;
      if (err.status === 401) { saveQueued = false; startReauth(); return; } // правки не теряем
      if (err.code === 'already_submitted') {
        saveQueued = false;
        state.submitted = true;
        loadCabinet();
        return;
      }
      if (err.code === 'validation' && err.data && err.data.fields) {
        showFieldErrors(err.data.fields); // 422 по-полевая подсветка (14b)
      }
      if (saveQueued) { // отложенная запись есть — повторяем с дебаунсом, статус «сохраняется»
        saveQueued = false;
        setSaveState('saving');
        if (!timers.save) timers.save = setTimeout(saveDraft, AUTOSAVE_DEBOUNCE_MS);
        return;
      }
      // Сбой не-401: явное сообщение + кнопка повтора
      setSaveState('failed', 'Не сохранено — ' +
        (err.code === 'validation' ? 'проверьте значения полей' : 'нет связи с сервером'));
    });
  }

  // Попап сетевой ошибки сабмита (историч. 2c-15, lk-source.html:1488–1516;
  // в v3 — #popup-network, :1386-1396, без изменений) — «Заполненные
  // данные сохранены»; закрывается тем же closeConflictPopup (id 'cab-popup').
  // Mount в document.body — по тем же причинам, что у showConflictPopup (см.
  // комментарий там); стили — параллельные селекторы #cab-popup (U4).
  function showFormNetworkErrorPopup(onRetry) {
    closeConflictPopup();
    var div = document.createElement('div');
    div.id = 'cab-popup';
    div.className = 'popup popup--visible';
    div.setAttribute('role', 'dialog');
    div.setAttribute('aria-modal', 'true');
    div.innerHTML =
      '<div class="popup__overlay"></div>' +
      '<div class="popup__dialog">' +
        '<div class="popup__title">Не удалось отправить</div>' +
        '<div class="popup__text">Проверьте соединение с интернетом и попробуйте ещё раз. ' +
          'Заполненные данные сохранены.</div>' +
        '<div class="popup__actions">' +
          '<button type="button" class="btn btn--sm" id="cab-popup-retry">Повторить</button>' +
          '<a class="link" id="cab-popup-close" href="#">Закрыть</a>' +
        '</div>' +
      '</div>';
    document.body.appendChild(div);
    div.querySelector('#cab-popup-retry').addEventListener('click', function () {
      closeConflictPopup();
      onRetry();
    });
    div.querySelector('#cab-popup-close').addEventListener('click', function (e) {
      e.preventDefault();
      closeConflictPopup();
    });
  }

  // 409-retry (код-ревью P1): перечитывает /me перед повторной отправкой —
  // иначе повтор шлёт тот же устаревший calc.item_id/price и снова ловит 409.
  function retrySubmitAfterConflict() {
    apiAuthed('GET', '/cabinet/api/me').then(function (me) {
      applyMeResponse(me);
      submitForm();
    }).catch(function () {
      loadCabinet(); // перечитывание не удалось — полный ре-старт кабинета
    });
  }

  function submitForm() {
    var btn = $('#cab-submit');
    var errBox = $('#cab-form-error');
    var emptyNote = $('#cab-empty-form-note');
    if (btn) { btn.disabled = true; btn.textContent = 'Отправляем…'; }
    if (errBox) errBox.innerHTML = '';
    if (emptyNote) emptyNote.hidden = true;

    var payload = {};
    for (var k in state.formValues) payload[k] = coerceValue(k, state.formValues[k]);
    // Опциональная сверка снапшота (U5): {item_id, price}, по которым уже
    // посчитан браузер, — расхождение с текущим снапшотом даёт 409 conflict.
    if (state.me && state.me.calc) {
      payload.calc = { item_id: state.me.calc.item_id, price: state.me.calc.price };
    }

    apiAuthed('POST', '/cabinet/api/form/submit', payload).then(function () {
      state.submitted = true;
      state.formDirty = {};
      loadCabinet(); // перечитать /me — submitted_at приходит от сервера
    }).catch(function (err) {
      if (err.status === 401) {
        state.pendingSubmit = true;
        startReauth();
        return;
      }
      if (err.code === 'already_submitted') {
        state.submitted = true;
        loadCabinet();
        return;
      }
      if (btn) { btn.disabled = false; btn.textContent = 'Отправить'; }
      if (err.code === 'conflict') {
        // Гонка формы/устаревший снапшот (409) — попап историч. lk-source.html:
        // 1504–1514 (в v3 попапы ошибок не менялись).
        // Повтор с устаревшим state.me.calc опять словит 409 — сначала перечитываем
        // /me тем же путём, что boot/loadCabinet (applyMeResponse), и только потом
        // повторяем submitForm; formValues не трогаем — applyMeResponse сохраняет
        // локальные dirty-правки клиента поверх серверного черновика.
        showConflictPopup(retrySubmitAfterConflict);
        return;
      }
      if (isNetworkError(err)) {
        showFormNetworkErrorPopup(submitForm); // историч. 2c-15 (в v3 кадра нет)
        return;
      }
      if (err.code === 'validation') {
        if (err.data && err.data.reason === 'empty_form') {
          if (emptyNote) emptyNote.hidden = false;
          highlightAllGroups();
        } else if (err.data && err.data.fields) {
          showFieldErrors(err.data.fields);
          if (errBox) errBox.innerHTML = '<div class="note note--error">Проверьте отмеченные поля.</div>';
        } else if (errBox) {
          errBox.innerHTML = '<div class="note note--error">' +
            'Проверьте значения полей — часть из них не принята.</div>';
        }
        return;
      }
      if (errBox) errBox.innerHTML = '<div class="note note--error">' + esc(errorText(err)) + '</div>';
    });
  }

  // ── 10. RE-OTP: 401 в кабинете → инлайн повторная авторизация ───────────────
  // Локальное состояние формы (state.formValues / state.formDirty) сохраняется.
  function startReauth() {
    if (isWeb()) {
      // R50: повторного входа в веб-сессии нет — 8f; черновик уже сохранён
      state.bearer = null;
      storageSet(BEARER_KEY, null);
      if (timers.save) { clearTimeout(timers.save); timers.save = null; }
      showSessionEnded();
      return;
    }
    if (state.reauth) return;
    state.reauth = true;
    state.bearer = null;
    storageSet(BEARER_KEY, null);
    stopPing();
    // Отменяем отложенный автосейв сразу: bearer уже null, запрос бессмыслен
    if (timers.save) { clearTimeout(timers.save); timers.save = null; }
    saveQueued = false;

    if (!state.token) {
      showFatal('Сессия истекла, а ссылка недоступна. Откройте кабинет по ссылке из чата.');
      return;
    }
    api('POST', '/cabinet/api/auth/init', { token: state.token }).then(function (data) {
      state.initData = data;
      if (data.state === 'bound') {
        state.targetMasked = data.masked_phone || '';
        state.consentAccepted = false; // согласие уже зафиксировано на сервере
        showChannelScreen('Сессия истекла — получите новый код. Данные анкеты не потеряны.');
      } else {
        showAuthScreen();
      }
    }).catch(function (err) {
      if (err.code === 'link_expired') showLinkExpired();
      else showFatal(errorText(err));
    });
  }

  function resumeAfterReauth() {
    state.reauth = false;
    var doSubmit = state.pendingSubmit;
    state.pendingSubmit = false;
    apiAuthed('GET', '/cabinet/api/me').then(function (me) {
      applyMeResponse(me);
      if (doSubmit && !state.submitted) submitForm();
    }).catch(function (err) {
      if (err.status === 401) { startReauth(); return; }
      showFatal(errorText(err));
    });
  }

  // ── 11. ФОНОВЫЙ ПИНГ /me — раз в 5 минут, независим от автосейва ────────────
  function startPing() {
    if (timers.ping) return;
    timers.ping = setInterval(function () {
      if (state.reauth || !state.bearer) return;
      apiAuthed('GET', '/cabinet/api/me').catch(function (err) {
        if (err.status === 401 && !state.reauth) startReauth();
        // прочие сбои пинга молчаливы — следующий тик через 5 минут
      });
    }, PING_INTERVAL_MS);
  }
  function stopPing() {
    if (timers.ping) { clearInterval(timers.ping); timers.ping = null; }
  }

  // ── 12. СТАРТ ───────────────────────────────────────────────────────────────
  function boot() {
    root = document.getElementById('cabinet-root');
    if (!root) return;
    showLoading();

    // Восстановление после перезагрузки: живая сессия → сразу кабинет
    if (state.bearer) {
      // Слияние formDirty внутри applyMeResponse здесь no-op: грязных правок ещё нет
      apiAuthed('GET', '/cabinet/api/me').then(applyMeResponse).catch(function (err) {
        state.bearer = null;
        storageSet(BEARER_KEY, null);
        if (err.status === 401) bootFromToken();
        else showFatal(errorText(err));
      });
      return;
    }
    bootFromToken();
  }

  function bootFromToken() {
    if (!state.token && isWeb()) { showSessionEnded(); return; }
    if (!state.token) {
      showFatal('Кабинет открывается по персональной ссылке из чата. ' +
        'Вернитесь в чат и перейдите по ссылке ещё раз.');
      return;
    }
    api('POST', '/cabinet/api/auth/init', { token: state.token }).then(function (data) {
      state.initData = data;
      rememberWeb(data.entry_source, data.return_url);
      if (isWeb() && data.state === 'bound') {
        // R50: номер уже подтверждён, но входа во вкладке нет (закрыли
        // вкладку/истёк вход) — повторного входа нет, 8f
        showSessionEnded();
        return;
      }
      if (data.call) {
        // R24: F5 на экране «Звоним на номер…» — тот же экран, номер и
        // остаток кулдауна (для всех клиентов)
        restoreCallScreen(data.call);
        return;
      }
      if (data.state === 'bound') {
        // Привязанный номер: телефон не спрашиваем — сразу запрос нового кода
        // (выбор канала обязателен: R28 требует предупредить о звонке ДО отправки)
        state.targetMasked = data.masked_phone || '';
        showChannelScreen();
      } else {
        showAuthScreen();
      }
    }).catch(function (err) {
      if (err.code === 'link_expired') showLinkExpired();
      else showFatal(errorText(err));
    });
  }

  // R24: восстановление экрана ввода кода по живой отправке из auth/init.
  // Кулдаун — от серверного момента следующего запроса (next_call_at).
  function restoreCallScreen(call) {
    state.channel = call.channel === 'sms' && !isWeb() ? 'sms' : 'voice';
    state.targetMasked = call.masked_phone || '';
    state.targetFull = null;
    if (state.channel === 'voice' && !isWeb()) state.voiceUsed = true; // O5: звонок уже был
    state.callsLeft = call.calls_left != null ? call.calls_left : null;
    state.recall = !!call.recall;
    var left = 0;
    if (call.next_call_at) {
      var at = Date.parse(call.next_call_at);
      if (!isNaN(at)) left = Math.max(0, Math.round((at - Date.now()) / 1000));
    }
    state.nextCallAt = Date.now() + left * 1000;
    showCodeScreen({ initialState: 'sent', cooldown: left });
    if (left <= 0) renderResendReady($('#cab-timer'));
    else if (isWeb() && call.code_expired) showWebNoAnswer(); // 2б: код истёк, таймер идёт
    focusTitle();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
