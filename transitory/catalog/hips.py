"""Sky images from HiPS surveys, cut out by CDS hips2fits and cached on disk.

The sky view plots plain RA/Dec (plate carrée), so :func:`view_wcs` asks
hips2fits for a CAR projection with ``CRVAL2 = 0``.  In that projection RA
and Dec are linear in pixel x and y everywhere, not only near the reference
point, so a cutout's pixels fall exactly on the plot's coordinates and the
image only needs placing over its RA/Dec rectangle, with no reprojection.

hips2fits returns image bytes, which the text-only
:class:`~transitory.catalog.client.CatalogClient` cannot carry, so cutouts
have their own small fetch (:func:`requests_bytes_transport`, with the same
timeout and retry policy as ``requests_transport``) and cache
(:class:`CutoutCache`, under ``cache_dir()/hips``).  Tests pass a fake
transport and a temporary cache, so they never touch the network.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from astropy.wcs import WCS

from . import client as _client
from ..config import USER_AGENT, cache_dir
from .client import CatalogError

log = logging.getLogger(__name__)

HIPS2FITS_URL = "https://alasky.cds.unistra.fr/hips-image-services/hips2fits"

# transport(url, params) -> reply body as bytes
BytesTransport = Callable[[str, dict[str, str]], bytes]

_JPEG_MAGIC = b"\xff\xd8\xff"


@dataclass(frozen=True)
class Survey:
    """One HiPS colour survey that can be shown behind the sky."""

    key: str
    label: str
    hips_id: str
    dec_min: float = -90.0  # southern limit of the survey's coverage (deg)

    def covers(self, dec_range: tuple[float, float]) -> bool:
        """Whether any of the Dec range lies inside the survey's coverage."""
        return max(dec_range) > self.dec_min


SURVEYS: dict[str, Survey] = {s.key: s for s in (
    Survey("dss2", "DSS2 colour", "CDS/P/DSS2/color"),
    Survey("2mass", "2MASS colour", "CDS/P/2MASS/color"),
    # Pan-STARRS observed from Hawaii and covers only Dec > -30 deg.
    Survey("panstarrs", "PanSTARRS DR1 colour", "CDS/P/PanSTARRS/DR1/color-z-zg-g",
           dec_min=-30.0),
)}


def view_wcs(ra_range: tuple[float, float], dec_range: tuple[float, float],
             width_px: int, height_px: int) -> WCS:
    """A CAR WCS whose image covers the RA/Dec rectangle, pixel for pixel.

    ``ra_range`` and ``dec_range`` are in degrees, in either order.  RA
    increases to the left (east left, as the sky view draws it) and Dec
    upwards; the outer pixel edges lie on the range ends.  The reference
    point sits at Dec 0 on the RA centre line, usually outside the image
    (the "CRPIX offset"), which keeps the mapping linear in Dec.  An RA
    range may run past 360 (say 350 to 370) to cross 0h.
    """
    if width_px < 1 or height_px < 1:
        raise ValueError(f"image size must be positive, not {width_px} x {height_px}")
    ra_lo, ra_hi = sorted(map(float, ra_range))
    dec_lo, dec_hi = sorted(map(float, dec_range))
    if not ra_hi - ra_lo > 0 or not dec_hi - dec_lo > 0:
        raise ValueError(f"empty sky range RA {ra_range}, Dec {dec_range}")
    if ra_hi - ra_lo > 360:
        raise ValueError(f"RA range {ra_range} spans more than 360 deg")
    if dec_lo < -90 or dec_hi > 90:
        raise ValueError(f"Dec range {dec_range} goes past a pole")

    cdelt_ra = -(ra_hi - ra_lo) / width_px
    cdelt_dec = (dec_hi - dec_lo) / height_px
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["RA---CAR", "DEC--CAR"]
    wcs.wcs.cunit = ["deg", "deg"]
    wcs.wcs.crval = [((ra_lo + ra_hi) / 2) % 360, 0.0]
    wcs.wcs.cdelt = [cdelt_ra, cdelt_dec]
    # FITS pixels are 1-based, with pixel 1's outer edge at 0.5.
    wcs.wcs.crpix = [0.5 + width_px / 2, 0.5 - dec_lo / cdelt_dec]
    wcs.pixel_shape = (width_px, height_px)
    return wcs


def wcs_params(wcs: WCS) -> dict[str, object]:
    """The WCS as the header dictionary hips2fits takes in its ``wcs`` field."""
    if wcs.pixel_shape is None:
        raise ValueError("the WCS needs pixel_shape (the image size)")
    header = {"NAXIS": 2, "NAXIS1": int(wcs.pixel_shape[0]), "NAXIS2": int(wcs.pixel_shape[1])}
    for key, value in wcs.to_header().items():
        if key not in ("WCSAXES", "COMMENT", "HISTORY", ""):
            header[key] = value
    return header


def request_params(survey: Survey, wcs: WCS) -> dict[str, str]:
    """The hips2fits query parameters for one JPEG cutout."""
    return {"hips": survey.hips_id,
            "wcs": json.dumps(wcs_params(wcs), sort_keys=True),
            "format": "jpg"}


def requests_bytes_transport(url: str, params: dict[str, str],
                             timeout: float | None = None) -> bytes:
    """The real transport: one GET with the project user agent, returning bytes.

    Follows ``requests_transport``'s policy: a timeout, failed connection
    or 5xx reply is tried once more after ``RETRY_BACKOFF_SECONDS``.
    """
    import requests

    if timeout is None:
        timeout = _client.REQUEST_TIMEOUT_SECONDS
    headers = {"User-Agent": USER_AGENT}
    for attempt in (1, 2):
        try:
            reply = requests.get(url, params=params, headers=headers, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == 2:
                raise CatalogError(f"{url}: {exc} (tried twice)") from exc
            log.warning("%s: %s; trying again in %.0f s", url, exc,
                        _client.RETRY_BACKOFF_SECONDS)
        except requests.RequestException as exc:
            raise CatalogError(f"{url}: {exc}") from exc
        else:
            if reply.status_code not in _client._TRANSIENT_STATUS or attempt == 2:
                break
            log.warning("%s: HTTP %s; trying again in %.0f s", url, reply.status_code,
                        _client.RETRY_BACKOFF_SECONDS)
        time.sleep(_client.RETRY_BACKOFF_SECONDS)
    if reply.status_code >= 400:
        # hips2fits explains a bad request (unknown survey, bad WCS) in text.
        detail = reply.text.strip()[:300]
        raise CatalogError(f"{url}: HTTP {reply.status_code}: {detail or reply.reason}")
    return reply.content


class CutoutCache:
    """Cutout images on disk, one file per request, named by its key.

    Sky images don't change, so entries never expire.  A file is written
    to a temporary name and renamed, so a half-written image is never read.
    """

    def __init__(self, directory: Path | str | None = None):
        self.directory = Path(directory) if directory is not None else cache_dir() / "hips"

    def path(self, key: str) -> Path:
        return self.directory / f"{key}.jpg"

    def get(self, key: str) -> bytes | None:
        try:
            return self.path(key).read_bytes()
        except FileNotFoundError:
            return None

    def put(self, key: str, data: bytes) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(tmp, self.path(key))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def cutout_key(params: dict[str, str]) -> str:
    """A stable identifier for one cutout request."""
    blob = json.dumps([HIPS2FITS_URL, params], sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def fetch_cutout(survey: Survey | str, wcs: WCS, *, transport: BytesTransport | None = None,
                 cache: CutoutCache | None = None, offline: bool = False) -> bytes:
    """JPEG bytes of ``survey`` drawn on ``wcs``, from the cache or hips2fits.

    ``survey`` is a :class:`Survey` or a key of :data:`SURVEYS`.  The JPEG's
    top row is the image's north (highest Dec) edge and its left column the
    highest RA, as in the sky view.  Raises ``CatalogError`` when the
    service can't answer, answers with something other than a JPEG, or
    ``offline`` is set and the cutout isn't cached.
    """
    if isinstance(survey, str):
        try:
            survey = SURVEYS[survey]
        except KeyError:
            raise ValueError(f"unknown survey {survey!r}; known: {', '.join(SURVEYS)}") from None
    cache = cache if cache is not None else CutoutCache()
    params = request_params(survey, wcs)
    key = cutout_key(params)
    cached = cache.get(key)
    if cached is not None:
        return cached
    if offline:
        raise CatalogError(f"{survey.label} cutout not cached and working offline")
    data = (transport or requests_bytes_transport)(HIPS2FITS_URL, params)
    if not data.startswith(_JPEG_MAGIC):
        snippet = data[:200].decode("utf-8", "replace").strip()
        raise CatalogError(f"{HIPS2FITS_URL}: {survey.label} reply is not a JPEG: {snippet!r}")
    cache.put(key, data)
    return data
