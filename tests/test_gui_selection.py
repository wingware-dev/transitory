"""Selection panel driven headless: typing names, the list, mission preference, submit.

Names resolve against the recorded MAST / Exoplanet Archive responses; no network.
"""

import threading

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication

from sky_selection_host import (click, drag, make_sky_selection_window, pinch, recorded_client,
                                sky_center, sky_pixel, trackpad_scroll, wheel)
from transitory.catalog import skycatalog
from transitory.catalog.client import CatalogClient, CatalogError, ResponseCache
from transitory.gui.main_window import TARGET_ZOOM_DEG
from transitory.gui.selection_panel import SelectionPanel
from transitory.models import StarTarget


def _make_panel(qtbot, **kwargs):
    panel = SelectionPanel(**kwargs)
    qtbot.addWidget(panel)
    panel.resize(380, 600)
    panel.show()
    qtbot.waitExposed(panel)
    return panel


@pytest.fixture
def client(tmp_path):
    return recorded_client(tmp_path / "catalog")


@pytest.fixture
def panel(qtbot, client):
    widget = _make_panel(qtbot, client=client)
    yield widget
    widget.close()


def type_and_enter(qtbot, panel, text):
    qtbot.keyClicks(panel.entry, text)
    qtbot.keyClick(panel.entry, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: not panel.resolving and panel.entry.isEnabled(), timeout=10000)


def item_texts(panel):
    return [panel.list.item(i).text() for i in range(panel.list.count())]


def test_starts_empty_with_submit_disabled(panel):
    assert panel.targets() == []
    assert panel.count_label.text() == "0 stars selected"
    assert not panel.submit_button.isEnabled()
    assert not panel.remove_button.isEnabled() and not panel.clear_button.isEnabled()


def test_typing_a_planet_name_adds_its_host(panel, qtbot):
    with qtbot.waitSignal(panel.targetsChanged, timeout=10000) as blocker:
        type_and_enter(qtbot, panel, "Kepler-10 b")
    (target,) = blocker.args[0]
    assert (target.name, target.tic, target.kic) == ("Kepler-10", 377780790, 11904151)
    assert item_texts(panel) == ["Kepler-10 (TIC 377780790) — Tmag 10.5"]
    assert panel.message.text() == "Added Kepler-10."
    assert panel.entry.text() == "" and panel.submit_button.isEnabled()
    assert panel.count_label.text() == "1 star selected"


def test_add_button_resolves_a_list_and_reports_bad_names(panel, qtbot):
    qtbot.keyClicks(panel.entry, "TOI-270.01, nosuchstarxyz; KIC 8006161")
    qtbot.mouseClick(panel.add_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: panel.entry.isEnabled(), timeout=10000)
    assert [t.name for t in panel.targets()] == ["TOI-270", "KIC 8006161"]
    assert item_texts(panel)[0] == "TOI-270 (TIC 259377017) — Tmag 10.5, TESS"
    text = panel.message.text()
    assert text.startswith("Added TOI-270, KIC 8006161.")
    assert "Not found: nosuchstarxyz:" in text
    assert panel.message.property("error") is True
    assert panel.entry.text() != ""  # kept so the typo can be fixed


def test_the_same_star_is_listed_once(panel, qtbot):
    type_and_enter(qtbot, panel, "Kepler-10 b")
    type_and_enter(qtbot, panel, "KOI-72")  # same TIC as Kepler-10
    assert len(panel.targets()) == 1
    assert panel.message.text() == "Already listed: KOI-72."


def test_remove_by_button_and_delete_key(panel, qtbot):
    for name in ("A", "B", "C"):
        panel.add_target(StarTarget(name=name, ra=1.0, dec=2.0))
    panel.list.setCurrentRow(1)
    assert panel.remove_button.isEnabled()
    with qtbot.waitSignal(panel.targetsChanged) as blocker:
        qtbot.mouseClick(panel.remove_button, Qt.MouseButton.LeftButton)
    assert [t.name for t in blocker.args[0]] == ["A", "C"]
    panel.list.setFocus()
    panel.list.setCurrentRow(0)
    qtbot.keyClick(panel.list, Qt.Key.Key_Delete)
    assert [t.name for t in panel.targets()] == ["C"]
    assert item_texts(panel) == ["C"]


def test_remove_several_and_clear(panel, qtbot):
    for name in ("A", "B", "C", "D"):
        panel.add_target(StarTarget(name=name))
    panel.list.item(0).setSelected(True)
    panel.list.item(2).setSelected(True)
    panel.remove_selected()
    assert [t.name for t in panel.targets()] == ["B", "D"]
    qtbot.mouseClick(panel.clear_button, Qt.MouseButton.LeftButton)
    assert panel.targets() == [] and panel.list.count() == 0
    assert not panel.submit_button.isEnabled()


def test_submit_applies_the_mission_preference(panel, qtbot):
    panel.add_target(StarTarget(name="A", tic=1, mission_hint="TESS"))
    panel.add_target(StarTarget(name="B", tic=2))
    with qtbot.waitSignal(panel.submitted) as blocker:
        qtbot.mouseClick(panel.submit_button, Qt.MouseButton.LeftButton)
    assert [t.mission_hint for t in blocker.args[0]] == ["TESS", "any"]  # Auto: each star's own

    panel.mission.setCurrentIndex(panel.mission.findData("Kepler"))
    assert panel.mission_preference == "Kepler"
    with qtbot.waitSignal(panel.submitted) as blocker:
        qtbot.mouseClick(panel.submit_button, Qt.MouseButton.LeftButton)
    assert [t.mission_hint for t in blocker.args[0]] == ["Kepler", "Kepler"]
    assert [t.mission_hint for t in panel.targets()] == ["TESS", "any"]  # the list is unchanged


def test_submit_with_nothing_selected_emits_nothing(panel, qtbot):
    with qtbot.assertNotEmitted(panel.submitted):
        assert panel.submit() == []


def test_unreachable_catalog_is_reported(qtbot, tmp_path):
    def down(method, url, params):
        raise CatalogError("connection refused")

    panel = _make_panel(qtbot, client=CatalogClient(transport=down, cache=ResponseCache(tmp_path)))
    type_and_enter(qtbot, panel, "Kepler-10")
    assert panel.message.text().startswith("Catalog service unavailable:")
    assert "connection refused" in panel.message.text()
    assert panel.targets() == [] and panel.entry.text() == "Kepler-10"
    panel.close()


def test_unexpected_resolver_failure_is_logged_and_shown(qtbot, caplog):
    def broken(text):
        raise ZeroDivisionError("oops")

    panel = _make_panel(qtbot, resolver=broken)
    type_and_enter(qtbot, panel, "anything")
    assert panel.message.text() == "Lookup failed: oops"
    assert "resolving star names failed" in caplog.text
    panel.close()


def test_entry_is_disabled_while_resolving(qtbot):
    release = threading.Event()

    def slow(text):
        release.wait(10)
        return [StarTarget(name=text, tic=5)], []

    panel = _make_panel(qtbot, resolver=slow)
    qtbot.keyClicks(panel.entry, "Slow star")
    qtbot.keyClick(panel.entry, Qt.Key.Key_Return)
    assert panel.resolving and not panel.entry.isEnabled() and not panel.add_button.isEnabled()
    assert panel.message.text() == "Looking up Slow star…"
    panel.add_from_entry()  # ignored while busy
    release.set()
    qtbot.waitUntil(lambda: panel.entry.isEnabled(), timeout=10000)
    assert [t.name for t in panel.targets()] == ["Slow star"]
    panel.close()


def test_empty_entry_does_nothing(panel):
    panel.add_from_entry()
    assert not panel.resolving and panel.message.text() == ""


# -- wired to the sky view ------------------------------------------------------------------

@pytest.fixture
def window(qtbot, client):
    win = make_sky_selection_window(client)
    qtbot.addWidget(win)
    qtbot.waitExposed(win)
    win.sky.set_catalog(skycatalog.host_stars(client=client, offline_fallback=False))
    yield win
    win.close()


def test_clicking_a_star_on_the_sky_adds_it(window, qtbot):
    sky, panel = window.sky, window.panel
    toi270 = next(s for s in sky.hosts if s.name == "TOI-270")
    with qtbot.waitSignal(panel.targetsChanged):
        sky.click_at(toi270.ra, toi270.dec)
    (target,) = panel.targets()
    assert (target.name, target.tic, target.mission_hint) == ("TOI-270", 259377017, "TESS")
    assert panel.message.text() == "Added TOI-270."
    assert [t.name for t in sky.selected] == ["TOI-270"]  # ringed on the sky

def test_clicking_a_listed_star_on_the_sky_removes_it(window, qtbot):
    sky, panel = window.sky, window.panel
    toi270 = next(s for s in sky.hosts if s.name == "TOI-270")
    panel.add_target(StarTarget(name="Elsewhere", ra=10.0, dec=10.0))
    sky.click_at(toi270.ra, toi270.dec)
    assert sky.is_listed(toi270)
    with qtbot.waitSignal(panel.targetsChanged):
        sky.click_at(toi270.ra, toi270.dec)
    assert [t.name for t in panel.targets()] == ["Elsewhere"]
    assert [panel.list.item(i).text() for i in range(panel.list.count())] == ["Elsewhere"]
    assert panel.message.text() == "Removed TOI-270."
    assert not sky.is_listed(toi270) and [t.name for t in sky.selected] == ["Elsewhere"]
    sky.click_at(toi270.ra, toi270.dec)  # and a third click adds it back
    assert [t.name for t in panel.targets()] == ["Elsewhere", "TOI-270"]


def test_remove_target_matches_by_tic_or_name(window):
    panel = window.panel
    panel.add_target(StarTarget(name="TOI-270", tic=259377017))
    panel.add_target(StarTarget(name="Somewhere", ra=1.0, dec=2.0))
    assert panel.remove_target(StarTarget(name="TIC 259377017", tic=259377017))
    assert not panel.remove_target(StarTarget(name="TOI-270", tic=259377017))  # gone already
    assert panel.remove_target(StarTarget(name="somewhere"))
    assert panel.targets() == [] and panel.list.count() == 0


def _menu_actions(menu):
    return {a.text(): a for a in menu.actions() if not a.isSeparator()}


def test_right_click_menu_adds_removes_centres_and_copies(window, qtbot):
    sky, panel = window.sky, window.panel
    toi270 = next(s for s in sky.hosts if s.name == "TOI-270")
    sky.center_on(toi270.ra + 3, toi270.dec, 20.0)
    QApplication.processEvents()
    click(sky, sky_pixel(sky, toi270.ra, toi270.dec), Qt.MouseButton.RightButton)
    qtbot.waitUntil(lambda: sky.menu is not None and sky.menu.isVisible(), timeout=2000)
    actions = _menu_actions(sky.menu)
    assert list(actions) == ["Add TOI-270 to the list", "Centre here", "Copy name", "Copy TIC ID"]
    assert panel.targets() == []  # a right-click adds nothing by itself
    sky.menu.close()

    actions["Add TOI-270 to the list"].trigger()
    assert [t.name for t in panel.targets()] == ["TOI-270"]
    actions = _menu_actions(sky.context_menu(toi270.ra, toi270.dec))
    assert "Remove TOI-270 from the list" in actions

    actions["Centre here"].trigger()
    ra, dec, _width, height = sky.view_region()
    assert (ra, dec) == (pytest.approx(toi270.ra), pytest.approx(toi270.dec))
    assert height == pytest.approx(20.0, rel=0.01)  # centring keeps the zoom

    actions["Copy name"].trigger()
    assert QApplication.clipboard().text() == "TOI-270"
    actions["Copy TIC ID"].trigger()
    assert QApplication.clipboard().text() == "TIC 259377017"

    actions["Remove TOI-270 from the list"].trigger()
    assert panel.targets() == []


def test_right_click_on_empty_sky_offers_only_centre_here(window):
    sky = window.sky
    sky.center_on(180.0, -10.0, 5.0)  # no recorded host here
    menu = sky.context_menu(181.0, -9.0)
    assert list(_menu_actions(menu)) == ["Centre here"]
    menu.actions()[0].trigger()
    ra, dec, _width, height = sky.view_region()
    assert (ra, dec) == (pytest.approx(181.0), pytest.approx(-9.0))
    assert height == pytest.approx(5.0, rel=0.01)  # the aspect re-locks for the new Dec


def test_typed_stars_are_ringed_on_the_sky(window, qtbot):
    type_and_enter(qtbot, window.panel, "pi Men c")
    assert [t.name for t in window.sky.selected] == ["pi Men"]
    window.panel.clear()
    assert window.sky.selected == []


def test_double_clicking_a_listed_star_centres_the_sky(window, qtbot):
    panel, sky = window.panel, window.sky
    panel.add_target(StarTarget(name="Somewhere", ra=120.0, dec=-30.0))
    item = panel.list.item(0)
    rect = panel.list.visualItemRect(item)
    # Item views emit doubleClicked only for the item the first click pressed.
    qtbot.mouseClick(panel.list.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    qtbot.mouseDClick(panel.list.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    ra, dec, width, height = sky.view_region()
    assert (ra, dec) == (pytest.approx(120.0), pytest.approx(-30.0))
    assert min(width, height) == pytest.approx(TARGET_ZOOM_DEG)


def test_show_on_sky_centres_the_selected_star(window, qtbot):
    panel, sky = window.panel, window.sky
    panel.add_target(StarTarget(name="Somewhere", ra=120.0, dec=-30.0))
    panel.add_target(StarTarget(name="Elsewhere", ra=200.0, dec=15.0))
    panel.add_target(StarTarget(name="Nowhere"))  # no position
    assert not panel.show_button.isEnabled()  # nothing selected
    panel.list.setCurrentRow(1)
    assert panel.show_button.isEnabled()
    qtbot.mouseClick(panel.show_button, Qt.MouseButton.LeftButton)
    ra, dec, width, height = sky.view_region()
    assert (ra, dec) == (pytest.approx(200.0), pytest.approx(15.0))
    assert min(width, height) == pytest.approx(TARGET_ZOOM_DEG)
    panel.list.selectAll()  # several rows: which to show is unclear
    assert not panel.show_button.isEnabled()
    panel.list.setCurrentRow(2)
    assert not panel.show_button.isEnabled()


def test_typing_one_star_centres_the_sky_on_it(window, qtbot):
    panel, sky = window.panel, window.sky
    with qtbot.waitSignal(panel.targetActivated, timeout=10000) as blocker:
        type_and_enter(qtbot, panel, "pi Men c")
    target = blocker.args[0]
    assert target.name == "pi Men"
    ra, dec, width, height = sky.view_region()
    assert (ra, dec) == (pytest.approx(target.ra), pytest.approx(target.dec))
    assert min(width, height) == pytest.approx(TARGET_ZOOM_DEG)


def test_typing_several_stars_or_a_listed_one_leaves_the_sky(panel, qtbot):
    with qtbot.assertNotEmitted(panel.targetActivated):
        type_and_enter(qtbot, panel, "TOI-270.01, KIC 8006161")
    assert len(panel.targets()) == 2
    with qtbot.assertNotEmitted(panel.targetActivated):
        type_and_enter(qtbot, panel, "TOI-270")  # already listed
    with qtbot.waitSignal(panel.targetActivated, timeout=10000):
        type_and_enter(qtbot, panel, "Kepler-10 b, KOI-72")  # one new star, one duplicate of it


# -- zooming and panning the sky ------------------------------------------------------------

def _centred_on_toi270(sky, size_deg=20.0):
    toi270 = next(s for s in sky.hosts if s.name == "TOI-270")
    sky.center_on(toi270.ra, toi270.dec, size_deg)
    QApplication.processEvents()  # pyqtgraph maps the pointer through the range last painted
    return toi270


def test_mouse_wheel_zooms_about_the_pointer(window):
    sky = window.sky
    toi270 = _centred_on_toi270(sky)
    wheel(sky, sky_center(sky), angle=3 * 120)
    ra, dec, width, height = sky.view_region()
    assert height < 15.0
    assert (ra, dec) == (pytest.approx(toi270.ra, abs=0.5), pytest.approx(toi270.dec, abs=0.5))
    wheel(sky, sky_center(sky), angle=-3 * 120)
    assert sky.view_region()[3] == pytest.approx(20.0, rel=0.02)


def test_dragging_pans_and_a_click_still_selects(window):
    sky, panel = window.sky, window.panel
    toi270 = _centred_on_toi270(sky)
    start = sky_center(sky)
    drag(sky, start, start + QPoint(120, 80))
    ra, dec, width, height = sky.view_region()
    # The sky follows the mouse: right is higher RA, down shows higher Dec.
    assert ra > toi270.ra + 1 and dec > toi270.dec + 1
    assert height == pytest.approx(20.0)
    assert panel.targets() == []  # a drag selects nothing
    click(sky, sky_pixel(sky, toi270.ra, toi270.dec))
    assert [t.name for t in panel.targets()] == ["TOI-270"]


def test_trackpad_scroll_pans_and_pinch_or_ctrl_scroll_zooms(window):
    sky = window.sky
    toi270 = _centred_on_toi270(sky)
    trackpad_scroll(sky, sky_center(sky), 120, 80)
    ra, dec, width, height = sky.view_region()
    assert ra > toi270.ra + 1 and dec > toi270.dec + 1  # as the same drag would
    assert height == pytest.approx(20.0)
    pinch(sky, sky_center(sky), 0.25)
    assert sky.view_region()[3] == pytest.approx(16.0)
    pinch(sky, sky_center(sky), -0.2)
    assert sky.view_region()[3] == pytest.approx(20.0)
    trackpad_scroll(sky, sky_center(sky), 0, 120, modifiers=Qt.KeyboardModifier.ControlModifier)
    assert sky.view_region()[3] < 19.0


def test_reset_view_button_and_home_key(window, qtbot):
    sky = window.sky
    _centred_on_toi270(sky)
    qtbot.mouseClick(sky.reset_button, Qt.MouseButton.LeftButton)
    ra, dec, width, height = sky.view_region()
    assert (ra, dec) == (pytest.approx(180.0), pytest.approx(0.0)) and height >= 180  # pole to pole
    sky.set_home(290.0, 44.0, 10.0)
    _centred_on_toi270(sky)
    window.activateWindow()
    sky.plot.setFocus()
    qtbot.keyClick(sky.plot, Qt.Key.Key_Home)
    ra, dec, width, height = sky.view_region()
    assert (ra, dec, height) == (pytest.approx(290.0), pytest.approx(44.0), pytest.approx(10.0))
