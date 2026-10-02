"""Retrieval against real Postgres + pgvector, seeded with two synthetic filings."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import date
from pathlib import Path

import asyncpg
import pytest

import ingest.parse as parse
from common.config import RetrievalBackend, Settings
from common.migrate import apply_migrations, discover
from ingest.chunk import chunk_document, embedding_text
from ingest.corpus import CorpusEntry
from ingest.embed import FakeEmbedder
from ingest.pipeline import run_ingest
from ingest.store import PgVectorStore
from rag.retrieval import PgSearch, build_retriever, create_retrieval_pool, retrieve
from rag.types import Filters

FIXTURE = Path(__file__).parent / "fixtures" / "sample_10k.htm"
SCHEMA = "retrieval_test"
AAA = CorpusEntry(
    "AAA",
    1,
    "Alpha Corp",
    "10-K",
    "0000000001-26-000001",
    "a.htm",
    date(2025, 12, 31),
    date(2026, 2, 1),
)


def _bbb() -> CorpusEntry:
    return replace(
        AAA,
        ticker="BBB",
        cik=2,
        company_name="Beta Inc",
        accession="0000000002-26-000001",
        period_end=date(2026, 1, 31),
    )


async def fetch(entry: CorpusEntry) -> tuple[bytes, bool]:
    return FIXTURE.read_bytes(), True


@pytest.fixture
async def seeded(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[PgSearch, FakeEmbedder]]:
    monkeypatch.setattr(parse, "MIN_ITEMS_FOR_SECTIONING", 4)
    monkeypatch.setattr(parse, "_APPENDED_MIN_CHARS", 100)
    dsn = Settings().pg_dsn()
    admin = await asyncpg.connect(dsn)
    await admin.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public")
    await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA}")
    await admin.execute(f"SET search_path TO {SCHEMA}, public")
    await apply_migrations(admin, discover())
    pool = await create_retrieval_pool(dsn, server_settings={"search_path": f"{SCHEMA},public"})
    embedder = FakeEmbedder(dim=768)
    report = await run_ingest(
        [AAA, _bbb()],
        fetch=fetch,
        embedder=embedder,
        store=PgVectorStore(pool),
        embedding_model=embedder.model,
    )
    assert all(d.status == "ingested" for d in report.docs)
    try:
        yield PgSearch(pool), embedder
    finally:
        await pool.close()
        await admin.execute(f"DROP SCHEMA {SCHEMA} CASCADE")
        await admin.close()


def _first_chunk_text(entry: CorpusEntry) -> tuple[str, str]:
    chunks = chunk_document(entry.document_id, parse.parse_10k(FIXTURE.read_bytes()))
    return chunks[1].id, embedding_text(entry, chunks[1])


@pytest.mark.db
async def test_vector_search_finds_exact_chunk(seeded: tuple[PgSearch, FakeEmbedder]) -> None:
    search, embedder = seeded
    chunk_id, text = _first_chunk_text(AAA)
    vector = (await embedder.embed_documents([text])).vectors[0]
    hits = await search.vector(vector, Filters(), k=3)
    assert hits[0].chunk_id == chunk_id
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)


@pytest.mark.db
async def test_filters_apply_inside_the_query(seeded: tuple[PgSearch, FakeEmbedder]) -> None:
    search, embedder = seeded
    _, text = _first_chunk_text(AAA)
    vector = (await embedder.embed_documents([text])).vectors[0]

    only_b = await search.vector(vector, Filters(tickers=("BBB",)), k=5)
    assert only_b and {h.ticker for h in only_b} == {"BBB"}
    assert len(only_b) == 5  # iterative HNSW scan: still k rows despite the filter

    fy = await search.vector(vector, Filters(fiscal_years=(2026,)), k=5)
    assert {h.fiscal_year for h in fy} == {2026}

    risk = await search.keyword("supply chain", Filters(items=("1A",)), k=5)
    assert risk and {h.item for h in risk} == {"1A"}


@pytest.mark.db
async def test_keyword_search_or_semantics_and_stopwords(
    seeded: tuple[PgSearch, FakeEmbedder],
) -> None:
    search, _ = seeded
    hits = await search.keyword("widget demand xyzzy-not-a-word", Filters(), k=5)
    assert any("APPENDED_MDA_MARKER" in h.content for h in hits)  # OR: unknown term doesn't block
    assert await search.keyword("the of and", Filters(), k=5) == []  # stopwords only: no error


@pytest.mark.db
async def test_hybrid_fusion_dedupes_and_respects_top_k(
    seeded: tuple[PgSearch, FakeEmbedder],
) -> None:
    search, embedder = seeded
    _, text = _first_chunk_text(AAA)
    vector = (await embedder.embed_documents([text])).vectors[0]
    retriever = build_retriever(
        backend=RetrievalBackend.PGVECTOR,
        mode="hybrid",
        search=search,
        filters=Filters(),
        top_k=4,
        candidates=10,
    )
    hits = await retrieve(retriever, "widget business", vector)
    ids = [h.chunk_id for h in hits]
    assert len(hits) == 4 and len(set(ids)) == 4
    assert all(h.score >= 0 for h in hits)
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_vertex_backend_not_available_locally() -> None:
    with pytest.raises(NotImplementedError, match="deployed index"):
        build_retriever(
            backend=RetrievalBackend.VERTEX_VECTOR_SEARCH,
            mode="vector",
            search=None,  # type: ignore[arg-type]
            filters=Filters(),
            top_k=3,
            candidates=10,
        )


class FakeMatcher:
    def __init__(self, hits: list[tuple[str, float]]) -> None:
        self.hits = hits
        self.calls: list[tuple[Filters, int]] = []

    async def search(
        self, vector: list[float], filters: Filters, k: int
    ) -> list[tuple[str, float]]:
        self.calls.append((filters, k))
        return self.hits


@pytest.mark.db
async def test_vertex_retriever_hydrates_from_postgres_in_index_order(
    seeded: tuple[PgSearch, FakeEmbedder],
) -> None:
    search, _ = seeded
    ids = [f"{AAA.accession}:3", f"{AAA.accession}:0", "not-in-postgres:9", f"{AAA.accession}:1"]
    matcher = FakeMatcher([(cid, 0.9 - i / 10) for i, cid in enumerate(ids)])
    retriever = build_retriever(
        backend=RetrievalBackend.VERTEX_VECTOR_SEARCH,
        mode="vector",
        search=search,
        filters=Filters(tickers=("AAA",)),
        top_k=4,
        candidates=10,
        matcher=matcher,  # type: ignore[arg-type]
    )
    hits = await retrieve(retriever, "q", [0.0] * 768)
    assert [h.chunk_id for h in hits] == [ids[0], ids[1], ids[3]]  # unknown id dropped, order kept
    assert [round(h.score, 2) for h in hits] == [0.9, 0.8, 0.6]
    assert matcher.calls == [(Filters(tickers=("AAA",)), 4)]
