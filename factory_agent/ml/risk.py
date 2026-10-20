"""계획의 납기 준수 확률 (몬테카를로).

예측 모델은 작업마다 log(실제/계획) 배율의 평균과 잔차 폭(log_sigma)을 준다. 계획의 설비 배정·순서를 고정한 채
작업 소요 시간을 그 분포에서 뽑아 여러 번 실행해 보고, 주문별로 납기 안에 끝난 비율을 센다.
앞 작업이 밀리면 뒤 작업도 밀리고, 밀려서 야간에 시작하게 되면 야간 배율이 붙는 것까지 재생에 들어간다.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

import numpy as np

from ..solver.schema import OrderRisk, ScheduleProblem
from .model import predict_log_ratio
from .plant import replay


def on_time_risk(problem: ScheduleProblem, assignments: list[dict], params: dict, n: int = 300, seed: int = 0,
                 overtime_used: list[dict] | None = None) -> dict:
    products = {p.id: p for p in problem.products}
    orders = {o.id: o for o in problem.orders}
    # 주문마다 교체 직후·야간 4가지 경우의 log 배율 평균을 한 번에 예측해 둔다.
    keys, rows = [], []
    for a in assignments:
        order = orders[a["order_id"]]
        product = products[order.product_id]
        planned = order.quantity / product.rates[a["machine_id"]] * 60
        for changed in (False, True):
            for night in (False, True):
                keys.append((a["order_id"], changed, night))
                rows.append({"machine_id": a["machine_id"], "product_id": product.id, "family": product.family,
                             "quantity": order.quantity, "planned_minutes": planned,
                             "after_setup": float(changed), "night": float(night)})
    mu = dict(zip(keys, predict_log_ratio(params, rows).tolist()))
    sigma = params.get("log_sigma", 0.0)
    rng = random.Random(seed)

    ends: dict[str, list[datetime]] = {o: [] for o in orders}
    late_counts = []
    for _ in range(n):
        def minutes(a, _machine, _family, planned, changed, night):
            return planned * math.exp(mu[a["order_id"], changed, night] + rng.gauss(0, sigma))

        run = replay(problem, assignments, minutes, overtime_used)
        late = 0
        for order_id, end in run.items():
            ends[order_id].append(end)
            late += end > orders[order_id].due
        late_counts.append(late)

    out = []
    for order_id, samples in ends.items():
        if not samples:
            continue
        offsets = np.array([(s - problem.horizon_start).total_seconds() for s in samples])
        due = orders[order_id].due
        out.append(OrderRisk(
            order_id=order_id,
            on_time_probability=round(float(np.mean([s <= due for s in samples])), 3),
            p50_end=_at(problem, np.percentile(offsets, 50)),
            p90_end=_at(problem, np.percentile(offsets, 90)),
        ))
    out.sort(key=lambda r: (r.on_time_probability, r.order_id))
    return {"orders": out, "expected_late_orders": round(float(np.mean(late_counts)), 2)}


def _at(problem: ScheduleProblem, seconds: float) -> datetime:
    return problem.horizon_start + timedelta(seconds=round(float(seconds)))
