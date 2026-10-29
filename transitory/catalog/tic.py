"""TESS Input Catalog lookups and name resolution through the MAST API.

``Mast.Name.Lookup`` resolves names through SIMBAD, NED and the K2 EPIC;
the TIC services give TIC/KIC cross-identifications, TESS magnitudes and
stellar radii, temperatures, gravities and masses.  All responses are
cached by :class:`CatalogClient`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .client import CatalogClient, CatalogError, default_client

# Largest cone the sky view may ask for; the TIC is dense (~1e4 stars/deg^2
# near the Galactic plane even at Tmag < 13), so larger fields come from the
# host-star catalog instead.
MAX_CONE_RADIUS_DEG = 1.0
MAX_CONE_ROWS = 5000


@dataclass
class TicStar:
    tic: int
    ra: float
    dec: float
    tmag: float | None = None
    kic: int | None = None
    radius_rsun: float | None = None
    teff_k: float | None = None
    logg: float | None = None
    mass_msun: float | None = None
    obj_type: str = "STAR"
    dist_arcsec: float | None = None  # from the cone centre, for cone searches


@dataclass
class NamePosition:
    """Where the MAST name resolver placed a name."""

    query: str
    canonical_name: str
    ra: float
    dec: float
    resolver: str = ""


def _opt_float(value) -> float | None:
    if value is None or value == "":
        return None
    result = float(value)
    return None if math.isnan(result) else result


def _opt_int(value) -> int | None:
    return None if value is None or value == "" else int(value)


def _tic_star(row: dict) -> TicStar:
    return TicStar(tic=int(row["ID"]), ra=float(row["ra"]), dec=float(row["dec"]),
                   tmag=_opt_float(row.get("Tmag")), kic=_opt_int(row.get("KIC")),
                   radius_rsun=_opt_float(row.get("rad")), teff_k=_opt_float(row.get("Teff")),
                   logg=_opt_float(row.get("logg")), mass_msun=_opt_float(row.get("mass")),
                   obj_type=row.get("objType") or "STAR",
                   dist_arcsec=_opt_float(row.get("dstArcSec")))


def _filtered(client: CatalogClient, param: str, value: int) -> list[TicStar]:
    request = {"service": "Mast.Catalogs.Filtered.Tic", "format": "json",
               "params": {"columns": "*",
                          "filters": [{"paramName": param, "values": [str(value)]}]}}
    rows = client.mast(request).data.get("data") or []
    return [_tic_star(row) for row in rows]


def tic_by_id(tic: int, client: CatalogClient | None = None) -> TicStar | None:
    """The TIC entry for ``tic``, or None if there is none."""
    stars = _filtered(client or default_client(), "ID", int(tic))
    return stars[0] if stars else None


def tic_by_kic(kic: int, client: CatalogClient | None = None) -> TicStar | None:
    """The TIC entry cross-matched to Kepler Input Catalog number ``kic``."""
    stars = _filtered(client or default_client(), "KIC", int(kic))
    # Split or duplicate TIC entries can share a KIC; the brightest is the star.
    stars.sort(key=lambda s: s.tmag if s.tmag is not None else 99.0)
    return stars[0] if stars else None


def tic_cone(ra: float, dec: float, radius_deg: float, max_tmag: float | None = None,
             limit: int = MAX_CONE_ROWS, client: CatalogClient | None = None) -> list[TicStar]:
    """TIC stars within ``radius_deg`` of (ra, dec), nearest first.

    ``max_tmag`` keeps only stars at least that bright.  The radius is
    capped at ``MAX_CONE_RADIUS_DEG`` and the result at ``limit`` rows.
    Positions are rounded (to ~0.4 arcsec) so a slightly moved view reuses
    the cache.
    """
    radius = min(float(radius_deg), MAX_CONE_RADIUS_DEG)
    params: dict = {"ra": round(float(ra), 4), "dec": round(float(dec), 4), "radius": round(radius, 4)}
    if max_tmag is None:
        request = {"service": "Mast.Catalogs.Tic.Cone", "format": "json", "params": params,
                   "pagesize": int(limit), "page": 1}
    else:
        params = {**params, "columns": "*",
                  "filters": [{"paramName": "Tmag", "values": [{"min": -5, "max": float(max_tmag)}]}]}
        request = {"service": "Mast.Catalogs.Filtered.Tic.Position", "format": "json",
                   "params": params, "pagesize": int(limit), "page": 1}
    rows = (client or default_client()).mast(request).data.get("data") or []
    stars = [_tic_star(row) for row in rows]
    stars.sort(key=lambda s: s.dist_arcsec if s.dist_arcsec is not None else math.inf)
    return stars


def nearest_star(ra: float, dec: float, radius_arcsec: float = 10.0,
                 client: CatalogClient | None = None) -> TicStar | None:
    """The TIC star a resolved position refers to.

    Among TIC stars within ``radius_arcsec``, prefers the brightest of those
    within 2 arcsec of the nearest (a resolver position can be a few arcsec
    off a faint companion), else the nearest.
    """
    stars = [s for s in tic_cone(ra, dec, radius_arcsec / 3600.0, client=client)
             if s.obj_type == "STAR"]
    if not stars:
        return None
    nearest = stars[0].dist_arcsec or 0.0
    close = [s for s in stars if (s.dist_arcsec or 0.0) <= nearest + 2.0]
    return min(close, key=lambda s: s.tmag if s.tmag is not None else 99.0)


def lookup_name(name: str, client: CatalogClient | None = None) -> NamePosition | None:
    """Resolve an object name to a position (SIMBAD / NED / EPIC via MAST)."""
    request = {"service": "Mast.Name.Lookup", "format": "json",
               "params": {"input": name.strip(), "format": "json"}}
    reply = (client or default_client()).mast(request).data
    found = reply.get("resolvedCoordinate") or []
    if not found:
        return None
    first = found[0]
    try:
        return NamePosition(query=name, canonical_name=first.get("canonicalName") or name,
                            ra=float(first["ra"]), dec=float(first["decl"]),
                            resolver=first.get("resolver") or "")
    except (KeyError, TypeError, ValueError) as exc:
        raise CatalogError(f"MAST name lookup for {name!r} gave an unusable answer: {first}") from exc
