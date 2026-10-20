"""스케줄링 문제/결과 스키마.

API, 워커, MCP 서버가 같은 스키마를 공유한다. 시각은 timezone 없는 공장 현지 시각을
기본으로 하고, timezone이 붙어 오면 UTC로 바꾼 뒤 timezone을 떼어 비교한다.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator, model_validator


def _naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


class TimeWindow(BaseModel):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _norm(cls, v: datetime) -> datetime:
        return _naive(v)

    @model_validator(mode="after")
    def _check(self) -> "TimeWindow":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class Machine(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str | None = None
    downtime: list[TimeWindow] = Field(default_factory=list, description="계획 정지·고장 구간")
    initial_family: str | None = Field(
        default=None, description="계획 시작 시점에 설비에 세팅된 제품군. 첫 작업의 세팅 시간 계산에 쓴다."
    )


class Product(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    family: str = Field(min_length=1, max_length=64, description="세팅(금형·원료) 그룹. 같은 그룹끼리는 교체 시간이 없다.")
    rates: dict[str, float] = Field(description="설비 id -> 시간당 생산량. 없는 설비에서는 생산할 수 없다.")

    @field_validator("rates")
    @classmethod
    def _positive(cls, v: dict[str, float]) -> dict[str, float]:
        if not v:
            raise ValueError("product needs at least one eligible machine")
        if any(r <= 0 for r in v.values()):
            raise ValueError("rates must be positive")
        return v


class Order(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    product_id: str
    quantity: int = Field(gt=0)
    due: datetime
    release: datetime | None = Field(default=None, description="이 시각 이전에는 시작할 수 없음 (자재 입고 등)")
    weight: int = Field(default=1, ge=1, le=10, description="납기 지연 가중치. 클수록 중요한 주문.")

    @field_validator("due", "release")
    @classmethod
    def _norm(cls, v: datetime | None) -> datetime | None:
        return None if v is None else _naive(v)


class SetupRule(BaseModel):
    from_family: str
    to_family: str
    minutes: int = Field(ge=0)


class FrozenAssignment(BaseModel):
    """이미 착수했거나 현장에 지시된 작업. 재계산 시 움직이지 않는다."""

    order_id: str
    machine_id: str
    start: datetime

    @field_validator("start")
    @classmethod
    def _norm(cls, v: datetime) -> datetime:
        return _naive(v)


class ObjectiveWeights(BaseModel):
    tardiness: int = Field(default=100, ge=0, description="가중 납기 지연(분)당 비용")
    makespan: int = Field(default=1, ge=0, description="전체 완료 시각(분)당 비용")


class ScheduleProblem(BaseModel):
    horizon_start: datetime = Field(description="계획 시작 시각. 이보다 앞서 시작하는 작업은 없다.")
    machines: list[Machine] = Field(min_length=1, max_length=50)
    products: list[Product] = Field(min_length=1, max_length=500)
    orders: list[Order] = Field(min_length=1, max_length=200)
    default_setup_minutes: int = Field(default=0, ge=0, description="제품군이 바뀔 때 기본 교체 시간")
    setup_rules: list[SetupRule] = Field(default_factory=list, description="제품군 쌍별 교체 시간 (기본값을 덮어씀)")
    frozen: list[FrozenAssignment] = Field(default_factory=list)
    weights: ObjectiveWeights = Field(default_factory=ObjectiveWeights)
    time_limit_s: float = Field(default=10, gt=0, le=120)
    duration_model_id: int | None = Field(
        default=None, description="학습된 소요 시간 예측 모델 id. 주면 계획 속도 대신 예측 소요 시간으로 일정을 짠다."
    )
    duration_minutes: dict[str, dict[str, int]] | None = Field(
        default=None, description="주문 id -> 설비 id -> 소요 분. 있으면 계획 속도 계산을 덮어쓴다 (워커가 예측 모델로 채움)."
    )

    @field_validator("horizon_start")
    @classmethod
    def _norm(cls, v: datetime) -> datetime:
        return _naive(v)

    @model_validator(mode="after")
    def _references(self) -> "ScheduleProblem":
        machine_ids = [m.id for m in self.machines]
        if len(set(machine_ids)) != len(machine_ids):
            raise ValueError("duplicate machine id")
        products = {p.id: p for p in self.products}
        if len(products) != len(self.products):
            raise ValueError("duplicate product id")
        for p in self.products:
            unknown = set(p.rates) - set(machine_ids)
            if unknown:
                raise ValueError(f"product {p.id}: unknown machines {sorted(unknown)}")
        order_ids = [o.id for o in self.orders]
        if len(set(order_ids)) != len(order_ids):
            raise ValueError("duplicate order id")
        for o in self.orders:
            if o.product_id not in products:
                raise ValueError(f"order {o.id}: unknown product {o.product_id}")
        known_orders = set(order_ids)
        for f in self.frozen:
            if f.order_id not in known_orders:
                raise ValueError(f"frozen: unknown order {f.order_id}")
        return self

    def setup_minutes(self, from_family: str | None, to_family: str) -> int:
        if from_family is None or from_family == to_family:
            return 0
        for rule in self.setup_rules:
            if rule.from_family == from_family and rule.to_family == to_family:
                return rule.minutes
        return self.default_setup_minutes


class Assignment(BaseModel):
    order_id: str
    machine_id: str
    product_id: str
    family: str
    start: datetime
    end: datetime
    setup_minutes: int = Field(description="이 작업 직전의 제품군 교체 시간")
    due: datetime
    tardiness_minutes: int
    frozen: bool = False


class ScheduleMetrics(BaseModel):
    makespan_minutes: int
    total_tardiness_minutes: int
    weighted_tardiness: int
    late_orders: list[str]
    total_setup_minutes: int
    utilization: dict[str, float] = Field(description="설비별 가동률 (생산+세팅 시간 / 전체 완료 시각)")


class ScheduleResult(BaseModel):
    status: str = Field(description="OPTIMAL | FEASIBLE | INFEASIBLE | UNKNOWN")
    message: str = ""
    assignments: list[Assignment] = Field(default_factory=list)
    metrics: ScheduleMetrics | None = None
    baseline_metrics: ScheduleMetrics | None = Field(
        default=None, description="사람이 쓰는 규칙(납기순 + 가장 빨리 끝나는 설비)으로 짠 일정의 지표"
    )
    released_frozen: list[str] = Field(
        default_factory=list, description="고장 구간과 겹쳐 고정을 풀고 다시 배치한 주문"
    )
    duration_source: str = Field(default="planned_rate", description="planned_rate | model:<id> | given")
    solve_seconds: float = 0.0
