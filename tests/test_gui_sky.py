"""Sky view driven headless: catalog loading, clicks, highlights and deep-zoom TIC fields.

Catalog data are the recorded MAST / Exoplanet Archive responses; no network.
"""

import json
import re

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtWidgets import QApplication

from harness import shot
from sky_selection_host import (RECORDED, SHOT_NAME, RecordedHipsTransport, recorded_client,
                                shot_sky_selection)
from transitory.catalog import hips, skycatalog
from transitory.catalog.client import CatalogClient, CatalogError, ResponseCache
from transitory.catalog.hips import CutoutCache
from transitory.catalog.skycatalog import SkyCatalog, SkyStar
from transitory.gui import credits_dialog, sky_background, sky_view
from transitory.gui.sky_axes import (DEC_STEPS, RA_STEPS, dec_precision, format_dec, format_ra,
                                     ra_precision, tick_steps)
from transitory.gui.sky_view import (CVZ_RADIUS_DEG, ECLIPTIC_POLES, SkyView, in_kepler_field,
                                     separation_deg, small_circle)
from transitory.models import StarTarget

ZOOM_HINT = "  ·  Zoom in below 2° to see TIC stars (Tmag ≤ 13)"  # the status's field part, zoomed out


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
    assert sky.status_text == "No catalog loaded" + ZOOM_HINT
    with qtbot.waitSignal(sky.catalogLoaded, timeout=10000) as blocker:
        sky.load_hosts()
        assert "Loading planet hosts" in sky.status_text
    catalog = blocker.args[0]
    assert not catalog.offline and len(sky.hosts) == 32
    assert sky.status_text == (f"32 planet hosts · NASA Exoplanet Archive, retrieved {catalog.retrieved}"
                               + ZOOM_HINT)
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
    assert sky.status_text == "Planet hosts unavailable: 'ra'" + ZOOM_HINT
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
    assert sky.coord_label.text().startswith(
        "RA 19h02m43.0s  Dec +50°14′29″  (285.679°, +50.241°)\nKepler-10")


def test_hover_rings_the_star_under_the_pointer(sky, hosts, qtbot):
    sky.set_catalog(hosts)
    kepler10 = star_named(sky, "Kepler-10")
    sky.center_on(kepler10.ra, kepler10.dec, 20.0)
    qtbot.wait(10)
    viewport = sky.plot.viewport()
    geometry = (sky.plot.geometry(), sky.view_box.sceneBoundingRect())
    pos = screen_pos(sky, kepler10.ra, kepler10.dec)
    qtbot.mouseMove(viewport, pos + QPoint(3, 0))
    qtbot.waitUntil(lambda: sky.hovered is kepler10, timeout=2000)
    assert sky.hover_item.data["x"][0] == pytest.approx(kepler10.ra)
    assert sky.hover_item.zValue() > sky.selected_item.zValue()
    assert (sky.plot.geometry(), sky.view_box.sceneBoundingRect()) == geometry  # drawn in the plot
    qtbot.wait(40)  # pyqtgraph drops moves faster than its event rate
    qtbot.mouseMove(viewport, pos + QPoint(60, 60))  # empty sky
    qtbot.waitUntil(lambda: sky.hovered is None, timeout=2000)
    assert len(sky.hover_item.data) == 0


def test_hover_ring_goes_when_the_pointer_leaves_or_the_star_does(sky, hosts):
    sky.set_catalog(hosts)
    kepler10 = star_named(sky, "Kepler-10")
    sky.set_hovered(kepler10)
    QApplication.sendEvent(sky.plot.viewport(), QEvent(QEvent.Type.Leave))
    assert sky.hovered is None
    field_star = SkyStar(name="F", ra=kepler10.ra, dec=kepler10.dec + 0.1, tmag=10.0, kind="field")
    sky.set_field([field_star])
    sky.set_hovered(field_star)
    sky.clear_field()
    assert sky.hovered is None and len(sky.hover_item.data) == 0


def test_click_emits_star_toggled(sky, hosts, qtbot):
    sky.set_catalog(hosts)
    kepler10 = star_named(sky, "Kepler-10")
    sky.center_on(kepler10.ra, kepler10.dec, 20.0)
    with qtbot.waitSignal(sky.starToggled, timeout=2000) as blocker:
        sky.click_at(kepler10.ra, kepler10.dec)
    assert blocker.args[0] is kepler10
    assert not sky.is_listed(kepler10)
    sky.set_selected([kepler10.to_target()])
    assert sky.is_listed(kepler10)



# -- aspect and sexagesimal axes ------------------------------------------------------------

@pytest.mark.parametrize("ra, precision, decimals, text", [
    (290.6667, "s", 0, "19h22m40s"),
    (290.6667, "m", 0, "19h23m"),
    (290.6667, "h", 0, "19h"),
    (285.679, "s", 1, "19h02m43.0s"),
    (359.9999, "s", 0, "00h00m00s"),  # rounds up past 24h
    (-15.0, "h", 0, "23h"),
    (14.99999, "s", 1, "01h00m00.0s"),  # 59.99..s carries into the minute
])
def test_format_ra(ra, precision, decimals, text):
    assert format_ra(ra, precision, decimals) == text


@pytest.mark.parametrize("dec, precision, text", [
    (44.5, "s", "+44°30′00″"),
    (44.5, "m", "+44°30′"),
    (-51.9573937, "s", "-51°57′27″"),
    (-51.9573937, "d", "-52°"),
    (-0.0001, "s", "+00°00′00″"),  # no minus on a value that rounds to zero
    (-0.5, "m", "-00°30′"),
    (89.99999, "s", "+90°00′00″"),
    (5.25, "m", "+05°15′"),
])
def test_format_dec(dec, precision, text):
    assert format_dec(dec, precision) == text


def test_tick_steps_are_whole_units_and_label_only_what_the_step_needs():
    major, minor = tick_steps(360.0, 420, 90, RA_STEPS)
    assert (major, minor) == (90.0, 30.0)  # 6h, minors at 2h
    assert ra_precision(major) == "h"
    major, minor = tick_steps(20.0, 900, 90, RA_STEPS)
    assert (major, minor) == (2.5, 0.5) and ra_precision(major) == "m"  # 10m, minors at 2m
    major, minor = tick_steps(0.05, 900, 90, RA_STEPS)
    assert major == pytest.approx(2 / 240) and ra_precision(major) == "s"
    assert tick_steps(180.0, 700, 40, DEC_STEPS) == (15, 5)
    assert dec_precision(15) == "d" and dec_precision(0.5) == "m" and dec_precision(10 / 3600) == "s"
    assert tick_steps(1e6, 100, 90, RA_STEPS)[0] == RA_STEPS[-1]  # never coarser than 12h


def test_axes_are_labelled_in_hours_and_degrees(sky):
    sky.center_on(290.0, 44.0, 2.0)
    QApplication.processEvents()
    for axis, pattern in (("bottom", r"^\d\dh\d\dm(\d\ds)?$"), ("left", r"^[+-]\d\d°\d\d′$")):
        item = sky.plot.getAxis(axis)
        ticks = item.tickValues(*item.range, item.geometry().width() if axis == "bottom"
                                else item.geometry().height())
        major_spacing, values = ticks[0]
        labels = item.tickStrings(values, 1.0, major_spacing)
        assert len(labels) >= 2 and all(re.match(pattern, t) for t in labels), labels


def _screen_extent(view: SkyView, ra, dec) -> tuple[float, float]:
    points = [screen_pos(view, r, d) for r, d in zip(ra, dec)]
    xs, ys = [p.x() for p in points], [p.y() for p in points]
    return max(xs) - min(xs), max(ys) - min(ys)


@pytest.mark.parametrize("size", [(900, 600), (420, 800)])
def test_cvz_circle_is_round_on_screen_at_any_widget_shape(sky, size):
    sky.resize(*size)
    QApplication.processEvents()
    ra0, dec0 = ECLIPTIC_POLES[0]
    sky.center_on(ra0, dec0, 2.5 * CVZ_RADIUS_DEG)
    QApplication.processEvents()
    width, height = _screen_extent(sky, *small_circle(ra0, dec0, CVZ_RADIUS_DEG))
    assert min(width, height) > 150
    # Plain RA/Dec bulges a 12 deg circle at Dec 66.6 by about 5% across.
    assert width == pytest.approx(height, rel=0.07)


def test_aspect_follows_the_view_centre_as_it_pans(sky):
    sky.center_on(0.0, 0.0, 10.0)
    QApplication.processEvents()
    assert sky.view_box.state["aspectLocked"] == pytest.approx(1.0)
    sky.view_box.translateBy(y=60.0)
    QApplication.processEvents()
    ra, dec, width, height = sky.view_region()
    assert dec == pytest.approx(60.0)
    assert sky.view_box.state["aspectLocked"] == pytest.approx(0.5)
    # A small square at the centre is still square on screen.
    w, h = _screen_extent(sky, [ra - 0.5 / 0.5, ra + 0.5 / 0.5], [dec - 0.5, dec + 0.5])
    assert w == pytest.approx(h, rel=0.01)


def test_center_on_fits_the_square_in_the_shorter_side(sky):
    for size in ((900, 600), (420, 800)):
        sky.resize(*size)
        QApplication.processEvents()
        sky.center_on(100.0, -30.0, 5.0)
        QApplication.processEvents()
        ra, dec, width, height = sky.view_region()
        assert (ra, dec) == (pytest.approx(100.0), pytest.approx(-30.0))
        assert min(width, height) == pytest.approx(5.0)
        assert (width > height) == (size[0] > size[1])


# -- legend, marker sizes and labels ------------------------------------------------------

def test_legend_lists_every_marker_kind(sky):
    entries = dict(sky.legend.entries)
    assert list(entries) == ["Planet host", "No known planets", "TIC field star", "In list",
                             "Last clicked", "Size = brightness (Tmag)"]
    # Each swatch is drawn as its markers are.
    assert entries["Planet host"].brush.color() == sky._brushes["host"].color()
    assert entries["No known planets"].brush.color() == sky._brushes["null"].color()
    assert entries["TIC field star"].brush.color() == sky._brushes["field"].color()
    assert entries["In list"].pen.color() == sky.selected_item.opts["pen"].color()
    assert entries["In list"].symbol == sky.selected_item.opts["symbol"]
    assert entries["Last clicked"].pen.color() == sky.current_item.opts["pen"].color()
    assert entries["Last clicked"].symbol == sky.current_item.opts["symbol"]
    assert len(entries["Size = brightness (Tmag)"].sizes) > 1


def _widget_rect(view: SkyView, scene_rect):
    """A scene rectangle in ``view``'s own pixel coordinates."""
    rect = view.plot.mapFromScene(scene_rect).boundingRect()
    return rect.translated(view.plot.viewport().mapTo(view, rect.topLeft()) - rect.topLeft())


def _has_color(image, rect, color, tolerance=40) -> bool:
    for x in range(rect.left(), rect.right()):
        for y in range(rect.top(), rect.bottom()):
            c = image.pixelColor(x, y)
            if (abs(c.red() - color.red()) + abs(c.green() - color.green())
                    + abs(c.blue() - color.blue())) <= tolerance:
                return True
    return False


@pytest.mark.parametrize("size", [(900, 600), (420, 800)])
def test_legend_is_drawn_inside_the_plot_clear_of_axes_and_reset(sky, hosts, size):
    sky.resize(*size)
    sky.set_catalog(hosts)
    QApplication.processEvents()
    legend = sky.legend.sceneBoundingRect()
    assert sky.view_box.sceneBoundingRect().contains(legend)  # so off the axes
    rect = _widget_rect(sky, legend)
    assert not rect.intersects(sky.reset_button.geometry())
    image = sky.grab().toImage()
    for color in (sky_view.SELECTED_COLOR, sky_view.CURRENT_COLOR, sky_view.NULL_COLOR):
        assert _has_color(image, rect, color), color.name()


def test_click_on_the_legend_selects_nothing(sky, qtbot):
    sky.center_on(0.0, 0.0, 1.0)
    QApplication.processEvents()
    legend = sky.legend.sceneBoundingRect()
    point = sky.view_box.mapSceneToView(legend.center())
    sky.set_catalog(SkyCatalog(stars=[SkyStar(name="Under", ra=point.x(), dec=point.y())]))
    with qtbot.assertNotEmitted(sky.starClicked):
        qtbot.mouseClick(sky.plot.viewport(), Qt.MouseButton.LeftButton,
                         pos=sky.plot.mapFromScene(legend.center()))
    assert sky.current is None


@pytest.mark.parametrize("deg_per_px, scale", [
    (0.001, 1.0), (0.02, 1.0), (0.05, 0.7), (0.25, 0.5), (5.0, 0.5), (0.0, 1.0)])
def test_marker_scale(deg_per_px, scale):
    assert sky_view.marker_scale(deg_per_px) == pytest.approx(scale)


def test_markers_shrink_as_the_view_widens(sky, hosts):
    sky.set_catalog(hosts)
    sky.show_all_sky()
    QApplication.processEvents()
    assert sky.marker_scale == sky_view.MIN_MARKER_SCALE
    all_sky = np.array(sky.host_item.data["size"])
    np.testing.assert_allclose(all_sky, sky._host_sizes * sky_view.MIN_MARKER_SCALE)
    kepler10 = star_named(sky, "Kepler-10")
    sky.center_on(kepler10.ra, kepler10.dec, 5.0)
    QApplication.processEvents()
    assert sky.marker_scale == 1.0
    np.testing.assert_allclose(sky.host_item.data["size"], sky._host_sizes)
    # Field stars loaded while zoomed in take the scale too.
    sky.set_field([SkyStar(name="F", ra=kepler10.ra, dec=kepler10.dec + 0.1, tmag=10.0)])
    np.testing.assert_allclose(sky.field_item.data["size"], sky._field_sizes)


def test_host_markers_are_translucent(sky):
    assert 0 < sky._brushes["host"].color().alpha() < 255
    assert sky._brushes["null"].color().alpha() == 255


def test_outline_labels_sit_outside_their_outlines_under_the_list_labels(sky):
    sky.set_selected([StarTarget(name="Kepler-10", ra=285.679, dec=50.241)])
    assert [t.toPlainText() for t in sky.outline_labels] == ["Kepler field", "TESS CVZ", "TESS CVZ"]
    for text in sky.outline_labels:
        assert text.zValue() < sky._labels[0].zValue()
    kepler = sky.outline_labels[0]
    _, dec = sky_view.kepler_outline()
    assert kepler.pos().y() == pytest.approx(dec.min())  # under the field, away from Kepler-10
    assert kepler.anchor.y() < 0  # hung below that point
    for text, (ra0, dec0) in zip(sky.outline_labels[1:], ECLIPTIC_POLES):
        assert text.pos().y() == pytest.approx(dec0 + CVZ_RADIUS_DEG, abs=0.01)
        assert text.anchor.y() > 1


# -- sky image ------------------------------------------------------------------------------

@pytest.fixture
def image_sky(qtbot, tmp_path, monkeypatch):
    """A sky view whose survey images are the recorded cutout; ``.hips`` counts the requests."""
    monkeypatch.setattr(sky_background, "BACKGROUND_DEBOUNCE_MS", 10)
    transport = RecordedHipsTransport()
    view = SkyView(client=recorded_client(tmp_path / "catalog"), field_zoom_deg=0.0,
                   hips_transport=transport, hips_cache=CutoutCache(tmp_path / "hips"))
    qtbot.addWidget(view)
    view.resize(900, 600)
    view.show()
    qtbot.waitExposed(view)
    view.hips = transport
    yield view
    view.close()


def _pixel(view: SkyView, x_frac: float, y_frac: float):
    """The colour drawn at a fraction of the way across and down the plot area."""
    rect = view.view_box.sceneBoundingRect()
    scene = QPointF(rect.left() + x_frac * rect.width(), rect.top() + y_frac * rect.height())
    point = view.plot.viewport().mapTo(view, view.plot.mapFromScene(scene))
    return view.grab().toImage().pixelColor(point)


def _choose(view: SkyView, text: str) -> None:
    next(a for a in view.background_menu.actions() if a.text() == text).trigger()


def test_background_chooser_is_in_the_footer_and_on_with_dss2(sky):
    menu = [(a.text(), a.isChecked()) for a in sky.background_menu.actions()]
    assert menu == [("Off", False), ("DSS2", True), ("2MASS", False), ("PanSTARRS", False)]
    button = sky.background_button
    assert button.text() == "DSS2" and sky.background.survey == "dss2"
    # Under Reset view, beside the position line: the footer stays four label lines tall.
    assert button.geometry().top() >= sky.reset_button.geometry().bottom()
    assert button.geometry().bottom() <= sky.coord_label.geometry().bottom()
    assert sky.height() - sky.plot.height() == (sky.status_label.height() + sky.coord_label.height()
                                                + 2 * sky.layout().spacing())
    _choose(sky, "PanSTARRS")
    assert button.text() == "PS1" and sky.background.survey == "panstarrs"
    assert button.width() == sky.reset_button.width() == sky.reset_button.sizeHint().width()


def test_no_sky_image_zoomed_out(image_sky, qtbot):
    image_sky.center_on(290.0, 44.0, 12.0)  # shorter side over BACKGROUND_ZOOM_DEG
    qtbot.wait(100)
    assert image_sky.hips.calls == [] and not image_sky.background.item.isVisible()


def test_zoomed_in_the_survey_image_shows_north_up_east_left(image_sky, qtbot):
    with qtbot.waitSignal(image_sky.background.loaded, timeout=10000) as blocker:
        image_sky.center_on(285.68, 50.24, 0.25)
    survey, (ra0, ra1), (dec0, dec1), _w, _h = blocker.args[0]
    assert survey == "dss2"
    (url, params), = image_sky.hips.calls
    assert params["hips"] == "CDS/P/DSS2/color"
    (x0, x1), (y0, y1) = image_sky.view_box.viewRange()
    assert ra0 < min(x0, x1) and max(x0, x1) < ra1 and dec0 < y0 and y1 < dec1  # view and margin
    item = image_sky.background.item
    assert item.isVisible() and item.zValue() < image_sky.field_item.zValue()
    # The recorded stand-in is red at its top row (north) and green at its left (high RA).
    top, bottom = _pixel(image_sky, 0.5, 0.05), _pixel(image_sky, 0.5, 0.95)
    left, right = _pixel(image_sky, 0.05, 0.5), _pixel(image_sky, 0.95, 0.5)
    assert top.red() > bottom.red() + 60
    assert left.green() > right.green() + 60


def test_small_pans_keep_the_image_and_zooming_out_drops_it(image_sky, qtbot):
    with qtbot.waitSignal(image_sky.background.loaded, timeout=10000):
        image_sky.center_on(285.68, 50.24, 1.0)
    image_sky.center_on(285.7, 50.26, 1.0)  # inside the fetched margin
    qtbot.wait(100)
    assert len(image_sky.hips.calls) == 1
    with qtbot.waitSignal(image_sky.background.loaded, timeout=10000):
        image_sky.center_on(285.68, 50.24, 0.3)  # zoomed in: the image is too coarse
    assert len(image_sky.hips.calls) == 2
    image_sky.center_on(285.68, 50.24, 30.0)
    qtbot.waitUntil(lambda: not image_sky.background.item.isVisible(), timeout=10000)


def test_choosing_off_or_another_survey(image_sky, qtbot):
    with qtbot.waitSignal(image_sky.background.loaded, timeout=10000):
        image_sky.center_on(285.68, 50.24, 1.0)
    _choose(image_sky, "Off")
    assert image_sky.background.survey is None and not image_sky.background.item.isVisible()
    with qtbot.waitSignal(image_sky.background.loaded, timeout=10000):
        _choose(image_sky, "2MASS")
    assert image_sky.hips.calls[-1][1]["hips"] == "CDS/P/2MASS/color"


def test_panstarrs_south_of_its_coverage_says_so(image_sky, qtbot):
    image_sky.set_background("panstarrs")
    image_sky.center_on(90.0, -66.0, 1.0)
    qtbot.waitUntil(lambda: "no images south of Dec -30°" in image_sky.status_text, timeout=10000)
    assert image_sky.hips.calls == []


def test_sky_image_failure_is_shown(image_sky, qtbot):
    def down(url, params):
        raise CatalogError("hips2fits is down")

    image_sky.background.transport = down
    image_sky.center_on(285.68, 50.24, 1.0)
    qtbot.waitUntil(lambda: "sky image unavailable" in image_sky.status_text, timeout=10000)
    assert "hips2fits is down" in image_sky.status_text
    assert not image_sky.background.item.isVisible()


def test_stale_sky_images_are_dropped(sky):
    background = sky.background
    background._pending = ("dss2", (1.0, 2.0), (3.0, 4.0), 10, 10)
    background._done(("dss2", (9.0, 9.5), (9.0, 9.5), 10, 10), np.zeros((10, 10, 3), np.uint8))
    assert background.shown is None and not background.item.isVisible()
    assert background._pending == ("dss2", (1.0, 2.0), (3.0, 4.0), 10, 10)


def test_star_markers_are_outlined_for_contrast_over_the_image(sky):
    for item in (sky.host_item, sky.field_item):
        pen = item.opts["pen"]
        assert pen.style() != Qt.PenStyle.NoPen and pen.color().lightness() < 40


def test_credits_name_the_sky_image_services():
    lines = [line for _, general in credits_dialog.general_credits() for line in general]
    assert hips.HIPS2FITS_CREDIT in lines
    assert all(hips.SURVEY_CREDITS[key] in lines for key in hips.SURVEYS)


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


def test_a_wide_view_loads_the_field_when_its_shorter_side_is_zoomed_in(cone_sky, qtbot):
    cone_sky.resize(1400, 400)
    kepler10 = star_named(cone_sky, "Kepler-10")
    with qtbot.waitSignal(cone_sky.fieldLoaded, timeout=10000):
        cone_sky.center_on(kepler10.ra, kepler10.dec, 1.9)
    _ra, _dec, width, height = cone_sky.view_region()
    assert width > 2 * height > 2 * 1.8  # far wider than FIELD_ZOOM_DEG
    (cone,) = cone_sky.transport.cones
    assert cone["radius"] == 1.0  # capped at MAX_CONE_RADIUS_DEG


def test_zoomed_out_status_says_how_far_to_zoom(sky):
    assert sky.status_text.endswith(ZOOM_HINT)
    sky.field_zoom_deg = 0.0  # field stars off, as in demo mode: no hint, at once
    assert "TIC stars" not in sky.status_text


def test_small_pan_inside_the_loaded_field_does_not_reload(cone_sky, qtbot):
    kepler10 = star_named(cone_sky, "Kepler-10")
    with qtbot.waitSignal(cone_sky.fieldLoaded, timeout=10000):
        cone_sky.center_on(kepler10.ra, kepler10.dec, 0.8)  # 1.35 x 0.8 deg: a 0.98 deg cone
    cone_sky.center_on(kepler10.ra + 0.02, kepler10.dec + 0.02, 0.8)
    qtbot.wait(100)
    assert len(cone_sky.transport.cones) == 1


def test_zooming_out_clears_the_field(cone_sky, qtbot):
    kepler10 = star_named(cone_sky, "Kepler-10")
    with qtbot.waitSignal(cone_sky.fieldLoaded, timeout=10000):
        cone_sky.center_on(kepler10.ra, kepler10.dec, 1.0)
    with qtbot.waitSignal(cone_sky.fieldLoaded, timeout=10000) as blocker:
        cone_sky.center_on(kepler10.ra, kepler10.dec, 30.0)
    assert blocker.args[0] == [] and cone_sky.field_region is None
    assert "10 TIC stars" not in cone_sky.status_text
    assert "Zoom in below 2° to see TIC stars (Tmag ≤ 13)" in cone_sky.status_text


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
