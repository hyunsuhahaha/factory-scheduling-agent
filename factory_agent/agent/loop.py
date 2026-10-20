"""Claude tool-use 루프: 자연어 요청을 MCP tool 호출로 바꿔 생산 일정을 짜고 설명한다.

- tool 목록은 MCP 서버에서 그대로 가져온다 (in-process MCPServer든 원격 MCP 세션이든 같은 인터페이스).
- 이력은 append-only로 쌓는다. 응답 content를 그대로 되돌려 보내야 thinking block이 유효하다.
- 매 턴의 tool 호출·토큰·지연을 trace로 남겨 평가와 비용 계산에 쓴다.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

SYSTEM_PROMPT = """\
You are a production planning assistant for a film extrusion plant. You plan and re-plan production schedules \
with the scheduling tools, then explain the result to the planner in Korean.

How to work:
- Turn the planner's description into a ScheduleProblem exactly as stated: machine ids, product families, \
per-machine hourly rates, order quantities, due dates, release times, changeover minutes. Do not invent numbers.
- If something the problem requires is missing (for example no due dates, no machine rates, or no orders), ask \
one concise question listing everything that is missing, and do not submit.
- Machines are often called by number ("2호기"); map them to the matching machine id (EXT-2) when unambiguous.
- Times are factory-local ISO 8601 without timezone. "Now" is given in the planner's message; use it as \
horizon_start for new plans and as horizon_start and freeze_until for re-plans unless told otherwise.
- After submit_schedule or reschedule, call wait_for_job and base your answer on its summary.
- For shop-floor changes to an existing plan (breakdown, maintenance, rush order, due date change, cancellation), \
use reschedule on that plan's job id rather than submitting a new problem.
- Answer briefly: late orders, total setup time, completion time, and the improvement over the due-date-first rule. \
If released_frozen is not empty, say which started orders had to move.
"""


class ToolProvider(Protocol):
    async def list_tools(self) -> list[dict]: ...
    async def call_tool(self, name: str, arguments: dict) -> tuple[str, bool]: ...


class InProcessMCPTools:
    """같은 프로세스의 MCPServer를 직접 호출한다 (평가·테스트용)."""

    def __init__(self, server):
        self.server = server

    async def list_tools(self) -> list[dict]:
        return [_to_claude_tool(t) for t in await self.server.list_tools()]

    async def call_tool(self, name: str, arguments: dict) -> tuple[str, bool]:
        try:
            result = await self.server.call_tool(name, arguments)
        except Exception as exc:  # noqa: BLE001 - tool 오류는 모델에 되돌려 스스로 고치게 한다
            return f"{type(exc).__name__}: {exc}", True
        return _result_text(result), bool(getattr(result, "is_error", False))


class MCPSessionTools:
    """원격 MCP 서버 세션 (stdio·streamable HTTP)."""

    def __init__(self, session):
        self.session = session

    async def list_tools(self) -> list[dict]:
        return [_to_claude_tool(t) for t in (await self.session.list_tools()).tools]

    async def call_tool(self, name: str, arguments: dict) -> tuple[str, bool]:
        result = await self.session.call_tool(name, arguments)
        return _result_text(result), bool(getattr(result, "is_error", False))


def _to_claude_tool(tool) -> dict:
    return {"name": tool.name, "description": tool.description or "", "input_schema": tool.input_schema}


def _result_text(result) -> str:
    data = getattr(result, "structured_content", None)
    if data is not None:
        if isinstance(data, dict) and set(data) == {"result"}:
            data = data["result"]
        return json.dumps(data, ensure_ascii=False, default=str)
    return "\n".join(getattr(c, "text", "") for c in result.content)


@dataclass
class ToolCall:
    name: str
    input: dict
    output: str
    is_error: bool
    seconds: float


@dataclass
class Trace:
    final_text: str = ""
    stop_reason: str = ""
    turns: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    seconds: float = 0.0
    error: str | None = None

    def calls(self, name: str) -> list[ToolCall]:
        return [c for c in self.tool_calls if c.name == name]

    def cost_usd(self, input_per_m: float = 4.0, output_per_m: float = 20.0) -> float:
        """기본 단가는 claude-opus-5-5. cache read는 0.1배, cache write는 1.25배로 계산."""
        return (
            self.input_tokens * input_per_m
            + self.cache_read_tokens * input_per_m * 0.1
            + self.cache_write_tokens * input_per_m * 1.25
            + self.output_tokens * output_per_m
        ) / 1_000_000


@dataclass
class AgentConfig:
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    max_tokens: int = 16000
    max_turns: int = 12
    use_fallbacks: bool = True  # 안전 분류기 거절 시 서버 측에서 권장 모델로 재시도


class SchedulingAgent:
    def __init__(self, client, tools: ToolProvider, config: AgentConfig | None = None):
        """client: anthropic.AsyncAnthropic (테스트에서는 같은 모양의 가짜 객체)."""
        self.client = client
        self.tools = tools
        self.config = config or AgentConfig()
        self._tool_defs: list[dict] | None = None

    async def _request(self, messages: list) -> Any:
        if self._tool_defs is None:
            defs = sorted(await self.tools.list_tools(), key=lambda t: t["name"])  # 순서 고정 → prompt cache 유지
            self._tool_defs = defs
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            "tools": self._tool_defs,
            "messages": messages,
            "output_config": {"effort": self.config.effort},
        }
        if self.config.use_fallbacks:
            kwargs |= {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
            return await self.client.beta.messages.create(**kwargs)
        return await self.client.messages.create(**kwargs)

    async def run(self, user_message: str, history: list | None = None) -> tuple[Trace, list]:
        """history를 이어 붙여 대화를 계속할 수 있다. (trace, 갱신된 messages)를 돌려준다."""
        trace = Trace()
        messages = list(history or []) + [{"role": "user", "content": user_message}]
        started = time.perf_counter()
        try:
            while trace.turns < self.config.max_turns:
                response = await self._request(messages)
                trace.turns += 1
                usage = response.usage
                trace.input_tokens += usage.input_tokens or 0
                trace.output_tokens += usage.output_tokens or 0
                trace.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
                trace.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
                trace.stop_reason = response.stop_reason
                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason == "refusal":
                    trace.error = "refusal"
                    break
                if response.stop_reason == "max_tokens":
                    trace.error = "max_tokens"
                    break
                uses = [b for b in response.content if b.type == "tool_use"]
                if not uses:
                    trace.final_text = "\n".join(b.text for b in response.content if b.type == "text").strip()
                    break

                results = []
                for block in uses:  # 병렬 호출이어도 결과는 한 user 메시지로 묶어 돌려준다
                    t = time.perf_counter()
                    output, is_error = await self.tools.call_tool(block.name, dict(block.input))
                    trace.tool_calls.append(ToolCall(block.name, dict(block.input), output, is_error, time.perf_counter() - t))
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": output, "is_error": is_error})
                messages.append({"role": "user", "content": results})
            else:
                trace.error = "max_turns"
        except Exception as exc:  # noqa: BLE001 - 평가에서 한 케이스의 API 오류가 전체를 멈추지 않게
            trace.error = f"{type(exc).__name__}: {exc}"
        trace.seconds = time.perf_counter() - started
        return trace, messages
