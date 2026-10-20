"""원격 MCP 엔드포인트(streamable HTTP)를 실제 MCP 클라이언트로 호출해 보는 스모크 테스트.

    python scripts/mcp_http_smoke.py --api http://localhost:8000 --mcp http://localhost:8001/mcp

1) 토큰 없이 연결하면 거부되는지, 2) 사용자 토큰으로 일정 생성 → 고장 반영 재계산 → 비교 흐름이 도는지 확인한다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.shared._httpx_utils import create_mcp_http_client  # noqa: E402

from factory_agent.solver.generator import generate  # noqa: E402


def data(result) -> dict:
    if getattr(result, "structured_content", None) is not None:
        d = result.structured_content
        return d.get("result", d) if isinstance(d, dict) and set(d) == {"result"} else d
    return json.loads(result.content[0].text)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--mcp", default="http://localhost:8001/mcp")
    args = ap.parse_args()

    email, password = f"mcp-{uuid.uuid4().hex[:6]}@example.com", "password123"
    async with httpx.AsyncClient(base_url=args.api) as api:
        await api.post("/auth/register", json={"email": email, "password": password})
        token = (await api.post("/auth/token", json={"email": email, "password": password})).json()["access_token"]

    async with httpx.AsyncClient() as raw:
        r = await raw.post(args.mcp, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        print(f"no token -> HTTP {r.status_code}")
        assert r.status_code == 401

    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(args.mcp, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = [t.name for t in (await session.list_tools()).tools]
                print("tools:", tools)

                problem = generate(n_orders=20, n_machines=4, seed=11, time_limit_s=5).model_dump(mode="json")
                job = data(await session.call_tool("submit_schedule", {"problem": problem, "note": "11월 1주차"}))
                done = data(await session.call_tool("wait_for_job", {"job_id": job["id"], "timeout_s": 60}))
                imp = done["summary"]["improvement"]
                print(f"plan {job['id'][:8]}: {done['status']}, late {imp['late_orders_before']} -> {imp['late_orders_after']}, "
                      f"weighted tardiness -{imp['weighted_tardiness_reduction_pct']}%")

                now = datetime.fromisoformat(problem["horizon_start"]) + timedelta(hours=3)
                patch = {
                    "reason": "EXT-2 스크류 파손, 8시간 정지",
                    "horizon_start": now.isoformat(),
                    "freeze_until": now.isoformat(),
                    "machine_downtime": [{"machine_id": "EXT-2", "start": now.isoformat(), "end": (now + timedelta(hours=8)).isoformat()}],
                }
                child = data(await session.call_tool("reschedule", {"job_id": job["id"], "patch": patch}))
                child = data(await session.call_tool("wait_for_job", {"job_id": child["id"], "timeout_s": 60}))
                diff = data(await session.call_tool("compare_jobs", {"job_a": job["id"], "job_b": child["id"]}))
                print(f"reschedule {child['id'][:8]}: {child['status']}, moved {len(diff['moved_orders'])} orders, "
                      f"late {diff['late_orders']['a']} -> {diff['late_orders']['b']}")
                fb = data(await session.call_tool("submit_feedback", {"job_id": child["id"], "rating": 4, "tags": ["good_plan"]}))
                print("feedback:", fb["rating"], fb["tags"])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
