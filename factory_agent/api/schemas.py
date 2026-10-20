from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

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


class FeedbackIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    tags: list[str] = Field(default_factory=list, max_length=10, description="예: late_order, too_many_setups, infeasible_in_practice")
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackOut(FeedbackIn):
    id: int
    job_id: str
    created_at: datetime
    updated_at: datetime


class UserLimitIn(BaseModel):
    max_running: int | None = Field(ge=0, le=50)
