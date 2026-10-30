"""Sky images from HiPS surveys, cut out by CDS hips2fits (or NASA SkyView) and cached on disk.

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

When hips2fits doesn't answer, :func:`fetch_sky_image` asks NASA SkyView
for the same CAR WCS instead (its FITS header for a ``view_wcs`` request
matches ``view_wcs`` exactly).  SkyView's own RGB mode only answers with a
"no data" placeholder, so a colour survey is fetched as three grey bands
and merged here.  Each service's cutouts are cached under their own keys,
so a SkyView image never stands in for the hips2fits one later.
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
SKYVIEW_URL = "https://skyview.gsfc.nasa.gov/current/cgi/runquery.pl"

# hips2fits gets one short try, since SkyView stands behind it.
HIPS2FITS_TIMEOUT_SECONDS = 15.0
# Each SkyView band is stretched from this percentile (black, a little over
# the sky) to this one (white, the brighter stars).
SKYVIEW_STRETCH_PERCENTILES = (30.0, 99.9)
# Width (pixels) of the median filter that takes the background off a
# skyview_flatten survey's bands: wider than stars, narrower than its tiles.
SKYVIEW_FLATTEN_PX = 15

# The services a cutout can come from: key -> name for status lines.
SERVICES = {"hips2fits": "CDS hips2fits", "skyview": "NASA SkyView"}

# transport(url, params) -> reply body as bytes
BytesTransport = Callable[[str, dict[str, str]], bytes]

_JPEG_MAGIC = b"\xff\xd8\xff"

# Acknowledgements, as each service asks (checked 2026-10-30).  hips2fits:
# the astroquery hips2fits docs (by its CDS author); CDS's own site was
# unreachable to check.  DSS: archive.stsci.edu/dss/acknowledging.html;
# 2MASS: irsa.ipac.caltech.edu/data/2MASS/docs/publications/; PanSTARRS:
# outerspace.stsci.edu/display/PANSTARRS/ ("Credit where it is due").  SkyView:
# skyview.gsfc.nasa.gov/current/help/faq.html ("How can I acknowledge SkyView").
HIPS2FITS_CREDIT = (
    "This research has made use of the hips2fits, a tool developed at CDS, Strasbourg, France "
    "aiming at extracting FITS images from HiPS sky maps with respect to a WCS.")
SKYVIEW_CREDIT = (
    "We acknowledge the use of NASA's SkyView facility (http://skyview.gsfc.nasa.gov) located "
    "at NASA Goddard Space Flight Center.")
SURVEY_CREDITS = {
    "dss2": "The Digitized Sky Surveys were produced at the Space Telescope Science Institute "
            "under U.S. Government grant NAG W-2166. The images of these surveys are based on "
            "photographic data obtained using the Oschin Schmidt Telescope on Palomar Mountain "
            "and the UK Schmidt Telescope. The plates were processed into the present compressed "
            "digital form with the permission of these institutions.",
    "2mass": "This publication makes use of data products from the Two Micron All Sky Survey, "
             "which is a joint project of the University of Massachusetts and the Infrared "
             "Processing and Analysis Center/California Institute of Technology, funded by the "
             "National Aeronautics and Space Administration and the National Science Foundation.",
    "panstarrs": "The Pan-STARRS1 Surveys (PS1) and the PS1 public science archive have been made "
                 "possible through contributions by the Institute for Astronomy, the University of "
                 "Hawaii, the Pan-STARRS Project Office, the Max-Planck Society and its "
                 "participating institutes, the Max Planck Institute for Astronomy, Heidelberg and "
                 "the Max Planck Institute for Extraterrestrial Physics, Garching, The Johns Hopkins "
                 "University, Durham University, the University of Edinburgh, the Queen's University "
                 "Belfast, the Harvard-Smithsonian Center for Astrophysics, the Las Cumbres "
                 "Observatory Global Telescope Network Incorporated, the National Central University "
                 "of Taiwan, the Space Telescope Science Institute, the National Aeronautics and "
                 "Space Administration under Grant No. NNX08AR22G issued through the Planetary "
                 "Science Division of the NASA Science Mission Directorate, the National Science "
                 "Foundation Grant No. AST-1238877, the University of Maryland, Eotvos Lorand "
                 "University (ELTE), the Los Alamos National Laboratory, and the Gordon and Betty "
                 "Moore Foundation.",
}


@dataclass(frozen=True)
class Survey:
    """One HiPS colour survey that can be shown behind the sky.

    ``skyview`` names the SkyView surveys standing in for it when hips2fits
    fails: one (grey), or the red, green and blue bands of a colour image;
    empty when SkyView doesn't have it.  ``skyview_scaling`` is the SkyView
    brightness scaling that leaves the bands room to stretch;
    ``skyview_flatten`` takes off a background that SkyView leaves uneven
    (2MASS: each tile's sky level differs, so the mosaic shows blocks).
    """

    key: str
    label: str
    hips_id: str
    dec_min: float = -90.0  # southern limit of the survey's coverage (deg)
    skyview: tuple[str, ...] = ()
    skyview_scaling: str = ""
    skyview_flatten: bool = False

    def covers(self, dec_range: tuple[float, float]) -> bool:
        """Whether any of the Dec range lies inside the survey's coverage."""
        return max(dec_range) > self.dec_min


SURVEYS: dict[str, Survey] = {s.key: s for s in (
    Survey("dss2", "DSS2 colour", "CDS/P/DSS2/color",
           skyview=("DSS2 IR", "DSS2 Red", "DSS2 Blue")),
    Survey("2mass", "2MASS colour", "CDS/P/2MASS/color",
           # SkyView's default Log scaling leaves 2MASS nearly all black.
           skyview=("2MASS-K", "2MASS-H", "2MASS-J"), skyview_scaling="LogLog",
           skyview_flatten=True),
    # Pan-STARRS observed from Hawaii and covers only Dec > -30 deg.  Not on SkyView.
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


def skyview_params(name: str, wcs: WCS, scaling: str = "") -> dict[str, str]:
    """The SkyView query parameters for one grey JPEG of survey ``name`` on ``wcs``.

    ``wcs`` must be a :func:`view_wcs`: SkyView builds the same CAR
    projection from the centre, size and ``RefCoords`` (CRVAL1, 0).
    ``scaling`` is SkyView's brightness scaling ("": its default, Log).
    """
    if wcs.pixel_shape is None:
        raise ValueError("the WCS needs pixel_shape (the image size)")
    width, height = (int(n) for n in wcs.pixel_shape)
    (_, dec), = wcs.wcs_pix2world([[(width - 1) / 2, (height - 1) / 2]], 0)
    crval1 = float(wcs.wcs.crval[0])
    size_ra, size_dec = abs(wcs.wcs.cdelt[0]) * width, abs(wcs.wcs.cdelt[1]) * height
    params = {"Survey": name,
              "Position": f"{crval1:.10g},{float(dec):.10g}",
              "Size": f"{size_ra:.10g},{size_dec:.10g}",
              "Pixels": f"{width},{height}",
              "Projection": "Car",
              "RefCoords": f"{crval1:.10g},0",
              "Return": "JPEG"}
    if scaling:
        params["Scaling"] = scaling
    return params


def requests_bytes_transport(url: str, params: dict[str, str],
                             timeout: float | None = None, attempts: int = 2) -> bytes:
    """The real transport: one GET with the project user agent, returning bytes.

    Follows ``requests_transport``'s policy: a timeout, failed connection
    or 5xx reply is tried once more after ``RETRY_BACKOFF_SECONDS``
    (``attempts=1``: not tried again).
    """
    import requests

    if timeout is None:
        timeout = _client.REQUEST_TIMEOUT_SECONDS
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(1, attempts + 1):
        try:
            reply = requests.get(url, params=params, headers=headers, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == attempts:
                raise CatalogError(f"{url}: {exc}" + (" (tried twice)" if attempts == 2 else "")
                                   ) from exc
            log.warning("%s: %s; trying again in %.0f s", url, exc,
                        _client.RETRY_BACKOFF_SECONDS)
        except requests.RequestException as exc:
            raise CatalogError(f"{url}: {exc}") from exc
        else:
            if reply.status_code not in _client._TRANSIENT_STATUS or attempt == attempts:
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


def cutout_key(params: dict[str, str] | list[dict[str, str]], url: str = HIPS2FITS_URL) -> str:
    """A stable identifier for one cutout request (``params``, or a list of them) to ``url``."""
    blob = json.dumps([url, params], sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class Cutout:
    """A cutout's JPEG bytes and the key of the service (in :data:`SERVICES`) it came from."""

    data: bytes
    service: str


def _survey(survey: Survey | str) -> Survey:
    if isinstance(survey, str):
        try:
            return SURVEYS[survey]
        except KeyError:
            raise ValueError(f"unknown survey {survey!r}; known: {', '.join(SURVEYS)}") from None
    return survey


def fetch_cutout(survey: Survey | str, wcs: WCS, *, transport: BytesTransport | None = None,
                 cache: CutoutCache | None = None, offline: bool = False) -> bytes:
    """JPEG bytes of ``survey`` drawn on ``wcs``: :func:`fetch_sky_image`'s data."""
    return fetch_sky_image(survey, wcs, transport=transport, cache=cache, offline=offline).data


def fetch_sky_image(survey: Survey | str, wcs: WCS, *, transport: BytesTransport | None = None,
                    cache: CutoutCache | None = None, offline: bool = False) -> Cutout:
    """``survey`` drawn on ``wcs``, from the cache, hips2fits or else SkyView.

    ``survey`` is a :class:`Survey` or a key of :data:`SURVEYS`; ``wcs`` a
    :func:`view_wcs`.  The JPEG's top row is the image's north (highest
    Dec) edge and its left column the highest RA, as in the sky view.
    hips2fits gets one short try (``HIPS2FITS_TIMEOUT_SECONDS``) with the
    real transport; when it fails and the survey is on SkyView, SkyView's
    image is used.  Offline, either service's cached image will do.
    Raises ``CatalogError`` when no service gives an image, or ``offline``
    is set and the cutout isn't cached.
    """
    survey = _survey(survey)
    cache = cache if cache is not None else CutoutCache()
    params = request_params(survey, wcs)
    key = cutout_key(params)
    cached = cache.get(key)
    if cached is not None:
        return Cutout(cached, "hips2fits")
    sky_params = [skyview_params(name, wcs, survey.skyview_scaling) for name in survey.skyview]
    sky_key = cutout_key(sky_params, SKYVIEW_URL) if sky_params else None
    if offline:
        cached = cache.get(sky_key) if sky_key else None
        if cached is not None:
            return Cutout(cached, "skyview")
        raise CatalogError(f"{survey.label} cutout not cached and working offline")
    try:
        if transport is None:
            data = requests_bytes_transport(HIPS2FITS_URL, params,
                                            timeout=HIPS2FITS_TIMEOUT_SECONDS, attempts=1)
        else:
            data = transport(HIPS2FITS_URL, params)
        if not data.startswith(_JPEG_MAGIC):
            snippet = data[:200].decode("utf-8", "replace").strip()
            raise CatalogError(f"{HIPS2FITS_URL}: {survey.label} reply is not a JPEG: {snippet!r}")
    except CatalogError as exc:
        if sky_key is None:
            raise
        log.warning("%s; trying NASA SkyView", exc)
        cached = cache.get(sky_key)
        if cached is not None:
            return Cutout(cached, "skyview")
        try:
            data = _skyview_image(survey, sky_params, transport or requests_bytes_transport)
        except CatalogError as sky_exc:
            raise CatalogError(f"{SERVICES['hips2fits']}: {exc}; "
                               f"{SERVICES['skyview']}: {sky_exc}") from sky_exc
        cache.put(sky_key, data)
        return Cutout(data, "skyview")
    cache.put(key, data)
    return Cutout(data, "hips2fits")


def _skyview_image(survey: Survey, params: list[dict[str, str]],
                   transport: BytesTransport) -> bytes:
    """One JPEG from SkyView's grey band images: one band grey, three merged as RGB.

    SkyView scales each band from its faintest pixel, which leaves the sky
    pale (beige, merged); each band is stretched so the sky is nearly black,
    as in the hips2fits colour surveys (``SKYVIEW_STRETCH_PERCENTILES``).
    A ``skyview_flatten`` survey first has its local background taken off.
    The bands are asked for at once, since SkyView may take half a minute
    over each.
    """
    import io
    from concurrent.futures import ThreadPoolExecutor

    import numpy as np
    from PIL import Image, UnidentifiedImageError

    with ThreadPoolExecutor(max_workers=len(params)) as pool:
        replies = list(pool.map(lambda band_params: transport(SKYVIEW_URL, band_params), params))
    bands = []
    for band_params, data in zip(params, replies):
        name = band_params["Survey"]
        try:
            band = Image.open(io.BytesIO(data))
            band.load()
        except (UnidentifiedImageError, OSError):
            snippet = data[:200].decode("utf-8", "replace").strip()
            raise CatalogError(f"{SKYVIEW_URL}: {name} reply is not an image: {snippet!r}") from None
        wanted = tuple(int(n) for n in band_params["Pixels"].split(","))
        # Where it has nothing, SkyView answers with a small colour picture saying so.
        if band.format != "JPEG" or band.mode != "L" or band.size != wanted:
            raise CatalogError(f"{SKYVIEW_URL}: no {name} image for this area "
                               f"(got a {band.size[0]} x {band.size[1]} {band.mode} {band.format})")
        pixels = np.asarray(band, dtype=float)
        if survey.skyview_flatten:
            from scipy import ndimage

            pixels -= ndimage.median_filter(np.asarray(band), size=SKYVIEW_FLATTEN_PX)
        lo, hi = np.percentile(pixels, SKYVIEW_STRETCH_PERCENTILES)
        stretched = np.clip((pixels - lo) / max(hi - lo, 1.0), 0.0, 1.0)
        bands.append(Image.fromarray(np.round(stretched * 255).astype(np.uint8), mode="L"))
    if len(bands) not in (1, 3):
        raise ValueError(f"{survey.key}: SkyView needs one band or three, not {len(bands)}")
    out = io.BytesIO()
    # No chroma subsampling: the bands' stars are single pixels at small sizes.
    image = bands[0] if len(bands) == 1 else Image.merge("RGB", bands)
    image.save(out, format="JPEG", quality=90, subsampling=0)
    return out.getvalue()
