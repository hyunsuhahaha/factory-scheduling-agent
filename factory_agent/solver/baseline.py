"""비교 기준 휴리스틱: 사람이 엑셀로 짜는 방식을 흉내 낸다.

납기가 빠른 주문부터 하나씩 꺼내, 세팅·정지 구간·근무 외 시간을 고려했을 때 가장 빨리 끝나는 설비에 붙인다.
되돌아가 순서를 바꾸지 않으므로 세팅이 자주 바뀌고, 나중 주문이 앞 주문의 선택에 끌려간다. 잔업은 쓰지 않는다.
"""
from __future__ import annotations

from collections.abc import Callable

from .core import Clock, Placement, Task, after_setup, build_tasks, is_night_at, machine_time, plan_horizon
from .schema import ScheduleProblem


def first_fit(earliest: int, duration_at: Callable[[int], int], windows: list[tuple[int, int]]) -> int:
    """막힌 구간과 겹치지 않는 가장 이른 시작 시각. 소요 시간이 시작 시각(야간 여부)에 따라 달라질 수 있다."""
    start = earliest
    moved = True
    while moved:
        moved = False
        end = start + duration_at(start)
        for ds, de in windows:
            if start < de and ds < end:
                start, moved = de, True
                break
    return start


class _Line:
    """설비마다 마지막 작업이 끝난 시각과 제품군을 들고, 다음 작업을 가장 이른 가능 시각에 붙인다."""

    def __init__(self, problem: ScheduleProblem):
        self.problem = problem
        self.clock = Clock(problem.horizon_start)
        self.tasks = build_tasks(problem, self.clock)
        times = machine_time(problem, self.clock, plan_horizon(problem, self.clock, self.tasks))
        self.blocked = {m: t.blocked(m, set()) for m, t in times.items()}
        self.state = {m.id: (0, m.initial_family) for m in problem.machines}  # machine -> (free_at, last_family)

    def candidate(self, task: Task, machine_id: str, not_before: int = 0) -> Placement:
        free_at, last_family = self.state[machine_id]
        setup = self.problem.setup_minutes(last_family, task.family)
        changed = after_setup(last_family, task.family)

        def duration_at(s: int) -> int:
            return task.duration(machine_id, changed, is_night_at(self.problem, self.clock, s))

        start = first_fit(max(task.release, free_at + setup, not_before), duration_at, self.blocked[machine_id])
        return Placement(task, machine_id, start, start + duration_at(start), setup)

    def fixed(self, task: Task, machine_id: str, start: int) -> Placement:
        _, last_family = self.state[machine_id]
        end = start + task.duration(machine_id, after_setup(last_family, task.family), is_night_at(self.problem, self.clock, start))
        return Placement(task, machine_id, start, end, 0, frozen=True)

    def place(self, p: Placement) -> Placement:
        self.state[p.machine_id] = (p.end, p.task.family)
        return p

    def earliest_end(self, task: Task) -> Placement:
        return min((self.candidate(task, m) for m in task.durations), key=lambda p: p.end)


def schedule_edd(problem: ScheduleProblem) -> tuple[list[Placement], Clock]:
    line = _Line(problem)
    ordered: list[Task] = sorted(line.tasks, key=lambda t: (t.due, -t.weight, t.index))
    return [line.place(line.earliest_end(task)) for task in ordered], line.clock


def schedule_from_hint(problem: ScheduleProblem) -> list[Placement]:
    """이전 해(problem.hint)의 설비 배정과 순서를 그대로 두고 시각만 새 조건(고장·근무)에 맞게 다시 잡는다.

    재계산 warm start용. 이전 해는 새 고장 구간과 겹쳐 그대로는 실행 불가능한 경우가 많아, 그대로 힌트로 주면
    solver가 버린다. 순서를 지킨 채 밀어낸 해는 늘 실행 가능하고, 이전 계획과 거의 같다.
    작업은 이전 시작 시각보다 당겨지지 않는다. 이전 해에 없던 작업(긴급 주문)은 끝에 가장 빨리 끝나는 설비로 붙인다.
    """
    line = _Line(problem)
    clock = line.clock
    by_order = {t.order_id: t for t in line.tasks}
    seq: dict[str, list[tuple[int, int, Task, bool]]] = {m: [] for m in line.state}
    for f in problem.frozen:
        task, start = by_order[f.order_id], clock.to_min(f.start)
        if f.machine_id not in task.durations:
            continue
        # 새 고장과 겹쳐 solver가 고정을 풀 작업은 일반 작업처럼 밀어낸다.
        overlaps = any(start < de and ds < start + task.max_duration(f.machine_id) for ds, de in line.blocked[f.machine_id])
        seq[f.machine_id].append((start, 0, task, not overlaps))
    for h in problem.hint:
        task = by_order.get(h.order_id)
        if task is not None and h.machine_id in task.durations:
            seq[h.machine_id].append((clock.to_min(h.start), 1, task, False))
    placements, seen = [], set()
    for machine_id, items in seq.items():
        for start, _, task, is_frozen in sorted(items, key=lambda x: (x[0], x[1])):
            if task.order_id in seen:
                continue
            seen.add(task.order_id)
            p = line.fixed(task, machine_id, start) if is_frozen else line.candidate(task, machine_id, not_before=start)
            placements.append(line.place(p))
    for task in sorted((t for t in line.tasks if t.order_id not in seen), key=lambda t: (t.due, -t.weight, t.index)):
        placements.append(line.place(line.earliest_end(task)))
    return placements
