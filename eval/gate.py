"""Regression gate: compare a run's metrics with the stored baseline (eval/baseline.json).

Tolerances absorb run-to-run noise. Generation (temperature 1.0) and the judge are both
non-deterministic, and one question is ~1.1 points on the 89 answerable items (10 points on
the 10 unanswerable). A metric fails only if it moves in the bad direction by more than its
tolerance. Hard floors catch absolute problems even when the baseline itself is bad.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASELINE_PATH = Path(__file__).parent / "baseline.json"

# metric path -> (direction, max tolerated move in the bad direction)
TOLERANCES: dict[str, tuple[str, float]] = {
    "retrieval.recall@6": ("higher", 0.03),
    "retrieval.mrr": ("higher", 0.05),
    "answers.correctness_score": ("higher", 0.05),
    "answers.hallucination_rate": ("lower", 0.03),
    "answers.false_refusal_rate": ("lower", 0.05),
    "answers.refusal_accuracy": ("higher", 0.10),
    "answers.invalid_citations_total": ("lower", 2),
}

# Absolute floors/ceilings that apply regardless of the baseline.
HARD_LIMITS: dict[str, tuple[str, float]] = {
    "answers.hallucination_rate": ("max", 0.10),
    "answers.refusal_accuracy": ("min", 0.80),
    "ops.errors": ("max", 2),
}


@dataclass
class GateResult:
    passed: bool
    failures: list[str]
    checks: list[dict[str, Any]]


def _get(metrics: dict[str, Any], path: str) -> float | None:
    node: Any = metrics
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return float(node) if isinstance(node, int | float) else None


def evaluate_gate(current: dict[str, Any], baseline: dict[str, Any] | None) -> GateResult:
    checks, failures = [], []
    for path, (direction, tol) in TOLERANCES.items():
        cur = _get(current, path)
        base = _get(baseline, path) if baseline else None
        if cur is None or base is None:
            checks.append({"metric": path, "current": cur, "baseline": base, "status": "skipped"})
            continue
        delta = cur - base
        bad = -delta if direction == "higher" else delta
        ok = bad <= tol
        checks.append(
            {
                "metric": path,
                "current": cur,
                "baseline": base,
                "delta": round(delta, 4),
                "tolerance": tol,
                "status": "pass" if ok else "fail",
            }
        )
        if not ok:
            failures.append(f"{path}: {cur} vs baseline {base} (worse by {round(bad, 4)} > {tol})")
    for path, (kind, limit) in HARD_LIMITS.items():
        cur = _get(current, path)
        if cur is None:
            continue
        ok = cur <= limit if kind == "max" else cur >= limit
        checks.append(
            {
                "metric": path,
                "current": cur,
                "limit": f"{kind} {limit}",
                "status": "pass" if ok else "fail",
            }
        )
        if not ok:
            failures.append(f"{path}: {cur} breaches hard {kind} {limit}")
    return GateResult(passed=not failures, failures=failures, checks=checks)


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text())
    return data
