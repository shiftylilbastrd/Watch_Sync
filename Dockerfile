FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PLEX_URL=http://192.168.1.13:32400
ENV PLEX_PREFS_GLOB_BASE=/appdata
ENV DB_PATH=/data/watch_sync.db
ENV POLL_INTERVAL_SECONDS=45

EXPOSE 8099
# --workers 1 is load-bearing, not just a size choice: run.py starts the
# sync-loop background thread once at import time, so more than one
# worker process would mean more than one sync loop polling Plex and
# scrobbling in parallel. --threads gives the web UI concurrency without
# touching that invariant.
CMD ["gunicorn", "--bind", "0.0.0.0:8099", "--workers", "1", "--threads", "4", "--timeout", "60", "run:app"]
