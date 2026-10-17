# infra/

Инфраструктура новой платформы: Docker Compose, Caddy, PostgreSQL, NocoDB, n8n, заглушка бота.
Команды на сервере выполняет только владелец, по одной. Правила — корневой `CLAUDE.md`.

## Что здесь лежит

| Путь | Что это |
|---|---|
| `compose.yaml` | весь стек, боевые лимиты памяти (`docs/architecture/ARCHITECTURE.md`, 7.3) |
| `compose.test.yaml` | надстройка для теста (vm.mini, 6 ГБ): уменьшенные лимиты |
| `.env.example` | список переменных окружения без значений |
| `caddy/Caddyfile` | HTTPS и адреса: платформа, n8n, NocoDB |
| `postgres/Dockerfile` | образ PostgreSQL 17 с pgBackRest, rclone и скриптом дампа |
| `postgres/init/10-roles.sh` | первый запуск базы: роли и базы `platform`, `n8n`, `nocodb_meta` |
| `postgres/pgbackrest/pgbackrest.conf` | pgBackRest: архив журнала и копии в Yandex Object Storage, 30 дней, шифрование |
| `postgres/bin/dealers-dump.sh` | ежедневный зашифрованный дамп всех баз и ролей (внутри контейнера) |
| `backup/` | задания бэкапов на хосте (копии, дамп, проверка журнала и диска, пробное восстановление) и `crontab` |
| `db/migrate.py`, `db/migrations/` | миграции базы `platform`: схемы `catalog`, `content`, `platform_ro` и права |
| `platform/Dockerfile` | образ сервиса платформы: `core/` + `leads/` + `dealer-cabinet/` + `billing/` + `db/` |
| `bot-stub/` | заглушка бота до этапа 7, отвечает только на `/health` |
| `tests/` | проверки файлов этой папки без сервера |
| `claude-mods/` | моды Claude Code (command-log, guard), к серверу не относятся |

Сервисы и лимиты:

| Сервис | Бой | Тест | Наружу |
|---|---|---|---|
| `caddy` | 256 МиБ | 128 МиБ | порты 80 и 443 — **только он** |
| `postgres` | 2 ГиБ, `shared_buffers=512MB` | 768 МиБ, `shared_buffers=192MB` | нет |
| `migrate` | 256 МиБ, разовый запуск | 256 МиБ | нет |
| `platform` | 1,5 ГиБ | 512 МиБ | через Caddy |
| `nocodb` | 2 ГиБ, `--max-old-space-size=1536` | 1,5 ГиБ, `--max-old-space-size=1152` | через Caddy |
| `n8n` | 3 ГиБ, `--max-old-space-size=2304` | 1 ГиБ, `--max-old-space-size=768` | через Caddy |
| `bot` (заглушка) | 1 ГиБ | 512 МиБ | нет |

Роли базы (создаёт `postgres/init/10-roles.sh`, права раздаёт миграция `0001`):

| Роль | Что может |
|---|---|
| `platform_admin` | владелец базы `platform`; под ней идут только миграции |
| `platform_app` | код платформы: чтение и запись в `catalog`, `content`, `platform_ro` |
| `nocodb_src` | NocoDB: правка строк в `catalog` и `content`, в `platform_ro` — только чтение |
| `bot_app`, `n8n_reader` | чтение `catalog` и `content` |
| `n8n`, `nocodb_meta` | владельцы служебных баз `n8n` и `nocodb_meta` |

## Тесты (без сервера)

Из корня репозитория, нужен только Python 3.11+:

```
python3 -m unittest discover -s infra/tests -t infra
```

---

# Инструкция для владельца: тестовый сервер

Сервер: Hostkey vm.mini, Ubuntu 24.04, IP `185.149.145.139`. Домены: `dealers.autodc.org` (платформа),
`n8n.dealers.autodc.org`, `noco.dealers.autodc.org`.

Как читать: каждая команда — отдельный блок. Набрали одну, сравнили вывод с «Ожидается», только потом —
следующая. Не совпало — остановитесь и пришлите в задачу текст ошибки (**без паролей и ключей**).

Строки `$` и `#` в начале не набирать: это значок приглашения. `deploy@…$` — команда под пользователем
`deploy`, `root@…#` — под root.

## 1. DNS

Записи у регистратора `autodc.org` (A-запись `dealers.autodc.org` уже создана, `n8n.` и `noco.` — тоже,
по комментарию в PDL-27):

| Имя | Тип | Значение |
|---|---|---|
| `dealers.autodc.org` | A | `185.149.145.139` |
| `n8n.dealers.autodc.org` | A | `185.149.145.139` |
| `noco.dealers.autodc.org` | A | `185.149.145.139` |

AAAA-записи не создавать. Проверка на своём компьютере (по одной):

```
dig +short dealers.autodc.org
```
```
dig +short n8n.dealers.autodc.org
```
```
dig +short noco.dealers.autodc.org
```

Ожидается: у каждой одна строка `185.149.145.139`. Если `dig` нет — `nslookup dealers.autodc.org`, в ответе
`Address: 185.149.145.139`. **Пока все три не отвечают этим IP, шаг 10 (первый запуск) не делать:** Caddy не
выпустит сертификаты, а повторные неудачные попытки у Let's Encrypt ограничены по числу.

## 2. Вход под root по ключу

```
ssh root@185.149.145.139
```

Ожидается: вход без пароля (ключ добавлен при заказе), приглашение `root@…:~#`. Если спросили «Are you sure
you want to continue connecting» — ответить `yes`.

## 3. Обновление системы

```
apt update
```
Ожидается: в конце `Reading package lists... Done`, ошибок `E:` нет.

```
apt upgrade -y
```
Ожидается: установка обновлений без строк `E:`. Если появилось окно про перезапуск служб — Enter.
Если в конце написано `System restart required` — `reboot`, подождать минуту и снова шаг 2.

## 4. Пользователь `deploy` без root

```
adduser deploy
```
Ожидается: просит пароль дважды (придумайте и сохраните в менеджере паролей: он нужен для `sudo`),
остальные вопросы — Enter, в конце `Is the information correct? [Y/n]` — Enter.

```
usermod -aG sudo deploy
```
Ожидается: пустой вывод.

```
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
```
Ожидается: пустой вывод.

```
install -m 600 -o deploy -g deploy /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
```
Ожидается: пустой вывод.

**Не закрывая это окно**, откройте второе окно терминала на своём компьютере:

```
ssh deploy@185.149.145.139
```
Ожидается: вход без пароля, приглашение `deploy@…:~$`.

```
sudo whoami
```
Ожидается: спросит пароль `deploy` из шага 4, ответ `root`.

Дальше всё — во втором окне, под `deploy`. Первое окно (root) не закрывать до конца шага 5.

## 5. Запрет входа по паролю и под root

В Ubuntu настройки SSH читаются из `/etc/ssh/sshd_config.d/` по алфавиту, и действует первое найденное
значение. Поэтому файл называется `00-…` — чтобы он шёл раньше файлов, которые мог положить образ сервера.

```
sudo nano /etc/ssh/sshd_config.d/00-platform.conf
```
Вставить две строки, сохранить (`Ctrl+O`, Enter) и выйти (`Ctrl+X`):
```
PasswordAuthentication no
PermitRootLogin no
```

```
sudo sshd -t
```
Ожидается: пустой вывод (ошибок в настройках нет). Есть текст — не продолжать, прислать его.

```
sudo sshd -T | grep -Ei '^(passwordauthentication|permitrootlogin) '
```
Ожидается две строки: `permitrootlogin no` и `passwordauthentication no`.

```
sudo systemctl restart ssh
```
Ожидается: пустой вывод.

Проверка из **третьего** окна на своём компьютере:

```
ssh deploy@185.149.145.139
```
Ожидается: вход по ключу, как раньше. Если не пускает — в первом окне (root) удалить файл
`rm /etc/ssh/sshd_config.d/00-platform.conf`, затем `systemctl restart ssh` и прислать ошибку в задачу.

```
ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password deploy@185.149.145.139
```
Ожидается: `Permission denied (publickey).` — вход по паролю закрыт.

```
ssh root@185.149.145.139
```
Ожидается: `Permission denied (publickey).` — root не пускают. Теперь окно с root можно закрыть.

## 6. Файрвол: только 22, 80, 443

Под `deploy`, по одной:

```
sudo ufw allow OpenSSH
```
Ожидается: `Rules updated` и `Rules updated (v6)`.

```
sudo ufw allow 80/tcp
```
Ожидается: `Rules updated` и `Rules updated (v6)`.

```
sudo ufw allow 443/tcp
```
Ожидается: `Rules updated` и `Rules updated (v6)`.

```
sudo ufw enable
```
Ожидается: вопрос `Command may disrupt existing ssh connections. Proceed with operation (y|n)?` — `y`,
затем `Firewall is active and enabled on system startup`.

```
sudo ufw status
```
Ожидается: `Status: active` и шесть правил: `OpenSSH`, `80/tcp`, `443/tcp` и они же с `(v6)`, все `ALLOW`.

Важно: Docker открывает опубликованные порты мимо ufw. Поэтому в `compose.yaml` порты есть только у Caddy
(80 и 443); база, NocoDB и n8n снаружи недоступны напрямую.

## 7. Docker

Команды — из официальной инструкции docs.docker.com, «Install Docker Engine on Ubuntu», способ apt.

```
sudo apt install -y ca-certificates curl git
```
Ожидается: без строк `E:`.

```
sudo install -m 0755 -d /etc/apt/keyrings
```
Ожидается: пустой вывод.

```
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
```
Ожидается: пустой вывод.

```
sudo chmod a+r /etc/apt/keyrings/docker.asc
```
Ожидается: пустой вывод.

Следующая команда — одна, из нескольких строк; скопируйте её целиком, от `sudo` до `EOF`:
```
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
```
Ожидается: те же строки на экране, в строке `Suites:` — `noble`, в `Architectures:` — `amd64`.

```
sudo apt update
```
Ожидается: среди строк есть `download.docker.com`, ошибок `E:` нет.

```
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```
Ожидается: установка без строк `E:`.

```
sudo docker run --rm hello-world
```
Ожидается: строка `Hello from Docker!`.

```
sudo usermod -aG docker deploy
```
Ожидается: пустой вывод. Затем выйти (`exit`) и снова войти `ssh deploy@185.149.145.139` — чтобы права
группы `docker` заработали.

```
docker compose version
```
Ожидается: `Docker Compose version v2…` (без `sudo`).

**Копия образа диска.** Сейчас система настроена, а ключей платформы на ней ещё нет — удобная точка отката.
В панели Hostkey сделайте ручную копию образа диска (у тарифа их до двух). Если позже что-то пойдёт не так,
можно вернуться к этому состоянию, не повторяя шаги 3–7.

## 8. Ключ GitHub только для чтения и клонирование

Репозиторий приватный. Ключ создаётся на сервере и с него не уходит; в GitHub вы добавляете только открытую
часть, без права записи.

```
ssh-keygen -t ed25519 -C "dealers-test-deploy" -f ~/.ssh/github_deploy -N ""
```
Ожидается: `Your identification has been saved in /home/deploy/.ssh/github_deploy` и рисунок ключа.

```
cat ~/.ssh/github_deploy.pub
```
Ожидается: одна строка `ssh-ed25519 AAAA… dealers-test-deploy`. Это **открытая** часть, её можно копировать.

В GitHub: репозиторий `Vitaalii/dealer-platform` → Settings → Deploy keys → Add deploy key. Title —
`dealers-test`, Key — строка выше, галочку **Allow write access не ставить** → Add key.

```
nano ~/.ssh/config
```
Вставить, сохранить и выйти:
```
Host github.com
    IdentityFile ~/.ssh/github_deploy
    IdentitiesOnly yes
```

```
chmod 600 ~/.ssh/config
```
Ожидается: пустой вывод.

```
ssh -T git@github.com
```
Ожидается: на вопрос `Are you sure you want to continue connecting` — `yes`; затем строка вида
`Hi Vitaalii/dealer-platform! You've successfully authenticated, but GitHub does not provide shell access.`

```
git clone git@github.com:Vitaalii/dealer-platform.git ~/dealer-platform
```
Ожидается: `Cloning into '/home/deploy/dealer-platform'...` и в конце `done`.

## 9. Файл `.env` с ключами

Все остальные команды — из папки `infra`:

```
cd ~/dealer-platform/infra
```
Ожидается: пустой вывод.

```
cp .env.example .env
```
Ожидается: пустой вывод.

```
chmod 600 .env
```
Ожидается: пустой вывод.

Пароли и ключи заполняются одной командой: для каждого генерируется `openssl rand -hex 32`, значения на экран не
выводятся. Скопируйте целиком:
```
for k in POSTGRES_PASSWORD PLATFORM_ADMIN_PASSWORD PLATFORM_APP_PASSWORD NOCODB_SRC_PASSWORD BOT_APP_PASSWORD N8N_READER_PASSWORD N8N_DB_PASSWORD NOCODB_META_PASSWORD N8N_ENCRYPTION_KEY NC_AUTH_JWT_SECRET PGBACKREST_REPO1_CIPHER_PASS DUMP_CIPHER_PASS; do sed -i "s/^$k=\$/$k=$(openssl rand -hex 32)/" .env; done
```
Ожидается: пустой вывод.

```
nano .env
```
Заполнить оставшиеся строки (они пустые после `=`), сохранить и выйти:
```
COMPOSE_FILE=compose.yaml:compose.test.yaml
PLATFORM_ENV=test
PLATFORM_DOMAIN=dealers.autodc.org
N8N_DOMAIN=n8n.dealers.autodc.org
NOCODB_DOMAIN=noco.dealers.autodc.org
ACME_EMAIL=<ваша почта для писем Let's Encrypt>
PLATFORM_TIMEZONE=<часовой пояс, например Europe/Moscow>
PGBACKREST_REPO1_S3_BUCKET=<имя бакета теста, раздел 13.1>
PGBACKREST_REPO1_S3_KEY=<идентификатор статического ключа, раздел 13.1>
PGBACKREST_REPO1_S3_KEY_SECRET=<секретный ключ, раздел 13.1>
DUMP_RETENTION_DAYS=30
HEALTHCHECKS_PING_KEY=<ping key проекта Healthchecks, раздел 13.1>
```
Без бакета и ключа хранилища стек не запустится (`docker compose config` назовёт переменную): сначала
раздел 13.1.

```
grep -c '=$' .env
```
Ожидается: `0` — пустых значений не осталось.

Ключ шифрования n8n: если он потеряется, все ключи, сохранённые внутри n8n, станут нечитаемыми. Сохраните его
копию в менеджере паролей (команда показывает значение — не пересылайте его в чат):
```
grep '^N8N_ENCRYPTION_KEY=' .env
```

## 10. Первый запуск

Перед запуском — снова шаг 1: все три имени должны отвечать `185.149.145.139`.

```
docker compose config --quiet
```
Ожидается: пустой вывод — файлы корректны и все ключи на месте. Строка `required variable … is missing a
value` — в `.env` не заполнена названная переменная.

```
PLATFORM_VERSION=$(git rev-parse --short HEAD) docker compose build
```
Ожидается: несколько минут сборки, в конце строки `Built` (или `naming to …`) без `ERROR`.

```
docker compose up -d
```
Ожидается: скачивание образов, затем строки `Container dealers-… Started` / `Healthy`, без `Error`.

Подождать 2–3 минуты (NocoDB и n8n стартуют дольше остальных):

```
docker compose ps -a
```
Ожидается: у `migrate` — `Exited (0)`; у `caddy`, `postgres`, `platform`, `nocodb`, `n8n`, `bot` — `Up … (healthy)`.
`(health: starting)` — подождать ещё минуту и повторить.

## 11. Проверка

```
docker compose logs migrate
```
Ожидается: `migration applied: 0001_schemas_and_grants` и `migrations done: 1 applied`.

```
curl -fsS https://dealers.autodc.org/health
```
Ожидается: `{"status": "ok", "db": "ok", "env": "test", "version": "<коммит>"}`.

```
docker compose exec postgres psql -U postgres -d platform -c '\dn'
```
Ожидается: в списке схем `catalog`, `content`, `platform_meta`, `platform_ro`, `public`.

Запрет записи NocoDB в `platform_ro` держит база. Проверка под ролью NocoDB — чтение разрешено:
```
docker compose exec postgres psql -U nocodb_src -d platform -c 'SELECT count(*) FROM platform_ro.rights_check'
```
Ожидается: `count` = `1`.

Запись запрещена:
```
docker compose exec postgres psql -U nocodb_src -d platform -c "INSERT INTO platform_ro.rights_check (note) VALUES ('test')"
```
Ожидается: `ERROR:  permission denied for table rights_check`.

Панели — в браузере:

- `https://n8n.dealers.autodc.org` — открывается n8n с замком в адресной строке; создайте учётную запись
  владельца (n8n предлагает это при первом входе).
- `https://noco.dealers.autodc.org` — открывается NocoDB; первая зарегистрированная учётная запись становится
  администратором — зарегистрируйтесь сразу.

Пароли панелей — в менеджер паролей, не в чат и не в задачу.

## 12. Если что-то не так

| Что видно | Что сделать |
|---|---|
| сервис не `healthy` или перезапускается | `docker compose logs <сервис> --tail 50` и прислать вывод в задачу (просмотрите, что в нём нет паролей) |
| в логе `CONFIG ERROR: missing ИМЯ` | в `.env` не заполнена переменная `ИМЯ`; заполнить, затем `docker compose up -d` |
| `postgres` упал на первом запуске (ошибка в `10-roles.sh`) | скрипт первого запуска выполняется только на пустом томе. **Только на первом запуске, пока в базе нет данных:** `docker compose down`, затем `docker volume rm dealers_pg_data`, затем снова шаг 10 |
| `curl` пишет про сертификат, в логе `caddy` — `challenge failed` | проверить DNS (шаг 1) и `sudo ufw status` (шаг 6); не перезапускать Caddy много раз подряд — Let's Encrypt ограничивает число попыток |
| `/health` отвечает `503` | база недоступна для `platform`: `docker compose logs postgres --tail 50` |

Остановить стек (данные сохраняются): `docker compose down`. **Не добавлять `-v`**: этот флаг удаляет тома
с базой.

Выкатка и откат появятся здесь в задаче 5.2. Бэкапы и восстановление — раздел 13.

## 13. Бэкапы и мониторинг

Что работает (`docs/architecture/ARCHITECTURE.md`, 7.5):

| Слой | Как часто | Где | Сигнал Healthchecks |
|---|---|---|---|
| архив журнала pgBackRest | непрерывно, сегмент не реже раза в 60 с | Yandex Object Storage, `pgbackrest/` | `test-wal` (раз в час, `backup/wal-check.sh`) |
| полная копия pgBackRest | вс 03:00 | там же | `test-backup-full` |
| разностная копия | пн–сб 03:00 | там же | `test-backup-diff` |
| дамп всех баз и ролей | ежедневно 04:00 | тот же бакет, `dump/<дата>/` | `test-dump` |
| пробное восстановление | пн 05:00 | временный том на этом же сервере | `test-restore-check` |
| диск занят < 80 % | раз в час | — | `test-disk` |

Копии и журнал хранятся 30 дней, дампы — `DUMP_RETENTION_DAYS` дней (по умолчанию 30). Всё шифруется на
сервере до отправки. Время — по часам сервера. На бою вместо `test-` будет `prod-` (берётся из `PLATFORM_ENV`).

**Пароли шифрования (`PGBACKREST_REPO1_CIPHER_PASS`, `DUMP_CIPHER_PASS`) — единственный способ прочитать
копии. Потеряете их вместе с сервером — копии бесполезны. Сохраните их в менеджере паролей (шаг 13.2).**

### 13.1. Что зарегистрировать (один раз, в браузере)

1. **Yandex Cloud → Object Storage.** Создать бакет для теста: класс хранения «Стандартное» (не «Ледяное»:
   у него минимальный срок хранения 12 месяцев), публичный доступ — выключен. Имя бакета →
   `PGBACKREST_REPO1_S3_BUCKET`. Для боя потом — отдельный бакет.
2. **Сервисный аккаунт** с ролью `storage.editor` **только на этот бакет**, у него — статический ключ
   доступа. Идентификатор ключа → `PGBACKREST_REPO1_S3_KEY`, секретный ключ → `PGBACKREST_REPO1_S3_KEY_SECRET`.
   Секрет показывается один раз: сразу в `.env` на сервере, больше никуда.
3. **Healthchecks.io.** Аккаунт, проект `dealers`. В настройках проекта создать ping key →
   `HEALTHCHECKS_PING_KEY`. Создать 6 проверок; имя (slug) — ровно как в таблице, расписание — «Cron»,
   часовой пояс — как у сервера (шаг ниже, `timedatectl`):

   | Slug | Cron | Grace |
   |---|---|---|
   | `test-backup-full` | `0 3 * * 0` | 12 часов |
   | `test-backup-diff` | `0 3 * * 1-6` | 6 часов |
   | `test-dump` | `0 4 * * *` | 6 часов |
   | `test-restore-check` | `0 5 * * 1` | 12 часов |
   | `test-wal` | `45 * * * *` | 30 минут |
   | `test-disk` | `50 * * * *` | 30 минут |

   Оповещения: в Healthchecks подключить Telegram (их бот `@HealthchecksBot`) и почту. Токен вашего бота
   никуда не передаётся.
4. **UptimeRobot.** Три HTTP-монитора с интервалом 5 минут: `https://dealers.autodc.org/health`,
   `https://n8n.dealers.autodc.org/`, `https://noco.dealers.autodc.org/`; оповещения — в Telegram средствами
   UptimeRobot.

Часовой пояс сервера (для Healthchecks):
```
timedatectl | grep 'Time zone'
```
Ожидается: строка `Time zone: …` — этот пояс указать в проверках Healthchecks.

### 13.2. Ключи бэкапов в `.env`

Если сервер настраивался по шагу 9 уже с этими строками — переходите к 13.3. Иначе из `~/dealer-platform/infra`:

Добавить недостающие строки (пустыми):
```
for k in PGBACKREST_REPO1_S3_BUCKET PGBACKREST_REPO1_S3_KEY PGBACKREST_REPO1_S3_KEY_SECRET PGBACKREST_REPO1_CIPHER_PASS DUMP_CIPHER_PASS DUMP_RETENTION_DAYS HEALTHCHECKS_PING_KEY; do grep -q "^$k=" .env || echo "$k=" >> .env; done
```
Ожидается: пустой вывод.

Пароли шифрования (если ещё пустые):
```
for k in PGBACKREST_REPO1_CIPHER_PASS DUMP_CIPHER_PASS; do sed -i "s/^$k=\$/$k=$(openssl rand -hex 32)/" .env; done
```
Ожидается: пустой вывод.

```
nano .env
```
Заполнить `PGBACKREST_REPO1_S3_BUCKET`, `PGBACKREST_REPO1_S3_KEY`, `PGBACKREST_REPO1_S3_KEY_SECRET`,
`HEALTHCHECKS_PING_KEY` (13.1) и `DUMP_RETENTION_DAYS=30`.

```
grep -c '=$' .env
```
Ожидается: `0`.

Сохранить оба пароля шифрования в менеджере паролей (команда показывает значения — не пересылайте их в чат):
```
grep -E '^(PGBACKREST_REPO1_CIPHER_PASS|DUMP_CIPHER_PASS)=' .env
```

### 13.3. Первый запуск бэкапов

Пока не выполнен `stanza-create`, PostgreSQL пишет в лог ошибки `archive-push`, а журнал копится на диске —
это ожидаемо, шаги ниже идут подряд.

```
docker compose config --quiet
```
Ожидается: пустой вывод.

```
docker compose build postgres
```
Ожидается: сборка без `ERROR`, в выводе есть установка `pgbackrest` и `rclone`. Если `Unable to locate
package pgbackrest` — остановиться и прислать вывод в задачу.

Перезапуск базы (нужен: `archive_mode` меняется только перезапуском; NocoDB, n8n и платформа переподключатся сами):
```
docker compose up -d postgres
```
Ожидается: `Container dealers-postgres-1 Started` (или `Recreated`), без `Error`.

```
docker compose exec -T -u postgres postgres psql -At -c 'show archive_mode'
```
Ожидается: `on`.

```
docker compose exec -T -u postgres postgres pgbackrest --stanza=dealers stanza-create
```
Ожидается: в конце `stanza-create command end: completed successfully`.

```
docker compose exec -T -u postgres postgres pgbackrest --stanza=dealers check
```
Ожидается: `check command end: completed successfully`.

Первая полная копия (на пустой базе — около минуты):
```
./backup/backup.sh full
```
Ожидается: в выводе `backup command end: completed successfully`, затем `status: ok` и строка `full backup: …`;
последняя строка — `finished with exit code 0`. В Healthchecks у `test-backup-full` — зелёный статус.

Первый дамп:
```
./backup/dump.sh
```
Ожидается: строка `dump <дата> uploaded: globals platform n8n nocodb_meta; retention 30d` и
`finished with exit code 0`.

Проверка журнала и диска:
```
./backup/wal-check.sh
```
Ожидается: `finished with exit code 0`.

```
./backup/disk-check.sh
```
Ожидается: `disk /var/lib/docker: used NN%, limit 80%` и `finished with exit code 0`.

Расписание (заменяет crontab пользователя `deploy` целиком; сейчас других задач там нет):
```
crontab ~/dealer-platform/infra/backup/crontab
```
Ожидается: пустой вывод.

```
crontab -l | grep -c backup/
```
Ожидается: `6`.

Лог заданий — `~/dealers-backup.log`; последние строки: `tail -n 30 ~/dealers-backup.log`.

### 13.4. Ручное пробное восстановление

Восстановление идёт во **временный** том и **временный** контейнер; рабочая база и сервисы работают дальше.
Память временной базы — 512 МиБ. Все команды — из `~/dealer-platform/infra`.

Есть ли копия:
```
docker compose exec -T -u postgres postgres pgbackrest --stanza=dealers info
```
Ожидается: `status: ok` и хотя бы одна строка `full backup: …`.

Контрольное число в рабочей базе (запомнить):
```
docker compose exec -T -u postgres postgres psql -d platform -At -c 'select count(*) from platform_ro.rights_check'
```
Ожидается: число, на чистом тесте — `1`.

Точка восстановления (при повторе — новое имя, например `manual-check-2`, и дальше везде оно):
```
docker compose exec -T -u postgres postgres psql -At -c "select pg_create_restore_point('manual-check')"
```
Ожидается: строка вида `0/5000090`.

Отправить журнал с этой точкой в архив:
```
docker compose exec -T -u postgres postgres pgbackrest --stanza=dealers check
```
Ожидается: `check command end: completed successfully`.

```
docker volume create dealers_restore_manual
```
Ожидается: `dealers_restore_manual`.

Восстановление во временный том; `--archive-mode=off` — временная база не будет писать в общий архив:
```
docker run --rm -u postgres --env-file .env -v dealers_restore_manual:/restore dealers/postgres:17 pgbackrest --stanza=dealers --pg1-path=/restore --type=name --target=manual-check --target-action=promote --archive-mode=off restore
```
Ожидается: в конце `restore command end: completed successfully`.

Временный PostgreSQL (портов наружу нет, слушает только внутренний сокет):
```
docker run -d --name dealers-restore-manual -m 512m -u postgres --env-file .env -e PGDATA=/restore -v dealers_restore_manual:/restore dealers/postgres:17 postgres -D /restore -c archive_mode=off -c shared_buffers=128MB -c listen_addresses=
```
Ожидается: длинный идентификатор контейнера.

Подождать минуту:
```
docker logs dealers-restore-manual 2>&1 | grep 'recovery stopping'
```
Ожидается: `recovery stopping at restore point "manual-check", time …`. Пусто — подождать ещё минуту и
повторить; через 10 минут пусто — `docker logs dealers-restore-manual --tail 30` в задачу.

```
docker exec dealers-restore-manual psql -At -c 'select pg_is_in_recovery()'
```
Ожидается: `f`.

```
docker exec dealers-restore-manual psql -d platform -c '\dn'
```
Ожидается: схемы `catalog`, `content`, `platform_meta`, `platform_ro`, `public`.

```
docker exec dealers-restore-manual psql -d platform -At -c 'select count(*) from platform_ro.rights_check'
```
Ожидается: то же число, что в рабочей базе выше.

Убрать временные контейнер и том:
```
docker rm -f dealers-restore-manual
```
Ожидается: `dealers-restore-manual`.

```
docker volume rm dealers_restore_manual
```
Ожидается: `dealers_restore_manual`.

Автоматическая проверка (то же самое плюс сверка числа строк во всех таблицах, `pg_dump` всех баз и
`pgbackrest verify`) — её же запускает crontab по понедельникам:
```
./backup/restore-check.sh
```
Ожидается: строки `OK: restore point check-…`, `OK: pg_dump platform n8n nocodb_meta; pgbackrest verify`,
`cleanup: temporary container and volume dealers_restore_check removed` и `finished with exit code 0`.

**Результат — в задачу PDL-30 комментарием:** вывод `pgbackrest … info`, строки `restore command end`,
`recovery stopping`, оба контрольных числа и вывод `./backup/restore-check.sh` (проверьте, что в нём нет
ключей — их там быть не должно). Разработчик перенесёт итог в журнал ниже.

### 13.5. Журнал пробных восстановлений

| Дата | Сервер | Копия (`pgbackrest info`) | Как | Итог | Кто проверял |
|---|---|---|---|---|---|
| — | — | — | — | пока не проводилось | — |

### 13.6. Если что-то не так

| Что видно | Что сделать |
|---|---|
| в Healthchecks проверка красная | `tail -n 50 ~/dealers-backup.log`; последний вывод задания есть и в самой проверке Healthchecks (тело сигнала) |
| `wal-check` красный, в логе `archive-push` / `timeout` | хранилище недоступно или ключ неверный: `docker compose logs postgres --tail 50` в задачу. Очередь журнала ограничена 4 ГиБ, после этого журнал перестаёт копиться — архив неполный, нужна новая полная копия |
| `restore-check`: `FAIL: row counts differ` | во время проверки шла запись; запустить `./backup/restore-check.sh` ещё раз. Повторилось — вывод в задачу |
| `restore-check`: `temporary postgres stopped` | не хватило памяти или нет журнала до точки: вывод в задачу |
| `disk` красный | `df -h` и `docker system df` в задачу; ничего не удалять вручную |
| строки `healthchecks signal failed` | нет связи с hc-ping.com; само задание при этом выполнено — смотреть код выхода в строке `finished with exit code` |
| остался временный том после сбоя | `docker rm -f dealers-restore-check` и `docker volume rm dealers_restore_check` (следующий запуск убирает их и сам) |
