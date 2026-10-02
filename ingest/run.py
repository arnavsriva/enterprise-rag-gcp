"""CLI: ingest the pinned 10-K corpus into pgvector (and optionally Vertex AI Vector Search).

    python -m ingest.run --dry-run                 # download + parse + chunk, no API calls
    python -m ingest.run --tickers AAPL,MSFT       # embed + store a subset
    python -m ingest.run                           # whole corpus (unchanged filings skipped)
    python -m ingest.run --force                   # re-embed even if unchanged

Every run writes a JSON report to results/ingest/ (a saved run: the source for any
throughput or cost number quoted elsewhere). With INGEST_USE_GCS=true (the Cloud Run Job),
raw filings are cached in and reports uploaded to the GCS bucket, because the job's local
disk disappears when it exits. INGEST_VECTOR_SEARCH=true also upserts to Vector Search.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from common import gcs
from common.config import Settings, get_settings
from common.logging import configure_logging
from ingest.corpus import CorpusEntry, load_corpus
from ingest.edgar import EdgarClient
from ingest.embed import VertexEmbedder
from ingest.pipeline import RunReport, run_ingest
from ingest.store import PgVectorStore, create_pool
from rag.vector_search import VertexIndexWriter, config_from_settings

RESULTS_DIR = Path("results/ingest")

log = logging.getLogger(__name__)


def select(entries: list[CorpusEntry], tickers: str | None) -> list[CorpusEntry]:
    if not tickers:
        return entries
    wanted = {t.strip().upper() for t in tickers.split(",") if t.strip()}
    unknown = wanted - {e.ticker for e in entries}
    if unknown:
        raise SystemExit(f"unknown tickers (not in ingest/corpus.toml): {sorted(unknown)}")
    return [e for e in entries if e.ticker in wanted]


async def ingest(
    s: Settings, *, tickers: str | None = None, dry_run: bool = False, force: bool = False
) -> RunReport:
    entries = select(load_corpus(), tickers)
    raw_dir = s.data_dir / "raw"
    bucket = s.gcs_bucket if s.ingest_use_gcs else None

    async with EdgarClient(
        user_agent=s.sec_user_agent,
        requests_per_second=s.sec_requests_per_second,
        max_retries=s.ingest_max_retries,
    ) as edgar:

        async def fetch(e: CorpusEntry) -> tuple[bytes, bool]:
            """Local cache -> GCS archive (cloud) -> SEC EDGAR; archive new downloads to GCS."""
            blob = f"raw/{e.ticker}/{e.accession}.htm"
            local = raw_dir / e.ticker / f"{e.accession}.htm"
            if bucket:
                archived = await gcs.download_if_exists(bucket, blob)
                if archived is not None:
                    return archived, True
            content, cached = await edgar.fetch_document(e.url, local)
            if bucket and not cached:
                await gcs.upload(bucket, blob, content, "text/html")
            return content, cached

        if dry_run:
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
        writer = None
        if s.ingest_vector_search:
            vs_config = config_from_settings(s)
            if vs_config is None:
                raise SystemExit("INGEST_VECTOR_SEARCH=true but VECTOR_SEARCH_INDEX_ID is not set")
            writer = VertexIndexWriter(vs_config)
        pool = await create_pool(s.pg_dsn())
        try:
            return await run_ingest(
                entries,
                fetch=fetch,
                embedder=embedder,
                store=PgVectorStore(pool),
                embedding_model=s.embedding_model,
                max_concurrency=s.ingest_max_concurrency,
                force=force,
                index_writer=writer,
            )
        finally:
            await pool.close()


def _write_local(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


async def save_report(report: RunReport, s: Settings) -> list[str]:
    """Persist the run report. On Cloud Run (INGEST_USE_GCS) the GCS copy is the durable one, so
    it is written first, and the local copy goes under DATA_DIR (the app dir is read-only)."""
    stamp = report.started_at.replace(":", "").replace("-", "").replace("+0000", "Z")
    name = f"{stamp}_{report.mode}.json"
    body = json.dumps(report.to_dict(), indent=2, default=str)
    locations = []
    if s.ingest_use_gcs and s.gcs_bucket:
        locations.append(
            await gcs.upload(
                s.gcs_bucket, f"results/ingest/{name}", body.encode(), "application/json"
            )
        )
    local_dir = s.data_dir / "results" / "ingest" if s.ingest_use_gcs else RESULTS_DIR
    try:
        await asyncio.to_thread(_write_local, local_dir / name, body)
        locations.append(str(local_dir / name))
    except OSError:
        log.exception("could not write local report copy", extra={"path": str(local_dir / name)})
        if not locations:
            raise
    return locations


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

    s = get_settings()
    configure_logging(s.log_level)

    async def run() -> RunReport:
        report = await ingest(s, tickers=args.tickers, dry_run=args.dry_run, force=args.force)
        for location in await save_report(report, s):
            print(f"report: {location}", file=sys.stderr)
        return report

    summary = asyncio.run(run()).summary()
    print(json.dumps(summary, indent=2), file=sys.stderr)
    sys.exit(1 if summary["docs_failed"] else 0)


if __name__ == "__main__":
    main()
