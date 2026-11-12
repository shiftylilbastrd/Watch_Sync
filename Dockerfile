FROM python:3.12-slim

WORKDIR /srv
COPY sync.py .

ENV PLEX_URL=http://192.168.1.13:32400
ENV PLEX_PREFS_GLOB_BASE=/appdata
ENV CONFIG_PATH=/config/rules.json
ENV STATE_PATH=/data/state.json
ENV POLL_INTERVAL_SECONDS=45

CMD ["python", "sync.py"]
