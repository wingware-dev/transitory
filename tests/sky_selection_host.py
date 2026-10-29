"""A test-only window holding the sky view and selection panel, wired as the dashboard will be.

Used by tests/test_gui_sky.py, tests/test_gui_selection.py and the
02-sky-selection.png screenshot.  Catalog data come from the responses
recorded in tests/data/catalog/recorded.json, so nothing touches the network.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSplitter

from transitory.catalog import skycatalog
from transitory.catalog.client import CatalogClient, RecordedTransport, ResponseCache
from transitory.gui.selection_panel import SelectionPanel
from transitory.gui.sky_view import SkyView

RECORDED = Path(__file__).resolve().parent / "data" / "catalog" / "recorded.json"
SHOT_NAME = "02-sky-selection.png"


def recorded_client(cache_dir: Path | str | None = None) -> CatalogClient:
    """A catalog client that replays the recorded MAST / Exoplanet Archive responses."""
    if cache_dir is None:
        cache_dir = tempfile.mkdtemp(prefix="transitory-catalog-")
    return CatalogClient(transport=RecordedTransport(RECORDED), cache=ResponseCache(cache_dir))


class SkySelectionWindow(QSplitter):
    def __init__(self, client: CatalogClient):
        super().__init__(Qt.Orientation.Horizontal)
        self.setWindowTitle("Sky and selection")
        self.client = client
        self.sky = SkyView(client=client)
        self.panel = SelectionPanel(client=client)
        self.addWidget(self.sky)
        self.addWidget(self.panel)
        self.setStretchFactor(0, 3)
        self.setStretchFactor(1, 1)
        self.sky.starClicked.connect(self.panel.add_star)
        self.panel.targetsChanged.connect(self.sky.set_selected)
        self.panel.targetActivated.connect(
            lambda t: t.ra is not None and self.sky.center_on(t.ra, t.dec, 3.0))

    def closeEvent(self, event):
        self.sky.close()
        self.panel.close()
        super().closeEvent(event)


def make_sky_selection_window(client: CatalogClient | None = None) -> SkySelectionWindow:
    window = SkySelectionWindow(client or recorded_client())
    window.resize(1400, 800)
    window.setSizes([1020, 380])
    window.show()
    return window


def shot_sky_selection(app, client: CatalogClient | None = None):
    """Screenshot state: hosts on the sky, two stars listed, Kepler-10 clicked on the sky.

    With no ``client`` the host catalog comes from the live archive (one
    cached query; the bundled targets when offline) so the shot shows the
    real sky; the listed stars are bundled targets, so no name lookups run.
    """
    window = make_sky_selection_window(client or CatalogClient())
    sky, panel = window.sky, window.panel
    sky.set_catalog(skycatalog.host_stars(client=window.client))
    bundled = {s.name: s for s in skycatalog.offline_targets()}
    for name in ("TOI-270", "pi Men"):
        panel.add_target(bundled[name].to_target())
    app.processEvents()
    kepler10 = next(s for s in sky.hosts if s.name == "Kepler-10")
    sky.click_at(kepler10.ra, kepler10.dec)  # adds it to the list through the wiring
    panel.entry.setText("KOI-72, LHS 3844")
    app.processEvents()
    return window
