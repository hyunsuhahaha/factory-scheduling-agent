from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from factory_agent.solver.baseline import schedule_edd
from factory_agent.solver.core import Clock, Placement, build_tasks, check_feasible
from factory_agent.solver.cpsat import solve
from factory_agent.solver.generator import generate
from factory_agent.solver.schema import ScheduleProblem

T0 = datetime(2026, 11, 2, 8, 0)


def placements_of(problem: ScheduleProblem, result) -> list[Placement]:
    clock = Clock(problem.horizon_start)
    tasks = {t.order_id: t for t in build_tasks(problem, clock)}
    return [
        Placement(tasks[a.order_id], a.machine_id, clock.to_min(a.start), clock.to_min(a.end), a.setup_minutes, a.frozen)
        for a in result.assignments
    ]


def small_problem(**overrides) -> ScheduleProblem:
    data = {
        "horizon_start": T0.isoformat(),
        "machines": [{"id": "M1", "initial_family": "A"}, {"id": "M2", "initial_family": "B"}],
        "products": [
            {"id": "PA", "family": "A", "rates": {"M1": 60, "M2": 30}},
            {"id": "PB", "family": "B", "rates": {"M2": 60}},
        ],
        "orders": [
            {"id": "O1", "product_id": "PA", "quantity": 120, "due": (T0 + timedelta(hours=3)).isoformat()},
            {"id": "O2", "product_id": "PB", "quantity": 60, "due": (T0 + timedelta(hours=2)).isoformat()},
            {"id": "O3", "product_id": "PA", "quantity": 60, "due": (T0 + timedelta(hours=8)).isoformat()},
        ],
        "default_setup_minutes": 30,
        "time_limit_s": 5,
    }
    data.update(overrides)
    return ScheduleProblem.model_validate(data)


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_solution_is_feasible_and_not_worse_than_baseline(seed):
    problem = generate(n_orders=15, n_machines=3, seed=seed, time_limit_s=5)
    result = solve(problem, threads=2)
    assert result.status in ("OPTIMAL", "FEASIBLE")
    assert check_feasible(problem, placements_of(problem, result), Clock(problem.horizon_start)) == []
    w = problem.weights
    cost = lambda m: w.tardiness * m.weighted_tardiness + w.makespan * m.makespan_minutes  # noqa: E731
    assert cost(result.metrics) <= cost(result.baseline_metrics)


def test_baseline_is_feasible():
    problem = generate(n_orders=25, n_machines=4, seed=3)
    placements, clock = schedule_edd(problem)
    assert check_feasible(problem, placements, clock) == []


def test_setup_time_between_families_is_respected():
    # M2 starts with family B. Putting A on M2 after B costs 30 minutes of setup.
    problem = small_problem()
    result = solve(problem, threads=2)
    by_machine = {}
    for a in result.assignments:
        by_machine.setdefault(a.machine_id, []).append(a)
    for seq in by_machine.values():
        for prev, cur in zip(seq, seq[1:]):
            if prev.family != cur.family:
                assert (cur.start - prev.end) >= timedelta(minutes=30)
                assert cur.setup_minutes == 30


def test_downtime_is_avoided():
    down = {"start": T0.isoformat(), "end": (T0 + timedelta(hours=4)).isoformat()}
    problem = small_problem(machines=[{"id": "M1", "downtime": [down]}, {"id": "M2"}])
    result = solve(problem, threads=2)
    for a in result.assignments:
        if a.machine_id == "M1":
            assert a.start >= T0 + timedelta(hours=4)


def test_frozen_assignment_is_kept_and_released_on_conflict():
    frozen_start = T0 + timedelta(hours=1)
    problem = small_problem(frozen=[{"order_id": "O3", "machine_id": "M1", "start": frozen_start.isoformat()}])
    result = solve(problem, threads=2)
    o3 = next(a for a in result.assignments if a.order_id == "O3")
    assert (o3.machine_id, o3.start, o3.frozen) == ("M1", frozen_start, True)

    down = {"start": frozen_start.isoformat(), "end": (frozen_start + timedelta(hours=2)).isoformat()}
    problem = small_problem(
        machines=[{"id": "M1", "downtime": [down]}, {"id": "M2"}],
        frozen=[{"order_id": "O3", "machine_id": "M1", "start": frozen_start.isoformat()}],
    )
    result = solve(problem, threads=2)
    assert result.released_frozen == ["O3"]
    o3 = next(a for a in result.assignments if a.order_id == "O3")
    assert not o3.frozen


def test_release_time_is_respected():
    orders = small_problem().model_dump(mode="json")["orders"]
    orders[0]["release"] = (T0 + timedelta(hours=5)).isoformat()
    result = solve(small_problem(orders=orders), threads=2)
    o1 = next(a for a in result.assignments if a.order_id == "O1")
    assert o1.start >= T0 + timedelta(hours=5)


def test_timezone_aware_input_is_normalized():
    problem = small_problem(horizon_start="2026-11-02T08:00:00+00:00")
    assert problem.horizon_start.tzinfo is None


@pytest.mark.parametrize(
    "change, message",
    [
        ({"orders": [{"id": "O1", "product_id": "NOPE", "quantity": 1, "due": T0.isoformat()}]}, "unknown product"),
        ({"products": [{"id": "PA", "family": "A", "rates": {"M9": 10}}]}, "unknown machines"),
        ({"machines": [{"id": "M1"}, {"id": "M1"}]}, "duplicate machine"),
    ],
)
def test_invalid_problem_is_rejected(change, message):
    with pytest.raises(ValidationError, match=message):
        small_problem(**change)
