"""Iterative BLS search on synthetic light curves with planets injected at known P and depth.

Tolerances: period within 0.1%, BLS depth within 25% of the injected depth
(the box averages over ingress/egress), and the BLS box duration within
50% of T14 (the grid of trial durations is coarse by design; the fit
refines it).
"""

import numpy as np
import pytest

from pipeline_synth import flat_cleaned
from transitory.pipeline.search import (SDE_THRESHOLD, SNR_THRESHOLD, count_transits,
                                        period_grid, search_transits)
from transitory.synthetic import InjectedPlanet

P_TOL = 1e-3
DEPTH_TOL = 0.25
DURATION_TOL = 0.5


def found(results):
    return [r.best for r in results if r.best is not None]


def assert_recovered(cand, planet):
    assert abs(cand.period - planet.period) / planet.period < P_TOL
    assert abs(cand.depth - planet.depth) / planet.depth < DEPTH_TOL
    assert abs(cand.duration * 24 - planet.duration_hours) / planet.duration_hours < DURATION_TOL
    assert cand.snr >= SNR_THRESHOLD and cand.sde >= SDE_THRESHOLD
    # Candidate t0 sits on one of the injected transits.
    offset = ((cand.t0 - planet.t0) / planet.period + 0.5) % 1.0 - 0.5
    assert abs(offset * planet.period) < 0.5 * planet.duration_hours / 24


@pytest.mark.parametrize("planet", [
    pytest.param(InjectedPlanet(3.21, 1.3, 0.010, 2.9), id="deep-hot-jupiter"),
    pytest.param(InjectedPlanet(5.74, 2.1, 0.0009, 2.4), id="super-earth"),
    pytest.param(InjectedPlanet(4.1, 1.0, 0.0005, 2.5), id="shallow-500ppm"),
    pytest.param(InjectedPlanet(0.46, 0.1, 0.004, 0.5), id="ultra-short-period"),
    pytest.param(InjectedPlanet(21.6, 7.0, 0.004, 5.5), id="long-period-3-transits"),
])
def test_single_planet_recovered(planet):
    results = search_transits(flat_cleaned([planet]))
    cands = found(results)
    assert len(cands) == 1
    assert_recovered(cands[0], planet)
    expected = int(np.ceil((54.0 - planet.t0) / planet.period))
    assert cands[0].n_transits == expected
    # The search stops at the first pass with nothing significant.
    assert len(results) == 2 and results[-1].best is None


def test_two_planets_found_in_separate_passes():
    b = InjectedPlanet(2.43, 0.8, 0.003, 2.0)
    c = InjectedPlanet(9.87, 4.4, 0.005, 3.8)
    results = search_transits(flat_cleaned([b, c]))
    cands = sorted(found(results), key=lambda k: k.period)
    assert len(cands) == 2
    assert_recovered(cands[0], b)
    assert_recovered(cands[1], c)
    assert len(results) == 3 and results[-1].best is None


def test_three_planets_stops_at_max_planets():
    planets = [InjectedPlanet(1.7, 0.3, 0.004, 1.8), InjectedPlanet(4.9, 1.1, 0.003, 2.6),
               InjectedPlanet(11.3, 5.0, 0.005, 3.5)]
    results = search_transits(flat_cleaned(planets))
    assert len(results) == 3
    cands = sorted(found(results), key=lambda k: k.period)
    for cand, planet in zip(cands, planets, strict=True):
        assert_recovered(cand, planet)
    assert len(search_transits(flat_cleaned(planets), max_planets=1)) == 1


@pytest.mark.parametrize("seed", [100, 104, 111, 117])
def test_flat_star_gives_no_detection(seed):
    # Seeds include the noisiest SDE peaks seen in tuning (SDE ~8-10, SNR < 6.5).
    results = search_transits(flat_cleaned([], seed=seed))
    assert len(results) == 1 and results[0].best is None
    assert len(results[0].periods) == len(results[0].power) > 1000


def test_too_shallow_gives_no_detection():
    # 100 ppm under 300 ppm noise: SNR ~4 even with all 13 transits.
    results = search_transits(flat_cleaned([InjectedPlanet(4.1, 1.0, 0.0001, 2.5)]))
    assert not found(results)


def test_single_transit_is_not_a_periodic_detection():
    # P=30 d: transits at 10 and 40 d, the second lost in a gap -> one event.
    lc = flat_cleaned([InjectedPlanet(30.0, 10.0, 0.004, 5.0)], gaps=[(35.0, 45.0)])
    assert not found(search_transits(lc))


def test_single_dip_does_not_take_a_pass_from_a_planet():
    # TOI-270-like: a deep one-off dip (from systematics, say) at 30.2 d folds
    # with one of d's transits into a 2-event peak at ~26 d.  It used to take
    # pass 2, so b (3.36 d, weakest) was never searched for.
    b = InjectedPlanet(3.36, 1.1, 0.0010, 1.3)
    c = InjectedPlanet(5.66, 2.3, 0.0030, 1.7)
    d = InjectedPlanet(11.38, 4.0, 0.0025, 2.1)
    dip = InjectedPlanet(500.0, 30.2, 0.0060, 4.0)
    results = search_transits(flat_cleaned([b, c, d, dip]))
    cands = sorted(found(results), key=lambda k: k.period)
    assert len(results) == 3 and len(cands) == 3
    for cand, planet in zip(cands, [b, c, d], strict=True):
        assert_recovered(cand, planet)


def test_hot_jupiter_secondary_does_not_take_a_pass():
    # The secondary eclipse used to come back in pass 2 at P/2, so the
    # 4.1 d planet was missed.
    hj = InjectedPlanet(1.53, 0.4, 0.010, 2.6, secondary_depth=0.0005)
    small = [InjectedPlanet(4.1, 1.0, 0.0008, 2.5), InjectedPlanet(7.7, 2.2, 0.0012, 3.0)]
    results = search_transits(flat_cleaned([hj, *small]))
    cands = sorted(found(results), key=lambda k: k.period)
    assert len(results) == 3 and len(cands) == 3
    for cand, planet in zip(cands, [hj, *small], strict=True):
        assert_recovered(cand, planet)


def test_rejected_peaks_are_capped(monkeypatch):
    # Many one-off dips: each is masked and searched past, but only up to
    # MAX_REJECTED_PEAKS extra BLS runs, and none becomes a candidate.  Depths
    # differ by 2.2x so no two dips pass as a 2-transit planet.
    from transitory.pipeline import search
    dips = [InjectedPlanet(500.0, t, 0.0015 * 2.2 ** k, 4.0)
            for k, t in enumerate((3.0, 11.2, 15.9, 27.4, 33.1, 41.8, 46.3))]
    calls = []
    real_pass = search._search_pass
    monkeypatch.setattr(search, "_search_pass", lambda *a: calls.append(1) or real_pass(*a))
    results = search_transits(flat_cleaned(dips))
    assert not found(results)
    assert len(results) == 1
    assert len(calls) <= 1 + search.MAX_REJECTED_PEAKS


def test_two_transits_at_the_grid_edge_unaliased():
    # P=20 d over 40 d (transits at 10, 30): BLS peaks at P/2 with an empty
    # middle epoch; the search must double back to 20 d.
    planet = InjectedPlanet(20.0, 10.0, 0.004, 5.0)
    cands = found(search_transits(flat_cleaned([planet], baseline_days=40.0)))
    assert len(cands) == 1
    assert abs(cands[0].period - 20.0) / 20.0 < 5e-3
    assert cands[0].n_transits == 2


def test_eclipsing_binary_is_not_unaliased():
    # Both eclipses present at different depths: P stays at the true orbit for vetting to flag.
    eb = InjectedPlanet(1.87, 0.5, 0.02, 3.1, odd_even_ratio=0.6)
    cand = found(search_transits(flat_cleaned([eb]), max_planets=1))[0]
    assert abs(cand.period - 1.87) / 1.87 < P_TOL


def test_period_grid_covers_range_with_valid_durations():
    grid = period_grid(54.0, 0.3, 27.0)
    periods = np.concatenate([p for p, _ in grid])
    assert periods.min() == pytest.approx(0.3)
    assert periods.max() < 27.0 and periods.max() > 26.9
    assert np.all(np.diff(periods) > 0)
    for seg_periods, durations in grid:
        assert durations.max() < seg_periods.min()  # BoxLeastSquares requirement
        assert durations.max() <= 13.0 / 24 + 1e-12


def test_period_grid_is_capped_for_long_baselines():
    from transitory.pipeline.search import MAX_GRID_PERIODS
    total = sum(len(p) for p, _ in period_grid(1400.0, 0.3, 700.0))
    assert total <= MAX_GRID_PERIODS


def test_count_transits_skips_gaps():
    time = np.arange(1.0, 29.0, 0.01)
    time = time[(time < 9.0) | (time > 11.0)]  # second transit (t=10) in a gap
    assert count_transits(time, 5.0, 5.0, 0.1) == 4  # 5, 15, 20, 25


def test_too_short_baseline_raises():
    lc = flat_cleaned([], baseline_days=0.5)
    with pytest.raises(ValueError, match="too short"):
        search_transits(lc)


def test_progress_reported_in_range():
    seen = []
    search_transits(flat_cleaned([InjectedPlanet(3.21, 1.3, 0.010, 2.9)]),
                    progress_cb=lambda m, f: seen.append(f))
    assert seen and all(0.0 <= f <= 1.0 for f in seen)
    assert seen == sorted(seen)
