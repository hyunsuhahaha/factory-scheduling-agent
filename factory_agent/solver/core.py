"""시간 변환, 처리 시간, 지표 계산처럼 CP-SAT 모델과 기준 휴리스틱이 함께 쓰는 부분."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .schema import Assignment, ScheduleMetrics, ScheduleProblem


@dataclass(frozen=True)
class Task:
    index: int
    order_id: str
    product_id: str
    family: str
    release: int
    due: int
    weight: int
    durations: dict[str, int]  # machine_id -> minutes


class Clock:
    """datetime <-> 계획 시작 기준 분(int) 변환."""

    def __init__(self, origin: datetime):
        self.origin = origin

    def to_min(self, dt: datetime) -> int:
        return math.floor((dt - self.origin).total_seconds() / 60)

    def to_dt(self, minute: int) -> datetime:
        return self.origin + timedelta(minutes=minute)


def build_tasks(problem: ScheduleProblem, clock: Clock) -> list[Task]:
    products = {p.id: p for p in problem.products}
    tasks = []
    for i, order in enumerate(problem.orders):
        product = products[order.product_id]
        durations = {
            machine_id: max(1, math.ceil(order.quantity / rate * 60)) for machine_id, rate in product.rates.items()
        }
        override = (problem.duration_minutes or {}).get(order.id, {})
        durations |= {m: max(1, int(v)) for m, v in override.items() if m in durations}
        release = 0 if order.release is None else max(0, clock.to_min(order.release))
        tasks.append(
            Task(
                index=i,
                order_id=order.id,
                product_id=product.id,
                family=product.family,
                release=release,
                due=clock.to_min(order.due),
                weight=order.weight,
                durations=durations,
            )
        )
    return tasks


def downtime_minutes(problem: ScheduleProblem, clock: Clock) -> dict[str, list[tuple[int, int]]]:
    """설비별 정지 구간 (분). 계획 시작 이전 부분은 잘라낸다."""
    result: dict[str, list[tuple[int, int]]] = {}
    for machine in problem.machines:
        windows = []
        for w in machine.downtime:
            start, end = max(0, clock.to_min(w.start)), clock.to_min(w.end)
            if end > start:
                windows.append((start, end))
        result[machine.id] = sorted(windows)
    return result


@dataclass
class Placement:
    task: Task
    machine_id: str
    start: int
    end: int
    setup: int
    frozen: bool = False


def to_assignments(placements: list[Placement], clock: Clock) -> list[Assignment]:
    rows = []
    for p in sorted(placements, key=lambda p: (p.machine_id, p.start)):
        rows.append(
            Assignment(
                order_id=p.task.order_id,
                machine_id=p.machine_id,
                product_id=p.task.product_id,
                family=p.task.family,
                start=clock.to_dt(p.start),
                end=clock.to_dt(p.end),
                setup_minutes=p.setup,
                due=clock.to_dt(p.task.due),
                tardiness_minutes=max(0, p.end - p.task.due),
                frozen=p.frozen,
            )
        )
    return rows


def compute_metrics(placements: list[Placement], machine_ids: list[str]) -> ScheduleMetrics:
    makespan = max((p.end for p in placements), default=0)
    late = sorted(p.task.order_id for p in placements if p.end > p.task.due)
    busy = {m: 0 for m in machine_ids}
    for p in placements:
        busy[p.machine_id] += (p.end - p.start) + p.setup
    return ScheduleMetrics(
        makespan_minutes=makespan,
        total_tardiness_minutes=sum(max(0, p.end - p.task.due) for p in placements),
        weighted_tardiness=sum(p.task.weight * max(0, p.end - p.task.due) for p in placements),
        late_orders=late,
        total_setup_minutes=sum(p.setup for p in placements),
        utilization={m: round(busy[m] / makespan, 3) if makespan else 0.0 for m in machine_ids},
    )


def check_feasible(problem: ScheduleProblem, placements: list[Placement], clock: Clock) -> list[str]:
    """해가 제약을 모두 지키는지 독립적으로 검사한다. 위반 사항 목록을 돌려준다 (비어 있으면 정상)."""
    errors = []
    machines = {m.id: m for m in problem.machines}
    down = downtime_minutes(problem, clock)
    if len({p.task.order_id for p in placements}) != len(problem.orders):
        errors.append("not every order is scheduled exactly once")
    by_machine: dict[str, list[Placement]] = {m: [] for m in machines}
    for p in placements:
        if p.machine_id not in p.task.durations:
            errors.append(f"{p.task.order_id}: machine {p.machine_id} not eligible")
            continue
        if p.end - p.start != p.task.durations[p.machine_id]:
            errors.append(f"{p.task.order_id}: wrong duration")
        if p.start < p.task.release and not p.frozen:
            errors.append(f"{p.task.order_id}: starts before release")
        for ds, de in down[p.machine_id]:
            if p.start < de and ds < p.end:
                errors.append(f"{p.task.order_id}: overlaps downtime on {p.machine_id}")
        by_machine[p.machine_id].append(p)
    for machine_id, seq in by_machine.items():
        seq.sort(key=lambda p: p.start)
        prev_family = machines[machine_id].initial_family
        prev_end = 0
        for k, p in enumerate(seq):
            if k == 0 and p.frozen:  # 이미 착수한 작업은 세팅이 끝난 상태로 본다
                prev_family, prev_end = p.task.family, p.end
                continue
            need = problem.setup_minutes(prev_family, p.task.family)
            if p.start < prev_end + need:
                errors.append(f"{p.task.order_id}: setup/overlap violated on {machine_id}")
            prev_family, prev_end = p.task.family, p.end
    return errors
