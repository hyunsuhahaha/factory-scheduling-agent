"""여러 사용자가 동시에 최적화 작업을 제출하는 부하 테스트.

    python scripts/loadtest.py --users 10 --jobs-per-user 5 --orders 20

측정: 제출 응답 지연, 대기 시간(제출→시작), 실행 시간(시작→완료), 처리량, 사용자별 공정성.
실패한 작업이 있으면 종료 코드 1.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory_agent.solver.generator import generate  # noqa: E402


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


async def user_session(client: httpx.AsyncClient, idx: int, args, run_id: str) -> list[dict]:
    email, password = f"load-{run_id}-{idx}@example.com", "password123"
    await client.post("/auth/register", json={"email": email, "password": password})
    token = (await client.post("/auth/token", json={"email": email, "password": password})).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}

    submitted = []
    for k in range(args.jobs_per_user):
        problem = generate(n_orders=args.orders, n_machines=4, seed=idx * 1000 + k, time_limit_s=args.time_limit)
        t = time.perf_counter()
        r = await client.post("/jobs", json={"problem": problem.model_dump(mode="json")}, headers=h)
        r.raise_for_status()
        submitted.append((r.json()["id"], time.perf_counter() - t))

    results = []
    for job_id, submit_latency in submitted:
        while True:
            job = (await client.get(f"/jobs/{job_id}", headers=h)).json()
            if job["status"] not in ("queued", "running"):
                break
            await asyncio.sleep(0.5)
        created, started, finished = (
            datetime.fromisoformat(job[k]) if job[k] else None for k in ("created_at", "started_at", "finished_at")
        )
        results.append(
            {
                "user": idx,
                "status": job["status"],
                "submit_ms": submit_latency * 1000,
                "wait_s": (started - created).total_seconds() if started else None,
                "run_s": (finished - started).total_seconds() if started and finished else None,
                "finished": finished,
                "created": created,
            }
        )
    return results


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--users", type=int, default=10)
    ap.add_argument("--jobs-per-user", type=int, default=5)
    ap.add_argument("--orders", type=int, default=20)
    ap.add_argument("--time-limit", type=float, default=5)
    args = ap.parse_args()

    run_id = uuid.uuid4().hex[:6]
    t0 = time.perf_counter()
    async with httpx.AsyncClient(base_url=args.url, timeout=30) as client:
        per_user = await asyncio.gather(*[user_session(client, i, args, run_id) for i in range(args.users)])
    wall = time.perf_counter() - t0
    rows = [r for u in per_user for r in u]
    ok = [r for r in rows if r["status"] == "succeeded"]
    waits = [r["wait_s"] for r in ok]
    runs = [r["run_s"] for r in ok]
    submits = [r["submit_ms"] for r in rows]
    span = (max(r["finished"] for r in ok) - min(r["created"] for r in ok)).total_seconds() if ok else 0
    # 공정성: 사용자별 첫 작업 시작까지의 대기 시간 분포
    first_wait = [min(r["wait_s"] for r in u if r["wait_s"] is not None) for u in per_user if any(r["wait_s"] for r in u)]

    print(f"jobs: {len(rows)} (succeeded {len(ok)}, failed {len(rows) - len(ok)}), users: {args.users}, wall {wall:.1f}s")
    print(f"submit latency ms  p50 {pct(submits, 50):.0f}  p95 {pct(submits, 95):.0f}  max {max(submits):.0f}")
    if ok:
        print(f"queue wait s       p50 {pct(waits, 50):.1f}  p95 {pct(waits, 95):.1f}  max {max(waits):.1f}")
        print(f"solve run s        p50 {pct(runs, 50):.1f}  p95 {pct(runs, 95):.1f}")
        print(f"throughput         {len(ok) / span * 60:.1f} jobs/min")
        print(f"first-job wait s   min {min(first_wait):.1f}  max {max(first_wait):.1f}  stdev {statistics.pstdev(first_wait):.1f}")
    return 0 if len(ok) == len(rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
