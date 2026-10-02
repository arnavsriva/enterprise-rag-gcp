"""Cloud Run Job entrypoint: apply DB migrations, then ingest the full corpus.

Runs inside the VPC (private-IP Cloud SQL, Vector Search). Idempotent: unchanged filings are
skipped, filings embedded in Postgres but missing from Vector Search are synced from the
stored vectors. Exit code is non-zero if any filing failed, so the job execution shows failed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

import asyncpg

from common.config import get_settings
from common.logging import configure_logging
from common.migrate import apply_migrations, discover
from ingest.run import ingest, save_report

log = logging.getLogger("ingest.job")


async def main() -> int:
    s = get_settings()
    configure_logging(s.log_level)

    conn = await asyncpg.connect(s.pg_dsn())
    try:
        applied = await apply_migrations(conn, discover())
    finally:
        await conn.close()
    log.info("migrations checked", extra={"applied": applied})

    report = await ingest(s)
    locations = await save_report(report, s)
    summary = report.summary()
    log.info("ingest job complete", extra={"summary": summary, "reports": locations})
    print(json.dumps(summary), file=sys.stderr)
    return 1 if summary["docs_failed"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
