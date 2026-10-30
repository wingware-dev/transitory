"""Write a validation run as Markdown, a JSON summary and PNG figures.

``write_report(run, directory)`` writes ``validation.md``, ``results.json``,
``summary.png`` (found vs. published period, Rp/Rs and T14) and one folded
light-curve PNG per detection into ``directory`` (``reports/validation`` by
default); ``publish`` copies them to the committed ``docs/validation``.
"""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import numpy as np
from matplotlib.figure import Figure
from matplotlib.ticker import NullFormatter, ScalarFormatter

from .. import plots
from ..catalog.archive import ARCHIVE_CREDIT, ARCHIVE_REFERENCES
from ..config import MAX_PLANETS_PER_STAR, MIN_PERIOD_DAYS, PROJECT_ROOT, reports_dir
from ..models import DUO_TRANSIT
from ..pipeline.search import MIN_TRANSITS, SDE_THRESHOLD, SDE_TWO_TRANSITS, SNR_THRESHOLD
from .validate import PlanetComparison, TargetResult, ValidationRun, found_period

REPORT_NAME = "validation.md"
JSON_NAME = "results.json"
SUMMARY_PNG = "summary.png"
FIGURE_DIR = "figures"
DOCS_DIR = PROJECT_ROOT / "docs" / "validation"
FIGURE_DPI = 90  # folded light curves: ~35 of them are committed, so keep them small


def default_directory() -> Path:
    return reports_dir() / "validation"


# -- formatting ----------------------------------------------------------------------

def _ok(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)


def _num(x, digits: int = 4) -> str:
    return f"{x:.{digits}f}" if _ok(x) else "—"


def _pct(found, published) -> str:
    if not (_ok(found) and _ok(published) and published):
        return "—"
    return f"{100.0 * (found - published) / published:+.1f}%"


def _cap(run: ValidationRun) -> str:
    return "all sectors / quarters" if run.max_products is None else \
        f"at most {run.max_products} sectors / quarters per star"


def _uses_archive(run: ValidationRun) -> bool:
    return any("Exoplanet Archive" in r.published.source for r in run.results)


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|")


def _planet_values(result: TargetResult, comp: PlanetComparison) -> dict:
    """Published and found values for one comparison, as plain numbers (None: unknown)."""
    pub = comp.published
    values = {
        "star": result.name, "planet": pub.name, "status": comp.status, "factor": comp.factor,
        "period_pub": pub.period, "period_err_pub": pub.period_err, "rp_rs_pub": pub.rp_rs,
        "t14_pub": pub.duration_h, "depth_pub": pub.depth, "ref": pub.ref, "explanation": comp.explanation,
        "period": None, "period_err": None, "rp_rs": None, "rp_rs_err": None, "t14": None,
        "t14_err": None, "snr": None, "sde": None, "confidence": None, "letter": None,
    }
    if comp.found_index is not None:
        planet = result.planet(comp.found_index)
        fit = planet.fit
        err = fit.uncertainties if fit is not None else {}
        values.update(
            letter=plots.planet_letter(comp.found_index), period=found_period(planet),
            period_err=err.get("period"), rp_rs=fit.rp_rs if fit else None, rp_rs_err=err.get("rp_rs"),
            t14=fit.duration_hours if fit else planet.candidate.duration * 24.0,
            t14_err=err.get("duration_hours"), snr=planet.candidate.snr, sde=planet.candidate.sde,
            confidence=planet.vetting.confidence if planet.vetting else None)
    return {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in values.items()}


def _period_sigma(v: dict) -> float | None:
    """|found − published period| in units of the fit's and the published errors combined.

    Published periods are often rounded and less precise than a fit over
    years of data, so the fit's error alone overstates the difference.
    """
    if v["period"] is None or not (_ok(v["period_err"]) and v["period_err"] > 0):
        return None
    factor = v["factor"] or 1.0
    pub_err = factor * v["period_err_pub"] if _ok(v["period_err_pub"]) and v["period_err_pub"] > 0 else 0.0
    return abs(v["period"] - v["period_pub"] * factor) / math.hypot(v["period_err"], pub_err)


def _period_cell(v: dict) -> str:
    if v["period"] is None:
        return "—"
    expected = v["period_pub"] * (v["factor"] or 1.0)
    diff = v["period"] - expected
    err = v["period_err"] if _ok(v["period_err"]) and v["period_err"] > 0 else None
    # Enough decimals for two significant digits of the error (at least 5).
    digits = max(5, min(8, 1 - int(math.floor(math.log10(err))))) if err else 5
    text = f"{v['period']:.{digits}f}" + (f" ± {err:.{digits}f}" if err else "")
    sigma = _period_sigma(v)
    return f"{text} ({diff:+.{digits}f} d" + (f", {sigma:.1f}σ" if sigma is not None else "") + ")"


# -- figures -----------------------------------------------------------------------

def summary_figure(run: ValidationRun) -> Figure:
    rows = [_planet_values(r, c) for r in run.results for c in r.comparisons
            if r.vt.role == "planet-host" and c.found_index is not None]
    fig = Figure(figsize=(12, 4.6))
    axes = fig.subplots(1, 3)
    panels = [("Period", "period_pub", "period", "d", True),
              ("Rp/Rs", "rp_rs_pub", "rp_rs", "", False),
              ("T14", "t14_pub", "t14", "h", False)]
    for ax, (title, pub_key, found_key, unit, log) in zip(axes, panels):
        pts = [(v[pub_key] * (v["factor"] or 1.0) if pub_key == "period_pub" else v[pub_key],
                v[found_key], v["planet"], v["factor"]) for v in rows
               if v[pub_key] is not None and v[found_key] is not None]
        if pts:
            x = np.array([p[0] for p in pts])
            y = np.array([p[1] for p in pts])
            if pub_key == "period_pub":
                ax.scatter(x, (y / x - 1.0) * 1e6, c="#1f77b4")
                ax.axhline(0, color="#888", lw=0.8)
                ax.set_xscale("log")
                ax.xaxis.set_major_formatter(ScalarFormatter())
                ax.xaxis.set_minor_formatter(NullFormatter())
                ax.set_ylabel("found / published − 1 (ppm)")
            else:
                lo, hi = min(x.min(), y.min()) * 0.9, max(x.max(), y.max()) * 1.1
                ax.plot([lo, hi], [lo, hi], color="#888", lw=0.8)
                ax.scatter(x, y, c="#1f77b4")
                ax.set_ylabel(f"found{f' ({unit})' if unit else ''}")
                if log:
                    ax.set_xscale("log")
            for xi, yi, name, factor in pts:
                yv = (yi / xi - 1.0) * 1e6 if pub_key == "period_pub" else yi
                label = name + ("" if factor in (None, 1.0) else f" ({factor:g}×)")
                ax.annotate(label, (xi, yv), fontsize=7, xytext=(3, 3), textcoords="offset points")
        else:
            ax.text(0.5, 0.5, "no recovered planets", ha="center", va="center", transform=ax.transAxes)
        ax.set_title(title)
        ax.set_xlabel(f"published{f' ({unit})' if unit else ''}")
        ax.grid(alpha=0.3)
    tally = run.tally()
    fig.suptitle(f"Known-planet validation: {tally['found'] + tally['aliases']} of {tally['published']} "
                 f"recovered, {tally['missed']} missed, {tally['spurious_null'] + tally['spurious_host']} "
                 f"spurious, {tally['duo_null'] + tally['duo_host']} duo-transit "
                 f"({run.source_name}, {_cap(run)})", fontsize=11, x=0.01, ha="left")
    if _uses_archive(run):
        fig.text(0.01, 0.01, f"Published values: NASA Exoplanet Archive (retrieved {run.retrieved}). "
                 + ARCHIVE_CREDIT, fontsize=6, color="#555", wrap=True)
    fig.subplots_adjust(left=0.06, right=0.99, top=0.84, bottom=0.2, wspace=0.3)
    return fig


def _folded_pngs(run: ValidationRun, directory: Path) -> dict[tuple[str, int], str]:
    """One folded light-curve PNG per detection; {(star, index): relative path}."""
    paths = {}
    for result in run.results:
        analysis = result.analysis
        if analysis is None or analysis.cleaned is None:
            continue
        for i in range(len(analysis.planets)):
            name = f"{plots.slug(result.name)}-{plots.planet_letter(i)}.png"
            plots.save_png(plots.folded_figure(analysis, i), directory / FIGURE_DIR / name, dpi=FIGURE_DPI)
            paths[(result.name, i)] = f"{FIGURE_DIR}/{name}"
    return paths


# -- Markdown --------------------------------------------------------------------

def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'s' if n != 1 else ''}"


def _data_line(result: TargetResult) -> str:
    a = result.analysis
    if a is None or a.raw is None:
        return "no light curve"
    raw = a.raw
    n = len(raw.sectors_or_quarters)
    what = "sector" if raw.mission == "TESS" else "quarter" if raw.mission == "Kepler" else "segment"
    parts = [raw.mission]
    if raw.author and raw.mission in ("TESS", "Kepler") and raw.author != raw.mission:
        parts.append(raw.author)
    if n:
        parts.append(_count(n, what))
    return f"{' '.join(parts)}, {len(raw):,} points, {raw.baseline_days:,.0f} d baseline"


def run_findings(run: ValidationRun) -> list[str]:
    """Patterns across the run, worked out from the results (not hand-written)."""
    findings = []
    comps = [(r, c) for r in run.results if r.vt.role == "planet-host" for c in r.comparisons]
    exhausted = [c.published.name for _, c in comps
                 if c.found_index is None and "search stopped after its" in c.explanation]
    if exhausted:
        findings.append(f"{', '.join(exhausted)} missed because the search spent its "
                        f"{MAX_PLANETS_PER_STAR} passes on other signals, though the planet is "
                        "detectable (see Missed planets).")
    others = [(r, o, r.planet(o.index)) for r in run.results for o in r.others]
    extra = [(r, o, p) for r, o, p in others if o.kind != "duplicate"]
    few = [x for x in extra if x[2].candidate.n_transits <= 2]
    if few:
        stars = sorted({r.name for r, _, _ in few})
        periods = sorted(found_period(p) for _, _, p in few)
        n_rejected = sum(1 for _, o, _ in few if o.kind == "rejected")
        n_duo = sum(1 for _, o, _ in few if o.kind == DUO_TRANSIT)
        text = (f"{len(few)} of {len(extra)} extra candidates cover at most 2 transits, at periods of "
                f"{periods[0]:.0f}–{periods[-1]:.0f} d"
                + (", too few to show periodicity on their own" if n_duo
                   else ": dips from systematics or variability"))
        if any(p.candidate.n_transits == 2 for _, _, p in few):
            text += ((", though the search accepts them" if n_duo else ", which the search accepts")
                     + f" as periodic when 2 dips agree at SDE ≥ {SDE_TWO_TRANSITS:g}")
        missed = any(c.found_index is None for _, c in comps)
        text += (" (on " + ", ".join(stars) + "). "
                 + ("They take search passes from real planets; " if exhausted else
                    "" if missed else "No planet was missed for them, but they fill the remaining "
                    "search passes; ")
                 + f"the vetting rejects {n_rejected} of them afterwards"
                 + (f" and lists {n_duo} as duo-transits, which may be long-period planets "
                    "(next item)" if n_duo else "") + ".")
        findings.append(text)
    duos = [(r, o, p) for r, o, p in extra if o.kind == DUO_TRANSIT]
    if duos:
        n_sys = sum(1 for _, o, _ in duos if "near " in o.explanation)
        findings.append(
            f"{_count(len(duos), 'duo-transit signal')} ("
            + ", ".join(f"{r.name} {plots.planet_letter(o.index)} at {found_period(p):.0f} d"
                        for r, o, p in duos)
            + "): 2 transits that pass every vetting check, including a fitted T14 that agrees with "
            "the dip's width. A real planet on a long period can show only two transits, so they are "
            "kept, but two dips can't show periodicity: they are counted neither as recoveries nor "
            "as spurious detections"
            + (f"; {n_sys} lie near an instrumental period, which makes systematics the likelier "
               "cause" if n_sys else "") + ".")
    spurious = [(r, o, p) for r, o, p in extra if o.kind == "spurious"]
    if spurious:
        ratings = sorted({p.vetting.confidence for _, _, p in spurious if p.vetting is not None})
        n_few = sum(1 for _, _, p in spurious if p.candidate.n_transits <= 3)
        n_sys = sum(1 for _, o, _ in spurious if "near " in o.explanation)
        one = len(spurious) == 1
        max_snr = max(p.candidate.snr for _, _, p in spurious)
        findings.append(
            f"{_count(len(spurious), 'spurious detection')} passed vetting as "
            + " or ".join(f"'{c}'" for c in ratings) + " ("
            + ", ".join(sorted({r.name for r, _, _ in spurious})) + "); "
            + (("it covers at most 3 transits" if n_few else "it covers more than 3 transits") if one
               else f"{n_few} of them cover at most 3 transits")
            + ((" and lies" if one else f" and {n_sys}" + (" lies" if n_sys == 1 else " lie"))
               + " near an instrumental period" if n_sys else "")
            + (f". Its SNR ({max_snr:.0f}) already allows" if one
               else f". Their SNRs (up to {max_snr:.0f}) already allow")
            + " for correlated noise (the red-noise beta factor), so the SNR cannot tell "
            + ("it" if one else "them") + " from a planet: with so few events, a recurring instrumental "
            "or stellar dip looks like a transit.")
    dups = [(r, o, p) for r, o, p in others if o.kind == "duplicate"]
    if dups:
        findings.append("Found again by a later search pass: "
                        + "; ".join(f"{o.explanation.split(' found again')[0]} on {r.name} "
                                    f"({p.candidate.depth * 1e6:.0f} ppm)" for r, o, p in dups)
                        + ". The search masks a detected planet's transits, and its secondary eclipse "
                        "when it sees one, but signal left in or around the masked transits remains at "
                        "the same period.")
    eclipse_only = [c.published.name for r, c in comps if c.found_index is not None
                    and (p := r.planet(c.found_index)).vetting is not None
                    and p.vetting.confidence == "weak" and p.vetting.reasons
                    and all("small enough to be planetary emission" in s for s in p.vetting.reasons)]
    if eclipse_only:
        findings.append(f"{', '.join(eclipse_only)} rated only 'weak' because the vetting detected a "
                        "secondary eclipse, although it calls the eclipse small enough to be the planet's "
                        "own emission (as published for hot Jupiters such as these). A planetary-size "
                        "secondary eclipse should not lower the confidence of a deep, many-transit signal.")
    values = [(r, c, _planet_values(r, c)) for r, c in comps if c.found_index is not None]
    off_p = [(c.published.name, s) for r, c, v in values
             if (s := _period_sigma(v)) is not None and s > 5]
    if off_p:
        findings.append("Found periods differ from the published ones by more than 5σ (the fit's and "
                        "the published errors combined) for "
                        + ", ".join(f"{n} ({s:.0f}σ)" for n, s in off_p)
                        + ". The fit's errors already allow for red noise and transit-timing scatter; "
                        "the published period comes from a different time span, and transit-timing "
                        "variations shift the mean period with the span observed"
                        + (" (TOI-270 c and d are near a 2:1 resonance)"
                           if any(n.startswith("TOI-270") for n, _ in off_p) else "") + ".")
    rp = [(c.published.name, r.vt.target.radius_rsun, (v["rp_rs"] - v["rp_rs_pub"]) / v["rp_rs_pub"], v)
          for r, c, v in values if v["rp_rs"] is not None and v["rp_rs_pub"]]
    if rp:
        off = [(name, rs, d, v) for name, rs, d, v in rp if abs(d) > 0.10]
        text = (f"Rp/Rs agrees with the published value within a median of "
                f"{100 * float(np.median([abs(d) for _, _, d, _ in rp])):.1f}%")
        if off:
            small = [n for n, rs, _, _ in off if rs is not None and rs < 0.6]
            long_t14 = [n for n, _, _, v in off if _ok(v["t14"]) and _ok(v["t14_pub"]) and v["t14_pub"]
                        and abs(v["t14"] / v["t14_pub"] - 1.0) > 0.10]
            text += ("; beyond 10%: " + ", ".join(f"{n} ({100 * d:+.0f}%)" for n, _, d, _ in off)
                     + ". The fit takes limb darkening for the mission's bandpass and the star's Teff "
                     "and constrains a/Rs by the stellar density, but shallow transits still leave "
                     "Rp/Rs degenerate with the impact parameter"
                     + (f"; for {', '.join(long_t14)} T14 is also off by more than 10%, so the fitted "
                        "transit shape differs from the published one" if long_t14 else "")
                     + (f"; limb darkening is least certain for M dwarfs (the host of {', '.join(small)})"
                        if small else ""))
        findings.append(text + ".")
    failed = [r.name for r in run.results if r.analysis is None or not r.analysis.ok or r.errors]
    if failed:
        findings.append(f"Errors on {', '.join(failed)} (see Targets).")
    return findings


def report_markdown(run: ValidationRun, figures: dict[tuple[str, int], str]) -> str:
    tally = run.tally()
    out = ["# Known-planet validation", ""]
    when = run.started.strftime("%Y-%m-%d %H:%M UTC")
    minutes = ((run.finished or run.started) - run.started).total_seconds() / 60
    if _uses_archive(run):
        published = (f"Published values: NASA Exoplanet Archive, retrieved {run.retrieved or 'n/a'} "
                     "(pscomppars for planets; pscomppars, the cumulative KOI table and the TOI table "
                     "to check that the null stars have no published signal).")
    else:
        sources = sorted({r.published.source for r in run.results if r.published.source})
        published = f"Published values: {', '.join(sources) or 'none'}."
    out += [
        f"Run {when} ({minutes:.0f} min) on **{run.source_name}** light curves, using {_cap(run)}.",
        "",
        published,
        "",
        f"Pipeline: BLS search from {MIN_PERIOD_DAYS:g} d to half the baseline, up to "
        f"{MAX_PLANETS_PER_STAR} passes per star, detection at SDE ≥ {SDE_THRESHOLD:g} and "
        f"SNR ≥ {SNR_THRESHOLD:g} (red-noise scaled), backed by {MIN_TRANSITS}+ transits or 2 "
        f"agreeing ones at SDE ≥ {SDE_TWO_TRANSITS:g}; a peak with fewer is masked and the pass "
        "searches again. A found period matches a published one within 1%, also at "
        "the 1/2× and 2× aliases; period differences are in units of the fit's and the published "
        "errors combined.",
        "",
    ]
    if run.cancelled:
        out += ["**The run was cancelled; only the targets below were validated.**", ""]
    n_null = sum(1 for r in run.results if r.vt.role == "null")
    out += ["## Summary", "",
            "| | |", "|---|---|",
            f"| Targets | {tally['targets']} ({_count(n_null, 'null star')}) |",
            f"| Published planets | {tally['published']} |",
            f"| Recovered at the published period | {tally['found']} |",
            f"| Recovered at an alias | {tally['aliases']} |",
            f"| Missed | {tally['missed']} |",
            f"| Spurious detections on null stars | {tally['spurious_null']} |",
            f"| Spurious detections on planet hosts | {tally['spurious_host']} |",
            f"| Duo-transit signals (2 transits, not candidates) on null stars | {tally['duo_null']} |",
            f"| Duo-transit signals on planet hosts | {tally['duo_host']} |",
            f"| Extra candidates rejected by vetting | {tally['rejected']} |",
            f"| Planets found twice (duplicates) | {tally['duplicates']} |",
            f"| Targets with errors | {tally['failed']} |",
            "", f"![Summary]({SUMMARY_PNG})", ""]
    findings = run_findings(run)
    if findings:
        out += ["## What the run shows", ""] + [f"- {f}" for f in findings] + [""]

    out += ["## Planets: found vs. published", "",
            "| Planet | Status | P published (d) | P found (d), difference | Rp/Rs published | "
            "Rp/Rs found | Δ | T14 published (h) | T14 found (h) | Δ | SNR | SDE | Confidence |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in run.results:
        if r.vt.role != "planet-host":
            continue
        for c in r.comparisons:
            v = _planet_values(r, c)
            rp = _num(v["rp_rs"]) + (f" ± {v['rp_rs_err']:.4f}" if _ok(v["rp_rs_err"]) else "")
            t14 = _num(v["t14"], 2) + (f" ± {v['t14_err']:.2f}" if _ok(v["t14_err"]) else "")
            out.append(" | ".join([
                f"| {v['planet']}" + (f" ({v['letter']})" if v["letter"] else ""), v["status"],
                _num(v["period_pub"], 5), _period_cell(v), _num(v["rp_rs_pub"]), rp,
                _pct(v["rp_rs"], v["rp_rs_pub"]), _num(v["t14_pub"], 2), t14, _pct(v["t14"], v["t14_pub"]),
                _num(v["snr"], 1), _num(v["sde"], 1), v["confidence"] or "—"]) + " |")
    out.append("")

    misses = [(r, c) for r in run.results for c in r.comparisons if c.found_index is None]
    aliases = [(r, c) for r in run.results for c in r.comparisons
               if c.found_index is not None and c.factor != 1.0]
    out += ["## Missed planets", ""]
    if misses:
        for r, c in misses:
            out.append(f"- **{c.published.name}** (P = {c.published.period:.4f} d): {c.explanation}")
            if r.vt.expectation:
                out.append(f"  Expected: {r.vt.expectation}")
    else:
        out.append("None.")
    out.append("")
    if aliases:
        out += ["## Recovered at an alias", ""]
        out += [f"- **{c.published.name}**: {c.explanation}" for _, c in aliases]
        out.append("")

    out += ["## Other detections", "",
            "Candidates that match no published planet. *Spurious* ones passed vetting (weak or "
            "strong) and count as false positives; *duo-transits* cover only 2 transits but pass "
            "every vetting check, including a fitted T14 that agrees with the dip's width: two "
            "dips can't show periodicity, so they are listed apart, as neither planet candidates nor "
            "false positives, until more data show a third transit; *rejected* ones were flagged by "
            "the vetting itself; *duplicates* are a recovered planet found again by a later search pass.",
            ""]
    others = [(r, o) for r in run.results for o in r.others]
    if others:
        out += ["| Star | Candidate | P (d) | Depth (ppm) | SNR | SDE | Transits | Confidence | Kind | Explanation |",
                "|---|---|---|---|---|---|---|---|---|---|"]
        for r, o in others:
            p = r.planet(o.index)
            out.append(" | ".join([
                f"| {r.name}", plots.planet_letter(o.index), _num(found_period(p), 4),
                f"{p.candidate.depth * 1e6:.0f}", _num(p.candidate.snr, 1), _num(p.candidate.sde, 1),
                str(p.candidate.n_transits), p.vetting.confidence if p.vetting else "—", o.kind,
                _md_escape(o.explanation)]) + " |")
    else:
        out.append("None.")
    out.append("")

    out += ["## Null stars", "",
            "| Star | Why it is in the set | Published signals (archive check) | Data | Detections |",
            "|---|---|---|---|---|"]
    for r in run.results:
        if r.vt.role != "null":
            continue
        signals = ", ".join(f"{p.name} ({p.disposition})" for p in r.published.planets) or "none"
        if r.errors:
            signals = "; ".join(r.errors)
        found = ", ".join(f"{plots.planet_letter(o.index)}: {o.kind}" for o in r.others) or "none"
        out.append(f"| {r.name} | {_md_escape(r.vt.expectation)} | {signals} | {_data_line(r)} | {found} |")
    out.append("")

    out += ["## Targets", "", "| Star | Role | Data | Time (s) | Errors |", "|---|---|---|---|---|"]
    for r in run.results:
        errors = "; ".join((r.analysis.errors if r.analysis else []) + r.errors) or ""
        out.append(f"| {r.name} | {r.vt.role} | {_data_line(r)} | {r.elapsed_s:.0f} | {_md_escape(errors)} |")
    out.append("")

    if figures:
        out += ["## Folded light curves", ""]
        for r in run.results:
            for (star, i), path in figures.items():
                if star == r.name:
                    out.append(f"![{star} {plots.planet_letter(i)}]({path})")
        out.append("")

    refs = sorted({p.ref for r in run.results for p in r.published.planets if p.ref})
    out += ["## References for published values", ""]
    out += [f"- {_md_escape(ref)}" for ref in refs] or ["None."]
    out.append("")

    credits = []
    for r in run.results:
        for line in (r.analysis.credits if r.analysis else []):
            if line not in credits:
                credits.append(line)
    if _uses_archive(run):
        credits += [line for line in (ARCHIVE_CREDIT, *ARCHIVE_REFERENCES) if line not in credits]
    out += ["## Data credits", ""] + [f"- {line}" for line in credits] + [""]
    return "\n".join(out)


def results_json(run: ValidationRun) -> dict:
    return {
        "started": run.started.isoformat(), "finished": run.finished.isoformat() if run.finished else None,
        "source": run.source_name, "max_products": run.max_products, "cancelled": run.cancelled,
        "published_retrieved": run.retrieved, "tally": run.tally(),
        "targets": [{
            "name": r.name, "role": r.vt.role, "elapsed_s": round(r.elapsed_s, 1),
            "errors": (r.analysis.errors if r.analysis else []) + r.errors,
            "published_source": r.published.source, "published_retrieved": r.published.retrieved,
            "planets": [_planet_values(r, c) for c in r.comparisons],
            "others": [{"index": o.index, "kind": o.kind, "period": found_period(r.planet(o.index)),
                        "snr": r.planet(o.index).candidate.snr, "sde": r.planet(o.index).candidate.sde,
                        "explanation": o.explanation} for o in r.others],
        } for r in run.results],
    }


def write_report(run: ValidationRun, directory: Path | str | None = None) -> Path:
    """Write the report into ``directory`` (default ``reports/validation``); returns the .md path."""
    directory = Path(directory) if directory is not None else default_directory()
    directory.mkdir(parents=True, exist_ok=True)
    old = directory / FIGURE_DIR
    if old.is_dir():
        for png in old.glob("*.png"):
            png.unlink()
    figures = _folded_pngs(run, directory)
    plots.save_png(summary_figure(run), directory / SUMMARY_PNG)
    (directory / JSON_NAME).write_text(json.dumps(results_json(run), indent=1, default=str) + "\n",
                                       encoding="utf-8")
    path = directory / REPORT_NAME
    path.write_text(report_markdown(run, figures), encoding="utf-8")
    return path


def publish(directory: Path | str, docs_dir: Path | str = DOCS_DIR) -> Path:
    """Copy a written report to ``docs_dir`` (the committed copy), replacing the previous one."""
    directory, docs_dir = Path(directory), Path(docs_dir)
    (docs_dir / FIGURE_DIR).mkdir(parents=True, exist_ok=True)
    for png in (docs_dir / FIGURE_DIR).glob("*.png"):
        png.unlink()
    for name in (REPORT_NAME, JSON_NAME, SUMMARY_PNG):
        shutil.copy2(directory / name, docs_dir / name)
    for png in (directory / FIGURE_DIR).glob("*.png"):
        shutil.copy2(png, docs_dir / FIGURE_DIR / png.name)
    return docs_dir / REPORT_NAME
