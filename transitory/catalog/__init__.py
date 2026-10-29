"""Star names, sky catalogs and published planets (MAST, NASA Exoplanet Archive).

Everything goes through a cached :class:`~.client.CatalogClient`; no Qt.
``resolve`` here is the module; the function is ``resolve.resolve``.
"""

from . import archive, client, resolve, skycatalog, tic
from .archive import ARCHIVE_CREDIT, ArchivePlanet, ArchiveResult, all_signals, known_planets
from .client import CatalogClient, CatalogError, default_client
from .resolve import ResolveError, parse_name, resolve_many, split_names
from .skycatalog import SkyCatalog, SkyStar, field_stars, host_stars, offline_targets

__all__ = [
    "ARCHIVE_CREDIT", "ArchivePlanet", "ArchiveResult", "CatalogClient", "CatalogError",
    "ResolveError", "SkyCatalog", "SkyStar", "all_signals", "archive", "client", "default_client",
    "field_stars", "host_stars", "known_planets", "offline_targets", "parse_name", "resolve",
    "resolve_many", "skycatalog", "split_names", "tic",
]
