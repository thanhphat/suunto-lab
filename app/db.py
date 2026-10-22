from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import sql as sql_mod
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb  # noqa: F401  (re-exported)
from psycopg_pool import ConnectionPool

from . import config

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=8,
                               kwargs={"row_factory": dict_row}, open=True)
    return _pool


@contextmanager
def conn():
    with pool().connection() as c:
        yield c


def migrate() -> int:
    """Apply schema.sql, set the agent role password, ensure one athlete exists. Returns athlete id."""
    sql = (Path(__file__).parent / "schema.sql").read_text()
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as c:
        # Serialise concurrent starts (api, worker and mcp boot together).
        c.execute("SELECT pg_advisory_lock(727001)")
        try:
            c.execute(sql)
            c.execute(sql_mod.SQL("ALTER ROLE agent_ro PASSWORD {}").format(
                sql_mod.Literal(config.AGENT_DB_PASSWORD)))
            row = c.execute("SELECT id FROM athletes ORDER BY id LIMIT 1").fetchone()
            if row is None:
                row = c.execute("INSERT INTO athletes (name) VALUES (%s) RETURNING id", (config.ATHLETE_NAME,)).fetchone()
            athlete_id = row[0]
            c.execute("INSERT INTO athlete_settings (athlete_id, timezone) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                      (athlete_id, config.DEFAULT_TZ))
        finally:
            c.execute("SELECT pg_advisory_unlock(727001)")
    return athlete_id


def default_athlete_id() -> int:
    with conn() as c:
        return c.execute("SELECT id FROM athletes ORDER BY id LIMIT 1").fetchone()["id"]
