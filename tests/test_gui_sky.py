"""Sky view driven headless: catalog loading, clicks, highlights and deep-zoom TIC fields.

Catalog data are the recorded MAST / Exoplanet Archive responses; no network.
"""

import json

import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt

from harness import shot
from sky_selection_host import RECORDED, SHOT_NAME, recorded_client, shot_sky_selection
from transitory.catalog import skycatalog
from transitory.catalog.client import CatalogClient, CatalogError, ResponseCache
from transitory.catalog.skycatalog import SkyCatalog, SkyStar
from transitory.gui import sky_view
from transitory.gui.sky_view import (CVZ_RADIUS_DEG, ECLIPTIC_POLES, SkyView, in_kepler_field,
                                     separation_deg, small_circle)
from transitory.models import StarTarget


def _down(method, url, params):
    raise CatalogError("connection refused")


def _make_sky(qtbot, client):
    view = SkyView(client=client)
    qtbot.addWidget(view)
    view.resize(900, 600)
    view.show()
    qtbot.waitExposed(view)
    return view


@pytest.fixture
def client(tmp_path):
    return recorded_client(tmp_path / "catalog")


@pytest.fixture
def sky(qtbot, client):
    view = _make_sky(qtbot, client)
    yield view
    view.close()


@pytest.fixture
def hosts(client):
    return skycatalog.host_stars(client=client, offline_fallback=False)


def screen_pos(view: SkyView, ra: float, dec: float):
    """Viewport pixel position of (ra, dec)."""
    scene = view.view_box.mapViewToScene(QPointF(ra, dec))
    return view.plot.mapFromScene(scene)


def star_named(view: SkyView, name: str) -> SkyStar:
    return next(s for s in view.hosts if s.name == name)


# -- geometry ---------------------------------------------------------------------

def test_cvz_circles_are_twelve_degrees_from_the_ecliptic_poles():
    for ra0, dec0 in ECLIPTIC_POLES:
        ra, dec = small_circle(ra0, dec0, CVZ_RADIUS_DEG)
        assert np.allclose(separation_deg(ra0, dec0, ra, dec), CVZ_RADIUS_DEG, atol=1e-6)


@pytest.mark.parametrize("name", ["Kepler-10", "Kepler-7", "Kepler-2", "Kepler-22", "KIC 8006161"])
def test_kepler_targets_lie_inside_the_kepler_outline(name):
    star = next(s for s in skycatalog.offline_targets() if s.name == name)
    assert in_kepler_field(star.ra, star.dec)


@pytest.mark.parametrize("name", ["pi Men", "TOI-270", "WASP-18", "LHS 3844"])
def test_tess_targets_lie_outside_the_kepler_outline(name):
    star = next(s for s in skycatalog.offline_targets() if s.name == name)
    assert not in_kepler_field(star.ra, star.dec)


def test_ra_increases_to_the_left(sky):
    assert screen_pos(sky, 100.0, 0.0).x() < screen_pos(sky, 50.0, 0.0).x()
    assert screen_pos(sky, 0.0, 40.0).y() < screen_pos(sky, 0.0, -40.0).y()


# -- catalogs -----------------------------------------------------------------------

def test_load_hosts_in_background_and_report_retrieval(sky, qtbot):
    assert sky.status_text == "No catalog loaded"
    with qtbot.waitSignal(sky.catalogLoaded, timeout=10000) as blocker:
        sky.load_hosts()
        assert "Loading planet hosts" in sky.status_text
    catalog = blocker.args[0]
    assert not catalog.offline and len(sky.hosts) == 32
    assert sky.status_text == f"32 planet hosts · NASA Exoplanet Archive, retrieved {catalog.retrieved}"
    assert len(sky.host_item.data) == 32


def test_offline_catalog_says_so(qtbot, tmp_path):
    view = _make_sky(qtbot, CatalogClient(transport=_down, cache=ResponseCache(tmp_path)))
    with qtbot.waitSignal(view.catalogLoaded, timeout=10000):
        view.load_hosts()
    text = view.status_text
    assert text.startswith("Offline: 9 bundled targets (archive data of ")
    assert "connection refused" in text
    assert star_named(view, "KIC 8006161").kind == "null"
    view.close()


def test_unexpected_catalog_failure_is_logged_and_shown(sky, qtbot, monkeypatch, caplog):
    def broken(**kwargs):
        raise KeyError("ra")

    monkeypatch.setattr(skycatalog, "host_stars", broken)
    sky.load_hosts()
    qtbot.waitUntil(lambda: "unavailable" in sky.status_text, timeout=10000)
    assert sky.status_text == "Planet hosts unavailable: 'ra'"
    assert "loading planet hosts failed" in caplog.text


# -- clicking ---------------------------------------------------------------------------

def test_click_selects_the_nearest_star(sky, hosts, qtbot):
    sky.set_catalog(hosts)
    kepler10 = star_named(sky, "Kepler-10")
    sky.center_on(kepler10.ra, kepler10.dec, 20.0)
    qtbot.wait(10)
    pos = screen_pos(sky, kepler10.ra, kepler10.dec)
    pos.setX(pos.x() + 4)  # a few pixels off still counts
    with qtbot.waitSignal(sky.starClicked, timeout=2000) as blocker:
        qtbot.mouseClick(sky.plot.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    assert blocker.args[0] is kepler10
    assert sky.current is kepler10
    assert sky.current_item.data["x"][0] == pytest.approx(kepler10.ra)
    assert sky.coord_label.text() == "Kepler-10 (TIC 377780790), Tmag 10.5, 2 planets"


def test_click_on_empty_sky_selects_nothing(sky, hosts, qtbot):
    sky.set_catalog(hosts)
    sky.center_on(180.0, -10.0, 5.0)  # no recorded host here
    qtbot.wait(10)
    with qtbot.assertNotEmitted(sky.starClicked):
        qtbot.mouseClick(sky.plot.viewport(), Qt.MouseButton.LeftButton,
                         pos=screen_pos(sky, 180.0, -10.0))
    assert sky.current is None


def test_click_tolerance_is_in_pixels(sky, hosts):
    sky.set_catalog(hosts)
    toi270 = star_named(sky, "TOI-270")
    sky.center_on(toi270.ra, toi270.dec, 10.0)
    px, py = sky.view_box.viewPixelSize()
    assert sky.nearest_star(toi270.ra, toi270.dec + 8 * py) is toi270
    assert sky.nearest_star(toi270.ra, toi270.dec + 20 * py) is None
    assert sky.nearest_star(toi270.ra - 8 * px, toi270.dec) is toi270


def test_nearest_star_wraps_at_ra_zero(sky):
    star = SkyStar(name="Edge", ra=359.999, dec=0.0)
    sky.set_catalog(SkyCatalog(stars=[star]))
    sky.center_on(0.0, 0.0, 1.0)
    assert sky.nearest_star(0.001, 0.0) is star


def test_selected_targets_are_ringed_and_labelled(sky):
    targets = [StarTarget(name="A", ra=10.0, dec=5.0), StarTarget(name="No position")]
    sky.set_selected(targets)
    assert [t.name for t in sky.selected] == ["A"]
    assert list(sky.selected_item.data["x"]) == [10.0]
    assert [label.toPlainText() for label in sky._labels] == ["A"]
    sky.set_selected([])
    assert sky._labels == [] and len(sky.selected_item.data) == 0


def test_hover_shows_coordinates_and_star(sky, hosts):
    sky.set_catalog(hosts)
    kepler10 = star_named(sky, "Kepler-10")
    sky.center_on(kepler10.ra, kepler10.dec, 20.0)
    scene = sky.view_box.mapViewToScene(QPointF(kepler10.ra, kepler10.dec))
    sky._on_mouse_moved(scene)
    assert sky.coord_label.text().startswith("RA 285.679°  Dec +50.241°  —  Kepler-10")


# -- deep zoom ----------------------------------------------------------------------------

def _cone_body() -> str:
    recordings = json.loads(RECORDED.read_text(encoding="utf-8"))
    return next(e["body"] for e in recordings.values()
                if "Filtered.Tic.Position" in e["params"].get("request", ""))


class ConeTransport:
    """Serves the recorded Kepler-10 field for any cone request; counts them."""

    def __init__(self):
        self.cones = []
        self.body = _cone_body()

    def __call__(self, method, url, params):
        request = json.loads(params["request"])
        self.cones.append(request["params"])
        return self.body


@pytest.fixture
def cone_sky(qtbot, tmp_path, hosts, monkeypatch):
    monkeypatch.setattr(sky_view, "FIELD_DEBOUNCE_MS", 10)
    transport = ConeTransport()
    view = _make_sky(qtbot, CatalogClient(transport=transport, cache=ResponseCache(tmp_path / "c")))
    view.set_catalog(hosts)
    view.transport = transport
    yield view
    view.close()


def test_deep_zoom_loads_tic_field_stars(cone_sky, qtbot):
    kepler10 = star_named(cone_sky, "Kepler-10")
    with qtbot.waitSignal(cone_sky.fieldLoaded, timeout=10000) as blocker:
        cone_sky.center_on(kepler10.ra, kepler10.dec, 1.0)
    field = blocker.args[0]
    assert len(field) == 10  # 11 recorded TIC stars, less Kepler-10 itself (a host)
    assert all(s.kind == "field" for s in field)
    (cone,) = cone_sky.transport.cones
    assert cone["ra"] == pytest.approx(kepler10.ra, abs=1e-3)
    assert cone["filters"][0]["values"][0]["max"] == 13.0
    assert "10 TIC stars (Tmag ≤ 13)" in cone_sky.status_text
    # Field stars can be clicked too.
    star = field[0]
    assert cone_sky.click_at(star.ra, star.dec) is star


def test_small_pan_inside_the_loaded_field_does_not_reload(cone_sky, qtbot):
    kepler10 = star_named(cone_sky, "Kepler-10")
    with qtbot.waitSignal(cone_sky.fieldLoaded, timeout=10000):
        cone_sky.center_on(kepler10.ra, kepler10.dec, 1.0)
    cone_sky.center_on(kepler10.ra + 0.02, kepler10.dec + 0.02, 1.0)
    qtbot.wait(100)
    assert len(cone_sky.transport.cones) == 1


def test_zooming_out_clears_the_field(cone_sky, qtbot):
    kepler10 = star_named(cone_sky, "Kepler-10")
    with qtbot.waitSignal(cone_sky.fieldLoaded, timeout=10000):
        cone_sky.center_on(kepler10.ra, kepler10.dec, 1.0)
    with qtbot.waitSignal(cone_sky.fieldLoaded, timeout=10000) as blocker:
        cone_sky.center_on(kepler10.ra, kepler10.dec, 30.0)
    assert blocker.args[0] == [] and cone_sky.field_region is None
    assert "TIC stars" not in cone_sky.status_text


def test_field_failure_is_shown(qtbot, tmp_path, hosts):
    view = _make_sky(qtbot, CatalogClient(transport=_down, cache=ResponseCache(tmp_path)))
    view.set_catalog(hosts)
    view.load_field(285.68, 50.24, 0.5)
    qtbot.waitUntil(lambda: "TIC stars unavailable" in view.status_text, timeout=10000)
    assert "connection refused" in view.status_text
    view.close()


def test_stale_field_results_are_dropped(sky):
    sky._pending_field = (1.0, 2.0, 0.5)
    sky._field_done((9.0, 9.0, 0.5), [SkyStar(name="old", ra=9.0, dec=9.0, kind="field")])
    assert sky.field == [] and sky._pending_field == (1.0, 2.0, 0.5)


# -- screenshot ------------------------------------------------------------------------------

def test_sky_selection_shot(qtbot, tmp_path, client):
    from PySide6.QtWidgets import QApplication

    window = shot_sky_selection(QApplication.instance(), client=client)
    qtbot.addWidget(window)
    qtbot.waitExposed(window)
    assert [t.name for t in window.panel.targets()] == ["TOI-270", "pi Men", "Kepler-10"]
    assert window.sky.current.name == "Kepler-10"
    assert len(window.sky.selected) == 3
    path = shot(window, SHOT_NAME, tmp_path)
    assert path.stat().st_size > 10000
    window.close()
