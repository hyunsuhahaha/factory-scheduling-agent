from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

from ..solver.schema import ScheduleMetrics, ScheduleProblem


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=255, pattern=r"^[^@\s]+@[^@\s]+$")
    password: str = Field(min_length=8, max_length=128)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(BaseModel):
    id: int
    email: str
    role: str
    max_running: int | None


class JobCreate(BaseModel):
    problem: ScheduleProblem
    priority: int = Field(default=5, ge=0, le=9, description="0(낮음)~9(긴급)")
    note: str | None = Field(default=None, max_length=500)


class Improvement(BaseModel):
    """기준 휴리스틱(납기순 배정) 대비 개선 폭."""

    weighted_tardiness_reduction_pct: float | None
    late_orders_before: int
    late_orders_after: int
    makespan_delta_minutes: int
    setup_delta_minutes: int


class JobSummary(BaseModel):
    solver_status: str
    message: str = ""
    metrics: ScheduleMetrics | None = None
    baseline_metrics: ScheduleMetrics | None = None
    improvement: Improvement | None = None
    released_frozen: list[str] = []
    solve_seconds: float = 0
    duration_source: str = "planned_rate"


class JobOut(BaseModel):
    id: str
    kind: str
    status: str
    priority: int
    attempt: int
    parent_job_id: str | None
    note: str | None
    error: str | None
    queue_position: int | None = Field(default=None, description="대기 중일 때 앞에 있는 작업 수")
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    summary: JobSummary | None = None
    training: dict | None = Field(default=None, description="학습 작업 결과: model_id, 평가 지표")


class FeedbackIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    tags: list[str] = Field(default_factory=list, max_length=10, description="예: late_order, too_many_setups, infeasible_in_practice")
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackOut(FeedbackIn):
    id: int
    job_id: str
    created_at: datetime
    updated_at: datetime


class ProductionRecordIn(BaseModel):
    machine_id: str = Field(min_length=1, max_length=64)
    product_id: str = Field(min_length=1, max_length=64)
    family: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0)
    planned_rate: float = Field(gt=0, description="계획(정격) 시간당 생산량")
    after_setup: bool = Field(description="원료 교체 직후 첫 작업 여부")
    started_at: datetime
    actual_minutes: float = Field(gt=0)

    @field_validator("started_at")
    @classmethod
    def _naive(cls, v: datetime) -> datetime:
        return v.astimezone(timezone.utc).replace(tzinfo=None) if v.tzinfo else v


class HistoryIn(BaseModel):
    records: list[ProductionRecordIn] = Field(min_length=1, max_length=10_000)


class HistoryStats(BaseModel):
    records: int
    actual_over_planned_by_machine: dict[str, float] = Field(description="설비별 실제/계획 소요 시간 평균 배율")


class TrainRequest(BaseModel):
    epochs: int = Field(default=300, ge=10, le=5000)
    test_fraction: float = Field(default=0.2, gt=0, lt=0.5)
    note: str | None = Field(default=None, max_length=500)


class DurationModelOut(BaseModel):
    id: int
    job_id: str
    metrics: dict
    created_at: datetime


class UserLimitIn(BaseModel):
    max_running: int | None = Field(ge=0, le=50)
