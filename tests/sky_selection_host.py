"""A test-only window holding the sky view and selection panel, wired as the dashboard will be.

Used by tests/test_gui_sky.py, tests/test_gui_selection.py and the
02-sky-selection.png screenshot.  Catalog data come from the responses
recorded in tests/data/catalog/recorded.json, so nothing touches the network.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QEventLoop, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QNativeGestureEvent, QPointingDevice, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSplitter

from transitory.catalog import skycatalog
from transitory.catalog.client import CatalogClient, RecordedTransport, ResponseCache
from transitory.gui.main_window import TARGET_ZOOM_DEG
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
        self.sky.starToggled.connect(self.panel.toggle_star)
        self.panel.targetsChanged.connect(self.sky.set_selected)
        self.panel.targetActivated.connect(
            lambda t: t.ra is not None and self.sky.center_on(t.ra, t.dec, TARGET_ZOOM_DEG))

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


# -- mouse and trackpad input on the sky ------------------------------------------------
#
# Real Qt events sent to the plot's viewport, so they go through the same
# paths (pyqtgraph's scene, the trackpad filter) as the user's.  pyqtgraph
# drops mouse moves that come faster than its event rate, so moves pause.

def _pause(ms: int = 40) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def sky_pixel(sky: SkyView, ra: float, dec: float) -> QPoint:
    """Viewport pixel position of (ra, dec)."""
    return sky.plot.mapFromScene(sky.view_box.mapViewToScene(QPointF(ra, dec)))


def sky_center(sky: SkyView) -> QPoint:
    return sky.plot.mapFromScene(sky.view_box.sceneBoundingRect().center())


def wheel(sky: SkyView, pos: QPoint, angle: int = 0, pixels: QPoint | None = None,
          phase=Qt.ScrollPhase.NoScrollPhase, modifiers=Qt.KeyboardModifier.NoModifier) -> None:
    """A wheel event at ``pos``: a mouse wheel (``angle``, 120 a notch) or, with
    ``pixels`` and a scroll ``phase``, a trackpad."""
    viewport = sky.plot.viewport()
    pixels = pixels or QPoint()
    if not angle:
        angle = pixels.y()
    event = QWheelEvent(QPointF(pos), QPointF(viewport.mapToGlobal(pos)), pixels, QPoint(0, angle),
                        Qt.MouseButton.NoButton, modifiers, phase, False)
    QApplication.sendEvent(viewport, event)
    QApplication.processEvents()


def trackpad_scroll(sky: SkyView, pos: QPoint, dx: int, dy: int,
                    modifiers=Qt.KeyboardModifier.NoModifier) -> None:
    """A two-finger trackpad scroll by (dx, dy) pixels, begin to end."""
    for phase, delta in ((Qt.ScrollPhase.ScrollBegin, QPoint()),
                         (Qt.ScrollPhase.ScrollUpdate, QPoint(dx, dy)),
                         (Qt.ScrollPhase.ScrollEnd, QPoint())):
        if phase == Qt.ScrollPhase.ScrollUpdate or not delta.isNull():
            wheel(sky, pos, pixels=delta, phase=phase, modifiers=modifiers)


def pinch(sky: SkyView, pos: QPoint, value: float) -> None:
    """A trackpad pinch at ``pos``: ``value`` > 0 spreads the fingers (zoom in)."""
    viewport = sky.plot.viewport()
    event = QNativeGestureEvent(Qt.NativeGestureType.ZoomNativeGesture,
                                QPointingDevice.primaryPointingDevice(), 2, QPointF(pos), QPointF(pos),
                                QPointF(viewport.mapToGlobal(pos)), value, QPointF())
    QApplication.sendEvent(viewport, event)
    QApplication.processEvents()


def drag(sky: SkyView, start: QPoint, end: QPoint, steps: int = 8) -> None:
    """Press the left button at ``start``, move to ``end`` in steps, release."""
    viewport = sky.plot.viewport()
    QTest.mouseMove(viewport, start)
    _pause()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    for i in range(1, steps + 1):
        _pause()
        QTest.mouseMove(viewport, start + (end - start) * (i / steps))
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
    QApplication.processEvents()


def click(sky: SkyView, pos: QPoint, button=Qt.MouseButton.LeftButton) -> None:
    """A click (left by default) at ``pos``, after a pause so it isn't taken for a double click."""
    _pause(QApplication.doubleClickInterval() + 50)
    QTest.mouseClick(sky.plot.viewport(), button, Qt.KeyboardModifier.NoModifier, pos)
    QApplication.processEvents()


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
