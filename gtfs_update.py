"""Download and keep the IDS JMK GTFS feed up to date.

The feed is published by KORDIS JMK (https://kordis-jmk.cz/gtfs/gtfs.zip, listed
on data.brno.cz) and refreshed weekly. Each downloaded version is unpacked into
its own folder ``<root>/feeds/<id>``; ``<root>/feed.json`` records which one is
current together with the HTTP validators used for cheap conditional checks.
"""
import hashlib
import io
import json
import os
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

DEFAULT_URL = "https://kordis-jmk.cz/gtfs/gtfs.zip"
USER_AGENT = "idsjmk-map/1.0 (GTFS visualisation)"
REQUIRED = ("agency.txt", "routes.txt", "stops.txt", "trips.txt", "stop_times.txt", "calendar.txt")
KEEP_VERSIONS = 2


class FeedStore:
    def __init__(self, root: Path, url: str, fallback_dir: Path):
        self.root = root
        self.url = url
        self.fallback_dir = fallback_dir
        self.meta_file = root / "feed.json"
        self.feeds = root / "feeds"

    # ------------------------------------------------------------ state ---

    def meta(self) -> dict:
        try:
            return json.loads(self.meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_meta(self, meta: dict):
        tmp = self.meta_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        tmp.replace(self.meta_file)

    def current(self) -> tuple[Path, dict]:
        """Folder of the feed to serve and a description of where it came from."""
        meta = self.meta()
        cur = meta.get("current")
        if cur and (self.feeds / cur / "stop_times.txt").exists():
            return self.feeds / cur, {
                "source": "download",
                "url": self.url,
                "published": meta.get("last_modified"),
                "downloaded": meta.get("downloaded"),
                "checked": meta.get("checked"),
            }
        return self.fallback_dir, {"source": "bundled", "url": self.url, "checked": meta.get("checked")}

    # ----------------------------------------------------------- update ---

    def check(self):
        """Download the feed if it changed.

        Returns ``None`` when nothing changed, otherwise ``(folder, commit)``: the
        unpacked new feed and a function that makes it current. The caller commits
        only after the feed was processed successfully, so a broken download is
        retried at the next check instead of becoming the served version."""
        self.root.mkdir(parents=True, exist_ok=True)
        meta = self.meta()
        headers = {"User-Agent": USER_AGENT}
        has_current = bool(meta.get("current")) and (self.feeds / meta["current"]).exists()
        if has_current and meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if has_current and meta.get("last_modified_raw"):
            headers["If-Modified-Since"] = meta["last_modified_raw"]

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with urllib.request.urlopen(urllib.request.Request(self.url, headers=headers), timeout=300) as resp:
                body = resp.read()
                etag = resp.headers.get("ETag")
                last_modified_raw = resp.headers.get("Last-Modified")
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                meta["checked"] = now
                self._save_meta(meta)
                return None
            raise

        feed_id = hashlib.sha1(body).hexdigest()[:12]
        if has_current and feed_id == meta["current"]:
            meta.update(checked=now, etag=etag, last_modified_raw=last_modified_raw)
            self._save_meta(meta)
            return None

        target = self.feeds / feed_id
        self._unpack(body, target)
        published = None
        if last_modified_raw:
            try:
                published = parsedate_to_datetime(last_modified_raw).isoformat()
            except (TypeError, ValueError):
                pass

        def commit():
            meta = self.meta()
            meta.update(
                current=feed_id, etag=etag, last_modified_raw=last_modified_raw,
                last_modified=published, downloaded=now, checked=now,
            )
            self._save_meta(meta)
            self._prune(keep={feed_id})

        return target, commit

    def _unpack(self, body: bytes, target: Path):
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            names = {Path(n).name: n for n in z.namelist() if n.endswith(".txt")}
            missing = [f for f in REQUIRED if f not in names]
            if missing:
                raise ValueError(f"downloaded GTFS is missing {', '.join(missing)}")
            tmp = target.with_name(target.name + ".tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            for short, full in names.items():
                (tmp / short).write_bytes(z.read(full))
                # Keep the publisher's timestamp, so the derived cache stamp is stable.
                ts = time.mktime(z.getinfo(full).date_time + (0, 0, -1))
                os.utime(tmp / short, (ts, ts))
        shutil.rmtree(target, ignore_errors=True)
        tmp.replace(target)

    def _prune(self, keep: set):
        if not self.feeds.exists():
            return
        dirs = sorted((d for d in self.feeds.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
        for d in dirs[KEEP_VERSIONS:]:
            if d.name not in keep:
                shutil.rmtree(d, ignore_errors=True)
