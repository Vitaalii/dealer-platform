# command-log — мод-журнал для Claude Code

Мод только записывает, какие инструменты вызывал Claude Code. Ничего не запрещает и не меняет: каждый вызов уходит дальше без изменений (`next(e)`), а ошибка записи журнала просто пропускается. Нужен, чтобы проверить, загружаются ли моды в запусках через Multica (план — задача PDL-8, работа — PDL-9).

Сделан под Claude Code `2.1.299`. API модов в этой версии помечено как «early access» и может меняться между версиями.

## Что пишет

Файл `~/Library/Logs/claude-command-log/<id сессии>.jsonl`, одна строка на вызов инструмента:

```json
{"time":"2026-10-15T12:00:00.000Z","session":"<id сессии>","cwd":"/Users/agent/multica_workspaces/...","tool":"Bash","program":"git","result":"ok"}
```

- `time` — время вызова; `session` — id сессии; `cwd` — рабочая папка (у запусков через Multica в ней есть `multica_workspaces`); `tool` — имя инструмента; `result` — `ok` или `error` (ошибка инструмента или запрет другим хуком).
- `program` — только для Bash: первое слово команды без пути (`/usr/bin/git` → `git`). Присваивания перед командой (`TOKEN=… curl`) пропускаются. Если первое слово не похоже на имя программы (кавычки, `$(…)` и т. п.) — пишется `?`.
- Никогда не пишутся: аргументы и полный текст команды, переменные окружения, содержимое файлов, промпты и ответы, пути из Edit/Write и других инструментов.
- Один файл на сессию. Если файл дорастёт до 3 МиБ, следующие строки идут в `<id сессии>.2.jsonl`, `.3.jsonl` и т. д. (запись файла в API модов — целиком и не больше 4 МиБ).

## Файлы

- `.claude-plugin/plugin.json` — описание мода; `.claude-plugin/marketplace.json` — marketplace `dealer-platform-mods`, чтобы мод можно было установить из этой папки;
- `hooks/hooks.json`, `hooks/register.ts` — сам мод (хук `tool.call`);
- `tests/command-log.test.ts` — тесты.

## Проверка и тесты

Из корня репозитория:

```
claude plugin validate infra/claude-mods/command-log
claude plugin test infra/claude-mods/command-log
```

Тесты идут во встроенной среде Claude Code: настоящий диск и настоящий Bash не трогаются, файловая система, часы и окружение подменены в памяти.

## Инструкция для владельца

Все команды — по SSH под пользователем agent, по одной. Подключение с основного компьютера:

```
ssh agent@vitalys-macbook-pro
```

Под каждой командой — что ожидается. Если вывод другой — остановиться и написать в задачу.

### 1. Подготовка

1. Версия Claude Code:
   ```
   claude --version
   ```
   Ожидается: `2.1.299 (Claude Code)`. Если версия другая — остановиться и сообщить: API модов меняется от версии к версии.

2. Скачать репозиторий в постоянную папку (после слияния PR в main):
   ```
   git clone https://github.com/Vitaalii/dealer-platform ~/claude-mods/dealer-platform
   ```
   Ожидается: появилась папка `~/claude-mods/dealer-platform`. До слияния PR можно взять ветку задачи — добавить в конец команды `--branch agent/agent/f817551e7faf`.
   Эту папку не удалять и не переключать на другие ветки, пока мод установлен: установленный мод читается прямо из неё.

3. Проверка мода:
   ```
   claude plugin validate ~/claude-mods/dealer-platform/infra/claude-mods/command-log
   ```
   Ожидается: в конце `✔ Validation passed`, в списке есть строка `./register.ts hooks: tool.call`.

4. Тесты мода:
   ```
   claude plugin test ~/claude-mods/dealer-platform/infra/claude-mods/command-log
   ```
   Ожидается: `7 pass`, `0 fail`.

### 2. Проверка без установки

5. Пробный запуск с модом только на одну сессию:
   ```
   cd ~ && claude -p "Выполни pwd и ответь одним словом: готово" --plugin-dir ~/claude-mods/dealer-platform/infra/claude-mods/command-log --allowedTools "Bash(pwd)"
   ```
   Ожидается: ответ «готово».

6. Новый файл журнала:
   ```
   ls -t ~/Library/Logs/claude-command-log/ | head -3
   ```
   Ожидается: первым в списке — новый файл `.jsonl`.

7. Содержимое файла (подставить имя из шага 6):
   ```
   cat ~/Library/Logs/claude-command-log/<файл из шага 6>
   ```
   Ожидается: строка с `"tool":"Bash","program":"pwd","result":"ok"`. Других данных команды в ней нет.

### 3. Установка пользователю agent

8. Добавить marketplace из папки мода:
   ```
   claude plugin marketplace add ~/claude-mods/dealer-platform/infra/claude-mods/command-log
   ```
   Ожидается: сообщение, что marketplace `dealer-platform-mods` добавлен.

9. Установить мод:
   ```
   claude plugin install command-log@dealer-platform-mods --scope user
   ```
   Ожидается: сообщение об установке `command-log`.

10. Проверить список:
    ```
    claude plugin list
    ```
    Ожидается: `command-log@dealer-platform-mods` включён, в строке `Read from:` — папка мода из шага 2.

11. Повторить шаг 5 без `--plugin-dir`:
    ```
    cd ~ && claude -p "Выполни pwd и ответь одним словом: готово" --allowedTools "Bash(pwd)"
    ```
    Затем шаги 6–7. Ожидается: новый файл с такой же строкой. Значит, установленный мод грузится в обычном запуске.

### Нужен ли перезапуск исполнителя после установки

По источникам — не нужен:
- Multica на каждый запуск агента стартует новый процесс `claude -p …` (`server/pkg/agent/claude.go:1078-1135` в Multica v0.6.2, разобрано в ИТОГ PDL-8, раздел 2);
- новый процесс `claude -p` всегда загружает моды заново («A headless `claude -p` always loads fresh»), а мод, установленный со scope `user`, работает в каждой сессии, начатой после установки (справка Claude Code 2.1.299, навык `plugin-authoring`, `reference.md`, разделы «Developing one» и «Sharing a mod»);
- список установленных модов хранится в `~/.claude` пользователя agent (`~/.claude/settings.json` → `enabledPlugins`, `~/.claude/plugins/installed_plugins.json`; так их читает и сама Multica — `server/internal/daemon/claude_plugins.go:34-92`), а запуски Multica читают именно этот `~/.claude`: `HOME=/Users/agent`, `CLAUDE_CONFIG_DIR` не задан (ИТОГ PDL-8, раздел 2).

Живой проверкой это не подтверждено — это и проверяется в шагах 12–16. Если журнал не появился, перезапускать исполнителя наугад не нужно: перейти к разделу 5 плана PDL-8 (сначала вариант А — диагностика). Если перезапуск всё же понадобится — только так:
```
launchctl kickstart -k gui/$(id -u)/ai.multica.daemon
```
`multica daemon start` по SSH не использовать.

### 4. Проверка в запуске через Multica

12. Создать в Multica задачу `[Проверка] Пробный запуск для журнала мода`, исполнитель — Разведчик, статус Todo. Описание: «Выполни команды `pwd` и `ls`, ничего не меняй, напиши результат одним комментарием и переведи задачу в In Review».

13. Дождаться комментария агента.

14. Новый файл журнала:
    ```
    ls -t ~/Library/Logs/claude-command-log/ | head -3
    ```
    Ожидается: первым — новый файл со временем этого запуска.

15. Строка от запуска через Multica:
    ```
    grep -h '"program":"pwd"' ~/Library/Logs/claude-command-log/*.jsonl | tail -3
    ```
    Ожидается: есть строка, где `cwd` содержит `multica_workspaces`. **Это и есть ответ: мод в Multica загружается.**

16. Проверка, что секретов в журнале нет (подставить файл из шага 14):
    ```
    grep -c -i -E 'token|password|secret|bearer' ~/Library/Logs/claude-command-log/<файл из шага 14>
    ```
    Ожидается: `0`.

17. Выдержки из журнала (строки как есть, без правок) — комментарием в задачу «[Проверка]».

### 5. Удаление

18. Удалить мод:
    ```
    claude plugin uninstall command-log@dealer-platform-mods --scope user
    ```
    Ожидается: сообщение, что мод удалён.

19. Удалить marketplace:
    ```
    claude plugin marketplace remove dealer-platform-mods
    ```
    Ожидается: сообщение, что marketplace удалён.

20. Проверить:
    ```
    claude plugin list
    ```
    Ожидается: `command-log` в списке нет (если других модов нет — «No plugins installed»).

21. По желанию — удалить журнал. Сначала посмотреть, что там:
    ```
    ls ~/Library/Logs/claude-command-log/
    ```
    Затем:
    ```
    rm -r ~/Library/Logs/claude-command-log
    ```
    После удаления мода можно удалить и папку из шага 2: `rm -rf ~/claude-mods/dealer-platform`.
