"""U6: потолок попыток pending_sync и недебаунсимый путь для кабинета (R35)."""
import asyncio
import json
import pathlib
import types
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

import database
from database import PendingSync, record_sync_failure
import main

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"


# =======================
# Фейковый aiohttp: один сценарий ответа на все post
# =======================

class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status = status
        self._body = body if body is not None else {"result": 1}

    async def json(self):
        return self._body


class _RespCtx:
    def __init__(self, resp_or_exc):
        self._r = resp_or_exc

    async def __aenter__(self):
        if isinstance(self._r, Exception):
            raise self._r
        return self._r

    async def __aexit__(self, *args):
        return False


class FakeClientSession:
    """Подменяет aiohttp.ClientSession в main. Сценарий задаётся классом:
    FakeResponse (в т.ч. не-200 или тело с ошибкой) либо Exception."""
    script = FakeResponse()

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, json=None):
        return _RespCtx(FakeClientSession.script)


# =======================
# Фикстуры (общая db — в conftest.py; здесь локальное отличие U6)
# =======================

@pytest_asyncio.fixture
async def db(db, monkeypatch):
    """Общая db + подмена AsyncSessionLocal ещё и в main."""
    monkeypatch.setattr(main, "AsyncSessionLocal", database.AsyncSessionLocal)
    yield db


@pytest.fixture
def replay_env(monkeypatch):
    """Фейковый транспорт + перехват техалертов для replay_pending_sync."""
    FakeClientSession.script = FakeResponse()
    monkeypatch.setattr(
        main, "aiohttp", types.SimpleNamespace(ClientSession=FakeClientSession)
    )
    alerts = []

    async def fake_alert(group_id=None, text=None, **kwargs):
        alerts.append((group_id, text))

    monkeypatch.setattr(main.max_client, "send_alert_to_group", fake_alert)
    monkeypatch.setattr(main, "TECH_ADMIN_ID", "tech-group-1")
    return alerts


async def _add_task(db, method="crm.deal.update", attempts=0, status="pending"):
    async with db.AsyncSessionLocal() as session:
        task = PendingSync(
            method=method,
            payload=json.dumps({"id": 42}),  # без кода заявки → часовой отчёт молчит
            user_id="u-ceiling",
            attempts=attempts,
            status=status,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task.id


async def _run_replay(db):
    async with db.AsyncSessionLocal() as session:
        await main.replay_pending_sync(session)


async def _fetch_task(db, task_id):
    async with db.AsyncSessionLocal() as session:
        return (
            await session.execute(select(PendingSync).where(PendingSync.id == task_id))
        ).scalar_one_or_none()


# =======================
# 1. Инкремент при неуспехе, удаление при успехе
# =======================

@pytest.mark.asyncio
async def test_failed_replay_increments_attempts(db, replay_env):
    task_id = await _add_task(db)
    FakeClientSession.script = ConnectionError("Bitrix down")
    await _run_replay(db)
    task = await _fetch_task(db, task_id)
    assert task is not None, "строка не должна удаляться при неуспехе"
    assert task.attempts == 1
    assert task.status == "pending"


@pytest.mark.asyncio
async def test_successful_replay_deletes_row(db, replay_env):
    task_id = await _add_task(db, attempts=3)
    FakeClientSession.script = FakeResponse(200, {"result": 777})
    await _run_replay(db)
    assert await _fetch_task(db, task_id) is None, "успешный реплей обязан удалить строку"
    assert replay_env == [], "успех не должен слать алертов"


# =======================
# 2. Молчаливый неуспех: не-200 или тело с ошибкой — без исключения
# =======================

@pytest.mark.asyncio
async def test_non_200_increments_attempts(db, replay_env):
    task_id = await _add_task(db)
    FakeClientSession.script = FakeResponse(status=500, body={})
    await _run_replay(db)
    task = await _fetch_task(db, task_id)
    assert task is not None and task.attempts == 1


@pytest.mark.asyncio
async def test_error_body_increments_attempts(db, replay_env):
    task_id = await _add_task(db)
    FakeClientSession.script = FakeResponse(200, {"error": "QUERY_LIMIT_EXCEEDED"})
    await _run_replay(db)
    task = await _fetch_task(db, task_id)
    assert task is not None and task.attempts == 1
    assert task.status == "pending"


# =======================
# 3. Порог: fatal, алерт ровно один раз, следующий цикл строку не трогает
# =======================

@pytest.mark.asyncio
async def test_ceiling_goes_fatal_alert_once_then_skipped(db, replay_env, monkeypatch):
    monkeypatch.setenv("PENDING_SYNC_MAX_ATTEMPTS", "3")
    task_id = await _add_task(db, attempts=2)
    FakeClientSession.script = FakeResponse(status=503, body={})

    await _run_replay(db)  # 3-я попытка → порог
    task = await _fetch_task(db, task_id)
    assert task.status == "fatal"
    assert task.attempts == 3
    assert len(replay_env) == 1, "алерт о переходе в fatal — ровно один"
    assert "fatal" in replay_env[0][1]

    await _run_replay(db)  # следующий цикл
    task = await _fetch_task(db, task_id)
    assert task is not None, "fatal-строка не удаляется"
    assert task.attempts == 3, "fatal-строка не ретраится"
    assert len(replay_env) == 1, "повторного алерта нет"


def test_record_sync_failure_helper_contract():
    """Хелпер сам по себе: сигнал алерта — только в момент перехода в fatal."""
    task = PendingSync(method="m", payload="{}", attempts=8, status="pending")
    assert record_sync_failure(task) is False   # 9 < 10 (дефолт порога)
    assert record_sync_failure(task) is True    # 10 → fatal
    assert task.status == "fatal"
    assert record_sync_failure(task) is False   # уже fatal: не трогаем, не сигналим
    assert task.attempts == 10


# =======================
# 4. Обход дебаунса для кабинета, чат-путь дебаунсится как раньше
# =======================

@pytest.mark.asyncio
async def test_skip_debounce_bypasses_30s_window(db):
    uid = f"user-{uuid.uuid4()}"
    method = "crm.deal.update"

    async def count_rows():
        async with db.AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    select(PendingSync).where(PendingSync.user_id == uid)
                )
            ).scalars().all()
            return len(rows)

    # Обычная постановка (чат-путь) проходит
    await main.BitrixService.enqueue_failed_request(method, {"n": 1}, user_id=uid)
    assert await count_rows() == 1

    # Повторная обычная в пределах 30 с — дебаунсится
    await main.BitrixService.enqueue_failed_request(method, {"n": 2}, user_id=uid)
    assert await count_rows() == 1

    # Кабинет с skip_debounce=True — попадает в очередь несмотря на окно
    await main.BitrixService.enqueue_failed_request(
        method, {"n": 3}, user_id=uid, skip_debounce=True
    )
    assert await count_rows() == 2

    # Чат-путь после кабинетной постановки по-прежнему дебаунсится
    await main.BitrixService.enqueue_failed_request(method, {"n": 4}, user_id=uid)
    assert await count_rows() == 2


# =======================
# 5. Изоляция шагов sync_worker: сбой реплея не уносит обязанности кабинета
# =======================

@pytest.mark.asyncio
async def test_sync_worker_isolates_replay_failure_from_cabinet_duties(db, monkeypatch):
    """Реплей идёт в собственном try/except, как соседние шаги: иначе сбой БД
    в нём пропускал экспирацию токенов и проверку баланса почти на полчаса."""
    order = []

    async def failing_replay(_session):
        order.append("replay")
        raise RuntimeError("БД отвалилась посреди реплея")

    async def cabinet_duties():
        order.append("cabinet")

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 1:          # вторая итерация цикла — выходим
            raise asyncio.CancelledError

    monkeypatch.setattr(main, "replay_pending_sync", failing_replay)
    monkeypatch.setattr(main.cabinet_service, "sync_worker_duties", cabinet_duties)
    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    await main.sync_worker()

    assert order == ["replay", "cabinet"], \
        "обязанности кабинета вызваны в ТОЙ ЖЕ итерации, что упавший реплей"
    assert sleeps[0] == 1800, "штатный интервал не подменён аварийным бэкоффом"


# =======================
# 6. Source-grep: wiring закреплён в исходнике main.py
# =======================

class TestSourceWiring:
    def test_replay_calls_record_sync_failure_on_both_paths(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        assert src.count("record_sync_failure(task)") >= 2, \
            "реплей обязан звать record_sync_failure на обоих путях неуспеха"
        assert "await replay_pending_sync(session)" in src, \
            "sync_worker обязан гонять очередь через replay_pending_sync"
        assert 'PendingSync.status == "pending"' in src, \
            "выборка реплея обязана пропускать fatal-строки"

    def test_enqueue_has_skip_debounce_parameter(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "skip_debounce: bool = False" in src, \
            "enqueue_failed_request обязан принимать skip_debounce с дефолтом False"
        assert "if not skip_debounce:" in src, \
            "дебаунс обязан обходиться только при skip_debounce=True"
