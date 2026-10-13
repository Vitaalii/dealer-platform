"""U5: сериализация конкурентных ходов per-user (R29)."""
import asyncio
import pathlib
from collections import defaultdict

import pytest

from turn_locks import get_turn_lock

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"


class TurnSimulator:
    """Мини-модель обвязки delayed_sender: буфер + лок, как в main.py."""

    def __init__(self):
        self.buffers = defaultdict(list)
        self.events = []  # (user, "start"|"end", combined_text)

    async def turn(self, user, roundtrip_s):
        async with get_turn_lock(user):
            combined = " ".join(self.buffers.pop(user, []))
            if not combined.strip():
                return None
            self.events.append((user, "start", combined))
            await asyncio.sleep(roundtrip_s)  # имитация round-trip к n8n
            self.events.append((user, "end", combined))
            return combined


@pytest.mark.asyncio
async def test_same_user_turns_are_serialized():
    sim = TurnSimulator()
    sim.buffers["u1"].append("A")
    t1 = asyncio.create_task(sim.turn("u1", 0.05))
    await asyncio.sleep(0.01)  # t1 уже в round-trip
    sim.buffers["u1"].append("B")  # сообщение во время in-flight → в буфер
    t2 = asyncio.create_task(sim.turn("u1", 0.01))
    r1, r2 = await asyncio.gather(t1, t2)

    assert r1 == "A" and r2 == "B", "оба хода обработаны, ничего не потеряно"
    kinds = [(e[0], e[1]) for e in sim.events]
    assert kinds == [("u1", "start"), ("u1", "end"), ("u1", "start"), ("u1", "end")], \
        "ходы не перемежаются: второй начался только после конца первого"


@pytest.mark.asyncio
async def test_message_during_inflight_joins_next_turn_not_lost():
    # уникальный id: asyncio.Lock нельзя переиспользовать между event loop'ами тестов
    sim = TurnSimulator()
    sim.buffers["u2"].append("A")
    t1 = asyncio.create_task(sim.turn("u2", 0.05))
    await asyncio.sleep(0.01)
    # два сообщения во время in-flight склеиваются в один следующий ход
    sim.buffers["u2"].append("B")
    sim.buffers["u2"].append("C")
    t2 = asyncio.create_task(sim.turn("u2", 0.01))
    t3 = asyncio.create_task(sim.turn("u2", 0.01))  # опоздавший таск с пустым буфером
    r1, r2, r3 = await asyncio.gather(t1, t2, t3)
    assert r1 == "A"
    assert r2 == "B C"
    assert r3 is None, "пустой буфер → ход-пустышка тихо выходит"


@pytest.mark.asyncio
async def test_users_do_not_block_each_other():
    sim = TurnSimulator()
    sim.buffers["slow"].append("S")
    sim.buffers["fast"].append("F")
    t_slow = asyncio.create_task(sim.turn("slow", 0.2))
    await asyncio.sleep(0.01)
    t_fast = asyncio.create_task(sim.turn("fast", 0.01))
    await asyncio.gather(t_slow, t_fast)
    order = [(e[0], e[1]) for e in sim.events]
    assert order.index(("fast", "end")) < order.index(("slow", "end")), \
        "быстрый пользователь не ждёт лок медленного"


class TestMainWiring:
    """Обвязка в main.py: дерегистрация → лок → pop буфера, именно в этом порядке."""

    def test_order_of_operations_in_delayed_sender(self):
        src = MAIN_PY.read_text(encoding="utf-8")
        assert "from turn_locks import get_turn_lock" in src

        dereg = src.index("USER_MESSAGE_TASKS.get(max_user_id) is asyncio.current_task()")
        lock = src.index("async with get_turn_lock(max_user_id):")
        pop = src.index('USER_MESSAGE_BUFFERS.pop(max_user_id, [])')
        assert dereg < lock < pop, "порядок: дерегистрация -> лок -> pop буфера"

    def test_no_unconditional_task_pop_after_processing(self):
        """Безусловный pop в конце обработки снял бы ЧУЖОЙ (новый) таск из реестра."""
        src = MAIN_PY.read_text(encoding="utf-8")
        lock_pos = src.index("async with get_turn_lock(max_user_id):")
        tail = src[lock_pos:src.index("USER_MESSAGE_TASKS[max_user_id] = asyncio.create_task", lock_pos)]
        assert "USER_MESSAGE_TASKS.pop" not in tail