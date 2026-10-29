"""Least-squares fit of the transit model to a BLS candidate.

Stub: the signature is the contract; the "search, transit model, fit,
vetting and analyze_star" step fills it in.
"""

from __future__ import annotations

from ..models import CleanedLightCurve, FitResult, TransitCandidate


def fit_transit(cleaned: CleanedLightCurve, candidate: TransitCandidate) -> FitResult:
    """Fit t0, period, rp/rs, a/rs and b starting from ``candidate``.

    Uncertainties come from the Jacobian covariance; ``model_time`` /
    ``model_flux`` sample the best model over one phase (days from t0).
    """
    raise NotImplementedError("fit_transit is not implemented yet")
