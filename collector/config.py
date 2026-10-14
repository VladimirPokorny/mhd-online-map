"""Settings from environment variables."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

PRAGUE = ZoneInfo("Europe/Prague")
USER_AGENT = "brno-history-collector/1.0 (open data history)"


def database_url() -> str:
    """PostgreSQL connection string, e.g. postgresql://user:password@db:5432/brno."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is not set (e.g. postgresql://brno:secret@localhost:5432/brno)")
    return url


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
