"""대화형 agent: 원격 MCP 엔드포인트에 붙어 Claude로 생산 계획을 짠다.

    FSA_TOKEN=<백엔드 토큰> python -m factory_agent.agent.cli --mcp http://localhost:8001/mcp

Claude 자격 증명은 ANTHROPIC_API_KEY (또는 `ant auth login` 프로필)에서 읽는다.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime

import anthropic
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from .loop import AgentConfig, MCPSessionTools, SchedulingAgent


async def chat(mcp_url: str, token: str, config: AgentConfig) -> None:
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(mcp_url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                agent = SchedulingAgent(anthropic.AsyncAnthropic(), MCPSessionTools(session), config)
                history: list = []
                print(f"connected to {mcp_url} ({config.model}, effort={config.effort}). 빈 줄로 종료.")
                while True:
                    try:
                        text = input("\n> ").strip()
                    except EOFError:
                        break
                    if not text:
                        break
                    # 현재 시각을 매 요청에 붙여 준다 (system prompt에 넣으면 prompt cache가 깨진다)
                    trace, history = await agent.run(f"현재 시각 {datetime.now():%Y-%m-%dT%H:%M}.\n{text}", history)
                    for call in trace.tool_calls:
                        print(f"  · {call.name}{' (error)' if call.is_error else ''} {call.seconds:.1f}s")
                    print(trace.final_text or f"[{trace.error}]")
                    print(f"  ({trace.turns} turns, in {trace.input_tokens} / cache {trace.cache_read_tokens} / "
                          f"out {trace.output_tokens} tokens, ${trace.cost_usd():.4f})")


def main() -> None:
    ap = argparse.ArgumentParser(description="factory scheduling agent (Claude + MCP)")
    ap.add_argument("--mcp", default=os.environ.get("FSA_MCP_URL", "http://localhost:8001/mcp"))
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--effort", default="medium")
    args = ap.parse_args()
    token = os.environ.get("FSA_TOKEN")
    if not token:
        raise SystemExit("set FSA_TOKEN to your backend access token (POST /auth/token)")
    asyncio.run(chat(args.mcp, token, AgentConfig(model=args.model, effort=args.effort)))


if __name__ == "__main__":
    main()
