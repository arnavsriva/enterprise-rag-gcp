"""Golden set schema and loader.

Relevance is labelled by *evidence strings*, not chunk IDs: each answerable question lists, per
company, a short verbatim quote from the filing that contains the answer. A retrieved chunk is
relevant if it belongs to that company and contains the quote (whitespace/case-normalised).
This survives re-chunking and lets both vector backends be scored identically.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

Category = Literal["numeric", "narrative", "comparison", "unanswerable"]

GOLDEN_DIR = Path(__file__).parent / "golden_set"


@dataclass(frozen=True)
class Evidence:
    ticker: str
    text: str  # verbatim substring of the filing
    item: str | None = None  # where it was found (informational; not required for relevance)


@dataclass(frozen=True)
class GoldenItem:
    id: str
    category: Category
    question: str
    answerable: bool
    reference_answer: str  # for unanswerable: what a correct reply should say
    evidence: list[Evidence] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)  # companies the question is about
    notes: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def contains_evidence(chunk_text: str, evidence_text: str) -> bool:
    return normalise(evidence_text) in normalise(chunk_text)


def load(path: Path) -> list[GoldenItem]:
    items = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        raw["evidence"] = [Evidence(**e) for e in raw.get("evidence", [])]
        items.append(GoldenItem(**raw))
    ids = [i.id for i in items]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate ids in {path}")
    for i in items:
        if i.answerable and not i.evidence:
            raise ValueError(f"{i.id}: answerable items need evidence")
    return items


def save(items: list[GoldenItem], path: Path) -> None:
    path.write_text("\n".join(i.to_json() for i in items) + "\n")
