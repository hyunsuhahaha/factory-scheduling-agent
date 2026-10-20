"""실행 중인 백엔드에서 실적 적재 → gpu 큐 학습 → 학습 모델로 일정 최적화까지 한 번에 확인한다.

    python scripts/ml_smoke.py --url http://localhost:8000
"""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory_agent.ml.plant import generate_history  # noqa: E402
from factory_agent.solver.generator import generate  # noqa: E402


def wait(c: httpx.Client, h: dict, job_id: str, timeout: float = 180) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        job = c.get(f"/jobs/{job_id}", headers=h).json()
        if job["status"] not in ("queued", "running"):
            return job
        if time.monotonic() > deadline:
            raise SystemExit(f"timeout waiting for {job_id}: {job}")
        time.sleep(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    args = ap.parse_args()
    email, password = f"ml-{uuid.uuid4().hex[:6]}@example.com", "password123"
    with httpx.Client(base_url=args.url, timeout=60) as c:
        c.post("/auth/register", json={"email": email, "password": password})
        h = {"Authorization": f"Bearer {c.post('/auth/token', json={'email': email, 'password': password}).json()['access_token']}"}

        problem = generate(n_orders=15, n_machines=4, seed=7, time_limit_s=5)
        records = [r.as_dict() for r in generate_history(problem, n=1500, seed=7)]
        print(c.post("/history", json={"records": records}, headers=h).json())

        train = wait(c, h, c.post("/models/train", json={"epochs": 200}, headers=h).json()["id"])
        assert train["status"] == "succeeded", train
        metrics = train["training"]["metrics"]
        print(f"model {train['training']['model_id']} on {metrics['device']}: "
              f"MAPE {metrics['mape_planned_pct']}% (rated) -> {metrics['mape_model_pct']}% (model)")
        assert metrics["mape_model_pct"] < metrics["mape_planned_pct"]

        body = {"problem": {**problem.model_dump(mode="json"), "duration_model_id": train["training"]["model_id"]}}
        sched = wait(c, h, c.post("/jobs", json=body, headers=h).json()["id"])
        assert sched["status"] == "succeeded", sched
        print(f"schedule with learned durations: {sched['summary']['duration_source']}, "
              f"late {len(sched['summary']['metrics']['late_orders'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
