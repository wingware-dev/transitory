"""Stars for the sky view.

- :func:`host_stars`: every host of a confirmed transiting planet
  (Exoplanet Archive ``pscomppars``), the whole-sky layer;
- :func:`field_stars`: TIC stars in a small field, for a zoomed-in view;
- :func:`offline_targets`: the validation targets bundled with the package
  (``transitory/data/targets_offline.json``), shown when the archive cannot
  be reached and usable with no network at all.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from importlib import resources
from typing import Literal

from ..models import KnownPlanet, MissionHint, StarTarget
from . import tic as ticdb
from .archive import tic_number
from .client import CatalogClient, CatalogError, default_client

log = logging.getLogger(__name__)

StarKind = Literal["host", "field", "null"]

OFFLINE_FILE = "targets_offline.json"

HOST_QUERY = ("select hostname, pl_name, tic_id, ra, dec, sy_tmag, sy_kepmag, st_rad, disc_facility "
              "from pscomppars where tran_flag = 1")


@dataclass
class SkyStar:
    name: str
    ra: float
    dec: float
    tmag: float | None = None
    tic: int | None = None
    kic: int | None = None
    radius_rsun: float | None = None
    kind: StarKind = "host"
    planets: list[str] = field(default_factory=list)  # planet names, for hosts
    mission_hint: MissionHint = "any"

    @property
    def n_planets(self) -> int:
        return len(self.planets)

    def to_target(self) -> StarTarget:
        return StarTarget(name=self.name, tic=self.tic, kic=self.kic, ra=self.ra, dec=self.dec,
                          tmag=self.tmag, mission_hint=self.mission_hint,
                          radius_rsun=self.radius_rsun)


@dataclass
class SkyCatalog:
    stars: list[SkyStar]
    retrieved: str = ""  # YYYY-MM-DD the rows were fetched, or the offline file's date
    offline: bool = False  # True: the bundled targets, because the archive was unreachable
    note: str = ""


def _hint_for_facility(facility: str | None) -> MissionHint:
    facility = (facility or "").lower()
    if "kepler" in facility or facility.startswith("k2"):
        return "Kepler"
    if "tess" in facility:
        return "TESS"
    return "any"


def hosts_from_rows(rows: list[dict]) -> list[SkyStar]:
    """Group ``pscomppars`` planet rows into one SkyStar per host."""
    by_host: dict[str, SkyStar] = {}
    for row in rows:
        if row.get("ra") is None or row.get("dec") is None:
            continue
        host = row.get("hostname") or row["pl_name"]
        star = by_host.get(host)
        if star is None:
            star = by_host[host] = SkyStar(
                name=host, ra=float(row["ra"]), dec=float(row["dec"]),
                tmag=row.get("sy_tmag") if row.get("sy_tmag") is not None else row.get("sy_kepmag"),
                tic=tic_number(row.get("tic_id")), radius_rsun=row.get("st_rad"),
                mission_hint=_hint_for_facility(row.get("disc_facility")))
        if row["pl_name"] not in star.planets:
            star.planets.append(row["pl_name"])
    stars = list(by_host.values())
    for star in stars:
        star.planets.sort()
    stars.sort(key=lambda s: s.name)
    return stars


def host_stars(client: CatalogClient | None = None, refresh: bool = False,
               offline_fallback: bool = True) -> SkyCatalog:
    """Hosts of confirmed transiting planets; the bundled targets if the archive is unreachable."""
    try:
        response = (client or default_client()).tap(HOST_QUERY, refresh=refresh)
    except CatalogError as exc:
        if not offline_fallback:
            raise
        log.warning("Exoplanet Archive unreachable, showing bundled targets only: %s", exc)
        catalog = offline_catalog()
        catalog.note = f"Exoplanet Archive unreachable ({exc}); showing bundled targets only"
        return catalog
    return SkyCatalog(stars=hosts_from_rows(response.data), retrieved=response.retrieved_date)


def field_stars(ra: float, dec: float, radius_deg: float, max_tmag: float | None = 13.0,
                limit: int = ticdb.MAX_CONE_ROWS, client: CatalogClient | None = None) -> list[SkyStar]:
    """TIC stars around (ra, dec), brightest first; for a zoomed-in field.

    Raises CatalogError when MAST cannot be reached; the sky view runs this
    in a worker thread and reports the error.
    """
    stars = ticdb.tic_cone(ra, dec, radius_deg, max_tmag=max_tmag, limit=limit, client=client)
    result = [SkyStar(name=f"TIC {s.tic}", ra=s.ra, dec=s.dec, tmag=s.tmag, tic=s.tic, kic=s.kic,
                      radius_rsun=s.radius_rsun, kind="field")
              for s in stars if s.obj_type == "STAR"]
    result.sort(key=lambda s: s.tmag if s.tmag is not None else 99.0)
    return result


# -- bundled targets ---------------------------------------------------------------

def load_offline_data() -> dict:
    """The parsed ``targets_offline.json``."""
    text = resources.files("transitory").joinpath("data", OFFLINE_FILE).read_text(encoding="utf-8")
    return json.loads(text)


def offline_targets() -> list[SkyStar]:
    """The bundled validation targets (planet hosts and null stars)."""
    stars = []
    for entry in load_offline_data()["targets"]:
        stars.append(SkyStar(
            name=entry["name"], ra=entry["ra"], dec=entry["dec"], tmag=entry.get("tmag"),
            tic=entry.get("tic"), kic=entry.get("kic"), radius_rsun=entry.get("radius_rsun"),
            kind="null" if entry.get("role") == "null" else "host",
            planets=[p["name"] for p in entry.get("planets", [])],
            mission_hint=entry.get("mission_hint", "any")))
    return stars


def offline_catalog() -> SkyCatalog:
    data = load_offline_data()
    return SkyCatalog(stars=offline_targets(), retrieved=data.get("retrieved", ""), offline=True)


def offline_known_planets(name: str) -> list[KnownPlanet]:
    """Published planets the bundled file records for target ``name`` (case-insensitive)."""
    data = load_offline_data()
    for entry in data["targets"]:
        if entry["name"].lower() == name.strip().lower():
            ref = f"NASA Exoplanet Archive pscomppars, retrieved {data.get('retrieved', '')}"
            return [KnownPlanet(name=p["name"], period=p["period"], rp_rs=p.get("rp_rs"),
                                duration_h=p.get("duration_h"), depth=p.get("depth"), ref=ref)
                    for p in entry.get("planets", [])]
    return []
