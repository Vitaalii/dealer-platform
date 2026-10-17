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
| `postgres/Dockerfile` | образ PostgreSQL 17 (в 5.3 сюда добавится pgBackRest) |
| `postgres/init/10-roles.sh` | первый запуск базы: роли и базы `platform`, `n8n`, `nocodb_meta` |
| `postgres/pgbackrest/` | место под бэкапы, настраивается в 5.3 |
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
for k in POSTGRES_PASSWORD PLATFORM_ADMIN_PASSWORD PLATFORM_APP_PASSWORD NOCODB_SRC_PASSWORD BOT_APP_PASSWORD N8N_READER_PASSWORD N8N_DB_PASSWORD NOCODB_META_PASSWORD N8N_ENCRYPTION_KEY NC_AUTH_JWT_SECRET; do sed -i "s/^$k=\$/$k=$(openssl rand -hex 32)/" .env; done
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
```

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

Выкатка и откат появятся здесь в задаче 5.2, бэкапы и восстановление — в 5.3.
