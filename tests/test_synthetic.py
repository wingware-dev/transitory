import numpy as np
import pytest

from transitory.models import LightCurve, StarTarget
from transitory.sources import LightCurveSource, SourceError, SyntheticSource
from transitory.synthetic import (
    InjectedPlanet,
    SyntheticStar,
    demo_stars,
    make_light_curve,
    trapezoid_transit,
)


def quiet_star(planets, **kw):
    kw.setdefault("noise_ppm", 0.0)
    kw.setdefault("trend_amplitude", 0.0)
    kw.setdefault("n_outliers", 0)
    return SyntheticStar(target=StarTarget(name="Test Star"), planets=planets, seed=1, **kw)


def test_trapezoid_depth_and_duration():
    planet = InjectedPlanet(period=3.0, t0=1.0, depth=0.01, duration_hours=3.0, ingress_fraction=0.1)
    t = np.array([1.0, 1.0 + 3.0, 1.0 + 1.0 / 24 * 1.6, 2.5])
    flux = trapezoid_transit(t, planet)
    assert flux[0] == pytest.approx(0.99)
    assert flux[1] == pytest.approx(0.99)
    assert flux[2] == pytest.approx(1.0)  # just past T14/2
    assert flux[3] == pytest.approx(1.0)


def test_odd_even_and_secondary():
    planet = InjectedPlanet(period=2.0, t0=0.0, depth=0.02, duration_hours=2.0,
                            odd_even_ratio=0.5, secondary_depth=0.003)
    flux = trapezoid_transit(np.array([0.0, 2.0, 1.0]), planet)
    assert flux[0] == pytest.approx(0.98)
    assert flux[1] == pytest.approx(0.99)
    assert flux[2] == pytest.approx(0.997)


def test_injected_transits_land_at_known_times():
    planet = InjectedPlanet(period=4.0, t0=1.5, depth=0.005, duration_hours=4.0)
    lc = make_light_curve(quiet_star([planet], baseline_days=20.0, cadence_minutes=2.0))
    in_transit = lc.flux < 1.0 - 0.9 * planet.depth
    epochs = np.unique(np.round((lc.time[in_transit] - planet.t0) / planet.period))
    assert list(epochs) == [0, 1, 2, 3, 4]
    assert lc.flux.min() == pytest.approx(1.0 - planet.depth, abs=1e-12)


def test_noise_trend_gaps_and_outliers():
    star = quiet_star([], noise_ppm=500.0, trend_amplitude=0.0, n_outliers=10,
                      baseline_days=30.0, gaps=[(13.0, 15.0)])
    lc = make_light_curve(star)
    assert isinstance(lc, LightCurve)
    assert not np.any((lc.time >= 13.0) & (lc.time < 15.0))
    clipped = lc.flux[lc.flux < 1.0 + 4.5 * 500e-6]
    assert np.std(clipped) == pytest.approx(500e-6, rel=0.1)
    assert np.sum(lc.flux > 1.0 + 5 * 500e-6 * 0.99) >= 10
    assert np.all(lc.flux_err == pytest.approx(500e-6))
    assert lc.mission == "Synthetic"
    assert lc.baseline_days == pytest.approx(30.0, abs=0.01)


def test_red_noise_rms_and_correlation():
    star = quiet_star([], red_noise_ppm=400.0, red_noise_hours=4.0, baseline_days=200.0,
                      gaps=[(50.0, 60.0)])
    lc = make_light_curve(star)
    resid = lc.flux - 1.0
    assert np.std(resid) == pytest.approx(400e-6, rel=0.15)
    # Neighbours 4 h apart are correlated by about 1/e, ones a day apart not at all.
    lag = int(round(4.0 * 6))
    assert np.corrcoef(resid[:-lag], resid[lag:])[0, 1] == pytest.approx(np.exp(-1), abs=0.1)
    assert abs(np.corrcoef(resid[:-144], resid[144:])[0, 1]) < 0.15
    # Off by default, leaving earlier light curves unchanged.
    assert np.all(make_light_curve(quiet_star([])).flux == 1.0)


def test_deterministic_per_seed():
    star = quiet_star([], noise_ppm=300.0)
    a, b = make_light_curve(star), make_light_curve(star)
    np.testing.assert_array_equal(a.flux, b.flux)


def test_synthetic_source_is_a_light_curve_source():
    source = SyntheticSource()
    assert isinstance(source, LightCurveSource)
    names = [t.name for t in source.targets()]
    assert names == [s.target.name for s in demo_stars()]
    assert all(t.ra is not None and t.dec is not None for t in source.targets())


def test_synthetic_source_fetch_and_known_planets():
    source = SyntheticSource()
    target = StarTarget(name="demo two-planet")
    lc = source.fetch(target)
    assert len(lc) > 1000
    known = source.known_planets(target)
    assert [round(k.period, 2) for k in known] == [2.43, 9.87]
    assert known[0].rp_rs == pytest.approx(np.sqrt(0.003))
    assert source.credits(lc) and "Synthetic" in source.credits(lc)[0]


def test_synthetic_source_unknown_targets():
    messages = []
    lc = SyntheticSource().fetch(StarTarget(name="Nobody"), progress_cb=lambda m, f: messages.append(f))
    assert len(lc) > 0 and messages == [0.0, 1.0]
    with pytest.raises(SourceError):
        SyntheticSource(allow_unknown=False).fetch(StarTarget(name="Nobody"))
