"""Known-planet validation: matching, explanations, the report, the CLI and the GUI menu.

Published values are synthetic here (injected planets, or a fake archive),
so nothing touches the network.
"""

import json
import math

import pytest

from transitory.catalog.client import CatalogClient, ResponseCache
from transitory.models import (
    FitResult,
    PlanetResult,
    StarAnalysis,
    StarTarget,
    TransitCandidate,
    Vetting,
)
from transitory.pipeline import AnalysisCancelled
from transitory.sources import SyntheticSource
from transitory.synthetic import InjectedPlanet, SyntheticStar, demo_stars
from transitory.validation import report
from transitory.validation.__main__ import main as cli_main
from transitory.validation.targets import ValidationTarget, select, validation_targets
from transitory.validation.validate import (
    Published,
    PublishedSet,
    alias_factor,
    archive_provider,
    compare,
    match_planets,
    run_validation,
    synthetic_provider,
    synthetic_targets,
)


# -- the target set ------------------------------------------------------------------

def test_validation_set_has_hosts_and_three_null_stars():
    targets = validation_targets()
    names = {t.target.name for t in targets}
    assert {"Kepler-10", "Kepler-7", "Kepler-2", "Kepler-22", "pi Men", "LHS 3844", "WASP-18",
            "TOI-270"} <= names
    nulls = [t for t in targets if t.role == "null"]
    assert [t.target.name for t in nulls] == ["KIC 8006161", "KIC 6106415", "TIC 31410749"]
    assert all(t.target.tic is not None and t.expectation for t in nulls)
    planets = [p for t in targets for p in t.planets]
    assert len(planets) == 11 and "Kepler-22 b" in planets and "HAT-P-7 b" in planets
    assert "hard case" in next(t for t in targets if t.target.name == "Kepler-22").expectation


def test_select_by_star_or_planet_name():
    targets = validation_targets()
    assert [t.target.name for t in select(targets, ["kepler-22", "TOI-270 c"])] == ["Kepler-22", "TOI-270"]
    with pytest.raises(KeyError, match="nope"):
        select(targets, ["Kepler-10", "nope"])


# -- matching ------------------------------------------------------------------------

def test_alias_factor():
    assert alias_factor(3.0, 3.0) == 1.0
    assert alias_factor(1.5004, 3.0) == 0.5
    assert alias_factor(6.02, 3.0) == 2.0
    assert alias_factor(3.2, 3.0) is None
    assert alias_factor(1.0, math.nan) is None


def test_match_prefers_true_periods_then_closest():
    # Found 2.0 is both P(b) exactly and a 2x alias of c (1.0); it goes to b.
    pairs = match_planets(found=[2.0, 0.5, 9.0], published=[2.0, 1.0, 7.0])
    assert pairs == [(0, 0, 1.0), (1, 1, 0.5)]
    # Each found planet matches at most one published planet.
    assert match_planets([4.0], [4.0, 4.01]) == [(0, 0, 1.0)]


def _planet(period, confidence="strong", rp_rs=0.1, n_transits=10, reasons=()):
    cand = TransitCandidate(period=period, t0=0.0, duration=0.1, depth=rp_rs ** 2, snr=20.0,
                            sde=12.0, n_transits=n_transits)
    fit = FitResult(period=period, t0=0.0, rp_rs=rp_rs, a_rs=10.0, b=0.2, duration_hours=2.4,
                    depth=rp_rs ** 2, uncertainties={"period": 1e-4, "rp_rs": 1e-3, "duration_hours": 0.05})
    return PlanetResult(candidate=cand, fit=fit,
                        vetting=Vetting(odd_even_sigma=0.5, secondary_depth=0.0, confidence=confidence,
                                        reasons=list(reasons)))


def _analysis(planets, mission="TESS"):
    from transitory.models import LightCurve
    import numpy as np

    t = np.linspace(0, 50, 2000)
    lc = LightCurve(time=t, flux=np.ones_like(t), flux_err=np.full_like(t, 1e-3), mission=mission)
    return StarAnalysis(target=StarTarget(name="Star"), raw=lc, planets=planets)


def test_compare_sorts_found_alias_missed_spurious_and_rejected():
    vt = ValidationTarget(target=StarTarget(name="Star"), role="planet-host")
    published = PublishedSet(planets=[Published("Star b", 3.0, rp_rs=0.1, duration_h=2.5),
                                      Published("Star c", 8.0), Published("Star d", 20.0)])
    analysis = _analysis([_planet(3.0001), _planet(4.0), _planet(9.0, "none", reasons=["odd/even differ"]),
                          _planet(9.0002)])
    comps, others = compare(vt, analysis, published)
    assert [c.status for c in comps] == ["found", "found at 1/2× alias", "missed"]
    assert comps[0].found_index == 0 and comps[1].found_index == 1
    assert "alias" in comps[1].explanation and comps[2].explanation
    assert [(o.index, o.kind) for o in others] == [(2, "rejected"), (3, "spurious")]
    assert "3× alias of Star b" in others[1].explanation
    assert "vetting: odd/even differ" in others[0].explanation


def test_planet_found_twice_is_a_duplicate_not_a_false_positive():
    vt = ValidationTarget(target=StarTarget(name="Star"), role="planet-host")
    published = PublishedSet(planets=[Published("Star b", 2.2)])
    secondary = _planet(2.2, "none", reasons=["secondary eclipse of the P=2.2 d signal"])
    comps, others = compare(vt, _analysis([_planet(2.2), secondary]), published)
    assert comps[0].found_index == 0  # the earlier pass gets the match
    assert [(o.index, o.kind) for o in others] == [(1, "duplicate")]
    assert "Star b found again" in others[0].explanation and "secondary eclipse" in others[0].explanation


def test_findings_summarise_patterns(tmp_path):
    from datetime import UTC, datetime

    from transitory.validation.validate import TargetResult, ValidationRun

    vt = ValidationTarget(target=StarTarget(name="Star", radius_rsun=0.4), role="planet-host")
    published = PublishedSet(planets=[Published("Star b", 2.2, rp_rs=0.08), Published("Star c", 3.3)])
    hot = _planet(2.2, "weak", rp_rs=0.1,
                  reasons=["secondary eclipse at phase 0.5: 50 ppm (9 sigma), small enough to be planetary emission"])
    single = _planet(300.0, "weak", n_transits=1)
    analysis = _analysis([hot, single, _planet(150.0, "none", n_transits=2)])
    result = TargetResult(vt=vt, analysis=analysis, published=published)
    result.comparisons, result.others = compare(vt, analysis, published)
    run = ValidationRun(results=[result], source_name="MAST", max_products=None, started=datetime.now(UTC))
    findings = " ".join(report.run_findings(run))
    assert "2 of 2 extra candidates cover at most 2 transits" in findings
    assert "1 spurious detection passed vetting as 'weak'" in findings
    assert "Star b rated only 'weak' because the vetting detected a secondary eclipse" in findings
    assert "Star b (+25%)" in findings and "M-dwarf host of Star b" in findings


def test_spurious_signal_near_tess_orbit_is_explained():
    vt = ValidationTarget(target=StarTarget(name="Quiet"), role="null")
    comps, others = compare(vt, _analysis([_planet(13.8, "weak", n_transits=3)]), PublishedSet())
    assert comps == [] and [o.kind for o in others] == ["spurious"]
    assert "TESS orbit" in others[0].explanation and "only 3 events" in others[0].explanation


def test_unexplained_spurious_signal_points_at_red_noise():
    vt = ValidationTarget(target=StarTarget(name="Quiet"), role="null")
    _, others = compare(vt, _analysis([_planet(5.3)]), PublishedSet())
    assert "red" in others[0].explanation


def test_null_star_false_positive_signals_are_not_expected_planets():
    vt = ValidationTarget(target=StarTarget(name="Quiet"), role="null")
    published = PublishedSet(planets=[Published("TOI-9.01", 2.0, disposition="FP")])
    comps, others = compare(vt, _analysis([]), published)
    assert comps == [] and others == []


# -- explanations from real (synthetic) data -------------------------------------------

def _validate_star(planets, noise_ppm=300.0):
    star = SyntheticStar(target=StarTarget(name="Test Star"), planets=planets, noise_ppm=noise_ppm, seed=3)
    source = SyntheticSource(stars=[star])
    run = run_validation(synthetic_targets(source), source, synthetic_provider(source))
    return run.results[0]


def test_miss_beyond_half_the_baseline_is_explained():
    result = _validate_star([InjectedPlanet(60.0, 10.0, 0.004, 5.0, name="Test Star b")])
    (comp,) = result.comparisons
    assert comp.status == "missed"
    assert "longer than half the" in comp.explanation
    assert "only 1 transit " in comp.explanation


def test_miss_too_shallow_for_the_noise_is_explained():
    result = _validate_star([InjectedPlanet(3.3, 1.0, 0.00003, 2.5, name="Test Star b")], noise_ppm=600.0)
    (comp,) = result.comparisons
    assert comp.status == "missed"
    assert "too shallow for the noise" in comp.explanation
    assert "SDE at the published period" in comp.explanation


# -- runs, reports and the archive -----------------------------------------------------

def _demo_source(n=2):
    return SyntheticSource(stars=demo_stars()[:n])


def test_run_and_report_on_demo_stars(tmp_path):
    source = SyntheticSource(stars=[s for s in demo_stars()
                                    if s.target.name in ("Demo Hot Jupiter", "Demo Quiet Star")])
    run = run_validation(synthetic_targets(source), source, synthetic_provider(source), max_products=None)
    tally = run.tally()
    assert tally["published"] == 1 and tally["found"] == 1 and tally["missed"] == 0
    assert tally["spurious_null"] == 0 and not run.cancelled

    path = report.write_report(run, tmp_path / "out")
    text = path.read_text()
    assert "# Known-planet validation" in text and "all sectors / quarters" in text
    assert "| Demo Hot Jupiter b (b) | found | 3.21000 |" in text
    assert "injected (synthetic)" in text and "NASA Exoplanet Archive" not in text
    assert "Demo Quiet Star" in text.split("## Null stars")[1]
    assert (tmp_path / "out" / "summary.png").stat().st_size > 10_000
    assert (tmp_path / "out" / "figures" / "demo-hot-jupiter-b.png").is_file()
    data = json.loads((tmp_path / "out" / "results.json").read_text())
    assert data["tally"] == tally and data["targets"][0]["planets"][0]["status"] == "found"

    docs = tmp_path / "docs"
    (docs / "figures").mkdir(parents=True)
    (docs / "figures" / "stale.png").write_bytes(b"x")
    published = report.publish(path.parent, docs)
    assert published.read_text() == text
    assert sorted(p.name for p in (docs / "figures").iterdir()) == ["demo-hot-jupiter-b.png"]


def test_cancelled_run_keeps_finished_targets():
    source = _demo_source(3)
    calls = []

    def progress(message, fraction):
        calls.append(message)
        if message.startswith("[2/3]") and "fetching" in message:
            raise AnalysisCancelled()

    run = run_validation(synthetic_targets(source), source, synthetic_provider(source), progress_cb=progress)
    assert run.cancelled and [r.name for r in run.results] == ["Demo Hot Jupiter"]
    assert "cancelled" in report.report_markdown(run, {})


def _fake_archive(method, url, params):
    query = params["query"]
    if "from pscomppars" in query and "377780790" in query:
        return json.dumps([{"pl_name": "Kepler-10 b", "hostname": "Kepler-10", "tic_id": "TIC 377780790",
                            "pl_orbper": 0.8374907, "pl_trandur": 1.81, "pl_trandep": 0.0152,
                            "pl_ratror": 0.0127, "pl_tranmid": 2454964.57513, "tran_flag": 1,
                            "pl_orbper_reflink": "<a href=https://example.org/ref>Smith et al. 2020</a>"}])
    if "from toi" in query:
        return json.dumps([{"tid": 123234496, "toi": "9999.01", "tfopwg_disp": "FP", "pl_orbper": 1.5}])
    return "[]"


def test_archive_provider_records_dates_and_refs(tmp_path):
    client = CatalogClient(transport=_fake_archive, cache=ResponseCache(tmp_path))
    provide = archive_provider(client)
    host = next(t for t in validation_targets() if t.target.name == "Kepler-10")
    published = provide(host)
    assert published.source == "NASA Exoplanet Archive pscomppars" and published.retrieved
    (b,) = published.planets
    assert b.name == "Kepler-10 b" and b.t0_bjd == pytest.approx(2454964.57513)
    assert "Smith et al. 2020" in b.ref and f"retrieved {published.retrieved}" in b.ref
    null = next(t for t in validation_targets() if t.target.name == "KIC 8006161")
    signals = provide(null)
    assert "TOI" in signals.source and [p.disposition for p in signals.planets] == ["FP"]


# -- command line and GUI --------------------------------------------------------------

def test_cli_demo_writes_report(tmp_path, capsys):
    assert cli_main(["--demo", "--targets", "Demo Super-Earth", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "1 found, 0 at aliases, 0 missed of 1 published" in out
    assert (tmp_path / "validation.md").is_file()
    assert cli_main(["--demo", "--targets", "Kepler-10"]) == 2


def test_gui_menu_runs_validation_and_shows_report(qtbot, tmp_path):
    from transitory.gui.main_window import MainWindow

    window = MainWindow(_demo_source(2))
    qtbot.addWidget(window)
    menus = [a.text() for a in window.menuBar().actions()]
    assert menus == ["&File", "&Settings", "&Validation", "&Help"]
    controller = window.validation
    controller.output_dir = tmp_path
    with qtbot.waitSignal(controller.reportWritten, timeout=60000) as blocker:
        assert controller.start(confirm=False)
        assert not controller.run_action.isEnabled() and controller.cancel_action.isEnabled()
    assert blocker.args[0] == tmp_path / "validation.md"
    assert controller.run_action.isEnabled() and not controller.cancel_action.isEnabled()
    dialog = controller.dialog
    qtbot.addWidget(dialog)
    text = dialog.browser.toPlainText()
    assert "Known-planet validation" in text and "Demo Super-Earth b" in text
    assert "Validation report written" in window.statusBar().currentMessage()


def test_gui_cancel_validation(qtbot, tmp_path):
    from transitory.gui.main_window import MainWindow

    window = MainWindow(_demo_source(3))
    qtbot.addWidget(window)
    controller = window.validation
    controller.output_dir = tmp_path
    with qtbot.waitSignal(controller.finished, timeout=60000):
        controller.start(confirm=False)
        controller.cancel()
    assert "cancelled" in (tmp_path / "validation.md").read_text()


def test_show_report_without_one_says_so(qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from transitory.gui.main_window import MainWindow
    from transitory.validation import gui

    told = []
    monkeypatch.setattr(gui, "latest_report", lambda: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *a: told.append(a[2]))
    window = MainWindow(_demo_source(1))
    qtbot.addWidget(window)
    assert window.validation.show_report() is None
    assert told and "No validation report yet" in told[0]
