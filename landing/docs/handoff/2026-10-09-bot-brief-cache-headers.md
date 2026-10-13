# ТЗ агенту бота: заголовки кэша у `/catalog/{table}` и `/bot-links`

Дата: 2026-10-09. От лендинга (план `docs/plans/2026-10-08-005-perf-faster-ready-plan.md`, вариант г2; в CLAUDE.md лендинга — отложенный R4). Решение владельца: в ближайший релиз бота, `max-age` 300 с, со `stale-while-revalidate` и `ETag`.

## Зачем

При повторном визите (в течение 5 минут) настройки дилера уже берутся из кэша браузера: `/cabinet/api/dealer/{code}` отдаёт `Cache-Control: public, max-age=300`. Каталог и боты — нет: у `/catalog/{table}` и `/bot-links` нет ни `Cache-Control`, ни `ETag`. Поэтому повторный визит всё равно ждёт запрос каталога — 0,34–0,72 с на быстрой сети и 0,85–1,39 с на Slow 4G (замер 2026-10-08). С кэшем это ожидание исчезает.

## Что сейчас отдаёт бот (curl, 2026-10-09, `Origin: https://offer.geely-radar.ru`)

| Путь | `Cache-Control` | `ETag` | `Access-Control-Allow-Origin` | `Vary` |
|---|---|---|---|---|
| `/cabinet/api/dealer/01` | `public, max-age=300` | нет | отражает Origin | `Origin` |
| `/catalog/mywf0f16ptn25gk` | нет | нет | `*` | нет |
| `/bot-links` | нет | нет | `*` | нет |

## Что сделать

Для `GET /catalog/{table}` и `GET /bot-links`, только для ответов **200**:

1. `Cache-Control: public, max-age=300, stale-while-revalidate=300`.
   Устаревший ответ браузер может отдать ещё до 5 минут, обновляя его в фоне. Худший случай — цены 10-минутной давности.
2. `ETag` — хеш тела ответа: тех же байт JSON, что уходят клиенту, до сжатия. Например, `W/"<sha1 первых 16 hex>"`. Слабый (`W/`) — потому что gzip меняет байты, а смысл ответа тот же.
3. Запрос с `If-None-Match`, совпавшим с текущим `ETag`, получает **304** без тела. В ответе должны быть те же `Cache-Control`, `ETag` и CORS-заголовки, что у 200 (`Access-Control-Allow-Origin` обязателен, иначе браузер отклонит ответ).
4. Ответы **не 200** (404, 500, 503, ошибки NocoDB) — `Cache-Control: no-store`. Иначе один сбой каталога закэшируется на 5 минут, и всё это время страница будет показывать «каталог недоступен».
5. `Access-Control-Allow-Origin: *` можно оставить как есть. Если когда-нибудь он начнёт отражать Origin (как у `/cabinet/api/dealer/`), нужен ещё `Vary: Origin`.

Чего не трогать:
- `/cabinet/api/dealer/{code}` — уже правильно (`max-age=300`). `stale-while-revalidate` добавить можно, но не обязательно.
- `POST /cabinet/api/web/open` и прочие POST — не кэшировать.

С 2026-10-09 лендинг (U2 боевой сборки) шлёт GET к боту **без** заголовков. Это простой запрос CORS, браузер больше не делает перед ним OPTIONS. Бот должен по-прежнему отвечать на такой GET с `Access-Control-Allow-Origin` — сейчас отвечает.

## Как проверить

```sh
B=https://obut-5oaj-5i2u.gw-1a.dockhost.net
O='Origin: https://offer.geely-radar.ru'

# 1. Заголовки у 200
curl -s -o /dev/null -D - --compressed -H "$O" $B/catalog/mywf0f16ptn25gk | grep -iE 'cache-control|etag|access-control-allow-origin'
curl -s -o /dev/null -D - -H "$O" $B/bot-links | grep -iE 'cache-control|etag|access-control-allow-origin'
#   ждём: cache-control: public, max-age=300, stale-while-revalidate=300; etag: W/"…"; access-control-allow-origin

# 2. 304 по ETag — с CORS
E=$(curl -s -o /dev/null -D - --compressed -H "$O" $B/catalog/mywf0f16ptn25gk | awk -F': ' 'tolower($1)=="etag"{print $2}' | tr -d '\r')
curl -s -o /dev/null -D - --compressed -H "$O" -H "If-None-Match: $E" $B/catalog/mywf0f16ptn25gk | head -1
#   ждём: HTTP/2 304, и в заголовках access-control-allow-origin, cache-control, etag

# 3. ETag меняется вместе с данными: поменять поле в каталоге NocoDB, повторить п. 1 — ETag другой.
#    Если у бота свой кэш NocoDB — после его истечения.

# 4. Ошибки не кэшируются
curl -s -o /dev/null -D - $B/catalog/zzzzzzzzzzzzzzz | grep -iE '^HTTP|cache-control'
#   ждём: 404 и cache-control: no-store
```

В браузере (после релиза): открыть https://offer.geely-radar.ru, затем открыть её ещё раз в той же вкладке. В DevTools → Network запросы `/catalog/…` и `/bot-links` должны идти «(disk cache)», а через 5 минут — уходить с `If-None-Match` и получать 304. Лендинг тоже проверит это своим замером (`npm run measure:ready`, кэш warm): окно повторного визита должно стать ≈ 0.

## Что сообщить лендингу

Версию релиза и вывод п. 1–2 и п. 4 для `/catalog/mywf0f16ptn25gk` и `/bot-links`.
