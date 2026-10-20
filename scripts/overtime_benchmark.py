"""근무 캘린더와 잔업 trade-off.

평일 2교대(06:00~22:00) 공장에 주문을 몰아 넣고, 평일 2시간 연장(22:00~24:00)과 토요일 주간 특근을 후보로 둔다.
잔업 1시간 비용(weights.overtime_per_hour)을 바꿔 가며 solver가 잔업을 얼마나 열고 지연이 얼마나 줄어드는지 본다.
agent가 "특근을 넣을까요?"에 답할 때 쓰는 곡선이다.

    python scripts/overtime_benchmark.py --seeds 5
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory_agent.solver.cpsat import solve  # noqa: E402
from factory_agent.solver.generator import generate  # noqa: E402
from factory_agent.solver.schema import WorkCalendar  # noqa: E402

MONDAY = datetime(2026, 11, 2, 6, 0)
CALENDAR = {
    "shifts": [
        {"weekdays": [0, 1, 2, 3, 4], "start": "06:00", "end": "14:00"},
        {"weekdays": [0, 1, 2, 3, 4], "start": "14:00", "end": "22:00"},
    ],
    "overtime": [{"start": f"2026-11-{d:02d}T22:00", "end": f"2026-11-{d + 1:02d}T00:00"} for d in range(2, 7)]
    + [{"start": "2026-11-07T06:00", "end": "2026-11-07T18:00"}],
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--orders", type=int, default=25)
    ap.add_argument("--machines", type=int, default=4)
    ap.add_argument("--time-limit", type=float, default=10)
    ap.add_argument("--costs", type=int, nargs="+", default=[1000, 3000, 6000, 15000, 30000, 10**7])
    args = ap.parse_args()

    out = {c: [] for c in args.costs}
    for seed in range(args.seeds):
        base = generate(args.orders, args.machines, seed=300 + seed, horizon_start=MONDAY, load=1.0, time_limit_s=args.time_limit)
        # 24시간 가동 기준으로 만든 납기를 2교대에 맞게 조금 늘리고(평일만으로는 빠듯하게), 한 조 안에 끝나는 크기로 자른다.
        orders = [
            o.model_copy(update={"quantity": min(o.quantity, 4000), "due": MONDAY + (o.due - MONDAY) * 1.25})
            for o in base.orders
        ]
        for cost in args.costs:
            weights = base.weights.model_copy(update={"overtime_per_hour": cost})
            p = base.model_copy(update={"orders": orders, "calendar": WorkCalendar.model_validate(CALENDAR), "weights": weights})
            r = solve(p, threads=8)
            out[cost].append({"ot": r.metrics.overtime_hours, "wt": r.metrics.weighted_tardiness, "late": len(r.metrics.late_orders)})
        print(f"seed {seed} done", flush=True)

    print()
    print("| 잔업 1시간 비용 | 연 잔업 (설비·시간) | 지연 주문 | 가중 납기 지연 |")
    print("|---|---|---|---|")
    for cost in args.costs:
        def m(k, cost=cost):
            return statistics.mean(r[k] for r in out[cost])

        label = "잔업 금지" if cost >= 10**7 else f"{cost:,}"
        print(f"| {label} | {m('ot'):.1f} | {m('late'):.1f} | {m('wt'):.0f} |")


if __name__ == "__main__":
    main()
