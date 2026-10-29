"""Candidate vetting: SNR, SDE, transit count, odd/even depths, secondary eclipse.

The confidence levels are:

- ``none``: below the detection thresholds, fewer than two transits seen,
  odd and even depths that disagree, a secondary eclipse deeper than a
  planet's could plausibly be, a fitted size too big for a planet, or a
  repeat of a stronger signal on the star (its secondary eclipse or a
  period alias).  These are the signs of noise, a single event or an
  eclipsing binary.
- ``weak``: a detection that is marginal (SNR or SDE close to threshold,
  only two transits, a large rp/rs, a significant but small secondary,
  a failed fit).
- ``strong``: everything else.

Depths are measured directly from the data (the median drop in the central
half of the transit against the local out-of-transit level), so the checks
don't depend on the fit converging.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from ..models import CleanedLightCurve, FitResult, TransitCandidate, Vetting
from .search import SDE_THRESHOLD, SNR_THRESHOLD, box_depth, robust_std

SNR_STRONG = 10.0
SDE_STRONG = 9.0
# Odd/even depths differing by more than this many sigma look like an EB.
ODD_EVEN_SIGMA_MAX = 3.0
# A secondary at least this significant counts as detected...
SECONDARY_SIGMA_MIN = 3.0
# ...and this deep compared with the primary rules out a planet (a hot
# Jupiter's thermal emission is ~1% of its transit depth).
SECONDARY_RATIO_MAX = 0.1
# Above this rp/rs the companion is likely stellar (about 2 Jupiter radii for a Sun).
RP_RS_PLANET_MAX = 0.2
RP_RS_STELLAR = 0.4
# A candidate at one of these period ratios to a stronger one on the same
# star repeats it (real resonant pairs sit a percent or more off exact).
HARMONICS = ((1.0, "repeat"), (0.5, "half-period alias"), (2.0, "double-period alias"),
             (1.0 / 3.0, "third-period alias"), (3.0, "triple-period alias"))
HARMONIC_TOLERANCE = 0.002
# A fitted T14 this many times the BLS box means the fit chased something
# broader than the dip (residual variability), not a transit.
FIT_DURATION_RATIO_MAX = 3.0


def _depth_at(time, flux, period, centre, duration, sigma, select=None) -> tuple[float, float]:
    """(depth, error) of the mean dip in the central half of the transits at ``centre``."""
    return box_depth(time, flux, period, centre, duration, sigma, select, half_width=0.25,
                     robust=True)


def _harmonic_of(candidate: TransitCandidate, other: TransitCandidate) -> str | None:
    """How ``candidate`` repeats the stronger signal ``other``, or None if it doesn't."""
    for ratio, name in HARMONICS:
        if abs(candidate.period / other.period - ratio) < HARMONIC_TOLERANCE * ratio:
            if ratio == 1.0:
                offset = ((candidate.t0 - other.t0) / other.period) % 1.0
                if abs(offset - 0.5) < 0.1:
                    return f"secondary eclipse of the P={other.period:.4f} d signal"
            return f"{name} of the P={other.period:.4f} d signal"
    return None


def vet_candidate(
    cleaned: CleanedLightCurve,
    candidate: TransitCandidate,
    fit: FitResult | None = None,
    others: Sequence[TransitCandidate] = (),
) -> Vetting:
    """Grade a candidate 'strong', 'weak' or 'none', with the reasons.

    ``others`` are the star's other candidates.  Their transits are left out
    of the depth checks, so another planet's transit can't pass for a
    secondary eclipse.  A candidate that repeats a stronger one (same period
    at phase 0.5, or a period alias) is graded 'none'.
    """
    lc = cleaned.lc
    good = np.isfinite(lc.time) & np.isfinite(lc.flux)
    for other in others:
        # A harmonic's transits overlap this candidate's own, so it stays in.
        if other is not candidate and not (_harmonic_of(candidate, other)
                                           or _harmonic_of(other, candidate)):
            epoch = np.round((lc.time - other.t0) / other.period)
            good &= np.abs(lc.time - (other.t0 + epoch * other.period)) >= other.duration
    time = np.asarray(lc.time[good], dtype=float)
    flux = np.asarray(lc.flux[good], dtype=float)

    if fit is not None:
        period, t0, duration = fit.period, fit.t0, fit.duration_hours / 24.0
    else:
        period, t0, duration = candidate.period, candidate.t0, candidate.duration

    epoch = np.round((time - t0) / period)
    sigma = robust_std(flux[np.abs(time - (t0 + epoch * period)) > duration])

    fatal: list[str] = []
    marginal: list[str] = []
    notes: list[str] = []

    repeats = []
    for other in others:
        if other is not candidate and other.snr > candidate.snr:
            harmonic = _harmonic_of(candidate, other)
            if harmonic:
                repeats.append(f"{harmonic}, not a separate planet")

    if candidate.snr < SNR_THRESHOLD:
        fatal.append(f"SNR {candidate.snr:.1f} below the detection threshold {SNR_THRESHOLD:g}")
    elif candidate.snr < SNR_STRONG:
        marginal.append(f"marginal SNR {candidate.snr:.1f}")
    if candidate.sde < SDE_THRESHOLD:
        fatal.append(f"SDE {candidate.sde:.1f} below the detection threshold {SDE_THRESHOLD:g}")
    elif candidate.sde < SDE_STRONG:
        marginal.append(f"marginal SDE {candidate.sde:.1f}")

    if candidate.n_transits < 2:
        fatal.append(f"only {candidate.n_transits} transit observed")
    elif candidate.n_transits == 2:
        marginal.append("only 2 transits observed")

    odd_d, odd_e = _depth_at(time, flux, period, t0, duration, sigma, lambda e: e % 2 == 1)
    even_d, even_e = _depth_at(time, flux, period, t0, duration, sigma, lambda e: e % 2 == 0)
    odd_even_sigma = float("nan")
    if np.isfinite(odd_d) and np.isfinite(even_d):
        odd_even_sigma = abs(odd_d - even_d) / np.hypot(odd_e, even_e)
        if odd_even_sigma > ODD_EVEN_SIGMA_MAX:
            fatal.append(f"odd/even depths differ by {odd_even_sigma:.1f} sigma "
                         f"({odd_d * 1e6:.0f} vs {even_d * 1e6:.0f} ppm): likely eclipsing binary")

    primary, _ = _depth_at(time, flux, period, t0, duration, sigma)
    secondary, sec_err = _depth_at(time, flux, period, t0 + period / 2.0, duration, sigma)
    if np.isfinite(secondary) and sec_err > 0 and secondary / sec_err > SECONDARY_SIGMA_MIN:
        ratio = secondary / primary if primary > 0 else np.inf
        text = (f"secondary eclipse at phase 0.5: {secondary * 1e6:.0f} ppm "
                f"({secondary / sec_err:.1f} sigma)")
        if ratio > SECONDARY_RATIO_MAX:
            fatal.append(f"{text}, {ratio:.0%} of the primary: likely eclipsing binary")
        else:
            marginal.append(f"{text}, small enough to be planetary emission")

    if fit is None:
        marginal.append("transit fit failed")
    else:
        if fit.rp_rs > RP_RS_STELLAR:
            fatal.append(f"rp/rs {fit.rp_rs:.2f} is too large for a planet")
        elif fit.rp_rs > RP_RS_PLANET_MAX:
            marginal.append(f"rp/rs {fit.rp_rs:.2f} is large for a planet")
        box_hours = candidate.duration * 24.0
        if fit.duration_hours > FIT_DURATION_RATIO_MAX * box_hours:
            marginal.append(f"fitted T14 {fit.duration_hours:.1f} h is much longer than the "
                            f"{box_hours:.1f} h detected dip: not transit-shaped")

    if repeats:
        # The other checks measured the stronger signal again; only this matters.
        fatal, marginal = repeats, []
    if fatal:
        confidence = "none"
    elif marginal:
        confidence = "weak"
    else:
        confidence = "strong"
        notes.append(f"SNR {candidate.snr:.1f}, SDE {candidate.sde:.1f}, "
                     f"{candidate.n_transits} transits, odd/even consistent")

    return Vetting(
        odd_even_sigma=float(odd_even_sigma),
        secondary_depth=float(secondary) if np.isfinite(secondary) else float("nan"),
        confidence=confidence,
        reasons=fatal + marginal + notes,
    )
