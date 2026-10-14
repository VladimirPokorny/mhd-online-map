FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GTFS_DIR=/app/GTFS \
    CACHE_DIR=/app/data \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY collector/ collector/
COPY templates/ templates/
COPY static/ static/
# Downloaded OpenStreetMap / OSRM data. With it the track geometry is computed
# in seconds; without it everything is downloaded again (several minutes).
COPY osm_cache/ data/osm_cache/

# Download the current timetable (KORDIS JMK) and precompute line geometry, so
# the container starts in seconds. At runtime it keeps checking for new versions.
# A bundled GTFS/ folder is optional (it is excluded by .dockerignore).
RUN python app.py --update --build-only

RUN useradd --system --create-home --uid 10001 app && mkdir -p /app/raw && chown -R app /app/data /app/raw
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/healthz', timeout=4)"

# One worker: the whole timetable lives in memory (~200 MB), threads serve requests.
CMD ["sh", "-c", "exec gunicorn 'app:create_app()' --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 120 --access-logfile -"]
