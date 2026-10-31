#!/usr/bin/env python3
"""Take the committed dashboard screenshots in docs/shots/, headless.

    python tools/take_shots.py            # all shots
    python tools/take_shots.py skeleton   # just some, by name

Each later step registers its shot in SHOTS: a function that drives a
demo-mode window into the state to capture and returns the widget to grab.
The sky shots and the data-source shot use the live archive, MAST, CDS and
STScI's archive files; when a service they need doesn't answer, the shot is
skipped and its old file kept.
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

from dashboard_host import shot_credits, shot_gallery, shot_running, shot_table  # noqa: E402
from harness import make_main_window, shot  # noqa: E402
from sky_selection_host import ShotUnavailable, shot_sky_selection, shot_sky_zoomed  # noqa: E402
from transitory.config import shots_dir  # noqa: E402


def shot_skeleton(app):
    return make_main_window()


def shot_validation(app):
    """The committed live validation report, as Validation ▸ Show validation report shows it."""
    from transitory.validation.gui import ReportDialog
    from transitory.validation.report import DOCS_DIR, REPORT_NAME

    dialog = ReportDialog(DOCS_DIR / REPORT_NAME)
    dialog.resize(1100, 900)
    dialog.show()
    return dialog


def shot_data_source(app):
    """A real star analysed on MAST data, the gallery header naming where its files came from.

    Uses the live archive, MAST and STScI's archive files, with an empty
    temporary cache so the files come from the first source rather than the
    user's cache.
    """
    import tempfile

    from dashboard_host import type_stars, wait_for_results
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from transitory.pipeline.fetch import MastSource

    cache = tempfile.TemporaryDirectory(prefix="transitory-shot-")
    old = os.environ.get("TRANSITORY_CACHE_DIR")
    os.environ["TRANSITORY_CACHE_DIR"] = cache.name
    try:
        window = make_main_window(MastSource(max_products=2))
        type_stars(window, "pi Men", timeout_s=60.0)
        if not window.panel.targets():
            raise ShotUnavailable("pi Men could not be resolved (Exoplanet Archive or MAST down?)")
        QTest.mouseClick(window.panel.submit_button, Qt.MouseButton.LeftButton)
        wait_for_results(window, timeout_s=300.0)
        analyses = window.gallery.analyses()
        if not analyses or analyses[0].raw is None:
            errors = "; ".join(analyses[0].errors) if analyses else "no result"
            raise ShotUnavailable(f"pi Men's light curve could not be fetched: {errors}")
        window.tabs.setCurrentIndex([window.tabs.tabText(i) for i in range(window.tabs.count())]
                                    .index("Gallery"))
        app.processEvents()
        window.shot_cache = cache  # removed with the window, after the shot
        return window
    finally:
        if old is None:
            os.environ.pop("TRANSITORY_CACHE_DIR", None)
        else:
            os.environ["TRANSITORY_CACHE_DIR"] = old


def shot_final(app):
    """Every demo star analysed, on the summary table: the README's picture."""
    from dashboard_host import analysed_window
    from transitory.sources import SyntheticSource

    return analysed_window([s.target.name for s in SyntheticSource().stars], tab="Table")


# name -> (file name, function(app) -> widget)
SHOTS = {
    "skeleton": ("01-skeleton.png", shot_skeleton),
    "sky-selection": ("02-sky-selection.png", shot_sky_selection),
    "running": ("03-running.png", shot_running),
    "gallery": ("04-gallery.png", shot_gallery),
    "table": ("05-table.png", shot_table),
    "credits": ("06-credits.png", shot_credits),
    "validation": ("07-validation.png", shot_validation),
    "final": ("08-final.png", shot_final),
    "sky-zoomed": ("09-sky-zoomed.png", shot_sky_zoomed),
    "data-source": ("10-data-source.png", shot_data_source),
}


def main(argv: list[str]) -> int:
    names = argv or list(SHOTS)
    unknown = [n for n in names if n not in SHOTS]
    if unknown:
        print(f"unknown shot(s): {', '.join(unknown)}; known: {', '.join(SHOTS)}", file=sys.stderr)
        return 2
    app = QApplication.instance() or QApplication([])
    skipped = []
    for name in names:
        filename, func = SHOTS[name]
        try:
            widget = func(app)
        except ShotUnavailable as exc:
            print(f"skipped {filename} (kept as it was): {exc}", file=sys.stderr)
            skipped.append(filename)
            continue
        app.processEvents()
        path = shot(widget, filename, shots_dir())
        print(f"wrote {path.relative_to(ROOT)}")
        widget.close()
    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
