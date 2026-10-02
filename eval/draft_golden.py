"""Draft the golden set from the ingested corpus (for HUMAN REVIEW before use).

    python -m eval.draft_golden          # -> eval/golden_set/golden_v1.draft.jsonl + REVIEW.md

Per company: 3 numeric/factual questions (Items 7/8) + 1 narrative question (Items 1/1A), from
randomly sampled chunks (fixed seed). Plus 10 two-company comparisons and 10 hand-written
unanswerable questions. An LLM (the judge model, not the generator under test) proposes each
question with a reference answer and a verbatim evidence quote; candidates are rejected unless
the quote is literally in the chunk, the question doesn't just copy the quote, and evidence is
unique. Known bias: generated questions can echo source wording and inflate recall, which is
why they must paraphrase and why a human reviews the draft.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
from google.genai import types

from common import genai as genai_helpers
from common.config import Settings, get_settings
from common.logging import configure_logging
from common.pricing import generation_cost_usd
from eval.golden import GOLDEN_DIR, Evidence, GoldenItem, contains_evidence, normalise, save
from ingest.corpus import load_corpus
from ingest.embed import VertexEmbedder
from rag.retrieval import PgSearch, create_retrieval_pool
from rag.types import Filters

log = logging.getLogger("eval.draft")

SEED = 20261002
NUMERIC_PER_COMPANY = 3
NARRATIVE_PER_COMPANY = 1

COMPARISONS: list[tuple[str, str, str]] = [
    ("AAPL", "MSFT", "total revenue or net sales for the fiscal year"),
    ("JPM", "BAC", "net interest income for the year"),
    ("XOM", "CVX", "net income attributable to the company for the year"),
    ("KO", "PG", "net operating revenues or net sales for the year"),
    ("PFE", "JNJ", "research and development expense for the year"),
    ("WMT", "AMZN", "total revenues or net sales for the fiscal year"),
    ("GOOGL", "META", "advertising revenue for the year"),
    ("CAT", "BA", "total revenues for the year"),
    ("NVDA", "MSFT", "research and development expense for the fiscal year"),
    ("GS", "JPM", "total net revenues for the year"),
]

UNANSWERABLE: list[tuple[str, list[str], str]] = [
    (
        "What was Tesla's total revenue in fiscal 2025?",
        [],
        "Tesla is not in the corpus; the system should say it cannot find this.",
    ),
    ("How much did Netflix spend on content in 2025?", [], "Netflix is not in the corpus."),
    (
        "What was Apple's net income in fiscal 2018?",
        ["AAPL"],
        "FY2018 is outside the corpus (only the latest 10-K, with a few comparative years, is indexed).",
    ),
    (
        "What were Microsoft's total revenues in fiscal 2015?",
        ["MSFT"],
        "FY2015 is outside the corpus.",
    ),
    ("What is the name of Jamie Dimon's dog?", ["JPM"], "Personal trivia not in a 10-K."),
    (
        "What will Coca-Cola's revenue be in fiscal 2030?",
        ["KO"],
        "A forecast not stated in the filing; must not speculate.",
    ),
    (
        "What was Boeing's share price at the close on March 3, 2026?",
        ["BA"],
        "Daily share prices are not in the 10-K.",
    ),
    (
        "How many Starbucks stores does Walmart operate inside its supercenters?",
        ["WMT"],
        "Not reported in the filing.",
    ),
    (
        "What was NVIDIA's revenue from selling cars in fiscal 2026?",
        ["NVDA"],
        "NVIDIA does not report car sales; must not invent a figure.",
    ),
    (
        "What did Chevron's CEO eat for breakfast at the 2025 annual meeting?",
        ["CVX"],
        "Irrelevant trivia not in the filing.",
    ),
]

SINGLE_PROMPT = """You write evaluation questions for a question-answering system over SEC 10-K filings.

Source: {company} ({ticker}), Form 10-K, fiscal year {fy}, {where}.
Excerpt:
<<<
{text}
>>>

Write ONE {kind} question that a financial analyst might ask and that this excerpt fully answers.
Rules:
- Name the company and the fiscal year in the question.
- Paraphrase: do not copy distinctive phrases from the excerpt into the question.
- {kind_rule}
- reference_answer: concise and complete; include units only if the excerpt states them.
- evidence: copy EXACTLY (character for character) a span of 10-160 characters from the excerpt
  that contains the key fact. Do not fix typos, spacing or punctuation.
- If the excerpt has no clear, self-contained answerable fact, set skip=true."""

KIND_RULES = {
    "numeric": "Ask for a specific figure (amount, percentage or count) stated in the excerpt.",
    "narrative": "Ask about a described risk, strategy, business fact or policy (not a number).",
}

COMPARE_PROMPT = """You write evaluation questions for a question-answering system over SEC 10-K filings.

Write ONE question comparing {a} and {b} on: {topic}. Use only the two excerpts below.
Name each company and its fiscal year (they may differ). Paraphrase rather than copying phrases.
reference_answer: both figures with units if stated, and which is larger.
evidence_a / evidence_b: copy EXACTLY a 10-160 character span from the corresponding excerpt that
contains that company's figure. If either excerpt lacks the figure, set skip=true.

Excerpt A: {a_label}
<<<
{a_text}
>>>

Excerpt B: {b_label}
<<<
{b_text}
>>>"""

SINGLE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "skip": types.Schema(type=types.Type.BOOLEAN),
        "question": types.Schema(type=types.Type.STRING),
        "reference_answer": types.Schema(type=types.Type.STRING),
        "evidence": types.Schema(type=types.Type.STRING),
    },
    required=["skip"],
)
COMPARE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "skip": types.Schema(type=types.Type.BOOLEAN),
        "question": types.Schema(type=types.Type.STRING),
        "reference_answer": types.Schema(type=types.Type.STRING),
        "evidence_a": types.Schema(type=types.Type.STRING),
        "evidence_b": types.Schema(type=types.Type.STRING),
    },
    required=["skip"],
)


@dataclass
class DraftStats:
    attempts: int = 0
    accepted: int = 0
    rejections: dict[str, int] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0

    def reject(self, reason: str) -> None:
        self.rejections[reason] = self.rejections.get(reason, 0) + 1


class Drafter:
    def __init__(self, s: Settings) -> None:
        if not s.gcp_project_id:
            raise SystemExit("GCP_PROJECT_ID is not set")
        self.model = s.judge_model
        self.client = genai_helpers.client(s.gcp_project_id, s.genai_location)
        self.stats = DraftStats()
        self.sem = asyncio.Semaphore(4)

    async def ask(self, prompt: str, schema: types.Schema) -> dict[str, Any]:
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        async with self.sem:
            async for attempt in genai_helpers.retrying(3):
                with attempt:
                    resp = await self.client.aio.models.generate_content(
                        model=self.model, contents=prompt, config=config
                    )
        meta = resp.usage_metadata
        self.stats.attempts += 1
        self.stats.input_tokens += int(getattr(meta, "prompt_token_count", 0) or 0)
        self.stats.output_tokens += int(getattr(meta, "candidates_token_count", 0) or 0)
        self.stats.thinking_tokens += int(getattr(meta, "thoughts_token_count", 0) or 0)
        try:
            parsed: dict[str, Any] = json.loads(resp.text or "{}")
        except json.JSONDecodeError:
            parsed = {"skip": True, "_error": "bad json"}
        return parsed


def _where(item: str | None, title: str | None) -> str:
    return f"Item {item}. {title}" if item else (title or "front matter")


def _valid_single(out: dict[str, Any], content: str, used: set[str], stats: DraftStats) -> bool:
    if out.get("skip"):
        stats.reject("model_skipped")
        return False
    q, ev = out.get("question", ""), out.get("evidence", "")
    if not q or not ev or not out.get("reference_answer"):
        stats.reject("missing_fields")
        return False
    if not 10 <= len(ev) <= 200:
        stats.reject("evidence_length")
        return False
    if not contains_evidence(content, ev):
        stats.reject("evidence_not_verbatim")
        return False
    if normalise(ev) in normalise(q):
        stats.reject("question_copies_evidence")
        return False
    if normalise(ev) in used:
        stats.reject("duplicate_evidence")
        return False
    return True


async def draft_single(
    d: Drafter, pool: asyncpg.Pool, kind: str, per_company: int, rng: random.Random
) -> list[GoldenItem]:
    items_filter = ("7", "8") if kind == "numeric" else ("1", "1A")
    entries = load_corpus()
    used: set[str] = set()
    out: list[GoldenItem] = []

    async def for_company(e: Any) -> list[GoldenItem]:
        rows = await pool.fetch(
            """
            SELECT c.id, c.item, c.item_title, c.content FROM chunks c
            WHERE c.document_id = $1 AND c.item = ANY($2) AND length(c.content) > 1200
            ORDER BY c.chunk_index
            """,
            e.document_id,
            list(items_filter),
        )
        if kind == "numeric":
            rows = [r for r in rows if len(re.findall(r"\d", r["content"])) > 60]
        else:
            rows = [r for r in rows if len(re.findall(r"\d", r["content"])) < 80]
        candidates = rng.sample(rows, min(len(rows), per_company * 3))
        accepted: list[GoldenItem] = []
        for r in candidates:
            if len(accepted) >= per_company:
                break
            prompt = SINGLE_PROMPT.format(
                company=e.company_name,
                ticker=e.ticker,
                fy=e.fiscal_year,
                where=_where(r["item"], r["item_title"]),
                text=r["content"],
                kind="numeric/factual" if kind == "numeric" else "narrative",
                kind_rule=KIND_RULES[kind],
            )
            res = await d.ask(prompt, SINGLE_SCHEMA)
            if not _valid_single(res, r["content"], used, d.stats):
                continue
            used.add(normalise(res["evidence"]))
            d.stats.accepted += 1
            accepted.append(
                GoldenItem(
                    id="",
                    category="numeric" if kind == "numeric" else "narrative",
                    question=res["question"].strip(),
                    answerable=True,
                    reference_answer=res["reference_answer"].strip(),
                    evidence=[Evidence(e.ticker, res["evidence"], r["item"])],
                    tickers=[e.ticker],
                    notes=f"drafted from chunk {r['id']}",
                )
            )
        return accepted

    for batch in await asyncio.gather(*(for_company(e) for e in entries)):
        out += batch
    return out


async def draft_comparisons(d: Drafter, pool: asyncpg.Pool, s: Settings) -> list[GoldenItem]:
    encoder = VertexEmbedder(
        project=s.gcp_project_id or "",
        location=s.gcp_region,
        model=s.embedding_model,
        dim=s.embedding_dim,
        task_type="RETRIEVAL_QUERY",
    )
    search = PgSearch(pool)
    by_ticker = {e.ticker: e for e in load_corpus()}
    items: list[GoldenItem] = []

    async def one(a: str, b: str, topic: str) -> GoldenItem | None:
        texts = {}
        for t in (a, b):
            vec = (await encoder.embed_documents([f"{by_ticker[t].company_name} {topic}"])).vectors[
                0
            ]
            hits = await search.vector(vec, Filters(tickers=(t,), items=("7", "8")), 2)
            texts[t] = hits
        a_text = "\n...\n".join(h.content for h in texts[a])
        b_text = "\n...\n".join(h.content for h in texts[b])
        res = await d.ask(
            COMPARE_PROMPT.format(
                a=by_ticker[a].company_name,
                b=by_ticker[b].company_name,
                topic=topic,
                a_label=f"{by_ticker[a].company_name} 10-K FY{by_ticker[a].fiscal_year}",
                a_text=a_text,
                b_label=f"{by_ticker[b].company_name} 10-K FY{by_ticker[b].fiscal_year}",
                b_text=b_text,
            ),
            COMPARE_SCHEMA,
        )
        if res.get("skip") or not res.get("question"):
            d.stats.reject("model_skipped")
            return None
        ea, eb = res.get("evidence_a", ""), res.get("evidence_b", "")
        if (
            not (contains_evidence(a_text, ea) and contains_evidence(b_text, eb))
            or min(len(ea), len(eb)) < 5
        ):
            d.stats.reject("evidence_not_verbatim")
            return None
        d.stats.accepted += 1
        return GoldenItem(
            id="",
            category="comparison",
            question=res["question"].strip(),
            answerable=True,
            reference_answer=res["reference_answer"].strip(),
            evidence=[Evidence(a, ea, None), Evidence(b, eb, None)],
            tickers=[a, b],
            notes=f"comparison on: {topic}",
        )

    for r in await asyncio.gather(*(one(a, b, t) for a, b, t in COMPARISONS)):
        if r is not None:
            items.append(r)
    return items


def review_markdown(items: list[GoldenItem]) -> str:
    def cell(x: str) -> str:
        return x.replace("|", "\\|").replace("\n", " ")

    lines = [
        "# Golden set v1: DRAFT for review",
        "",
        "Review each row: is the question clear and fair, is the reference answer correct, is the",
        "evidence the right supporting quote? Mark changes by editing `golden_v1.draft.jsonl` or",
        "listing IDs to fix/drop. Nothing is evaluated against this set until it is approved.",
        "",
    ]
    for cat in ("numeric", "narrative", "comparison", "unanswerable"):
        sel = [i for i in items if i.category == cat]
        lines += [
            f"## {cat} ({len(sel)})",
            "",
            "| id | question | reference answer | evidence |",
            "|---|---|---|---|",
        ]
        for i in sel:
            ev = "<br>".join(f"**{e.ticker}**: “{cell(e.text)}”" for e in i.evidence) or "—"
            lines.append(f"| {i.id} | {cell(i.question)} | {cell(i.reference_answer)} | {ev} |")
        lines.append("")
    return "\n".join(lines)


async def main() -> int:
    s = get_settings()
    configure_logging("WARNING")
    rng = random.Random(SEED)  # noqa: S311 - reproducible sampling, not security
    pool = await create_retrieval_pool(s.pg_dsn())
    d = Drafter(s)
    started = datetime.now(UTC)
    try:
        numeric = await draft_single(d, pool, "numeric", NUMERIC_PER_COMPANY, rng)
        narrative = await draft_single(d, pool, "narrative", NARRATIVE_PER_COMPANY, rng)
        comparisons = await draft_comparisons(d, pool, s)
    finally:
        await pool.close()
    unanswerable = [
        GoldenItem("", "unanswerable", q, False, ref, [], tickers, "hand-written")
        for q, tickers, ref in UNANSWERABLE
    ]
    numbered: list[GoldenItem] = []
    for prefix, group in (
        ("num", numeric),
        ("nar", narrative),
        ("cmp", comparisons),
        ("una", unanswerable),
    ):
        for n, item in enumerate(group, start=1):
            numbered.append(GoldenItem(**{**item.__dict__, "id": f"{prefix}-{n:03d}"}))

    GOLDEN_DIR.mkdir(exist_ok=True)
    save(numbered, GOLDEN_DIR / "golden_v1.draft.jsonl")
    (GOLDEN_DIR / "REVIEW.md").write_text(review_markdown(numbered))
    st = d.stats
    cost = generation_cost_usd(
        d.model,
        input_tokens=st.input_tokens,
        output_tokens=st.output_tokens,
        thinking_tokens=st.thinking_tokens,
    )
    report = {
        "started_at": started.isoformat(timespec="seconds"),
        "drafting_model": d.model,
        "seed": SEED,
        "counts": {
            c: sum(i.category == c for i in numbered)
            for c in ("numeric", "narrative", "comparison", "unanswerable")
        },
        "llm_calls": st.attempts,
        "accepted": st.accepted,
        "rejections": st.rejections,
        "tokens": {
            "input": st.input_tokens,
            "output": st.output_tokens,
            "thinking": st.thinking_tokens,
        },
        "estimated_cost_usd": round(cost or 0, 4),
    }
    out = Path("results/golden") / f"{started.strftime('%Y%m%dT%H%M%SZ')}_draft.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
