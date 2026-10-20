"""시간 변환, 처리 시간, 근무 캘린더, 지표 계산처럼 CP-SAT 모델과 기준 휴리스틱이 함께 쓰는 부분."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .schema import Assignment, DurationVariants, ScheduleMetrics, ScheduleProblem


@dataclass(frozen=True)
class Task:
    index: int
    order_id: str
    product_id: str
    family: str
    release: int
    due: int
    weight: int
    durations: dict[str, int]  # machine_id -> minutes (교체 직후·야간이 아닐 때)
    variants: dict[str, DurationVariants] | None = None  # machine_id -> 교체 직후·야간별 소요 분

    def duration(self, machine_id: str, after_setup: bool, night: bool) -> int:
        v = (self.variants or {}).get(machine_id)
        return self.durations[machine_id] if v is None else v.pick(after_setup, night)

    def max_duration(self, machine_id: str) -> int:
        v = (self.variants or {}).get(machine_id)
        if v is None:
            return self.durations[machine_id]
        return max(v.base, v.after_setup, v.night, v.after_setup_night)


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
        variants = {m: v for m, v in (problem.sequence_durations or {}).get(order.id, {}).items() if m in durations}
        durations |= {m: v.base for m, v in variants.items()}
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
                variants=variants or None,
            )
        )
    return tasks


def after_setup(prev_family: str | None, family: str) -> bool:
    """원료를 바꾼 직후인가. 시작 제품군을 모르는 설비의 첫 작업은 바꾼 것으로 보지 않는다."""
    return prev_family is not None and prev_family != family


def is_night_at(problem: ScheduleProblem, clock: Clock, minute: int) -> bool:
    return problem.is_night(clock.to_dt(minute))


def night_table(problem: ScheduleProblem) -> list[int]:
    """(계획 시작 기준 분 + 시작 시각의 분) // 60 % 24 -> 그 시각에 시작하면 야간인가 (0/1)."""
    origin = problem.horizon_start.replace(minute=0, second=0, microsecond=0)
    return [int(problem.is_night(origin + timedelta(hours=h))) for h in range(24)]


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


def _merge(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in sorted(windows):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _subtract(windows: list[tuple[int, int]], cut: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out = []
    for s, e in windows:
        pieces = [(s, e)]
        for cs, ce in cut:
            pieces = [p for a, b in pieces for p in ((a, min(b, cs)), (max(a, ce), b)) if p[1] > p[0]]
        out += pieces
    return out


def _intersect(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> list[tuple[int, int]]:
    return [(max(s1, s2), min(e1, e2)) for s1, e1 in a for s2, e2 in b if max(s1, s2) < min(e1, e2)]


def working_windows(problem: ScheduleProblem, clock: Clock, until: int) -> list[tuple[int, int]]:
    """캘린더의 근무 구간을 [0, until) 분 단위로 펼친다. 캘린더가 없으면 전부 근무."""
    cal = problem.calendar
    if cal is None:
        return [(0, until)]
    holidays = set(cal.holidays)
    day = problem.horizon_start.date() - timedelta(days=1)  # 전날 시작한 야간 조가 넘어올 수 있다
    last = clock.to_dt(until).date()
    windows = []
    while day <= last:
        if day not in holidays:
            for shift in cal.shifts:
                if day.weekday() not in shift.weekdays:
                    continue
                start, end = datetime.combine(day, shift.start), datetime.combine(day, shift.end)
                if end <= start:
                    end += timedelta(days=1)
                s, e = max(0, clock.to_min(start)), min(until, clock.to_min(end))
                if e > s:
                    windows.append((s, e))
        day += timedelta(days=1)
    return _merge(windows)


def calendar_horizon(problem: ScheduleProblem, clock: Clock, work_minutes: int) -> int:
    """쉬는 시간을 감안해, 근무 시간이 work_minutes 이상 들어가는 계획 끝 시각 (분)."""
    if problem.calendar is None:
        return work_minutes
    until = work_minutes
    for _ in range(30):
        if sum(e - s for s, e in working_windows(problem, clock, until)) >= work_minutes:
            return until
        until = int(until * 1.5) + 1440
    raise ValueError("calendar has too little working time for these orders")


def plan_horizon(problem: ScheduleProblem, clock: Clock, tasks: list[Task]) -> int:
    """모든 작업을 한 줄로 세워도 들어가는 계획 끝 시각 (분). 근무 외 시간만큼 늘린다."""
    down = downtime_minutes(problem, clock)
    families = {t.family for t in tasks} | {m.initial_family for m in problem.machines if m.initial_family}
    max_setup = max((problem.setup_minutes(a, b) for a in families for b in families), default=0)
    fixed = [clock.to_min(f.start) for f in problem.frozen]
    work = (
        max([0] + [t.release for t in tasks] + fixed + [de for w in down.values() for _, de in w])
        + sum(max(t.max_duration(m) for m in t.durations) for t in tasks)
        + len(tasks) * max_setup
    )
    return calendar_horizon(problem, clock, work)


@dataclass
class MachineTime:
    """설비가 막힌 시간. fixed는 늘 막힘, optional은 잔업 구간 k를 열면 쓸 수 있다: (k, start, end)."""

    fixed: list[tuple[int, int]]
    optional: list[tuple[int, int, int]]

    def blocked(self, machine_id: str, overtime_used: set[tuple[int, str]]) -> list[tuple[int, int]]:
        return sorted(self.fixed + [(s, e) for k, s, e in self.optional if (k, machine_id) not in overtime_used])


def machine_time(problem: ScheduleProblem, clock: Clock, until: int) -> dict[str, MachineTime]:
    """설비별 정지 구간 + 근무 외 시간. 잔업으로 열 수 있는 근무 외 시간은 optional로 따로 둔다."""
    down = downtime_minutes(problem, clock)
    cal = problem.calendar
    off = _subtract([(0, until)], working_windows(problem, clock, until))
    result = {}
    for machine in problem.machines:
        mid = machine.id
        if cal is None or (cal.machine_ids is not None and mid not in cal.machine_ids):
            result[mid] = MachineTime(down[mid], [])
            continue
        optional = []
        for k, w in enumerate(cal.overtime):
            if w.machine_ids is not None and mid not in w.machine_ids:
                continue
            span = [(max(0, clock.to_min(w.start)), min(until, clock.to_min(w.end)))]
            optional += [(k, s, e) for s, e in _intersect(off, span)]
        fixed = _subtract(off, [(s, e) for _, s, e in optional])
        result[mid] = MachineTime(_merge(down[mid] + fixed), optional)
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


def compute_metrics(placements: list[Placement], machine_ids: list[str], overtime_minutes: int = 0) -> ScheduleMetrics:
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
        overtime_hours=round(overtime_minutes / 60, 2),
    )


def check_feasible(problem: ScheduleProblem, placements: list[Placement], clock: Clock,
                   overtime_used: set[tuple[int, str]] = frozenset()) -> list[str]:
    """해가 제약을 모두 지키는지 독립적으로 검사한다. 위반 사항 목록을 돌려준다 (비어 있으면 정상).

    overtime_used: 열기로 한 (잔업 구간 번호, 설비). 나머지 근무 외 시간은 막힌 것으로 본다.
    """
    errors = []
    machines = {m.id: m for m in problem.machines}
    times = machine_time(problem, clock, max([p.end for p in placements] + [1]))
    if len({p.task.order_id for p in placements}) != len(problem.orders):
        errors.append("not every order is scheduled exactly once")
    by_machine: dict[str, list[Placement]] = {m: [] for m in machines}
    for p in placements:
        if p.machine_id not in p.task.durations:
            errors.append(f"{p.task.order_id}: machine {p.machine_id} not eligible")
            continue
        if p.start < p.task.release and not p.frozen:
            errors.append(f"{p.task.order_id}: starts before release")
        for ds, de in times[p.machine_id].blocked(p.machine_id, overtime_used):
            if p.start < de and ds < p.end:
                errors.append(f"{p.task.order_id}: overlaps downtime/off-shift on {p.machine_id}")
        by_machine[p.machine_id].append(p)
    for machine_id, seq in by_machine.items():
        seq.sort(key=lambda p: p.start)
        prev_family = machines[machine_id].initial_family
        prev_end = 0
        for k, p in enumerate(seq):
            expected = p.task.duration(machine_id, after_setup(prev_family, p.task.family), is_night_at(problem, clock, p.start))
            if p.end - p.start != expected:
                errors.append(f"{p.task.order_id}: wrong duration ({p.end - p.start} != {expected})")
            if k == 0 and p.frozen:  # 이미 착수한 작업은 세팅이 끝난 상태로 본다
                prev_family, prev_end = p.task.family, p.end
                continue
            need = problem.setup_minutes(prev_family, p.task.family)
            if p.start < prev_end + need:
                errors.append(f"{p.task.order_id}: setup/overlap violated on {machine_id}")
            prev_family, prev_end = p.task.family, p.end
    return errors
