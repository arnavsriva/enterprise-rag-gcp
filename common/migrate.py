"""Minimal forward-only SQL migration runner.

Applies `common/migrations/NNNN_*.sql` in order, each in its own transaction, recording
applied versions in `schema_migrations`. A Postgres advisory lock makes concurrent runs
(e.g. two Cloud Run Job tasks) safe. Applied files must never be edited — add a new one.

Usage: python -m common.migrate
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from common.config import get_settings
from common.logging import configure_logging

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_ADVISORY_LOCK_KEY = 0x5241_4701  # arbitrary, stable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        prefix, _, _ = path.stem.partition("_")
        if not prefix.isdigit():
            raise ValueError(f"migration file must start with a number: {path.name}")
        migrations.append(Migration(int(prefix), path.stem, path.read_text()))
    versions = [m.version for m in migrations]
    if len(versions) != len(set(versions)):
        raise ValueError(f"duplicate migration versions in {directory}")
    return migrations


async def apply_migrations(conn: asyncpg.Connection, migrations: list[Migration]) -> list[str]:
    """Apply pending migrations; return the names applied. Fails on edited migrations."""
    await conn.execute("SELECT pg_advisory_lock($1)", _ADVISORY_LOCK_KEY)
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     integer PRIMARY KEY,
                name        text NOT NULL,
                checksum    text NOT NULL,
                applied_at  timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        rows = await conn.fetch("SELECT version, checksum FROM schema_migrations")
        applied = {r["version"]: r["checksum"] for r in rows}

        done = []
        for m in migrations:
            if m.version in applied:
                if applied[m.version] != m.checksum:
                    raise RuntimeError(f"applied migration {m.name} was modified")
                continue
            async with conn.transaction():
                await conn.execute(m.sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES ($1, $2, $3)",
                    m.version,
                    m.name,
                    m.checksum,
                )
            log.info("migration applied", extra={"migration": m.name})
            done.append(m.name)
        return done
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", _ADVISORY_LOCK_KEY)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    conn = await asyncpg.connect(settings.pg_dsn())
    try:
        applied = await apply_migrations(conn, discover())
        log.info("migrations complete", extra={"applied_count": len(applied)})
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
