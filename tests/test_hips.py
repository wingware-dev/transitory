"""HiPS cutouts: the view WCS, the request, the cache, error replies.  No network."""

import json
from pathlib import Path

import numpy as np
import pytest

from transitory.catalog import client as C
from transitory.catalog import hips as H
from transitory.catalog.client import CatalogError

FIXTURE = Path(__file__).parent / "data" / "catalog" / "hips_cutout.jpg"
JPEG = FIXTURE.read_bytes()


class FakeTransport:
    """Answers every request with the next body in ``bodies`` (the last one repeats)."""

    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.calls = []

    def __call__(self, url, params):
        self.calls.append((url, params))
        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        if isinstance(body, Exception):
            raise body
        return body


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(C, "RETRY_BACKOFF_SECONDS", 0.0)


@pytest.fixture
def cache(tmp_path):
    return H.CutoutCache(tmp_path / "hips")


def pixel_edges(width, height):
    """0-based pixel coordinates of the image's outer edges and centre."""
    return np.array([[-0.5, -0.5], [width - 0.5, height - 0.5], [(width - 1) / 2, (height - 1) / 2]])


# --- view_wcs ---------------------------------------------------------------

@pytest.mark.parametrize("ra_range, dec_range, width, height", [
    ((285.43, 285.93), (50.115, 50.365), 64, 32),     # Kepler-10, small
    ((80.0, 100.0), (-75.0, -55.0), 300, 200),        # southern CVZ
    ((270.0, 310.0), (30.0, 55.0), 800, 500),         # Kepler field, wide
    ((350.0, 370.0), (-10.0, 10.0), 100, 100),        # across 0h
    ((10.0, 30.0), (60.0, 89.0), 120, 90),            # near the pole
])
def test_view_wcs_puts_the_edges_on_the_range(ra_range, dec_range, width, height):
    wcs = H.view_wcs(ra_range, dec_range, width, height)
    assert wcs.pixel_shape == (width, height)
    world = wcs.wcs_pix2world(pixel_edges(width, height), 0)
    ra, dec = world[:, 0], world[:, 1]
    # Left edge is the highest RA (east left), bottom edge the lowest Dec.
    assert ra[0] == pytest.approx(ra_range[1] % 360, abs=1e-9)
    assert ra[1] == pytest.approx(ra_range[0] % 360, abs=1e-9)
    assert dec[0] == pytest.approx(dec_range[0], abs=1e-9)
    assert dec[1] == pytest.approx(dec_range[1], abs=1e-9)
    assert ra[2] == pytest.approx((sum(ra_range) / 2) % 360, abs=1e-9)
    assert dec[2] == pytest.approx(sum(dec_range) / 2, abs=1e-9)


def test_view_wcs_is_linear_in_ra_and_dec():
    """Pixels follow plain RA/Dec everywhere, not just near the centre: no reprojection."""
    width, height = 400, 250
    wcs = H.view_wcs((270.0, 310.0), (30.0, 55.0), width, height)
    xs, ys = np.meshgrid(np.linspace(-0.5, width - 0.5, 9), np.linspace(-0.5, height - 0.5, 7))
    ra, dec = wcs.wcs_pix2world(xs, ys, 0)
    np.testing.assert_allclose(ra, 310.0 - (xs + 0.5) * 40.0 / width, atol=1e-9)
    np.testing.assert_allclose(dec, 30.0 + (ys + 0.5) * 25.0 / height, atol=1e-9)


def test_view_wcs_round_trips_world_to_pixel():
    wcs = H.view_wcs((80.0, 100.0), (-75.0, -55.0), 300, 200)
    ra = np.array([80.0, 85.5, 92.25, 100.0])
    dec = np.array([-75.0, -70.0, -60.125, -55.0])
    x, y = wcs.wcs_world2pix(ra, dec, 0)
    back_ra, back_dec = wcs.wcs_pix2world(x, y, 0)
    np.testing.assert_allclose(back_ra, ra, atol=1e-9)
    np.testing.assert_allclose(back_dec, dec, atol=1e-9)


def test_view_wcs_header_has_crval2_zero_and_an_offset_crpix():
    wcs = H.view_wcs((285.43, 285.93), (50.115, 50.365), 64, 32)
    header = H.wcs_params(wcs)
    assert header["CTYPE1"] == "RA---CAR" and header["CTYPE2"] == "DEC--CAR"
    assert header["CRVAL2"] == 0.0
    assert header["CRPIX2"] < 0  # Dec 0 lies far below the image
    assert (header["NAXIS"], header["NAXIS1"], header["NAXIS2"]) == (2, 64, 32)
    assert header["CDELT1"] < 0 < header["CDELT2"]


def test_view_wcs_takes_ranges_in_either_order():
    a = H.wcs_params(H.view_wcs((10, 20), (5, -5), 50, 40))
    b = H.wcs_params(H.view_wcs((20, 10), (-5, 5), 50, 40))
    assert a == b


@pytest.mark.parametrize("ra_range, dec_range, width, height", [
    ((10, 10), (0, 5), 10, 10),
    ((10, 20), (3, 3), 10, 10),
    ((0, 400), (0, 5), 10, 10),
    ((10, 20), (80, 95), 10, 10),
    ((10, 20), (0, 5), 0, 10),
])
def test_view_wcs_rejects_impossible_views(ra_range, dec_range, width, height):
    with pytest.raises(ValueError):
        H.view_wcs(ra_range, dec_range, width, height)


# --- surveys ----------------------------------------------------------------

def test_surveys_table():
    assert list(H.SURVEYS) == ["dss2", "2mass", "panstarrs"]
    assert all(s.hips_id.startswith("CDS/P/") for s in H.SURVEYS.values())
    panstarrs = H.SURVEYS["panstarrs"]
    assert panstarrs.covers((40.0, 50.0)) and panstarrs.covers((-40.0, -20.0))
    assert not panstarrs.covers((-75.0, -55.0))
    assert H.SURVEYS["dss2"].covers((-90.0, -80.0))


# --- fetch_cutout -----------------------------------------------------------

def test_fetch_cutout_asks_hips2fits_and_caches_the_jpeg(cache):
    wcs = H.view_wcs((285.43, 285.93), (50.115, 50.365), 64, 32)
    transport = FakeTransport(JPEG)
    assert H.fetch_cutout("dss2", wcs, transport=transport, cache=cache) == JPEG
    [(url, params)] = transport.calls
    assert url == H.HIPS2FITS_URL
    assert params["hips"] == "CDS/P/DSS2/color"
    assert params["format"] == "jpg"
    assert json.loads(params["wcs"]) == H.wcs_params(wcs)

    # Again, and from a new cache on the same directory: no second request.
    assert H.fetch_cutout("dss2", wcs, transport=transport, cache=cache) == JPEG
    fresh = H.CutoutCache(cache.directory)
    assert H.fetch_cutout(H.SURVEYS["dss2"], wcs, transport=transport, cache=fresh) == JPEG
    assert len(transport.calls) == 1
    assert [p.suffix for p in cache.directory.iterdir()] == [".jpg"]


def test_each_survey_and_view_is_cached_separately(cache):
    transport = FakeTransport(JPEG)
    wcs = H.view_wcs((10, 20), (0, 5), 50, 40)
    H.fetch_cutout("dss2", wcs, transport=transport, cache=cache)
    H.fetch_cutout("2mass", wcs, transport=transport, cache=cache)
    H.fetch_cutout("dss2", H.view_wcs((10, 20), (0, 5), 100, 80), transport=transport, cache=cache)
    assert len(transport.calls) == 3
    assert len(list(cache.directory.iterdir())) == 3


def test_offline_answers_only_from_the_cache(cache):
    wcs = H.view_wcs((10, 20), (0, 5), 50, 40)
    transport = FakeTransport(CatalogError("must not be called"))
    with pytest.raises(CatalogError, match="offline"):
        H.fetch_cutout("dss2", wcs, transport=transport, cache=cache, offline=True)
    assert transport.calls == []
    H.fetch_cutout("dss2", wcs, transport=FakeTransport(JPEG), cache=cache)
    assert H.fetch_cutout("dss2", wcs, transport=transport, cache=cache, offline=True) == JPEG


def test_a_non_jpeg_reply_raises_and_is_not_cached(cache):
    wcs = H.view_wcs((10, 20), (0, 5), 50, 40)
    transport = FakeTransport(b"Error: unknown HiPS", JPEG)
    with pytest.raises(CatalogError, match="not a JPEG.*unknown HiPS"):
        H.fetch_cutout("dss2", wcs, transport=transport, cache=cache)
    assert not cache.directory.exists() or not list(cache.directory.iterdir())
    assert H.fetch_cutout("dss2", wcs, transport=transport, cache=cache) == JPEG


def test_a_transport_error_propagates_and_is_not_cached(cache):
    wcs = H.view_wcs((10, 20), (0, 5), 50, 40)
    transport = FakeTransport(CatalogError("offline"), JPEG)
    with pytest.raises(CatalogError, match="offline"):
        H.fetch_cutout("dss2", wcs, transport=transport, cache=cache)
    assert H.fetch_cutout("dss2", wcs, transport=transport, cache=cache) == JPEG
    assert len(transport.calls) == 2


def test_unknown_survey_key(cache):
    with pytest.raises(ValueError, match="unknown survey"):
        H.fetch_cutout("sdss", H.view_wcs((10, 20), (0, 5), 50, 40), cache=cache)


def test_default_cache_lives_under_cache_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TRANSITORY_CACHE_DIR", str(tmp_path))
    assert H.CutoutCache().directory == tmp_path / "hips"


def test_fixture_is_a_jpeg_of_the_recorded_size():
    from PIL import Image

    with Image.open(FIXTURE) as image:
        assert image.format == "JPEG"
        assert image.size == (64, 32)


# --- requests_bytes_transport -----------------------------------------------

class FakeReply:
    def __init__(self, status, content=b"", reason="", text=None):
        self.status_code = status
        self.content = content
        self.reason = reason
        self.text = content.decode("utf-8", "replace") if text is None else text


@pytest.fixture
def fake_get(monkeypatch):
    """Replaces requests.get with a queue of replies or exceptions."""
    import requests

    calls, queue = [], []

    def get(url, params=None, headers=None, timeout=None):
        calls.append((url, params, headers, timeout))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(requests, "get", get)
    return calls, queue


def test_transport_returns_bytes_with_user_agent_and_timeout(fake_get):
    calls, queue = fake_get
    queue.append(FakeReply(200, JPEG))
    assert H.requests_bytes_transport(H.HIPS2FITS_URL, {"hips": "x"}) == JPEG
    [(url, params, headers, timeout)] = calls
    assert headers["User-Agent"].startswith("transitory/")
    assert timeout == C.REQUEST_TIMEOUT_SECONDS


def test_transport_retries_once_on_timeout_and_5xx(fake_get):
    import requests

    calls, queue = fake_get
    queue.extend([requests.Timeout("slow"), FakeReply(200, JPEG)])
    assert H.requests_bytes_transport(H.HIPS2FITS_URL, {}) == JPEG
    queue.extend([FakeReply(503), FakeReply(200, JPEG)])
    assert H.requests_bytes_transport(H.HIPS2FITS_URL, {}) == JPEG
    assert len(calls) == 4


def test_transport_gives_up_after_two_failures(fake_get):
    import requests

    calls, queue = fake_get
    queue.extend([requests.ConnectionError("down"), requests.ConnectionError("down")])
    with pytest.raises(CatalogError, match="tried twice"):
        H.requests_bytes_transport(H.HIPS2FITS_URL, {})
    queue.extend([FakeReply(502, reason="Bad Gateway", text=""),
                  FakeReply(502, reason="Bad Gateway", text="")])
    with pytest.raises(CatalogError, match="HTTP 502: Bad Gateway"):
        H.requests_bytes_transport(H.HIPS2FITS_URL, {})
    assert len(calls) == 4


def test_transport_reports_a_4xx_explanation_without_retrying(fake_get):
    calls, queue = fake_get
    queue.append(FakeReply(400, b"Unknown HiPS 'CDS/P/NOPE'"))
    with pytest.raises(CatalogError, match="HTTP 400: Unknown HiPS"):
        H.requests_bytes_transport(H.HIPS2FITS_URL, {})
    assert len(calls) == 1
