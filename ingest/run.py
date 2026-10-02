"""CLI: ingest the pinned 10-K corpus into pgvector.

    python -m ingest.run --dry-run                 # download + parse + chunk, no API calls
    python -m ingest.run --tickers AAPL,MSFT       # embed + store a subset
    python -m ingest.run                           # whole corpus (unchanged filings skipped)
    python -m ingest.run --force                   # re-embed even if unchanged

Every run writes a JSON report to results/ingest/ (a saved run: the source for any
throughput or cost number quoted elsewhere).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from common.config import get_settings
from common.logging import configure_logging
from ingest.corpus import CorpusEntry, load_corpus
from ingest.edgar import EdgarClient
from ingest.embed import VertexEmbedder
from ingest.pipeline import RunReport, run_ingest
from ingest.store import PgVectorStore, create_pool

RESULTS_DIR = Path("results/ingest")


def _select(entries: list[CorpusEntry], tickers: str | None) -> list[CorpusEntry]:
    if not tickers:
        return entries
    wanted = {t.strip().upper() for t in tickers.split(",") if t.strip()}
    unknown = wanted - {e.ticker for e in entries}
    if unknown:
        raise SystemExit(f"unknown tickers (not in ingest/corpus.toml): {sorted(unknown)}")
    return [e for e in entries if e.ticker in wanted]


async def _main(args: argparse.Namespace) -> RunReport:
    s = get_settings()
    entries = _select(load_corpus(), args.tickers)
    raw_dir = s.data_dir / "raw"

    async with EdgarClient(
        user_agent=s.sec_user_agent,
        requests_per_second=s.sec_requests_per_second,
        max_retries=s.ingest_max_retries,
    ) as edgar:

        async def fetch(e: CorpusEntry) -> tuple[bytes, bool]:
            return await edgar.fetch_document(e.url, raw_dir / e.ticker / f"{e.accession}.htm")

        if args.dry_run:
            return await run_ingest(
                entries,
                fetch=fetch,
                embedder=None,
                store=None,
                embedding_model=s.embedding_model,
                max_concurrency=s.ingest_max_concurrency,
            )

        if not s.gcp_project_id:
            raise SystemExit("GCP_PROJECT_ID is not set (needed for the embedding API)")
        embedder = VertexEmbedder(
            project=s.gcp_project_id,
            location=s.gcp_region,
            model=s.embedding_model,
            dim=s.embedding_dim,
            max_texts=s.embed_batch_max_texts,
            max_tokens=s.embed_batch_max_tokens,
            max_concurrency=s.embed_max_concurrency,
            max_retries=s.ingest_max_retries,
        )
        pool = await create_pool(s.pg_dsn())
        try:
            return await run_ingest(
                entries,
                fetch=fetch,
                embedder=embedder,
                store=PgVectorStore(pool),
                embedding_model=s.embedding_model,
                max_concurrency=s.ingest_max_concurrency,
                force=args.force,
            )
        finally:
            await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--tickers", help="comma-separated subset, e.g. AAPL,MSFT")
    parser.add_argument(
        "--dry-run", action="store_true", help="no embedding API calls, no DB writes"
    )
    parser.add_argument("--force", action="store_true", help="re-process unchanged filings")
    args = parser.parse_args()

    configure_logging(get_settings().log_level)
    report = asyncio.run(_main(args))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = report.started_at.replace(":", "").replace("-", "").replace("+0000", "Z")
    out = RESULTS_DIR / f"{stamp}_{report.mode}.json"
    out.write_text(json.dumps(report.to_dict(), indent=2, default=str))

    summary = report.summary()
    print(json.dumps(summary, indent=2), file=sys.stderr)
    print(f"report: {out}", file=sys.stderr)
    sys.exit(1 if summary["docs_failed"] else 0)


if __name__ == "__main__":
    main()
