"""Limb-darkened transit light-curve model (quadratic law, numpy only).

The star is a disk of unit radius with intensity
``I(mu) = 1 - u1 (1 - mu) - u2 (1 - mu)^2``.  The flux the planet blocks is
the intensity integrated over the overlap of the planet's disk and the
star's disk.  That integral is done numerically, annulus by annulus: an annulus
of radius r is covered over an arc fraction set by the planet's
distance z and radius p.  The part of the star the planet covers
completely (r < p - z) has a closed form.  The rest uses a midpoint rule
in a variable that bunches samples near the overlap edges, where the arc
fraction changes fastest.
"""

from __future__ import annotations

import numpy as np

DEFAULT_U1 = 0.40
DEFAULT_U2 = 0.25

# Annulus samples per in-transit point; 64 keeps the model within ~1e-4 of
# the depth, well below photometric noise, for planets up to rp/rs = 0.5.
_N_ANNULI = 64


def _cumulative_intensity(radius: np.ndarray, u1: float, u2: float) -> np.ndarray:
    """``integral_0^R I(r) 2r dr`` (the disk of radius R's flux, over pi)."""
    r2 = np.clip(radius, 0.0, 1.0) ** 2
    mu3 = (1.0 - r2) ** 1.5
    lin = r2 - (2.0 / 3.0) * (1.0 - mu3)
    quad = 2.0 * r2 - (4.0 / 3.0) * (1.0 - mu3) - 0.5 * r2 * r2
    return r2 - u1 * lin - u2 * quad


def _intensity(radius: np.ndarray, u1: float, u2: float) -> np.ndarray:
    one_minus_mu = 1.0 - np.sqrt(np.clip(1.0 - radius * radius, 0.0, 1.0))
    return 1.0 - u1 * one_minus_mu - u2 * one_minus_mu * one_minus_mu


def occulted_fraction(z: np.ndarray, p: float, u1: float = DEFAULT_U1,
                      u2: float = DEFAULT_U2) -> np.ndarray:
    """Fraction of the star's flux blocked by a planet of radius ``p`` at separation ``z``.

    Both are in stellar radii; ``z`` is the projected centre-to-centre distance.
    """
    z = np.abs(np.asarray(z, dtype=float))
    blocked = np.zeros_like(z)
    if p <= 0:
        return blocked
    total = _cumulative_intensity(np.array(1.0), u1, u2)
    on = z < 1.0 + p
    if not np.any(on):
        return blocked
    zo = z[on]

    # Annuli inside the planet's disk are fully covered.
    full = np.where(zo < p, _cumulative_intensity(np.minimum(p - zo, 1.0), u1, u2), 0.0)

    # Annuli crossing the planet's limb are partly covered.
    lo = np.abs(zo - p)
    hi = np.minimum(1.0, zo + p)
    span = np.clip(hi - lo, 0.0, None)
    theta = (np.arange(_N_ANNULI) + 0.5) * (np.pi / _N_ANNULI)
    s = 0.5 * (1.0 - np.cos(theta))  # 0..1, dense near both ends
    ds = 0.5 * np.sin(theta) * (np.pi / _N_ANNULI)
    r = lo[:, None] + span[:, None] * s[None, :]
    dr = span[:, None] * ds[None, :]
    with np.errstate(divide="ignore", invalid="ignore"):
        cos_arg = (r * r + zo[:, None] ** 2 - p * p) / (2.0 * r * zo[:, None])
    arc = np.arccos(np.clip(np.nan_to_num(cos_arg, nan=1.0), -1.0, 1.0)) / np.pi
    partial = np.sum(_intensity(r, u1, u2) * arc * 2.0 * r * dr, axis=1)

    blocked[on] = (full + partial) / total
    return blocked


def projected_separation(time: np.ndarray, t0: float, period: float, a_rs: float,
                         b: float) -> np.ndarray:
    """Planet-star distance on the sky in stellar radii; +inf when the planet is behind."""
    phase = 2.0 * np.pi * (np.asarray(time, dtype=float) - t0) / period
    cos_i = min(b / a_rs, 1.0) if a_rs > 0 else 1.0
    x = a_rs * np.sin(phase)
    y = a_rs * cos_i * np.cos(phase)
    z = np.hypot(x, y)
    return np.where(np.cos(phase) > 0, z, np.inf)


def transit_flux(
    time: np.ndarray,
    t0: float,
    period: float,
    rp_rs: float,
    a_rs: float,
    b: float,
    u1: float = DEFAULT_U1,
    u2: float = DEFAULT_U2,
    exposure_days: float = 0.0,
    supersample: int = 1,
) -> np.ndarray:
    """Relative flux (1 out of transit) for a circular orbit.

    With ``exposure_days`` > 0 and ``supersample`` > 1 each point averages the
    model over its exposure, which matters for 30-minute Kepler cadence.
    """
    time = np.asarray(time, dtype=float)
    if exposure_days > 0 and supersample > 1:
        offsets = ((np.arange(supersample) + 0.5) / supersample - 0.5) * exposure_days
        fine = (time[:, None] + offsets[None, :]).ravel()
        return transit_flux(fine, t0, period, rp_rs, a_rs, b, u1, u2).reshape(
            time.shape[0], supersample).mean(axis=1)
    z = projected_separation(time, t0, period, a_rs, b)
    return 1.0 - occulted_fraction(z, rp_rs, u1, u2)


def t14_hours(period: float, rp_rs: float, a_rs: float, b: float) -> float:
    """Total transit duration (first to fourth contact) in hours; 0 if it never transits."""
    if a_rs <= 1.0 or b >= 1.0 + rp_rs:
        return 0.0
    sin_i = np.sqrt(max(1.0 - (b / a_rs) ** 2, 0.0))
    chord = np.sqrt((1.0 + rp_rs) ** 2 - b * b)
    arg = chord / (a_rs * sin_i)
    return float(period / np.pi * np.arcsin(min(arg, 1.0)) * 24.0)
