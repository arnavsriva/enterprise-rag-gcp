import time
from datetime import date
from pathlib import Path

import httpx
import pytest

from ingest.corpus import COMPANIES, CorpusEntry, load_corpus, render_corpus
from ingest.edgar import EdgarClient
from ingest.ratelimit import AsyncRateLimiter

UA = "Test Person test@example.com"

SUBMISSIONS = {
    "name": "SAMPLE CORP",
    "filings": {
        "recent": {
            "form": ["8-K", "10-K/A", "10-K", "10-K"],
            "accessionNumber": ["a-0", "a-1", "a-2", "a-3"],
            "primaryDocument": ["x.htm", "amend.htm", "s-2025.htm", "s-2024.htm"],
            "reportDate": ["2026-03-01", "2025-12-31", "2025-12-31", "2024-12-31"],
            "filingDate": ["2026-03-02", "2026-03-01", "2026-02-10", "2025-02-11"],
        }
    },
}


def client(handler: httpx.MockTransport, **kw: object) -> EdgarClient:
    return EdgarClient(user_agent=UA, requests_per_second=1000, transport=handler, **kw)  # type: ignore[arg-type]


def test_user_agent_must_contain_email() -> None:
    with pytest.raises(ValueError, match="SEC_USER_AGENT"):
        EdgarClient(user_agent="anonymous")


async def test_latest_filing_skips_amendments_and_sends_user_agent() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["User-Agent"])
        assert request.url.path == "/submissions/CIK0000000042.json"
        return httpx.Response(200, json=SUBMISSIONS)

    async with client(httpx.MockTransport(handler)) as edgar:
        ref = await edgar.latest_filing(42)
    assert (ref.accession, ref.primary_document, ref.period_end) == (
        "a-2",
        "s-2025.htm",
        date(2025, 12, 31),
    )
    assert ref.url == "https://www.sec.gov/Archives/edgar/data/42/a2/s-2025.htm"
    assert seen == [UA]


async def test_retries_on_429_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429) if calls < 3 else httpx.Response(200, json=SUBMISSIONS)

    async with client(httpx.MockTransport(handler), backoff_max_seconds=0.01) as edgar:
        await edgar.latest_filing(42)
    assert calls == 3


async def test_does_not_retry_404() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    async with client(httpx.MockTransport(handler), backoff_max_seconds=0.01) as edgar:
        with pytest.raises(httpx.HTTPStatusError):
            await edgar.latest_filing(42)
    assert calls == 1


async def test_fetch_document_caches_on_disk(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"<html>10-K</html>")

    target = tmp_path / "AAA" / "doc.htm"
    async with client(httpx.MockTransport(handler)) as edgar:
        assert await edgar.fetch_document("https://www.sec.gov/x", target) == (
            b"<html>10-K</html>",
            False,
        )
        assert await edgar.fetch_document("https://www.sec.gov/x", target) == (
            b"<html>10-K</html>",
            True,
        )
    assert calls == 1
    assert not target.with_suffix(".htm.part").exists()


async def test_rate_limiter_spaces_calls() -> None:
    limiter = AsyncRateLimiter(rate_per_second=50)  # 20 ms apart
    start = time.monotonic()
    for _ in range(6):
        await limiter.acquire()
    assert time.monotonic() - start >= 0.09


def test_corpus_round_trip_and_pins(tmp_path: Path) -> None:
    entries = load_corpus()
    assert [e.ticker for e in entries] == [t for t, _, _ in COMPANIES]
    assert len({e.accession for e in entries}) == len(entries)
    path = tmp_path / "corpus.toml"
    path.write_text(render_corpus(entries))
    assert load_corpus(path) == entries


def test_fiscal_year_follows_period_end() -> None:
    e = CorpusEntry(
        "WMT", 104169, "Walmart Inc.", "10-K", "a", "d.htm", date(2026, 1, 31), date(2026, 3, 13)
    )
    assert e.fiscal_year == 2026
