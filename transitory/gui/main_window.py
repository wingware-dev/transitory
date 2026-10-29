"""Main dashboard window.

The sky view (left dock) and selection panel (right dock) pick stars; Submit
queues them on an ``AnalysisRunner`` whose progress shows in the Progress
dock (bottom); each finished analysis goes to the Gallery and Table tabs.
Double-clicking a table row jumps to that star's plots.

With a ``SyntheticSource`` (``--demo``) nothing touches the network: the sky
shows the demo stars, names resolve against them, and TIC field loading is
off.  Otherwise the sky loads planet hosts from the NASA Exoplanet Archive
once the window is shown, names resolve through ``catalog.resolve`` and
light curves come from MAST.
"""

from __future__ import annotations

import argparse
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QLabel,
    QMainWindow,
    QMessageBox,
    QTabWidget,
    QWidget,
)

from ..catalog.client import CatalogClient
from ..catalog.resolve import split_names
from ..catalog.skycatalog import SkyCatalog, SkyStar
from ..models import StarAnalysis, StarTarget
from ..sources import LightCurveSource, SyntheticSource
from ..validation.gui import ValidationController
from .credits_dialog import CreditsDialog
from .gallery import GalleryWidget
from .results_table import ResultsTable
from .selection_panel import Resolver, SelectionPanel
from .sky_view import SkyView
from .workers import DEFAULT_WORKERS, AnalysisRunner, ProgressPanel

WINDOW_TITLE = "Transitory — transiting planet finder"

# Sectors / quarters used per star from MAST.  20 keeps every Kepler quarter
# (long periods need the whole 4-year baseline) and bounds TESS CVZ stars,
# which can have 40+ sectors, to about 1.5 years of data.
DEFAULT_MAX_PRODUCTS = 20
MAX_PRODUCTS_CHOICES = (5, 10, 20, 40, None)  # None: all
TARGET_ZOOM_DEG = 3.0
DEMO_SKY_CENTER = (289.0, 44.0)  # RA, Dec: the Kepler field
DEMO_SKY_SIZE_DEG = 18.0


# -- demo mode ---------------------------------------------------------------------

def demo_catalog(source: SyntheticSource) -> SkyCatalog:
    """The sky catalog for demo mode: the synthetic stars, at their made-up positions."""
    stars = [SkyStar(name=t.name, ra=t.ra, dec=t.dec, tmag=t.tmag, tic=t.tic, kic=t.kic,
                     radius_rsun=t.radius_rsun, teff_k=t.teff_k, logg=t.logg,
                     mass_msun=t.mass_msun, mission_hint=t.mission_hint,
                     planets=[p.name for p in source.known_planets(t)])
             for t in source.targets() if t.ra is not None and t.dec is not None]
    return SkyCatalog(stars=stars, offline=True,
                      note="demo mode: made-up stars with synthetic light curves")


def demo_resolver(source: SyntheticSource) -> Resolver:
    """Resolve typed names against the demo stars ("Demo Hot Jupiter" or just "hot jupiter")."""
    def resolve(text: str) -> tuple[list[StarTarget], list[str]]:
        targets, errors = [], []
        known = source.targets()
        for name in split_names(text):
            key = name.lower()
            match = next((t for t in known if key in (t.name.lower(), t.name.lower().removeprefix("demo "))),
                         None)
            if match is None:
                errors.append(f"{name} is not a demo star (try {', '.join(t.name for t in known[:3])}…)")
            else:
                targets.append(match)
        return targets, errors
    return resolve


# -- the window --------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, source: LightCurveSource | None, parent: QWidget | None = None,
                 client: CatalogClient | None = None, max_workers: int = DEFAULT_WORKERS):
        super().__init__(parent)
        self.source = source
        self.demo = isinstance(source, SyntheticSource)
        self.setWindowTitle(WINDOW_TITLE + (" (demo data)" if self.demo else ""))
        self.resize(1400, 900)
        self._hosts_requested = False

        # Results
        self.tabs = QTabWidget()
        self.tabs.setObjectName("resultTabs")
        self.gallery = GalleryWidget()
        self.table = ResultsTable()
        self.tabs.addTab(self.gallery, "Gallery")
        self.tabs.addTab(self.table, "Table")
        self.setCentralWidget(self.tabs)
        self.table.planetActivated.connect(self.show_in_gallery)

        # Picking stars
        resolver = demo_resolver(source) if self.demo else None
        self.sky = SkyView(client=client)
        self.panel = SelectionPanel(client=client, resolver=resolver)
        if self.demo:
            self.sky.field_zoom_deg = 0.0  # no TIC field stars: they would come from MAST
            self.sky.set_catalog(demo_catalog(source))
            # Most demo stars sit in the Kepler field; all-sky, their labels overlap.
            self.sky.center_on(*DEMO_SKY_CENTER, DEMO_SKY_SIZE_DEG)
        self.sky.starClicked.connect(self.panel.add_star)
        self.panel.targetsChanged.connect(self.sky.set_selected)
        self.panel.targetActivated.connect(self._target_activated)
        self.panel.submitted.connect(self.analyse)
        self.sky_dock = self._add_dock("Sky", "skyDock", Qt.DockWidgetArea.LeftDockWidgetArea, self.sky)
        self.selection_dock = self._add_dock(
            "Selection", "selectionDock", Qt.DockWidgetArea.RightDockWidgetArea, self.panel)

        # Running analyses
        self.runner = AnalysisRunner(source, max_workers=max_workers, parent=self)
        self.runner.starFinished.connect(self._star_finished)
        self._run_errors: list[str] = []
        self.runner.starFailed.connect(self._star_failed)
        self.runner.idle.connect(self._run_idle)
        self.progress = ProgressPanel(self.runner)
        self.progress_dock = self._add_dock(
            "Progress", "progressDock", Qt.DockWidgetArea.BottomDockWidgetArea, self.progress)
        self.setCorner(Qt.Corner.BottomLeftCorner, Qt.DockWidgetArea.LeftDockWidgetArea)
        self.setCorner(Qt.Corner.BottomRightCorner, Qt.DockWidgetArea.RightDockWidgetArea)
        # The results tabs get most of the width: gallery figures scroll sideways below ~700 px.
        self.resizeDocks([self.sky_dock, self.selection_dock], [420, 270], Qt.Orientation.Horizontal)
        self.resizeDocks([self.progress_dock], [150], Qt.Orientation.Vertical)

        self._build_menus()

        source_name = source.name if source is not None else "none"
        self.source_label = QLabel(f"Data source: {source_name}" + (" (demo)" if self.demo else ""))
        self.source_label.setObjectName("sourceLabel")
        self.statusBar().addPermanentWidget(self.source_label)
        if source is None:
            self.statusBar().showMessage("No light-curve source: MAST support is not installed; "
                                         "start with --demo for made-up data")

    def _add_dock(self, title: str, name: str, area: Qt.DockWidgetArea, widget: QWidget) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(name)
        dock.setWidget(widget)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable
                         | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.addDockWidget(area, dock)
        return dock

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        save = file_menu.addAction("Save all plots as PNG…")
        save.triggered.connect(lambda: self.gallery.save_all_button.click())
        export = file_menu.addAction("Export table as CSV…")
        export.triggered.connect(lambda: self.table.export_button.click())
        file_menu.addSeparator()
        quit_action = file_menu.addAction("&Quit")
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)

        settings = self.menuBar().addMenu("&Settings")
        self.max_products_menu = settings.addMenu("Sectors / quarters per star")
        self.max_products_menu.setObjectName("maxProductsMenu")
        self.max_products_group = QActionGroup(self)
        current = getattr(self.source, "max_products", None)
        for value in MAX_PRODUCTS_CHOICES:
            action = QAction("All" if value is None else str(value), self)
            action.setCheckable(True)
            action.setData(value)
            action.setChecked(value == current)
            action.triggered.connect(lambda checked, v=value: self.set_max_products(v))
            self.max_products_group.addAction(action)
            self.max_products_menu.addAction(action)
        if not hasattr(self.source, "max_products"):
            self.max_products_menu.setEnabled(False)
            self.max_products_menu.setTitle("Sectors / quarters per star (MAST only)")
        self.max_products_menu.setToolTipsVisible(True)
        self.max_products_menu.menuAction().setToolTip(
            "How many TESS sectors or Kepler quarters to download and analyse per star")

        self.validation = ValidationController(self, self.menuBar().addMenu("&Validation"))

        help_menu = self.menuBar().addMenu("&Help")
        self.credits_action = help_menu.addAction("Data Credits…")
        self.credits_action.triggered.connect(self.show_credits)

    # -- actions -----------------------------------------------------------------------

    def set_max_products(self, value: int | None) -> None:
        """Cap the sectors / quarters each MAST analysis uses (None: all); applies to new runs."""
        if not hasattr(self.source, "max_products"):
            return
        self.source.max_products = value
        for action in self.max_products_group.actions():
            action.setChecked(action.data() == value)
        self.statusBar().showMessage(
            f"Using {'all' if value is None else f'up to {value}'} sectors / quarters per star "
            "for stars analysed from now on", 8000)

    def analyse(self, targets: list[StarTarget]) -> list[StarTarget]:
        """Queue ``targets`` for analysis (what Submit does)."""
        if self.source is None:
            QMessageBox.warning(self, "No data source",
                                "MAST support is not installed, so there is no light-curve data.\n"
                                "Install the requirements, or start with --demo for made-up data.")
            return []
        queued = self.runner.submit(targets)
        skipped = len(targets) - len(queued)
        message = f"Analysing {len(queued)} star{'s' if len(queued) != 1 else ''}…"
        if skipped:
            message += f" ({skipped} already running)"
        self.statusBar().showMessage(message)
        return queued

    def show_in_gallery(self, star_name: str, planet_index: int = -1) -> None:
        self.tabs.setCurrentWidget(self.gallery)
        # Let the tab lay out before scrolling, or the section's position is stale.
        QTimer.singleShot(0, lambda: self.gallery.scroll_to(star_name))

    def show_credits(self) -> CreditsDialog:
        dialog = CreditsDialog(self.gallery.analyses(), self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        return dialog

    def _target_activated(self, target: StarTarget) -> None:
        if target.ra is not None and target.dec is not None:
            self.sky.center_on(target.ra, target.dec, TARGET_ZOOM_DEG)

    def _star_finished(self, analysis: StarAnalysis) -> None:
        self.gallery.add_analysis(analysis)
        self.table.add_analysis(analysis)
        if analysis.errors:
            self._run_errors.append(f"{analysis.target.name}: {analysis.errors[0]}")
            self.statusBar().showMessage(self._run_errors[-1])

    def _star_failed(self, name: str, error: str) -> None:
        self._run_errors.append(f"{name}: analysis failed: {error}")
        self.statusBar().showMessage(self._run_errors[-1])

    def _run_idle(self) -> None:
        if self._run_errors:
            n = len(self._run_errors)
            self.statusBar().showMessage(f"Analyses finished, {n} with errors (see Progress) — "
                                         f"{self._run_errors[-1]}")
        else:
            self.statusBar().showMessage("Analyses finished", 10000)
        self._run_errors = []

    # -- window events -----------------------------------------------------------------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self.demo and not self._hosts_requested:
            self._hosts_requested = True
            self.sky.load_hosts()

    def closeEvent(self, event) -> None:
        self.runner.cancel()
        self.validation.cancel()
        self.runner.wait()
        self.validation.wait()
        self.sky.close()
        self.panel.close()
        super().closeEvent(event)


def make_source(demo: bool, max_products: int | None = DEFAULT_MAX_PRODUCTS) -> LightCurveSource | None:
    """The data source for the dashboard: synthetic in demo mode, else MAST."""
    if demo:
        return SyntheticSource()
    try:
        from ..pipeline.fetch import MastSource
    except ImportError as exc:
        print(f"MAST data source is not available ({exc}); run with --demo", file=sys.stderr)
        return None
    return MastSource(max_products=max_products)


def _max_products(text: str) -> int | None:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 (all) or more")
    return value or None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dashboard for finding transiting planets in TESS / Kepler data")
    parser.add_argument("--demo", action="store_true",
                        help="use made-up light curves (no downloads)")
    parser.add_argument("--targets", metavar="NAMES", default="",
                        help='stars to put in the selection list, e.g. "Kepler-10, TIC 261136679"')
    parser.add_argument("--max-sectors", metavar="N", type=_max_products, default=DEFAULT_MAX_PRODUCTS,
                        help=f"TESS sectors / Kepler quarters per star, 0 for all "
                             f"(default {DEFAULT_MAX_PRODUCTS})")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = MainWindow(make_source(args.demo, args.max_sectors))
    window.show()
    if args.targets:
        window.panel.entry.setText(args.targets)
        window.panel.add_from_entry()
    return app.exec()
