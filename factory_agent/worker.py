"""작업 워커.

큐(자원 클래스)별로 워커를 띄운다.
  cpu  일정 최적화 (CP-SAT). 슬롯 수만큼 동시 실행.
  gpu  소요 시간 예측 모델 학습 (PyTorch). 장치(--devices)마다 슬롯 1개, 작업마다 장치 하나를 빌려 쓴다.

- 큐에서 작업을 claim하고 무거운 계산은 프로세스 풀에서 돌린다 (이벤트 루프를 막지 않게).
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

from sqlalchemy import select, update

from .config import Settings, get_settings
from .db import Database, DurationModel, Job, ProductionRecord, utcnow
from . import metrics
from .queue import Claimed, JobQueue
from .solver.cpsat import solve_payload

KIND_QUEUE = {"schedule": "cpu", "train_duration_model": "gpu"}
MAX_TRAIN_RECORDS = 200_000

log = logging.getLogger("fsa.worker")


class Worker:
    def __init__(self, db: Database, queue: JobQueue, settings: Settings, worker_id: str | None = None,
                 executor: Executor | None = None, devices: list[str] | None = None):
        """devices: gpu 큐 워커가 나눠 쓸 장치 (예: ["cuda:0", "cuda:1"]). 주면 슬롯 수 = 장치 수."""
        self.db = db
        self.queue = queue
        self.settings = settings
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self._executor = executor
        self._owns_executor = executor is None
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self.devices = devices
        self.slots = len(devices) if devices else settings.worker_slots
        self._free_devices: asyncio.Queue[str] = asyncio.Queue()
        for d in devices or []:
            self._free_devices.put_nowait(d)

    @property
    def executor(self) -> Executor:
        if self._executor is None:
            self._executor = ProcessPoolExecutor(max_workers=self.slots)
        return self._executor

    def stop(self) -> None:
        self._stop.set()

    async def reap(self) -> None:
        requeued, dead = await self.queue.reap()
        metrics.LEASES_REAPED.labels(self.queue.name, "requeued").inc(len(requeued))
        metrics.LEASES_REAPED.labels(self.queue.name, "dead").inc(len(dead))
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
                .returning(Job.kind, Job.payload, Job.user_id, Job.created_at)
            )
            row = res.first()
            await s.commit()
        if row is None:  # 취소됐거나 이미 끝난 작업
            await self.queue.complete(job_id)
            return
        started_at = utcnow()
        metrics.JOB_WAIT_SECONDS.labels(row.kind).observe((started_at - row.created_at).total_seconds())

        log.info("job %s attempt %d started", job_id, attempt)
        heartbeat = asyncio.create_task(self._heartbeat(job_id))
        retry_later = False
        model_row: DurationModel | None = None
        try:
            if row.kind == "schedule":
                result = await self._run_schedule(row.payload, row.user_id)
            elif row.kind == "train_duration_model":
                result, model_row = await self._run_training(job_id, row.payload, row.user_id)
            else:
                raise ValueError(f"unknown job kind {row.kind}")
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
            if model_row is not None and values["status"] == "succeeded":  # 모델 저장과 작업 완료를 한 트랜잭션으로
                s.add(model_row)
                await s.flush()
                values["result"] = {"model_id": model_row.id, "metrics": model_row.metrics}
            res = await s.execute(
                update(Job)
                .where(Job.id == job_id, Job.attempt == attempt, Job.status == "running")
                .values(finished_at=utcnow(), **values)
            )
            if res.rowcount == 0:
                await s.rollback()
            else:
                await s.commit()
        if res.rowcount == 0:
            log.warning("job %s attempt %d result discarded (cancelled or reassigned)", job_id, attempt)
        else:
            log.info("job %s attempt %d %s", job_id, attempt, values["status"])
            metrics.JOBS_FINISHED.labels(row.kind, values["status"]).inc()
            metrics.JOB_RUN_SECONDS.labels(row.kind, values["status"]).observe((utcnow() - started_at).total_seconds())
            if row.kind == "schedule" and values["status"] == "succeeded":
                metrics.observe_schedule_result(values["result"])

    async def _run_schedule(self, payload: dict, user_id: int) -> dict:
        model = None
        model_id = payload.get("duration_model_id")
        if model_id is not None:
            async with self.db.session() as s:
                row = await s.get(DurationModel, model_id)
            if row is None or row.user_id != user_id:
                raise ValueError(f"duration model {model_id} not found")
            model = (row.id, row.params)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, solve_payload, payload, self.settings.solver_threads, model)

    async def _run_training(self, job_id: str, payload: dict, user_id: int) -> tuple[dict, DurationModel]:
        from .ml.model import train_payload

        async with self.db.session() as s:
            rows = (await s.scalars(
                select(ProductionRecord).where(ProductionRecord.user_id == user_id)
                .order_by(ProductionRecord.started_at.desc()).limit(MAX_TRAIN_RECORDS)
            )).all()
        records = [{
            "machine_id": r.machine_id, "product_id": r.product_id, "family": r.family, "quantity": r.quantity,
            "planned_rate": r.planned_rate, "after_setup": r.after_setup, "started_at": r.started_at.isoformat(),
            "actual_minutes": r.actual_minutes,
        } for r in rows]
        device = await self._free_devices.get() if self.devices else "cpu"
        try:
            loop = asyncio.get_running_loop()
            out = await loop.run_in_executor(
                self.executor, train_payload, records, device, payload.get("epochs", 300), payload.get("test_fraction", 0.2)
            )
        finally:
            if self.devices:
                self._free_devices.put_nowait(device)
        return {}, DurationModel(user_id=user_id, job_id=job_id, metrics=out["metrics"], params=out["params"])

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
        slots = asyncio.Semaphore(self.slots)
        reap_every = max(1.0, self.settings.lease_seconds / 2)
        last_reap = 0.0
        loop = asyncio.get_running_loop()
        log.info("worker %s started (queue=%s slots=%d devices=%s)", self.worker_id, self.queue.name, self.slots, self.devices)
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


def build_queue(redis, settings: Settings, name: str = "cpu") -> JobQueue:
    return JobQueue(
        redis,
        name=name,
        default_limit=settings.max_running_per_user,
        lease_seconds=settings.lease_seconds,
        max_attempts=settings.max_attempts,
    )


def _detect_devices() -> list[str]:
    try:
        import torch

        if torch.cuda.is_available():
            return [f"cuda:{i}" for i in range(torch.cuda.device_count())]
    except ImportError:
        pass
    return ["cpu"]


async def _amain(args) -> None:
    from redis.asyncio import Redis

    settings = get_settings()
    db = Database(settings.database_url)
    await db.create_all()
    if settings.metrics_port:
        from prometheus_client import start_http_server

        start_http_server(settings.metrics_port)
    redis = Redis.from_url(settings.redis_url)
    devices = None
    if args.queue == "gpu":
        devices = args.devices.split(",") if args.devices else _detect_devices()
    worker = Worker(db, build_queue(redis, settings, args.queue), settings, devices=devices)
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
    parser.add_argument("--queue", choices=sorted(set(KIND_QUEUE.values())), default="cpu")
    parser.add_argument("--devices", help="gpu 큐 장치 목록 (예: cuda:0,cuda:1). 생략하면 자동 감지, 없으면 cpu 1개")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
