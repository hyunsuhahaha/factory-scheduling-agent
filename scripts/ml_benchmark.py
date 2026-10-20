"""소요 시간 예측 모델이 실제 현장 결과를 얼마나 바꾸는지 측정한다.

같은 문제를 (A) 계획 속도로, (B) 학습한 예측 소요 시간으로 최적화한 뒤,
두 계획의 설비 배정·순서를 그대로 '진짜' 공장(plant 시뮬레이터)에서 실행해 실제 납기 지연을 비교한다.

    python scripts/ml_benchmark.py --seeds 10 --orders 20 --machines 4
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory_agent.ml.model import train  # noqa: E402
from factory_agent.ml.plant import execute, generate_history  # noqa: E402
from factory_agent.solver.cpsat import apply_duration_model, solve  # noqa: E402
from factory_agent.solver.generator import generate  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--orders", type=int, default=20)
    ap.add_argument("--machines", type=int, default=4)
    ap.add_argument("--history", type=int, default=2000)
    ap.add_argument("--time-limit", type=float, default=10)
    ap.add_argument("--load", type=float, default=0.75, help="계획 속도 기준 부하율")
    args = ap.parse_args()

    rows = []
    for seed in range(args.seeds):
        problem = generate(args.orders, args.machines, seed=100 + seed, time_limit_s=args.time_limit, load=args.load)
        records = [r.as_dict() for r in generate_history(problem, n=args.history, seed=seed)]
        model = train(records, epochs=300)

        plan_a = solve(problem, threads=8)
        plan_b = solve(apply_duration_model(problem, 1, model["params"]), threads=8)
        real_a = execute(problem, [a.model_dump(mode="json") for a in plan_a.assignments], seed=seed)
        real_b = execute(problem, [a.model_dump(mode="json") for a in plan_b.assignments], seed=seed)
        rows.append({
            "mape_planned": model["metrics"]["mape_planned_pct"], "mape_model": model["metrics"]["mape_model_pct"],
            "promised_a": len(plan_a.metrics.late_orders), "real_a": len(real_a["late_orders"]), "wt_a": real_a["weighted_tardiness"],
            "promised_b": len(plan_b.metrics.late_orders), "real_b": len(real_b["late_orders"]), "wt_b": real_b["weighted_tardiness"],
            "gap_a": real_a["makespan_minutes"] - plan_a.metrics.makespan_minutes,
            "gap_b": real_b["makespan_minutes"] - plan_b.metrics.makespan_minutes,
        })
        r = rows[-1]
        print(f"seed {seed}: late promised/real  A {r['promised_a']}/{r['real_a']}  B {r['promised_b']}/{r['real_b']}  "
              f"weighted tardiness A {r['wt_a']} B {r['wt_b']}", flush=True)

    mean = lambda k: statistics.mean(r[k] for r in rows)  # noqa: E731
    wt_a, wt_b = sum(r["wt_a"] for r in rows), sum(r["wt_b"] for r in rows)
    print()
    print("| | 계획 속도 (A) | 예측 소요 시간 (B) |")
    print("|---|---|---|")
    print(f"| 작업 시간 예측 오차 (MAPE) | {mean('mape_planned'):.1f}% | {mean('mape_model'):.1f}% |")
    print(f"| 계획상 지연 주문 | {mean('promised_a'):.1f} | {mean('promised_b'):.1f} |")
    print(f"| 실제 실행 시 지연 주문 | {mean('real_a'):.1f} | {mean('real_b'):.1f} |")
    print(f"| 실제 가중 납기 지연 | {wt_a / len(rows):.0f} | {wt_b / len(rows):.0f} ({-(wt_a - wt_b) / wt_a * 100:+.0f}%) |")
    print(f"| 계획 대비 실제 완료 지연 (분) | {mean('gap_a'):.0f} | {mean('gap_b'):.0f} |")


if __name__ == "__main__":
    main()
