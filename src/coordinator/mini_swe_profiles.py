"""Coordinator-owned behavior profiles layered over mini-swe-agent."""

from __future__ import annotations

from pathlib import Path


MINI_SWE_PROFILES = ("bounded", "exploratory")
ROLE_PROFILES = ("bounded", "primary-review", "exploratory")
PROFILE_DIRECTORY = Path(__file__).resolve().parent / "assets" / "mini-swe"


def profile_config(profile: str) -> Path | None:
    """Return the packaged policy overlay for a validated role profile."""

    if profile not in ROLE_PROFILES:
        raise ValueError(
            f"mini-swe-agent profile must be one of {', '.join(ROLE_PROFILES)}"
        )
    if profile == "exploratory":
        return None
    path = PROFILE_DIRECTORY / f"{profile}.yaml"
    if not path.is_file():
        raise ValueError(f"packaged mini-swe-agent profile is missing: {profile}")
    return path
