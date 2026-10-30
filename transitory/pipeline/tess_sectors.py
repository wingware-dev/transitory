"""Which TESS sectors observed a sky position, without MAST's CAOM search.

The primary source is the FFI footprint file MAST publishes on AWS
(:data:`FOOTPRINT_URL`): one ``s_region`` polygon per sector, camera and
CCD.  It is downloaded to ``cache_dir()/tess/``, refreshed after
:data:`REFRESH_SECONDS`, and a stale copy is used when working offline or
when the download fails.  A position is tested against each polygon
locally, as astrocut does.

When there is no footprint file at all (first run with S3 unreachable),
TESScut's sector endpoint, a separate MAST service from the CAOM search,
answers the same question.

Footprints are FFI coverage: a listed sector means the star fell on a
CCD, not that a SPOC or TESS-SPOC light curve exists for it.
"""

from __future__ import annotations

import functools
import json
import logging
import math
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..catalog.client import CatalogError, Transport, requests_transport
from ..config import cache_dir

log = logging.getLogger(__name__)

FOOTPRINT_URL = ("https://stpubdata.s3.amazonaws.com/tess/public/footprints/"
                 "tess_ffi_footprint_cache.json")
TESSCUT_SECTOR_URL = "https://mast.stsci.edu/tesscut/api/v0.1/sector"

REFRESH_SECONDS = 7 * 24 * 3600
LOOKUP_TIMEOUT_SECONDS = 15.0

# The footprint file has a stray sequence number (1751) besides the real
# sectors; nothing near that is a sector.
MAX_SECTOR = 1000

# A CCD is about 12 degrees across, so a polygon whose first vertex is
# farther than this from the position can't contain it.
_NEAR_DEG = 25.0


class SectorLookupError(CatalogError):
    """Neither the footprint file nor TESScut could say which sectors cover a position."""


def footprint_path() -> Path:
    return cache_dir() / "tess" / "tess_ffi_footprint_cache.json"


def _default_transport() -> Transport:
    return functools.partial(requests_transport, timeout=LOOKUP_TIMEOUT_SECONDS)


@dataclass(frozen=True)
class Footprints:
    """The footprint file's polygons, as unit vectors for fast tests."""

    sectors: np.ndarray    # (n,) sector of each polygon
    vertices: np.ndarray   # (n, k, 3) unit vectors of each polygon's corners

    @classmethod
    def from_json(cls, text: str) -> Footprints:
        """Parse the footprint file; raises ``ValueError`` if it isn't one."""
        data = json.loads(text)
        try:
            regions, sequences = data["s_region"], data["sequence_number"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"not a TESS footprint file: missing {exc}") from None
        if len(regions) != len(sequences):
            raise ValueError("footprint file has unequal s_region and sequence_number lists")
        sectors, corners = [], []
        for region, sequence in zip(regions, sequences):
            if int(sequence) > MAX_SECTOR:
                continue
            words = region.split()
            if not words or words[0].upper() != "POLYGON" or len(words) % 2 == 0:
                raise ValueError(f"unexpected s_region {region[:80]!r}")
            pairs = np.array(words[1:], dtype=float).reshape(-1, 2)
            if len(pairs) > 1 and np.allclose(pairs[0], pairs[-1]):
                pairs = pairs[:-1]     # the closing corner repeats the first
            sectors.append(int(sequence))
            corners.append(pairs)
        if not corners:
            return cls(np.zeros(0, dtype=int), np.zeros((0, 4, 3)))
        width = max(len(c) for c in corners)
        # Pad short polygons by repeating their last corner, a zero-length edge.
        padded = np.array([np.vstack([c, np.repeat(c[-1:], width - len(c), axis=0)])
                           for c in corners])
        return cls(np.array(sectors), _unit_vectors(padded[..., 0], padded[..., 1]))

    def sectors_at(self, ra: float, dec: float) -> list[int]:
        """Sorted sectors with a CCD polygon containing (``ra``, ``dec``), in degrees."""
        if not len(self.sectors):
            return []
        point = _unit_vectors(np.array(ra), np.array(dec))
        near = self.vertices[:, 0, :] @ point >= math.cos(math.radians(_NEAR_DEG))
        found = {int(sector) for sector, corners in zip(self.sectors[near], self.vertices[near])
                 if _contains(corners, point)}
        return sorted(found)


def _unit_vectors(ra_deg: np.ndarray, dec_deg: np.ndarray) -> np.ndarray:
    ra, dec = np.radians(ra_deg), np.radians(dec_deg)
    return np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=-1)


def _contains(corners: np.ndarray, point: np.ndarray) -> bool:
    """Whether the spherical polygon ``corners`` (unit vectors) contains ``point``.

    Projects onto the plane tangent at the point (gnomonic), where the
    polygon's great-circle edges are straight lines and the point is the
    origin, then casts a ray along +x.  Working on the sphere this way needs
    no special case for RA wrapping at 0/360 or polygons around a pole.
    """
    cos_dist = corners @ point
    if np.any(cos_dist <= 0):
        return False      # a corner 90 degrees or more away: CCDs are far smaller
    # Tangent-plane axes: east and north at the point (any orthogonal pair
    # works; near a pole, fall back to a fixed axis).
    pole = np.array([0.0, 0.0, 1.0])
    east = np.cross(pole, point)
    if np.linalg.norm(east) < 1e-12:
        east = np.array([0.0, 1.0, 0.0])
    east /= np.linalg.norm(east)
    north = np.cross(point, east)
    x = (corners @ east) / cos_dist
    y = (corners @ north) / cos_dist
    inside = False
    for i in range(len(x)):
        j = i - 1
        if (y[i] > 0) != (y[j] > 0):
            crossing = x[j] - y[j] * (x[i] - x[j]) / (y[i] - y[j])
            if crossing > 0:
                inside = not inside
    return inside


# Parsed footprints keyed by (path, mtime, size), so a run over many stars
# parses the 400 KB file once.
_parsed: dict[tuple[str, int, int], Footprints] = {}


def _load(path: Path) -> Footprints:
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    footprints = _parsed.get(key)
    if footprints is None:
        footprints = Footprints.from_json(path.read_text(encoding="utf-8"))
        _parsed.clear()
        _parsed[key] = footprints
    return footprints


def _save(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a temporary file, so readers never see half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def footprints(*, transport: Transport | None = None, offline: bool = False) -> Footprints:
    """The footprint polygons, downloading the file when missing or older than a week.

    Uses a stale copy when ``offline`` or when the download fails.  Raises
    :class:`SectorLookupError` when there is no usable copy and none can be
    downloaded.
    """
    path = footprint_path()
    fresh = path.exists() and time.time() - path.stat().st_mtime < REFRESH_SECONDS
    if fresh or (offline and path.exists()):
        try:
            return _load(path)
        except (OSError, ValueError) as exc:
            log.warning("TESS footprint cache %s is unreadable (%s)", path, exc)
            if offline:
                raise SectorLookupError(f"TESS footprint cache {path} is unreadable: {exc}") from exc
    if offline:
        raise SectorLookupError("TESS footprint file not cached and working offline")
    try:
        text = (transport or _default_transport())("GET", FOOTPRINT_URL, {})
        parsed = Footprints.from_json(text)
    except (CatalogError, ValueError) as exc:
        if path.exists():
            log.warning("TESS footprints: download failed (%s); using the copy from %s",
                        exc, time.strftime("%Y-%m-%d", time.localtime(path.stat().st_mtime)))
            try:
                return _load(path)
            except (OSError, ValueError) as stale_exc:
                log.warning("TESS footprint cache %s is unreadable (%s)", path, stale_exc)
        raise SectorLookupError(f"TESS footprints: {exc}") from exc
    _save(path, text)
    return parsed


def tesscut_sectors(ra: float, dec: float, *, transport: Transport | None = None) -> list[int]:
    """Sorted sectors covering (``ra``, ``dec``) according to TESScut's sector endpoint."""
    params = {"ra": f"{ra:.6f}", "dec": f"{dec:.6f}", "radius": "0"}
    text = (transport or _default_transport())("GET", TESSCUT_SECTOR_URL, params)
    try:
        results = json.loads(text)["results"]
        sectors = {int(row["sector"]) for row in results}
    except (ValueError, KeyError, TypeError) as exc:
        raise CatalogError(f"{TESSCUT_SECTOR_URL}: unexpected reply ({exc}): {text[:200]!r}") from exc
    return sorted(s for s in sectors if s <= MAX_SECTOR)


def sectors_for(ra: float, dec: float, *, transport: Transport | None = None,
                offline: bool = False) -> list[int]:
    """Sorted TESS sectors whose FFIs cover (``ra``, ``dec``), in degrees.

    From the footprint file (fresh, or stale when offline or S3 fails),
    else from TESScut.  ``transport`` is ``transport(method, url, params)
    -> str`` as in :mod:`transitory.catalog.client`.  Raises
    :class:`SectorLookupError` when neither source can answer.
    """
    try:
        return footprints(transport=transport, offline=offline).sectors_at(ra, dec)
    except SectorLookupError as footprint_exc:
        if offline:
            raise
        log.warning("%s; asking TESScut instead", footprint_exc)
        try:
            return tesscut_sectors(ra, dec, transport=transport)
        except CatalogError as exc:
            raise SectorLookupError(f"{footprint_exc}; TESScut: {exc}") from exc
