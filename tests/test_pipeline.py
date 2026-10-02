"""End-to-end pipeline against real Postgres (fixture filing, fake embedder, no network)."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import date
from pathlib import Path

import asyncpg
import pytest

import ingest.parse as parse
from common.config import Settings
from common.migrate import apply_migrations, discover
from ingest.corpus import CorpusEntry
from ingest.embed import FakeEmbedder
from ingest.pipeline import run_ingest
from ingest.store import PgVectorStore, create_pool

FIXTURE = Path(__file__).parent / "fixtures" / "sample_10k.htm"
SCHEMA = "pipeline_test"
ENTRY = CorpusEntry(
    "SMPL",
    42,
    "Sample Corp",
    "10-K",
    "0000000042-26-000001",
    "s.htm",
    date(2025, 12, 31),
    date(2026, 2, 1),
)


async def fetch_fixture(entry: CorpusEntry) -> tuple[bytes, bool]:
    return FIXTURE.read_bytes(), True


@pytest.fixture
async def store(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[PgVectorStore, asyncpg.Pool]]:
    monkeypatch.setattr(parse, "MIN_ITEMS_FOR_SECTIONING", 4)
    dsn = Settings().pg_dsn()
    admin = await asyncpg.connect(dsn)
    await admin.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public")
    await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA}")
    await admin.execute(f"SET search_path TO {SCHEMA}, public")
    await apply_migrations(admin, discover())
    pool = await create_pool(dsn, server_settings={"search_path": f"{SCHEMA},public"})
    try:
        yield PgVectorStore(pool), pool
    finally:
        await pool.close()
        await admin.execute(f"DROP SCHEMA {SCHEMA} CASCADE")
        await admin.close()


@pytest.mark.db
async def test_ingest_then_skip_then_force(store: tuple[PgVectorStore, asyncpg.Pool]) -> None:
    pg, pool = store
    embedder = FakeEmbedder(dim=768)
    kwargs = {
        "fetch": fetch_fixture,
        "embedder": embedder,
        "store": pg,
        "embedding_model": embedder.model,
    }

    first = await run_ingest([ENTRY], **kwargs)  # type: ignore[arg-type]
    assert first.docs[0].status == "ingested", first.docs[0].error
    n = first.docs[0].chunks
    assert n > 0
    assert await pool.fetchval("SELECT count(*) FROM chunks") == n
    doc = await pool.fetchrow(
        "SELECT fiscal_year, chunk_count, embedding_model, period_end FROM documents"
    )
    assert tuple(doc) == (2025, n, "fake-embedding", date(2025, 12, 31))

    # Nearest neighbour of a stored chunk's own vector is itself (pgvector index + codec work).
    row = await pool.fetchrow("SELECT id, embedding FROM chunks ORDER BY chunk_index LIMIT 1")
    nearest = await pool.fetchval(
        "SELECT id FROM chunks ORDER BY embedding <=> $1 LIMIT 1", row["embedding"]
    )
    assert nearest == row["id"]

    second = await run_ingest([ENTRY], **kwargs)  # type: ignore[arg-type]
    assert second.docs[0].status == "skipped"

    forced = await run_ingest([ENTRY], force=True, **kwargs)  # type: ignore[arg-type]
    assert forced.docs[0].status == "ingested"
    assert await pool.fetchval("SELECT count(*) FROM chunks") == n  # replaced, not duplicated


@pytest.mark.db
async def test_one_failure_does_not_abort_run(store: tuple[PgVectorStore, asyncpg.Pool]) -> None:
    pg, _ = store
    broken = replace(ENTRY, ticker="BRKN", accession="0000000099-26-000001")

    async def fetch(entry: CorpusEntry) -> tuple[bytes, bool]:
        if entry.ticker == "BRKN":
            raise ConnectionError("EDGAR unreachable")
        return await fetch_fixture(entry)

    embedder = FakeEmbedder(dim=768)
    report = await run_ingest(
        [ENTRY, broken], fetch=fetch, embedder=embedder, store=pg, embedding_model=embedder.model
    )
    statuses = {d.ticker: d.status for d in report.docs}
    assert statuses == {"SMPL": "ingested", "BRKN": "failed"}
    assert report.summary()["docs_failed"] == 1
    assert "EDGAR unreachable" in (report.docs[1].error or "")


async def test_dry_run_estimates_without_embedder_or_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parse, "MIN_ITEMS_FOR_SECTIONING", 4)
    report = await run_ingest(
        [ENTRY],
        fetch=fetch_fixture,
        embedder=None,
        store=None,
        embedding_model="gemini-embedding-001",
    )
    doc = report.docs[0]
    assert (report.mode, doc.status, doc.tokens_estimated) == ("dry_run", "dry_run", True)
    assert doc.chunks > 0 and doc.tokens > 0
    assert report.summary()["estimated_cost_usd"] is not None
