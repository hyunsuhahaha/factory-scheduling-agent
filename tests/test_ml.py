from factory_agent.ml.model import evaluate, train
from factory_agent.ml.plant import execute, generate_history
from factory_agent.solver.cpsat import solve
from factory_agent.solver.generator import generate

from .conftest import login


def history(seed=1, n=600):
    problem = generate(n_orders=10, n_machines=3, seed=seed, time_limit_s=3)
    return problem, [r.as_dict() for r in generate_history(problem, n=n, seed=seed)]


def test_model_beats_rated_speed_on_held_out_records():
    _, records = history(n=1500)
    out = train(records, epochs=200)
    m = out["metrics"]
    assert m["mape_model_pct"] < m["mape_planned_pct"] / 3, m
    # 예측은 numpy만으로 재현된다 (torch 없는 CPU 워커용)
    again = evaluate(out["params"], records[:200])
    assert again["mape_model_pct"] < again["mape_planned_pct"]


def test_duration_override_changes_task_lengths():
    problem = generate(n_orders=4, n_machines=2, seed=3, time_limit_s=2)
    order = problem.orders[0]
    machine = next(iter(next(p for p in problem.products if p.id == order.product_id).rates))
    forced = problem.model_copy(update={"duration_minutes": {order.id: {machine: 999}}})
    result = solve(forced, threads=2)
    a = next(x for x in result.assignments if x.order_id == order.id)
    if a.machine_id == machine:
        assert (a.end - a.start).total_seconds() / 60 == 999


def test_execute_replays_plan_under_true_durations():
    problem = generate(n_orders=6, n_machines=2, seed=4, time_limit_s=2)
    result = solve(problem, threads=2).model_dump(mode="json")
    realized = execute(problem, result["assignments"], seed=0)
    assert realized["makespan_minutes"] >= result["metrics"]["makespan_minutes"]  # 계획 속도는 낙관적이다


async def test_train_on_gpu_queue_then_schedule_with_model(client, worker, gpu_worker):
    h = await login(client)
    assert (await client.post("/models/train", json={}, headers=h)).status_code == 422  # 실적 없음

    problem, records = history()
    r = await client.post("/history", json={"records": records}, headers=h)
    assert r.json() == {"inserted": len(records)}
    stats = (await client.get("/history/stats", headers=h)).json()
    assert stats["records"] == len(records) and all(v > 1 for v in stats["actual_over_planned_by_machine"].values())

    job = (await client.post("/models/train", json={"epochs": 100}, headers=h)).json()
    assert job["kind"] == "train_duration_model"
    assert await worker.run_once() is False  # cpu 워커는 학습 작업을 가져가지 않는다
    assert await gpu_worker.run_once()
    job = (await client.get(f"/jobs/{job['id']}", headers=h)).json()
    assert job["status"] == "succeeded", job
    model_id = job["training"]["model_id"]
    assert job["training"]["metrics"]["mape_model_pct"] < job["training"]["metrics"]["mape_planned_pct"]
    assert [m["id"] for m in (await client.get("/models", headers=h)).json()] == [model_id]

    body = {"problem": {**problem.model_dump(mode="json"), "duration_model_id": model_id}}
    sched = (await client.post("/jobs", json=body, headers=h)).json()
    assert await worker.run_once()
    sched = (await client.get(f"/jobs/{sched['id']}", headers=h)).json()
    assert sched["status"] == "succeeded" and sched["summary"]["duration_source"] == f"model:{model_id}"

    # 다른 사용자는 남의 모델을 쓸 수 없다
    other = await login(client, "other@example.com")
    assert (await client.post("/jobs", json=body, headers=other)).status_code == 422
