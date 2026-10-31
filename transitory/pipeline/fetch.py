"""``MastSource``: real TESS, Kepler and K2 light curves from MAST.

Products chosen, per sector, quarter or campaign:

* TESS: SPOC 2-minute light curves, else TESS-SPOC full-frame-image light
  curves, else SPOC 20-second ones.
* Kepler: long-cadence (30-minute) light curves from the Kepler pipeline.
* K2: long-cadence light curves from the K2 pipeline; campaigns 9-11, which
  K2 observed in two halves, give one file per half.

PDCSAP flux is used, falling back to SAP for a file with no PDCSAP values.
Quality-flagged cadences are dropped on reading (lightkurve's default
bitmask) and each file is normalised by its median before stitching.

Where the files come from, tried in this order (``$TRANSITORY_LC_SOURCES``,
such as ``static,s3,search``, overrides it):

1. ``static``: STScI's static archive directories, where a star's files are
   found by path from its KIC, EPIC or TIC (``archive_files``); for TESS,
   the sectors whose images cover the star come from MAST's footprint file,
   read once a run (``tess_sectors``), else from TESScut.
2. ``s3``: the same files on the AWS Open Data mirror (``stpubdata``), which
   has no TESS-SPOC light curves.
3. ``search``: lightkurve's search of MAST's CAOM database, for stars known
   only by name or position, or when neither mirror can be reached.

The answer of a source that could be asked is believed: a mirror with no
files for the star (STScI's, or S3's for Kepler and K2) means no data, and
the next source is not asked.  A source that cannot be asked (timeout, no
connection, HTTP 5xx or 429) is skipped for the next, and held off for
other stars until the next run starts (``MastSource.new_run``) or
``MAST_OUTAGE_HOLD_SECONDS`` pass, so they don't wait on it too.  A star
fails only when no source can be used, with a message naming each source
and why, which says it is not a lack of data.

Being polite to the archive: files go to ``config.cache_dir()`` (from the
mirrors, ``archive/<mission>/<filename>``) and the query -> files mapping to
the cache index (``pipeline/cache.py``), whichever source gave them, so
nothing is downloaded twice and a repeated fetch makes no request at all.
Requests are serial across all threads and carry a descriptive user agent.

Timeouts are set so a source that is down fails fast:

* Mirror listings get ``archive_files.LISTING_TIMEOUT_SECONDS`` and no
  retry: the next source is the retry.
* Downloads get ``MAST_TIMEOUT_SECONDS`` between bytes and are tried once
  more after ``MAST_RETRY_BACKOFF_SECONDS``.
* astroquery sends its requests with no timeout, so each gets
  ``MAST_TIMEOUT_SECONDS``, and a search MAST keeps answering "still
  executing" is given up after ``MAST_PORTAL_TIMEOUT_SECONDS``.  A search
  that times out or cannot connect is tried once more, except as a
  fallback after the mirrors failed, when it gets
  ``MAST_FALLBACK_SEARCH_SECONDS`` and one try.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
import time
import warnings
from collections.abc import Callable, Iterable
from typing import Any

import numpy as np
import requests
from requests.adapters import HTTPAdapter

from ..catalog.client import CatalogError
from ..config import USER_AGENT, cache_dir
from ..models import LightCurve, Mission, ProgressCallback, StarTarget
from ..sources import SourceError
from . import archive_files, tess_sectors
from .archive_files import SourceUnavailable
from .cache import CacheIndex, cache_key

log = logging.getLogger(__name__)

TESS_AUTHORS = ("SPOC", "TESS-SPOC")
KEPLER_AUTHOR = "Kepler"
K2_AUTHOR = "K2"
KEPLER_LONG_CADENCE_S = 1800.0  # Kepler's and K2's

# Mission data DOIs at MAST, by pipeline, with their landing-page titles
# (checked at doi.org, 2026-10-29).  MAST asks for each as a doi.org URL.
DOIS = {
    "SPOC": "10.17909/t9-nmc8-f686",
    "TESS-SPOC": "10.17909/t9-wpz1-8s54",
    "Kepler": "10.17909/T9488N",
    # From https://archive.stsci.edu/publishing/doi; title from its landing page (2026-10-30).
    "K2": "10.17909/T9WS3R",
}
DOI_TITLES = {
    "10.17909/t9-nmc8-f686": "TESS Light Curves - All Sectors",
    "10.17909/t9-wpz1-8s54": 'TESS Light Curves From Full Frame Images ("TESS-SPOC")',
    "10.17909/T9488N": "Kepler LC, Q0-Q17",
    "10.17909/T9WS3R": "K2 Light Curves (all)",
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
# The page gives one acknowledgement for "Kepler and K2 mission data products"
# (checked 2026-10-30): K2 data is credited with the Kepler text.
MAST_CREDITS["K2"] = MAST_CREDITS["Kepler"]
# Lightkurve's own recommended acknowledgement ("Citing Lightkurve").
LIGHTKURVE_CREDIT = ("This research made use of Lightkurve, a Python package for Kepler and TESS "
                     "data analysis (Lightkurve Collaboration, 2018).")


# Where a light curve's files came from (``LightCurve.source``), for its credits.
RETRIEVED_FROM = {
    "STScI archive files": "STScI archive files (https://archive.stsci.edu/missions/)",
    "AWS Open Data (stpubdata)": "AWS Open Data (s3://stpubdata)",
    "MAST search": "the MAST search (https://mast.stsci.edu)",
}
RETRIEVAL_NOTE = (
    "Light curves are retrieved from STScI archive files (https://archive.stsci.edu/missions/), "
    "else from AWS Open Data (s3://stpubdata), else found with the MAST search "
    "(https://mast.stsci.edu). They are the same MAST products, with the same DOIs and "
    "acknowledgements, from whichever copy; each star's credits name the one used and the date.")
# The Registry of Open Data on AWS asks to be cited as "<name> was accessed on
# DATE from https://registry.opendata.aws/<slug>" (each dataset's "How to Cite",
# checked 2026-10-30).
AWS_DATASETS = {
    "TESS": ("Transiting Exoplanet Survey Satellite (TESS)", "mast-tess"),
    "Kepler": ("Kepler Mission Data", "mast-kepler"),
    "K2": ("K2 Mission Data", "mast-k2"),
}


def doi_line(doi: str, mission: str = "") -> str:
    """``title: https://doi.org/...`` for a mission data DOI."""
    title = DOI_TITLES.get(doi, f"{mission} data".strip())
    return f"{title}: https://doi.org/{doi}"

# Where light curves come from, in the order tried (see the module docstring).
LC_SOURCES_ENV = "TRANSITORY_LC_SOURCES"
DEFAULT_LC_SOURCES = ("static", "s3", "search")
SOURCE_TITLES = {
    "static": archive_files.MIRROR_TITLES["stsci"],
    "s3": archive_files.MIRROR_TITLES["s3"],
    "search": "MAST search",
}
_MIRROR_FOR = {"static": "stsci", "s3": "s3"}
# A product record's ``source``: the mirror it was listed on (``archive_files``), or the search.
_RECORD_SOURCE_TITLES = {**archive_files.MIRROR_TITLES, "search": SOURCE_TITLES["search"]}


def lc_sources() -> list[str]:
    """The light-curve sources to try, in order: ``$TRANSITORY_LC_SOURCES``, else the default."""
    names = [n.strip().lower() for n in os.environ.get(LC_SOURCES_ENV, "").split(",") if n.strip()]
    unknown = [n for n in names if n not in SOURCE_TITLES]
    if unknown:
        log.warning("$%s: ignoring unknown source(s) %s; known: %s", LC_SOURCES_ENV,
                    ", ".join(unknown), ", ".join(SOURCE_TITLES))
    names = list(dict.fromkeys(n for n in names if n in SOURCE_TITLES))
    return names or list(DEFAULT_LC_SOURCES)


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
# A search tried after the mirrors failed gets this long, and one try.
MAST_FALLBACK_SEARCH_SECONDS = 60.0
# A failed search or download is tried once more after this pause.
MAST_RETRY_BACKOFF_SECONDS = 10.0
# After a request finds a source down, other stars skip it without asking,
# until the next run starts (``MastSource.new_run``) or this many seconds pass.
MAST_OUTAGE_HOLD_SECONDS = 600.0


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
    _limits["adapter"] = adapter
    if getattr(mast_utils, "requests", None) is requests:
        mast_utils.requests = _limits["resolver"] = _TimeoutRequests(MAST_TIMEOUT_SECONDS)
    else:
        log.warning("MAST name resolution keeps astroquery's default (no) timeout")
    portal = getattr(Observations, "_portal_api_connection", None)
    if hasattr(portal, "TIMEOUT"):
        portal.TIMEOUT = MAST_PORTAL_TIMEOUT_SECONDS
        _limits["portal"] = portal
    else:
        log.warning("MAST searches keep astroquery's default polling limit")
    _timeouts_set = True


# What _ensure_timeouts set, by name, for _search_budget to shorten for a while.
_limits: dict[str, Any] = {}


@contextlib.contextmanager
def _search_budget(seconds: float | None):
    """Within the block, MAST requests and search polling are limited to ``seconds``.

    ``None`` leaves the limits as they are.  Callers hold ``_network_lock``.
    """
    if seconds is None:
        yield
        return
    saved = []
    for name, attribute in (("adapter", "timeout"), ("resolver", "timeout"), ("portal", "TIMEOUT")):
        holder = _limits.get(name)
        if holder is not None:
            saved.append((holder, attribute, getattr(holder, attribute)))
            setattr(holder, attribute, seconds)
    try:
        yield
    finally:
        for holder, attribute, value in saved:
            setattr(holder, attribute, value)


def _lookup_transport(method: str, url: str, params: dict[str, str]) -> str:
    """``tess_sectors``' transport: one GET with the listing timeout and no retry.

    TESScut is the footprint file's fallback, so is its retry.
    """
    try:
        reply = requests.request(method, url, params=params, headers={"User-Agent": USER_AGENT},
                                 timeout=archive_files.LISTING_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise CatalogError(f"{url}: {exc}") from exc
    if reply.status_code != 200:
        raise CatalogError(f"{url}: HTTP {reply.status_code}")
    return reply.text


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


def _with_retry(what: str, call, on_retry=None, retry: bool = True):
    """``call()``, tried once more after a pause if MAST timed out or was unreachable.

    ``on_retry(message)`` is told before the pause.  With ``retry`` False, just ``call()``.
    """
    if not retry:
        return call()
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


def _outage(exc: Exception) -> str | None:
    """What ``exc`` says about MAST as a whole, when it says MAST is unusable for now."""
    status = _http_status(exc)
    if status == 429:
        return "MAST is limiting requests from this machine (HTTP 429)"
    if _is_transient(exc):
        return f"MAST is having problems (HTTP {status})" if status else \
            "MAST is down or cannot be reached from this network"
    if isinstance(exc, TimeoutError):
        return "MAST is overloaded or down"
    return None


def _failure(what: str, exc: Exception, tries: int = 2) -> SourceError:
    problem = _outage(exc)
    if problem is not None:
        if _http_status(exc) == 429:
            happened = "was refused"
        elif _is_transient(exc):
            happened = "timed out or could not connect" + (" (tried twice)" if tries == 2 else "")
        else:
            happened = "timed out"
        return SourceError(f"{problem}: {what} {happened}. {_NOT_MISSING_DATA} ({exc})")
    if type(exc).__name__ == "RemoteServiceError":
        # astroquery's: MAST answered that the search failed on its side.
        return SourceError(f"MAST reported an error on its side: {what} failed. "
                           f"{_NOT_MISSING_DATA} ({exc})")
    return SourceError(f"{what} failed: {exc}")


def _all_failed(mission: Mission, query: str, skipped: list[tuple[str, _Skip]]) -> SourceError:
    """The error for a star no source could answer for: each one tried, and why."""
    tried = [(name, skip) for name, skip in skipped if skip.tried]
    if len(tried) == 1:
        return tried[0][1].error
    if not tried:
        reasons = "; ".join(f"{SOURCE_TITLES[name]} {skip.reason}" for name, skip in skipped)
        return SourceError(f"No source can look for {mission} light curves of {query!r}: {reasons}")
    reasons = "; ".join(f"{SOURCE_TITLES[name]}: {skip.reason}" for name, skip in tried)
    return SourceError(f"No source could be used for {mission} light curves of {query!r}: "
                       f"{reasons}. {_NOT_MISSING_DATA}")


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

    MAST asks for the sectors used next to a TESS DOI; quarters and
    campaigns are given for Kepler and K2 in the same way.
    """
    if lc.mission not in MAST_CREDITS:
        return []
    lines = [MAST_CREDITS[lc.mission]]
    for doi in lc.dois:
        sectors = lc.sectors_for(doi)
        used = f" ({sector_unit(lc.mission)} {number_ranges(sectors)})" if sectors else ""
        lines.append(doi_line(doi, lc.mission) + used)
    lines += retrieval_credits(lc)
    lines.append(LIGHTKURVE_CREDIT)
    return lines


def retrieval_credits(lc: LightCurve) -> list[str]:
    """Where ``lc``'s files were retrieved from, and when; for AWS, the citation it asks for.

    The products, DOIs and acknowledgements are MAST's whichever source gave
    them; these lines say which copy was used.
    """
    sources = [s for s in lc.source.split(" + ") if s]
    if not sources:
        return []
    when = f" on {lc.retrieved}" if lc.retrieved else ""
    where = " and ".join(RETRIEVED_FROM.get(s, s) for s in sources)
    lines = [f"Light curves retrieved{when} from {where}."]
    if SOURCE_TITLES["s3"] in sources and lc.mission in AWS_DATASETS:
        lines.append(aws_citation(lc.mission, lc.retrieved or "DATE"))
    return lines


def aws_citation(mission: str, date: str) -> str:
    """The Registry of Open Data's "How to Cite" line for a mission's files on AWS."""
    name, slug = AWS_DATASETS[mission]
    return f"{name} was accessed on {date} from https://registry.opendata.aws/{slug}."


def sector_unit(mission: str) -> str:
    """What a mission's observing periods are called: TESS sectors, Kepler quarters, K2 campaigns."""
    return {"TESS": "sectors", "K2": "campaigns"}.get(mission, "quarters")


def _mission_order(target: StarTarget) -> list[Mission]:
    if target.mission_hint == "TESS":
        return ["TESS"]
    if target.mission_hint in ("Kepler", "K2"):
        return [target.mission_hint]
    # A KIC number means the star was in the Kepler field, whose 4-year
    # light curves beat TESS for small planets; otherwise TESS covers more
    # sky, and K2 (along the ecliptic, never in the Kepler field) the rest.
    return ["Kepler", "TESS"] if target.kic is not None else ["TESS", "K2"]


def _query_for(target: StarTarget, mission: Mission) -> str:
    if mission == "TESS" and target.tic is not None:
        return f"TIC {target.tic}"
    if mission == "Kepler" and target.kic is not None:
        return f"KIC {target.kic}"
    if mission == "K2" and target.epic is not None:
        return f"EPIC {target.epic}"
    if target.name.strip():
        return target.name.strip()
    if target.ra is not None and target.dec is not None:
        return f"{target.ra:.6f} {target.dec:.6f}"
    raise SourceError(f"No identifier, name or coordinates to search {mission} data for")


def _seq_number(label: str) -> int | None:
    """Sector / quarter / campaign number from a SearchResult 'mission' label like 'TESS Sector 14'.

    The two halves of K2 campaigns 9-11 ('K2 Campaign 10a') give the campaign's number.
    """
    m = re.search(r"(\d+)\s*[ab]?\s*$", str(label))
    return int(m.group(1)) if m else None


def _seq_key(label: str) -> tuple[int, str] | None:
    """``(number, half)`` from a 'mission' label: ``(10, "a")`` for 'K2 Campaign 10a', else half ``""``."""
    m = re.search(r"(\d+)\s*([ab]?)\s*$", str(label))
    return (int(m.group(1)), m.group(2)) if m else None


def _tess_rank(author: str, exptime: float) -> int | None:
    if author == "SPOC" and round(exptime) == 120:
        return 0
    if author == "TESS-SPOC":
        return 1
    if author == "SPOC":
        return 2
    return None


def select_products(table: Any, mission: Mission) -> list[int]:
    """Row indices of the products to use: one per sector, quarter or campaign half, in order.

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

    best: dict[tuple[int, str], tuple[int, int]] = {}  # (seq, half) -> (rank, row)
    for i in range(n):
        if names[i] != nearest:
            continue
        author = str(table["author"][i])
        exptime = float(table["exptime"][i])
        seq = _seq_key(table["mission"][i])
        if seq is None:
            continue
        if mission == "TESS":
            rank = _tess_rank(author, exptime)
        else:
            pipeline = K2_AUTHOR if mission == "K2" else KEPLER_AUTHOR
            rank = 0 if author == pipeline and round(exptime) == KEPLER_LONG_CADENCE_S else None
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


class _Skip(Exception):
    """A source gave no answer for a star: ``reason`` in a few words, ``error`` in full.

    ``tried`` is False for a source that does not apply to the star (no
    catalog number or position to find its files by), which no error names.
    """

    def __init__(self, reason: str, error: SourceError | None = None, tried: bool = True):
        super().__init__(reason)
        self.reason = reason
        self.error = error or SourceError(reason)
        self.tried = tried


class MastSource:
    """TESS / Kepler / K2 light curves from MAST, cached in ``cache_dir()``.

    ``max_products`` caps how many sectors or quarters are used (the most
    recent ones are dropped first), for stars with very long coverage.
    ``transport``, ``get`` and ``sectors_for`` stand in for the network in
    tests: the mirrors' listing transport (``archive_files.Transport``),
    ``requests.get`` for downloads, and ``(ra, dec) -> sectors``.
    """

    name = "MAST"

    def __init__(self, index: CacheIndex | None = None, max_products: int | None = None, *,
                 transport: archive_files.Transport | None = None, get=None,
                 sectors_for: Callable[[float, float], list[int]] | None = None):
        self._index = index
        self.max_products = max_products
        self._transport = transport
        self._get = get
        self._sectors_for = sectors_for
        # source -> (time.monotonic(), message) of the request that found it unusable this run.
        self._outages: dict[str, tuple[float, str]] = {}
        # The TESS footprints, read once a run, or the SectorLookupError that reading them gave.
        self._footprints: tess_sectors.Footprints | Exception | None = None
        self._sectors: dict[tuple[float, float], list[int]] = {}

    def new_run(self) -> None:
        """A new batch of stars starts: ask every source again, even one that was down."""
        self._outages.clear()
        self._footprints = None
        self._sectors.clear()

    def _held(self, source: str) -> str | None:
        """Why ``source`` is not to be asked now, if a request this run found it unusable."""
        outage = self._outages.get(source)
        if outage is None:
            return None
        when, message = outage
        if time.monotonic() - when > MAST_OUTAGE_HOLD_SECONDS:
            del self._outages[source]
            return None
        return message

    def _hold(self, source: str, message: str) -> None:
        self._outages[source] = (time.monotonic(), message)

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
            products = self._products(target, query, mission,
                                      lambda msg, f: progress(msg, base + span * f))
            if products:
                lc = self._stitch(products, mission)
                progress(f"{mission} light curve ready (from {lc.source})", 1.0)
                return lc
        raise SourceError(f"No light curves at MAST for {target.label}: searched {', '.join(tried)}")

    # --- products: from the index, else the first source that answers -----------------

    def _products(self, target: StarTarget, query: str, mission: Mission,
                  progress: ProgressCallback) -> list[dict]:
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
                products = [dict(p, path=index.absolute(p["file"]), retrieved=entry["created"][:10])
                            for p in entry["products"]]
                if all(p["path"].exists() for p in products):
                    progress(f"Reading {len(products)} cached {mission} file(s)", 0.1)
                    return products
                log.info("%s: cached %s files missing; fetching again", query, mission)

        products = self._download(index, target, query, mission, progress)
        stored = index.put(key, {
            "query": query,
            "mission": mission,
            "products": [{k: v for k, v in p.items() if k != "path"} for p in products],
        })
        return [dict(p, retrieved=stored["created"][:10]) for p in products]

    def _negative_fresh(self, index: CacheIndex, key: str) -> bool:
        return index.get(key, max_age_days=NEGATIVE_CACHE_DAYS) is not None

    def _download(self, index: CacheIndex, target: StarTarget, query: str, mission: Mission,
                  progress: ProgressCallback) -> list[dict]:
        """Products from the first source in ``lc_sources()`` that can answer for the star."""
        skipped: list[tuple[str, _Skip]] = []
        with _network_lock:
            for name in lc_sources():
                title = SOURCE_TITLES[name]
                unusable = [SOURCE_TITLES[n] for n, s in skipped if s.tried]
                if unusable:
                    progress(f"{' and '.join(unusable)} unavailable; trying {title}", 0.0)
                try:
                    # Checked once the lock is ours: stars queued behind the request that
                    # found a source down skip it as soon as it does.
                    held = self._held(name)
                    if held is not None:
                        raise self._held_skip(name, held, query)
                    if name == "search":
                        return self._search(index, query, mission, progress, fallback=bool(unusable))
                    return self._from_mirror(index, target, query, mission, name, progress)
                except _Skip as skip:
                    log.info("%s for %s light curves of %s: %s", title, mission, query, skip.reason)
                    skipped.append((name, skip))
        raise _all_failed(mission, query, skipped)

    def _held_skip(self, source: str, held: str, query: str) -> _Skip:
        if source == "search":
            error = SourceError(f"{held}: MAST was not asked for {query!r}, as an earlier search "
                                f"in this run found it unusable. {_NOT_MISSING_DATA}")
        else:
            error = SourceError(f"{held}: it was not asked for {query!r}, as it could not be "
                                f"reached earlier in this run. {_NOT_MISSING_DATA}")
        return _Skip(f"{held} (earlier in this run)", error)

    # --- the mirrors: files found by path ----------------------------------------------

    def _from_mirror(self, index: CacheIndex, target: StarTarget, query: str, mission: Mission,
                     source: str, progress: ProgressCallback) -> list[dict]:
        mirror = archive_files.MIRRORS[_MIRROR_FOR[source]]
        title = SOURCE_TITLES[source]
        number, kind = {"TESS": (target.tic, "TIC"), "Kepler": (target.kic, "KIC"),
                        "K2": (target.epic, "EPIC")}[mission]
        if number is None:
            raise _Skip(f"needs the star's {kind} number", tried=False)
        sectors = None
        if mission == "TESS":
            sectors = self._tess_sectors(target)
            if not sectors:
                log.info("%s: no TESS sector covers it", query)
                return []
        progress(f"Looking for {mission} light curves of {query} in {title}", 0.0)
        try:
            records = archive_files.find_products(mission, number, mirror=mirror, sectors=sectors,
                                                  transport=self._transport)
        except SourceUnavailable as exc:
            log.warning("%s: %s", title, exc)
            problem = f"{title} could not be reached"
            self._hold(source, problem)
            raise _Skip(f"could not be reached ({exc})",
                        SourceError(f"{problem}: {exc}. {_NOT_MISSING_DATA}")) from exc
        if not records and mission == "TESS" and not mirror.has_tess_spoc:
            raise _Skip("has no SPOC light curve for the star and does not hold TESS-SPOC ones",
                        SourceError(f"{title} has no SPOC light curves for {query!r} and does not "
                                    f"hold TESS-SPOC ones, so cannot say whether the star has any. "
                                    f"{_NOT_MISSING_DATA}"))
        if self.max_products is not None:
            records = records[: self.max_products]
        products = []
        for n, record in enumerate(records):
            path = index.absolute(record["file"])
            if not path.exists():  # another source may have fetched the same file already
                what = f"Download of {record['label']} from {title}"
                progress(f"Downloading {record['label']} ({n + 1}/{len(records)}) from {title}",
                         0.1 + 0.8 * n / len(records))
                self._download_file(record["url"], path, what, source, progress)
            products.append(dict(record, path=path))
        return products

    def _download_file(self, url: str, path, what: str, source: str,
                       progress: ProgressCallback) -> None:
        """Download ``url`` to ``path``, once more after a pause if the mirror could not be reached."""
        for attempt in (1, 2):
            try:
                archive_files.download(url, path, get=self._get)
                return
            except SourceUnavailable as exc:
                if attempt == 2:
                    self._hold(source, f"{SOURCE_TITLES[source]} could not be reached")
                    raise _Skip(f"a download failed twice ({exc})",
                                SourceError(f"{what} failed twice: {exc}. {_NOT_MISSING_DATA}")) from exc
                log.warning("%s failed (%s); trying again in %.0f s", what, exc,
                            MAST_RETRY_BACKOFF_SECONDS)
                progress(f"{what} failed; trying again", 0.1)
                time.sleep(MAST_RETRY_BACKOFF_SECONDS)
            except SourceError as exc:  # a listed file that is not there
                raise _Skip(f"listed a file that is not there ({exc})",
                            SourceError(f"{what} failed: {exc}")) from exc

    def _tess_sectors(self, target: StarTarget) -> list[int]:
        """The TESS sectors whose images cover ``target``, looked up once a run."""
        if target.ra is None or target.dec is None:
            raise _Skip("needs the star's position to find its TESS sectors", tried=False)
        key = (target.ra, target.dec)
        if key not in self._sectors:
            lookup = self._sectors_for or self._footprint_sectors
            try:
                self._sectors[key] = lookup(target.ra, target.dec)
            except CatalogError as exc:
                raise _Skip(f"could not find the star's TESS sectors ({exc})",
                            SourceError(f"The TESS sectors of {target.label} could not be looked "
                                        f"up: {exc}. {_NOT_MISSING_DATA}")) from exc
        return self._sectors[key]

    def _footprint_sectors(self, ra: float, dec: float) -> list[int]:
        """Sectors from the footprint file, read once a run, else from TESScut."""
        footprints = self._footprints
        if footprints is None:
            try:
                footprints = tess_sectors.footprints(transport=_lookup_transport)
            except tess_sectors.SectorLookupError as exc:
                log.warning("%s; asking TESScut for each star's sectors this run", exc)
                footprints = exc
            self._footprints = footprints
        if isinstance(footprints, tess_sectors.Footprints):
            return footprints.sectors_at(ra, dec)
        return tess_sectors.tesscut_sectors(ra, dec, transport=_lookup_transport)

    # --- the MAST search, via lightkurve -------------------------------------------------

    def _search(self, index: CacheIndex, query: str, mission: Mission,
                progress: ProgressCallback, fallback: bool) -> list[dict]:
        """Products from lightkurve's search; ``fallback``: the mirrors failed, so fail fast."""
        lk = _lightkurve()
        download_dir = str(cache_dir())
        _ensure_user_agent()
        _ensure_timeouts()
        progress(f"Searching MAST for {mission} light curves of {query}", 0.0)

        def search():
            if mission == "TESS":
                return lk.search_lightcurve(query, mission="TESS", author=TESS_AUTHORS)
            if mission == "K2":
                return lk.search_lightcurve(query, mission="K2", author=K2_AUTHOR, exptime="long")
            return lk.search_lightcurve(query, mission="Kepler", author=KEPLER_AUTHOR,
                                        exptime="long")

        what = f"MAST search for {query!r}"
        try:
            with _search_budget(MAST_FALLBACK_SEARCH_SECONDS if fallback else None):
                sr = _with_retry(what, search, lambda msg: progress(msg, 0.0), retry=not fallback)
        except Exception as exc:
            if type(exc).__name__ == "SearchError" and "resolve" in str(exc).lower():
                # An unknown name: the same as finding no data.
                log.info("MAST could not resolve %r: %s", query, exc)
                return []
            log.exception("MAST search for %r (%s) failed", query, mission)
            problem = _outage(exc)
            if problem is not None:
                self._hold("search", problem)
            raise _Skip(problem or f"failed: {exc}",
                        _failure(what, exc, tries=1 if fallback else 2)) from exc

        rows = select_products(sr.table, mission)
        if self.max_products is not None:
            rows = rows[: self.max_products]
        tab = sr.table
        products = []
        for n, row in enumerate(rows):
            label = str(tab["mission"][row])
            progress(f"Downloading {label} ({n + 1}/{len(rows)})", 0.1 + 0.8 * n / max(len(rows), 1))
            what = f"Download of {label} for {query!r}"
            try:
                lc = _with_retry(what, lambda r=row: sr[r].download(quality_bitmask="default",
                                                                    download_dir=download_dir,
                                                                    flux_column="pdcsap_flux"))
            except Exception as exc:
                log.exception("Download of %s for %r failed", label, query)
                raise _Skip(_outage(exc) or f"a download failed: {exc}", _failure(what, exc)) from exc
            path = lc.meta.get("FILENAME") if lc is not None else None
            if not path:
                raise SourceError(f"Download of {label} for {query!r} returned no file")
            rel = index.relative(path)
            products.append({
                "file": rel,
                "path": index.absolute(rel),
                "url": str(tab["dataURI"][row]) if "dataURI" in tab.colnames else "",
                "source": "search",
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
        products = list(products)
        times, fluxes, errs = [], [], []
        seqs, ids, authors, exptimes, kinds = set(), [], [], [], set()
        sources = []
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
            # Records cached before there were other sources came from the search.
            source = p.get("source") or "search"
            sources.append(_RECORD_SOURCE_TITLES.get(source, source))
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
            source=" + ".join(dict.fromkeys(sources)),
            retrieved=min((p["retrieved"] for p in products if p.get("retrieved")), default=""),
        )
