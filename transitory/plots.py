"""Matplotlib figures for the results gallery and saved reports.

Each function takes a :class:`StarAnalysis` and returns a
``matplotlib.figure.Figure`` built without pyplot, so figures can be made
in worker threads, embedded in Qt with ``FigureCanvasQTAgg(fig)`` or saved
with :func:`save_png`.  Every figure carries the analysis's credit lines
as a footer.

Figures handle missing pieces (no light curve, no fit, no planets) by
showing a short message instead of raising.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from matplotlib.figure import Figure
from matplotlib.transforms import blended_transform_factory

from .models import PlanetResult, StarAnalysis

FIG_SIZE = (10.0, 6.0)  # inches
PNG_DPI = 150
FOOTER_FONT_SIZE = 6.5
PLANET_LETTERS = "bcdefghijklmnopqrstuvwxyz"

CONFIDENCE_COLORS = {"strong": "#1a9641", "weak": "#e6860b", "none": "#808080"}
PLANET_COLORS = ["#d7191c", "#2b83ba", "#7b3294", "#008837", "#e66101"]
DATA_COLOR = "#9e9e9e"
TREND_COLOR = "#e6550d"
BIN_COLOR = "#08519c"
MODEL_COLOR = "#d7191c"


@dataclass
class GalleryFigure:
    """One figure of a star's gallery section."""

    key: str  # file-name friendly: "lightcurve", "periodogram", "planet-b"
    title: str
    figure: Figure


def planet_letter(index: int) -> str:
    return PLANET_LETTERS[index] if index < len(PLANET_LETTERS) else str(index + 1)


def planet_color(index: int) -> str:
    return PLANET_COLORS[index % len(PLANET_COLORS)]


# --- figure scaffolding ---------------------------------------------------


def _footer_lines(credits: list[str], width_in: float) -> list[str]:
    chars = max(40, int(width_in * 72 / (FOOTER_FONT_SIZE * 0.52)))
    lines: list[str] = []
    for credit in credits:
        lines.extend(textwrap.wrap(credit, chars) or [""])
    return lines


def _new_figure(analysis: StarAnalysis, title: str, size=FIG_SIZE) -> tuple[Figure, float, float]:
    """A figure with suptitle and credit footer; returns (fig, bottom, top) for the axes area."""
    fig = Figure(figsize=size)
    width_in, height_in = size
    height_pt = height_in * 72

    lines = _footer_lines(analysis.credits, width_in)
    line_pt = FOOTER_FONT_SIZE * 1.35
    footer_frac = (len(lines) * line_pt + 6) / height_pt if lines else 0.0
    if lines:
        fig.text(0.01, 4 / height_pt, "\n".join(lines), ha="left", va="bottom",
                 fontsize=FOOTER_FONT_SIZE, color="#555555", linespacing=1.2)
        fig.add_artist(_footer_rule(fig, footer_frac + 2 / height_pt))

    fig.suptitle(title, fontsize=11, x=0.01, ha="left", y=1 - 6 / height_pt, va="top")
    bottom = footer_frac + 48 / height_pt  # room for x tick labels and label
    top = 1 - 36 / height_pt
    return fig, bottom, top


def _footer_rule(fig: Figure, y: float):
    from matplotlib.lines import Line2D

    return Line2D([0.01, 0.99], [y, y], transform=fig.transFigure, color="#cccccc", lw=0.6)


def _message(ax, text: str) -> None:
    ax.text(0.5, 0.5, text, ha="center", va="center", transform=ax.transAxes,
            fontsize=10, color="#555555", wrap=True)
    ax.set_xticks([])
    ax.set_yticks([])


def _star_title(analysis: StarAnalysis, what: str) -> str:
    mission = analysis.raw.mission if analysis.raw is not None else ""
    parts = [analysis.target.label, what]
    if mission:
        parts.append(mission)
    return " — ".join(parts)


def _ppt_axis(ax) -> None:
    """Show flux as parts-per-thousand deviation for readability of shallow dips."""
    ax.yaxis.set_major_formatter(lambda v, _pos: f"{(v - 1) * 1e3:+.1f}")
    ax.set_ylabel("Relative flux − 1 (ppt)")


def _robust_ylim(ax, flux: np.ndarray, low_q=0.05, high_q=99.5, pad=0.15) -> None:
    flux = flux[np.isfinite(flux)]
    if flux.size < 2:
        return
    lo, hi = np.percentile(flux, [low_q, high_q])
    if hi <= lo:
        return
    span = hi - lo
    ax.set_ylim(lo - pad * span, hi + pad * span)


# --- light curve ----------------------------------------------------------


def _transit_times(planet: PlanetResult, tmin: float, tmax: float) -> np.ndarray:
    period, t0 = _ephemeris(planet)
    if not (np.isfinite(period) and period > 0):
        return np.empty(0)
    n0 = np.ceil((tmin - t0) / period)
    n1 = np.floor((tmax - t0) / period)
    return t0 + np.arange(n0, n1 + 1) * period


def light_curve_figure(analysis: StarAnalysis) -> Figure:
    """Raw flux with the removed trend (top) and cleaned, flattened flux with transit marks (bottom)."""
    fig, bottom, top = _new_figure(analysis, _star_title(analysis, "light curve"))
    if analysis.raw is None or len(analysis.raw) == 0:
        ax = fig.add_subplot(fig.add_gridspec(1, 1, bottom=bottom, top=top, left=0.08, right=0.98)[0])
        _message(ax, _missing_text(analysis, "No light curve"))
        return fig

    gs = fig.add_gridspec(2, 1, bottom=bottom, top=top, left=0.08, right=0.98, hspace=0.08)
    ax_raw = fig.add_subplot(gs[0])
    ax_clean = fig.add_subplot(gs[1], sharex=ax_raw)

    raw = analysis.raw
    ax_raw.plot(raw.time, raw.flux, ".", ms=1.2, color=DATA_COLOR, rasterized=True, label="raw")
    cleaned = analysis.cleaned
    if cleaned is not None and len(cleaned.trend) == len(raw.time):
        ax_raw.plot(raw.time, cleaned.trend, "-", lw=1.0, color=TREND_COLOR, label="trend")
    ax_raw.legend(loc="upper right", fontsize=7, markerscale=6, frameon=False)
    _robust_ylim(ax_raw, raw.flux)
    ax_raw.set_ylabel("Raw flux")
    ax_raw.tick_params(labelbottom=False)

    if cleaned is None or len(cleaned.lc) == 0:
        _message(ax_clean, _missing_text(analysis, "No cleaned light curve"))
    else:
        lc = cleaned.lc
        ax_clean.plot(lc.time, lc.flux, ".", ms=1.2, color="#444444", rasterized=True)
        _robust_ylim(ax_clean, lc.flux)
        if analysis.planets:  # room below the deepest dips for the transit marks
            lo, hi = ax_clean.get_ylim()
            ax_clean.set_ylim(lo - (0.04 * len(analysis.planets) + 0.04) * (hi - lo), hi)
        _ppt_axis(ax_clean)
        marks = blended_transform_factory(ax_clean.transData, ax_clean.transAxes)
        tmin, tmax = float(np.nanmin(lc.time)), float(np.nanmax(lc.time))
        for i, planet in enumerate(analysis.planets):
            times = _transit_times(planet, tmin, tmax)
            if times.size:
                ax_clean.plot(times, np.full(times.shape, 0.03 + 0.04 * i), "^", ms=4,
                              color=planet_color(i), transform=marks, clip_on=False,
                              label=f"{planet_letter(i)}: P = {_ephemeris(planet)[0]:.4g} d")
        if analysis.planets:
            ax_clean.legend(loc="upper right", fontsize=7, framealpha=0.85, edgecolor="none")
    ax_clean.set_xlabel(_time_label(analysis))
    return fig


def _time_label(analysis: StarAnalysis) -> str:
    mission = analysis.raw.mission if analysis.raw is not None else ""
    system = {"TESS": "BTJD", "Kepler": "BKJD"}.get(mission, "days")
    return f"Time ({system})"


def _missing_text(analysis: StarAnalysis, lead: str) -> str:
    if analysis.errors:
        return lead + "\n" + "\n".join(textwrap.shorten(e, 100) for e in analysis.errors[:4])
    return lead


# --- periodogram ----------------------------------------------------------


def periodogram_figure(analysis: StarAnalysis) -> Figure:
    """BLS power against period for each search pass, with each pass's peak marked."""
    fig, bottom, top = _new_figure(analysis, _star_title(analysis, "BLS periodogram"))
    passes = [p for p in analysis.periodograms if len(p.periods)]
    if not passes:
        ax = fig.add_subplot(fig.add_gridspec(1, 1, bottom=bottom, top=top, left=0.08, right=0.98)[0])
        _message(ax, _missing_text(analysis, "No periodogram"))
        return fig

    gs = fig.add_gridspec(len(passes), 1, bottom=bottom, top=top, left=0.08, right=0.98, hspace=0.12)
    axes = []
    found = 0  # planets are the passes' best candidates, in pass order
    for row, pg in enumerate(passes):
        ax = fig.add_subplot(gs[row], sharex=axes[0] if axes else None)
        axes.append(ax)
        ax.plot(pg.periods, pg.power, "-", lw=0.7, color="#333333")
        ax.set_xscale("log")
        ax.set_ylabel(f"Pass {row + 1}\npower", fontsize=8)
        if pg.best is not None:
            color = planet_color(found)
            period = pg.best.period
            ax.axvline(period, color=color, lw=1.0, alpha=0.8)
            for alias in (0.5, 2.0):
                ax.axvline(period * alias, color=color, lw=0.8, ls=":", alpha=0.6)
            peak = _power_at(pg.periods, pg.power, period)
            ax.annotate(f"{planet_letter(found)}: {period:.5g} d  SDE {pg.best.sde:.1f}",
                        xy=(period, peak), xytext=(6, -2), textcoords="offset points",
                        fontsize=8, color=color, va="top")
            found += 1
        else:
            ax.text(0.99, 0.9, "no significant peak", transform=ax.transAxes,
                    ha="right", va="top", fontsize=8, color="#777777")
        if row < len(passes) - 1:
            ax.tick_params(labelbottom=False)
    axes[-1].set_xlabel("Period (days)")
    return fig


def _power_at(periods: np.ndarray, power: np.ndarray, period: float) -> float:
    i = int(np.argmin(np.abs(periods - period)))
    return float(power[i])


# --- phase-folded transit -------------------------------------------------


def _ephemeris(planet: PlanetResult) -> tuple[float, float]:
    if planet.fit is not None and np.isfinite(planet.fit.period):
        return planet.fit.period, planet.fit.t0
    return planet.candidate.period, planet.candidate.t0


def _duration_days(planet: PlanetResult) -> float:
    if planet.fit is not None and np.isfinite(planet.fit.duration_hours) and planet.fit.duration_hours > 0:
        return planet.fit.duration_hours / 24.0
    return planet.candidate.duration


def fold(time: np.ndarray, period: float, t0: float) -> np.ndarray:
    """Time from the nearest mid-transit, in days, in [-P/2, P/2)."""
    return (time - t0 + 0.5 * period) % period - 0.5 * period


def bin_folded(phase: np.ndarray, flux: np.ndarray, width: float, lo: float, hi: float):
    """Mean flux in bins of ``width`` over [lo, hi]; returns (centres, means, standard errors)."""
    edges = np.arange(lo, hi + width, width)
    idx = np.digitize(phase, edges) - 1
    centres, means, errs = [], [], []
    for b in range(len(edges) - 1):
        sel = flux[idx == b]
        sel = sel[np.isfinite(sel)]
        if sel.size == 0:
            continue
        centres.append(0.5 * (edges[b] + edges[b + 1]))
        means.append(sel.mean())
        errs.append(sel.std(ddof=1) / np.sqrt(sel.size) if sel.size > 1 else np.nan)
    return np.array(centres), np.array(means), np.array(errs)


def _other_transits(analysis: StarAnalysis, index: int, time: np.ndarray) -> np.ndarray:
    """Points inside the transits of planets other than ``index`` (padded by half a T14)."""
    inside = np.zeros(time.shape, dtype=bool)
    for i, other in enumerate(analysis.planets):
        if i == index:
            continue
        period, t0 = _ephemeris(other)
        if np.isfinite(period) and period > 0:
            inside |= np.abs(fold(time, period, t0)) <= _duration_days(other)
    return inside


def folded_figure(analysis: StarAnalysis, index: int) -> Figure:
    """Cleaned flux folded on planet ``index``'s ephemeris, binned, with the fitted model."""
    planet = analysis.planets[index]
    letter = planet_letter(index)
    fig, bottom, top = _new_figure(analysis, _star_title(analysis, f"planet candidate {letter}"))
    ax = fig.add_subplot(fig.add_gridspec(1, 1, bottom=bottom, top=top, left=0.08, right=0.72)[0])

    period, t0 = _ephemeris(planet)
    duration = _duration_days(planet)
    cleaned = analysis.cleaned
    if cleaned is None or len(cleaned.lc) == 0:
        _message(ax, _missing_text(analysis, "No cleaned light curve"))
    else:
        half = min(0.5 * period, max(3.0 * duration, 2.0 / 24.0))
        phase = fold(cleaned.lc.time, period, t0)
        near = (np.abs(phase) <= half) & ~_other_transits(analysis, index, cleaned.lc.time)
        hours = phase[near] * 24.0
        flux = cleaned.lc.flux[near]
        ax.plot(hours, flux, ".", ms=1.5, color=DATA_COLOR, rasterized=True, label="data")

        width = max(duration / 12.0, (cleaned.lc.cadence_s or 0) / 86400.0, 1e-4)
        bc, bm, be = bin_folded(phase[near], flux, width, -half, half)
        if bc.size:
            ax.errorbar(bc * 24.0, bm, yerr=be, fmt="o", ms=3, lw=0.8, color=BIN_COLOR,
                        label=f"binned ({width * 24 * 60:.0f} min)")

        if planet.fit is not None and len(planet.fit.model_time):
            mt = np.asarray(planet.fit.model_time)
            order = np.argsort(mt)
            mt, mf = mt[order], np.asarray(planet.fit.model_flux)[order]
            show = np.abs(mt) <= half
            ax.plot(mt[show] * 24.0, mf[show], "-", lw=1.6, color=MODEL_COLOR, label="fitted model")

        ax.set_xlim(-half * 24.0, half * 24.0)
        depth = planet.fit.depth if planet.fit is not None else planet.candidate.depth
        _folded_ylim(ax, flux, bm if bc.size else np.empty(0), depth)
        _ppt_axis(ax)
        ax.legend(loc="lower right", fontsize=7, frameon=False, markerscale=2)
    ax.set_xlabel("Hours from mid-transit")
    ax.axvline(0, color="#bbbbbb", lw=0.6, zorder=0)

    fig.text(0.74, top, _parameter_text(planet), ha="left", va="top", fontsize=8.5,
             family="monospace", linespacing=1.35)
    return fig


def _folded_ylim(ax, flux: np.ndarray, binned: np.ndarray, depth: float) -> None:
    flux = flux[np.isfinite(flux)]
    if flux.size < 2:
        return
    lo, hi = np.percentile(flux, [0.5, 99.5])
    if binned.size:
        lo = min(lo, np.nanmin(binned))
    if np.isfinite(depth):
        lo = min(lo, 1.0 - 1.3 * depth)
    span = max(hi - lo, 1e-6)
    ax.set_ylim(lo - 0.1 * span, hi + 0.1 * span)


def _fmt(value: float, err: float | None, digits: int) -> str:
    if value is None or not np.isfinite(value):
        return "—"
    text = f"{value:.{digits}f}"
    if err is not None and np.isfinite(err):
        text += f" ± {err:.{digits}f}"
    return text


def _parameter_text(planet: PlanetResult) -> str:
    cand, fit, vet = planet.candidate, planet.fit, planet.vetting
    rows: list[str] = []
    if fit is not None:
        u = fit.uncertainties
        rows += [
            f"P     {_fmt(fit.period, u.get('period'), 5)} d",
            f"t0    {_fmt(fit.t0, u.get('t0'), 4)}",
            f"Rp/Rs {_fmt(fit.rp_rs, u.get('rp_rs'), 4)}",
            f"a/Rs  {_fmt(fit.a_rs, u.get('a_rs'), 2)}",
            f"b     {_fmt(fit.b, u.get('b'), 2)}",
            f"T14   {_fmt(fit.duration_hours, u.get('duration_hours'), 2)} h",
            f"depth {fit.depth * 1e6:.0f} ppm",
        ]
        if np.isfinite(fit.chi2):
            rows.append(f"χ²ᵣ   {fit.chi2:.2f}")
    else:
        rows += [
            f"P     {cand.period:.5f} d",
            f"t0    {cand.t0:.4f}",
            f"T14   {cand.duration * 24:.2f} h",
            f"depth {cand.depth * 1e6:.0f} ppm",
            "(no fit)",
        ]
    rows += ["", f"SNR   {cand.snr:.1f}", f"SDE   {cand.sde:.1f}", f"N tr  {cand.n_transits}"]
    if vet is not None:
        rows += [
            "",
            f"Confidence: {vet.confidence}",
            f"odd/even {vet.odd_even_sigma:.1f}σ",
            f"secondary {vet.secondary_depth * 1e6:.0f} ppm",
        ]
        rows += textwrap.wrap("; ".join(vet.reasons), 30)
    return "\n".join(rows)


def confidence_color(planet: PlanetResult) -> str:
    conf = planet.vetting.confidence if planet.vetting is not None else "none"
    return CONFIDENCE_COLORS.get(conf, CONFIDENCE_COLORS["none"])


# --- gallery and saving ---------------------------------------------------


def gallery_figures(analysis: StarAnalysis) -> list[GalleryFigure]:
    """All figures for one star, in gallery order: light curve, periodogram, one per planet."""
    figures = [
        GalleryFigure("lightcurve", "Light curve", light_curve_figure(analysis)),
        GalleryFigure("periodogram", "Periodogram", periodogram_figure(analysis)),
    ]
    for i in range(len(analysis.planets)):
        letter = planet_letter(i)
        figures.append(GalleryFigure(f"planet-{letter}", f"Planet candidate {letter}",
                                     folded_figure(analysis, i)))
    return figures


def save_png(fig: Figure, path: Path | str, dpi: int = PNG_DPI) -> Path:
    """Save ``fig`` as a PNG (credit footer included) and return the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, format="png", dpi=dpi, facecolor="white")
    return path


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "star"


def save_all(analysis: StarAnalysis, directory: Path | str, dpi: int = PNG_DPI) -> list[Path]:
    """Save every gallery figure for ``analysis`` as ``<star>-<key>.png`` in ``directory``."""
    base = slug(analysis.target.name)
    return [save_png(g.figure, Path(directory) / f"{base}-{g.key}.png", dpi)
            for g in gallery_figures(analysis)]
