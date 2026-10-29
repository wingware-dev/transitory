"""Shared data contracts between the pipeline, catalog, plots, GUI and validation.

Everything here is plain dataclasses over numpy arrays: no Qt, no network.
Times are in days (BTJD for TESS, BKJD for Kepler, or arbitrary for
synthetic data); flux is normalised to a median of about 1.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

Mission = Literal["TESS", "Kepler", "Synthetic"]
MissionHint = Literal["TESS", "Kepler", "any"]
Confidence = Literal["strong", "weak", "none"]

# progress_cb(message, fraction) with fraction in [0, 1].  A callback may raise
# (e.g. a cancellation exception) to stop the work in progress.
ProgressCallback = Callable[[str, float], None]


@dataclass
class StarTarget:
    """A star the user selected or the validation set names."""

    name: str
    tic: int | None = None
    kic: int | None = None
    ra: float | None = None  # degrees, ICRS
    dec: float | None = None  # degrees, ICRS
    tmag: float | None = None  # TESS magnitude (or Kepler mag if only that is known)
    mission_hint: MissionHint = "any"
    radius_rsun: float | None = None  # stellar radius, when known, for Rp in Earth radii
    teff_k: float | None = None  # effective temperature, for limb darkening
    logg: float | None = None  # surface gravity, log10(cm s^-2)
    mass_msun: float | None = None  # stellar mass; with the radius, a density prior on a/Rs

    @property
    def label(self) -> str:
        if self.tic is not None and f"{self.tic}" not in self.name:
            return f"{self.name} (TIC {self.tic})"
        if self.kic is not None and f"{self.kic}" not in self.name:
            return f"{self.name} (KIC {self.kic})"
        return self.name


@dataclass
class LightCurve:
    """Stitched brightness measurements for one star from one mission."""

    time: np.ndarray
    flux: np.ndarray
    flux_err: np.ndarray
    mission: Mission
    sectors_or_quarters: list[int] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)  # MAST product / obs ids
    dois: list[str] = field(default_factory=list)  # mission data DOIs
    author: str = ""  # pipeline that produced it, e.g. "SPOC", "Kepler"
    cadence_s: float | None = None

    def __len__(self) -> int:
        return len(self.time)

    @property
    def baseline_days(self) -> float:
        return float(np.nanmax(self.time) - np.nanmin(self.time)) if len(self.time) else 0.0


@dataclass
class CleanedLightCurve:
    """A light curve after quality masking, outlier removal and detrending.

    ``lc`` holds the flattened (detrended) flux; ``trend`` is the removed
    trend on the same time grid as the raw curve; ``mask`` marks raw points
    kept (True) in ``lc``.
    """

    lc: LightCurve
    trend: np.ndarray
    mask: np.ndarray
    cleaning_log: list[str] = field(default_factory=list)


@dataclass
class PeriodogramResult:
    """One Box Least Squares search pass."""

    periods: np.ndarray  # days
    power: np.ndarray  # SDE-normalised or raw BLS power
    best: TransitCandidate | None = None


@dataclass
class TransitCandidate:
    period: float  # days
    t0: float  # mid-transit time, same time system as the light curve
    duration: float  # days
    depth: float  # fractional (e.g. 0.001 = 1000 ppm)
    snr: float
    sde: float
    n_transits: int


@dataclass
class FitResult:
    period: float  # days
    t0: float
    rp_rs: float
    a_rs: float
    b: float
    duration_hours: float  # T14
    depth: float  # fractional
    uncertainties: dict[str, float] = field(default_factory=dict)  # 1-sigma, keyed by field name
    chi2: float = float("nan")  # reduced chi-squared
    limb_darkening: tuple[float, float] | None = None  # quadratic (u1, u2) the fit held fixed
    a_rs_prior: float | None = None  # a/Rs from the stellar density, when it constrained the fit
    beta: float = 1.0  # red-noise factor the uncertainties were scaled by
    model_time: np.ndarray = field(default_factory=lambda: np.empty(0))  # phase grid (days from t0)
    model_flux: np.ndarray = field(default_factory=lambda: np.empty(0))


@dataclass
class Vetting:
    odd_even_sigma: float  # odd vs even depth difference in sigma
    secondary_depth: float  # fractional depth at phase 0.5
    confidence: Confidence
    reasons: list[str] = field(default_factory=list)


@dataclass
class PlanetResult:
    candidate: TransitCandidate
    fit: FitResult | None = None
    vetting: Vetting | None = None


@dataclass
class StarAnalysis:
    """Everything the pipeline found for one star; what the GUI and validation consume."""

    target: StarTarget
    raw: LightCurve | None = None
    cleaned: CleanedLightCurve | None = None
    periodograms: list[PeriodogramResult] = field(default_factory=list)  # one per search pass
    planets: list[PlanetResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    credits: list[str] = field(default_factory=list)  # acknowledgement lines for the data used

    @property
    def ok(self) -> bool:
        return self.raw is not None and not self.errors


@dataclass
class KnownPlanet:
    """A published planet, for validation and table comparison."""

    name: str
    period: float  # days
    rp_rs: float | None = None
    duration_h: float | None = None
    depth: float | None = None  # fractional
    ref: str = ""  # literature or archive reference
