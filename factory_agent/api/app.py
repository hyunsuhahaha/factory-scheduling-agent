from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db import Database, Feedback, Job, User, utcnow
from ..queue import JobQueue
from ..reschedule import ReschedulePatch, apply_patch
from ..security import create_token, decode_token, hash_password, verify_password
from ..solver.schema import ScheduleProblem, ScheduleResult
from ..worker import Worker, build_queue
from .schemas import (
    Credentials,
    FeedbackIn,
    FeedbackOut,
    Improvement,
    JobCreate,
    JobOut,
    JobSummary,
    TokenOut,
    UserLimitIn,
    UserOut,
)

log = logging.getLogger("fsa.api")


# ---------------------------------------------------------------- dependencies

async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.db.session() as session:
        yield session


def get_queue(request: Request) -> JobQueue:
    return request.app.state.queue


def get_cfg(request: Request) -> Settings:
    return request.app.state.settings


Session = Annotated[AsyncSession, Depends(get_session)]
Queue = Annotated[JobQueue, Depends(get_queue)]
Cfg = Annotated[Settings, Depends(get_cfg)]
_bearer = HTTPBearer(auto_error=False)


async def current_user(
    session: Session, cfg: Cfg, cred: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> User:
    if cred is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = decode_token(cred.credentials, cfg.jwt_secret)
    except Exception:  # noqa: BLE001 - 만료·서명 오류 모두 401
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token", headers={"WWW-Authenticate": "Bearer"})
    user = await session.get(User, int(payload["sub"]))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def admin_user(user: CurrentUser) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    return user


AdminUser = Annotated[User, Depends(admin_user)]


async def owned_job(job_id: str, user: User, session: AsyncSession) -> Job:
    job = await session.get(Job, job_id)
    if job is None or (job.user_id != user.id and user.role != "admin"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")  # 남의 작업은 존재 여부도 숨긴다
    return job


# ---------------------------------------------------------------- helpers

def summarize(job: Job) -> JobSummary | None:
    if not job.result:
        return None
    r = ScheduleResult.model_validate(job.result)
    improvement = None
    if r.metrics and r.baseline_metrics:
        base, opt = r.baseline_metrics, r.metrics
        improvement = Improvement(
            weighted_tardiness_reduction_pct=(
                round((base.weighted_tardiness - opt.weighted_tardiness) / base.weighted_tardiness * 100, 1)
                if base.weighted_tardiness
                else None
            ),
            late_orders_before=len(base.late_orders),
            late_orders_after=len(opt.late_orders),
            makespan_delta_minutes=opt.makespan_minutes - base.makespan_minutes,
            setup_delta_minutes=opt.total_setup_minutes - base.total_setup_minutes,
        )
    return JobSummary(
        solver_status=r.status,
        message=r.message,
        metrics=r.metrics,
        baseline_metrics=r.baseline_metrics,
        improvement=improvement,
        released_frozen=r.released_frozen,
        solve_seconds=r.solve_seconds,
    )


async def to_out(job: Job, queue: JobQueue) -> JobOut:
    position = await queue.position(job.id) if job.status == "queued" else None
    return JobOut(
        id=job.id,
        kind=job.kind,
        status=job.status,
        priority=job.priority,
        attempt=job.attempt,
        parent_job_id=job.parent_job_id,
        note=job.note,
        error=job.error,
        queue_position=position,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        summary=summarize(job),
    )


async def submit(
    session: AsyncSession, queue: JobQueue, cfg: Settings, user: User, problem: ScheduleProblem,
    priority: int, note: str | None, parent_job_id: str | None = None,
) -> Job:
    queued = await session.scalar(select(func.count()).select_from(Job).where(Job.user_id == user.id, Job.status == "queued"))
    if queued >= cfg.max_queued_per_user:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"too many queued jobs (limit {cfg.max_queued_per_user})")
    job = Job(
        user_id=user.id, problem=problem.model_dump(mode="json"), priority=priority, note=note, parent_job_id=parent_job_id
    )
    session.add(job)
    await session.commit()
    # DB 커밋 후 큐에 넣는다. 큐 등록이 실패하면 작업을 실패로 표시해 유령 작업이 남지 않게 한다.
    try:
        await queue.enqueue(job.id, user.id, priority)
    except Exception:
        job.status, job.error, job.finished_at = "failed", "enqueue failed", utcnow()
        await session.commit()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "queue unavailable")
    return job


# ---------------------------------------------------------------- routes

auth = APIRouter(prefix="/auth", tags=["auth"])


@auth.post("/register", response_model=UserOut, status_code=201)
async def register(body: Credentials, session: Session, cfg: Cfg) -> UserOut:
    email = body.email.lower()
    user = User(email=email, password_hash=hash_password(body.password), role="admin" if email in cfg.admin_emails else "user")
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")
    return UserOut(id=user.id, email=user.email, role=user.role, max_running=user.max_running)


@auth.post("/token", response_model=TokenOut)
async def token(body: Credentials, session: Session, cfg: Cfg) -> TokenOut:
    user = await session.scalar(select(User).where(User.email == body.email.lower()))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid email or password")
    return TokenOut(
        access_token=create_token(user.id, user.role, cfg.jwt_secret, cfg.jwt_ttl_minutes), expires_in=cfg.jwt_ttl_minutes * 60
    )


@auth.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut(id=user.id, email=user.email, role=user.role, max_running=user.max_running)


jobs = APIRouter(prefix="/jobs", tags=["jobs"])


@jobs.post("", response_model=JobOut, status_code=202)
async def create_job(body: JobCreate, user: CurrentUser, session: Session, queue: Queue, cfg: Cfg) -> JobOut:
    job = await submit(session, queue, cfg, user, body.problem, body.priority, body.note)
    return await to_out(job, queue)


@jobs.get("", response_model=list[JobOut])
async def list_jobs(
    user: CurrentUser, session: Session, queue: Queue,
    status_: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[JobOut]:
    q = select(Job).where(Job.user_id == user.id).order_by(Job.created_at.desc()).limit(limit)
    if status_:
        q = q.where(Job.status == status_)
    return [await to_out(j, queue) for j in (await session.scalars(q)).all()]


@jobs.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: str, user: CurrentUser, session: Session, queue: Queue) -> JobOut:
    return await to_out(await owned_job(job_id, user, session), queue)


@jobs.get("/{job_id}/result", response_model=ScheduleResult)
async def get_result(job_id: str, user: CurrentUser, session: Session) -> ScheduleResult:
    job = await owned_job(job_id, user, session)
    if job.status != "succeeded":
        raise HTTPException(status.HTTP_409_CONFLICT, f"job is {job.status}")
    return ScheduleResult.model_validate(job.result)


@jobs.get("/{job_id}/problem", response_model=ScheduleProblem)
async def get_problem(job_id: str, user: CurrentUser, session: Session) -> ScheduleProblem:
    return ScheduleProblem.model_validate((await owned_job(job_id, user, session)).problem)


@jobs.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: str, user: CurrentUser, session: Session, queue: Queue) -> JobOut:
    job = await owned_job(job_id, user, session)
    if job.status not in ("queued", "running"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"job is {job.status}")
    # 실행 중이면 워커가 끝난 뒤 조건부 UPDATE에서 결과를 버린다.
    await queue.cancel(job.id)
    job.status, job.finished_at = "cancelled", utcnow()
    await session.commit()
    return await to_out(job, queue)


@jobs.post("/{job_id}/reschedule", response_model=JobOut, status_code=202)
async def reschedule(job_id: str, patch: ReschedulePatch, user: CurrentUser, session: Session, queue: Queue, cfg: Cfg) -> JobOut:
    parent = await owned_job(job_id, user, session)
    if patch.freeze_until is not None and parent.status != "succeeded":
        raise HTTPException(status.HTTP_409_CONFLICT, "freeze_until needs a succeeded parent job")
    result = ScheduleResult.model_validate(parent.result) if parent.result else None
    try:
        problem = apply_patch(ScheduleProblem.model_validate(parent.problem), result, patch)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, str(exc))
    job = await submit(session, queue, cfg, user, problem, parent.priority, patch.reason, parent_job_id=parent.id)
    return await to_out(job, queue)


@jobs.put("/{job_id}/feedback", response_model=FeedbackOut)
async def put_feedback(job_id: str, body: FeedbackIn, user: CurrentUser, session: Session) -> FeedbackOut:
    job = await owned_job(job_id, user, session)
    fb = await session.scalar(select(Feedback).where(Feedback.job_id == job.id, Feedback.user_id == user.id))
    if fb is None:
        fb = Feedback(job_id=job.id, user_id=user.id, rating=body.rating, tags=body.tags, comment=body.comment)
        session.add(fb)
    else:
        fb.rating, fb.tags, fb.comment = body.rating, body.tags, body.comment
    await session.commit()
    await session.refresh(fb)
    return FeedbackOut(id=fb.id, job_id=fb.job_id, rating=fb.rating, tags=fb.tags, comment=fb.comment,
                       created_at=fb.created_at, updated_at=fb.updated_at)


admin = APIRouter(prefix="/admin", tags=["admin"])


@admin.get("/queue")
async def queue_stats(_: AdminUser, session: Session, queue: Queue) -> dict:
    counts = dict((await session.execute(select(Job.status, func.count()).group_by(Job.status))).all())
    return {"queue": await queue.stats(), "jobs_by_status": counts}


@admin.put("/users/{user_id}/limit", response_model=UserOut)
async def set_limit(user_id: int, body: UserLimitIn, _: AdminUser, session: Session, queue: Queue) -> UserOut:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    user.max_running = body.max_running
    await session.commit()
    await queue.set_user_limit(user.id, body.max_running)
    return UserOut(id=user.id, email=user.email, role=user.role, max_running=user.max_running)


@admin.get("/feedback/summary")
async def feedback_summary(_: AdminUser, session: Session) -> dict:
    total, avg = (await session.execute(select(func.count(), func.avg(Feedback.rating)))).one()
    by_rating = dict((await session.execute(select(Feedback.rating, func.count()).group_by(Feedback.rating))).all())
    tags: dict[str, int] = {}
    for (tag_list,) in (await session.execute(select(Feedback.tags))).all():
        for t in tag_list or []:
            tags[t] = tags.get(t, 0) + 1
    low = (
        await session.execute(
            select(Feedback.job_id, Feedback.rating, Feedback.comment)
            .where(Feedback.rating <= 2).order_by(Feedback.updated_at.desc()).limit(20)
        )
    ).all()
    return {
        "count": total,
        "avg_rating": round(float(avg), 2) if avg is not None else None,
        "by_rating": by_rating,
        "top_tags": dict(sorted(tags.items(), key=lambda kv: -kv[1])[:10]),
        "recent_low_ratings": [{"job_id": j, "rating": r, "comment": c} for j, r, c in low],
    }


# ---------------------------------------------------------------- app factory

def create_app(settings: Settings | None = None, redis=None, run_worker: bool | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not settings.embedded and (settings.jwt_secret == "dev-only-change-me" or len(settings.jwt_secret) < 32):
            raise RuntimeError("set FSA_JWT_SECRET to a random string of 32+ chars")
        db = Database(settings.database_url)
        await db.create_all()
        r = redis
        if r is None:
            if settings.embedded:
                from fakeredis import FakeAsyncRedis

                r = FakeAsyncRedis()
            else:
                from redis.asyncio import Redis

                r = Redis.from_url(settings.redis_url)
        queue = build_queue(r, settings)
        app.state.db, app.state.queue, app.state.settings = db, queue, settings
        worker_task = None
        if run_worker if run_worker is not None else settings.embedded:
            worker = Worker(db, queue, settings, worker_id="embedded")
            worker_task = asyncio.create_task(worker.run_forever())
            app.state.worker = worker
        try:
            yield
        finally:
            if worker_task:
                app.state.worker.stop()
                await worker_task
            if redis is None:
                await r.aclose()
            await db.dispose()

    app = FastAPI(
        title="Factory Scheduling Agent API",
        version="0.1.0",
        description="생산 일정 최적화 작업을 비동기로 제출·조회하는 API. MCP 서버가 이 API를 tool로 노출한다.",
        lifespan=lifespan,
    )

    @app.get("/healthz", tags=["ops"])
    async def healthz(request: Request) -> dict:
        await request.app.state.queue.r.ping()
        return {"ok": True}

    app.include_router(auth)
    app.include_router(jobs)
    app.include_router(admin)
    return app
