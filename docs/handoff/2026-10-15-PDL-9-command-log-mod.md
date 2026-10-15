# PDL-9 — мод-журнал command-log для Claude Code

Задача: PDL-9 «[Работа] Мод-журнал command-log для Claude Code». План — PDL-8, комментарий «ИТОГ PDL-8», разделы 1–3 и 5.

## Что сделано

Папка `infra/claude-mods/command-log/`:

- `.claude-plugin/plugin.json` — мод `command-log` 0.1.0;
- `.claude-plugin/marketplace.json` — marketplace `dealer-platform-mods`, источник `./` (мод ставится из папки);
- `hooks/hooks.json`, `hooks/register.ts` — один хук `tool.call` на все инструменты. Сначала `await next(e)` без изменений, потом запись строки журнала в `try/catch`, затем возврат результата как есть. У регистрации `.catch(($, e, next) => next(e))`: если хук упадёт, вызов всё равно пройдёт;
- `tests/command-log.test.ts` — 7 тестов;
- `README.md` — что пишет мод и инструкция для владельца (подготовка, проверка без установки, установка, перезапуск исполнителя, проверка через Multica, удаление).

Журнал: `~/Library/Logs/claude-command-log/<id сессии>.jsonl`, поля `time`, `session`, `cwd`, `tool`, для Bash `program`, `result` (`ok`/`error`).

Детали, которых не было в плане (все — в сторону меньшего объёма данных или надёжности):
- `program` — первое слово без пути (`/usr/local/bin/gh` → `gh`), ведущие `NAME=value` пропускаются (в них бывают токены), слово не из `[A-Za-z0-9._+-]{1,64}` пишется как `?`;
- `$.fs.write` пишет файл целиком и не больше 4 МиБ (`types/claude-code.d.ts`, `fs:`), поэтому строки сессии держатся в памяти, записи идут по очереди, а после 3 МиБ начинается `<сессия>.2.jsonl`; при перезагрузке модуля уже записанный файл дочитывается, а не затирается;
- `result: "error"` ставится и при ошибке инструмента, и при запрете другим хуком (`deny`).

## Что проверено

`claude plugin validate` и `claude plugin test` перед запуском проверены по справке: `claude plugin validate --help` — «Validate a plugin or marketplace manifest…», только чтение; `claude plugin test --help` — «Runs every *.test.ts … in an environment like the one the mod's hooks run in». Ничего не устанавливают.

```
$ claude --version
2.1.299 (Claude Code)

$ claude plugin validate infra/claude-mods/command-log
Validating marketplace manifest: infra/claude-mods/command-log/.claude-plugin/marketplace.json
Validating hooks: infra/claude-mods/command-log/hooks/hooks.json
  ❯ ./register.ts hooks: tool.call
  ❯ ./register.ts gating hook with .catch: tool.call
  ❯ ./register.ts calls: $.clock.now (via write), $.env.get (via write), $.fs.exists (via open), $.fs.read (via open), $.fs.write (via write), $.session.cwd (via write), $.session.id (via write)
  ❯ ./register.ts env writes: nothing
  ❯ ./register.ts env reads: HOME
✔ Validation passed

$ claude plugin test infra/claude-mods/command-log
tests/command-log.test.ts:
(pass) command-log > вызов Bash уходит дальше без изменений и не запрещается
(pass) command-log > для Bash пишется только первое слово, токена в журнале нет
(pass) command-log > присваивание перед командой и путь к программе в журнал не попадают
(pass) command-log > для Write путь и содержимое файла не пишутся
(pass) command-log > ошибка инструмента записывается как error, результат не меняется
(pass) command-log > ошибка записи журнала не роняет и не запрещает вызов
(pass) command-log > строки копятся в одном файле сессии, параллельные вызовы не теряются
 7 pass
 0 fail
```

Тесты идут в среде `claude plugin test`: диск, часы, окружение и инструмент подменены хуками теста, настоящие файлы и Bash не трогаются.

Проверка, что тесты ловят утечку: временно заменил в `register.ts` `programOf(e.command)` на `e.command` — упали 3 теста (токен, присваивание, параллельные вызовы), после возврата кода снова 7 из 7.

Зона: изменены только `infra/claude-mods/command-log/` и этот файл (`git diff --name-only main...HEAD`).

## Что не проверено

- Загрузка мода в живом запуске (обычном и через Multica) — это делает владелец по README, результат — отдельная задача «[Проверка]».
- Проверка типов `tsc`: `tsc` не установлен (`which tsc` → `tsc not found`). Типы косвенно проверены валидатором и тестами.
- gitleaks не установлен (`which gitleaks` → `gitleaks not found`). Дифф просмотрен вручную и `grep -i -E "token|password|secret|bearer|ghp_|sk-"`: находки — только выдуманный токен в тестах и слова в README.
- Ответ «перезапуск исполнителя не нужен» — по источникам (README, раздел «Нужен ли перезапуск исполнителя»), живой проверкой не подтверждён.
- Мод не устанавливался и в моих запусках не подключался; `~/.claude`, настройки Claude Code, Multica и исполнителя не менялись. Навык `plugin-authoring` предлагает писать моды в `~/.claude/dev-mods/…` — туда не писал (запрет задачи), горячая перезагрузка в этой сессии выключена.

## Открытые вопросы

Нет.

## Что дальше

1. Владелец сливает PR и выполняет README по шагам 1–21.
2. Задача «[Проверка]» с выдержками из журнала (шаг 17).
3. Если мод в Multica не загрузился — раздел 5 плана PDL-8 (сначала вариант А, диагностика).
