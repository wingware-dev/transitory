"""Sky axes: RA in hours and Dec in degrees, sexagesimal, for the sky view.

The formatting functions are pure.  ``format_ra`` / ``format_dec`` write a
position to a chosen precision (``"h"``/``"d"``, ``"m"`` or ``"s"``, plus
decimals of seconds), rounding before splitting so 59.96 s never shows as
60.0 s.  ``RaAxis`` / ``DecAxis`` put ticks at whole hours, minutes or
seconds (of time for RA, of arc for Dec), as finely as the axis's length
allows, and label them only as precisely as the tick step needs.
"""

from __future__ import annotations

import math

import pyqtgraph as pg

# Tick steps in degrees, finest first.  RA steps are whole seconds, minutes
# or hours of time (1 h = 15 deg); Dec steps are whole arcseconds, arcminutes
# or degrees.
RA_STEPS = ([s / 240 for s in (1, 2, 5, 10, 15, 30)]
            + [m / 4 for m in (1, 2, 5, 10, 15, 30)]
            + [h * 15 for h in (1, 2, 3, 4, 6, 12)])
DEC_STEPS = ([s / 3600 for s in (1, 2, 5, 10, 15, 30)]
             + [m / 60 for m in (1, 2, 5, 10, 15, 30)]
             + [1, 2, 5, 10, 15, 30, 45])

RA_LABEL_PIXELS = 90  # room for a label like 19h22m30s
DEC_LABEL_PIXELS = 40


def format_ra(ra_deg: float, precision: str = "s", decimals: int = 0) -> str:
    """RA in hours: ``"19h"``, ``"19h22m"`` or ``"19h22m40s"`` (``decimals`` on the seconds)."""
    hours = (ra_deg % 360.0) / 15.0
    if precision == "h":
        return f"{round(hours) % 24:02d}h"
    if precision == "m":
        minutes = round(hours * 60) % (24 * 60)
        return f"{minutes // 60:02d}h{minutes % 60:02d}m"
    scale = 10 ** decimals
    ticks = round(hours * 3600 * scale) % (24 * 3600 * scale)
    h, rest = divmod(ticks, 3600 * scale)
    m, s = divmod(rest, 60 * scale)
    return f"{h:02d}h{m:02d}m{_seconds(s, decimals)}s"


def format_dec(dec_deg: float, precision: str = "s", decimals: int = 0) -> str:
    """Dec in degrees: ``"+44°"``, ``"+44°30′"`` or ``"+44°30′15″"`` (``decimals`` on the seconds)."""
    sign = "-" if dec_deg < 0 else "+"
    value = abs(dec_deg)
    if precision == "d":
        units = round(value)
        text = f"{units:02d}°"
    elif precision == "m":
        units = round(value * 60)
        text = f"{units // 60:02d}°{units % 60:02d}′"
    else:
        scale = 10 ** decimals
        units = round(value * 3600 * scale)
        d, rest = divmod(units, 3600 * scale)
        m, s = divmod(rest, 60 * scale)
        text = f"{d:02d}°{m:02d}′{_seconds(s, decimals)}″"
    return ("+" if units == 0 else sign) + text


def _seconds(units: int, decimals: int) -> str:
    """Seconds given in 10**-decimals units, zero-padded: ``"05"``, ``"05.3"``."""
    if not decimals:
        return f"{units:02d}"
    whole, frac = divmod(units, 10 ** decimals)
    return f"{whole:02d}.{frac:0{decimals}d}"


def ra_precision(step_deg: float) -> str:
    """The coarsest RA unit that labels ticks ``step_deg`` apart exactly."""
    if step_deg >= 15 - 1e-9:
        return "h"
    return "m" if step_deg >= 0.25 - 1e-9 else "s"


def dec_precision(step_deg: float) -> str:
    if step_deg >= 1 - 1e-9:
        return "d"
    return "m" if step_deg >= 1 / 60 - 1e-9 else "s"


def tick_steps(span_deg: float, length_px: float, label_px: float,
               steps: list[float]) -> tuple[float, float | None]:
    """(major, minor) tick steps for ``span_deg`` over ``length_px`` pixels.

    The major step is the finest in ``steps`` that leaves ``label_px``
    between labels; the minor step is the coarsest finer step dividing it
    into 3 to 6 parts, or None.
    """
    count = max(1.0, length_px / label_px)
    wanted = abs(span_deg) / count
    major = next((s for s in steps if s >= wanted * (1 - 1e-9)), steps[-1])
    minor = None
    for step in steps:
        if step >= major:
            break
        parts = major / step
        if 3 <= round(parts) <= 6 and math.isclose(parts, round(parts), rel_tol=1e-6):
            minor = step
    return major, minor


class _SexagesimalAxis(pg.AxisItem):
    steps: list[float] = []
    label_px = 50

    def __init__(self, orientation: str, **kwargs):
        super().__init__(orientation, **kwargs)
        self.enableAutoSIPrefix(False)

    def tickSpacing(self, minVal, maxVal, size):
        major, minor = tick_steps(maxVal - minVal, size, self.label_px, self.steps)
        return [(major, 0)] + ([(minor, 0)] if minor else [])

    def tickStrings(self, values, scale, spacing):
        return [self.format(v, spacing) for v in values]

    def format(self, value: float, spacing: float) -> str:
        raise NotImplementedError


class RaAxis(_SexagesimalAxis):
    """An RA axis labelled in hours and minutes, with seconds when zoomed in."""

    steps = RA_STEPS
    label_px = RA_LABEL_PIXELS

    def format(self, value: float, spacing: float) -> str:
        return format_ra(value, ra_precision(spacing))


class DecAxis(_SexagesimalAxis):
    """A Dec axis labelled in degrees and arcminutes, with arcseconds when zoomed in."""

    steps = DEC_STEPS
    label_px = DEC_LABEL_PIXELS

    def format(self, value: float, spacing: float) -> str:
        if abs(value) > 90 + 1e-9:
            return ""  # the view may run a little past the poles
        return format_dec(value, dec_precision(spacing))
