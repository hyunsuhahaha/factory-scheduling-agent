from types import SimpleNamespace

import pytest

from factory_agent.agent.evalset import build_cases
from factory_agent.agent.harness import OracleClient, run_eval
from factory_agent.agent.loop import AgentConfig, SchedulingAgent


def one_per_category():
    seen, picked = set(), []
    for case in build_cases():
        if case.category not in seen:
            seen.add(case.category)
            picked.append(case)
    return picked


async def test_eval_set_shape():
    cases = build_cases()
    assert len(cases) == 36 and len({c.id for c in cases}) == 36
    assert all(c.oracle is not None for c in cases if c.category != "missing_info")


async def test_oracle_passes_every_category():
    results = await run_eval(one_per_category(), lambda c, ctx: OracleClient(c, ctx), AgentConfig(), concurrency=4)
    assert [r.id for r in results if not r.passed] == []


def _mutations():
    def wrong_quantity(name, args):
        args["problem"]["orders"][0]["quantity"] += 100
        return args

    def wrong_rate(name, args):
        prod = args["problem"]["products"][0]
        machine = next(iter(prod["rates"]))
        prod["rates"][machine] *= 1.1
        return args

    def wrong_machine(name, args):
        args["patch"]["machine_downtime"][0]["machine_id"] = "EXT-3" if args["patch"]["machine_downtime"][0]["machine_id"] != "EXT-3" else "EXT-1"
        return args

    def no_freeze(name, args):
        args["patch"].pop("freeze_until")
        return args

    return [("create_abs_1", wrong_quantity, "order SO-001"), ("create_abs_2", wrong_rate, "product"),
            ("breakdown_1", wrong_machine, "downtime"), ("breakdown_2", no_freeze, "freeze_until")]


@pytest.mark.parametrize("case_id, mutate, expected", _mutations())
async def test_graders_catch_wrong_tool_input(case_id, mutate, expected):
    case = next(c for c in build_cases() if c.id == case_id)
    [result] = await run_eval([case], lambda c, ctx: OracleClient(c, ctx, mutate=mutate), AgentConfig())
    assert not result.passed
    assert expected in " ".join(result.failures)


async def test_missing_info_fails_if_agent_submits():
    case = next(c for c in build_cases() if c.category == "missing_info")
    donor = next(c for c in build_cases() if c.id == "create_abs_1")
    case.oracle = donor.oracle  # 정보가 없는데도 제출하는 agent
    [result] = await run_eval([case], lambda c, ctx: OracleClient(c, ctx), AgentConfig())
    assert not result.passed and "submitted despite missing info" in " ".join(result.failures)


class _Recorder:
    def __init__(self, responses):
        self.calls, self._responses = [], list(responses)
        create = self._create
        self.messages = SimpleNamespace(create=create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _Tools:
    def __init__(self):
        self.seen = []

    async def list_tools(self):
        return [{"name": n, "description": n, "input_schema": {"type": "object"}} for n in ("zeta", "alpha")]

    async def call_tool(self, name, arguments):
        self.seen.append((name, arguments))
        return ("bad input", True) if arguments.get("bad") else ("ok", False)


def _usage():
    return SimpleNamespace(input_tokens=100, output_tokens=10, cache_read_input_tokens=50, cache_creation_input_tokens=0)


async def test_request_shape_and_tool_error_roundtrip():
    tool_use = lambda i, args: SimpleNamespace(type="tool_use", id=f"t{i}", name="alpha", input=args)  # noqa: E731
    client = _Recorder([
        SimpleNamespace(content=[tool_use(1, {"bad": True}), tool_use(2, {})], stop_reason="tool_use", usage=_usage()),
        SimpleNamespace(content=[SimpleNamespace(type="text", text="done")], stop_reason="end_turn", usage=_usage()),
    ])
    tools = _Tools()
    trace, messages = await SchedulingAgent(client, tools, AgentConfig(effort="low")).run("hi")

    first = client.calls[0]
    assert first["model"] == "claude-opus-5-5" and first["output_config"] == {"effort": "low"}
    assert first["fallbacks"] == "default" and first["betas"] == ["server-side-fallback-2026-07-01"]
    assert [t["name"] for t in first["tools"]] == ["alpha", "zeta"]  # 순서 고정 (prompt cache)
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "tool_choice" not in first  # Opus 5.5는 강제 tool_choice 불가

    # 병렬 tool 호출 결과는 한 user 메시지로, 오류는 is_error로 돌려준다
    results = messages[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"] and results[0]["is_error"] is True
    assert trace.final_text == "done" and trace.turns == 2 and trace.cache_read_tokens == 100
    assert trace.cost_usd() == pytest.approx((200 * 4 + 100 * 0.4 + 20 * 20) / 1e6)


async def test_refusal_stops_the_loop():
    client = _Recorder([SimpleNamespace(content=[], stop_reason="refusal", usage=_usage())])
    trace, _ = await SchedulingAgent(client, _Tools()).run("hi")
    assert trace.error == "refusal" and trace.turns == 1
