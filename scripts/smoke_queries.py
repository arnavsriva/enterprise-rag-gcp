"""Smoke run: a fixed set of questions through the real service; saves latency/cost per query.

This is NOT the evaluation (no ground truth, no scoring); that's the Phase 5 golden set.
It produces the first measured latency and cost-per-query numbers, and checks that out-of-
corpus questions are refused rather than answered from the model's own knowledge.

    python scripts/smoke_queries.py        # writes results/smoke/<UTC timestamp>.json
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common.config import get_settings
from common.logging import configure_logging
from rag.factory import build_services
from rag.service import QueryOptions
from rag.types import Filters


@dataclass(frozen=True)
class Probe:
    question: str
    kind: str  # "direct" | "agent"
    tickers: tuple[str, ...] = ()
    expect_refusal: bool = False


PROBES = [
    Probe(
        "What were Apple's total net sales in fiscal 2025, and how much did they grow?", "direct"
    ),
    Probe("How much net interest income did JPMorgan Chase report for 2025?", "direct"),
    Probe("What was NVIDIA's Data Center revenue in its latest fiscal year?", "direct", ("NVDA",)),
    Probe(
        "What risks does Pfizer describe related to patent expirations and loss of exclusivity?",
        "direct",
        ("PFE",),
    ),
    Probe("How many employees did Walmart have at fiscal year end?", "direct", ("WMT",)),
    Probe(
        "What does Microsoft say about capital expenditures for AI infrastructure?",
        "direct",
        ("MSFT",),
    ),
    Probe("How does Coca-Cola manage cybersecurity risk?", "direct", ("KO",)),
    Probe("How much did ExxonMobil spend on Cash Capex in 2025?", "direct", ("XOM",)),
    # XOM's FY2025 10-K reports "Cash Capex", not this older measure: the right behaviour is to
    # say it isn't reported and list the related measures under their own names.
    Probe(
        "What were ExxonMobil's capital and exploration expenditures in 2025?",
        "direct",
        ("XOM",),
        expect_refusal=True,
    ),
    Probe("What was Tesla's total revenue in 2025?", "direct", expect_refusal=True),
    Probe("What is the name of Apple's CEO's dog?", "direct", ("AAPL",), expect_refusal=True),
    Probe("What was Apple's revenue in fiscal 2019?", "direct", ("AAPL",), expect_refusal=True),
    Probe("Compare research and development spending at Microsoft and Alphabet.", "agent"),
    Probe("Compare how Chevron and ExxonMobil describe climate-related regulatory risk.", "agent"),
]


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]


async def main() -> int:
    configure_logging("WARNING")
    settings = get_settings()
    services = await build_services(settings)
    started = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    try:
        for probe in PROBES:  # sequential, so latencies aren't inflated by self-contention
            t = time.perf_counter()
            if probe.kind == "agent":
                a = await services.agent.run(probe.question)
                answer, answered, usage, cost = a.answer, a.answered, a.usage, a.estimated_cost_usd
                cited, invalid, extra = a.cited, a.invalid_citations, {"tool_calls": a.tool_calls}
                stages = a.timings.stages
            else:
                r = await services.service.answer(
                    probe.question, QueryOptions(filters=Filters(tickers=probe.tickers))
                )
                answer, answered, usage, cost = r.answer, r.answered, r.usage, r.estimated_cost_usd
                cited, invalid, extra = r.cited, r.invalid_citations, {}
                stages = r.timings.stages
            rows.append(
                {
                    "question": probe.question,
                    "kind": probe.kind,
                    "tickers": list(probe.tickers),
                    "expect_refusal": probe.expect_refusal,
                    "answered": answered,
                    "refusal_as_expected": (not answered) == probe.expect_refusal,
                    "cited": cited,
                    "invalid_citations": invalid,
                    "latency_ms": round((time.perf_counter() - t) * 1000, 1),
                    "stage_ms": stages,
                    "usage": vars(usage),
                    "estimated_cost_usd": cost,
                    "answer": answer,
                    **extra,
                }
            )
            print(
                f"{probe.kind:6} {rows[-1]['latency_ms']:8.0f} ms  ${cost or 0:.5f}  {probe.question[:70]}",
                file=sys.stderr,
            )
    finally:
        await services.close()

    def summarize(kind: str) -> dict[str, Any]:
        sel = [r for r in rows if r["kind"] == kind]
        lat = [r["latency_ms"] for r in sel]
        costs = [r["estimated_cost_usd"] or 0.0 for r in sel]
        return {
            "n": len(sel),
            "latency_ms_p50": round(statistics.median(lat), 1),
            "latency_ms_p95": round(pct(lat, 95), 1),
            "latency_ms_max": round(max(lat), 1),
            "mean_cost_usd": round(statistics.mean(costs), 6),
            "cost_per_1k_queries_usd": round(1000 * statistics.mean(costs), 2),
            "refusal_checks_passed": sum(r["refusal_as_expected"] for r in sel),
            "invalid_citations_total": sum(len(r["invalid_citations"]) for r in sel),
        }

    report = {
        "started_at": started.isoformat(timespec="seconds"),
        "config": {
            "generation_model": settings.generation_model,
            "genai_location": settings.genai_location,
            "thinking_level": settings.generation_thinking_level,
            "embedding_model": settings.embedding_model,
            "retrieval_backend": str(settings.retrieval_backend),
            "retrieval_mode": settings.retrieval_mode,
            "top_k": settings.retrieval_top_k,
            "environment": "local laptop -> Vertex AI; local Docker pgvector",
        },
        "summary": {"direct": summarize("direct"), "agent": summarize("agent")},
        "queries": rows,
    }
    out = Path("results/smoke") / f"{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report["summary"], indent=2), file=sys.stderr)
    print(f"report: {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
