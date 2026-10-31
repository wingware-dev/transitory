"""Regenerate ``transitory/data/targets_offline.json`` from the live services.

    python -m transitory.catalog.build_offline [--output PATH]

Resolves each validation target, records its TIC/KIC identifiers, position,
magnitude and radius, and its confirmed transiting planets from
``pscomppars``.  A null star is listed only if the archive has no confirmed
planet, KOI or TOI for it.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import archive
from .client import CatalogClient
from .resolve import resolve

# (name typed, role).  Step 8 (validation) may add or swap null stars.
VALIDATION_TARGETS: list[tuple[str, str]] = [
    ("Kepler-10", "planet-host"),
    ("Kepler-7", "planet-host"),
    ("Kepler-2", "planet-host"),  # = HAT-P-7
    ("Kepler-22", "planet-host"),
    ("pi Men", "planet-host"),
    ("LHS 3844", "planet-host"),
    ("WASP-18", "planet-host"),
    ("TOI-270", "planet-host"),
    ("K2-18", "planet-host"),
    ("K2-334", "planet-host"),
    ("KIC 8006161", "null"),
]
# Stars validated on another mission than the KIC rule below picks: the K2 hosts,
# which also have TESS data, are here to check K2 light curves.
MISSIONS = {"K2-18": "K2", "K2-334": "K2"}

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "data" / "targets_offline.json"


def _round(value, digits):
    return None if value is None else round(value, digits)


def build(client: CatalogClient | None = None) -> dict:
    client = client or CatalogClient()
    targets = []
    for name, role in VALIDATION_TARGETS:
        target = resolve(name, client=client)
        if role == "null":
            signals = archive.all_signals(target, client=client)
            if signals.planets:
                raise SystemExit(f"{name} is listed as a null star but the archive has "
                                 f"{[p.name for p in signals.planets]}")
            planets = []
        else:
            planets = archive.known_planets(target, client=client).planets
            if not planets:
                raise SystemExit(f"{name}: no confirmed transiting planets found in pscomppars")
        # Kepler stars have Kepler's long, precise baseline; the rest only TESS.
        mission = MISSIONS.get(name, target.mission_hint)
        if mission == "any":
            mission = "Kepler" if target.kic is not None else "TESS"
        if mission == "K2" and target.epic is None:
            raise SystemExit(f"{name}: no EPIC number in the archive's k2pandc table")
        targets.append({
            "name": name, "role": role, "tic": target.tic, "kic": target.kic, "epic": target.epic,
            "ra": _round(target.ra, 6), "dec": _round(target.dec, 6),
            "tmag": _round(target.tmag, 3), "radius_rsun": _round(target.radius_rsun, 4),
            "teff_k": _round(target.teff_k, 0), "logg": _round(target.logg, 3),
            "mass_msun": _round(target.mass_msun, 3),
            "mission_hint": mission,
            "planets": [{"name": p.name, "period": p.period, "duration_h": p.duration_h,
                         "depth": _round(p.depth, 10), "rp_rs": p.rp_rs, "ref": p.ref}
                        for p in planets],
        })
    return {
        "description": ("Validation targets bundled for offline use: identifiers and positions from "
                        "the TESS Input Catalog (MAST), published planets from the NASA Exoplanet "
                        "Archive pscomppars table. Regenerate with "
                        "python -m transitory.catalog.build_offline."),
        "retrieved": datetime.now(UTC).date().isoformat(),
        "credit": archive.ARCHIVE_CREDIT,
        "targets": targets,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    data = build()
    args.output.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(data['targets'])} targets to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
