from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest

from common.config import Settings
from common.migrate import Migration, apply_migrations, discover


def test_discover_orders_bundled_migrations() -> None:
    migrations = discover()
    assert migrations, "expected at least one bundled migration"
    assert [m.version for m in migrations] == sorted(m.version for m in migrations)
    assert migrations[0].name == "0001_init"


def test_discover_rejects_duplicates(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0001_b.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="duplicate"):
        discover(tmp_path)


def test_discover_rejects_unnumbered(tmp_path: Path) -> None:
    (tmp_path / "init.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="must start with a number"):
        discover(tmp_path)


# ---------------------------------------------------------------- real Postgres (make db-up)


@pytest.fixture
async def conn() -> AsyncIterator[asyncpg.Connection]:
    """Connection inside a throwaway schema, so tests never touch real tables."""
    c = await asyncpg.connect(Settings().pg_dsn())
    # Pin the extension to public so dropping the test schema never drops it.
    await c.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public")
    await c.execute("DROP SCHEMA IF EXISTS migrate_test CASCADE; CREATE SCHEMA migrate_test")
    await c.execute("SET search_path TO migrate_test, public")
    try:
        yield c
    finally:
        await c.execute("DROP SCHEMA migrate_test CASCADE")
        await c.close()


@pytest.mark.db
async def test_apply_is_idempotent(conn: asyncpg.Connection) -> None:
    migrations = discover()
    assert await apply_migrations(conn, migrations) == [m.name for m in migrations]
    assert await apply_migrations(conn, migrations) == []

    tables = {
        r["tablename"]
        for r in await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='migrate_test'")
    }
    assert {"documents", "chunks", "schema_migrations"} <= tables


@pytest.mark.db
async def test_edited_migration_is_detected(conn: asyncpg.Connection) -> None:
    await apply_migrations(conn, [Migration(1, "0001_x", "CREATE TABLE t (id int);")])
    with pytest.raises(RuntimeError, match="was modified"):
        await apply_migrations(conn, [Migration(1, "0001_x", "CREATE TABLE t (id bigint);")])
