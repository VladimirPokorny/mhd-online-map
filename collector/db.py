"""PostgreSQL access: connection pool, schema migrations, partitions and job cursors."""
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

log = logging.getLogger("collector.db")

MIGRATIONS = Path(__file__).with_name("migrations")
# Session advisory lock held by the running collector: a second instance waits instead of
# writing the same data twice.
INSTANCE_LOCK = 0x62726E6F  # "brno"


def make_pool(url: str, size: int = 4) -> ConnectionPool:
    """Connection pool; broken connections (database restart) are replaced on checkout."""
    return ConnectionPool(url, min_size=1, max_size=size, open=True, check=ConnectionPool.check_connection,
                          kwargs={"application_name": "brno-collector"})


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply the SQL files in collector/migrations that have not run yet, in name order."""
    applied = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (INSTANCE_LOCK + 1,))  # one migrator at a time
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migration (
                            name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())""")
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migration")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            log.info("applying migration %s", path.name)
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migration (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return applied


def _month_start(d: date) -> date:
    """First day of the month of ``d``."""
    return d.replace(day=1)


def _next_month(d: date) -> date:
    """First day of the month after ``d``."""
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def ensure_partitions(conn: psycopg.Connection, first: date, last: date):
    """Create the monthly partitions of vehicle_sample covering days ``first`` to ``last``."""
    month = _month_start(first)
    while month <= last:
        nxt = _next_month(month)
        name = f"vehicle_sample_{month:%Y_%m}"
        # Partition bounds are local months; timestamptz literals with the Prague offset.
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF vehicle_sample "
            f"FOR VALUES FROM ('{month.isoformat()} 00:00 Europe/Prague') TO ('{nxt.isoformat()} 00:00 Europe/Prague')")
        month = nxt


def lock_day(conn: psycopg.Connection, day: date):
    """Serialise writers of one day of vehicle_sample (archive import, stream flush) until the transaction ends."""
    conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (INSTANCE_LOCK, day.toordinal()))


def get_state(conn: psycopg.Connection, key: str, default=None):
    """A job cursor (any JSON value), or ``default`` if never set."""
    row = conn.execute("SELECT value FROM collector_state WHERE key = %s", (key,)).fetchone()
    return default if row is None else row[0]


def set_state(conn: psycopg.Connection, key: str, value):
    """Store a job cursor; call inside the transaction that stores the data it describes."""
    conn.execute("""INSERT INTO collector_state (key, value, updated_at) VALUES (%s, %s, now())
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                 (key, Jsonb(value)))


def utc_now() -> datetime:
    """The current time, timezone-aware."""
    return datetime.now(timezone.utc)
