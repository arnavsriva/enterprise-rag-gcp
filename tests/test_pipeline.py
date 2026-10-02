"""End-to-end pipeline against real Postgres (fixture filing, fake embedder, no network)."""

from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path

import asyncpg
import pytest

import ingest.parse as parse
from common.config import Settings
from common.migrate import apply_migrations, discover
from ingest.corpus import CorpusEntry
from ingest.embed import EmbedResult, FakeEmbedder
from ingest.pipeline import run_ingest
from ingest.store import PgVectorStore, create_pool
from rag.vector_search import DatapointSpec

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


class FakeIndexWriter:
    def __init__(self, index_id: str) -> None:
        self.index_id = index_id
        self.upserted: list[str] = []
        self.removed: list[str] = []

    async def upsert(self, specs: Sequence[DatapointSpec]) -> int:
        assert all(len(s.vector) == 768 for s in specs)
        self.upserted += [s.chunk_id for s in specs]
        return len(specs)

    async def remove(self, chunk_ids: Sequence[str]) -> int:
        self.removed += list(chunk_ids)
        return len(chunk_ids)


class CountingEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__(dim=768)
        self.calls = 0

    async def embed_documents(self, texts: list[str]) -> EmbedResult:
        self.calls += 1
        return await super().embed_documents(texts)


@pytest.mark.db
async def test_vector_search_sink_sync_and_orphan_removal(
    store: tuple[PgVectorStore, asyncpg.Pool], monkeypatch: pytest.MonkeyPatch
) -> None:
    pg, pool = store
    embedder = CountingEmbedder()

    async def run(writer: FakeIndexWriter | None, **kw: object) -> str:
        report = await run_ingest(
            [ENTRY],
            fetch=fetch_fixture,
            embedder=embedder,
            store=pg,
            embedding_model=embedder.model,
            index_writer=writer,
            **kw,  # type: ignore[arg-type]
        )
        assert report.docs[0].error is None, report.docs[0].error
        return report.docs[0].status

    # 1) Postgres only (as in local Phase 2), then 2) add an index: synced from stored vectors.
    assert await run(None) == "ingested"
    n = await pool.fetchval("SELECT count(*) FROM chunks")
    writer = FakeIndexWriter("idx-1")
    assert await run(writer) == "synced"
    assert embedder.calls == 1  # no re-embedding for the sync
    assert len(writer.upserted) == n
    assert await pool.fetchval("SELECT vector_search_index FROM documents") == "idx-1"

    # 3) Already in sync: skipped. A different index id: synced again.
    assert await run(writer) == "skipped"
    assert await run(FakeIndexWriter("idx-2")) == "synced"

    # 4) Re-chunk into fewer chunks: the now-orphaned datapoints are removed from the index.
    import ingest.chunk as chunk_mod

    monkeypatch.setattr(chunk_mod, "TARGET_CHARS", 100_000)
    monkeypatch.setattr(chunk_mod, "MAX_TOKENS", 100_000)
    monkeypatch.setattr(
        "ingest.pipeline.chunk_document",
        lambda doc_id, sections: chunk_mod.chunk_document(doc_id, sections)[:2],
    )
    shrink = FakeIndexWriter("idx-2")
    assert await run(shrink, force=True) == "ingested"
    assert shrink.upserted == [f"{ENTRY.accession}:0", f"{ENTRY.accession}:1"]
    assert shrink.removed == [f"{ENTRY.accession}:{i}" for i in range(2, n)]


async def test_save_report_uploads_to_gcs_first_and_writes_under_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ingest import run as run_mod

    uploaded: list[str] = []

    async def fake_upload(bucket: str, name: str, data: bytes, content_type: str) -> str:
        uploaded.append(f"gs://{bucket}/{name}")
        return uploaded[-1]

    monkeypatch.setattr("common.gcs.upload", fake_upload)
    report = await run_ingest(
        [ENTRY], fetch=fetch_fixture, embedder=None, store=None, embedding_model="m"
    )
    s = Settings(_env_file=None, gcs_bucket="b", ingest_use_gcs=True, data_dir=tmp_path)  # type: ignore[call-arg]
    locations = await run_mod.save_report(report, s)
    assert locations[0].startswith("gs://b/results/ingest/") and locations[0] == uploaded[0]
    assert Path(locations[1]).parent == tmp_path / "results" / "ingest"
