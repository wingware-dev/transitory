"""Light-curve cleaning: quality/NaN masking, upward sigma-clipping,
per-sector normalisation and flattening.

Stub: the signature is the contract; the "fetch/cache and cleaning" step fills it in.
"""

from __future__ import annotations

from ..config import MAX_TRANSIT_DURATION_HOURS
from ..models import CleanedLightCurve, LightCurve, ProgressCallback


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
    raise NotImplementedError("clean_light_curve is not implemented yet")
