"""A test-only window holding the sky view and selection panel, wired as the dashboard will be.

Used by tests/test_gui_sky.py, tests/test_gui_selection.py and the
02-sky-selection.png and 09-sky-zoomed.png screenshots.  In the tests,
catalog data come from the responses recorded in
tests/data/catalog/recorded.json and sky images from a recorded cutout, so
nothing touches the network; the screenshots use the live services.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QEventLoop, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QNativeGestureEvent, QPointingDevice, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSplitter

from transitory.catalog import skycatalog
from transitory.catalog.client import CatalogClient, RecordedTransport, ResponseCache
from transitory.catalog.hips import CutoutCache
from transitory.gui.main_window import TARGET_ZOOM_DEG
from transitory.gui.selection_panel import SelectionPanel
from transitory.gui.sky_view import SkyView

RECORDED = Path(__file__).resolve().parent / "data" / "catalog" / "recorded.json"
HIPS_CUTOUT = Path(__file__).resolve().parent / "data" / "catalog" / "hips_cutout.jpg"
SHOT_NAME = "02-sky-selection.png"
SHOT_WAIT_S = 90  # for a live TIC field or sky image


class RecordedHipsTransport:
    """Answers every hips2fits request with the recorded cutout; keeps the requests."""

    def __init__(self):
        self.data = HIPS_CUTOUT.read_bytes()
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, params: dict[str, str]) -> bytes:
        self.calls.append((url, params))
        return self.data


def recorded_client(cache_dir: Path | str | None = None) -> CatalogClient:
    """A catalog client that replays the recorded MAST / Exoplanet Archive responses."""
    if cache_dir is None:
        cache_dir = tempfile.mkdtemp(prefix="transitory-catalog-")
    return CatalogClient(transport=RecordedTransport(RECORDED), cache=ResponseCache(cache_dir))


class SkySelectionWindow(QSplitter):
    """``live_sky``: sky images from CDS hips2fits, not the recorded cutout.

    Either way cutouts go to a fresh temporary cache, so the recorded one
    never lands in the user's cache and a live shot never shows it.
    """

    def __init__(self, client: CatalogClient, live_sky: bool = False):
        super().__init__(Qt.Orientation.Horizontal)
        self.setWindowTitle("Sky and selection")
        self.client = client
        self.sky = SkyView(client=client, hips_transport=None if live_sky else RecordedHipsTransport(),
                           hips_cache=CutoutCache(tempfile.mkdtemp(prefix="transitory-hips-")))
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


def make_sky_selection_window(client: CatalogClient | None = None,
                              live_sky: bool = False) -> SkySelectionWindow:
    window = SkySelectionWindow(client or recorded_client(), live_sky=live_sky)
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
    window = _listed_window(app, client)
    window.panel.entry.setText("KOI-72, LHS 3844")
    app.processEvents()
    return window


def _listed_window(app, client: CatalogClient | None, live_sky: bool = False) -> SkySelectionWindow:
    """The shots' window: hosts on the sky; TOI-270, pi Men and (clicked on the sky) Kepler-10 listed."""
    window = make_sky_selection_window(client or CatalogClient(), live_sky=live_sky)
    sky, panel = window.sky, window.panel
    sky.set_catalog(skycatalog.host_stars(client=window.client))
    bundled = {s.name: s for s in skycatalog.offline_targets()}
    for name in ("TOI-270", "pi Men"):
        panel.add_target(bundled[name].to_target())
    app.processEvents()
    kepler10 = next(s for s in sky.hosts if s.name == "Kepler-10")
    sky.click_at(kepler10.ra, kepler10.dec)  # adds it to the list through the wiring
    app.processEvents()
    return window


class ShotUnavailable(RuntimeError):
    """A live service a screenshot needs did not answer; the shot is not taken."""


def wait_until(condition, timeout_s: float) -> bool:
    """Run the event loop until ``condition()`` holds or ``timeout_s`` passes; whether it held."""
    for _ in range(int(timeout_s * 10)):
        if condition():
            return True
        QTest.qWait(100)
    return condition()


def shot_sky_zoomed(app, client: CatalogClient | None = None, attempts: int = 3):
    """Screenshot state: Kepler-10 shown on the sky from the list, with its TIC field and sky image.

    With no ``client`` the field comes from live MAST and the image from
    live CDS hips2fits (no image while ``sky_background.SKY_IMAGES`` is off).  A field that won't load raises ``ShotUnavailable``;
    an image that won't load after ``attempts`` tries leaves the background
    off (the status line says so), with a warning.
    """
    window = _listed_window(app, client, live_sky=client is None)
    sky, panel = window.sky, window.panel
    items = panel.list.findItems("Kepler-10", Qt.MatchFlag.MatchStartsWith)
    panel.list.setCurrentItem(items[0])
    panel.show_button.click()  # centres on it at TARGET_ZOOM_DEG: the field and image load
    background = sky.background
    field_failed = lambda: "TIC stars unavailable" in sky.status_label.text()  # noqa: E731
    if not wait_until(lambda: sky.field_region is not None or field_failed(), SHOT_WAIT_S) or not sky.field:
        status = sky.status_label.text()
        window.close()
        raise ShotUnavailable(f"no TIC field for Kepler-10: {status}")
    if background is not None:  # None: sky images are off (sky_background.SKY_IMAGES)
        wait_for_sky_image(sky, attempts)
    app.processEvents()
    return window


def wait_for_sky_image(sky: SkyView, attempts: int) -> None:
    """Wait for the sky image, retrying; after ``attempts`` tries, turn the background off."""
    background = sky.background
    for attempt in range(attempts):
        if wait_until(lambda: background.shown is not None or "unavailable" in background.status, SHOT_WAIT_S) \
                and background.shown is not None:
            return
        print(f"sky image, try {attempt + 1} of {attempts}: {background.status or 'no reply'}", file=sys.stderr)
        background.clear()
        background.update()
    print("warning: no sky image; the zoomed shot has the background off", file=sys.stderr)
    sky.set_background(None)
    return window
