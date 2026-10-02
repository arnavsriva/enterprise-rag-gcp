"""Grounded answer generation with Gemini: numbered sources in, cited answer out.

Hallucination controls: answer only from the provided sources, cite every claim, never guess
units, and a fixed refusal sentence when the sources don't contain the answer (so refusals are
machine-detectable). Citations pointing at sources that don't exist are flagged.

Temperature is left at the Gemini 3 default of 1.0: Google recommends not lowering it (it can
cause looping or degraded reasoning; https://ai.google.dev/gemini-api/docs/gemini-3). Outputs
are therefore not deterministic run-to-run; see docs/exec_brief.md.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from google.genai import types

from common.genai import retrying
from rag.types import RetrievedChunk, Usage

NOT_FOUND = "I could not find this in the provided filings."

SYSTEM_PROMPT = f"""You answer questions about companies using excerpts from their SEC Form 10-K filings.

Rules:
1. Use ONLY the numbered sources below. Do not use outside knowledge, even if you are confident.
2. Cite every factual claim with its source number in square brackets, e.g. [2] or [1][3].
3. Report figures exactly as written, with their units and the fiscal year. Units are often
   stated in a table caption such as "(In millions)"; if the unit is not visible in the
   sources, say the unit is not stated rather than guessing.
4. Answer whenever the sources support an answer, even if the filing uses different wording
   (e.g. "associates" for employees) or gives an approximate figure; say how the filing words
   it. If the exact measure asked for is not reported but a closely related one is, give the
   related measure under the filing's own name and say it may not be the same measure.
5. Never substitute a different company or a different fiscal year for the one asked about.
6. Only if the sources contain nothing that answers the question, reply with exactly:
   "{NOT_FOUND}" Then, in one sentence, mention any closely related information they contain.
7. If sources disagree or the question is ambiguous (e.g. which fiscal year), say so briefly.
8. Be concise: lead with the direct answer, then the supporting detail. No preamble."""

_DASHES = "\u2013-"  # en dash or hyphen, as in citation ranges like [2-4]
_CITATION = re.compile(r"\[(\d+(?:\s*[," + _DASHES + r"]\s*\d+)*)\]")


@dataclass
class Generation:
    text: str
    answered: bool
    cited: list[int]  # 1-based source numbers, in order of first citation
    invalid_citations: list[int]  # cited numbers with no matching source
    usage: Usage
    model: str
    finish_reason: str | None
    raw: dict[str, Any] = field(default_factory=dict)


def format_sources(chunks: list[RetrievedChunk]) -> str:
    return "\n\n".join(f"[{i}] {c.label}\n{c.content}" for i, c in enumerate(chunks, start=1))


def build_prompt(question: str, chunks: list[RetrievedChunk]) -> str:
    return f"Sources:\n\n{format_sources(chunks)}\n\nQuestion: {question}"


def parse_citations(text: str, n_sources: int) -> tuple[list[int], list[int]]:
    seen: list[int] = []
    for group in _CITATION.findall(text):
        for part in re.split(r"\s*,\s*", group):
            bounds = re.split(r"\s*[" + _DASHES + r"]\s*", part)
            nums = (
                range(int(bounds[0]), int(bounds[-1]) + 1) if len(bounds) == 2 else [int(bounds[0])]
            )
            for n in nums:
                if n not in seen:
                    seen.append(n)
    valid = [n for n in seen if 1 <= n <= n_sources]
    invalid = [n for n in seen if not 1 <= n <= n_sources]
    return valid, invalid


def is_refusal(text: str) -> bool:
    return text.strip().startswith(NOT_FOUND)


class Generator:
    def __init__(
        self,
        client: Any,
        *,
        model: str,
        thinking_level: str = "LOW",
        max_output_tokens: int = 2048,
        max_retries: int = 3,
        timeout_s: float = 15.0,
    ) -> None:
        self._client = client
        self.model = model
        self._thinking_level = thinking_level
        self._max_output_tokens = max_output_tokens
        self._max_retries = max_retries
        self._timeout_s = timeout_s

    async def generate(self, question: str, chunks: list[RetrievedChunk]) -> Generation:
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            thinking_config=types.ThinkingConfig(
                thinking_level=types.ThinkingLevel(self._thinking_level)
            ),
            max_output_tokens=self._max_output_tokens,
            # No tools are passed; disabling AFC also silences a per-request SDK warning.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        async for attempt in retrying(self._max_retries):
            with attempt:
                # TimeoutError is retryable (common.genai.is_retryable), so a queued call is retried.
                response = await asyncio.wait_for(
                    self._client.aio.models.generate_content(
                        model=self.model, contents=build_prompt(question, chunks), config=config
                    ),
                    timeout=self._timeout_s,
                )
        text = (response.text or "").strip()
        meta = response.usage_metadata
        usage = Usage(
            input_tokens=int(getattr(meta, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(meta, "candidates_token_count", 0) or 0),
            thinking_tokens=int(getattr(meta, "thoughts_token_count", 0) or 0),
            model_calls=1,
        )
        candidates = response.candidates or []
        finish = getattr(candidates[0].finish_reason, "name", None) if candidates else None
        cited, invalid = parse_citations(text, len(chunks))
        return Generation(
            text=text,
            answered=bool(text) and not is_refusal(text),
            cited=cited,
            invalid_citations=invalid,
            usage=usage,
            model=getattr(response, "model_version", None) or self.model,
            finish_reason=finish,
        )
