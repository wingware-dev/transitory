"""The sky view's background: a HiPS survey image behind the stars.

Zoomed in so the view's shorter side is under ``BACKGROUND_ZOOM_DEG``, a
cutout of the chosen survey (DSS2, 2MASS or PanSTARRS colour) is fetched
from CDS hips2fits in a worker thread and drawn under everything else.
Closing the view never waits for a fetch under way (``stop``).
The sky view plots plain RA/Dec, and :func:`~transitory.catalog.hips.view_wcs`
asks for a CAR image that is linear in RA and Dec, so the image only needs
placing over its RA/Dec rectangle.

Each request covers the view and a margin around it, so small pans don't
reload; the old image stays in place (it moves with the sky) until the new
one arrives.  As for TIC field stars, loads are debounced and a reply for
a view the user has moved on from is dropped.
"""

from __future__ import annotations

import logging
import math

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QObject, QRectF, QTimer, Signal
from PySide6.QtGui import QImage

from ..catalog import hips
from ..catalog.client import CatalogError
from .catalog_jobs import DetachedJobRunner

log = logging.getLogger(__name__)

BACKGROUND_ZOOM_DEG = 10.0  # shorter view side (true degrees) at or below which the image loads
BACKGROUND_DEBOUNCE_MS = 400
BACKGROUND_Z = -1  # under the outlines, markers and labels
BACKGROUND_OPACITY = 0.85  # a little dimmed, so markers and outlines stand out
MARGIN = 0.25  # fraction of the view's size fetched beyond each edge
MAX_IMAGE_PX = 1600  # cap on a cutout's longer side
COARSE_FACTOR = 1.4  # refetch when the shown image is this much coarser than the screen

DEFAULT_SURVEY = "dss2"
# (key, chooser menu text, chooser button text); None is no background.  The
# button texts are short so the button is no wider than Reset view above it.
CHOICES = [(None, "Off", "Sky off"), ("dss2", "DSS2", "DSS2"), ("2mass", "2MASS", "2MASS"),
           ("panstarrs", "PanSTARRS", "PS1")]


def image_array(data: bytes) -> np.ndarray:
    """JPEG bytes as an RGB array, rows south to north and columns RA rising.

    hips2fits draws north at the top and the highest RA on the left; the
    image item draws row 0 at the lowest Dec and column 0 at the lowest RA
    (the plot's x axis is inverted, so that is on the right), so both axes
    are flipped.
    """
    image = QImage.fromData(data, "JPEG")
    if image.isNull():
        raise CatalogError("the sky image could not be decoded")
    image = image.convertToFormat(QImage.Format.Format_RGB888)
    width, height, stride = image.width(), image.height(), image.bytesPerLine()
    rows = np.frombuffer(image.constBits(), dtype=np.uint8, count=stride * height)
    rgb = rows.reshape(height, stride)[:, :width * 3].reshape(height, width, 3)
    return np.ascontiguousarray(rgb[::-1, ::-1])


def shifted(request: tuple, turn: float) -> tuple:
    """``request`` with its RA range moved by ``turn`` degrees."""
    survey, (ra0, ra1), dec_range, width, height = request
    return survey, (round(ra0 + turn, 5), round(ra1 + turn, 5)), dec_range, width, height


def same_sky(a: tuple, b: tuple) -> bool:
    """Whether two requests ask for the same image, their RA ranges perhaps whole turns apart."""
    if a[0] != b[0] or a[2:] != b[2:]:
        return False
    turns = round((b[1][0] - a[1][0]) / 360.0)
    return all(abs(x + 360.0 * turns - y) < 1e-4 for x, y in zip(a[1], b[1]))


class SkyBackground(QObject):
    """Loads and shows the survey image under ``plot``'s view.

    ``transport`` and ``cache`` go to :func:`hips.fetch_cutout` (None: the
    real hips2fits and the user's cache); with ``offline`` only cached
    cutouts show.  ``statusChanged`` carries a line for the sky's status
    ("" when there is nothing to say); ``loaded`` the request now shown.
    """

    statusChanged = Signal(str)
    loaded = Signal(object)

    def __init__(self, plot: pg.PlotWidget, transport: hips.BytesTransport | None = None,
                 cache: hips.CutoutCache | None = None, offline: bool = False,
                 survey: str | None = DEFAULT_SURVEY, zoom_deg: float = BACKGROUND_ZOOM_DEG):
        super().__init__(plot)
        self.plot = plot
        self.view_box = plot.getViewBox()
        self.transport = transport
        self.cache = cache
        self.offline = offline
        self.zoom_deg = zoom_deg
        self._survey = survey
        self.shown: tuple | None = None  # the request whose image is on screen
        self._pending: tuple | None = None
        self.status = ""
        self.runner = DetachedJobRunner(self, name="sky-image")  # closing never waits for it

        self.item = pg.ImageItem(axisOrder="row-major")
        self.item.setZValue(BACKGROUND_Z)
        self.item.setOpacity(BACKGROUND_OPACITY)
        self.item.setVisible(False)
        plot.addItem(self.item)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(BACKGROUND_DEBOUNCE_MS)
        self.timer.timeout.connect(self.update)
        self.view_box.sigRangeChanged.connect(lambda *_: self.timer.start())
        self.view_box.sigResized.connect(lambda *_: self.timer.start())

    # -- the survey ------------------------------------------------------------

    @property
    def survey(self) -> str | None:
        """The key of the survey shown (see ``hips.SURVEYS``), or None for no background."""
        return self._survey

    @survey.setter
    def survey(self, key: str | None) -> None:
        if key is not None and key not in hips.SURVEYS:
            raise ValueError(f"unknown survey {key!r}; known: {', '.join(hips.SURVEYS)}")
        if key == self._survey:
            return
        self._survey = key
        self.clear()
        self.update()

    def clear(self) -> None:
        """Hide the image and forget any load under way."""
        self._pending = None
        self.shown = None
        self.item.setVisible(False)
        self._set_status("")

    # -- loading ---------------------------------------------------------------

    def wanted(self) -> tuple | None:
        """The request the view needs now, or None when no image should show.

        A request is (survey key, (ra_lo, ra_hi), (dec_lo, dec_hi), width_px,
        height_px), in plot coordinates, so its RA range may run below 0 or
        past 360 as the view does.
        """
        if self._survey is None or self.zoom_deg <= 0:
            return None
        (x0, x1), (y0, y1) = self.view_box.viewRange()
        dec = (y0 + y1) / 2
        true_width = abs(x1 - x0) * math.cos(math.radians(max(-89.9, min(89.9, dec))))
        if min(true_width, abs(y1 - y0)) > self.zoom_deg:
            return None
        bounds = self.view_box.rect()
        if bounds.width() < 1 or bounds.height() < 1:
            return None
        ra_lo, ra_hi = sorted((x0, x1))
        dec_lo, dec_hi = sorted((y0, y1))
        dra, ddec = (ra_hi - ra_lo) * MARGIN, (dec_hi - dec_lo) * MARGIN
        ra_range = (ra_lo - dra, ra_hi + dra)
        dec_range = (max(-90.0, dec_lo - ddec), min(90.0, dec_hi + ddec))
        if dec_range[1] - dec_range[0] <= 0:
            return None
        # Screen resolution over the fetched area, capped.
        width_px = bounds.width() * (ra_range[1] - ra_range[0]) / (ra_hi - ra_lo)
        height_px = bounds.height() * (dec_range[1] - dec_range[0]) / (dec_hi - dec_lo)
        shrink = min(1.0, MAX_IMAGE_PX / max(width_px, height_px))
        # Rounded, so the same view asks for the same (cached) cutout.
        return (self._survey, (round(ra_range[0], 5), round(ra_range[1], 5)),
                (round(dec_range[0], 5), round(dec_range[1], 5)),
                max(1, round(width_px * shrink)), max(1, round(height_px * shrink)))

    @staticmethod
    def covers(have: tuple | None, want: tuple) -> bool:
        """Whether the image for ``have`` serves ``want``: same survey, area inside, fine enough."""
        if have is None or have[0] != want[0]:
            return False
        (hr0, hr1), (hd0, hd1), hw, hh = have[1], have[2], have[3], have[4]
        (wr0, wr1), (wd0, wd1), ww, wh = want[1], want[2], want[3], want[4]
        # The view itself (want without its margin) must lie inside.
        mr, md = (wr1 - wr0) * MARGIN / (1 + 2 * MARGIN), (wd1 - wd0) * MARGIN / (1 + 2 * MARGIN)
        if not (hr0 <= wr0 + mr and wr1 - mr <= hr1 and hd0 <= wd0 + md and wd1 - md <= hd1):
            return False
        have_res = (hd1 - hd0) / hh  # degrees per pixel
        want_res = (wd1 - wd0) / wh
        return have_res <= want_res * COARSE_FACTOR

    def update(self) -> None:
        """Load the image the view needs, unless what is shown or coming serves it."""
        want = self.wanted()
        if want is None:
            if self.shown is not None or self._pending is not None or self.status:
                self.clear()
            return
        if self.covers(self._pending, want):
            return
        if self.covers(self.shown, want):
            if self._pending is not None:  # back over the image shown: drop the load under way
                self._pending = None
                self._set_status("")
            return
        survey = hips.SURVEYS[want[0]]
        if not survey.covers(want[2]):
            self.clear()
            self._set_status(f"{survey.label} has no images south of Dec {survey.dec_min:+.0f}°")
            return
        self.load(want)

    def load(self, request: tuple) -> None:
        self._pending = request
        survey = hips.SURVEYS[request[0]]
        self._set_status(f"Loading the {survey.label} sky image from CDS…")
        transport, cache, offline = self.transport, self.cache, self.offline

        def fetch():
            if not self._is_pending(request):
                return None  # the view moved on before this job's turn
            wcs = hips.view_wcs(request[1], request[2], request[3], request[4])
            data = hips.fetch_cutout(survey, wcs, transport=transport, cache=cache, offline=offline)
            return image_array(data)

        self.runner.submit(fetch, lambda image: self._done(request, image),
                           lambda exc: self._failed(request, exc))

    def _is_pending(self, request: tuple) -> bool:
        """Whether ``request`` is the load under way, perhaps since moved a whole turn (``shift_ra``)."""
        return self._pending is not None and same_sky(request, self._pending)

    def _done(self, request: tuple, image: np.ndarray | None) -> None:
        if not self._is_pending(request) or image is None:
            return  # the view moved on; a newer request is coming
        request, self._pending = self._pending, None
        self.show_image(request, image)

    def _failed(self, request: tuple, exc: Exception) -> None:
        if not self._is_pending(request):
            return
        self._pending = None
        if not isinstance(exc, CatalogError):
            log.error("loading the sky image failed", exc_info=exc)
        self._set_status(f"{hips.SURVEYS[request[0]].label} sky image unavailable: {exc}")

    def show_image(self, request: tuple, image: np.ndarray) -> None:
        """Draw ``image`` (as from :func:`image_array`) over ``request``'s RA/Dec rectangle."""
        (ra0, ra1), (dec0, dec1) = request[1], request[2]
        self.item.setImage(image, autoLevels=False, levels=(0, 255))
        self.item.setRect(QRectF(ra0, dec0, ra1 - ra0, dec1 - dec0))
        self.item.setVisible(True)
        self.shown = request
        self._set_status("")
        self.loaded.emit(request)

    def shift_ra(self, turn: float) -> None:
        """Move the image shown and the load under way by ``turn`` (a whole turn, +-360) in RA.

        The sky view does this as it moves its view a turn to pan across 0h,
        so the image stays under the same stars and nothing reloads.
        """
        if self._pending is not None:
            self._pending = shifted(self._pending, turn)
        if self.shown is not None:
            self.shown = shifted(self.shown, turn)
            (ra0, ra1), (dec0, dec1) = self.shown[1], self.shown[2]
            self.item.setRect(QRectF(ra0, dec0, ra1 - ra0, dec1 - dec0))

    def _set_status(self, text: str) -> None:
        if text != self.status:
            self.status = text
            self.statusChanged.emit(text)

    def stop(self) -> None:
        """Stop loading, for the view's close, without waiting for a fetch under way.

        A stalled hips2fits request may take two 60 s timeouts; it runs on,
        detached, and its reply is dropped (``_pending`` no longer matches).
        """
        self.timer.stop()
        self._pending = None
        self.runner.discard()
