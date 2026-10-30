"""``MastSource``: real TESS and Kepler light curves from MAST, via lightkurve.

Products chosen, per sector or quarter:

* TESS: SPOC 2-minute light curves, else TESS-SPOC full-frame-image light
  curves, else SPOC 20-second ones.
* Kepler: long-cadence (30-minute) light curves from the Kepler pipeline.

PDCSAP flux is used, falling back to SAP for a file with no PDCSAP values.
Quality-flagged cadences are dropped on reading (lightkurve's default
bitmask) and each file is normalised by its median before stitching.

Being polite to the archive: files go to ``config.cache_dir()`` and the
query -> files mapping to the cache index (``pipeline/cache.py``), so nothing
is downloaded twice and a repeated fetch makes no request at all.  Requests
are serial across all threads and carry a descriptive user agent.

MAST is sometimes slow for minutes at a time.  astroquery sends its requests
with no timeout, so each one here gets ``MAST_TIMEOUT_SECONDS``, and a search
or download that times out or cannot connect is tried once more after
``MAST_RETRY_BACKOFF_SECONDS`` before the star fails with a ``SourceError``.
A search MAST keeps answering "still executing" is given up after
``MAST_PORTAL_TIMEOUT_SECONDS`` in all, without another try.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import warnings
from collections.abc import Iterable
from typing import Any

import numpy as np
import requests
from requests.adapters import HTTPAdapter

from ..config import USER_AGENT, cache_dir
from ..models import LightCurve, Mission, ProgressCallback, StarTarget
from ..sources import SourceError
from .cache import CacheIndex, cache_key

log = logging.getLogger(__name__)

TESS_AUTHORS = ("SPOC", "TESS-SPOC")
KEPLER_AUTHOR = "Kepler"
KEPLER_LONG_CADENCE_S = 1800.0

# Mission data DOIs at MAST, by pipeline, with their landing-page titles
# (checked at doi.org, 2026-10-29).  MAST asks for each as a doi.org URL.
DOIS = {
    "SPOC": "10.17909/t9-nmc8-f686",
    "TESS-SPOC": "10.17909/t9-wpz1-8s54",
    "Kepler": "10.17909/T9488N",
}
DOI_TITLES = {
    "10.17909/t9-nmc8-f686": "TESS Light Curves - All Sectors",
    "10.17909/t9-wpz1-8s54": 'TESS Light Curves From Full Frame Images ("TESS-SPOC")',
    "10.17909/T9488N": "Kepler LC, Q0-Q17",
}

# MAST's acknowledgement for each mission, as given at
# https://archive.stsci.edu/publishing/mission-acknowledgements (checked
# 2026-10-29; the page writes the contract number with an en dash).
MAST_CREDITS = {
    "TESS": ("This paper includes data collected with the TESS mission, obtained from the MAST data "
             "archive at the Space Telescope Science Institute (STScI). Funding for US Institutions "
             "for the TESS mission is provided by the NASA Explorer Program. STScI is operated by "
             "the Association of Universities for Research in Astronomy, Inc., under NASA contract "
             "NAS5-26555."),
    "Kepler": ("This paper includes data collected by the Kepler mission and obtained from the MAST "
               "data archive at the Space Telescope Science Institute (STScI). Funding to US "
               "Institutions for the Kepler mission was provided by the NASA Science Mission "
               "Directorate. STScI is operated by the Association of Universities for Research in "
               "Astronomy, Inc., under NASA contract NAS5-26555."),
}
# Lightkurve's own recommended acknowledgement ("Citing Lightkurve").
LIGHTKURVE_CREDIT = ("This research made use of Lightkurve, a Python package for Kepler and TESS "
                     "data analysis (Lightkurve Collaboration, 2018).")


def doi_line(doi: str, mission: str = "") -> str:
    """``title: https://doi.org/...`` for a mission data DOI."""
    title = DOI_TITLES.get(doi, f"{mission} data".strip())
    return f"{title}: https://doi.org/{doi}"

# A search that found nothing is believed for this long before MAST is asked again.
NEGATIVE_CACHE_DAYS = 7.0

# One request at a time to MAST from this process, whichever thread asks.
_network_lock = threading.Lock()
_user_agent_set = False
_timeouts_set = False

# Seconds to wait for MAST to connect or to send more of a reply.
MAST_TIMEOUT_SECONDS = 120.0
# Seconds in all astroquery keeps asking about a search MAST is still executing.
MAST_PORTAL_TIMEOUT_SECONDS = 180.0
# A failed search or download is tried once more after this pause.
MAST_RETRY_BACKOFF_SECONDS = 10.0


def _lightkurve():
    """Import lightkurve on first use (it is slow to import); tests replace this."""
    with warnings.catch_warnings():
        # lightkurve warns at import about an optional PRF dependency we don't use.
        warnings.filterwarnings("ignore", message=".*tpfmodel submodule.*")
        import lightkurve
    return lightkurve


def _ensure_user_agent() -> None:
    """Prefix the transitory user agent on astroquery's MAST session (once)."""
    global _user_agent_set
    if _user_agent_set:
        return
    try:
        from astroquery.mast import Observations
        headers = Observations._session.headers
    except (ImportError, AttributeError) as exc:
        log.warning("Could not set the MAST user agent: %s", exc)
        return
    if USER_AGENT not in headers.get("User-Agent", ""):
        headers["User-Agent"] = f"{USER_AGENT} {headers.get('User-Agent', '')}".strip()
    _user_agent_set = True


class _TimeoutAdapter(HTTPAdapter):
    """An adapter that gives requests sent with no timeout ``timeout`` instead."""

    def __init__(self, timeout: float, **kwargs):
        self.timeout = timeout
        super().__init__(**kwargs)

    def send(self, request, timeout=None, **kwargs):
        return super().send(request, timeout=self.timeout if timeout is None else timeout, **kwargs)


class _TimeoutRequests:
    """The ``requests`` module, but ``get`` with no timeout gets ``timeout``."""

    def __init__(self, timeout: float):
        self.timeout = timeout

    def get(self, *args, timeout=None, **kwargs):
        return requests.get(*args, timeout=self.timeout if timeout is None else timeout, **kwargs)

    def __getattr__(self, name):
        return getattr(requests, name)


def _ensure_timeouts() -> None:
    """Give astroquery's MAST requests a finite timeout (once).

    Searches and downloads go through the ``Observations`` session; name
    resolution calls ``requests.get`` directly from ``astroquery.mast.utils``.
    The portal API polls a search that is still executing for up to its
    ``TIMEOUT`` (600 s by default), then raises ``TimeoutError``.
    """
    global _timeouts_set
    if _timeouts_set:
        return
    try:
        from astroquery.mast import Observations, utils as mast_utils
        session = Observations._session
    except (ImportError, AttributeError) as exc:
        log.warning("Could not set a timeout on MAST requests: %s", exc)
        return
    adapter = _TimeoutAdapter(MAST_TIMEOUT_SECONDS)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    if getattr(mast_utils, "requests", None) is requests:
        mast_utils.requests = _TimeoutRequests(MAST_TIMEOUT_SECONDS)
    else:
        log.warning("MAST name resolution keeps astroquery's default (no) timeout")
    portal = getattr(Observations, "_portal_api_connection", None)
    if hasattr(portal, "TIMEOUT"):
        portal.TIMEOUT = MAST_PORTAL_TIMEOUT_SECONDS
    else:
        log.warning("MAST searches keep astroquery's default polling limit")
    _timeouts_set = True


def _http_status(exc: BaseException | None) -> int | None:
    """The HTTP status of the first ``HTTPError`` with a reply in ``exc``'s chain."""
    while exc is not None:
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            return exc.response.status_code
        exc = exc.__cause__ or exc.__context__
    return None


def _is_transient(exc: BaseException) -> bool:
    """Whether ``exc`` is MAST being slow or unreachable, worth one more try.

    Not the portal's ``TimeoutError``: that search already ran its full time.
    Not HTTP 429 either: MAST is asking us to slow down.
    """
    if _http_status(exc) in (500, 502, 503, 504):
        return True
    while exc is not None:
        if isinstance(exc, (requests.Timeout, requests.ConnectionError, ConnectionError)):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _with_retry(what: str, call, on_retry=None):
    """``call()``, tried once more after a pause if MAST timed out or was unreachable.

    ``on_retry(message)`` is told before the pause.
    """
    try:
        return call()
    except Exception as exc:
        if not _is_transient(exc):
            raise
        log.warning("%s failed (%s); trying again in %.0f s", what, exc, MAST_RETRY_BACKOFF_SECONDS)
        if on_retry:
            on_retry(f"{what} timed out or could not connect; trying again")
    time.sleep(MAST_RETRY_BACKOFF_SECONDS)
    return call()


# Said with every failure that is MAST's side, so it is not read as "no data".
_NOT_MISSING_DATA = "This is not a lack of data for the star: try again later."


def _failure(what: str, exc: Exception) -> SourceError:
    status = _http_status(exc)
    if status == 429:
        return SourceError(f"MAST is limiting requests from this machine (HTTP 429): {what} was "
                           f"refused. {_NOT_MISSING_DATA} ({exc})")
    if _is_transient(exc):
        problem = f"MAST is having problems (HTTP {status})" if status else \
            "MAST is down or cannot be reached from this network"
        return SourceError(f"{problem}: {what} timed out or could not connect (tried twice). "
                           f"{_NOT_MISSING_DATA} ({exc})")
    if isinstance(exc, TimeoutError):
        return SourceError(f"MAST is overloaded or down: {what} timed out. "
                           f"{_NOT_MISSING_DATA} ({exc})")
    return SourceError(f"{what} failed: {exc}")


def number_ranges(numbers: Iterable[int]) -> str:
    """``[1, 2, 3, 5, 7, 8]`` -> ``"1-3, 5, 7-8"``."""
    ranges: list[list[int]] = []
    for n in sorted(set(numbers)):
        if ranges and n == ranges[-1][1] + 1:
            ranges[-1][1] = n
        else:
            ranges.append([n, n])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in ranges)


def mission_credits(lc: LightCurve) -> list[str]:
    """Acknowledgement lines for the mission data in ``lc``.

    MAST asks for the sectors used next to a TESS DOI; quarters are given
    for Kepler in the same way.
    """
    if lc.mission not in ("TESS", "Kepler"):
        return []
    lines = [MAST_CREDITS[lc.mission]]
    for doi in lc.dois:
        sectors = lc.sectors_for(doi)
        used = f" ({sector_unit(lc.mission)} {number_ranges(sectors)})" if sectors else ""
        lines.append(doi_line(doi, lc.mission) + used)
    lines.append(LIGHTKURVE_CREDIT)
    return lines


def sector_unit(mission: str) -> str:
    """What a mission's observing periods are called: TESS sectors, Kepler quarters."""
    return "sectors" if mission == "TESS" else "quarters"


def _mission_order(target: StarTarget) -> list[Mission]:
    if target.mission_hint == "TESS":
        return ["TESS"]
    if target.mission_hint == "Kepler":
        return ["Kepler"]
    # A KIC number means the star was in the Kepler field, whose 4-year
    # light curves beat TESS for small planets; otherwise TESS covers more sky.
    return ["Kepler", "TESS"] if target.kic is not None else ["TESS", "Kepler"]


def _query_for(target: StarTarget, mission: Mission) -> str:
    if mission == "TESS" and target.tic is not None:
        return f"TIC {target.tic}"
    if mission == "Kepler" and target.kic is not None:
        return f"KIC {target.kic}"
    if target.name.strip():
        return target.name.strip()
    if target.ra is not None and target.dec is not None:
        return f"{target.ra:.6f} {target.dec:.6f}"
    raise SourceError(f"No identifier, name or coordinates to search {mission} data for")


def _seq_number(label: str) -> int | None:
    """Sector / quarter number from a SearchResult 'mission' label like 'TESS Sector 14'."""
    m = re.search(r"(\d+)\s*[ab]?\s*$", str(label))
    return int(m.group(1)) if m else None


def _tess_rank(author: str, exptime: float) -> int | None:
    if author == "SPOC" and round(exptime) == 120:
        return 0
    if author == "TESS-SPOC":
        return 1
    if author == "SPOC":
        return 2
    return None


def select_products(table: Any, mission: Mission) -> list[int]:
    """Row indices of the products to use: one per sector or quarter, best first.

    ``table`` is a lightkurve ``SearchResult.table`` (rows sorted by distance).
    Only the target nearest the search position is kept, so a name search
    does not stitch in a neighbour.
    """
    n = len(table)
    if n == 0:
        return []
    names = [str(x) for x in table["target_name"]]
    if "distance" in table.colnames:
        dist = np.asarray(table["distance"], dtype=float)
        dist = np.where(np.isfinite(dist), dist, np.inf)
        nearest = names[int(np.argmin(dist))]
    else:
        nearest = names[0]

    best: dict[int, tuple[int, int]] = {}  # seq -> (rank, row)
    for i in range(n):
        if names[i] != nearest:
            continue
        author = str(table["author"][i])
        exptime = float(table["exptime"][i])
        seq = _seq_number(table["mission"][i])
        if seq is None:
            continue
        if mission == "TESS":
            rank = _tess_rank(author, exptime)
        else:
            rank = 0 if author == KEPLER_AUTHOR and round(exptime) == KEPLER_LONG_CADENCE_S else None
        if rank is None:
            continue
        if seq not in best or rank < best[seq][0]:
            best[seq] = (rank, i)
    return [row for _, (_, row) in sorted(best.items())]


def _values(q: Any) -> np.ndarray:
    """A float array from a Quantity / Time / masked column, masked values as NaN."""
    v = getattr(q, "value", q)
    if hasattr(v, "filled"):
        v = v.filled(np.nan)
    return np.asarray(v, dtype=float)


class MastSource:
    """TESS / Kepler light curves from MAST, cached in ``cache_dir()``.

    ``max_products`` caps how many sectors or quarters are used (the most
    recent ones are dropped first), for stars with very long coverage.
    """

    name = "MAST"

    def __init__(self, index: CacheIndex | None = None, max_products: int | None = None):
        self._index = index
        self.max_products = max_products

    @property
    def index(self) -> CacheIndex:
        # Resolved lazily so cache_dir() follows $TRANSITORY_CACHE_DIR at fetch time.
        return self._index if self._index is not None else CacheIndex()

    def credits(self, lc: LightCurve) -> list[str]:
        return mission_credits(lc)

    def fetch(self, target: StarTarget, progress_cb: ProgressCallback | None = None) -> LightCurve:
        def progress(message: str, fraction: float) -> None:
            if progress_cb:
                progress_cb(message, fraction)

        tried = []
        missions = _mission_order(target)
        for m_i, mission in enumerate(missions):
            query = _query_for(target, mission)
            tried.append(f"{mission} ({query})")
            base, span = m_i / len(missions), 1.0 / len(missions)
            products = self._products(query, mission,
                                      lambda msg, f: progress(msg, base + span * f))
            if products:
                lc = self._stitch(products, mission)
                progress(f"{mission} light curve ready", 1.0)
                return lc
        raise SourceError(f"No light curves at MAST for {target.label}: searched {', '.join(tried)}")

    # --- products: from the index, else search + download ---------------------------

    def _products(self, query: str, mission: Mission, progress: ProgressCallback) -> list[dict]:
        """Product records (with absolute ``path``) for ``query``, downloading as needed."""
        index = self.index
        key = cache_key("lightcurve", mission, query)
        entry = index.get(key)
        if entry is not None:
            if not entry["products"]:
                if self._negative_fresh(index, key):
                    log.info("%s: no %s data (cached %s)", query, mission, entry["created"])
                    return []
            else:
                products = [dict(p, path=index.absolute(p["file"])) for p in entry["products"]]
                if all(p["path"].exists() for p in products):
                    progress(f"Reading {len(products)} cached {mission} file(s)", 0.1)
                    return products
                log.info("%s: cached %s files missing; fetching again", query, mission)

        products = self._download(index, query, mission, progress)
        index.put(key, {
            "query": query,
            "mission": mission,
            "products": [{k: v for k, v in p.items() if k != "path"} for p in products],
        })
        return products

    def _negative_fresh(self, index: CacheIndex, key: str) -> bool:
        return index.get(key, max_age_days=NEGATIVE_CACHE_DAYS) is not None

    def _download(self, index: CacheIndex, query: str, mission: Mission,
                  progress: ProgressCallback) -> list[dict]:
        lk = _lightkurve()
        download_dir = str(cache_dir())
        with _network_lock:
            _ensure_user_agent()
            _ensure_timeouts()
            progress(f"Searching MAST for {mission} light curves of {query}", 0.0)

            def search():
                if mission == "TESS":
                    return lk.search_lightcurve(query, mission="TESS", author=TESS_AUTHORS)
                return lk.search_lightcurve(query, mission="Kepler", author=KEPLER_AUTHOR,
                                            exptime="long")

            try:
                sr = _with_retry(f"MAST search for {query!r}", search,
                                 lambda msg: progress(msg, 0.0))
            except Exception as exc:
                if type(exc).__name__ == "SearchError" and "resolve" in str(exc).lower():
                    # An unknown name: the same as finding no data.
                    log.info("MAST could not resolve %r: %s", query, exc)
                    return []
                log.exception("MAST search for %r (%s) failed", query, mission)
                raise _failure(f"MAST search for {query!r}", exc) from exc

            rows = select_products(sr.table, mission)
            if self.max_products is not None:
                rows = rows[: self.max_products]
            products = []
            for n, row in enumerate(rows):
                tab = sr.table
                label = str(tab["mission"][row])
                progress(f"Downloading {label} ({n + 1}/{len(rows)})", 0.1 + 0.8 * n / max(len(rows), 1))
                try:
                    lc = _with_retry(
                        f"Download of {label} for {query!r}",
                        lambda r=row: sr[r].download(quality_bitmask="default",
                                                     download_dir=download_dir,
                                                     flux_column="pdcsap_flux"))
                except Exception as exc:
                    log.exception("Download of %s for %r failed", label, query)
                    raise _failure(f"Download of {label} for {query!r}", exc) from exc
                path = lc.meta.get("FILENAME") if lc is not None else None
                if not path:
                    raise SourceError(f"Download of {label} for {query!r} returned no file")
                rel = index.relative(path)
                products.append({
                    "file": rel,
                    "path": index.absolute(rel),
                    "author": str(tab["author"][row]),
                    "exptime": float(tab["exptime"][row]),
                    "seq": _seq_number(label),
                    "obs_id": str(tab["obs_id"][row]),
                    # Kepler quarters share one obs_id; the file name is unique.
                    "product": re.sub(r"\.fits(\.gz)?$", "", str(tab["productFilename"][row])),
                    "target_name": str(tab["target_name"][row]),
                })
        return products

    # --- reading and stitching ---------------------------------------------------------

    def _read(self, path) -> tuple[Any, str]:
        lk = _lightkurve()
        lc = lk.read(str(path), flux_column="pdcsap_flux", quality_bitmask="default")
        if not np.isfinite(_values(lc.flux)).any():
            log.info("%s has no PDCSAP flux; using SAP", path)
            return lk.read(str(path), flux_column="sap_flux", quality_bitmask="default"), "SAP"
        return lc, "PDCSAP"

    def _stitch(self, products: Iterable[dict], mission: Mission) -> LightCurve:
        times, fluxes, errs = [], [], []
        seqs, ids, authors, exptimes, kinds = set(), [], [], [], set()
        doi_seqs: dict[str, set[int]] = {}
        for p in products:
            try:
                lc, kind = self._read(p["path"])
            except Exception as exc:
                log.exception("Could not read %s", p["path"])
                raise SourceError(f"Could not read cached file {p['path']}: {exc}") from exc
            t, f, e = _values(lc.time), _values(lc.flux), _values(lc.flux_err)
            good = np.isfinite(t) & np.isfinite(f)
            if not good.any():
                log.warning("%s has no usable flux; skipped", p["path"])
                continue
            med = float(np.median(f[good]))
            if not med > 0:
                log.warning("%s has non-positive median flux; skipped", p["path"])
                continue
            times.append(t[good])
            fluxes.append(f[good] / med)
            errs.append(e[good] / med)
            kinds.add(kind)
            if p.get("seq") is not None:
                seqs.add(int(p["seq"]))
                if p.get("author") in DOIS:
                    doi_seqs.setdefault(DOIS[p["author"]], set()).add(int(p["seq"]))
            ids.append(p.get("product") or p.get("obs_id", ""))
            authors.append(p.get("author", ""))
            exptimes.append(float(p.get("exptime", np.nan)))
        if not times:
            raise SourceError(f"The {mission} files found have no usable flux")

        t = np.concatenate(times)
        order = np.argsort(t, kind="stable")
        uniq_authors = list(dict.fromkeys(a for a in authors if a))
        author = "+".join(uniq_authors)
        if kinds == {"SAP"}:
            author += " (SAP)"
        elif "SAP" in kinds:
            author += " (PDCSAP; SAP for some)"
        return LightCurve(
            time=t[order],
            flux=np.concatenate(fluxes)[order],
            flux_err=np.concatenate(errs)[order],
            mission=mission,
            sectors_or_quarters=sorted(seqs),
            source_ids=list(dict.fromkeys(i for i in ids if i)),
            dois=[DOIS[a] for a in uniq_authors if a in DOIS],
            author=author,
            cadence_s=float(np.nanmedian(exptimes)) if exptimes else None,
            doi_sectors={doi: sorted(s) for doi, s in doi_seqs.items()} if len(doi_seqs) > 1 else {},
        )
