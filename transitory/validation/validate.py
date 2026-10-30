"""Run the pipeline on stars with known answers and score what it finds.

For each target, ``analyze_star`` runs on its light curve.  Found candidates
are matched to the published planets by period, allowing the 1/2x and 2x
aliases BLS is prone to, and the rest are sorted out:

- a published planet with no match is a *miss*, with an explanation worked
  out from the data (too few transits covered, too shallow for the noise,
  beyond the search's period range, the search ran out of passes, ...);
- an unmatched candidate the vetting rejected (confidence "none") is a
  *rejected* detection: the pipeline flagged it itself;
- an unmatched candidate rated "duo-transit" (two transits, passing every
  check) is a *duo-transit*: not a planet candidate, so counted apart from
  both recoveries and spurious detections;
- an unmatched candidate rated "weak" or "strong" is a *spurious* detection
  (a false positive), also with an explanation.

Published values come from a provider: ``archive_provider`` (NASA
Exoplanet Archive, cached, with retrieval dates) for the real set, or
``synthetic_provider`` (the injected planets) for demo stars and tests.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

from ..catalog import archive
from ..catalog.client import CatalogClient, CatalogError
from ..config import MAX_PLANETS_PER_STAR, MIN_PERIOD_DAYS
from ..models import DUO_TRANSIT, LightCurve, PlanetResult, ProgressCallback, StarAnalysis
from ..pipeline import AnalysisCancelled
from ..pipeline.run import analyze_star, data_credits
from ..pipeline.noise import beta_factor, robust_std
from ..pipeline.search import SNR_THRESHOLD, count_transits
from ..sources import LightCurveSource, SyntheticSource
from .targets import ValidationTarget

log = logging.getLogger(__name__)

# found period / published period ratios accepted as a match, preferred in this order.
ALIASES = (1.0, 0.5, 2.0)
PERIOD_TOLERANCE = 0.01  # relative
# Light-curve time = BJD_TDB - offset (TESS: BTJD, Kepler: BKJD; synthetic times are used as is).
BJD_OFFSETS = {"TESS": 2457000.0, "Kepler": 2454833.0}
# Instrumental periods (days) a spurious signal may come from.
SYSTEMATIC_PERIODS = {
    "TESS": [(13.7, "the TESS orbit (13.7 d: scattered light and momentum dumps)"),
             (27.4, "a TESS sector (27.4 d)")],
    "Kepler": [(372.5, "the Kepler orbit (372.5 d)"), (93.0, "a Kepler quarter (~93 d)"),
               (3.0, "Kepler momentum dumps (~3 d)")],
}


@dataclass
class Published:
    """A published planet (or, for null stars, any published signal)."""

    name: str
    period: float  # days
    period_err: float | None = None  # days, 1 sigma
    t0_bjd: float | None = None  # BJD_TDB (synthetic: light-curve time)
    duration_h: float | None = None
    depth: float | None = None  # fractional
    rp_rs: float | None = None
    ref: str = ""
    disposition: str = "CONFIRMED"

    @classmethod
    def from_archive(cls, planet: archive.ArchivePlanet) -> Published:
        return cls(name=planet.name, period=planet.period if planet.period is not None else math.nan,
                   period_err=planet.period_err, t0_bjd=planet.t0_bjd, duration_h=planet.duration_h,
                   depth=planet.depth, rp_rs=planet.rp_rs, ref=planet.to_known().ref,
                   disposition=planet.disposition)


@dataclass
class PublishedSet:
    planets: list[Published] = field(default_factory=list)
    retrieved: str = ""  # YYYY-MM-DD
    source: str = ""  # where the values came from
    queries: list[str] = field(default_factory=list)
    credits: list[str] = field(default_factory=list)  # acknowledgement lines for where they came from


PublishedProvider = Callable[[ValidationTarget], PublishedSet]


def archive_provider(client: CatalogClient | None = None, refresh: bool = False) -> PublishedProvider:
    """Published values from the NASA Exoplanet Archive.

    Planet hosts: their confirmed transiting planets (pscomppars).  Null
    stars: every published signal (confirmed planets, KOIs, TOIs), which
    should be none; false-positive KOIs / TOIs are kept, so the report shows
    them, but they are not expected planets.
    """
    def provide(vt: ValidationTarget) -> PublishedSet:
        if vt.role == "null":
            result = archive.all_signals(vt.target, client=client, refresh=refresh)
            source = "NASA Exoplanet Archive pscomppars, cumulative KOI and TOI tables"
        else:
            result = archive.known_planets(vt.target, client=client, refresh=refresh)
            source = "NASA Exoplanet Archive pscomppars"
        return PublishedSet(planets=[Published.from_archive(p) for p in result.planets],
                            retrieved=result.retrieved, source=source, queries=list(result.queries),
                            credits=[archive.ARCHIVE_CREDIT])
    return provide


def synthetic_targets(source: SyntheticSource) -> list[ValidationTarget]:
    """The demo stars as a validation set: stars with injected planets are hosts, the rest null."""
    result = []
    for star in source.stars:
        names = [p.name for p in star.planets]
        result.append(ValidationTarget(target=star.target, role="planet-host" if names else "null",
                                       planets=names, expectation="synthetic (demo) star"))
    return result


def synthetic_provider(source: SyntheticSource) -> PublishedProvider:
    """The injected planets of synthetic stars, as "published" values."""
    def provide(vt: ValidationTarget) -> PublishedSet:
        star = source.find(vt.target)
        planets = [] if star is None else [
            Published(name=p.name, period=p.period, t0_bjd=p.t0, duration_h=p.duration_hours,
                      depth=p.depth, rp_rs=float(np.sqrt(p.depth)), ref="injected")
            for p in star.planets]
        return PublishedSet(planets=planets, retrieved=datetime.now(UTC).strftime("%Y-%m-%d"),
                            source="injected (synthetic)")
    return provide


# -- matching ------------------------------------------------------------------------

def found_period(planet: PlanetResult) -> float:
    if planet.fit is not None and np.isfinite(planet.fit.period):
        return planet.fit.period
    return planet.candidate.period


def alias_factor(found: float, published: float, tolerance: float = PERIOD_TOLERANCE) -> float | None:
    """The first of ALIASES with found ≈ factor × published, or None."""
    if not (np.isfinite(found) and np.isfinite(published) and published > 0):
        return None
    for factor in ALIASES:
        if abs(found / (factor * published) - 1.0) < tolerance:
            return factor
    return None


def match_planets(found: list[float], published: list[float],
                  tolerance: float = PERIOD_TOLERANCE) -> list[tuple[int, int, float]]:
    """(published index, found index, factor) pairs; each planet used at most once.

    True periods are matched before aliases.  When the search found the same
    signal twice, the earlier pass (lower found index) gets the match, then
    the closer period.
    """
    options = []
    for pi, pp in enumerate(published):
        for fi, fp in enumerate(found):
            factor = alias_factor(fp, pp, tolerance)
            if factor is not None:
                options.append((ALIASES.index(factor), fi, abs(fp / (factor * pp) - 1.0), pi, factor))
    options.sort()
    used_p, used_f, pairs = set(), set(), []
    for _, fi, _, pi, factor in options:
        if pi not in used_p and fi not in used_f:
            used_p.add(pi)
            used_f.add(fi)
            pairs.append((pi, fi, factor))
    return sorted(pairs)


# -- results ---------------------------------------------------------------------

@dataclass
class PlanetComparison:
    published: Published
    found_index: int | None = None  # index into analysis.planets
    factor: float | None = None  # found P / published P (1, 0.5 or 2)
    explanation: str = ""  # for a miss (or an alias), why

    @property
    def status(self) -> str:
        if self.found_index is None:
            return "missed"
        return "found" if self.factor == 1.0 else f"found at {_factor_text(self.factor)} alias"


@dataclass
class OtherDetection:
    """A found candidate matching no published planet."""

    index: int  # into analysis.planets
    kind: str  # "spurious", "duo-transit", "rejected" or "duplicate" (a matched planet found again)
    explanation: str


@dataclass
class TargetResult:
    vt: ValidationTarget
    analysis: StarAnalysis | None = None
    published: PublishedSet = field(default_factory=PublishedSet)
    comparisons: list[PlanetComparison] = field(default_factory=list)
    others: list[OtherDetection] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # validation-level (archive) errors
    elapsed_s: float = 0.0

    @property
    def name(self) -> str:
        return self.vt.target.name

    def planet(self, index: int) -> PlanetResult:
        return self.analysis.planets[index]


@dataclass
class ValidationRun:
    results: list[TargetResult]
    source_name: str
    max_products: int | None  # sectors / quarters cap used (None: all)
    started: datetime
    finished: datetime | None = None
    cancelled: bool = False

    @property
    def retrieved(self) -> str:
        """Oldest retrieval date of the published values used."""
        dates = [r.published.retrieved for r in self.results if r.published.retrieved]
        return min(dates) if dates else ""

    def tally(self) -> dict[str, int]:
        comps = [c for r in self.results for c in r.comparisons
                 if r.vt.role == "planet-host"]
        others = [(r, o) for r in self.results for o in r.others]
        return {
            "targets": len(self.results),
            "published": len(comps),
            "found": sum(1 for c in comps if c.status == "found"),
            "aliases": sum(1 for c in comps if c.found_index is not None and c.factor != 1.0),
            "missed": sum(1 for c in comps if c.found_index is None),
            "spurious_null": sum(1 for r, o in others if o.kind == "spurious" and r.vt.role == "null"),
            "spurious_host": sum(1 for r, o in others if o.kind == "spurious" and r.vt.role != "null"),
            "duo_null": sum(1 for r, o in others if o.kind == DUO_TRANSIT and r.vt.role == "null"),
            "duo_host": sum(1 for r, o in others if o.kind == DUO_TRANSIT and r.vt.role != "null"),
            "rejected": sum(1 for _, o in others if o.kind == "rejected"),
            "duplicates": sum(1 for _, o in others if o.kind == "duplicate"),
            "failed": sum(1 for r in self.results if r.analysis is None or not r.analysis.ok or r.errors),
        }


def _factor_text(factor: float | None) -> str:
    return {0.5: "1/2×", 2.0: "2×", 1.0: "1×"}.get(factor, f"{factor}×")


# -- explanations ------------------------------------------------------------------

def _lc_time(analysis: StarAnalysis) -> tuple[np.ndarray, np.ndarray]:
    lc = analysis.cleaned.lc
    good = np.isfinite(lc.time) & np.isfinite(lc.flux)
    return np.asarray(lc.time[good], float), np.asarray(lc.flux[good], float)


def _sde_at(analysis: StarAnalysis, period: float) -> list[float]:
    """Each search pass's SDE at ``period`` (max within the matching tolerance)."""
    values = []
    for pg in analysis.periodograms:
        near = np.abs(pg.periods / period - 1.0) < PERIOD_TOLERANCE / 2
        values.append(float(np.max(pg.power[near])) if np.any(near) else math.nan)
    return values


def explain_miss(pub: Published, analysis: StarAnalysis | None) -> str:
    """Why ``pub`` was not found, from what the data and the search show."""
    if analysis is None or analysis.raw is None:
        errors = "; ".join(analysis.errors) if analysis else "not analysed"
        return f"No light curve: {errors}."
    if analysis.cleaned is None or not analysis.periodograms:
        return f"The analysis stopped before the search: {'; '.join(analysis.errors) or 'no search'}."
    t, f = _lc_time(analysis)
    baseline = float(t.max() - t.min()) if len(t) else 0.0
    reasons = []
    if pub.period < MIN_PERIOD_DAYS:
        reasons.append(f"P = {pub.period:.3f} d is below the search's {MIN_PERIOD_DAYS} d minimum")
    if pub.period > baseline / 2:
        reasons.append(f"P = {pub.period:.1f} d is longer than half the {baseline:.0f} d baseline, "
                       "the longest period searched (at least 3 transits are needed in general)")
    duration = (pub.duration_h or 3.0) / 24.0
    n_transits = None
    if pub.t0_bjd is not None and np.isfinite(pub.period):
        t0 = pub.t0_bjd - BJD_OFFSETS.get(analysis.raw.mission, 0.0)
        n_transits = count_transits(t, pub.period, t0, duration)
        if n_transits < 3:
            reasons.append(f"the data cover only {n_transits} transit{'s' if n_transits != 1 else ''} "
                           "at the published ephemeris")
        if pub.depth:
            epoch = np.round((t - t0) / pub.period)
            dt = np.abs(t - (t0 + epoch * pub.period))
            n_in = int(np.sum(dt < 0.5 * duration))
            out = dt > duration
            sigma = robust_std(f[out]) * beta_factor(t[out], f[out], duration)
            if n_in and np.isfinite(sigma) and sigma > 0:
                # Red-noise aware, as the search's SNR is.
                snr = pub.depth / (sigma / math.sqrt(n_in))
                if snr < SNR_THRESHOLD:
                    reasons.append(f"too shallow for the noise: the published depth "
                                   f"({pub.depth * 1e6:.0f} ppm) gives an expected SNR of {snr:.1f}, "
                                   f"below the threshold of {SNR_THRESHOLD:.0f}")
                else:
                    reasons.append(f"expected SNR {snr:.0f} from the published depth "
                                   f"({pub.depth * 1e6:.0f} ppm) and {n_in:,} in-transit points")
    passes = analysis.periodograms
    if len(passes) >= MAX_PLANETS_PER_STAR and all(p.best is not None for p in passes):
        found = ", ".join(f"{p.best.period:.3f} d" for p in passes)
        reasons.append(f"the search stopped after its {MAX_PLANETS_PER_STAR} passes, "
                       f"which found {found}")
    sde = [s for s in _sde_at(analysis, pub.period) if np.isfinite(s)]
    if sde:
        reasons.append("periodogram SDE at the published period: "
                       + ", ".join(f"{s:.1f}" for s in sde) + " by pass (detection needs 7)")
    if not reasons:
        reasons.append("not recovered; no simple cause found")
    return "; ".join(reasons) + "."


def explain_other(planet: PlanetResult, analysis: StarAnalysis, published: list[Published],
                  matched: list[tuple[str, float]]) -> str:
    """Where an unmatched candidate may come from."""
    period = found_period(planet)
    reasons = []
    # A matched planet is also in ``published``: check each name once.
    for name, p in dict(matched + [(pub.name, pub.period) for pub in published]).items():
        for k, text in ((3.0, "3×"), (1 / 3, "1/3×"), (4.0, "4×"), (0.25, "1/4×"), (1.5, "3/2×"),
                        (2 / 3, "2/3×")):
            if np.isfinite(p) and abs(period / (k * p) - 1.0) < PERIOD_TOLERANCE:
                reasons.append(f"a {text} alias of {name} (P = {p:.4f} d)")
                break
    mission = analysis.raw.mission if analysis.raw is not None else ""
    for p_sys, what in SYSTEMATIC_PERIODS.get(mission, []):
        for k in (0.5, 1.0, 2.0):
            if abs(period / (k * p_sys) - 1.0) < 0.05:
                reasons.append(f"near {_factor_text(k)} {what}")
                break
    cand = planet.candidate
    if planet.is_duo_transit:
        reasons.append("only 2 events covered, passing every vetting check: a duo-transit, which "
                       "may be a planet on a long period or two unrelated dips from systematics or "
                       "stellar variability; more data must show a third transit")
    elif cand.n_transits <= 3:
        events = f"{cand.n_transits} event{'s' if cand.n_transits != 1 else ''}"
        reasons.append(f"only {events} covered: more likely individual systematics or stellar "
                       "variability than a periodic signal")
    rejected = planet.vetting is not None and planet.vetting.confidence == "none"
    if not reasons and not rejected:
        reasons.append("not an alias or a known systematic period; correlated noise or stellar "
                       "variability beyond what the red-noise-scaled SNR allows for is the likely cause")
    if planet.vetting is not None and planet.vetting.reasons:
        reasons.append("vetting: " + "; ".join(planet.vetting.reasons))
    return "; ".join(reasons or ["no explanation found"]) + "."


# -- running -----------------------------------------------------------------------

Analyzer = Callable[..., StarAnalysis]


def compare(vt: ValidationTarget, analysis: StarAnalysis, published: PublishedSet
            ) -> tuple[list[PlanetComparison], list[OtherDetection]]:
    """Match ``analysis`` to the published planets; explain misses and the leftovers."""
    expected = [p for p in published.planets
                if vt.role == "planet-host" or not p.disposition.upper().startswith(("FALSE", "FP", "FA"))]
    found = [found_period(p) for p in analysis.planets]
    pairs = match_planets(found, [p.period for p in expected])
    by_pub = {pi: (fi, factor) for pi, fi, factor in pairs}
    comparisons = []
    for pi, pub in enumerate(expected):
        fi, factor = by_pub.get(pi, (None, None))
        comp = PlanetComparison(published=pub, found_index=fi, factor=factor)
        if fi is None:
            comp.explanation = explain_miss(pub, analysis)
        elif factor != 1.0:
            comp.explanation = (f"found at {_factor_text(factor)} the published period, a BLS alias: "
                                "the period search locked onto a harmonic")
        comparisons.append(comp)
    matched_f = {fi for _, fi, _ in pairs}
    matched = [(expected[pi].name, expected[pi].period) for pi, _, _ in pairs]
    others = []
    for fi, planet in enumerate(analysis.planets):
        if fi in matched_f:
            continue
        again = next((name for name, p in matched if alias_factor(found[fi], p) is not None), None)
        if again is not None:
            explanation = (f"{again} found again in a later search pass (at "
                           f"{_factor_text(alias_factor(found[fi], dict(matched)[again]))} its period): "
                           "what the masking after its detection left behind (residual transit signal, "
                           "or the planet's secondary eclipse)")
            if planet.vetting is not None and planet.vetting.reasons:
                explanation += "; vetting: " + "; ".join(planet.vetting.reasons)
            others.append(OtherDetection(index=fi, kind="duplicate", explanation=explanation + "."))
            continue
        rejected = planet.vetting is not None and planet.vetting.confidence == "none"
        kind = "rejected" if rejected else DUO_TRANSIT if planet.is_duo_transit else "spurious"
        others.append(OtherDetection(index=fi, kind=kind,
                                     explanation=explain_other(planet, analysis, expected, matched)))
    return comparisons, others


def validate_target(vt: ValidationTarget, source: LightCurveSource, provider: PublishedProvider,
                    analyzer: Analyzer = analyze_star,
                    progress_cb: ProgressCallback | None = None) -> TargetResult:
    result = TargetResult(vt=vt)
    start = time.monotonic()
    try:
        result.published = provider(vt)
    except CatalogError as exc:
        log.warning("%s: published values unavailable: %s", vt.target.name, exc)
        result.errors.append(f"published values unavailable: {exc}")
    result.analysis = analyzer(vt.target, source, progress_cb=progress_cb)
    # The star's plots and credits then acknowledge the published values too.
    for line in result.published.credits:
        if line not in result.analysis.credits:
            result.analysis.credits.append(line)
    if not result.errors:
        result.comparisons, result.others = compare(vt, result.analysis, result.published)
    result.elapsed_s = time.monotonic() - start
    return result


def fetch_credits(vt: ValidationTarget, source: LightCurveSource, provider: PublishedProvider
                  ) -> tuple[LightCurve, list[str]]:
    """A target's light curve and credit lines as ``validate_target`` gives them, without the
    search: for credits a saved report did not record (from the cache, for a cached star)."""
    raw = source.fetch(vt.target)
    credits = data_credits(source, raw, vt.target)
    return raw, credits + [line for line in provider(vt).credits if line not in credits]


def run_validation(targets: list[ValidationTarget], source: LightCurveSource, provider: PublishedProvider,
                   analyzer: Analyzer = analyze_star, progress_cb: ProgressCallback | None = None,
                   max_products: int | None = None) -> ValidationRun:
    """Validate each target in turn; ``AnalysisCancelled`` from ``progress_cb`` stops the run.

    A cancelled run keeps the targets finished so far and is marked ``cancelled``.
    """
    run = ValidationRun(results=[], source_name=source.name, max_products=max_products,
                        started=datetime.now(UTC))
    n = len(targets)
    for i, vt in enumerate(targets):
        def progress(message: str, fraction: float, i=i) -> None:
            if progress_cb:
                progress_cb(f"[{i + 1}/{n}] {message}", (i + fraction) / n)

        try:
            progress(f"{vt.target.name}: starting", 0.0)
            result = validate_target(vt, source, provider, analyzer, progress)
        except AnalysisCancelled:
            run.cancelled = True
            break
        log.info("%s validated in %.0f s: %s", vt.target.name, result.elapsed_s,
                 ", ".join(c.status for c in result.comparisons) or "no published planets")
        run.results.append(result)
    run.finished = datetime.now(UTC)
    if progress_cb and not run.cancelled:
        progress_cb("validation finished", 1.0)
    return run
