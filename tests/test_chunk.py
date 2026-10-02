from datetime import date
from itertools import pairwise

from ingest.chunk import (
    Chunk,
    chunk_document,
    context_header,
    embedding_text,
    estimate_tokens,
    split_text,
)
from ingest.corpus import CorpusEntry
from ingest.parse import Section

ENTRY = CorpusEntry(
    ticker="NVDA",
    cik=1045810,
    company_name="NVIDIA Corporation",
    form="10-K",
    accession="0001045810-26-000021",
    primary_document="nvda-20260125.htm",
    period_end=date(2026, 1, 25),
    filed=date(2026, 2, 25),
)


def paragraphs(n: int, size: int = 500) -> str:
    return "\n".join(f"P{i} " + "w" * (size - 4) for i in range(n))


def test_chunks_respect_target_size() -> None:
    chunks = split_text(paragraphs(30), target=3000, overlap=400)
    assert len(chunks) > 1
    assert all(len(c) <= 3000 for c in chunks)


def test_consecutive_chunks_overlap() -> None:
    chunks = split_text(paragraphs(30), target=3000, overlap=600)
    for prev, nxt in pairwise(chunks):
        assert prev.split("\n")[-1] == nxt.split("\n")[0]


def test_long_paragraph_split_on_sentences() -> None:
    para = " ".join(f"Sentence number {i} is here." for i in range(400))
    chunks = split_text(para, target=1000, overlap=0)
    assert all(len(c) <= 1000 for c in chunks)
    assert all(c.rstrip().endswith(".") for c in chunks)


def test_unbreakable_text_hard_wrapped() -> None:
    chunks = split_text("x" * 7000, target=3000, overlap=0)
    assert [len(c) for c in chunks] == [3000, 3000, 1000]


def test_number_dense_tables_respect_token_cap() -> None:
    # Financial table rows: digits tokenise individually, so chars alone is not a safe limit.
    rows = "\n".join(
        f"Line item {i} | $1,234,567 | (98,765) | 12.3% | 4,567,890" for i in range(200)
    )
    chunks = split_text(rows, target=3000, overlap=400, max_tokens=1200)
    assert all(estimate_tokens(c) <= 1200 for c in chunks)
    assert all(len(c) <= 3000 for c in chunks)
    assert max(len(c) for c in chunks) < 3000  # the token cap, not the char target, binds


def test_estimator_counts_digits_individually() -> None:
    assert estimate_tokens("1234567890") == 10
    assert estimate_tokens("revenue") == 2  # 1 word * 1.3, rounded up
    assert estimate_tokens("") == 1


def test_small_text_single_chunk_and_empty_text() -> None:
    assert split_text("short", target=3000, overlap=400) == ["short"]
    assert split_text("   \n ") == []


def test_document_indices_are_sequential_across_sections() -> None:
    sections = [
        Section("1", "Business", paragraphs(10)),
        Section("1A", "Risk Factors", paragraphs(10)),
    ]
    chunks = chunk_document(ENTRY.document_id, sections)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert chunks[0].id == f"{ENTRY.accession}:0"
    assert {c.item for c in chunks} == {"1", "1A"}


def test_context_header_identifies_company_year_and_item() -> None:
    chunk = Chunk(ENTRY.document_id, 0, "1A", "Risk Factors", "Supply is constrained.")
    header = context_header(ENTRY, chunk)
    assert "NVIDIA Corporation (NVDA)" in header
    assert "fiscal year 2026" in header
    assert "Item 1A. Risk Factors" in header
    assert embedding_text(ENTRY, chunk).endswith("\n\nSupply is constrained.")
