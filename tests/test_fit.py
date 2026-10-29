"""Transit model and least-squares fit.

Recovery tolerances against the synthetic (trapezoid) transits:
period within 0.1%, T14 within 10%, and Rp/Rs within 10% of sqrt(depth)
(20% for the shallow case, whose b and a/rs are poorly constrained).
sqrt(depth) is only approximate for a limb-darkened fit to a flat-bottomed
trapezoid (the fit's own depth is checked within 10%).  Against data made
with the fit's own model the tolerances are tight: 2%.  Limb darkening
and the stellar-density prior are checked against data made with the
fit's own model for a given star.
"""

import numpy as np
import pytest

from pipeline_synth import flat_cleaned, injected_candidate
from transitory.models import CleanedLightCurve, LightCurve, StarTarget, TransitCandidate
from transitory.pipeline.fit import a_rs_from_density, fit_transit, limb_darkening
from transitory.pipeline.transit_model import (DEFAULT_U1, DEFAULT_U2, occulted_fraction, t14_hours,
                                               transit_flux)
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


# --- limb darkening and the stellar-density prior ---

def star_cleaned(mission, star, period, k, b, noise, baseline=54.0, cadence_s=120.0, seed=3):
    """Data made with the fit's own model, for ``star`` seen by ``mission``; returns (cleaned, candidate, a, t14)."""
    a = a_rs_from_density(period, star.mass_msun, star.radius_rsun)
    ld = limb_darkening(mission, star.teff_k)
    t14 = t14_hours(period, k, a, b)
    time = np.arange(0.0, baseline, cadence_s / 86400.0)
    rng = np.random.default_rng(seed)
    smear = dict(exposure_days=cadence_s / 86400.0, supersample=9) if cadence_s >= 900 else {}
    flux = transit_flux(time, 1.0, period, k, a, b, *ld, **smear) + rng.normal(0, noise, time.shape)
    lc = LightCurve(time=time, flux=flux, flux_err=np.full_like(time, noise), mission=mission,
                    cadence_s=cadence_s)
    cleaned = CleanedLightCurve(lc=lc, trend=np.ones_like(time), mask=np.ones(len(time), dtype=bool))
    cand = TransitCandidate(period=period, t0=1.0, duration=0.8 * t14 / 24.0, depth=k * k,
                            snr=30.0, sde=20.0, n_transits=int(baseline / period))
    return cleaned, cand, a, t14


def test_limb_darkening_by_mission_and_teff():
    assert limb_darkening("TESS", 3500) == pytest.approx((0.20, 0.38))
    assert limb_darkening("Kepler", 4500) == pytest.approx((0.59, 0.12))
    assert limb_darkening("TESS", 3750) == pytest.approx((0.28, 0.31))  # interpolated
    assert limb_darkening("Kepler", 9000) == pytest.approx((0.27, 0.31))  # held at the grid's end
    # A Sun-like star in Kepler's band is close to the old fixed defaults.
    assert limb_darkening("Kepler", 5750) == pytest.approx((DEFAULT_U1, DEFAULT_U2), abs=0.02)
    for mission, teff in [("TESS", None), ("Synthetic", 3500), ("TESS", float("nan"))]:
        assert limb_darkening(mission, teff) == (DEFAULT_U1, DEFAULT_U2)


def test_a_rs_from_density():
    assert a_rs_from_density(365.25, 1.0, 1.0) == pytest.approx(215.0, rel=2e-3)  # 1 AU in R_sun
    # a/Rs scales as rho^(1/3) P^(2/3).
    assert a_rs_from_density(8 * 365.25, 8.0, 2.0) == pytest.approx(215.0 * 4 * 2 / 2, rel=2e-3)
    assert a_rs_from_density(3.0, None, 1.0) is None
    assert a_rs_from_density(3.0, 1.0, 0.0) is None


@pytest.mark.parametrize("mission,teff,mass,radius", [
    pytest.param("TESS", 3200, 0.25, 0.27, id="tess-m-dwarf"),
    pytest.param("Kepler", 4500, 0.70, 0.68, id="kepler-k-dwarf"),
])
def test_fit_uses_mission_limb_darkening(mission, teff, mass, radius):
    star = StarTarget(name="star", teff_k=teff)
    cleaned, cand, _, _ = star_cleaned(mission, StarTarget(name="star", teff_k=teff, mass_msun=mass,
                                                           radius_rsun=radius),
                                       period=3.3, k=0.1, b=0.3, noise=300e-6, baseline=27.0)
    fit = fit_transit(cleaned, cand, star)
    assert fit.limb_darkening == pytest.approx(limb_darkening(mission, teff))
    assert fit.a_rs_prior is None  # no mass or radius
    assert abs(fit.rp_rs - 0.1) < 3 * fit.uncertainties["rp_rs"]
    # The Sun-like defaults are measurably wrong for this star: rp/rs biased by ~2-3%, many sigma.
    sun_like = fit_transit(cleaned, cand)
    assert sun_like.limb_darkening == (DEFAULT_U1, DEFAULT_U2)
    assert abs(sun_like.rp_rs / 0.1 - 1) > 0.015
    assert abs(sun_like.rp_rs - 0.1) > 5 * sun_like.uncertainties["rp_rs"]
    assert sun_like.chi2 > fit.chi2


@pytest.mark.parametrize("mission,star,period,k,b,noise,cadence_s", [
    # Shallow transit on a Sun-like star: b and a/rs are poorly constrained.
    pytest.param("TESS", StarTarget(name="G", teff_k=5800, mass_msun=1.0, radius_rsun=1.0),
                 4.1, 0.022, 0.3, 1000e-6, 120.0, id="shallow-sun-like"),
    # TOI-270 c-like: M dwarf, 30-minute full-frame-image cadence.
    pytest.param("TESS", StarTarget(name="M", teff_k=3506, mass_msun=0.39, radius_rsun=0.38),
                 5.66, 0.058, 0.2, 500e-6, 1800.0, id="m-dwarf-ffi"),
])
def test_density_prior_breaks_rp_rs_b_degeneracy(mission, star, period, k, b, noise, cadence_s):
    errors = {"free": [], "prior": []}
    for seed in range(8):
        cleaned, cand, a, t14 = star_cleaned(mission, star, period, k, b, noise,
                                             cadence_s=cadence_s, seed=seed)
        free = fit_transit(cleaned, cand, StarTarget(name=star.name, teff_k=star.teff_k))
        prior = fit_transit(cleaned, cand, star)
        assert free.a_rs_prior is None
        assert prior.a_rs_prior == pytest.approx(a)
        assert prior.a_rs == pytest.approx(a, rel=0.1)
        assert prior.chi2 == pytest.approx(1.0, abs=0.25)  # ~100 in-window points at 30-min cadence
        errors["free"].append(abs(free.rp_rs / k - 1))
        errors["prior"].append(abs(prior.rp_rs / k - 1))
        assert prior.duration_hours == pytest.approx(t14, rel=0.08)
    # Without the prior some fits slide to high b and inflate rp/rs; with it none do.
    assert max(errors["free"]) > 0.08
    assert max(errors["prior"]) < 0.06
    assert np.mean(errors["prior"]) < 0.5 * np.mean(errors["free"])


def test_density_prior_ignored_when_out_of_range():
    # A density giving a/rs below the fit's lower bound (a planet inside its star) is not used.
    star = StarTarget(name="odd", teff_k=5800, mass_msun=1.0, radius_rsun=1.0)
    cleaned, cand, _, _ = star_cleaned("TESS", star, period=3.3, k=0.1, b=0.3, noise=300e-6, baseline=27.0)
    bad = StarTarget(name="odd", teff_k=5800, mass_msun=1e-4, radius_rsun=50.0)
    fit = fit_transit(cleaned, cand, bad)
    assert fit.a_rs_prior is None
    assert fit.rp_rs == pytest.approx(0.1, rel=0.03)
