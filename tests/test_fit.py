"""Transit model and least-squares fit.

Recovery tolerances against the synthetic (trapezoid) transits:
period within 0.1%, T14 within 10%, and Rp/Rs within 10% of sqrt(depth)
(20% for the shallow case, whose b and a/rs are poorly constrained).
sqrt(depth) is only approximate for a limb-darkened fit to a flat-bottomed
trapezoid (the fit's own depth is checked within 10%).  Against data made
with the fit's own model the tolerances are tight: 2%.
"""

import numpy as np
import pytest

from pipeline_synth import flat_cleaned, injected_candidate
from transitory.models import CleanedLightCurve, LightCurve
from transitory.pipeline.fit import fit_transit
from transitory.pipeline.transit_model import occulted_fraction, t14_hours, transit_flux
from transitory.synthetic import InjectedPlanet


def uniform_overlap(z, p):
    """Exact area of a disk of radius p at distance z overlapping the unit disk, over pi."""
    k0 = np.arccos((p * p + z * z - 1) / (2 * p * z))
    k1 = np.arccos((1 - p * p + z * z) / (2 * z))
    return (p * p * k0 + k1 - 0.5 * np.sqrt(4 * z * z - (1 + z * z - p * p) ** 2)) / np.pi


# --- transit model ---

@pytest.mark.parametrize("z,p", [(0.95, 0.1), (1.0, 0.1), (1.05, 0.1), (0.6, 0.5), (0.9, 0.5)])
def test_uniform_disk_partial_overlap_is_exact(z, p):
    assert occulted_fraction(np.array([z]), p, 0.0, 0.0)[0] == pytest.approx(uniform_overlap(z, p), rel=1e-4)


def test_uniform_disk_full_overlap_is_area_ratio():
    assert occulted_fraction(np.array([0.0, 0.3, 0.85]), 0.1, 0.0, 0.0) == pytest.approx(0.01, rel=1e-9)
    assert occulted_fraction(np.array([0.2]), 0.5, 0.0, 0.0)[0] == pytest.approx(0.25, rel=1e-4)


def test_limb_darkening_deepens_central_transit():
    u1, u2 = 0.4, 0.25
    expected = 0.01 / (1 - u1 / 3 - u2 / 6)  # small planet: p^2 * I(centre) / mean intensity
    assert occulted_fraction(np.array([0.0]), 0.1, u1, u2)[0] == pytest.approx(expected, rel=2e-3)
    # Near the limb the star is darker, so the same planet blocks less.
    centre, limb = occulted_fraction(np.array([0.0, 0.85]), 0.1, u1, u2)
    assert limb < centre


def test_flux_is_one_out_of_transit_and_symmetric():
    t = np.linspace(-0.5, 0.5, 2001)
    f = transit_flux(t, 0.0, 3.0, 0.1, 10.0, 0.3)
    assert np.all(f <= 1.0)
    assert f[0] == 1.0 and f[-1] == 1.0
    assert np.allclose(f, f[::-1], atol=1e-12)
    # Secondary eclipse phase: planet behind the star, no dip.
    assert transit_flux(np.array([1.5]), 0.0, 3.0, 0.1, 10.0, 0.3)[0] == 1.0


def test_model_duration_matches_t14():
    period, k, a, b = 3.0, 0.1, 10.0, 0.3
    t = np.linspace(-0.2, 0.2, 400001)
    dipping = t[transit_flux(t, 0.0, period, k, a, b) < 1.0]
    measured = (dipping.max() - dipping.min()) * 24
    assert measured == pytest.approx(t14_hours(period, k, a, b), abs=0.01)
    assert t14_hours(period, k, a, b) == pytest.approx(
        period / np.pi * np.arcsin(np.sqrt((1 + k) ** 2 - b * b) / (a * np.sqrt(1 - (b / a) ** 2))) * 24)


def test_t14_zero_when_not_transiting():
    assert t14_hours(3.0, 0.1, 10.0, 1.2) == 0.0
    assert t14_hours(3.0, 0.1, 0.9, 0.0) == 0.0


def test_exposure_smearing_keeps_area_but_lowers_depth():
    t = np.linspace(-0.15, 0.15, 3001)
    sharp = transit_flux(t, 0.0, 3.0, 0.1, 10.0, 0.3)
    smeared = transit_flux(t, 0.0, 3.0, 0.1, 10.0, 0.3, exposure_days=30 / 1440, supersample=15)
    assert smeared.min() > sharp.min()
    assert np.sum(1 - smeared) == pytest.approx(np.sum(1 - sharp), rel=1e-3)


# --- fit ---

def model_cleaned(period, t0, k, a, b, noise=300e-6, seed=3) -> CleanedLightCurve:
    time = np.arange(0.0, 54.0, 10 / 1440)
    rng = np.random.default_rng(seed)
    flux = transit_flux(time, t0, period, k, a, b) + rng.normal(0, noise, time.shape)
    lc = LightCurve(time=time, flux=flux, flux_err=np.full_like(time, noise), mission="Synthetic",
                    cadence_s=600.0)
    return CleanedLightCurve(lc=lc, trend=np.ones_like(time), mask=np.ones(len(time), dtype=bool))


def test_fit_recovers_own_model_parameters():
    period, t0, k, a, b = 4.3, 1.7, 0.08, 12.0, 0.3
    t14 = t14_hours(period, k, a, b)
    cand = injected_candidate(InjectedPlanet(period, t0, k * k, t14))
    cand.period *= 1.0003  # a BLS-like starting error
    cand.t0 += 0.01
    fit = fit_transit(model_cleaned(period, t0, k, a, b), cand)
    assert fit.period == pytest.approx(period, rel=1e-4)
    assert fit.rp_rs == pytest.approx(k, rel=0.02)
    assert fit.duration_hours == pytest.approx(t14, rel=0.02)
    assert fit.chi2 == pytest.approx(1.0, abs=0.1)
    # Reported errors are sane: positive and consistent with the truth.
    for name, truth in [("period", period), ("rp_rs", k), ("duration_hours", t14)]:
        err = fit.uncertainties[name]
        assert err > 0 and abs(getattr(fit, name) - truth) < 4 * err
    # t0 is moved to the transit nearest mid-data but stays on the ephemeris.
    epochs = (fit.t0 - t0) / period
    assert abs(epochs - round(epochs)) * period * 24 < 0.05
    assert abs(fit.t0 - 27.0) <= period


@pytest.mark.parametrize("planet,k_tol", [
    pytest.param(InjectedPlanet(3.21, 1.3, 0.010, 2.9), 0.10, id="deep-hot-jupiter"),
    pytest.param(InjectedPlanet(5.74, 2.1, 0.0009, 2.4), 0.10, id="super-earth"),
    pytest.param(InjectedPlanet(4.1, 1.0, 0.0005, 2.5), 0.20, id="shallow-500ppm"),
    pytest.param(InjectedPlanet(0.46, 0.1, 0.004, 0.5), 0.10, id="ultra-short-period"),
    pytest.param(InjectedPlanet(21.6, 7.0, 0.004, 5.5), 0.10, id="long-period"),
])
def test_fit_recovers_injected_trapezoid(planet, k_tol):
    cand = injected_candidate(planet)
    fit = fit_transit(flat_cleaned([planet]), cand)
    assert fit.period == pytest.approx(planet.period, rel=1e-3)
    assert fit.rp_rs == pytest.approx(np.sqrt(planet.depth), rel=k_tol)
    assert fit.duration_hours == pytest.approx(planet.duration_hours, rel=0.10)
    assert fit.depth == pytest.approx(planet.depth, rel=0.10)
    assert set(fit.uncertainties) >= {"t0", "period", "rp_rs", "a_rs", "b", "duration_hours", "depth"}
    assert all(np.isfinite(v) and v >= 0 for v in fit.uncertainties.values())


def test_fit_model_curve_covers_one_phase():
    planet = InjectedPlanet(3.21, 1.3, 0.010, 2.9)
    fit = fit_transit(flat_cleaned([planet]), injected_candidate(planet))
    assert len(fit.model_time) == len(fit.model_flux) > 100
    assert np.all(np.diff(fit.model_time) > 0)
    assert fit.model_time[0] == pytest.approx(-fit.period / 2)
    assert fit.model_time[-1] == pytest.approx(fit.period / 2)
    assert 1 - fit.model_flux.min() == pytest.approx(fit.depth, rel=0.01)
    assert fit.model_flux[0] == 1.0


def test_fit_uses_scatter_when_errors_missing():
    planet = InjectedPlanet(3.21, 1.3, 0.010, 2.9)
    cleaned = flat_cleaned([planet])
    cleaned.lc.flux_err = np.full_like(cleaned.lc.flux, np.nan)
    fit = fit_transit(cleaned, injected_candidate(planet))
    assert fit.rp_rs == pytest.approx(0.1, rel=0.1)
    assert np.isfinite(fit.chi2)


def test_fit_needs_points_in_transit():
    planet = InjectedPlanet(3.21, 1.3, 0.010, 2.9)
    cleaned = flat_cleaned([planet], baseline_days=1.0)  # ends before the first transit
    with pytest.raises(ValueError, match="points near"):
        fit_transit(cleaned, injected_candidate(planet))
