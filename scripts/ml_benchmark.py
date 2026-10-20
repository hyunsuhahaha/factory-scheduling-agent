"""소요 시간 예측 모델이 실제 현장 결과를 얼마나 바꾸는지 측정한다.

같은 문제를 네 가지 소요 시간으로 최적화한 뒤, 계획의 설비 배정·순서를 그대로 '진짜' 공장(plant 시뮬레이터)에서
여러 번 실행해 실제 납기 지연을 비교한다.

- A 계획 속도 (정격)
- B 예측 모델, 교체 직후·야간 여부를 0.5로 근사한 한 가지 값 (이전 방식)
- C 예측 모델, 순서·시작 시각에 따라 solver가 고르는 값 (P50)
- D C와 같되 P90 소요 시간으로 계획 (보수적)

C·D는 몬테카를로로 주문별 납기 준수 확률을 내고, 그 확률이 실제 실행 결과와 얼마나 맞는지(Brier 점수)도 잰다.

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
from factory_agent.ml.risk import on_time_risk  # noqa: E402
from factory_agent.solver.cpsat import apply_duration_model, solve  # noqa: E402
from factory_agent.solver.generator import generate  # noqa: E402

PLANS = ("A", "B", "C", "D")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--orders", type=int, default=20)
    ap.add_argument("--machines", type=int, default=4)
    ap.add_argument("--history", type=int, default=2000)
    ap.add_argument("--time-limit", type=float, default=10)
    ap.add_argument("--runs", type=int, default=20, help="계획마다 실제 실행을 몇 번 돌릴지 (잡음 평균)")
    ap.add_argument("--load", type=float, default=0.75, help="계획 속도 기준 부하율")
    args = ap.parse_args()

    rows = []
    for seed in range(args.seeds):
        problem = generate(args.orders, args.machines, seed=100 + seed, time_limit_s=args.time_limit, load=args.load)
        records = [r.as_dict() for r in generate_history(problem, n=args.history, seed=seed)]
        model = train(records, epochs=300)
        params = model["params"]
        inputs = {
            "A": problem,
            "B": apply_duration_model(problem, 1, params, sequence_aware=False),
            "C": apply_duration_model(problem, 1, params),
            "D": apply_duration_model(problem.model_copy(update={"duration_quantile": 0.9}), 1, params),
        }
        row = {"mape_planned": model["metrics"]["mape_planned_pct"], "mape_model": model["metrics"]["mape_model_pct"],
               "p90_coverage": model["metrics"]["p90_coverage_pct"]}
        for name, p in inputs.items():
            plan = solve(p, threads=8)
            assignments = [a.model_dump(mode="json") for a in plan.assignments]
            real = [execute(problem, assignments, seed=1000 * seed + k) for k in range(args.runs)]
            row[name] = {
                "promised": len(plan.metrics.late_orders),
                "real": statistics.mean(len(r["late_orders"]) for r in real),
                "wt": statistics.mean(r["weighted_tardiness"] for r in real),
                "gap": statistics.mean(r["makespan_minutes"] for r in real) - plan.metrics.makespan_minutes,
                "solve": plan.solve_seconds,
            }
            if name in ("C", "D"):
                risk = on_time_risk(p, assignments, params, n=300)
                late_rate = {o.id: statistics.mean(o.id in r["late_orders"] for r in real) for o in problem.orders}
                row[name]["expected_late"] = risk["expected_late_orders"]
                row[name]["brier"] = statistics.mean(
                    (r.on_time_probability - (1 - late_rate[r.order_id])) ** 2 for r in risk["orders"]
                )
        rows.append(row)
        print(f"seed {seed}: real late " + "  ".join(f"{n} {row[n]['real']:.1f}" for n in PLANS)
              + "  weighted tardiness " + "  ".join(f"{n} {row[n]['wt']:.0f}" for n in PLANS), flush=True)

    def mean(f):
        return statistics.mean(f(r) for r in rows)

    def col(key, fmt):
        return " | ".join(format(mean(lambda r, n=n: r[n][key]), fmt) for n in PLANS)

    wt_a = mean(lambda r: r["A"]["wt"])
    wt = []
    for n in PLANS:
        v = mean(lambda r, n=n: r[n]["wt"])
        wt.append(f"{v:.0f}" if n == "A" else f"{v:.0f} ({(v - wt_a) / wt_a * 100:+.0f}%)")
    print()
    print(f"작업 시간 예측 오차 (MAPE, 미학습 실적): 계획 속도 {mean(lambda r: r['mape_planned']):.1f}%"
          f" -> 모델 {mean(lambda r: r['mape_model']):.1f}%, P90 커버리지 {mean(lambda r: r['p90_coverage']):.1f}%")
    print()
    print("| | A 계획 속도 | B 예측 (0.5 근사) | C 예측 (순서 반영, P50) | D 순서 반영, P90 |")
    print("|---|---|---|---|---|")
    print(f"| 계획상 지연 주문 | {col('promised', '.1f')} |")
    print(f"| 실제 실행 시 지연 주문 | {col('real', '.1f')} |")
    print(f"| 실제 가중 납기 지연 | {' | '.join(wt)} |")
    print(f"| 계획 대비 실제 완료 지연 (분) | {col('gap', '.0f')} |")
    print(f"| 평균 solve (s) | {col('solve', '.1f')} |")
    print(f"| 몬테카를로 예상 지연 주문 | - | - | {mean(lambda r: r['C']['expected_late']):.1f} | {mean(lambda r: r['D']['expected_late']):.1f} |")
    print(f"| 납기 준수 확률 Brier 점수 (낮을수록 정확) | - | - | {mean(lambda r: r['C']['brier']):.3f} | {mean(lambda r: r['D']['brier']):.3f} |")


if __name__ == "__main__":
    main()
