"""The data sources: one job per source, each with its own schedule."""
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Result:
    """What a job run did: rows written, a short note and the newest time in the source data."""
    rows: int = 0
    note: str = ""
    source_time: datetime | None = None
    more: bool = False   # a backfill stopped at its batch limit: run again soon, not after the full interval


class Job:
    """A periodic job. ``run`` does one complete, idempotent pass and may raise on failure;
    every pass stores its data and cursors in one transaction, so a failed pass leaves nothing half done."""
    name = ""
    interval = 600       # seconds between runs
    retry = 60           # first pause after a failure (doubles, up to ``interval``)
    stale_after = None   # seconds after which the source data count as stale (health check)

    def run(self, pool) -> Result:
        """Fetch and store one pass of data."""
        raise NotImplementedError


def all_jobs() -> list[Job]:
    """Every job, in the order they start."""
    from .chmi import Chmi
    from .city import CityEvents, WaterOutages
    from .counters import BikeCountsHistory, BikeCountsLive, TsbCounts
    from .kordis import KordisArchive, KordisStream
    from .parking import Parking
    from .traffic import Traffic
    from .transport import IdsDiversions
    from .venues import Venues
    from .weather import Temperatures, Weather
    return [Parking(), Venues(), Temperatures(), BikeCountsLive(), Weather(), Chmi(), Traffic(), IdsDiversions(),
            WaterOutages(), CityEvents(), KordisStream(), KordisArchive(), BikeCountsHistory(), TsbCounts()]
