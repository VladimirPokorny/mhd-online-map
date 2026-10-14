"""Command line of the collector: python -m collector run|once|status|health|migrate."""
import argparse
import logging
import signal
import sys
import threading
import time

import psycopg

from . import config, db, scheduler
from .jobs import all_jobs

log = logging.getLogger("collector")


def _hold_instance_lock(url: str, stop: threading.Event) -> psycopg.Connection:
    """Take the advisory lock that makes this the only running collector; wait while another one holds it."""
    while not stop.is_set():
        conn = psycopg.connect(url, autocommit=True, application_name="brno-collector-lock")
        if conn.execute("SELECT pg_try_advisory_lock(%s)", (db.INSTANCE_LOCK,)).fetchone()[0]:
            return conn
        conn.close()
        log.warning("another collector is running on this database; waiting")
        stop.wait(60)
    raise SystemExit(0)


def _watch_lock(conn: psycopg.Connection, stop: threading.Event, lost: threading.Event):
    """Stop the collector when the lock connection is lost (the lock is gone with it); Docker restarts it."""
    while not stop.wait(60):
        try:
            conn.execute("SELECT 1")
        except Exception as exc:
            log.error("lost the database connection holding the instance lock (%s); stopping", exc)
            lost.set()
            stop.set()
            return


def cmd_run(pool, url: str):
    """Run all jobs until SIGTERM or SIGINT."""
    stop = threading.Event()

    def on_signal(signum, _frame):
        """Ask every job to finish its current step and stop."""
        log.info("signal %s: stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    lock = _hold_instance_lock(url, stop)
    # Runs left open by a collector that was killed; with the lock held, none of them is still running.
    n = lock.execute("""UPDATE collector_run SET finished_at = now(), ok = false,
                            note = 'interrupted: the collector stopped during the run'
                        WHERE finished_at IS NULL""").rowcount
    if n:
        log.info("marked %s unfinished runs of a previous collector as interrupted", n)
    lost = threading.Event()
    threading.Thread(target=_watch_lock, args=(lock, stop, lost), daemon=True, name="lock").start()
    scheduler.run_forever(pool, all_jobs(), stop)
    lock.close()
    return 1 if lost.is_set() else 0   # non-zero: the container restarts and takes the lock again


def cmd_once(pool, names: list[str]):
    """Run the named jobs once each (all if none named); exit code 1 if any failed."""
    jobs = {j.name: j for j in all_jobs()}
    unknown = [n for n in names if n not in jobs]
    if unknown:
        raise SystemExit(f"unknown job(s): {', '.join(unknown)}; jobs: {', '.join(jobs)}")
    if "kordis_stream" in names:
        raise SystemExit("kordis_stream runs continuously; it is started by 'python -m collector run'")
    failed = 0
    for name in names or [n for n in jobs if n != "kordis_stream"]:
        try:
            scheduler.run_once(pool, jobs[name])
        except Exception as exc:
            failed += 1
            log.error("%s failed: %s: %s", name, type(exc).__name__, exc)
    return 1 if failed else 0


def cmd_status(pool):
    """Print the last run of every job."""
    rows = scheduler.status(pool)
    if not rows:
        print("no job has run yet")
        return 0
    fmt = lambda t: t.astimezone(config.PRAGUE).strftime("%Y-%m-%d %H:%M") if t else "–"
    print(f"{'job':16} {'last run':16} {'ok':3} {'rows':>9}  {'data from':16} {'last ok':16}  last error")
    for job, started, ok, rows_, source_time, last_ok, error in rows:
        err = (error or "").splitlines()[0][:70] if error else ""
        print(f"{job:16} {fmt(started):16} {'yes' if ok else 'NO' if ok is False else '…':3} {rows_ if rows_ is not None else '':>9}"
              f"  {fmt(source_time):16} {fmt(last_ok):16}  {err}")
    return 0


def cmd_health(pool):
    """Exit code 1 when a job has not succeeded for too long (for the container health check)."""
    problems, warnings = scheduler.health(pool, all_jobs())
    for w in warnings:
        print("warning:", w)
    for p in problems:
        print("problem:", p)
    return 1 if problems else 0


def main(argv=None) -> int:
    """Parse the command line and run the command."""
    parser = argparse.ArgumentParser(prog="python -m collector", description="Brno open data history collector")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run all jobs continuously")
    once = sub.add_parser("once", help="run jobs once (default: all except the stream)")
    once.add_argument("jobs", nargs="*")
    sub.add_parser("status", help="last run of every job")
    sub.add_parser("health", help="exit 1 when jobs fail (container health check)")
    sub.add_parser("migrate", help="create or update the database schema")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)
    url = config.database_url()
    if args.command in ("run", "migrate", "once"):
        # The database may still be starting (docker compose up): retry for a while.
        for attempt in range(30):
            try:
                with psycopg.connect(url, autocommit=False) as conn:
                    applied = db.migrate(conn)
                if applied:
                    log.info("applied migrations: %s", ", ".join(applied))
                break
            except psycopg.OperationalError as exc:
                if attempt == 29:
                    raise
                log.warning("database not reachable (%s); retrying", str(exc).strip().splitlines()[0])
                time.sleep(5)
        if args.command == "migrate":
            return 0
    pool = db.make_pool(url, size=len(all_jobs()) + 2)
    try:
        if args.command == "run":
            return cmd_run(pool, url)
        if args.command == "once":
            return cmd_once(pool, args.jobs)
        if args.command == "status":
            return cmd_status(pool)
        return cmd_health(pool)
    finally:
        pool.close()


if __name__ == "__main__":
    sys.exit(main())
