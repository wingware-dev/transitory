"""HTTP access to MAST and the NASA Exoplanet Archive, cached on disk.

Every catalog request goes through :class:`CatalogClient`, which

- sends requests one at a time (a lock serialises worker threads) with the
  project's descriptive user agent,
- caches each parsed response as JSON under ``cache_dir()/catalog`` together
  with the date it was retrieved, so nothing is fetched twice,
- delegates the actual HTTP to a *transport*, a callable
  ``transport(method, url, params) -> str``.  Tests pass a
  :class:`RecordedTransport` that replays recorded responses, so they never
  touch the network.

Two services are used: the Exoplanet Archive TAP endpoint (ADQL queries
returning JSON rows) and the MAST Mashup API (TIC queries, TIC cone search
and name resolution through SIMBAD / NED / the K2 EPIC).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import USER_AGENT, cache_dir

log = logging.getLogger(__name__)

TAP_URL = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"
MAST_URL = "https://mast.stsci.edu/api/v0/invoke"

Transport = Callable[[str, str, dict[str, str]], str]

# Archive tables gain planets and revised parameters over time; the TIC and
# name lookups essentially never change.
ARCHIVE_MAX_AGE = timedelta(days=30)
MAST_MAX_AGE: timedelta | None = None

# MAST answers EXECUTING while a query is still running; ask again this often.
MAST_RETRY_SECONDS = 2.0
MAST_MAX_WAIT_SECONDS = 60.0


class CatalogError(RuntimeError):
    """A catalog service could not answer (offline, HTTP error, query error)."""


@dataclass
class Response:
    """A parsed service response and when it was fetched from the service."""

    data: Any
    retrieved: datetime  # UTC
    from_cache: bool = False

    @property
    def retrieved_date(self) -> str:
        """Retrieval date as YYYY-MM-DD, for credits and reports."""
        return self.retrieved.date().isoformat()


def request_key(method: str, url: str, params: dict[str, str]) -> str:
    """A stable identifier for one request, used for the cache and recordings."""
    blob = json.dumps([method.upper(), url, params], sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def requests_transport(method: str, url: str, params: dict[str, str], timeout: float = 60.0) -> str:
    """The real transport: one HTTP request with the project user agent."""
    import requests

    headers = {"User-Agent": USER_AGENT}
    try:
        if method.upper() == "POST":
            reply = requests.post(url, data=params, headers=headers, timeout=timeout)
        else:
            reply = requests.get(url, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise CatalogError(f"{url}: {exc}") from exc
    if reply.status_code >= 400:
        # The archive explains a bad query in a VOTable body; MAST in JSON.
        detail = reply.text.strip()
        detail = _votable_error(detail) if detail.startswith("<") else detail[:300]
        raise CatalogError(f"{url}: HTTP {reply.status_code}: {detail or reply.reason}")
    return reply.text


class RecordedTransport:
    """Replays responses recorded to a JSON file; for tests, no network.

    The file maps :func:`request_key` to ``{"method", "url", "params",
    "body"}``.  A request that was not recorded raises ``CatalogError``, as
    an unreachable service would, and is listed in ``missing``.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.recordings: dict[str, dict] = json.loads(self.path.read_text(encoding="utf-8"))
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.missing: list[tuple[str, str, dict[str, str]]] = []

    def __call__(self, method: str, url: str, params: dict[str, str]) -> str:
        self.calls.append((method, url, params))
        entry = self.recordings.get(request_key(method, url, params))
        if entry is None:
            self.missing.append((method, url, params))
            raise CatalogError(f"no recorded response for {method} {url} {params}")
        return entry["body"]


class RecordingTransport:
    """Wraps a transport and keeps each response, to write test recordings.

    ``trim`` may shrink a body before it is stored (for example to drop TIC
    columns the code never reads).
    """

    def __init__(self, inner: Transport = requests_transport,
                 trim: Callable[[str, dict[str, str], str], str] | None = None):
        self.inner = inner
        self.trim = trim
        self.recordings: dict[str, dict] = {}

    def __call__(self, method: str, url: str, params: dict[str, str]) -> str:
        body = self.inner(method, url, params)
        stored = self.trim(url, params, body) if self.trim else body
        self.recordings[request_key(method, url, params)] = {
            "method": method, "url": url, "params": params, "body": stored}
        return stored

    def save(self, path: Path | str) -> None:
        Path(path).write_text(json.dumps(self.recordings, indent=1, sort_keys=True) + "\n",
                              encoding="utf-8")


class ResponseCache:
    """Parsed responses on disk, one JSON file per request.

    With no ``directory`` the cache follows ``cache_dir()`` at each call, so a
    changed ``$TRANSITORY_CACHE_DIR`` (as in tests) takes effect at once.
    """

    def __init__(self, directory: Path | str | None = None):
        self._directory = Path(directory) if directory is not None else None

    @property
    def directory(self) -> Path:
        path = self._directory if self._directory is not None else cache_dir() / "catalog"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str, max_age: timedelta | None = None) -> Response | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            retrieved = datetime.fromisoformat(entry["retrieved"])
            data = entry["data"]
        except (OSError, ValueError, KeyError) as exc:
            log.warning("ignoring unreadable catalog cache entry %s: %s", path, exc)
            return None
        if max_age is not None and datetime.now(UTC) - retrieved > max_age:
            return None
        return Response(data=data, retrieved=retrieved, from_cache=True)

    def put(self, key: str, request: dict, response: Response) -> None:
        entry = {"retrieved": response.retrieved.isoformat(), "request": request,
                 "data": response.data}
        path = self._path(key)
        tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        tmp.replace(path)


class CatalogClient:
    """Cached, serialised access to the Exoplanet Archive TAP and MAST APIs."""

    def __init__(self, transport: Transport | None = None, cache: ResponseCache | None = None,
                 offline: bool = False):
        self.transport = transport or requests_transport
        self.cache = cache or ResponseCache()
        self.offline = offline  # True: answer only from the cache
        self._lock = threading.Lock()

    # -- the two services ---------------------------------------------------

    def tap(self, adql: str, refresh: bool = False,
            max_age: timedelta | None = ARCHIVE_MAX_AGE) -> Response:
        """Run an ADQL query on the Exoplanet Archive; ``data`` is a list of row dicts."""
        params = {"query": " ".join(adql.split()), "format": "json"}
        return self._fetch("GET", TAP_URL, params, _parse_tap, refresh, max_age)

    def mast(self, request: dict, refresh: bool = False,
             max_age: timedelta | None = MAST_MAX_AGE) -> Response:
        """Invoke a MAST Mashup service; ``data`` is the decoded JSON reply."""
        params = {"request": json.dumps(request, sort_keys=True)}
        return self._fetch("POST", MAST_URL, params, _parse_mast, refresh, max_age)

    # -- plumbing ------------------------------------------------------------

    def _fetch(self, method: str, url: str, params: dict[str, str],
               parse: Callable[[str], tuple[Any, bool]], refresh: bool,
               max_age: timedelta | None) -> Response:
        key = request_key(method, url, params)
        if not refresh:
            cached = self.cache.get(key, max_age=None if self.offline else max_age)
            if cached is not None:
                return cached
        if self.offline:
            raise CatalogError(f"offline and not cached: {url} {params}")
        with self._lock:
            deadline = time.monotonic() + MAST_MAX_WAIT_SECONDS
            while True:
                body = self.transport(method, url, params)
                data, complete = parse(body)
                if complete:
                    break
                if time.monotonic() > deadline:
                    raise CatalogError(f"{url}: query still running after {MAST_MAX_WAIT_SECONDS:.0f} s")
                time.sleep(MAST_RETRY_SECONDS)
        response = Response(data=data, retrieved=datetime.now(UTC))
        self.cache.put(key, {"method": method, "url": url, "params": params}, response)
        return response


def _parse_tap(body: str) -> tuple[Any, bool]:
    text = body.lstrip()
    if text.startswith("<"):
        # The archive reports query errors as a VOTable even when JSON was asked for.
        raise CatalogError(f"Exoplanet Archive query failed: {_votable_error(text)}")
    try:
        rows = json.loads(text)
    except ValueError as exc:
        raise CatalogError(f"Exoplanet Archive returned invalid JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise CatalogError(f"Exoplanet Archive returned {type(rows).__name__}, expected rows")
    return rows, True


def _votable_error(text: str) -> str:
    match = re.search(r'<INFO name="QUERY_STATUS" value="ERROR">\s*(.*?)\s*</INFO>', text, re.S)
    return match.group(1) if match else text[:200]


def _parse_mast(body: str) -> tuple[Any, bool]:
    try:
        reply = json.loads(body)
    except ValueError as exc:
        raise CatalogError(f"MAST returned invalid JSON: {exc}") from exc
    status = str(reply.get("status", "")).upper()
    if status == "EXECUTING":
        return reply, False
    if status == "ERROR":
        raise CatalogError(f"MAST query failed: {reply.get('msg') or reply}")
    return reply, True


_default_client: CatalogClient | None = None
_default_lock = threading.Lock()


def default_client() -> CatalogClient:
    """The shared client over the real services (cache follows ``cache_dir()``)."""
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = CatalogClient()
        return _default_client


def adql_string(value: str) -> str:
    """``value`` as a quoted ADQL string literal."""
    return "'" + value.replace("'", "''") + "'"
