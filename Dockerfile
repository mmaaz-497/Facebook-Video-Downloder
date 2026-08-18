# fb-video-downloader — single-process image (Constitution II: one uvicorn
# worker, no extra infrastructure). TLS terminates at the reverse proxy
# (deploy/Caddyfile); client IPs reach the app only via uvicorn's
# --forwarded-allow-ips handling — app code never parses X-Forwarded-For.

FROM python:3.11-slim

# FFmpeg is a hard runtime requirement (muxing); /api/health reports it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /usr/sbin/nologin app

WORKDIR /srv/backend
COPY backend/pyproject.toml ./
COPY backend/app ./app
COPY frontend /srv/frontend
# Editable install keeps the package rooted at /srv/backend/app so the
# factory resolves /srv/frontend for static serving.
RUN pip install --no-cache-dir -e .

# Downloads are ephemeral by construction: run with --tmpfs /downloads so
# nothing ever reaches persistent storage (files are TTL-purged regardless).
ENV DOWNLOADS_DIR=/downloads
RUN mkdir -p /downloads && chown app:app /downloads
VOLUME ["/downloads"]

# Set to the reverse proxy's address (e.g. the Docker bridge or Caddy host).
ENV FORWARDED_ALLOW_IPS=127.0.0.1

USER app
EXPOSE 8000

# Single worker, factory app — the app object is create_app(), not a module
# attribute. exec so uvicorn receives signals as PID 1.
CMD ["sh", "-c", "exec uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000 --forwarded-allow-ips ${FORWARDED_ALLOW_IPS}"]
