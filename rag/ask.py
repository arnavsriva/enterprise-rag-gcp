"""CLI: ask a question from the terminal (same code path as the API).

python -m rag.ask "What were Apple's total net sales in fiscal 2025?"
python -m rag.ask --tickers JPM "What was net interest income?"
python -m rag.ask --agent "Compare R&D spending at Microsoft and Alphabet"
python -m rag.ask --json ...      # full structured result
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from typing import Any

from common.config import get_settings
from common.logging import configure_logging
from rag.factory import build_services
from rag.service import QueryOptions
from rag.types import Filters


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    services = await build_services(get_settings())
    try:
        if args.agent:
            a = await services.agent.run(args.question)
            sources, cited, prefix = a.sources, set(a.cited), "S"
            out: dict[str, Any] = {"answer": a.answer, "tool_calls": a.tool_calls}
            usage, timings, cost = a.usage, a.timings, a.estimated_cost_usd
        else:
            filters = Filters(
                tickers=tuple(t.upper() for t in args.tickers.split(",")) if args.tickers else ()
            )
            r = await services.service.answer(
                args.question, QueryOptions(filters=filters, mode=args.mode)
            )
            sources, cited, prefix = r.chunks, set(r.cited), ""
            out = {"answer": r.answer}
            usage, timings, cost = r.usage, r.timings, r.estimated_cost_usd
        out |= {
            "sources": [
                {
                    "n": f"{prefix}{i}",
                    "cited": i in cited,
                    "label": c.label,
                    "chunk_id": c.chunk_id,
                    "score": round(c.score, 4),
                }
                for i, c in enumerate(sources, start=1)
            ],
            "usage": asdict(usage),
            "latency_ms": timings.stages,
            "estimated_cost_usd": cost,
        }
        return out
    finally:
        await services.close()


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("question")
    p.add_argument("--agent", action="store_true", help="use the tool-calling agent")
    p.add_argument("--tickers", help="comma-separated filter, e.g. AAPL,MSFT (direct mode only)")
    p.add_argument("--mode", choices=["vector", "hybrid"], help="retrieval mode (direct mode only)")
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = p.parse_args()
    configure_logging("WARNING")

    out = asyncio.run(_run(args))
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return
    print(out["answer"], "\n")
    for s in out["sources"]:
        print(f"  {'*' if s['cited'] else ' '} [{s['n']}] {s['label']}  (score {s['score']})")
    if "tool_calls" in out:
        print("\n  tool calls:", ", ".join(f"{c['tool']}({c['args']})" for c in out["tool_calls"]))
    u = out["usage"]
    print(
        f"\n  latency {out['latency_ms']} ms | tokens in={u['input_tokens']} out={u['output_tokens']} "
        f"thinking={u['thinking_tokens']} embed={u['embedding_tokens']} | est ${out['estimated_cost_usd']}"
    )


if __name__ == "__main__":
    main()
