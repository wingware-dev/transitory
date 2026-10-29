"""Results gallery driven headless with synthetic StarAnalysis objects."""

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QLabel

from harness import shot
from test_plots import make_analysis
from transitory.gui.gallery import MIN_FIGURE_WIDTH, GalleryCanvas, GalleryWidget
from transitory.models import StarAnalysis, StarTarget


def settle():
    """Run Qt's deferred show and layout passes (new sections lay out on the next events)."""
    for _ in range(3):
        QApplication.processEvents()


@pytest.fixture
def gallery(qtbot):
    widget = GalleryWidget()
    qtbot.addWidget(widget)
    widget.resize(900, 700)
    widget.show()
    qtbot.waitExposed(widget)
    return widget


def test_empty_gallery_shows_hint(gallery):
    assert gallery.findChild(QLabel, "galleryEmpty").isVisible()
    assert not gallery.save_all_button.isEnabled()
    gallery.add_analysis(StarAnalysis(target=StarTarget(name="Lost"), errors=["fetch: no data"]))
    assert not gallery.save_all_button.isEnabled()  # nothing to save yet
    gallery.add_analysis(make_analysis())
    assert gallery.save_all_button.isEnabled()


def test_sections_embed_each_stars_figures(gallery, qtbot):
    gallery.set_analyses([make_analysis(name="Star A"),
                          StarAnalysis(target=StarTarget(name="Star B"), errors=["fetch: no data"])])
    settle()
    a, b = gallery.sections()
    assert [c.objectName() for c in a.canvases] == [
        "figure-lightcurve", "figure-periodogram", "figure-planet-b", "figure-planet-c"]
    assert a.built and a.figure_keys == ["lightcurve", "periodogram", "planet-b", "planet-c"]
    assert b.figure_keys == [] and not b.save_button.isEnabled()  # no light curve: errors only
    assert b.findChild(QLabel, "starErrors").text() == "fetch: no data"
    badges = [w.text().strip() for w in a.findChildren(QLabel, "confidenceBadge")]
    assert badges == ["b: strong", "c: strong"]
    assert not gallery.findChild(QLabel, "galleryEmpty").isVisible()
    assert gallery.status.text() == "2 stars, 2 candidates"


def test_canvas_keeps_figure_layout_when_resized(gallery, qtbot):
    section = gallery.add_analysis(make_analysis())
    settle()
    canvas = section.canvases[0]
    width_in, height_in = canvas.figure.get_size_inches()
    assert (width_in, height_in) == pytest.approx((10.0, 6.0), rel=0.01)
    assert canvas.height() == pytest.approx(canvas.width() * 0.6, abs=1)
    gallery.resize(1300, 700)
    qtbot.waitUntil(lambda: canvas.width() > 1000)
    assert tuple(canvas.figure.get_size_inches()) == pytest.approx((10.0, 6.0), rel=0.01)
    assert canvas.height() == pytest.approx(canvas.width() * 0.6, abs=1)
    gallery.resize(300, 700)  # narrower than readable: scroll sideways instead
    qtbot.waitUntil(lambda: gallery.scroll.horizontalScrollBar().maximum() > 0)
    assert canvas.width() >= MIN_FIGURE_WIDTH  # the section header may need more


def _wheel(widget):
    pos = QPointF(widget.width() / 2, widget.height() / 2)
    return QWheelEvent(pos, widget.mapToGlobal(pos), QPoint(), QPoint(0, -120),
                       Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                       Qt.ScrollPhase.NoScrollPhase, False)


def test_wheel_over_figure_is_left_for_the_gallery(gallery):
    # Qt passes an ignored wheel event up to the scroll area, but only for real
    # (spontaneous) input, so a test can only check that the canvas declines it.
    gallery.set_analyses([make_analysis(name="Star A"), make_analysis(name="Star B")])
    settle()
    assert gallery.scroll.verticalScrollBar().maximum() > 0
    canvas = gallery.sections()[0].canvases[0]
    event = _wheel(canvas)
    QApplication.sendEvent(canvas, event)
    assert not event.isAccepted()
    # whereas the scroll area itself scrolls on it
    viewport = gallery.scroll.viewport()
    QApplication.sendEvent(viewport, _wheel(viewport))
    assert gallery.scroll.verticalScrollBar().value() > 0


def test_replace_star_in_place_and_scroll_to(gallery, qtbot):
    gallery.set_analyses([make_analysis(name=n) for n in ("Star A", "Star B", "Star C")])
    updated = make_analysis(name="Star B")
    updated.planets = updated.planets[:1]
    gallery.add_analysis(updated)
    assert [s.key for s in gallery.sections()] == ["Star A", "Star B", "Star C"]
    layout = gallery.sections_layout
    order = [layout.itemAt(i).widget() for i in range(layout.count())]
    assert order.index(gallery.section("Star B")) < order.index(gallery.section("Star C"))
    assert len(gallery.section("Star B").figure_keys) == 3
    settle()
    assert gallery.scroll_to("Star C")
    assert gallery.scroll.verticalScrollBar().value() > 0
    assert not gallery.scroll_to("Nope")


def built_map(gallery):
    return "".join("B" if s.built else "." for s in gallery.sections())


def test_figures_built_only_near_view(gallery, qtbot):
    gallery.set_analyses([make_analysis(name=f"Star {i}") for i in range(10)])
    settle()
    # each section is taller than the view: the one in view, and the next one prefetched
    assert built_map(gallery) == "BB........"
    unbuilt = gallery.section("Star 5")
    assert unbuilt.canvases == [] and [s.objectName() for s in unbuilt.slots] == [
        "slot-lightcurve", "slot-periodogram", "slot-planet-b", "slot-planet-c"]
    # placeholders are canvas-sized, so building never moves what's below
    assert unbuilt.height() == gallery.section("Star 0").height()
    y_after = gallery.section("Star 6").y()

    bar = gallery.scroll.verticalScrollBar()
    bar.setValue(unbuilt.y() + 50)  # as the user scrolling there
    qtbot.waitUntil(lambda: unbuilt.built)
    settle()
    assert gallery.section("Star 6").y() == y_after
    assert built_map(gallery) == "BB..BBB..."  # in view, plus the neighbour on each side


def test_scroll_to_builds_and_lands_on_star(gallery, qtbot):
    gallery.set_analyses([make_analysis(name=f"Star {i}") for i in range(8)])
    assert gallery.scroll_to("Star 6")  # straight after adding, before any layout pass
    settle()
    target = gallery.section("Star 6")
    assert target.built
    assert gallery.scroll.verticalScrollBar().value() == target.y()
    gallery.scroll.verticalScrollBar().setValue(0)  # the user scrolling away ends the pin
    settle()
    assert gallery.scroll.verticalScrollBar().value() == 0


def test_hidden_gallery_builds_nothing_until_shown(qtbot):
    widget = GalleryWidget()
    qtbot.addWidget(widget)
    widget.resize(900, 700)
    widget.set_analyses([make_analysis(name=f"Star {i}") for i in range(3)])
    settle()
    assert built_map(widget) == "..."
    widget.show()
    qtbot.waitUntil(lambda: built_map(widget).startswith("B"))


def test_collapsed_section_builds_when_expanded(gallery, qtbot):
    gallery.set_analyses([make_analysis(name=n) for n in ("Star A", "Star B", "Star C")])
    for section in gallery.sections():  # collapsed before any were built: all headers in view
        section.toggle.click()
    settle()
    assert built_map(gallery) == "..."
    c = gallery.section("Star C")
    c.toggle.click()
    qtbot.waitUntil(lambda: c.built)
    assert built_map(gallery) == "..B"


def test_collapse_hides_figures(gallery):
    section = gallery.add_analysis(make_analysis())
    settle()
    section.toggle.click()
    assert not section.body.isVisible()
    assert section.toggle.arrowType() == Qt.ArrowType.RightArrow
    section.toggle.click()
    assert section.body.isVisible()


def test_save_all_writes_every_stars_pngs(gallery, tmp_path, qtbot):
    gallery.set_analyses([make_analysis(name="Star A"), make_analysis(name="Star B"),
                          StarAnalysis(target=StarTarget(name="Star C"), errors=["fetch: no data"])])
    with qtbot.waitSignal(gallery.saved) as blocker:
        paths = gallery.save_all(tmp_path)
    assert blocker.args[0] == paths
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        f"star-{s}-{k}.png" for s in "ab" for k in ("lightcurve", "periodogram", "planet-b", "planet-c"))
    one = gallery.save_all(tmp_path / "one", [gallery.section("Star A").analysis])
    assert len(one) == 4 and all(p.parent == tmp_path / "one" for p in one)


def test_save_buttons_ask_for_folder(gallery, tmp_path, monkeypatch, qtbot):
    from transitory.gui import gallery as gallery_mod

    monkeypatch.setattr(gallery_mod.QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(tmp_path)))
    gallery.set_analyses([make_analysis(name="Star A"), make_analysis(name="Star B")])
    gallery.section("Star B").save_button.click()
    assert sorted(p.name for p in tmp_path.iterdir())[0] == "star-b-lightcurve.png"
    assert len(list(tmp_path.iterdir())) == 4
    qtbot.mouseClick(gallery.save_all_button, Qt.MouseButton.LeftButton)
    assert len(list(tmp_path.iterdir())) == 8
    assert gallery.status.text() == f"Saved 8 PNGs to {tmp_path}"


def test_gallery_screenshot(gallery, tmp_path, qtbot):
    gallery.set_analyses([make_analysis(name="Star A")])
    settle()
    image = shot(gallery, "gallery", tmp_path)
    assert image.stat().st_size > 10_000
    assert isinstance(gallery.sections()[0].canvases[0], GalleryCanvas)
