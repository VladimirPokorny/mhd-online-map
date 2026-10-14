-- Measurements of the ČHMÚ stations in Brno (opendata.chmi.cz, climate data), as published.
-- A series is one element of one station at one period (e.g. Tuřany, 10-minute temperature);
-- elements a station adds later become new series, with no schema change.

CREATE TABLE chmi_station (
    station    text PRIMARY KEY,     -- WIGOS station id, e.g. 0-20000-0-11723 (Brno, Tuřany)
    gh_id      text,                 -- ČHMÚ id, e.g. B2BTUR01
    name       text,
    lat        double precision,
    lon        double precision,
    elevation  real,
    begin_date date
);

-- What each station measures: name and unit of every element (from the published metadata).
CREATE TABLE chmi_element (
    station  text NOT NULL REFERENCES chmi_station,
    period   text NOT NULL,          -- '10m', '1h' or '1d'
    element  text NOT NULL,          -- ČHMÚ abbreviation: T, H, P, Td, N, SRA, …
    name     text,
    unit     text,
    height   real,                   -- height of the sensor above ground, m
    PRIMARY KEY (station, period, element)
);

CREATE TABLE chmi_series (
    series_id smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    station   text NOT NULL REFERENCES chmi_station,
    period    text NOT NULL,                 -- '10m', '1h' or '1d'
    element   text NOT NULL,
    vtype     text NOT NULL DEFAULT '',      -- daily values: observation term ('07:00', 'AVG', …)
    UNIQUE (station, period, element, vtype)
);

-- About 2.4 million rows a year for the four stations; the keys live in chmi_series to keep rows small.
CREATE TABLE chmi_observation (
    series_id   smallint    NOT NULL REFERENCES chmi_series,
    observed_at timestamptz NOT NULL,   -- end of the interval (daily: the published time, mostly 06:00 UTC)
    value       real        NOT NULL,
    flag        text,                   -- ČHMÚ flag (see the meta3 metadata), e.g. T = trace of rain
    quality     smallint,               -- 0 good, 1 suspect, 2 poor, 3 estimated, 5 unknown
    PRIMARY KEY (series_id, observed_at)
);

-- The series with their keys, for queries.
CREATE VIEW chmi_value AS
    SELECT s.station, s.period, s.element, s.vtype, o.observed_at, o.value, o.flag, o.quality
    FROM chmi_observation o JOIN chmi_series s USING (series_id);
