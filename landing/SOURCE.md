# Источник снимка: landing/

| Поле | Значение |
| --- | --- |
| Исходный проект | Tilda Landing 2.0 (лендинг), только локальный git, на GitHub нет |
| Локальный путь | `DEV/Tilda/Landing_2.0/` |
| Ветка | `main` (единственная) |
| Коммит | `899a18870d39c04b95a7c9877eb4e8e37edc1c54` (2026-10-13 12:30 +0800) |
| Способ | `git archive 899a188 -- <пути ниже>` — только выбранные пути, без истории |
| Дата снимка | 2026-10-13 |

## Что перенесено

- `docs/tilda-blocks/Ver_4_dev/` целиком — исходники блоков U1–U4, U6, U7, сборки `Production/` (бой) и `Production-test/` (тест).
- Сборка: `package.json`, `package-lock.json`, `tools/build-production.mjs`, `.githooks/pre-commit`
  (`core.hooksPath` в новом репозитории не включён).
- Проверки: `tools/run-harnesses.py`, стенды `docs/testing/dealer-settings-harness/`, `lk-tab-harness/`,
  `phone-validation-harness/` (сгенерированных `harness_*.html` в коммите нет).
- Документация: `CLAUDE.md`, `CONCEPTS.md`, `docs/solutions/`, handoff `2026-10-06-release-lk-tab.md`,
  `2026-10-09-bot-brief-cache-headers.md`, `2026-10-12-measure-uc-ready.md`, планы `2026-09-14-001`,
  `2026-09-14-002`, `2026-09-15-001`, `2026-09-18-001`, `2026-09-30-001`, `2026-10-06-002`, `2026-10-08-001`…`005`,
  `2026-10-12-001`.

## Что не перенесено и почему

Выгрузки страниц Tilda (`sourse page/`, `new page/`, `inprogress page/` — собранные страницы, в `new page/`
был токен Airtable), `docs/designs/`, архивы и старые версии блоков, `docs/brainstorms/`, остальные планы и
handoff, `rollback-*`, проверки живых сайтов старой системы (`tools/check-live.py`, `visual-check.py`,
`hero-early-click-check.py`, `swiper-fallback-check.py`, `ready-waterfall.py`, `lk-states-shots.py`), разовые
файлы `docs/testing/build_u*.py`, `u3_*.html`, `u7_*.html`, `minus-features-*`, `dealer-*.js/html`.
Не отслеживались и не попали: `node_modules/`, `.claude/settings.local.json`.

## Что изменено в снимке

Только пометка «исторический снимок» в начале `CLAUDE.md`. Код не менялся.

## Задачи и оговорки на будущее

- **Адреса старого бота** (Dockhost `obut-5oaj-5i2u.gw-1a.dockhost.net`, ngrok) в сборщике, U2 и стендах не заменены:
  они связаны с самопроверкой сборщика. Заменить при переходе на новую инфраструктуру — согласованно во всех местах
  (сборщик, U2, стенды).
- `terser` и `clean-css` импортируются сборщиком, но в `package.json` явно не прописаны (приходят зависимостями
  `html-minifier-terser`); версия Node не закреплена.
- Стендам нужны python3, playwright, Chromium и сеть; `dealer-settings-harness` скачивает живую страницу haval-demo-new.
- Ключ API Яндекс.Карт в `u7-contacts.html` и сборках — браузерный, оставлен; в новой платформе станет настройкой сервера.
