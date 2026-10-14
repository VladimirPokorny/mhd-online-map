"""Conversions of the time formats the sources use, all to timezone-aware datetimes."""
from datetime import date, datetime, timedelta, timezone

from .config import PRAGUE


def from_ms(ms) -> datetime | None:
    """ArcGIS date field (milliseconds since the epoch, UTC)."""
    return None if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc)


def from_local_ms(ms) -> datetime | None:
    """ArcGIS date field that holds Prague wall-clock time as if it were UTC (the parking layer does)."""
    if ms is None:
        return None
    wall = datetime.fromtimestamp(ms / 1000, timezone.utc).replace(tzinfo=None)
    return wall.replace(tzinfo=PRAGUE)


def local_hour(day, hour: int) -> datetime:
    """Start of an hour of a Prague local day (``day`` as date or "YYYY-MM-DD"); hour 24 is the next midnight."""
    d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
    # Adding a timedelta to an aware datetime is wall-clock arithmetic: hour 3 is 03:00 even on DST days.
    return datetime(d.year, d.month, d.day, tzinfo=PRAGUE) + timedelta(hours=hour)


def parse_local(text, formats=("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                               "%d.%m.%Y")) -> datetime | None:
    """Prague local time written as text in one of the usual Czech or ISO formats; None if empty or unknown."""
    text = (text or "").strip()
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=PRAGUE)
        except ValueError:
            continue
    return None


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """Start and end of a Prague local day (23 or 25 hours long when the clocks change)."""
    start = datetime(day.year, day.month, day.day, tzinfo=PRAGUE)
    nxt = day + timedelta(days=1)
    return start, datetime(nxt.year, nxt.month, nxt.day, tzinfo=PRAGUE)
