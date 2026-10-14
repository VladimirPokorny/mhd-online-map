"""Collector of Brno open data history into PostgreSQL.

Live sources on data.brno.cz mostly publish only the current state (free parking spaces,
people in a swimming pool, the last hour of a bike counter), and the KORDIS archive of
vehicle positions keeps just 7 days. This service polls them on a schedule and keeps
everything in PostgreSQL, as the basis for statistics and predictions.

Run:      python -m collector run            (all jobs, forever; one instance per database)
One job:  python -m collector once parking   (for testing and backfills)
Status:   python -m collector status         (last run of every job, rows, errors, data age)
Health:   python -m collector health         (exit code 1 when jobs fail or data stop coming)
Schema:   python -m collector migrate

Configuration: DATABASE_URL (required), see collector/config.py for the rest.
"""
