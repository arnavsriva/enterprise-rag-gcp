"""Section text -> overlapping chunks, plus the context header used for embedding.

Chunks pack whole paragraphs (and table rows) up to BOTH a character target and an
estimated-token cap; oversized paragraphs are split on sentence boundaries, and only a single
oversized sentence is hard-wrapped.

Why a token cap: the embedding model rejects inputs over 2,048 tokens, and chars/4 badly
underestimates number-dense financial tables (the tokenizer splits digits individually; one
3,000-char GS table chunk was 2,114 tokens). `estimate_tokens` was calibrated on 1,366 real
API token counts from this corpus: worst case real/estimate = 1.21 on large chunks, so a
1,200-token cap keeps real counts around 1,500 (+ ~45 for the context header).

Bump CHUNKER_VERSION whenever the output could change: it forces re-chunking on next ingest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ingest.corpus import CorpusEntry
from ingest.parse import Section

CHUNKER_VERSION = "v2-c3000-t1200-o400"
TARGET_CHARS = 3000
MAX_TOKENS = 1200
OVERLAP_CHARS = 400

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+(?=[A-Z0-9(\"\u201c])")
_DIGIT = re.compile(r"\d")
_WORD = re.compile(r"[^\W\d_]+")
_PUNCT = re.compile(r"[^\w\s]")


@dataclass(frozen=True)
class Chunk:
    document_id: str
    chunk_index: int
    item: str | None
    item_title: str
    content: str

    @property
    def id(self) -> str:
        return f"{self.document_id}:{self.chunk_index}"


def estimate_tokens(text: str) -> int:
    """Digits count individually, words ~1.3 tokens, punctuation 1 each (see module docstring)."""
    words = len(_WORD.findall(text))
    return max(1, len(_DIGIT.findall(text)) + (words * 13 + 9) // 10 + len(_PUNCT.findall(text)))


def _fits(text: str, max_chars: int, max_tokens: int) -> bool:
    return len(text) <= max_chars and estimate_tokens(text) <= max_tokens


def _hard_wrap(sentence: str, max_chars: int, max_tokens: int) -> list[str]:
    out = []
    while not _fits(sentence, max_chars, max_tokens):
        cut = min(max_chars, len(sentence))
        while cut > 1 and estimate_tokens(sentence[:cut]) > max_tokens:
            cut = int(cut * 0.8)
        space = sentence.rfind(" ", 0, cut)
        cut = space if space > cut // 2 else cut
        out.append(sentence[:cut])
        sentence = sentence[cut:].lstrip()
    if sentence:
        out.append(sentence)
    return out


def _pieces(text: str, max_chars: int, max_tokens: int) -> list[str]:
    """Paragraphs; any that is too big is broken into sentences, then hard-wrapped."""
    out: list[str] = []
    for para in text.split("\n"):
        if _fits(para, max_chars, max_tokens):
            out.append(para)
            continue
        for sentence in _SENTENCE_SPLIT.split(para):
            out += _hard_wrap(sentence, max_chars, max_tokens)
    return out


def split_text(
    text: str,
    target: int = TARGET_CHARS,
    overlap: int = OVERLAP_CHARS,
    max_tokens: int = MAX_TOKENS,
) -> list[str]:
    """Greedy-pack pieces within `target` chars and `max_tokens`; each new chunk repeats up
    to `overlap` trailing chars of the previous one (dropped if it would break the limits)."""
    if not text.strip():
        return []
    chunks: list[str] = []
    current: list[str] = []
    size = tokens = 0
    for piece in _pieces(text, target, max_tokens):
        piece_tokens = estimate_tokens(piece)
        if current and (size + len(piece) + 1 > target or tokens + piece_tokens > max_tokens):
            chunks.append("\n".join(current))
            carry: list[str] = []
            carried = 0
            for prev in reversed(current):
                if carried + len(prev) > overlap:
                    break
                carry.insert(0, prev)
                carried += len(prev) + 1
            carry_tokens = sum(estimate_tokens(c) for c in carry)
            if carried + len(piece) + 1 > target or carry_tokens + piece_tokens > max_tokens:
                carry, carried, carry_tokens = [], 0, 0
            current, size, tokens = carry, carried, carry_tokens
        current.append(piece)
        size += len(piece) + 1
        tokens += piece_tokens
    if current and (not chunks or size > overlap):
        chunks.append("\n".join(current))
    return chunks


def chunk_document(document_id: str, sections: list[Section]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for section in sections:
        for text in split_text(section.text):
            chunks.append(Chunk(document_id, len(chunks), section.item, section.title, text))
    return chunks


def context_header(entry: CorpusEntry, chunk: Chunk) -> str:
    """Prepended at embedding time so each vector knows which company/year/section it is from.

    Without it, a chunk saying "revenue grew 6%" is indistinguishable across 20 companies.
    """
    where = f"Item {chunk.item}. {chunk.item_title}" if chunk.item else chunk.item_title
    return (
        f"{entry.company_name} ({entry.ticker}) Form {entry.form}, fiscal year "
        f"{entry.fiscal_year} (period ended {entry.period_end.isoformat()}). {where}."
    )


def embedding_text(entry: CorpusEntry, chunk: Chunk) -> str:
    return f"{context_header(entry, chunk)}\n\n{chunk.content}"
