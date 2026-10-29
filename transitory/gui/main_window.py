"""Main dashboard window.

Skeleton layout: a sky dock (left), a selection dock (right) and a central
tab widget with Gallery and Table pages.  The sky view, selection panel,
gallery and results table steps replace the placeholders.
"""

from __future__ import annotations

import argparse
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QLabel,
    QMainWindow,
    QTabWidget,
    QWidget,
)

from ..sources import LightCurveSource

WINDOW_TITLE = "Transitory — transiting planet finder"


def _placeholder(text: str, name: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setWordWrap(True)
    label.setStyleSheet("color: #777; font-size: 14px; padding: 20px;")
    return label


class MainWindow(QMainWindow):
    def __init__(self, source: LightCurveSource | None, parent: QWidget | None = None):
        super().__init__(parent)
        self.source = source
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(1400, 900)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("resultTabs")
        self.gallery_page = _placeholder(
            "Gallery\n\nPlots for each analysed star will appear here.", "galleryPlaceholder")
        self.table_page = _placeholder(
            "Summary table\n\nPlanet candidates and confidence will appear here.", "tablePlaceholder")
        self.tabs.addTab(self.gallery_page, "Gallery")
        self.tabs.addTab(self.table_page, "Table")
        self.setCentralWidget(self.tabs)

        self.sky_dock = self._add_dock(
            "Sky", "skyDock", Qt.DockWidgetArea.LeftDockWidgetArea,
            _placeholder("Sky view\n\nZoom and click to pick stars.", "skyPlaceholder"))
        self.selection_dock = self._add_dock(
            "Selection", "selectionDock", Qt.DockWidgetArea.RightDockWidgetArea,
            _placeholder("Selected stars\n\nEnter a name or TIC / KIC number.", "selectionPlaceholder"))
        self.resizeDocks([self.sky_dock, self.selection_dock], [560, 300], Qt.Orientation.Horizontal)

        source_name = source.name if source is not None else "none"
        self.statusBar().showMessage(f"Data source: {source_name}")

    def _add_dock(self, title: str, name: str, area: Qt.DockWidgetArea, widget: QWidget) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(name)
        dock.setWidget(widget)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable
                         | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.addDockWidget(area, dock)
        return dock


def make_source(demo: bool) -> LightCurveSource | None:
    """The data source for the dashboard: synthetic in demo mode, else MAST."""
    if demo:
        from ..sources import SyntheticSource
        return SyntheticSource()
    try:
        from ..pipeline.fetch import MastSource
    except ImportError:
        # The MAST fetcher arrives in a later step; until then only --demo has data.
        print("MAST data source is not available yet; run with --demo", file=sys.stderr)
        return None
    return MastSource()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dashboard for finding transiting planets in TESS / Kepler data")
    parser.add_argument("--demo", action="store_true",
                        help="use made-up light curves (no downloads)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = MainWindow(make_source(args.demo))
    window.show()
    return app.exec()
