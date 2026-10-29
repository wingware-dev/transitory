"""Made-up light curves with planets injected at known periods and depths.

Used by tests (no downloads) and by the dashboard's ``--demo`` mode.  The
transit shape is a symmetric trapezoid, which is deliberately simpler than
the pipeline's limb-darkened fit model so tests don't just fit their own
generator.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field

import numpy as np

from .models import KnownPlanet, LightCurve, StarTarget


@dataclass
class InjectedPlanet:
    period: float  # days
    t0: float  # days, first mid-transit
    depth: float  # fractional
    duration_hours: float  # T14
    ingress_fraction: float = 0.15  # ingress time / T14
    odd_even_ratio: float = 1.0  # depth of odd transits / even ones (EB stand-in when != 1)
    secondary_depth: float = 0.0  # fractional dip at phase 0.5 (EB stand-in when > 0)
    name: str = ""

    def as_known(self, ref: str = "injected") -> KnownPlanet:
        return KnownPlanet(
            name=self.name,
            period=self.period,
            rp_rs=float(np.sqrt(self.depth)),
            duration_h=self.duration_hours,
            depth=self.depth,
            ref=ref,
        )


@dataclass
class SyntheticStar:
    target: StarTarget
    planets: list[InjectedPlanet] = field(default_factory=list)
    noise_ppm: float = 300.0  # white noise per cadence
    baseline_days: float = 54.0  # two TESS sectors
    cadence_minutes: float = 10.0
    trend_amplitude: float = 0.002  # slow stellar / systematic variability
    trend_period_days: float = 7.3
    gaps: list[tuple[float, float]] = field(default_factory=list)  # (start, end) days removed
    n_outliers: int = 20  # positive flares / cosmic rays
    seed: int | None = None  # None: derived from the target name


def trapezoid_transit(time: np.ndarray, planet: InjectedPlanet) -> np.ndarray:
    """Relative flux (1 out of transit) for one injected planet, odd/even and secondary included."""
    period = planet.period
    t14 = planet.duration_hours / 24.0
    tau = max(planet.ingress_fraction * t14, 1e-6)
    epoch = np.round((time - planet.t0) / period)
    dt = np.abs(time - (planet.t0 + epoch * period))

    shape = np.clip((t14 / 2.0 - dt) / tau, 0.0, 1.0)
    depth = np.where(epoch % 2 == 1, planet.depth * planet.odd_even_ratio, planet.depth)
    flux = 1.0 - depth * shape

    if planet.secondary_depth > 0:
        sec_epoch = np.round((time - planet.t0 - period / 2.0) / period)
        sdt = np.abs(time - (planet.t0 + period / 2.0 + sec_epoch * period))
        sec_shape = np.clip((t14 / 2.0 - sdt) / tau, 0.0, 1.0)
        flux -= planet.secondary_depth * sec_shape
    return flux


def _seed_for(star: SyntheticStar) -> int:
    if star.seed is not None:
        return star.seed
    return zlib.crc32(star.target.name.encode("utf-8"))


def make_light_curve(star: SyntheticStar) -> LightCurve:
    """Build a noisy, trended, gappy light curve with the star's planets injected."""
    rng = np.random.default_rng(_seed_for(star))
    step = star.cadence_minutes / (24.0 * 60.0)
    time = np.arange(0.0, star.baseline_days, step)
    keep = np.ones(time.shape, dtype=bool)
    for start, end in star.gaps:
        keep &= ~((time >= start) & (time < end))
    time = time[keep]

    flux = np.ones_like(time)
    for planet in star.planets:
        flux *= trapezoid_transit(time, planet)

    phase = rng.uniform(0, 2 * np.pi)
    flux *= 1.0 + star.trend_amplitude * np.sin(2 * np.pi * time / star.trend_period_days + phase)
    sigma = star.noise_ppm * 1e-6
    flux += rng.normal(0.0, sigma, time.shape)

    if star.n_outliers and len(time):
        idx = rng.choice(len(time), size=min(star.n_outliers, len(time)), replace=False)
        flux[idx] += rng.uniform(5, 20, idx.shape) * sigma

    return LightCurve(
        time=time,
        flux=flux,
        flux_err=np.full_like(time, sigma),
        mission="Synthetic",
        sectors_or_quarters=[],
        source_ids=[f"synthetic:{star.target.name}"],
        dois=[],
        author="transitory.synthetic",
        cadence_s=star.cadence_minutes * 60.0,
    )


def _demo(name, ra, dec, tmag, planets, **kw) -> SyntheticStar:
    return SyntheticStar(
        target=StarTarget(name=name, ra=ra, dec=dec, tmag=tmag, mission_hint="any", radius_rsun=1.0),
        planets=planets,
        **kw,
    )


def demo_stars() -> list[SyntheticStar]:
    """The fixed made-up stars the ``--demo`` dashboard shows on its sky."""
    return [
        _demo("Demo Hot Jupiter", 285.0, 45.0, 9.5,
              [InjectedPlanet(3.21, 1.3, 0.010, 2.9, name="Demo Hot Jupiter b")]),
        _demo("Demo Super-Earth", 290.5, 42.3, 8.7,
              [InjectedPlanet(5.74, 2.1, 0.0009, 2.4, name="Demo Super-Earth b")],
              noise_ppm=200.0),
        _demo("Demo Two-Planet", 293.2, 47.8, 10.1,
              [InjectedPlanet(2.43, 0.8, 0.003, 2.0, name="Demo Two-Planet b"),
               InjectedPlanet(9.87, 4.4, 0.005, 3.8, name="Demo Two-Planet c")]),
        _demo("Demo Eclipsing Binary", 281.7, 39.6, 11.2,
              [InjectedPlanet(1.87, 0.5, 0.02, 3.1, odd_even_ratio=0.6, secondary_depth=0.004,
                              name="Demo EB")]),
        _demo("Demo Quiet Star", 296.8, 44.1, 8.9, [], noise_ppm=250.0),
        _demo("Demo Long Period", 90.0, -66.5, 9.8,
              [InjectedPlanet(21.6, 7.0, 0.004, 5.5, name="Demo Long Period b")]),
    ]
