#!/usr/bin/env node
/* Сборка кабинета (ускорение A2/A3, решение владельца 2026-10-03).

   Исходники с комментариями — widget-custom/cabinet-app/*.js (их читают
   тесты и разработка). На страницы Tilda (/cabinet, /cabinet_dev)
   подключается ТОЛЬКО сборка:
     widget-custom/cabinet-app/Production/<имя>.js   — без комментариев, минификация;
     widget-custom/cabinet-app/Production/<имя>.js.gz / .br — заранее сжатые копии,
       aiohttp отдаёт их сам по Accept-Encoding;
     widget-custom/cabinet-app/Production/manifest.json — версия каждого файла
       (первые 10 hex sha256 сборки) и sha256 исходника.

   Версия идёт в адрес: …/Production/app.js?v=<версия>. Бот отдаёт файл
   с долгим кэшем (immutable), только если v совпадает с манифестом; иначе —
   no-cache (устаревший ?v= на странице не закэширует чужую копию на год).

   Классические <script>: имена верхнего уровня — глобалы, на них ссылаются
   соседние файлы (calc_model → функции движка), поэтому toplevel: false
   (не переименовывать и не удалять). Любая ошибка разбора останавливает
   сборку.

   npm run build:cabinet   — собрать и вывести строки подключения для Tilda
   npm run check:cabinet   — собрать в памяти и сравнить с Production;
                             выход 1, если сборка устарела
*/
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { gzipSync, brotliCompressSync, constants as zc } from 'node:zlib';
import { minify } from 'terser';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'widget-custom', 'cabinet-app');
const OUT = join(SRC, 'Production');
// Порядок подключения обязателен: движок → модель → приложение
const FILES = ['calc_engine.browser.js', 'calc_model.js', 'app.js'];

const sha = (buf) => createHash('sha256').update(buf).digest('hex');

async function buildOne(name) {
  const source = readFileSync(join(SRC, name), 'utf8');
  const res = await minify(source, {
    ecma: 2017,
    toplevel: false,
    compress: { defaults: true, passes: 2, drop_console: false },
    mangle: true,
    format: { comments: false, ascii_only: false },
  });
  if (!res.code) throw new Error(`${name}: пустой результат terser`);
  const code = Buffer.from(res.code + '\n', 'utf8');
  return {
    name,
    code,
    gz: gzipSync(code, { level: 9 }),     // mtime в заголовке gzip = 0 — сборка детерминирована
    br: brotliCompressSync(code, { params: { [zc.BROTLI_PARAM_QUALITY]: 11 } }),
    sourceSha: sha(Buffer.from(source, 'utf8')),
    sourceBytes: Buffer.byteLength(source, 'utf8'),
  };
}

function manifestOf(builds) {
  const files = {};
  for (const b of builds) {
    files[b.name] = {
      version: sha(b.code).slice(0, 10),
      source_sha256: b.sourceSha,
      bytes: b.code.length,
      gzip_bytes: b.gz.length,
      br_bytes: b.br.length,
    };
  }
  return JSON.stringify({ order: FILES, files }, null, 2) + '\n';
}

const check = process.argv.includes('--check');
const builds = [];
for (const name of FILES) builds.push(await buildOne(name));
const manifest = manifestOf(builds);

if (check) {
  const stale = [];
  for (const b of builds) {
    for (const [suffix, buf] of [['', b.code], ['.gz', b.gz], ['.br', b.br]]) {
      const p = join(OUT, b.name + suffix);
      if (!existsSync(p) || !readFileSync(p).equals(buf)) stale.push(b.name + suffix);
    }
  }
  const mp = join(OUT, 'manifest.json');
  if (!existsSync(mp) || readFileSync(mp, 'utf8') !== manifest) stale.push('manifest.json');
  if (stale.length) {
    console.error('Сборка кабинета устарела: ' + stale.join(', ') + '\nЗапустите: npm run build:cabinet');
    process.exit(1);
  }
  console.log('Сборка кабинета актуальна.');
  process.exit(0);
}

mkdirSync(OUT, { recursive: true });
for (const b of builds) {
  writeFileSync(join(OUT, b.name), b.code);
  writeFileSync(join(OUT, b.name + '.gz'), b.gz);
  writeFileSync(join(OUT, b.name + '.br'), b.br);
}
writeFileSync(join(OUT, 'manifest.json'), manifest);

const m = JSON.parse(manifest).files;
console.log('файл                     исходник   сборка     gzip      br');
for (const b of builds) {
  const f = m[b.name];
  console.log(`${b.name.padEnd(24)} ${String(b.sourceBytes).padStart(8)} ${String(f.bytes).padStart(8)} ${String(f.gzip_bytes).padStart(8)} ${String(f.br_bytes).padStart(7)}`);
}
console.log('\nСтроки подключения для Tilda (BOT — адрес бота):');
for (const name of FILES) {
  console.log(`<script src="BOT/widget-custom/cabinet-app/Production/${name}?v=${m[name].version}"></script>`);
}
