"""Light-curve cleaning on synthetic data with known injected transits."""

import numpy as np
import pytest

from transitory.models import LightCurve, StarTarget
from transitory.pipeline.clean import (biweight_location, biweight_poly, clean_light_curve,
                                       find_dips, robust_std, split_segments)
from transitory.synthetic import InjectedPlanet, SyntheticStar, make_light_curve, trapezoid_transit

GAPS = [(13.0, 14.0), (27.0, 28.5)]


def fitted_depth(lc: LightCurve, planet: InjectedPlanet) -> float:
    """Depth from a linear least-squares fit of the known transit shape plus a baseline."""
    unit = InjectedPlanet(planet.period, planet.t0, 1.0, planet.duration_hours, planet.ingress_fraction)
    shape = 1.0 - trapezoid_transit(lc.time, unit)
    design = np.vstack([np.ones_like(shape), -shape]).T
    return float(np.linalg.lstsq(design, lc.flux, rcond=None)[0][1])


def _edges(baseline: float, gaps) -> np.ndarray:
    return np.array([0.0, baseline] + [x for gap in gaps for x in gap])


def transits_clear_of_edges(star: SyntheticStar) -> bool:
    """True if no transit of ``star`` comes within 2 h of a gap or the ends of the data.

    A transit cut by an edge is genuinely ambiguous to any detrender, so the
    depth tests keep clear of them.
    """
    edges = _edges(star.baseline_days, star.gaps)
    for p in star.planets:
        margin = p.duration_hours / 48.0 + 2.0 / 24.0
        mids = p.t0 + p.period * np.arange(-1, star.baseline_days / p.period + 2)
        if np.min(np.abs(mids[:, None] - edges[None, :])) < margin:
            return False
    return True


def clear_t0(period: float, duration_hours: float, gaps=GAPS, baseline: float = 54.0) -> float:
    """The first t0 (in 0.01 d steps) whose transits are all clear of the data edges."""
    for t0 in np.arange(0.0, period, 0.01):
        star = SyntheticStar(StarTarget("t0"), [InjectedPlanet(period, t0, 0.001, duration_hours)],
                             baseline_days=baseline, gaps=gaps)
        if transits_clear_of_edges(star):
            return float(t0)
    raise AssertionError(f"no clear t0 for P={period}")


DEPTH_CASES = [
    # depth, T14 hours, noise ppm, period
    pytest.param(0.010, 3.0, 300, 3.7, id="hot-jupiter"),
    pytest.param(0.0009, 2.4, 200, 5.74, id="super-earth"),
    pytest.param(0.0005, 2.0, 100, 2.9, id="shallow"),
    pytest.param(0.005, 5.5, 300, 6.1, id="medium-duration"),
    pytest.param(0.002, 10.0, 200, 4.3, id="long-duration"),
    pytest.param(0.004, 13.0, 200, 3.7, id="longest-searched"),
    pytest.param(0.003, 2.0, 200, 1.3, id="short-period"),
    pytest.param(0.004, 6.0, 200, 17.0, id="long-period"),
]


@pytest.mark.parametrize("depth,duration,noise,period", DEPTH_CASES)
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_injected_depth_kept_within_5_percent(depth, duration, noise, period, seed):
    planet = InjectedPlanet(period, clear_t0(period, duration), depth, duration)
    star = SyntheticStar(StarTarget("depth test"), [planet], noise_ppm=noise, gaps=GAPS, seed=seed)
    assert transits_clear_of_edges(star)
    cleaned = clean_light_curve(make_light_curve(star))
    assert fitted_depth(cleaned.lc, planet) == pytest.approx(depth, rel=0.05)


def test_two_planets_both_kept():
    b = InjectedPlanet(2.43, clear_t0(2.43, 2.0), 0.003, 2.0)
    c = InjectedPlanet(9.87, clear_t0(9.87, 3.8), 0.005, 3.8)
    star = SyntheticStar(StarTarget("two"), [b, c], noise_ppm=250, gaps=GAPS, seed=5)
    assert transits_clear_of_edges(star)
    cleaned = clean_light_curve(make_light_curve(star))
    for planet in (b, c):
        assert fitted_depth(cleaned.lc, planet) == pytest.approx(planet.depth, rel=0.05)


def test_trend_removed_to_white_noise():
    star = SyntheticStar(StarTarget("quiet"), [], noise_ppm=250, trend_amplitude=0.005,
                         n_outliers=0, gaps=GAPS, seed=7)
    raw = make_light_curve(star)
    cleaned = clean_light_curve(raw)
    assert robust_std(raw.flux - 1) > 5 * 250e-6
    assert robust_std(cleaned.lc.flux - 1) == pytest.approx(250e-6, rel=0.1)
    assert np.median(cleaned.lc.flux) == pytest.approx(1.0, abs=2e-5)


def test_upward_outliers_clipped_dips_kept():
    star = SyntheticStar(StarTarget("flares"), [], noise_ppm=300, n_outliers=0, seed=3)
    raw = make_light_curve(star)
    rng = np.random.default_rng(0)
    up = rng.choice(len(raw), 25, replace=False)
    down = np.setdiff1d(rng.choice(len(raw), 25, replace=False), up)
    raw.flux[up] += 0.003  # 10 sigma flares / cosmic rays
    raw.flux[down] -= 0.003  # single low points are not clipped (they may be transit data)
    cleaned = clean_light_curve(raw)
    assert not cleaned.mask[up].any()
    assert cleaned.mask[down].all()
    assert (~cleaned.mask).sum() <= len(up) + 3
    assert any("upward outliers" in line for line in cleaned.cleaning_log)


def test_bad_points_removed_and_masked():
    star = SyntheticStar(StarTarget("bad"), [], seed=4)
    raw = make_light_curve(star)
    raw.flux[[10, 20]] = np.nan
    raw.flux[30] = -1.0
    raw.time[40] = np.nan
    raw.flux_err[50] = np.nan  # a missing error alone keeps the point
    cleaned = clean_light_curve(raw)
    assert not cleaned.mask[[10, 20, 30, 40]].any()
    assert cleaned.mask[50]
    assert np.all(np.isfinite(cleaned.lc.flux))
    assert np.all(np.isfinite(cleaned.lc.flux_err)) and np.all(cleaned.lc.flux_err > 0)
    assert len(cleaned.trend) == len(raw)
    assert np.isnan(cleaned.trend[[10, 20, 30, 40]]).all()
    assert any("Removed 4 of" in line for line in cleaned.cleaning_log)


def test_segments_normalised_separately_and_trend_in_raw_units():
    # Two "sectors" at very different count rates, as raw SAP-like flux would be.
    star = SyntheticStar(StarTarget("sectors"), [], noise_ppm=200, gaps=[(25.0, 29.0)], seed=8)
    raw = make_light_curve(star)
    second = raw.time > 27
    raw.flux = raw.flux * np.where(second, 1500.0, 1000.0)
    raw.flux_err = raw.flux_err * np.where(second, 1500.0, 1000.0)
    cleaned = clean_light_curve(raw)
    lc = cleaned.lc
    for part in (lc.time < 27, lc.time > 27):
        assert np.median(lc.flux[part]) == pytest.approx(1.0, abs=1e-4)
    assert robust_std(lc.flux - 1) == pytest.approx(200e-6, rel=0.15)
    kept = cleaned.mask
    np.testing.assert_allclose(raw.flux[kept] / cleaned.trend[kept], lc.flux, rtol=1e-12)
    assert lc.flux_err == pytest.approx(200e-6, rel=0.05)
    assert any("2 segment(s)" in line for line in cleaned.cleaning_log)


def test_unsorted_input_mask_in_raw_order():
    star = SyntheticStar(StarTarget("shuffled"), [InjectedPlanet(3.0, 1.0, 0.004, 3.0)], seed=9)
    raw = make_light_curve(star)
    perm = np.random.default_rng(1).permutation(len(raw))
    shuffled = LightCurve(raw.time[perm], raw.flux[perm], raw.flux_err[perm], "Synthetic")
    a = clean_light_curve(raw)
    b = clean_light_curve(shuffled)
    assert np.all(np.diff(b.lc.time) > 0)
    np.testing.assert_allclose(b.lc.flux, a.lc.flux)
    np.testing.assert_array_equal(b.mask, a.mask[perm])
    np.testing.assert_allclose(b.trend, a.trend[perm])


def test_metadata_carried_over():
    raw = make_light_curve(SyntheticStar(StarTarget("meta"), [], seed=2))
    raw.mission, raw.sectors_or_quarters, raw.dois = "TESS", [14, 15], ["10.17909/t9-nmc8-f686"]
    raw.source_ids, raw.author, raw.cadence_s = ["obs1"], "SPOC", 120.0
    lc = clean_light_curve(raw).lc
    assert (lc.mission, lc.sectors_or_quarters, lc.dois) == ("TESS", [14, 15], ["10.17909/t9-nmc8-f686"])
    assert (lc.source_ids, lc.author, lc.cadence_s) == (["obs1"], "SPOC", 120.0)


def test_window_follows_longest_duration_and_can_be_set():
    raw = make_light_curve(SyntheticStar(StarTarget("w"), [], seed=2))
    default = clean_light_curve(raw)
    assert any("window 39.0 h" in line for line in default.cleaning_log)
    shorter = clean_light_curve(raw, max_duration_hours=5.0)
    assert any("window 15.0 h" in line for line in shorter.cleaning_log)
    explicit = clean_light_curve(raw, window_days=1.0)
    assert any("window 24.0 h" in line for line in explicit.cleaning_log)
    with pytest.raises(ValueError):
        clean_light_curve(raw, window_days=0)


def test_no_usable_points_raises():
    lc = LightCurve(np.arange(5.0), np.full(5, np.nan), np.ones(5), "Synthetic")
    with pytest.raises(ValueError, match="no usable points"):
        clean_light_curve(lc)


def test_tiny_light_curve():
    lc = LightCurve(np.arange(4.0) * 0.01, np.array([1.0, 1.001, 0.999, 1.0]),
                    np.full(4, 1e-3), "Synthetic")
    cleaned = clean_light_curve(lc)
    assert len(cleaned.lc) == 4
    assert np.all(np.isfinite(cleaned.lc.flux))


def test_progress_reported_in_order():
    raw = make_light_curve(SyntheticStar(StarTarget("p"), [], seed=2))
    calls = []
    clean_light_curve(raw, progress_cb=lambda m, f: calls.append((m, f)))
    fractions = [f for _, f in calls]
    assert fractions[0] == 0.0 and fractions[-1] == 1.0
    assert fractions == sorted(fractions)


def test_biweight_ignores_outliers():
    x = np.concatenate([np.random.default_rng(0).normal(1.0, 0.01, 200), np.full(20, 5.0)])
    assert biweight_location(x) == pytest.approx(1.0, abs=0.003)


def test_biweight_poly_follows_slope_at_one_sided_edge():
    dt = np.linspace(0.0, 1.0, 100)
    x = 1.0 + 0.01 * dt + np.random.default_rng(0).normal(0, 1e-4, 100)
    assert biweight_poly(dt, x, degree=1) == pytest.approx(1.0, abs=1e-4)
    assert biweight_location(x) == pytest.approx(1.005, abs=5e-4)  # a constant would lag


def test_split_segments():
    t = np.array([0.0, 0.1, 0.2, 2.0, 2.1, 5.0])
    assert split_segments(t, 0.5) == [slice(0, 3), slice(3, 5), slice(5, 6)]
    assert split_segments(np.empty(0), 0.5) == []


def test_find_dips_marks_transit_not_neighbours():
    planet = InjectedPlanet(4.0, 2.0, 0.002, 3.0)
    star = SyntheticStar(StarTarget("d"), [planet], noise_ppm=200, trend_amplitude=0, n_outliers=0, seed=1)
    lc = make_light_curve(star)
    dips = find_dips(lc.time, lc.flux - 1.0, 13 / 24, np.median(np.diff(lc.time)))
    in_transit = trapezoid_transit(lc.time, planet) < 1
    assert dips[in_transit].mean() > 0.95
    assert dips[~in_transit].mean() < 0.05
