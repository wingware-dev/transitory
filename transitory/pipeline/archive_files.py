"""Light-curve files found by path in MAST's static directories, without the CAOM search.

MAST lays its light curves out by star number, so a KIC, EPIC or TIC (and,
for TESS, the sectors that cover the star) is enough to find the files
without the search service, which can be down when the files are not:

* Kepler: ``missions/kepler/lightcurves/{kic9[:4]}/{kic9}/kplr{kic9}-{stamp}_llc.fits``,
  one directory for all quarters.  The time stamp names the quarter
  (``KEPLER_QUARTER_STAMPS``, read from the files' ``QUARTER`` headers).
* K2: ``missions/k2/lightcurves/c{N}/{epic[:4]}00000/{epic[4:6]}000/ktwo{epic}-c{NN}_llc.fits``,
  one directory per campaign, so every campaign is probed.  Campaigns 10
  and 11 were observed in halves: directory ``c102`` is campaign 10's second
  half (its first was never released) and ``c111`` / ``c112`` are campaign
  11's.  Campaign 9 has no light curves.
* TESS SPOC: ``missions/tess/tid/s{SSSS}/{tic16 in four groups of 4}/``, with
  ``..-s_lc.fits`` (2-minute) and ``..-a_fast-lc.fits`` (20-second).
* TESS-SPOC (from full-frame images):
  ``hlsps/tess-spoc/s{SSSS}/target/{same groups}/hlsp_tess-spoc_tess_phot_{tic16}-s{SSSS}_tess_v1_lc.fits``.

Two mirrors hold them (``MIRRORS``): archive.stsci.edu, listed by reading
its Apache index pages, and the AWS Open Data bucket ``stpubdata``, listed
with S3's ListObjectsV2 XML over anonymous HTTPS.  S3 has no TESS-SPOC
files (``Mirror.has_tess_spoc``), so a TESS sector with no SPOC file there
is not proof that the star has no light curve for it.

``find_products`` returns product records in ``fetch.py``'s shape, one per
sector, quarter or campaign half, ranked as ``fetch.select_products`` ranks
search results.  A directory or file that is not there (HTTP 404, or an
empty S3 listing) is "no data"; anything else that goes wrong (timeout,
failed connection, 5xx, 429, ...) raises ``SourceUnavailable``, so the
caller can try another mirror rather than record the star as having no data.
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote

import requests

from ..config import USER_AGENT
from ..models import Mission
from ..sources import SourceError

log = logging.getLogger(__name__)

LISTING_TIMEOUT_SECONDS = 15.0
DOWNLOAD_CONNECT_TIMEOUT_SECONDS = 15.0
DOWNLOAD_READ_TIMEOUT_SECONDS = 120.0  # between bytes, not for the whole file
DOWNLOAD_CHUNK_BYTES = 1 << 16

STSCI_URL = "https://archive.stsci.edu"
S3_URL = "https://stpubdata.s3.amazonaws.com"

# The long-cadence file of each Kepler quarter carries the same time stamp
# for every star; checked against the QUARTER header of the files of KIC
# 11904151 (Kepler-10), 10593626, 8311864 and 6922244 (2026-10-30).  Q4 has
# two: stars on CCD module 3, which failed during Q4, have a shorter file.
KEPLER_QUARTER_STAMPS = {
    "2009131105131": 0, "2009166043257": 1, "2009259160929": 2, "2009350155506": 3,
    "2010009091648": 4, "2010078095331": 4, "2010174085026": 5, "2010265121752": 6,
    "2010355172524": 7, "2011073133259": 8, "2011177032512": 9, "2011271113734": 10,
    "2012004120508": 11, "2012088054726": 12, "2012179063303": 13, "2012277125453": 14,
    "2013011073258": 15, "2013098041711": 16, "2013131215648": 17,
}

# K2 campaign directories with light curves: (directory, campaign, half).
K2_CAMPAIGN_DIRS = (
    [(f"c{n}", n, "") for n in range(0, 9)]
    + [("c102", 10, "b"), ("c111", 11, "a"), ("c112", 11, "b")]
    + [(f"c{n}", n, "") for n in range(12, 20)]
)

KEPLER_LONG_CADENCE_S = 1800.0


def tess_spoc_cadence_s(sector: int) -> float:
    """Exposure of TESS full-frame images, so of TESS-SPOC light curves, in a sector."""
    if sector <= 26:
        return 1800.0
    if sector <= 55:
        return 600.0
    return 200.0


class SourceUnavailable(SourceError):
    """A mirror could not be asked (timeout, no connection, server error, refused).

    Not a lack of data: the same files may be on another mirror, or here later.
    """


# transport(url, timeout) -> the reply body, or None for HTTP 404; raises
# SourceUnavailable for anything else that is not a 200.
Transport = Callable[[str, float], "bytes | None"]


def _unavailable(what: str, exc: BaseException) -> SourceUnavailable:
    return SourceUnavailable(f"{what} could not be fetched: {exc}")


def requests_transport(url: str, timeout: float) -> bytes | None:
    """The real transport: one GET with the project user agent and no retry."""
    try:
        reply = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    except requests.RequestException as exc:
        raise _unavailable(url, exc) from exc
    if reply.status_code in (404, 410):
        return None
    if reply.status_code != 200:
        raise SourceUnavailable(f"{url} could not be fetched: HTTP {reply.status_code}")
    return reply.content


# --- listing parsers ----------------------------------------------------------------------

class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def parse_apache_index(html: str) -> list[str]:
    """The file names an Apache directory index links to (not subdirectories or sorting links)."""
    parser = _LinkParser()
    parser.feed(html)
    names = []
    for href in parser.hrefs:
        if "/" in href or "?" in href or href.startswith("#"):
            continue
        names.append(unquote(href))
    return list(dict.fromkeys(names))


_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def parse_s3_listing(xml: bytes | str) -> tuple[list[str], str | None]:
    """``(keys, continuation token or None)`` from an S3 ListObjectsV2 reply."""
    root = ET.fromstring(xml)
    keys = [el.text or "" for el in root.iter(f"{_S3_NS}Key")]
    truncated = (root.findtext(f"{_S3_NS}IsTruncated") or "").strip().lower() == "true"
    token = root.findtext(f"{_S3_NS}NextContinuationToken") if truncated else None
    return keys, token


# --- mirrors ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Mirror:
    """One copy of MAST's light-curve files, and how to list a directory of it.

    ``roots`` maps a collection (``kepler``, ``k2``, ``tess``, ``tess-spoc``)
    to its top directory on this mirror; a collection it lacks is absent.
    """

    name: str
    base_url: str
    roots: dict[str, str]
    kind: str  # "apache" or "s3"

    @property
    def has_tess_spoc(self) -> bool:
        return "tess-spoc" in self.roots

    def url(self, collection: str, path: str, filename: str = "") -> str:
        return f"{self.base_url}/{self.roots[collection]}/{path}/{quote(filename)}"

    def list(self, collection: str, path: str, prefix: str = "", *,
             transport: Transport | None = None) -> list[str] | None:
        """File names starting with ``prefix`` in a directory, or None if it is not there."""
        get = transport or requests_transport
        if self.kind == "apache":
            body = get(self.url(collection, path), LISTING_TIMEOUT_SECONDS)
            if body is None:
                return None
            try:
                names = parse_apache_index(body.decode("utf-8", errors="replace"))
            except Exception as exc:
                raise _unavailable(f"Listing of {self.url(collection, path)}", exc) from exc
            return [n for n in names if n.startswith(prefix)]
        directory = f"{self.roots[collection]}/{path}/"
        names: list[str] = []
        token = None
        while True:
            url = f"{self.base_url}/?list-type=2&prefix={quote(directory + prefix)}"
            if token:
                url += f"&continuation-token={quote(token)}"
            body = get(url, LISTING_TIMEOUT_SECONDS)
            if body is None:  # the bucket itself is missing: not a lack of data
                raise SourceUnavailable(f"{url} could not be fetched: HTTP 404")
            try:
                keys, token = parse_s3_listing(body)
            except ET.ParseError as exc:
                raise _unavailable(f"Listing of {url}", exc) from exc
            names += [k[len(directory):] for k in keys if k.startswith(directory)]
            if not token:
                break
        return [n for n in names if "/" not in n] or None


MIRRORS = {
    "stsci": Mirror("stsci", STSCI_URL, {
        "kepler": "missions/kepler/lightcurves",
        "k2": "missions/k2/lightcurves",
        "tess": "missions/tess/tid",
        "tess-spoc": "hlsps/tess-spoc",
    }, "apache"),
    "s3": Mirror("s3", S3_URL, {
        "kepler": "kepler/public/lightcurves",
        "k2": "k2/public/lightcurves",
        "tess": "tess/public/tid",
    }, "s3"),
}
MIRROR_TITLES = {"stsci": "STScI archive files", "s3": "AWS Open Data (stpubdata)"}


# --- paths --------------------------------------------------------------------------------

def kepler_dir(kic: int) -> str:
    kic9 = f"{int(kic):09d}"
    return f"{kic9[:4]}/{kic9}"


def k2_dir(campaign_dir: str, epic: int) -> str:
    epic9 = f"{int(epic):09d}"
    return f"{campaign_dir}/{epic9[:4]}00000/{epic9[4:6]}000"


def _tic_groups(tic: int) -> str:
    tic16 = f"{int(tic):016d}"
    return "/".join(tic16[i:i + 4] for i in range(0, 16, 4))


def tess_spoc_dir(sector: int, tic: int) -> str:
    """The directory of a star's SPOC (2-minute and 20-second) files in a sector."""
    return f"s{int(sector):04d}/{_tic_groups(tic)}"


def tess_hlsp_dir(sector: int, tic: int) -> str:
    """The directory of a star's TESS-SPOC (full-frame-image) files in a sector."""
    return f"s{int(sector):04d}/target/{_tic_groups(tic)}"


# --- product records ----------------------------------------------------------------------

def _record(mirror: Mirror, mission: Mission, collection: str, path: str, filename: str, *,
            author: str, exptime: float, seq: int | None, half: str, target_name: str,
            label: str) -> dict:
    stem = re.sub(r"\.fits(\.gz)?$", "", filename)
    return {
        "file": f"archive/{mission}/{filename}",
        "url": mirror.url(collection, path, filename),
        "source": mirror.name,
        "author": author,
        "exptime": float(exptime),
        "seq": seq,
        "half": half,
        "label": label,
        "obs_id": stem,
        "product": stem,
        "target_name": target_name,
    }


def rank(record: dict) -> int | None:
    """``fetch.select_products``'s preference for a product: lower is better, None is unused."""
    author, exptime = record["author"], round(float(record["exptime"]))
    if author in ("Kepler", "K2"):
        return 0 if exptime == KEPLER_LONG_CADENCE_S else None
    if author == "SPOC" and exptime == 120:
        return 0
    if author == "TESS-SPOC":
        return 1
    if author == "SPOC":
        return 2
    return None


def best_per_period(records: Iterable[dict]) -> list[dict]:
    """The best-ranked record for each sector, quarter or campaign half, in order."""
    best: dict[tuple[int, str], tuple[int, dict]] = {}
    for r in records:
        if r["seq"] is None:
            continue
        rk = rank(r)
        if rk is None:
            continue
        key = (int(r["seq"]), r.get("half", ""))
        if key not in best or rk < best[key][0]:
            best[key] = (rk, r)
    return [r for _, (_, r) in sorted(best.items())]


def _kepler(mirror: Mirror, kic: int, transport) -> list[dict]:
    kic9 = f"{int(kic):09d}"
    path = kepler_dir(kic)
    names = mirror.list("kepler", path, f"kplr{kic9}-", transport=transport) or []
    records = []
    for name in names:
        m = re.fullmatch(rf"kplr{kic9}-(\d{{13}})_llc\.fits", name)
        if not m:
            continue
        quarter = KEPLER_QUARTER_STAMPS.get(m.group(1))
        if quarter is None:
            log.warning("Kepler file %s has a time stamp of no known quarter; skipped", name)
            continue
        records.append(_record(mirror, "Kepler", "kepler", path, name, author="Kepler",
                               exptime=KEPLER_LONG_CADENCE_S, seq=quarter, half="",
                               target_name=f"kplr{kic9}", label=f"Kepler Quarter {quarter:02d}"))
    return records


def _k2(mirror: Mirror, epic: int, transport) -> list[dict]:
    epic9 = f"{int(epic):09d}"
    records = []
    for campaign_dir, campaign, half in K2_CAMPAIGN_DIRS:
        path = k2_dir(campaign_dir, epic)
        names = mirror.list("k2", path, f"ktwo{epic9}-", transport=transport) or []
        name = f"ktwo{epic9}-{campaign_dir[0]}{campaign_dir[1:].zfill(2)}_llc.fits"
        if name in names:
            records.append(_record(mirror, "K2", "k2", path, name, author="K2",
                                   exptime=KEPLER_LONG_CADENCE_S, seq=campaign, half=half,
                                   target_name=f"ktwo{epic9}",
                                   label=f"K2 Campaign {campaign:02d}{half}"))
    return records


def _tess(mirror: Mirror, tic: int, sectors: Iterable[int], transport) -> list[dict]:
    tic16 = f"{int(tic):016d}"
    records = []
    for sector in sorted(set(int(s) for s in sectors)):
        label = f"TESS Sector {sector:02d}"
        common = dict(seq=sector, half="", target_name=str(int(tic)), label=label)
        path = tess_spoc_dir(sector, tic)
        names = mirror.list("tess", path, "tess", transport=transport) or []
        found = {}
        for name in names:
            m = re.fullmatch(rf"tess\d{{13}}-s{sector:04d}-{tic16}-\d{{4}}-(s_lc|a_fast-lc)\.fits",
                             name)
            if m:
                found[m.group(1)] = name
        if "s_lc" in found:
            records.append(_record(mirror, "TESS", "tess", path, found["s_lc"], author="SPOC",
                                   exptime=120, **common))
            continue  # the best there is: no need to look further
        if mirror.has_tess_spoc:
            hpath = tess_hlsp_dir(sector, tic)
            hname = f"hlsp_tess-spoc_tess_phot_{tic16}-s{sector:04d}_tess_v1_lc.fits"
            if hname in (mirror.list("tess-spoc", hpath, "hlsp_", transport=transport) or []):
                records.append(_record(mirror, "TESS", "tess-spoc", hpath, hname,
                                       author="TESS-SPOC", exptime=tess_spoc_cadence_s(sector),
                                       **common))
                continue
        if "a_fast-lc" in found:
            records.append(_record(mirror, "TESS", "tess", path, found["a_fast-lc"],
                                   author="SPOC", exptime=20, **common))
    return records


def find_products(mission: Mission, number: int, *, mirror: str | Mirror = "stsci",
                  sectors: Iterable[int] | None = None,
                  transport: Transport | None = None) -> list[dict]:
    """Product records for a star's light curves on ``mirror``, best per period, in order.

    ``number`` is the KIC for Kepler, the EPIC for K2 and the TIC for TESS.
    TESS needs ``sectors``, the sectors whose images cover the star (from
    ``tess_sectors.sectors_for``); only those are looked in.  An empty list
    means the mirror has no light curves for the star.  Raises
    ``SourceUnavailable`` when a listing cannot be fetched.
    """
    m = MIRRORS[mirror] if isinstance(mirror, str) else mirror
    if mission == "Kepler":
        records = _kepler(m, number, transport)
    elif mission == "K2":
        records = _k2(m, number, transport)
    elif mission == "TESS":
        if sectors is None:
            raise ValueError("TESS products need the sectors that cover the star")
        records = _tess(m, number, sectors, transport)
    else:
        raise ValueError(f"Unknown mission {mission!r}")
    return best_per_period(records)


# --- downloading --------------------------------------------------------------------------

def download(url: str, dest: str | os.PathLike, *, get=None,
             timeout: tuple[float, float] = (DOWNLOAD_CONNECT_TIMEOUT_SECONDS,
                                             DOWNLOAD_READ_TIMEOUT_SECONDS)) -> Path:
    """Stream ``url`` to ``dest`` and return its path; nothing is left at ``dest`` on failure.

    The file is written beside ``dest`` and renamed into place once complete.
    Raises ``SourceError`` if the file is not there (404) and
    ``SourceUnavailable`` if it could not be fetched.  ``get`` stands in for
    ``requests.get`` in tests.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    get = get or requests.get
    try:
        with get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout, stream=True) as reply:
            if reply.status_code in (404, 410):
                raise SourceError(f"{url} is not there (HTTP {reply.status_code})")
            if reply.status_code != 200:
                raise SourceUnavailable(f"Download of {url} failed: HTTP {reply.status_code}")
            with open(part, "wb") as out:
                for chunk in reply.iter_content(DOWNLOAD_CHUNK_BYTES):
                    out.write(chunk)
        os.replace(part, dest)
    except requests.RequestException as exc:
        raise _unavailable(f"Download of {url}", exc) from exc
    finally:
        part.unlink(missing_ok=True)
    return dest
