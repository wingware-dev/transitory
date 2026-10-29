"""Paths and settings shared by the pipeline, catalog and GUI."""

from __future__ import annotations

import os
from pathlib import Path

from . import __version__

PROJECT_ROOT = Path(__file__).resolve().parent.parent

USER_AGENT = f"transitory/{__version__} (transit-search dashboard; research use)"


def _dir_from_env(var: str, default: Path) -> Path:
    value = os.environ.get(var)
    path = Path(value).expanduser() if value else default
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    """Directory for downloaded data; $TRANSITORY_CACHE_DIR or ./cache."""
    return _dir_from_env("TRANSITORY_CACHE_DIR", PROJECT_ROOT / "cache")


def reports_dir() -> Path:
    """Directory for generated reports; $TRANSITORY_REPORTS_DIR or ./reports."""
    return _dir_from_env("TRANSITORY_REPORTS_DIR", PROJECT_ROOT / "reports")


def shots_dir() -> Path:
    """Committed screenshots of the dashboard."""
    path = PROJECT_ROOT / "docs" / "shots"
    path.mkdir(parents=True, exist_ok=True)
    return path


# Search defaults, shared by pipeline steps.
MIN_PERIOD_DAYS = 0.3
MAX_TRANSIT_DURATION_HOURS = 13.0
MAX_PLANETS_PER_STAR = 3
