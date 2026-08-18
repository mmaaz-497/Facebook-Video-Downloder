# fb-video-downloader

A small, single-server web app that downloads **one public Facebook video at a
time** as an MP4. Paste a video URL, see the title/thumbnail/duration and the
actual available resolutions, pick one, and receive the muxed file — which is
deleted from the server the moment it is delivered.

Built with FastAPI + yt-dlp + FFmpeg, a plain HTML/CSS/JS frontend served from
the same process, an in-memory job store, and a single uvicorn worker. No
database, no queue broker, no accounts.

> ## ⚠️ Public videos only — this service never authenticates
>
> This tool downloads **only publicly accessible Facebook videos**. It holds no
> cookies, no sessions, no credentials, and it **will never log in to
> Facebook** — that ban is constitutional and non-negotiable (see
> `.specify/memory/constitution.md`, Principle I). If a video requires a login,
> the API answers `403 AUTH_REQUIRED` and stops. **Failure is the feature.**
> Use it only for content you are authorized to download.

## Quickstart — local (venv)

Prerequisites: Python 3.11+ and FFmpeg on PATH
(`winget install Gyan.FFmpeg` / `apt install ffmpeg`).

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate      POSIX: source .venv/bin/activate
pip install -e ".[dev]"
uvicorn --factory app.main:create_app --reload --port 8000
```

Open http://localhost:8000 — the frontend is served by the same process.

Run the test suite (fully offline — the extractor is mocked; no test ever
talks to Facebook or any CDN):

```bash
python -m pytest
```

## Quickstart — Docker

```bash
docker build -t fb-video-downloader .
docker run --rm -p 127.0.0.1:8000:8000 --tmpfs /downloads fb-video-downloader
```

`--tmpfs /downloads` keeps downloaded files in RAM only — ephemeral by
construction. For production, put the container behind the Caddy config in
`deploy/Caddyfile` (automatic TLS) and set `FORWARDED_ALLOW_IPS` to the
proxy's address; the app itself never parses `X-Forwarded-For`.

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/info?url=<fb-url>` | Inspect a video: title, thumbnail (proxied), duration, width/height, available resolutions |
| POST | `/api/download` | Body `{"url": ..., "resolution": <height>}` → `202 {"job_id": ...}` |
| GET | `/api/status/{job_id}` | Job state (`queued`/`running`/`completed`/`failed`), progress 0–100, queue position, typed error |
| GET | `/api/file/{job_id}` | Collect the finished MP4; the file **and** the job record are purged after delivery |
| GET | `/api/thumb?video_id=<id>` | Thumbnail proxy (the CDN URL never reaches the browser) |
| GET | `/api/health` | Liveness: FFmpeg presence, extractor self-check, running/queued counts, `accepting_new` |

Interactive OpenAPI docs: `/docs`.

Every failure, on every endpoint, has exactly the shape
`{"code": "<STABLE_CODE>", "message": "<human readable>"}`.

### Error codes

| Code | HTTP | Meaning |
|------|------|---------|
| `INVALID_URL` | 422 | Not a syntactically valid HTTP(S) URL |
| `UNSUPPORTED_URL` | 422 | Valid URL but not a supported public Facebook video link |
| `VIDEO_UNAVAILABLE` | 404 | Video deleted, region-blocked, or nonexistent |
| `AUTH_REQUIRED` | 403 | Video needs a login — unsupported by policy, permanently |
| `EXTRACTION_FAILED` | 502 | The extractor could not produce a downloadable stream |
| `RATE_LIMITED` | 503 | Facebook is throttling; `Retry-After` header included |
| `RESOLUTION_UNAVAILABLE` | 422 | Requested height not in the extracted format list (no silent substitution — ever) |
| `NETWORK_ERROR` | 502 | Upstream network failure (retried ≤2× internally) |
| `FFMPEG_MISSING` | 500 | FFmpeg not installed on the server; downloads disabled |
| `JOB_NOT_FOUND` | 404 | Unknown or already-purged job id |
| `JOB_NOT_READY` | 409 | File requested before the job completed |
| `TOO_MANY_JOBS` | 429 | Per-client active-job quota reached |
| `SERVER_BUSY` | 503 | Global capacity reached; the request left no trace |
| `INSUFFICIENT_STORAGE` | 507 | Server disk below the free-space floor |
| `INTERNAL_ERROR` | 500 | Unmapped failure; traceback stays server-side |

## Configuration

All tunables are environment variables with working defaults (see
`backend/.env.example`; optional `.env` in `backend/`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `MAX_CONCURRENT_DOWNLOADS` | `5` | Parallel download slots (worker pool + semaphore) |
| `EXTRACT_CONCURRENCY` | `8` | Parallel metadata extractions |
| `MAX_JOBS_PER_CLIENT` | `3` | Active jobs allowed per (hashed) client |
| `MAX_QUEUED_JOBS` | `10` | Global queue depth beyond the running slots |
| `JOB_TTL_SECONDS` | `1800` | Jobs and files expire together after this |
| `CLEANUP_INTERVAL_SECONDS` | `60` | Sweep period for TTL/orphan cleanup |
| `INFO_CACHE_TTL_SECONDS` | `300` | Metadata cache lifetime |
| `INFO_CACHE_MAX_ENTRIES` | `500` | Metadata cache LRU bound |
| `MAX_DOWNLOAD_BYTES` | `2147483648` | Hard byte cap, enforced while streaming |
| `MIN_FREE_DISK_MB` | `1024` | Refuse downloads below this free-disk floor |
| `DOWNLOADS_DIR` | `./downloads` | Ephemeral file staging (tmpfs-friendly) |
| `IP_HASH_SALT` | random at boot | Salt for hashed client identity; unset = unlinkable across restarts |
| `LOG_LEVEL` | `INFO` | Logging level (video ids appear at DEBUG only) |

A drift-guard test (`backend/tests/unit/test_config_defaults.py`) pins these
defaults to the documented values.

## Privacy

No download history, no per-URL analytics, no telemetry. Client IPs are salted
and hashed before they can appear in any log line; signed CDN URLs and raw
payloads are never logged at any level; files and job records co-expire on one
TTL and are purged immediately after delivery.

## More

- Feature docs: `specs/001-fb-video-download/` (spec, plan, data model, quickstart)
- Project principles: `.specify/memory/constitution.md`
