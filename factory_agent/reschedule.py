"""기존 일정에 현장 변경(설비 고장, 긴급 주문, 납기 변경)을 반영한 새 문제를 만든다.

freeze_until 이전에 시작하는 작업은 이미 현장에 지시됐다고 보고 설비·시각을 고정한다.
고정 작업이 새 고장 구간과 겹치면 solver가 고정을 풀고 다시 배치한다 (결과의 released_frozen).
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .solver.schema import FrozenAssignment, Order, Product, ScheduleProblem, ScheduleResult, TimeWindow


class MachineDowntime(TimeWindow):
    machine_id: str


class OrderUpdate(BaseModel):
    id: str
    due: datetime | None = None
    quantity: int | None = Field(default=None, gt=0)
    weight: int | None = Field(default=None, ge=1, le=10)


class ReschedulePatch(BaseModel):
    reason: str | None = Field(default=None, max_length=500, description="재계산 사유 (기록용)")
    horizon_start: datetime | None = Field(default=None, description="새 계획 시작 시각. 보통 현재 시각.")
    freeze_until: datetime | None = Field(default=None, description="이 시각 이전에 시작하는 기존 작업은 고정")
    machine_downtime: list[MachineDowntime] = Field(default_factory=list)
    add_products: list[Product] = Field(default_factory=list)
    add_orders: list[Order] = Field(default_factory=list)
    remove_order_ids: list[str] = Field(default_factory=list)
    order_updates: list[OrderUpdate] = Field(default_factory=list)
    time_limit_s: float | None = Field(default=None, gt=0, le=120)


def apply_patch(problem: ScheduleProblem, result: ScheduleResult | None, patch: ReschedulePatch) -> ScheduleProblem:
    data = problem.model_dump(mode="python")
    removed = set(patch.remove_order_ids)
    known_orders = {o["id"] for o in data["orders"]}
    unknown = (removed | {u.id for u in patch.order_updates}) - known_orders
    if unknown:
        raise ValueError(f"unknown orders: {sorted(unknown)}")

    machines = {m["id"]: m for m in data["machines"]}
    for d in patch.machine_downtime:
        if d.machine_id not in machines:
            raise ValueError(f"unknown machine: {d.machine_id}")
        machines[d.machine_id]["downtime"].append({"start": d.start, "end": d.end})

    data["products"] += [p.model_dump(mode="python") for p in patch.add_products]

    updates = {u.id: u.model_dump(exclude_none=True, exclude={"id"}) for u in patch.order_updates}
    orders = []
    for o in data["orders"]:
        if o["id"] in removed:
            continue
        orders.append({**o, **updates.get(o["id"], {})})
    orders += [o.model_dump(mode="python") for o in patch.add_orders]
    data["orders"] = orders

    frozen = {f["order_id"]: f for f in data["frozen"] if f["order_id"] not in removed}
    if patch.freeze_until is not None and result is not None:
        for a in result.assignments:
            if a.start < patch.freeze_until and a.order_id not in removed:
                frozen[a.order_id] = FrozenAssignment(order_id=a.order_id, machine_id=a.machine_id, start=a.start).model_dump()
    data["frozen"] = list(frozen.values())

    if patch.horizon_start is not None:
        data["horizon_start"] = patch.horizon_start
    if patch.time_limit_s is not None:
        data["time_limit_s"] = patch.time_limit_s
    return ScheduleProblem.model_validate(data)
