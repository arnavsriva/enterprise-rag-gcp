"""Ingestion pipeline: fetch -> parse -> chunk -> embed -> store, per filing, concurrently.

Concurrency is bounded at every external boundary: SEC requests by a rate limiter (inside
EdgarClient), embedding calls by a semaphore (inside the embedder), and filings in flight by
`max_concurrency` here. CPU-heavy HTML parsing runs in a worker thread so it doesn't block
the event loop. One failing filing is reported and skipped; it never aborts the run.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from common.pricing import embedding_cost_usd
from ingest.chunk import CHUNKER_VERSION, chunk_document, embedding_text, estimate_tokens
from ingest.corpus import CorpusEntry
from ingest.embed import Embedder
from ingest.parse import parse_10k
from ingest.store import PgVectorStore

log = logging.getLogger(__name__)

Fetcher = Callable[[CorpusEntry], Awaitable[tuple[bytes, bool]]]
Status = Literal["ingested", "skipped", "dry_run", "failed"]


@dataclass
class DocReport:
    ticker: str
    document_id: str
    fiscal_year: int
    status: Status
    from_cache: bool = False
    items: list[str] = field(default_factory=list)
    chunks: int = 0
    chars: int = 0
    tokens: int = 0  # real (API) when embedded; estimated in dry runs
    tokens_estimated: bool = False
    seconds: dict[str, float] = field(default_factory=dict)
    error: str | None = None


@dataclass
class RunReport:
    mode: Literal["ingest", "dry_run"]
    embedding_model: str
    chunker_version: str
    started_at: str
    finished_at: str = ""
    wall_seconds: float = 0.0
    docs: list[DocReport] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        done = [d for d in self.docs if d.status in {"ingested", "dry_run"}]
        minutes = self.wall_seconds / 60 if self.wall_seconds else 0
        tokens = sum(d.tokens for d in done)
        chars = sum(d.chars for d in done)
        cost = embedding_cost_usd(self.embedding_model, tokens=tokens, chars=chars)
        stage_totals: dict[str, float] = {}
        for d in done:
            for stage, secs in d.seconds.items():
                stage_totals[stage] = round(stage_totals.get(stage, 0.0) + secs, 2)
        return {
            "docs_total": len(self.docs),
            "docs_processed": len(done),
            "docs_skipped": sum(d.status == "skipped" for d in self.docs),
            "docs_failed": sum(d.status == "failed" for d in self.docs),
            "chunks": sum(d.chunks for d in done),
            "chars_embedded": chars,
            "tokens": tokens,
            "tokens_estimated": any(d.tokens_estimated for d in done),
            "estimated_cost_usd": round(cost, 6) if cost is not None else None,
            "wall_seconds": round(self.wall_seconds, 2),
            "docs_per_min": round(len(done) / minutes, 2) if minutes else None,
            "chunks_per_min": round(sum(d.chunks for d in done) / minutes, 1) if minutes else None,
            "stage_seconds_total": stage_totals,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "summary": self.summary()}


async def _process(
    entry: CorpusEntry,
    *,
    fetch: Fetcher,
    embedder: Embedder | None,
    store: PgVectorStore | None,
    force: bool,
) -> DocReport:
    report = DocReport(entry.ticker, entry.document_id, entry.fiscal_year, status="failed")
    timings: dict[str, float] = {}
    t = time.monotonic()
    try:
        raw, report.from_cache = await fetch(entry)
        sha = hashlib.sha256(raw).hexdigest()
        timings["fetch"] = time.monotonic() - t

        if store is not None and embedder is not None and not force:
            state = await store.get_state(entry.document_id)
            if state and (state.content_sha256, state.chunker_version, state.embedding_model) == (
                sha,
                CHUNKER_VERSION,
                embedder.model,
            ):
                report.status = "skipped"
                return report

        t = time.monotonic()
        sections = await asyncio.to_thread(parse_10k, raw)
        chunks = chunk_document(entry.document_id, sections)
        texts = [embedding_text(entry, c) for c in chunks]
        timings["parse_chunk"] = time.monotonic() - t
        report.items = sorted({s.item for s in sections if s.item}, key=_item_sort_key)
        report.chunks = len(chunks)
        report.chars = sum(len(x) for x in texts)

        if embedder is None or store is None:
            report.status = "dry_run"
            report.tokens = sum(estimate_tokens(x) for x in texts)
            report.tokens_estimated = True
            return report

        t = time.monotonic()
        result = await embedder.embed_documents(texts)
        timings["embed"] = time.monotonic() - t
        report.tokens = sum(result.token_counts)
        if result.truncated:
            log.warning(
                "inputs truncated", extra={"ticker": entry.ticker, "count": result.truncated}
            )

        t = time.monotonic()
        await store.replace_document(
            entry,
            content_sha256=sha,
            chunker_version=CHUNKER_VERSION,
            embedding_model=embedder.model,
            chunks=chunks,
            vectors=result.vectors,
            token_counts=result.token_counts,
        )
        timings["store"] = time.monotonic() - t
        report.status = "ingested"
        return report
    except Exception as exc:
        report.error = f"{type(exc).__name__}: {exc}"
        log.exception("filing failed", extra={"ticker": entry.ticker})
        return report
    finally:
        report.seconds = {k: round(v, 3) for k, v in timings.items()}
        log.info(
            "filing done",
            extra={
                "ticker": entry.ticker,
                "status": report.status,
                "chunks": report.chunks,
                "tokens": report.tokens,
                "seconds": report.seconds,
            },
        )


def _item_sort_key(item: str) -> tuple[int, str]:
    digits = "".join(ch for ch in item if ch.isdigit())
    return int(digits), item


async def run_ingest(
    entries: list[CorpusEntry],
    *,
    fetch: Fetcher,
    embedder: Embedder | None,
    store: PgVectorStore | None,
    embedding_model: str,
    max_concurrency: int = 8,
    force: bool = False,
) -> RunReport:
    dry = embedder is None or store is None
    report = RunReport(
        mode="dry_run" if dry else "ingest",
        embedding_model=embedding_model,
        chunker_version=CHUNKER_VERSION,
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    semaphore = asyncio.Semaphore(max_concurrency)

    async def bounded(entry: CorpusEntry) -> DocReport:
        async with semaphore:
            return await _process(entry, fetch=fetch, embedder=embedder, store=store, force=force)

    t = time.monotonic()
    report.docs = list(await asyncio.gather(*(bounded(e) for e in entries)))
    report.wall_seconds = time.monotonic() - t
    report.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
    log.info("ingest run complete", extra={"mode": report.mode, **report.summary()})
    return report
