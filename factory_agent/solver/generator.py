"""테스트·벤치마크용 문제 생성기.

필름 압출 공장을 모델로 한다: 설비마다 생산 가능한 제품과 속도가 다르고,
원료 계열(family)이 바뀌면 퍼지·세팅 시간이 든다. 같은 seed는 같은 문제를 만든다.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from .schema import ScheduleProblem

FAMILIES = ["LDPE", "LLDPE", "HDPE", "PP"]


def generate(
    n_orders: int = 20,
    n_machines: int = 4,
    n_products: int = 8,
    seed: int = 0,
    horizon_start: datetime = datetime(2026, 11, 2, 8, 0),
    load: float = 0.85,
    time_limit_s: float = 10,
) -> ScheduleProblem:
    """load: 전체 작업량 / (설비 수 * 납기 구간). 1에 가까울수록 납기 맞추기가 빡빡하다."""
    rng = random.Random(seed)
    machines = [{"id": f"EXT-{k + 1}", "initial_family": rng.choice(FAMILIES)} for k in range(n_machines)]
    products = []
    for k in range(n_products):
        eligible = rng.sample([m["id"] for m in machines], k=rng.randint(1, max(1, min(3, n_machines))))
        base = rng.uniform(300, 900)
        products.append(
            {
                "id": f"P{k + 1:02d}",
                "family": FAMILIES[k % len(FAMILIES)],
                "rates": {m: round(base * rng.uniform(0.7, 1.3), 1) for m in eligible},
            }
        )

    raw = []
    for k in range(n_orders):
        product = rng.choice(products)
        quantity = rng.randrange(1000, 8000, 100)
        avg_rate = sum(product["rates"].values()) / len(product["rates"])
        raw.append((k, product, quantity, quantity / avg_rate * 60))
    total_work = sum(r[3] for r in raw)
    span = total_work / n_machines / load

    orders = []
    for k, product, quantity, work in raw:
        due = horizon_start + timedelta(minutes=rng.uniform(work + 60, span * 1.1))
        release = horizon_start + timedelta(minutes=rng.choice([0, 0, 0, rng.uniform(0, span * 0.3)]))
        orders.append(
            {
                "id": f"SO-{k + 1:03d}",
                "product_id": product["id"],
                "quantity": quantity,
                "due": due.replace(second=0, microsecond=0).isoformat(),
                "release": release.replace(second=0, microsecond=0).isoformat(),
                "weight": rng.choice([1, 1, 1, 2, 3]),
            }
        )

    return ScheduleProblem.model_validate(
        {
            "horizon_start": horizon_start.isoformat(),
            "machines": machines,
            "products": products,
            "orders": orders,
            "default_setup_minutes": 45,
            "setup_rules": [
                {"from_family": "PP", "to_family": "LDPE", "minutes": 120},
                {"from_family": "LDPE", "to_family": "PP", "minutes": 90},
                {"from_family": "LDPE", "to_family": "LLDPE", "minutes": 20},
                {"from_family": "LLDPE", "to_family": "LDPE", "minutes": 20},
            ],
            "time_limit_s": time_limit_s,
        }
    )
