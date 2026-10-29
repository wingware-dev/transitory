"""Headless GUI harness shared by tests and tools/take_shots.py.

``main_window`` is a dashboard wired to ``SyntheticSource`` (no network);
``shot(widget, name)`` saves what the widget looks like as a PNG.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from transitory.sources import SyntheticSource

SHOT_SIZE = (1400, 900)


def shot(widget, name: str, directory: Path | str) -> Path:
    """Save ``widget`` as ``directory/name.png`` and return the path."""
    from PySide6.QtWidgets import QApplication

    QApplication.processEvents()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name if name.endswith(".png") else f"{name}.png")
    if not widget.grab().save(str(path)):
        raise OSError(f"could not save screenshot to {path}")
    return path


def make_main_window(source=None):
    """A sized, shown dashboard on ``source`` (default: SyntheticSource)."""
    from transitory.gui.main_window import MainWindow

    window = MainWindow(source if source is not None else SyntheticSource())
    window.resize(*SHOT_SIZE)
    window.show()
    return window


@pytest.fixture
def synthetic_source():
    return SyntheticSource()


@pytest.fixture
def main_window(qtbot, synthetic_source):
    window = make_main_window(synthetic_source)
    qtbot.addWidget(window)
    qtbot.waitExposed(window)
    return window
