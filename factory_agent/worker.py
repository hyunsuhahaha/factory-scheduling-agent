"""최적화 워커.

- 큐에서 작업을 claim하고 CP-SAT solve를 프로세스 풀에서 돌린다 (CPU 바운드라 이벤트 루프를 막지 않게).
- 실행 중에는 lease를 주기적으로 연장한다. 워커가 죽으면 lease가 만료되고 다른 워커의 reaper가 작업을 되돌린다.
- DB 갱신은 (id, attempt, status='running') 조건부 UPDATE라서, 회수됐거나 취소된 작업의 늦은 결과는 버려진다.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
from concurrent.futures import Executor, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from sqlalchemy import update

from .config import Settings, get_settings
from .db import Database, Job, utcnow
from .queue import Claimed, JobQueue
from .solver.cpsat import solve_payload

log = logging.getLogger("fsa.worker")


class Worker:
    def __init__(self, db: Database, queue: JobQueue, settings: Settings, worker_id: str | None = None, executor: Executor | None = None):
        self.db = db
        self.queue = queue
        self.settings = settings
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self._executor = executor
        self._owns_executor = executor is None
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()

    @property
    def executor(self) -> Executor:
        if self._executor is None:
            self._executor = ProcessPoolExecutor(max_workers=self.settings.worker_slots)
        return self._executor

    def stop(self) -> None:
        self._stop.set()

    async def reap(self) -> None:
        requeued, dead = await self.queue.reap()
        async with self.db.session() as s:
            for job_id in requeued:
                await s.execute(update(Job).where(Job.id == job_id, Job.status == "running").values(status="queued", worker_id=None))
            for job_id in dead:
                await s.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status.in_(("queued", "running")))
                    .values(status="failed", error="worker lost the job too many times", finished_at=utcnow())
                )
            await s.commit()
        if requeued or dead:
            log.warning("reaped expired leases: requeued=%s dead=%s", requeued, dead)

    async def run_once(self) -> bool:
        """작업 하나를 가져와 끝까지 처리한다. 가져올 작업이 없으면 False."""
        claimed = await self.queue.claim()
        if claimed is None:
            return False
        await self._process(claimed)
        return True

    async def _process(self, claimed: Claimed) -> None:
        job_id, attempt = claimed.job_id, claimed.attempt
        async with self.db.session() as s:
            res = await s.execute(
                update(Job)
                .where(Job.id == job_id, Job.status.in_(("queued", "running")))
                .values(status="running", attempt=attempt, worker_id=self.worker_id, started_at=utcnow())
                .returning(Job.problem)
            )
            row = res.first()
            await s.commit()
        if row is None:  # 취소됐거나 이미 끝난 작업
            await self.queue.complete(job_id)
            return

        log.info("job %s attempt %d started", job_id, attempt)
        heartbeat = asyncio.create_task(self._heartbeat(job_id))
        retry_later = False
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(self.executor, solve_payload, row.problem, self.settings.solver_threads)
            values = {"status": "succeeded", "result": result, "error": None}
        except BrokenProcessPool:
            # solve 프로세스가 죽었다 (OOM 등). 풀을 다시 만들고 lease 만료 후 재시도되게 둔다.
            log.exception("process pool broken while running %s", job_id)
            self._reset_executor()
            retry_later = True
            return
        except Exception as exc:  # noqa: BLE001 - 입력 문제 등은 재시도해도 같으므로 실패로 확정
            log.exception("job %s failed", job_id)
            values = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:2000]}
        finally:
            heartbeat.cancel()
            if not retry_later:
                await self.queue.complete(job_id)

        async with self.db.session() as s:
            res = await s.execute(
                update(Job)
                .where(Job.id == job_id, Job.attempt == attempt, Job.status == "running")
                .values(finished_at=utcnow(), **values)
            )
            await s.commit()
        if res.rowcount == 0:
            log.warning("job %s attempt %d result discarded (cancelled or reassigned)", job_id, attempt)
        else:
            log.info("job %s attempt %d %s", job_id, attempt, values["status"])

    async def _heartbeat(self, job_id: str) -> None:
        interval = max(1.0, self.settings.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            if not await self.queue.heartbeat(job_id):
                log.warning("lost lease for %s", job_id)
                return

    def _reset_executor(self) -> None:
        if self._owns_executor and self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    async def run_forever(self) -> None:
        slots = asyncio.Semaphore(self.settings.worker_slots)
        reap_every = max(1.0, self.settings.lease_seconds / 2)
        last_reap = 0.0
        loop = asyncio.get_running_loop()
        log.info("worker %s started (slots=%d)", self.worker_id, self.settings.worker_slots)
        while not self._stop.is_set():
            if loop.time() - last_reap >= reap_every:
                try:
                    await self.reap()
                except Exception:  # noqa: BLE001
                    log.exception("reap failed")
                last_reap = loop.time()
            await slots.acquire()
            try:
                claimed = await self.queue.claim()
            except Exception:  # noqa: BLE001 - Redis 순단 시 잠시 후 재시도
                log.exception("claim failed")
                claimed = None
            if claimed is None:
                slots.release()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.settings.poll_interval)
                except asyncio.TimeoutError:
                    pass
                continue
            task = asyncio.create_task(self._process(claimed))
            self._tasks.add(task)
            task.add_done_callback(lambda t: (self._tasks.discard(t), slots.release()))
        # graceful shutdown: 새 작업은 받지 않고 실행 중인 작업은 끝까지 마친다.
        if self._tasks:
            log.info("waiting for %d running jobs", len(self._tasks))
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._reset_executor()
        log.info("worker %s stopped", self.worker_id)


def build_queue(redis, settings: Settings) -> JobQueue:
    return JobQueue(
        redis,
        default_limit=settings.max_running_per_user,
        lease_seconds=settings.lease_seconds,
        max_attempts=settings.max_attempts,
    )


async def _amain() -> None:
    from redis.asyncio import Redis

    settings = get_settings()
    db = Database(settings.database_url)
    await db.create_all()
    redis = Redis.from_url(settings.redis_url)
    worker = Worker(db, build_queue(redis, settings), settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:  # Windows
            pass
    try:
        await worker.run_forever()
    finally:
        await redis.aclose()
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="factory scheduling worker")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
