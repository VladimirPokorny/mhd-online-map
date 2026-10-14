"""Runs the jobs: one thread each, a log row per run, backoff after failures, clean shutdown."""
import logging
import threading
import time
import traceback

from . import config, db
from .jobs import Job, Result

log = logging.getLogger("collector")

BACKFILL_PAUSE_S = 30  # between batches of a backfill (Result.more), to stay polite to the source


def _log_start(pool, job: str) -> int:
    """Insert the run's log row; it is written outside the job's transaction so failures are kept."""
    with pool.connection() as conn:
        return conn.execute("INSERT INTO collector_run (job, started_at) VALUES (%s, now()) RETURNING id",
                            (job,)).fetchone()[0]


def _log_end(pool, run_id: int, ok: bool, result: Result | None, note: str):
    """Complete the run's log row."""
    with pool.connection() as conn:
        conn.execute("""UPDATE collector_run SET finished_at = now(), ok = %s, rows = %s, note = %s, source_time = %s
                        WHERE id = %s""",
                     (ok, result.rows if result else None, note[:2000], result.source_time if result else None, run_id))


def run_once(pool, job: Job) -> Result:
    """One logged run of a job; raises what the job raised."""
    run_id = _log_start(pool, job.name)
    t0 = time.monotonic()
    try:
        result = job.run(pool) or Result()
    except Exception as exc:
        _log_end(pool, run_id, False, None, f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}")
        raise
    _log_end(pool, run_id, True, result, result.note)
    log.info("%s: %s rows in %.1fs%s", job.name, result.rows, time.monotonic() - t0,
             f" ({result.note})" if result.note else "")
    return result


def _loop(pool, job: Job, stop: threading.Event):
    """Run a job every ``interval`` seconds until ``stop`` is set; after a failure retry sooner."""
    failures = 0
    while not stop.is_set():
        try:
            result = run_once(pool, job)
            failures = 0
            pause = BACKFILL_PAUSE_S if result.more else job.interval
        except Exception as exc:
            failures += 1
            pause = min(job.interval, job.retry * 2 ** (failures - 1))
            log.error("%s failed (%s in a row): %s: %s; next try in %ss",
                      job.name, failures, type(exc).__name__, exc, pause)
        stop.wait(pause)


def run_forever(pool, jobs: list[Job], stop: threading.Event):
    """Start every enabled job in its own thread and wait for ``stop``."""
    threads = []
    for job in jobs:
        if not config.enabled(job.name):
            log.info("%s: disabled", job.name)
            continue
        target = job.run_forever if hasattr(job, "run_forever") else _loop
        t = threading.Thread(target=target, args=(pool, job, stop), name=job.name, daemon=True)
        t.start()
        threads.append(t)
        stop.wait(2)  # stagger the first runs
    log.info("collector running %s jobs", len(threads))
    stop.wait()
    for t in threads:
        t.join(timeout=30)


def status(pool) -> list[tuple]:
    """Per job: last run, whether it succeeded, rows, newest source time, last success and last error."""
    with pool.connection() as conn:
        return conn.execute("""
            SELECT j.job, last.started_at, last.ok, last.rows, last.source_time,
                   (SELECT max(started_at) FROM collector_run r WHERE r.job = j.job AND r.ok) AS last_ok,
                   (SELECT left(note, 160) FROM collector_run r WHERE r.job = j.job AND NOT r.ok
                        AND r.started_at > coalesce((SELECT max(started_at) FROM collector_run s
                                                     WHERE s.job = j.job AND s.ok), '-infinity')
                    ORDER BY started_at DESC LIMIT 1) AS last_error   -- only errors since the last success
            FROM (SELECT DISTINCT job FROM collector_run) j
            CROSS JOIN LATERAL (SELECT * FROM collector_run r WHERE r.job = j.job
                                ORDER BY started_at DESC LIMIT 1) last
            ORDER BY j.job""").fetchall()


def health(pool, jobs: list[Job]) -> tuple[list[str], list[str]]:
    """(problems, warnings). Problems are on our side: a job without a recent successful run.
    Warnings are stale sources: the job works but the publisher has not updated the data."""
    problems, warnings = [], []
    rows = {r[0]: r for r in status(pool)}
    with pool.connection() as conn:
        first = dict(conn.execute("SELECT job, min(started_at) FROM collector_run GROUP BY job").fetchall())
    now = db.utc_now()
    for job in jobs:
        if not config.enabled(job.name) or job.name not in rows:
            continue
        _, _, _, _, source_time, last_ok, _ = rows[job.name]
        allowed = max(3 * job.interval, 1800)
        since = last_ok or first[job.name]   # a job that never succeeded counts from its first run
        if (now - since).total_seconds() > allowed:
            problems.append(f"{job.name}: no successful run for over {allowed // 60} min")
        if job.stale_after and source_time and (now - source_time).total_seconds() > job.stale_after:
            warnings.append(f"{job.name}: newest source data from "
                            f"{source_time.astimezone(config.PRAGUE):%Y-%m-%d %H:%M} (stale)")
    return problems, warnings
