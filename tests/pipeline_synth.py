"""Synthetic inputs for the search / fit / vetting tests.

``flat_cleaned`` builds a CleanedLightCurve straight from the synthetic
generator with no stellar trend and no flares, so those stages are tested
on their own, independent of the cleaning step.
"""

from __future__ import annotations

import numpy as np

from transitory.models import CleanedLightCurve, StarTarget, TransitCandidate
from transitory.synthetic import InjectedPlanet, SyntheticStar, make_light_curve


def flat_cleaned(planets: list[InjectedPlanet], seed: int = 1, **star_kw) -> CleanedLightCurve:
    star_kw.setdefault("trend_amplitude", 0.0)
    star_kw.setdefault("n_outliers", 0)
    lc = make_light_curve(SyntheticStar(StarTarget(name="test star"), planets, seed=seed, **star_kw))
    return CleanedLightCurve(lc=lc, trend=np.ones_like(lc.time), mask=np.ones(len(lc), dtype=bool))


def injected_candidate(planet: InjectedPlanet, snr: float = 50.0, sde: float = 20.0,
                       n_transits: int = 10) -> TransitCandidate:
    """The candidate a perfect search would report for ``planet`` (box ~80% of T14)."""
    return TransitCandidate(period=planet.period, t0=planet.t0,
                            duration=0.8 * planet.duration_hours / 24.0, depth=planet.depth,
                            snr=snr, sde=sde, n_transits=n_transits)
