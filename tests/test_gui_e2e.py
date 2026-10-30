"""End to end: the dashboard in demo mode, and the analysis runner behind Submit."""

import threading
import time

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from dashboard_host import click_star, hover_star, select_and_submit, type_stars, wait_for_results, wait_until
from harness import shot
from sky_selection_host import recorded_client
from transitory.catalog.client import CatalogError
from transitory.gui.credits_dialog import credits_text
from transitory.gui.main_window import (
    DEFAULT_MAX_PRODUCTS,
    MainWindow,
    demo_resolver,
    make_source,
    parse_args,
)
from transitory.gui.workers import AnalysisRunner, ProgressPanel
from transitory.models import StarAnalysis, StarTarget
from transitory.pipeline import AnalysisCancelled
from transitory.sources import SYNTHETIC_CREDIT, SyntheticSource


def test_select_two_stars_submit_and_see_results(main_window, qtbot, tmp_path):
    window = main_window
    select_and_submit(window, ["Demo Hot Jupiter", "Demo Two-Planet"])
    assert [t.name for t in window.sky.selected] == ["Demo Hot Jupiter", "Demo Two-Planet"]
    assert window.runner.busy
    wait_for_results(window)

    rows = window.table.rows()
    assert [(r.star_key, r.planet) for r in rows] == [
        ("Demo Hot Jupiter", "b"), ("Demo Two-Planet", "b"), ("Demo Two-Planet", "c")]
    assert all(r.confidence == "strong" for r in rows)
    periods = sorted(r.period for r in rows if r.star_key == "Demo Two-Planet")
    assert periods == [pytest.approx(2.43, rel=0.01), pytest.approx(9.87, rel=0.01)]

    sections = {s.key: s for s in window.gallery.sections()}
    assert set(sections) == {"Demo Hot Jupiter", "Demo Two-Planet"}
    # figures are built when a section scrolls into view; keys are known before that
    assert sections["Demo Hot Jupiter"].figure_keys == ["lightcurve", "periodogram", "planet-b"]
    assert len(sections["Demo Two-Planet"].figure_keys) == 4

    assert window.progress.status("Demo Hot Jupiter") == "Done — 1 candidate (1 strong)"
    assert window.progress.status("Demo Two-Planet") == "Done — 2 candidates (2 strong)"
    assert window.progress.bar.value() == window.progress.bar.maximum()
    assert not window.progress.cancel_button.isEnabled()
    shot(window, "e2e", tmp_path)


def test_table_row_opens_star_in_gallery(main_window, qtbot):
    window = main_window
    select_and_submit(window, ["Demo Hot Jupiter", "Demo Super-Earth"])
    wait_for_results(window)
    window.tabs.setCurrentWidget(window.table)
    row = next(r for r in range(window.table.proxy.rowCount())
               if window.table.proxy.index(r, 0).data() == "Demo Super-Earth")
    index = window.table.proxy.index(row, 0)
    window.table.view.doubleClicked.emit(index)
    assert window.tabs.currentWidget() is window.gallery
    section = window.gallery.section("Demo Super-Earth")
    qtbot.waitUntil(lambda: window.gallery.scroll.verticalScrollBar().value() == min(
        section.y(), window.gallery.scroll.verticalScrollBar().maximum()))


def test_typed_names_resolve_against_demo_stars(main_window):
    type_stars(main_window, "hot jupiter, Kepler-10")
    assert [t.name for t in main_window.panel.targets()] == ["Demo Hot Jupiter"]
    assert "Kepler-10 is not a demo star" in main_window.panel.message.text()


def test_demo_resolver_matches_names():
    resolve = demo_resolver(SyntheticSource())
    targets, errors = resolve("Demo Quiet Star; two-planet, nope")
    assert [t.name for t in targets] == ["Demo Quiet Star", "Demo Two-Planet"]
    assert len(errors) == 1 and errors[0].startswith("nope is not a demo star")


def test_demo_sky_shows_demo_stars_and_never_loads_fields(main_window):
    sky = main_window.sky
    assert {s.name for s in sky.hosts} == {t.name for t in main_window.source.targets()}
    assert "demo mode" in sky.status_text
    ra, dec, width, height = sky.view_region()
    assert abs(ra - 289) < 1 and abs(dec - 44) < 1 and height < 20  # starts on the Kepler field
    click_star(main_window, "Demo Quiet Star")
    assert [t.name for t in main_window.panel.targets()] == ["Demo Quiet Star"]
    main_window.panel.targetActivated.emit(main_window.panel.targets()[0])
    sky._maybe_load_field()  # zoomed to 3 deg; a field load would go to MAST
    assert sky.field == [] and not sky.runner.busy


def test_hovering_stars_never_moves_the_sky_plot(main_window):
    sky = main_window.sky
    QApplication.processEvents()

    def geometry():
        return (sky.plot.geometry(), sky.view_box.sceneBoundingRect(), main_window.sky_dock.width())

    before = geometry()
    in_view = [s for s in sky.hosts if sky.view_box.sceneBoundingRect().contains(
        sky.view_box.mapViewToScene(QPointF(s.ra, s.dec)))]
    assert len(in_view) >= 4
    for star in in_view:
        hover_star(sky, star)
        assert star.name in sky.coord_label.text()
        assert geometry() == before, star.name

    # The status wraps onto its two lines, whole when it fits, elided when not.
    shown = QLabel.text(sky.status_label)  # what is drawn, not the full text
    assert shown.count("\n") == 1 and "…" not in shown
    assert shown.replace("\n", " ") == sky.status_text
    for change in (lambda: sky.set_field([], (290.0, 44.0, 1.0)),
                   lambda: sky._catalog_failed(CatalogError("the archive is down " * 20)),
                   sky.clear_field):
        change()
        QApplication.processEvents()
        assert geometry() == before, sky.status_text
    shown = QLabel.text(sky.status_label)
    assert shown.count("\n") == 1 and shown.endswith("…")


def test_stage_errors_show_in_progress_and_gallery(qtbot):
    source = SyntheticSource(allow_unknown=False)
    window = MainWindow(source)
    qtbot.addWidget(window)
    window.panel.add_target(StarTarget(name="Nowhere Star"))
    window.panel.submit()
    wait_for_results(window)
    status = window.progress.status("Nowhere Star")
    assert status.startswith("Error — fetch:") and "No synthetic data" in status
    message = window.statusBar().currentMessage()
    assert "1 with errors" in message and "Nowhere Star: fetch:" in message
    assert window.gallery.section("Nowhere Star").figure_keys == []
    assert [r.star_key for r in window.table.rows()] == ["Nowhere Star"]


def test_credits_dialog_lists_session_and_general_credits(main_window, qtbot, tmp_path):
    dialog = main_window.show_credits()
    qtbot.addWidget(dialog)
    assert "No stars analysed yet" in dialog.browser.toPlainText()
    dialog.close()

    select_and_submit(main_window, ["Demo Quiet Star"])
    wait_for_results(main_window)
    dialog = main_window.show_credits()
    text = dialog.browser.toPlainText()
    assert SYNTHETIC_CREDIT in text and "Demo Quiet Star" in text
    for phrase in ("TESS mission", "Kepler mission", "NASA Exoplanet Archive", "https://doi.org/10.17909/T9488N",
                   "https://doi.org/10.26133/NEA13", "TESS Input Catalog",
                   "Lightkurve", "Astropy", "astroquery"):
        assert phrase in text
    dialog.copy()
    assert QApplication.clipboard().text() == credits_text(main_window.gallery.analyses())
    shot(dialog, "credits", tmp_path)


# -- MAST mode, without the network -----------------------------------------------

class _NotDemo:
    """Stands in for MastSource: not synthetic, so the window runs in MAST mode."""

    name = "MAST"

    def __init__(self):
        self.max_products = DEFAULT_MAX_PRODUCTS


def test_mast_mode_loads_hosts_when_shown_and_caps_sectors(qtbot, tmp_path):
    window = MainWindow(_NotDemo(), client=recorded_client(tmp_path / "catalog"))
    qtbot.addWidget(window)
    assert window.sky.hosts == []
    window.show()
    qtbot.waitUntil(lambda: len(window.sky.hosts) > 0, timeout=10000)
    assert any(s.name == "Kepler-10" for s in window.sky.hosts)

    menu = window.max_products_menu
    assert menu.isEnabled()
    checked = [a.text() for a in menu.actions() if a.isChecked()]
    assert checked == [str(DEFAULT_MAX_PRODUCTS)]
    next(a for a in menu.actions() if a.text() == "All").trigger()
    assert window.source.max_products is None
    next(a for a in menu.actions() if a.text() == "5").trigger()
    assert window.source.max_products == 5
    assert [a.text() for a in menu.actions() if a.isChecked()] == ["5"]


def test_sector_cap_disabled_in_demo(main_window):
    assert not main_window.max_products_menu.isEnabled()


def test_command_line():
    args = parse_args(["--targets", "Kepler-10, TOI-270", "--max-sectors", "0"])
    assert args.targets == "Kepler-10, TOI-270" and args.max_sectors is None and not args.demo
    assert parse_args([]).max_sectors == DEFAULT_MAX_PRODUCTS
    with pytest.raises(SystemExit):
        parse_args(["--max-sectors", "-3"])
    source = make_source(False, 7)
    assert source.name == "MAST" and source.max_products == 7


def test_no_source_warns_instead_of_running(qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[1]))
    window = MainWindow(None)
    qtbot.addWidget(window)
    assert window.analyse([StarTarget(name="Kepler-10")]) == []
    assert warned == ["No data source"] and not window.runner.busy


# -- the runner on its own -------------------------------------------------------

def _slow_analyzer(started: threading.Event):
    def analyze(target, source, progress_cb=None):
        started.set()
        for i in range(500):
            progress_cb(f"{target.name}: step {i}", i / 500)
            time.sleep(0.01)
        return StarAnalysis(target=target)
    return analyze


def test_cancel_stops_running_and_queued_stars(qtbot):
    started = threading.Event()
    runner = AnalysisRunner(SyntheticSource(), max_workers=1, analyzer=_slow_analyzer(started))
    panel = ProgressPanel(runner)
    qtbot.addWidget(panel)
    cancelled, finished = [], []
    runner.starCancelled.connect(cancelled.append)
    runner.starFinished.connect(finished.append)
    queued = runner.submit([StarTarget(name="A"), StarTarget(name="B"), StarTarget(name="A")])
    assert [t.name for t in queued] == ["A", "B"]  # the duplicate is skipped
    assert started.wait(5)
    qtbot.waitUntil(lambda: "step" in panel.status("A"))
    assert panel.status("B") == "Queued" and panel.cancel_button.isEnabled()
    with qtbot.waitSignal(runner.idle, timeout=5000):
        QTest.mouseClick(panel.cancel_button, Qt.MouseButton.LeftButton)
    assert cancelled == ["A", "B"] and finished == []
    assert panel.status("A") == panel.status("B") == "Cancelled"
    assert not runner.busy and not panel.cancel_button.isEnabled()
    panel.clear_finished()
    assert panel.tree.topLevelItemCount() == 0


def test_unexpected_exception_is_reported_per_star(qtbot):
    def analyze(target, source, progress_cb=None):
        if target.name == "Bad":
            raise ValueError("broken light curve")
        return StarAnalysis(target=target)

    runner = AnalysisRunner(SyntheticSource(), analyzer=analyze)
    panel = ProgressPanel(runner)
    qtbot.addWidget(panel)
    failed, finished = [], []
    runner.starFailed.connect(lambda name, error: failed.append((name, error)))
    runner.starFinished.connect(lambda a: finished.append(a.target.name))
    with qtbot.waitSignal(runner.idle, timeout=5000):
        runner.submit([StarTarget(name="Bad"), StarTarget(name="Good")])
    assert failed == [("Bad", "ValueError: broken light curve")] and finished == ["Good"]
    assert panel.status("Bad") == "Error — ValueError: broken light curve"
    assert panel.status("Good") == "Done — no candidates"


def test_analysis_cancelled_from_progress_is_not_an_error(qtbot):
    def analyze(target, source, progress_cb=None):
        raise AnalysisCancelled()

    runner = AnalysisRunner(SyntheticSource(), analyzer=analyze)
    cancelled = []
    runner.starCancelled.connect(cancelled.append)
    with qtbot.waitSignal(runner.idle, timeout=5000):
        runner.submit([StarTarget(name="X")])
    assert cancelled == ["X"]


def test_closing_window_cancels_running_analyses(qtbot):
    started = threading.Event()
    window = MainWindow(SyntheticSource())
    window.runner.analyzer = _slow_analyzer(started)
    window.analyse([StarTarget(name="Demo Hot Jupiter")])
    assert started.wait(5)
    t0 = time.monotonic()
    window.close()
    assert time.monotonic() - t0 < 2.0
    assert window.runner.pool.activeThreadCount() == 0
    wait_until(lambda: not window.runner.busy, 5)
