"""Least-squares fit of the transit model to a BLS candidate.

Fits t0, P, rp/rs, a/rs and b with ``scipy.optimize.least_squares``, with
the quadratic limb darkening held fixed: looked up for the mission's
bandpass and the star's Teff when both are known, else the model's
Sun-like defaults.  When the star's mass and radius are known, a Gaussian
prior ties a/rs to the stellar density (circular orbit), which breaks the
rp/rs - b - a/rs degeneracy of shallow or grazing-looking transits.  Only
points near the predicted transits enter the fit.  The reference epoch is
moved to the middle of the data, where t0 and P are least correlated.  The
fit is started from a few impact parameters, because b and a/rs are nearly
degenerate at low SNR, and the best start is kept.

Uncertainties come from the Jacobian covariance, with the data's rows
scaled by the reduced chi-squared and by the red-noise factor beta of
the light curve's residuals on the T14 timescale (``noise.beta_factor``).
The error on P and t0 is at least what a straight line through
individually fitted transit times gives, which also covers
transit-timing variations.  T14 and depth are derived,
with their errors propagated through the covariance.
"""

from __future__ import annotations

import logging

import numpy as np
from scipy.optimize import least_squares

from ..models import CleanedLightCurve, FitResult, Mission, StarTarget, TransitCandidate
from .noise import beta_factor, robust_std
from .transit_model import DEFAULT_U1, DEFAULT_U2, t14_hours, transit_flux

log = logging.getLogger(__name__)

PARAMS = ("t0", "period", "rp_rs", "a_rs", "b")

# Quadratic limb-darkening coefficients (u1, u2) by Teff for dwarfs
# (log g 4.5, solar metallicity), linearly interpolated in Teff and held at
# the end values outside the grid.  TESS: Claret (2017), A&A 600, A30
# (VizieR J/A+A/600/A30); Kepler: Claret & Bloemen (2011), A&A 529, A75
# (VizieR J/A+A/529/A75).  The entries are rounded to 0.01 and approximate;
# limb darkening is uncertain to a few hundredths anyway.  log g is not
# used: across dwarfs it moves the coefficients by less than the rounding.
_LD_TEFF = np.array([3000.0, 3500.0, 4000.0, 4500.0, 5000.0, 5500.0, 6000.0, 6500.0, 7000.0])
_LD_TABLES: dict[str, np.ndarray] = {
    "TESS": np.array([(0.15, 0.44), (0.20, 0.38), (0.36, 0.24), (0.44, 0.17), (0.42, 0.19),
                      (0.37, 0.22), (0.31, 0.25), (0.26, 0.28), (0.22, 0.30)]),
    "Kepler": np.array([(0.30, 0.38), (0.38, 0.32), (0.52, 0.18), (0.59, 0.12), (0.52, 0.18),
                        (0.45, 0.22), (0.38, 0.26), (0.32, 0.29), (0.27, 0.31)]),
}
_LD_TABLES["K2"] = _LD_TABLES["Kepler"]  # the same telescope and bandpass

# Fractional 1-sigma on the stellar density from catalog masses and radii
# (TIC: ~7% in R, ~12% in M); a/rs goes as density^(1/3), so ~10% on a/rs,
# loose enough to absorb modest eccentricity.
DENSITY_FRACTIONAL_ERR = 0.30
_GM_SUN = 1.32712440018e20  # m^3 s^-2
_R_SUN = 6.957e8  # m

# Points within this many BLS durations of a transit centre enter the fit.
FIT_WINDOW_DURATIONS = 2.0
# Cadences at least this long (s) are integrated over the exposure.
SUPERSAMPLE_MIN_CADENCE_S = 900.0
SUPERSAMPLE = 5
_START_B = (0.1, 0.5, 0.85)
_MODEL_POINTS = 2000

# Per-transit timing: a transit's time counts if it is better than this
# fraction of T14, and the line through the times needs this many of them.
_TIMING_MAX_ERR_T14 = 0.1
_TIMING_MIN_TRANSITS = 4


def _fit_data(cleaned: CleanedLightCurve, candidate: TransitCandidate):
    lc = cleaned.lc
    time = np.asarray(lc.time, dtype=float)
    flux = np.asarray(lc.flux, dtype=float)
    err = np.asarray(lc.flux_err, dtype=float) if lc.flux_err is not None else np.full_like(flux, np.nan)
    good = np.isfinite(time) & np.isfinite(flux)
    time, flux, err = time[good], flux[good], err[good]

    period, t0 = candidate.period, candidate.t0
    epoch = np.round((time - t0) / period)
    near = np.abs(time - (t0 + epoch * period)) < FIT_WINDOW_DURATIONS * candidate.duration
    if np.sum(near) < 10:
        raise ValueError(f"only {int(np.sum(near))} points near the candidate's transits")
    time, flux, err = time[near], flux[near], err[near]

    # Errors: the reported ones when usable, else the scatter of the data.
    if not np.all(np.isfinite(err) & (err > 0)):
        err = np.full_like(flux, robust_std(flux))
    return time, flux, err


def limb_darkening(mission: Mission | str, teff_k: float | None) -> tuple[float, float]:
    """Quadratic (u1, u2) for ``mission``'s bandpass and ``teff_k``; the Sun-like defaults if either is unknown."""
    table = _LD_TABLES.get(mission)
    if table is None or teff_k is None or not np.isfinite(teff_k):
        return DEFAULT_U1, DEFAULT_U2
    return (float(np.interp(teff_k, _LD_TEFF, table[:, 0])),
            float(np.interp(teff_k, _LD_TEFF, table[:, 1])))


def a_rs_from_density(period: float, mass_msun: float | None, radius_rsun: float | None) -> float | None:
    """a/Rs of a circular orbit of ``period`` days around the star; None without mass and radius."""
    if not mass_msun or not radius_rsun or mass_msun <= 0 or radius_rsun <= 0:
        return None
    period_s = period * 86400.0
    a_m = (_GM_SUN * mass_msun * period_s ** 2 / (4.0 * np.pi ** 2)) ** (1.0 / 3.0)
    return float(a_m / (radius_rsun * _R_SUN))


def _model(params: np.ndarray, time: np.ndarray, exposure: float, ld: tuple[float, float]) -> np.ndarray:
    t0, period, rp_rs, a_rs, b = params
    if exposure > 0:
        return transit_flux(time, t0, period, rp_rs, a_rs, b, *ld,
                            exposure_days=exposure, supersample=SUPERSAMPLE)
    return transit_flux(time, t0, period, rp_rs, a_rs, b, *ld)


def _t14_and_depth(params: np.ndarray, ld: tuple[float, float]) -> np.ndarray:
    t0, period, rp_rs, a_rs, b = params
    depth = 1.0 - float(transit_flux(np.array([t0]), t0, period, rp_rs, a_rs, b, *ld)[0])
    return np.array([t14_hours(period, rp_rs, a_rs, b), depth])


def _derived_errors(params: np.ndarray, cov: np.ndarray, ld: tuple[float, float]) -> np.ndarray:
    """1-sigma errors on (T14, depth) by linear propagation through ``cov``."""
    grad = np.zeros((2, len(params)))
    for i, value in enumerate(params):
        step = max(abs(value), 1e-3) * 1e-5
        hi, lo = params.copy(), params.copy()
        hi[i] += step
        lo[i] -= step
        grad[:, i] = (_t14_and_depth(hi, ld) - _t14_and_depth(lo, ld)) / (2.0 * step)
    var = np.einsum("ij,jk,ik->i", grad, cov, grad)
    return np.sqrt(np.clip(var, 0.0, None))


def _light_curve_beta(cleaned: CleanedLightCurve, params: np.ndarray, exposure: float,
                      ld: tuple[float, float]) -> float:
    """Red-noise beta of the whole light curve's residuals from the best model, on the T14 timescale."""
    lc = cleaned.lc
    time = np.asarray(lc.time, dtype=float)
    flux = np.asarray(lc.flux, dtype=float)
    good = np.isfinite(time) & np.isfinite(flux)
    time, flux = time[good], flux[good]
    # The model takes the transits out, so every point counts as out of transit.
    resid = flux - _model(params, time, exposure, ld)
    t14 = t14_hours(*params[1:]) / 24.0
    return beta_factor(time, resid, t14)


def _transit_times(time: np.ndarray, flux: np.ndarray, err: np.ndarray, params: np.ndarray,
                   exposure: float, ld: tuple[float, float], scale: float):
    """Mid-times of individual transits with the shape held at ``params``.

    Returns (epochs, times, errors) for transits covered on both sides of
    the centre whose timing error, inflated by ``scale``, is under
    ``_TIMING_MAX_ERR_T14`` of T14.
    """
    t0, period = float(params[0]), float(params[1])
    t14 = t14_hours(period, *params[2:]) / 24.0
    epochs = np.round((time - t0) / period).astype(int)
    out_n, out_t, out_e = [], [], []
    for n in np.unique(epochs):
        sel = epochs == n
        tt, ff, ee = time[sel], flux[sel], err[sel]
        centre = t0 + n * period
        # Ingress and egress both sampled, so the time is set by both edges.
        if not (np.any(tt < centre - 0.25 * t14) and np.any(tt > centre + 0.25 * t14)):
            continue

        def resid(x, tt=tt, ff=ff, ee=ee):
            p = params.copy()
            p[0] = x[0]
            return (ff - _model(p, tt, exposure, ld)) / ee

        lo, hi = centre - 0.5 * t14, centre + 0.5 * t14
        try:
            res = least_squares(resid, [centre], bounds=([lo], [hi]), x_scale=[t14 * 0.01])
        except (ValueError, np.linalg.LinAlgError) as exc:
            log.debug("timing of transit %d failed: %s", n, exc)
            continue
        info = float(res.jac[:, 0] @ res.jac[:, 0])
        if res.status <= 0 or info <= 0:
            continue
        sigma = scale / np.sqrt(info)
        at_bound = min(res.x[0] - lo, hi - res.x[0]) < 1e-3 * t14
        if sigma < _TIMING_MAX_ERR_T14 * t14 and not at_bound:
            out_n.append(int(n))
            out_t.append(float(res.x[0]))
            out_e.append(float(sigma))
    return np.array(out_n), np.array(out_t), np.array(out_e)


def timing_ephemeris_errors(epochs: np.ndarray, times: np.ndarray,
                            errors: np.ndarray) -> tuple[float, float] | None:
    """(t0 error, period error) of a weighted line through transit times, epoch 0 as reference.

    The formal errors are scaled up by the line's reduced chi-squared when
    it exceeds 1 (timing noise or TTVs).  None with too few transits or
    fewer than two distinct epochs.
    """
    if len(epochs) < _TIMING_MIN_TRANSITS or len(np.unique(epochs)) < 2:
        return None
    w = 1.0 / errors ** 2
    design = np.column_stack([np.ones_like(times), epochs.astype(float)])
    normal = design.T @ (design * w[:, None])
    coef = np.linalg.solve(normal, design.T @ (w * times))
    cov = np.linalg.inv(normal)
    chi2_red = float(np.sum(w * (times - design @ coef) ** 2) / (len(times) - 2))
    cov *= max(chi2_red, 1.0)
    return float(np.sqrt(cov[0, 0])), float(np.sqrt(cov[1, 1]))


def fit_transit(cleaned: CleanedLightCurve, candidate: TransitCandidate,
                star: StarTarget | None = None) -> FitResult:
    """Fit t0, period, rp/rs, a/rs and b starting from ``candidate``.

    ``star``, when given, supplies Teff for the limb darkening and mass and
    radius for the a/rs prior.  Uncertainties come from the Jacobian
    covariance; ``model_time`` / ``model_flux`` sample the best model over
    one phase (days from t0).
    """
    time, flux, err = _fit_data(cleaned, candidate)
    cadence_s = cleaned.lc.cadence_s or 0.0
    exposure = cadence_s / 86400.0 if cadence_s >= SUPERSAMPLE_MIN_CADENCE_S else 0.0
    ld = limb_darkening(cleaned.lc.mission, star.teff_k if star else None)

    period = candidate.period
    t_ref = candidate.t0 + np.round((np.median(time) - candidate.t0) / period) * period
    k0 = float(np.clip(np.sqrt(max(candidate.depth, 1e-6)), 1e-3, 0.5))

    # t0 within a BLS duration, P within the phase drift that keeps every
    # transit inside its fit window.
    n_cycles = max((time.max() - time.min()) / period, 1.0)
    dp = candidate.duration / n_cycles
    lower = [t_ref - candidate.duration, period - dp, 1e-4, 1.05, 0.0]
    upper = [t_ref + candidate.duration, period + dp, 0.6, 2000.0, 1.5]

    a_prior = a_rs_from_density(period, star.mass_msun, star.radius_rsun) if star else None
    if a_prior is not None and not lower[3] < a_prior < upper[3]:
        log.warning("%s: density a/rs %.3g is outside the fit's range; not using it", star.name, a_prior)
        a_prior = None
    a_sigma = a_prior * DENSITY_FRACTIONAL_ERR / 3.0 if a_prior is not None else None

    def residuals(params):
        resid = (flux - _model(params, time, exposure, ld)) / err
        if a_prior is None:
            return resid
        return np.append(resid, (params[3] - a_prior) / a_sigma)

    best = None
    for b0 in _START_B:
        if a_prior is not None:
            a0 = a_prior
        else:
            chord = np.sqrt(max((1.0 + k0) ** 2 - b0 * b0, 1e-4))
            a0 = float(np.clip(period / (np.pi * candidate.duration) * chord, 1.2, 1500.0))
        x0 = np.clip([t_ref, period, k0, a0, b0], lower, upper)
        try:
            res = least_squares(residuals, x0, bounds=(lower, upper), x_scale="jac", method="trf")
        except (ValueError, np.linalg.LinAlgError) as exc:
            log.warning("fit start b=%.2f failed: %s", b0, exc)
            continue
        if res.status > 0 and (best is None or res.cost < best.cost):
            best = res
    if best is None:
        raise RuntimeError("transit fit did not converge")

    params = best.x
    t14, depth = _t14_and_depth(params, ld)
    if t14 <= 0 or depth <= 0:
        raise RuntimeError("fitted model does not transit")

    # chi-squared of the data alone.  The data's Jacobian rows are scaled by
    # it and by the red-noise beta; the prior's row is left as it is.
    n_data = len(flux)
    dof = max(n_data - len(params), 1)
    chi2_red = float(np.sum(best.fun[:n_data] ** 2) / dof)
    beta = _light_curve_beta(cleaned, params, exposure, ld)
    noise_scale = np.sqrt(max(chi2_red, 1e-12)) * beta
    jac = best.jac.copy()
    jac[:n_data] /= noise_scale
    cov = np.linalg.pinv(jac.T @ jac)
    errors = np.sqrt(np.clip(np.diag(cov), 0.0, None))

    timing = timing_ephemeris_errors(*_transit_times(time, flux, err, params, exposure, ld, noise_scale))
    if timing is not None:
        # t0 is the transit nearest mid-data, so the line's epoch 0 is t0.
        for i, timing_err in enumerate(timing):
            if timing_err > errors[i] > 0:
                cov[i, :] *= timing_err / errors[i]
                cov[:, i] *= timing_err / errors[i]
                errors[i] = timing_err
    log.debug("fit errors: chi2_red %.2f, beta %.2f, timing errors %s", chi2_red, beta, timing)
    t14_err, depth_err = _derived_errors(params, cov, ld)
    uncertainties = {name: float(e) for name, e in zip(PARAMS, errors)}
    uncertainties.update(duration_hours=float(t14_err), depth=float(depth_err))

    t0, period, rp_rs, a_rs, b = (float(v) for v in params)
    half = 0.5 * t14 / 24.0
    model_time = np.unique(np.concatenate([
        np.linspace(-period / 2.0, period / 2.0, _MODEL_POINTS // 2),
        np.linspace(-3.0 * half, 3.0 * half, _MODEL_POINTS // 2),
    ]))
    model_flux = transit_flux(model_time + t0, t0, period, rp_rs, a_rs, b, *ld)

    return FitResult(
        period=period, t0=t0, rp_rs=rp_rs, a_rs=a_rs, b=b,
        duration_hours=float(t14), depth=float(depth),
        uncertainties=uncertainties, chi2=chi2_red,
        limb_darkening=ld, a_rs_prior=a_prior, beta=beta,
        model_time=model_time, model_flux=model_flux,
    )
