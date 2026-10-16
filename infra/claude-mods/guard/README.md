# guard — мод с запретами для Claude Code

Мод смотрит на каждый вызов инструмента **до** запуска и запрещает его, если вызов нарушает одно из трёх правил. Остальное проходит без изменений. План — задача PDL-11 (комментарий «ИТОГ PDL-11»), работа — PDL-12.

Это страховка от ошибок агента, а не от умысла: команда, записанная в файл-скрипт и запущенная потом (`bash x.sh`), закодированная строка (`base64 -d`) или программа на Python, которая сама ходит в сеть, мимо пройдут. Правила в `CLAUDE.md` и проверка Ревьюером остаются.

Мод действует во всех сессиях Claude Code пользователя `agent`, где он установлен: и в запусках Multica, и в ручных `claude` (решение владельца, вариант А). Команды, которые владелец набирает в терминале сам (по SSH или на экране), через Claude Code не идут — мод их не видит и не мешает.

Сделан под Claude Code `2.1.299`. API модов в этой версии — «early access» и может меняться между версиями.

## Что запрещает

**1. main, force-push, слияние PR** (`CLAUDE.md`, раздел «В конце работы»):
- `git push` в `main` или `master` в любой форме: `origin main`, `HEAD:main`, `x:refs/heads/main`, по URL, через `git -C`/`git -c`, `bash -c`, `eval`, `$(…)`, `find -exec`, `xargs`, после `cd`;
- `git push` без ветки (`git push`, `git push origin`, `git push -u origin HEAD`), если текущая ветка — `main`/`master`. Ветку мод спрашивает командой `git symbolic-ref --short HEAD` в папке команды. Не смог узнать — запрет с подсказкой указать ветку явно;
- `git push` с `--force`, `-f`, `--force-with-lease`, `--force-if-includes`, `+ветка`, `:ветка`, `--delete`/`-d`, `--mirror`, `--all`, `--prune`, шаблоном `*`;
- `gh pr merge` (и с `--auto`), `gh repo sync`, `gh api` с записью в `…/merges`, `…/pulls/<n>/merge`, `…/git/refs`, `…/contents/…`, `…/branches/…/protection`, `gh api graphql` с `mergePullRequest` и похожими;
- инструменты MCP `…merge_pull_request`, `…push_files`, `…create_or_update_file`.

**2. Старая система и SSH** (`CLAUDE.md`, раздел «Запреты»; навык `prod-safety`):
- `ssh`, `scp`, `sftp`, `mosh`, `autossh`, `sshpass`; `rsync` на удалённый хост или с `-e ssh`; `docker` с `-H`/`--context`/`DOCKER_HOST`, `docker login`, `docker push`;
- любая программа, кроме тех, что только читают файлы, с адресом `*.dockhost.net`, `*.bitrix24.*`, `sms-prosto.ru`, `hub.docker.com`, `registry-1.docker.io`, `api.telegram.org`, `platform-api.max.ru`; `WebFetch` на эти адреса;
- старые репозитории `Vitaalii/unifiedbot`, `Vitaalii/unifiedbot-n8n`, `Vitaalii/site_graber` — всё, включая `git clone` и `gh` (кроме `--help`);
- запуск скриптов из `bot/SOURCE.md` и `n8n/SOURCE.md`, раздел «Скрипты с записью во внешние сервисы» (по имени файла).

Программы, которые только читают (адрес в их аргументах не запрещается): `grep`, `rg`, `cat`, `head`, `tail`, `less`, `sed` без `-i`, `awk`, `wc`, `ls`, `find`, `echo`, `printf`, `sort`, `uniq`, `cut`, `tr`, `diff`, `jq`, `multica`, а также `git`, кроме подкоманд, которые ходят в сеть (`clone`, `fetch`, `pull`, `push`, `remote`, `ls-remote`, `submodule`, `config` и т. п.).

**3. Сам мод, `~/.claude`, `~/.multica`, настройки** (правила агентов; навык `zone-guard`):
- `Edit`, `Write`, `NotebookEdit` внутри `~/.claude/` (кроме `~/.claude/projects/*/memory/` — память агента), `~/claude-mods/`, `~/.multica/` и любой `.claude/settings.json`/`.claude/settings.local.json`. Путь сверяется и после раскрытия ссылок;
- в Bash: `rm`, `mv`, `cp`, `ln`, `tee`, `chmod`, `touch`, `mkdir`, `sed -i`, `curl -o` и т. п. и перенаправление `>`/`>>` с такими путями; `git` с записью в этих папках (`git -C ~/claude-mods/… pull`);
- `claude plugin disable|enable|install|uninstall|update|configure`, `claude plugin marketplace add|remove|update`, `claude config set`;
- запуск `claude` с `--bare`, `--safe-mode`, `--setting-sources`, `--plugin-dir`, `--settings` или с `CLAUDE_CONFIG_DIR=…` (вложенный запуск без модов).

Проходит обычная работа: ветка, коммит, `git push` своей ветки, `git fetch/pull/merge/rebase`, `gh pr create --base main`, `gh pr view/list/diff/checks/comment`, `gh api` на чтение, команды `multica`, чтение любых файлов (в том числе снимков с адресами и `~/.claude`), `claude plugin validate/test/list`, правка `infra/claude-mods/guard/` в своей рабочей копии репозитория.

### Если мод сам упал

Если хук мода упал или не успел за отведённое время, решение принимается только по тексту вызова: есть опасные слова (`push`, `merge`, `ssh`, `docker`, `dockhost`, `.claude`, `.multica`, `plugin` и др.) — запрет «мод guard не смог проверить вызов»; нет — вызов проходит. Так ошибка мода не останавливает всю работу, но и не открывает опасное. Такие запреты в журнал не пишутся (в этом месте мод не может обращаться к диску).

## Что видит агент

Текст запрета приходит модели как ошибка инструмента:

```
guard: действие запрещено — отправка в main или master.
Почему: в git push цель — ветка main.
Правило: CLAUDE.md, раздел «В конце работы»: отправка в main, слияние PR и force-push — только владелец; навык zone-guard.
Не обходи запрет другой формой команды. Если задачу без этого не выполнить — напиши комментарий в задаче, что нужно и зачем, и переведи её в blocked. Решает владелец.
```

Текста команды в нём нет (там могут быть токены).

## Журнал запретов

Файл `~/Library/Logs/claude-guard/<id сессии>.jsonl`, одна строка на запрет:

```json
{"time":"2026-10-16T12:00:00.000Z","session":"<id сессии>","cwd":"/Users/agent/multica_workspaces/...","tool":"Bash","rule":"push-main"}
```

Пишутся только `time`, `session`, `cwd`, `tool` и `rule`. Текст команды, пути и содержимое файлов не пишутся. Правила: `push-main`, `push-branch-unknown`, `push-force`, `pr-merge`, `gh-api-write`, `mcp-github-write`, `ssh`, `docker-remote`, `legacy-host`, `legacy-repo`, `legacy-script`, `protected-path`, `claude-plugin`, `claude-nested`.

Журнал `command-log` при запрете может записать строку с `result: "error"`, а может не записать — зависит от порядка двух модов; поэтому у guard свой журнал.

## Известные ложные срабатывания и пропуски

Запрещается, хотя опасности нет:
- адрес старой системы в аргументе программы не из списка «только читают» (например, `python3 tools/x.py --check x.dockhost.net`, `gh pr create --body "…dockhost.net…"` — тело PR лучше писать в файл и передавать `--body-file`);
- `gh api` с записью в `…/contents/…` даже своего репозитория (агентам хватает `git push`);
- `git push` из папки, которую мод не понял (`cd "$VAR"`), — запрет с подсказкой указать ветку;
- `cp`/`rm` с путём, где встречается `.claude/settings.json` (например, в тестовых данных).

Пропускается (правило остаётся на Ревьюере): команды из файла-скрипта, закодированные строки, сеть из программ на Python/Node без адреса в командной строке, git-алиасы (`git config alias.p push`).

## Файлы

- `.claude-plugin/plugin.json` — мод `guard` 0.1.0; `.claude-plugin/marketplace.json` — marketplace `dealer-platform-guard` (отдельный от журнала, чтобы выключать отдельно);
- `hooks/hooks.json`, `hooks/register.ts` — хук `tool.call` с `.catch`, журнал запретов;
- `hooks/rules.ts` — разбор команды и три правила;
- `tests/guard.test.ts` — тесты: «должно запрещаться», «должно проходить», текст запрета и журнал, «сторож падает».

## Проверка и тесты

Из корня репозитория:

```
claude plugin validate infra/claude-mods/guard
claude plugin test infra/claude-mods/guard
```

Тесты идут во встроенной среде Claude Code: настоящий Bash, git и диск не трогаются.

## Инструкция для владельца

Где выполнять:
- команды `git`, `claude plugin …`, `ls`, `cat` — по SSH под `agent` (`ssh agent@vitalys-macbook-pro`) или в Терминале на экране второго MacBook;
- команды с `claude -p` — **только в Терминале на экране второго MacBook**: по SSH вход в Claude Code не виден («Not logged in»), `/login` по SSH не делать (`docs/coordination/COORDINATOR.md`, раздел 7).

Команды — по одной. Под каждой — что ожидается. Если вывод другой — остановиться и написать в задачу.

### 1. Подготовка

1. Версия Claude Code:
   ```
   claude --version
   ```
   Ожидается: `2.1.299 (Claude Code)`. Если версия другая — остановиться и сообщить.

2. Обновить папку модов (она уже есть — из неё читается `command-log`; после слияния PR в main):
   ```
   git -C ~/claude-mods/dealer-platform pull --ff-only
   ```
   Ожидается: `Fast-forward` и список изменённых файлов, среди них `infra/claude-mods/guard/...`. Если папки нет — сначала шаг 2 из `infra/claude-mods/command-log/README.md` (`git clone`).

3. Проверка мода:
   ```
   claude plugin validate ~/claude-mods/dealer-platform/infra/claude-mods/guard
   ```
   Ожидается: строки `./register.ts hooks: tool.call` и `./register.ts gating hook with .catch: tool.call`, в конце `✔ Validation passed`.

4. Тесты мода:
   ```
   claude plugin test ~/claude-mods/dealer-platform/infra/claude-mods/guard
   ```
   Ожидается: `147 pass`, `0 fail`.

### 2. Установка

5. Добавить marketplace из папки мода:
   ```
   claude plugin marketplace add ~/claude-mods/dealer-platform/infra/claude-mods/guard
   ```
   Ожидается: сообщение, что marketplace `dealer-platform-guard` добавлен.

6. Установить мод:
   ```
   claude plugin install guard@dealer-platform-guard --scope user
   ```
   Ожидается: сообщение об установке `guard`.

7. Проверить список:
   ```
   claude plugin list
   ```
   Ожидается: `guard@dealer-platform-guard` включён, в строке `Read from:` — `~/claude-mods/dealer-platform/infra/claude-mods/guard`. `command-log@dealer-platform-mods` — по-прежнему в списке.

Перезапуск исполнителя Multica не нужен: Multica на каждую задачу запускает новый `claude -p`, а он загружает моды заново (так же было с `command-log`, `COORDINATOR.md`, раздел 7).

### 3. Короткая проверка (на экране второго MacBook)

8. Обычная команда проходит:
   ```
   cd ~ && claude -p "Выполни pwd и ответь одним словом: готово" --allowedTools "Bash(pwd)"
   ```
   Ожидается: «готово».

9. Запрет срабатывает (в папке `~` нет репозитория, так что даже без мода команда ничего бы не отправила):
   ```
   cd ~ && claude -p "Выполни ровно команду git push origin main и перескажи ответ одной строкой" --allowedTools "Bash(git push:*)"
   ```
   Ожидается: в ответе — «guard: действие запрещено — отправка в main или master».

10. Строка в журнале запретов:
    ```
    cat $(ls -t ~/Library/Logs/claude-guard/*.jsonl | head -1)
    ```
    Ожидается: строка с `"tool":"Bash","rule":"push-main"`, без текста команды.

Полная проверка на запусках Multica — отдельная задача «[Проверка]».

### 4. Обновление после изменений мода

После слияния PR с изменениями мода — одна команда:
```
git -C ~/claude-mods/dealer-platform pull --ff-only
```
Ожидается: `Fast-forward` (или `Already up to date.`). Мод читается прямо из этой папки, переустанавливать не нужно; новые запуски агентов берут новую версию. Эта же команда обновляет и `command-log`. Папку держать на ветке `main`.

### 5. Выключить и вернуть

Выключить одной командой (по SSH или на экране; агент сам этого сделать не может — мод запрещает):
```
claude plugin disable guard@dealer-platform-guard --scope user
```
Ожидается: сообщение, что `guard` выключен. Проверка: `claude plugin list` — `guard@dealer-platform-guard` выключен.

Вернуть:
```
claude plugin enable guard@dealer-platform-guard --scope user
```
Ожидается: сообщение, что `guard` включён; в `claude plugin list` — включён.

Действует со следующего запуска агента. Запуск, который уже идёт, доработает со старым состоянием (по справке — живой проверкой подтверждается в задаче «[Проверка]»). `command-log` это не затрагивает.

### 6. Удаление

11. Удалить мод:
    ```
    claude plugin uninstall guard@dealer-platform-guard --scope user
    ```
    Ожидается: сообщение, что мод удалён.

12. Удалить marketplace:
    ```
    claude plugin marketplace remove dealer-platform-guard
    ```
    Ожидается: сообщение, что marketplace удалён.

13. Проверить:
    ```
    claude plugin list
    ```
    Ожидается: `guard` в списке нет, `command-log` остался.

Папку `~/claude-mods/dealer-platform` не удалять — из неё читается `command-log`. Журнал запретов при желании: `rm -r ~/Library/Logs/claude-guard`.
