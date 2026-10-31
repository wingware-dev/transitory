"""Light-curve cleaning: quality/NaN masking, upward sigma-clipping,
per-sector normalisation and flattening.

Quality-flagged cadences are dropped when the light curve is read (see
``pipeline/fetch.py``, which uses lightkurve's default quality bitmask), so
here "bad" means non-finite or non-positive values.  K2's roll systematics,
a sawtooth on the 6-hour thruster cycle this filter cannot follow, are also
removed on reading (``fetch.roll_correct``).  The curve is split into
segments at gaps longer than half the flattening window (sectors, quarters,
TESS orbit gaps), and each segment is normalised and flattened on its own.

Flattening uses a time-windowed Tukey biweight (as in Hippke et al. 2019,
wotan), here a biweight-weighted local quadratic, so stellar curvature and
one-sided windows at segment edges are followed; the biweight makes it robust to the transit points in the window.
"""

from __future__ import annotations

import numpy as np

from ..config import MAX_TRANSIT_DURATION_HOURS
from ..models import CleanedLightCurve, LightCurve, ProgressCallback

# Flattening window as a multiple of the longest transit duration searched.
WINDOW_DURATION_FACTOR = 3.0
# Tukey biweight tuning constant, in units of the window's MAD.
BIWEIGHT_C = 5.0
BIWEIGHT_ITERATIONS = 5
# Degree of the local polynomial fitted in each window: 2 follows stellar
# curvature over the window (a line or constant misses it by ~100 ppm for a
# 0.2%, 7-day variation); the dip refit below keeps it out of transits.
TREND_DEGREE = 2
# Below this many points a window gets a constant; with fewer than this
# fraction on either side of the node, at most a line (no wild extrapolation).
MIN_POINTS_FOR_SLOPE = 10
MIN_SIDE_FRACTION = 0.25
# The trend is evaluated at nodes this many per window and interpolated.
NODES_PER_WINDOW = 20
# Box-filtered dips this many sigma deep are left out of the final trend fit,
# so shallow transits (not outliers point by point) are not partly flattened away.
DIP_THRESHOLD_SIGMA = 3.0
# Skip that refit when "dips" cover more than this fraction: the trend is not
# following the star, and masking would hide the problem.
MAX_DIP_FRACTION = 0.3


def robust_std(x: np.ndarray) -> float:
    """Standard deviation estimated from the median absolute deviation."""
    if len(x) == 0:
        return float("nan")
    return float(1.4826 * np.median(np.abs(x - np.median(x))))


def biweight_location(x: np.ndarray, c: float = BIWEIGHT_C, iterations: int = BIWEIGHT_ITERATIONS) -> float:
    """Tukey biweight location of ``x``: a mean that ignores points beyond ``c`` MADs."""
    return biweight_poly(np.zeros(len(x)), x, degree=0, c=c, iterations=iterations)


def _weighted_fit(design: np.ndarray, x: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Weighted least-squares coefficients; normal equations, which are fast for a few columns."""
    wd = design * w[:, None]
    try:
        return np.linalg.solve(design.T @ wd, wd.T @ x)
    except np.linalg.LinAlgError:
        sw = np.sqrt(w)
        return np.linalg.lstsq(design * sw[:, None], x * sw, rcond=None)[0]


def biweight_poly(dt: np.ndarray, x: np.ndarray, degree: int = TREND_DEGREE, c: float = BIWEIGHT_C,
                  iterations: int = BIWEIGHT_ITERATIONS) -> float:
    """Value at ``dt == 0`` of a polynomial fitted to ``x`` with Tukey biweights.

    Points more than ``c`` MADs from the fit get no weight, so transits and
    flares in the window barely move it.  A local polynomial rather than a
    constant follows the star's curvature inside the window and stays
    unbiased at segment edges, where the window is one-sided.
    """
    n = len(x)
    degree = min(degree, max(n - 1, 0))
    if n and np.ptp(dt) == 0:
        degree = 0
    scale = float(np.max(np.abs(dt))) if n and degree else 1.0
    design = np.vander(dt / scale, degree + 1, increasing=True)
    if degree:
        # Start from a plain fit, so the first weights don't see the curvature as outliers.
        fit = design @ _weighted_fit(design, x, np.ones(n))
    else:
        fit = np.full(n, float(np.median(x)))
    level = fit[np.argmin(np.abs(dt))] if n else float("nan")
    for _ in range(iterations):
        resid = x - fit
        mad = float(np.median(np.abs(resid)))
        if mad == 0.0:
            break
        u = resid / (c * mad)
        w = np.where(np.abs(u) < 1.0, (1.0 - u * u) ** 2, 0.0)
        if np.count_nonzero(w) <= degree:
            break
        coef = _weighted_fit(design, x, w)
        fit = design @ coef
        done = abs(coef[0] - level) < 1e-10 * max(abs(level), 1.0)
        level = float(coef[0])
        if done:
            break
    return level


def split_segments(time: np.ndarray, max_gap: float) -> list[slice]:
    """Slices of sorted ``time`` separated by gaps longer than ``max_gap`` days."""
    if len(time) == 0:
        return []
    breaks = np.flatnonzero(np.diff(time) > max_gap) + 1
    edges = np.concatenate([[0], breaks, [len(time)]])
    return [slice(int(a), int(b)) for a, b in zip(edges[:-1], edges[1:])]


def _safe_degree(dt: np.ndarray) -> int:
    """TREND_DEGREE, or less where the window's points sit mostly on one side.

    A quadratic extrapolated beyond its data (segment edges, or next to a
    masked dip) can run far off, so lopsided windows get a line and nearly
    empty ones a constant.
    """
    n = len(dt)
    if n < MIN_POINTS_FOR_SLOPE:
        return 0
    if min(np.count_nonzero(dt < 0), np.count_nonzero(dt > 0)) < MIN_SIDE_FRACTION * n:
        return min(TREND_DEGREE, 1)
    return TREND_DEGREE


def biweight_trend(time: np.ndarray, flux: np.ndarray, window: float,
                   use: np.ndarray | None = None) -> np.ndarray:
    """Running biweight of one segment, evaluated on a node grid and interpolated to ``time``.

    Only points where ``use`` is True (all, by default) shape the trend; it is
    interpolated across the others.
    """
    if len(time) == 0:
        return np.empty(0)
    t_fit, f_fit = (time, flux) if use is None else (time[use], flux[use])
    if len(t_fit) == 0:
        return np.full(len(time), np.median(flux))
    span = time[-1] - time[0]
    n_nodes = max(int(np.ceil(span / window * NODES_PER_WINDOW)) + 1, 2)
    nodes = np.linspace(time[0], time[-1], n_nodes)
    lo = np.searchsorted(t_fit, nodes - window / 2.0, side="left")
    hi = np.searchsorted(t_fit, nodes + window / 2.0, side="right")
    values = np.full(n_nodes, np.nan)
    for i in range(n_nodes):
        if hi[i] > lo[i]:
            dt = t_fit[lo[i]:hi[i]] - nodes[i]
            values[i] = biweight_poly(dt, f_fit[lo[i]:hi[i]], degree=_safe_degree(dt))
    good = np.isfinite(values)
    if not good.any():
        return np.full(len(time), np.median(f_fit))
    return np.interp(time, nodes[good], values[good])


def _flatten_segments(time, flux, segments, window, use=None):
    trend = np.empty_like(flux)
    for seg in segments:
        trend[seg] = biweight_trend(time[seg], flux[seg], window, None if use is None else use[seg])
    return trend


def find_dips(time: np.ndarray, resid: np.ndarray, max_duration: float, cadence: float,
              threshold: float = DIP_THRESHOLD_SIGMA) -> np.ndarray:
    """Points in significant dips of ``resid``, found with running boxes of several widths.

    Each point gets the box width (from a few cadences up to ``max_duration``
    days) whose mean is most significantly low, as a matched filter.  Each
    run of points whose best box is more than ``threshold`` sigma low marks
    the span of the single best box in the run, which is the dip itself, not
    everything a wide box overlaps.
    """
    n = len(time)
    dips = np.zeros(n, dtype=bool)
    if n < 3:
        return dips
    csum = np.concatenate([[0.0], np.cumsum(resid)])
    widths = []
    width = max(3.0 * cadence, 0.5 / 24.0)
    while width < max_duration:
        widths.append(width)
        width *= np.sqrt(2.0)
    widths.append(max_duration)

    best_snr = np.zeros(n)
    best_width = np.zeros(n)
    for width in widths:
        lo = np.searchsorted(time, time - width / 2.0, side="left")
        hi = np.searchsorted(time, time + width / 2.0, side="right")
        box = (csum[hi] - csum[lo]) / (hi - lo)
        sigma = robust_std(box)
        if not sigma > 0:
            continue
        snr = np.where(hi - lo >= 3, -box / sigma, 0.0)
        better = snr > best_snr
        best_snr[better] = snr[better]
        best_width[better] = width

    low = np.flatnonzero(best_snr > threshold)
    if len(low) == 0:
        return dips
    runs = np.split(low, np.flatnonzero(np.diff(low) > 1) + 1)
    for run in runs:
        j = run[np.argmax(best_snr[run])]
        # Wider than the box itself: the best box can be shorter than the dip
        # (one width step, a factor sqrt(2)), and ingress / egress lie beyond it.
        half = 0.75 * best_width[j]
        a = np.searchsorted(time, time[j] - half, side="left")
        b = np.searchsorted(time, time[j] + half, side="right")
        dips[a:b] = True
    return dips


def clean_light_curve(
    lc: LightCurve,
    window_days: float | None = None,
    sigma_upper: float = 4.0,
    max_duration_hours: float = MAX_TRANSIT_DURATION_HOURS,
    progress_cb: ProgressCallback | None = None,
) -> CleanedLightCurve:
    """Return ``lc`` detrended and outlier-clipped, keeping transit dips intact.

    ``window_days`` is the flattening window; None picks about 3x
    ``max_duration_hours``.  Only upward outliers beyond ``sigma_upper`` are
    clipped so transits survive.
    """
    def progress(message: str, fraction: float) -> None:
        if progress_cb:
            progress_cb(message, fraction)

    if window_days is None:
        window_days = WINDOW_DURATION_FACTOR * max_duration_hours / 24.0
    if window_days <= 0:
        raise ValueError(f"window_days must be positive, not {window_days}")
    log: list[str] = []

    time = np.asarray(lc.time, dtype=float)
    flux = np.asarray(lc.flux, dtype=float)
    flux_err = np.asarray(lc.flux_err, dtype=float)
    n_raw = len(time)

    progress("masking bad points", 0.0)
    order = np.argsort(time, kind="stable")
    valid = np.isfinite(time) & np.isfinite(flux) & (flux > 0)
    # A missing error does not make the flux unusable; fill it in below.
    n_bad = int(n_raw - valid.sum())
    if n_bad:
        log.append(f"Removed {n_bad} of {n_raw} points with missing or non-positive flux")
    idx = order[valid[order]]
    if len(idx) == 0:
        raise ValueError("light curve has no usable points")

    t, f, e = time[idx], flux[idx], flux_err[idx]
    max_gap = window_days / 2.0
    segments = split_segments(t, max_gap)

    progress("normalising segments", 0.1)
    scale = np.empty_like(f)
    for seg in segments:
        scale[seg] = np.median(f[seg])
    f_norm = f / scale
    e_norm = e / scale
    bad_err = ~np.isfinite(e_norm) | (e_norm <= 0)
    if bad_err.any():
        fill = robust_std(np.diff(f_norm)) / np.sqrt(2.0) if len(f_norm) > 1 else 0.0
        e_norm[bad_err] = fill
        log.append(f"Estimated flux errors for {int(bad_err.sum())} points from the scatter")
    log.append(f"Normalised {len(segments)} segment(s) by their median flux "
               f"(split at gaps > {max_gap * 24:.1f} h)")

    # Pass 1: a trend over everything, only to find dips (likely transits).
    progress("finding dips", 0.2)
    trend = _flatten_segments(t, f_norm, segments, window_days)
    cadence = float(np.median(np.diff(t))) if len(t) > 1 else window_days
    dips = find_dips(t, f_norm / trend - 1.0, max_duration_hours / 24.0, cadence)
    n_dip = int(dips.sum())
    if n_dip > MAX_DIP_FRACTION * len(t):
        log.append(f"Dips cover {n_dip} of {len(t)} points; not protecting them from the trend fit")
        dips[:] = False
    elif n_dip:
        log.append(f"Left {n_dip} points in dips out of the trend fit")

    # Pass 2: a trend that leaves the dips out, to find upward outliers
    # (flares, cosmic rays) without mistaking transit shoulders for them.
    progress("finding outliers", 0.45)
    if n_dip:
        trend = _flatten_segments(t, f_norm, segments, window_days, use=~dips)
    resid = f_norm / trend - 1.0
    sigma = robust_std(resid[~dips])
    keep = resid <= sigma_upper * sigma if sigma > 0 else np.ones(len(t), dtype=bool)
    n_clip = int((~keep).sum())
    if n_clip:
        log.append(f"Clipped {n_clip} upward outliers above {sigma_upper:g} sigma")

    # Pass 3: the final trend from the kept points outside dips, on every point.
    progress("flattening", 0.7)
    trend = _flatten_segments(t, f_norm, segments, window_days, use=keep & ~dips)
    flat = f_norm[keep] / trend[keep]
    log.append(f"Flattened with a biweight filter, window {window_days * 24:.1f} h "
               f"({WINDOW_DURATION_FACTOR:g}x the {max_duration_hours:g} h longest searched duration)")

    # The removed trend on the raw grid, in raw flux units, so raw / trend = flattened.
    trend_raw = np.full(n_raw, np.nan)
    trend_raw[idx] = trend * scale

    mask = np.zeros(n_raw, dtype=bool)
    mask[idx[keep]] = True

    cleaned = LightCurve(
        time=t[keep],
        flux=flat,
        flux_err=e_norm[keep] / trend[keep],
        mission=lc.mission,
        sectors_or_quarters=list(lc.sectors_or_quarters),
        source_ids=list(lc.source_ids),
        dois=list(lc.dois),
        author=lc.author,
        cadence_s=lc.cadence_s,
        doi_sectors={doi: list(s) for doi, s in lc.doi_sectors.items()},
        source=lc.source,
        retrieved=lc.retrieved,
    )
    log.append(f"Kept {int(keep.sum())} of {n_raw} points; flattened scatter "
               f"{robust_std(flat - 1.0) * 1e6:.0f} ppm")
    progress("cleaned", 1.0)
    return CleanedLightCurve(lc=cleaned, trend=trend_raw, mask=mask, cleaning_log=log)
