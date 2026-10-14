"""HTTP with timeouts and retries, and paging through ArcGIS feature layers."""
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import USER_AGENT

log = logging.getLogger("collector.http")

RETRIES = 3          # attempts after the first one
BACKOFF_S = 5        # first pause; doubles with every attempt
RETRY_STATUS = {429, 500, 502, 503, 504}


class NotModified(Exception):
    """The server answered 304: the resource has not changed since the given ETag."""


def open_url(url: str, params: dict | None = None, timeout: float = 60, headers: dict | None = None):
    """Open a URL (GET), retrying network errors and 429/5xx answers. The caller closes the response."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    for attempt in range(RETRIES + 1):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                raise NotModified() from None
            if exc.code not in RETRY_STATUS or attempt == RETRIES:
                raise
            reason = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt == RETRIES:
                raise
            reason = f"{type(exc).__name__}: {exc}"
        pause = BACKOFF_S * 2 ** attempt
        log.warning("%s: %s, retrying in %ss", url.split("?")[0], reason, pause)
        time.sleep(pause)


def get_json(url: str, params: dict | None = None, timeout: float = 60):
    """GET a JSON document."""
    with open_url(url, params, timeout) as resp:
        return json.loads(resp.read().decode("utf-8-sig"))


def arcgis_query(layer_url: str, timeout: float = 120, **params) -> dict:
    """Query an ArcGIS feature layer (``layer_url`` ends with /FeatureServer/<n>).

    ArcGIS reports errors with HTTP 200 and an "error" object; those are raised."""
    params = {"f": "json", "where": "1=1", "outFields": "*", "returnGeometry": "false", **params}
    data = get_json(f"{layer_url}/query", params, timeout)
    if "error" in data:
        err = data["error"]
        raise RuntimeError(f"ArcGIS {layer_url}: {err.get('code')} {err.get('message')} {err.get('details') or ''}")
    return data


def arcgis_features(layer_url: str, **params) -> list[dict]:
    """Attributes of all features of a small layer (one request)."""
    return [f["attributes"] for f in arcgis_query(layer_url, **params).get("features", [])]


def arcgis_pages(layer_url: str, oid_field: str, after: int, page: int = 1000, **params):
    """Features with an object id above ``after``, in pages ordered by object id.

    Paging by object id (not by offset) stays correct while the layer grows, and the last
    id of a page is a cursor a job can store to resume after a failure or restart."""
    while True:
        data = arcgis_query(layer_url, where=f"{oid_field} > {int(after)}", orderByFields=f"{oid_field} ASC",
                            resultRecordCount=page, **params)
        feats = [f["attributes"] for f in data.get("features", [])]
        if not feats:
            return
        yield feats
        after = feats[-1][oid_field]
        if len(feats) < page and not data.get("exceededTransferLimit"):
            return


def layer_info(layer_url: str) -> dict:
    """Metadata of an ArcGIS layer (fields, editingInfo with the time of the last data change)."""
    return get_json(layer_url, {"f": "json"})
