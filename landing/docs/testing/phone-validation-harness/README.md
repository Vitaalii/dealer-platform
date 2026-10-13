# Харнесс проверки маски и валидации телефона (U6)

Проверяет сценарии из плана
`docs/plans/2026-09-14-001-feat-phone-validation-submit-indicator-plan.md`
на **настоящем** коде: `build_harness.py` вытаскивает `<style>` и
`<script>` из `docs/tilda-blocks/Ver_3_ong/u6-gallery.html` дословно и
обстраивает их минимальным DOM плюс управляемой заглушкой формы Tilda.

## Запуск

```bash
cd docs/testing/phone-validation-harness
python3 build_harness.py          # соберёт harness.html рядом
python3 -m http.server 8731       # file:// не годится, нужен http
```

Открыть `http://localhost:8731/harness.html`, **кликнуть в поле
телефона** (без реального фокуса проверки каретки пропускаются) и в
консоли:

```js
await runAll()        // всё, кроме сторожа
await runAll(true)    // со сторожем, +21 с
```

Последняя строка — `итого: N ok, M fail, K skip`.

Каждая проверка попадает в блок `#log` на странице сразу, а не в конце
прогона, и её имя дублируется в `window.LAST`. Во вкладке без фокуса
возврат промиса может не дождаться конца — тогда результат читайте
прямо со страницы:

```js
[...document.querySelectorAll('#log .fail')].map(e => e.textContent)
```

Пустой массив — всё зелёное.

## Заглушка Tilda

`window.TILDA_MODE` переключает поведение:

| режим | что изображает |
|-------|----------------|
| `ok` | обычный цикл: `t-btn_sending` → `tildaform:aftersuccess` |
| `error` | сбой отправки: `t-btn_sending` → `tildaform:aftererror` **асинхронно** |
| `blocked` | валидация Tilda не пропустила: `t-btn_sending` ставится, `tildaform:aftererror` бросается **синхронно**, класс снимается |
| `hang` | запрос ушёл и не вернулся — проверка сторожа |

Кроме режима, заглушка смотрит на сам чекбокс согласия: снятый чекбокс
даёт отказ валидации независимо от `TILDA_MODE`. Так ведёт себя живая
форма — согласие в ней единственное поле с `js-tilda-rule`, и снятая
галочка роняет форму в ветку «форма пустая».

### Откуда взято поведение

Сверено с `tilda-forms-1.0.min.js` (`t_forms__initBtnClick`,
`window.tildaForm.validate`, `window.tildaForm.showErrors`,
`t_triggerEvent`) 2026-09-18. Ключевое: `showErrors` заканчивается
синхронным `dispatchEvent('tildaform:aftererror')`, и в этот момент
`t-btn_sending` ещё висит на кнопке.

**До 2026-09-18 режим `blocked` изображал «класса нет, события нет».**
Такой Tilda не бывает, и на этой выдумке мимо тестов прошёл дефект:
общее сообщение «Не удалось отправить заявку» вместо причины отказа
(см. `docs/plans/2026-09-18-001-fix-consent-gate-both-paths-plan.md`).
Меняя заглушку, сверяйтесь с исходником Tilda, а не с ожиданиями.

`window.TILDA_DELAY` — задержка ответа, мс.

## Чего харнесс НЕ проверяет

- **Узкий вьюпорт.** `resize_window` не меняет `clientWidth` в этом
  окружении, см. `docs/solutions/developer-experience/mobile-viewport-not-emulatable-in-browser-harness.md`.
  Вёрстку кнопки на 375-390px проверять на живом устройстве.
- **Реальный round-trip до `forms.tildaapi.com`** и доставку заявки
  в CRM.
- **Поведение на iOS Safari и Android Chrome** — только на устройствах.

## Особенность окружения

Во вкладке без фокуса Chrome душит таймеры, и прогон растягивается с
~2 с до полутора минут. Это не дефект кода: держите вкладку активной
либо просто дождитесь результата.
