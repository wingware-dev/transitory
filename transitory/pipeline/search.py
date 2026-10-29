"""Iterative Box Least Squares transit search.

Stub: the signature is the contract; the "search, transit model, fit,
vetting and analyze_star" step fills it in.
"""

from __future__ import annotations

from ..config import MAX_PLANETS_PER_STAR, MIN_PERIOD_DAYS
from ..models import CleanedLightCurve, PeriodogramResult, ProgressCallback


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
    raise NotImplementedError("search_transits is not implemented yet")
