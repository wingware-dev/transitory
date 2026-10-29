"""analyze_star end to end over SyntheticSource, with the real cleaning step.

The demo stars carry stellar variability and flares, so this also checks
that search, fit and vetting hold up on cleaned (not ideal) data.
"""

import numpy as np
import pytest

from transitory.models import StarTarget
from transitory.pipeline import AnalysisCancelled, run
from transitory.pipeline.run import analyze_star
from transitory.sources import SourceError, SyntheticSource
from transitory.synthetic import InjectedPlanet, SyntheticStar


@pytest.fixture(scope="module")
def source():
    return SyntheticSource()


def analyze(source, name, **kw):
    return analyze_star(StarTarget(name=name), source, **kw)


def strong(result):
    return [p for p in result.planets if p.vetting and p.vetting.confidence == "strong"]


def matches(planet_result, injected, rel=1e-3):
    return abs(planet_result.fit.period - injected.period) / injected.period < rel


@pytest.mark.parametrize("name", ["Demo Hot Jupiter", "Demo Super-Earth", "Demo Long Period"])
def test_single_planet_stars(source, name):
    result = analyze(source, name)
    assert result.ok, result.errors
    (injected,) = source.find(StarTarget(name=name)).planets
    found = strong(result)
    assert len(found) == 1 and matches(found[0], injected)
    fit = found[0].fit
    assert fit.rp_rs == pytest.approx(np.sqrt(injected.depth), rel=0.15)
    assert fit.duration_hours == pytest.approx(injected.duration_hours, rel=0.10)


def test_two_planet_star(source):
    result = analyze(source, "Demo Two-Planet")
    assert result.ok, result.errors
    injected = source.find(StarTarget(name="Demo Two-Planet")).planets
    found = strong(result)
    assert len(found) == 2
    for inj in injected:
        assert any(matches(p, inj) for p in found)


def test_eclipsing_binary_flagged(source):
    result = analyze(source, "Demo Eclipsing Binary")
    assert result.ok, result.errors
    assert not strong(result)
    primary = result.planets[0]
    assert abs(primary.fit.period - 1.87) / 1.87 < 1e-3
    assert primary.vetting.confidence == "none"
    assert any("eclipsing binary" in r for r in primary.vetting.reasons)


def test_quiet_star_has_no_strong_detection(source):
    result = analyze(source, "Demo Quiet Star")
    assert result.ok, result.errors
    assert not strong(result)


def test_periodograms_one_per_pass(source):
    result = analyze(source, "Demo Hot Jupiter")
    assert len(result.periodograms) >= 1
    assert len(result.planets) == sum(p.best is not None for p in result.periodograms)
    # Passes stop at the first empty one, or at the planet limit.
    assert all(p.best is not None for p in result.periodograms[:-1])
    for p in result.periodograms:
        assert len(p.periods) == len(p.power)


def test_progress_runs_from_zero_to_one(source):
    seen = []
    analyze(source, "Demo Hot Jupiter", progress_cb=lambda m, f: seen.append((m, f)))
    fractions = [f for _, f in seen]
    assert fractions[0] == 0.0 and fractions[-1] == 1.0
    assert fractions == sorted(fractions)
    assert all(m.startswith("Demo Hot Jupiter: ") for m, _ in seen)


def test_cancel_during_search_propagates(source):
    def cancel(message, fraction):
        if "BLS" in message:
            raise AnalysisCancelled()

    with pytest.raises(AnalysisCancelled):
        analyze(source, "Demo Hot Jupiter", progress_cb=cancel)


def test_fetch_error_recorded():
    source = SyntheticSource(stars=[], allow_unknown=False)
    result = analyze_star(StarTarget(name="Nobody"), source)
    assert result.raw is None and not result.ok
    assert result.errors[0].startswith("fetch:")


def test_too_few_transits_not_strong():
    # One transit in the data (the second falls in a gap).
    star = SyntheticStar(StarTarget(name="Lonely"), [InjectedPlanet(30.0, 10.0, 0.004, 5.0)],
                         gaps=[(35.0, 45.0)], seed=5)
    result = analyze_star(star.target, SyntheticSource(stars=[star], allow_unknown=False))
    assert result.ok, result.errors
    assert not strong(result)


def test_two_transit_planet_is_a_duo_transit():
    # P=20 d over 40 d: two clean transits, kept but listed apart from planet candidates.
    star = SyntheticStar(StarTarget(name="Duo"), [InjectedPlanet(20.0, 10.0, 0.004, 5.0)],
                         baseline_days=40.0, seed=5)
    result = analyze_star(star.target, SyntheticSource(stars=[star], allow_unknown=False))
    assert result.ok, result.errors
    assert [p.vetting.confidence for p in result.planets] == ["duo-transit"]
    duo = result.planets[0]
    assert duo.is_duo_transit and duo.candidate.n_transits == 2
    assert abs(duo.fit.period - 20.0) / 20.0 < 5e-3


def test_fit_failure_keeps_candidate(source, monkeypatch):
    def broken_fit(cleaned, candidate, star=None):
        raise RuntimeError("did not converge")

    monkeypatch.setattr(run, "fit_transit", broken_fit)
    result = analyze(source, "Demo Hot Jupiter")
    assert result.planets and result.planets[0].fit is None
    assert result.planets[0].vetting.confidence == "weak"
    assert any(e.startswith("fit #1:") for e in result.errors)
