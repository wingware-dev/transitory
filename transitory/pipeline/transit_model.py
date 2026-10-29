"""Limb-darkened transit light-curve model (quadratic law, numpy only).

Stub: the signatures are the contract; the "search, transit model, fit,
vetting and analyze_star" step fills them in.
"""

from __future__ import annotations

import numpy as np

DEFAULT_U1 = 0.40
DEFAULT_U2 = 0.25


def transit_flux(
    time: np.ndarray,
    t0: float,
    period: float,
    rp_rs: float,
    a_rs: float,
    b: float,
    u1: float = DEFAULT_U1,
    u2: float = DEFAULT_U2,
) -> np.ndarray:
    """Relative flux (1 out of transit) for a circular orbit."""
    raise NotImplementedError("transit_flux is not implemented yet")


def t14_hours(period: float, rp_rs: float, a_rs: float, b: float) -> float:
    """Total transit duration (first to fourth contact) in hours."""
    raise NotImplementedError("t14_hours is not implemented yet")
