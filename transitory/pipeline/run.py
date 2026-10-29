"""``analyze_star``: the one pipeline API the GUI and validation call.

Runs fetch -> clean -> search -> fit -> vet for one star.  A failing stage
is recorded in ``StarAnalysis.errors`` (and logged) and later stages that
need its output are skipped; ``AnalysisCancelled`` always propagates.
"""

from __future__ import annotations

import logging

from ..models import PlanetResult, ProgressCallback, StarAnalysis, StarTarget
from ..sources import LightCurveSource
from . import AnalysisCancelled
from .clean import clean_light_curve
from .fit import fit_transit
from .search import search_transits
from .vetting import vet_candidate

log = logging.getLogger(__name__)


def _stage_error(result: StarAnalysis, stage: str, exc: Exception) -> None:
    if isinstance(exc, NotImplementedError):
        log.warning("%s: %s stage not implemented: %s", result.target.name, stage, exc)
    else:
        log.exception("%s: %s stage failed", result.target.name, stage)
    result.errors.append(f"{stage}: {exc}")


def analyze_star(
    target: StarTarget,
    source: LightCurveSource,
    progress_cb: ProgressCallback | None = None,
) -> StarAnalysis:
    def progress(message: str, fraction: float) -> None:
        if progress_cb:
            progress_cb(f"{target.name}: {message}", fraction)

    result = StarAnalysis(target=target)

    progress("fetching light curve", 0.0)
    try:
        result.raw = source.fetch(target, progress_cb=lambda m, f: progress(m, 0.3 * f))
        result.credits = list(source.credits(result.raw))
    except AnalysisCancelled:
        raise
    except Exception as exc:
        _stage_error(result, "fetch", exc)
        return result

    progress("cleaning", 0.3)
    try:
        result.cleaned = clean_light_curve(result.raw)
    except AnalysisCancelled:
        raise
    except Exception as exc:
        _stage_error(result, "clean", exc)
        return result

    progress("searching for transits", 0.4)
    try:
        result.periodograms = search_transits(
            result.cleaned, progress_cb=lambda m, f: progress(m, 0.4 + 0.4 * f))
    except AnalysisCancelled:
        raise
    except Exception as exc:
        _stage_error(result, "search", exc)
        return result

    candidates = [p.best for p in result.periodograms if p.best is not None]
    for i, cand in enumerate(candidates):
        progress(f"fitting candidate {i + 1}/{len(candidates)}", 0.8 + 0.2 * i / len(candidates))
        planet = PlanetResult(candidate=cand)
        try:
            planet.fit = fit_transit(result.cleaned, cand, target)
        except AnalysisCancelled:
            raise
        except Exception as exc:
            _stage_error(result, f"fit #{i + 1}", exc)
        try:
            planet.vetting = vet_candidate(result.cleaned, cand, planet.fit, others=candidates)
        except AnalysisCancelled:
            raise
        except Exception as exc:
            _stage_error(result, f"vetting #{i + 1}", exc)
        result.planets.append(planet)

    progress("done", 1.0)
    return result
