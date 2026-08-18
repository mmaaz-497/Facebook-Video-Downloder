"""Client identity + in-memory job store (data-model §3).

Identity for quotas and logs is sha256(salt + peer_ip). Only the ASGI peer
address is ever used; X-Forwarded-For is never parsed here (uvicorn's
--forwarded-allow-ips handles proxy topology in deployment).

Jobs live only in memory (constitutional — no database), keyed by uuid4-hex
ids, behind a single asyncio.Lock. Purge removes the record atomically with
its file; after purge every lookup is JOB_NOT_FOUND.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from app.models.errors import AppError, ErrorCode


def client_hash(ip: str, salt: str) -> str:
    """Salted hash of a peer IP — the only client identity in the system."""
    return hashlib.sha256((salt + ip).encode("utf-8")).hexdigest()


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


_LEGAL_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING}),
    JobState.RUNNING: frozenset({JobState.COMPLETED, JobState.FAILED}),
    JobState.COMPLETED: frozenset(),
    JobState.FAILED: frozenset(),
}

_ACTIVE_STATES = (JobState.QUEUED, JobState.RUNNING)


@dataclass
class Job:
    client_hash: str
    url: str
    video_id: str
    requested_height: int
    title: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: JobState = JobState.QUEUED
    progress: int = 0
    error: AppError | None = None
    file_path: Path | None = None
    created_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None

    def bump_progress(self, value: int) -> None:
        """Monotonic clamp on every write (FR-003/FR-009). Called from the
        download worker thread — a single writer per job, so the
        read-modify-write below cannot race."""
        self.progress = max(self.progress, min(100, max(0, value)))


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()

    async def create(self, job: Job) -> Job:
        async with self._lock:
            self._jobs[job.id] = job
        return job

    async def get(self, job_id: str) -> Job | None:
        async with self._lock:
            return self._jobs.get(job_id)

    async def transition(self, job_id: str, new_state: JobState, **updates) -> Job:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise AppError(ErrorCode.JOB_NOT_FOUND, "Unknown job.")
            if new_state not in _LEGAL_TRANSITIONS[job.state]:
                raise AppError(
                    ErrorCode.INTERNAL_ERROR,
                    "Illegal job state transition.",
                )
            job.state = new_state
            for key, value in updates.items():
                setattr(job, key, value)
            if new_state in (JobState.COMPLETED, JobState.FAILED):
                job.finished_at = time.monotonic()
            return job

    async def purge(self, job_id: str) -> None:
        """Remove the job record and its file together (FR-007)."""
        async with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is not None and job.file_path is not None:
            try:
                job.file_path.unlink(missing_ok=True)
            except OSError:
                pass  # the periodic sweep is the safety net

    async def active_count(self, chash: str) -> int:
        async with self._lock:
            return sum(
                1
                for j in self._jobs.values()
                if j.client_hash == chash and j.state in _ACTIVE_STATES
            )

    async def global_active(self) -> int:
        async with self._lock:
            return sum(1 for j in self._jobs.values() if j.state in _ACTIVE_STATES)

    async def queue_position(self, job: Job) -> int | None:
        """Rank among queued jobs by creation time; None once running."""
        if job.state is not JobState.QUEUED:
            return None
        async with self._lock:
            queued = sorted(
                (j for j in self._jobs.values() if j.state is JobState.QUEUED),
                key=lambda j: j.created_at,
            )
            for idx, j in enumerate(queued, start=1):
                if j.id == job.id:
                    return idx
        return None

    async def expired_ids(self, ttl_seconds: float, now: float) -> list[str]:
        """Jobs past the TTL, measured from finished_at when terminal and
        created_at otherwise (abandoned queued/running jobs expire too)."""
        async with self._lock:
            return [
                job_id
                for job_id, job in self._jobs.items()
                if now - (job.finished_at if job.finished_at is not None else job.created_at)
                >= ttl_seconds
            ]

    async def known_ids(self) -> frozenset[str]:
        """Every live job id — files whose stem is not here are orphans."""
        async with self._lock:
            return frozenset(self._jobs)

    async def counts(self) -> tuple[int, int]:
        """(running, queued) — for /api/health."""
        async with self._lock:
            running = sum(
                1 for j in self._jobs.values() if j.state is JobState.RUNNING
            )
            queued = sum(
                1 for j in self._jobs.values() if j.state is JobState.QUEUED
            )
        return running, queued
