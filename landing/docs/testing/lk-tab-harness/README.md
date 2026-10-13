# Харнесс вкладки «В личном кабинете» (U6, Ver_4_dev)

Проверяет сценарии U3, U5 и U6 из плана
`docs/plans/2026-09-30-001-feat-lk-tab-cabinet-web-entry-plan.md` на
**настоящем** коде: `build_harness.py` дословно извлекает `<style>`,
разметку calc-секции и `<script>` из
`docs/tilda-blocks/Ver_4_dev/u6-gallery.html` и обстраивает их заглушками
формы Tilda (поля живой формы page154296216), `window.ucQuiz` и
`window._ratesReady`. Включён `*{box-sizing:content-box}` — ловушка Tilda
из CLAUDE.md.

## Запуск

```bash
cd docs/testing/lk-tab-harness
python3 build_harness.py          # пересобрать после каждой правки u6
python3 -m http.server 8732       # file:// не годится
```

Открыть `http://localhost:8732/harness.html` и в консоли:

```js
await runAll()
```

Последняя строка — `итого: N ok, M fail`. Прогон на 1000 px и на 390 px
(узкое окно или режим устройства) — переполнение проверяется на текущей
ширине.

## Что заглушено

- Форма Tilda: `TILDA_MODE` = `none` (только запись в `window.SUBMITS`),
  `ok` (`t-btn_sending` → `aftersuccess` → окно успеха и очистка полей уже
  после события — как в tilda-forms-1.0), `error`, `hang`.
- «Открыть сессию»: `window.fetch` с `FETCH_MODE` = `ok`, `422`, `404`,
  `429`, `403`, `503`, `network`, `hang`, `badurl`, `http`; тела запросов —
  в `window.OPEN_CALLS`.
- Переход в кабинет — шов `window.__ucLkNavigate` (адреса в
  `window.NAVIGATED`): `location.assign` в браузере не подменяется.
- `window.UC_SITE` и `window.UNIFIEDBOT_URL` — заглушка блока «ТЕСТ / БОЙ».
- `window._ratesReady` выставляют сами сценарии.
- Swiper галереи — пустая заглушка.

Сценарии дедлайна ждут по 15 с — полный прогон около минуты. Удобнее
прогонять из Playwright (см. отчёты в истории коммитов): страница на
ширинах 1000, 768 и 390 px.
