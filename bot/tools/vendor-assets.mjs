/*
   Вендорские файлы лендинга, раздаваемые ботом (план 2026-10-11-001, U4):
   widget-custom/vendor/<пакет>/<версия>/… — байт в байт копии jsDelivr.

   node tools/vendor-assets.mjs --fetch   скачать с cdn.jsdelivr.net, сверить
                                          с fastly.jsdelivr.net, положить файлы,
                                          сжатые соседи .br/.gz (js, css) и
                                          widget-custom/vendor/manifest.json
   node tools/vendor-assets.mjs --check   (по умолчанию) сверить файлы с
                                          манифестом без сети

   Новая версия пакета — новая строка в PACKAGES (новая папка); старые файлы
   не меняются: адреса с версией кэшируются браузерами на год (immutable).
*/
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { gzipSync, brotliCompressSync, gunzipSync, brotliDecompressSync, constants as zc } from 'node:zlib';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const VENDOR = join(ROOT, 'widget-custom', 'vendor');
const MANIFEST = join(VENDOR, 'manifest.json');

// npm-пакет → папка на боте; files: путь в пакете → путь в папке версии
const PACKAGES = [
  {
    npm: 'swiper@11.2.10', dir: 'swiper/11.2.10', license: 'MIT',
    files: {
      'swiper-bundle.min.js': 'swiper-bundle.min.js',
      'swiper-bundle.min.css': 'swiper-bundle.min.css',
      'LICENSE': 'LICENSE',
    },
  },
  {
    npm: '@tabler/icons-webfont@3.44.0', dir: 'tabler-icons-webfont/3.44.0', license: 'MIT',
    files: {
      // CSS ссылается на шрифты относительно (./fonts/…) — структура сохранена
      'dist/tabler-icons.min.css': 'tabler-icons.min.css',
      'dist/fonts/tabler-icons.woff2': 'fonts/tabler-icons.woff2',
      'dist/fonts/tabler-icons.woff': 'fonts/tabler-icons.woff',
      'dist/fonts/tabler-icons.ttf': 'fonts/tabler-icons.ttf',
      'LICENSE': 'LICENSE',
    },
  },
];

const COMPRESS = /\.(js|css)$/;   // шрифты не сжимаем: woff2 уже сжат

const sri = (buf) => 'sha384-' + createHash('sha384').update(buf).digest('base64');

async function download(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${url}: HTTP ${resp.status}`);
  return Buffer.from(await resp.arrayBuffer());
}

async function fetchAll() {
  const manifest = { source: 'https://cdn.jsdelivr.net/npm/', files: {} };
  for (const pkg of PACKAGES) {
    for (const [src, dst] of Object.entries(pkg.files)) {
      const cdn = `https://cdn.jsdelivr.net/npm/${pkg.npm}/${src}`;
      const body = await download(cdn);
      const mirror = await download(`https://fastly.jsdelivr.net/npm/${pkg.npm}/${src}`);
      if (!body.equals(mirror)) throw new Error(`${src}: cdn и fastly отдают разные байты`);
      const rel = `${pkg.dir}/${dst}`;
      const path = join(VENDOR, rel);
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, body);
      const entry = { url: cdn, bytes: body.length, integrity: sri(body), license: pkg.license };
      if (COMPRESS.test(dst)) {
        // mtime в заголовке gzip = 0 — результат детерминирован
        writeFileSync(path + '.gz', gzipSync(body, { level: 9 }));
        writeFileSync(path + '.br', brotliCompressSync(body, { params: { [zc.BROTLI_PARAM_QUALITY]: 11 } }));
        entry.compressed = ['br', 'gz'];
      }
      manifest.files[rel] = entry;
      console.log(`${rel.padEnd(52)} ${String(body.length).padStart(8)}  ${entry.integrity}`);
    }
  }
  writeFileSync(MANIFEST, JSON.stringify(manifest, null, 2) + '\n');
}

function check() {
  const manifest = JSON.parse(readFileSync(MANIFEST, 'utf8'));
  const bad = [];
  for (const [rel, entry] of Object.entries(manifest.files)) {
    const path = join(VENDOR, rel);
    if (!existsSync(path)) { bad.push(`${rel}: нет файла`); continue; }
    const body = readFileSync(path);
    if (body.length !== entry.bytes || sri(body) !== entry.integrity) bad.push(`${rel}: байты не совпадают с манифестом`);
    if (entry.compressed) {
      if (!gunzipSync(readFileSync(path + '.gz')).equals(body)) bad.push(`${rel}.gz: не распаковывается в исходник`);
      if (!brotliDecompressSync(readFileSync(path + '.br')).equals(body)) bad.push(`${rel}.br: не распаковывается в исходник`);
    }
  }
  if (bad.length) { console.error(bad.join('\n')); process.exit(1); }
  console.log(`Вендорские файлы совпадают с манифестом (${Object.keys(manifest.files).length}).`);
}

if (process.argv.includes('--fetch')) await fetchAll();
else check();
