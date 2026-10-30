"""Turn what a user types into a :class:`StarTarget`.

Accepted forms (case-insensitive except planet letters):

- catalog numbers: ``TIC 261136679``, ``KIC 8006161``, ``EPIC 201367065``,
  or a bare number (TIC, or KIC when the mission hint is Kepler);
- objects of interest: ``KOI-72``, ``K00072.01``, ``TOI-270``, ``TOI 270.01``;
- planet names (``Kepler-10 b``, ``WASP-18b``, ``pi Men c``), which resolve
  to their host star;
- any star name SIMBAD knows (``Kepler-2``, ``LHS 3844``, ``HD 39091``).

Names are looked up first among confirmed planet hosts in the Exoplanet
Archive, then through the MAST name resolver (SIMBAD / NED / EPIC) and the
nearest TIC star.  Every service response is cached on disk, so resolving a
name a second time needs no network.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

from ..models import MissionHint, StarTarget
from . import archive, tic as ticdb
from .client import CatalogClient, default_client

log = logging.getLogger(__name__)

NameKind = Literal["tic", "kic", "epic", "koi", "toi", "planet", "name"]


class ResolveError(LookupError):
    """A name could not be resolved to a star."""


@dataclass(frozen=True)
class ParsedName:
    kind: NameKind
    text: str  # normalised display form, e.g. "TIC 261136679", "KOI-72", "Kepler-10 b"
    number: int | None = None  # catalog or object-of-interest number
    host: str | None = None  # for planets: the host-star part of the name


_TIC = re.compile(r"^TIC[\s_-]*(\d+)$", re.I)
_KIC = re.compile(r"^(?:KIC|KID|kepid)[\s_-]*(\d+)$", re.I)
_EPIC = re.compile(r"^(?:EPIC|K2ID)[\s_-]*(\d+)$", re.I)
_KOI = re.compile(r"^(?:KOI[\s_-]*0*(\d+)(?:\.\d+)?|K0*(\d+)\.\d{2})$", re.I)
_TOI = re.compile(r"^TOI[\s_-]*(\d+)(?:\.\d+)?$", re.I)
_NUMBER = re.compile(r"^\d+$")
# A lower-case planet letter after whitespace or a digit: "Kepler-10 b", "WASP-18b".
_PLANET = re.compile(r"^(?P<host>.*?\S)(?:\s+|(?<=\d))(?P<letter>[b-i])$")


def parse_name(text: str, mission_hint: MissionHint = "any") -> ParsedName:
    """Classify ``text``; raises ResolveError for an empty string."""
    text = " ".join(text.split())
    if not text:
        raise ResolveError("empty name")
    if m := _TIC.match(text):
        return ParsedName("tic", f"TIC {int(m.group(1))}", int(m.group(1)))
    if m := _KIC.match(text):
        return ParsedName("kic", f"KIC {int(m.group(1))}", int(m.group(1)))
    if m := _EPIC.match(text):
        return ParsedName("epic", f"EPIC {int(m.group(1))}", int(m.group(1)))
    if m := _KOI.match(text):
        number = int(m.group(1) or m.group(2))
        return ParsedName("koi", f"KOI-{number}", number)
    if m := _TOI.match(text):
        return ParsedName("toi", f"TOI-{int(m.group(1))}", int(m.group(1)))
    if _NUMBER.match(text):
        if mission_hint == "Kepler":
            return ParsedName("kic", f"KIC {int(text)}", int(text))
        return ParsedName("tic", f"TIC {int(text)}", int(text))
    if m := _PLANET.match(text):
        return ParsedName("planet", text, host=m.group("host"))
    return ParsedName("name", text)


def split_names(text: str) -> list[str]:
    """Split a typed list such as ``"Kepler-10, TIC 261136679; pi Men"``."""
    return [part.strip() for part in re.split(r"[,;\n]+", text) if part.strip()]


def resolve(text: str, mission_hint: MissionHint | None = None,
            client: CatalogClient | None = None) -> StarTarget:
    """Resolve ``text`` to a star; raises ResolveError, or CatalogError when offline."""
    client = client or default_client()
    parsed = parse_name(text, mission_hint or "any")
    target = _resolve_parsed(parsed, client)
    if mission_hint is not None:
        target.mission_hint = mission_hint
    log.debug("resolved %r to %s", text, target)
    return target


def resolve_many(text: str, mission_hint: MissionHint | None = None,
                 client: CatalogClient | None = None) -> tuple[list[StarTarget], list[str]]:
    """Resolve a typed list; returns (targets, error messages) so one bad name doesn't stop the rest.

    ``CatalogError`` (a service is unreachable) still propagates.
    """
    targets, errors = [], []
    for name in split_names(text):
        try:
            targets.append(resolve(name, mission_hint, client))
        except ResolveError as exc:
            errors.append(f"{name}: {exc}")
    return targets, errors


# -- per kind ------------------------------------------------------------------

def _from_tic_star(name: str, star: ticdb.TicStar, mission_hint: MissionHint = "any") -> StarTarget:
    return StarTarget(name=name, tic=star.tic, kic=star.kic, ra=star.ra, dec=star.dec,
                      tmag=star.tmag, mission_hint=mission_hint, radius_rsun=star.radius_rsun,
                      teff_k=star.teff_k, logg=star.logg, mass_msun=star.mass_msun,
                      credits=star.credits)


def _by_tic(name: str, number: int, client: CatalogClient, hint: MissionHint = "any") -> StarTarget:
    star = ticdb.tic_by_id(number, client)
    if star is None:
        raise ResolveError(f"TIC {number} is not in the TESS Input Catalog")
    return _from_tic_star(name, star, hint)


def _by_kic(name: str, number: int, client: CatalogClient) -> StarTarget:
    star = ticdb.tic_by_kic(number, client)
    if star is None:
        raise ResolveError(f"KIC {number} has no TESS Input Catalog cross-match")
    target = _from_tic_star(name, star, "Kepler")
    target.kic = number
    return target


def _by_position(name: str, ra: float, dec: float, client: CatalogClient) -> StarTarget:
    star = ticdb.nearest_star(ra, dec, client=client)
    if star is None:
        log.info("%s: no TIC star near %.5f %+.5f; keeping the resolver position", name, ra, dec)
        return StarTarget(name=name, ra=ra, dec=dec)
    return _from_tic_star(name, star)


def _by_lookup(name: str, client: CatalogClient) -> StarTarget | None:
    position = ticdb.lookup_name(name, client)
    if position is None:
        return None
    return _by_position(name, position.ra, position.dec, client)


def _by_archive_host(parsed: ParsedName, client: CatalogClient) -> StarTarget | None:
    """A confirmed-planet host matching the name (or the planet's host part)."""
    host = parsed.host or parsed.text
    result = archive.confirmed_planets(host=host, name=parsed.text, client=client)
    if not result.planets:
        return None
    planet = result.planets[0]
    # Keep the name the user knows ("pi Men", not the archive's "HD 39091"),
    # with the archive's capitalisation when it is the same name.
    name = planet.host if planet.host.lower() == host.lower() else host
    if planet.tic is not None:
        try:
            return _by_tic(name, planet.tic, client)
        except ResolveError:
            log.warning("archive host %s lists TIC %s, which the TIC lacks", planet.host, planet.tic)
    if planet.ra is not None and planet.dec is not None:
        return _by_position(name, planet.ra, planet.dec, client)
    return None


def _resolve_parsed(parsed: ParsedName, client: CatalogClient) -> StarTarget:
    kind, number = parsed.kind, parsed.number
    if kind == "tic":
        return _by_tic(parsed.text, number, client)
    if kind == "kic":
        return _by_kic(parsed.text, number, client)
    if kind == "epic":
        target = _by_lookup(parsed.text, client)
        if target is None:
            raise ResolveError(f"{parsed.text} is not known to the MAST name resolver")
        return target
    if kind == "koi":
        found = archive.kois(koi_number=number, client=client)
        if not found.planets:
            raise ResolveError(f"{parsed.text} is not in the cumulative KOI table")
        return _by_kic(parsed.text, found.planets[0].kic, client)
    if kind == "toi":
        found = archive.tois(toi_number=number, client=client)
        if not found.planets:
            raise ResolveError(f"{parsed.text} is not in the TOI table")
        return _by_tic(parsed.text, found.planets[0].tic, client, "TESS")

    # Planet and star names.
    target = _by_archive_host(parsed, client)
    if target is not None:
        return target
    if kind == "planet":
        host = parse_name(parsed.host)
        if host.kind != "name":
            return _resolve_parsed(host, client)
        target = _by_lookup(parsed.host, client)
    else:
        target = _by_lookup(parsed.text, client)
    if target is None:
        raise ResolveError(f"{parsed.text!r} is not a known planet host and the MAST name "
                           "resolver (SIMBAD / NED) does not know it")
    return target
