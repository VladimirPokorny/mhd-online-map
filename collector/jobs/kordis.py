"""KORDIS vehicle positions and delays: the daily 7-day archive and the live stream.

Both keep a sample only when a vehicle's line, course, last stop or delay changes (about a
quarter of the 30-second samples). The archive is complete but uploaded irregularly and keeps
only 7 days, so the live stream is recorded too; when a day arrives in the archive it replaces
whatever the stream recorded for that day.
"""
import io
import json
import logging
import struct
import tempfile
import time
import zlib
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from .. import config, db
from ..http import NotModified, open_url
from ..timeutil import local_day_bounds
from . import Job, Result

log = logging.getLogger("collector.kordis")

ARCHIVE_URL = "https://www.arcgis.com/sharing/rest/content/items/c022f917ab5148658bdce5892d8912ce/data"
STREAM_URL = "wss://gis.brno.cz/geoevent/ws/services/Kordis_stream/StreamServer/subscribe"
LIVE, ARCHIVE = 1, 2                      # vehicle_sample.source
CHANGE_KEY = ["ID", "LineID", "Course", "LastStopID", "Delay"]
COLUMNS = ("observed_at", "vehicle_id", "line_id", "route_id", "course", "last_stop", "final_stop", "delay_min",
           "lat_e6", "lon_e6", "bearing", "low_floor", "vtype", "ltype", "source")


# --------------------------------------------------------------- archive ---

def zip_members(data: bytes):
    """(name, bytes) of the members of a ZIP file, read from the local headers.

    The KORDIS archive is often served cut short, without the ZIP's central directory, so
    ``zipfile`` refuses it although the members before the cut are intact. A member whose
    data is cut off ends the listing."""
    view = memoryview(data)
    pos = 0
    while True:
        pos = data.find(b"PK\x03\x04", pos)
        if pos < 0:
            return
        _ver, flags, method, _t, _d, _crc, csize, _usize, nlen, xlen = struct.unpack_from("<HHHHHIIIHH", data, pos + 4)
        name = bytes(view[pos + 30:pos + 30 + nlen]).decode("utf-8", "replace")
        start = pos + 30 + nlen + xlen
        if method == 0 and not flags & 8:
            if start + csize > len(data):
                return
            body, end = bytes(view[start:start + csize]), start + csize
        elif method == 8:
            dec = zlib.decompressobj(-15)
            try:
                body = dec.decompress(view[start:])
            except zlib.error:
                return
            if not dec.eof:
                return
            end = len(data) - len(dec.unused_data)
        else:
            pos = start
            continue
        yield name, body
        pos = end


def changes(df: pd.DataFrame) -> pd.DataFrame:
    """Samples where a vehicle's line, course, last stop or delay differs from its previous sample."""
    df = df.sort_values(["ID", "TimeUpdated"], kind="stable")
    # Missing values would not compare equal to each other: give them a fixed stand-in first.
    key = df[CHANGE_KEY].astype(object).fillna("<NA>")
    changed = (key != key.shift()).any(axis=1).to_numpy(copy=True)
    changed[0:1] = True   # shift() leaves the first row without a predecessor
    return df[changed]


def _column(s: pd.Series) -> list:
    """A column as a Python list with None for missing values (for COPY)."""
    return s.astype(object).where(s.notna(), None).tolist()


def archive_rows(df: pd.DataFrame) -> list[tuple]:
    """vehicle_sample rows of an archive day (only the changes)."""
    df = changes(df.dropna(subset=["ID", "LineID", "TimeUpdated"]))
    ms = df["TimeUpdated"].dt.tz_convert("UTC").dt.as_unit("ms").astype("int64").to_numpy()
    times = [datetime.fromtimestamp(m / 1000, timezone.utc) for m in ms.tolist()]
    delay = df["Delay"].round()
    cols = [_column(df[c]) for c in ("ID", "LineID", "RouteID", "Course", "LastStopID", "FinalStopID")]
    rest = [_column(delay.astype("Int16")), _column(df["LatE6"]), _column(df["LngE6"]), _column(df["Bearing"]),
            _column(df["LF"]), _column(df["VType"]), _column(df["LType"])]
    return [(t, *vals, ARCHIVE) for t, *vals in zip(times, *cols, *rest)]


def _copy(conn, rows: list[tuple]):
    """Bulk insert into vehicle_sample."""
    with conn.cursor().copy(f"COPY vehicle_sample ({', '.join(COLUMNS)}) FROM STDIN") as copy:
        for r in rows:
            copy.write_row(r)


class KordisArchive(Job):
    """Imports the days of the 7-day archive not stored from the archive yet."""
    name = "kordis_archive"
    interval = 3 * 3600
    retry = 900
    stale_after = 10 * 86400   # the archive is uploaded irregularly; over 10 days old means days are lost

    def run(self, pool) -> Result:
        """Download the archive if it changed and import its new days."""
        with pool.connection() as conn:
            etag = db.get_state(conn, "kordis.archive_etag")
            done = {r[0] for r in conn.execute("SELECT day FROM vehicle_day WHERE source = %s", (ARCHIVE,))}
        try:
            # ~75 MB: spooled to disk while downloading rather than held in memory twice.
            with tempfile.TemporaryFile() as tmp:
                with open_url(ARCHIVE_URL, timeout=600, headers={"If-None-Match": etag} if etag else None) as resp:
                    while chunk := resp.read(1 << 20):
                        tmp.write(chunk)
                    etag = resp.headers.get("ETag")
                tmp.seek(0)
                data = tmp.read()
        except NotModified:
            return Result(0, "archive unchanged", self._newest(pool))
        imported, skipped, total = [], [], 0
        for name, body in zip_members(data):
            if not name.endswith(".parquet"):
                continue
            try:
                day = date.fromisoformat(Path(name).stem.rsplit("_", 1)[-1])   # kordis_2026-10-02.parquet
            except ValueError:
                continue
            if day in done:
                continue
            try:
                df = pd.read_parquet(io.BytesIO(body))
            except Exception as exc:   # cut off; a later upload has this day again
                skipped.append(f"{day} ({type(exc).__name__})")
                continue
            rows = archive_rows(df)
            self._keep_raw(name, body)
            start, end = local_day_bounds(day)
            with pool.connection() as conn, conn.transaction():
                db.lock_day(conn, day)
                if conn.execute("SELECT 1 FROM vehicle_day WHERE day = %s AND source = %s", (day, ARCHIVE)).fetchone():
                    continue   # imported meanwhile by another run
                db.ensure_partitions(conn, day, day)
                conn.execute("DELETE FROM vehicle_sample WHERE observed_at >= %s AND observed_at < %s", (start, end))
                _copy(conn, rows)
                conn.execute("""INSERT INTO vehicle_day (day, source, rows, imported_at) VALUES (%s, %s, %s, now())
                                ON CONFLICT (day) DO UPDATE SET source = EXCLUDED.source, rows = EXCLUDED.rows,
                                    imported_at = now()""", (day, ARCHIVE, len(rows)))
            imported.append(f"{day} ({len(rows):,} of {len(df):,} samples)")
            total += len(rows)
        del data
        with pool.connection() as conn, conn.transaction():
            if etag:
                db.set_state(conn, "kordis.archive_etag", etag)
        note = "imported " + ", ".join(imported) if imported else "no new days"
        if skipped:
            note += "; unreadable: " + ", ".join(skipped)
        return Result(total, note, self._newest(pool))

    @staticmethod
    def _newest(pool):
        """End of the newest day imported from the archive (the age of the source)."""
        with pool.connection() as conn:
            row = conn.execute("SELECT max(day) FROM vehicle_day WHERE source = %s", (ARCHIVE,)).fetchone()
        return local_day_bounds(row[0])[1] if row and row[0] else None

    @staticmethod
    def _keep_raw(name: str, body: bytes):
        """Keep the day's Parquet file (all samples) when KORDIS_RAW_DIR is set."""
        folder = config.kordis_raw_dir()
        if folder:
            folder.mkdir(parents=True, exist_ok=True)
            tmp = folder / (Path(name).name + ".tmp")
            tmp.write_bytes(body)
            tmp.replace(folder / Path(name).name)


# ---------------------------------------------------------------- stream ---

class KordisStream(Job):
    """Records the live vehicle stream; runs continuously and writes a summary to collector_run
    every ``interval`` seconds. A silent stream is the source's fault, not a failure of the job:
    the summary stays ok and its source_time shows how old the last message is. Only failing to
    write to the database marks it as failed."""
    name = "kordis_stream"
    interval = 600
    stale_after = 15 * 60
    FLUSH_S = 30
    SILENT_S = 120            # no message for this long: reconnect (the stream can stay open but silent)
    MAX_BUFFER = 500_000      # samples kept while the database is unreachable (~2 hours); older ones are dropped

    def __init__(self):
        self.last = {}        # vehicle → (line, course, last stop, delay) of its last buffered sample
        self.buffer = []

    def sample(self, a: dict) -> bool:
        """Buffer a stream message if it changes the vehicle's line, course, last stop or delay."""
        try:
            vid, ms = int(a["ID"]), int(a["TimeUpdated"])
            line = int(a.get("LineID") or 0)
            delay = a.get("Delay")
            delay = None if delay is None else int(round(float(delay)))
            lat = None if a.get("Lat") is None else int(round(float(a["Lat"]) * 1e6))
            lon = None if a.get("Lng") is None else int(round(float(a["Lng"]) * 1e6))
            bearing = None if a.get("Bearing") is None else int(a["Bearing"])
        except (KeyError, TypeError, ValueError):
            return False
        if not line or str(a.get("IsInactive")).lower() == "true":
            return False   # the archive has no inactive vehicles either
        course, last_stop = a.get("Course") or None, a.get("LastStopID")
        key = (line, course, last_stop, delay)
        if self.last.get(vid) == key:
            return False
        self.last[vid] = key
        self.buffer.append((datetime.fromtimestamp(ms / 1000, timezone.utc), vid, line, None, course, last_stop,
                            a.get("FinalStopID"), delay, lat, lon, bearing, str(a.get("LF")).lower() == "true",
                            a.get("VType"), None, LIVE))
        return True

    def flush(self, pool) -> int:
        """Write the buffered samples, except for days already complete from the archive.
        The buffer is emptied only after the commit, so nothing is lost when the database is down."""
        if not self.buffer:
            return 0
        rows = list(self.buffer)
        by_day = {}
        for r in rows:
            by_day.setdefault(r[0].astimezone(config.PRAGUE).date(), []).append(r)
        written = 0
        with pool.connection() as conn, conn.transaction():
            for day in sorted(by_day):   # in order, so two writers never wait for each other's locks
                db.lock_day(conn, day)
            archived = {r[0] for r in conn.execute("SELECT day FROM vehicle_day WHERE source = %s AND day = ANY(%s)",
                                                    (ARCHIVE, list(by_day)))}
            for day, day_rows in by_day.items():
                if day in archived:
                    continue
                db.ensure_partitions(conn, day, day)
                _copy(conn, day_rows)
                conn.execute("""INSERT INTO vehicle_day (day, source, rows) VALUES (%s, %s, %s)
                                ON CONFLICT (day) DO UPDATE SET rows = vehicle_day.rows + EXCLUDED.rows, imported_at = now()
                                WHERE vehicle_day.source = %s""", (day, LIVE, len(day_rows), LIVE))
                written += len(day_rows)
        del self.buffer[:len(rows)]
        return written

    def _flush_safely(self, pool) -> tuple[int, str | None]:
        """flush() that reports a database error instead of raising, and caps the buffer meanwhile."""
        try:
            return self.flush(pool), None
        except Exception as exc:
            if len(self.buffer) > self.MAX_BUFFER:
                dropped = len(self.buffer) - self.MAX_BUFFER
                del self.buffer[:dropped]
                log.error("kordis_stream: buffer full, dropped %s oldest samples", dropped)
            return 0, f"{type(exc).__name__}: {exc}"

    def run_forever(self, pool, _job, stop):
        """Connect, record and reconnect until ``stop`` is set."""
        import websocket  # websocket-client
        from ..scheduler import _log_end, _log_start
        stats = {"stored": 0, "heard": 0, "last_msg": None, "db_error": None, "stream_error": None}
        run_id, report_at, last_flush = _log_start(pool, self.name), time.monotonic() + self.interval, time.monotonic()

        def report(final=False):
            """Close the current summary row and open the next one."""
            nonlocal run_id, report_at
            note = f"{stats['heard']:,} messages"
            if stats["stream_error"]:
                note += f"; stream: {stats['stream_error']}"
            if stats["db_error"]:
                note += f"; database: {stats['db_error']}"
            _log_end(pool, run_id, stats["db_error"] is None, Result(stats["stored"], note, stats["last_msg"]), note)
            stats.update(stored=0, heard=0, db_error=None, stream_error=None)
            if not final:
                run_id, report_at = _log_start(pool, self.name), time.monotonic() + self.interval

        def tick():
            """Flush and report when due."""
            nonlocal last_flush
            now = time.monotonic()
            if now - last_flush >= self.FLUSH_S or len(self.buffer) >= 20_000:
                n, err = self._flush_safely(pool)
                stats["stored"] += n
                stats["db_error"] = err or stats["db_error"]
                last_flush = now
            if now >= report_at:
                try:
                    report()
                except Exception as exc:   # database down: try again at the next tick
                    log.error("kordis_stream: cannot write the run log: %s", exc)

        backoff = 5
        while not stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(STREAM_URL, timeout=15, header=[f"User-Agent: {config.USER_AGENT}"])
                log.info("kordis_stream: connected")
                silent_since = time.monotonic()
                while not stop.is_set():
                    try:
                        msg = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        msg = None
                    if msg:
                        try:
                            self.sample(json.loads(msg)["attributes"])
                            stats["heard"] += 1
                            stats["last_msg"] = db.utc_now()
                            silent_since = time.monotonic()
                            backoff = 5
                        except (KeyError, TypeError, ValueError):
                            pass
                    tick()
                    if time.monotonic() - silent_since > self.SILENT_S:
                        raise TimeoutError(f"no message for {self.SILENT_S}s")
            except Exception as exc:
                stats["stream_error"] = f"{type(exc).__name__}: {exc}"
                log.warning("kordis_stream: %s; reconnecting in %ss", stats["stream_error"], backoff)
                tick()
                stop.wait(backoff)
                backoff = min(backoff * 2, 300)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
        n, err = self._flush_safely(pool)
        stats["stored"] += n
        stats["db_error"] = err
        if err:
            log.error("kordis_stream: %s samples not written at shutdown: %s", len(self.buffer), err)
        try:
            report(final=True)
        except Exception as exc:   # database gone at shutdown; nothing more to do
            log.error("kordis_stream: cannot write the run log at shutdown: %s", exc)
