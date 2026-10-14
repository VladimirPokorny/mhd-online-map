-- History of Brno open data for statistics and predictions.
-- Times are timestamptz (stored in UTC). Every table keeps the source's own values;
-- nothing is cleaned or clamped here, so analyses can choose their own filters.

-- ------------------------------------------------------------- bookkeeping ---

-- One row per job run: what ran, how long, how many rows it wrote, and why it failed.
CREATE TABLE collector_run (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job         text        NOT NULL,
    started_at  timestamptz NOT NULL,
    finished_at timestamptz,
    ok          boolean,
    rows        integer,
    note        text,          -- short summary, or the error on failure
    source_time timestamptz    -- newest timestamp in the source data, to spot stale sources
);
CREATE INDEX collector_run_job ON collector_run (job, started_at DESC);

-- Cursors of incremental jobs (last object id, ETag, …), written in the same transaction as the data.
CREATE TABLE collector_state (
    key        text PRIMARY KEY,
    value      jsonb       NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- -------------------------------------------------------- public transport ---

-- KORDIS vehicle positions: a row whenever a vehicle's line, course, last stop or delay changes.
-- Between changes the vehicle only moves along its route, so delays and stop passages are complete.
-- Sources: 1 = live stream (may have gaps), 2 = daily archive (complete; replaces the stream's day).
CREATE TABLE vehicle_sample (
    observed_at timestamptz NOT NULL,
    vehicle_id  integer     NOT NULL,   -- KORDIS vehicle ID (DPMB fleet number for 1000–9999)
    line_id     integer     NOT NULL,   -- KORDIS LineID (GTFS route_id "L<line>D…")
    route_id    integer,                -- archive only
    course      text,                   -- KORDIS course ("00101": line 1, run 01)
    last_stop   integer,                -- KORDIS stop number (the "U<number>" of a GTFS stop_id)
    final_stop  integer,
    delay_min   smallint,               -- whole minutes, negative = early
    lat_e6      integer,
    lon_e6      integer,
    bearing     smallint,
    low_floor   boolean,
    vtype       smallint,
    ltype       smallint,               -- archive only
    source      smallint    NOT NULL
) PARTITION BY RANGE (observed_at);
CREATE INDEX vehicle_sample_time ON vehicle_sample USING brin (observed_at);
-- Monthly partitions are created ahead of time by the collector (see db.ensure_partitions).

-- Which local days are stored, and from which source.
CREATE TABLE vehicle_day (
    day         date PRIMARY KEY,
    source      smallint    NOT NULL,  -- as vehicle_sample.source
    rows        integer     NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now()
);

-- Diversions, closures and moved stops of IDS JMK, as long as they are published.
CREATE TABLE ids_diversion (
    diversion_id   integer PRIMARY KEY,
    kind           text,               -- KORDIS type code (Z = moved stop, …)
    title          text,
    stop_label     text,
    affected_lines text[],
    public_text    text,
    valid_from     timestamptz,
    valid_to       timestamptz,
    updated_at     timestamptz,
    lat            double precision,
    lon            double precision,
    first_seen     timestamptz NOT NULL,
    last_seen      timestamptz NOT NULL,
    raw            jsonb       NOT NULL
);

-- ---------------------------------------------------------------- parking ---

CREATE TABLE parking_lot (
    lot      text PRIMARY KEY,   -- name as published
    lat      double precision,
    lon      double precision,
    capacity integer,
    updated  timestamptz NOT NULL
);

CREATE TABLE parking_observation (
    lot                  text        NOT NULL REFERENCES parking_lot,
    observed_at          timestamptz NOT NULL,
    capacity             integer,
    capacity_public      integer,
    free                 integer,
    subscribers_vacant   integer,
    subscribers_occupied integer,
    all_vacant           integer,
    all_occupied         integer,
    cars                 integer,
    PRIMARY KEY (lot, observed_at)
);

-- ------------------------------------------------- cyclists and pedestrians ---

-- Hourly counts. Sources have their own station ids:
-- 'live' = current layer of the counting cameras (UnitId/LocationId, direction by name),
-- 'history' = their full history since 2020 (detektor_id / smer_id).
CREATE TABLE bike_count (
    source      text        NOT NULL,
    station     text        NOT NULL,
    direction   text        NOT NULL,
    hour_start  timestamptz NOT NULL,
    cyclists    integer,
    pedestrians integer,
    total       integer,
    PRIMARY KEY (source, station, direction, hour_start)
);

CREATE TABLE bike_station (
    source    text NOT NULL,
    station   text NOT NULL,
    name      text,
    lat       double precision,
    lon       double precision,
    PRIMARY KEY (source, station)
);

-- Counting sensors of Technické sítě Brno (paths by the dam, Riviéra, …), hourly.
CREATE TABLE tsb_count (
    sensor_id   integer     NOT NULL,
    category_id integer     NOT NULL,   -- kind of traffic counted (as published)
    direction   text        NOT NULL,
    hour_start  timestamptz NOT NULL,
    count       integer,
    place       text,
    lat         double precision,
    lon         double precision,
    PRIMARY KEY (sensor_id, category_id, direction, hour_start)
);

-- ------------------------------------------------------- weather and city ---

-- Air temperature at the Teplárny Brno sites.
CREATE TABLE temperature_station (
    station_id integer PRIMARY KEY,
    name       text,
    district   text,
    lat        double precision,
    lon        double precision
);

CREATE TABLE temperature_observation (
    station_id  integer     NOT NULL REFERENCES temperature_station,
    observed_at timestamptz NOT NULL,
    temperature real,
    PRIMARY KEY (station_id, observed_at)
);

-- Hourly weather for Brno (Open-Meteo). Past hours are the model's best estimate and may be revised.
CREATE TABLE weather_hour (
    hour_start    timestamptz PRIMARY KEY,
    temperature   real,
    precipitation real,
    rain          real,
    snowfall      real,
    snow_depth    real,
    visibility    real,
    wind_speed    real,
    wind_gusts    real,
    cloud_cover   real,
    humidity      real,
    weather_code  smallint,
    source        text NOT NULL,       -- 'forecast' (recent hours) or 'archive' (reanalysis)
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- Forecasts as they were issued, so predictions can later be judged with the weather known at the time.
CREATE TABLE weather_forecast (
    issued_at     timestamptz NOT NULL,   -- the hour the forecast was fetched
    hour_start    timestamptz NOT NULL,
    temperature   real,
    precipitation real,
    rain          real,
    snowfall      real,
    visibility    real,
    wind_gusts    real,
    weather_code  smallint,
    precipitation_probability real,
    PRIMARY KEY (issued_at, hour_start)
);

-- STAREZ swimming pools, ice rinks and wellness: people inside now and today.
CREATE TABLE venue_occupancy (
    venue       text        NOT NULL,
    observed_at timestamptz NOT NULL,   -- when the source layer was last updated
    capacity    integer,
    present     integer,
    today       integer,
    PRIMARY KEY (venue, observed_at)
);

-- Planned and unplanned water supply outages (Brněnské vodárny a kanalizace).
CREATE TABLE water_outage (
    outage_id          text PRIMARY KEY,
    starts_at          timestamptz,
    ends_at            timestamptz,
    supply_restricted  boolean,
    replacement        text,
    municipality       text,
    street             text,
    note               text,
    lat                double precision,
    lon                double precision,
    first_seen         timestamptz NOT NULL,
    last_seen          timestamptz NOT NULL
);

-- Cultural events in Brno (GoToBrno / TIC Brno).
CREATE TABLE city_event (
    event_id   text PRIMARY KEY,
    name       text,
    categories text,
    starts_at  timestamptz,
    ends_at    timestamptz,
    url        text,
    lat        double precision,
    lon        double precision,
    first_seen timestamptz NOT NULL,
    last_seen  timestamptz NOT NULL,
    raw        jsonb       NOT NULL
);
