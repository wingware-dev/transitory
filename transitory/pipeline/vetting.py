"""Candidate vetting: SNR, SDE, transit count, odd/even depths, secondary eclipse.

Stub: the signature is the contract; the "search, transit model, fit,
vetting and analyze_star" step fills it in.
"""

from __future__ import annotations

from ..models import CleanedLightCurve, FitResult, TransitCandidate, Vetting


def vet_candidate(
    cleaned: CleanedLightCurve,
    candidate: TransitCandidate,
    fit: FitResult | None = None,
) -> Vetting:
    """Grade a candidate 'strong', 'weak' or 'none', with the reasons."""
    raise NotImplementedError("vet_candidate is not implemented yet")
