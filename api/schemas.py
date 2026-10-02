"""Request/response models for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from rag.types import RetrievedChunk, Usage


class QueryRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    tickers: list[str] = Field(default_factory=list, max_length=20)
    fiscal_years: list[int] = Field(default_factory=list, max_length=5)
    items: list[str] = Field(
        default_factory=list, max_length=25, description='10-K Items, e.g. "1A", "7"'
    )
    top_k: int | None = Field(default=None, ge=1, le=20)
    mode: Literal["vector", "hybrid"] | None = None
    backend: Literal["pgvector", "vertex_vector_search"] | None = Field(
        default=None, description="Override the vector backend for this request (A/B comparisons)"
    )
    include_source_text: bool = Field(
        default=False,
        description="Return each source's full text (used by the evaluation pipeline)",
    )


class AgentRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


class Source(BaseModel):
    n: int = Field(
        description="Citation number used in the answer: [n] for /query, [Sn] for /agent"
    )
    cited: bool
    chunk_id: str
    ticker: str
    company_name: str
    fiscal_year: int
    item: str | None
    item_title: str | None
    source_url: str
    score: float
    excerpt: str
    text: str | None = None

    @classmethod
    def from_chunk(
        cls, n: int, chunk: RetrievedChunk, cited: bool, full_text: bool = False
    ) -> Source:
        excerpt = " ".join(chunk.content.split())
        return cls(
            n=n,
            cited=cited,
            chunk_id=chunk.chunk_id,
            ticker=chunk.ticker,
            company_name=chunk.company_name,
            fiscal_year=chunk.fiscal_year,
            item=chunk.item,
            item_title=chunk.item_title,
            source_url=chunk.source_url,
            score=round(chunk.score, 6),
            excerpt=excerpt[:300] + ("…" if len(excerpt) > 300 else ""),
            text=chunk.content if full_text else None,
        )


class UsageOut(BaseModel):
    embedding_tokens: int
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    model_calls: int

    @classmethod
    def from_usage(cls, u: Usage) -> UsageOut:
        return cls(**vars(u))


class QueryResponse(BaseModel):
    request_id: str
    answer: str
    answered: bool = Field(
        description="False when the model said the filings don't contain the answer"
    )
    sources: list[Source]
    invalid_citations: list[int] = Field(description="Cited numbers with no matching source")
    usage: UsageOut
    latency_ms: dict[str, float]
    estimated_cost_usd: float | None
    model: str
    retrieval: dict[str, Any]


class AgentResponse(QueryResponse):
    tool_calls: list[dict[str, Any]]


class Health(BaseModel):
    status: Literal["ok"]
    documents: int
    chunks: int
