"""Environment-driven settings (NFR-007). No module-level instantiation:
build via get_settings() and inject through app state (testability)."""

from __future__ import annotations

import secrets
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    max_concurrent_downloads: int = Field(default=5, ge=1)
    extract_concurrency: int = Field(default=8, ge=1)
    max_jobs_per_client: int = Field(default=3, ge=1)
    max_queued_jobs: int = Field(default=10, ge=0)
    job_ttl_seconds: int = Field(default=1800, ge=1)
    cleanup_interval_seconds: int = Field(default=60, ge=1)
    info_cache_ttl_seconds: int = Field(default=300, ge=1)
    info_cache_max_entries: int = Field(default=500, ge=1)
    max_download_bytes: int = Field(default=2_147_483_648, ge=1)
    min_free_disk_mb: int = Field(default=1024, ge=0)
    downloads_dir: Path = Path("./downloads")
    ip_hash_salt: str = ""
    log_level: str = "INFO"

    def model_post_init(self, __context: object) -> None:
        if not self.ip_hash_salt:
            # Random per boot: hashes stay unlinkable across restarts by default.
            object.__setattr__(self, "ip_hash_salt", secrets.token_hex(16))


def get_settings(**overrides: object) -> Settings:
    return Settings(**overrides)
