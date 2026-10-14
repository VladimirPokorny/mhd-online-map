# IDS JMK Public Transport Map

A web app that shows the network of the South Moravian Region's integrated public transport system (IDS JMK) on an OpenStreetMap map. It uses the GTFS timetable published by KORDIS JMK ([data.brno.cz](https://data.brno.cz/datasets/379d2e9a7907460c8ca7fda1f3e84328/about)) and downloads new versions automatically.

- **Lines** are drawn along their real routes (rails, roads), not as straight lines between stops.
- **Vehicles** are shown at any time of the chosen day: workday, Saturday, Sunday or public holiday.
- **A chart** shows how many vehicles are in service throughout the day, by mode of transport.
- **A reachability map** shows where you can get from a chosen stop within 15–120 minutes.
- **A live map** (`/zive`) shows where every vehicle is right now and how late it is, with the remaining stops and their expected times, service alerts for the line, live car traffic and road closures in Brno.

The user interface is in Czech. Below, Czech labels are quoted as they appear on screen, with a translation where it helps.

---

## 1. User guide

### Left panel – lines and filters

| Control | What it does |
|---|---|
| **Search** (*„Hledat číslo linky, cíl nebo zastávku“*) | Filters lines by number, destination or stop, e.g. `1`, `S2`, `Blansko` or `Mendlovo náměstí`. The filter applies to the map, the list and the vehicles. |
| **Mode chips** (*Tramvaj / Trolejbus / Autobus / Vlak / Loď*) | Turn trams, trolleybuses, buses, trains and boats on or off. |
| **Layers** (*Vozidla / Linky / Zastávky*) | Show or hide vehicles, lines and stops. Stops appear only when zoomed in. |
| **Line list** | Click a line to select it. The map zooms to it, the other lines fade, and a detail panel opens with the number of trips, the stops and how many vehicles are running right now. Clicking a stop name in the detail moves the map there. |

You can also select a line by clicking its route or a vehicle on the map. Click an empty spot on the map, or the × in the detail panel, to clear the selection.

### Timeline (below the map)

1. **Date:** pick any day within the timetable's validity, or click **Pracovní den / Sobota / Neděle / Svátek** (workday / Saturday / Sunday / holiday) to jump to the nearest such day. A label next to it shows the kind of day, including the holiday's name (holidays run on the Sunday timetable).
2. **Time:** drag the slider, or click or drag directly in the chart.
   - **← / →** moves the time by 1 minute, with **Shift** by 10 minutes.
3. **Playback:** the **▶** button (or the space bar) animates the day. The speed selector next to it goes from 1× to 900×.
4. **Chart:** a stacked chart of vehicles in service during the day, by mode. Hovering shows exact counts; the vertical line marks the selected time.
5. **Counts above the chart:** how many vehicles of each mode are in service at that moment across the whole network. The large number on the right is the number of vehicles currently on the map (after any filtering).

**Vehicles on the map** are dots in the line's color. Hovering one shows the line, destination and next stop; clicking selects the line.

### Where can I get from a stop? (reachability map)

1. Type a stop into the **„Kam se dostanu ze zastávky?“** field, e.g. `Hlavní nádraží`. Alternatively, click a stop on the map and choose **„Kam se odsud dostanu?“**.
2. Choose a time limit (15–120 min).
3. The departure uses the **date and time from the timeline**. Changing the time or starting playback recalculates the map.

How to read the map:

| Color | Meaning |
|---|---|
| **Blue gradient** (dark → lighter) | Reachable within the chosen limit. The darker, the sooner you get there. |
| **Light grey** | Reachable, but only after the limit (up to twice the limit, at most 4 hours). |
| **Dark grey** | Further than you can get from this departure. |

- Each place's color is the **fastest combination of riding and walking** (walking from the last stop is included).
- When zoomed in, the reachable stops are shown. Hover one to see the travel time; click it to see the **full journey**, e.g. *"S2 8:29 Hlavní nádraží → 8:59 Blansko město"*.
- From any stop in the result you can start a new calculation.
- The reachability map turns off the vehicle layer so it doesn't get in the way. Tick *Vozidla* to show vehicles again; the × button restores everything.

### Live map (`/zive`)

Linked from the map's sidebar (*„Živá mapa se zpožděním“*). It shows the real positions of all IDS JMK vehicles, refreshed every 10 seconds.

| Part | What it does |
|---|---|
| **Vehicles** | A badge with the line number at each vehicle's real position, coloured by its current delay: blue *napřed* (early), green *včas* (on time, up to 1 min), yellow 2–4 min, orange 5–9 min, red 10+ min, grey when the position is more than 5 minutes old. The arrow shows the direction. Zoomed out, vehicles are dots. |
| **Vehicle detail** (click a vehicle) | Line, destination, delay, vehicle number (for DPMB vehicles the fleet number, linked to the [bmhd.cz register](https://www.bmhd.cz/evidence-dpmb/) of the DPMB fleet), for trains the train number linked to its composition on [vagonweb.cz](https://www.vagonweb.cz/razeni/), the connection number, mode, low-floor, run number, route, last stop, age of the position, how the delay developed during the run, **service alerts** for the line (closures, diversions, moved stops), and **all stops of the trip** with the timetable time and the expected time (timetable + current delay). The map shows the line's route and stops and the vehicle's trail. *Sledovat vybrané vozidlo* keeps the map on it. |
| **Summary** | Vehicles in service, average delay, share delayed 3+ min, and the most delayed lines right now (click one to show only that line). |
| **Filters** | Line search, or a vehicle's fleet number (four or more digits, e.g. `1720`) to find that vehicle; modes; *Jen zpožděné* (only vehicles 3+ min late). |
| **Car traffic** | Roads in Brno where cars are slow right now (dopravniinfo.cz, refreshed every 2 minutes); free-flowing roads stay uncoloured. Hover a road for its speed. |
| **Closures** | Road closures and restrictions in Brno (dopravniinfo.cz), shown when zoomed in. |

The address keeps the line, the vehicle and the position: `/zive?linka=1&vuz=1720#15/49.2/16.6`.

### My location

Both maps have a location button (below the zoom buttons). After the browser asks for permission, it shows a blue dot with the accuracy circle, centres the map on it the first time and follows the device as it moves. Pressing it again after panning away brings the map back; pressing it while centred turns it off. The position stays in the browser; nothing is sent to the server. Browsers allow this only on HTTPS (and on `localhost`).

### On the home screen

The app can be added to a phone's home screen. It then opens the live map full-screen, without the browser bar; a long press on the icon (Android) also offers the network map.

- **Android (Chrome, Edge):** the *Přidat na plochu* link (in the sidebar and on the About page) opens the browser's install dialog; it only appears when the browser offers installation. The browser menu works too.
- **iPhone and iPad (Safari):** *Sdílet* → *Přidat na plochu*. The same link leads to these instructions on the About page.

It is a web app (PWA): a manifest (`/manifest.webmanifest`), icons (`static/icons/`), and a small service worker (`/sw.js`) that shows a *Jste bez připojení* page when there is no connection. It caches nothing else: the map and live data always come from the network. Installing needs HTTPS (or `localhost`).

### Links to a specific view

The address can be bookmarked or shared:

- `#15/49.1960/16.6100` – zoom level and map centre (updated automatically as you pan),
- `?from=Hlavní nádraží&date=2026-10-14&time=08:00&minutes=60` – opens the reachability map directly.

---

## 2. How it works

### Data

| Source | Used for |
|---|---|
| IDS JMK timetable (GTFS) – <https://kordis-jmk.cz/gtfs/gtfs.zip> | lines, stops, trips, service calendar, transfers |
| OpenStreetMap (Overpass API) | real line routes (IDS JMK route relations), the rail network |
| OSRM (public routing server) | routes for buses that aren't mapped in OSM |
| OpenStreetMap tiles | the base map in the browser |

OSM and OSRM data is stored in `osm_cache/` and only downloaded again when a new timetable needs it (for example, a new line).

### Automatic timetable updates

KORDIS publishes a new timetable once a week (on Sundays). The app handles this on its own:

1. **Check:** every 6 hours (configurable) it asks the server whether `gtfs.zip` has changed. It uses a conditional request (`ETag` / `Last-Modified`), so nothing is downloaded when nothing changed.
2. **New version:** it is downloaded (about 8 MB), validated and unpacked into its own folder, `feeds/<version>/`.
3. **Background processing:** the app keeps serving the previous version the whole time and switches once the new one is ready.
4. **Safety:** a new version only becomes current after it has been processed successfully. If processing fails (for example, OpenStreetMap is unavailable), the previous data keeps running and the next check tries again. The last 2 versions are kept.
5. **Open pages:** browsers that still show the older version display a bar *„Jízdní řád byl aktualizován – Načíst znovu“* ("The timetable was updated – Reload"). After reloading, the date, time and selected stop are kept.

The app's header shows the timetable's validity period, when it was published and when the last check ran.

### Line routes

The IDS JMK GTFS has no `shapes.txt` (route shapes), so routes are put together like this:

1. **Lines mapped in OSM** are matched by line number and mode (293 lines). A route is used only if at least 80 % of the line's stops lie within 200 m of it.
2. **The other lines** are calculated through their stops in travel order (58 lines): buses and trolleybuses via OSRM, trams and trains as the shortest path over OSM tracks.
3. **Straight lines** between stops are used only as a last resort.

A line's detail panel shows how its route was obtained.

### Vehicle positions

- Every trip running at a given moment is one vehicle.
- Positions come from the timetable: between a departure and the next arrival the vehicle moves smoothly along the route, and it waits at a stop for the scheduled dwell time.
- Trips that started the previous day and run past midnight are included.
- In this data, `departure_time` at a trip's last stop means the departure of the vehicle's next trip (a layover), so it is not counted as running.
- These are **scheduled positions**, not live data. Delays are not taken into account.

### Reachability map

- **Calculation:** uses the *Connection Scan* algorithm over the chosen day's timetable to find the earliest arrival at every stop. One calculation takes about 0.1 s.
- **Transfers:**
  - at least 1 minute at the same stop
  - the official walking transfers from `transfers.txt`
  - walks of up to 400 m between nearby stops (about 4.5 km/h)
- **Starting point:** all platforms with the same name as the chosen stop.
- **Journeys:** when two journeys arrive at the same time, the one with fewer changes wins.
- **Map surface:** from every reached stop, time spreads into the surroundings at walking speed. The grid cells are about 160 m and cover the whole region.

### Live map

Everything is kept in memory (`live.py`); nothing is stored.

- **Positions and delays** come from the KORDIS vehicle stream (`wss://gis.brno.cz/geoevent/ws/services/Kordis_stream/StreamServer/subscribe`, one message per vehicle about every 30 s, delay in whole minutes). The server keeps the latest state of every vehicle and its trail for the last hour.
- **The trip** a vehicle runs comes from KORDIS GTFS Realtime (`https://kordis-jmk.cz/gtfs/gtfsReal.dat`, polled every 30 s): its vehicle label is the stream's vehicle number, and it carries the GTFS `trip_id`. The trip's stops come from the loaded timetable; the vehicle's position on it is the stream's *last stop*. Expected times are the timetable times plus the current delay. The realtime trip IDs refer to KORDIS's current timetable, so stops are only shown when the app runs the current version (the default with automatic updates; not with `--url ""` and an old `GTFS/` folder).
- **Fallback when the stream is silent:** if no stream message arrives for 2 minutes (the WebSocket can stay connected and send nothing), positions come from GTFS Realtime instead. That feed has no delays, so the delay is estimated from the timetable. A vehicle at a stop is late by the time past its scheduled departure. Between two stops, the scheduled time at its position is interpolated by the straight-line distances to the previous and next stop. Vehicles more than 10 minutes before their trip's first departure count as not in service and are hidden, and estimates over 90 minutes are dropped as a finished trip the feed still names. The map's status line then says "záložní zdroj, zpoždění odhadnuté", and the vehicle detail marks the delay as an estimate ("odhad"). GTFS Realtime zero-pads stop IDs (`U09440Z01` = `U9440Z1` in the timetable), so stops are matched by their stop and post numbers.
- **Service alerts** per line come from the same GTFS Realtime feed.
- **Vehicle numbers:** the stream's vehicle ID is the DPMB fleet number for DPMB vehicles (1000–9999: trams 1xxx, buses 2xxx/6xxx/7xxx, trolleybuses 3xxx); other carriers have five-digit IDs of the KORDIS system (regional buses 2xxxx, trains 3xxxx), which are not the carriers' fleet numbers and do not say which carrier runs the vehicle.
- **Train and connection numbers** come from `api.txt` in the timetable (`Linka/CVlaku = trip_id: 130/4605 = 786`): KORDIS's connection number of each trip, which for trains is the train number.
- **Car traffic and road closures** come from dopravniinfo.cz (Národní dopravní informační centrum), polled every 2 minutes: speed as a share of free-flow speed on ~1,000 road segments in Brno, and current closures and restrictions. These are the endpoints of its public map (`api.dopravniinfo.cz/TownData.ashx?town=582786`), **not a documented API**, and may change; the official equivalent is the NDIC DATEX II feed (free, needs registration at mobilitydata.rsd.cz). Geometry comes in S-JTSK (EPSG:5514) and is converted with `pyproj`. `LIVE_TRAFFIC=false` turns it off.

### Limitations

- The main map shows scheduled positions; real positions and delays are on the live map. No connections guaranteed between specific trips.
- Walking uses straight-line distance with an allowance for detours, not real footpaths.
- About 5 % of the stop-to-stop sections can't be matched to the line's route (mostly regional bus stops off the OSM route). Vehicles move in a straight line there.

---

## 3. Running locally

Requires Python 3.12 or newer.

```bash
pip install -r requirements.txt
python app.py
```

Then open <http://127.0.0.1:5000>.

- **Where the data comes from:** at startup the app uses the most recently downloaded timetable, or the `GTFS/` folder if it exists. If neither is available, it downloads the timetable first.
- **Updates:** run in the background as described in *Automatic timetable updates*.

Options:

```bash
python app.py --update                 # download the latest timetable before starting
python app.py --update --build-only    # only download and process it, then exit
python app.py --cache ./data --port 8000 --host 0.0.0.0
python app.py --url ""                 # no downloads, only data from the GTFS/ folder
```

- **Startup:** about 2–3 s with a ready cache; processing a new version takes about 10–15 s.
- **First run without `osm_cache/`:** downloads data from OSM and OSRM and takes about 7 minutes.

---

## 4. Docker

```bash
docker build -t idsjmk-map .
docker run -p 8000:8000 -v idsjmk-data:/app/data idsjmk-map
```

Then open <http://localhost:8000>.

What the image does:

- **At build time** it downloads the current timetable from KORDIS and precomputes the routes from the included `osm_cache/` (`python app.py --update --build-only`, about 15 s). The build therefore needs internet access. The container then starts in 2–3 s.
- **At runtime** it checks for new timetable versions and deploys them without a restart.
- It uses the **gunicorn** production server with 1 process and 8 threads. The whole timetable is kept in memory, so the container needs about 400 MB of RAM, briefly around twice that while switching to a new version.
- **Health check** at `GET /healthz`, which also returns the data version and source information.
- It runs as the unprivileged user `app`.
- The `GTFS/` folder is not copied into the image (it is listed in `.dockerignore`).

**A volume at `/app/data` is recommended.** It keeps downloaded timetable versions and caches across restarts and redeployments. Without it, a restarted container starts from the version that was current when the image was built and downloads the newer one again. A named volume is filled with the image's content the first time it is used.

Environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `GTFS_URL` | `https://kordis-jmk.cz/gtfs/gtfs.zip` | where to download the timetable from; an empty value turns updates off |
| `GTFS_UPDATE_INTERVAL_HOURS` | `6` | how often to check for a new version |
| `CACHE_DIR` | `/app/data` | downloaded versions (`feeds/`, `feed.json`), route caches (`network_cache.json`, `osm_cache/`) |
| `GTFS_DIR` | `/app/GTFS` | optional bundled timetable (fallback if downloading fails) |
| `PORT` | `8000` | port inside the container |
| `LIVE` | `true` | the live map (`/zive`): one WebSocket connection to the KORDIS vehicle stream and GTFS Realtime every 30 s |
| `LIVE_TRAFFIC` | `true` | car traffic and road closures in Brno on the live map (dopravniinfo.cz, every 2 minutes) |
| `UMAMI_WEBSITE_ID` | *(empty)* | [Umami](https://umami.is) website ID; when set, the tracking script is added to every page |
| `UMAMI_SCRIPT_URL` | `https://cloud.umami.is/script.js` | tracker script URL; point it at a self-hosted Umami (e.g. `https://umami.example.com/script.js`) |
| `UMAMI_DOMAINS` | *(empty)* | optional comma-separated hostnames to restrict tracking to (Umami's `data-domains`) |
| `UMAMI_RECORDER` | `false` | `true` also loads Umami's `recorder.js` for session replays and heatmaps (Umami 3.1+; enable them in Umami under *Websites → Edit → Replays & Heatmaps*) |
| `UMAMI_FRAME_ANCESTORS` | *(empty)* | space-separated origins allowed to frame the pages, needed for the heatmap overlay (e.g. `https://umami.example.com`); sends `Content-Security-Policy: frame-ancestors 'self' …` |

A `docker-compose.yml` is included as well (`docker compose up --build`); it sets up the volume. To configure it locally, copy `.env.example` to `.env` and fill in the values; Compose reads `.env` automatically (it is git- and docker-ignored).

---

## 5. Deploying to Dokploy

### Preparing the repository

Dokploy builds the app from a Git repository. **Include the `osm_cache/` folder** in the repository (currently about 66 MB; the largest file is 25 MB, within GitHub's limits). Without it, every build would download the OSM data again, which takes several minutes and puts load on public servers. The `GTFS/` folder isn't needed, because the timetable is downloaded automatically.

```bash
git init
git add .
git commit -m "IDS JMK map"
git remote add origin git@github.com:<user>/idsjmk-map.git
git push -u origin main
```

`network_cache.json`, `feeds/` and `feed.json` are not committed (they are in `.gitignore`); they are created automatically.

### Option A – Application (Dockerfile), recommended

1. In Dokploy: **Projects → Create Project**, then **Create Service → Application**.
2. **Provider:** GitHub / GitLab / Git. Choose the repository and the `main` branch.
3. **Build Type:** `Dockerfile`, Docker File: `./Dockerfile`, Docker Context Path: `.`
4. **Deploy:** the first build takes about 1–2 minutes.
5. **Domains → Add Domain:**
   - Host: your domain (e.g. `map.example.com`), Path `/`
   - **Container Port: `8000`**
   - HTTPS on, certificate *Let's Encrypt*
6. **Advanced → Volumes:** add a *Volume Mount* (a named volume, e.g. `idsjmk-data`) with the container path **`/app/data`**.
7. Recommended resources (**Advanced → Resources**): a memory limit of at least **1 GB** (because of version switching).
8. Optionally set `GTFS_UPDATE_INTERVAL_HOURS` under **Environment** (default 6).
9. Optionally enable Umami analytics under **Environment**:
   ```
   UMAMI_WEBSITE_ID=<website id from Umami>
   UMAMI_SCRIPT_URL=https://umami.example.com/script.js
   UMAMI_DOMAINS=map.example.com
   UMAMI_RECORDER=true
   UMAMI_FRAME_ANCESTORS=https://umami.example.com
   ```
   Only `UMAMI_WEBSITE_ID` is required; leave it out to disable analytics. `UMAMI_RECORDER` and `UMAMI_FRAME_ANCESTORS` are only needed for session replays and heatmaps. Session recording collects more data than page views, so mention it in your privacy notice. Changes apply after **Redeploy** (no rebuild needed).

### Option B – Docker Compose

1. **Create Service → Compose**, same repository, Compose Path `./docker-compose.yml`.
2. Under **Domains**, choose the `idsjmk-map` service and port `8000`.
3. Optionally set the `UMAMI_*` variables (see `.env.example`) under **Environment**; the compose file passes them to the container.

### After deploying

- Check `https://<domain>/healthz`. The response contains `"status": "ok"`, the data version, and in `feed` the publication date and the time of the last check.
- **Logs** show a line like `Loaded 347 lines, ... (version …, download feed)`. Each check logs either `GTFS feed unchanged` or `New GTFS feed downloaded …`.
- To redeploy automatically after `git push`, enable **Auto Deploy** (webhook) in Dokploy.

---

## 6. Timetable updates

The timetable is updated **automatically** (see *Automatic timetable updates*). Nothing needs to be done.

Manually, you can:

- **force a check now:** restart the service. The check runs 5 s after startup.
- **use another source or your own export:** set `GTFS_URL`, or turn updates off (`GTFS_URL=`) and put the timetable into the `GTFS/` folder (and remove it from `.dockerignore`).

When a new timetable contains new lines, the app fetches the missing routes from OSM or OSRM while processing it and stores them in `osm_cache/` on the volume. You can bring them into the repository from time to time by running this locally:

```bash
python app.py --update --build-only
git add osm_cache && git commit -m "Update osm_cache"
```

To download fresh routes from OpenStreetMap (about 7 minutes, needs internet):

```bash
rm -rf osm_cache network_cache.json
python app.py --update --build-only
```

---

## 7. Project structure

| File | Contents |
|---|---|
| `app.py` | Flask server, API, startup (development and gunicorn), live updates |
| `gtfs_update.py` | downloading and versioning the timetable |
| `gtfs_loader.py` | loading GTFS, building the network and its cache |
| `osm_tracks.py` | routes from OpenStreetMap, routing via OSRM and rails, stop-to-stop sections |
| `timetable.py` | service calendar, holidays, vehicle positions, daily profile |
| `reach.py` | reachability map (Connection Scan, travel-time grid) |
| `templates/index.html` | the whole web interface (Leaflet) |
| `live.py` | live state of the vehicles: vehicle stream, GTFS Realtime (trips, alerts), car traffic and closures |
| `templates/live.html` | the live map |
| `templates/_locate.html` | the "my location" button, included in both maps |
| `templates/_pwa.html` | home-screen app: manifest link, icons, service worker, install link |
| `static/sw.js`, `static/icons/` | service worker and app icons |
| `templates/about.html` | the about / help page for users |
| `GTFS/` | optional bundled timetable (fallback) |
| `osm_cache/` | downloaded OSM and OSRM data |
| `feeds/`, `feed.json` | downloaded timetable versions (created at runtime in `CACHE_DIR`) |

### API

| Endpoint | Description |
|---|---|
| `GET /` | the map |
| `GET /o-aplikaci` | about page for users (Czech): features, how-to, data sources, FAQ |
| `GET /api/network` | lines (with routes) and stops |
| `GET /api/calendar` | timetable validity and holidays |
| `GET /api/profile?date=YYYY-MM-DD` | vehicles in service per 5 minutes, by mode |
| `GET /api/vehicles?date=YYYY-MM-DD&time=<seconds since midnight>` | vehicles at a given moment |
| `GET /api/reach?date=…&time=…&stop=<stop index>&minutes=60` | reachability map |
| `GET /healthz` | health status, data version and source information |
| `GET /manifest.webmanifest`, `GET /sw.js`, `GET /offline` | home-screen app: manifest, service worker, offline page |
| `GET /zive` | live map (Czech) |
| `GET /api/live/meta` | line names and colours by KORDIS `LineID` |
| `GET /api/live/vehicles` | every vehicle heard from in the last 30 min: `[number, LineID, lat, lon, delay (min), bearing, age (s), inactive, low-floor, destination, course]` |
| `GET /api/live/vehicle/<number>` | one vehicle: state, trail, trip with stops and expected times, service alerts |
| `GET /api/live/route/<LineID>` | track and stops of a line |
| `GET /api/live/traffic/geometry` | car traffic road segments: `[id, name, road, [flat lat/lon list per part]]` |
| `GET /api/live/traffic` | car traffic now: `{segment id: [speed / free-flow speed, delay s]}` |
| `GET /api/live/events` | road closures and restrictions in Brno |

All endpoints except `/api/network` accept a `v=<version>` parameter. If the server has meanwhile switched to a newer timetable, it responds with `409` and the page offers to reload.

---

## 8. Notes for public deployment

- **Map tiles** are loaded from the OpenStreetMap servers (`tile.openstreetmap.org`). They are meant for light use and require attribution (shown in the map corner). For heavier traffic, use a commercial tile provider or your own tile server. The tile URL is in `templates/index.html` (`L.tileLayer(...)`).
- **Overpass and OSRM** are only called while processing a new timetable version, and only for data missing from `osm_cache/`. Not during normal operation.
- **Timetable download** is one conditional request every 6 hours. The full file (8 MB) is downloaded only when it has changed.
- **The live map** keeps one WebSocket connection to gis.brno.cz open, loads the ~220 kB GTFS Realtime feed every 30 seconds and about 0.9 MB from dopravniinfo.cz every 2 minutes, however many people use it. Browsers get the vehicles from this server every 10 seconds (~30 kB gzipped).
- **Timetable data:** KORDIS JMK, DPMB. **Map data:** © OpenStreetMap contributors (ODbL license). **Car traffic and closures:** Národní dopravní informační centrum (ŘSD), dopravniinfo.cz.
