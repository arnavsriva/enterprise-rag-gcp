"""Async SEC EDGAR client: rate-limited, retried, with an on-disk cache for filings.

SEC fair-access policy: identify yourself with a `User-Agent: Name email` header and stay
under 10 requests/second. https://www.sec.gov/os/accessing-edgar-data
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from ingest.ratelimit import AsyncRateLimiter

log = logging.getLogger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{document}"


@dataclass(frozen=True)
class FilingRef:
    cik: int
    company_name: str
    form: str
    accession: str
    primary_document: str
    period_end: date
    filed: date

    @property
    def url(self) -> str:
        return ARCHIVE_URL.format(
            cik=self.cik,
            accession_nodash=self.accession.replace("-", ""),
            document=self.primary_document,
        )


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {403, 429} or exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


class EdgarClient:
    """Use as `async with EdgarClient(user_agent=...) as edgar:`."""

    def __init__(
        self,
        *,
        user_agent: str,
        requests_per_second: float = 5.0,
        max_retries: int = 5,
        backoff_max_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if "@" not in user_agent:
            raise ValueError('SEC_USER_AGENT must be "Your Name your-email@example.com"')
        self._limiter = AsyncRateLimiter(requests_per_second)
        self._max_retries = max_retries
        self._backoff_max = backoff_max_seconds
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=True,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    async def _get(self, url: str) -> httpx.Response:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(self._max_retries + 1),
            wait=wait_random_exponential(multiplier=0.5, max=self._backoff_max),
            reraise=True,
        ):
            with attempt:
                await self._limiter.acquire()
                response = await self._client.get(url)
                if attempt.retry_state.attempt_number > 1:
                    log.info(
                        "edgar retry",
                        extra={"url": url, "attempt": attempt.retry_state.attempt_number},
                    )
                response.raise_for_status()
                return response
        raise AssertionError("unreachable")  # pragma: no cover

    async def latest_filing(self, cik: int, form: str = "10-K") -> FilingRef:
        """Most recent filing of exactly `form` (amendments like 10-K/A are excluded)."""
        data: dict[str, Any] = (await self._get(SUBMISSIONS_URL.format(cik=cik))).json()
        recent = data["filings"]["recent"]
        for i, f in enumerate(recent["form"]):
            if f == form:
                return FilingRef(
                    cik=cik,
                    company_name=data["name"],
                    form=f,
                    accession=recent["accessionNumber"][i],
                    primary_document=recent["primaryDocument"][i],
                    period_end=date.fromisoformat(recent["reportDate"][i]),
                    filed=date.fromisoformat(recent["filingDate"][i]),
                )
        raise LookupError(f"no {form} found in recent filings for CIK {cik}")

    async def fetch_document(self, url: str, cache_path: Path) -> tuple[bytes, bool]:
        """Return (content, from_cache). Filings are immutable, so the cache never expires."""
        cached = await asyncio.to_thread(_read_if_exists, cache_path)
        if cached is not None:
            return cached, True
        content = (await self._get(url)).content
        await asyncio.to_thread(_write_atomic, cache_path, content)
        return content, False


def _read_if_exists(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _write_atomic(path: Path, content: bytes) -> None:
    """Write via a temp file + rename, so a crash never leaves a half-written cache file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(content)
    tmp.replace(path)
