"""agent 평가 하네스.

백엔드(API + 내장 워커 + fakeredis + SQLite)와 MCP 서버를 한 프로세스에 띄우고,
케이스마다 별도 사용자로 agent를 돌린 뒤 프로그램 채점기로 판정한다.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import httpx

from ..api import create_app
from ..config import Settings
from ..mcp_server import ApiClient, build_server
from .evalset import Case, CaseContext
from .loop import AgentConfig, InProcessMCPTools, SchedulingAgent, Trace

PASSWORD = "eval-password-123"


@dataclass
class CaseResult:
    id: str
    category: str
    passed: bool
    failures: list[str]
    turns: int
    tool_calls: list[str]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: float
    seconds: float
    error: str | None
    final_text: str = ""
    tool_inputs: list[dict] = field(default_factory=list)


async def _wait(api: httpx.AsyncClient, headers: dict, job_id: str, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        job = (await api.get(f"/jobs/{job_id}", headers=headers)).json()
        if job["status"] not in ("queued", "running") or time.monotonic() > deadline:
            return job
        await asyncio.sleep(0.2)


async def run_eval(
    cases: list[Case],
    client_factory: Callable[[Case, CaseContext], object],
    config: AgentConfig,
    concurrency: int = 4,
    on_result: Callable[[CaseResult], None] | None = None,
) -> list[CaseResult]:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            database_url=f"sqlite+aiosqlite:///{Path(tmp) / 'eval.db'}",
            embedded=True,
            jwt_secret="eval-secret-0123456789abcdef0123456789",
            max_running_per_user=4,
            max_queued_per_user=50,
            worker_slots=4,
            solver_threads=2,
        )
        app = create_app(settings, run_worker=True)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://eval") as api:
                sem = asyncio.Semaphore(concurrency)

                async def one(case: Case) -> CaseResult:
                    async with sem:
                        result = await _run_case(case, api, transport, client_factory, config)
                    if on_result:
                        on_result(result)
                    return result

                return list(await asyncio.gather(*(one(c) for c in cases)))


async def _run_case(case: Case, api: httpx.AsyncClient, transport, client_factory, config: AgentConfig) -> CaseResult:
    email = f"eval-{case.id}@example.com"
    await api.post("/auth/register", json={"email": email, "password": PASSWORD})
    token = (await api.post("/auth/token", json={"email": email, "password": PASSWORD})).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    ctx = CaseContext()
    if case.parent is not None:
        r = await api.post("/jobs", json={"problem": case.parent.model_dump(mode="json")}, headers=headers)
        job = await _wait(api, headers, r.json()["id"])
        ctx.parent_job_id, ctx.parent_problem = job["id"], case.parent

    server = build_server(lambda: ApiClient("http://eval", token=token, transport=transport))
    agent = SchedulingAgent(client_factory(case, ctx), InProcessMCPTools(server), config)
    prompt = case.prompt.replace("{parent_job_id}", ctx.parent_job_id or "")
    trace, _ = await agent.run(prompt)

    failures = []
    for check in case.checks:
        ok, why = check(trace, ctx)
        if not ok:
            failures.append(f"{check.__name__ if hasattr(check, '__name__') else 'check'}: {why}")
    return CaseResult(
        id=case.id,
        category=case.category,
        passed=not failures and trace.error is None,
        failures=failures + ([f"error: {trace.error}"] if trace.error else []),
        turns=trace.turns,
        tool_calls=[c.name for c in trace.tool_calls],
        input_tokens=trace.input_tokens,
        output_tokens=trace.output_tokens,
        cache_read_tokens=trace.cache_read_tokens,
        cost_usd=round(trace.cost_usd(), 5),
        seconds=round(trace.seconds, 2),
        error=trace.error,
        final_text=trace.final_text,
        tool_inputs=[{"name": c.name, "input": c.input, "is_error": c.is_error} for c in trace.tool_calls],
    )


def summarize(results: list[CaseResult]) -> str:
    cats: dict[str, list[CaseResult]] = {}
    for r in results:
        cats.setdefault(r.category, []).append(r)
    lines = ["| category | pass | avg turns | avg tool calls |", "|---|---|---|---|"]
    for cat, rs in cats.items():
        lines.append(
            f"| {cat} | {sum(r.passed for r in rs)}/{len(rs)} | {statistics.mean(r.turns for r in rs):.1f} "
            f"| {statistics.mean(len(r.tool_calls) for r in rs):.1f} |"
        )
    n = len(results)
    passed = sum(r.passed for r in results)
    lines.append(f"| **total** | **{passed}/{n} ({passed / n * 100:.0f}%)** | {statistics.mean(r.turns for r in results):.1f} "
                 f"| {statistics.mean(len(r.tool_calls) for r in results):.1f} |")
    cost = sum(r.cost_usd for r in results)
    lines.append("")
    lines.append(f"tokens in {sum(r.input_tokens for r in results)} (cache read {sum(r.cache_read_tokens for r in results)}), "
                 f"out {sum(r.output_tokens for r in results)}, cost ${cost:.3f} (${cost / n:.4f}/case), "
                 f"latency p50 {statistics.median(r.seconds for r in results):.1f}s")
    return "\n".join(lines)


def dump(results: list[CaseResult], path: Path, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"meta": meta, "results": [asdict(r) for r in results]}, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")


# ---------------------------------------------------------------- oracle client (하네스·채점기 검증용)

class OracleClient:
    """정답 tool 호출을 그대로 내는 가짜 모델. 하네스와 채점기가 맞는지 API 키 없이 확인한다.

    mutate를 주면 정답 입력을 일부러 망가뜨려 채점기가 잡아내는지 본다.
    """

    def __init__(self, case: Case, ctx: CaseContext, mutate: Callable[[str, dict], dict] | None = None):
        self.case, self.ctx, self.mutate = case, ctx, mutate
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, *, messages: list, **_: object):
        results = [m for m in messages if m["role"] == "user" and isinstance(m["content"], list)]
        usage = SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        if self.case.oracle is None:
            return self._text("필요한 정보가 빠져 있어요. 설비별 생산량, 납기, 주문 목록을 알려줄래요?", usage)
        if not results:
            name, args = self.case.oracle
            args = json.loads(json.dumps(args))
            if name == "reschedule":
                args["job_id"] = self.ctx.parent_job_id
            if self.mutate:
                args = self.mutate(name, args)
            return self._tool(name, args, usage)
        last = results[-1]["content"][0]
        if len(results) == 1:
            job_id = json.loads(last["content"])["id"]
            return self._tool("wait_for_job", {"job_id": job_id, "timeout_s": 60}, usage)
        return self._text("일정을 짰어요.", usage)

    @staticmethod
    def _tool(name: str, args: dict, usage):
        block = SimpleNamespace(type="tool_use", id=f"tu_{name}", name=name, input=args)
        return SimpleNamespace(content=[block], stop_reason="tool_use", usage=usage)

    @staticmethod
    def _text(text: str, usage):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn", usage=usage)
