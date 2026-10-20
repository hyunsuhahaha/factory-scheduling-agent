"""CP-SAT vs 기준 휴리스틱(납기순 배정) 벤치마크.

    python scripts/benchmark.py --seeds 10 --sizes 10x3 20x4 40x5 --time-limit 10

결과를 마크다운 표로 출력한다 (README의 수치가 여기서 나온다).
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory_agent.solver.cpsat import solve  # noqa: E402
from factory_agent.solver.generator import generate  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--sizes", nargs="+", default=["10x3", "20x4", "40x5"])
    ap.add_argument("--time-limit", type=float, default=10)
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args()

    print("| orders x machines | late orders (rule → opt) | weighted tardiness reduction | setup time (rule → opt) | makespan (rule → opt) | optimal / runs | avg solve s |")
    print("|---|---|---|---|---|---|---|")
    for size in args.sizes:
        n, m = map(int, size.split("x"))
        rows = []
        for seed in range(args.seeds):
            r = solve(generate(n_orders=n, n_machines=m, seed=seed, time_limit_s=args.time_limit), threads=args.threads)
            if r.metrics is None:
                continue
            rows.append(r)
        b, o = [r.baseline_metrics for r in rows], [r.metrics for r in rows]
        tb, to = sum(x.weighted_tardiness for x in b), sum(x.weighted_tardiness for x in o)
        mean = lambda xs: statistics.mean(xs)  # noqa: E731
        print(
            f"| {n} x {m} "
            f"| {mean([len(x.late_orders) for x in b]):.1f} → {mean([len(x.late_orders) for x in o]):.1f} "
            f"| {(tb - to) / tb * 100:.0f}% "
            f"| {mean([x.total_setup_minutes for x in b]):.0f} → {mean([x.total_setup_minutes for x in o]):.0f} min "
            f"| {mean([x.makespan_minutes for x in b]) / 60:.1f} → {mean([x.makespan_minutes for x in o]) / 60:.1f} h "
            f"| {sum(r.status == 'OPTIMAL' for r in rows)} / {len(rows)} "
            f"| {mean([r.solve_seconds for r in rows]):.1f} |",
            flush=True,
        )


if __name__ == "__main__":
    main()
