from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError

from factory_agent.solver.baseline import schedule_edd
from factory_agent.solver.core import Clock, Placement, build_tasks, check_feasible
from factory_agent.solver.cpsat import solve
from factory_agent.solver.generator import generate
from factory_agent.solver.schema import ScheduleProblem, WorkCalendar

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


def variants(base, after_setup=None, night=None, both=None):
    after_setup = after_setup or base
    night = night or base
    return {"base": base, "after_setup": after_setup, "night": night, "after_setup_night": both or max(after_setup, night)}


def test_sequence_dependent_duration_follows_the_chosen_order():
    # 제품군을 바꾼 직후 작업은 길어진다. solver가 고른 순서마다 그에 맞는 길이가 들어가야 한다.
    seq = {o: {"M1": variants(60, after_setup=150)} for o in ("O1", "O3")} | {"O2": {"M2": variants(60, after_setup=150)}}
    problem = small_problem(sequence_durations=seq, night_hours=(0, 0))
    result = solve(problem, threads=2)
    assert check_feasible(problem, placements_of(problem, result), Clock(problem.horizon_start)) == []
    o1 = next(a for a in result.assignments if a.order_id == "O1")
    if o1.machine_id == "M1":  # M1은 제품군 A로 시작하므로 A 작업은 교체 없음 -> 기본 길이
        assert o1.end - o1.start == timedelta(minutes=60)


def test_solver_waits_for_day_shift_when_night_run_is_much_slower():
    start = datetime(2026, 11, 2, 18, 0)
    problem = ScheduleProblem.model_validate({
        "horizon_start": start.isoformat(),
        "machines": [{"id": "M1", "initial_family": "A"}],
        "products": [{"id": "PA", "family": "A", "rates": {"M1": 60}}],
        "orders": [{"id": "O1", "product_id": "PA", "quantity": 60, "due": (start + timedelta(days=3)).isoformat(),
                    "release": (start + timedelta(hours=2, minutes=30)).isoformat()}],
        "sequence_durations": {"O1": {"M1": variants(60, night=2000)}},
        "time_limit_s": 5,
    })
    a = solve(problem, threads=2).assignments[0]
    assert a.start == datetime(2026, 11, 3, 8, 0) and a.end - a.start == timedelta(minutes=60)


WEEKDAY_DAY_SHIFT = {"shifts": [{"weekdays": [0, 1, 2, 3, 4], "start": "08:00", "end": "20:00"}]}


def test_work_calendar_blocks_off_shift_and_holidays():
    calendar = WEEKDAY_DAY_SHIFT | {"holidays": ["2026-11-03"]}
    problem = generate(n_orders=8, n_machines=2, seed=5, time_limit_s=5)
    orders = [o.model_copy(update={"quantity": min(o.quantity, 2000)}) for o in problem.orders]  # 한 조(12시간) 안에 끝나는 크기
    problem = problem.model_copy(update={"orders": orders, "calendar": WorkCalendar.model_validate(calendar)})
    result = solve(problem, threads=2)
    assert result.status in ("OPTIMAL", "FEASIBLE")
    assert check_feasible(problem, placements_of(problem, result), Clock(problem.horizon_start)) == []
    for a in result.assignments:
        assert a.start.weekday() < 5 and a.start.date() != date(2026, 11, 3)
        assert 8 <= a.start.hour and (a.end.date() == a.start.date() and (a.end.hour, a.end.minute) <= (20, 0))


def test_order_longer_than_any_shift_is_reported():
    problem = small_problem(calendar={"shifts": [{"start": "08:00", "end": "09:30"}]})  # O1은 어디서든 2시간 이상
    result = solve(problem, threads=2)
    assert result.status == "INFEASIBLE" and "O1" in result.message


def overtime_problem(cost_per_hour):
    friday = datetime(2026, 11, 6, 8, 0)
    return ScheduleProblem.model_validate({
        "horizon_start": friday.isoformat(),
        "machines": [{"id": "M1", "initial_family": "A"}],
        "products": [{"id": "PA", "family": "A", "rates": {"M1": 60}}],
        "orders": [
            {"id": "O1", "product_id": "PA", "quantity": 600, "due": (friday + timedelta(hours=10)).isoformat()},
            {"id": "O2", "product_id": "PA", "quantity": 300, "due": (friday + timedelta(days=1, hours=9)).isoformat(), "weight": 3},
        ],
        "calendar": WEEKDAY_DAY_SHIFT | {"overtime": [{"start": "2026-11-07T08:00", "end": "2026-11-07T17:00"}]},
        "weights": {"overtime_per_hour": cost_per_hour},
        "time_limit_s": 5,
    })


def test_overtime_is_opened_only_when_it_pays_off():
    # O2는 금요일 근무 안에 못 끝난다. 토요일 특근을 열면 토요일 오전에 끝나고, 안 열면 월요일까지 밀린다.
    cheap = solve(overtime_problem(100), threads=2)
    assert [u.machine_id for u in cheap.overtime_used] == ["M1"] and cheap.metrics.overtime_hours == 9
    o2 = next(a for a in cheap.assignments if a.order_id == "O2")
    assert o2.start.weekday() == 5
    assert check_feasible(overtime_problem(100), placements_of(overtime_problem(100), cheap), Clock(datetime(2026, 11, 6, 8)),
                          {(0, "M1")}) == []

    pricey = solve(overtime_problem(10_000_000), threads=2)
    assert pricey.overtime_used == [] and pricey.metrics.overtime_hours == 0
    assert any(a.start.weekday() == 0 for a in pricey.assignments)  # 한 주문은 월요일로 밀린다
    assert pricey.metrics.weighted_tardiness > cheap.metrics.weighted_tardiness


def test_reschedule_warm_starts_from_previous_plan():
    from factory_agent.reschedule import ReschedulePatch, apply_patch

    problem = generate(n_orders=12, n_machines=3, seed=6, time_limit_s=3)
    first = solve(problem, threads=2)
    assert first.hint_source == "baseline"
    patch = ReschedulePatch(
        horizon_start=problem.horizon_start + timedelta(hours=2),
        freeze_until=problem.horizon_start + timedelta(hours=2),
        machine_downtime=[{"machine_id": "EXT-1", "start": problem.horizon_start + timedelta(hours=2),
                           "end": problem.horizon_start + timedelta(hours=10)}],
    )
    new = apply_patch(problem, first, patch)
    assert new.hint and not ({h.order_id for h in new.hint} & {f.order_id for f in new.frozen})
    second = solve(new, threads=2)
    assert second.hint_source == "previous" and second.status in ("OPTIMAL", "FEASIBLE")
    assert check_feasible(new, placements_of(new, second), Clock(new.horizon_start)) == []
    assert apply_patch(problem, first, patch.model_copy(update={"warm_start": False})).hint == []


def test_reschedule_can_offer_overtime():
    from factory_agent.reschedule import ReschedulePatch, apply_patch

    problem = overtime_problem(100).model_copy(update={"calendar": WorkCalendar.model_validate(WEEKDAY_DAY_SHIFT)})
    first = solve(problem, threads=2)
    assert first.overtime_used == []
    patch = ReschedulePatch(add_overtime=[{"start": "2026-11-07T08:00", "end": "2026-11-07T17:00"}])
    second = solve(apply_patch(problem, first, patch), threads=2)
    assert [u.machine_id for u in second.overtime_used] == ["M1"]
    assert second.metrics.weighted_tardiness < first.metrics.weighted_tardiness

    with pytest.raises(ValueError, match="calendar"):
        apply_patch(small_problem(), None, patch)
