"""Shared data types for retrieval and generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Filters:
    """Metadata filters applied inside the vector/keyword queries (not after them)."""

    tickers: tuple[str, ...] = ()
    fiscal_years: tuple[int, ...] = ()
    items: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    document_id: str
    ticker: str
    company_name: str
    fiscal_year: int
    period_end: date | None
    item: str | None
    item_title: str | None
    content: str
    source_url: str
    score: float  # cosine similarity (vector) / ts_rank_cd (keyword) / weighted fused score

    @property
    def label(self) -> str:
        where = f"Item {self.item}. {self.item_title}" if self.item else (self.item_title or "")
        period = f" (period ended {self.period_end.isoformat()})" if self.period_end else ""
        return f"{self.company_name} ({self.ticker}) | 10-K FY{self.fiscal_year}{period} | {where}"


@dataclass
class Usage:
    embedding_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    model_calls: int = 0

    def add(self, other: Usage) -> None:
        self.embedding_tokens += other.embedding_tokens
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.thinking_tokens += other.thinking_tokens
        self.model_calls += other.model_calls


@dataclass
class Timings:
    """Milliseconds per stage."""

    stages: dict[str, float] = field(default_factory=dict)

    def record(self, stage: str, ms: float) -> None:
        self.stages[stage] = round(self.stages.get(stage, 0.0) + ms, 1)
