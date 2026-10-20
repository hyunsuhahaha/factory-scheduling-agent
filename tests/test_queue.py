import asyncio

import pytest

from factory_agent.queue import JobQueue


@pytest.fixture
def queue(redis):
    return JobQueue(redis, default_limit=1, lease_seconds=30, max_attempts=2)


async def test_priority_then_fifo(queue):
    await queue.enqueue("low", 1, priority=1)
    await queue.enqueue("normal-1", 2, priority=5)
    await queue.enqueue("urgent", 3, priority=9)
    await queue.enqueue("normal-2", 4, priority=5)
    order = [(await queue.claim()).job_id for _ in range(4)]
    assert order == ["urgent", "normal-1", "normal-2", "low"]


async def test_fair_share_interleaves_users(redis):
    # 사용자 1이 작업 3개를 먼저 몰아 넣고 사용자 2가 1개를 넣으면, 사용자 2의 작업이 사용자 1의 두 번째 작업보다 먼저다.
    q = JobQueue(redis, default_limit=100)
    for i in range(3):
        await q.enqueue(f"u1-{i}", 1, cost_seconds=10)
    await q.enqueue("u2-0", 2, cost_seconds=10)
    order = [(await q.claim()).job_id for _ in range(4)]
    assert order == ["u1-0", "u2-0", "u1-1", "u1-2"]


async def test_per_user_limit_lets_other_users_through(queue):
    # 사용자 1이 작업을 여러 개 먼저 넣어도, 한도(1)를 채우면 사용자 2의 작업이 먼저 돈다.
    for i in range(3):
        await queue.enqueue(f"u1-{i}", 1)
    await queue.enqueue("u2-0", 2)
    first, second = await queue.claim(), await queue.claim()
    assert [first.job_id, second.job_id] == ["u1-0", "u2-0"]
    assert await queue.claim() is None  # 둘 다 한도에 걸림
    await queue.complete("u1-0")
    assert (await queue.claim()).job_id == "u1-1"


async def test_user_limit_override(queue):
    await queue.set_user_limit(1, 2)
    for i in range(3):
        await queue.enqueue(f"j{i}", 1)
    assert (await queue.claim()) and (await queue.claim())
    assert await queue.claim() is None


async def test_concurrent_claims_never_double_assign(redis):
    q = JobQueue(redis, default_limit=100)
    for i in range(50):
        await q.enqueue(f"j{i}", i % 5)
    results = await asyncio.gather(*[q.claim() for _ in range(80)])
    claimed = [c.job_id for c in results if c]
    assert len(claimed) == 50 and len(set(claimed)) == 50


async def test_expired_lease_is_requeued_then_dead(redis):
    q = JobQueue(redis, default_limit=1, lease_seconds=0, max_attempts=2)
    await q.enqueue("j", 1)
    assert (await q.claim()).attempt == 1
    await asyncio.sleep(0.01)
    assert await q.reap() == (["j"], [])
    assert (await q.stats())["running_by_user"] == {}
    assert (await q.claim()).attempt == 2
    await asyncio.sleep(0.01)
    assert await q.reap() == ([], ["j"])
    assert await q.claim() is None


async def test_heartbeat_and_late_complete(redis):
    q = JobQueue(redis, default_limit=1, lease_seconds=0)
    await q.enqueue("j", 1)
    await q.claim()
    await asyncio.sleep(0.01)
    await q.reap()  # 다른 워커가 회수
    q.lease_ms = 30_000
    assert await q.heartbeat("j") is False
    assert await q.complete("j") is False  # 늦게 끝난 워커가 카운터를 망가뜨리지 않는다


async def test_cancel_pending(queue):
    await queue.enqueue("j", 1)
    assert await queue.position("j") == 0
    assert await queue.cancel("j")
    assert await queue.position("j") is None
    assert await queue.claim() is None
