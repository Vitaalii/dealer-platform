// Тесты renderText виджета: allowlist-рендер сообщений бота.
// Функция извлекается из widget.js текстом (виджет — IIFE, экспорта нет)
// и инстанцируется через new Function — тестируется ровно тот код,
// который уезжает в браузер.
import fs from 'node:fs';
import assert from 'node:assert/strict';

const WIDGET_PATH = new URL('../../widget-custom/widget.js', import.meta.url);

function extractFunction(source, name) {
  const start = source.indexOf('function ' + name + '(');
  assert.notEqual(start, -1, name + ' не найден в widget.js');
  const bodyStart = source.indexOf('{', start);
  let depth = 0;
  for (let i = bodyStart; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') {
      depth--;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  assert.fail('скобки функции ' + name + ' не сошлись');
}

const src = fs.readFileSync(WIDGET_PATH, 'utf8');
const renderText = new Function('return (' + extractFunction(src, 'renderText') + ')')();

const CAB_URL = 'https://demo.autodc.org/cabinet#t=h85YEhXf0aB';

let failed = 0;
function check(label, fn) {
  try {
    fn();
    console.log('ok - ' + label);
  } catch (e) {
    failed++;
    console.error('FAIL - ' + label + '\n  ' + e.message);
  }
}

// ── Восстановление ссылки кабинета ──────────────────────────────────────────
check('валидная форма <a href="https://..."> восстанавливается в тег', () => {
  const out = renderText('Вот <a href="' + CAB_URL + '">личный кабинет</a>\nПолучилось зайти?');
  assert.ok(out.includes(
    '<a href="' + CAB_URL + '" target="_blank" rel="noopener">личный кабинет</a>'),
    'тег не восстановлен: ' + out);
  assert.ok(!out.includes('&lt;a'), 'остатки экранированного тега: ' + out);
  assert.ok(out.includes('Получилось зайти?<br>') || out.includes('<br>Получилось зайти?'),
    'перенос строки потерян: ' + out);
});

// ── Отсечение невалидных href ───────────────────────────────────────────────
function assertNoAnchor(label, input) {
  check(label, () => {
    const out = renderText(input);
    assert.ok(!out.includes('<a '), 'тег <a> не должен появиться: ' + out);
    assert.ok(!out.includes('<a>'), 'тег <a> не должен появиться: ' + out);
  });
}

assertNoAnchor('не-https (http://) не проходит',
  'Зайдите <a href="http://evil.example/x">сюда</a>');
assertNoAnchor('схема javascript: не проходит',
  '<a href="javascript:alert(1)">кабинет</a>');
assertNoAnchor('кавычка в href не проходит',
  '<a href="https://x.example/a"onclick=alert(1)>кабинет</a>');
assertNoAnchor('пробел в href не проходит',
  '<a href="https://x.example/a b">кабинет</a>');
assertNoAnchor('угловая скобка в href не проходит',
  '<a href="https://x.example/a<b">кабинет</a>');
assertNoAnchor('одинарная кавычка в href не проходит',
  "<a href=\"https://x.example/a'onmouseover=alert(1)\">кабинет</a>");

// ── Инъекция атрибутов в валидноподобную форму ──────────────────────────────
check('лишний атрибут в теге от модели не проходит', () => {
  const out = renderText('<a href="https://x.example/" onclick="alert(1)">кабинет</a>');
  assert.ok(!out.includes('<a '), 'форма с лишним атрибутом обязана остаться текстом: ' + out);
  assert.ok(!out.includes('onclick="'), 'сырой onclick в разметке: ' + out);
});

// ── Дыра img: кавычка в адресе картинки не вырывается из src ────────────────
check('кавычка в URL картинки не вырывается из атрибута src', () => {
  const out = renderText('смотрите https://x.example/im"onerror=alert(1)//.jpg');
  assert.ok(!out.includes('"onerror'),
    'сырая кавычка перед onerror — вырыв из src: ' + out);
  assert.ok(!/<img[^>]*"[^>]*onerror/.test(out.replace(/src="[^"]*"/, 'src')),
    'onerror оказался атрибутом img: ' + out);
});

check('одинарная кавычка в URL картинки экранирована', () => {
  const out = renderText("https://x.example/im'onerror=x.jpg");
  assert.ok(!out.includes("im'onerror"), 'сырая одинарная кавычка в src: ' + out);
});

// ── Разрешённые теги и обычный текст ────────────────────────────────────────
check('<b> и <i> восстанавливаются', () => {
  const out = renderText('<b>👨‍💼 Александр:</b>\nтекст <i>курсив</i>');
  assert.ok(out.includes('<b>👨‍💼 Александр:</b>'), out);
  assert.ok(out.includes('<i>курсив</i>'), out);
});

check('прочая разметка экранируется', () => {
  const out = renderText('<script>alert(1)</script> и <u>подчёркнутый</u>');
  assert.ok(!out.includes('<script>'), out);
  assert.ok(!out.includes('<u>'), out);
  assert.ok(out.includes('&lt;script&gt;'), out);
});

check('кавычки в обычном тексте отображаются через сущности', () => {
  const out = renderText('Модель "Vesta" за 1\'500\'000');
  assert.ok(!out.includes('"Vesta"'), 'сырая кавычка в разметке: ' + out);
  assert.ok(out.includes('&quot;Vesta&quot;'), out);
});

// ── Пин против innerHTML в пузыре пользователя ──────────────────────────────
check('appendUserBubble выводит текст через textContent, без innerHTML', () => {
  const fn = extractFunction(src, 'appendUserBubble');
  assert.ok(fn.includes('bubble.textContent = text'), fn.slice(0, 200));
  assert.ok(!fn.includes('bubble.innerHTML'), 'innerHTML в пузыре пользователя');
});

if (failed) {
  console.error(failed + ' проверок упало');
  process.exit(1);
}
console.log('widget_render: все проверки зелёные');
