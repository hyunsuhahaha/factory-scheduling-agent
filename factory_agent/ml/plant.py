"""실제 공장의 '진짜' 소요 시간 모델 (시뮬레이터).

MES에서 계획 속도(정격 시간당 생산량)로 잡은 시간과 실제 시간은 다르다.
- 설비 노후도: 같은 제품이라도 오래된 설비는 느리다
- 원료 계열: PP·HDPE는 압출 조건 잡는 데 더 걸린다
- 소량 오버헤드: 주문마다 시작·마무리 고정 시간
- 교체 직후 안정화: 원료를 바꾼 직후 첫 작업은 불량·조정으로 더 걸린다
- 야간 조: 인원이 적어 조금 느리다
이 모듈은 그 차이를 만들어 내는 기준(정답) 모델이다. 학습 데이터 생성과 '실제 실행' 평가에만 쓰고,
최적화·예측 모델은 이 모듈을 보지 않는다.
"""
from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..solver.schema import ScheduleProblem

MACHINE_EFFICIENCY = {"EXT-1": 0.95, "EXT-2": 1.18, "EXT-3": 1.02, "EXT-4": 1.30, "EXT-5": 1.08}
FAMILY_FACTOR = {"LDPE": 1.00, "LLDPE": 1.04, "HDPE": 1.10, "PP": 1.16}
LOT_OVERHEAD_MIN = 20.0
AFTER_SETUP_MIN = 30.0
NIGHT_FACTOR = 1.08
NOISE_SIGMA = 0.05


def is_night(dt: datetime) -> bool:
    return dt.hour >= 20 or dt.hour < 8


def true_minutes(machine_id: str, family: str, planned_minutes: float, after_setup: bool, night: bool,
                 rng: random.Random | None = None) -> float:
    minutes = planned_minutes * MACHINE_EFFICIENCY.get(machine_id, 1.0) * FAMILY_FACTOR.get(family, 1.0)
    if night:
        minutes *= NIGHT_FACTOR
    minutes += LOT_OVERHEAD_MIN + (AFTER_SETUP_MIN if after_setup else 0.0)
    if rng is not None:
        minutes *= math.exp(rng.gauss(0, NOISE_SIGMA))
    return minutes


@dataclass
class Record:
    machine_id: str
    product_id: str
    family: str
    quantity: int
    planned_rate: float
    after_setup: bool
    started_at: datetime
    actual_minutes: float

    @property
    def planned_minutes(self) -> float:
        return self.quantity / self.planned_rate * 60

    def as_dict(self) -> dict:
        return {
            "machine_id": self.machine_id, "product_id": self.product_id, "family": self.family,
            "quantity": self.quantity, "planned_rate": self.planned_rate, "after_setup": self.after_setup,
            "started_at": self.started_at.isoformat(), "actual_minutes": round(self.actual_minutes, 1),
        }


def generate_history(problem: ScheduleProblem, n: int = 2000, seed: int = 0) -> list[Record]:
    """문제에 나온 설비·제품 구성으로 과거 생산 실적을 만든다 (MES 작업 실적 테이블에 해당)."""
    rng = random.Random(seed)
    start = problem.horizon_start - timedelta(days=180)
    records = []
    for _ in range(n):
        product = rng.choice(problem.products)
        machine_id = rng.choice(list(product.rates))
        quantity = rng.randrange(500, 9000, 100)
        planned = quantity / product.rates[machine_id] * 60
        started = start + timedelta(minutes=rng.randrange(0, 180 * 24 * 60))
        after_setup = rng.random() < 0.35
        actual = true_minutes(machine_id, product.family, planned, after_setup, is_night(started), rng)
        records.append(Record(machine_id, product.id, product.family, quantity, product.rates[machine_id], after_setup, started, actual))
    return records


def replay(problem: ScheduleProblem, assignments: list[dict], minutes_fn: Callable[[dict, str, str, float, bool, bool], float],
           overtime_used: list[dict] | None = None) -> dict:
    """계획의 설비 배정·순서를 그대로 현장에서 실행한다. minutes_fn이 작업마다 실제 소요 분을 정한다.

    각 설비는 계획 순서대로, 앞 작업이 끝나고 교체 시간이 지나면 바로 다음 작업을 시작한다
    (계획 시작 시각보다 늦어질 수는 있어도 일찍 시작하지는 않는다). 시작하려는 시각이 정지·근무 외 시간이면
    다시 열릴 때까지 기다린다. 이미 시작한 작업은 근무가 끝나도 마저 돌린다.
    minutes_fn(assignment, machine_id, family, planned_minutes, after_setup, night) -> minutes
    """
    from ..solver.core import Clock, after_setup, machine_time

    clock = Clock(problem.horizon_start)
    machines = {m.id: m for m in problem.machines}
    orders = {o.id: o for o in problem.orders}
    products = {p.id: p for p in problem.products}
    until = max([clock.to_min(_dt(a["end"])) for a in assignments] + [0]) * 3 + 7 * 1440
    times = machine_time(problem, clock, until)
    opened = [(u["machine_id"], clock.to_min(_dt(u["start"])), clock.to_min(_dt(u["end"]))) for u in overtime_used or []]
    blocked = {
        m: t.fixed + [(s, e) for _, s, e in t.optional if not any(om == m and os <= s and e <= oe for om, os, oe in opened)]
        for m, t in times.items()
    }
    by_machine: dict[str, list[dict]] = {}
    for a in assignments:
        by_machine.setdefault(a["machine_id"], []).append(a)
    ends: dict[str, datetime] = {}
    for machine_id, seq in by_machine.items():
        seq.sort(key=lambda a: _dt(a["start"]))
        prev_family, prev_end = machines[machine_id].initial_family, problem.horizon_start
        for a in seq:
            order = orders[a["order_id"]]
            product = products[order.product_id]
            setup = problem.setup_minutes(prev_family, product.family)
            start = clock.to_min(max(_dt(a["start"]), prev_end + timedelta(minutes=setup)))
            for ds, de in sorted(blocked[machine_id]):
                if ds <= start < de:
                    start = de
            start_dt = clock.to_dt(start)
            planned = order.quantity / product.rates[machine_id] * 60
            minutes = minutes_fn(a, machine_id, product.family, planned, after_setup(prev_family, product.family),
                                 problem.is_night(start_dt))
            end = start_dt + timedelta(minutes=minutes)
            ends[order.id] = end
            prev_family, prev_end = product.family, end
    return ends


def summarize(problem: ScheduleProblem, ends: dict[str, datetime]) -> dict:
    orders = {o.id: o for o in problem.orders}
    tardiness, weighted, late = 0.0, 0.0, []
    for order_id, end in ends.items():
        late_min = max(0.0, (end - orders[order_id].due).total_seconds() / 60)
        tardiness += late_min
        weighted += orders[order_id].weight * late_min
        if late_min > 0:
            late.append(order_id)
    makespan = max(ends.values(), default=problem.horizon_start)
    return {
        "total_tardiness_minutes": round(tardiness),
        "weighted_tardiness": round(weighted),
        "late_orders": sorted(late),
        "makespan_minutes": round((makespan - problem.horizon_start).total_seconds() / 60),
    }


def execute(problem: ScheduleProblem, assignments: list[dict], seed: int = 0, overtime_used: list[dict] | None = None) -> dict:
    """계획을 '진짜' 공장에서 실행했을 때의 실제 결과 (true_minutes + 잡음)."""
    rng = random.Random(seed)

    def minutes(_a, machine_id, family, planned, changed, night):
        return true_minutes(machine_id, family, planned, changed, night, rng)

    return summarize(problem, replay(problem, assignments, minutes, overtime_used))


def _dt(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(v)
