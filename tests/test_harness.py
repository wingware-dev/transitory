import os

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QLabel

from harness import shot
from transitory.gui.main_window import MainWindow, make_source, parse_args
from transitory.models import StarTarget
from transitory.pipeline import AnalysisCancelled
from transitory.pipeline.run import analyze_star
from transitory.sources import SyntheticSource


def test_qt_is_offscreen(qapp):
    assert os.environ["QT_QPA_PLATFORM"] == "offscreen"
    assert QApplication.platformName() == "offscreen"


def test_cache_dir_is_per_test(tmp_path):
    from transitory.config import cache_dir, reports_dir

    assert cache_dir() == tmp_path / "cache" and cache_dir().is_dir()
    assert reports_dir() == tmp_path / "reports"


def test_main_window_layout(main_window):
    assert isinstance(main_window, MainWindow)
    assert isinstance(main_window.source, SyntheticSource)
    assert main_window.sky_dock.windowTitle() == "Sky"
    assert main_window.selection_dock.windowTitle() == "Selection"
    tabs = main_window.tabs
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["Gallery", "Table"]
    assert main_window.findChild(QLabel, "skyPlaceholder") is not None
    assert "Synthetic" in main_window.statusBar().currentMessage()


def test_shot_writes_png(main_window, tmp_path):
    path = shot(main_window, "skeleton", tmp_path)
    image = QImage(str(path))
    assert path.name == "skeleton.png"
    assert image.width() == main_window.width() and image.height() == main_window.height()


def test_demo_flag_picks_synthetic_source():
    assert parse_args(["--demo"]).demo
    assert isinstance(make_source(True), SyntheticSource)


def test_analyze_star_records_unimplemented_stages():
    # Until the pipeline steps land, analyze_star fetches and reports the stage that stopped it.
    progress = []
    result = analyze_star(StarTarget(name="Demo Hot Jupiter"), SyntheticSource(),
                          progress_cb=lambda m, f: progress.append(f))
    assert result.raw is not None and len(result.raw) > 0
    assert result.credits
    assert progress[0] == 0.0 and all(0.0 <= f <= 1.0 for f in progress)
    if result.errors:
        assert result.errors[0].split(":")[0] in {"clean", "search", "fit #1", "vetting #1"}


def test_analyze_star_propagates_cancel():
    def cancel(message, fraction):
        raise AnalysisCancelled()

    try:
        analyze_star(StarTarget(name="Demo Hot Jupiter"), SyntheticSource(), progress_cb=cancel)
    except AnalysisCancelled:
        return
    raise AssertionError("AnalysisCancelled was swallowed")
