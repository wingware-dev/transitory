#!/usr/bin/env python3
"""Take the committed dashboard screenshots in docs/shots/, headless.

    python tools/take_shots.py            # all shots
    python tools/take_shots.py skeleton   # just some, by name

Each later step registers its shot in SHOTS: a function that drives a
demo-mode window into the state to capture and returns the widget to grab.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("MPLBACKEND", "Agg")

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from PySide6.QtWidgets import QApplication  # noqa: E402

from harness import make_main_window, shot  # noqa: E402
from transitory.config import shots_dir  # noqa: E402


def shot_skeleton(app):
    return make_main_window()


# name -> (file name, function(app) -> widget)
SHOTS = {
    "skeleton": ("01-skeleton.png", shot_skeleton),
}


def main(argv: list[str]) -> int:
    names = argv or list(SHOTS)
    unknown = [n for n in names if n not in SHOTS]
    if unknown:
        print(f"unknown shot(s): {', '.join(unknown)}; known: {', '.join(SHOTS)}", file=sys.stderr)
        return 2
    app = QApplication.instance() or QApplication([])
    for name in names:
        filename, func = SHOTS[name]
        widget = func(app)
        app.processEvents()
        path = shot(widget, filename, shots_dir())
        print(f"wrote {path.relative_to(ROOT)}")
        widget.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
