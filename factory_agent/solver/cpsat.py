"""CP-SAT 기반 생산 일정 최적화.

모델 (flexible single-stage scheduling with sequence-dependent setups):
- 주문마다 시작 시각 1개, 가능한 설비마다 선택 여부(bool)와 optional interval 1개
- 주문은 가능한 설비 중 정확히 하나에 배정
- 설비별 NoOverlap: 배정된 작업 + 정지 구간 + 근무 외 시간 (잔업을 열면 그 구간은 빠진다)
- 설비별 Circuit: 같은 설비에서 연속한 두 작업 사이에 제품군 교체 시간 확보
- 순서 의존 소요 시간: 들어오는 circuit arc가 제품군을 바꾸는 arc면 '교체 직후', 시작 시각의 시(hour)가
  야간 조면 '야간'. 소요 시간 = 두 bool로 고른 예측값 (sequence_durations가 있을 때)
- 고정(frozen) 작업은 설비·시작 시각을 그대로 둔다. 정지 구간과 겹치면 고정을 풀고 다시 배치한다.
- 목적: tardiness 가중치 * Σ(주문 가중치 * 납기 지연분) + makespan 가중치 * 전체 완료 시각 + 잔업 비용
- 탐색 시작점: 재계산이면 이전 해(hint), 아니면 납기순 규칙 해
"""
from __future__ import annotations

import time

from ortools.sat.python import cp_model

from .baseline import schedule_edd, schedule_from_hint
from .core import (
    Clock,
    Placement,
    after_setup,
    build_tasks,
    check_feasible,
    compute_metrics,
    machine_time,
    night_table,
    plan_horizon,
    to_assignments,
)
from .schema import DurationVariants, OvertimeUse, ScheduleProblem, ScheduleResult

_STATUS = {
    cp_model.OPTIMAL: "OPTIMAL",
    cp_model.FEASIBLE: "FEASIBLE",
    cp_model.INFEASIBLE: "INFEASIBLE",
    cp_model.MODEL_INVALID: "MODEL_INVALID",
    cp_model.UNKNOWN: "UNKNOWN",
}


def _overlaps(start: int, end: int, windows: list[tuple[int, int]]) -> bool:
    return any(start < de and ds < end for ds, de in windows)


class _Progress(cp_model.CpSolverSolutionCallback):
    """해를 찾을 때마다 (경과 초, 목적값)을 남긴다. warm start 벤치마크용."""

    def __init__(self, out: list[tuple[float, float]]):
        super().__init__()
        self.out = out

    def on_solution_callback(self) -> None:
        self.out.append((round(self.wall_time, 3), self.objective_value))


def solve(problem: ScheduleProblem, threads: int = 8, progress: list[tuple[float, float]] | None = None) -> ScheduleResult:
    started = time.perf_counter()
    clock = Clock(problem.horizon_start)
    tasks = build_tasks(problem, clock)
    machines = {m.id: m for m in problem.machines}
    machine_ids = list(machines)
    by_order = {t.order_id: t for t in tasks}
    horizon = plan_horizon(problem, clock, tasks)
    times = machine_time(problem, clock, horizon)

    # 고정 작업 검증: 설비에서 생산 불가하거나 정지 구간·근무 외 시간과 겹치면 고정을 푼다.
    # 잔업으로 열 수 있는 구간과 겹치는 고정 작업은 NoOverlap이 그 잔업을 열게 만든다.
    frozen: dict[int, tuple[str, int]] = {}
    released: list[str] = []
    for f in problem.frozen:
        task = by_order[f.order_id]
        start = clock.to_min(f.start)
        if f.machine_id not in task.durations or _overlaps(start, start + task.max_duration(f.machine_id), times[f.machine_id].fixed):
            released.append(f.order_id)
            continue
        frozen[task.index] = (f.machine_id, start)

    too_long = _longer_than_any_shift(tasks, times, horizon)
    if too_long:
        return ScheduleResult(
            status="INFEASIBLE",
            message=f"orders longer than any working window on their machines (split them or add overtime): {too_long}",
            released_frozen=released,
            solve_seconds=round(time.perf_counter() - started, 3),
        )

    lower = min([0] + [s for _, s in frozen.values()])
    model = cp_model.CpModel()
    start_v, end_v, dur_v = {}, {}, {}
    present: dict[tuple[int, str], cp_model.IntVar] = {}
    intervals: dict[str, list] = {m: [] for m in machine_ids}
    on_machine: dict[str, list[int]] = {m: [] for m in machine_ids}
    changed: dict[int, cp_model.IntVar] = {}  # 교체 직후 여부 (variants가 있는 작업만)
    night: dict[int, cp_model.IntVar] = {}
    table = night_table(problem)
    hour_offset = problem.horizon_start.minute

    for t in tasks:
        lb = lower if t.index in frozen else t.release
        start_v[t.index] = model.new_int_var(lb, horizon, f"s_{t.index}")
        end_v[t.index] = model.new_int_var(lb, horizon, f"e_{t.index}")
        if t.variants:
            changed[t.index] = model.new_bool_var(f"chg_{t.index}")
            night[t.index] = model.new_bool_var(f"night_{t.index}")
            hours = model.new_int_var(0, (horizon + hour_offset) // 60, f"h_{t.index}")
            model.add_division_equality(hours, start_v[t.index] + hour_offset, 60)
            hour = model.new_int_var(0, 23, f"hod_{t.index}")
            model.add_modulo_equality(hour, hours, 24)
            model.add_element(hour, table, night[t.index])
            both = model.new_bool_var(f"chg_night_{t.index}")
            model.add_multiplication_equality(both, [changed[t.index], night[t.index]])
            lengths = [d for m in t.durations for a in (False, True) for n in (False, True) for d in [t.duration(m, a, n)]]
            dur_v[t.index] = model.new_int_var(min(lengths), max(lengths), f"d_{t.index}")
        choices = []
        for machine_id, duration in t.durations.items():
            lit = model.new_bool_var(f"x_{t.index}_{machine_id}")
            v = (t.variants or {}).get(machine_id)
            if t.variants:
                if v is None:
                    v = DurationVariants(base=duration, after_setup=duration, night=duration, after_setup_night=duration)
                model.add(
                    dur_v[t.index]
                    == v.base
                    + (v.after_setup - v.base) * changed[t.index]
                    + (v.night - v.base) * night[t.index]
                    + (v.after_setup_night - v.after_setup - v.night + v.base) * both
                ).only_enforce_if(lit)
                interval = model.new_optional_interval_var(
                    start_v[t.index], dur_v[t.index], end_v[t.index], lit, f"i_{t.index}_{machine_id}"
                )
            else:
                interval = model.new_optional_fixed_size_interval_var(start_v[t.index], duration, lit, f"i_{t.index}_{machine_id}")
                model.add(end_v[t.index] == start_v[t.index] + duration).only_enforce_if(lit)
            present[t.index, machine_id] = lit
            intervals[machine_id].append(interval)
            on_machine[machine_id].append(t.index)
            choices.append(lit)
        model.add_exactly_one(choices)
        if t.index in frozen:
            machine_id, start = frozen[t.index]
            model.add(present[t.index, machine_id] == 1)
            model.add(start_v[t.index] == start)

    # 정지·근무 외 시간. 잔업 구간은 (구간, 설비)마다 bool 하나로 열고 닫는다.
    overtime: dict[tuple[int, str], cp_model.IntVar] = {}
    overtime_minutes: dict[tuple[int, str], int] = {}
    for machine_id in machine_ids:
        mt = times[machine_id]
        blocked = [model.new_fixed_size_interval_var(ds, de - ds, f"down_{machine_id}_{ds}") for ds, de in mt.fixed]
        for k, ds, de in mt.optional:
            key = (k, machine_id)
            if key not in overtime:
                overtime[key] = model.new_bool_var(f"ot_{k}_{machine_id}")
                overtime_minutes[key] = 0
            overtime_minutes[key] += de - ds
            blocked.append(model.new_optional_fixed_size_interval_var(ds, de - ds, overtime[key].Not(), f"off_{machine_id}_{ds}"))
        model.add_no_overlap(intervals[machine_id] + blocked)

    # 제품군 교체: 설비별 작업 순서를 circuit으로 표현하고, 연속한 두 작업 사이에 간격을 강제한다.
    # 같은 arc가 '교체 직후' 여부도 정한다.
    arc_lits: dict[str, dict[tuple[int, int], cp_model.IntVar]] = {}
    incoming_change: dict[int, list[cp_model.IntVar]] = {i: [] for i in changed}
    for machine_id in machine_ids:
        idx = on_machine[machine_id]
        initial = machines[machine_id].initial_family
        fam = {i: tasks[i].family for i in idx}
        needs_setup = any(problem.setup_minutes(fam[a], fam[b]) for a in idx for b in idx) or any(
            problem.setup_minutes(initial, fam[i]) for i in idx
        )
        needs_order = any(i in changed for i in idx) and (
            len(set(fam.values())) > 1 or any(after_setup(initial, fam[i]) for i in idx)
        )
        if not idx or not (needs_setup or needs_order):
            continue
        arcs = []
        lits: dict[tuple[int, int], cp_model.IntVar] = {}
        node = {i: k + 1 for k, i in enumerate(idx)}
        for i in idx:
            arcs.append((node[i], node[i], present[i, machine_id].Not()))
            first = model.new_bool_var(f"first_{machine_id}_{i}")
            arcs.append((0, node[i], first))
            lits[-1, i] = first
            last = model.new_bool_var(f"last_{machine_id}_{i}")
            arcs.append((node[i], 0, last))
            lits[i, -1] = last
            setup0 = problem.setup_minutes(initial, fam[i])
            if setup0 and i not in frozen:
                model.add(start_v[i] >= setup0).only_enforce_if(first)
            if i in changed and after_setup(initial, fam[i]):
                incoming_change[i].append(first)
            for j in idx:
                if i == j:
                    continue
                lit = model.new_bool_var(f"arc_{machine_id}_{i}_{j}")
                arcs.append((node[i], node[j], lit))
                lits[i, j] = lit
                model.add(start_v[j] >= end_v[i] + problem.setup_minutes(fam[i], fam[j])).only_enforce_if(lit)
                if j in changed and after_setup(fam[i], fam[j]):
                    incoming_change[j].append(lit)
        empty = model.new_bool_var(f"empty_{machine_id}")
        arcs.append((0, 0, empty))
        lits[-1, -1] = empty
        model.add_circuit(arcs)
        arc_lits[machine_id] = lits
    for i, lits in incoming_change.items():
        model.add(changed[i] == sum(lits))  # 작업은 한 설비에만 있으므로 들어오는 arc는 많아야 하나

    tardy = []
    for t in tasks:
        late = model.new_int_var(0, horizon, f"late_{t.index}")
        model.add(late >= end_v[t.index] - t.due)
        tardy.append(t.weight * late)
    makespan = model.new_int_var(0, horizon, "makespan")
    model.add_max_equality(makespan, list(end_v.values()))
    overtime_cost = sum(round(problem.weights.overtime_per_hour * overtime_minutes[k] / 60) * b for k, b in overtime.items())
    model.minimize(problem.weights.tardiness * sum(tardy) + problem.weights.makespan * makespan + overtime_cost)

    # 탐색 시작점: 이전 해(재계산) 또는 납기순 규칙 해. 설비 배정·시작 시각과 circuit 순서를 함께 넣는다.
    baseline, _ = schedule_edd(problem)
    if problem.hint:
        hint_source = "previous"
        seeds = [(p.task.index, p.machine_id, p.start) for p in schedule_from_hint(problem)]
    else:
        hint_source = "baseline"
        seeds = [(p.task.index, p.machine_id, p.start) for p in baseline]
    _add_hints(model, seeds, frozen, start_v, present, arc_lits, tasks)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = problem.time_limit_s
    solver.parameters.num_workers = max(1, threads)
    solver.parameters.random_seed = 0
    status = solver.solve(model, _Progress(progress) if progress is not None else None)
    name = _STATUS.get(status, "UNKNOWN")
    baseline_metrics = compute_metrics(baseline, machine_ids)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        message = "no feasible schedule (check frozen assignments, downtime and work calendar)" if status == cp_model.INFEASIBLE else (
            "time limit reached before a schedule was found"
        )
        return ScheduleResult(
            status=name,
            message=message,
            baseline_metrics=baseline_metrics,
            released_frozen=released,
            hint_source=hint_source,
            solve_seconds=round(time.perf_counter() - started, 3),
        )

    placements = []
    for t in tasks:
        machine_id = next(m for m in t.durations if solver.value(present[t.index, m]))
        s = solver.value(start_v[t.index])
        placements.append(Placement(t, machine_id, s, solver.value(end_v[t.index]), 0, frozen=t.index in frozen))
    for machine_id in machine_ids:
        seq = sorted((p for p in placements if p.machine_id == machine_id), key=lambda p: p.start)
        prev = machines[machine_id].initial_family
        for k, p in enumerate(seq):
            p.setup = 0 if (k == 0 and p.frozen) else problem.setup_minutes(prev, p.task.family)
            prev = p.task.family

    used = {k for k, b in overtime.items() if solver.value(b)}
    violations = check_feasible(problem, placements, clock, used)
    if violations:  # 모델 버그 방어선: 제약을 어긴 해는 내보내지 않는다.
        raise RuntimeError(f"solver produced an invalid schedule: {violations[:5]}")

    overtime_used = []
    for k, machine_id in sorted(used, key=lambda x: (x[1], x[0])):
        spans = [(s, e) for kk, s, e in times[machine_id].optional if kk == k]
        overtime_used.append(OvertimeUse(machine_id=machine_id, start=clock.to_dt(min(s for s, _ in spans)),
                                         end=clock.to_dt(max(e for _, e in spans))))
    return ScheduleResult(
        status=name,
        assignments=to_assignments(placements, clock),
        metrics=compute_metrics(placements, machine_ids, sum(overtime_minutes[k] for k in used)),
        baseline_metrics=baseline_metrics,
        released_frozen=released,
        overtime_used=overtime_used,
        hint_source=hint_source,
        solve_seconds=round(time.perf_counter() - started, 3),
    )


def _longer_than_any_shift(tasks, times, horizon: int) -> list[str]:
    """작업은 중간에 멈추지 않는다. 가능한 설비 어디에도 들어갈 근무 구간(잔업 포함)이 없는 주문."""
    longest = {}
    for machine_id, mt in times.items():
        free = [(0, horizon)]
        for ds, de in mt.fixed:
            free = [p for a, b in free for p in ((a, min(b, ds)), (max(a, de), b)) if p[1] > p[0]]
        longest[machine_id] = max((b - a for a, b in free), default=0)
    return [t.order_id for t in tasks if all(min(t.duration(m, a, n) for a in (False, True) for n in (False, True)) > longest[m]
                                             for m in t.durations)]


def _add_hints(model, seeds, frozen, start_v, present, arc_lits, tasks) -> None:
    """(작업, 설비, 시작) 목록을 배정·시작 시각·circuit arc 힌트로 바꾼다.

    고정 작업은 변수가 이미 정해져 있으니 배정·시각 힌트는 빼되, 설비 순서에는 넣어야 arc 힌트가 일관된다.
    """
    for i, machine_id, start in seeds:
        if i in frozen:
            continue
        model.add_hint(start_v[i], max(start, 0))
        for m in tasks[i].durations:
            model.add_hint(present[i, m], m == machine_id)
    if len({i for i, _, _ in seeds}) < len(tasks):
        return  # 순서 힌트는 모든 작업의 위치를 알 때만 일관된다
    for machine_id, lits in arc_lits.items():
        seq = [i for i, m, _ in sorted(seeds, key=lambda s: s[2]) if m == machine_id]
        nodes = [-1] + seq + [-1]
        on = set(zip(nodes, nodes[1:])) if seq else {(-1, -1)}
        for key, lit in lits.items():
            model.add_hint(lit, key in on)


def apply_duration_model(problem: ScheduleProblem, model_id: int, params: dict, sequence_aware: bool = True) -> ScheduleProblem:
    """예측 모델로 주문 × 가능 설비별 소요 시간을 채운다.

    sequence_aware=True: 교체 직후·야간 4가지 경우를 모두 예측해 sequence_durations로 넘기고, solver가 순서와
    시작 시각에 맞는 값을 고른다. False: 계획 시점에 모르는 두 값을 0.5(기댓값)로 넣은 한 가지 값 (이전 방식).
    problem.duration_quantile 분위수로 예측한다 (0.5 = 중앙값, 0.9 = 보수적).
    """
    from ..ml.model import predict_minutes

    products = {p.id: p for p in problem.products}
    cases = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)] if sequence_aware else [(0.5, 0.5)]
    keys, rows = [], []
    for order in problem.orders:
        product = products[order.product_id]
        for machine_id, rate in product.rates.items():
            for a, n in cases:
                keys.append((order.id, machine_id))
                rows.append({"machine_id": machine_id, "product_id": product.id, "family": product.family, "quantity": order.quantity,
                             "planned_minutes": order.quantity / rate * 60, "after_setup": a, "night": n})
    minutes = predict_minutes(params, rows, quantile=problem.duration_quantile)
    values: dict[str, dict[str, list[int]]] = {}
    for (order_id, machine_id), m in zip(keys, minutes):
        values.setdefault(order_id, {}).setdefault(machine_id, []).append(max(1, round(float(m))))
    if not sequence_aware:
        table = {o: {m: v[0] for m, v in per.items()} for o, per in values.items()}
        return problem.model_copy(update={"duration_minutes": table, "sequence_durations": None})
    variants = {
        o: {m: DurationVariants(base=v[0], after_setup=v[1], night=v[2], after_setup_night=v[3]) for m, v in per.items()}
        for o, per in values.items()
    }
    return problem.model_copy(update={"sequence_durations": variants, "duration_minutes": None})


def solve_payload(problem: dict, threads: int = 8, model: tuple[int, dict] | None = None) -> dict:
    """프로세스 풀에서 호출하는 진입점. pickle 가능한 dict만 주고받는다. model = (model_id, params)."""
    p = ScheduleProblem.model_validate(problem)
    source = "given" if (p.duration_minutes or p.sequence_durations) else "planned_rate"
    if model is not None:
        p = apply_duration_model(p, *model)
        source = f"model:{model[0]}"
    result = solve(p, threads)
    result.duration_source = source
    if model is not None and result.assignments:
        from ..ml.risk import on_time_risk

        risk = on_time_risk(p, [a.model_dump(mode="json") for a in result.assignments], model[1],
                            overtime_used=[u.model_dump(mode="json") for u in result.overtime_used])
        result.risk, result.expected_late_orders = risk["orders"], risk["expected_late_orders"]
    return result.model_dump(mode="json")

