"""LLM agent 평가 실행.

    python scripts/run_agent_eval.py                     # Claude API (ANTHROPIC_API_KEY 필요)
    python scripts/run_agent_eval.py --effort low --cases breakdown rush
    python scripts/run_agent_eval.py --oracle            # 정답 호출만 내는 가짜 모델로 하네스·채점기 점검

결과: evals/results/<시각>-<모델>-<effort>.json, 표는 stdout.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from factory_agent.agent.evalset import build_cases  # noqa: E402
from factory_agent.agent.harness import OracleClient, dump, run_eval, summarize  # noqa: E402
from factory_agent.agent.loop import AgentConfig  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--cases", nargs="*", help="id 또는 category 접두어로 필터")
    ap.add_argument("--oracle", action="store_true")
    args = ap.parse_args()

    for name in ("httpx", "fsa", "mcp"):  # 진행 상황 출력만 남긴다
        logging.getLogger(name).setLevel(logging.WARNING)
    cases = build_cases()
    if args.cases:
        cases = [c for c in cases if any(c.id.startswith(f) or c.category.startswith(f) for f in args.cases)]

    if args.oracle:
        factory = lambda case, ctx: OracleClient(case, ctx)  # noqa: E731
        label = "oracle"
    else:
        import anthropic

        client = anthropic.AsyncAnthropic()
        factory = lambda case, ctx: client  # noqa: E731
        label = f"{args.model}-{args.effort}"

    config = AgentConfig(model=args.model, effort=args.effort)
    done = []

    def progress(r) -> None:
        done.append(r)
        mark = "PASS" if r.passed else "FAIL"
        print(f"[{len(done):>2}/{len(cases)}] {mark} {r.id:<16} turns={r.turns} tools={','.join(r.tool_calls)}"
              + ("" if r.passed else f"  -> {' | '.join(r.failures)[:300]}"), flush=True)

    results = await run_eval(cases, factory, config, concurrency=args.concurrency, on_result=progress)
    results.sort(key=lambda r: r.id)
    print()
    print(summarize(results))
    out = ROOT / "evals" / "results" / f"{datetime.now():%Y%m%d-%H%M%S}-{label}.json"
    dump(results, out, {"model": args.model, "effort": args.effort, "oracle": args.oracle, "cases": len(cases)})
    print(f"\nsaved {out.relative_to(ROOT)}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
