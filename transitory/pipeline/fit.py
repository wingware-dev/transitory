"""Least-squares fit of the transit model to a BLS candidate.

Fits t0, P, rp/rs, a/rs and b with ``scipy.optimize.least_squares``, with
the limb darkening fixed at the model's defaults.  Only points near the
predicted transits enter the fit.  The reference epoch is moved to the
middle of the data, where t0 and P are least correlated.  The fit is
started from a few impact parameters, because b and a/rs are nearly
degenerate at low SNR, and the best start is kept.  Uncertainties come
from the Jacobian covariance, scaled by the reduced chi-squared.  T14 and
depth are derived, with their errors propagated through that covariance.
"""

from __future__ import annotations

import logging

import numpy as np
from scipy.optimize import least_squares

from ..models import CleanedLightCurve, FitResult, TransitCandidate
from .search import robust_std
from .transit_model import t14_hours, transit_flux

log = logging.getLogger(__name__)

PARAMS = ("t0", "period", "rp_rs", "a_rs", "b")

# Points within this many BLS durations of a transit centre enter the fit.
FIT_WINDOW_DURATIONS = 2.0
# Cadences at least this long (s) are integrated over the exposure.
SUPERSAMPLE_MIN_CADENCE_S = 900.0
SUPERSAMPLE = 5
_START_B = (0.1, 0.5, 0.85)
_MODEL_POINTS = 2000


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


def _model(params: np.ndarray, time: np.ndarray, exposure: float) -> np.ndarray:
    t0, period, rp_rs, a_rs, b = params
    if exposure > 0:
        return transit_flux(time, t0, period, rp_rs, a_rs, b,
                            exposure_days=exposure, supersample=SUPERSAMPLE)
    return transit_flux(time, t0, period, rp_rs, a_rs, b)


def _t14_and_depth(params: np.ndarray) -> np.ndarray:
    t0, period, rp_rs, a_rs, b = params
    depth = 1.0 - float(transit_flux(np.array([t0]), t0, period, rp_rs, a_rs, b)[0])
    return np.array([t14_hours(period, rp_rs, a_rs, b), depth])


def _derived_errors(params: np.ndarray, cov: np.ndarray) -> np.ndarray:
    """1-sigma errors on (T14, depth) by linear propagation through ``cov``."""
    grad = np.zeros((2, len(params)))
    for i, value in enumerate(params):
        step = max(abs(value), 1e-3) * 1e-5
        hi, lo = params.copy(), params.copy()
        hi[i] += step
        lo[i] -= step
        grad[:, i] = (_t14_and_depth(hi) - _t14_and_depth(lo)) / (2.0 * step)
    var = np.einsum("ij,jk,ik->i", grad, cov, grad)
    return np.sqrt(np.clip(var, 0.0, None))


def fit_transit(cleaned: CleanedLightCurve, candidate: TransitCandidate) -> FitResult:
    """Fit t0, period, rp/rs, a/rs and b starting from ``candidate``.

    Uncertainties come from the Jacobian covariance; ``model_time`` /
    ``model_flux`` sample the best model over one phase (days from t0).
    """
    time, flux, err = _fit_data(cleaned, candidate)
    cadence_s = cleaned.lc.cadence_s or 0.0
    exposure = cadence_s / 86400.0 if cadence_s >= SUPERSAMPLE_MIN_CADENCE_S else 0.0

    period = candidate.period
    t_ref = candidate.t0 + np.round((np.median(time) - candidate.t0) / period) * period
    k0 = float(np.clip(np.sqrt(max(candidate.depth, 1e-6)), 1e-3, 0.5))

    def residuals(params):
        return (flux - _model(params, time, exposure)) / err

    # t0 within a BLS duration, P within the phase drift that keeps every
    # transit inside its fit window.
    n_cycles = max((time.max() - time.min()) / period, 1.0)
    dp = candidate.duration / n_cycles
    lower = [t_ref - candidate.duration, period - dp, 1e-4, 1.05, 0.0]
    upper = [t_ref + candidate.duration, period + dp, 0.6, 2000.0, 1.5]

    best = None
    for b0 in _START_B:
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
    t14, depth = _t14_and_depth(params)
    if t14 <= 0 or depth <= 0:
        raise RuntimeError("fitted model does not transit")

    dof = max(len(flux) - len(params), 1)
    chi2_red = float(2.0 * best.cost / dof)
    jac = best.jac
    cov = np.linalg.pinv(jac.T @ jac) * max(chi2_red, 1e-12)
    errors = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    t14_err, depth_err = _derived_errors(params, cov)
    uncertainties = {name: float(e) for name, e in zip(PARAMS, errors)}
    uncertainties.update(duration_hours=float(t14_err), depth=float(depth_err))

    t0, period, rp_rs, a_rs, b = (float(v) for v in params)
    half = 0.5 * t14 / 24.0
    model_time = np.unique(np.concatenate([
        np.linspace(-period / 2.0, period / 2.0, _MODEL_POINTS // 2),
        np.linspace(-3.0 * half, 3.0 * half, _MODEL_POINTS // 2),
    ]))
    model_flux = transit_flux(model_time + t0, t0, period, rp_rs, a_rs, b)

    return FitResult(
        period=period, t0=t0, rp_rs=rp_rs, a_rs=a_rs, b=b,
        duration_hours=float(t14), depth=float(depth),
        uncertainties=uncertainties, chi2=chi2_red,
        model_time=model_time, model_flux=model_flux,
    )
