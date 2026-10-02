import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from eval.gate import evaluate_gate
from eval.golden import GOLDEN_DIR, Evidence, GoldenItem, load, save
from eval.judge import Judge, Judgment
from eval.metrics import answer_metrics, coverage_rank, retrieval_metrics


def src(n: int, ticker: str, text: str) -> dict[str, Any]:
    return {
        "n": n,
        "ticker": ticker,
        "text": text,
        "company_name": ticker,
        "fiscal_year": 2025,
        "item": "7",
    }


NUM = GoldenItem(
    "num-1", "numeric", "Q?", True, "$5", [Evidence("AAPL", "net sales were $5")], ["AAPL"]
)
CMP = GoldenItem(
    "cmp-1",
    "comparison",
    "Q?",
    True,
    "A>B",
    [Evidence("AAPL", "alpha 1"), Evidence("MSFT", "beta 2")],
    ["AAPL", "MSFT"],
)
UNA = GoldenItem("una-1", "unanswerable", "Tesla?", False, "not in corpus", [], [])


def test_coverage_rank_needs_right_company_and_all_evidence() -> None:
    sources = [
        src(1, "MSFT", "Net sales were $5"),
        src(2, "AAPL", "x  NET SALES   were $5 y"),
        src(3, "MSFT", "beta 2"),
    ]
    assert (
        coverage_rank(NUM, sources) == 2
    )  # rank-1 text matches but wrong company; whitespace/case normalised
    assert coverage_rank(CMP, [src(1, "AAPL", "alpha 1")]) is None  # only one side covered
    assert (
        coverage_rank(CMP, [src(1, "MSFT", "beta 2"), src(2, "x", ""), src(3, "AAPL", "alpha 1")])
        == 3
    )


def test_retrieval_metrics() -> None:
    rows: dict[str, dict[str, Any]] = {
        "num-1": {"sources": [src(1, "AAPL", "net sales were $5")]},
        "cmp-1": {"sources": [src(1, "AAPL", "alpha 1"), src(2, "MSFT", "nope")]},
        "una-1": {"sources": []},
    }
    m = retrieval_metrics([NUM, CMP, UNA], rows, (1, 6))
    assert m["n"] == 2 and m["recall@1"] == 0.5 and m["recall@6"] == 0.5 and m["mrr"] == 0.5
    assert m["by_category"]["comparison"]["recall@6"] == 0.0


def test_answer_metrics() -> None:
    rows = {
        i.id: {"answer": "a", "sources": [], "invalid_citations": [7] if i is NUM else []}
        for i in (NUM, CMP, UNA)
    }
    j = {
        "num-1": Judgment("num-1", correctness="correct", faithful=True),
        "cmp-1": Judgment(
            "cmp-1", correctness="partially_correct", faithful=False, unsupported_claims=["x"]
        ),
        "una-1": Judgment("una-1", appropriate_refusal=True, fabricated=False),
    }
    m = answer_metrics([NUM, CMP, UNA], rows, j)
    assert m["correctness_score"] == 0.75 and m["accuracy"] == 0.5
    assert m["refusal_accuracy"] == 1.0 and m["false_refusal_rate"] == 0.0
    assert m["hallucination_rate"] == round(1 / 3, 4)
    assert m["invalid_citations_total"] == 1


def metrics(
    recall: float = 0.84, halluc: float = 0.02, refusal: float = 1.0, errors: int = 0
) -> dict[str, Any]:
    return {
        "retrieval": {"recall@6": recall, "mrr": 0.68},
        "answers": {
            "correctness_score": 0.9,
            "hallucination_rate": halluc,
            "false_refusal_rate": 0.02,
            "refusal_accuracy": refusal,
            "invalid_citations_total": 0,
        },
        "ops": {"errors": errors},
    }


def test_gate_tolerances_and_hard_limits() -> None:
    base = metrics()
    assert evaluate_gate(metrics(recall=0.82), base).passed  # within 0.03
    bad = evaluate_gate(metrics(recall=0.80), base)
    assert not bad.passed and "retrieval.recall@6" in bad.failures[0]
    assert evaluate_gate(metrics(recall=0.99), base).passed  # improvements never fail
    hard = evaluate_gate(
        metrics(halluc=0.12), metrics(halluc=0.11)
    )  # within tolerance, over the hard cap
    assert not hard.passed and any("hard max" in f for f in hard.failures)
    assert not evaluate_gate(metrics(errors=5), base).passed


def test_gate_without_baseline_only_applies_hard_limits() -> None:
    g = evaluate_gate(metrics(), None)
    assert g.passed and all(c["status"] in {"skipped", "pass"} for c in g.checks)


class FakeJudgeClient:
    def __init__(self, payload: dict[str, Any] | Exception) -> None:
        self.payload = payload
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._gen))

    async def _gen(self, *, model: str, contents: str, config: Any) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        assert config.response_mime_type == "application/json"
        return SimpleNamespace(
            text=json.dumps(self.payload),
            usage_metadata=SimpleNamespace(
                prompt_token_count=100, candidates_token_count=20, thoughts_token_count=5
            ),
        )


async def test_judge_answerable_unanswerable_and_errors() -> None:
    ok = Judge(
        project="p",
        location="global",
        model="m",
        client=FakeJudgeClient(
            {
                "correctness": "correct",
                "faithful": False,
                "unsupported_claims": ["made up"],
                "reasoning": "r",
            }
        ),
    )
    j = await ok.judge(NUM, "answer", [src(1, "AAPL", "net sales were $5")])
    assert j.correctness == "correct" and j.hallucinated and ok.usage.input_tokens == 100

    una = Judge(
        project="p",
        location="global",
        model="m",
        client=FakeJudgeClient(
            {
                "appropriate_refusal": True,
                "fabricated": False,
                "unsupported_claims": [],
                "reasoning": "r",
            }
        ),
    )
    ju = await una.judge(UNA, "I could not find this", [])
    assert ju.appropriate_refusal and not ju.hallucinated

    broken = Judge(
        project="p", location="global", model="m", client=FakeJudgeClient(ValueError("boom"))
    )
    jb = await broken.judge(NUM, "a", [])
    assert jb.error and "boom" in jb.error and not jb.hallucinated


def test_golden_set_v1_is_valid() -> None:
    items = load(GOLDEN_DIR / "golden_v1.jsonl")
    assert len(items) == 99
    assert {i.category for i in items} == {"numeric", "narrative", "comparison", "unanswerable"}
    assert all(i.evidence for i in items if i.answerable)


def test_golden_load_rejects_duplicates_and_missing_evidence(tmp_path: Path) -> None:
    p = tmp_path / "g.jsonl"
    save([NUM, NUM], p)
    with pytest.raises(ValueError, match="duplicate"):
        load(p)
    save([GoldenItem("x", "numeric", "q", True, "a", [], [])], p)
    with pytest.raises(ValueError, match="evidence"):
        load(p)


async def test_score_step_reads_artifacts_and_enforces_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from argparse import Namespace

    from eval import steps

    monkeypatch.setattr(steps, "_items", lambda limit: [NUM, UNA])
    rows = {
        "num-1": {
            "answer": "a",
            "sources": [src(1, "AAPL", "net sales were $5")],
            "invalid_citations": [],
        },
        "una-1": {"answer": "I could not find", "sources": [], "invalid_citations": []},
    }
    (tmp_path / "a.json").write_text(
        json.dumps({"started_at": "2026-10-02T12:00:00+00:00", "api_url": "u", "rows": rows})
    )
    (tmp_path / "j.json").write_text(
        json.dumps(
            {
                "model": "m",
                "usage": {"input_tokens": 1},
                "judgments": {
                    "num-1": Judgment("num-1", correctness="correct", faithful=True).to_dict(),
                    "una-1": Judgment(
                        "una-1", appropriate_refusal=False, fabricated=True
                    ).to_dict(),
                },
            }
        )
    )
    args = Namespace(
        answers=str(tmp_path / "a.json"),
        judgments=str(tmp_path / "j.json"),
        out=str(tmp_path / "r.json"),
        label="t",
        results_gcs="",
        enforce_gate="true",
        limit=0,
    )
    monkeypatch.setattr(steps, "load_baseline", lambda: None)
    code = await steps.score_step(args)
    report = json.loads((tmp_path / "r.json").read_text())
    assert report["metrics"]["answers"]["refusal_accuracy"] == 0.0
    assert code == 1  # fabricated answer -> refusal_accuracy below the hard floor -> gate fails


def test_token_audience_strips_revision_tag() -> None:
    from eval.run import token_audience

    assert (
        token_audience("https://candidate---rag-dev-api-abc-uc.a.run.app")
        == "https://rag-dev-api-abc-uc.a.run.app"
    )
    assert (
        token_audience("https://rag-dev-api-abc-uc.a.run.app/")
        == "https://rag-dev-api-abc-uc.a.run.app"
    )
