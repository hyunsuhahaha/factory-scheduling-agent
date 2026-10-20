"""Redis 기반 작업 큐.

DB가 작업 상태의 원본이고, Redis는 "누가 다음에 무엇을 돌릴지"만 관리한다.

키 (prefix = fsa:{queue}):
  pending       ZSET  job_id -> score (우선순위 구간 * 1e13 + 등록 ms). 작을수록 먼저.
  running       ZSET  job_id -> lease 만료 ms
  owner         HASH  job_id -> user_id
  score         HASH  job_id -> 원래 pending score (재등록 시 순서 유지)
  attempts      HASH  job_id -> 시도 횟수
  user_running  HASH  user_id -> 실행 중 작업 수
  user_limit    HASH  user_id -> 동시 실행 한도 (없으면 기본값)

모든 상태 전이는 Lua 스크립트로 원자적으로 처리해 워커 여러 대가 동시에 claim해도
같은 작업을 두 번 가져가거나 사용자 한도를 넘지 않는다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from redis.asyncio import Redis

_CLAIM = """
local pending, running, owner, attempts, user_running, user_limit = KEYS[1], KEYS[2], KEYS[3], KEYS[4], KEYS[5], KEYS[6]
local now, lease, default_limit, scan = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4])
local ids = redis.call('ZRANGE', pending, 0, scan - 1)
for _, id in ipairs(ids) do
  local uid = redis.call('HGET', owner, id)
  local limit = tonumber(redis.call('HGET', user_limit, uid) or default_limit)
  local cnt = tonumber(redis.call('HGET', user_running, uid) or '0')
  if cnt < limit then
    redis.call('ZREM', pending, id)
    redis.call('ZADD', running, now + lease, id)
    redis.call('HINCRBY', user_running, uid, 1)
    local n = redis.call('HINCRBY', attempts, id, 1)
    return {id, n}
  end
end
return nil
"""

# 실행 중 목록에서 빼고 사용자 카운터를 줄인다. 이미 빠져 있으면(다른 워커가 회수) 0을 돌려준다.
_RELEASE = """
local running, owner, score, attempts, user_running = KEYS[1], KEYS[2], KEYS[3], KEYS[4], KEYS[5]
local id = ARGV[1]
if redis.call('ZREM', running, id) == 0 then return 0 end
local uid = redis.call('HGET', owner, id)
if uid and tonumber(redis.call('HINCRBY', user_running, uid, -1)) <= 0 then redis.call('HDEL', user_running, uid) end
redis.call('HDEL', owner, id)
redis.call('HDEL', score, id)
redis.call('HDEL', attempts, id)
return 1
"""

# lease가 만료된 작업을 pending으로 되돌린다. 시도 횟수를 다 쓴 작업은 dead로 돌려준다.
_REAP = """
local pending, running, owner, score, attempts, user_running = KEYS[1], KEYS[2], KEYS[3], KEYS[4], KEYS[5], KEYS[6]
local now, max_attempts = tonumber(ARGV[1]), tonumber(ARGV[2])
local requeued, dead = {}, {}
for _, id in ipairs(redis.call('ZRANGEBYSCORE', running, '-inf', now)) do
  redis.call('ZREM', running, id)
  local uid = redis.call('HGET', owner, id)
  if uid and tonumber(redis.call('HINCRBY', user_running, uid, -1)) <= 0 then redis.call('HDEL', user_running, uid) end
  if tonumber(redis.call('HGET', attempts, id) or '0') >= max_attempts then
    redis.call('HDEL', owner, id)
    redis.call('HDEL', score, id)
    redis.call('HDEL', attempts, id)
    table.insert(dead, id)
  else
    redis.call('ZADD', pending, redis.call('HGET', score, id) or now, id)
    table.insert(requeued, id)
  end
end
return {requeued, dead}
"""

_CANCEL = """
local pending, owner, score, attempts = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local id = ARGV[1]
if redis.call('ZREM', pending, id) == 0 then return 0 end
redis.call('HDEL', owner, id)
redis.call('HDEL', score, id)
redis.call('HDEL', attempts, id)
return 1
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Claimed:
    job_id: str
    attempt: int


class JobQueue:
    def __init__(self, redis: Redis, name: str = "cpu", default_limit: int = 2, lease_seconds: int = 30, max_attempts: int = 3):
        self.r = redis
        self.default_limit = default_limit
        self.lease_ms = lease_seconds * 1000
        self.max_attempts = max_attempts
        p = f"fsa:{name}"
        self.k_pending, self.k_running = f"{p}:pending", f"{p}:running"
        self.k_owner, self.k_score, self.k_attempts = f"{p}:owner", f"{p}:score", f"{p}:attempts"
        self.k_user_running, self.k_user_limit = f"{p}:user_running", f"{p}:user_limit"
        self._claim = redis.register_script(_CLAIM)
        self._release = redis.register_script(_RELEASE)
        self._reap = redis.register_script(_REAP)
        self._cancel = redis.register_script(_CANCEL)

    async def enqueue(self, job_id: str, user_id: int, priority: int = 5) -> None:
        """priority: 0(낮음)~9(긴급)."""
        score = (9 - priority) * 10**13 + _now_ms()
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.hset(self.k_owner, job_id, user_id)
            pipe.hset(self.k_score, job_id, score)
            pipe.zadd(self.k_pending, {job_id: score})
            await pipe.execute()

    async def claim(self, scan: int = 200) -> Claimed | None:
        keys = [self.k_pending, self.k_running, self.k_owner, self.k_attempts, self.k_user_running, self.k_user_limit]
        res = await self._claim(keys=keys, args=[_now_ms(), self.lease_ms, self.default_limit, scan])
        if not res:
            return None
        job_id = res[0].decode() if isinstance(res[0], bytes) else res[0]
        return Claimed(job_id, int(res[1]))

    async def heartbeat(self, job_id: str) -> bool:
        """lease를 연장한다. False면 이미 회수된 작업이므로 결과를 쓰면 안 된다."""
        changed = await self.r.zadd(self.k_running, {job_id: _now_ms() + self.lease_ms}, xx=True, ch=True)
        return bool(changed) or await self.r.zscore(self.k_running, job_id) is not None

    async def complete(self, job_id: str) -> bool:
        keys = [self.k_running, self.k_owner, self.k_score, self.k_attempts, self.k_user_running]
        return bool(await self._release(keys=keys, args=[job_id]))

    async def reap(self) -> tuple[list[str], list[str]]:
        keys = [self.k_pending, self.k_running, self.k_owner, self.k_score, self.k_attempts, self.k_user_running]
        requeued, dead = await self._reap(keys=keys, args=[_now_ms(), self.max_attempts])
        dec = lambda xs: [x.decode() if isinstance(x, bytes) else x for x in xs]  # noqa: E731
        return dec(requeued), dec(dead)

    async def cancel(self, job_id: str) -> bool:
        return bool(await self._cancel(keys=[self.k_pending, self.k_owner, self.k_score, self.k_attempts], args=[job_id]))

    async def set_user_limit(self, user_id: int, limit: int | None) -> None:
        if limit is None:
            await self.r.hdel(self.k_user_limit, user_id)
        else:
            await self.r.hset(self.k_user_limit, user_id, limit)

    async def position(self, job_id: str) -> int | None:
        """대기열 순번 (0부터). 대기 중이 아니면 None."""
        return await self.r.zrank(self.k_pending, job_id)

    async def stats(self) -> dict:
        running = await self.r.hgetall(self.k_user_running)
        return {
            "pending": await self.r.zcard(self.k_pending),
            "running": await self.r.zcard(self.k_running),
            "running_by_user": {k.decode() if isinstance(k, bytes) else k: int(v) for k, v in running.items()},
        }
