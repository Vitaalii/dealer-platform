"""U7 (план 2026-09-15-001, KTD-9): register_webhooks — идемпотентность по
фактическому состоянию (URL) и intent-состоянию (секрет), порядок POST→DELETE
для MAX, --confirm-env, --enforce как отдельный шаг (R18), маскирование,
завершение процесса (дефект дев-прогона 2026-09-19)."""
import asyncio as _asyncio
import hashlib
import os
import pathlib
import subprocess
import sys

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import select

import bot_identity
import bot_registry
import database
from scripts import register_webhooks as rw


class AlertRecorder:
    async def __call__(self, text):
        return True


def rows_fetcher(rows):
    async def fetch():
        return rows
    return fetch


PILOT_ROWS = [
    {"Bot_Key": "max-haval", "Channel": "max", "Brands": "haval",
     "Token_Env": "BOT_TOKEN_MAX_HAVAL_T", "Environment": "dev", "Active": True},
    {"Bot_Key": "tg-haval", "Channel": "telegram", "Brands": "haval",
     "Token_Env": "BOT_TOKEN_TG_HAVAL_T", "Environment": "dev", "Active": True},
]

BASE = "https://dev.example.test"
HOST = "dev.example.test"


class FakeHttp:
    """Скриптуемое состояние платформ: TG-URL и список MAX-подписок."""

    def __init__(self):
        self.tg_urls = {}          # token -> url
        self.max_urls = {}         # token -> [urls]
        self.calls = []            # (method, короткая метка, body)
        self.fail = set()          # {'tg-set', 'max-post', 'max-delete'}

    def _tok(self, url, headers):
        if "api.telegram.org" in url:
            return url.split("/bot")[1].split("/")[0]
        return (headers or {}).get("Authorization", "")

    async def request(self, method, url, headers=None, json_body=None):
        token = self._tok(url, headers)
        if "getWebhookInfo" in url:
            self.calls.append(("GET", "tg-info", None))
            return 200, {"ok": True, "result": {"url": self.tg_urls.get(token, "")}}
        if "deleteWebhook" in url:
            self.calls.append(("POST", "tg-delete", None))
            if "tg-delete" in self.fail:
                return 500, {}
            self.tg_urls[token] = ""
            return 200, {"ok": True}
        if "setWebhook" in url:
            self.calls.append(("POST", "tg-set", json_body))
            if "tg-set" in self.fail:
                return 500, {}
            self.tg_urls[token] = json_body["url"]
            return 200, {"ok": True}
        if url.endswith("/subscriptions") and method == "GET":
            self.calls.append(("GET", "max-list", None))
            return 200, {"subscriptions": [
                {"url": u} for u in self.max_urls.get(token, [])]}
        if "/subscriptions" in url and method == "POST":
            self.calls.append(("POST", "max-post", json_body))
            if "max-post" in self.fail:
                return 500, {}
            self.max_urls.setdefault(token, []).append(json_body["url"])
            return 200, {}
        if method == "DELETE":
            target = url.split("url=", 1)[1]
            self.calls.append(("DELETE", "max-delete", target))
            if "max-delete" in self.fail:
                return 500, {}
            self.max_urls[token] = [u for u in self.max_urls.get(token, [])
                                    if u != target]
            return 200, {}
        raise AssertionError(f"неожиданный запрос: {method} {url}")

    def mutating_calls(self):
        return [c for c in self.calls
                if c[1] in ("tg-set", "tg-delete", "max-post", "max-delete")]


@pytest.fixture
def env(db, monkeypatch):
    bot_registry.reset_for_tests()
    monkeypatch.setenv("BOT_ENV", "dev")
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("MAX_BOT_TOKEN", "legacy-max-token-0001")
    monkeypatch.setenv("TG_BOT_TOKEN", "legacy-tg-token-0002")
    # U9: те же значения под новыми именами (см. комментарий в test_bot_registry)
    monkeypatch.setenv("MAX_BOT_TOKEN_TEST", "legacy-max-token-0001")
    monkeypatch.setenv("TG_BOT_TOKEN_TEST", "legacy-tg-token-0002")
    monkeypatch.setenv("BOT_TOKEN_MAX_HAVAL_T", "pilot-max-token-0003")
    monkeypatch.setenv("BOT_TOKEN_TG_HAVAL_T", "pilot-tg-token-0004")
    bot_registry.setup(alert=AlertRecorder(), fetch_rows=rows_fetcher(PILOT_ROWS))
    yield
    bot_registry.reset_for_tests()


def args(*argv):
    return rw.build_parser().parse_args(list(argv))


async def _snapshot(bot_key):
    async with database.AsyncSessionLocal() as session:
        return (await session.execute(
            select(database.BotRegistrySnapshot)
            .where(database.BotRegistrySnapshot.bot_key == bot_key)
        )).scalar_one_or_none()


# =======================
# check / confirm-env
# =======================

@pytest.mark.asyncio
async def test_check_makes_no_mutating_calls(env):
    http = FakeHttp()
    code = await rw.run(args(), http=http)
    assert code == 0
    assert http.mutating_calls() == []
    # явный флаг --check принимается и эквивалентен дефолту (инструкции U7)
    assert await rw.run(args("--check"), http=http) == 0
    assert http.mutating_calls() == []


@pytest.mark.asyncio
async def test_apply_refused_without_or_wrong_confirm_env(env, capsys):
    http = FakeHttp()
    assert await rw.run(args("--apply"), http=http) == 1
    assert await rw.run(args("--apply", "--confirm-env=prod.example"), http=http) == 1
    assert http.mutating_calls() == []
    assert "ОТКАЗ" in capsys.readouterr().out


# =======================
# идемпотентность: URL + intent-секрет
# =======================

@pytest.mark.asyncio
async def test_apply_then_second_apply_is_noop(env):
    http = FakeHttp()
    assert await rw.run(args("--apply", f"--confirm-env={HOST}"), http=http) == 0
    # состояние применено: 4 бота (2 TG setWebhook + 2 MAX POST)
    assert len([c for c in http.calls if c[1] == "tg-set"]) == 2
    assert len([c for c in http.calls if c[1] == "max-post"]) == 2
    snap = await _snapshot("max-haval")
    secret = bot_identity.derive_webhook_secret("pilot-max-token-0003", "max-haval")
    assert snap.secret_intent_hash == hashlib.sha256(secret.encode()).hexdigest()
    assert snap.confirmed_url == f"{BASE}/max_webhook/max-haval"
    assert snap.secret_enforced is False                  # apply НЕ включает проверку

    http.calls.clear()
    assert await rw.run(args("--apply", f"--confirm-env={HOST}"), http=http) == 0
    assert http.mutating_calls() == []                    # два прогона подряд — no-op


@pytest.mark.asyncio
async def test_url_matches_but_secret_unconfirmed_forces_reapply(env):
    """Слепота к секрету закрыта: секрет снаружи ненаблюдаем — решает intent."""
    http = FakeHttp()
    # U9: легаси-ключи окружения (BOT_ENV=dev в фикстуре) — tg-test/max-test
    want_tg = f"{BASE}/tg_webhook/tg-test"
    want_max = f"{BASE}/max_webhook/max-test"
    http.tg_urls["legacy-tg-token-0002"] = want_tg        # URL уже совпадает
    http.max_urls["legacy-max-token-0001"] = [want_max]
    assert await rw.run(args("--apply", f"--confirm-env={HOST}", "--bot", "tg-test"),
                        http=http) == 0
    assert "tg-set" in [c[1] for c in http.calls]         # setWebhook всё равно вызван
    http.calls.clear()
    # MAX: пере-подписка того же URL = DELETE этого URL, затем POST (без дублей)
    assert await rw.run(args("--apply", f"--confirm-env={HOST}", "--bot", "max-test"),
                        http=http) == 0
    labels = [c[1] for c in http.calls]
    assert labels.index("max-delete") < labels.index("max-post")
    deleted = [c[2] for c in http.calls if c[1] == "max-delete"]
    assert want_max in deleted
    assert http.max_urls["legacy-max-token-0001"] == [want_max]   # без дублей


@pytest.mark.asyncio
async def test_max_stale_subscriptions_post_before_delete(env):
    http = FakeHttp()
    http.max_urls["pilot-max-token-0003"] = [
        "https://old-ngrok-1.test/max_webhook", "https://old-ngrok-2.test/max_webhook"]
    assert await rw.run(args("--apply", f"--confirm-env={HOST}", "--bot", "max-haval"),
                        http=http) == 0
    labels = [c[1] for c in http.calls]
    assert labels.index("max-post") < labels.index("max-delete")   # POST раньше
    assert http.max_urls["pilot-max-token-0003"] == [f"{BASE}/max_webhook/max-haval"]


@pytest.mark.asyncio
async def test_max_post_failure_inconsistent_then_repair(env, capsys):
    http = FakeHttp()
    http.max_urls["pilot-max-token-0003"] = ["https://old.test/max_webhook"]
    http.fail.add("max-post")
    assert await rw.run(args("--apply", f"--confirm-env={HOST}", "--bot", "max-haval"),
                        http=http) == 1
    assert "неконсистентно" in capsys.readouterr().out
    assert "max-delete" not in [c[1] for c in http.calls]  # старую не сносили
    http.fail.clear()
    assert await rw.run(args("--apply", f"--confirm-env={HOST}", "--bot", "max-haval"),
                        http=http) == 0                    # повторный --apply чинит
    assert http.max_urls["pilot-max-token-0003"] == [f"{BASE}/max_webhook/max-haval"]


@pytest.mark.asyncio
async def test_max_delete_failure_inconsistent_then_repair(env, capsys):
    http = FakeHttp()
    http.max_urls["pilot-max-token-0003"] = ["https://old.test/max_webhook"]
    http.fail.add("max-delete")
    assert await rw.run(args("--apply", f"--confirm-env={HOST}", "--bot", "max-haval"),
                        http=http) == 1
    assert "неконсистентно" in capsys.readouterr().out
    http.fail.clear()
    assert await rw.run(args("--apply", f"--confirm-env={HOST}", "--bot", "max-haval"),
                        http=http) == 0


# =======================
# enforce — отдельный шаг (R18)
# =======================

@pytest.mark.asyncio
async def test_enforce_refused_before_apply_allowed_after(env, capsys):
    http = FakeHttp()
    assert await rw.run(args("--enforce", "tg-haval"), http=http) == 1
    assert "ОТКАЗ" in capsys.readouterr().out
    assert await rw.run(args("--apply", f"--confirm-env={HOST}"), http=http) == 0
    assert await rw.run(args("--enforce", "tg-haval"), http=http) == 0
    snap = await _snapshot("tg-haval")
    assert snap.secret_enforced is True
    assert await rw.run(args("--unenforce", "tg-haval"), http=http) == 0
    assert (await _snapshot("tg-haval")).secret_enforced is False


@pytest.mark.asyncio
async def test_enforce_refused_after_token_change(env, monkeypatch, capsys):
    http = FakeHttp()
    assert await rw.run(args("--apply", f"--confirm-env={HOST}"), http=http) == 0
    monkeypatch.setenv("BOT_TOKEN_TG_HAVAL_T", "rotated-token-9999")  # смена токена
    assert await rw.run(args("--enforce", "tg-haval"), http=http) == 1
    assert "повторите --apply" in capsys.readouterr().out


# =======================
# пропуски и маскирование
# =======================

@pytest.mark.asyncio
async def test_missing_env_var_skipped_not_fatal(env, monkeypatch, capsys):
    monkeypatch.delenv("BOT_TOKEN_MAX_HAVAL_T", raising=False)
    http = FakeHttp()
    assert await rw.run(args("--apply", f"--confirm-env={HOST}"), http=http) == 0
    out = capsys.readouterr().out
    assert "пропущен" in out and "BOT_TOKEN_MAX_HAVAL_T" in out
    # изменяющих вызовов для пропущенного MAX-бота нет
    assert not [c for c in http.calls if c[1] == "max-post"
                and "max-haval" in str(c[2])]


@pytest.mark.asyncio
async def test_stdout_never_contains_tokens_or_secrets(env, capsys):
    http = FakeHttp()
    await rw.run(args(), http=http)
    await rw.run(args("--apply", f"--confirm-env={HOST}"), http=http)
    await rw.run(args("--enforce", "tg-haval"), http=http)
    out = capsys.readouterr().out
    for token in ("legacy-max-token-0001", "legacy-tg-token-0002",
                  "pilot-max-token-0003", "pilot-tg-token-0004"):
        assert token not in out
        for key in ("max-main", "tg-main", "max-haval", "tg-haval"):
            assert bot_identity.derive_webhook_secret(token, key) not in out


# =======================
# Webhook_Base (U9a): своя база вебхука бота (прокси-воркер обхода TG)
# =======================

WORKER = "https://sparkling-sun-8c0c.example.workers.dev"
PROXY_ROWS = PILOT_ROWS + [
    {"Bot_Key": "tg-proxy", "Channel": "telegram", "Brands": "geely",
     "Token_Env": "BOT_TOKEN_TG_HAVAL_T", "Environment": "dev", "Active": True,
     "Webhook_Base": WORKER + "/"},                     # хвостовой '/' срезается
]


@pytest.fixture
def env_proxy(env, monkeypatch):
    bot_registry.setup(fetch_rows=rows_fetcher(PROXY_ROWS))
    yield


@pytest.mark.asyncio
async def test_webhook_base_apply_and_check(env_proxy):
    """--apply прописывает <база бота>/tg_webhook/<bot_key>, confirmed_url
    подтверждается той же базой, повторный прогон — no-op."""
    http = FakeHttp()
    want = f"{WORKER}/tg_webhook/tg-proxy"
    assert await rw.run(args("--apply", f"--confirm-env={HOST}", "--bot", "tg-proxy"),
                        http=http) == 0
    assert http.tg_urls["pilot-tg-token-0004"] == want    # URL — воркерный
    snap = await _snapshot("tg-proxy")
    assert snap.confirmed_url == want
    http.calls.clear()
    assert await rw.run(args("--apply", f"--confirm-env={HOST}", "--bot", "tg-proxy"),
                        http=http) == 0
    assert http.mutating_calls() == []                    # сверка с базой бота


@pytest.mark.asyncio
async def test_webhook_base_empty_falls_back_to_public_base(env_proxy):
    """У ботов без Webhook_Base ожидание остаётся на PUBLIC_BASE_URL."""
    http = FakeHttp()
    assert await rw.run(args("--apply", f"--confirm-env={HOST}", "--bot", "tg-haval"),
                        http=http) == 0
    assert http.tg_urls["pilot-tg-token-0004"] == f"{BASE}/tg_webhook/tg-haval"


@pytest.mark.asyncio
async def test_webhook_base_enforce_after_worker_apply(env_proxy):
    """--enforce работает поверх воркерной регистрации как обычно (R18)."""
    http = FakeHttp()
    await rw.run(args("--apply", f"--confirm-env={HOST}", "--bot", "tg-proxy"), http=http)
    assert await rw.run(args("--enforce", "tg-proxy"), http=http) == 0
    assert (await _snapshot("tg-proxy")).secret_enforced is True


# =======================
# --deregister (U9)
# =======================

@pytest.mark.asyncio
async def test_deregister_requires_confirm_env(env, capsys):
    http = FakeHttp()
    assert await rw.run(args("--deregister", "tg-haval"), http=http) == 1
    assert http.mutating_calls() == []
    assert "ОТКАЗ" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_deregister_tg_clears_webhook_and_snapshot(env):
    http = FakeHttp()
    assert await rw.run(args("--apply", f"--confirm-env={HOST}"), http=http) == 0
    assert (await _snapshot("tg-haval")).confirmed_url
    http.calls.clear()
    assert await rw.run(args("--deregister", "tg-haval",
                             f"--confirm-env={HOST}"), http=http) == 0
    assert http.tg_urls["pilot-tg-token-0004"] == ""      # deleteWebhook прошёл
    snap = await _snapshot("tg-haval")
    assert snap.secret_intent_hash is None
    assert (snap.confirmed_url or "") == ""
    assert snap.secret_enforced is False


@pytest.mark.asyncio
async def test_deregister_max_removes_all_subscriptions(env):
    http = FakeHttp()
    http.max_urls["pilot-max-token-0003"] = [
        f"{BASE}/max_webhook/max-haval", "https://old.test/max_webhook"]
    assert await rw.run(args("--deregister", "max-haval",
                             f"--confirm-env={HOST}"), http=http) == 0
    assert http.max_urls["pilot-max-token-0003"] == []    # сняты ВСЕ подписки


@pytest.mark.asyncio
async def test_deregister_failure_keeps_snapshot_and_exit_code(env):
    http = FakeHttp()
    http.max_urls["pilot-max-token-0003"] = [f"{BASE}/max_webhook/max-haval"]
    await rw.run(args("--apply", f"--confirm-env={HOST}"), http=http)
    http.fail.add("max-delete")
    assert await rw.run(args("--deregister", "max-haval",
                             f"--confirm-env={HOST}"), http=http) == 1
    # секретный блок НЕ очищен: подписка фактически осталась
    assert (await _snapshot("max-haval")).secret_intent_hash is not None


@pytest.mark.asyncio
async def test_deregister_historic_alias_refused(env, capsys):
    """max-main — алиас легаси-бота окружения с ТЕМ ЖЕ токеном: снятие всех
    подписок убило бы действующего бота; скрипт отказывает с подсказкой."""
    http = FakeHttp()
    assert await rw.run(args("--deregister", "max-main",
                             f"--confirm-env={HOST}"), http=http) == 1
    out = capsys.readouterr().out
    assert "ОТКАЗ" in out and "max-test" in out
    assert http.mutating_calls() == []


# =======================
# Завершение процесса (дефект 2026-09-19: aiosqlite-поток без dispose)
# =======================

@pytest.mark.asyncio
@pytest.mark.parametrize("argv", [
    ["--bot", "ghost"],                      # режим по умолчанию (просмотр)
    ["--check", "--bot", "ghost"],           # явный --check
    ["--apply"],                             # отказ без --confirm-env
    ["--unenforce", "ghost"],                # enforce-семейство
])
async def test_cli_process_terminates(tmp_path, argv):
    """Скрипт обязан ЗАВЕРШАТЬСЯ после вывода: без dispose() пула БД
    non-daemon поток aiosqlite вешал процесс в threading._shutdown.
    Субпроцесс без сети (NocoDB env нет → фолбэк; --bot ghost/отказы —
    до HTTP-вызовов), но с реальным касанием БД (загрузка снапшота)."""
    db_path = tmp_path / "cli.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    import database as _db
    async with engine.begin() as conn:
        await conn.run_sync(_db.Base.metadata.create_all)
    await engine.dispose()

    script = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "register_webhooks.py"
    env_vars = {
        "PATH": os.environ.get("PATH", ""),
        "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "BOT_ENV": "dev",
        "PUBLIC_BASE_URL": "https://cli-test.example",
        "MAX_BOT_TOKEN": "cli-max-token-0001",
        "TG_BOT_TOKEN": "cli-tg-token-0002",
        # без сети: пустые значения выключают NocoDB-фетч (load_dotenv с
        # override=False уже существующие переменные не перезаписывает)
        "NOCODB_API_URL": "",
        "NOCODB_TOKEN": "",
    }
    proc = await _asyncio.to_thread(
        subprocess.run, [sys.executable, str(script), *argv],
        capture_output=True, text=True, timeout=25, env=env_vars,
        cwd=str(script.parent.parent))
    assert proc.returncode == 1                       # ghost/отказ — штатный exit-код
    assert "ОТКАЗ" in proc.stdout                     # вывод состоялся до завершения
    assert "cli-max-token-0001" not in proc.stdout


@pytest.mark.asyncio
async def test_bot_filter_limits_scope(env):
    http = FakeHttp()
    assert await rw.run(args("--apply", f"--confirm-env={HOST}", "--bot", "tg-haval"),
                        http=http) == 0
    assert len(http.mutating_calls()) == 1
    assert http.mutating_calls()[0][1] == "tg-set"
    assert await rw.run(args("--bot", "max-ghost"), http=http) == 1
