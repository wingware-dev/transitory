"""Published planets, KOIs and TOIs from the NASA Exoplanet Archive (TAP).

Three tables are used:

- ``pscomppars``: confirmed planets, one row per planet with a composite
  parameter set (``pl_trandep`` is in percent, ``pl_trandur`` in hours);
- ``cumulative``: the Kepler Objects of Interest (``koi_depth`` in ppm,
  ``koi_duration`` in hours);
- ``toi``: TESS Objects of Interest (``pl_trandep`` in ppm,
  ``pl_trandurh`` in hours).

Every result carries the date its rows were retrieved, because archive
values are revised over time and reports must say which ones they used.
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass, field

from ..models import KnownPlanet, StarTarget
from .client import CatalogClient, Response, adql_string, default_client

ARCHIVE_CREDIT = ("This research has made use of the NASA Exoplanet Archive, which is operated by "
                  "the California Institute of Technology, under contract with the National "
                  "Aeronautics and Space Administration under the Exoplanet Exploration Program.")

# R_earth / R_sun, to turn published radii into Rp/Rs when no ratio is given.
EARTH_PER_SUN_RADIUS = 6371.0 / 695700.0

PSCOMPPARS_COLUMNS = ("pl_name, hostname, tic_id, ra, dec, sy_tmag, sy_kepmag, st_rad, "
                      "pl_orbper, pl_orbpererr1, pl_tranmid, pl_trandur, pl_trandep, pl_ratror, "
                      "pl_rade, tran_flag, disc_facility, pl_orbper_reflink")
KOI_COLUMNS = ("kepid, kepoi_name, kepler_name, koi_disposition, koi_period, koi_period_err1, "
               "koi_time0bk, koi_duration, koi_depth, koi_ror, koi_prad, ra, dec, koi_kepmag")
TOI_COLUMNS = ("tid, toi, tfopwg_disp, ra, dec, st_tmag, st_rad, pl_orbper, pl_orbpererr1, "
               "pl_tranmid, pl_trandurh, pl_trandep, pl_rade")

# Kepler times (BKJD) are BJD - 2454833.
BKJD_OFFSET = 2454833.0


@dataclass
class ArchivePlanet:
    """One published planet or candidate, in the project's units."""

    name: str  # "Kepler-10 b", "K00072.01", "TOI-270.01"
    host: str
    table: str  # "pscomppars", "cumulative" or "toi"
    period: float | None  # days
    period_err: float | None = None
    t0_bjd: float | None = None  # mid-transit, BJD_TDB
    duration_h: float | None = None  # T14
    depth: float | None = None  # fractional
    rp_rs: float | None = None
    rp_earth: float | None = None
    disposition: str = "CONFIRMED"  # archive disposition (KOI / TFOPWG) or CONFIRMED
    tic: int | None = None
    kic: int | None = None
    ra: float | None = None
    dec: float | None = None
    ref: str = ""  # literature reference for the parameters, when the table gives one
    retrieved: str = ""  # YYYY-MM-DD

    @property
    def is_false_positive(self) -> bool:
        return self.disposition.upper() in ("FALSE POSITIVE", "FP", "FA")

    def to_known(self) -> KnownPlanet:
        source = f"NASA Exoplanet Archive {self.table}, retrieved {self.retrieved}"
        return KnownPlanet(name=self.name, period=self.period if self.period is not None else math.nan,
                           rp_rs=self.rp_rs, duration_h=self.duration_h, depth=self.depth,
                           ref=f"{self.ref}; {source}" if self.ref else source)


@dataclass
class ArchiveResult:
    planets: list[ArchivePlanet] = field(default_factory=list)
    retrieved: str = ""  # YYYY-MM-DD, oldest retrieval among the queries used
    queries: list[str] = field(default_factory=list)

    def known(self, include_false_positives: bool = False) -> list[KnownPlanet]:
        return [p.to_known() for p in self.planets
                if include_false_positives or not p.is_false_positive]

    def extend(self, other: ArchiveResult) -> None:
        self.planets.extend(other.planets)
        self.queries.extend(other.queries)
        dates = [d for d in (self.retrieved, other.retrieved) if d]
        self.retrieved = min(dates) if dates else ""


# -- value helpers ---------------------------------------------------------

def _num(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) else result


def _scaled(value, factor: float) -> float | None:
    number = _num(value)
    return None if number is None else number * factor


def tic_number(value) -> int | None:
    """``"TIC 377780790"`` (or a bare number) as 377780790."""
    if value is None:
        return None
    match = re.search(r"(\d+)", str(value))
    return int(match.group(1)) if match else None


def reference_text(reflink: str | None) -> str:
    """Plain text of an archive ``*_reflink`` HTML anchor, with its URL."""
    if not reflink:
        return ""
    text = html.unescape(re.sub(r"<[^>]+>", "", reflink)).strip()
    href = re.search(r"href=([^\s>]+)", reflink)
    return f"{text} ({href.group(1)})" if href and text else text


def _rp_rs_from_radii(rp_earth: float | None, rs_sun: float | None) -> float | None:
    if rp_earth is None or not rs_sun:
        return None
    return rp_earth * EARTH_PER_SUN_RADIUS / rs_sun


# -- row conversion ----------------------------------------------------------

def _from_pscomppars(row: dict, retrieved: str) -> ArchivePlanet:
    rp_earth = _num(row.get("pl_rade"))
    rp_rs = _num(row.get("pl_ratror"))
    return ArchivePlanet(
        name=row["pl_name"], host=row.get("hostname") or "", table="pscomppars",
        period=_num(row.get("pl_orbper")), period_err=_num(row.get("pl_orbpererr1")),
        t0_bjd=_num(row.get("pl_tranmid")), duration_h=_num(row.get("pl_trandur")),
        depth=_scaled(row.get("pl_trandep"), 1e-2),
        rp_rs=rp_rs if rp_rs is not None else _rp_rs_from_radii(rp_earth, _num(row.get("st_rad"))),
        rp_earth=rp_earth, tic=tic_number(row.get("tic_id")),
        ra=_num(row.get("ra")), dec=_num(row.get("dec")),
        ref=reference_text(row.get("pl_orbper_reflink")), retrieved=retrieved)


def _from_koi(row: dict, retrieved: str) -> ArchivePlanet:
    t0 = _num(row.get("koi_time0bk"))
    return ArchivePlanet(
        name=row["kepoi_name"], host=row.get("kepler_name") or f"KIC {row['kepid']}", table="cumulative",
        period=_num(row.get("koi_period")), period_err=_num(row.get("koi_period_err1")),
        t0_bjd=None if t0 is None else t0 + BKJD_OFFSET, duration_h=_num(row.get("koi_duration")),
        depth=_scaled(row.get("koi_depth"), 1e-6), rp_rs=_num(row.get("koi_ror")),
        rp_earth=_num(row.get("koi_prad")), disposition=row.get("koi_disposition") or "",
        kic=int(row["kepid"]), ra=_num(row.get("ra")), dec=_num(row.get("dec")),
        retrieved=retrieved)


def _from_toi(row: dict, retrieved: str) -> ArchivePlanet:
    rp_earth = _num(row.get("pl_rade"))
    return ArchivePlanet(
        name=f"TOI-{row['toi']}", host=f"TIC {row['tid']}", table="toi",
        period=_num(row.get("pl_orbper")), period_err=_num(row.get("pl_orbpererr1")),
        t0_bjd=_num(row.get("pl_tranmid")), duration_h=_num(row.get("pl_trandurh")),
        depth=_scaled(row.get("pl_trandep"), 1e-6),
        rp_rs=_rp_rs_from_radii(rp_earth, _num(row.get("st_rad"))), rp_earth=rp_earth,
        disposition=row.get("tfopwg_disp") or "", tic=int(row["tid"]),
        ra=_num(row.get("ra")), dec=_num(row.get("dec")), retrieved=retrieved)


def _run(client: CatalogClient | None, adql: str, convert, refresh: bool) -> ArchiveResult:
    response: Response = (client or default_client()).tap(adql, refresh=refresh)
    date = response.retrieved_date
    planets = [convert(row, date) for row in response.data]
    planets.sort(key=lambda p: (p.period is None, p.period or 0.0))
    return ArchiveResult(planets=planets, retrieved=date, queries=[" ".join(adql.split())])


# -- queries -------------------------------------------------------------------

def confirmed_planets(tic: int | None = None, host: str | None = None, name: str | None = None,
                      transiting_only: bool = False, client: CatalogClient | None = None,
                      refresh: bool = False) -> ArchiveResult:
    """Confirmed planets of a star (by TIC or host name) or one planet by name."""
    clauses = []
    if tic is not None:
        clauses.append(f"tic_id = {adql_string(f'TIC {int(tic)}')}")
    if host:
        clauses.append(f"lower(hostname) = {adql_string(host.strip().lower())}")
    if name:
        clauses.append(f"lower(pl_name) = {adql_string(name.strip().lower())}")
    if not clauses:
        raise ValueError("confirmed_planets needs a tic, host or name")
    where = " or ".join(clauses)
    if transiting_only:
        where = f"({where}) and tran_flag = 1"
    return _run(client, f"select {PSCOMPPARS_COLUMNS} from pscomppars where {where}",
                _from_pscomppars, refresh)


def kois(kic: int | None = None, koi_number: int | None = None,
         client: CatalogClient | None = None, refresh: bool = False) -> ArchiveResult:
    """Kepler Objects of Interest for a KIC star or a KOI system number (72 for K00072.xx)."""
    if kic is not None:
        where = f"kepid = {int(kic)}"
    elif koi_number is not None:
        where = f"kepoi_name like {adql_string(f'K{int(koi_number):05d}.%')}"
    else:
        raise ValueError("kois needs a kic or koi_number")
    return _run(client, f"select {KOI_COLUMNS} from cumulative where {where}", _from_koi, refresh)


def tois(tic: int | None = None, toi_number: int | None = None,
         client: CatalogClient | None = None, refresh: bool = False) -> ArchiveResult:
    """TESS Objects of Interest for a TIC star or a TOI system number (270 for TOI-270.xx)."""
    if tic is not None:
        where = f"tid = {int(tic)}"
    elif toi_number is not None:
        where = f"toipfx = {adql_string(str(int(toi_number)))}"
    else:
        raise ValueError("tois needs a tic or toi_number")
    return _run(client, f"select {TOI_COLUMNS} from toi where {where}", _from_toi, refresh)


def known_planets(target: StarTarget, client: CatalogClient | None = None,
                  refresh: bool = False) -> ArchiveResult:
    """Confirmed transiting planets of ``target`` (by TIC, else by name)."""
    if target.tic is not None:
        return confirmed_planets(tic=target.tic, transiting_only=True, client=client, refresh=refresh)
    return confirmed_planets(host=target.name, transiting_only=True, client=client, refresh=refresh)


def all_signals(target: StarTarget, client: CatalogClient | None = None,
                refresh: bool = False) -> ArchiveResult:
    """Every published signal on ``target``: confirmed planets, KOIs and TOIs.

    Used to check that a validation "null" star really has none.
    """
    result = ArchiveResult()
    if target.tic is not None:
        result.extend(confirmed_planets(tic=target.tic, client=client, refresh=refresh))
        result.extend(tois(tic=target.tic, client=client, refresh=refresh))
    else:
        result.extend(confirmed_planets(host=target.name, client=client, refresh=refresh))
    if target.kic is not None:
        result.extend(kois(kic=target.kic, client=client, refresh=refresh))
    return result
