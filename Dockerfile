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
CMD ["python", "run.py"]
