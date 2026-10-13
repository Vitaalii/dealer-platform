/**
 * Chat Widget
 * Персональный ассистент — Tidio/Intercom style
 * Только desktop (> 480px). Открывается программно через window.openTDWidget(payload).
 * v2026-08-19: кликабельная ссылка кабинета <a> в пузыре (план 2026-08-19-001, U2) —
 * при выкате обновить query-параметр версии в подключении скрипта на Tilda (cache-bust).
 */
(function () {
  'use strict';

  // ─── CONFIG ────────────────────────────────────────────────────────────────
  var BOT_URL = window.UNIFIEDBOT_URL || '';

  // URL аватара — берём из того же хоста что и BOT_URL
  var AVATAR_URL = BOT_URL + '/widget-custom/avatar.jpg';
  var AVATAR_OFFLINE_URL = BOT_URL + '/widget-custom/robot.png';

  // Кэш уже загруженных blob-URL, чтобы не качать одно и то же дважды
  var _imgCache = {};

  // Грузит картинку через fetch (с ngrok-заголовком) и подставляет в <img>.
  // Тег <img> не умеет отправлять кастомные заголовки, поэтому при работе
  // через ngrok прямой src отдаёт HTML-заглушку вместо изображения.
  function setImageSrc(imgEl, url) {
    if (!imgEl || !url) return;
    if (_imgCache[url]) { imgEl.src = _imgCache[url]; return; }
    fetch(url, { headers: { 'ngrok-skip-browser-warning': '1' } })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.blob();
      })
      .then(function (blob) {
        var objUrl = URL.createObjectURL(blob);
        _imgCache[url] = objUrl;
        imgEl.src = objUrl;
      })
      .catch(function (e) {
        console.warn('[Widget] avatar load failed:', url, e);
        imgEl.src = url; // фолбэк: вдруг сработает напрямую
      });
  }

  // ─── GUARD: только desktop ─────────────────────────────────────────────────
  if (window.innerWidth <= 480) return;

  // ─── STATE ─────────────────────────────────────────────────────────────────
  var state = {
    open:                 false,
    sessionId:            null,
    lastPayload:          null,
    _sseAbort:            null,   // AbortController для fetch-based SSE
    messages:             [],
    inputLocked:          false,
    _initSent:            false,  // защита от двойного старта
    firstMessageReceived: false,  // переключатель спиннер → три точки
  };

  // ─── STYLES ────────────────────────────────────────────────────────────────
  var css = [
    '#td-widget-root *{box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}',

    /* Подложка */
    '#td-overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:2147483646;',
    'display:none;align-items:center;justify-content:center}',
    '#td-overlay.td-open{display:flex}',

    /* Окно */
    '#td-chat{width:680px;max-width:95vw;height:560px;background:#fff;',
    'border-radius:4px;display:flex;flex-direction:column;overflow:hidden;',
    'transform:translateY(16px);opacity:0;transition:transform .22s ease,opacity .22s ease}',
    '#td-overlay.td-open #td-chat{transform:translateY(0);opacity:1}',

    /* Заголовок */
    '#td-header{display:flex;align-items:center;gap:14px;padding:20px 28px;',
    'border-bottom:1px solid #e8e8e8;flex-shrink:0;background:#fff}',
    '#td-header-avatar{position:relative;flex-shrink:0}',
    '#td-header-avatar img{width:48px;height:48px;border-radius:50%;object-fit:cover;border:none}',
    '#td-header-info{flex:1;min-width:0}',
    '#td-header-name{color:#111;font-size:17px;font-weight:600;letter-spacing:-0.3px}',
    '#td-header-status{color:#666;font-size:13px;margin-top:3px;display:none;align-items:center;gap:6px}',
    '#td-header-status::before{content:\"\";width:8px;height:8px;background:#9ca3af;border-radius:50%;display:inline-block;flex-shrink:0}',
    '#td-header-status.online{display:flex}',
    '#td-header-status.online::before{background:#22c55e}',
    '#td-close-btn{background:none;border:none;cursor:pointer;color:#999;font-size:20px;line-height:1;padding:4px;flex-shrink:0}',
    '#td-close-btn:hover{color:#111}',

    /* Сообщения */
    '#td-messages{flex:1;overflow-y:auto;padding:20px 28px;display:flex;flex-direction:column;gap:12px;background:#fafafa}',
    '#td-messages::-webkit-scrollbar{width:4px}',
    '#td-messages::-webkit-scrollbar-thumb{background:#ddd;border-radius:2px}',

    '.td-msg{max-width:75%;display:flex;flex-direction:column;gap:4px}',
    '.td-msg.bot{align-self:flex-start}',
    '.td-msg.user{align-self:flex-end}',

    '.td-bubble{padding:16px 20px;font-size:14px;line-height:1.6;word-break:break-word;white-space:pre-wrap}',
    '.td-msg.bot .td-bubble{background:#fff;color:#111;border-radius:0 12px 12px 12px;border:1px solid #e8e8e8}',
    '.td-msg.user .td-bubble{background:#111;color:#fff;border-radius:12px 0 12px 12px}',
    /* Ссылка в пузыре: палитра виджета вместо браузерного дефолта; паддинг с
       компенсирующим отступом расширяет тач-таргет без сдвига строк */
    '.td-bubble a{color:inherit;text-decoration:underline;text-underline-offset:2px;',
    'padding:8px 2px;margin:-8px -2px}',

    '.td-time{font-size:11px;color:#bbb;padding:0 2px}',
    '.td-msg.user .td-time{text-align:right}',

    /* Quick replies */
    '.td-quick-replies{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}',
    '.td-qr-btn{background:#fff;border:1.5px solid #111;color:#111;border-radius:2px;',
    'padding:6px 14px;font-size:12px;font-weight:500;cursor:pointer;',
    'text-transform:uppercase;letter-spacing:0.3px;transition:all .15s;white-space:nowrap}',
    '.td-qr-btn:hover{background:#111;color:#fff}',

    /* Typing indicator */
    '#td-typing{display:none;align-items:center;gap:6px;padding:12px 16px;',
    'background:#fff;border-radius:0 12px 12px 12px;border:1px solid #e8e8e8;',
    'align-self:flex-start;margin-bottom:2px}',
    '#td-typing.visible{display:flex}',
    '.td-dot{width:7px;height:7px;background:#bbb;border-radius:50%;animation:td-bounce .9s infinite}',
    '.td-dot:nth-child(2){animation-delay:.15s}.td-dot:nth-child(3){animation-delay:.3s}',
    '@keyframes td-bounce{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-6px)}}',

    /* Spinner */
    '.td-spinner{width:14px;height:14px;border:2px solid #e0e0e0;border-top-color:#888;border-radius:50%;animation:tdspin .8s linear infinite;flex-shrink:0}',
    '@keyframes tdspin{to{transform:rotate(360deg)}}',

    /* Footer */
    '#td-footer{padding:16px 28px 12px;border-top:1px solid #e8e8e8;background:#fff;flex-shrink:0}',
    '#td-input-row{display:flex;align-items:center;gap:10px}',
    '#td-input{flex:1;background:#f0f0f0;border:1.5px solid #ddd;border-radius:24px;',
    'padding:12px 20px;font-size:14px;resize:none;outline:none;max-height:100px;',
    'line-height:1.4;font-family:inherit;transition:border-color .15s}',
    '#td-input:focus{border-color:#aaa;background:#f5f5f5}',
    '#td-send-btn{width:40px;height:40px;background:#111;border:none;border-radius:50%;',
    'cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0}',
    '#td-send-btn:hover{background:#333}',
    '#td-footer-note{font-size:11px;color:#bbb;margin-top:8px}',

    /* Кнопка-иконка (свёрнутый чат) */
    '#td-fab{position:fixed;bottom:24px;right:24px;width:52px;height:52px;',
    'background:#111;border:none;border-radius:4px;cursor:pointer;',
    'display:none;align-items:center;justify-content:center;',
    'z-index:2147483645;box-shadow:0 4px 16px rgba(0,0,0,.25);',
    'transition:background .15s,transform .15s}',
    '#td-fab:hover{background:#333;transform:scale(1.05)}',
    '#td-fab.td-fab-visible{display:flex}',
    '#td-fab svg{fill:#fff;width:24px;height:24px}',
  ].join('');

  // ─── DOM BUILD ─────────────────────────────────────────────────────────────
  function buildWidget() {
    var root = document.createElement('div');
    root.id = 'td-widget-root';

    var styleEl = document.createElement('style');
    styleEl.textContent = css;
    root.appendChild(styleEl);

    var overlay = document.createElement('div');
    overlay.id = 'td-overlay';
    overlay.innerHTML = [
      '<div id="td-chat">',
      '  <div id="td-header">',
      '    <div id="td-header-avatar">',
      '      <img id="td-avatar-img" alt="Ассистент">',
      '    </div>',
      '    <div id="td-header-info">',
      '      <div id="td-header-name">Информационный бот</div>',
      '      <div id="td-header-status"></div>',
      '    </div>',
      '    <button id="td-close-btn" title="Закрыть">&#x2715;</button>',
      '  </div>',
      '  <div id="td-messages">',
      '    <div id="td-typing">',
      '      <div class="td-dot"></div><div class="td-dot"></div><div class="td-dot"></div>',
      '    </div>',
      '  </div>',
      '  <div id="td-footer">',
      '    <div id="td-input-row">',
      '      <textarea id="td-input" rows="1" placeholder="Напишите сообщение..."></textarea>',
      '      <button id="td-send-btn">',
      '        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">',
      '          <line x1="22" y1="2" x2="11" y2="13"></line>',
      '          <polygon points="22 2 15 22 11 13 2 9 22 2" fill="#fff" stroke="none"></polygon>',
      '        </svg>',
      '      </button>',
      '    </div>',
      '    <div id="td-footer-note" style="display:none"></div>',
      '  </div>',
      '</div>',
    ].join('');

    root.appendChild(overlay);

    var fab = document.createElement('button');
    fab.id = 'td-fab';
    fab.title = 'Открыть чат';
    fab.innerHTML = '<svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg>';
    fab.addEventListener('click', function() {
      showFab(false);
      showWidget();
      startSSE();
    });
    root.appendChild(fab);

    document.body.appendChild(root);

    setImageSrc(document.getElementById('td-avatar-img'), AVATAR_OFFLINE_URL);

    document.getElementById('td-close-btn').addEventListener('click', closeWidget);

    var input = document.getElementById('td-input');
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendUserMessage(); }
    });
    input.addEventListener('input', function () {
      this.style.height = 'auto';
      this.style.height = Math.min(this.scrollHeight, 100) + 'px';
    });
    document.getElementById('td-send-btn').addEventListener('click', sendUserMessage);
  }

  // ─── SESSION HELPERS ───────────────────────────────────────────────────────
  function getOrCreateSession() {
    var sid = localStorage.getItem('td_session_id');
    if (!sid) { sid = String(Date.now()); localStorage.setItem('td_session_id', sid); }
    return sid;
  }

  function resetSession() {
    localStorage.removeItem('td_session_id');
    localStorage.removeItem('td_chat_history');
    return getOrCreateSession();
  }

  function saveHistory(msgs) {
    localStorage.setItem('td_chat_history', JSON.stringify(msgs));
  }

  function loadHistory() {
    try { return JSON.parse(localStorage.getItem('td_chat_history') || '[]'); }
    catch (e) { return []; }
  }

  // ─── OPEN / CLOSE ──────────────────────────────────────────────────────────
  function showWidget() {
    var overlay = document.getElementById('td-overlay');
    if (!overlay) return;
    overlay.classList.add('td-open');
    state.open = true;
    // Снимаем зелёный outline с FAB при открытии
    var fab = document.getElementById('td-fab');
    if (fab) fab.style.outline = 'none';
    if (!state.inputLocked) document.getElementById('td-input').focus();
  }

  function closeWidget() {
    var overlay = document.getElementById('td-overlay');
    if (overlay) overlay.classList.remove('td-open');
    state.open = false;
    document.body.style.overflow = ''; // восстанавливаем скролл страницы
    document.body.classList.remove('t-body_popupshowed');         // убираем классы Tilda
    document.body.classList.remove('t-body_success-popup-showed');
    // SSE НЕ останавливаем — бот может ответить пока чат свёрнут
    if (state.sessionId && state.messages.length > 0) {
      showFab(true);
    }
  }

  function showFab(visible) {
    var fab = document.getElementById('td-fab');
    if (!fab) return;
    if (visible) {
      fab.classList.add('td-fab-visible');
    } else {
      fab.classList.remove('td-fab-visible');
    }
  }

  // ─── SEND MESSAGE ──────────────────────────────────────────────────────────
  function sendMessage(text, payload) {
    var body = { session_id: state.sessionId, text: text || '' };
    if (payload) body.payload = payload;
    fetch(BOT_URL + '/chat_message', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'ngrok-skip-browser-warning': '1',
      },
      body: JSON.stringify(body),
    }).catch(function (err) {
      console.error('[Widget] Send error:', err);
    });
  }

  function sendUserMessage() {
    if (state.inputLocked) return;
    var input = document.getElementById('td-input');
    var text  = (input.value || '').trim();
    if (!text) return;

    appendUserBubble(text);
    state.messages.push({ role: 'user', text: text });
    saveHistory(state.messages);

    input.value = '';
    input.style.height = 'auto';

    startSSE();
    sendMessage(text, '');
    showTyping();
  }

  // ─── SSE ───────────────────────────────────────────────────────────────────
  function handleSSEMessage(msg) {
    console.log('[Widget] SSE message:', msg);
    if (msg.type === 'typing_on') {
      showTyping();
    } else if (msg.type === 'chat_close') {
      hideTyping();
      lockInput();
      stopSSE();
    } else if (msg.type === 'text') {
      hideTyping();
      state.firstMessageReceived = true;
      appendBotBubble(msg.text || '', msg.buttons);
      state.messages.push({ role: 'bot', text: msg.text || '', buttons: msg.buttons });
      saveHistory(state.messages);
      // Если чат свёрнут — мигнуть FAB зелёным
      if (!state.open) {
        var fab = document.getElementById('td-fab');
        if (fab) fab.style.outline = '3px solid #22c55e';
      }
      // Переключаем статус + аватар после сообщения об отправке специалисту (с задержкой 3 сек)
      if ((msg.text || '').indexOf('Принято! Ваш запрос отправлен специалисту') !== -1) {
        setTimeout(function() {
          var statusEl = document.getElementById('td-header-status');
          if (statusEl) {
            statusEl.classList.add('online');
            statusEl.textContent = 'Онлайн';
          }
          var nameEl = document.getElementById('td-header-name');
          if (nameEl) {
            nameEl.textContent = 'Ассистент отдела продаж';
          }
          var avatarEl = document.getElementById('td-avatar-img');
          if (avatarEl) {
            setImageSrc(avatarEl, AVATAR_URL);
          }
          showTyping();
        }, 3000);
      }
    }
  }

  function startSSE() {
    if (state._sseAbort) {
      console.log('[Widget] startSSE: already running, skipping');
      return;
    }
    console.log('[Widget] startSSE called, sessionId=' + state.sessionId);
    if (!state.sessionId) {
      console.error('[Widget] startSSE: sessionId is null, aborting');
      return;
    }

    var url = BOT_URL + '/chat_stream/' + state.sessionId;
    console.log('[Widget] Opening SSE via fetch:', url);

    var abort = new AbortController();
    state._sseAbort = abort;

    fetch(url, {
      headers: {
        'ngrok-skip-browser-warning': '1',
        'Accept': 'text/event-stream',
      },
      signal: abort.signal,
    })
      .then(function (response) {
        if (!response.ok) {
          console.error('[Widget] SSE response error:', response.status);
          state._sseAbort = null;
          return;
        }
        console.log('[Widget] SSE fetch connected, reading stream');
        var reader  = response.body.getReader();
        var decoder = new TextDecoder();
        var buffer  = '';

        function read() {
          reader.read().then(function (result) {
            if (result.done) {
              console.log('[Widget] SSE stream ended');
              state._sseAbort = null;
              return;
            }
            buffer += decoder.decode(result.value, { stream: true });
            var lines = buffer.split('\n');
            buffer = lines.pop(); // неполная строка — в буфер
            lines.forEach(function (line) {
              if (line.indexOf('data: ') === 0) {
                var raw = line.slice(6).trim();
                if (!raw) return;
                try { handleSSEMessage(JSON.parse(raw)); }
                catch (e) { console.error('[Widget] SSE parse error:', e, raw); }
              }
            });
            read(); // читаем следующий чанк
          }).catch(function (err) {
            if (err.name !== 'AbortError') {
              console.error('[Widget] SSE read error:', err);
            }
            state._sseAbort = null;
          });
        }
        read();
      })
      .catch(function (err) {
        if (err.name !== 'AbortError') {
          console.error('[Widget] SSE fetch error:', err);
        }
        state._sseAbort = null;
      });
  }

  function stopSSE() {
    if (state._sseAbort) {
      state._sseAbort.abort();
      state._sseAbort = null;
    }
  }

  // ─── INPUT LOCK ────────────────────────────────────────────────────────────
  function lockInput() {
    state.inputLocked = true;
    var input = document.getElementById('td-input');
    var btn   = document.getElementById('td-send-btn');
    if (input) { input.disabled = true; input.placeholder = 'Диалог завершён'; }
    if (btn)   { btn.disabled = true; btn.style.opacity = '0.4'; }
  }

  function unlockInput() {
    state.inputLocked = false;
    var input = document.getElementById('td-input');
    var btn   = document.getElementById('td-send-btn');
    if (input) { input.disabled = false; input.placeholder = 'Напишите сообщение...'; }
    if (btn)   { btn.disabled = false; btn.style.opacity = '1'; }
  }

  function clearChatMessages() {
    var msgs  = document.getElementById('td-messages');
    var typing = document.getElementById('td-typing');
    if (!msgs) return;
    // Удаляем все дочерние кроме #td-typing
    Array.from(msgs.children).forEach(function (c) {
      if (c.id !== 'td-typing') c.remove();
    });
  }

  // ─── UI HELPERS ────────────────────────────────────────────────────────────
  function renderText(text) {
    // Экранируем HTML, потом восстанавливаем нужные теги. Кавычки экранируются
    // тоже: сырая кавычка в тексте модели не должна уметь закрыть атрибут
    // src/href вставляемых ниже тегов (вырыв из атрибута = XSS).
    var safe = (text || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
    // Разрешаем <b>, </b>, <i>, </i>
    safe = safe.replace(/&lt;b&gt;/g, '<b>').replace(/&lt;\/b&gt;/g, '</b>');
    safe = safe.replace(/&lt;i&gt;/g, '<i>').replace(/&lt;\/i&gt;/g, '</i>');
    // Ссылка кабинета: строго ограниченная форма от бота (план 2026-08-19-001, U2).
    // Только https; кавычки/пробелы/скобки в href исключены классом символов,
    // амперсанд тоже — href с ЛЮБОЙ сущностью (сырая кавычка/скобка/амперсанд
    // в оригинале) не матчится и остаётся текстом, fail-closed. Кавычки формы
    // к этому шагу уже экранированы — матчим &quot;. Стоит ДО картинок, чтобы
    // <img>-регэксп не видел содержимое href как отдельный URL.
    safe = safe.replace(
      /&lt;a href=&quot;(https:\/\/[^"'<>\s&]+?)&quot;&gt;([^<>]+?)&lt;\/a&gt;/g,
      '<a href="$1" target="_blank" rel="noopener">$2</a>'
    );
    // Картинки
    safe = safe.replace(
      /(https?:\/\/\S+\.(?:jpg|jpeg|png|webp|gif)(?:\?\S*)?)/gi,
      '<br><img src="$1" style="max-width:100%;border-radius:8px;margin-top:6px;" />'
    );
    // Переносы строк
    safe = safe.replace(/\n/g, '<br>');
    return safe;
  }

  function appendBotBubble(text, buttons) {
    var msgs = document.getElementById('td-messages');
    if (!msgs) return;

    // Убираем typing из DOM и вставляем перед ним
    var typing = document.getElementById('td-typing');

    var wrap = document.createElement('div');
    wrap.className = 'td-msg bot';

    var bubble = document.createElement('div');
    bubble.className = 'td-bubble';
    bubble.innerHTML = renderText(text);
    wrap.appendChild(bubble);

    var timeEl = document.createElement('div');
    timeEl.className = 'td-time';
    timeEl.textContent = formatTime(new Date());
    wrap.appendChild(timeEl);

    if (buttons && buttons.length) {
      var qr = document.createElement('div');
      qr.className = 'td-quick-replies';
      buttons.forEach(function (btn_data) {
        var label   = typeof btn_data === 'string' ? btn_data : (btn_data.text || '');
        var payload = typeof btn_data === 'string' ? btn_data : (btn_data.payload || btn_data.text || '');
        var btn = document.createElement('button');
        btn.className = 'td-qr-btn';
        btn.textContent = label;
        btn.addEventListener('click', function () {
          appendUserBubble(label);
          state.messages.push({ role: 'user', text: label });
          saveHistory(state.messages);
          startSSE();
          sendMessage(payload, '');
          qr.remove();
          showTyping();
        });
        qr.appendChild(btn);
      });
      bubble.appendChild(qr);
    }

    msgs.insertBefore(wrap, typing);
    scrollToBottom();
  }

  function appendUserBubble(text) {
    var msgs = document.getElementById('td-messages');
    if (!msgs) return;

    var typing = document.getElementById('td-typing');

    var wrap = document.createElement('div');
    wrap.className = 'td-msg user';

    var bubble = document.createElement('div');
    bubble.className = 'td-bubble';
    bubble.textContent = text;
    wrap.appendChild(bubble);

    var timeEl = document.createElement('div');
    timeEl.className = 'td-time';
    timeEl.textContent = formatTime(new Date());
    wrap.appendChild(timeEl);

    msgs.insertBefore(wrap, typing);
    scrollToBottom();
  }

  function showTyping() {
    var t = document.getElementById('td-typing');
    if (!t) return;
    if (!state.firstMessageReceived) {
      t.innerHTML = '<div class="td-spinner"></div><span style="font-size:13px;color:#888;margin-left:8px">Обработка запроса...</span>';
    } else {
      t.innerHTML = '<span class="td-dot"></span><span class="td-dot"></span><span class="td-dot"></span>';
    }
    t.classList.add('visible');
    scrollToBottom();
  }

  function hideTyping() {
    var t = document.getElementById('td-typing');
    if (t) t.classList.remove('visible');
  }

  function scrollToBottom() {
    var msgs = document.getElementById('td-messages');
    if (msgs) msgs.scrollTop = msgs.scrollHeight;
  }

  function formatTime(d) {
    return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
  }

  // ─── RESIZE GUARD ──────────────────────────────────────────────────────────
  window.addEventListener('resize', function () {
    if (window.innerWidth <= 480 && state.open) closeWidget();
  });

  // ─── PUBLIC API ────────────────────────────────────────────────────────────
  window.openTDWidget = function (payload) {
    if (window.innerWidth <= 480) return;

    // 1. Скрыть попап Tilda
    var closeBtn = document.getElementById('tildaformsuccesspopupbtn-new')
                || document.getElementById('tildaformsuccesspopupclose-new');
    if (closeBtn) closeBtn.click();
    setTimeout(function () {
      var wrapper = document.querySelector('.t-form-success-popup__wrapper');
      if (wrapper) {
        var p = wrapper.closest('.t-popup');
        if (p) p.style.display = 'none';
        wrapper.style.display = 'none';
      }
    }, 100);

    // 2. Новый payload = новая сессия (сброс флага защиты)
    if (payload && payload !== state.lastPayload) {
      stopSSE();
      state.sessionId            = resetSession();
      state.lastPayload          = payload;
      state.messages             = [];
      state._initSent            = false;
      state.firstMessageReceived = false;
      clearChatMessages();
      showFab(false);
      unlockInput();
    } else if (!state.sessionId) {
      state.sessionId = getOrCreateSession();
    }

    // 3. Показать виджет
    showWidget();

    // 4. Восстановить историю или начать новую сессию
    var history = loadHistory();
    if (history.length > 0 && (!payload || payload === state.lastPayload)) {
      // Есть история — рендерим без повторного запроса к боту
      if (state.messages.length === 0) {
        history.forEach(function (m) {
          if (m.role === 'user') appendUserBubble(m.text);
          else if (m.role === 'bot') appendBotBubble(m.text, m.buttons);
        });
        state.messages = history;
      }
      console.log('[Widget] calling startSSE from openTDWidget (history branch)');
      startSSE();
    } else {
      // Новая сессия — отправляем стартовый запрос ровно один раз
      if (!state._initSent) {
        state._initSent = true;
        showTyping();
        sendMessage('', payload);
      }
      console.log('[Widget] calling startSSE from openTDWidget (new session branch)');
      startSSE();
    }
  };

  window.closeTDWidget = function () { closeWidget(); };

  // ─── INIT ──────────────────────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', buildWidget);
  } else {
    buildWidget();
  }

})();
