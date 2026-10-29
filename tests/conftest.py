"""Test setup: headless Qt, non-interactive matplotlib, per-test cache dirs.

The environment is set before anything imports Qt or matplotlib, so GUI tests
never touch the desktop on macOS, Linux or Windows.
"""

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("MPLBACKEND", "Agg")

import pytest  # noqa: E402

from harness import main_window, synthetic_source  # noqa: E402,F401  (fixtures)


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path, monkeypatch):
    """Keep caches and reports per test, so parallel (xdist) runs never share files."""
    monkeypatch.setenv("TRANSITORY_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("TRANSITORY_REPORTS_DIR", str(tmp_path / "reports"))


def pytest_collection_modifyitems(config, items):
    """Skip network tests unless they were selected explicitly with ``-m network``."""
    if "network" in (config.getoption("-m") or "") and "not network" not in config.getoption("-m"):
        return
    skip = pytest.mark.skip(reason="needs network; run with -m network")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)
