from datetime import datetime, timedelta

from factory_agent.solver.generator import generate

from .conftest import login


def problem_json(seed=0, n=8):
    return generate(n_orders=n, n_machines=3, seed=seed, time_limit_s=3).model_dump(mode="json")


async def test_auth_required(client):
    assert (await client.get("/jobs")).status_code == 401
    assert (await client.get("/jobs", headers={"Authorization": "Bearer nope"})).status_code == 401


async def test_register_duplicate_and_bad_password(client):
    body = {"email": "a@example.com", "password": "password123"}
    assert (await client.post("/auth/register", json=body)).status_code == 201
    assert (await client.post("/auth/register", json=body)).status_code == 409
    bad = await client.post("/auth/token", json={**body, "password": "wrongpass1"})
    assert bad.status_code == 401


async def test_submit_run_result_feedback(client, worker):
    h = await login(client)
    r = await client.post("/jobs", json={"problem": problem_json()}, headers=h)
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "queued" and job["queue_position"] == 0

    assert await worker.run_once()
    job = (await client.get(f"/jobs/{job['id']}", headers=h)).json()
    assert job["status"] == "succeeded", job
    summary = job["summary"]
    assert summary["solver_status"] in ("OPTIMAL", "FEASIBLE")
    assert summary["improvement"]["late_orders_after"] <= summary["improvement"]["late_orders_before"]

    result = (await client.get(f"/jobs/{job['id']}/result", headers=h)).json()
    assert len(result["assignments"]) == 8

    fb = await client.put(f"/jobs/{job['id']}/feedback", json={"rating": 2, "tags": ["too_many_setups"]}, headers=h)
    assert fb.status_code == 200
    fb = await client.put(f"/jobs/{job['id']}/feedback", json={"rating": 4, "comment": "ok"}, headers=h)
    assert fb.json()["rating"] == 4  # 같은 작업에 다시 남기면 덮어쓴다


async def test_other_users_cannot_see_job(client, worker):
    h1 = await login(client, "one@example.com")
    h2 = await login(client, "two@example.com")
    job = (await client.post("/jobs", json={"problem": problem_json()}, headers=h1)).json()
    assert (await client.get(f"/jobs/{job['id']}", headers=h2)).status_code == 404
    assert (await client.post(f"/jobs/{job['id']}/cancel", headers=h2)).status_code == 404


async def test_queued_quota(client):
    h = await login(client)
    for _ in range(3):
        assert (await client.post("/jobs", json={"problem": problem_json()}, headers=h)).status_code == 202
    assert (await client.post("/jobs", json={"problem": problem_json()}, headers=h)).status_code == 429


async def test_cancel_queued_job_is_never_run(client, worker):
    h = await login(client)
    job = (await client.post("/jobs", json={"problem": problem_json()}, headers=h)).json()
    assert (await client.post(f"/jobs/{job['id']}/cancel", headers=h)).json()["status"] == "cancelled"
    assert await worker.run_once() is False


async def test_cancel_while_running_discards_result(client, worker, app):
    h = await login(client)
    job = (await client.post("/jobs", json={"problem": problem_json()}, headers=h)).json()
    claimed = await app.state.queue.claim()
    # 워커가 claim한 직후 사용자가 취소 → 워커의 결과 저장은 조건부 UPDATE에서 버려져야 한다
    await client.post(f"/jobs/{job['id']}/cancel", headers=h)
    await worker._process(claimed)
    final = (await client.get(f"/jobs/{job['id']}", headers=h)).json()
    assert final["status"] == "cancelled" and final["summary"] is None


async def test_reschedule_with_breakdown_keeps_started_work(client, worker):
    h = await login(client)
    problem = problem_json(seed=4, n=10)
    parent = (await client.post("/jobs", json={"problem": problem}, headers=h)).json()
    await worker.run_once()
    before = (await client.get(f"/jobs/{parent['id']}/result", headers=h)).json()

    t0 = datetime.fromisoformat(problem["horizon_start"])
    now = t0 + timedelta(hours=2)
    patch = {
        "reason": "EXT-2 고장",
        "horizon_start": now.isoformat(),
        "freeze_until": now.isoformat(),
        "machine_downtime": [{"machine_id": "EXT-2", "start": now.isoformat(), "end": (now + timedelta(hours=8)).isoformat()}],
    }
    r = await client.post(f"/jobs/{parent['id']}/reschedule", json=patch, headers=h)
    assert r.status_code == 202, r.text
    child = r.json()
    assert child["parent_job_id"] == parent["id"] and child["note"] == "EXT-2 고장"
    await worker.run_once()
    after = (await client.get(f"/jobs/{child['id']}/result", headers=h)).json()
    assert after["status"] in ("OPTIMAL", "FEASIBLE"), after

    started = {a["order_id"] for a in before["assignments"] if datetime.fromisoformat(a["start"]) < now}
    released = set(after["released_frozen"])
    for a in after["assignments"]:
        start, end = datetime.fromisoformat(a["start"]), datetime.fromisoformat(a["end"])
        if a["order_id"] in started - released:
            orig = next(b for b in before["assignments"] if b["order_id"] == a["order_id"])
            assert (a["machine_id"], a["start"]) == (orig["machine_id"], orig["start"]) and a["frozen"]
        else:
            assert start >= now
        if a["machine_id"] == "EXT-2":
            assert end <= now or start >= now + timedelta(hours=8)


async def test_reschedule_unknown_order_is_422(client, worker):
    h = await login(client)
    parent = (await client.post("/jobs", json={"problem": problem_json()}, headers=h)).json()
    r = await client.post(f"/jobs/{parent['id']}/reschedule", json={"remove_order_ids": ["SO-999"]}, headers=h)
    assert r.status_code == 422


async def test_admin_endpoints(client, worker):
    user_h = await login(client)
    admin_h = await login(client, "admin@example.com")
    assert (await client.get("/admin/queue", headers=user_h)).status_code == 403
    job = (await client.post("/jobs", json={"problem": problem_json()}, headers=user_h)).json()
    stats = (await client.get("/admin/queue", headers=admin_h)).json()
    assert stats["queue"]["pending"] == 1 and stats["jobs_by_status"] == {"queued": 1}

    await worker.run_once()
    await client.put(f"/jobs/{job['id']}/feedback", json={"rating": 1, "tags": ["late_order"], "comment": "납기 못 맞춤"}, headers=user_h)
    summary = (await client.get("/admin/feedback/summary", headers=admin_h)).json()
    assert summary["count"] == 1 and summary["top_tags"] == {"late_order": 1}
    assert summary["recent_low_ratings"][0]["job_id"] == job["id"]

    me = (await client.get("/auth/me", headers=user_h)).json()
    r = await client.put(f"/admin/users/{me['id']}/limit", json={"max_running": 3}, headers=admin_h)
    assert r.json()["max_running"] == 3
