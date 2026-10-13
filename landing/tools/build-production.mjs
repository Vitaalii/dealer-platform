#!/usr/bin/env node
/* Сборка блоков Tilda T123 для вставки.

   Исходники с комментариями — docs/tilda-blocks/Ver_4_dev/*.html.
   Две сборки с теми же именами, без комментариев (HTML, CSS, JS) и
   лишних пробелов. В Tilda вставляется ТОЛЬКО сборка:
     Ver_4_dev/Production/      — боевые страницы (режим 'prod');
     Ver_4_dev/Production-test/ — тестовая page154296216 (режим 'test').
   Режим подставляется в константу UC_BUILD_MODE (terser global_defs):
   в U2 набор значений другого режима выбрасывается как мёртвый код.
   Остальные блоки в двух папках одинаковы байт в байт — это проверяется. Причины: лимит блока T123 (100 000 байт)
   и то, что комментарии видны в коде страницы.

   Код разбирают парсеры, а не регулярки: html-minifier-terser для HTML,
   внутри него terser для JS и clean-css для CSS. Логика не меняется:
   terser не переименовывает имена верхнего уровня (кросс-блочные
   глобалы var/function), строки и URL не трогаются, пометки Tilda
   <!-- nominify --> сохраняются. Любая ошибка разбора останавливает
   сборку — несжатый кусок молча не проскочит.

   npm run build:prod             — собрать обе папки, таблица размеров
   npm run check:prod             — собрать в памяти и сравнить с обеими
                                    папками; выход 1, если сборка устарела
   node tools/build-production.mjs --stdin --mode test|prod < in.html > out.html
                                  — собрать один фрагмент (для харнессов)
*/
import { readFileSync, writeFileSync, readdirSync, mkdirSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { minify as minifyHtml } from 'html-minifier-terser';
import { minify as minifyJs } from 'terser';
import CleanCSS from 'clean-css';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC  = join(ROOT, 'docs', 'tilda-blocks', 'Ver_4_dev');
const BUILDS = [
    { mode: 'prod', dir: join(SRC, 'Production') },
    { mode: 'test', dir: join(SRC, 'Production-test') }
];
/* Значения, которых не должно быть в сборке чужого режима (U2).
   Боевой бот в тестовой сборке допустим в одном месте — неизменяемые
   файлы /widget-custom/vendor/ (Swiper, Tabler; план 2026-10-12-001).
   Проверка — по пути сразу за хостом, не по подстроке: путь должен
   начинаться ровно с /widget-custom/vendor/, без «..» и без
   %-кодировки; хост ищется без учёта регистра, а допускается только в
   нижнем. Любое другое вхождение хоста (/cabinet/api/, /catalog/, голый
   адрес бота, порт) по-прежнему валит сборку. */
const PROD_BOT_HOST = 'obut-5oaj-5i2u.gw-1a.dockhost.net';
const VENDOR_PATH = '/widget-custom/vendor/';
const FOREIGN = {
    prod: [/ngrok-free\.dev/gi, /\/cabinet_dev/g],
    test: []
};
function prodBotHits(text) {
    const hits = [];
    const re = new RegExp(PROD_BOT_HOST.replace(/[.-]/g, '\\$&'), 'gi');
    for (const m of text.matchAll(re)) {
        const rest = text.slice(m.index + m[0].length).match(/^[^"'`\s<>)]*/)[0];
        const ok = m[0] === PROD_BOT_HOST && rest.startsWith(VENDOR_PATH)
            && !/(^|\/)\.\.(\/|$)/.test(rest) && rest.indexOf('%') === -1;
        if (!ok) hits.push(text.slice(m.index, m.index + 60));
    }
    return hits;
}
function foreignHits(mode, text) {
    const hits = mode === 'test' ? prodBotHits(text) : [];
    for (const re of FOREIGN[mode]) {
        for (const m of text.matchAll(re)) hits.push(text.slice(m.index, m.index + 60));
    }
    return hits;
}
/* Самопроверка правила при каждом запуске (и в --check, то есть в хуке):
   ослабнет правило — сборка падает раньше, чем соберётся что-нибудь. */
const BOT = 'https://' + PROD_BOT_HOST;
const FOREIGN_CASES = [
    ['test', BOT + '/widget-custom/vendor/swiper/11.2.10/swiper-bundle.min.js', true],
    ['test', '"' + BOT + '/widget-custom/vendor/tabler-icons-webfont/3.44.0/tabler-icons.min.css"', true],
    ['test', BOT + '/cabinet/api/dealer/', false],
    ['test', BOT + '/catalog/', false],
    ['test', "'" + BOT + "'", false],
    ['test', BOT + '/widget-custom/widget.js', false],
    ['test', BOT + '/widget-custom/vendorX/swiper.js', false],
    ['test', BOT + '//widget-custom/vendor/swiper.js', false],
    ['test', BOT + ':443/widget-custom/vendor/swiper.js', false],
    ['test', BOT + '/widget-custom/vendor/a.js ' + BOT + '/cabinet/api/web/open', false],
    ['test', BOT + '/widget-custom/vendor/../../cabinet/api/dealer/', false],
    ['test', BOT + '/widget-custom/vendor/swiper/..', false],
    ['test', BOT + '/widget-custom/vendor/%2e%2e/cabinet/api/', false],
    ['test', BOT + '/widget-custom/vendor/%73wiper.js', false],
    ['test', 'https://OBUT-5oaj-5i2u.gw-1a.DOCKHOST.net/widget-custom/vendor/swiper.js', false],
    ['test', 'https://Obut-5oaj-5i2u.gw-1a.dockhost.net/cabinet/api/', false],
    ['prod', BOT + '/cabinet/api/dealer/', true],
    ['prod', 'https://sporophoric-devorah-remonstrative.ngrok-free.dev', false],
    ['prod', '/cabinet_dev', false]
];
/* То же на настоящем исходнике U2: путь боевого бота подставляется в его
   первый <script>, блок собирается в тестовом режиме, и защита должна
   отклонить всё, кроме vendor. Проверяет связку «минификация → поиск»,
   а не только правило на строках. */
const FOREIGN_PROBES = [
    ['/cabinet/api/dealer/', false],
    ['/catalog/x', false],
    ['/widget-custom/widget.js', false],
    ['/widget-custom/vendor/swiper/11.2.10/probe.js', true]
];
async function checkForeignOnRealU2(files) {
    const u2 = files.find(f => f.startsWith('u2-'));
    const src = readFileSync(join(SRC, u2), 'utf8');
    if (src.indexOf('<script>') === -1) throw new Error(u2 + ': нет <script> для пробы защиты');
    const bad = [];
    for (const [path, ok] of FOREIGN_PROBES) {
        const probe = src.replace('<script>', "<script>\nconsole.log('" + BOT + path + "');\n");
        const built = await buildBlock(probe, 'test');
        if (built.indexOf(BOT + path) === -1) { bad.push(path + ' — проба выпала из сборки'); continue; }
        const passed = foreignHits('test', built).length === 0;
        if (passed !== ok) bad.push(path + ' — ожидалось ' + (ok ? 'пропустить' : 'отклонить'));
    }
    if (bad.length) {
        console.error('⛔ Защита от боевого бота в Production-test на исходнике U2:\n   ' + bad.join('\n   '));
        process.exit(2);
    }
}
function checkForeignRule() {
    const bad = FOREIGN_CASES.filter(([mode, text, ok]) => (foreignHits(mode, text).length === 0) !== ok);
    if (bad.length) {
        console.error('⛔ Правило «значения чужого режима» сломано:\n' +
            bad.map(([mode, text, ok]) => '   ' + mode + ': ' + text + ' — ожидалось ' + (ok ? 'пропустить' : 'отклонить')).join('\n'));
        process.exit(2);
    }
}
const T123_LIMIT = 100000;   // байт; Tilda отказывается сохранять блок больше
const WARN_SHARE = 0.8;      // выше 80 % лимита — сигнал к выносу кода в отдельный блок

const cleanCss = new CleanCSS({ level: 1, returnPromise: false });

/* html-minifier-terser зовёт обработчик CSS для трёх случаев:
   содержимое <style> (type не задан), атрибут style (type 'inline' —
   список объявлений без селектора) и атрибут media (type 'media').
   clean-css понимает только таблицы стилей: объявления без селектора он
   выбрасывает целиком (так однажды опустели все инлайн-стили блоков).
   Поэтому, как сама библиотека, заворачиваем в правило и разворачиваем
   обратно; не развернулось — ошибка сборки, а не молча пустой атрибут. */
function cssMinifier(text, type) {
    const wrapped = type === 'inline' ? '*{' + text + '}'
                  : type === 'media'  ? '@media ' + text + '{a{top:0}}'
                  : text;
    const out = cleanCss.minify(wrapped);
    if (out.errors.length) throw new Error('CSS: ' + out.errors.join('; '));
    if (!type) return out.styles;
    const m = type === 'inline' ? out.styles.match(/^\*\{([\s\S]*)\}$/)
                                : out.styles.match(/^@media ([\s\S]*?)\s*\{[\s\S]*\}$/);
    if (!m) throw new Error('CSS (' + type + '): не удалось развернуть «' + text.slice(0, 80) + '»');
    if (type === 'inline' && text.trim() && !m[1].trim()) throw new Error('CSS (inline): стили пропали: «' + text.slice(0, 80) + '»');
    return m[1];
}

/* Классические <script> страницы: top-level имена — глобалы, на них
   ссылаются другие блоки, поэтому toplevel: false (не переименовывать
   и не удалять). Остальное — безопасные настройки по умолчанию. */
function jsMinifier(mode) {
  return async function (text, inline) {
    const res = await minifyJs(text, {
        ecma: 2017,
        toplevel: false,
        compress: { defaults: true, drop_console: false, passes: 2,
                    global_defs: { UC_BUILD_MODE: mode } },
        mangle: true,
        format: { comments: false, ascii_only: false },
        parse: { bare_returns: !!inline }
    });
    if (typeof res.code !== 'string') throw new Error('JS: terser вернул пустой результат');
    return res.code;
  };
}

export async function buildBlock(html, mode) {
    if (mode !== 'prod' && mode !== 'test') throw new Error('режим сборки: prod или test');
    return minifyHtml(html, {
        collapseWhitespace: true,
        conservativeCollapse: true,
        removeComments: true,
        ignoreCustomComments: [/^\s*nominify\b/],
        caseSensitive: true,
        decodeEntities: false,
        removeAttributeQuotes: false,
        keepClosingSlash: true,
        minifyCSS: cssMinifier,
        minifyJS: jsMinifier(mode)
    });
}

function kb(n) { return (n / 1000).toFixed(1).replace('.', ',') + ' КБ'; }

async function main() {
    const args = process.argv.slice(2);

    if (args.includes('--stdin')) {
        const i = args.indexOf('--mode');
        const mode = i !== -1 ? args[i + 1] : 'test';
        const input = readFileSync(0, 'utf8');
        process.stdout.write(await buildBlock(input, mode));
        return;
    }

    checkForeignRule();
    const check = args.includes('--check');
    const files = readdirSync(SRC).filter(f => f.endsWith('.html')).sort();
    await checkForeignOnRealU2(files);
    const built = {};          // built[mode][file]
    for (const b of BUILDS) {
        built[b.mode] = {};
        for (const f of files) {
            try {
                built[b.mode][f] = await buildBlock(readFileSync(join(SRC, f), 'utf8'), b.mode);
            } catch (e) {
                console.error('⛔ ' + f + ' (' + b.mode + '): ' + e.message);
                process.exit(2);
            }
        }
    }

    /* Инварианты сборки: блоки кроме U2 одинаковы в обеих папках;
       в U2 нет значений чужого режима. */
    const problems = [];
    for (const f of files) {
        if (!f.startsWith('u2-') && built.prod[f] !== built.test[f]) problems.push(f + ': различается между режимами');
    }
    for (const b of BUILDS) {
        const u2 = Object.keys(built[b.mode]).find(f => f.startsWith('u2-'));
        for (const v of u2 ? foreignHits(b.mode, built[b.mode][u2]) : []) {
            problems.push(u2 + ' (' + b.mode + '): осталось значение чужого режима «' + v + '…»');
        }
    }
    /* Цвет «ДАЛЕЕ →» — одной строкой в начале U1 (её правят руками при
       вставке, см. чек-лист выкладки) и нигде больше. Подсказки в сборке
       нет (решение владельца 2026-10-08: в боевом коде — никаких
       комментариев о внутренней кухне), поэтому строка опознаётся по
       виду: первый <style> блока сразу после nominify, в нём только
       :root с двумя значениями цвета по умолчанию. */
    for (const b of BUILDS) for (const f of files) {
        const has = built[b.mode][f].includes('--uc-btn-next-bg:');
        if (f.startsWith('u1-')) {
            if (!/^<!-- nominify begin -->\s*<style>:root\{--uc-btn-next-bg:#2D5467;--uc-btn-next-bg-hover:#3C728D\}<\/style>/.test(built[b.mode][f]))
                problems.push(f + ' (' + b.mode + '): строка цвета кнопки не в начале блока или не в ожидаемом виде: ' + built[b.mode][f].slice(0, 260));
        } else if (has) problems.push(f + ' (' + b.mode + '): объявляет цвет кнопки — он задаётся только в U1');
        // Сохраняемых комментариев /*! … */ в сборке быть не должно: они видны в коде страницы
        if (/\/\*!/.test(built[b.mode][f])) problems.push(f + ' (' + b.mode + '): в сборке остался комментарий /*! … */');
        // Кириллица в CSS вне строк и сохраняемых комментариев — признак комментария, закрытого раньше времени
        const css = (built[b.mode][f].match(/<style>[\s\S]*?<\/style>/g) || []).join('')
            .replace(/\/\*![\s\S]*?\*\//g, '').replace(/(["'])(?:\\.|(?!\1)[^\\])*\1/g, '');
        if (/[А-Яа-яЁё]/.test(css)) problems.push(f + ' (' + b.mode + '): в CSS текст вне комментария — комментарий закрыт раньше времени?');
    }
    if (problems.length) { console.error('⛔ ' + problems.join('\n⛔ ')); process.exit(2); }

    if (check) {
        const stale = [];
        for (const b of BUILDS) for (const f of files) {
            const target = join(b.dir, f);
            const current = existsSync(target) ? readFileSync(target, 'utf8') : null;
            if (current !== built[b.mode][f]) stale.push(b.dir.split('/').pop() + '/' + f);
        }
        if (stale.length) {
            console.error('⛔ Сборка устарела — пересоберите (npm run build:prod): ' + stale.join(', '));
            process.exit(1);
        }
        console.log('✓ Production и Production-test актуальны (' + files.length + ' блоков)');
        return;
    }

    for (const b of BUILDS) {
        if (!existsSync(b.dir)) mkdirSync(b.dir);
        for (const f of files) writeFileSync(join(b.dir, f), built[b.mode][f]);
    }

    console.log('| Блок | Исходник | Production (бой) | Production-test | Доля лимита (макс.) | Запас до 100 000 байт |');
    console.log('|---|---|---|---|---|---|');
    let over = false;
    for (const f of files) {
        const src = Buffer.byteLength(readFileSync(join(SRC, f), 'utf8'));
        const p = Buffer.byteLength(built.prod[f]), t = Buffer.byteLength(built.test[f]);
        const m = Math.max(p, t), share = m / T123_LIMIT;
        const flag = share > 1 ? ' ⛔' : share > WARN_SHARE ? ' ⚠️' : '';
        if (share > WARN_SHARE) over = true;
        console.log(`| ${f} | ${kb(src)} | ${kb(p)} | ${kb(t)} | ${Math.round(share * 100)} %${flag} | ${kb(T123_LIMIT - m)} |`);
    }
    if (over) console.log('\n⚠️ Есть блоки выше ' + Math.round(WARN_SHARE * 100) + ' % лимита T123.');
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
    main().catch(e => { console.error(e); process.exit(2); });
}
