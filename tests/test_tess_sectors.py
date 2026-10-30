"""TESS sector lookup from the FFI footprint file, with TESScut as fallback.

Replays recorded replies from ``tests/data/tess_sectors/`` (see its
``record.py``); nothing here touches the network.
"""

import json
import os
import time
from pathlib import Path

import pytest

from transitory.catalog.client import CatalogError
from transitory.pipeline import tess_sectors as T

DATA = Path(__file__).resolve().parent / "data" / "tess_sectors"
FOOTPRINTS = (DATA / "footprints.json").read_text(encoding="utf-8")
TESSCUT_PI_MEN = (DATA / "tesscut_pi_men.json").read_text(encoding="utf-8")

PI_MEN = (84.291188, -80.469119)
# The sectors TESScut listed for pi Men when the fixtures were recorded.
PI_MEN_SECTORS = [1, 4, 8, 11, 12, 13, 27, 28, 31, 34, 35, 38, 39, 61, 62, 64, 65, 66, 67, 68,
                  88, 89, 93, 94, 95]
# Checked against TESScut for both RA 359.9 and 0.1 at Dec -30.
DEC_MINUS_30_SECTORS = [2, 29, 69, 96, 106, 107]


class FakeTransport:
    """Answers the footprint and TESScut URLs from fixtures, or fails as told."""

    def __init__(self, footprints=FOOTPRINTS, tesscut=TESSCUT_PI_MEN):
        self.replies = {T.FOOTPRINT_URL: footprints, T.TESSCUT_SECTOR_URL: tesscut}
        self.calls = []

    def __call__(self, method, url, params):
        self.calls.append((url, params))
        reply = self.replies[url]
        if reply is None:
            raise CatalogError(f"{url}: connection timed out (tried twice)")
        return reply

    def urls(self):
        return [url for url, _ in self.calls]


def _age_cache(seconds):
    path = T.footprint_path()
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_pi_men_matches_tesscut():
    transport = FakeTransport()
    assert T.sectors_for(*PI_MEN, transport=transport) == PI_MEN_SECTORS
    assert transport.urls() == [T.FOOTPRINT_URL]
    # The recorded TESScut reply agrees.
    assert T.tesscut_sectors(*PI_MEN, transport=transport) == PI_MEN_SECTORS


def test_footprint_file_is_cached_for_a_week():
    transport = FakeTransport()
    T.sectors_for(*PI_MEN, transport=transport)
    assert T.footprint_path().is_file()
    assert T.sectors_for(*PI_MEN, transport=transport) == PI_MEN_SECTORS
    assert transport.urls() == [T.FOOTPRINT_URL]

    _age_cache(T.REFRESH_SECONDS + 60)
    assert T.sectors_for(*PI_MEN, transport=transport) == PI_MEN_SECTORS
    assert transport.urls() == [T.FOOTPRINT_URL, T.FOOTPRINT_URL]


@pytest.mark.parametrize("ra", [359.9, 0.1, 0.0, 360.0])
def test_ra_wrap(ra):
    """CCDs straddling RA 0/360 contain points on both sides of it."""
    assert T.sectors_for(ra, -30.0, transport=FakeTransport()) == DEC_MINUS_30_SECTORS


def test_polygon_around_a_pole():
    """A CCD polygon that encloses the pole: corners spread over all RAs."""
    region = "POLYGON 0 -85 90 -85 180 -85 270 -85 0 -85"
    text = json.dumps({"s_region": [region], "sequence_number": [5]})
    footprints = T.Footprints.from_json(text)
    assert footprints.sectors_at(0.0, -90.0) == [5]
    assert footprints.sectors_at(123.0, -88.0) == [5]
    assert footprints.sectors_at(123.0, -84.0) == []
    assert footprints.sectors_at(0.0, 90.0) == []


def test_drops_sequence_numbers_past_real_sectors():
    data = json.loads(FOOTPRINTS)
    ra, dec = PI_MEN
    data["s_region"].append(f"POLYGON {ra - 5} {dec - 1} {ra + 5} {dec - 1} {ra + 5} {dec + 1} "
                            f"{ra - 5} {dec + 1} {ra - 5} {dec - 1}")
    data["sequence_number"].append(1751)
    assert T.sectors_for(*PI_MEN, transport=FakeTransport(footprints=json.dumps(data))) \
        == PI_MEN_SECTORS


def test_stale_cache_used_offline():
    T.sectors_for(*PI_MEN, transport=FakeTransport())
    _age_cache(30 * 24 * 3600)
    transport = FakeTransport(footprints=None, tesscut=None)
    assert T.sectors_for(*PI_MEN, transport=transport, offline=True) == PI_MEN_SECTORS
    assert transport.calls == []


def test_offline_without_cache_raises():
    transport = FakeTransport()
    with pytest.raises(T.SectorLookupError, match="offline"):
        T.sectors_for(*PI_MEN, transport=transport, offline=True)
    assert transport.calls == []


def test_stale_cache_used_when_s3_fails():
    T.sectors_for(*PI_MEN, transport=FakeTransport())
    _age_cache(T.REFRESH_SECONDS + 60)
    transport = FakeTransport(footprints=None, tesscut=None)
    assert T.sectors_for(*PI_MEN, transport=transport) == PI_MEN_SECTORS
    assert transport.urls() == [T.FOOTPRINT_URL]


def test_falls_back_to_tesscut_when_s3_fails():
    transport = FakeTransport(footprints=None)
    assert T.sectors_for(*PI_MEN, transport=transport) == PI_MEN_SECTORS
    assert transport.urls() == [T.FOOTPRINT_URL, T.TESSCUT_SECTOR_URL]
    assert transport.calls[1][1] == {"ra": "84.291188", "dec": "-80.469119", "radius": "0"}
    assert not T.footprint_path().exists()


def test_falls_back_to_tesscut_on_a_garbled_footprint_file():
    transport = FakeTransport(footprints="<html>Service Unavailable</html>")
    assert T.sectors_for(*PI_MEN, transport=transport) == PI_MEN_SECTORS
    assert not T.footprint_path().exists()


def test_both_sources_down():
    with pytest.raises(T.SectorLookupError, match="TESScut"):
        T.sectors_for(*PI_MEN, transport=FakeTransport(footprints=None, tesscut=None))
