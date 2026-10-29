"""Iterative Box Least Squares transit search.

Each pass runs astropy's ``BoxLeastSquares`` over a log-spaced period grid.
It takes the highest peak of the SDE-normalised power and keeps it as a
candidate when both its SDE and its SNR clear the detection thresholds.
Then it masks that signal's transits and searches again, up to
``max_planets`` passes.

The period range is split into octaves.  Each octave gets its own set of
trial durations, from the shortest plausible transit at that period up to
P/5 (capped at ``MAX_TRANSIT_DURATION_HOURS``).  BLS needs every trial
duration to be shorter than the shortest period, and a single set would
either miss long transits at long periods or break that rule at short
ones.  Grid spacing in log P follows from the shortest duration, so a
transit drifts by at most a fraction of its length across the baseline.
"""

from __future__ import annotations

import logging

import numpy as np
from astropy.timeseries import BoxLeastSquares
from scipy.ndimage import median_filter

from ..config import MAX_PLANETS_PER_STAR, MAX_TRANSIT_DURATION_HOURS, MIN_PERIOD_DAYS
from ..models import CleanedLightCurve, PeriodogramResult, ProgressCallback, TransitCandidate

log = logging.getLogger(__name__)

# A peak counts as a detection only when it clears both thresholds.
SDE_THRESHOLD = 7.0
SNR_THRESHOLD = 7.0

# Phase drift across the baseline allowed between neighbouring trial periods,
# as a fraction of the shortest trial duration.
GRID_OVERSAMPLE = 3.0
# Cap on trial periods per pass, so multi-year Kepler curves stay tractable;
# when it applies, the grid is coarser than GRID_OVERSAMPLE asks for.
MAX_GRID_PERIODS = 120_000
# A transit epoch counts when it has at least this fraction of the points a
# fully sampled transit would have (and at least one).
MIN_TRANSIT_COVERAGE = 0.5


def _min_duration_days(period: float) -> float:
    """Shortest transit worth searching for at ``period``.

    About a quarter of a central transit across a dense (M-dwarf-like) star,
    so grazing transits and small stars are still covered.
    """
    return max(0.3, 0.5 * period ** (1.0 / 3.0)) / 24.0


def _octaves(min_period: float, max_period: float) -> list[tuple[float, float]]:
    edges = [min_period]
    while edges[-1] * 2.0 < max_period:
        edges.append(edges[-1] * 2.0)
    edges.append(max_period)
    return list(zip(edges[:-1], edges[1:]))


def _durations(p_lo: float) -> np.ndarray:
    d_min = _min_duration_days(p_lo)
    d_max = min(MAX_TRANSIT_DURATION_HOURS / 24.0, p_lo / 5.0)
    if d_max <= d_min:
        return np.array([d_min])
    n = int(np.ceil(np.log(d_max / d_min) / np.log(1.3))) + 1
    return np.geomspace(d_min, d_max, n)


def period_grid(baseline: float, min_period: float, max_period: float) -> list[tuple[np.ndarray, np.ndarray]]:
    """(periods, durations) per octave of the search range."""
    segments = []
    for p_lo, p_hi in _octaves(min_period, max_period):
        durations = _durations(p_lo)
        dlog = durations[0] / (baseline * GRID_OVERSAMPLE)
        n = max(int(np.ceil(np.log(p_hi / p_lo) / dlog)), 2)
        segments.append((p_lo, p_hi, durations, n))
    total = sum(s[3] for s in segments)
    scale = min(1.0, MAX_GRID_PERIODS / total)
    if scale < 1.0:
        log.info("BLS grid capped at %d periods (asked for %d)", MAX_GRID_PERIODS, total)
    grid = []
    for p_lo, p_hi, durations, n in segments:
        n = max(int(n * scale), 2)
        grid.append((np.geomspace(p_lo, p_hi, n, endpoint=False), durations))
    return grid


def robust_std(values: np.ndarray) -> float:
    values = values[np.isfinite(values)]
    if len(values) < 2:
        return float("nan")
    return float(1.4826 * np.median(np.abs(values - np.median(values))))


def count_transits(time: np.ndarray, period: float, t0: float, duration: float) -> int:
    """Number of distinct epochs with data covering at least MIN_TRANSIT_COVERAGE of the transit."""
    epoch = np.round((time - t0) / period)
    in_transit = np.abs(time - (t0 + epoch * period)) < 0.5 * duration
    if not np.any(in_transit):
        return 0
    cadence = float(np.median(np.diff(np.sort(time)))) if len(time) > 1 else duration
    min_points = max(1, int(MIN_TRANSIT_COVERAGE * duration / cadence))
    _, counts = np.unique(epoch[in_transit], return_counts=True)
    return int(np.sum(counts >= min_points))


def transit_snr(time: np.ndarray, flux: np.ndarray, period: float, t0: float,
                duration: float, depth: float) -> float:
    """Depth over its white-noise error, with the noise from out-of-transit scatter."""
    epoch = np.round((time - t0) / period)
    dt = np.abs(time - (t0 + epoch * period))
    n_in = int(np.sum(dt < 0.5 * duration))
    sigma = robust_std(flux[dt > duration])
    if n_in == 0 or not np.isfinite(sigma) or sigma <= 0:
        return 0.0
    return float(depth / (sigma / np.sqrt(n_in)))


def box_depth(time: np.ndarray, flux: np.ndarray, period: float, t0: float, duration: float,
              sigma: float, select=None, half_width: float = 0.5,
              robust: bool = False) -> tuple[float, float]:
    """(depth, error) of the mean dip within ``half_width`` durations of each centre.

    ``select(epoch)`` picks which epochs count (odd/even).  The baseline is
    the median of points 1 to 2 durations from each centre.  ``robust``
    takes the median of the in-transit points instead of the mean, so a
    flare inside a transit (which clipping misses, as it is still below the
    trend) doesn't make that transit look shallow.  Depth and error are NaN
    when no point falls in transit.
    """
    epoch = np.round((time - t0) / period)
    dt = np.abs(time - (t0 + epoch * period))
    use = np.ones(len(time), dtype=bool) if select is None else select(epoch)
    inner = use & (dt < half_width * duration)
    outer = use & (dt > duration) & (dt < 2.0 * duration)
    n_in = int(np.sum(inner))
    if n_in == 0:
        return float("nan"), float("nan")
    level = np.median(flux[outer]) if np.sum(outer) >= 5 else np.median(flux)
    if robust:
        # The median's error is sqrt(pi/2) times the mean's for Gaussian noise.
        return (float(level - np.median(flux[inner])),
                float(1.2533 * sigma / np.sqrt(n_in)))
    return float(level - np.mean(flux[inner])), float(sigma / np.sqrt(n_in))


def _unalias(time, flux, sigma, period, t0, duration, max_period) -> tuple[float, float]:
    """Double the period while every other transit at it is empty.

    BLS can find a planet with few transits at half its period, when
    the extra epochs fall in gaps or at the baseline edges.  When one parity
    shows the dip and the other is consistent with nothing, the true period
    is twice as long.  An eclipsing binary, whose two eclipses are both
    there but differ in depth, is left alone for vetting to flag.
    """
    while 2.0 * period <= max_period:
        odd = box_depth(time, flux, period, t0, duration, sigma, lambda e: e % 2 == 1)
        even = box_depth(time, flux, period, t0, duration, sigma, lambda e: e % 2 == 0)
        if not (np.isfinite(odd[0]) and np.isfinite(even[0])):
            # One parity has no data at all: nothing says the period is wrong.
            break
        (deep, deep_err), (shallow, shallow_err), deep_t0 = (
            (odd, even, t0 + period) if odd[0] > even[0] else (even, odd, t0))
        if not (deep > 5.0 * deep_err and shallow < 2.0 * shallow_err and shallow < 0.25 * deep):
            break
        log.info("BLS: P=%.5f d has empty alternate transits; using P=%.5f d", period, 2 * period)
        period, t0 = 2.0 * period, deep_t0
    return period, t0


def _sde(power: np.ndarray) -> np.ndarray:
    """Signal detection efficiency: power with its slow trend removed, in units of its scatter."""
    width = max(len(power) // 100, 51) | 1
    detrended = power - median_filter(power, size=width, mode="nearest")
    std = np.std(detrended)
    if not np.isfinite(std) or std <= 0:
        return np.zeros_like(power)
    return (detrended - np.mean(detrended)) / std


def _search_pass(time, flux, dy, grid, progress) -> tuple[np.ndarray, np.ndarray, dict]:
    bls = BoxLeastSquares(time, flux, dy)
    periods, powers, durations, t0s, depths = [], [], [], [], []
    for i, (seg_periods, seg_durations) in enumerate(grid):
        res = bls.power(seg_periods, seg_durations, objective="likelihood")
        periods.append(seg_periods)
        powers.append(np.asarray(res.power))
        durations.append(np.asarray(res.duration))
        t0s.append(np.asarray(res.transit_time))
        depths.append(np.asarray(res.depth))
        progress((i + 1) / len(grid))
    periods = np.concatenate(periods)
    power = np.nan_to_num(np.concatenate(powers), nan=0.0)
    extra = dict(duration=np.concatenate(durations), t0=np.concatenate(t0s),
                 depth=np.concatenate(depths))
    return periods, power, extra


def search_transits(
    cleaned: CleanedLightCurve,
    max_planets: int = MAX_PLANETS_PER_STAR,
    min_period: float = MIN_PERIOD_DAYS,
    max_period: float | None = None,
    progress_cb: ProgressCallback | None = None,
) -> list[PeriodogramResult]:
    """Search ``cleaned`` for periodic dips, masking each found signal and repeating.

    Returns one PeriodogramResult per pass.  A pass's ``best`` is its
    significant candidate, or None when the pass found nothing, which ends
    the search.  ``max_period`` None means half the baseline.
    """
    lc = cleaned.lc
    good = np.isfinite(lc.time) & np.isfinite(lc.flux)
    time = np.asarray(lc.time[good], dtype=float)
    flux = np.asarray(lc.flux[good], dtype=float)
    if len(time) < 10:
        raise ValueError(f"too few points to search ({len(time)})")
    baseline = float(time.max() - time.min())
    if max_period is None:
        max_period = baseline / 2.0
    if max_period <= min_period:
        raise ValueError(f"baseline {baseline:.2f} d is too short to search periods "
                         f"above {min_period} d")
    grid = period_grid(baseline, min_period, max_period)

    results: list[PeriodogramResult] = []
    keep = np.ones(len(time), dtype=bool)
    for n_pass in range(max_planets):
        def progress(frac: float, n_pass=n_pass) -> None:
            if progress_cb:
                progress_cb(f"BLS pass {n_pass + 1}", (n_pass + frac) / max_planets)

        t, f = time[keep], flux[keep]
        sigma = robust_std(f)
        if len(t) < 10 or not np.isfinite(sigma) or sigma <= 0:
            break
        periods, power, extra = _search_pass(t, f, np.full_like(t, sigma), grid, progress)
        sde = _sde(power)
        i = int(np.argmax(sde))
        period, t0 = float(periods[i]), float(extra["t0"][i])
        duration, depth = float(extra["duration"][i]), float(extra["depth"][i])
        if depth > 0:
            unaliased = _unalias(t, f, sigma, period, t0, duration, baseline)
            if unaliased[0] != period:
                period, t0 = unaliased
                depth = box_depth(t, f, period, t0, duration, sigma)[0]
        snr = transit_snr(t, f, period, t0, duration, depth)
        n_transits = count_transits(t, period, t0, duration)
        cand = TransitCandidate(period=period, t0=t0, duration=duration, depth=depth,
                                snr=snr, sde=float(sde[i]), n_transits=n_transits)
        significant = depth > 0 and cand.sde >= SDE_THRESHOLD and snr >= SNR_THRESHOLD
        log.info("BLS pass %d: P=%.5f d depth=%.0f ppm SDE=%.1f SNR=%.1f n=%d%s",
                 n_pass + 1, period, depth * 1e6, cand.sde, snr, n_transits,
                 "" if significant else " (not significant)")
        results.append(PeriodogramResult(periods=periods, power=sde,
                                         best=cand if significant else None))
        if not significant:
            break
        epoch = np.round((time - t0) / period)
        keep &= np.abs(time - (t0 + epoch * period)) >= duration
    return results
