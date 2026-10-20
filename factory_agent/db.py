from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user")
    max_running: Mapped[int | None] = mapped_column(default=None, comment="null이면 기본 할당량")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_user_created", "user_id", "created_at"),
        Index("ix_jobs_status", "status"),
        CheckConstraint("status in ('queued','running','succeeded','failed','cancelled')", name="ck_jobs_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(32), default="schedule", comment="schedule | train_duration_model")
    status: Mapped[str] = mapped_column(String(16), default="queued")
    priority: Mapped[int] = mapped_column(default=5)
    payload: Mapped[dict] = mapped_column(JSON, comment="kind별 입력 (schedule이면 ScheduleProblem)")
    result: Mapped[dict | None] = mapped_column(JSON, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    attempt: Mapped[int] = mapped_column(default=0)
    worker_id: Mapped[str | None] = mapped_column(String(64), default=None)
    parent_job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), default=None)
    note: Mapped[str | None] = mapped_column(String(500), default=None, comment="재계산 사유 등")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("job_id", "user_id", name="uq_feedback_job_user"),
        CheckConstraint("rating between 1 and 5", name="ck_feedback_rating"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    rating: Mapped[int]
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    comment: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ProductionRecord(Base):
    """MES 작업 실적: 계획 속도와 실제 소요 시간. 소요 시간 예측 모델의 학습 데이터."""

    __tablename__ = "production_records"
    __table_args__ = (Index("ix_records_user_started", "user_id", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    machine_id: Mapped[str] = mapped_column(String(64))
    product_id: Mapped[str] = mapped_column(String(64))
    family: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[int]
    planned_rate: Mapped[float]
    after_setup: Mapped[bool]
    started_at: Mapped[datetime]
    actual_minutes: Mapped[float]


class DurationModel(Base):
    __tablename__ = "duration_models"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    metrics: Mapped[dict] = mapped_column(JSON)
    params: Mapped[dict] = mapped_column(JSON, comment="vocab, 정규화 값, 레이어 가중치")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Database:
    def __init__(self, url: str):
        kwargs = {} if url.startswith("sqlite") else {"pool_size": 10, "max_overflow": 10, "pool_pre_ping": True}
        self.engine = create_async_engine(url, **kwargs)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()
