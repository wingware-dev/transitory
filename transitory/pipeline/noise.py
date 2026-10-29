"""Correlated (red) noise: the beta factor of Pont, Zucker & Queloz (2006).

A depth averaged over a transit has the white-noise error sigma/sqrt(N)
only when the noise is uncorrelated.  On real light curves, residual
stellar variability and instrumental systematics are correlated over
hours, so averages over a transit scatter more than that, and a dip of
noise looks significant.  The beta factor measures how much more: the
scatter of the data binned at the transit's timescale, over what white
noise would give for bins with that many points (the "time-averaging"
method of Winn et al. 2008).  Multiplying white-noise errors by it makes
them hold for the red noise too.
"""

from __future__ import annotations

import numpy as np

# Bin widths, in units of the timescale asked for; beta is the median over
# them, so a single width's scatter doesn't decide it.
BIN_WIDTHS = (0.75, 1.0, 1.5)
# Fewer bins than this give too noisy a scatter to use.
MIN_BINS = 10
# A bin counts when it has at least this fraction of a full bin's points.
MIN_BIN_FILL = 0.5


def robust_std(values: np.ndarray) -> float:
    values = values[np.isfinite(values)]
    if len(values) < 2:
        return float("nan")
    return float(1.4826 * np.median(np.abs(values - np.median(values))))


def _binned_beta(time: np.ndarray, resid: np.ndarray, sigma: float, width: float,
                 cadence: float) -> float:
    index = np.floor((time - time[0]) / width).astype(np.int64)
    _, inverse, counts = np.unique(index, return_inverse=True, return_counts=True)
    sums = np.bincount(inverse, weights=resid)
    full = (counts >= max(2, MIN_BIN_FILL * width / cadence))
    if np.sum(full) < MIN_BINS:
        return float("nan")
    # Each bin mean scaled by sqrt(n) has scatter sigma for white noise.
    scaled = sums[full] / counts[full] * np.sqrt(counts[full])
    return robust_std(scaled) / sigma


def beta_factor(time: np.ndarray, flux: np.ndarray, timescale: float) -> float:
    """How much more than white noise ``flux`` scatters when averaged over ``timescale`` days.

    Pass out-of-transit points only, so a transit's own dip doesn't count as
    noise.  Never below 1 (white noise, or too little data to tell).
    """
    good = np.isfinite(time) & np.isfinite(flux)
    order = np.argsort(time[good])
    time, flux = time[good][order], flux[good][order]
    if len(time) < 2 * MIN_BINS or timescale <= 0:
        return 1.0
    resid = flux - np.median(flux)
    sigma = robust_std(resid)
    if not np.isfinite(sigma) or sigma <= 0:
        return 1.0
    cadence = float(np.median(np.diff(time)))
    if not cadence > 0:
        return 1.0
    betas = [_binned_beta(time, resid, sigma, k * timescale, cadence) for k in BIN_WIDTHS]
    betas = [b for b in betas if np.isfinite(b)]
    if not betas:
        return 1.0
    return max(1.0, float(np.median(betas)))
