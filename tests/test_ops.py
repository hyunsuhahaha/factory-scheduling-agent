import os
import subprocess
import sys
from pathlib import Path

from factory_agent.solver.generator import generate

from .conftest import login

ROOT = Path(__file__).resolve().parents[1]


async def test_metrics_endpoint_reports_jobs_and_queues(client, worker):
    h = await login(client)
    problem = generate(n_orders=5, n_machines=2, seed=2, time_limit_s=2).model_dump(mode="json")
    await client.post("/jobs", json={"problem": problem}, headers=h)
    before = (await client.get("/metrics")).text
    assert 'fsa_queue_pending{queue="cpu"} 1.0' in before
    assert 'fsa_jobs_submitted_total{kind="schedule"}' in before

    await worker.run_once()
    after = (await client.get("/metrics")).text
    assert 'fsa_queue_pending{queue="cpu"} 0.0' in after
    assert 'fsa_jobs_finished_total{kind="schedule",status="succeeded"}' in after
    assert 'fsa_http_requests_total{method="POST",route="/jobs",status="202"}' in after
    job_id = (await client.get("/jobs", headers=h)).json()[0]["id"]
    await client.get(f"/jobs/{job_id}", headers=h)
    text = (await client.get("/metrics")).text
    assert 'route="/jobs/{job_id}"' in text and job_id not in text  # 라벨은 경로 템플릿, job id가 라벨로 새지 않음
    assert "fsa_http_request_seconds_bucket" in after


def test_migrations_match_models(tmp_path):
    """alembic upgrade head로 만든 스키마가 ORM 모델과 같고, downgrade까지 되는지 확인한다."""
    env = {**os.environ, "FSA_DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"}
    run = lambda *a: subprocess.run([sys.executable, "-m", "alembic", *a], cwd=ROOT, env=env, capture_output=True, text=True)  # noqa: E731
    for step in (("upgrade", "head"), ("check",), ("downgrade", "base")):
        r = run(*step)
        assert r.returncode == 0, (step, r.stdout, r.stderr)
