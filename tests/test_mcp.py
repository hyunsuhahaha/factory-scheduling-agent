import json
from datetime import datetime, timedelta

import httpx

from factory_agent.mcp_server import ApiClient, build_server
from factory_agent.solver.generator import generate

from .conftest import login


def payload(result) -> dict:
    """call_tool 결과에서 구조화된 값을 꺼낸다."""
    if getattr(result, "structured_content", None) is not None:
        data = result.structured_content
        return data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data
    return json.loads(result.content[0].text)


async def test_tools_are_listed(app):
    server = build_server(lambda: None)
    names = {t.name for t in await server.list_tools()}
    assert {"submit_schedule", "wait_for_job", "get_schedule", "reschedule", "compare_jobs", "submit_feedback"} <= names


async def test_agent_flow_through_mcp(app, client, worker):
    await login(client, "agent@example.com")
    transport = httpx.ASGITransport(app=app)
    server = build_server(lambda: ApiClient("http://test", email="agent@example.com", password="password123", transport=transport))

    problem = generate(n_orders=6, n_machines=2, seed=7, time_limit_s=2).model_dump(mode="json")
    job = payload(await server.call_tool("submit_schedule", {"problem": problem, "note": "주간 계획"}))
    assert job["status"] == "queued"

    await worker.run_once()
    done = payload(await server.call_tool("wait_for_job", {"job_id": job["id"], "timeout_s": 5}))
    assert done["status"] == "succeeded"

    one_machine = payload(await server.call_tool("get_schedule", {"job_id": job["id"], "machine_id": "EXT-1"}))
    assert all(a["machine_id"] == "EXT-1" for a in one_machine["assignments"])

    now = datetime.fromisoformat(problem["horizon_start"]) + timedelta(hours=1)
    patch = {
        "reason": "EXT-1 정비",
        "freeze_until": now.isoformat(),
        "machine_downtime": [{"machine_id": "EXT-1", "start": now.isoformat(), "end": (now + timedelta(hours=3)).isoformat()}],
    }
    child = payload(await server.call_tool("reschedule", {"job_id": job["id"], "patch": patch}))
    await worker.run_once()
    diff = payload(await server.call_tool("compare_jobs", {"job_a": job["id"], "job_b": child["id"]}))
    assert set(diff["metrics"]) >= {"makespan_minutes", "weighted_tardiness"}

    fb = payload(await server.call_tool("submit_feedback", {"job_id": child["id"], "rating": 5, "tags": ["good_plan"]}))
    assert fb["rating"] == 5


async def test_http_mode_verifies_and_forwards_caller_token(app, client, worker):
    """HTTP 모드: 호출자 토큰을 백엔드로 검증하고, tool 호출도 그 사용자 권한으로 나간다."""
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

    from factory_agent.mcp_server import BackendTokenVerifier

    transport = httpx.ASGITransport(app=app)
    factory = lambda: ApiClient("http://test", transport=transport)  # noqa: E731 - 서비스 계정 없음
    verifier = BackendTokenVerifier(factory)
    assert await verifier.verify_token("garbage") is None

    alice = (await login(client, "alice@example.com"))["Authorization"].split()[1]
    bob = (await login(client, "bob@example.com"))["Authorization"].split()[1]
    alice_token = await verifier.verify_token(alice)
    assert alice_token is not None

    server = build_server(factory, public_url="http://localhost:8001")
    problem = generate(n_orders=4, n_machines=2, seed=1, time_limit_s=1).model_dump(mode="json")
    ctx = auth_context_var.set(AuthenticatedUser(alice_token))
    try:
        job = payload(await server.call_tool("submit_schedule", {"problem": problem}))
    finally:
        auth_context_var.reset(ctx)

    # 작업은 alice 소유: bob 토큰으로는 보이지 않는다.
    assert (await client.get(f"/jobs/{job['id']}", headers={"Authorization": f"Bearer {alice}"})).status_code == 200
    assert (await client.get(f"/jobs/{job['id']}", headers={"Authorization": f"Bearer {bob}"})).status_code == 404
