# Вендорские файлы лендинга

Бот раздаёт эти файлы для лендингов дилеров (план `docs/plans/2026-10-11-001-feat-web-entry-recall-45s-plan.md`,
часть 2): адрес `/widget-custom/vendor/<пакет>/<версия>/<файл>`.

| Пакет | Папка | Лицензия |
|---|---|---|
| `swiper@11.2.10` | `swiper/11.2.10/` | MIT, `swiper/11.2.10/LICENSE` |
| `@tabler/icons-webfont@3.44.0` | `tabler-icons-webfont/3.44.0/` (шрифты — `fonts/`) | MIT, `tabler-icons-webfont/3.44.0/LICENSE` |

- Файлы — **байт в байт** копии `cdn.jsdelivr.net/npm/<пакет>/…`: лендинг сверяет SHA-384 (`integrity`). Не править,
  не переформатировать; `.gitattributes` запрещает git менять переводы строк.
- `manifest.json` — адрес на jsDelivr, размер и `sha384-…` каждого файла. `tests/test_vendor_assets.py` сверяет файлы с
  манифестом, хеши Swiper — с integrity лендинга.
- `.br` и `.gz` рядом с js и css — заранее сжатые копии; бот отдаёт их при `Accept-Encoding`.
- Заголовки (`main.py`, `widget_static_utf8_charset`): `Cache-Control: public, max-age=31536000, immutable`,
  `Access-Control-Allow-Origin: *`, шрифты — `font/woff2`, `font/woff`, `font/ttf`.

Новая версия пакета — новая строка в `tools/vendor-assets.mjs` (новая папка), затем `npm run vendor:fetch`.
Старые папки не меняются и не удаляются, пока на них ссылается лендинг: браузеры кэшируют адреса на год.
Проверка без сети — `npm run vendor:check`.
