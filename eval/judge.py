"""LLM-as-judge for answer quality.

The judge model (Gemini 3.1 Pro by default) is deliberately different from the generator
(Gemini 3.8 Flash) to reduce self-preference bias. It grades two independent things:

- correctness: does the answer state the reference facts? (vs the golden reference answer)
- faithfulness: is every claim supported by the sources the system retrieved? (vs sources only,
  never the reference; an answer can be correct but unfaithful if it used outside knowledge)

For unanswerable questions it checks for an appropriate refusal and for fabrication.
Judges are themselves non-deterministic, which is why the gate uses tolerances, not equality.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from google.genai import types

from common import genai as genai_helpers
from eval.golden import GoldenItem
from rag.types import Usage

Correctness = Literal["correct", "partially_correct", "incorrect", "refused"]

ANSWERABLE_PROMPT = """You grade a question-answering system that answers questions about SEC 10-K
filings using ONLY retrieved excerpts.

Question: {question}

Reference answer (ground truth taken from the filing): {reference}

Sources the system retrieved (the only material it was allowed to use):
{sources}

System answer:
<<<
{answer}
>>>

Grade:
1. correctness:
   - "correct": states the reference facts. Allow equivalent formats ($416,161 million =
     $416.2 billion), rounding, and extra correct detail.
   - "partially_correct": some but not all key facts, or a minor error.
   - "incorrect": wrong, contradicts the reference, or answers a different question.
   - "refused": says it could not find the answer.
2. faithful: true if EVERY factual claim in the answer is supported by the sources above. Judge
   against the sources, not the reference. A refusal is faithful.
3. unsupported_claims: the specific claims not supported by the sources (empty if none).
4. reasoning: one or two sentences."""

UNANSWERABLE_PROMPT = """You grade a question-answering system that answers questions about SEC
10-K filings using ONLY retrieved excerpts. This question CANNOT be answered from the corpus:
{reference}

Question: {question}

Sources the system retrieved:
{sources}

System answer:
<<<
{answer}
>>>

Grade:
1. appropriate_refusal: true if the system declined or said it could not find the answer.
   Mentioning related information that IS in the sources is fine.
2. fabricated: true if it presents a specific answer to the question that is not supported by
   the sources.
3. unsupported_claims: any claims not supported by the sources (empty if none).
4. reasoning: one or two sentences."""

ANSWERABLE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "correctness": types.Schema(
            type=types.Type.STRING, enum=["correct", "partially_correct", "incorrect", "refused"]
        ),
        "faithful": types.Schema(type=types.Type.BOOLEAN),
        "unsupported_claims": types.Schema(
            type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING)
        ),
        "reasoning": types.Schema(type=types.Type.STRING),
    },
    required=["correctness", "faithful", "unsupported_claims", "reasoning"],
)
UNANSWERABLE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "appropriate_refusal": types.Schema(type=types.Type.BOOLEAN),
        "fabricated": types.Schema(type=types.Type.BOOLEAN),
        "unsupported_claims": types.Schema(
            type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING)
        ),
        "reasoning": types.Schema(type=types.Type.STRING),
    },
    required=["appropriate_refusal", "fabricated", "unsupported_claims", "reasoning"],
)


@dataclass
class Judgment:
    item_id: str
    correctness: Correctness | None = None
    faithful: bool | None = None
    appropriate_refusal: bool | None = None
    fabricated: bool | None = None
    unsupported_claims: list[str] = field(default_factory=list)
    reasoning: str = ""
    error: str | None = None

    @property
    def hallucinated(self) -> bool:
        """Any unsupported claim (answerable) or a fabricated answer (unanswerable)."""
        if self.error:
            return False
        return bool(self.fabricated) or self.faithful is False

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "hallucinated": self.hallucinated}


def format_sources(sources: list[dict[str, Any]], max_chars: int = 4000) -> str:
    if not sources:
        return "(no sources retrieved)"
    return "\n\n".join(
        f"[{s['n']}] {s['company_name']} ({s['ticker']}) FY{s['fiscal_year']} Item {s.get('item') or '-'}\n"
        f"{(s.get('text') or s.get('excerpt') or '')[:max_chars]}"
        for s in sources
    )


class Judge:
    def __init__(
        self,
        *,
        project: str,
        location: str,
        model: str,
        concurrency: int = 4,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self._client = client or genai_helpers.client(project, location)
        self._sem = asyncio.Semaphore(concurrency)
        self.usage = Usage()

    async def _ask(self, prompt: str, schema: types.Schema) -> dict[str, Any]:
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        async with self._sem:
            async for attempt in genai_helpers.retrying(4):
                with attempt:
                    resp = await asyncio.wait_for(
                        self._client.aio.models.generate_content(
                            model=self.model, contents=prompt, config=config
                        ),
                        timeout=60,
                    )
        meta = resp.usage_metadata
        self.usage.input_tokens += int(getattr(meta, "prompt_token_count", 0) or 0)
        self.usage.output_tokens += int(getattr(meta, "candidates_token_count", 0) or 0)
        self.usage.thinking_tokens += int(getattr(meta, "thoughts_token_count", 0) or 0)
        self.usage.model_calls += 1
        parsed: dict[str, Any] = json.loads(resp.text or "{}")
        return parsed

    async def judge(self, item: GoldenItem, answer: str, sources: list[dict[str, Any]]) -> Judgment:
        src = format_sources(sources)
        try:
            if item.answerable:
                out = await self._ask(
                    ANSWERABLE_PROMPT.format(
                        question=item.question,
                        reference=item.reference_answer,
                        sources=src,
                        answer=answer,
                    ),
                    ANSWERABLE_SCHEMA,
                )
                return Judgment(
                    item_id=item.id,
                    correctness=out["correctness"],
                    faithful=bool(out["faithful"]),
                    unsupported_claims=list(out.get("unsupported_claims") or []),
                    reasoning=out.get("reasoning", ""),
                )
            out = await self._ask(
                UNANSWERABLE_PROMPT.format(
                    question=item.question,
                    reference=item.reference_answer,
                    sources=src,
                    answer=answer,
                ),
                UNANSWERABLE_SCHEMA,
            )
            return Judgment(
                item_id=item.id,
                appropriate_refusal=bool(out["appropriate_refusal"]),
                fabricated=bool(out["fabricated"]),
                unsupported_claims=list(out.get("unsupported_claims") or []),
                reasoning=out.get("reasoning", ""),
            )
        except Exception as exc:  # a judge failure must not sink the whole run
            return Judgment(item_id=item.id, error=f"{type(exc).__name__}: {exc}"[:300])
