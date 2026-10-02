"""Evaluation runner: golden set -> answers -> LLM judge -> metrics -> regression gate.

    python -m eval.run --target api --url https://...            # deployed API (gates releases)
    python -m eval.run --target local                            # in-process services
    python -m eval.run --target local --retrieval-only --mode vector   # recall only, ~free
    python -m eval.run ... --update-baseline                     # store this run as the baseline

Steps are separate functions (answer_all, judge_all, score) so the Vertex AI pipeline can run
them as separate components. Every run writes results/eval/<UTC ts>_<label>.json. Exit code:
0 = gate passed (or no baseline yet), 1 = regression, 2 = run errors.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from common.config import RetrievalBackend, Settings, get_settings
from common.logging import configure_logging
from common.pricing import generation_cost_usd
from eval.gate import BASELINE_PATH, evaluate_gate, load_baseline
from eval.golden import GOLDEN_DIR, GoldenItem, load
from eval.judge import Judge, Judgment
from eval.metrics import answer_metrics, ops_metrics, retrieval_metrics
from rag.service import QueryOptions
from rag.types import Filters, RetrievedChunk, Timings, Usage

log = logging.getLogger("eval")
RESULTS_DIR = Path("results/eval")
KS = (1, 3, 6)


def _source_dict(n: int, c: RetrievedChunk, cited: bool) -> dict[str, Any]:
    return {
        "n": n,
        "cited": cited,
        "chunk_id": c.chunk_id,
        "ticker": c.ticker,
        "company_name": c.company_name,
        "fiscal_year": c.fiscal_year,
        "item": c.item,
        "score": round(c.score, 6),
        "text": c.content,
    }


def token_audience(url: str) -> str:
    """Cloud Run rejects service-account ID tokens whose audience is a revision *tag* URL
    (`https://candidate---svc-xyz.a.run.app` -> 401), so mint for the service's base URL."""
    scheme, _, rest = url.partition("://")
    host = rest.partition("/")[0]
    if "---" in host:
        host = host.split("---", 1)[1]
    return f"{scheme}://{host}"


async def identity_token(audience: str) -> str:
    """An ID token for calling the IAM-protected API, from whichever source is available:

    1. the metadata server (service account on GCP);
    2. the IAM Credentials API, minting a token for the current service account
       (needs roles/iam.serviceAccountTokenCreator on itself);
    3. the gcloud CLI (a developer laptop).
    """
    import shutil

    errors: list[str] = []
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token

        token: str = await asyncio.to_thread(id_token.fetch_id_token, Request(), audience)
        return token
    except Exception as exc:
        errors.append(f"metadata: {type(exc).__name__}: {exc}"[:300])
    try:
        import google.auth
        from google.auth import impersonated_credentials
        from google.auth.transport.requests import Request

        def mint() -> str:
            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            email = getattr(creds, "service_account_email", None)
            if not email or email == "default":
                creds.refresh(Request())  # type: ignore[no-untyped-call]
                email = getattr(creds, "service_account_email", None)
            if not email or "@" not in email:
                raise RuntimeError(f"no service account email on {type(creds).__name__}")
            target = impersonated_credentials.Credentials(  # type: ignore[no-untyped-call]
                source_credentials=creds,
                target_principal=email,
                target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            idc = impersonated_credentials.IDTokenCredentials(  # type: ignore[no-untyped-call]
                target, target_audience=audience, include_email=True
            )
            idc.refresh(Request())
            return str(idc.token)

        return await asyncio.to_thread(mint)
    except Exception as exc:
        errors.append(f"iamcredentials: {type(exc).__name__}: {exc}"[:300])
    if shutil.which("gcloud"):
        proc = await asyncio.create_subprocess_exec(
            "gcloud", "auth", "print-identity-token", stdout=asyncio.subprocess.PIPE
        )
        return (await proc.communicate())[0].decode().strip()
    errors.append("gcloud: not installed")
    msg = "could not obtain an ID token: " + " | ".join(errors)
    print(msg, file=sys.stderr, flush=True)
    raise RuntimeError(msg)


async def answer_all(
    items: list[GoldenItem],
    *,
    target: str,
    url: str | None = None,
    options: QueryOptions | None = None,
    retrieval_only: bool = False,
    concurrency: int = 2,
    settings: Settings | None = None,
) -> dict[str, dict[str, Any]]:
    options = options or QueryOptions()
    sem = asyncio.Semaphore(concurrency)
    rows: dict[str, dict[str, Any]] = {}

    if target == "local":
        from rag.factory import build_services

        services = await build_services(settings or get_settings())

        async def one(item: GoldenItem) -> None:
            async with sem:
                t = time.perf_counter()
                try:
                    if retrieval_only:
                        chunks = await services.service.retrieve(
                            item.question, options, Usage(), Timings()
                        )
                        rows[item.id] = {
                            "sources": [_source_dict(n, c, False) for n, c in enumerate(chunks, 1)]
                        }
                    else:
                        r = await services.service.answer(item.question, options)
                        cited = set(r.cited)
                        rows[item.id] = {
                            "answer": r.answer,
                            "answered": r.answered,
                            "cited": r.cited,
                            "invalid_citations": r.invalid_citations,
                            "usage": vars(r.usage),
                            "estimated_cost_usd": r.estimated_cost_usd,
                            "server_ms": r.timings.stages.get("total"),
                            "sources": [
                                _source_dict(n, c, n in cited) for n, c in enumerate(r.chunks, 1)
                            ],
                        }
                except Exception as exc:
                    rows[item.id] = {"error": f"{type(exc).__name__}: {exc}"[:300], "sources": []}
                rows[item.id]["client_ms"] = round((time.perf_counter() - t) * 1000, 1)

        try:
            await asyncio.gather(*(one(i) for i in items))
        finally:
            await services.close()
        return rows

    if target != "api" or not url:
        raise ValueError("target must be 'local', or 'api' with a url")
    if retrieval_only:
        raise ValueError("retrieval-only is supported for the local target")
    token = await identity_token(token_audience(url))
    payload_extra: dict[str, Any] = {"include_source_text": True}
    if options.mode:
        payload_extra["mode"] = options.mode
    if options.backend:
        payload_extra["backend"] = str(options.backend)
    if options.top_k:
        payload_extra["top_k"] = options.top_k

    async with httpx.AsyncClient(
        base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=180
    ) as client:

        async def call(item: GoldenItem) -> None:
            async with sem:
                t = time.perf_counter()
                try:
                    b = await _post_with_retry(client, {"question": item.question, **payload_extra})
                    rows[item.id] = {
                        "answer": b["answer"],
                        "answered": b["answered"],
                        "cited": [s["n"] for s in b["sources"] if s["cited"]],
                        "invalid_citations": b["invalid_citations"],
                        "usage": b["usage"],
                        "estimated_cost_usd": b["estimated_cost_usd"],
                        "server_ms": b["latency_ms"].get("total"),
                        "sources": b["sources"],
                        "request_id": b["request_id"],
                        "attempts": b.get("_attempts", 1),
                    }
                except Exception as exc:
                    rows[item.id] = {"error": f"{type(exc).__name__}: {exc}"[:300], "sources": []}
                rows[item.id]["client_ms"] = round((time.perf_counter() - t) * 1000, 1)

        await asyncio.gather(*(call(i) for i in items))
    return rows


TRANSIENT_STATUS = {429, 500, 502, 503, 504}


async def _post_with_retry(
    client: httpx.AsyncClient, body: dict[str, Any], attempts: int = 3
) -> dict[str, Any]:
    """Retry transient upstream failures so one congested minute doesn't masquerade as a quality
    regression. Requests that still fail are counted as errors (an availability metric)."""
    for attempt in range(1, attempts + 1):
        try:
            resp = await client.post("/query", json=body)
            if resp.status_code in TRANSIENT_STATUS and attempt < attempts:
                await asyncio.sleep(5 * attempt)
                continue
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            data["_attempts"] = attempt
            return data
        except httpx.TransportError:
            if attempt == attempts:
                raise
            await asyncio.sleep(5 * attempt)
    raise RuntimeError("unreachable")


async def judge_all(
    items: list[GoldenItem], rows: dict[str, dict[str, Any]], judge: Judge
) -> dict[str, Judgment]:
    todo = [
        i for i in items if i.id in rows and not rows[i.id].get("error") and "answer" in rows[i.id]
    ]
    results = await asyncio.gather(
        *(judge.judge(i, rows[i.id]["answer"], rows[i.id]["sources"]) for i in todo)
    )
    return {j.item_id: j for j in results}


def score(
    items: list[GoldenItem], rows: dict[str, dict[str, Any]], judgments: dict[str, Judgment] | None
) -> dict[str, Any]:
    retrieval = retrieval_metrics(items, rows, KS)
    ranks = retrieval.pop("ranks")
    metrics: dict[str, Any] = {"retrieval": retrieval, "ops": ops_metrics(rows)}
    if judgments is not None:
        metrics["answers"] = answer_metrics(items, rows, judgments)
    metrics["_ranks"] = ranks
    return metrics


def build_report(
    *,
    label: str,
    config: dict[str, Any],
    items: list[GoldenItem],
    rows: dict[str, dict[str, Any]],
    judgments: dict[str, Judgment] | None,
    metrics: dict[str, Any],
    gate: dict[str, Any] | None,
    judge_usage: Usage | None,
    judge_model: str | None,
    started: datetime,
) -> dict[str, Any]:
    ranks = metrics.pop("_ranks", {})
    judge_cost = (
        generation_cost_usd(
            judge_model,
            input_tokens=judge_usage.input_tokens,
            output_tokens=judge_usage.output_tokens,
            thinking_tokens=judge_usage.thinking_tokens,
        )
        if judge_usage and judge_model
        else None
    )
    system_cost = sum((r.get("estimated_cost_usd") or 0.0) for r in rows.values())
    per_item = []
    for i in items:
        r = rows.get(i.id, {})
        per_item.append(
            {
                "id": i.id,
                "category": i.category,
                "question": i.question,
                "answerable": i.answerable,
                "coverage_rank": ranks.get(i.id),
                "answer": r.get("answer"),
                "answered": r.get("answered"),
                "error": r.get("error"),
                "server_ms": r.get("server_ms"),
                "estimated_cost_usd": r.get("estimated_cost_usd"),
                "sources": [
                    {k: s.get(k) for k in ("n", "cited", "chunk_id", "ticker", "item", "score")}
                    for s in r.get("sources", [])
                ],
                "judgment": judgments[i.id].to_dict() if judgments and i.id in judgments else None,
            }
        )
    return {
        "label": label,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "golden_set": "eval/golden_set/golden_v1.jsonl",
        "n_items": len(items),
        "config": config,
        "metrics": metrics,
        "gate": gate,
        "cost_usd": {
            "system_estimated": round(system_cost, 4),
            "judge_estimated": round(judge_cost, 4) if judge_cost else None,
        },
        "judge": {"model": judge_model, "usage": vars(judge_usage) if judge_usage else None},
        "items": per_item,
    }


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


async def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--target", choices=["local", "api"], default="local")
    p.add_argument("--url", help="API base URL (target=api)")
    p.add_argument("--mode", choices=["vector", "hybrid"])
    p.add_argument("--backend", choices=["pgvector", "vertex_vector_search"])
    p.add_argument("--top-k", type=int)
    p.add_argument(
        "--retrieval-only",
        action="store_true",
        help="recall metrics only (no generation, no judge)",
    )
    p.add_argument("--limit", type=int, help="first N items only (smoke-testing the harness)")
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--label", default="")
    p.add_argument("--golden", type=Path, default=GOLDEN_DIR / "golden_v1.jsonl")
    p.add_argument("--update-baseline", action="store_true")
    args = p.parse_args()

    s = get_settings()
    configure_logging("WARNING")
    items = load(args.golden)[: args.limit] if args.limit else load(args.golden)
    options = QueryOptions(
        filters=Filters(),
        top_k=args.top_k,
        mode=args.mode,
        backend=RetrievalBackend(args.backend) if args.backend else None,
    )
    started = datetime.now(UTC)
    rows = await answer_all(
        items,
        target=args.target,
        url=args.url,
        options=options,
        retrieval_only=args.retrieval_only,
        concurrency=args.concurrency,
        settings=s,
    )
    judge = None
    judgments = None
    if not args.retrieval_only:
        judge = Judge(
            project=s.gcp_project_id or "", location=s.genai_location, model=s.judge_model
        )
        judgments = await judge_all(items, rows, judge)
    metrics = score(items, rows, judgments)

    gate_dict = None
    if not args.retrieval_only and not args.limit:
        baseline = load_baseline()
        g = evaluate_gate(metrics, baseline["metrics"] if baseline else None)
        gate_dict = {
            "baseline": str(BASELINE_PATH) if baseline else None,
            "passed": g.passed,
            "failures": g.failures,
            "checks": g.checks,
        }

    label = args.label or "_".join(
        x
        for x in [
            args.target,
            args.backend or "",
            args.mode or "",
            "retrieval" if args.retrieval_only else "",
        ]
        if x
    )
    config = {
        "target": args.target,
        "url": args.url,
        "mode": args.mode or s.retrieval_mode,
        "backend": args.backend,
        "top_k": args.top_k or s.retrieval_top_k,
        "retrieval_only": args.retrieval_only,
        "limit": args.limit,
        "generation_model": s.generation_model,
        "judge_model": s.judge_model if judge else None,
        "vector_weight": s.retrieval_vector_weight,
        "concurrency": args.concurrency,
    }
    report = build_report(
        label=label,
        config=config,
        items=items,
        rows=rows,
        judgments=judgments,
        metrics=metrics,
        gate=gate_dict,
        judge_usage=judge.usage if judge else None,
        judge_model=s.judge_model if judge else None,
        started=started,
    )
    out = RESULTS_DIR / f"{started.strftime('%Y%m%dT%H%M%SZ')}_{label}.json"
    await asyncio.to_thread(_write, out, json.dumps(report, indent=2, default=str))
    if args.update_baseline and report["metrics"]["ops"]["errors"] > 0:
        print("NOT updating the baseline: this run had request errors (re-run it)", file=sys.stderr)
    elif args.update_baseline:
        await asyncio.to_thread(
            _write,
            BASELINE_PATH,
            json.dumps(
                {"source_run": str(out), "config": config, "metrics": report["metrics"]}, indent=2
            ),
        )
        print(f"baseline updated from {out}", file=sys.stderr)

    print(
        json.dumps(
            {
                "metrics": report["metrics"],
                "cost_usd": report["cost_usd"],
                "gate": gate_dict and {k: gate_dict[k] for k in ("passed", "failures")},
            },
            indent=2,
        ),
        file=sys.stderr,
    )
    print(f"report: {out}", file=sys.stderr)
    if report["metrics"]["ops"]["errors"] > 2:
        return 2
    return 0 if (gate_dict is None or gate_dict["passed"]) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
