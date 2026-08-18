"""Config-default drift guard: Settings() defaults and .env.example must both
match the documented tunable defaults (data-model.md §6 / spec NFR-007) —
verbatim. Any drift in either place fails here before it ships."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings

# The 13 NFR-007 tunables exactly as documented in data-model.md §6.
DOCUMENTED_DEFAULTS = {
    "max_concurrent_downloads": 5,
    "extract_concurrency": 8,
    "max_jobs_per_client": 3,
    "max_queued_jobs": 10,
    "job_ttl_seconds": 1800,
    "cleanup_interval_seconds": 60,
    "info_cache_ttl_seconds": 300,
    "info_cache_max_entries": 500,
    "max_download_bytes": 2_147_483_648,
    "min_free_disk_mb": 1024,
    "downloads_dir": Path("./downloads"),
    # ip_hash_salt: random at boot if unset — asserted separately
    "log_level": "INFO",
}

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ambient environment must not leak into default assertions."""
    for name in [*DOCUMENTED_DEFAULTS, "ip_hash_salt"]:
        monkeypatch.delenv(name.upper(), raising=False)


def test_settings_defaults_match_documented_values(clean_env) -> None:
    settings = Settings(_env_file=None)
    for field, expected in DOCUMENTED_DEFAULTS.items():
        assert getattr(settings, field) == expected, (
            f"Settings.{field} default drifted from data-model.md §6: "
            f"expected {expected!r}, got {getattr(settings, field)!r}"
        )


def test_ip_hash_salt_random_per_boot_when_unset(clean_env) -> None:
    a = Settings(_env_file=None)
    b = Settings(_env_file=None)
    assert a.ip_hash_salt and b.ip_hash_salt
    assert a.ip_hash_salt != b.ip_hash_salt  # unlinkable across restarts


def test_env_example_matches_documented_defaults() -> None:
    assert ENV_EXAMPLE.is_file(), ".env.example is part of the NFR-007 contract"
    example: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        example[key.strip()] = value.strip()

    for field, expected in DOCUMENTED_DEFAULTS.items():
        env_name = field.upper()
        if field == "downloads_dir":
            expected = "./downloads"  # documented literal; str(Path) normalizes
        assert env_name in example, f".env.example is missing {env_name}"
        assert example[env_name] == str(expected), (
            f".env.example {env_name} drifted from data-model.md §6: "
            f"expected {expected}, got {example[env_name]}"
        )
    # IP_HASH_SALT stays commented out (random at boot); no other stray keys.
    assert "IP_HASH_SALT" not in example
    assert set(example) == {f.upper() for f in DOCUMENTED_DEFAULTS}
