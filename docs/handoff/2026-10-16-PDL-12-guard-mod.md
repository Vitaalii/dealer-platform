# PDL-12 — мод с запретами guard для Claude Code

Задача: PDL-12 «[Работа] Мод с запретами guard для Claude Code». План — PDL-11, комментарий «ИТОГ PDL-11», разделы 1–9; ответы владельца на открытые вопросы — в описании PDL-12.

## Что сделано

Папка `infra/claude-mods/guard/`:

- `.claude-plugin/plugin.json` — мод `guard` 0.1.0; `.claude-plugin/marketplace.json` — marketplace `dealer-platform-guard`, источник `./`;
- `hooks/hooks.json`, `hooks/register.ts` — один хук `tool.call`: проверка до `next(e)`, при нарушении — `{ deny }` и строка в журнале `~/Library/Logs/claude-guard/<сессия>.jsonl` (`time`, `session`, `cwd`, `tool`, `rule`). У регистрации `.catch`: при ошибке мода вызов с опасными словами запрещается, остальное проходит (ответ 6 владельца);
- `hooks/rules.ts` — разбор команды Bash (части по `&&`, `||`, `;`, `|`, переводам строк, `$(…)`, обратным кавычкам, `<(…)`, heredoc; присваивания и обёртки `env`, `sudo`, `nohup`, `time`, `nice`, `timeout`, `xargs`; `bash -c`, `eval`, `find -exec`; учёт `cd`) и три запрета из плана;
- `tests/guard.test.ts` — 147 тестов: 92 «должно запрещаться», 40 «должно проходить», 5 на текст запрета и журнал, 10 «сторож падает»;
- `README.md` — что запрещает, текст отказа, журнал, ложные срабатывания и пропуски, инструкция для владельца (подготовка, установка, короткая проверка, обновление, выключение одной командой и возврат, удаление). Шаги с `claude -p` — на экране второго MacBook.

Ответы владельца учтены: мод действует везде (1); `api.telegram.org` и `platform-api.max.ru` запрещены (2); старые репозитории — запрещено всё, включая `git clone` (3); свой журнал без текста команды (4); `~/.multica` — тем же правилом, что `~/.claude` (5); поведение при ошибке мода — как в плане (6).

Детали, которых в плане не было (все — уточнения внутри тех же трёх правил; владельцу стоит знать):
- Bash-правила применяются и к инструменту `Monitor` — он тоже запускает команду оболочки (`types/claude-code.d.ts`, `Monitor.command`), иначе через него запрет обходился бы;
- вложенный запуск без модов: кроме четырёх флагов из плана — ещё `--settings` и `CLAUDE_CONFIG_DIR=…` (тот же обход: другие настройки, где мод выключен);
- в список «только читают» (адрес в аргументах не запрещается) добавлены `echo`, `printf`, `sort`, `uniq`, `cut`, `tr`, `diff`, `jq`, `multica` и подкоманды `git`, которые не ходят в сеть (`commit`, `add`, `status` и т. п.) — иначе `git commit -m "…dockhost…"` или `multica … | grep dockhost` запрещались бы;
- `git push --prune` и шаблон `*` в ветке — тоже `push-force` (могут удалить или перезаписать `main`);
- `gh api graphql` с `mergePullRequest`, `enablePullRequestAutoMerge`, `updateRef`, `deleteRef`, `createCommitOnBranch`, `mergeBranch` — запрет (то же слияние, что `gh pr merge`, другим путём);
- тело heredoc проверяется как команды только у оболочек (`bash <<EOF`); у остальных программ — только на адреса старой системы. Поэтому `git commit -m "$(cat <<'EOF' … EOF)"` с текстом про main проходит.

## Что проверено

```
$ claude --version
2.1.299 (Claude Code)

$ claude plugin validate infra/claude-mods/guard
Validating marketplace manifest: infra/claude-mods/guard/.claude-plugin/marketplace.json

Validating hooks: infra/claude-mods/guard/hooks/hooks.json

  ❯ ./register.ts hooks: tool.call
  ❯ ./register.ts gating hook with .catch: tool.call
  ❯ ./register.ts calls: $.clock.now (via writeLog), $.env.get, $.fs.exists (via open), $.fs.read (via open), $.fs.stat (via envOf), $.fs.write (via writeLog), $.process.run (via envOf), $.session.cwd, $.session.id (via writeLog)
  ❯ ./register.ts env writes: nothing
  ❯ ./register.ts env reads: HOME

✔ Validation passed

$ claude plugin test infra/claude-mods/guard
tests/guard.test.ts:
(pass) … 147 строк …
 147 pass
 0 fail
Ran 147 tests across 1 file. [10.91s]
```

Тесты идут в среде `claude plugin test`: окружение, папка сессии, git (`process.run`), диск (`fs.*`) и сам инструмент подменены хуками теста, настоящие Bash, git и файлы не трогаются.

Проверка, что тесты ловят ошибки (код потом возвращён как был):
- `checkCall` всегда «можно» — упали 94 теста (все «должно запрещаться» на тот момент и тест текста запрета);
- `main|master` в правиле push заменены на несуществующее имя — упали 17 тестов (все `push-main` и тест текста).

Зона: изменены только `infra/claude-mods/guard/` и этот файл (`git diff --name-only origin/main...HEAD`).

Секреты: в коде только имена хостов и репозиториев, которые уже есть в репозитории (`bot/`, `n8n/`, `landing/SOURCE.md`, `CLAUDE.md`); в тестах — выдуманный `fake-token-0000-not-a-secret`, как в тестах `command-log`.

## Что не проверено

- gitleaks не установлен на машине (`which gitleaks` → `gitleaks not found`); дифф просмотрен вручную на токены, пароли, ключи, телефоны и email.
- Проверка типов `tsc` не запускалась: `tsc` не установлен (`which tsc` → `tsc not found`), ставить пакеты задача не разрешает. `claude plugin validate` и `claude plugin test` прошли.
- Мод не устанавливался и в моих запусках не подключался (по «Границам»). Работа на живых запусках, порядок с `command-log` и поведение уже идущего запуска после `disable` — в задаче «[Проверка]».
- Тексты ответов `claude plugin marketplace add|install|disable|enable|uninstall` в README описаны словами («сообщение, что …»), без точного вывода: команды не запускал, чтобы ничего не ставить.

## Открытые вопросы

Нет. Отступления от плана перечислены выше — если какое-то не нужно, убрать одной правкой.

## Что дальше

1. Ревью PR.
2. Владелец после слияния ставит мод по `infra/claude-mods/guard/README.md`, разделы 1–3.
3. Задача «[Проверка]»: провокация 3 запретов и 3 разрешённых действий в запуске Multica, `grep` по журналам `claude-guard` и `claude-command-log`.
