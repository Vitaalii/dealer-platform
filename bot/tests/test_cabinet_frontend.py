"""U8: фронт кабинета — source-grep-ассерты по app.js (R1, R3, R28, R37).

JS-тестовой инфраструктуры в репозитории нет (конвенция tests/test_payload.py:141-152):
проверяем присутствие ключевых конструкций в исходнике, поведение — ручным
чек-листом из плана.
"""
import pathlib

import cabinet_service

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_JS = ROOT / "widget-custom" / "cabinet-app" / "app.js"
MAIN_PY = ROOT / "main.py"


def _src() -> str:
    return APP_JS.read_text(encoding="utf-8")


class TestAppJsExists:
    def test_app_js_exists(self):
        assert APP_JS.is_file(), "widget-custom/cabinet-app/app.js отсутствует"

    def test_main_py_serves_widget_custom_static(self):
        """Раздача через существующий add_static — отдельный wiring не нужен."""
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "add_static('/widget-custom/'" in src, \
            "main.py не раздаёт /widget-custom/ статикой"


class TestTokenHandling:
    def test_token_copied_to_session_storage(self):
        assert "sessionStorage" in _src(), "токен обязан жить в sessionStorage"

    def test_fragment_cleared_via_replace_state(self):
        """R1: фрагмент #t=... затирается сразу после чтения."""
        assert "replaceState" in _src(), "нет history.replaceState — фрагмент с токеном останется в URL"


class TestMountAndConfig:
    def test_mounts_into_cabinet_root(self):
        assert "cabinet-root" in _src(), "нет монтирования в #cabinet-root"

    def test_config_via_unifiedbot_url(self):
        assert "UNIFIEDBOT_URL" in _src(), "база API обязана читаться из window.UNIFIEDBOT_URL"


class TestApiContract:
    def test_calls_auth_init(self):
        assert "/cabinet/api/auth/init" in _src()

    def test_polls_send_status(self):
        assert "send-status" in _src(), "нет опроса send-status"

    def test_send_status_token_in_header(self):
        """Токен send-status — в заголовке, не в query (не попадает в access-лог)."""
        assert "X-Cabinet-Token" in _src()

    def test_ping_interval_five_minutes(self):
        """R37: фоновый пинг /me раз в 5 минут (300000 мс)."""
        assert "300000" in _src(), "нет интервала пинга 5 мин (300000 мс)"


class TestChannelChoiceR28:
    def test_both_channels_present_as_equals(self):
        """R28: голос и SMS — равноправные карточки, обе присутствуют в разметке."""
        src = _src()
        assert "Звонок" in src, "нет карточки канала «Звонок»"
        assert "SMS" in src, "нет карточки канала «SMS»"

    def test_incoming_call_warning_present(self):
        """R28: предупреждение о входящем звонке обязательно и остаётся видимым."""
        assert "входящий звонок с незнакомого номера" in _src(), \
            "нет предупреждения о входящем звонке с незнакомого номера"


class TestRaceGuards:
    """Гонки двойного тапа и параллельных автосейвов (код-ревью P1)."""

    def test_request_code_locked_synchronously(self):
        """Двойной тап по кнопкам запроса кода не должен слать два OTP."""
        src = _src()
        assert "requestCodeInFlight" in src, "нет синхронного замка запроса кода"
        assert "if (requestCodeInFlight) return;" in src, \
            "замок запроса кода не проверяется на входе в requestCode"
        assert "setRequestCodeButtonsDisabled(true)" in src, \
            "кнопки запроса кода не блокируются синхронно на клике"
        assert "unlockRequestCode()" in src, \
            "замок запроса кода не снимается в хвосте .then/.catch"

    def test_request_code_response_freshness_guard(self):
        """Устаревший ответ request-code не должен перерисовывать экран."""
        src = _src()
        assert "requestCodeOutcomeStale" in src, "нет гарда актуальности ответа request-code"
        assert "screenSeq" in src, "нет метки актуальности экрана (screenSeq)"

    def test_autosave_serialized(self):
        """PUT /form не должны лететь параллельно и применяться не по порядку."""
        src = _src()
        assert "saveInFlight" in src, "нет замка на время летящего автосейва"
        assert "saveQueued" in src, "нет отложенной перезаписи автосейва (saveQueued)"

    def test_verify_locked_synchronously(self):
        """Код-ревью P3: поллинг send-status не должен ре-энейблить ячейки
        посреди верификации кода (verifyInFlight)."""
        src = _src()
        assert "verifyInFlight" in src, "нет замка на время POST /auth/verify"
        assert "if (verifyInFlight) return;" in src, \
            "замок verify не проверяется на входе в verifyCode/поллинг send-status"
        assert src.count("verifyInFlight = false;") >= 2, \
            "замок verify должен сниматься и в .then, и в .catch"

    def test_car_card_fetch_has_generation_guard(self):
        """Код-ревью P3: устаревший ответ /cabinet/api/car (после смены экрана
        или разлогина) не должен патчить чужую отрисовку #cab-car-head."""
        src = _src()
        start = src.index("function fetchCarCard()")
        end = src.index("\n  function ", start + 20)  # следующая функция того же (2-пробельного) уровня
        block = src[start:end]
        assert "screenSeq" in block, "нет метки поколения экрана в fetchCarCard"
        assert "state.bearer" in block, "нет проверки живого bearer в fetchCarCard"


class TestU6MergedAuthScreen:
    """U6: объединённый экран телефон + согласие + канал (макет m1/1a)."""

    def test_get_code_button_present(self):
        src = _src()
        assert 'id="cab-get-code"' in src, "нет единой кнопки «Получить код» на объединённом экране"
        assert "Получить код</button>" in src, "текст кнопки должен быть «Получить код» (макет)"

    def test_channel_tile_texts_verbatim(self):
        """Тексты плиток канала — дословно из макета (m1)."""
        src = _src()
        assert "Звонок с кодом" in src, "нет заголовка плитки «Звонок с кодом» (макет)"
        assert "Продиктует робот" in src, "нет подписи плитки «Продиктует робот» (макет)"
        assert "Придёт сообщением" in src, "нет подписи плитки SMS «Придёт сообщением» (макет)"

    def test_use_known_button_preserved(self):
        """R27: кнопка использования известного из чата номера сохраняется."""
        src = _src()
        assert 'id="cab-use-known"' in src, "кнопка use_known пропала при переверстке"
        assert "Использовать номер " in src


class TestU6CodeScreen:
    """U6: 4 ячейки кода (макет m2) вместо одного текстового поля."""

    def test_four_code_cells(self):
        src = _src()
        assert "code__cell" in src, "нет класса ячейки кода из макета"
        assert "cab-code-0" in src and "cab-code-3" in src, "должно быть ровно 4 ячейки (id 0..3)"

    def test_code_cells_paste_and_autosubmit(self):
        """Вставка кода из SMS в любую ячейку и автосабмит на 4-й цифре."""
        src = _src()
        assert "addEventListener('paste'" in src, "нет обработчика вставки кода из SMS"
        assert "автосабмит на 4-й цифре" in src, "нет автосабмита по заполнении всех 4 ячеек"

    def test_ios_autofill_uses_shared_distribute_helper(self):
        """Код-ревью P3: iOS one-time-code autofill может подставить весь код в
        одну ячейку (не посимвольно) — тот же хелпер, что и paste, распределяет
        цифры по ячейкам вместо обрезки slice(-1)."""
        src = _src()
        assert "function distributeCodeDigits(" in src, \
            "нет общего хелпера распределения цифр (paste + iOS autofill)"
        assert "digits.length > 1" in src, \
            "нет ветки многосимвольного автофилла в input-обработчике ячейки"
        assert src.count("distributeCodeDigits(cells, confirmBtn,") >= 2, \
            "хелпер распределения должен вызываться и из paste, и из input-автофилла"

    def test_resend_timer_separate_line(self):
        """Таймер «Отправить повторно через 0:NN» — отдельной строкой, не на кнопке."""
        src = _src()
        assert "Отправить повторно через " in src
        assert "fmtClock" in src, "таймер должен форматироваться как m:ss (fmtClock)"

    def test_change_phone_link_present(self):
        """«Изменить номер» — доступно до привязки номера."""
        src = _src()
        assert "Изменить номер" in src
        assert 'id="cab-change-phone"' in src

    def test_change_phone_link_survives_send_failure_rerender(self):
        """Ручной тест дев-стенда: retry_free/expired
        перезаписывают #cab-auth-body целиком (renderSendFailureArea) — без
        отдельного вызова ссылка «Изменить номер» терялась при первом же
        переходе в состояние ошибки отправки (частый путь на деве без
        настроенного провайдера SMS/звонков). Общий хелпер обязан вызываться
        из ОБОИХ рендеров под-состояний экрана кода."""
        src = _src()
        assert "function changePhoneLinkHtml()" in src
        assert "function wireChangePhoneLink()" in src
        start = src.index("function renderSendFailureArea(")
        end = src.index("\n  function ", start + 20)
        block = src[start:end]
        assert "changePhoneLinkHtml()" in block, \
            "renderSendFailureArea не рендерит «Изменить номер» — теряется при переходе в ошибку"
        assert "wireChangePhoneLink()" in block, \
            "renderSendFailureArea не навешивает обработчик «Изменить номер»"

    def test_full_number_for_fresh_vs_mask_for_repeat(self):
        """Решение ревизии: полный номер для свежевведённого, маска — для bound."""
        src = _src()
        assert "targetFull" in src, "нет отдельного поля полного номера свежевведённого клиента"


class TestU6StatusStates:
    """U6: состояния бэка вне макета + состояния макета — тексты дословно/по решению."""

    def test_link_expired_macro_text(self):
        src = _src()
        assert "Ссылка недействительна" in src
        assert "Срок действия ссылки истёк. Попросите ассистента прислать актуальную ссылку " \
            "на личный кабинет." in src

    def test_invalid_code_attempts_left(self):
        """2a-2: неверный код — обводка ошибки + «Осталось N попыток»."""
        src = _src()
        assert "code__cell--error" in src
        assert "Неверный код. Осталось" in src

    def test_locked_state_styling(self):
        """2a-3: блокировка — .code__cell--locked, живой отсчёт сохранён."""
        src = _src()
        assert "code__cell--locked" in src
        assert "btn--disabled" in src

    def test_lock_expiry_clears_disabled_button_class(self):
        """Код-ревью P3: после истечения блокировки клетки/кнопка снова активны —
        класс btn--disabled не должен переживать clearCodeCells/markCodeCellsError
        (disabled-атрибут остаётся управляться длиной кода, как раньше)."""
        src = _src()
        clear_start = src.index("function clearCodeCells()")
        clear_end = src.index("function markCodeCellsError()")
        error_end = src.index("function markCodeCellsLocked()")
        assert "classList.remove('btn--disabled')" in src[clear_start:clear_end], \
            "clearCodeCells не убирает класс btn--disabled с кнопки подтверждения"
        assert "classList.remove('btn--disabled')" in src[clear_end:error_end], \
            "markCodeCellsError не убирает класс btn--disabled с кнопки подтверждения"

    def test_rate_limit_is_separate_screen(self):
        """2a-4: лимит запросов — отдельный экран с точным retry_after, не инлайн."""
        src = _src()
        assert "showRateLimitScreen" in src, "нет отдельного экрана лимита запросов"
        assert "Слишком много запросов" in src
        assert "showRateLimitScreen(" in src and "retry_after" in src

    def test_retry_free_error_style_texts(self):
        """2a-5: retry_free — error-стиль с семантикой «попытка не засчитана»."""
        src = _src()
        assert "Попытка не засчитана" in src
        assert "Получить код в SMS" in src

    def test_expired_send_status_text(self):
        src = _src()
        assert "Код устарел. Запросите новый." in src

    def test_fallback_sms_removed_entirely(self):
        """U6/R11: автофолбэк голос→SMS снят (план 2026-08-27-001) — ни экрана,
        ни обработчика, ни состояний fallback_sms/delivery_failed в полинге."""
        src = _src()
        assert "Звонок не прошёл" not in src, "фолбэк-экран должен быть удалён"
        assert "handleFallbackSms" not in src, "обработчик фолбэка должен быть удалён"
        assert "fallback_sms" not in src, "состояние fallback_sms ушло из контракта send-status"
        assert "delivery_failed" not in src, "состояние delivery_failed ушло из контракта send-status"

    def test_voice_branch_has_no_repeat_call(self):
        """U6/R12+O5: в голосовой ветке повторного звонка не существует — после
        отсчёта и в ошибках отправки только переход на SMS."""
        src = _src()
        start = src.index("function renderResendReady(")
        block = src[start:src.index("\n  function ", start + 20)]
        assert "state.channel === 'voice'" in block, \
            "renderResendReady не различает ветки каналов"
        assert "Получить код ещё раз" not in src, \
            "универсальная ссылка повтора означает повторный звонок в голосовой ветке"
        fail_start = src.index("function renderSendFailureArea(")
        fail_block = src[fail_start:src.index("\n  function ", fail_start + 20)]
        assert "isVoice" in fail_block and "Получить код в SMS" in fail_block, \
            "renderSendFailureArea не разводит ветки: в голосовой не должно быть «Повторить»"

    def test_voice_switch_is_one_shot_via_voice_used(self):
        """U6/R13+O5: «Получить код звонком» одноразова; voiceUsed взводится по
        ФАКТУ запроса звонка (даже не принятого) — непринятый запрос тоже
        взводит 5-минутный триггер подмены у провайдера."""
        src = _src()
        assert "voiceUsed: false" in src, "нет флага voiceUsed в state"
        assert "if (channel === 'voice') state.voiceUsed = true;" in src, \
            "voiceUsed должен взводиться в requestCode по факту запроса, не по успеху"
        assert src.count("state.voiceUsed ? '' :") >= 2, \
            "гейт voiceUsed обязан стоять в обоих рендерах ссылки «Получить код звонком»"

    def test_rate_limited_inline_on_code_screen(self):
        """U6 + код-ревью: 429 с открытого экрана ввода кода — инлайн, не
        полноэкранная замена; канал восстанавливается (полинг живёт старой
        отправкой), отсчёт перезапускается (ссылки повтора не остаются
        кликабельными на весь лимит)."""
        src = _src()
        assert "renderRateLimitInline" in src
        assert "state.channel = prevChannel;" in src, \
            "отбитый лимитом запрос не должен оставлять state.channel нового канала"
        block = src[src.index("state.channel = prevChannel;"):]
        block = block[:block.index("} else {")]
        assert "renderRateLimitInline(err);" in block
        assert "startResendCooldown(" in block, \
            "инлайн-429 обязан перезапустить отсчёт повтора"

    def test_send_failure_area_clears_inline_banner(self):
        """Код-ревью: #cab-code-error — сосед #cab-auth-body; renderSendFailureArea
        обязан чистить его явно, иначе баннер лимита зависает над новым сообщением."""
        src = _src()
        start = src.index("function renderSendFailureArea(")
        block = src[start:src.index("\n  function ", start + 20)]
        assert "cab-code-error" in block and "innerHTML = ''" in block

    def test_rate_limit_human_wait_and_phone_source(self):
        """U6/U5: retry_after форматируется по-человечески («завтра»/часы/минуты),
        лимит по номеру объясняется отдельной формулировкой."""
        src = _src()
        assert "fmtWaitPhrase" in src
        assert "'завтра'" in src, "суточное ожидание должно читаться как «завтра»"
        assert "limit_source" in src, "фронт обязан читать признак источника лимита"
        assert "привязано к номеру" in src, \
            "нет отдельной формулировки для лимита по номеру телефона"

    def test_dead_code_hints_without_attempt_loss(self):
        """U6/U4: code_superseded/code_expired — подсказка без потери поля ввода
        и без упоминания счётчика попыток (бэкенд попытку не тратит)."""
        src = _src()
        assert "code_superseded" in src and "code_expired" in src
        assert "введите код из последнего сообщения" in src
        assert "Код устарел — запросите новый код." in src

    def test_voice_reminder_says_digits_only(self):
        """U6/R10: подсказка ожидания звонка соответствует роботу. Робот
        говорит «проверочный шифр», затем цифры — без «только» и «без слов»
        (живая проверка v3.4.2, решение В2 плана 2026-10-12-002)."""
        src = _src()
        assert "Ответьте на звонок — ' +\n          'робот продиктует цифры кода.'" in src
        assert "только цифры кода" not in src and "без слов" not in src

    def test_resend_cooldown_30s(self):
        """U6/R6: пауза до появления ссылок повтора — 30 секунд."""
        assert "var RESEND_COOLDOWN_S = 30;" in _src()

    def test_skeleton_instead_of_spinner(self):
        """2a-8: скелетон загрузки вместо спиннера."""
        src = _src()
        assert "cab-spinner" not in src, "спиннер должен быть заменён скелетоном"
        assert '"skeleton"' in src or "'skeleton'" in src or "class=\"skeleton\"" in src
        assert "sk--title" in src and "sk--btn" in src

    def test_conflict_popup_present(self):
        """409 conflict (гонка формы) — попап lk-source.html:1504–1514 с «Повторить»."""
        src = _src()
        assert "showConflictPopup" in src
        assert "popup--visible" in src
        assert "'conflict'" in src

    def test_conflict_retry_refetches_me_before_resubmitting(self):
        """Код-ревью P1: повтор после 409 не должен слать тот же устаревший
        state.me.calc — сначала GET /me (тем же путём, что boot/loadCabinet),
        и только потом повторный submitForm."""
        src = _src()
        assert "function retrySubmitAfterConflict()" in src, \
            "нет отдельной функции перечитывания /me перед повтором отправки"
        assert "showConflictPopup(retrySubmitAfterConflict)" in src, \
            "conflict-ветка submitForm всё ещё повторяет наивный submitForm без перечитывания /me"
        retry_start = src.index("function retrySubmitAfterConflict()")
        retry_end = src.index("function submitForm()")
        retry_block = src[retry_start:retry_end]
        assert "apiAuthed('GET', '/cabinet/api/me')" in retry_block, \
            "retry не перечитывает /me перед повтором"
        assert "applyMeResponse(me)" in retry_block, \
            "retry не применяет ответ /me тем же путём, что boot/loadCabinet"
        assert "submitForm()" in retry_block, "retry не повторяет отправку после перечитывания /me"
        assert "loadCabinet()" in retry_block, \
            "нет полного ре-старта кабинета, если перечитывание /me не удалось"


class TestU6RepeatLoginBound:
    """KTD: повторный вход с привязанным номером — только через выбор канала."""

    def test_no_this_is_not_my_number_link(self):
        """Ссылка «Это не мой номер» не реализуется (TOFU запрещает смену номера)."""
        assert "Это не мой номер" not in _src()

    def test_code_not_sent_automatically_on_bound_screen(self):
        """Плитки showChannelScreen шлют код только по клику (addEventListener), не при рендере."""
        src = _src()
        assert "addEventListener('click', function () { requestCode('voice', '#cab-channel-error'); })" in src
        assert "addEventListener('click', function () { requestCode('sms', '#cab-channel-error'); })" in src


# =======================================================================
# U8: сценарные анкеты, карточка авто, «Ваш автомобиль» (R5, R6, R9, R10).
# =======================================================================

class TestU8TileDictionariesUnity:
    """Страж единства (U1↔U8): каждая каноническая строка плитки из
    cabinet_service.FORM_TILE_FIELDS обязана присутствовать в app.js —
    сервер валидирует ТОЛЬКО эти строки (422 not_allowed на прочие)."""

    def test_all_canonical_tile_values_present_in_frontend(self):
        src = _src()
        missing = []
        for field, values in cabinet_service.FORM_TILE_FIELDS.items():
            for value in values:
                if value not in src:
                    missing.append((field, value))
        assert not missing, f"канонические значения плиток отсутствуют в app.js: {missing}"


class TestU8RadioTiles:
    """Плитки — скрытый нативный input[type=radio] + label (клавиатура/скринридер
    из коробки), не ARIA-роли на div."""

    def test_native_radio_markup_used(self):
        src = _src()
        assert 'type="radio"' in src, "плитки анкеты должны быть нативными input[type=radio]"
        assert "tile-radio" in src, "нет класса скрытого нативного радио-инпута плитки"

    def test_tile_group_keys_wired(self):
        """Каждое поле-словарь плиток заведено в TILE_GROUPS (объектный ключ —
        литерал в исходнике) и рендерится радио-группой (префикс id cab-group-)."""
        src = _src()
        assert "cab-group-" in src, "нет радио-группы плиток (id cab-group-<key>)"
        for field in cabinet_service.FORM_TILE_FIELDS:
            assert field + ":" in src, f"поле {field} не заведено как ключ TILE_GROUPS в app.js"


class TestU8OwnBlock:
    """Блок «Ваш автомобиль»: read-only .own из found_info, .own__empty — редкий
    край при отсутствии фото; обязательная справочная подпись фото модели."""

    def test_own_and_own_empty_classes_present(self):
        src = _src()
        assert 'class="own"' in src, "нет класса .own read-only блока «Ваш автомобиль»"
        assert "own__empty" in src, "нет заглушки .own__empty на случай отсутствия фото"

    def test_photo_reference_caption_verbatim(self):
        """Обязательная подпись — дословно (клиент не должен принять каталожное
        фото за снимок своей машины)."""
        assert "Фото модели. Изображение носит справочный характер" in _src()


class TestU8CarHead:
    """Шапка авто: скелетон на время запроса, вызов /cabinet/api/car."""

    def test_car_head_skeleton_reuses_u6_classes(self):
        src = _src()
        assert "cab-car-head" in src
        assert "sk--title" in src, "скелетон шапки авто должен переиспользовать .sk--* из U6"

    def test_car_endpoint_called_with_bearer(self):
        src = _src()
        assert "/cabinet/api/car" in src
        assert "fetchCarCard" in src


class TestU8SuccessTextsVerbatim:
    """Тексты успеха по сценарию — дословно (R9/R10)."""

    def test_credit_success_text(self):
        assert ("Специалисты дилерского центра вернутся к вам в ближайшее время "
                "с информацией по кредиту.") in _src()

    def test_tradein_success_text(self):
        assert ("Специалисты дилерского центра вернутся к вам в ближайшее время "
                "с предварительными условиями трейд-ина.") in _src()

    def test_combo_success_text(self):
        src = _src()
        assert "Специалисты дилерского центра вернутся к вам в ближайшее время с " in src
        assert "трейд-ина и информацией по кредиту." in src


class TestScenarioLeadTextsVerbatim:
    """v3 (план 2026-09-26-001): h2+lead заменены подзаголовком сценария в шапке
    и серыми плашками-фразами. Тексты — дословно из макета, сверка посимвольно
    (решение владельца п.1: в трейд-ин-фразе NBSP, «приема» без «ё»)."""

    def test_credit_phrase_text(self):
        assert ("Расскажите немного о себе, и мы подберем кредит "
                "на подходящих условиях.") in _src()

    def test_tradein_phrase_text_with_nbsp(self):
        #   — неразрывный пробел между «рассчитаем» и «условия» (&nbsp; макета)
        assert ("Расскажите о своём автомобиле, и мы рассчитаем условия "
                "его приема в зачёт.") in _src()

    def test_combo_phrase_text(self):
        assert ("Расскажите о своём автомобиле, и мы рассчитаем трейд-ин "
                "и кредит на новую машину.") in _src()

    def test_phrase_plate_class_present(self):
        assert 'class="cab-phrase"' in _src()

    def test_scenario_subtitles_verbatim(self):
        src = _src()
        assert "'Покупка в кредит'" in src
        assert "'Покупка в трейд-ин'" in src
        assert "'Покупка в кредит и трейд-ин'" in src

    def test_unknown_scenario_subtitle_fallback(self):
        # В2: запасной вариант — подзаголовок «Анкета» вместо прежнего h2
        assert "return 'Анкета'" in _src()

    def test_old_lead_texts_gone(self):
        """Замена, не дублирование: старых формулировок в исходнике нет."""
        src = _src()
        assert "предварительное решение по кредиту" not in src
        assert "Предварительная оценка, уточняется после осмотра" not in src
        # v3: удалённые h2 и lead-тексты трёх сценариев + «Уточните данные»
        assert "Подбор кредита" not in src
        assert "Оценка вашего автомобиля" not in src
        assert "Кредит с зачётом вашего автомобиля" not in src
        assert "Уточните данные" not in src
        assert "Заполните анкету — и получите" not in src
        assert "Уточните данные по своей машине" not in src
        assert "Вы сдаёте свою машину в счёт первоначального взноса" not in src


class TestOwnCardValuationV3:
    """Оценка «~ N ₽» внутри карточки «Ваш автомобиль» (v3 кадры 4b/6; R5/R8
    плана 2026-09-26-001). Источник — me.calc.ti_valuation, тот же, что у
    калькулятора; null (редкий сбойный случай) → строки нет."""

    def test_valuation_strip_markup_present(self):
        assert 'class="own__valuation"' in _src()

    def test_valuation_tilde_format(self):
        # «~ 890 000 ₽»: тильда + пробел + formatMoney (решение владельца п.2)
        assert "own__valuation-sum\">~ ' + esc(formatMoney(tiValuation))" in _src()

    def test_valuation_only_with_amount(self):
        # Строка рендерится только при значении; null → пустая строка
        assert "tiValuation != null" in _src()

    def test_ti_valuation_passed_into_own_block(self):
        src = _src()
        assert "renderOwnBlock(found, tiValuation)" in src
        assert "ownOrTradeinFieldsHtml(found, values, disabled, tiValuation)" in src


class TestCarCardV3:
    """Карточка-товар авто в шапке (v3 кадры 7a/3; R2/KTD3 плана
    2026-09-26-001): миниатюра, имя, опциональное «от <price_new> ₽»,
    aria-label вместо текстового хинта, шеврон."""

    def test_thumb_with_placeholder_fallback(self):
        src = _src()
        assert 'class="car-pick__thumb"' in src
        assert "car-pick__thumb car-pick__thumb--empty" in src

    def test_price_row_from_price_new_optional(self):
        """KTD3: строка цены только при числовом price_new > 0 из /car."""
        src = _src()
        assert "carState.price_new" in src
        assert "car-pick__price-from\">от<" in src
        assert "priceNew != null" in src

    def test_aria_label_replaces_hint(self):
        """P2 ревью: смысл хинта переносится в aria-label, хинт удалён."""
        src = _src()
        assert " — смотреть данные об автомобиле" in src
        assert "Выбранный автомобиль — смотреть данные</span>" not in src
        assert "car-pick__hint" not in src

    def test_car_icon_gone(self):
        src = _src()
        assert "carIconHtml" not in src
        assert "car-icon" not in src


class TestZh4RevisedPopupSlot:
    """Пересмотр Ж4 (решение владельца 2026-09-26, кадр 7 v3): карточка авто
    кликабельна и после отправки; попап живёт в слоте-сиблинге #cab-content
    (KTD2) — затемнение .readonly (opacity:.55) его не красит и не запирает
    в stacking context."""

    def test_interactive_no_longer_gated_by_submitted(self):
        src = _src()
        assert "hasContent && !state.submitted" not in src
        assert "var interactive = hasContent;" in src

    def test_popup_rendered_in_slot_outside_cab_content(self):
        src = _src()
        assert 'id="cab-car-popup-slot"' in src
        start = src.index("cab-screen container")
        tpl = src[start:src.index("setScreen(html)", start)]
        assert tpl.index("'</form>'") < tpl.index("cab-car-popup-slot"), \
            "слот попапа должен идти после закрытия #cab-content"

    def test_car_head_no_longer_carries_popup(self):
        """Кнопка и попап — раздельные узлы; patchCarHead патчит оба."""
        src = _src()
        seg = src[src.index("function carHeadHtml"):src.index("function carPopupSlotHtml")]
        assert "carPopupHtml" not in seg
        assert "carPopupSlotHtml(state.car, currentCarName())" in src

    def test_focus_return_unified(self):
        """В8: фокус возвращается на карточку в любом состоянии экрана —
        отдельной submitted-ветки больше нет."""
        src = _src()
        start = src.index("if (carPopupWasOpenBeforeReload) {")
        seg = src[start:src.index("\n    }", start)]  # тело блока до его закрытия
        assert "if (submitted)" not in seg
        assert "pickAfterReload.focus()" in seg


class TestAuthDividerMargin:
    """П.2 (В1, 2026-08-31): узкое правило разделителя — только поток
    авторизации (.card); анкета (.container) остаётся на базовых 22px."""

    def test_auth_divider_rule_present(self):
        assert "#cabinet-root .card .divider--md{margin:2px 0}" in _src(), \
            "нет узкого правила разделителя для экранов авторизации"

    def test_base_divider_rule_unchanged(self):
        assert "#cabinet-root .divider--md{margin:22px 0}" in _src(), \
            "базовое правило divider--md (анкета) должно остаться 22px"


class TestComboPersonalTilesHidden:
    """П.6 (2026-08-31): персональные плитки скрыты ТОЛЬКО в комбо; в кредите
    и неизвестном сценарии остаются; код блоков не удалён, откат — флагом."""

    def test_hide_flag_enabled(self):
        assert "var HIDE_PERSONAL_TILES_IN_COMBO = true;" in _src(), \
            "флаг скрытия персональных плиток в комбо должен быть включён"

    def test_show_personal_condition_scoped_to_combo(self):
        src = _src()
        assert ("var showPersonal = !isKnown || scenario === 'credit' ||\n"
                "      (scenario === 'combo' && !HIDE_PERSONAL_TILES_IN_COMBO);") in src, \
            "условие показа: кредит/unknown — как раньше, комбо — за флагом"

    def test_personal_tiles_code_not_removed(self):
        """Требование «не удалять»: список ключей и рендер-цикл на месте."""
        src = _src()
        assert "var TILE_PERSONAL_KEYS = ['marital_status', 'education', 'employment'];" in src
        assert "TILE_PERSONAL_KEYS[j]" in src, "рендер-цикл персональных плиток удалён"


class TestConsentLinksAndTextPopup:
    """П.3 (2026-08-31): кликабельные фразы согласия + текстовый поп-ап.
    В2 — вставка только текстом; В3 — ссылка лишь при непустом тексте дилера,
    фразы независимы; В4 — кликабельна вся фраза согласия целиком."""

    def _text_popup_block(self):
        src = _src()
        start = src.index("function closeTextPopup")
        return src[start:src.index("// ── ПОПАП: 409 conflict")]

    def test_consent_pieces_recompose_canonical_text(self):
        """Куски подписи — дословно; полный текст согласия собирается из них."""
        src = _src()
        assert "var CONSENT_PREFIX = 'Я ознакомлен(а) с ';" in src
        assert "var CONSENT_POLICY_PHRASE = 'Политикой конфиденциальности';" in src
        assert "var CONSENT_MIDDLE = ' и даю ';" in src
        assert ("var CONSENT_AGREEMENT_PHRASE = 'Согласие на предоставление "
                "и обработку персональных данных';") in src

    def test_link_only_when_dealer_text_present(self):
        src = _src()
        assert "if (!consentDocAvailable(docKey)) return esc(phrase);" in src, \
            "фраза без текста дилера обязана остаться обычным текстом (В3)"
        assert "return dealer[kind + '_available'] === true && !!dealer.code;" in src

    def test_legal_text_loaded_on_click_not_in_init(self):
        """Ускорение A6: текст — запросом по клику, с кэшем в памяти."""
        src = _src()
        assert "'/cabinet/api/dealer/' + encodeURIComponent(code) + '/legal/' + kind" in src
        start = src.index("function wireConsentLinks")
        block = src[start:src.index("// Экран m1/1a")]
        assert "loadConsentDoc(docKey).then(" in block and "showTextPopup(title, text, link)" in block

    def test_click_does_not_toggle_consent_checkbox(self):
        src = _src()
        start = src.index("function wireConsentLinks")
        block = src[start:src.index("// Экран m1/1a")]
        assert "e.preventDefault();" in block, "клик по фразе не должен вести по href/активировать label"
        assert "e.stopPropagation();" in block

    def test_popup_text_inserted_as_text_node_only(self):
        block = self._text_popup_block()
        assert "div.querySelector('#cab-text-popup-body').textContent = text;" in block, \
            "текст обязан вставляться textContent, не innerHTML (В2)"
        assert ".innerHTML = text" not in _src()

    def test_popup_mounted_in_body_with_id_scoped_styles(self):
        block = self._text_popup_block()
        assert "document.body.appendChild(div);" in block, \
            "поп-ап обязан монтироваться в document.body (переживает setScreen)"
        src = _src()
        assert "#cab-text-popup .popup__overlay" in src
        assert "#cab-text-popup .popup__dialog" in src

    def test_popup_preserves_newlines_and_scrolls(self):
        src = _src()
        assert "#cab-text-popup .popup__body{overflow-y:auto" in src, \
            "тело поп-апа обязано скроллиться (тексты до ~18 тыс. знаков)"
        assert "white-space:pre-wrap" in src, "переносы строк текста должны сохраняться"

    def test_popup_mobile_width_rule(self):
        assert "#cab-text-popup .popup__dialog{width:auto;margin:0 8px" in _src(), \
            "нет мобильного правила ширины диалога"

    def test_popup_closes_by_cross_overlay_and_esc(self):
        block = self._text_popup_block()
        assert ("div.querySelector('#cab-text-popup-close')"
                ".addEventListener('click', closeTextPopup);") in block
        assert ("div.querySelector('#cab-text-popup-overlay')"
                ".addEventListener('click', closeTextPopup);") in block
        assert "if (e.key !== 'Escape') return;" in block


class TestU8ReadOnlyAfterSubmit:
    """Read-only анкета после отправки, дата — из submitted_at (R10)."""

    def test_uses_submitted_at_field(self):
        assert "submitted_at" in _src()

    def test_readonly_class_used(self):
        assert 'class="readonly"' in _src() or "'readonly'" in _src()


class TestU8NoLegacyFields:
    """R1: выведенные из состава анкеты поля не должны рендериться (income и др.)."""

    def test_income_field_removed_from_render(self):
        assert "income" not in _src()

    def test_work_experience_and_family_composition_removed(self):
        src = _src()
        assert "work_experience" not in src
        assert "family_composition" not in src


# =======================================================================
# U9: блок «Условия кредита» на calc_model.js — ползунки, blocked-зоны,
# комбо (R11, R16-R19).
# =======================================================================

class TestU9CalcBlock:
    def test_money_input_present(self):
        """Платёж — редактируемый money__input (m4 макета), не статичный money__value."""
        assert "money__input" in _src()

    def test_native_range_sliders_present(self):
        """Срок и взнос — нативные input[type=range] с классом .slider--tap (44px, тач)."""
        src = _src()
        assert 'type="range"' in src, "срок/взнос должны быть нативными input[type=range]"
        assert "slider--tap" in src

    def test_blocked_zone_drawn_as_track_gradient(self):
        """Blocked-зона — фоновый градиент трека (--slider-bg custom prop), не div-оверлей."""
        src = _src()
        assert "linear-gradient" in src
        assert "--slider-bg" in src

    def test_impossible_state_warn_and_min_payment_note(self):
        """Фрейм 10: money--warn + «Минимально возможный платеж — N ₽/мес»."""
        src = _src()
        assert "money--warn" in src
        assert "Минимально возможный платеж" in src

    def test_calc_model_public_api_used(self):
        """app.js работает через публичный API calc_model.js (initState/touch),
        не дублирует его математику."""
        src = _src()
        assert "CalcModel.initState" in src
        assert "CalcModel.touch" in src

    def test_combo_tradein_readout_text_verbatim(self):
        assert "Зачёт вашего автомобиля" in _src()

    def test_combo_tradein_estimate_tilde_only_with_amount(self):
        """v3 п.2 (2026-09-26): «~ N ₽» — только при реальной сумме;
        при tiValuation == null рендерится «—» без тильды."""
        assert "tiValuation != null ? '~ ' + formatMoney(tiValuation) : '—'" in _src(), \
            "формат «~ N ₽» должен жить только в ветке с реальной суммой"

    def test_combo_tradein_note_text_verbatim(self):
        """Подпись оценки — дословно (с v3 живёт в карточке «Ваш автомобиль»)."""
        assert ("Расчёт по сделкам с похожими автомобилями. "
                "Финальная оценка — после осмотра.") in _src()

    def test_combo_extra_down_field_attrs(self):
        """P1 ревью плана 2026-09-26-001: без data-key автосейв доплаты умирает
        молча (делегированный листенер #cab-form читает data-key); money__input
        нужен для подсветки 422 (.money__input.input--error)."""
        src = _src()
        assert 'data-key="combo_extra_down"' in src
        idx = src.index('id="cab-f-combo_extra_down"')
        tag = src[src.rindex('<input', 0, idx):src.index('>', idx)]
        assert 'data-key="combo_extra_down"' in tag
        assert 'inputmode="numeric"' in tag
        assert 'money__input' in tag
        assert "Доплата к взносу" not in src  # В6: подпись FORM_LABELS удалена
        assert "+ доплата" in src
        assert ("Взнос складывается из оценки вашего автомобиля и доплаты — "
                "её можно не указывать.") in src

    def test_credit_wrap_single_header_outside_rebuilt_node(self):
        """KTD1: заголовок «Условия кредита» строится в одной точке
        (buildFormBlocksHtml, обёртка #cab-credit-wrap) и живёт вне
        пересобираемого #cab-credit-block; билдеры возвращают внутренний узел."""
        src = _src()
        assert src.count('group-title">Условия кредита') == 1
        assert 'id="cab-credit-wrap"' in src
        wrap_idx = src.index('id="cab-credit-wrap"')
        title_idx = src.index('group-title">Условия кредита')
        assert title_idx > wrap_idx, "заголовок должен жить внутри обёртки"
        assert '<div class="cab-group" id="cab-credit-block"' not in src, \
            "билдеры кредит-блока должны возвращать внутренний узел без cab-group"

    def test_combo_block_order_own_before_credit(self):
        """v3 4c: в комбо «Ваш автомобиль» и «Комментарий» идут раньше
        «Условий кредита»."""
        src = _src()
        combo_branch = src[src.index("if (scenario === 'combo')"):
                           src.index('if (showPersonal)')]
        own_idx = combo_branch.index('ownOrTradeinFieldsHtml(found, values, disabled, tiValuation)')
        credit_idx = combo_branch.index('creditWrapHtml')
        assert own_idx < credit_idx

    def test_combo_extra_down_writable_outside_combo_mode(self):
        """Код-ревью P2: при scenario=combo, но calcState.mode!=='combo' (ti_valuation
        null) combo_extra_down не мёртвое — делегированный листенер #cab-form
        пропускает его как calc-managed ТОЛЬКО когда mode==='combo'."""
        src = _src()
        assert "key !== 'combo_extra_down' || state.calcState.mode === 'combo'" in src, \
            "гард делегированного листенера не сужен по mode==='combo' для combo_extra_down"

    def test_calc_draft_restored_on_reload(self):
        """Код-ревью P2: черновик калькулятора (не submitted) должен
        восстанавливаться из formValues при reload/re-OTP тем же путём
        (restoreCalcStateFromForm), что и submitted-режим."""
        src = _src()
        assert "function hasCalcDraftValues()" in src, \
            "нет проверки наличия сохранённого черновика тройки калькулятора"
        update_start = src.index("function updateCalcState(me)")
        update_end = src.index("function patchCreditBlockDom(")
        update_block = src[update_start:update_end]
        assert "hasCalcDraftValues()" in update_block, \
            "updateCalcState не проверяет наличие черновика для не-submitted состояния"
        assert "restoreCalcStateFromForm(me.calc || null, state.formValues)" in update_block, \
            "updateCalcState не восстанавливает черновик через restoreCalcStateFromForm"
        assert "applyCalcStateToFormValues(state.calcState)" in update_block, \
            "нет тихого зеркалирования init-состояния в formValues (без markDirty) как фолбэка"

    def test_combo_no_credit_needed_text_verbatim(self):
        assert "Кредит не требуется" in _src()

    def test_touch_handlers_have_no_network_calls(self):
        """R11: расчёт при движении ползунка — синхронно, без сети. Маркеры
        очерчивают границы блока обработчиков touch — внутри не должно быть
        fetch/api/apiAuthed."""
        src = _src()
        start_marker = "U9: обработчики ползунков/полей условий кредита"
        end_marker = "КОНЕЦ БЛОКА БЕЗ СЕТИ (U9)"
        assert start_marker in src and end_marker in src, "нет маркеров границ touch-блока (U9)"
        block = src[src.index(start_marker):src.index(end_marker)]
        assert "fetch(" not in block, "обработчик прикосновения не должен звать fetch()"
        assert "apiAuthed(" not in block, "обработчик прикосновения не должен звать apiAuthed()"
        assert "api(" not in block, "обработчик прикосновения не должен звать api()"

    def test_degrades_without_calc_engine_or_model(self):
        """R18: отсутствие CalcModel/движка (скрипты не подключены тегами) —
        typeof-проверка перед использованием, без ReferenceError."""
        src = _src()
        assert "typeof CalcModel" in src

    def test_engine_mismatch_logs_console_error(self):
        """KTD «Защита от дрейфа»: несовпадение версии движка — деградация +
        сообщение в консоль (сломанная математика не должна тихо показываться)."""
        src = _src()
        assert "engine_mismatch" in src
        assert "console.error" in src


# =======================================================================
# Правки по ручному тесту дев-стенда (2026-08-10): адаптивная десктопная
# раскладка по макету (docs/design/lk-source.html), удаление лишних
# надписей (дилер на экране авторизации, ставка в блоке кредита).
# =======================================================================

class TestDesktopResponsiveLayout:
    """U1: десктопные классы макета (плитки в сетку, срок/взнос рядом,
    slider-head «заголовок слева — значение справа») — мобильные варианты
    (--tap/.cab-slider-label-mobile и т.д.) обязаны быть под media-запросом,
    а не применяться безусловно (иначе на десктопе рендерится мобильная
    раскладка — находка ручного теста)."""

    def test_desktop_tile_grid_classes_present(self):
        """#auth (tiles--2, канал получения кода) и #credit/#tradein (tiles--4,
        плитки анкеты) — макет lk-source.html:635,749."""
        src = _src()
        assert "tiles--2" in src, "нет десктопной сетки tiles--2 (канал/тех. состояние — макет)"
        assert "tiles--4" in src, "нет десктопной сетки tiles--4 (плитки анкеты — макет)"

    def test_desktop_duo_layout_present(self):
        """#credit: срок и взнос — рядом (.duo), не друг под другом (макет lk-source.html:725)."""
        assert 'class="duo"' in _src(), "нет .duo — срок/взнос должны идти рядом на десктопе"

    def test_slider_head_desktop_and_mobile_markers_present(self):
        """Один и тот же slider-head должен нести и десктопный (.bounds под
        ползунком, заголовок слева), и мобильный (bounds внутри slider-head,
        макет m4) маркер — переключение видимости через media, не сочинённые
        стили (см. .cab-slider-title/.cab-bound-inline/.cab-slider-label-mobile)."""
        src = _src()
        assert "slider-head" in src
        assert 'class="bounds"' in src, "нет отдельной строки .bounds под ползунком (десктоп, макет)"
        assert "cab-slider-title" in src and "cab-bound-inline" in src, \
            "нет переключателей заголовка/границ ползунка между десктопом и мобильным"

    def test_mobile_media_query_overrides_present(self):
        """Мобильные варианты (--tap/колонка) обязаны жить под media-запросом,
        а не применяться на десктопе безусловно (находка ручного теста дев-стенда)."""
        src = _src()
        assert "@media (max-width:600px)" in src, \
            "нет media-запроса мобильного брейкпоинта (600px — макет lk-source.html:312)"
        media_start = src.index("@media (max-width:600px)")
        media_block = src[media_start:media_start + 2500]
        assert "tiles--2" in media_block and "tiles--4" in media_block, \
            "media-блок не схлопывает tiles--2/tiles--4 в одну колонку на мобильном"
        assert "duo" in media_block, "media-блок не схлопывает .duo в одну колонку на мобильном"
        assert "slider-head--tap" in media_block, \
            "мобильные переопределения slider-head--tap должны быть внутри media, не безусловно"


class TestCabinetManualStandCleanup:
    """Ручной тест дев-стенда (2026-08-10): две лишние надписи, которых нет
    в макете — название дилера на экране авторизации и ставка в блоке
    условий кредита."""

    def test_dealer_name_note_removed_from_auth_screen(self):
        """Auth-экран (макет 1a/#auth) не показывает дилера/модель — строка
        «Автосалон — <модель>» пропадает при дефолтном имени дилера
        (cabinet_service.py/main.py: fallback dealer_name == 'Автосалон')."""
        src = _src()
        assert "dealer.brand_model" not in src, "auth-экран всё ещё читает dealer.brand_model"
        assert "Автосалон" not in src, "не должно быть литерала имени дилера в исходнике фронта"

    def test_credit_rate_not_rendered(self):
        """Ставки нет ни в одном фрейме анкеты/#slider-states макета — отображение
        убрано из creditBlockHtml/comboCreditBlockHtml и patch-функций; сама
        модель (CalcModel, cs.rateText) не тронута — это фронтовый рендер."""
        src = _src()
        assert "cab-credit-rate" not in src, "блок условий кредита всё ещё рендерит id ставки"
        assert "rateText" not in src, "фронт всё ещё читает cs.rateText для отображения"


class TestU10TildaThreeScripts:
    """U10 (R22): порядок подключения и версионирование трёх скриптов на Tilda.

    Документ docs/CABINET_TILDA_SETUP.md — единственная инструкция для страницы
    дилера; стражи пинят три тега со ?v= и их порядок, а версию движка сверяют
    с маркером в статике (KTD «Защита от дрейфа»).
    """

    DOC = ROOT / "docs" / "CABINET_TILDA_SETUP.md"
    ENGINE_JS = ROOT / "widget-custom" / "cabinet-app" / "calc_engine.browser.js"

    def _doc(self) -> str:
        return self.DOC.read_text(encoding="utf-8")

    def test_three_script_tags_with_version_query(self):
        doc = self._doc()
        for name in ("calc_engine.browser.js?v=", "calc_model.js?v=", "app.js?v="):
            assert name in doc, f"в CABINET_TILDA_SETUP.md нет тега {name}"

    def test_script_order_engine_model_app(self):
        doc = self._doc()
        i_engine = doc.index("calc_engine.browser.js?v=")
        i_model = doc.index("calc_model.js?v=")
        i_app = doc.index("cabinet-app/Production/app.js?v=")
        assert i_engine < i_model < i_app, \
            "порядок тегов обязан быть: движок -> модель -> app.js"

    def test_doc_points_to_production_build_with_build_versions(self):
        """Ускорение A2/A3 (2026-10-03): на страницах — только сборка Production,
        ?v= — версия из сборки (манифест), не ручной счётчик и не маркер движка
        (дрейф движка и модели ловит tests/test_js_suite.py)."""
        doc = self._doc()
        for name in ("calc_engine.browser.js", "calc_model.js", "app.js"):
            assert f"/widget-custom/cabinet-app/Production/{name}?v=<версия>" in doc
        assert "npm run build:cabinet" in doc and "manifest.json" in doc

    def test_doc_states_deploy_version_rule(self):
        doc = self._doc()
        assert "Порядок тегов обязателен" in doc
        assert "при каждом деплое" in doc.lower()


class TestSteeringCanonUnity:
    """Simplify-pass: карта «руль» (LEFT/RIGHT → Левый/Правый) существует и на
    сервере (STEERING_CANON, UF-поле), и на фронте (STEERING_LABELS, рендер
    блока «Ваш автомобиль») — страж по образцу словарей плиток, чтобы значения
    не разъехались."""

    def test_steering_values_present_in_both_sides(self):
        src_js = _src()
        src_py = (ROOT / "cabinet_service.py").read_text(encoding="utf-8")
        for canon in ("Левый", "Правый"):
            assert canon in src_js, f"app.js потерял каноническую строку руля «{canon}»"
            assert canon in src_py, f"cabinet_service.py потерял каноническую строку руля «{canon}»"
        for code in ("LEFT", "RIGHT"):
            assert code in src_js and code in src_py, \
                f"код руля {code} должен маппиться и на сервере, и на фронте"


class TestStaticCharset:
    """Отдача /widget-custom/ обязана нести charset=utf-8 — без него кириллица
    в JS при прямом открытии показывается кракозябрами."""

    def test_hook_wired_after_add_static(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "app.on_response_prepare.append(widget_static_utf8_charset)" in src

    @staticmethod
    def test_static_js_served_with_utf8_charset():
        import asyncio
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        import main as main_mod

        async def run():
            app = web.Application()
            app.router.add_static('/widget-custom/', path=str(ROOT / 'widget-custom'),
                                  name='widget_static')
            app.on_response_prepare.append(main_mod.widget_static_utf8_charset)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.get('/widget-custom/cabinet-app/app.js')
                assert resp.status == 200
                ct = resp.headers.get('Content-Type', '')
                assert 'charset=utf-8' in ct.lower(), f"Content-Type без charset: {ct}"
                body = await resp.text()
                assert 'Отправить' in body, "кириллица обязана читаться как utf-8"
            finally:
                await client.close()

        asyncio.run(run())


class TestNgrokDevHeader:
    """Дев-туннель ngrok перехватывает браузерные GET interstitial-страницей
    без CORS-заголовков (симптом: preflight OK, GET /me — ACAO missing).
    Обход: заголовок на всех fetch + его же имя в whitelist CORS бэка."""

    def test_app_js_sends_skip_header_on_all_fetches(self):
        src = _src()
        assert "'ngrok-skip-browser-warning': '1'" in src
        assert src.count("fetch(") == 1, \
            "все запросы обязаны идти через единый api() с дев-заголовком"

    def test_backend_whitelists_the_header(self):
        src_py = (ROOT / "cabinet_service.py").read_text(encoding="utf-8")
        assert "ngrok-skip-browser-warning" in src_py, \
            "заголовок фронта обязан быть в Allow-Headers (урок CORS)"


# =======================
# Веб-вход (план 2026-09-30-001, U8/U9): экраны входа, 8a–8h, опрос оценки
# =======================

def _function_body(name: str) -> str:
    """Тело JS-функции `function name(` до следующего объявления функции того
    же уровня (2 пробела) — грубо, но стабильно для source-grep."""
    src = _src()
    start = src.index(f"  function {name}(")
    nxt = src.find("\n  function ", start + 10)
    return src[start: nxt if nxt != -1 else len(src)]


class TestWebEntryAuthU8:
    def test_web_marker_read_from_url_before_fragment(self):
        assert "src=web" in _src() and "WEB_KEY" in _src()
        assert "sessionStorage.setItem(WEB_KEY, '1')" in _src()

    def test_web_auth_screen_has_no_sms_tiles(self):
        body = _function_body("showAuthScreen")
        assert "isWeb()" in body
        assert "Код подтверждения продиктует робот во входящем звонке." in body
        # плитки выбора канала рендерятся только в не-веб ветке тернарника
        web_branch, _, chat_branch = body.partition(": '<div class=\"group-title\">Способ получения кода</div>'")
        assert "channelTilesHtml" not in web_branch.split("(isWeb()")[-1]
        assert "channelTilesHtml(state.channel)" in chat_branch

    def test_web_send_failure_has_no_sms_switch(self):
        body = _function_body("renderWebSendFailureArea")
        assert "SMS" not in body and "cab-switch-channel" not in body
        assert "startResendCooldown" in body and "renderResendReady" in body

    def test_recall_texts_and_45s_window(self):
        src = _src()
        assert "WEB_RECALL_S = 45;" in src
        assert "WEB_RECALL_S = 315" not in src and "5:15" not in src
        assert "'Позвонить повторно через '" in src
        assert "Позвонить ещё раз" in src
        assert "Не дозвонились? Проверьте, что телефон рядом и не в режиме «Не беспокоить»." in src

    # План 2026-10-11-001: тексты после повтора (В1/В3) и время вместо ссылки (В4)

    def test_recall_flag_from_server_both_paths(self):
        assert "state.recall = !!data.recall;" in _function_body("requestCode")
        body = _function_body("restoreCallScreen")
        assert "state.recall = !!call.recall;" in body and "call.calls_left" in body

    def test_code_screen_texts_after_recall(self):
        body = _function_body("showCodeScreen")
        assert "var webRecall = isWeb() && state.recall;" in body
        recall_branch = body.split("var leadText = webRecall", 1)[1].split(": isVoice", 1)[0]
        assert "Отправляем новый код на номер" in recall_branch
        reminder = body.split("var reminderHtml = webRecall", 1)[1].split(": isVoice", 1)[0]
        assert "Новый код придёт звонком или в SMS. Если позвонят — " in reminder
        assert "робот продиктует цифры кода." in reminder
        # до повтора и в чате — прежние тексты
        assert "'Звоним на номер <span class=\"strong\">'" in body
        assert "Поступит входящий звонок с незнакомого номера. Ответьте на звонок — " in body

    def test_send_state_and_hint_after_recall(self):
        state_body = _function_body("renderSendState")
        assert "isWeb() && state.recall" in state_body
        assert "Код отправлен — ожидайте звонок или SMS." in state_body
        assert "Звонок выполняется — ожидайте входящий вызов." in state_body
        hint = _function_body("webNoAnswerText")
        assert "state.recall" in hint
        assert "Код не пришёл? Проверьте SMS и что телефон не в режиме «Не беспокоить»." in hint
        assert "webNoAnswerText()" in _function_body("renderResendReady")
        assert "webNoAnswerText()" in _function_body("showWebNoAnswer")

    def test_no_recall_link_when_no_calls_left(self):
        body = _function_body("startResendCooldown")
        assert "state.callsLeft === 0" in body and "renderNextCallTime(timerEl, left)" in body
        timer = _function_body("renderNextCallTime")
        assert "'Позвонить повторно можно в '" in timer and "pad2(at.getHours())" in timer
        assert "cab-resend" not in timer          # ссылки нет до наступления времени
        assert "renderResendReady(timerEl)" in timer
        assert "fmtClock" not in timer            # без посекундного отсчёта

    def test_chat_resend_ready_unchanged(self):
        body = _function_body("renderResendReady")
        assert "'<a class=\"link\" id=\"cab-switch-channel\" href=\"#\">Получить код в SMS</a>'" in body

    def test_f5_restore_uses_server_next_call_at(self):
        body = _function_body("restoreCallScreen")
        assert "call.next_call_at" in body and "Date.parse" in body
        assert "showCodeScreen" in body and "focusTitle()" in body
        init = _function_body("bootFromToken")
        assert "restoreCallScreen(data.call)" in init

    def test_session_ended_8f_for_web_states(self):
        src = _src()
        assert "Сессия завершена" in src and "Вернуться на сайт" in src
        assert "Если вы уже отправили заявку, она у нас — менеджер свяжется с вами." in src
        assert "showSessionEnded()" in _function_body("startReauth")
        assert "showSessionEnded()" in _function_body("showLinkExpired")
        boot = _function_body("bootFromToken")
        assert "!state.token && isWeb()" in boot and "data.state === 'bound'" in boot

    def test_return_url_validated_same_host(self):
        body = _function_body("safeReturnUrl")
        assert "window.location.origin + '/'" in body and "https:" in body

    def test_too_many_attempts_8g(self):
        body = _function_body("showTooManyAttempts")
        assert "Слишком много попыток" in body and "Повторить можно в" in body
        assert 'id="cab-limit-get-code" disabled' in body
        assert "setInterval(tick, 1000)" in body
        assert "showTooManyAttempts(" in _function_body("requestCode")

    def test_focus_and_live_regions(self):
        src = _src()
        assert 'id="cab-screen-title" tabindex="-1"' in src
        assert 'aria-live="polite"' in _function_body("showCodeScreen")
        # границы таймера объявляются, не каждый тик
        body = _function_body("startResendCooldown")
        assert body.count("announce(") == 1

    def test_chat_texts_hidden_for_web(self):
        body = _function_body("showFatal")
        assert "isWeb() ? null" in body


class TestWebEntryValuationU9:
    def test_polling_stops_on_terminal_cap_and_three_failures(self):
        src = _src()
        assert "VALUATION_POLL_CAP_MS = 120000" in src and "VALUATION_POLL_MAX_FAILS = 3" in src
        tick = _function_body("valuationPollTick")
        assert "VALUATION_POLL_CAP_MS" in tick and "valuationGiveUp()" in tick
        assert "VALUATION_POLL_MAX_FAILS" in tick
        assert "valClientFailed = true" in _function_body("valuationGiveUp")
        assert "visibilitychange" in src
        assert "stopValuationPolling()" in _function_body("applyValuationPoll")

    def test_poll_does_not_overwrite_client_car_fields(self):
        body = _function_body("applyValuationPoll")
        assert "formValues" not in body, "опрос не должен трогать локальные значения анкеты"

    def test_combo_one_down_with_status_row(self):
        src = _src()
        for text in ("оцениваем…", "после осмотра", "не получен"):
            assert f"'{text}'" in src
        assert "combo: profile.scenario_type === 'combo'" in _function_body("calcOpts")
        assert "CalcModel.initState(calc || null, calcOpts())" in src

    def test_frames_8a_to_8e_texts(self):
        src = _src()
        for text in ("Фото появится после оценки", "Оцениваем автомобиль",
                     "Не нашли автомобиль по госномеру", "Не удалось получить оценку по госномеру",
                     "Оценка — после осмотра", "Оценку проведёт менеджер",
                     "После отправки заявки менеджер свяжется с вами, уточнит детали "):
            assert text in src, text
        # подписи, которой нет в макете v4, во фронте нет
        assert "Повторно оценка по госномеру не запрашивается." not in src

    def test_8c_fields_match_mockup(self):
        body = _function_body("editableCarFieldsHtml")
        for text in ("'Например, NISSAN'", "'Например, ALMERA'", "'ГГГГ'", "'Год выпуска'",
                     "maxlength=\"4\"", "<span>км</span>", "formatThousands(mileage)",
                     "formatPlate(values.tradein_plate)"):
            assert text in body, text
        src = _src()
        assert "grid-template-columns:repeat(3,minmax(0,1fr))" in src      # десктоп — 3 колонки
        media = src[src.index("'@media (max-width:600px){'"):]
        assert "'#cabinet-root .cab-car-grid{grid-template-columns:1fr}'" in media  # 390 — одна

    def test_steering_defaults_to_left(self):
        body = _function_body("prefillWebCarFields")
        assert "values.tradein_steering = TILE_TRADEIN_STEERING[0]" in body

    def test_plate_format_space_before_region(self):
        body = _function_body("formatPlate")
        assert "m[1] + ' ' + m[2]" in body

    def test_mobile_note_texts_from_mockup(self):
        src = _src()
        for text in ("Сумма зачёта появится после оценки. Доплату можно указать уже сейчас.",
                     "Сумму зачёта назовёт менеджер после осмотра. Кредит рассчитаем на сумму доплаты.'",
                     "Сумма зачёта станет известна после осмотра.'",
                     "Можно отправить заявку без зачёта — менеджер уточнит сумму.'"):
            assert text in src, text

    def test_8a_status_clock_eta_progress(self):
        body = _function_body("webOwnBlockHtml")
        assert "cab-clock" in body and "около 1 мин" in body and "cab-progress" in body
        assert "skeletonHtml(90)" in body and "skeletonHtml(110)" in body
        assert "Отправить можно и сейчас — оценку добавим " in _function_body("savebarIdleText")

    def test_8g_return_is_link(self):
        body = _function_body("showTooManyAttempts")
        assert 'class="cab-link-muted" id="cab-return"' in body and "cab-limit-box" in body

    def test_steering_tiles_match_backend(self):
        import cabinet_service
        assert list(cabinet_service.TILE_TRADEIN_STEERING) == ["Левый", "Правый"]
        assert "var TILE_TRADEIN_STEERING = ['Левый', 'Правый'];" in _src()

    def test_landing_payment_clamped_in_restore(self):
        body = _function_body("restoreCalcStateFromForm")
        assert "CalcModel.minPayment" in body and "isWeb()" in body

    def test_rerender_keeps_focus_scroll_and_flushes_autosave(self):
        body = _function_body("rerenderCabinetKeepingPlace")
        assert "saveDraft()" in body and "setSelectionRange" in body and "scrollTo" in body


class TestTermStep12AndSliderBounds:
    def test_draft_normalized_written_back_and_saved(self):
        body = _function_body("updateCalcState")
        assert "applyCalcStateToFormValues(restored)" in body
        assert "state.formDirty[ck] = true" in body

    def test_mobile_hides_desktop_bounds_row(self):
        src = _src()
        media = src[src.index("'@media (max-width:600px){'"):]
        assert "'#cabinet-root .bounds{display:none}'" in media
        assert "'#cabinet-root .cab-bound-inline{display:inline}'" in media

    def test_term_step_is_12(self):
        calc_model = (APP_JS.parent / "calc_model.js").read_text(encoding="utf-8")
        assert "var CALC_MODEL_TERM_STEP = 12;" in calc_model
