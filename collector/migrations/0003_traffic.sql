-- Car traffic in Brno from dopravniinfo.cz (Národní dopravní informační centrum): speed and delay on road
-- segments, measured from vehicles driving through them, and road events (closures, roadworks, accidents).
-- These are the endpoints the public dopravniinfo.cz map uses, not a documented API.

-- Road segments with traffic data. Geometry decoded from the source's own encoding (kept in geo).
CREATE TABLE traffic_segment (
    segment_id    bigint PRIMARY KEY,
    section_name  text,
    road_class    smallint,             -- as published: 0 motorway, 1–3 road classes I–III, 4 local roads
    road_number   text,                 -- 'D1', '42', …; NULL for local roads
    chainage_from integer,              -- metres along the road, numbered roads only
    chainage_to   integer,
    direction     smallint,
    length_m      integer,
    geo           text,                 -- the source's encoding: polyline-encoded S-JTSK metres
    geometry      jsonb,                -- [[[lat, lon], …], …]: one list per part
    first_seen    timestamptz NOT NULL
);

-- Traffic on a segment: a row whenever its traffic level or its delay in whole minutes changes, or it
-- leaves or returns to the feed. Between rows the segment stays on that level and delay, so jams are
-- complete: when they begin and end, and how bad they got. A segment leaves the feed when no vehicle
-- has passed it for a few minutes; that is a row with level NULL.
-- The collector writes a row for every segment when it starts, as it does not know the stored state.
CREATE TABLE traffic_sample (
    segment_id     bigint      NOT NULL,
    observed_at    timestamptz NOT NULL,  -- when the source measured it; for level NULL, when it was missed
    level          smallint,              -- as published (VD10), by speed / free-flow speed: 1 ≥ 85 %,
                                          -- 2 75–85 %, 3 50–75 %, 4 20–50 %, 5 below 20 %
    speed          smallint,              -- km/h (VD2)
    free_flow      smallint,              -- km/h, the segment's usual speed without traffic (VD5)
    travel_time    integer,               -- s (VD3)
    free_flow_time integer,               -- s (VD6)
    delay_s        integer,               -- s, travel_time − free_flow_time (VD4)
    samples        smallint,              -- VD1 as published; the number of measurements behind the values
    PRIMARY KEY (segment_id, observed_at)
);
CREATE INDEX traffic_sample_time ON traffic_sample USING brin (observed_at);

-- Road events in and around Brno: closures, restrictions, roadworks, accidents; every one ever published,
-- with the first and last time it was seen (accidents last as long as they were published).
CREATE TABLE road_event (
    event_id    bigint PRIMARY KEY,
    guid        text,
    version     integer,
    title       text,                   -- kind of event: 'Nehody', 'Práce na silnici', …
    icon        text,                   -- as published; 'p_' prefix = planned
    locality    text,
    description text,
    valid_from  timestamptz,
    valid_to    timestamptz,
    road_number text,
    chainage    integer,
    direction   smallint,
    lat         double precision,
    lon         double precision,
    geometry    jsonb,                  -- affected stretch, as traffic_segment.geometry
    first_seen  timestamptz NOT NULL,
    last_seen   timestamptz NOT NULL,
    raw         jsonb       NOT NULL
);
