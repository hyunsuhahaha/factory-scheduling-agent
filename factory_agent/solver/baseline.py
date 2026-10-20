"""비교 기준 휴리스틱: 사람이 엑셀로 짜는 방식을 흉내 낸다.

납기가 빠른 주문부터 하나씩 꺼내, 세팅·정지 구간을 고려했을 때 가장 빨리 끝나는 설비에 붙인다.
되돌아가 순서를 바꾸지 않으므로 세팅이 자주 바뀌고, 나중 주문이 앞 주문의 선택에 끌려간다.
"""
from __future__ import annotations

from .core import Clock, Placement, Task, build_tasks, downtime_minutes
from .schema import ScheduleProblem


def _first_fit(earliest: int, duration: int, windows: list[tuple[int, int]]) -> int:
    start = earliest
    for ds, de in windows:
        if start < de and ds < start + duration:
            start = de
    return start


def schedule_edd(problem: ScheduleProblem) -> tuple[list[Placement], Clock]:
    clock = Clock(problem.horizon_start)
    tasks = build_tasks(problem, clock)
    down = downtime_minutes(problem, clock)
    state = {m.id: (0, m.initial_family) for m in problem.machines}  # machine -> (free_at, last_family)
    placements = []
    ordered: list[Task] = sorted(tasks, key=lambda t: (t.due, -t.weight, t.index))
    for task in ordered:
        best = None
        for machine_id, duration in task.durations.items():
            free_at, last_family = state[machine_id]
            setup = problem.setup_minutes(last_family, task.family)
            start = _first_fit(max(task.release, free_at + setup), duration, down[machine_id])
            if best is None or start + duration < best.end:
                best = Placement(task, machine_id, start, start + duration, setup)
        placements.append(best)
        state[best.machine_id] = (best.end, task.family)
    return placements, clock
