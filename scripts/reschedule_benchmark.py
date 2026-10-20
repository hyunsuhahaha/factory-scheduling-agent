"""재계산 warm start 효과.

큰 문제를 한 번 최적화한 뒤 설비 1대가 8시간 고장 났다고 하고 재계산한다. 같은 재계산을
(a) 납기순 규칙 해에서 시작, (b) 이전 해에서 시작 두 가지로 짧은 time limit마다 돌려
목적값, 첫 해까지 걸린 시간, 이전 계획에서 바뀐 주문 수를 비교한다.

    python scripts/reschedule_benchmark.py --seeds 5 --orders 40 --machines 5
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory_agent.reschedule import ReschedulePatch, apply_patch  # noqa: E402
from factory_agent.solver.cpsat import solve  # noqa: E402
from factory_agent.solver.generator import generate  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--orders", type=int, default=40)
    ap.add_argument("--machines", type=int, default=5)
    ap.add_argument("--first-limit", type=float, default=20)
    ap.add_argument("--limits", type=float, nargs="+", default=[0.5, 1, 3])
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args()

    out = {(lim, w): [] for lim in args.limits for w in (False, True)}
    for seed in range(args.seeds):
        problem = generate(args.orders, args.machines, seed=200 + seed, time_limit_s=args.first_limit)
        first = solve(problem, threads=args.threads)
        now = problem.horizon_start + timedelta(hours=4)
        before = {a.order_id: (a.machine_id, a.start) for a in first.assignments}
        for lim in args.limits:
            for warm in (False, True):
                patch = ReschedulePatch(
                    horizon_start=now, freeze_until=now, time_limit_s=lim, warm_start=warm,
                    machine_downtime=[{"machine_id": "EXT-2", "start": now, "end": now + timedelta(hours=8)}],
                )
                new = apply_patch(problem, first, patch)
                progress: list[tuple[float, float]] = []
                r = solve(new, threads=args.threads, progress=progress)
                moved = sum(before[a.order_id] != (a.machine_id, a.start) for a in r.assignments if not a.frozen)
                out[lim, warm].append({"obj": progress[-1][1], "first": progress[0][0], "moved": moved,
                                       "late": len(r.metrics.late_orders)})
        print(f"seed {seed} done", flush=True)

    print()
    print("| time limit | 시작점 | 목적값 (규칙 시작 = 100) | 첫 해까지 (s) | 지연 주문 | 바뀐 주문 |")
    print("|---|---|---|---|---|---|")
    for lim in args.limits:
        cold = out[lim, False]
        for warm in (False, True):
            rows = out[lim, warm]
            rel = statistics.mean(r["obj"] / c["obj"] * 100 for r, c in zip(rows, cold))

            def m(k, rows=rows):
                return statistics.mean(r[k] for r in rows)

            start = "이전 해" if warm else "납기순 규칙"
            print(f"| {lim:g}s | {start} | {rel:.1f} | {m('first'):.2f} | {m('late'):.1f} | {m('moved'):.1f} |")


if __name__ == "__main__":
    main()
