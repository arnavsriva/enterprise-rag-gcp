"""Smoke run: a fixed set of questions through the real service; saves latency/cost per query.

This is NOT the evaluation (no ground truth, no scoring); that's the Phase 5 golden set.
It produces the first measured latency and cost-per-query numbers, and checks that out-of-
corpus questions are refused rather than answered from the model's own knowledge.

    python scripts/smoke_queries.py                    # local services -> results/smoke/<ts>.json
    python scripts/smoke_queries.py --api https://...  # deployed API (identity token from gcloud)
                                                       #   -> results/smoke/<ts>_cloud.json
In --api mode, `latency_ms` is measured by this client (includes the internet round trip) and
`server_ms` is the API's own total; summaries report both.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

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
    expect_refusal: bool | None = False  # None: either answering or refusing is acceptable


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
    # XOM's FY2025 10-K reports "Cash Capex", not this older measure. Faithful replies either
    # refuse and list the related measures, or say the measure isn't reported and list them;
    # the model has done both for this exact prompt (non-determinism). Phase 5's judge grades it.
    Probe(
        "What were ExxonMobil's capital and exploration expenditures in 2025?",
        "direct",
        ("XOM",),
        expect_refusal=None,
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


async def run_local(probe: Probe, services: Any) -> dict[str, Any]:
    if probe.kind == "agent":
        a = await services.agent.run(probe.question)
        return {
            "answer": a.answer,
            "answered": a.answered,
            "cited": a.cited,
            "invalid_citations": a.invalid_citations,
            "usage": vars(a.usage),
            "estimated_cost_usd": a.estimated_cost_usd,
            "stage_ms": a.timings.stages,
            "tool_calls": a.tool_calls,
        }
    r = await services.service.answer(
        probe.question, QueryOptions(filters=Filters(tickers=probe.tickers))
    )
    return {
        "answer": r.answer,
        "answered": r.answered,
        "cited": r.cited,
        "invalid_citations": r.invalid_citations,
        "usage": vars(r.usage),
        "estimated_cost_usd": r.estimated_cost_usd,
        "stage_ms": r.timings.stages,
    }


async def run_api(probe: Probe, client: httpx.AsyncClient) -> dict[str, Any]:
    if probe.kind == "agent":
        resp = await client.post("/agent", json={"question": probe.question})
    else:
        resp = await client.post(
            "/query", json={"question": probe.question, "tickers": list(probe.tickers)}
        )
    resp.raise_for_status()
    b = resp.json()
    return {
        "answer": b["answer"],
        "answered": b["answered"],
        "cited": [s["n"] for s in b["sources"] if s["cited"]],
        "invalid_citations": b["invalid_citations"],
        "usage": b["usage"],
        "estimated_cost_usd": b["estimated_cost_usd"],
        "stage_ms": b["latency_ms"],
        "server_ms": b["latency_ms"].get("total"),
        "model": b["model"],
        "retrieval": b["retrieval"],
        **({"tool_calls": b["tool_calls"]} if "tool_calls" in b else {}),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", help="base URL of the deployed API; omit to use local services")
    args = parser.parse_args()
    configure_logging("WARNING")
    settings = get_settings()
    started = datetime.now(UTC)
    rows: list[dict[str, Any]] = []

    services: Any = None
    client: httpx.AsyncClient | None = None
    if args.api:
        proc = await asyncio.create_subprocess_exec(
            "gcloud", "auth", "print-identity-token", stdout=asyncio.subprocess.PIPE
        )
        token = (await proc.communicate())[0].decode().strip()
        client = httpx.AsyncClient(
            base_url=args.api, headers={"Authorization": f"Bearer {token}"}, timeout=120
        )
    else:
        services = await build_services(settings)
    try:
        for probe in PROBES:  # sequential, so latencies aren't inflated by self-contention
            t = time.perf_counter()
            result = await (run_api(probe, client) if client else run_local(probe, services))
            rows.append(
                {
                    "question": probe.question,
                    "kind": probe.kind,
                    "tickers": list(probe.tickers),
                    "expect_refusal": probe.expect_refusal,
                    "refusal_as_expected": None
                    if probe.expect_refusal is None
                    else (not result["answered"]) == probe.expect_refusal,
                    "latency_ms": round((time.perf_counter() - t) * 1000, 1),
                    **result,
                }
            )
            cost = result["estimated_cost_usd"] or 0
            print(
                f"{probe.kind:6} {rows[-1]['latency_ms']:8.0f} ms  ${cost:.5f}  {probe.question[:70]}",
                file=sys.stderr,
            )
    finally:
        if services is not None:
            await services.close()
        if client is not None:
            await client.aclose()

    def summarize(kind: str) -> dict[str, Any]:
        sel = [r for r in rows if r["kind"] == kind]
        lat = [r["latency_ms"] for r in sel]
        costs = [r["estimated_cost_usd"] or 0.0 for r in sel]
        out = {
            "n": len(sel),
            "latency_ms_p50": round(statistics.median(lat), 1),
            "latency_ms_p95": round(pct(lat, 95), 1),
            "latency_ms_max": round(max(lat), 1),
            "mean_cost_usd": round(statistics.mean(costs), 6),
            "cost_per_1k_queries_usd": round(1000 * statistics.mean(costs), 2),
            "refusal_checks_passed": sum(r["refusal_as_expected"] is True for r in sel),
            "refusal_checks_total": sum(r["refusal_as_expected"] is not None for r in sel),
            "invalid_citations_total": sum(len(r["invalid_citations"]) for r in sel),
        }
        server = [r["server_ms"] for r in sel if r.get("server_ms") is not None]
        if server:
            out |= {
                "server_ms_p50": round(statistics.median(server), 1),
                "server_ms_p95": round(pct(server, 95), 1),
            }
        return out

    report = {
        "started_at": started.isoformat(timespec="seconds"),
        "config": (
            {
                "environment": "deployed Cloud Run API (us-central1) -> Cloud SQL + Vertex AI; client: laptop",
                "api": args.api,
                "model": rows[0].get("model"),
                "retrieval": rows[0].get("retrieval"),
            }
            if args.api
            else {
                "environment": "local laptop -> Vertex AI; local Docker pgvector",
                "generation_model": settings.generation_model,
                "genai_location": settings.genai_location,
                "thinking_level": settings.generation_thinking_level,
                "embedding_model": settings.embedding_model,
                "retrieval_backend": str(settings.retrieval_backend),
                "retrieval_mode": settings.retrieval_mode,
                "top_k": settings.retrieval_top_k,
            }
        ),
        "summary": {"direct": summarize("direct"), "agent": summarize("agent")},
        "queries": rows,
    }
    suffix = "_cloud" if args.api else ""
    out = Path("results/smoke") / f"{started.strftime('%Y%m%dT%H%M%SZ')}{suffix}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report["summary"], indent=2), file=sys.stderr)
    print(f"report: {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
