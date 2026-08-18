<!--
SYNC IMPACT REPORT
==================
Version change: (unversioned template) → 1.0.0
Bump rationale: MAJOR — initial ratification of a previously unfilled template.

Modified principles:
  - [PRINCIPLE_1_NAME] → I. Legal & Ethical Use (NON-NEGOTIABLE)
  - [PRINCIPLE_2_NAME] → II. Simplicity First
  - [PRINCIPLE_3_NAME] → III. Fail Loudly
  - [PRINCIPLE_4_NAME] → IV. Async by Design
  - [PRINCIPLE_5_NAME] → V. Clear API Contract
  - [PRINCIPLE_6_NAME] → VI. Privacy by Default

Added sections:
  - Technology Constraints (was [SECTION_2_NAME])
  - Development Workflow & Quality Gates (was [SECTION_3_NAME])
  - Governance (filled)

Removed sections: none

Templates requiring updates:
  - ✅ .specify/templates/plan-template.md — "Constitution Check" gate is generic
    ("[Gates determined based on constitution file]"); resolves against this file at
    plan time. No edit required.
  - ✅ .specify/templates/spec-template.md — no constitution-coupled sections.
  - ✅ .specify/templates/tasks-template.md — no constitution-coupled task categories.
  - ✅ CLAUDE.md — references constitution by path only; still accurate.
  - ⚠ README.md — not present in repo. Create at project scaffold time and link
    Principles I and VI (legal use + privacy) in user-facing docs.

Deferred TODOs: none
-->

# fb-video-downloader Constitution

## Core Principles

### I. Legal & Ethical Use (NON-NEGOTIABLE)

This project downloads **only** publicly accessible Facebook videos, or videos the
requesting user owns or is otherwise authorized to download.

The following are permanently banned and MUST NOT appear in this codebase, its
configuration, its documentation, or its dependencies:

- Cookie jars, session tokens, `Authorization` headers, or any other credential
  passed to Facebook — including via `yt-dlp` options such as `cookiefile`,
  `cookiesfrombrowser`, or `http_headers` carrying auth material.
- Any technique that bypasses, defeats, or negotiates Facebook's login wall.
- Retrieval of private, friends-only, group-restricted, or otherwise gated content.
- Credential prompts, credential storage, or credential forwarding of any kind.

If extraction fails because content requires authentication, the correct behavior is
to return the typed error `AUTH_REQUIRED` and stop. Failure is the feature.

**Rationale:** The legal and ethical defensibility of this tool rests entirely on the
public-content boundary. Any authentication capability, even unused, converts a
lawful utility into a circumvention tool. This principle is constitutional: it MUST
NOT be waived by a plan, a task, a feature request, or a `Complexity Tracking` entry.

### II. Simplicity First

The system is a single-process, single-server application. The following MUST NOT be
introduced:

- Databases of any kind (SQL, NoSQL, embedded, or file-backed persistence layers)
- Redis, RabbitMQ, or any external broker
- Celery, RQ, or any distributed task queue
- Authentication, authorization, accounts, or session management
- SPA frameworks (React, Vue, Svelte, Angular) or client-side build tooling

The system MUST use: an in-memory job store, a plain HTML/CSS/vanilla-JS frontend,
and a single `uvicorn` worker.

Any proposal to add infrastructure MUST first demonstrate that the in-memory,
single-worker design provably cannot meet a stated, measured requirement. Anticipated
future scale is not sufficient justification.

**Rationale:** The problem is small: accept a URL, extract, mux, deliver one file,
delete it. Infrastructure added "for later" is permanent operational cost paid against
a benefit that may never arrive. Single-worker also makes the in-memory job store
correct by construction — a constraint we accept deliberately.

### III. Fail Loudly

Every failure path MUST map to a typed error code with a stable, documented HTTP
status. Error codes are part of the public contract and MUST NOT change meaning
across versions.

- No silent fallbacks. If the requested resolution is unavailable, the API MUST
  return an error (`RESOLUTION_UNAVAILABLE`), never a different resolution.
- No silent format substitution, no "best effort" degradation, no partial results
  presented as complete.
- Bare `except:` and `except Exception: pass` are prohibited. Every caught exception
  MUST be either re-raised or translated into a typed error code.
- Unmapped exceptions surface as `INTERNAL_ERROR` (HTTP 500) and MUST be logged with
  a stack trace.

**Rationale:** A downloader that quietly hands back 480p when 1080p was requested
produces a user who cannot trust any result it returns. Silent substitution destroys
more value than an honest failure.

### IV. Async by Design

All routes are `async def`. The event loop MUST NOT block.

- `yt-dlp` is a blocking library. Every call into it MUST run via
  `loop.run_in_executor()` against a bounded `ThreadPoolExecutor` with an explicit
  `max_workers`.
- Concurrent extraction and download work MUST be gated by `asyncio.Semaphore`, with
  limits sourced from configuration, not hardcoded at call sites.
- Blocking filesystem, subprocess (FFmpeg), and network calls in request paths MUST
  likewise be offloaded to the executor.
- No unbounded executors, no `ThreadPoolExecutor()` without `max_workers`, no
  fire-and-forget tasks without a tracked handle and an error callback.

**Rationale:** A single uvicorn worker means one blocked coroutine stalls every
concurrent user. Bounded pools plus semaphores also cap memory, disk, and outbound
bandwidth — the same mechanism that keeps the server responsive keeps it affordable.

### V. Clear API Contract

- All request and response bodies MUST be typed Pydantic v2 models. No bare `dict`
  returns from routes.
- Every error response MUST have exactly the shape
  `{ "code": "<STABLE_CODE>", "message": "<human readable>" }`. No additional
  top-level keys; no variant error shapes per endpoint.
- The frontend MUST depend only on this published contract. It MUST NOT rely on
  undocumented fields, incidental key ordering, or error-message string matching.
- Contract changes MUST be reflected in the OpenAPI schema and in the frontend in the
  same change set.

**Rationale:** The frontend and backend ship together from one process, which makes it
tempting to couple them informally. A single explicit contract is what keeps the API
independently testable and the frontend replaceable.

### VI. Privacy by Default

The server MUST NOT retain user activity.

- No download history, no per-URL analytics, no telemetry on submitted URLs.
- Downloaded files are ephemeral: deleted immediately after successful delivery, and
  purged by a TTL sweeper regardless of delivery outcome.
- Client IPs MUST be hashed (salted) before appearing in any log line.
- Facebook video IDs MUST be logged at `DEBUG` level only, never at `INFO` or above.
- The following MUST NEVER be logged at any level: tokens or credentials of any kind,
  raw request/response payloads, and signed CDN URLs.
- Job records in the in-memory store MUST expire on the same TTL as their files.

**Rationale:** Data not collected cannot be leaked, subpoenaed, or misused. Signed CDN
URLs in particular are bearer credentials with real lifetimes — logging one is
equivalent to logging the video itself.

## Technology Constraints

**Runtime and framework**
- Python 3.11+
- FastAPI with Pydantic v2
- `uvicorn`, single worker

**Extraction engine**
- `yt-dlp`, used as a Python library — never invoked as a subprocess.
- The extraction layer MUST be wrapped behind a single module exposing one interface.
  **No module outside that wrapper may `import yt_dlp`.** This boundary is enforced by
  test and by review; violating it is a blocking review finding.

**Rationale:** `yt-dlp`'s Facebook extractor is the single most breakage-prone
dependency in the system. Confining it to one module keeps the engine swappable
without touching routes, job management, or the frontend.

**Media processing**
- FFmpeg, external binary, required for muxing separate audio/video streams.
- Its presence MUST be verified by `GET /api/health`, which reports unhealthy when
  FFmpeg is missing or unusable.

**Frontend**
- Plain HTML, CSS, and vanilla JavaScript, served by the same FastAPI process.
- No bundler, transpiler, npm dependency tree, or CDN-loaded framework.

**Testing**
- `pytest`. `yt-dlp` MUST be mocked at the wrapper boundary defined above.
- **No test may make a live network call to Facebook or any CDN.** Tests MUST pass
  offline.

**Security posture**
- URL validation MUST run **before any network work** and MUST be SSRF-conscious:
  reject non-HTTP(S) schemes, credentials in URLs, IP-literal hosts, and any host
  resolving to a private, loopback, link-local, or reserved range.
- A host allowlist restricted to `facebook.com` (and its documented regional and
  `www`/`m`/`web` subdomain variants) plus `fb.watch`. Allowlist matching MUST be
  exact-suffix on parsed hostnames — never substring matching.
- Downloads MUST be capped by `MAX_DOWNLOAD_BYTES`, enforced **while streaming**, not
  by trusting a `Content-Length` header. Exceeding the cap aborts the job and deletes
  partial output.
- On-disk filenames MUST be server-generated UUIDs. Facebook-supplied titles and
  filenames MUST NOT influence any filesystem path. Titles may appear only in a
  sanitized `Content-Disposition` header.
- Secrets and tunables come from environment/`.env`. No secret is ever committed.

**Deployment**
- Local development first; then Docker running `uvicorn` behind Caddy or nginx on a
  single VPS. TLS terminates at the reverse proxy.

## Development Workflow & Quality Gates

**Planning gate.** Every `/sp.plan` MUST include a Constitution Check evaluating all
six principles. Principle I violations are unconditionally blocking and MUST NOT be
recorded in `Complexity Tracking` as a justified exception. Violations of II–VI MUST
be either resolved or explicitly justified with a documented rationale.

**Change size.** Prefer the smallest viable diff. Unrelated refactoring MUST be a
separate change set.

**Review checklist.** Every review MUST verify:
- [ ] No credential, cookie, or session material is passed toward Facebook (I)
- [ ] No banned infrastructure introduced (II)
- [ ] Every new failure path has a typed code and a stable status (III)
- [ ] No silent quality or format substitution (III)
- [ ] Every `yt-dlp`, FFmpeg, and blocking-IO call is offloaded and bounded (IV)
- [ ] Request/response models are typed; errors match `{code, message}` (V)
- [ ] Nothing prohibited is logged; new files have TTL and cleanup (VI)
- [ ] `yt_dlp` is imported only inside the extraction wrapper

**Testing gate.** Changes touching extraction, job lifecycle, URL validation, or error
mapping MUST ship with tests. The suite MUST pass with no network access.

**Documentation.** Public contract changes (routes, models, error codes) MUST update
the OpenAPI schema and any affected user-facing documentation in the same change set.

## Governance

This constitution supersedes all other development practices, conventions, and
preferences. Where a plan, task list, style guide, or reviewer preference conflicts
with this document, this document wins.

**Amendment procedure.** Amendments MUST be proposed as a change to this file,
including: the text being changed, the rationale, and the resulting version bump.
Amendments require explicit maintainer approval before merge. Any amendment that
invalidates existing code MUST include a migration plan in the same change set.

**Principle I is not amendable by ordinary procedure.** Removing or weakening the
legal/ethical boundary — including any relaxation of the authentication ban —
constitutes a different project and requires a MAJOR bump with an accompanying
written legal-risk assessment.

**Versioning policy.** Semantic versioning applies to this document:
- **MAJOR** — a principle is removed, or redefined in a backward-incompatible way.
- **MINOR** — a principle or normative section is added, or guidance materially
  expanded.
- **PATCH** — clarification, wording, or typo fixes with no change in obligation.

**Compliance review.** Constitution compliance is verified at plan time (Constitution
Check) and at review time (checklist above). Complexity MUST be justified in writing,
never assumed. Runtime development guidance for agents lives in `CLAUDE.md`; where it
and this document disagree, this document governs.

**Version**: 1.0.0 | **Ratified**: 2026-07-18 | **Last Amended**: 2026-07-18
