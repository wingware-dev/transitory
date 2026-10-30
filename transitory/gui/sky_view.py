"""Sky view: an RA/Dec map of planet hosts and, zoomed in, TIC field stars.

RA increases to the left, as on the sky.  Drag pans; the mouse wheel zooms
about the pointer.  On a trackpad, two-finger scrolling pans and pinching
zooms (Ctrl/Cmd + scroll zooms too).  Reset view (or Home) returns to the
home view: all sky, or what ``set_home`` chose.  The star under the pointer
(within a few pixels) gets a white ring.  A click on a star marks it as last
clicked and emits ``starClicked`` and ``starToggled``; the window adds it
to the selection list, or removes it when it is listed already.  A
right-click opens a menu: Add to / Remove from the list, Centre here, and
Copy name / Copy TIC ID.  Zoomed in so the view's shorter side is under
``FIELD_ZOOM_DEG``, TIC stars in view are loaded from MAST in a worker
thread; zoomed out, the status line says how far to zoom.  The Kepler field (approx.)
and the TESS continuous viewing zones are outlined, and labelled outside
the outlines, under the list's name labels.

A legend in the plot's top-right corner keys the markers.  Marker size
follows brightness (Tmag), scaled down as the view widens past about 10
degrees so the all-sky map doesn't clot; host markers are translucent, so
the Kepler field shows its CCD modules.

Positions are plotted as plain RA/Dec degrees, with the aspect locked so a
degree of RA is cos(Dec) as wide as a degree of Dec at the view's centre:
shapes there are true at any widget size, and the lock follows the view as
it pans.  The axes are labelled in hours and degrees (``sky_axes``).  The
nearest-star search measures in screen pixels, so clicks still pick what
the user sees.

Under the plot, the catalog status (two word-wrapped lines) and the
hovered position and star (a line each) are elided to the view's width, so
changing text never resizes the footer or moves the plot under the mouse.
"""

from __future__ import annotations

import logging
import math

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut, QTextLayout
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMenu, QSizePolicy, QToolButton,
                               QVBoxLayout, QWidget)

from ..catalog import skycatalog
from ..catalog.client import CatalogClient, CatalogError
from ..catalog.skycatalog import SkyCatalog, SkyStar
from ..catalog.tic import MAX_CONE_RADIUS_DEG
from ..models import StarTarget
from .catalog_jobs import JobRunner
from .selection_panel import target_key
from .sky_axes import DecAxis, RaAxis, format_dec, format_ra

log = logging.getLogger(__name__)

FIELD_ZOOM_DEG = 2.0  # shorter view side (true degrees) at or below which TIC field stars load
FIELD_MAX_TMAG = 13.0
FIELD_DEBOUNCE_MS = 400
CLICK_PIXELS = 12  # how far from a star a click may land
MIN_COS_DEC = 0.05  # caps RA stretching near the poles (about 87 deg)

# Kepler: 21 CCD modules (a 5x5 grid less its corners) centred on
# RA 19h22m40s, Dec +44.5; module pitch and roll are approximate.
KEPLER_CENTER = (290.6667, 44.5)
KEPLER_MODULE_DEG = 2.6
KEPLER_ROLL_DEG = 61.5
# TESS continuous viewing zones: within 12 deg of the ecliptic poles.
ECLIPTIC_POLES = ((270.0, 66.5607), (90.0, -66.5607))
CVZ_RADIUS_DEG = 12.0

HOST_COLOR = QColor(255, 170, 60)
HOST_ALPHA = 175  # translucent, so crowded fields (Kepler's CCD modules) keep their shape
NULL_COLOR = QColor(110, 170, 255)
FIELD_COLOR = QColor(170, 170, 170)
SELECTED_COLOR = QColor(255, 70, 70)
CURRENT_COLOR = QColor(255, 255, 90)
HOVER_COLOR = QColor(255, 255, 255, 210)
HOVER_RING_PX = 14  # inside the list's 18 px ring, so both show on a listed star
OUTLINE_COLORS = {"Kepler": QColor(120, 200, 255), "TESS": QColor(150, 230, 150)}

# Markers are drawn at full size up to FULL_SIZE_DEG_PER_PX (true degrees per
# pixel, about 10 deg across a 500 px view) and shrink as the view widens.
FULL_SIZE_DEG_PER_PX = 0.02
MIN_MARKER_SCALE = 0.5
MARKER_SCALE_STEP = 0.05  # sizes change in steps, so panning never resizes them

# z order: field stars, hosts, outlines and their labels, list rings, the
# hover ring, the last-clicked square, then the list's name labels, which
# nothing covers.
OUTLINE_Z = 2.5
SELECTION_LABEL_Z = 5


# -- sky geometry ----------------------------------------------------------------

def small_circle(ra0: float, dec0: float, radius_deg: float, n: int = 181) -> tuple[np.ndarray, np.ndarray]:
    """Points on the circle ``radius_deg`` from (ra0, dec0), as RA/Dec degrees."""
    r, d0 = math.radians(radius_deg), math.radians(dec0)
    theta = np.linspace(0.0, 2.0 * np.pi, n)
    dec = np.arcsin(np.sin(d0) * np.cos(r) + np.cos(d0) * np.sin(r) * np.cos(theta))
    dra = np.arctan2(np.sin(theta) * np.sin(r) * np.cos(d0), np.cos(r) - np.sin(d0) * np.sin(dec))
    return (ra0 + np.degrees(dra)) % 360.0, np.degrees(dec)


def from_tangent(ra0: float, dec0: float, xi_deg: np.ndarray, eta_deg: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Inverse gnomonic projection of tangent-plane offsets around (ra0, dec0)."""
    xi, eta = np.radians(xi_deg), np.radians(eta_deg)
    d0 = math.radians(dec0)
    denom = np.cos(d0) - eta * np.sin(d0)
    ra = ra0 + np.degrees(np.arctan2(xi, denom))
    dec = np.degrees(np.arctan2(np.sin(d0) + eta * np.cos(d0), np.hypot(xi, denom)))
    return ra % 360.0, dec


def kepler_outline() -> tuple[np.ndarray, np.ndarray]:
    """The Kepler field's outer edge (approximate), as a closed RA/Dec polyline."""
    # Corners of a 5x5 module square with the corner modules removed, in module units.
    a, b = 2.5, 1.5
    u = np.array([-b, b, b, a, a, b, b, -b, -b, -a, -a, -b, -b])
    v = np.array([a, a, b, b, -b, -b, -a, -a, -b, -b, b, b, a])
    # Densify the edges so they curve properly on the RA/Dec plot.
    t = np.linspace(0.0, 1.0, 12)[:-1]
    u = np.concatenate([u[i] + (u[i + 1] - u[i]) * t for i in range(len(u) - 1)] + [u[-1:]])
    v = np.concatenate([v[i] + (v[i + 1] - v[i]) * t for i in range(len(v) - 1)] + [v[-1:]])
    roll = math.radians(KEPLER_ROLL_DEG)
    xi = KEPLER_MODULE_DEG * (u * math.cos(roll) - v * math.sin(roll))
    eta = KEPLER_MODULE_DEG * (u * math.sin(roll) + v * math.cos(roll))
    return from_tangent(*KEPLER_CENTER, xi, eta)


def in_kepler_field(ra: float, dec: float) -> bool:
    """Whether (ra, dec) lies inside the approximate Kepler outline."""
    from matplotlib.path import Path

    xs, ys = kepler_outline()
    return bool(Path(np.column_stack([xs, ys])).contains_point((ra, dec)))


def separation_deg(ra1, dec1, ra2, dec2):
    """Angular distance in degrees (haversine; arrays allowed)."""
    ra1, dec1, ra2, dec2 = (np.radians(x) for x in (ra1, dec1, ra2, dec2))
    h = np.sin((dec2 - dec1) / 2) ** 2 + np.cos(dec1) * np.cos(dec2) * np.sin((ra2 - ra1) / 2) ** 2
    return np.degrees(2 * np.arcsin(np.sqrt(np.clip(h, 0.0, 1.0))))


def _marker_size(tmag: float | None, brightest: float = 17.0, low: float = 4.0, high: float = 13.0) -> float:
    return 6.0 if tmag is None else float(np.clip(brightest - tmag, low, high))


def marker_scale(deg_per_px: float) -> float:
    """How much to shrink star markers at ``deg_per_px`` (true degrees per screen pixel).

    1 when zoomed in, falling to ``MIN_MARKER_SCALE`` all-sky, in steps of
    ``MARKER_SCALE_STEP``.
    """
    if not deg_per_px > 0:
        return 1.0
    scale = min(1.0, (FULL_SIZE_DEG_PER_PX / deg_per_px) ** 0.4)
    scale = round(scale / MARKER_SCALE_STEP) * MARKER_SCALE_STEP
    return max(MIN_MARKER_SCALE, scale)


# -- the widget --------------------------------------------------------------------

class ElidedLabel(QLabel):
    """A label of ``lines`` lines whose size never follows its text.

    Text is word-wrapped onto the lines, and what doesn't fit is elided on
    the last.  ``text()`` returns the full text, which is also the tooltip.
    """

    def __init__(self, parent: QWidget | None = None, lines: int = 1):
        super().__init__(parent)
        self._full_text = ""
        self._lines = lines
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        margins = self.contentsMargins()
        metrics = self.fontMetrics()
        self.setFixedHeight(metrics.height() + (lines - 1) * metrics.lineSpacing()
                            + margins.top() + margins.bottom())

    def setText(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self._elide()

    def text(self) -> str:
        return self._full_text

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        width = self.contentsRect().width()
        shown: list[str] = []
        paragraphs = self._full_text.split("\n")  # "\n" starts a new line
        for i, text in enumerate(paragraphs):
            while len(shown) < self._lines - 1 and width > 0:
                end = self._first_line_end(text, width)
                if end >= len(text):
                    break  # the rest fits on this line
                shown.append(text[:end].rstrip())
                text = text[end:]
            if len(shown) == self._lines - 1 or width <= 0:
                # The last line: what is left of this paragraph and the rest, elided.
                rest = "  ".join([text] + paragraphs[i + 1:])
                shown.append(self.fontMetrics().elidedText(rest, Qt.TextElideMode.ElideRight, width))
                break
            shown.append(text)
        super().setText("\n".join(shown))

    def _first_line_end(self, text: str, width: int) -> int:
        """Where ``text`` word-wraps at ``width`` pixels: the end of its first line."""
        layout = QTextLayout(text, self.font())
        layout.beginLayout()
        line = layout.createLine()
        if line.isValid():
            line.setLineWidth(width)
            end = line.textStart() + line.textLength()
        else:
            end = len(text)
        layout.endLayout()
        return max(end, 1)


class TrackpadNavigator(QObject):
    """Trackpad gestures on a plot's viewport: two-finger scroll pans, pinch zooms.

    pyqtgraph zooms on every wheel event and ignores native gestures, so a
    trackpad's scroll zooms and its pinch does nothing.  Wheel events from a
    trackpad (those with a scroll phase and a pixel delta) pan the view by
    the pixels scrolled, unless Ctrl/Cmd is held; mouse-wheel events go on
    to pyqtgraph, which zooms.
    """

    def __init__(self, plot: pg.PlotWidget):
        super().__init__(plot)
        self.plot = plot
        self.view_box = plot.getViewBox()
        plot.viewport().installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Wheel and self.is_trackpad_scroll(event):
            self.pan_pixels(event.pixelDelta().x(), event.pixelDelta().y())
            event.accept()
            return True
        if (event.type() == QEvent.Type.NativeGesture
                and event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture):
            self.zoom_at(event.position(), 1.0 + event.value())
            event.accept()
            return True
        return super().eventFilter(obj, event)

    @staticmethod
    def is_trackpad_scroll(event) -> bool:
        return (event.phase() != Qt.ScrollPhase.NoScrollPhase and not event.pixelDelta().isNull()
                and not event.modifiers() & Qt.KeyboardModifier.ControlModifier)

    def pan_pixels(self, dx: float, dy: float) -> None:
        """Move the sky by (dx, dy) screen pixels, as if dragged."""
        origin = self.plot.mapToScene(0, 0)
        shifted = self.plot.mapToScene(round(dx), round(dy))
        delta = self.view_box.mapSceneToView(origin) - self.view_box.mapSceneToView(shifted)
        self.view_box.translateBy(x=delta.x(), y=delta.y())

    def zoom_at(self, viewport_pos: QPointF, factor: float) -> None:
        """Magnify by ``factor`` (> 1 zooms in) about a viewport position."""
        if factor <= 0:
            return
        center = self.view_box.mapSceneToView(self.plot.mapToScene(viewport_pos.toPoint()))
        self.view_box.scaleBy((1 / factor, 1 / factor), center)


class LegendSample(pg.ItemSample):
    """A legend swatch drawn from its own symbol, pen and brush, not a plot item's.

    With several ``sizes``, the symbol is drawn at each, small to large, as
    a key to marker size.  Clicks do nothing (pyqtgraph's samples hide
    their item).
    """

    def __init__(self, symbol: str, sizes: tuple[float, ...], pen=None, brush=None):
        super().__init__(None)
        self.symbol = symbol
        self.sizes = sizes
        self.pen = pg.mkPen(pen) if pen is not None else pg.mkPen(None)
        self.brush = pg.mkBrush(brush) if brush is not None else pg.mkBrush(None)
        self._width = 20 if len(sizes) == 1 else 34
        self.setFixedWidth(self._width)

    def boundingRect(self):
        return QRectF(0, 0, self._width, 20)

    def paint(self, p, *args):
        p.setRenderHint(p.RenderHint.Antialiasing)
        step = self._width / len(self.sizes)
        for i, size in enumerate(self.sizes):
            p.save()
            p.translate(step * (i + 0.5), 10)
            pg.graphicsItems.ScatterPlotItem.drawSymbol(p, self.symbol, size, self.pen, self.brush)
            p.restore()

    def mouseClickEvent(self, event):
        event.ignore()


class SkyLegend(pg.LegendItem):
    """The key to the sky's markers, fixed in a corner of the plot.

    Unlike pyqtgraph's legend it can't be dragged: drags on it pan the sky.
    """

    def __init__(self):
        super().__init__(offset=(-8, 8), brush=pg.mkBrush(11, 16, 32, 215),
                         pen=pg.mkPen(255, 255, 255, 60), labelTextColor=(220, 225, 235),
                         labelTextSize="8pt", verSpacing=-6)
        self.entries: list[tuple[str, LegendSample]] = []

    def add_entry(self, sample: LegendSample, text: str) -> None:
        self.addItem(sample, text)
        self.entries.append((text, sample))

    def hoverEvent(self, ev):
        pass

    def mouseDragEvent(self, ev):
        ev.ignore()

    def mouseDoubleClickEvent(self, ev):
        ev.ignore()


class SkyView(QWidget):
    starClicked = Signal(object)  # SkyStar
    starToggled = Signal(object)  # SkyStar to add to the selection list, or remove if listed
    catalogLoaded = Signal(object)  # SkyCatalog
    fieldLoaded = Signal(list)  # list[SkyStar]

    def __init__(self, client: CatalogClient | None = None, parent: QWidget | None = None,
                 field_zoom_deg: float = FIELD_ZOOM_DEG):
        super().__init__(parent)
        self.client = client
        self._field_zoom_deg = field_zoom_deg
        self.catalog: SkyCatalog | None = None
        self.hosts: list[SkyStar] = []
        self.field: list[SkyStar] = []
        self.field_region: tuple[float, float, float] | None = None  # (ra, dec, radius) loaded
        self.current: SkyStar | None = None
        self.selected: list[StarTarget] = []
        self._pending_field: tuple[float, float, float] | None = None
        self._setting_view = False
        self._catalog_status = "No catalog loaded"
        self._field_status = ""
        self._stars: list[SkyStar] = []
        self._ra = np.empty(0)
        self._dec = np.empty(0)
        self.runner = JobRunner(self)

        self.plot = pg.PlotWidget(background="#0b1020",
                                  axisItems={"bottom": RaAxis("bottom"), "left": DecAxis("left")})
        self.plot.setObjectName("skyPlot")
        self.view_box = self.plot.getViewBox()
        self.view_box.invertX(True)
        self.view_box.setMenuEnabled(False)
        self.view_box.setLimits(xMin=-5, xMax=365, yMin=-92, yMax=92, minXRange=0.01, minYRange=0.01)
        self.plot.setLabel("bottom", "RA")
        self.plot.setLabel("left", "Dec")
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.hideButtons()

        self.outline_labels: list[pg.TextItem] = []
        self._add_outlines()
        host_color = QColor(HOST_COLOR)
        host_color.setAlpha(HOST_ALPHA)
        self._brushes = {"host": pg.mkBrush(host_color), "null": pg.mkBrush(NULL_COLOR),
                         "field": pg.mkBrush(FIELD_COLOR)}
        self.host_item = pg.ScatterPlotItem(pen=None, brush=self._brushes["host"], size=6)
        self.field_item = pg.ScatterPlotItem(pen=None, brush=self._brushes["field"], size=4)
        self.selected_item = pg.ScatterPlotItem(pen=pg.mkPen(SELECTED_COLOR, width=2), brush=None,
                                                size=18, symbol="o")
        self.current_item = pg.ScatterPlotItem(pen=pg.mkPen(CURRENT_COLOR, width=2), brush=None,
                                               size=24, symbol="s")
        self.hover_item = pg.ScatterPlotItem(pen=pg.mkPen(HOVER_COLOR, width=1.5), brush=None,
                                             size=HOVER_RING_PX, symbol="o")
        self.hovered: SkyStar | None = None
        self.menu: QMenu | None = None  # the last right-click menu
        for item, z in ((self.field_item, 1), (self.host_item, 2), (self.selected_item, 3),
                        (self.hover_item, 3.5), (self.current_item, 4)):
            item.setZValue(z)
            self.plot.addItem(item)
        self._labels: list[pg.TextItem] = []
        self._host_sizes = np.empty(0)
        self._field_sizes = np.empty(0)
        self.marker_scale = 1.0
        self.legend = self._make_legend()

        self.view_box.setMouseEnabled(x=True, y=True)
        self.trackpad = TrackpadNavigator(self.plot)
        self.home: tuple[float, float, float] | None = None  # (ra, dec, size) or all sky

        self.status_label = ElidedLabel(lines=2)
        self.status_label.setObjectName("skyStatus")
        self.coord_label = ElidedLabel(lines=2)  # position, then the star under the pointer
        self.coord_label.setObjectName("skyCoords")
        self.reset_button = QToolButton()
        self.reset_button.setObjectName("skyReset")
        self.reset_button.setText("Reset view")
        self.reset_button.setToolTip("Back to the starting view (Home)")
        self.reset_button.clicked.connect(self.reset_view)
        self.reset_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Home), self, self.reset_view,
                                        context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.reset_button, 0, Qt.AlignmentFlag.AlignTop)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.plot, 1)
        layout.addLayout(status_row)
        layout.addWidget(self.coord_label)

        self._field_timer = QTimer(self)
        self._field_timer.setSingleShot(True)
        self._field_timer.setInterval(FIELD_DEBOUNCE_MS)
        self._field_timer.timeout.connect(self._maybe_load_field)
        self.view_box.sigRangeChanged.connect(lambda *_: self._field_timer.start())
        self.view_box.sigRangeChanged.connect(lambda *_: self._follow_aspect())
        self.view_box.sigRangeChanged.connect(lambda *_: self._update_marker_sizes())
        self.view_box.sigResized.connect(lambda *_: self._update_marker_sizes())
        self.plot.scene().sigMouseClicked.connect(self._on_mouse_clicked)
        self.plot.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self.plot.viewport().installEventFilter(self)  # to drop the hover ring on leaving

        self.show_all_sky()
        self._show_zoom_hint()
        self._update_status()

    # -- outlines ----------------------------------------------------------------

    def _add_outlines(self) -> None:
        # Labels go outside: under the Kepler field, whose top edge is crowded
        # with well-known targets (Kepler-10), and over the CVZs (pi Men sits
        # just under the southern one).
        ra, dec = kepler_outline()
        self._add_outline(ra, dec, "Kepler field", OUTLINE_COLORS["Kepler"], above=False)
        for ra0, dec0 in ECLIPTIC_POLES:
            ra, dec = small_circle(ra0, dec0, CVZ_RADIUS_DEG)
            self._add_outline(ra, dec, "TESS CVZ", OUTLINE_COLORS["TESS"])

    def _add_outline(self, ra: np.ndarray, dec: np.ndarray, label: str, color: QColor,
                     above: bool = True) -> None:
        """Draw an outline, labelled just outside its top (``above``) or bottom point."""
        curve = pg.PlotCurveItem(ra, dec, pen=pg.mkPen(color, width=1.5, style=Qt.PenStyle.DashLine))
        curve.setZValue(OUTLINE_Z)  # above the host markers, which crowd the Kepler field
        self.plot.addItem(curve)
        edge = int(np.argmax(dec) if above else np.argmin(dec))
        text = pg.TextItem(label, color=color, anchor=(0.5, 1.1 if above else -0.1))
        text.setPos(float(ra[edge]), float(dec[edge]))
        text.setZValue(OUTLINE_Z)  # under the list's name labels
        self.plot.addItem(text)
        self.outline_labels.append(text)

    # -- legend --------------------------------------------------------------------

    def _make_legend(self) -> SkyLegend:
        """The key to every kind of marker, in the plot's top-right corner (inside the axes)."""
        legend = SkyLegend()
        legend.setParentItem(self.view_box)
        legend.setZValue(10)
        host_color = self._brushes["host"].color()
        legend.add_entry(LegendSample("o", (8,), brush=host_color), "Planet host")
        legend.add_entry(LegendSample("o", (8,), brush=NULL_COLOR), "No known planets")
        legend.add_entry(LegendSample("o", (6,), brush=FIELD_COLOR), "TIC field star")
        legend.add_entry(LegendSample("o", (14,), pen=self.selected_item.opts["pen"]), "In list")
        legend.add_entry(LegendSample("s", (15,), pen=self.current_item.opts["pen"]), "Last clicked")
        legend.add_entry(LegendSample("o", (3, 6, 10), brush=host_color), "Size = brightness (Tmag)")
        return legend

    def _update_marker_sizes(self) -> None:
        """Size the star markers for the view's true degrees per pixel."""
        _px, py = self.view_box.viewPixelSize()  # Dec degrees are true degrees
        scale = marker_scale(py)
        if scale == self.marker_scale:
            return
        self.marker_scale = scale
        if len(self._host_sizes):
            self.host_item.setSize(self._host_sizes * scale)
        if len(self._field_sizes):
            self.field_item.setSize(self._field_sizes * scale)

    # -- catalogs ------------------------------------------------------------------

    def load_hosts(self, refresh: bool = False) -> None:
        """Fetch the planet-host catalog in the background, then show it."""
        self._catalog_status = "Loading planet hosts from the NASA Exoplanet Archive…"
        self._update_status()
        client = self.client
        self.runner.submit(lambda: skycatalog.host_stars(client=client, refresh=refresh),
                           self.set_catalog, self._catalog_failed)

    def set_catalog(self, catalog: SkyCatalog) -> None:
        self.catalog = catalog
        self.hosts = list(catalog.stars)
        self._host_sizes = np.array([_marker_size(s.tmag) for s in self.hosts])
        self.host_item.setData([{"pos": (s.ra, s.dec), "size": size * self.marker_scale,
                                 "brush": self._brushes["null" if s.kind == "null" else "host"]}
                                for s, size in zip(self.hosts, self._host_sizes)])
        what = "bundled targets" if catalog.offline else "planet hosts"
        status = f"{len(self.hosts):,} {what}"
        if catalog.offline:
            status = f"Offline: {status}"
            if catalog.retrieved:
                status += f" (archive data of {catalog.retrieved})"
            if catalog.note:
                status += f" — {catalog.note}"
        else:
            status += " · NASA Exoplanet Archive"
            if catalog.retrieved:
                status += f", retrieved {catalog.retrieved}"
        self._catalog_status = status
        self._rebuild_index()
        self._update_status()
        self.catalogLoaded.emit(catalog)

    def _catalog_failed(self, exc: Exception) -> None:
        if not isinstance(exc, CatalogError):
            log.error("loading planet hosts failed", exc_info=exc)
        self._catalog_status = f"Planet hosts unavailable: {exc}"
        self._update_status()

    def load_field(self, ra: float, dec: float, radius_deg: float) -> None:
        """Fetch TIC stars around (ra, dec) in the background, then show them."""
        request = (ra, dec, min(radius_deg, MAX_CONE_RADIUS_DEG))
        self._pending_field = request
        self._field_status = (f"Loading TIC stars (Tmag ≤ {FIELD_MAX_TMAG:g}) within "
                              f"{request[2]:.2f}° from MAST…")
        self._update_status()
        client = self.client
        self.runner.submit(
            lambda: skycatalog.field_stars(*request, max_tmag=FIELD_MAX_TMAG, client=client),
            lambda stars: self._field_done(request, stars),
            lambda exc: self._field_failed(request, exc))

    def _field_done(self, request, stars: list[SkyStar]) -> None:
        if request != self._pending_field:
            return  # the view moved on; a newer request is coming
        self._pending_field = None
        self.set_field(stars, request)

    def _field_failed(self, request, exc: Exception) -> None:
        if request != self._pending_field:
            return
        self._pending_field = None
        if not isinstance(exc, CatalogError):
            log.error("loading TIC field stars failed", exc_info=exc)
        self._field_status = f"TIC stars unavailable: {exc}"
        self._update_status()

    def set_field(self, stars: list[SkyStar], region: tuple[float, float, float] | None = None) -> None:
        """Show ``stars`` as field stars; ``region`` is the (ra, dec, radius) they cover."""
        host_tics = {s.tic for s in self.hosts if s.tic is not None}
        self.field = [s for s in stars if s.tic not in host_tics]
        self.field_region = region
        self._field_sizes = np.array([_marker_size(s.tmag, 15.0, 3.0, 9.0) for s in self.field])
        self.field_item.setData([{"pos": (s.ra, s.dec), "size": size * self.marker_scale}
                                 for s, size in zip(self.field, self._field_sizes)])
        if region is None:
            self._field_status = ""
        else:
            self._field_status = (f"{len(self.field):,} TIC stars (Tmag ≤ {FIELD_MAX_TMAG:g}) "
                                  f"within {region[2]:.2f}°")
        self._rebuild_index()
        self._update_status()
        self.fieldLoaded.emit(self.field)

    def clear_field(self) -> None:
        self._pending_field = None
        self.set_field([], None)

    # -- view ----------------------------------------------------------------------------

    def show_all_sky(self) -> None:
        """Pole to pole, and as much RA as the widget's shape allows (all of it when 2:1 or wider)."""
        self.center_on(180.0, 0.0, 182.0)

    def set_home(self, ra: float, dec: float, size_deg: float) -> None:
        """Make (ra, dec), ``size_deg`` high, the view Reset view returns to, and show it."""
        self.home = (ra, dec, size_deg)
        self.reset_view()

    def reset_view(self) -> None:
        if self.home is None:
            self.show_all_sky()
        else:
            self.center_on(*self.home)

    @staticmethod
    def aspect_at(dec: float) -> float:
        """The x/y pixel-scale ratio that draws the sky true at ``dec``: cos(dec)."""
        return max(math.cos(math.radians(max(-90.0, min(90.0, dec)))), MIN_COS_DEC)

    def _follow_aspect(self) -> None:
        """Re-lock the aspect for the view's centre Dec, when it has moved enough to matter.

        Keeps the Dec range and centre, so panning never zooms; only the RA
        width changes.
        """
        if self._setting_view:
            return
        (x0, x1), (y0, y1) = self.view_box.viewRange()
        aspect = self.aspect_at((y0 + y1) / 2)
        locked = self.view_box.state["aspectLocked"]
        if locked and abs(aspect - locked) <= 1e-4 * locked:
            return
        bounds = self.view_box.rect()
        if bounds.height() > 0:
            width = abs(y1 - y0) * bounds.width() / bounds.height() / aspect
        else:
            width = abs(x1 - x0)
        ra = (x0 + x1) / 2
        self._set_view(aspect, (ra - width / 2, ra + width / 2), (y0, y1))

    def _set_view(self, aspect: float, x_range, y_range) -> None:
        """Lock ``aspect`` and show the ranges, without re-entering ``_follow_aspect``."""
        self._setting_view = True
        try:
            self.view_box.setAspectLocked(True, ratio=aspect)
            self.view_box.setRange(xRange=x_range, yRange=y_range, padding=0)
        finally:
            self._setting_view = False

    def center_on(self, ra: float, dec: float, size_deg: float | None = None) -> None:
        """Centre the view on (ra, dec).

        With ``size_deg``, a ``size_deg`` square of sky (true degrees) fills
        the view's shorter side, so it is all visible; without, the visible
        area keeps its size.
        """
        (x0, x1), (y0, y1) = self.view_box.viewRange()
        if size_deg is None:
            width, height = abs(x1 - x0), abs(y1 - y0)
        else:
            height = size_deg
            width = size_deg / self.aspect_at(dec)
        self._set_view(self.aspect_at(dec), (ra - width / 2, ra + width / 2),
                       (dec - height / 2, dec + height / 2))

    def view_region(self) -> tuple[float, float, float, float]:
        """(ra, dec, true width, height) of the visible area, in degrees."""
        (x0, x1), (y0, y1) = self.view_box.viewRange()
        ra, dec = (x0 + x1) / 2, (y0 + y1) / 2
        width = abs(x1 - x0) * math.cos(math.radians(max(-89.9, min(89.9, dec))))
        return ra, dec, width, abs(y1 - y0)

    @property
    def field_zoom_deg(self) -> float:
        """Shorter view side (true degrees) at or below which field stars load; 0 turns them off."""
        return self._field_zoom_deg

    @field_zoom_deg.setter
    def field_zoom_deg(self, value: float) -> None:
        self._field_zoom_deg = value
        self._maybe_load_field()  # and the status hint follows at once

    def _maybe_load_field(self) -> None:
        ra, dec, width, height = self.view_region()
        if min(width, height) > self.field_zoom_deg:  # a wide view's shorter side
            if self.field or self._pending_field:
                self.clear_field()
            self._show_zoom_hint()
            return
        radius = min(0.5 * math.hypot(width, height), MAX_CONE_RADIUS_DEG)
        loaded = self._pending_field or self.field_region
        if loaded is not None:
            lra, ldec, lradius = loaded
            if separation_deg(ra, dec, lra, ldec) + radius <= lradius * 1.001:
                return  # the view is inside what is (being) loaded
        self.load_field(round(ra, 4), round(dec, 4), radius * 1.25)

    def _show_zoom_hint(self) -> None:
        """Say how far to zoom in for field stars (nothing when they are off, as in demo mode)."""
        hint = ""
        if self.field_zoom_deg > 0:
            hint = (f"Zoom in below {self.field_zoom_deg:g}° to see TIC stars "
                    f"(Tmag ≤ {FIELD_MAX_TMAG:g})")
        if hint != self._field_status:
            self._field_status = hint
            self._update_status()

    # -- picking ---------------------------------------------------------------------

    def _rebuild_index(self) -> None:
        self._stars = self.hosts + self.field
        self._ra = np.array([s.ra for s in self._stars], dtype=float)
        self._dec = np.array([s.dec for s in self._stars], dtype=float)
        if self.hovered is not None and not any(s is self.hovered for s in self._stars):
            self.set_hovered(None)

    def nearest_star(self, ra: float, dec: float, max_pixels: float = CLICK_PIXELS) -> SkyStar | None:
        """The displayed star nearest (ra, dec) on screen, if within ``max_pixels``."""
        if not self._stars:
            return None
        px, py = self.view_box.viewPixelSize()
        if not px or not py:
            return None
        dx = ((self._ra - ra + 180.0) % 360.0 - 180.0) / px
        dy = (self._dec - dec) / py
        dist = np.hypot(dx, dy)
        i = int(np.argmin(dist))
        return self._stars[i] if dist[i] <= max_pixels else None

    def click_at(self, ra: float, dec: float) -> SkyStar | None:
        """Select the star nearest (ra, dec) and toggle it in the list, as a click there would."""
        star = self.nearest_star(ra, dec)
        if star is not None:
            self.select_star(star)
            self.starToggled.emit(star)
        return star

    def is_listed(self, star: SkyStar) -> bool:
        """Whether ``star`` is in the selection list (as last given to ``set_selected``)."""
        key = target_key(star.to_target())
        return any(target_key(t) == key for t in self.selected)

    def set_hovered(self, star: SkyStar | None) -> None:
        """Ring ``star`` as the one under the pointer (None: no ring)."""
        if star is self.hovered:
            return
        self.hovered = star
        self.hover_item.setData([] if star is None else [{"pos": (star.ra, star.dec)}])

    def context_menu(self, ra: float, dec: float) -> QMenu:
        """The right-click menu for (ra, dec): for the star there, if any, else just Centre here."""
        menu = QMenu(self)
        menu.setObjectName("skyMenu")
        star = self.nearest_star(ra, dec)
        if star is None:
            menu.addAction("Centre here", lambda: self.center_on(ra % 360.0, dec))
            return menu
        if self.is_listed(star):
            menu.addAction(f"Remove {star.name} from the list", lambda: self.starToggled.emit(star))
        else:
            menu.addAction(f"Add {star.name} to the list", lambda: self.starToggled.emit(star))
        menu.addAction("Centre here", lambda: self.center_on(star.ra, star.dec))
        menu.addSeparator()
        menu.addAction("Copy name", lambda: QApplication.clipboard().setText(star.name))
        if star.tic is not None:
            menu.addAction("Copy TIC ID", lambda: QApplication.clipboard().setText(f"TIC {star.tic}"))
        return menu

    def select_star(self, star: SkyStar) -> None:
        self.current = star
        self.current_item.setData([{"pos": (star.ra, star.dec)}])
        self.coord_label.setText(self._describe(star))
        self.starClicked.emit(star)

    def set_selected(self, targets: list[StarTarget]) -> None:
        """Mark the stars in the selection list (those with a position)."""
        self.selected = [t for t in targets if t.ra is not None and t.dec is not None]
        self.selected_item.setData([{"pos": (t.ra, t.dec)} for t in self.selected])
        for label in self._labels:
            self.plot.removeItem(label)
        self._labels = []
        for target in self.selected:
            label = pg.TextItem(target.name, color=SELECTED_COLOR, anchor=(-0.15, 0.5),
                                fill=pg.mkBrush(11, 16, 32, 210))
            label.setPos(target.ra, target.dec)
            label.setZValue(SELECTION_LABEL_Z)
            self.plot.addItem(label)
            self._labels.append(label)

    def _on_mouse_clicked(self, event) -> None:
        button = event.button()
        if button not in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton) or event.double():
            return
        pos = event.scenePos()
        if (not self.view_box.sceneBoundingRect().contains(pos)
                or self.legend.sceneBoundingRect().contains(pos)):
            return
        point = self.view_box.mapSceneToView(pos)
        if button == Qt.MouseButton.LeftButton:
            self.click_at(point.x(), point.y())
            return
        if self.menu is not None:
            self.menu.deleteLater()
        self.menu = self.context_menu(point.x(), point.y())
        self.menu.popup(event.screenPos().toPoint())

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Leave:
            self.set_hovered(None)
        return super().eventFilter(obj, event)

    def _on_mouse_moved(self, pos) -> None:
        if not self.view_box.sceneBoundingRect().contains(pos):
            self.set_hovered(None)
            return
        point = self.view_box.mapSceneToView(pos)
        ra, dec = point.x() % 360.0, point.y()
        star = self.nearest_star(ra, dec)
        self.set_hovered(star)
        text = self.position_text(ra, dec)
        self.coord_label.setText(f"{text}\n{self._describe(star)}" if star else text)

    @staticmethod
    def position_text(ra: float, dec: float) -> str:
        """``RA 19h22m40.0s  Dec +44°30′00″  (290.667°, +44.500°)``."""
        return f"RA {format_ra(ra, decimals=1)}  Dec {format_dec(dec)}  ({ra % 360.0:.3f}°, {dec:+.3f}°)"

    @staticmethod
    def _describe(star: SkyStar) -> str:
        text = star.name
        if star.tic is not None and str(star.tic) not in star.name:
            text += f" (TIC {star.tic})"
        if star.tmag is not None:
            text += f", Tmag {star.tmag:.1f}"
        if star.planets:
            text += f", {star.n_planets} planet{'s' if star.n_planets != 1 else ''}"
        elif star.kind == "null":
            text += ", no known planets"
        return text

    # -- status ------------------------------------------------------------------------

    @property
    def status_text(self) -> str:
        return self.status_label.text()

    def _update_status(self) -> None:
        parts = [self._catalog_status]
        if self._field_status:
            parts.append(self._field_status)
        self.status_label.setText("  ·  ".join(parts))

    def closeEvent(self, event) -> None:
        self._field_timer.stop()
        self.runner.wait()
        super().closeEvent(event)
