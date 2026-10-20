"""MCP 서버: LLM이 생산 일정 최적화 API를 tool로 쓰게 한다.

MCP 서버는 상태를 갖지 않는 얇은 클라이언트다. 인증·할당량·큐는 모두 백엔드 API가 맡는다.

실행 모드:
  stdio            로컬 MCP 클라이언트(Claude Desktop 등)가 띄운다. 한 사용자 전용.
                   FSA_API_TOKEN, 또는 FSA_EMAIL / FSA_PASSWORD로 백엔드에 로그인한다.
  streamable-http  원격 다중 사용자용. 호출자가 보낸 Bearer 토큰을 백엔드 /auth/me로 검증하고
                   그 토큰을 그대로 백엔드에 전달한다. 공용 서비스 계정이 없으므로 사용자별 할당량이 유지된다.

환경 변수:
  FSA_API_URL        백엔드 주소 (기본 http://localhost:8000)
  FSA_MCP_PUBLIC_URL HTTP 모드에서 이 MCP 서버의 외부 주소 (기본 http://localhost:8001)
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Callable

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer

from .reschedule import ReschedulePatch
from .solver.schema import ScheduleProblem

INSTRUCTIONS = """\
Factory production scheduling tools backed by a CP-SAT optimizer.

Typical flow:
1. Build a ScheduleProblem from the user's description (machines, products with per-machine hourly rates,
   orders with due dates, family changeover times). Ask the user only for facts you cannot infer.
2. submit_schedule -> job_id, then wait_for_job until it finishes.
3. Explain the result: late orders, makespan, setups, and the improvement over the due-date-first baseline.
4. When the shop floor changes (machine breakdown, rush order, due date change), call reschedule with
   freeze_until = now so already-started work stays put. Report released_frozen if any.
5. When the user says whether the plan was usable, record it with submit_feedback.
6. Rated speeds are often optimistic. history_stats shows how actual run times compare to plan per machine.
   If the user has a trained duration model (list_duration_models), set problem.duration_model_id so the plan
   uses predicted run times; train_duration_model trains a new one from uploaded production history.
   With a model the plan accounts for the slower first run after a material change and night shifts, and the
   summary gives each order's on-time probability (at_risk_orders, expected_late_orders). If the user wants a
   safer plan, set problem.duration_quantile (e.g. 0.8 or 0.9) and compare with the median plan.
7. If the plant does not run 24/7, put shifts and holidays in problem.calendar. For "should we run overtime?",
   add candidate windows to calendar.overtime (or reschedule with add_overtime). The solver opens a window only
   when it saves more tardiness than weights.overtime_per_hour costs; report overtime_used and the late orders
   it avoided by comparing with a run without that window (compare_jobs).
Times are factory-local ISO 8601 without timezone unless the user specifies one.
"""


class ApiClient:
    def __init__(self, base_url: str, token: str | None = None, email: str | None = None, password: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=30, transport=transport)
        self._token = token
        self._email, self._password = email, password
        self._lock = asyncio.Lock()

    async def _auth_header(self) -> dict[str, str]:
        async with self._lock:
            if self._token is None:
                if not (self._email and self._password):
                    raise RuntimeError("set FSA_API_TOKEN or FSA_EMAIL/FSA_PASSWORD")
                r = await self._client.post("/auth/token", json={"email": self._email, "password": self._password})
                r.raise_for_status()
                self._token = r.json()["access_token"]
        return {"Authorization": f"Bearer {self._token}"}

    async def request(self, method: str, path: str, token: str | None = None, **kwargs: Any) -> Any:
        """token을 주면 그 토큰으로 호출한다 (HTTP 모드에서 호출자 토큰 전달)."""
        for retry in (False, True):
            headers = {"Authorization": f"Bearer {token}"} if token else await self._auth_header()
            r = await self._client.request(method, path, headers=headers, **kwargs)
            if r.status_code == 401 and not retry and self._email and not token:
                self._token = None  # 만료된 토큰이면 한 번 다시 로그인
                continue
            if r.status_code >= 400:
                try:
                    detail = r.json().get("detail")
                except ValueError:
                    detail = r.text
                raise RuntimeError(f"API {r.status_code}: {detail}")
            return r.json()

    async def me(self, token: str) -> dict | None:
        r = await self._client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        return r.json() if r.status_code == 200 else None

    async def aclose(self) -> None:
        await self._client.aclose()


class BackendTokenVerifier:
    """MCP HTTP 요청의 Bearer 토큰을 백엔드에 물어 검증한다. 백엔드 JWT 비밀키를 MCP 서버와 공유하지 않는다."""

    def __init__(self, client_factory: Callable[[], ApiClient]):
        self._factory = client_factory
        self._client: ApiClient | None = None

    async def verify_token(self, token: str) -> AccessToken | None:
        if self._client is None:
            self._client = self._factory()
        me = await self._client.me(token)
        if me is None:
            return None
        return AccessToken(token=token, client_id=f"user-{me['id']}", scopes=[], subject=str(me["id"]))


def _compact(job: dict) -> dict:
    """LLM 컨텍스트를 아끼려고 작업 상태에서 필요한 필드만 남긴다."""
    keep = ("id", "kind", "status", "queue_position", "parent_job_id", "note", "error", "summary", "training")
    return {k: job[k] for k in keep if job.get(k) is not None}


def build_server(client_factory: Callable[[], ApiClient], public_url: str | None = None) -> MCPServer:
    """public_url을 주면 HTTP 다중 사용자 모드 (호출자 토큰 검증·전달)."""
    auth_kwargs: dict[str, Any] = {}
    if public_url:
        auth_kwargs = {
            "token_verifier": BackendTokenVerifier(client_factory),
            # 토큰 검증은 BackendTokenVerifier가 백엔드에 위임하므로 resource 검사는 끈다.
            "auth": AuthSettings(issuer_url=public_url, resource_server_url=public_url, validate_token_resource=False),
        }
    mcp = MCPServer(name="factory-scheduler", instructions=INSTRUCTIONS, version="0.1.0", **auth_kwargs)
    client: ApiClient | None = None

    class _Api:
        """호출자 토큰이 있으면 그 토큰으로, 없으면 (stdio) 설정된 계정으로 백엔드를 호출한다."""

        async def request(self, method: str, path: str, **kwargs: Any) -> Any:
            nonlocal client
            if client is None:
                client = client_factory()
            caller = get_access_token()
            return await client.request(method, path, token=caller.token if caller else None, **kwargs)

    _api = _Api()

    def api() -> _Api:
        return _api

    @mcp.tool()
    async def submit_schedule(problem: ScheduleProblem, priority: int = 5, note: str | None = None) -> dict:
        """Submit a production scheduling problem for optimization. Returns the queued job (use wait_for_job next).

        priority: 0 (low) .. 9 (urgent). note: short free-text reason, stored with the job.
        """
        body = {"problem": problem.model_dump(mode="json"), "priority": priority, "note": note}
        return _compact(await api().request("POST", "/jobs", json=body))

    @mcp.tool()
    async def get_job(job_id: str) -> dict:
        """Get job status, queue position, and a metric summary when finished."""
        return _compact(await api().request("GET", f"/jobs/{job_id}"))

    @mcp.tool()
    async def wait_for_job(job_id: str, timeout_s: float = 60) -> dict:
        """Wait until the job finishes (or timeout_s, max 120) and return its status and summary."""
        deadline = asyncio.get_running_loop().time() + min(max(timeout_s, 1), 120)
        while True:
            job = await api().request("GET", f"/jobs/{job_id}")
            if job["status"] not in ("queued", "running") or asyncio.get_running_loop().time() >= deadline:
                return _compact(job)
            await asyncio.sleep(1)

    @mcp.tool()
    async def get_schedule(job_id: str, machine_id: str | None = None) -> dict:
        """Get the optimized schedule (assignments per machine, metrics). Optionally filter to one machine."""
        result = await api().request("GET", f"/jobs/{job_id}/result")
        if machine_id:
            result["assignments"] = [a for a in result["assignments"] if a["machine_id"] == machine_id]
        return result

    @mcp.tool()
    async def reschedule(job_id: str, patch: ReschedulePatch) -> dict:
        """Re-optimize an existing schedule after a shop-floor change.

        Use machine_downtime for breakdowns/maintenance, add_orders(+add_products) for rush orders,
        remove_order_ids for cancellations, order_updates for due date/quantity changes.
        Set freeze_until (usually now) to keep work that already started where it is.
        add_overtime offers overtime windows the solver may open. The previous plan is the search starting
        point (warm_start), so a good plan is found sooner after a small change.
        """
        return _compact(await api().request("POST", f"/jobs/{job_id}/reschedule", json=patch.model_dump(mode="json")))

    @mcp.tool()
    async def compare_jobs(job_a: str, job_b: str) -> dict:
        """Compare metrics of two finished jobs (e.g. before/after a reschedule) and list orders that moved."""
        a = await api().request("GET", f"/jobs/{job_a}/result")
        b = await api().request("GET", f"/jobs/{job_b}/result")
        pos_a = {x["order_id"]: x for x in a["assignments"]}
        moved = []
        for x in b["assignments"]:
            old = pos_a.get(x["order_id"])
            if old is None:
                moved.append({"order_id": x["order_id"], "change": "added", "machine": x["machine_id"], "start": x["start"]})
            elif old["machine_id"] != x["machine_id"] or old["start"] != x["start"]:
                moved.append({"order_id": x["order_id"], "from": [old["machine_id"], old["start"]], "to": [x["machine_id"], x["start"]]})
        removed = sorted(set(pos_a) - {x["order_id"] for x in b["assignments"]})
        keys = ("makespan_minutes", "total_tardiness_minutes", "weighted_tardiness", "total_setup_minutes")
        ma, mb = a.get("metrics") or {}, b.get("metrics") or {}
        return {
            "metrics": {k: {"a": ma.get(k), "b": mb.get(k)} for k in keys},
            "late_orders": {"a": ma.get("late_orders"), "b": mb.get("late_orders")},
            "moved_orders": moved,
            "removed_orders": removed,
        }

    @mcp.tool()
    async def list_my_jobs(limit: int = 10) -> list[dict]:
        """List the caller's recent jobs (newest first)."""
        return [_compact(j) for j in await api().request("GET", "/jobs", params={"limit": min(max(limit, 1), 50)})]

    @mcp.tool()
    async def submit_feedback(job_id: str, rating: int, tags: list[str] | None = None, comment: str | None = None) -> dict:
        """Record whether the plan was usable on the shop floor. rating 1..5.

        Suggested tags: late_order, too_many_setups, infeasible_in_practice, good_plan, wrong_input.
        """
        body = {"rating": rating, "tags": tags or [], "comment": comment}
        return await api().request("PUT", f"/jobs/{job_id}/feedback", json=body)

    @mcp.tool()
    async def history_stats() -> dict:
        """Production history summary: record count and actual/planned run-time ratio per machine."""
        return await api().request("GET", "/history/stats")

    @mcp.tool()
    async def train_duration_model(epochs: int = 300, note: str | None = None) -> dict:
        """Train a run-time prediction model from the caller's production history (runs on the GPU queue).

        Returns a job; wait_for_job, then its training field holds model_id and test-set error vs. rated speeds.
        """
        return _compact(await api().request("POST", "/models/train", json={"epochs": epochs, "note": note}))

    @mcp.tool()
    async def list_duration_models() -> list[dict]:
        """List the caller's trained run-time models with their test-set metrics (newest first)."""
        return await api().request("GET", "/models")

    return mcp



def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="factory scheduling MCP server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    api_url = os.environ.get("FSA_API_URL", "http://localhost:8000")
    if args.transport == "stdio":
        server = build_server(
            lambda: ApiClient(
                api_url,
                token=os.environ.get("FSA_API_TOKEN"),
                email=os.environ.get("FSA_EMAIL"),
                password=os.environ.get("FSA_PASSWORD"),
            )
        )
        server.run(transport="stdio")
    else:
        server = build_server(lambda: ApiClient(api_url), public_url=os.environ.get("FSA_MCP_PUBLIC_URL", "http://localhost:8001"))
        server.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
