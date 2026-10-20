"""CP-SAT 기반 생산 일정 최적화.

모델 (flexible single-stage scheduling with sequence-dependent setups):
- 주문마다 시작 시각 1개, 가능한 설비마다 선택 여부(bool)와 optional interval 1개
- 주문은 가능한 설비 중 정확히 하나에 배정
- 설비별 NoOverlap: 배정된 작업 + 정지 구간
- 설비별 Circuit: 같은 설비에서 연속한 두 작업 사이에 제품군 교체 시간 확보
- 고정(frozen) 작업은 설비·시작 시각을 그대로 둔다. 정지 구간과 겹치면 고정을 풀고 다시 배치한다.
- 목적: tardiness 가중치 * Σ(주문 가중치 * 납기 지연분) + makespan 가중치 * 전체 완료 시각
"""
from __future__ import annotations

import time

from ortools.sat.python import cp_model

from .baseline import schedule_edd
from .core import Clock, Placement, build_tasks, check_feasible, compute_metrics, downtime_minutes, to_assignments
from .schema import ScheduleProblem, ScheduleResult

_STATUS = {
    cp_model.OPTIMAL: "OPTIMAL",
    cp_model.FEASIBLE: "FEASIBLE",
    cp_model.INFEASIBLE: "INFEASIBLE",
    cp_model.MODEL_INVALID: "MODEL_INVALID",
    cp_model.UNKNOWN: "UNKNOWN",
}


def _overlaps(start: int, end: int, windows: list[tuple[int, int]]) -> bool:
    return any(start < de and ds < end for ds, de in windows)


def solve(problem: ScheduleProblem, threads: int = 8) -> ScheduleResult:
    started = time.perf_counter()
    clock = Clock(problem.horizon_start)
    tasks = build_tasks(problem, clock)
    down = downtime_minutes(problem, clock)
    machines = {m.id: m for m in problem.machines}
    machine_ids = list(machines)
    by_order = {t.order_id: t for t in tasks}

    # 고정 작업 검증: 설비에서 생산 불가하거나 정지 구간과 겹치면 고정을 푼다.
    frozen: dict[int, tuple[str, int]] = {}
    released: list[str] = []
    for f in problem.frozen:
        task = by_order[f.order_id]
        start = clock.to_min(f.start)
        duration = task.durations.get(f.machine_id)
        if duration is None or _overlaps(start, start + duration, down[f.machine_id]):
            released.append(f.order_id)
            continue
        frozen[task.index] = (f.machine_id, start)

    families = {t.family for t in tasks} | {m.initial_family for m in problem.machines if m.initial_family}
    max_setup = max((problem.setup_minutes(a, b) for a in families for b in families), default=0)
    lower = min([0] + [s for _, s in frozen.values()])
    horizon = (
        max([0] + [t.release for t in tasks] + [s for _, s in frozen.values()] + [de for w in down.values() for _, de in w])
        + sum(max(t.durations.values()) for t in tasks)
        + len(tasks) * max_setup
    )

    model = cp_model.CpModel()
    start_v, end_v = {}, {}
    present: dict[tuple[int, str], cp_model.IntVar] = {}
    intervals: dict[str, list] = {m: [] for m in machine_ids}
    on_machine: dict[str, list[int]] = {m: [] for m in machine_ids}

    for t in tasks:
        lb = lower if t.index in frozen else t.release
        start_v[t.index] = model.new_int_var(lb, horizon, f"s_{t.index}")
        end_v[t.index] = model.new_int_var(lb, horizon, f"e_{t.index}")
        choices = []
        for machine_id, duration in t.durations.items():
            lit = model.new_bool_var(f"x_{t.index}_{machine_id}")
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

    for machine_id in machine_ids:
        blocked = [model.new_fixed_size_interval_var(ds, de - ds, f"down_{machine_id}_{ds}") for ds, de in down[machine_id]]
        model.add_no_overlap(intervals[machine_id] + blocked)

    # 제품군 교체 시간: 설비별 작업 순서를 circuit으로 표현하고, 연속한 두 작업 사이에 간격을 강제한다.
    for machine_id in machine_ids:
        idx = on_machine[machine_id]
        initial = machines[machine_id].initial_family
        fam = {i: tasks[i].family for i in idx}
        needs_setup = any(problem.setup_minutes(fam[a], fam[b]) for a in idx for b in idx) or any(
            problem.setup_minutes(initial, fam[i]) for i in idx
        )
        if not idx or not needs_setup:
            continue
        arcs = []
        node = {i: k + 1 for k, i in enumerate(idx)}
        for i in idx:
            arcs.append((node[i], node[i], present[i, machine_id].Not()))
            first = model.new_bool_var(f"first_{machine_id}_{i}")
            arcs.append((0, node[i], first))
            arcs.append((node[i], 0, model.new_bool_var(f"last_{machine_id}_{i}")))
            setup0 = problem.setup_minutes(initial, fam[i])
            if setup0 and i not in frozen:
                model.add(start_v[i] >= setup0).only_enforce_if(first)
            for j in idx:
                if i == j:
                    continue
                lit = model.new_bool_var(f"arc_{machine_id}_{i}_{j}")
                arcs.append((node[i], node[j], lit))
                model.add(start_v[j] >= end_v[i] + problem.setup_minutes(fam[i], fam[j])).only_enforce_if(lit)
        arcs.append((0, 0, model.new_bool_var(f"empty_{machine_id}")))
        model.add_circuit(arcs)

    tardy = []
    for t in tasks:
        late = model.new_int_var(0, horizon, f"late_{t.index}")
        model.add(late >= end_v[t.index] - t.due)
        tardy.append(t.weight * late)
    makespan = model.new_int_var(0, horizon, "makespan")
    model.add_max_equality(makespan, list(end_v.values()))
    model.minimize(problem.weights.tardiness * sum(tardy) + problem.weights.makespan * makespan)

    # 기준 휴리스틱 해를 힌트로 넣어 초기 해를 빨리 찾게 한다.
    baseline, _ = schedule_edd(problem)
    for p in baseline:
        if p.task.index in frozen:
            continue
        model.add_hint(start_v[p.task.index], p.start)
        for machine_id in p.task.durations:
            model.add_hint(present[p.task.index, machine_id], machine_id == p.machine_id)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = problem.time_limit_s
    solver.parameters.num_workers = max(1, threads)
    solver.parameters.random_seed = 0
    status = solver.solve(model)
    name = _STATUS.get(status, "UNKNOWN")
    baseline_metrics = compute_metrics(baseline, machine_ids)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        message = "no feasible schedule (check frozen assignments and downtime)" if status == cp_model.INFEASIBLE else (
            "time limit reached before a schedule was found"
        )
        return ScheduleResult(
            status=name,
            message=message,
            baseline_metrics=baseline_metrics,
            released_frozen=released,
            solve_seconds=round(time.perf_counter() - started, 3),
        )

    placements = []
    for t in tasks:
        machine_id = next(m for m in t.durations if solver.value(present[t.index, m]))
        s = solver.value(start_v[t.index])
        placements.append(Placement(t, machine_id, s, s + t.durations[machine_id], 0, frozen=t.index in frozen))
    for machine_id in machine_ids:
        seq = sorted((p for p in placements if p.machine_id == machine_id), key=lambda p: p.start)
        prev = machines[machine_id].initial_family
        for k, p in enumerate(seq):
            p.setup = 0 if (k == 0 and p.frozen) else problem.setup_minutes(prev, p.task.family)
            prev = p.task.family

    violations = check_feasible(problem, placements, clock)
    if violations:  # 모델 버그 방어선: 제약을 어긴 해는 내보내지 않는다.
        raise RuntimeError(f"solver produced an invalid schedule: {violations[:5]}")

    return ScheduleResult(
        status=name,
        assignments=to_assignments(placements, clock),
        metrics=compute_metrics(placements, machine_ids),
        baseline_metrics=baseline_metrics,
        released_frozen=released,
        solve_seconds=round(time.perf_counter() - started, 3),
    )


def apply_duration_model(problem: ScheduleProblem, model_id: int, params: dict) -> ScheduleProblem:
    """예측 모델로 주문 × 가능 설비별 소요 시간을 채운다.

    교체 직후·야간 여부는 계획 시점에 모르므로 0.5(기댓값)로 넣는다.
    """
    from ..ml.model import predict_minutes

    products = {p.id: p for p in problem.products}
    keys, rows = [], []
    for order in problem.orders:
        product = products[order.product_id]
        for machine_id, rate in product.rates.items():
            keys.append((order.id, machine_id))
            rows.append({"machine_id": machine_id, "product_id": product.id, "family": product.family, "quantity": order.quantity,
                         "planned_minutes": order.quantity / rate * 60, "after_setup": 0.5, "night": 0.5})
    minutes = predict_minutes(params, rows)
    table: dict[str, dict[str, int]] = {}
    for (order_id, machine_id), m in zip(keys, minutes):
        table.setdefault(order_id, {})[machine_id] = max(1, round(float(m)))
    return problem.model_copy(update={"duration_minutes": table})


def solve_payload(problem: dict, threads: int = 8, model: tuple[int, dict] | None = None) -> dict:
    """프로세스 풀에서 호출하는 진입점. pickle 가능한 dict만 주고받는다. model = (model_id, params)."""
    p = ScheduleProblem.model_validate(problem)
    source = "given" if p.duration_minutes else "planned_rate"
    if model is not None:
        p = apply_duration_model(p, *model)
        source = f"model:{model[0]}"
    result = solve(p, threads)
    result.duration_source = source
    return result.model_dump(mode="json")
