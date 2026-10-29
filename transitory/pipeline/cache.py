"""Cache index and keys for downloaded data in ``config.cache_dir()``.

lightkurve stores the FITS files themselves (under ``cache_dir()/mastDownload``);
this module records which files answer which query, so a repeated fetch reads
them straight from disk without asking MAST again.  The index is one JSON file,
written atomically, with paths relative to the cache directory so the cache can
be moved.  ``load_json`` / ``save_json`` give other modules (catalog lookups,
archive queries) the same keyed, dated storage for small results.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import cache_dir

log = logging.getLogger(__name__)

INDEX_NAME = "index.json"
INDEX_VERSION = 1

_lock = threading.Lock()


def cache_key(*parts: object) -> str:
    """A normalised key from ``parts``: case-folded, whitespace collapsed, joined by ':'.

    ``cache_key("lightcurve", "TESS", "TIC  261136679")`` is
    ``"lightcurve:tess:tic 261136679"``.
    """
    clean = [re.sub(r"\s+", " ", str(p).strip()).casefold() for p in parts]
    return ":".join(clean)


def key_filename(key: str, suffix: str = ".json") -> str:
    """A filesystem-safe file name for ``key``: a readable prefix plus a hash."""
    readable = re.sub(r"[^a-z0-9]+", "-", key.casefold()).strip("-")[:60]
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return f"{readable}-{digest}{suffix}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_fresh(created: str | None, max_age_days: float | None) -> bool:
    """True if ``created`` (ISO time) is younger than ``max_age_days``; None means forever."""
    if max_age_days is None:
        return True
    if not created:
        return False
    try:
        when = datetime.fromisoformat(created)
    except ValueError:
        log.warning("Bad cache timestamp %r; treating the entry as stale", created)
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when < timedelta(days=max_age_days)


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Any:
    """The JSON in ``path``, or None if missing; a corrupt file is logged and ignored."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("Ignoring unreadable cache file %s: %s", path, exc)
        return None


class CacheIndex:
    """Maps query keys to entries (plain JSON dicts) in ``<cache_dir>/index.json``.

    Each write re-reads the file and merges, so several sources (or processes
    sharing a cache) don't drop each other's entries.  ``created`` is set on
    every stored entry.
    """

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else cache_dir()
        self.path = self.root / INDEX_NAME

    def _load(self) -> dict[str, Any]:
        data = _read_json(self.path)
        if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
            if data is not None:
                log.warning("Cache index %s has an unknown format; starting a new one", self.path)
            return {"version": INDEX_VERSION, "entries": {}}
        data.setdefault("entries", {})
        return data

    def get(self, key: str, max_age_days: float | None = None) -> dict[str, Any] | None:
        """The entry for ``key``, or None if absent or older than ``max_age_days``."""
        with _lock:
            entry = self._load()["entries"].get(key)
        if entry is None or not is_fresh(entry.get("created"), max_age_days):
            return None
        return entry

    def put(self, key: str, entry: dict[str, Any]) -> dict[str, Any]:
        entry = dict(entry, created=now_iso())
        with _lock:
            data = self._load()
            data["entries"][key] = entry
            _write_json_atomic(self.path, data)
        return entry

    def remove(self, key: str) -> None:
        with _lock:
            data = self._load()
            if data["entries"].pop(key, None) is not None:
                _write_json_atomic(self.path, data)

    def keys(self) -> list[str]:
        with _lock:
            return sorted(self._load()["entries"])

    # Paths in entries are stored relative to the cache root.
    def relative(self, path: str | os.PathLike) -> str:
        p = Path(path).resolve()
        try:
            return p.relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return str(p)

    def absolute(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p


def save_json(namespace: str, key: str, data: Any, root: Path | None = None) -> Path:
    """Store ``data`` for ``key`` in ``<cache_dir>/<namespace>/``, stamped with the time."""
    base = Path(root) if root is not None else cache_dir()
    path = base / namespace / key_filename(key)
    _write_json_atomic(path, {"key": key, "created": now_iso(), "data": data})
    return path


def load_json(namespace: str, key: str, max_age_days: float | None = None,
              root: Path | None = None) -> tuple[Any, str] | None:
    """``(data, created)`` stored by ``save_json``, or None if absent or too old."""
    base = Path(root) if root is not None else cache_dir()
    stored = _read_json(base / namespace / key_filename(key))
    if not isinstance(stored, dict) or stored.get("key") != key:
        return None
    if not is_fresh(stored.get("created"), max_age_days):
        return None
    return stored.get("data"), stored.get("created", "")
