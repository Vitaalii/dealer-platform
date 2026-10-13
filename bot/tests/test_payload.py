"""U4: payload v2 — слоты, флаги, история, эвристическая стадия (R5, R14, R15, R30)."""
import copy
import json
import pathlib
import time

import pytest

import payload_v2
from payload_v2 import (
    build_recent_history,
    build_session_context,
    compute_stage_heuristic,
    detect_flags,
    extend_payload,
    should_refresh_cache,
)

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"


class TestFlags:
    def test_phone_variants_detected(self):
        for text in ["+7 999 123-45-67", "89991234567", "мой номер 8 (912) 345 67 89", "7-912-345-67-89"]:
            assert detect_flags(text)["has_valid_phone_in_msg"], text

    def test_non_phone_not_detected(self):
        for text in ["", "приеду в 15:30", "цена 2500000", "взнос 300 тыс"]:
            assert not detect_flags(text)["has_valid_phone_in_msg"], text

    def test_plate_cyrillic_and_latin(self):
        assert detect_flags("госномер А123АА77")["has_valid_plate"]
        assert detect_flags("a123aa777")["has_valid_plate"]  # латиница, нижний регистр
        assert detect_flags("А 123 АА 77")["has_valid_plate"]  # с пробелами
        assert not detect_flags("Я123ЯЯ77")["has_valid_plate"]  # буквы вне алфавита номеров
        assert not detect_flags("просто текст 123")["has_valid_plate"]

    def test_stop_words(self):
        assert detect_flags("Не пишите мне больше!")["stop_word_detected"]
        assert detect_flags("ХВАТИТ ПИСАТЬ")["stop_word_detected"]
        # substring-матч сознательно грубый: флаг — только сигнал для конвейера
        assert detect_flags("не пишите цену в лоб, лучше позвоните")["stop_word_detected"]
        assert not detect_flags("хочу купить машину")["stop_word_detected"]


class TestRecentHistory:
    HISTORY = (
        [{"time": "10:00", "role": "user", "text": f"msg {i}"} for i in range(6)]
        + [{"time": "10:10", "role": "bot", "text": "ответ"}]
        + [{"time": "10:11", "role": "user", "text": "__SYSTEM_REMINDER__"}]
        + [{"time": "10:12", "role": "user", "text": ""}]
        + [{"time": "10:13", "role": "user", "text": "финал"}]
    )

    def test_limit_and_filtering(self):
        result = build_recent_history(self.HISTORY, limit=8)
        assert len(result) == 8
        texts = [m["text"] for m in result]
        assert "__SYSTEM_REMINDER__" not in texts
        assert "" not in texts
        assert texts[-1] == "финал"

    def test_role_mapping(self):
        result = build_recent_history(self.HISTORY)
        roles = {m["role"] for m in result}
        assert roles <= {"client", "bot"}
        assert result[-1]["role"] == "client"

    def test_empty_history(self):
        assert build_recent_history(None) == []
        assert build_recent_history([]) == []


class TestStageHeuristic:
    def test_greeting_on_first_message(self):
        assert compute_stage_heuristic("credit", {"phone_collected": False}, 0) == "greeting"

    def test_phone_collected_wins(self):
        assert compute_stage_heuristic("credit", {"phone_collected": True}, 0) == "closing"

    def test_tool_needed_when_slots_complete(self):
        assert compute_stage_heuristic("credit", {"payment": 300000}, 3) == "tool_needed"
        assert compute_stage_heuristic("tradein", {"plate": "А123АА77", "mileage": 85000}, 3) == "tool_needed"
        assert compute_stage_heuristic(
            "combo", {"payment": 40000, "plate": "А123АА77", "mileage": 85000}, 3) == "tool_needed"

    def test_data_collect_when_slots_missing(self):
        assert compute_stage_heuristic("credit", {"payment": 0}, 3) == "data_collect"
        assert compute_stage_heuristic("tradein", {"plate": "А123АА77"}, 3) == "data_collect"

    def test_consult_and_stock_never_tool_needed(self):
        assert compute_stage_heuristic("consult", {}, 5) == "data_collect"
        assert compute_stage_heuristic("stock", {"payment": 1}, 5) == "data_collect"


class TestSessionContext:
    CTX = {
        "scenario_type": "credit",
        "payment": 300000,
        "model": "Xray",
        "channel": "telegram",
        "phone_ask_count": 1,
        "chat_history": [
            {"role": "user", "text": "привет"},
            {"role": "bot", "text": "здравствуйте"},
            {"role": "user", "text": "взнос 300"},
        ],
    }

    def test_shape_and_values(self):
        sc = build_session_context(self.CTX, "мой телефон +79991234567", "base_v01.03")
        assert sc["request_type"] == "credit"
        assert sc["msg_count"] == 2
        assert sc["version_set"] == "base_v01.03"
        assert sc["channel"] == "telegram"
        assert sc["phone_ask_count"] == 1
        assert sc["flags"]["has_valid_phone_in_msg"]
        assert sc["current_stage"] == "tool_needed"  # payment собран, телефона в слотах нет
        assert sc["collected_data"]["payment"] == 300000
        assert sc["deal_finalized"] is False

    def test_json_serializable(self):
        sc = build_session_context(self.CTX, "текст", "base_v01.03")
        json.dumps(sc, ensure_ascii=False)


class TestBackwardCompatibility:
    def test_extend_payload_adds_only_new_top_level_keys(self):
        """R30: существующие ключи payload и qualification_context не меняются."""
        payload = {
            "action": "chat",
            "user_id": "u1",
            "message": "текст",
            "qualification_context": {"request_type": "credit", "system_prompt": "...", "cached_json": {"a": 1}},
            "data_routing": {"catalog_table_id": "t1"},
        }
        before = copy.deepcopy(payload)
        extend_payload(payload, {"chat_history": [], "scenario_type": "credit"}, "текст", "base_v01.03")
        for key, val in before.items():
            assert payload[key] == val, f"существующий ключ {key} изменился"
        assert set(payload) - set(before) == {"recent_history", "session_context"}

    def test_main_py_uses_shared_phone_extractor(self):
        """Рефлекс перехвата телефона в main.py обязан извлекать через
        payload_v2.extract_phone (план 2026-09-07-001) — без инлайн-копий."""
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "payload_v2.extract_phone(" in src, "main.py не использует единый извлекатель"
        assert src.count(r"\(?[489][0-9]{2}\)?") == 0, "в main.py осталась инлайн-копия регэкспа телефона"

    def test_db_config_carries_both_rates_keys(self):
        """Контракт с n8n: Get Rates читает исторический credit_table_id,
        новое имя rates_table_id тоже остаётся — db_config обязан слать оба."""
        src = MAIN_PY.read_text(encoding="utf-8")
        assert '"rates_table_id": rates_table' in src
        assert '"credit_table_id": rates_table' in src


class TestCabinetCalcWiring:
    """U5 (план 2026-08-06): проводка данных калькулятора — main.py остаётся
    wiring-only, связи пинятся source-grep'ом (образец выше)."""

    def test_setup_injects_nocodb_loader_and_base_url(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "nocodb_table_loader=get_cars_from_nocodb_dynamic" in src, \
            "setup() обязан инъецировать загрузчик NocoDB (TTL-кэш 600 с внутри)"
        assert "nocodb_base_url=NOCODB_API_URL" in src, \
            "setup() обязан передавать базовый URL NocoDB для абсолютных URL фото"

    def test_cache_save_point_refreshes_snapshot(self):
        """Вторая точка заморозки: сохранение json_cache перезамораживает
        снапшот тем же ходом, в try/except (сбой не ломает ответ клиенту)."""
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "cabinet_service.refresh_calc_snapshot(" in src

    def test_bg_refresh_task_holds_strong_reference(self):
        """P2 code-review #6: asyncio.create_task() без сохранённой ссылки
        может быть собран GC до завершения — задача обязана попадать в
        модульный _BG_TASKS и убирать себя оттуда по завершении."""
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "_BG_TASKS: set = set()" in src, \
            "модульный реестр сильных ссылок на фоновые задачи должен существовать"
        assert "_BG_TASKS.add(_task)" in src, \
            "задача _bg_refresh обязана сохраняться в _BG_TASKS"
        assert "_task.add_done_callback(_BG_TASKS.discard)" in src, \
            "задача обязана самоочищаться из _BG_TASKS по завершении"

    def test_attach_cabinet_context_passes_cache(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "n8n_cache=user_context.get('n8n_cache')" in src, \
            "_attach_cabinet_context обязан передавать кэш в prepare_cabinet_context"


class TestDealerFields:
    """Контракт v3.0: значения карточки дилера уезжают отдельными полями
    session_context.dealer_name / dealer_address / dealer_hours на КАЖДОМ ходе
    (v3.0 пересобирает промпт сам из блоков NocoDB с сырыми плейсхолдерами).
    Значения — как есть, без умолчаний: null = «данных в справочнике нет»,
    политика пустых значений — на стороне n8n."""

    CARD = {"dealer_name": "Тест Авто", "dealer_address": "г. Тестов, ул. Ленина, 1",
            "dealer_hours": "09:00 - 21:00"}

    def test_values_match_dealer_card(self):
        sc = build_session_context({"scenario_type": "credit", "chat_history": []},
                                   "текст", "base_v01.03", dealer_info=self.CARD)
        assert sc["dealer_name"] == "Тест Авто"
        assert sc["dealer_address"] == "г. Тестов, ул. Ленина, 1"
        assert sc["dealer_hours"] == "09:00 - 21:00"

    def test_null_values_pass_through_without_error(self):
        """NocoDB отдаёт пустые поля явным null (не отсутствующим ключом):
        None проходит как есть — сборка не падает, ключи присутствуют."""
        card = {"dealer_name": None, "dealer_address": None, "dealer_hours": None}
        payload = {"qualification_context": {}}
        extend_payload(payload, {"chat_history": [], "scenario_type": "credit"},
                       "текст", "base_v01.03", dealer_info=card)
        sc = payload["session_context"]
        assert sc["dealer_name"] is None
        assert sc["dealer_address"] is None
        assert sc["dealer_hours"] is None
        json.dumps(sc, ensure_ascii=False)

    def test_keys_present_even_without_dealer_info(self):
        """Отсутствие карточки (неизвестный дилер) — поля присутствуют с null:
        приёмная сторона всегда видит ключи по фиксированному пути."""
        sc = build_session_context({"scenario_type": "credit", "chat_history": []},
                                   "текст", "base_v01.03")
        assert sc["dealer_name"] is None
        assert sc["dealer_address"] is None
        assert sc["dealer_hours"] is None

    def test_wiring_both_payload_paths_pass_dealer_info(self):
        """Проводка: оба payload-пути (chat + handoff) передают карточку —
        поля шлются на каждом ходе, не только в первом хендофе."""
        src = MAIN_PY.read_text(encoding="utf-8")
        assert src.count("prompt_key, dealer_info)") == 2, \
            "extend_payload обязан получать dealer_info в chat И handoff путях"


class _CaptureSession:
    """Фейк aiohttp.ClientSession: перехват тела POST, ответ 200 + JSON."""

    captured = None  # класс-атрибут: последний payload любого экземпляра

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        _CaptureSession.captured = json
        _CaptureSession.headers = headers

        class _Resp:
            status = 200

            async def json(self):
                return {"text": "ок"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        return _Resp()


class TestDealerFieldsInRequestBody:
    """Интеграционно: поля дилера в ТЕЛЕ запроса к n8n на обычном ходе и на
    INIT-хендофе; путь v2.0 (подстановка в system_prompt) не изменился."""

    CARD = {"dealer_name": "Тест Авто", "dealer_address": "г. Тестов, ул. Ленина, 1",
            "dealer_hours": "09:00 - 21:00", "db_config": {"catalog_table_id": "t1"}}
    PROMPT = "Салон [dealer_name], адрес: [dealer_address], график: [dealer_hours]."
    PROMPT_READY = ("Салон Тест Авто, адрес: г. Тестов, ул. Ленина, 1, "
                    "график: 09:00 - 21:00.")

    def _patch(self, monkeypatch):
        import main as main_mod
        monkeypatch.setenv("N8N_WEBHOOK_URL", "http://n8n.test/webhook")
        monkeypatch.setattr(main_mod, "CACHED_DEALERS", {"01": dict(self.CARD)})
        monkeypatch.setattr(main_mod, "PROMPTS_CACHE",
                            {"base_v01.00": {"credit": self.PROMPT}})
        monkeypatch.setattr(main_mod.aiohttp, "ClientSession", _CaptureSession)

        # Кабинетный блок ходит в БД (в бою — PostgreSQL): в тесте немедленный
        # отказ вместо зависания TCP-коннекта — обе точки обёрнуты try/except,
        # payload собирается дальше без cabinet-ключей.
        def _no_db(*args, **kwargs):
            raise RuntimeError("БД недоступна в тесте payload-контракта")
        monkeypatch.setattr(main_mod, "AsyncSessionLocal", _no_db)

        async def _no_cabinet(*args, **kwargs):
            return None
        monkeypatch.setattr(main_mod, "_attach_cabinet_context", _no_cabinet)
        _CaptureSession.captured = None
        return main_mod

    @pytest.mark.asyncio
    async def test_regular_chat_turn_carries_dealer_fields(self, monkeypatch):
        main_mod = self._patch(monkeypatch)
        ctx = {"start_code": "010101", "scenario_type": "credit",
               "chat_history": [{"role": "user", "text": "привет"}]}
        await main_mod.send_chat_message_to_n8n("u-dlr-1", "вопрос", "Имя", ctx)
        body = _CaptureSession.captured
        assert body is not None, "запрос к n8n не ушёл"
        sc = body["session_context"]
        assert sc["dealer_name"] == "Тест Авто"
        assert sc["dealer_address"] == "г. Тестов, ул. Ленина, 1"
        assert sc["dealer_hours"] == "09:00 - 21:00"
        # Путь v2.0 не тронут: готовый промпт с подставленными значениями
        assert body["qualification_context"]["system_prompt"] == self.PROMPT_READY

    @pytest.mark.asyncio
    async def test_init_handoff_carries_dealer_fields(self, monkeypatch):
        import database as db_mod
        main_mod = self._patch(monkeypatch)
        user = db_mod.User(platform_user_id="u-dlr-2", channel="max",
                           full_name="Тест", username=None, phone=None,
                           active_deal_id=None)
        ctx = {"start_code": "010101", "scenario_type": "credit",
               "chat_history": []}
        await main_mod.send_handoff_webhook("u-dlr-2", ctx, user)
        body = _CaptureSession.captured
        assert body is not None, "запрос к n8n не ушёл"
        sc = body["session_context"]
        assert sc["dealer_name"] == "Тест Авто"
        assert sc["dealer_address"] == "г. Тестов, ул. Ленина, 1"
        assert sc["dealer_hours"] == "09:00 - 21:00"
        assert body["qualification_context"]["system_prompt"] == self.PROMPT_READY


class TestNaiveDatetimeGuard:
    """Страж naive-времени (урок docs/solutions/database-issues/
    naive-datetime-written-as-local-timezone.md): naive datetime, записанный в
    timestamptz через SQLAlchemy/asyncpg, интерпретируется по таймзоне МАШИНЫ
    (не UTC — проба дала сдвиг −8 ч). Новый код обязан использовать aware-время:
    _utcnow()/_aware() в кабинете, datetime.now(timezone.utc) в тестах."""

    ROOT = pathlib.Path(__file__).resolve().parent.parent
    GUARDED = (
        "cabinet_service.py", "cabinet_api.py", "database.py",
        "sms_gateway.py", "log_masking.py", "payload_v2.py", "crm_mapping.py",
    )
    # Сборка из частей — чтобы страж не ловил собственный исходник
    NAIVE_UTCNOW = "datetime." + "utcnow"
    NAIVE_NOW = "datetime." + "now()"

    def test_no_naive_datetime_in_guarded_modules(self):
        for rel in self.GUARDED:
            src = (self.ROOT / rel).read_text(encoding="utf-8")
            for token in (self.NAIVE_UTCNOW, self.NAIVE_NOW):
                assert token not in src, (
                    f"{rel}: naive-время ({token}) запрещено — "
                    "используйте _utcnow()/_aware() или datetime.now(timezone.utc)")

    def test_no_naive_datetime_in_tests(self):
        for path in sorted((self.ROOT / "tests").glob("*.py")):
            src = path.read_text(encoding="utf-8")
            for token in (self.NAIVE_UTCNOW, self.NAIVE_NOW):
                assert token not in src, (
                    f"tests/{path.name}: naive-время ({token}) запрещено — "
                    "на PostgreSQL сеяние уезжает на смещение локальной таймзоны")

    def test_main_py_naive_usage_pinned_to_known_legacy(self):
        """main.py — легаси вне кабинетной конвенции: ровно два naive-места,
        оба display-only и в БД не пишутся («тихие часы» ремайндеров ~2848 и
        метка python_logs через strftime). Новые naive-вхождения запрещены;
        починка существующих — отдельная работа."""
        src = MAIN_PY.read_text(encoding="utf-8")
        assert src.count(self.NAIVE_UTCNOW) == 1, \
            "в main.py появилось новое naive-время — используйте aware (timezone.utc)"
        # 15 display-меток «HH:MM» стенограммы (strftime, в БД не пишутся);
        # осознанное добавление такой же метки — инкремент константы с ревью.
        # +1 (2026-08-15): системная запись прощания в close_cabinet_session
        # (план 2026-08-14-001, U2) — той же display-конвенции.
        # +1 (2026-08-21): маркер границы передачи mark_ai_handoff
        # (план 2026-08-21-001, U1) — display-метка строки истории.
        assert src.count(self.NAIVE_NOW) == 16, \
            "в main.py появилось новое naive-время — используйте aware (timezone.utc)"


class TestEvaluationCarryover:
    """Контракт docs/N8N_HANDOFF_CABINET.md: found_info/ti_valuation переживают
    пересборку json_cache через qualification_context.evaluation_carryover
    (шлётся на каждом ходе; нет данных → ключа нет вовсе, не пустой объект)."""

    FOUND = {"mark": "KIA", "model": "Rio", "year": 2019, "plate": "А123БВ777",
             "mileage": 87000, "steering_wheel": "LEFT",
             "photos": {"main": "//avatars.mds.yandex.net/x.jpg"}}

    def test_full_structure(self):
        carry = payload_v2.build_evaluation_carryover(
            {"n8n_cache": {"found_info": dict(self.FOUND), "ti_valuation": 890000}})
        assert carry == {
            "found_info": self.FOUND,
            "ti_valuation": 890000,
            "request": {"plate": "А123БВ777", "mileage": 87000},
        }

    def test_only_valuation_without_found_info(self):
        carry = payload_v2.build_evaluation_carryover(
            {"n8n_cache": {"ti_valuation": 500000}})
        assert carry == {"found_info": None, "ti_valuation": 500000,
                         "request": {"plate": None, "mileage": None}}

    def test_absent_when_no_evaluation_data(self):
        assert payload_v2.build_evaluation_carryover({}) is None
        assert payload_v2.build_evaluation_carryover({"n8n_cache": None}) is None
        assert payload_v2.build_evaluation_carryover(
            {"n8n_cache": {"complectations": []}}) is None
        # Пустой/не-dict found_info = отсутствие, не «пустой объект»
        assert payload_v2.build_evaluation_carryover(
            {"n8n_cache": {"found_info": {}}}) is None
        assert payload_v2.build_evaluation_carryover(
            {"n8n_cache": {"found_info": "мусор"}}) is None

    def test_wiring_pinned_in_both_payload_paths(self):
        """Проводка: оба места сборки payload (chat и handoff) зовут билдер
        и кладут ключ только при непустом результате."""
        src = MAIN_PY.read_text(encoding="utf-8")
        assert src.count("payload_v2.build_evaluation_carryover(user_context)") == 2, \
            "carryover обязан собираться в обоих payload-путях (chat + handoff)"
        assert src.count('payload["qualification_context"]["evaluation_carryover"]') == 2


class TestEvaluationStageLatch:
    """План 2026-08-14-001, U4: защёлка «оценка получена» → closing.
    Правило ЗА ВЫКЛЮЧЕННЫМ env-флагом STAGE_EVAL_RULE: код и тесты живут,
    включение — отдельное решение, синхронно со стадийной половиной n8n."""

    CACHE_OK = {"ti_valuation": 890000, "found_info": {"plate": "А123БВ777"}}

    def _latched_ctx(self, scenario="tradein", **extra):
        ctx = {"scenario_type": scenario, "chat_history": [
            {"role": "user", "text": "оцените"}],
            "n8n_cache": dict(self.CACHE_OK)}
        ctx.update(extra)
        payload_v2.register_evaluation(ctx)
        return ctx

    # --- Флаг (условие включения) ---

    def test_rule_inert_with_flag_off_default(self, monkeypatch):
        monkeypatch.delenv("STAGE_EVAL_RULE", raising=False)
        ctx = self._latched_ctx()
        sc = build_session_context(ctx, "ещё вопрос", "base_v01.03")
        assert ctx["evaluation_received"] is True, "защёлка стоит"
        assert sc["current_stage"] == "data_collect", \
            "дефолт — флаг ВЫКЛ: поведение эвристики прежнее"

    def test_rule_applies_with_flag_on(self, monkeypatch):
        monkeypatch.setenv("STAGE_EVAL_RULE", "1")
        for scenario in ("tradein", "combo"):
            ctx = self._latched_ctx(scenario)
            sc = build_session_context(ctx, "ещё вопрос", "base_v01.03")
            assert sc["current_stage"] == "closing", scenario

    def test_rule_scoped_to_tradein_combo(self, monkeypatch):
        monkeypatch.setenv("STAGE_EVAL_RULE", "1")
        ctx = self._latched_ctx("credit", payment=300000)
        sc = build_session_context(ctx, "ещё вопрос", "base_v01.03")
        assert sc["current_stage"] == "tool_needed", \
            "кредит правила не касается — прежнее поведение по слотам"

    # --- Приоритеты ---

    def test_phone_collected_still_wins(self, monkeypatch):
        monkeypatch.setenv("STAGE_EVAL_RULE", "1")
        ctx = self._latched_ctx(phone_collected=True)
        sc = build_session_context(ctx, "текст", "base_v01.03")
        assert sc["current_stage"] == "closing"

    def test_greeting_wins_over_latch(self, monkeypatch):
        monkeypatch.setenv("STAGE_EVAL_RULE", "1")
        ctx = self._latched_ctx(chat_history=[])
        sc = build_session_context(ctx, "", "base_v01.03")
        assert sc["current_stage"] == "greeting"

    # --- Валидность оценки ---

    @pytest.mark.parametrize("valuation", [None, 0, -5, "890000", {}, [], True])
    def test_invalid_valuation_does_not_latch(self, valuation):
        ctx = {"n8n_cache": {"ti_valuation": valuation,
                             "found_info": {"plate": "А123БВ777"}}}
        payload_v2.register_evaluation(ctx)
        assert "evaluation_received" not in ctx, repr(valuation)

    def test_cache_rebuild_without_valuation_keeps_latch(self, monkeypatch):
        """R9: пересборка кэша (воркфлоу не влил carryover) защёлку не снимает
        — стадия не осциллирует."""
        monkeypatch.setenv("STAGE_EVAL_RULE", "1")
        ctx = self._latched_ctx()
        ctx["n8n_cache"] = {"complectations": []}  # свежий кэш без оценки
        payload_v2.register_evaluation(ctx)
        sc = build_session_context(ctx, "ещё вопрос", "base_v01.03")
        assert sc["current_stage"] == "closing", "защёлка пережила пересборку"

    # --- Снятие по госномеру из ТЕКСТА (R10) ---

    def test_same_plate_normalized_keeps_latch(self, monkeypatch):
        monkeypatch.setenv("STAGE_EVAL_RULE", "1")
        ctx = self._latched_ctx()
        sc = build_session_context(ctx, "а 123 бв 777 мой номер", "base_v01.03")
        assert sc["current_stage"] == "closing", \
            "нормализация: регистр/пробелы не различаются"

    def test_latin_homoglyphs_match(self, monkeypatch):
        monkeypatch.setenv("STAGE_EVAL_RULE", "1")
        ctx = self._latched_ctx()
        # A/B — латиница; после нормализации совпадает с А123БВ777... латинской
        # B в кириллице соответствует В: возьмём номер без Б — отдельный кейс
        assert payload_v2.normalize_plate("A123BC77") == \
            payload_v2.normalize_plate("а123вс77")

    def test_new_plate_in_text_unlatches_and_updates_slot(self, monkeypatch):
        monkeypatch.setenv("STAGE_EVAL_RULE", "1")
        ctx = self._latched_ctx()
        sc = build_session_context(ctx, "другая машина х777хх77", "base_v01.03")
        assert "evaluation_received" not in ctx, "защёлка снята"
        assert ctx["plate"] == "Х777ХХ77", "слот plate обновлён извлечённым номером"
        assert sc["current_stage"] == "data_collect", \
            "повторная оценка не блокирована (mileage-слот пуст — data_collect)"

    def test_message_without_plate_keeps_unlatched_state(self, monkeypatch):
        monkeypatch.setenv("STAGE_EVAL_RULE", "1")
        ctx = self._latched_ctx()
        build_session_context(ctx, "другая машина х777хх77", "base_v01.03")
        sc = build_session_context(ctx, "просто вопрос", "base_v01.03")
        assert "evaluation_received" not in ctx, \
            "защёлка не восстанавливается сама (до новой валидной оценки)"
        assert sc["current_stage"] == "data_collect"

    def test_latch_without_plate_binding_unlatched_by_any_plate(self, monkeypatch):
        monkeypatch.setenv("STAGE_EVAL_RULE", "1")
        ctx = {"scenario_type": "tradein", "chat_history": [
            {"role": "user", "text": "оцените"}],
            "n8n_cache": {"ti_valuation": 500000}}  # found_info.plate нет
        payload_v2.register_evaluation(ctx)
        assert ctx.get("evaluation_plate") is None
        sc = build_session_context(ctx, "номер е555кх154", "base_v01.03")
        assert "evaluation_received" not in ctx, \
            "защёлка без привязки снимается любым валидным номером"
        assert sc["current_stage"] == "data_collect"

    # --- Взаимодействие со сбросом кэша (R12) ---

    def test_refresh_fires_once_on_transition_to_closing(self, monkeypatch):
        monkeypatch.setenv("STAGE_EVAL_RULE", "1")
        ctx = {"n8n_cache": {"x": 1}, "n8n_cache_saved_at": time.time()}
        assert should_refresh_cache(ctx, "closing", "data_collect") is True, \
            "переход по защёлке — один сброс"
        assert should_refresh_cache(ctx, "closing", "closing") is False, \
            "пребывание в closing сброс не повторяет"


class TestEvaluationLatchReviewFixes:
    """Фиксы ревью 2026-08-15: unlatch за флагом; эхо carryover не
    ре-защёлкивает снятый номер."""

    CACHE_OK = {"ti_valuation": 890000, "found_info": {"plate": "А123БВ777"}}

    def test_flag_off_plate_slot_never_mutated(self, monkeypatch):
        """Флаг ВЫКЛ: номер в тексте НЕ мутирует слот plate и защёлку —
        прод-поведение не меняется вовсе."""
        monkeypatch.delenv("STAGE_EVAL_RULE", raising=False)
        ctx = {"scenario_type": "tradein", "chat_history": [
            {"role": "user", "text": "оцените"}], "n8n_cache": dict(self.CACHE_OK)}
        payload_v2.register_evaluation(ctx)
        build_session_context(ctx, "другая машина х777хх77", "base_v01.03")
        assert "plate" not in ctx, "слот plate при выключенном флаге не трогается"
        assert ctx.get("evaluation_received") is True, "защёлка цела (инертна)"

    def test_carryover_echo_does_not_relatch_stale_plate(self, monkeypatch):
        """После снятия защёлки новым номером эхо carryover (старые
        ti_valuation/found_info в свежем кэше) НЕ ре-защёлкивает старый номер."""
        monkeypatch.setenv("STAGE_EVAL_RULE", "1")
        ctx = {"scenario_type": "tradein", "chat_history": [
            {"role": "user", "text": "оцените"}], "n8n_cache": dict(self.CACHE_OK)}
        payload_v2.register_evaluation(ctx)
        build_session_context(ctx, "другая машина х777хх77", "base_v01.03")
        assert "evaluation_received" not in ctx

        # Следующий ход: n8n влил carryover старой оценки в новый кэш
        ctx["n8n_cache"] = dict(self.CACHE_OK)
        payload_v2.register_evaluation(ctx)
        assert "evaluation_received" not in ctx, \
            "эхо старой оценки не восстанавливает защёлку (R10)"

        # Новая оценка ДРУГОГО номера ставится штатно
        ctx["n8n_cache"] = {"ti_valuation": 500000,
                            "found_info": {"plate": "Х777ХХ77"}}
        payload_v2.register_evaluation(ctx)
        assert ctx.get("evaluation_received") is True
        assert ctx.get("evaluation_plate") == "Х777ХХ77"
        assert "evaluation_stale_plate" not in ctx

    def test_plate_re_false_positive_on_glued_text_documented(self, monkeypatch):
        """Известное ограничение (residual ревью): PLATE_RE на склеенном
        probe может выхватить межсловную последовательность. Фиксируем
        текущее поведение тестом, чтобы изменение было осознанным."""
        monkeypatch.setenv("STAGE_EVAL_RULE", "1")
        ctx = {"scenario_type": "tradein", "chat_history": [
            {"role": "user", "text": "оцените"}], "n8n_cache": dict(self.CACHE_OK)}
        payload_v2.register_evaluation(ctx)
        # «вариант 123 вс 77» → probe "ВАРИАНТ123ВС77" → матч Т123ВС77
        build_session_context(ctx, "вариант 123 вс 77 подходит?", "base_v01.03")
        assert "evaluation_received" not in ctx, \
            "склеенный текст снимает защёлку (известное flag-gated ограничение)"


class TestEvaluationValueSlot:
    """План 2026-09-07-001 (U5): сумма оценки — персистентным слотом защёлки."""

    CACHE_OK = {"ti_valuation": 1820000, "found_info": {"plate": "М736ОТ37"}}

    def _ctx(self):
        ctx = {"scenario_type": "combo", "chat_history": [
            {"role": "user", "text": "оцените"}],
            "n8n_cache": dict(self.CACHE_OK)}
        payload_v2.register_evaluation(ctx)
        return ctx

    def test_slot_set_with_latch(self):
        assert self._ctx()["evaluation_value"] == 1820000

    def test_slot_survives_cache_rebuild(self):
        """Пересборка кэша без оценки слот не трогает — как и защёлку (R9)."""
        ctx = self._ctx()
        ctx["n8n_cache"] = {"complectations": []}
        payload_v2.register_evaluation(ctx)
        assert ctx["evaluation_value"] == 1820000

    def test_invalid_valuation_sets_no_slot(self):
        ctx = {"n8n_cache": {"ti_valuation": "мусор",
                             "found_info": {"plate": "М736ОТ37"}}}
        payload_v2.register_evaluation(ctx)
        assert "evaluation_value" not in ctx

    def test_unlatch_clears_slot(self, monkeypatch):
        """Новый госномер снял защёлку → оценка снятого номера недействительна."""
        monkeypatch.setenv("STAGE_EVAL_RULE", "1")
        ctx = self._ctx()
        build_session_context(ctx, "другая машина х777хх77", "base_v01.03")
        assert "evaluation_value" not in ctx
