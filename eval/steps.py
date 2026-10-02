"""Pipeline step entrypoints (one per Vertex AI Pipelines component), reusing eval.run.

    python -m eval.steps answer --api-url URL --out answers.json [--mode M] [--backend B] [--limit N]
    python -m eval.steps judge  --answers answers.json --out judgments.json --project P --location L --model M
    python -m eval.steps score  --answers answers.json --judgments judgments.json --out report.json
                                [--results-gcs gs://bucket/results/eval] [--enforce-gate true]

The score step exits 1 when the regression gate fails (and --enforce-gate is true), which fails
the pipeline run, so a release does not get promoted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common import gcs
from common.config import RetrievalBackend
from common.logging import configure_logging
from eval.gate import BASELINE_PATH, evaluate_gate, load_baseline
from eval.golden import GOLDEN_DIR, GoldenItem, load
from eval.judge import Judge, Judgment
from eval.run import answer_all, build_report, judge_all, score
from rag.service import QueryOptions
from rag.types import Filters, Usage


def _items(limit: int) -> list[GoldenItem]:
    items = load(GOLDEN_DIR / "golden_v1.jsonl")
    return items[:limit] if limit else items


def _write_sync(path: str, data: Any) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, default=str))


async def _write(path: str, data: Any) -> None:
    await asyncio.to_thread(_write_sync, path, data)


async def _read(path: str) -> Any:
    return json.loads(await asyncio.to_thread(Path(path).read_text))


def _judgment_from_dict(d: dict[str, Any]) -> Judgment:
    return Judgment(**{k: v for k, v in d.items() if k != "hallucinated"})


async def answer(args: argparse.Namespace) -> int:
    options = QueryOptions(
        filters=Filters(),
        mode=args.mode or None,
        backend=RetrievalBackend(args.backend) if args.backend else None,
    )
    started = datetime.now(UTC).isoformat(timespec="seconds")
    rows = await answer_all(_items(args.limit), target="api", url=args.api_url, options=options)
    await _write(
        args.out,
        {
            "started_at": started,
            "api_url": args.api_url,
            "mode": args.mode,
            "backend": args.backend,
            "rows": rows,
        },
    )
    errors = sum(1 for r in rows.values() if r.get("error"))
    print(f"answered {len(rows)} questions, {errors} errors", file=sys.stderr)
    return 0


async def judge(args: argparse.Namespace) -> int:
    data = await _read(args.answers)
    j = Judge(project=args.project, location=args.location, model=args.model)
    judgments = await judge_all(_items(args.limit), data["rows"], j)
    await _write(
        args.out,
        {
            "model": args.model,
            "usage": vars(j.usage),
            "judgments": {k: v.to_dict() for k, v in judgments.items()},
        },
    )
    print(f"judged {len(judgments)} answers", file=sys.stderr)
    return 0


async def score_step(args: argparse.Namespace) -> int:
    items = _items(args.limit)
    a = await _read(args.answers)
    jdata = await _read(args.judgments)
    judgments = {k: _judgment_from_dict(v) for k, v in jdata["judgments"].items()}
    metrics = score(items, a["rows"], judgments)
    baseline = load_baseline()
    g = evaluate_gate(metrics, baseline["metrics"] if baseline else None)
    gate = {
        "baseline": str(BASELINE_PATH) if baseline else None,
        "passed": g.passed,
        "failures": g.failures,
        "checks": g.checks,
    }
    report = build_report(
        label=args.label,
        items=items,
        rows=a["rows"],
        judgments=judgments,
        metrics=metrics,
        gate=gate,
        config={
            "target": "api",
            "url": a["api_url"],
            "mode": a.get("mode"),
            "backend": a.get("backend"),
            "limit": args.limit or None,
            "runner": "vertex-ai-pipelines",
            "judge_model": jdata["model"],
        },
        judge_usage=Usage(**jdata["usage"]),
        judge_model=jdata["model"],
        started=datetime.fromisoformat(a["started_at"]),
    )
    await _write(args.out, report)
    if args.results_gcs:
        bucket, _, prefix = args.results_gcs.removeprefix("gs://").partition("/")
        name = f"{prefix.rstrip('/')}/{a['started_at'].replace(':', '').replace('-', '').replace('+0000', 'Z')}_{args.label}.json"
        print(
            "uploaded",
            await gcs.upload(
                bucket, name, json.dumps(report, indent=2, default=str).encode(), "application/json"
            ),
            file=sys.stderr,
        )
    print(json.dumps({"gate": {k: gate[k] for k in ("passed", "failures")}}), file=sys.stderr)
    enforce = args.enforce_gate.lower() in {"1", "true", "yes"}
    return 1 if (enforce and not g.passed) else 0


def main() -> None:
    configure_logging("WARNING")
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="step", required=True)
    a = sub.add_parser("answer")
    a.add_argument("--api-url", required=True)
    a.add_argument("--out", required=True)
    a.add_argument("--mode", default="")
    a.add_argument("--backend", default="")
    a.add_argument("--limit", type=int, default=0)
    j = sub.add_parser("judge")
    j.add_argument("--answers", required=True)
    j.add_argument("--out", required=True)
    j.add_argument("--project", required=True)
    j.add_argument("--location", default="global")
    j.add_argument("--model", required=True)
    j.add_argument("--limit", type=int, default=0)
    s = sub.add_parser("score")
    s.add_argument("--answers", required=True)
    s.add_argument("--judgments", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--label", default="pipeline")
    s.add_argument("--results-gcs", default="")
    s.add_argument("--enforce-gate", default="true")
    s.add_argument("--limit", type=int, default=0)
    args = p.parse_args()
    fn = {"answer": answer, "judge": judge, "score": score_step}[args.step]
    sys.exit(asyncio.run(fn(args)))


if __name__ == "__main__":
    main()
