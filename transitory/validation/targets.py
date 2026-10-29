"""The known-planet validation set.

Confirmed transiting planets over a range of sizes and periods, and three
bright, quiet "null" stars with good coverage and no published signal of
any kind (confirmed planet, KOI or TOI; checked live with
``catalog.archive.all_signals`` on every run, and recorded in the report).

Identifiers and positions of the planet hosts and KIC 8006161 come from
the bundled ``targets_offline.json``.  The other two null stars were chosen
for this set on 2026-10-29: bright dwarf-sized (0.8–1.6 R_sun) stars with
long Kepler / TESS coverage at MAST and no signal in pscomppars, the Kepler
KOI table or the TOI table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..catalog.skycatalog import offline_targets
from ..models import StarTarget

Role = Literal["planet-host", "null"]


@dataclass
class ValidationTarget:
    target: StarTarget
    role: Role
    planets: list[str] = field(default_factory=list)  # published planets expected, by name
    expectation: str = ""  # what we expect, or why the star is in the set


# What to expect for the hard cases, said up front so a miss is not a surprise.
EXPECTATIONS = {
    "Kepler-10": "Kepler-10 b is shallow (~150 ppm) but transits every 0.84 d; c (45.3 d) is ~400 ppm.",
    "Kepler-22": ("Expected hard case: a 290 d period gives only 3 transits in the 4-year Kepler "
                  "baseline, at the edge of what the search accepts."),
    "TOI-270": ("Three small planets around an M dwarf, c and d near a 2:1 period ratio; the search "
                "stops after 3 passes, so every pass must find a real planet."),
    "LHS 3844": "Ultra-short period (11 h) around an M dwarf, with short (~31 min) transits.",
    "pi Men": "Shallow (~300 ppm) 2 R_earth planet on a very bright star.",
}

# Null stars beyond the bundled KIC 8006161 (identifiers and stellar parameters from the TESS
# Input Catalog at MAST, rounded as build_offline rounds them).  Teff, log g and mass give the
# fit its limb darkening and a/Rs density prior, as for the bundled targets.
EXTRA_NULL_STARS = [
    StarTarget(name="KIC 6106415", tic=120495825, kic=6106415, ra=285.415326, dec=41.490090,
               tmag=6.674, mission_hint="Kepler", radius_rsun=1.271,
               teff_k=6007.0, logg=4.275, mass_msun=1.11),
    StarTarget(name="TIC 31410749", tic=31410749, ra=89.004307, dec=-67.066291,
               tmag=8.304, mission_hint="TESS", radius_rsun=1.218,
               teff_k=5927.0, logg=4.3, mass_msun=1.08),
]
NULL_NOTES = {
    "KIC 8006161": "Bright (Tmag 6.8), quiet dwarf (HD 173701), Kepler asteroseismology target, 18 quarters.",
    "KIC 6106415": "Bright (Tmag 6.7) dwarf, Kepler asteroseismology target, 14 long-cadence quarters.",
    "TIC 31410749": ("Tmag 8.3 dwarf in the southern TESS continuous viewing zone, 13 sectors "
                     "(SPOC 2-min and TESS-SPOC full-frame-image light curves)."),
}


def validation_targets() -> list[ValidationTarget]:
    """The full set: the bundled planet hosts and null star, then the extra null stars."""
    result = []
    for star in offline_targets():
        role: Role = "null" if star.kind == "null" else "planet-host"
        notes = EXPECTATIONS if role == "planet-host" else NULL_NOTES
        result.append(ValidationTarget(target=star.to_target(), role=role, planets=list(star.planets),
                                       expectation=notes.get(star.name, "")))
    for target in EXTRA_NULL_STARS:
        result.append(ValidationTarget(target=target, role="null",
                                       expectation=NULL_NOTES.get(target.name, "")))
    return result


def select(targets: list[ValidationTarget], names: list[str]) -> list[ValidationTarget]:
    """The targets whose star (or one of whose planets) is named, case-insensitively."""
    wanted = {n.strip().lower() for n in names if n.strip()}
    picked = [t for t in targets
              if t.target.name.lower() in wanted or any(p.lower() in wanted for p in t.planets)]
    found = {t.target.name.lower() for t in picked} | {p.lower() for t in picked for p in t.planets}
    unknown = sorted(wanted - found)
    if unknown:
        raise KeyError(f"not in the validation set: {', '.join(unknown)}")
    return picked
