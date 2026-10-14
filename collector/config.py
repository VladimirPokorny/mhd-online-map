"""Settings from environment variables."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

PRAGUE = ZoneInfo("Europe/Prague")
USER_AGENT = "brno-history-collector/1.0 (open data history)"


def database_url() -> str:
    """PostgreSQL connection: DATABASE_URL, or "" to let libpq read PGHOST, PGUSER, PGPASSWORD, PGDATABASE.

    The PG* variables need no URL encoding, so any password works; compose uses them."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    if os.environ.get("PGHOST", "").strip():
        return ""
    raise SystemExit("set DATABASE_URL (e.g. postgresql://brno:secret@localhost:5432/brno) "
                     "or PGHOST, PGUSER, PGPASSWORD and PGDATABASE")


def kordis_raw_dir() -> Path | None:
    """Folder to keep the daily KORDIS Parquet files in (all samples, ~13 MB a day); unset = not kept."""
    value = os.environ.get("KORDIS_RAW_DIR", "").strip()
    return Path(value) if value else None


def enabled(job: str) -> bool:
    """A job runs unless listed in COLLECTOR_DISABLE (comma-separated job names)."""
    off = {j.strip() for j in os.environ.get("COLLECTOR_DISABLE", "").split(",") if j.strip()}
    return job not in off


def interval(job: str, default: int) -> int:
    """Seconds between runs of a job; COLLECTOR_INTERVAL_<JOB> overrides the default."""
    value = os.environ.get(f"COLLECTOR_INTERVAL_{job.upper()}", "").strip()
    return int(value) if value else default
